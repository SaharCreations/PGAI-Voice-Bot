"""Local, scenario-aware transcription of the final MP3. Never imports app.py."""
import argparse
import hashlib
import json
import math
import os
import re
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from evidence import EvidenceStore, ROOT, atomic_json, now

MODEL = 'medium.en'
SAMPLE_RATE = 16000
SCENARIO_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}')


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def safe_error_message(exc):
    """Return a bounded local diagnostic without URLs or credential-like values."""
    message = " ".join(str(exc).split()) or "Transcription failed"
    message = re.sub(r'https?://\S+', '[URL REDACTED]', message)
    message = re.sub(
        r'(?i)\b(?:authorization|api[_ -]?key|token|secret|password)\s*[:=]\s*\S+',
        '[REDACTED]',
        message,
    )
    return message[:500]


def decode_channels(path):
    """Decode without downmixing; retain silence and offsets on the MP3 timeline."""
    import av
    import numpy as np

    pieces = [[], []]
    cursor = 0
    with av.open(str(path)) as container:
        if len(container.streams.audio) != 1:
            raise ValueError('Expected one MP3 audio stream')
        stream = container.streams.audio[0]
        if stream.codec_context.name not in ('mp3', 'mp3float'):
            raise ValueError('Source must be an actual MP3')
        if len(stream.codec_context.layout.channels) != 2:
            raise ValueError('Speaker mapping requires a verified two-channel recording')
        origin = float((stream.start_time or 0) * stream.time_base)
        resampler = av.AudioResampler(format='fltp', layout=stream.codec_context.layout, rate=SAMPLE_RATE)

        def append(frame):
            nonlocal cursor
            values = frame.to_ndarray()
            if values.shape[0] != 2:
                raise ValueError('Decoded channel count changed')
            position = cursor if frame.pts is None else round((float(frame.pts * frame.time_base) - origin) * SAMPLE_RATE)
            # Pad timestamp gaps rather than removing silence. Trim decoder overlap.
            if position > cursor:
                for channel in range(2):
                    pieces[channel].append(np.zeros(position - cursor, dtype=np.float32))
                cursor = position
            skip = max(0, cursor - position)
            if skip < values.shape[1]:
                for channel in range(2):
                    pieces[channel].append(values[channel, skip:].copy())
                cursor += values.shape[1] - skip

        for frame in container.decode(stream):
            for output in resampler.resample(frame):
                append(output)
        for output in resampler.resample(None):
            append(output)
    if not cursor:
        raise ValueError('Recording has no decoded audio')
    channels = [np.concatenate(parts) for parts in pieces]
    if any(not np.isfinite(samples).all() for samples in channels):
        raise ValueError('Invalid decoded audio')
    if any(np.max(np.abs(samples)) < 1e-5 for samples in channels):
        raise ValueError('A recording channel is silent; speaker attribution needs review')
    if np.allclose(channels[0], channels[1], atol=1e-6):
        raise ValueError('Duplicated mono channels cannot establish speaker separation')
    return channels, {'channels': 2, 'sample_rate': SAMPLE_RATE,
                      'duration_seconds': cursor / SAMPLE_RATE,
                      'channel_indices': [0, 1], 'timeline': 'seconds from MP3 playback start'}


class LocalWhisper:
    def __init__(self, model_dir, threads):
        from ctranslate2 import get_supported_compute_types
        from faster_whisper import WhisperModel

        model_dir = Path(model_dir).resolve()
        required = ('model.bin', 'config.json', 'tokenizer.json')
        if not model_dir.is_dir() or any(not (model_dir / name).is_file() for name in required):
            raise ValueError('Local medium.en model files are missing; automatic downloads are disabled')
        if 'int8' not in get_supported_compute_types('cpu'):
            raise ValueError('CPU int8 is unsupported; no silent compute-type or model fallback')
        self.provenance = {
            'engine': 'faster-whisper', 'engine_version': version('faster-whisper'),
            'decoder': 'PyAV', 'decoder_version': version('av'),
            'model': MODEL, 'model_directory': str(model_dir),
            'model_files_sha256': {name: sha256(model_dir / name) for name in required},
            'device': 'cpu', 'compute_type': 'int8', 'cpu_threads': threads,
            'language': 'en', 'word_timestamps': True, 'vad_filter': False,
            'condition_on_previous_text': False, 'beam_size': 5, 'temperature': 0,
        }
        self.model = WhisperModel(str(model_dir), device='cpu', compute_type='int8',
                                  cpu_threads=threads, num_workers=1, local_files_only=True)

    def transcribe(self, samples):
        segments, _ = self.model.transcribe(
            samples, language='en', beam_size=5, temperature=0,
            word_timestamps=True, vad_filter=False, condition_on_previous_text=False,
        )
        for segment in segments:
            yield {'start': segment.start, 'end': segment.end, 'text': segment.text.strip(),
                   'words': [{'start': word.start, 'end': word.end, 'text': word.word}
                             for word in (segment.words or [])]}


def verified_mapping(metadata, source_hash, supplied):
    mapping = supplied if supplied is not None else metadata.get('transcript', {}).get('channel_mapping')
    if not isinstance(mapping, dict) or mapping.get('verified') is not True:
        raise ValueError('Verified channel mapping is required; channel identity is never guessed')
    if not isinstance(mapping.get('basis'), str) or not mapping['basis'].strip():
        raise ValueError('Describe how the channel mapping was verified against this recording')
    if mapping.get('source_sha256') != source_hash:
        raise ValueError('Channel mapping is not verified for this MP3 hash')
    speakers = mapping.get('speakers')
    if not isinstance(speakers, dict) or set(speakers) != {'0', '1'}:
        raise ValueError('Map both zero-based recording channels explicitly')
    if any(not isinstance(label, str) or not label.strip() or '\n' in label or '\r' in label for label in speakers.values()):
        raise ValueError('Invalid speaker labels')
    if len(set(speakers.values())) != 2 or 'PGAI Clinic Agent' not in speakers.values():
        raise ValueError('Map distinct patient and clinic speakers')
    if not any(label.startswith('Patient / ') and label.removeprefix('Patient / ').strip() for label in speakers.values()):
        raise ValueError('Patient label must explicitly supply the patient name')
    return {**mapping, 'speakers': dict(speakers)}


def timestamp(seconds):
    milliseconds = round(seconds * 1000)
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, milliseconds = divmod(rest, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}'


def valid_interval(start, end, duration):
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in (start, end)) or start < 0 or end < start or end > duration + 0.1:
        raise ValueError('Recognizer returned invalid timestamps')


def _transcribe_scenario(scenario_id, *, root=ROOT, model_dir=None, mapping=None,
                         regenerate=False, threads=None, decoder=decode_channels,
                         engine_factory=LocalWhisper):
    if not isinstance(scenario_id, str) or not SCENARIO_ID.fullmatch(scenario_id):
        raise ValueError('Scenario ID must be a single safe directory name')
    root = Path(root).resolve()
    directory = root / scenario_id
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Scenario evidence directory does not exist or is a symlink')
    source, target = directory / 'recording.mp3', directory / 'transcript.txt'
    metadata_path = directory / 'metadata.json'
    if any(path.is_symlink() for path in (source, target, metadata_path)):
        raise ValueError('Evidence files must not be symlinks')
    if threads is None:
        threads = min(4, os.cpu_count() or 1)
    if not isinstance(threads, int) or not 1 <= threads <= 64:
        raise ValueError('CPU threads must be between 1 and 64')

    # Match the evidence writer's cross-process lock. Never import or invoke dialing.
    with EvidenceStore(root).locked(directory):
        metadata = json.loads(metadata_path.read_text())
        if metadata.get('scenario_id') != scenario_id:
            raise ValueError('Scenario identity does not match metadata')
        old = metadata.get('transcript', {})
        if not regenerate and (old.get('status') == 'completed' or (target.exists() and target.stat().st_size)):
            raise ValueError('Transcript already exists; explicit --regenerate is required')
        if metadata.get('artifact', {}).get('status') != 'downloaded':
            raise ValueError('Final recording download is not complete')
        source_hash = sha256(source)
        if source_hash != metadata['artifact'].get('sha256'):
            raise ValueError('MP3 SHA-256 does not match downloaded evidence')
        # CLI mapping is explicitly verified by a human; tie it to these exact bytes.
        if mapping is not None:
            mapping = {**mapping, 'source_sha256': mapping.get('source_sha256', source_hash)}
        mapping = verified_mapping(metadata, source_hash, mapping)
        audio, audio_info = decoder(source)
        if len(audio) != 2 or audio_info.get('channels') != 2:
            raise ValueError('Expected two separately decoded channels')
        engine = engine_factory(model_dir or Path(__file__).resolve().parent / '.models' / MODEL, threads)
        segments = []
        for channel, samples in enumerate(audio):
            for result in engine.transcribe(samples):
                start, end = result['start'], result['end']
                valid_interval(start, end, audio_info['duration_seconds'])
                text = result['text'].strip()
                if not text:
                    continue
                words = result.get('words', [])
                for word in words:
                    valid_interval(word['start'], word['end'], audio_info['duration_seconds'])
                segments.append({**result, 'text': ' '.join(text.split()), 'channel_index': channel,
                                 'speaker': mapping['speakers'][str(channel)]})
        segments.sort(key=lambda item: (item['start'], item['channel_index'], item['end']))
        if not segments:
            raise ValueError('No speech recognized; no completed transcript written')
        if sha256(source) != source_hash:
            raise ValueError('MP3 changed during transcription; refusing to save')
        lines = [f'Scenario: {scenario_id}', 'Source: recording.mp3', f'Source SHA-256: {source_hash}',
                 'Timestamps: HH:MM:SS.mmm from MP3 playback start; overlaps are retained.',
                 f'Channel 0: {mapping["speakers"]["0"]}', f'Channel 1: {mapping["speakers"]["1"]}',
                 'Machine transcript from recorded audio; review against audio for evidence accuracy.', '']
        def label(item):
            return 'PGAI' if item['speaker'] == 'PGAI Clinic Agent' else 'PATIENT'

        lines.extend(
            f'[{timestamp(item["start"])} - {timestamp(item["end"])}] '
            f'{label(item)}: {item["text"]} [channel {item["channel_index"]}]'
            for item in segments
        )
        content = '\n'.join(lines) + '\n'
        completed = {
            'status': 'completed', 'path': 'transcript.txt', 'required_source': 'recording.mp3',
            'source_sha256': source_hash, 'sha256': hashlib.sha256(content.encode()).hexdigest(),
            'completed_at': now(), 'channel_mapping': mapping, 'audio': audio_info,
            'recognizer': engine.provenance, 'segments': segments,
            'recording_id': metadata.get('recording', {}).get('recording_id'),
            'call': metadata.get('call', {}), 'attempt_id': metadata.get('attempt_id'),
            'review_status': 'needs_audio_review',
        }
        temporary = directory / f'.transcript.{uuid4().hex}.tmp'
        try:
            with temporary.open('x') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            # Write pending commit provenance first; a crash never silently replaces a
            # completed transcript. Nonempty output still requires --regenerate.
            metadata['transcript'] = {**old, 'pending_commit': completed}
            atomic_json(metadata_path, metadata)
            temporary.replace(target)
            metadata['transcript'] = completed
            atomic_json(metadata_path, metadata)
        finally:
            temporary.unlink(missing_ok=True)
        return completed


def _mark_transcription_error(scenario_id, root, exc, mapping):
    if not isinstance(scenario_id, str) or not SCENARIO_ID.fullmatch(scenario_id):
        return
    root = Path(root).resolve()
    directory = root / scenario_id
    metadata_path = directory / 'metadata.json'
    source = directory / 'recording.mp3'
    if directory.is_symlink() or not directory.is_dir() or not metadata_path.is_file():
        return
    with EvidenceStore(root).locked(directory):
        metadata = json.loads(metadata_path.read_text())
        if metadata.get('scenario_id') != scenario_id:
            return
        old = metadata.get('transcript', {})
        if old.get('status') == 'completed':
            return
        failed = {
            **old,
            'status': 'error',
            'path': 'transcript.txt',
            'required_source': 'recording.mp3',
            'failed_at': now(),
            'error_type': type(exc).__name__,
            'error_message': safe_error_message(exc),
        }
        failed.pop('pending_commit', None)
        source_hash = metadata.get('artifact', {}).get('sha256')
        if isinstance(source_hash, str):
            failed['source_sha256'] = source_hash
        if mapping is not None and isinstance(source_hash, str) and source.is_file():
            candidate = {**mapping, 'source_sha256': mapping.get('source_sha256', source_hash)}
            try:
                failed['channel_mapping'] = verified_mapping(metadata, source_hash, candidate)
            except ValueError:
                pass
        metadata['transcript'] = failed
        atomic_json(metadata_path, metadata)


def transcribe_scenario(scenario_id, *, root=ROOT, model_dir=None, mapping=None,
                        regenerate=False, threads=None, decoder=decode_channels,
                        engine_factory=LocalWhisper):
    try:
        return _transcribe_scenario(
            scenario_id,
            root=root,
            model_dir=model_dir,
            mapping=mapping,
            regenerate=regenerate,
            threads=threads,
            decoder=decoder,
            engine_factory=engine_factory,
        )
    except Exception as exc:
        try:
            _mark_transcription_error(scenario_id, root, exc, mapping)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario_id', help='Evidence directory ID, including follow-up IDs')
    parser.add_argument('--model-dir', type=Path, default=Path(__file__).resolve().parent / '.models' / MODEL)
    parser.add_argument('--threads', type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument('--patient-channel', type=int, choices=(0, 1))
    parser.add_argument('--clinic-channel', type=int, choices=(0, 1))
    parser.add_argument('--patient-name')
    parser.add_argument('--mapping-basis', help='How you verified speaker identity from this MP3')
    parser.add_argument('--verify-mapping', action='store_true', help='Attest that you checked this audio/channel mapping')
    parser.add_argument('--regenerate', action='store_true', help='Explicitly replace an existing transcript after successful transcription')
    args = parser.parse_args(argv)
    mapping = None
    supplied = (args.patient_channel, args.clinic_channel, args.patient_name, args.mapping_basis)
    if any(value is not None for value in supplied) or args.verify_mapping:
        if not args.verify_mapping or any(value is None for value in supplied) or args.patient_channel == args.clinic_channel:
            parser.error('Supply both distinct channels, patient name, mapping basis, and --verify-mapping together')
        mapping = {'verified': True, 'basis': args.mapping_basis, 'verified_at': now(),
                   'speakers': {str(args.patient_channel): f'Patient / {args.patient_name}',
                                str(args.clinic_channel): 'PGAI Clinic Agent'}}
    try:
        result = transcribe_scenario(args.scenario_id, model_dir=args.model_dir, mapping=mapping,
                                     regenerate=args.regenerate, threads=args.threads)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f'Transcription not completed: {exc}\n')
    print(f'Saved evidence/{args.scenario_id}/transcript.txt ({len(result["segments"])} segments)')


if __name__ == '__main__':
    main()
