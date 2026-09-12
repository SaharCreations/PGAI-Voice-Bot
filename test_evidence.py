"""Offline evidence tests. Never invoke dial_test or real provider endpoints."""
import ast
import asyncio
import base64
import hashlib
import importlib
import json
import socket
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import httpx
from fastapi import HTTPException
from telnyx import TelnyxError

from evidence import EvidenceStore, RECORDING_OPTIONS, download_mp3

with patch('dotenv.load_dotenv'), patch.dict('os.environ', {}, clear=True):
    app_module = importlib.import_module('app')

PROJECT = Path(__file__).resolve().parent
AUDIO = b'ID3' + b'offline fixture bytes'  # Format fixture, not a claimed playable recording.


def fake_download(url, destination):
    destination.write_bytes(AUDIO)
    return {'sha256': hashlib.sha256(AUDIO).hexdigest(), 'size_bytes': len(AUDIO)}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='.test-evidence-', dir=PROJECT)
        self.addCleanup(self.temp.cleanup)
        self.downloader = Mock(side_effect=fake_download)
        self.store = EvidenceStore(Path(self.temp.name) / 'evidence', self.downloader)
        self.reservation = self.store.reserve()
        self.directory = self.store.root / 'scenario_01'

    def metadata(self, directory=None):
        return json.loads(((directory or self.directory) / 'metadata.json').read_text())

    def event(self, kind='call.recording.saved', event_id='event-1', reservation=None, **payload):
        reservation = reservation or self.reservation
        data = {
            'client_state': self.store.client_state(reservation),
            'call_control_id': 'control-1', 'call_leg_id': 'leg-1',
            'call_session_id': 'session-1', 'connection_id': 'connection-1',
        }
        if kind == 'call.recording.saved':
            data.update(recording_id='recording-1', channels='dual',
                        recording_started_at='2026-09-09T12:00:00Z',
                        recording_ended_at='2026-09-09T12:01:00Z',
                        recording_urls={'mp3': 'https://s3.amazonaws.com/test/recording.mp3?secret=private'})
        data.update(payload)
        return {'data': {'event_type': kind, 'id': event_id,
                         'occurred_at': '2026-09-09T12:01:01Z', 'payload': data}}

    def test_structure_and_no_fake_final_artifacts(self):
        self.assertEqual(self.reservation['scenario_id'], 'scenario_01')
        self.assertTrue((self.directory / 'metadata.json').exists())
        self.assertEqual((self.directory / 'transcript.txt').read_text(), '')
        self.assertFalse((self.directory / 'recording.mp3').exists())
        self.assertEqual(self.metadata()['transcript']['status'], 'pending')

    def test_mp3_provenance_and_private_urls(self):
        self.assertEqual(self.store.handle(self.event()), 'downloaded')
        metadata = self.metadata()
        self.assertEqual((self.directory / 'recording.mp3').read_bytes(), AUDIO)
        self.assertEqual(metadata['artifact']['sha256'], hashlib.sha256(AUDIO).hexdigest())
        self.assertEqual(metadata['artifact']['recording_id'], 'recording-1')
        self.assertEqual(metadata['artifact']['source_event_id'], 'event-1')
        self.assertEqual(metadata['call']['call_control_id'], 'control-1')
        self.assertEqual(metadata['recording']['channels'], 'dual')
        self.assertNotIn('secret=private', (self.directory / 'metadata.json').read_text())
        self.assertIn('secret=private', (self.directory / '.recording-source.json').read_text())
        self.assertFalse(list(self.directory.glob('*.wav')))
        self.assertEqual((self.directory / 'transcript.txt').read_text(), '')

    def test_duplicates_and_out_of_order_lifecycle(self):
        saved = self.event()
        self.store.handle(saved)
        self.store.handle(saved)
        self.downloader.assert_called_once()
        for kind in ('call.hangup', 'call.conversation.ended', 'call.answered', 'call.initiated'):
            self.store.handle(self.event(kind, event_id=kind))
        self.store.handle(self.event('call.recording.error', event_id='late-error', reason='Internal server error'))
        metadata = self.metadata()
        self.assertEqual(metadata['call_status'], 'ended')
        self.assertEqual(metadata['recording']['status'], 'saved')
        self.assertIn('answered_at', metadata['timestamps'])
        self.assertIn('relay_ended_at', metadata['timestamps'])
        self.assertEqual(len(metadata['events']), 6)

    def test_hangup_does_not_claim_recording_ready(self):
        self.store.handle(self.event('call.hangup'))
        self.assertEqual(self.metadata()['recording']['status'], 'requested')
        self.assertEqual(self.metadata()['artifact']['status'], 'pending')

    def test_recording_error(self):
        self.store.handle(self.event('call.recording.error', reason='Internal server error'))
        self.assertEqual(self.metadata()['recording']['status'], 'error')
        self.downloader.assert_not_called()

    def test_download_failure_retries_duplicate_and_cleans_partial(self):
        def fail(url, destination):
            destination.write_bytes(b'partial')
            raise OSError('offline failure')
        self.downloader.side_effect = fail
        event = self.event()
        self.assertEqual(self.store.handle(event), 'download_failed')
        self.assertFalse((self.directory / 'recording.mp3').exists())
        self.assertFalse(list(self.directory.glob('*.tmp')))
        self.downloader.side_effect = fake_download
        self.assertEqual(self.store.handle(event), 'downloaded')
        self.assertEqual(len(self.metadata()['events']), 1)

    def test_wav_only_is_not_renamed_to_mp3(self):
        self.assertEqual(self.store.handle(self.event(recording_urls={'wav': 'https://s3.amazonaws.com/test.wav'})),
                         'missing_mp3_url')
        self.downloader.assert_not_called()
        self.assertFalse((self.directory / 'recording.mp3').exists())

    def test_isolation_restart_and_conflicting_identifiers(self):
        other = self.store.reserve()
        self.assertEqual(other['scenario_id'], 'scenario_02')
        self.store.handle(self.event())
        restarted = EvidenceStore(self.store.root, self.downloader)
        restarted.handle(self.event('call.hangup', 'hangup', client_state=None))
        self.assertEqual(self.metadata()['call_status'], 'ended')
        self.assertEqual(self.metadata(self.store.root / 'scenario_02')['call_status'], 'prepared')
        with self.assertRaises(ValueError):
            self.store.handle(self.event('call.hangup', 'wrong-call', call_leg_id='different-leg'))
        with self.assertRaises(FileExistsError):
            self.store.reserve('scenario_01')

    def test_unknown_state_and_path_traversal_are_rejected(self):
        state = base64.b64encode(json.dumps({'evidence_version': 1, 'scenario_id': '../escape',
                                            'attempt_id': 'bad'}).encode()).decode()
        self.assertEqual(self.store.handle(self.event(client_state=state)), 'unmatched')
        self.assertEqual(self.store.handle(self.event(client_state='not base64')), 'unmatched')
        with self.assertRaises(ValueError):
            self.store.reserve('../escape')
        self.downloader.assert_not_called()

    def test_concurrent_duplicate_is_downloaded_once(self):
        event = self.event()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.store.handle(event), range(4)))
        self.downloader.assert_called_once()
        self.assertEqual(len(self.metadata()['events']), 1)

    def test_dial_response_after_webhook_does_not_regress_state(self):
        self.store.handle(self.event('call.hangup'))
        self.store.record_dial_result('scenario_01', {'recording_id': 'recording-1', 'call_leg_id': 'leg-1'})
        self.assertEqual(self.metadata()['call_status'], 'ended')
        self.assertEqual(self.metadata()['recording']['recording_id'], 'recording-1')
        self.store.record_dial_result('scenario_01', failed=True)
        self.assertEqual(self.metadata()['dial_status'], 'outcome_unknown')

    def test_recording_id_backfilled_after_download(self):
        self.store.handle(self.event(recording_id=None))
        self.assertIsNone(self.metadata()['artifact']['recording_id'])
        self.store.record_dial_result('scenario_01', {'recording_id': 'recording-1'})
        self.assertEqual(self.metadata()['artifact']['recording_id'], 'recording-1')

    def test_older_saved_event_does_not_replace_provenance(self):
        self.store.handle(self.event())
        older = self.event(event_id='older', channels='single')
        older['data']['occurred_at'] = '2026-09-09T12:00:00Z'
        self.store.handle(older)
        self.assertEqual(self.metadata()['recording']['channels'], 'dual')
        self.assertEqual(self.metadata()['artifact']['source_event_id'], 'event-1')
        self.downloader.assert_called_once()

    def test_final_artifacts_are_not_gitignored(self):
        for name in ('recording.mp3', 'transcript.txt', 'metadata.json'):
            result = subprocess.run(['git', 'check-ignore', '--no-index', '-q', '--',
                                     f'evidence/scenario_01/{name}'], cwd=PROJECT, check=False)
            self.assertEqual(result.returncode, 1, name)
        for name in ('.recording-source.json', 'temporary.wav', 'partial.tmp'):
            result = subprocess.run(['git', 'check-ignore', '--no-index', '-q', '--',
                                     f'evidence/scenario_01/{name}'], cwd=PROJECT, check=False)
            self.assertEqual(result.returncode, 0, name)

    def test_dial_configuration_static_only(self):
        tree = ast.parse((PROJECT / 'app.py').read_text())
        route = next(node for node in tree.body if getattr(node, 'name', None) == 'submit_scenario_call')
        dial = next(node for node in ast.walk(route) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == 'dial')
        self.assertTrue(any(key.arg is None and isinstance(key.value, ast.Name)
                            and key.value.id == 'RECORDING_OPTIONS' for key in dial.keywords))
        self.assertEqual(RECORDING_OPTIONS, {'record': 'record-from-answer', 'record_channels': 'dual',
                                            'record_track': 'both', 'record_format': 'mp3'})
        self.assertTrue({'client_state', 'webhook_url', 'webhook_url_method'} <= {key.arg for key in dial.keywords})

    def test_real_downloader_with_mock_http_transport(self):
        original_client = httpx.Client
        def client(**kwargs):
            return original_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=AUDIO)), **kwargs)
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
        with patch('evidence.socket.getaddrinfo', return_value=addresses), patch('evidence.httpx.Client', side_effect=client):
            result = download_mp3('https://s3.amazonaws.com/fixture.mp3', self.directory / 'fixture.tmp')
        self.assertEqual(result['sha256'], hashlib.sha256(AUDIO).hexdigest())

    def test_downloader_rejects_untrusted_urls_and_wav(self):
        for url in ('http://s3.amazonaws.com/x', 'https://127.0.0.1/x', 'https://attacker.example/x'):
            with self.assertRaises(ValueError):
                download_mp3(url, self.directory / 'bad.tmp')
        original_client = httpx.Client
        def client(**kwargs):
            return original_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'RIFFwavdata')), **kwargs)
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
        with patch('evidence.socket.getaddrinfo', return_value=addresses), patch('evidence.httpx.Client', side_effect=client):
            with self.assertRaises(ValueError):
                download_mp3('https://s3.amazonaws.com/x', self.directory / 'bad.tmp')


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_recording_download_schedules_transcription_once(self):
        from unittest.mock import AsyncMock

        event = {
            'data': {
                'event_type': 'call.recording.saved',
                'id': 'recording-event',
                'occurred_at': '2026-09-09T12:01:00Z',
                'payload': {'client_state': 'offline-state'},
            }
        }
        body = json.dumps(event)
        request = SimpleNamespace(body=AsyncMock(return_value=body.encode()), headers={})
        store = Mock(
            handle=Mock(side_effect=['downloaded', 'duplicate']),
            resolve=Mock(return_value=PROJECT / 'evidence' / 'scenario_01'),
            root=PROJECT / 'evidence',
        )
        client = SimpleNamespace(webhooks=SimpleNamespace(unwrap=Mock()))
        with patch.object(app_module, 'telnyx_client', client), \
             patch.object(app_module, 'TELNYX_PUBLIC_KEY', 'offline-public-key'), \
             patch.object(app_module, 'evidence_store', store), \
             patch.object(app_module, 'transcribe_scenario', return_value={'segments': [1]}) as transcribe:
            await app_module.telnyx_webhook(request)
            await app_module.telnyx_webhook(request)
            for _ in range(100):
                if transcribe.called and not app_module.transcription_tasks:
                    break
                await asyncio.sleep(0.01)
        transcribe.assert_called_once()
        self.assertEqual(transcribe.call_args.args[0], 'scenario_01')
        store.handle.assert_has_calls([call(event), call(event)])

    async def test_real_sdk_signature_verification_offline(self):
        import time
        from nacl.signing import SigningKey
        from telnyx import Telnyx
        from unittest.mock import AsyncMock

        signing_key = SigningKey.generate()
        public_key = base64.b64encode(bytes(signing_key.verify_key)).decode()
        body = json.dumps({'data': {'event_type': 'call.hangup', 'id': 'signed-fixture',
                                    'occurred_at': '2026-09-09T12:00:00Z', 'payload': {}}})

        def headers(timestamp):
            signature = signing_key.sign(f'{timestamp}|{body}'.encode()).signature
            return {'telnyx-timestamp': str(timestamp),
                    'telnyx-signature-ed25519': base64.b64encode(signature).decode()}

        store = Mock(handle=Mock(return_value='stored'))
        with patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')), \
             Telnyx(api_key='offline-placeholder') as client, \
             patch.object(app_module, 'telnyx_client', client), \
             patch.object(app_module, 'TELNYX_PUBLIC_KEY', public_key), \
             patch.object(app_module, 'evidence_store', store):
            request = SimpleNamespace(body=AsyncMock(return_value=body.encode()), headers=headers(int(time.time())))
            self.assertEqual((await app_module.telnyx_webhook(request))['evidence'], 'stored')
            store.handle.assert_called_once()
            store.handle.reset_mock()
            for content, signed_headers in ((body + ' ', request.headers),
                                            (body, headers(int(time.time()) - 600))):
                request = SimpleNamespace(body=AsyncMock(return_value=content.encode()), headers=signed_headers)
                with self.assertRaises(HTTPException) as error:
                    await app_module.telnyx_webhook(request)
                self.assertEqual(error.exception.status_code, 400)
                store.handle.assert_not_called()

    async def test_verification_boundary_and_fail_closed(self):
        from unittest.mock import AsyncMock
        request = SimpleNamespace(body=AsyncMock(return_value=b'{"data": {}}'), headers={'test': 'header'})
        verifier = Mock()
        client = SimpleNamespace(webhooks=SimpleNamespace(unwrap=verifier))
        store = Mock(handle=Mock(return_value='stored'))
        with patch.object(app_module, 'telnyx_client', client), \
             patch.object(app_module, 'TELNYX_PUBLIC_KEY', 'offline-public-key'), \
             patch.object(app_module, 'evidence_store', store), \
             patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')):
            self.assertEqual((await app_module.telnyx_webhook(request))['evidence'], 'stored')
            verifier.assert_called_once_with('{"data": {}}', headers=request.headers, key='offline-public-key')
            store.handle.reset_mock()
            for failure, expected in [(ValueError('signature invalid'), 400), (TelnyxError('missing dependency'), 503)]:
                verifier.side_effect = failure
                with self.assertRaises(HTTPException) as error:
                    await app_module.telnyx_webhook(request)
                self.assertEqual(error.exception.status_code, expected)
                store.handle.assert_not_called()
            verifier.side_effect = None
            store.handle.return_value = 'download_failed'
            with self.assertRaises(HTTPException) as error:
                await app_module.telnyx_webhook(request)
            self.assertEqual(error.exception.status_code, 503)

    async def test_missing_public_key(self):
        with patch.object(app_module, 'TELNYX_PUBLIC_KEY', None):
            with self.assertRaises(HTTPException) as error:
                await app_module.telnyx_webhook(None)
            self.assertEqual(error.exception.status_code, 503)


if __name__ == '__main__':
    unittest.main()
