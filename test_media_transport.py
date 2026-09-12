"""Offline regression coverage for Telnyx bidirectional PCMU media transport."""

import asyncio
import base64
import copy
import io
import ssl
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import certifi
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from media_transport import (
    AudioDiagnostics,
    ELEVENLABS_OUTPUT_FORMAT,
    ElevenLabsTTS,
    MediaStreamSession,
    MediaTokenStore,
    MediaValidationError,
    STTRuntimeError,
    STTStartError,
    TELNYX_STT_CONFIG,
    TelnyxStreamingSTT,
    format_media_validation_error,
    format_stt_transport_error,
)
from scenarios import load_scenario
from stt_preflight import preflight
from test_conversation_relay import FakeClient, FakeStream, app_module, eventually


ROOT = Path(__file__).resolve().parent
DOCUMENTED_CLIENT_STATE = 'aGF2ZSBhIG5pY2UgZGF5ID1d'
DOCUMENTED_CONTROL_ID = 'v2:T02llQxIyaRkhfRKxgAP8nY511EhFLizdvdUKJiSw8d6A9BborherQ'
DOCUMENTED_SESSION_ID = 'ff55a038-6f5d-11ef-9692-02420aeffb1f'
DOCUMENTED_STREAM_ID = '32DE0DEA-53CB-4B21-89A4-9E1819C043BC'
DOCUMENTED_CONNECTED = {'event': 'connected', 'version': '1.0.0'}
DOCUMENTED_START = {
    'event': 'start',
    'sequence_number': '1',
    'start': {
        'user_id': '3E6F995F-85F7-4705-9741-53B116D28237',
        'call_control_id': DOCUMENTED_CONTROL_ID,
        'call_session_id': DOCUMENTED_SESSION_ID,
        'from': '+13122010094',
        'to': '+13122123456',
        'tags': ['TAG1', 'TAG2'],
        'client_state': DOCUMENTED_CLIENT_STATE,
        'media_format': {'encoding': 'PCMU', 'sample_rate': 8000, 'channels': 1},
    },
    'stream_id': DOCUMENTED_STREAM_ID,
}
DOCUMENTED_MEDIA = {
    'event': 'media',
    'sequence_number': '4',
    'media': {
        'track': 'inbound/outbound',
        'chunk': '2',
        'timestamp': '5',
        'payload': 'no+JhoaJjpzSHxAKBgYJDhtEopGKh4aI',
    },
    'stream_id': DOCUMENTED_STREAM_ID,
}
DOCUMENTED_STOP = {
    'event': 'stop',
    'sequence_number': '5',
    'stop': {
        'user_id': '3E6F995F-85F7-4705-9741-53B116D28237',
        'call_control_id': DOCUMENTED_CONTROL_ID,
    },
    'stream_id': DOCUMENTED_STREAM_ID,
}


async def _async_noop(*_args) -> None:
    pass


class FakeMediaWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


class ScriptedMediaWebSocket(FakeMediaWebSocket):
    def __init__(self, received):
        super().__init__()
        self.received = list(received)

    async def receive_json(self):
        return self.received.pop(0)


class QueuedMediaWebSocket(FakeMediaWebSocket):
    def __init__(self, received=()):
        super().__init__()
        self.received = asyncio.Queue()
        for message in received:
            self.received.put_nowait(message)

    async def receive_json(self):
        return await self.received.get()


class FakeSTT:
    def __init__(self):
        self.audio = []
        self.callback = None
        self.closed = False
        self.last_audio_bytes = 0

    async def start(self, callback):
        self.callback = callback

    async def send_audio(self, audio):
        self.audio.append(audio)
        self.last_audio_bytes = len(audio)

    async def close(self):
        self.closed = True


class FakeTTS:
    def __init__(self, chunks):
        self.chunks = chunks
        self.requests = []
        self.last_response_status = 200

    async def stream(self, text, voice_id):
        self.requests.append({
            'text': text,
            'voice_id': voice_id,
            'output_format': ELEVENLABS_OUTPUT_FORMAT,
        })
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


class ManualPacing:
    def __init__(self):
        self.now = 0.0
        self.delays = []

    def clock(self):
        return self.now

    async def sleep(self, delay):
        self.delays.append(delay)
        self.now += delay


class BlockingPacing(ManualPacing):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def sleep(self, delay):
        self.delays.append(delay)
        self.started.set()
        await self.release.wait()
        self.now += delay


def staged(store=None, *, clock=None, client_state='offline-client-state'):
    store = store or MediaTokenStore(clock=clock or (lambda: 0))
    placeholder, token = store.stage('scenario_01', 'attempt-1', client_state, 'voice-01')
    association = store.consume(placeholder, token)
    return store, placeholder, token, association


def start_event(**overrides):
    start = {
        'user_id': 'offline-user',
        'call_control_id': 'v3:offline-control',
        'call_session_id': 'offline-session',
        'from': '+13122010094',
        'to': '+13122123456',
        'tags': ['TAG1', 'TAG2'],
        'client_state': 'offline-client-state',
        'media_format': {'encoding': 'PCMU', 'sample_rate': 8000, 'channels': 1},
    }
    start.update(overrides)
    return {
        'event': 'start', 'sequence_number': '1',
        'stream_id': 'offline-stream', 'start': start,
    }


class TokenTests(unittest.TestCase):
    def test_invalid_expired_reused_and_mismatched_tokens_are_rejected(self):
        current = [10.0]
        store = MediaTokenStore(ttl_seconds=5, clock=lambda: current[0])
        first, token = store.stage('scenario_01', 'attempt-1', 'state', 'voice')
        second, other = store.stage('scenario_01', 'attempt-2', 'other-state', 'voice')
        self.assertIsNone(store.consume(first, 'invalid'))
        self.assertIsNone(store.consume(first, other))
        self.assertIsNotNone(store.consume(first, token))
        self.assertIsNone(store.consume(first, token))
        current[0] = 16.0
        self.assertIsNone(store.consume(second, other))

    def test_acceptance_before_start_allows_different_stream_leg_control_id(self):
        store, placeholder, _, association = staged()
        store.bind_accepted(placeholder, {
            'call_control_id': 'accepted-control',
            'call_leg_id': 'accepted-leg',
            'call_session_id': 'offline-session',
        })
        store.validate_start(association, start_event()['start'])
        self.assertEqual(association.started_ids['call_control_id'], 'v3:offline-control')

    def test_start_before_acceptance_binds_on_session_despite_different_control_id(self):
        store, placeholder, _, association = staged()
        store.validate_start(association, start_event()['start'])
        store.bind_accepted(placeholder, {
            'call_control_id': 'later-accepted-control',
            'call_leg_id': 'later-accepted-leg',
            'call_session_id': 'offline-session',
        })
        store.validate_active(association)

    def test_session_mismatch_is_rejected_in_both_timing_orders(self):
        store, placeholder, _, association = staged()
        store.bind_accepted(placeholder, {
            'call_control_id': 'accepted-control',
            'call_leg_id': 'accepted-leg',
            'call_session_id': 'accepted-session',
        })
        with self.assertRaises(MediaValidationError) as caught:
            store.validate_start(association, start_event()['start'])
        self.assertEqual(caught.exception.reason_code, 'call_session_id_mismatch')

        store, placeholder, _, association = staged()
        store.validate_start(association, start_event()['start'])
        with self.assertRaises(MediaValidationError) as caught:
            store.bind_accepted(placeholder, {
                'call_control_id': 'later-control',
                'call_leg_id': 'later-leg',
                'call_session_id': 'different-session',
            })
        self.assertEqual(caught.exception.reason_code, 'call_session_id_mismatch')
        with self.assertRaises(MediaValidationError) as caught:
            store.validate_active(association)
        self.assertEqual(caught.exception.reason_code, 'association_rejected')

    def test_missing_and_typed_start_association_fields_have_safe_reason_codes(self):
        cases = (
            ('client_state', None, 'client_state_missing'),
            ('client_state', 123, 'client_state_not_string'),
            ('call_control_id', '', 'call_control_id_missing'),
            ('call_control_id', 123, 'call_control_id_not_string'),
            ('call_session_id', None, 'call_session_id_missing'),
            ('call_session_id', 123, 'call_session_id_not_string'),
        )
        for field, value, reason in cases:
            with self.subTest(field=field, value=value):
                store, _, _, association = staged()
                start = start_event()["start"]
                start[field] = value
                with self.assertRaises(MediaValidationError) as caught:
                    store.validate_start(association, start)
                self.assertEqual(caught.exception.reason_code, reason)

    def test_client_state_mismatch_diagnostic_never_contains_sensitive_values(self):
        store, _, _, association = staged()
        start = start_event(client_state='raw-state-that-must-not-be-logged')['start']
        with self.assertRaises(MediaValidationError) as caught:
            store.validate_start(association, start)
        diagnostic = format_media_validation_error(caught.exception)
        self.assertIn('reason_code=client_state_mismatch', diagnostic)
        self.assertIn('client_state_match=false', diagnostic)
        for secret in ('raw-state-that-must-not-be-logged', 'offline-client-state',
                       'v3:offline-control', 'offline-session'):
            self.assertNotIn(secret, diagnostic)

    def test_audio_diagnostics_are_opt_in_and_outside_evidence(self):
        with tempfile.TemporaryDirectory(prefix='.test-audio-debug-', dir=ROOT) as temporary:
            root = Path(temporary) / 'debug_audio'
            disabled = AudioDiagnostics(False, root, 'attempt-disabled')
            disabled.write('telnyx_in', b'not-written')
            disabled.close()
            self.assertFalse(root.exists())
            enabled = AudioDiagnostics(True, root, 'attempt-enabled')
            for kind in AudioDiagnostics.FILES:
                enabled.write(kind, kind.encode())
            directory = enabled.directory
            enabled.close()
            self.assertNotIn('evidence', directory.parts)
            self.assertEqual(
                {path.name for path in directory.iterdir()},
                set(AudioDiagnostics.FILES.values()),
            )


class FakeProviderWebSocket:
    _CLOSE = object()

    def __init__(self, frames=()):
        self.sent = []
        self.closed = False
        self.frames = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(frame)

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.frames.get()
        if frame is self._CLOSE:
            raise StopAsyncIteration
        if isinstance(frame, BaseException):
            raise frame
        return frame

    async def send(self, value):
        self.sent.append(value)

    async def close(self):
        self.closed = True
        self.frames.put_nowait(self._CLOSE)

    def end_unexpectedly(self):
        self.frames.put_nowait(self._CLOSE)


class RouteMediaWebSocket(ScriptedMediaWebSocket):
    def __init__(self, received, placeholder, token, client):
        super().__init__(received)
        self.query_params = {'call': placeholder, 'token': token}
        self.app = SimpleNamespace(state=SimpleNamespace(anthropic_client=client))
        self.accepted = False
        self.close_code = None
        self.close_reason = None

    async def accept(self):
        self.accepted = True

    async def close(self, code, reason):
        self.close_code = code
        self.close_reason = reason


class FakeHTTPResponse:
    def __init__(self, chunks):
        self.chunks = chunks
        self.checked = False
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def raise_for_status(self):
        self.checked = True

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk


class FakeHTTPClient:
    def __init__(self, capture, chunks, **client_options):
        self.capture = capture
        self.chunks = chunks
        capture['client_options'] = client_options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, method, url, **kwargs):
        self.capture['request'] = {'method': method, 'url': url, **kwargs}
        return FakeHTTPResponse(self.chunks)


class AdapterContractTests(unittest.IsolatedAsyncioTestCase):
    def test_default_stt_context_uses_certifi_and_keeps_verification_enabled(self):
        with (
            patch('media_transport.certifi.where', wraps=certifi.where) as ca_file,
            patch(
                'media_transport.ssl.create_default_context',
                wraps=ssl.create_default_context,
            ) as create_context,
        ):
            stt = TelnyxStreamingSTT('offline-telnyx-key', connect=lambda: None)

        ca_file.assert_called_once_with()
        create_context.assert_called_once_with(cafile=certifi.where())
        self.assertEqual(stt._ssl_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIs(stt._ssl_context.check_hostname, True)

    def test_injected_stt_context_is_used_without_creating_a_default(self):
        injected = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch('media_transport.ssl.create_default_context') as create_context:
            stt = TelnyxStreamingSTT(
                'offline-telnyx-key', connect=lambda: None, ssl_context=injected,
            )
        create_context.assert_not_called()
        self.assertIs(stt._ssl_context, injected)

    async def test_telnyx_hosted_deepgram_connects_with_bearer_and_streams_raw_linear16(self):
        provider = FakeProviderWebSocket()
        capture = {}

        async def connect(url, **kwargs):
            capture.update({'url': url, **kwargs})
            return provider

        stt = TelnyxStreamingSTT('offline-telnyx-key', connect=connect)
        await stt.start(lambda text, final: None)
        audio = b'\xff\x01\x02\x03' * 40
        await stt.send_audio(audio)
        self.assertEqual(len(provider.sent), 1)
        self.assertEqual(len(provider.sent[0]), len(audio) * 4)
        self.assertNotEqual(provider.sent[0], audio)
        self.assertNotEqual(provider.sent[0][:4], b'RIFF')
        self.assertEqual(stt.last_audio_bytes, len(audio) * 4)
        self.assertIn('transcription_engine=Deepgram', capture['url'])
        self.assertIn('model=nova-3', capture['url'])
        self.assertIn('input_format=linear16', capture['url'])
        self.assertIn('sample_rate=16000', capture['url'])
        self.assertEqual(capture['url'].count('?'), 1)
        self.assertEqual(
            parse_qs(urlsplit(capture['url']).query),
            {key: [value] for key, value in TELNYX_STT_CONFIG.items()},
        )
        self.assertEqual(capture['additional_headers'], {
            'Authorization': 'Bearer offline-telnyx-key',
        })
        self.assertIs(capture['ssl'], stt._ssl_context)
        self.assertEqual(capture['ssl'].verify_mode, ssl.CERT_REQUIRED)
        self.assertIs(capture['ssl'].check_hostname, True)
        await stt.close()

    async def test_connect_failures_are_safe_stt_start_failures(self):
        failures = (
            ssl.SSLCertVerificationError(1, 'private certificate details'),
            OSError('private network details'),
            ValueError('private adapter details'),
        )
        for original in failures:
            with self.subTest(error_type=type(original).__name__):
                async def connect(_url, **_kwargs):
                    raise original

                stt = TelnyxStreamingSTT('private-api-key', connect=connect)
                with self.assertRaises(STTStartError) as caught:
                    await stt.start(lambda text, final: None)
                failure = caught.exception
                self.assertEqual(failure.stage, 'tls_connect')
                self.assertEqual(failure.error_type, type(original).__name__)
                self.assertEqual(failure.status, None)
                safe = format_stt_transport_error(failure)
                self.assertNotIn(str(original), safe)
                self.assertNotIn('private-api-key', safe)

    async def test_websocket_authentication_handshake_failure_is_classified(self):
        response = Response(401, 'Unauthorized', Headers(), b'private response body')

        async def connect(_url, **_kwargs):
            raise InvalidStatus(response)

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        with self.assertRaises(STTStartError) as caught:
            await stt.start(lambda text, final: None)
        self.assertEqual(
            format_stt_transport_error(caught.exception),
            'stage=tls_connect error_type=InvalidStatus status=401',
        )

    async def test_linear16_start_sends_no_container_header(self):
        provider = FakeProviderWebSocket()

        async def connect(_url, **_kwargs):
            return provider

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        await stt.start(lambda text, final: None)
        self.assertEqual(provider.sent, [])
        await stt.close()

    async def test_audio_send_failure_is_classified_as_stt_runtime(self):
        provider = FakeProviderWebSocket()

        async def connect(_url, **_kwargs):
            return provider

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        await stt.start(lambda text, final: None)

        async def fail_send(_value):
            raise OSError('private runtime details')

        provider.send = fail_send
        with self.assertRaises(STTRuntimeError) as caught:
            await stt.send_audio(b'\xff' * 160)
        self.assertEqual(caught.exception.stage, 'audio_send')
        self.assertNotIn('private runtime details', str(caught.exception))
        await stt.close()

    async def test_text_json_frame_calls_callback_with_transcript_and_final_flag(self):
        provider = FakeProviderWebSocket([
            '{"transcript":"  hello  ","is_final":true}',
        ])
        received = []
        delivered = asyncio.Event()

        async def connect(_url, **_kwargs):
            return provider

        async def callback(transcript, is_final):
            received.append((transcript, is_final))
            delivered.set()

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        await stt.start(callback)
        await asyncio.wait_for(delivered.wait(), 1)
        self.assertEqual(received, [('hello', True)])
        await stt.close()

    async def test_binary_json_frame_is_supported_without_manual_decoding(self):
        provider = FakeProviderWebSocket([
            b'{"transcript":"binary","is_final":false}',
        ])
        received = []
        delivered = asyncio.Event()

        async def connect(_url, **_kwargs):
            return provider

        async def callback(transcript, is_final):
            received.append((transcript, is_final))
            delivered.set()

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        await stt.start(callback)
        await asyncio.wait_for(delivered.wait(), 1)
        self.assertEqual(received, [('binary', False)])
        await stt.close()

    async def test_decoded_json_must_be_an_object(self):
        provider = FakeProviderWebSocket(['["private-provider-body"]'])

        async def connect(_url, **_kwargs):
            return provider

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        await stt.start(_async_noop)
        with self.assertRaises(STTRuntimeError) as caught:
            await stt.wait_healthy(1)
        self.assertEqual(caught.exception.stage, 'message_shape')
        self.assertEqual(caught.exception.error_type, 'TypeError')
        with self.assertRaises(STTRuntimeError):
            await stt.close()

    async def test_malformed_json_is_ignored_and_receiver_continues(self):
        provider = FakeProviderWebSocket([
            '{private malformed json',
            '{"transcript":"recovered","is_final":true}',
        ])
        received = []
        delivered = asyncio.Event()

        async def connect(_url, **_kwargs):
            return provider

        async def callback(transcript, is_final):
            received.append((transcript, is_final))
            delivered.set()

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        with self.assertLogs('media_transport', level='WARNING') as logs:
            await stt.start(callback)
            await asyncio.wait_for(delivered.wait(), 1)
        self.assertEqual(received, [('recovered', True)])
        self.assertIn('stage=json_decode error_type=JSONDecodeError', '\n'.join(logs.output))
        self.assertNotIn('private malformed json', '\n'.join(logs.output))
        await stt.close()

    async def test_provider_error_event_fails_receiver(self):
        provider = FakeProviderWebSocket([
            '{"errors":[{"detail":"private provider body"}]}',
        ])

        async def connect(_url, **_kwargs):
            return provider

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        await stt.start(_async_noop)
        with self.assertRaises(STTRuntimeError) as caught:
            await stt.wait_healthy(1)
        self.assertEqual(caught.exception.stage, 'provider_event')
        self.assertEqual(caught.exception.error_type, 'ProviderError')
        with self.assertRaises(STTRuntimeError):
            await stt.close()

    async def test_callback_type_error_is_classified_and_preserved(self):
        provider = FakeProviderWebSocket([
            '{"transcript":"private transcript","is_final":true}',
        ])

        async def connect(_url, **_kwargs):
            return provider

        async def callback(_transcript, _is_final):
            raise TypeError('private callback details')

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        await stt.start(callback)
        with self.assertRaises(STTRuntimeError) as caught:
            await stt.wait_healthy(1)
        self.assertEqual(caught.exception.stage, 'callback')
        self.assertEqual(caught.exception.error_type, 'TypeError')
        self.assertNotIn('private callback details', str(caught.exception))
        with self.assertRaises(STTRuntimeError):
            await stt.close()

    async def test_receive_iterator_type_error_is_classified_and_preserved(self):
        provider = FakeProviderWebSocket([TypeError('private iterator details')])

        async def connect(_url, **_kwargs):
            return provider

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        await stt.start(_async_noop)
        with self.assertRaises(STTRuntimeError) as caught:
            await stt.wait_healthy(1)
        self.assertEqual(caught.exception.stage, 'receive_frame')
        self.assertEqual(caught.exception.error_type, 'TypeError')
        with self.assertRaises(STTRuntimeError):
            await stt.close()

    async def test_owner_initiated_close_is_normal(self):
        provider = FakeProviderWebSocket()

        async def connect(_url, **_kwargs):
            return provider

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        await stt.start(_async_noop)
        await stt.wait_healthy(0.01)
        await stt.close()
        self.assertTrue(provider.closed)

    async def test_remote_normal_close_is_unexpected_during_operation(self):
        provider = FakeProviderWebSocket()

        async def connect(_url, **_kwargs):
            return provider

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        await stt.start(_async_noop)
        provider.end_unexpectedly()
        with self.assertRaises(STTRuntimeError) as caught:
            await stt.wait_healthy(1)
        self.assertEqual(caught.exception.stage, 'connection_close')
        with self.assertRaises(STTRuntimeError):
            await stt.close()

    async def test_receiver_failure_logs_only_safe_allowlisted_metadata(self):
        private_values = (
            'private-api-key', 'private transcript', 'private callback details',
            'https://private.example', '+13125550123', 'patient-private-data',
        )
        provider = FakeProviderWebSocket([
            '{"transcript":"private transcript","is_final":true}',
        ])

        async def connect(_url, **_kwargs):
            return provider

        async def callback(_transcript, _is_final):
            raise TypeError(
                'private callback details https://private.example '
                '+13125550123 patient-private-data'
            )

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        with self.assertLogs('media_transport', level='ERROR') as logs:
            await stt.start(callback)
            with self.assertRaises(STTRuntimeError):
                await stt.wait_healthy(1)
        output = '\n'.join(logs.output)
        self.assertIn(
            'STT_RUNTIME_FAILED stage=callback error_type=TypeError status=none', output,
        )
        for private in private_values:
            self.assertNotIn(private, output)
        with self.assertRaises(STTRuntimeError):
            await stt.close()

    async def test_tls_failure_log_is_safe_and_never_media_start_reject(self):
        client_state = 'private-client-state'
        store = MediaTokenStore(clock=lambda: 0)
        placeholder, token = store.stage(
            'scenario_01', 'private-attempt-id', client_state, 'private-voice-id',
        )
        message = start_event(
            client_state=client_state,
            call_control_id='private-control-id',
            call_session_id='private-session-id',
        )
        websocket = RouteMediaWebSocket(
            [message], placeholder, token, FakeClient([]),
        )

        async def connect(_url, **_kwargs):
            raise ssl.SSLCertVerificationError(
                1, 'private certificate https://secret.example private-api-key',
            )

        stt = TelnyxStreamingSTT('private-api-key', connect=connect)
        with (
            patch.object(app_module, 'media_token_store', store),
            patch.object(app_module, 'TELNYX_API_KEY', 'private-api-key'),
            patch.object(app_module, 'ELEVENLABS_API_KEY', 'private-elevenlabs-key'),
            patch.object(app_module, 'load_scenario', return_value=load_scenario()),
            patch.object(app_module, '_create_stt_adapter', return_value=stt),
            self.assertLogs(app_module.__name__, level='ERROR') as logs,
        ):
            await app_module.media_stream(websocket)

        output = '\n'.join(logs.output)
        self.assertIn(
            'STT_START_FAILED stage=tls_connect '
            'error_type=SSLCertVerificationError status=none',
            output,
        )
        self.assertNotIn('MEDIA_START_REJECT', output)
        for private in (
            'private certificate', 'secret.example', 'private-api-key',
            'private-elevenlabs-key', 'private-client-state', 'private-attempt-id',
            'private-voice-id', 'private-control-id', 'private-session-id',
            placeholder, token, stt.url,
        ):
            self.assertNotIn(private, output)
        self.assertTrue(websocket.accepted)
        self.assertEqual(websocket.close_code, 1011)

    async def test_elevenlabs_http_stream_requests_exact_native_format(self):
        capture = {}
        chunks = [b'\x01' * 160, b'\x02' * 160]
        factory = lambda **kwargs: FakeHTTPClient(capture, chunks, **kwargs)
        tts = ElevenLabsTTS('offline-elevenlabs-key', 'eleven_flash_v2_5', client_factory=factory)
        actual = [chunk async for chunk in tts.stream('Hello', 'scenario/voice')]
        self.assertEqual(actual, chunks)
        request = capture['request']
        self.assertEqual(request['method'], 'POST')
        self.assertTrue(request['url'].endswith('/scenario%2Fvoice/stream'))
        self.assertEqual(request['params'], {'output_format': 'ulaw_8000'})
        self.assertEqual(request['json'], {
            'text': 'Hello',
            'model_id': 'eleven_flash_v2_5',
            'voice_settings': {
                'stability': 0.5,
                'similarity_boost': 0.75,
                'style': 0.0,
                'use_speaker_boost': True,
                'speed': 1.0,
            },
        })
        self.assertEqual(request['headers']['xi-api-key'], 'offline-elevenlabs-key')
        self.assertEqual(tts.last_response_status, 200)


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_does_not_print_success_after_receiver_failure(self):
        class FailingAdapter:
            def __init__(self, _api_key):
                pass

            async def start(self, callback):
                await callback('', False)

            async def wait_healthy(self, _interval):
                raise STTRuntimeError('callback', TypeError('private runtime details'))

            async def close(self):
                pass

        output = io.StringIO()
        with patch('stt_preflight.os.getenv', return_value='private-api-key'), redirect_stdout(output):
            result = await preflight(adapter_factory=FailingAdapter, validation_interval=0)
        rendered = output.getvalue()
        self.assertEqual(result, 1)
        self.assertIn(
            'FAILURE: STT_RUNTIME_FAILED stage=callback error_type=TypeError status=none',
            rendered,
        )
        self.assertNotIn('SUCCESS', rendered)
        self.assertNotIn('private runtime details', rendered)
        self.assertNotIn('private-api-key', rendered)

    async def test_preflight_runtime_failure_during_close_exits_nonzero(self):
        class CloseFailureAdapter:
            def __init__(self, _api_key):
                pass

            async def start(self, _callback):
                pass

            async def wait_healthy(self, _interval):
                pass

            async def close(self):
                raise STTRuntimeError(
                    'connection_close', TypeError('private close details'),
                )

        output = io.StringIO()
        with patch('stt_preflight.os.getenv', return_value='private-api-key'), redirect_stdout(output):
            result = await preflight(adapter_factory=CloseFailureAdapter, validation_interval=0)
        self.assertEqual(result, 1)
        self.assertIn('stage=connection_close error_type=TypeError status=none', output.getvalue())
        self.assertNotIn('SUCCESS', output.getvalue())

    async def test_preflight_success_requires_healthy_interval_and_clean_close(self):
        calls = []

        class HealthyAdapter:
            def __init__(self, _api_key):
                pass

            async def start(self, _callback):
                calls.append('start')

            async def wait_healthy(self, interval):
                calls.append(('wait_healthy', interval))

            async def close(self):
                calls.append('close')

        output = io.StringIO()
        with patch('stt_preflight.os.getenv', return_value='private-api-key'), redirect_stdout(output):
            result = await preflight(adapter_factory=HealthyAdapter, validation_interval=0.25)
        self.assertEqual(result, 0)
        self.assertEqual(calls, ['start', ('wait_healthy', 0.25), 'close'])
        self.assertIn('receiver remained healthy', output.getvalue())


class MediaSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.network = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.store, _, _, association = staged()
        self.ws = FakeMediaWebSocket()
        self.stt = FakeSTT()
        self.audio = bytes((index % 254) + 1 for index in range(480))
        self.tts = FakeTTS([self.audio[:137], self.audio[137:331], self.audio[331:]])
        self.claude_stream = FakeStream(['Patient ', 'reply.', None])
        self.claude = FakeClient([self.claude_stream])
        self.session = MediaStreamSession(
            self.ws,
            association,
            load_scenario(),
            self.claude,
            self.stt,
            self.tts,
            self.store,
            AudioDiagnostics(False, ROOT / 'debug_audio', 'attempt-1'),
        )
        await self.session._handle_start(start_event())

    def _unstarted_session(self, store, association, *, websocket=None, stt=None):
        return MediaStreamSession(
            websocket or FakeMediaWebSocket(),
            association,
            load_scenario(),
            self.claude,
            stt or FakeSTT(),
            self.tts,
            store,
            AudioDiagnostics(False, ROOT / 'debug_audio', 'attempt-extra'),
        )

    async def asyncTearDown(self):
        await self.session._cancel_response('test_cleanup', clear=False)
        await self.stt.close()

    async def test_start_captures_actual_stream_call_and_format(self):
        self.assertEqual(self.session.stream_id, 'offline-stream')
        self.assertEqual(self.session.call_control_id, 'v3:offline-control')
        self.assertEqual(self.session.call_session_id, 'offline-session')
        self.assertEqual(self.session.user_id, 'offline-user')
        self.assertEqual(self.session.media_format, {
            'encoding': 'PCMU', 'sample_rate': 8000, 'channels': 1,
        })

    async def test_exact_documented_connected_start_media_and_stop_structures(self):
        store, placeholder, _, association = staged(client_state=DOCUMENTED_CLIENT_STATE)
        store.bind_accepted(placeholder, {
            'call_control_id': DOCUMENTED_CONTROL_ID,
            'call_leg_id': '2dc6fc34-f9e0-11ea-b68e-02420a0f7768',
            'call_session_id': DOCUMENTED_SESSION_ID,
        })
        events = [
            copy.deepcopy(DOCUMENTED_CONNECTED),
            copy.deepcopy(DOCUMENTED_START),
            copy.deepcopy(DOCUMENTED_MEDIA),
            copy.deepcopy(DOCUMENTED_STOP),
        ]
        websocket = ScriptedMediaWebSocket(events)
        stt = FakeSTT()
        session = self._unstarted_session(
            store, association, websocket=websocket, stt=stt,
        )
        await session.run()
        self.assertEqual(session.stream_id, DOCUMENTED_STREAM_ID)
        self.assertEqual(session.call_control_id, DOCUMENTED_CONTROL_ID)
        self.assertEqual(session.call_session_id, DOCUMENTED_SESSION_ID)
        self.assertTrue(session.audio_compatible)
        self.assertTrue(stt.closed)

    async def test_missing_start_stream_and_media_format_are_rejected(self):
        cases = []
        cases.append(({'event': 'start', 'stream_id': 'stream'}, 'start_not_object'))
        no_stream = start_event()
        no_stream.pop('stream_id')
        cases.append((no_stream, 'stream_id_missing'))
        typed_stream = start_event()
        typed_stream['stream_id'] = 123
        cases.append((typed_stream, 'stream_id_not_string'))
        no_format = start_event()
        no_format['start'].pop('media_format')
        cases.append((no_format, 'media_format_not_object'))
        for message, reason in cases:
            with self.subTest(reason=reason):
                store, _, _, association = staged()
                session = self._unstarted_session(store, association)
                with self.assertRaises(MediaValidationError) as caught:
                    await session._handle_start(message)
                self.assertEqual(caught.exception.reason_code, reason)

    async def test_duplicate_start_is_rejected(self):
        with self.assertRaises(MediaValidationError) as caught:
            await self.session._handle_start(start_event())
        self.assertEqual(caught.exception.reason_code, 'duplicate_start')

    async def test_start_rejection_carries_only_full_safe_boolean_diagnostics(self):
        store, _, _, association = staged()
        session = self._unstarted_session(store, association)
        message = start_event(
            client_state='raw-state-that-must-not-be-logged',
            call_control_id='raw-control-that-must-not-be-logged',
            call_session_id='raw-session-that-must-not-be-logged',
        )
        with self.assertRaises(MediaValidationError) as caught:
            await session._handle_start(message)
        diagnostic = format_media_validation_error(caught.exception)
        self.assertIn('reason_code=client_state_mismatch', diagnostic)
        self.assertIn('stream_id_present=true', diagnostic)
        self.assertIn('media_format_is_object=true', diagnostic)
        self.assertIn('encoding_match=true', diagnostic)
        for raw in (
            message['start']['client_state'], message['start']['call_control_id'],
            message['start']['call_session_id'], message['stream_id'],
        ):
            self.assertNotIn(raw, diagnostic)

    async def test_only_documented_pcmu_8000_mono_format_starts_stt(self):
        formats = (
            ({'encoding': 'PCMU', 'sample_rate': 8000, 'channels': 1}, True),
            ({'encoding': 'pcmu', 'sample_rate': 8000, 'channels': 1}, False),
            ({'encoding': 'PCMA', 'sample_rate': 8000, 'channels': 1}, False),
            ({'encoding': 'PCMU', 'sample_rate': 16000, 'channels': 1}, False),
            ({'encoding': 'PCMU', 'sample_rate': 8000, 'channels': 2}, False),
            ({'encoding': 'PCMU', 'sample_rate': True, 'channels': 1}, False),
        )
        for media_format, compatible in formats:
            with self.subTest(media_format=media_format):
                store, _, _, association = staged()
                stt = FakeSTT()
                session = self._unstarted_session(store, association, stt=stt)
                await session._handle_start(start_event(media_format=media_format))
                self.assertEqual(session.audio_compatible, compatible)
                self.assertEqual(stt.callback is not None, compatible)
                await stt.close()

    async def test_inbound_pcmu_reaches_stt_unchanged(self):
        raw = b'\xff\x7f\x01\x02' * 40
        await self.session._handle_media({
            'event': 'media', 'stream_id': 'offline-stream',
            'media': {'track': 'inbound', 'chunk': '1', 'timestamp': '0',
                      'payload': base64.b64encode(raw).decode()},
        })
        self.assertEqual(self.stt.audio, [raw])
        self.assertEqual(self.session.stats['inbound_media_frames'], 1)
        self.assertEqual(self.session.stats['inbound_media_bytes'], len(raw))
        self.assertEqual(self.session.stats['inbound_nonzero_bytes'], len(raw))
        self.assertEqual(self.session.stats['stt_pcm_bytes'], len(raw))
        self.assertEqual(self.session.stats['received_bytes'], len(raw))
        self.assertEqual(self.session.stats['stt_bytes'], len(raw))

    async def test_media_stream_close_emits_one_safe_counter_summary(self):
        raw = b'\x00\x01\xff\x00'
        store, _, _, association = staged()
        websocket = ScriptedMediaWebSocket([
            start_event(),
            {
                'event': 'media',
                'stream_id': 'offline-stream',
                'media': {'track': 'inbound', 'payload': base64.b64encode(raw).decode()},
            },
            {'event': 'stop', 'stream_id': 'offline-stream'},
        ])
        session = self._unstarted_session(store, association, websocket=websocket)
        with self.assertLogs('media_transport', level='INFO') as logs:
            await session.run()
        output = '\n'.join(logs.output)
        summary = [line for line in logs.output if 'MEDIA_STREAM_SUMMARY' in line]
        self.assertEqual(len(summary), 1)
        self.assertIn(
            'MEDIA_STREAM_SUMMARY stage=media_stream_close '
            'inbound_media_frames=1 inbound_media_bytes=4 inbound_nonzero_bytes=2 '
            'stt_pcm_bytes=4 stt_result_count=0 claude_request_count=0 '
            'claude_reply_count=0 elevenlabs_response_status=none '
            'elevenlabs_output_bytes=0 outbound_rtp_frames=0 outbound_rtp_bytes=0',
            output,
        )
        for private in ('offline-stream', 'offline-session', 'offline-client-state'):
            self.assertNotIn(private, output)

    async def test_session_stops_when_stt_receiver_fails_without_more_media(self):
        class FailedReceiverSTT(FakeSTT):
            async def wait_for_failure(self):
                raise STTRuntimeError(
                    'receive_frame', TypeError('private receiver details'),
                )

        store, _, _, association = staged()
        websocket = QueuedMediaWebSocket([start_event()])
        stt = FailedReceiverSTT()
        session = self._unstarted_session(
            store, association, websocket=websocket, stt=stt,
        )
        with self.assertRaises(STTRuntimeError) as caught:
            await asyncio.wait_for(session.run(), 1)
        self.assertEqual(caught.exception.stage, 'receive_frame')
        self.assertTrue(stt.closed)

    async def test_inbound_speech_barges_in_without_waiting_for_stt(self):
        blocked = FakeStream()
        self.session.anthropic_client = FakeClient([blocked])
        await self.session.handle_stt_result('First question', True)
        await self.session._flush_pending_stt_turn()
        await asyncio.wait_for(blocked.started.wait(), 2)
        speech = b'\x00' * 160
        await self.session._handle_media({
            'event': 'media', 'stream_id': 'offline-stream',
            'media': {'track': 'inbound', 'payload': base64.b64encode(speech).decode()},
        })
        await eventually(lambda: blocked.closed)
        self.assertEqual(self.ws.sent[-1], {'event': 'clear'})
        self.assertEqual(self.stt.audio[-1], speech)

    async def test_clinic_introduction_never_triggers_patient_filler(self):
        for fragment in (
            "This call may be recorded for quality and training purposes.",
            "Thanks for calling Pivot Point Orthopaedics.",
            "Part of Pretty Good AI.",
        ):
            await self.session.handle_stt_result(fragment, True)
            await self.session._flush_pending_stt_turn()

        self.assertEqual(self.claude.requests, [])
        self.assertEqual(self.session.messages, [])

        await self.session.handle_stt_result(
            "How may I help you today?",
            True,
        )
        self.assertEqual(self.claude.requests, [])

        await self.session._flush_pending_stt_turn()
        await eventually(lambda: self.claude_stream.closed)

        self.assertEqual(len(self.claude.requests), 1)
        self.assertEqual(
            self.session.messages[0],
            {"role": "user", "content": "How may I help you today?"},
        )

    async def test_interim_after_final_restarts_settle_timer(self):
        await self.session.handle_stt_result("How may I help", True)
        first_timer = self.session._stt_settle_task

        self.assertIsNotNone(first_timer)

        await self.session.handle_stt_result(
            "How may I help you today?",
            False,
        )
        restarted_timer = self.session._stt_settle_task

        self.assertIsNotNone(restarted_timer)
        self.assertIsNot(first_timer, restarted_timer)
        self.assertTrue(first_timer.done())
        self.assertFalse(restarted_timer.done())
        self.assertEqual(self.claude.requests, [])

        await self.session._flush_pending_stt_turn()
        await eventually(lambda: self.claude_stream.closed)

        self.assertEqual(len(self.claude.requests), 1)

    async def test_final_fragments_become_one_clinic_turn(self):
        await self.session.handle_stt_result(
            "I found several openings next",
            True,
        )
        await self.session.handle_stt_result(
            "week after 3 PM. Would Monday work?",
            True,
        )

        self.assertEqual(self.claude.requests, [])

        await self.session._flush_pending_stt_turn()
        await eventually(lambda: self.claude_stream.closed)

        self.assertEqual(len(self.claude.requests), 1)
        self.assertEqual(
            self.session.messages[0],
            {
                "role": "user",
                "content": (
                    "I found several openings next "
                    "week after 3 PM. Would Monday work?"
                ),
            },
        )

    async def test_partial_does_not_call_claude_and_final_calls_once(self):
        await self.session.handle_stt_result('How can I help', False)
        self.assertEqual(self.claude.requests, [])
        await self.session.handle_stt_result('How can I help you today?', True)
        await self.session._flush_pending_stt_turn()
        await eventually(lambda: self.claude_stream.closed)
        self.assertEqual(len(self.claude.requests), 1)
        self.assertEqual(self.session.stats['stt_result_count'], 2)
        self.assertEqual(self.claude.requests[0]['model'], 'claude-haiku-4-5-20251001')

    async def test_claude_text_uses_scenario_voice_and_ulaw_8000(self):
        await self.session.handle_stt_result('How can I help you today?', True)
        await self.session._flush_pending_stt_turn()
        await eventually(lambda: self.claude_stream.closed and bool(self.tts.requests))
        self.assertEqual(self.tts.requests, [{
            'text': 'Patient reply.',
            'voice_id': 'voice-01',
            'output_format': 'ulaw_8000',
        }])

    async def test_non_silent_audio_is_ordered_exact_and_never_text_only(self):
        await self.session.handle_stt_result('How can I help you today?', True)
        await self.session._flush_pending_stt_turn()
        await eventually(lambda: any(frame.get('event') == 'mark' for frame in self.ws.sent))
        media = [frame for frame in self.ws.sent if frame.get('event') == 'media']
        decoded = b''.join(base64.b64decode(frame['media']['payload']) for frame in media)
        self.assertEqual(decoded, self.audio)
        self.assertTrue(any(byte != 0xff for byte in decoded))
        self.assertEqual(self.session.stats['claude_request_count'], 1)
        self.assertEqual(self.session.stats['claude_reply_count'], 1)
        self.assertEqual(self.session.stats['stt_result_count'], 1)
        self.assertEqual(self.session.stats['elevenlabs_response_status'], 200)
        self.assertEqual(self.session.stats['elevenlabs_output_bytes'], len(self.audio))
        self.assertEqual(self.session.stats['outbound_rtp_bytes'], len(self.audio))
        self.assertGreater(self.session.stats['outbound_rtp_frames'], 0)
        self.assertEqual(self.session.stats['generated_bytes'], len(self.audio))
        self.assertEqual(self.session.stats['sent_bytes'], len(self.audio))
        self.assertFalse(any(frame.get('type') == 'text' for frame in self.ws.sent))

    async def test_playback_frames_are_exactly_160_bytes_ordered_and_paced(self):
        pacing = ManualPacing()
        source = bytes(index % 251 for index in range(480))
        self.session._clock = pacing.clock
        self.session._sleep = pacing.sleep
        self.tts.chunks = [source]

        await self.session.handle_stt_result('How can I help you today?', True)
        await self.session._flush_pending_stt_turn()
        await eventually(lambda: any(frame.get('event') == 'mark' for frame in self.ws.sent))

        media = [frame for frame in self.ws.sent if frame.get('event') == 'media']
        payloads = [frame['media']['payload'] for frame in media]
        self.assertEqual([len(base64.b64decode(payload)) for payload in payloads], [160, 160, 160])
        self.assertEqual(
            payloads,
            [base64.b64encode(source[offset:offset + 160]).decode() for offset in (0, 160, 320)],
        )
        self.assertEqual(pacing.delays, [0.02, 0.02])
        self.assertEqual(self.session.stats['outbound_rtp_frames'], 3)
        self.assertEqual(self.session.stats['outbound_rtp_bytes'], len(source))
        self.assertEqual(self.session.stats['generated_bytes'], len(source))
        self.assertEqual(self.session.stats['elevenlabs_output_bytes'], len(source))
        self.assertEqual(self.session.stats['claude_request_count'], 1)
        self.assertEqual(self.session.stats['claude_reply_count'], 1)

    async def test_final_partial_frame_is_flushed_without_padding(self):
        pacing = ManualPacing()
        source = bytes(index % 251 for index in range(321))
        self.session._clock = pacing.clock
        self.session._sleep = pacing.sleep
        self.tts.chunks = [source]

        await self.session.handle_stt_result('How can I help you today?', True)
        await self.session._flush_pending_stt_turn()
        await eventually(lambda: any(frame.get('event') == 'mark' for frame in self.ws.sent))

        media = [frame for frame in self.ws.sent if frame.get('event') == 'media']
        decoded = [base64.b64decode(frame['media']['payload']) for frame in media]
        self.assertEqual([len(frame) for frame in decoded], [160, 160, 1])
        self.assertEqual(b''.join(decoded), source)
        self.assertEqual(pacing.delays, [0.02, 0.02])
        self.assertEqual(self.session.stats['outbound_rtp_frames'], 3)
        self.assertEqual(self.session.stats['outbound_rtp_bytes'], len(source))

    async def test_cancellation_during_pacing_stops_without_sending_later_frames(self):
        pacing = BlockingPacing()
        self.session._clock = pacing.clock
        self.session._sleep = pacing.sleep
        self.tts.chunks = [b'P' * 320]

        await self.session.handle_stt_result('First question', True)
        await self.session._flush_pending_stt_turn()
        await asyncio.wait_for(pacing.started.wait(), 1)
        self.assertEqual(
            [frame for frame in self.ws.sent if frame.get('event') == 'media'],
            [{
                'event': 'media',
                'media': {'payload': base64.b64encode(b'P' * 160).decode()},
            }],
        )
        await self.session.handle_stt_result('Interrupting', False)
        self.assertEqual(self.session.stats['outbound_rtp_frames'], 1)
        self.assertEqual(self.session.stats['outbound_rtp_bytes'], 160)
        self.assertEqual(self.ws.sent[-1], {'event': 'clear'})

    async def test_mark_commits_reply_only_after_playback_and_clear_prevents_commit(self):
        await self.session.handle_stt_result('How can I help you today?', True)
        await self.session._flush_pending_stt_turn()
        await eventually(lambda: any(frame.get('event') == 'mark' for frame in self.ws.sent))
        self.assertEqual([item['role'] for item in self.session.messages], ['user'])
        mark = next(frame['mark']['name'] for frame in self.ws.sent if frame.get('event') == 'mark')
        await self.session.handle_stt_result('Wait', False)
        self.assertEqual(self.ws.sent[-1], {'event': 'clear'})
        self.session._handle_mark({
            'event': 'mark', 'stream_id': 'offline-stream', 'mark': {'name': mark},
        })
        self.assertEqual([item['role'] for item in self.session.messages], ['user'])
        self.assertEqual(self.session.stats['clears'], 1)

    async def test_barge_in_cancels_active_claude_and_sends_clear(self):
        blocked = FakeStream()
        self.session.anthropic_client = FakeClient([blocked])
        await self.session.handle_stt_result('First question', True)
        await self.session._flush_pending_stt_turn()
        await asyncio.wait_for(blocked.started.wait(), 2)
        await self.session.handle_stt_result('Interrupting', False)
        await eventually(lambda: blocked.closed)
        self.assertEqual(self.ws.sent[-1], {'event': 'clear'})
        self.assertEqual(self.session.stats['clears'], 1)

    async def test_incompatible_start_never_connects_stt(self):
        store, _, _, association = staged()
        stt = FakeSTT()
        session = MediaStreamSession(
            FakeMediaWebSocket(), association, load_scenario(), self.claude, stt, self.tts,
            store, AudioDiagnostics(False, ROOT / 'debug_audio', 'attempt-2'),
        )
        await session._handle_start(start_event(
            media_format={'encoding': 'L16', 'sample_rate': 16000, 'channels': 1},
        ))
        self.assertFalse(session.audio_compatible)
        self.assertIsNone(stt.callback)

    def test_stt_uses_validated_deepgram_nova3_raw_linear16_contract(self):
        self.assertEqual(TELNYX_STT_CONFIG, {
            'transcription_engine': 'Deepgram',
            'model': 'nova-3',
            'input_format': 'linear16',
            'sample_rate': '16000',
            'language': 'en-US',
            'interim_results': 'true',
            'endpointing': '300',
        })


if __name__ == '__main__':
    unittest.main()
