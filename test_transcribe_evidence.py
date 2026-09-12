"""Offline stereo MP3 decoding plus mocked recognition; no speech model downloads."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import av
import numpy as np

from evidence import EvidenceStore, atomic_json
from transcribe_evidence import LocalWhisper, decode_channels, sha256, transcribe_scenario, main

PROJECT = Path(__file__).resolve().parent


def write_fixture(path, channels=2):
    """Generate tones with silence and distinct channel frequencies, not fake speech."""
    rate = 24000
    t = np.arange(rate * 3) / rate
    left = (0.2 * np.sin(2 * np.pi * 330 * t) * ((t >= .4) & (t < 1.4))).astype('float32')
    right = (0.2 * np.sin(2 * np.pi * 660 * t) * ((t >= 1.0) & (t < 2.4))).astype('float32')
    samples = np.stack([left, right] if channels == 2 else [left])
    with av.open(str(path), 'w', format='mp3') as container:
        stream = container.add_stream('libmp3lame', rate=rate)
        stream.layout = 'stereo' if channels == 2 else 'mono'
        frame = av.AudioFrame.from_ndarray(samples, format='fltp', layout=stream.layout.name)
        frame.sample_rate = rate
        frame.pts = 0
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


class FakeEngine:
    provenance = {'engine': 'offline-mock', 'model': 'medium.en', 'device': 'cpu', 'compute_type': 'int8'}

    def __init__(self):
        self.received = []

    def transcribe(self, samples):
        channel = len(self.received)
        self.received.append(samples.copy())
        start, end = ((.4, 1.4) if channel == 0 else (1.0, 2.4))
        return [{'start': start, 'end': end, 'text': f'Audio fixture channel {channel}',
                 'words': [{'start': start, 'end': end, 'text': 'fixture'}]}]


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='.test-transcription-', dir=PROJECT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'evidence'
        self.store = EvidenceStore(self.root)
        self.store.reserve('scenario_01')
        self.directory = self.root / 'scenario_01'
        self.source = self.directory / 'recording.mp3'
        write_fixture(self.source)
        metadata = self.metadata()
        metadata['artifact'].update(status='downloaded', sha256=sha256(self.source))
        metadata['recording']['recording_id'] = 'offline-recording'
        atomic_json(self.directory / 'metadata.json', metadata)
        self.mapping = {'verified': True, 'basis': 'Offline fixture channels constructed explicitly',
                        'speakers': {'0': 'Patient / Meredith White', '1': 'PGAI Clinic Agent'}}
        self.engine = FakeEngine()
        self.factory = Mock(return_value=self.engine)
        self.network = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.hub = patch('huggingface_hub.snapshot_download', side_effect=AssertionError('Model download forbidden'))
        self.hub.start()
        self.addCleanup(self.hub.stop)

    def metadata(self):
        return json.loads((self.directory / 'metadata.json').read_text())

    def run_transcription(self, **kwargs):
        return transcribe_scenario('scenario_01', root=self.root, engine_factory=self.factory,
                                   mapping=kwargs.pop('mapping', self.mapping), **kwargs)

    def test_stereo_decode_preserves_channels_silence_and_timeline(self):
        channels, info = decode_channels(self.source)
        self.assertAlmostEqual(info['duration_seconds'], 3, delta=.05)
        self.assertEqual(info['channel_indices'], [0, 1])
        self.assertEqual(len(channels[0]), len(channels[1]))
        for index, expected_start in enumerate((.4, 1.0)):
            active = np.flatnonzero(np.abs(channels[index]) > .04)
            self.assertAlmostEqual(active[0] / 16000, expected_start, delta=.03)
        # The channel with 330 Hz must remain channel 0 after MP3 decode/resampling.
        for index, (start, end, hz) in enumerate(((.5, 1.2, 330), (1.5, 2.1, 660))):
            window = channels[index][int(start * 16000):int(end * 16000)]
            frequencies = np.fft.rfftfreq(len(window), 1 / 16000)
            self.assertAlmostEqual(frequencies[np.abs(np.fft.rfft(window)).argmax()], hz, delta=3)

    def test_transcript_provenance_overlap_and_original_unchanged(self):
        before = self.source.read_bytes()
        result = self.run_transcription()
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(result['source_sha256'], sha256(self.source))
        self.assertEqual(result['channel_mapping']['source_sha256'], sha256(self.source))
        self.assertIn('constructed explicitly', result['channel_mapping']['basis'])
        self.assertEqual([segment['channel_index'] for segment in result['segments']], [0, 1])
        self.assertLess(result['segments'][1]['start'], result['segments'][0]['end'])
        content = (self.directory / 'transcript.txt').read_text()
        patient = '[00:00:00.400 - 00:00:01.400] PATIENT: Audio fixture channel 0 [channel 0]'
        clinic = '[00:00:01.000 - 00:00:02.400] PGAI: Audio fixture channel 1 [channel 1]'
        self.assertIn(patient, content)
        self.assertIn(clinic, content)
        self.assertLess(content.index(patient), content.index(clinic))
        self.assertEqual(result['sha256'], sha256(self.directory / 'transcript.txt'))
        self.assertEqual(self.metadata()['transcript']['status'], 'completed')
        self.assertEqual(len(self.engine.received), 2)

    def test_hash_mismatch_stops_before_recognition(self):
        self.source.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.run_transcription()
        self.factory.assert_not_called()
        self.assertEqual((self.directory / 'transcript.txt').read_text(), '')
        self.assertEqual(self.metadata()['transcript']['status'], 'error')
        self.assertEqual(self.metadata()['transcript']['error_type'], 'ValueError')

    def test_empty_transcript_is_error_and_never_completed(self):
        self.engine.transcribe = Mock(return_value=[])
        with self.assertRaisesRegex(ValueError, 'No speech recognized'):
            self.run_transcription()
        transcript = self.metadata()['transcript']
        self.assertEqual(transcript['status'], 'error')
        self.assertEqual(transcript['required_source'], 'recording.mp3')
        self.assertEqual(transcript['source_sha256'], sha256(self.source))
        self.assertEqual(transcript['channel_mapping']['speakers'], self.mapping['speakers'])
        self.assertEqual((self.directory / 'transcript.txt').read_text(), '')

    def test_no_mapping_and_unverified_mapping_are_rejected(self):
        for mapping in (None, {**self.mapping, 'verified': False}, {**self.mapping, 'basis': ''},
                        {**self.mapping, 'source_sha256': 'different'},
                        {**self.mapping, 'speakers': {'0': 'Meredith'}}):
            with self.subTest(mapping=mapping):
                with self.assertRaises(ValueError):
                    self.run_transcription(mapping=mapping)
        self.factory.assert_not_called()

    def test_reversed_verified_mapping_is_respected(self):
        mapping = {**self.mapping, 'speakers': {'0': 'PGAI Clinic Agent', '1': 'Patient / Meredith White'}}
        result = self.run_transcription(mapping=mapping)
        self.assertEqual(result['segments'][0]['speaker'], 'PGAI Clinic Agent')
        self.assertEqual(result['segments'][1]['speaker'], 'Patient / Meredith White')

    def test_mono_rejected_without_guessing(self):
        write_fixture(self.source, channels=1)
        metadata = self.metadata()
        metadata['artifact']['sha256'] = sha256(self.source)
        atomic_json(self.directory / 'metadata.json', metadata)
        with self.assertRaisesRegex(ValueError, 'two-channel'):
            self.run_transcription()
        self.factory.assert_not_called()

    def test_completion_protected_and_explicit_regeneration_uses_saved_mapping(self):
        self.run_transcription()
        before = (self.directory / 'transcript.txt').read_bytes()
        with self.assertRaisesRegex(ValueError, '--regenerate'):
            self.run_transcription()
        self.engine.received.clear()
        result = self.run_transcription(regenerate=True, mapping=None)
        self.assertEqual(result['channel_mapping']['speakers'], self.mapping['speakers'])
        self.assertEqual((self.directory / 'transcript.txt').read_bytes(), before)

    def test_failed_regeneration_preserves_completed_files(self):
        self.run_transcription()
        before = {name: (self.directory / name).read_bytes() for name in ('transcript.txt', 'metadata.json')}
        self.factory.side_effect = RuntimeError('mock failure')
        with self.assertRaises(RuntimeError):
            self.run_transcription(regenerate=True)
        for name, data in before.items():
            self.assertEqual((self.directory / name).read_bytes(), data)

    def test_existing_nonempty_transcript_is_not_replaced_even_without_status(self):
        (self.directory / 'transcript.txt').write_text('Existing transcript')
        with self.assertRaises(ValueError):
            self.run_transcription()
        self.assertEqual((self.directory / 'transcript.txt').read_text(), 'Existing transcript')

    def test_all_scenarios_and_followup_ids_are_generic(self):
        for identifier in ('scenario_15', 'scenario_01_followup_01'):
            directory = self.root / identifier
            directory.mkdir()
            (directory / 'recording.mp3').write_bytes(self.source.read_bytes())
            metadata = self.metadata()
            metadata['scenario_id'] = identifier
            atomic_json(directory / 'metadata.json', metadata)
            engine = FakeEngine()
            result = transcribe_scenario(identifier, root=self.root, mapping=self.mapping,
                                         engine_factory=lambda *args: engine)
            self.assertEqual(result['status'], 'completed')
            self.assertTrue((directory / 'transcript.txt').exists())
        self.assertEqual((self.directory / 'transcript.txt').read_text(), '')

    def test_path_traversal_and_metadata_mismatch(self):
        with self.assertRaises(ValueError):
            transcribe_scenario('../scenario_01', root=self.root)
        metadata = self.metadata()
        metadata['scenario_id'] = 'scenario_15'
        atomic_json(self.directory / 'metadata.json', metadata)
        with self.assertRaises(ValueError):
            self.run_transcription()

    def test_invalid_timestamps_do_not_write_output(self):
        self.engine.transcribe = Mock(return_value=[{'start': float('nan'), 'end': 2, 'text': 'bad'}])
        with self.assertRaises(ValueError):
            self.run_transcription()
        self.assertEqual((self.directory / 'transcript.txt').read_text(), '')

    def test_whisper_local_only_medium_int8_configuration(self):
        model = Path(self.temporary.name) / 'medium.en'
        model.mkdir()
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            (model / name).write_text('{}')
        with patch('ctranslate2.get_supported_compute_types', return_value={'int8'}), \
             patch('faster_whisper.WhisperModel') as constructor:
            backend = LocalWhisper(model, 4)
            constructor.assert_called_once_with(str(model.resolve()), device='cpu', compute_type='int8',
                                                cpu_threads=4, num_workers=1, local_files_only=True)
            self.assertEqual(backend.provenance['model'], 'medium.en')

    def test_missing_model_and_unsupported_int8_have_no_fallback(self):
        with patch('faster_whisper.WhisperModel') as constructor:
            with self.assertRaisesRegex(ValueError, 'missing'):
                LocalWhisper(Path(self.temporary.name) / 'absent', 4)
            constructor.assert_not_called()

            model = Path(self.temporary.name) / 'medium.en'
            model.mkdir()
            for name in ('model.bin', 'config.json', 'tokenizer.json'):
                (model / name).write_text('{}')
            with patch('ctranslate2.get_supported_compute_types', return_value={'float32'}) as supported:
                with self.assertRaisesRegex(ValueError, 'CPU int8 is unsupported'):
                    LocalWhisper(model, 4)
                supported.assert_called_once_with('cpu')
            constructor.assert_not_called()

    def test_cli_requires_verified_mapping_and_accepts_any_safe_scenario(self):
        with patch('transcribe_evidence.transcribe_scenario', return_value={'segments': []}) as run:
            main(['scenario_15'])
            self.assertEqual(run.call_args.args[0], 'scenario_15')
            with self.assertRaises(SystemExit):
                main(['scenario_15', '--patient-channel', '0'])
