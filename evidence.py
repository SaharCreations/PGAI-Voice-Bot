"""Local Telnyx evidence storage. No calls are initiated by this module."""
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parent / 'evidence'
SCENARIO = re.compile(r'scenario_[0-9]{2,}')
CALL_IDS = ('call_control_id', 'call_leg_id', 'call_session_id', 'connection_id')
EVENTS = {
    'call.initiated', 'call.answered', 'call.hangup', 'call.conversation.ended',
    'call.recording.saved', 'call.recording.error',
}
RECORDING_OPTIONS = {
    'record': 'record-from-answer', 'record_channels': 'dual',
    'record_track': 'both', 'record_format': 'mp3',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    try:
        with temporary.open('x') as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def public_url(url):
    """Keep provenance but never put signed query credentials in submission JSON."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.hostname or '', parts.path, '', ''))


def download_mp3(url, destination):
    """Download only an HTTPS recording from Telnyx-managed storage, without auth headers."""
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    allowed = any(host == suffix or host.endswith('.' + suffix)
                  for suffix in ('amazonaws.com', 'telnyx.com'))
    if parsed.scheme != 'https' or not allowed or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Unsupported recording URL')
    # Reject private/local DNS destinations; redirects and environment proxies are disabled.
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('Non-public recording destination')
    digest = hashlib.sha256()
    size = 0
    prefix = b''
    started = time.monotonic()
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
        with client.stream('GET', url) as response, destination.open('xb') as output:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('Unexpected recording response')
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 100 * 1024 * 1024 or time.monotonic() - started > 45:
                    raise ValueError('Recording download limit exceeded')
                prefix = (prefix + chunk)[:12]
                output.write(chunk)
                digest.update(chunk)
    # Reject WAV/HTML/error bodies. This is a format sanity check, not a decoder.
    if size < 3 or not (prefix.startswith(b'ID3') or (prefix[0] == 255 and prefix[1] & 224 == 224)):
        raise ValueError('Recording is not MP3')
    return {'sha256': digest.hexdigest(), 'size_bytes': size}


class EvidenceStore:
    def __init__(self, root=ROOT, downloader=download_mp3):
        self.root = Path(root)
        self.downloader = downloader

    def directory(self, scenario):
        if not isinstance(scenario, str) or not SCENARIO.fullmatch(scenario):
            raise ValueError('Expected scenario_NN')
        directory = self.root / scenario
        if directory.is_symlink():
            raise ValueError('Scenario must not be a symlink')
        return directory

    @contextmanager
    def submission_lock(self):
        # Outside final evidence: serialize acceptance/publication and early callbacks.
        self.root.parent.mkdir(parents=True, exist_ok=True)
        with (self.root.parent / f'.{self.root.name}-submission.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def publish_accepted(self, scenario, attempt_id, persona, payload):
        """Called under submission_lock only after a valid accepted dial response."""
        destination = self.directory(scenario)
        if destination.exists():
            raise FileExistsError(destination)
        with tempfile.TemporaryDirectory(prefix='.evidence-stage-', dir=self.root.parent) as temporary:
            staged = EvidenceStore(Path(temporary))
            metadata = staged.reserve(scenario, persona=persona)
            metadata['attempt_id'] = attempt_id
            self.merge_ids(metadata, payload)
            metadata['dial_status'] = 'submitted'
            metadata['timestamps']['dial_response_at'] = now()
            atomic_json(Path(temporary) / scenario / 'metadata.json', metadata)
            self.root.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise FileExistsError(destination)
            (Path(temporary) / scenario).rename(destination)
        return metadata

    @contextmanager
    def locked(self, directory):
        with (directory / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def reserve(self, scenario=None, *, persona=None):
        """One attempt per scenario; atomic mkdir prevents silent evidence replacement."""
        self.root.mkdir(parents=True, exist_ok=True)
        if scenario is None:
            number = 1
            while True:
                scenario = f'scenario_{number:02d}'
                directory = self.directory(scenario)
                try:
                    directory.mkdir()
                    break
                except FileExistsError:
                    number += 1
        else:
            directory = self.directory(scenario)
            directory.mkdir()
        metadata = {
            'schema_version': 1, 'scenario_id': scenario, 'attempt_id': uuid4().hex,
            'created_at': now(), 'call_status': 'prepared', 'dial_status': 'prepared',
            'call': {key: None for key in CALL_IDS},
            'timestamps': {}, 'events': [], 'recording_options': dict(RECORDING_OPTIONS),
            'recording': {'status': 'requested', 'recording_id': None, 'urls': {}},
            'artifact': {'path': 'recording.mp3', 'status': 'pending', 'format': 'mp3'},
            'transcript': {'path': 'transcript.txt', 'status': 'pending',
                           'required_source': 'recording.mp3'},
            'limitations': ['Interrupted assistant speech is omitted from conversation history.'],
        }
        if persona is not None:
            metadata['persona'] = dict(persona)
        # An empty placeholder is not a generated transcript. Never invent audio bytes.
        (directory / 'transcript.txt').touch(exist_ok=False)
        atomic_json(directory / 'metadata.json', metadata)
        return metadata

    @staticmethod
    def client_state(metadata):
        value = {key: metadata[key] for key in ('scenario_id', 'attempt_id')}
        value['evidence_version'] = 1
        return base64.b64encode(json.dumps(value).encode()).decode()

    def relay_metadata(self, setup):
        with self.submission_lock():
            return self._relay_metadata(setup)

    def _relay_metadata(self, setup):
        """Read a pre-bound scenario using the relay's setup custom parameters.

        This is association validation, not WebSocket sender authentication.
        Unlike signed webhooks, setup never writes call IDs to evidence.
        """
        parameters = setup.get('customParameters')
        if not isinstance(parameters, dict):
            raise ValueError('Missing relay scenario association')
        directory = self.directory(parameters.get('scenario_id'))
        with self.locked(directory):
            metadata = json.loads((directory / 'metadata.json').read_text())
        if parameters.get('attempt_id') != metadata['attempt_id']:
            raise ValueError('Relay attempt does not match evidence')
        if not isinstance(setup.get('callControlId'), str) or not setup['callControlId']:
            raise ValueError('Missing relay call control ID')
        for field, wire_name in (('call_control_id', 'callControlId'),
                                 ('call_leg_id', 'callLegId'),
                                 ('call_session_id', 'callSessionId')):
            known = metadata['call'][field]
            if known is not None and setup.get(wire_name) != known:
                raise ValueError('Relay call identifiers do not match evidence')
        return metadata

    @staticmethod
    def merge_ids(metadata, payload):
        for key in CALL_IDS:
            value = payload.get(key)
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise ValueError('Invalid call identifier')
                known = metadata['call'][key]
                if known is not None and known != value:
                    raise ValueError('Call identifier conflicts with scenario')
                metadata['call'][key] = value
        recording_id = payload.get('recording_id')
        if recording_id is not None:
            if not isinstance(recording_id, str) or not recording_id:
                raise ValueError('Invalid recording identifier')
            known = metadata['recording']['recording_id']
            if known is not None and known != recording_id:
                raise ValueError('Recording identifier conflicts with scenario')
            metadata['recording']['recording_id'] = recording_id
            if metadata['artifact']['status'] == 'downloaded':
                metadata['artifact']['recording_id'] = recording_id

    def record_dial_result(self, scenario, payload=None, failed=False):
        directory = self.directory(scenario)
        with self.locked(directory):
            metadata = json.loads((directory / 'metadata.json').read_text())
            self.merge_ids(metadata, payload or {})
            # A failed HTTP response does not prove Telnyx did not create a call.
            metadata['dial_status'] = 'outcome_unknown' if failed else 'submitted'
            metadata['timestamps']['dial_response_at'] = now()
            atomic_json(directory / 'metadata.json', metadata)

    def resolve(self, payload):
        with self.submission_lock():
            return self._resolve(payload)

    def _resolve(self, payload):
        state = payload.get('client_state')
        if state:
            try:
                decoded = json.loads(base64.b64decode(state, validate=True))
                if not isinstance(decoded, dict) or decoded.get('evidence_version') != 1:
                    return None
                directory = self.directory(decoded.get('scenario_id'))
                metadata = json.loads((directory / 'metadata.json').read_text())
                if decoded.get('attempt_id') != metadata['attempt_id']:
                    return None
                return directory
            except (ValueError, TypeError, OSError):
                return None
        # Some lifecycle frames omit client_state; only match known call/leg IDs.
        matches = []
        for path in self.root.glob('scenario_*/metadata.json'):
            metadata = json.loads(path.read_text())
            if any(payload.get(key) and metadata['call'].get(key) == payload[key]
                   for key in ('call_control_id', 'call_leg_id')):
                matches.append(self.directory(metadata['scenario_id']))
        return matches[0] if len(matches) == 1 else None

    def handle(self, event):
        data = event.get('data') if isinstance(event, dict) else None
        if not isinstance(data, dict):
            raise ValueError('Invalid webhook envelope')
        kind = data.get('event_type')
        if kind not in EVENTS:
            return 'ignored'
        payload = data.get('payload')
        if not isinstance(payload, dict) or not isinstance(data.get('id'), str) or not data['id']:
            raise ValueError('Missing webhook payload or event ID')
        occurred = data.get('occurred_at')
        if not isinstance(occurred, str):
            raise ValueError('Missing event timestamp')
        parsed = datetime.fromisoformat(occurred.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Timestamp must include timezone')
        directory = self.resolve(payload)
        if directory is None:
            return 'unmatched'
        with self.locked(directory):
            metadata = json.loads((directory / 'metadata.json').read_text())
            self.merge_ids(metadata, payload)
            duplicate = any(item['id'] == data['id'] for item in metadata['events'])
            if not duplicate:
                metadata['events'].append({'id': data['id'], 'type': kind, 'occurred_at': occurred})
                times = metadata['timestamps']
                field = {'call.initiated': 'initiated_at', 'call.answered': 'answered_at',
                         'call.hangup': 'ended_at', 'call.conversation.ended': 'relay_ended_at'}.get(kind)
                if field:
                    previous = times.get(field)
                    # Deterministic if distinct events for the same phase arrive out of order.
                    if previous is None or parsed < datetime.fromisoformat(previous.replace('Z', '+00:00')):
                        times[field] = occurred
                metadata['call_status'] = ('ended' if 'ended_at' in times else
                                           'answered' if 'answered_at' in times else
                                           'initiated' if 'initiated_at' in times else 'prepared')
                if kind == 'call.hangup':
                    metadata['hangup_cause'] = payload.get('hangup_cause')
                if kind == 'call.conversation.ended':
                    metadata['relay_end_reason'] = payload.get('reason')
                recording = metadata['recording']
                if kind == 'call.recording.error':
                    recording['errors'] = recording.get('errors', []) + [
                        {'event_id': data['id'], 'occurred_at': occurred, 'reason': payload.get('reason')}]
                    if recording['status'] != 'saved':
                        recording['status'] = 'error'
                if kind == 'call.recording.saved':
                    recording['status'] = 'saved'
                    # Refresh expired source links only with newer provider events.
                    prior = recording.get('saved_event_at')
                    if prior is None or parsed >= datetime.fromisoformat(prior.replace('Z', '+00:00')):
                        for key in ('recording_started_at', 'recording_ended_at', 'channels'):
                            if payload.get(key) is not None:
                                recording[key] = payload[key]
                        signed = payload.get('recording_urls') or {}
                        public = payload.get('public_recording_urls') or {}
                        if not isinstance(signed, dict) or not isinstance(public, dict):
                            raise ValueError('Invalid recording URLs')
                        urls = {key: value for key, value in {**public, **signed}.items() if value is not None}
                        if any(not isinstance(v, str) for v in urls.values()):
                            raise ValueError('Invalid recording URLs')
                        recording['urls'] = {key: public_url(value) for key, value in urls.items()}
                        recording['saved_event_at'] = occurred
                        recording['saved_event_id'] = data['id']
                        atomic_json(directory / '.recording-source.json', {'urls': urls})
            atomic_json(directory / 'metadata.json', metadata)
            if kind == 'call.recording.saved' and metadata['artifact']['status'] != 'downloaded':
                return self.save_audio(directory, metadata)
            return 'duplicate' if duplicate else 'stored'

    def save_audio(self, directory, metadata):
        source = json.loads((directory / '.recording-source.json').read_text())
        url = source['urls'].get('mp3')
        if not url:
            metadata['artifact']['status'] = 'missing_mp3_url'
            atomic_json(directory / 'metadata.json', metadata)
            return 'missing_mp3_url'
        temporary = directory / f'.recording.{uuid4().hex}.tmp'
        try:
            details = self.downloader(url, temporary)
            temporary.replace(directory / 'recording.mp3')
            metadata['artifact'].update(details)
            metadata['artifact'].update(status='downloaded', downloaded_at=now(),
                                        source_event_id=metadata['recording']['saved_event_id'],
                                        recording_id=metadata['recording']['recording_id'])
            metadata['artifact'].pop('error_type', None)
        except Exception as exc:
            metadata['artifact'].update(status='download_failed', error_type=type(exc).__name__)
        finally:
            temporary.unlink(missing_ok=True)
        atomic_json(directory / 'metadata.json', metadata)
        return metadata['artifact']['status']
