"""Authenticated Telnyx PCMU media streaming and live provider adapters."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import secrets
import ssl
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable
from urllib.parse import quote, urlencode
from uuid import uuid4

import certifi
import httpx
from websockets.exceptions import ConnectionClosed


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
if not LOGGER.handlers:
    LOGGER.addHandler(logging.StreamHandler())
TELNYX_STT_URL = "wss://api.telnyx.com/v2/speech-to-text/transcription"
TELNYX_STT_CONFIG = {
    "transcription_engine": "Deepgram",
    "model": "nova-3",
    "input_format": "linear16",
    "sample_rate": "16000",
    "language": "en-US",
    "interim_results": "true",
    "endpointing": "300",
}
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
ELEVENLABS_OUTPUT_FORMAT = "ulaw_8000"
PCMU_FRAME_BYTES = 160  # 20 ms at 8 kHz, one byte per sample.
PCMU_FRAME_INTERVAL_SECONDS = PCMU_FRAME_BYTES / 8000
# Deepgram endpointing is 300 ms; this adds 500 ms for a complete turn.
STT_TURN_SETTLE_SECONDS = 0.25


_MEDIA_VALIDATION_MESSAGES = {
    "duplicate_start": "duplicate start event",
    "start_not_object": "start field is missing or is not an object",
    "stream_id_missing": "stream_id is missing or empty",
    "stream_id_not_string": "stream_id is not a string",
    "client_state_missing": "client_state is missing or empty",
    "client_state_not_string": "client_state is not a string",
    "client_state_mismatch": "client_state does not match the staged attempt",
    "call_control_id_missing": "call_control_id is missing or empty",
    "call_control_id_not_string": "call_control_id is not a string",
    "call_session_id_missing": "call_session_id is missing or empty",
    "call_session_id_not_string": "call_session_id is not a string",
    "call_session_id_mismatch": "call_session_id does not match the accepted call",
    "media_format_not_object": "media_format is missing or is not an object",
    "staged_credential_missing": "staged media credential is unavailable at acceptance",
    "accepted_call_session_id_missing": "accepted call has no usable call_session_id",
    "association_rejected": "media association was rejected after start",
}


class MediaValidationError(ValueError):
    """A media-association failure containing only safe diagnostic facts."""

    def __init__(self, reason_code: str, facts: dict[str, bool] | None = None):
        self.reason_code = reason_code
        self.reason_message = _MEDIA_VALIDATION_MESSAGES.get(
            reason_code, "media validation failed"
        )
        self.facts = dict(facts or {})
        super().__init__(self.reason_message)


class STTTransportError(Exception):
    """Credential-safe details for an STT transport failure."""

    def __init__(self, stage: str, error: Exception):
        self.stage = stage
        self.error_type = type(error).__name__
        self.status = _exception_status(error)
        super().__init__(format_stt_transport_error(self))


class STTStartError(STTTransportError):
    """A failure while connecting or initializing the STT stream."""


class STTRuntimeError(STTTransportError):
    """A failure while using an initialized STT stream."""


class ProviderError(Exception):
    """Marker for a provider rejection whose response body must remain private."""


class UnexpectedConnectionClose(Exception):
    """Marker for a WebSocket that ended before its owner requested shutdown."""


def _exception_status(error: Exception) -> int | None:
    """Extract only a numeric HTTP status, never provider response content."""
    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def format_stt_transport_error(error: STTTransportError) -> str:
    """Render only allowlisted STT failure metadata for logs and CLI output."""
    status = "none" if error.status is None else str(error.status)
    return f"stage={error.stage} error_type={error.error_type} status={status}"


def format_media_validation_error(error: MediaValidationError) -> str:
    """Render a credential-safe, identifier-free diagnostic for application logs."""
    facts = " ".join(
        f"{key}={'true' if value else 'false'}" for key, value in sorted(error.facts.items())
    )
    suffix = f" {facts}" if facts else ""
    return (
        f"reason_code={error.reason_code} reason_message={error.reason_message!r}{suffix}"
    )


@dataclass
class StagedMediaToken:
    placeholder_id: str
    token_digest: bytes
    scenario_id: str
    attempt_id: str
    client_state: str
    voice_id: str
    expires_at: float
    secondary_voice_id: str | None = None
    used: bool = False
    accepted_ids: dict[str, str] = field(default_factory=dict)
    started_ids: dict[str, str] = field(default_factory=dict)
    start_facts: dict[str, bool] = field(default_factory=dict)
    association_rejected: bool = False


def _nonempty_string(value) -> bool:
    return isinstance(value, str) and bool(value)


def _safe_start_facts(
    entry: StagedMediaToken,
    start: dict,
    *,
    message: dict | None = None,
) -> dict[str, bool]:
    """Return presence/type/match facts only; never return field values."""
    client_state = start.get("client_state")
    call_control_id = start.get("call_control_id")
    call_session_id = start.get("call_session_id")
    accepted_control = entry.accepted_ids.get("call_control_id")
    accepted_session = entry.accepted_ids.get("call_session_id")
    media_format = start.get("media_format")
    media = media_format if isinstance(media_format, dict) else {}
    stream_id = message.get("stream_id") if isinstance(message, dict) else None
    return {
        "acceptance_bound": _nonempty_string(accepted_session),
        "call_control_id_accepted_present": _nonempty_string(accepted_control),
        "call_control_id_is_string": isinstance(call_control_id, str),
        "call_control_id_match": (
            _nonempty_string(call_control_id)
            and _nonempty_string(accepted_control)
            and secrets.compare_digest(call_control_id, accepted_control)
        ),
        "call_control_id_present": call_control_id is not None and call_control_id != "",
        "call_session_id_accepted_present": _nonempty_string(accepted_session),
        "call_session_id_is_string": isinstance(call_session_id, str),
        "call_session_id_match": (
            _nonempty_string(call_session_id)
            and _nonempty_string(accepted_session)
            and secrets.compare_digest(call_session_id, accepted_session)
        ),
        "call_session_id_present": call_session_id is not None and call_session_id != "",
        "channels_match": (
            isinstance(media.get("channels"), int)
            and not isinstance(media.get("channels"), bool)
            and media.get("channels") == 1
        ),
        "client_state_is_string": isinstance(client_state, str),
        "client_state_match": (
            _nonempty_string(client_state)
            and secrets.compare_digest(client_state, entry.client_state)
        ),
        "client_state_present": client_state is not None and client_state != "",
        "encoding_match": media.get("encoding") == "PCMU",
        "media_format_is_object": isinstance(media_format, dict),
        "media_format_present": media_format is not None,
        "sample_rate_match": (
            isinstance(media.get("sample_rate"), int)
            and not isinstance(media.get("sample_rate"), bool)
            and media.get("sample_rate") == 8000
        ),
        "start_is_object": isinstance(message.get("start"), dict) if message is not None else True,
        "start_present": "start" in message if message is not None else True,
        "stream_id_is_string": isinstance(stream_id, str) if message is not None else False,
        "stream_id_present": (
            stream_id is not None and stream_id != "" if message is not None else False
        ),
    }


class MediaTokenStore:
    """Single-process, one-use WebSocket credentials staged before dialing."""

    def __init__(self, *, ttl_seconds: float = 300, clock=time.monotonic):
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._tokens: dict[str, StagedMediaToken] = {}

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    def stage(
        self,
        scenario_id: str,
        attempt_id: str,
        client_state: str,
        voice_id: str,
        *,
        secondary_voice_id: str | None = None,
    ):
        token = secrets.token_urlsafe(32)
        placeholder_id = uuid4().hex
        entry = StagedMediaToken(
            placeholder_id=placeholder_id,
            token_digest=self._digest(token),
            scenario_id=scenario_id,
            attempt_id=attempt_id,
            client_state=client_state,
            voice_id=voice_id,
            expires_at=self.clock() + self.ttl_seconds,
            secondary_voice_id=secondary_voice_id,
        )
        with self._lock:
            self._purge_expired_locked()
            self._tokens[placeholder_id] = entry
        return placeholder_id, token

    def consume(self, placeholder_id: str, token: str) -> StagedMediaToken | None:
        if not isinstance(placeholder_id, str) or not isinstance(token, str):
            return None
        with self._lock:
            self._purge_expired_locked()
            entry = self._tokens.get(placeholder_id)
            if entry is None or entry.used:
                return None
            if not secrets.compare_digest(entry.token_digest, self._digest(token)):
                return None
            entry.used = True
            return entry

    def bind_accepted(self, placeholder_id: str, payload: dict) -> None:
        accepted = {
            key: payload[key]
            for key in ("call_control_id", "call_leg_id", "call_session_id")
            if isinstance(payload.get(key), str) and payload[key]
        }
        with self._lock:
            entry = self._tokens.get(placeholder_id)
            if entry is None:
                raise MediaValidationError("staged_credential_missing")
            entry.accepted_ids = accepted
            accepted_session = accepted.get("call_session_id")
            started_session = entry.started_ids.get("call_session_id")
            facts = dict(entry.start_facts)
            association_facts = _safe_start_facts(entry, entry.started_ids)
            for key in (
                "acceptance_bound",
                "call_control_id_accepted_present",
                "call_control_id_match",
                "call_session_id_accepted_present",
                "call_session_id_match",
            ):
                facts[key] = association_facts[key]
            if accepted_session is None:
                entry.association_rejected = True
                raise MediaValidationError("accepted_call_session_id_missing", facts)
            if started_session is not None and not secrets.compare_digest(
                started_session, accepted_session
            ):
                entry.association_rejected = True
                raise MediaValidationError("call_session_id_mismatch", facts)

    def validate_start(
        self,
        entry: StagedMediaToken,
        start: dict,
        *,
        facts: dict[str, bool] | None = None,
    ) -> None:
        with self._lock:
            diagnostic = dict(facts or _safe_start_facts(entry, start))
            entry.start_facts = diagnostic
            if entry.association_rejected:
                raise MediaValidationError("association_rejected", diagnostic)
            client_state = start.get("client_state")
            if client_state is None or client_state == "":
                raise MediaValidationError("client_state_missing", diagnostic)
            if not isinstance(client_state, str):
                raise MediaValidationError("client_state_not_string", diagnostic)
            if not secrets.compare_digest(client_state, entry.client_state):
                raise MediaValidationError("client_state_mismatch", diagnostic)

            started = {}
            for key in ("call_control_id", "call_session_id"):
                value = start.get(key)
                if value is None or value == "":
                    raise MediaValidationError(f"{key}_missing", diagnostic)
                if not isinstance(value, str):
                    raise MediaValidationError(f"{key}_not_string", diagnostic)
                started[key] = value

            # call_control_id controls one leg and may legitimately differ for the
            # streamed leg. call_session_id is the stable call-wide association.
            expected_session = entry.accepted_ids.get("call_session_id")
            if expected_session is not None and not secrets.compare_digest(
                started["call_session_id"], expected_session
            ):
                raise MediaValidationError("call_session_id_mismatch", diagnostic)
            entry.started_ids = started

    def validate_active(self, entry: StagedMediaToken) -> None:
        """Reject media if a later dial-acceptance bind disproved the session."""
        with self._lock:
            if entry.association_rejected:
                raise MediaValidationError("association_rejected", entry.start_facts)

    def discard(self, placeholder_id: str) -> None:
        with self._lock:
            self._tokens.pop(placeholder_id, None)

    def has_pending(self, placeholder_id: str) -> bool:
        with self._lock:
            self._purge_expired_locked()
            return placeholder_id in self._tokens

    def _purge_expired_locked(self) -> None:
        current = self.clock()
        expired = [key for key, value in self._tokens.items() if value.expires_at < current]
        for key in expired:
            self._tokens.pop(key, None)


class TelnyxStreamingSTT:
    """PCMU-input adapter for Telnyx-hosted Deepgram STT via raw 16 kHz PCM."""

    def __init__(
        self,
        api_key: str,
        *,
        connect=None,
        on_audio_sent=None,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.api_key = api_key
        self._connect = connect
        self._on_audio_sent = on_audio_sent
        self._ssl_context = (
            ssl_context
            if ssl_context is not None
            else ssl.create_default_context(cafile=certifi.where())
        )
        self._websocket = None
        self._receiver: asyncio.Task | None = None
        self._on_result: Callable[[str, bool], Awaitable[None]] | None = None
        self._previous_sample: int | None = None
        self.last_audio_bytes = 0
        self._runtime_error: STTRuntimeError | None = None
        self._closing = False

    @property
    def url(self) -> str:
        return f"{TELNYX_STT_URL}?{urlencode(TELNYX_STT_CONFIG)}"

    async def start(self, on_result: Callable[[str, bool], Awaitable[None]]) -> None:
        self._closing = False
        self._runtime_error = None
        if self._connect is None:
            from websockets.asyncio.client import connect

            self._connect = connect
        self._on_result = on_result
        try:
            self._websocket = await self._connect(
                self.url,
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
                ssl=self._ssl_context,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise STTStartError("tls_connect", exc) from None
        # linear16 is a raw format: send only PCM audio frames, with no WAV header.
        self._receiver = asyncio.create_task(self._receive(self._websocket))

    async def send_audio(self, audio: bytes) -> None:
        self.raise_if_failed()
        if self._websocket is None:
            raise RuntimeError("STT stream has not started")
        pcm, self._previous_sample = _pcmu_to_pcm16_16khz(audio, self._previous_sample)
        try:
            await self._websocket.send(pcm)
            self.last_audio_bytes = len(pcm)
            if self._on_audio_sent is not None:
                self._on_audio_sent(pcm)
        except asyncio.CancelledError:
            raise
        except STTRuntimeError:
            raise
        except Exception as exc:
            raise STTRuntimeError("audio_send", exc) from None
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        """Synchronously surface a receiver failure before more audio is sent."""
        if self._runtime_error is not None:
            raise self._runtime_error
        receiver = self._receiver
        if receiver is None or not receiver.done() or self._closing:
            return
        if receiver.cancelled():
            failure = STTRuntimeError("connection_close", UnexpectedConnectionClose())
        else:
            exception = receiver.exception()
            if isinstance(exception, STTRuntimeError):
                failure = exception
            elif isinstance(exception, Exception):
                failure = STTRuntimeError("receive_frame", exception)
            else:
                failure = STTRuntimeError("connection_close", UnexpectedConnectionClose())
        self._runtime_error = failure
        raise failure

    async def wait_healthy(self, validation_interval: float) -> None:
        """Return after a healthy interval, or raise the receiver's exact failure."""
        self.raise_if_failed()
        receiver = self._receiver
        if receiver is None:
            raise RuntimeError("STT stream has not started")
        try:
            await asyncio.wait_for(asyncio.shield(receiver), validation_interval)
        except TimeoutError:
            self.raise_if_failed()
            return
        self.raise_if_failed()

    async def wait_for_failure(self) -> None:
        """Let an owning session race its input stream against STT receiver death."""
        self.raise_if_failed()
        receiver = self._receiver
        if receiver is None:
            raise RuntimeError("STT stream has not started")
        await asyncio.shield(receiver)
        self.raise_if_failed()

    async def _receive(self, websocket=None) -> None:
        websocket = self._websocket if websocket is None else websocket
        try:
            try:
                iterator = websocket.__aiter__()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise STTRuntimeError("receive_frame", exc) from None

            while True:
                try:
                    raw = await anext(iterator)
                except StopAsyncIteration:
                    if self._closing:
                        return
                    raise STTRuntimeError(
                        "connection_close", UnexpectedConnectionClose(),
                    ) from None
                except asyncio.CancelledError:
                    raise
                except ConnectionClosed as exc:
                    if self._closing:
                        return
                    raise STTRuntimeError("connection_close", exc) from None
                except Exception as exc:
                    raise STTRuntimeError("receive_frame", exc) from None

                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    LOGGER.warning(
                        "STT event ignored stage=json_decode error_type=%s",
                        type(exc).__name__,
                    )
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise STTRuntimeError("json_decode", exc) from None

                if not isinstance(message, dict):
                    raise STTRuntimeError("message_shape", TypeError()) from None

                if "errors" in message:
                    errors = message.get("errors")
                    if not isinstance(errors, list) or not errors:
                        raise STTRuntimeError("message_shape", TypeError()) from None
                    raise STTRuntimeError("provider_event", ProviderError()) from None

                if "transcript" not in message:
                    continue
                transcript = message.get("transcript")
                is_final = message.get("is_final")
                if not isinstance(transcript, str) or not isinstance(is_final, bool):
                    raise STTRuntimeError("message_shape", TypeError()) from None
                transcript = transcript.strip()
                if not transcript:
                    continue
                if self._on_result is not None:
                    try:
                        await self._on_result(transcript, is_final)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise STTRuntimeError("callback", exc) from None
        except asyncio.CancelledError:
            raise
        except STTRuntimeError as failure:
            self._runtime_error = failure
            LOGGER.error("STT_RUNTIME_FAILED %s", format_stt_transport_error(failure))
            raise

    async def _close_websocket(self) -> None:
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                pass

    async def close(self) -> None:
        receiver, websocket = self._receiver, self._websocket
        self._closing = True
        self._websocket = None
        close_failure = None
        if websocket is not None:
            try:
                await websocket.close()
            except Exception as exc:
                close_failure = STTRuntimeError("connection_close", exc)
        if receiver is not None:
            if not receiver.done():
                receiver.cancel()
            results = await asyncio.gather(receiver, return_exceptions=True)
            if self._runtime_error is None and results:
                result = results[0]
                if isinstance(result, STTRuntimeError):
                    self._runtime_error = result
        self._receiver = None
        if self._runtime_error is not None:
            raise self._runtime_error
        if close_failure is not None:
            raise close_failure


class ElevenLabsTTS:
    """Direct ElevenLabs streaming TTS returning telephony-native μ-law bytes."""

    def __init__(self, api_key: str, model_id: str, *, client_factory=httpx.AsyncClient):
        self.api_key = api_key
        self.model_id = model_id
        self.client_factory = client_factory
        self.last_response_status: int | None = None
        self._client_context = None
        self._client = None

    async def _get_client(self):
        if self._client is None:
            self._client_context = self.client_factory(
                timeout=30,
                follow_redirects=False,
                trust_env=False,
            )
            self._client = await self._client_context.__aenter__()
        return self._client

    async def close(self) -> None:
        context = self._client_context
        self._client_context = None
        self._client = None
        if context is not None:
            await context.__aexit__(None, None, None)

    async def stream(self, text: str, voice_id: str) -> AsyncIterator[bytes]:
        url = ELEVENLABS_TTS_URL.format(voice_id=quote(voice_id, safe=""))
        self.last_response_status = None
        client = await self._get_client()
        async with client.stream(
                "POST",
                url,
                params={"output_format": ELEVENLABS_OUTPUT_FORMAT},
                headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
                json={
                    "text": text,
                    "model_id": self.model_id,
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75,
                        "style": 0.0,
                        "use_speaker_boost": True,
                        "speed": 1.0,
                    },
                },
            ) as response:
                status = getattr(response, "status_code", None)
                self.last_response_status = (
                    status if isinstance(status, int) and not isinstance(status, bool) else None
                )
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk


class AudioDiagnostics:
    """Opt-in raw audio capture kept strictly outside the evidence tree."""

    FILES = {
        "telnyx_in": "telnyx_received.ulaw",
        "stt_out": "stt_sent.wav",
        "elevenlabs_in": "elevenlabs_received.ulaw",
        "telnyx_out": "telnyx_sent.ulaw",
    }

    def __init__(self, enabled: bool, root: Path, attempt_id: str):
        self.directory: Path | None = None
        self._files = {}
        if enabled:
            safe_attempt = "".join(c for c in attempt_id if c.isalnum())[:64]
            self.directory = root / safe_attempt
            self.directory.mkdir(parents=True, exist_ok=False)
            self._files = {
                key: (self.directory / filename).open("xb") for key, filename in self.FILES.items()
            }

    def write(self, kind: str, audio: bytes) -> None:
        output = self._files.get(kind)
        if output is not None:
            output.write(audio)
            output.flush()

    def close(self) -> None:
        for output in self._files.values():
            output.close()
        self._files = {}


class MediaStreamSession:
    """One Telnyx media stream, one STT stream, and serialized response playback."""

    def __init__(
        self,
        websocket,
        association: StagedMediaToken,
        scenario,
        anthropic_client,
        stt,
        tts,
        token_store: MediaTokenStore,
        diagnostics: AudioDiagnostics,
        *,
        clock=time.monotonic,
        sleeper=asyncio.sleep,
    ):
        self.websocket = websocket
        self.association = association
        self.scenario = scenario
        self.anthropic_client = anthropic_client
        self.stt = stt
        self.tts = tts
        self.token_store = token_store
        self.diagnostics = diagnostics
        self._clock = clock
        self._sleep = sleeper
        self.messages: list[dict[str, str]] = []
        self.stream_id: str | None = None
        self.call_control_id: str | None = None
        self.call_session_id: str | None = None
        self.user_id: str | None = None
        self.media_format: dict | None = None
        self.audio_compatible = False
        self.stt_started = False
        self.generation = 0
        self.active_task: asyncio.Task | None = None
        self.pending_reply: dict | None = None
        self.secondary_voice_active = False
        self._stt_final_parts: list[str] = []
        self._stt_settle_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._summary_logged = False
        self.stats = {
            "inbound_media_frames": 0,
            "inbound_media_bytes": 0,
            "inbound_nonzero_bytes": 0,
            "stt_pcm_bytes": 0,
            "stt_result_count": 0,
            "claude_request_count": 0,
            "claude_reply_count": 0,
            "elevenlabs_response_status": None,
            "elevenlabs_output_bytes": 0,
            "outbound_rtp_frames": 0,
            "outbound_rtp_bytes": 0,
            "received_bytes": 0,
            "stt_bytes": 0,
            "generated_bytes": 0,
            "sent_bytes": 0,
            "clears": 0,
            "completed_playbacks": 0,
        }

    async def run(self) -> None:
        try:
            while True:
                message = await self._receive_media_message()
                if not isinstance(message, dict):
                    LOGGER.warning("TELNYX MEDIA event=invalid_message")
                    continue
                event = message.get("event")
                safe_event = event if event in {
                    "connected", "start", "media", "mark", "dtmf", "stop", "error"
                } else "unknown"
                if safe_event != "media":
                    LOGGER.info("TELNYX MEDIA event=%s", safe_event)
                if safe_event == "start":
                    await self._handle_start(message)
                elif safe_event == "media":
                    await self._handle_media(message)
                elif safe_event == "mark":
                    self._handle_mark(message)
                elif safe_event == "dtmf":
                    digit = message.get("dtmf", {}).get("digit")
                    LOGGER.info("TELNYX MEDIA dtmf present=%s", isinstance(digit, str) and bool(digit))
                elif safe_event == "error":
                    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                    LOGGER.error(
                        "TELNYX MEDIA error code=%s title=%s",
                        payload.get("code", "unspecified"), str(payload.get("title", "unspecified"))[:120],
                    )
                elif safe_event == "stop":
                    break
        finally:
            try:
                await self._discard_pending_stt_turn()
                await self._cancel_response("stream_closed", clear=False)
            finally:
                try:
                    await self.stt.close()
                finally:
                    try:
                        close_tts = getattr(self.tts, "close", None)
                        if close_tts is not None:
                            await close_tts()
                    finally:
                        try:
                            self.diagnostics.close()
                        finally:
                            self._log_summary()

    def _log_summary(self) -> None:
        if self._summary_logged:
            return
        self._summary_logged = True
        LOGGER.info(
            "MEDIA_STREAM_SUMMARY stage=media_stream_close "
            "inbound_media_frames=%s inbound_media_bytes=%s "
            "inbound_nonzero_bytes=%s stt_pcm_bytes=%s stt_result_count=%s "
            "claude_request_count=%s claude_reply_count=%s "
            "elevenlabs_response_status=%s elevenlabs_output_bytes=%s "
            "outbound_rtp_frames=%s outbound_rtp_bytes=%s",
            self.stats["inbound_media_frames"],
            self.stats["inbound_media_bytes"],
            self.stats["inbound_nonzero_bytes"],
            self.stats["stt_pcm_bytes"],
            self.stats["stt_result_count"],
            self.stats["claude_request_count"],
            self.stats["claude_reply_count"],
            self.stats["elevenlabs_response_status"]
            if self.stats["elevenlabs_response_status"] is not None else "none",
            self.stats["elevenlabs_output_bytes"],
            self.stats["outbound_rtp_frames"],
            self.stats["outbound_rtp_bytes"],
        )

    async def _receive_media_message(self):
        """Stop the media session promptly if its production STT receiver dies."""
        wait_for_failure = getattr(self.stt, "wait_for_failure", None)
        if not self.stt_started or wait_for_failure is None:
            return await self.websocket.receive_json()

        media_task = asyncio.create_task(self.websocket.receive_json())
        stt_task = asyncio.create_task(wait_for_failure())
        try:
            done, _ = await asyncio.wait(
                (media_task, stt_task), return_when=asyncio.FIRST_COMPLETED,
            )
            if stt_task in done:
                if not media_task.done():
                    media_task.cancel()
                    await asyncio.gather(media_task, return_exceptions=True)
                return await stt_task
            return await media_task
        finally:
            if not stt_task.done():
                stt_task.cancel()
                await asyncio.gather(stt_task, return_exceptions=True)

    async def _handle_start(self, message: dict) -> None:
        start = message.get("start")
        start_object = start if isinstance(start, dict) else {}
        facts = _safe_start_facts(
            self.association, start_object, message=message,
        )
        if self.stream_id is not None:
            raise MediaValidationError("duplicate_start", facts)
        if not isinstance(start, dict):
            raise MediaValidationError("start_not_object", facts)
        stream_id = message.get("stream_id")
        if stream_id is None or stream_id == "":
            raise MediaValidationError("stream_id_missing", facts)
        if not isinstance(stream_id, str):
            raise MediaValidationError("stream_id_not_string", facts)
        self.token_store.validate_start(self.association, start, facts=facts)
        media_format = start.get("media_format")
        if not isinstance(media_format, dict):
            raise MediaValidationError("media_format_not_object", facts)
        self.stream_id = stream_id
        self.call_control_id = start["call_control_id"]
        self.call_session_id = start["call_session_id"]
        self.user_id = start.get("user_id") if isinstance(start.get("user_id"), str) else None
        self.media_format = dict(media_format)
        encoding = media_format.get("encoding")
        sample_rate = media_format.get("sample_rate")
        channels = media_format.get("channels")
        self.audio_compatible = (
            encoding == "PCMU"
            and isinstance(sample_rate, int)
            and not isinstance(sample_rate, bool)
            and sample_rate == 8000
            and isinstance(channels, int)
            and not isinstance(channels, bool)
            and channels == 1
        )
        LOGGER.info(
            "TELNYX MEDIA start accepted acceptance_bound=%s "
            "call_control_id_match=%s call_session_id_match=%s audio_format_match=%s",
            facts["acceptance_bound"], facts["call_control_id_match"],
            facts["call_session_id_match"], self.audio_compatible,
        )
        if not self.audio_compatible:
            LOGGER.warning("TELNYX MEDIA incompatible_audio expected=PCMU/8000/mono; audio_processing=stopped")
            return
        await self.stt.start(self.handle_stt_result)
        self.stt_started = True

    async def _handle_media(self, message: dict) -> None:
        self.token_store.validate_active(self.association)
        if not self.audio_compatible or not self.stt_started:
            return
        if message.get("stream_id") != self.stream_id:
            LOGGER.warning("TELNYX MEDIA ignored reason=stream_mismatch")
            return
        media = message.get("media")
        if not isinstance(media, dict) or media.get("track") not in ("inbound", "inbound_track"):
            return
        payload = media.get("payload")
        if not isinstance(payload, str):
            return
        try:
            audio = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            LOGGER.warning("TELNYX MEDIA ignored reason=invalid_base64")
            return
        self.stats["inbound_media_frames"] += 1
        self.stats["inbound_media_bytes"] += len(audio)
        self.stats["received_bytes"] += len(audio)
        self.stats["inbound_nonzero_bytes"] += sum(byte != 0 for byte in audio)
        if not audio:
            return
        self.diagnostics.write("telnyx_in", audio)
        if self._response_in_progress() and _pcmu_has_speech(audio):
            await self._cancel_response("barge_in_audio", clear=True)
        await self.stt.send_audio(audio)
        pcm_bytes = getattr(self.stt, "last_audio_bytes", None)
        if isinstance(pcm_bytes, int) and not isinstance(pcm_bytes, bool):
            self.stats["stt_pcm_bytes"] += pcm_bytes
            self.stats["stt_bytes"] += pcm_bytes

    async def handle_stt_result(self, transcript: str, is_final: bool) -> None:
        transcript = transcript.strip()
        if not transcript:
            return

        self.stats["stt_result_count"] += 1
        LOGGER.info("STT result final=%s characters=%s", is_final, len(transcript))

        await self._cancel_stt_settle_task()

        if not is_final:
            if self._response_in_progress():
                await self._cancel_response("barge_in", clear=True)

            # Speech continued after a finalized fragment. Restart the timer
            # so the buffered turn cannot become stranded indefinitely.
            if self._stt_final_parts:
                self._stt_settle_task = asyncio.create_task(
                    self._settle_and_flush_stt_turn()
                )
            return

        if self._response_in_progress():
            await self._cancel_response("new_final_transcript", clear=True)

        self._append_stt_final_part(transcript)

        # Deepgram punctuation indicates a complete conversational turn.
        # Flush it immediately; retain the settle timer only for fragments.
        if transcript.rstrip().endswith((".", "?", "!")):
            await self._flush_pending_stt_turn()
        else:
            self._stt_settle_task = asyncio.create_task(
                self._settle_and_flush_stt_turn()
            )

    def _append_stt_final_part(self, transcript: str) -> None:
        if not self._stt_final_parts:
            self._stt_final_parts.append(transcript)
            return

        previous = self._stt_final_parts[-1]

        if transcript == previous or previous.startswith(transcript):
            return

        if transcript.startswith(previous):
            self._stt_final_parts[-1] = transcript
            return

        self._stt_final_parts.append(transcript)

    async def _cancel_stt_settle_task(self) -> None:
        task = self._stt_settle_task
        self._stt_settle_task = None

        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _settle_and_flush_stt_turn(self) -> None:
        try:
            await asyncio.sleep(STT_TURN_SETTLE_SECONDS)
        except asyncio.CancelledError:
            return

        if self._stt_settle_task is asyncio.current_task():
            self._stt_settle_task = None

        await self._flush_pending_stt_turn()

    async def _discard_pending_stt_turn(self) -> None:
        await self._cancel_stt_settle_task()
        self._stt_final_parts.clear()

    async def _flush_pending_stt_turn(self) -> None:
        await self._cancel_stt_settle_task()

        parts = self._stt_final_parts
        self._stt_final_parts = []

        if not parts:
            return

        accepted_parts = []

        for part in parts:
            normalized = " ".join(part.lower().split())

            contains_opening_question = any(
                phrase in normalized
                for phrase in (
                    "how may i help",
                    "how can i help",
                    "what can i help",
                    "what brings you",
                    "what are you calling",
                )
            )

            preamble_reason = _preamble_reason(part)
            if preamble_reason is not None and not contains_opening_question:
                _log_dialogue("PGAI", part, preamble_reason)
                continue

            is_opening_fragment = any(
                phrase in normalized
                for phrase in (
                    "thanks for calling",
                    "thank you for calling",
                    "part of pretty good ai",
                    "pivot point orthopaedics",
                    "pivot point orthopedics",
                )
            )

            if (
                not self.messages
                and is_opening_fragment
                and not contains_opening_question
            ):
                _log_dialogue("PGAI", part, "clinic_introduction")
                continue

            accepted_parts.append(part)

        transcript = " ".join(accepted_parts).strip()
        if not transcript:
            return

        _log_dialogue("PGAI", transcript)
        self.messages.append({"role": "user", "content": transcript})

        turn = self.generation
        history = [dict(item) for item in self.messages]
        turn_ready_at = self._clock()

        LOGGER.info(
            "STT turn ready fragments=%s characters=%s",
            len(accepted_parts),
            len(transcript),
        )

        self.active_task = asyncio.create_task(
            self._generate_and_play(
                turn,
                history,
                turn_ready_at=turn_ready_at,
            )
        )

    async def _generate_and_play(
        self,
        turn: int,
        history: list[dict[str, str]],
        *,
        turn_ready_at: float | None = None,
    ) -> None:
        text_parts = []
        audio_buffer = bytearray()
        generated_bytes = 0
        sent_bytes = 0
        next_frame_at: float | None = None
        mark_name = f"assistant-{turn}-{uuid4().hex}"
        try:
            self.stats["claude_request_count"] += 1
            async with self.anthropic_client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=96,
                system=self.scenario.system_prompt,
                messages=history,
            ) as stream:
                async for text in stream.text_stream:
                    if turn != self.generation:
                        return
                    if text:
                        text_parts.append(text)
                final = await stream.get_final_message()
            if turn != self.generation or final.stop_reason != "end_turn":
                return
            raw_reply = "".join(text_parts).strip()
            if not raw_reply:
                return

            marker = "[[VOICE:SECONDARY]]"
            requested_secondary = raw_reply.startswith(marker)

            if marker in raw_reply and not requested_secondary:
                LOGGER.error(
                    "MEDIA response rejected reason=misplaced_voice_marker"
                )
                return

            reply = (
                raw_reply[len(marker):].lstrip()
                if requested_secondary
                else raw_reply
            )
            if not reply:
                LOGGER.error(
                    "MEDIA response rejected reason=empty_after_voice_marker"
                )
                return

            if requested_secondary:
                self.secondary_voice_active = True

            voice_id = self.association.voice_id
            if self.secondary_voice_active:
                voice_id = self.association.secondary_voice_id
                if not voice_id:
                    LOGGER.error(
                        "MEDIA response rejected reason=secondary_voice_missing"
                    )
                    return

            self.stats["claude_reply_count"] += 1
            claude_ready_at = self._clock()
            first_audio_logged = False

            async for audio in self.tts.stream(reply, voice_id):
                if turn != self.generation:
                    return
                if audio and not first_audio_logged:
                    first_audio_logged = True
                    first_audio_at = self._clock()
                    if turn_ready_at is not None:
                        LOGGER.info(
                            "MEDIA latency claude_ms=%s tts_first_byte_ms=%s",
                            round((claude_ready_at - turn_ready_at) * 1000),
                            round((first_audio_at - turn_ready_at) * 1000),
                        )
                audio_buffer.extend(audio)
                generated_bytes += len(audio)
                self.stats["generated_bytes"] += len(audio)
                self.stats["elevenlabs_output_bytes"] += len(audio)
                self.diagnostics.write("elevenlabs_in", audio)
                while len(audio_buffer) >= PCMU_FRAME_BYTES:
                    frame = bytes(audio_buffer[:PCMU_FRAME_BYTES])
                    del audio_buffer[:PCMU_FRAME_BYTES]
                    if self.pending_reply is None:
                        self.pending_reply = {
                            "mark": mark_name, "reply": reply, "turn": turn, "cleared": False,
                        }
                    if next_frame_at is None:
                        next_frame_at = self._clock()
                    await self._send_audio_frame(frame, send_at=next_frame_at)
                    next_frame_at += PCMU_FRAME_INTERVAL_SECONDS
                    sent_bytes += len(frame)
            if turn != self.generation or generated_bytes < PCMU_FRAME_BYTES:
                if generated_bytes:
                    LOGGER.error("TTS output too short for a valid Telnyx RTP media frame")
                return
            if self.pending_reply is None:
                self.pending_reply = {
                    "mark": mark_name, "reply": reply, "turn": turn, "cleared": False,
                }
            if audio_buffer:
                if next_frame_at is None:
                    next_frame_at = self._clock()
                # Flush only the final remainder; never fabricate or pad bytes.
                await self._send_audio_frame(bytes(audio_buffer), send_at=next_frame_at)
                sent_bytes += len(audio_buffer)
            await self._send({"event": "mark", "mark": {"name": mark_name}})
            _log_dialogue("PATIENT", reply)
            LOGGER.info(
                "MEDIA playback queued generated_bytes=%s sent_bytes=%s mark=%s",
                generated_bytes, sent_bytes, _safe_identifier(mark_name),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error("MEDIA response failed error_type=%s", type(exc).__name__)
        finally:
            status = getattr(self.tts, "last_response_status", None)
            if isinstance(status, int) and not isinstance(status, bool):
                self.stats["elevenlabs_response_status"] = status

    def _handle_mark(self, message: dict) -> None:
        if message.get("stream_id") not in (None, self.stream_id):
            return
        name = message.get("mark", {}).get("name")
        pending = self.pending_reply
        if pending is None or name != pending["mark"]:
            return
        if not pending["cleared"] and pending["turn"] == self.generation:
            self.messages.append({"role": "assistant", "content": pending["reply"]})
            self.stats["completed_playbacks"] += 1
        self.pending_reply = None

    def _response_in_progress(self) -> bool:
        return (
            self.pending_reply is not None
            or (self.active_task is not None and not self.active_task.done())
        )

    async def _cancel_response(self, reason: str, *, clear: bool) -> None:
        had_response = self._response_in_progress()
        self.generation += 1
        pending = self.pending_reply
        if pending is not None:
            pending["cleared"] = True
            self.pending_reply = None
        active = self.active_task
        self.active_task = None
        if active is not None and not active.done():
            active.cancel(reason)
            await asyncio.gather(active, return_exceptions=True)
        if clear and had_response:
            await self._send({"event": "clear"})
            self.stats["clears"] += 1
        if had_response:
            LOGGER.info("MEDIA response cancelled reason=%s clear=%s", reason, clear)

    async def _send(self, message: dict) -> None:
        async with self._send_lock:
            await self.websocket.send_json(message)

    async def _send_audio_frame(self, frame: bytes, *, send_at: float | None = None) -> None:
        if send_at is not None:
            delay = send_at - self._clock()
            if delay > 0:
                await self._sleep(delay)
        await self._send({
            "event": "media",
            "media": {"payload": base64.b64encode(frame).decode("ascii")},
        })
        self.stats["outbound_rtp_frames"] += 1
        self.stats["outbound_rtp_bytes"] += len(frame)
        self.stats["sent_bytes"] += len(frame)
        self.diagnostics.write("telnyx_out", frame)


def _safe_identifier(value: str | None) -> str:
    if not value:
        return "missing"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _decode_mulaw(value: int) -> int:
    value = (~value) & 0xFF
    magnitude = (((value & 0x0F) << 3) + 0x84) << ((value >> 4) & 0x07)
    sample = magnitude - 0x84
    return -sample if value & 0x80 else sample


def _pcmu_to_pcm16_16khz(audio: bytes, previous: int | None = None):
    """Decode 8 kHz G.711 μ-law and linearly upsample to 16 kHz little-endian PCM."""
    output = bytearray()
    for encoded in audio:
        current = _decode_mulaw(encoded)
        interpolated = current if previous is None else (previous + current) // 2
        output.extend(struct.pack("<hh", interpolated, current))
        previous = current
    return bytes(output), previous


def _pcmu_has_speech(audio: bytes) -> bool:
    """Conservative local activity check used only to clear playback on barge-in."""
    if not audio:
        return False
    loud = sum(abs(_decode_mulaw(value)) >= 700 for value in audio)
    return loud >= max(8, len(audio) // 8)


def _streaming_wav_header(*, sample_rate: int) -> bytes:
    """44-byte PCM WAV header with open-ended sizes, matching Telnyx's Pipecat plugin."""
    channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 0x7FFFFFFF, b"WAVE", b"fmt ", 16, 1, channels,
        sample_rate, byte_rate, block_align, bits_per_sample, b"data", 0x7FFFFFFF,
    )


# Injected by app.py to retain its existing prompt filtering and opt-in logging policy.
_preamble_reason: Callable[[str], str | None] = lambda text: None
_log_dialogue: Callable[[str, str, str | None], None] = lambda speaker, text, reason=None: None
