"""Test submission helper only; never invoke the route or a real provider."""
import base64
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import HTTPException
from telnyx import APIStatusError
from evidence import EvidenceStore
from media_transport import MediaTokenStore
from test_conversation_relay import app_module

ROOT = Path(__file__).resolve().parent


class DialLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='.test-dial-lifecycle-', dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.store = EvidenceStore(self.parent / 'evidence')
        self.payload = {'call_control_id': 'v3:offline-control', 'call_leg_id': 'offline-leg',
                        'call_session_id': 'offline-session', 'recording_id': 'offline-recording'}
        self.client = Mock()
        self.tokens = MediaTokenStore()
        self.dial = self.client.with_options.return_value.calls.dial
        self.dial.return_value = SimpleNamespace(data=SimpleNamespace(model_dump=lambda **kwargs: dict(self.payload)))
        for item in (
            patch.object(app_module, 'evidence_store', self.store),
            patch.object(app_module, 'telnyx_client', self.client),
            patch.object(app_module, 'TELNYX_PHONE_NUMBER', 'offline-from'),
            patch.object(app_module, 'TELNYX_CONNECTION_ID', 'offline-connection'),
            patch.object(app_module, 'TELNYX_PUBLIC_KEY', 'offline-public-key'),
            patch.object(app_module, 'PUBLIC_BASE_URL', 'https://offline.example'),
            patch.object(app_module, 'ELEVENLABS_API_KEY', 'offline-elevenlabs-key'),
            patch.object(app_module, 'media_token_store', self.tokens),
            patch.dict('os.environ', {'ELEVENLABS_VOICE_ID_SCENARIO_01': 'offline-voice'}),
            patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')),
        ):
            item.start()
            self.addCleanup(item.stop)

    def assert_no_evidence(self):
        self.assertFalse((self.store.root / 'scenario_01').exists())
        self.assertFalse(list(self.parent.glob('.evidence-stage-*')))
        self.assertFalse(list(self.store.root.rglob('*')))

    def test_rejected_dial_logs_safely_and_creates_nothing(self):
        response = httpx.Response(402, request=httpx.Request('POST', 'https://offline.example'))
        self.dial.side_effect = APIStatusError('secret must not be logged', response=response, body={})
        with self.assertLogs(app_module.__name__, level='ERROR') as logs:
            with self.assertRaises(HTTPException) as error:
                app_module.submit_scenario_call('scenario_01')
        self.assertEqual(error.exception.status_code, 502)
        self.assertIn('status=402', str(logs.output))
        self.assertNotIn('secret must not be logged', str(logs.output))
        self.assert_no_evidence()

    def test_unexpected_exception_and_invalid_responses_create_nothing(self):
        for result in (RuntimeError('private details'), SimpleNamespace(data=None),
                       SimpleNamespace(data=SimpleNamespace(model_dump=lambda **kw: {'call_control_id': 'only-one'}))):
            with self.subTest(result=type(result).__name__):
                self.dial.side_effect = result if isinstance(result, Exception) else None
                self.dial.return_value = result
                with self.assertLogs(app_module.__name__, level='ERROR'):
                    with self.assertRaises(HTTPException) as error:
                        app_module.submit_scenario_call('scenario_01')
                self.assertEqual(error.exception.status_code, 502)
                self.assert_no_evidence()

    def test_success_publishes_only_after_acceptance_exactly_once(self):
        original = self.dial.return_value
        def accept(**kwargs):
            self.assert_no_evidence()
            return original
        self.dial.side_effect = accept
        with patch.object(self.store, 'publish_accepted', wraps=self.store.publish_accepted) as publish:
            result = app_module.submit_scenario_call('scenario_01')
            publish.assert_called_once()
        self.assertEqual(result['status'], 'submitted')
        metadata = json.loads((self.store.root / 'scenario_01/metadata.json').read_text())
        self.assertEqual(metadata['scenario_id'], 'scenario_01')
        self.assertEqual(metadata['dial_status'], 'submitted')
        for key in ('call_control_id', 'call_leg_id', 'call_session_id'):
            self.assertEqual(metadata['call'][key], self.payload[key])
        state = json.loads(base64.b64decode(self.dial.call_args.kwargs['client_state']))
        self.assertEqual(state['attempt_id'], metadata['attempt_id'])
        dial = self.dial.call_args.kwargs
        self.assertNotIn('conversation_relay_config', dial)
        self.assertEqual(dial['stream_track'], 'inbound_track')
        self.assertEqual(dial['stream_codec'], 'PCMU')
        self.assertEqual(dial['stream_bidirectional_mode'], 'rtp')
        self.assertEqual(dial['stream_bidirectional_codec'], 'PCMU')
        self.assertEqual(dial['stream_bidirectional_sampling_rate'], 8000)
        self.assertEqual(dial['stream_bidirectional_target_legs'], 'self')
        self.assertIn('/media-stream?', dial['stream_url'])
        self.assertFalse(list(self.parent.glob('.evidence-stage-*')))
        self.client.with_options.assert_called_once_with(max_retries=0)

    def test_existing_legitimate_evidence_is_unchanged(self):
        self.store.reserve('scenario_01')
        self.store.record_dial_result('scenario_01', self.payload)
        directory = self.store.root / 'scenario_01'
        (directory / 'recording.mp3').write_bytes(b'preserve recording')
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        with self.assertRaises(HTTPException) as error:
            app_module.submit_scenario_call('scenario_01')
        self.assertEqual(error.exception.status_code, 409)
        self.dial.assert_not_called()
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_invalid_scenario_never_submits_or_creates_evidence(self):
        with self.assertRaises(HTTPException) as error:
            app_module.submit_scenario_call('scenario_99')
        self.assertEqual(error.exception.status_code, 400)
        self.dial.assert_not_called()
        self.assert_no_evidence()

    def test_staging_write_failure_cleans_up_after_accepted_call(self):
        with patch('evidence.atomic_json', side_effect=OSError('offline disk error')):
            with self.assertLogs(app_module.__name__, level='ERROR'):
                with self.assertRaises(HTTPException) as error:
                    app_module.submit_scenario_call('scenario_01')
        self.assertEqual(error.exception.status_code, 503)
        self.assertIn('Call accepted', error.exception.detail)
        self.dial.assert_called_once()
        self.assert_no_evidence()

    def test_concurrent_submissions_only_one_dial(self):
        def submit():
            try:
                return app_module.submit_scenario_call('scenario_01')['status']
            except HTTPException as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertCountEqual(results, ['submitted', 409])
        self.dial.assert_called_once()

    def test_early_webhook_waits_for_published_identifiers(self):
        import threading
        lookup_started = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            accepted = self.dial.return_value
            futures = []
            def accept(**kwargs):
                def lookup():
                    lookup_started.set()
                    return self.store.resolve({'client_state': kwargs['client_state']})
                future = pool.submit(lookup)
                futures.append(future)
                self.assertTrue(lookup_started.wait(2))
                self.assertFalse(future.done())
                return accepted
            self.dial.side_effect = accept
            app_module.submit_scenario_call('scenario_01')
            self.assertEqual(futures[0].result(timeout=2), self.store.root / 'scenario_01')

    def test_media_token_is_staged_before_dial_submission(self):
        original = self.dial.return_value

        def accept(**kwargs):
            from urllib.parse import parse_qs, urlsplit
            query = parse_qs(urlsplit(kwargs['stream_url']).query)
            self.assertTrue(self.tokens.has_pending(query['call'][0]))
            self.assertEqual(query['token'][0], kwargs['stream_auth_token'])
            return original

        self.dial.side_effect = accept
        app_module.submit_scenario_call('scenario_01')
