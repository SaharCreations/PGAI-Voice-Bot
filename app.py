# PGAI Voice Bot
import asyncio
import importlib.util
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from telnyx import APIError, Telnyx, TelnyxError

from evidence import EvidenceStore, RECORDING_OPTIONS
import media_transport
from media_transport import (
    AudioDiagnostics,
    ElevenLabsTTS,
    MediaStreamSession,
    MediaTokenStore,
    MediaValidationError,
    STTRuntimeError,
    STTStartError,
    TelnyxStreamingSTT,
    format_media_validation_error,
    format_stt_transport_error,
)
from scenarios import load_relay_scenario, load_scenario, relay_parameters
from transcribe_evidence import safe_error_message, transcribe_scenario

load_dotenv(Path(__file__).resolve().with_name(".env"))

TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")
TELNYX_PHONE_NUMBER = os.getenv("TELNYX_PHONE_NUMBER")
TELNYX_CONNECTION_ID = os.getenv("TELNYX_CONNECTION_ID")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
TELNYX_PUBLIC_KEY = os.getenv("TELNYX_PUBLIC_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
LOG_CONVERSATION = os.getenv("LOG_CONVERSATION", "false").strip().lower() == "true"
AUDIO_DIAGNOSTICS = os.getenv("AUDIO_DIAGNOSTICS", "false").strip().lower() == "true"
DEBUG_AUDIO_ROOT = Path(__file__).resolve().with_name("debug_audio")

telnyx_client = Telnyx(api_key=TELNYX_API_KEY) if TELNYX_API_KEY else None
evidence_store = EvidenceStore()
media_token_store = MediaTokenStore()
transcription_tasks = set()

@asynccontextmanager
async def lifespan(app: FastAPI):
    api_key = os.getenv("ANTHROPIC_API_KEY")
    client = AsyncAnthropic(api_key=api_key, max_retries=0, timeout=30.0) if api_key else None
    app.state.anthropic_client = client
    try:
        yield
    finally:
        if client is not None:
            await client.close()


app = FastAPI(lifespan=lifespan)


_ACTIONABLE_PROMPT = re.compile(
    r"\b(?:how (?:can|may) i help|what can i|can i help|may i help|"
    r"what(?:'s| is)|when|where|which|who|do you|are you|would you|"
    r"could you|can you|may i|please (?:tell|provide|confirm)|"
    r"date of birth|first name|last name|appointment|schedule|available)\b"
)


def _preamble_reason(transcript: str) -> str | None:
    """Classify relay-only audio that must not start a patient response."""
    normalized = " ".join(re.sub(r"[^a-z0-9']+", " ", transcript.lower()).split())
    if not normalized:
        return "empty"
    if (
        "para espanol" in normalized
        or (
            re.search(r"\b(?:press|say|select|choose|oprima)\b", normalized)
            and re.search(r"\b(?:english|spanish|language)\b", normalized)
        )
    ):
        return "language_menu"
    recording_disclosure = (
        re.search(r"\b(?:call|conversation)\b", normalized)
        and re.search(r"\b(?:recorded|recording|monitored)\b", normalized)
    )
    if recording_disclosure and not _ACTIONABLE_PROMPT.search(normalized):
        return "recording_disclosure"
    if normalized in {
        "hello", "hi", "hey", "good morning", "good afternoon", "good evening",
        "hello how are you", "hi how are you", "how are you",
    }:
        return "greeting"
    if not _ACTIONABLE_PROMPT.search(normalized) and (
        re.fullmatch(r"(?:hello |hi )?thank you for calling(?: [a-z0-9']+){0,6}", normalized)
        or re.fullmatch(r"(?:hello |hi )?welcome to(?: [a-z0-9']+){0,6}", normalized)
        or re.fullmatch(r"(?:hello |hi )?this is(?: [a-z0-9']+){1,6}", normalized)
    ):
        return "greeting"
    return None


def _safe_dialogue_text(text: str) -> str:
    """Make dialogue one line and remove credentials or URLs before local logging."""
    safe = " ".join(text.split())
    for name, value in os.environ.items():
        if value and len(value) >= 4 and any(
            marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")
        ):
            safe = safe.replace(value, "[REDACTED]")
    safe = re.sub(r"(?i)\b(?:authorization|api[_ -]?key|token|secret|password)\s*[:=]\s*\S+",
                  "[REDACTED]", safe)
    safe = re.sub(r"https?://\S+", "[URL REDACTED]", safe)
    return safe[:4000]


def _log_dialogue(speaker: str, text: str, ignored_reason: str | None = None) -> None:
    if not LOG_CONVERSATION:
        return
    ignored = f" [ignored: {ignored_reason}]" if ignored_reason else ""
    print(f"CONVERSATION {speaker}:{ignored} {_safe_dialogue_text(text)}")


media_transport._preamble_reason = _preamble_reason
media_transport._log_dialogue = _log_dialogue


def _scenario_voice_id(scenario_id: str) -> str | None:
    """Resolve voice selection per scenario without putting operational data in persona JSON."""
    per_scenario = f"ELEVENLABS_VOICE_ID_{scenario_id.upper()}"
    return os.getenv(per_scenario) or os.getenv("ELEVENLABS_VOICE_ID")


def _scenario_secondary_voice_id(scenario_id: str) -> str | None:
    """Resolve an optional second speaker voice."""
    return os.getenv(
        f"ELEVENLABS_VOICE_ID_{scenario_id.upper()}_SECONDARY"
    )


def _create_stt_adapter(diagnostics):
    return TelnyxStreamingSTT(
        TELNYX_API_KEY,
        on_audio_sent=lambda audio: diagnostics.write("stt_out", audio),
    )


def _create_tts_adapter():
    return ElevenLabsTTS(ELEVENLABS_API_KEY, ELEVENLABS_MODEL_ID)


def _outbound_channel_mapping(scenario):
    return {
        "verified": True,
        "basis": (
            "Telnyx outbound Call Control dual-channel recording: channel 0 is the remote "
            "destination/PGAI track and channel 1 is the bidirectional media patient track."
        ),
        "speakers": {
            "0": "PGAI Clinic Agent",
            "1": f"Patient / {scenario.name}",
        },
    }


async def _transcribe_downloaded_recording(event):
    try:
        payload = event["data"]["payload"]
        directory = await asyncio.to_thread(evidence_store.resolve, payload)
        if directory is None:
            print("EVIDENCE transcription not scheduled: reason=unmatched_recording")
            return
        scenario = await asyncio.to_thread(load_scenario, directory.name)
        result = await asyncio.to_thread(
            transcribe_scenario,
            scenario.scenario_id,
            root=evidence_store.root,
            mapping=_outbound_channel_mapping(scenario),
        )
        print(
            "EVIDENCE transcription completed:",
            "stage=local_recording_transcription",
            f"scenario={scenario.scenario_id}",
            f"segments={len(result['segments'])}",
        )
    except Exception as exc:
        print(
            "EVIDENCE transcription failed:",
            f"error_type={type(exc).__name__}",
            f"message={safe_error_message(exc)}",
        )


def _schedule_transcription(event):
    task = asyncio.create_task(_transcribe_downloaded_recording(event))
    transcription_tasks.add(task)
    task.add_done_callback(transcription_tasks.discard)


@app.get("/")
def root():
    return {"status": "PGAI Voice Bot running"}


@app.post("/webhook")
async def telnyx_webhook(request: Request):
    if not telnyx_client or not TELNYX_PUBLIC_KEY:
        raise HTTPException(status_code=503, detail="Webhook verification is not configured")
    try:
        body = (await request.body()).decode("utf-8")
        # SDK verification is local: Ed25519 signature plus timestamp freshness.
        telnyx_client.webhooks.unwrap(body, headers=request.headers, key=TELNYX_PUBLIC_KEY)
        event = json.loads(body)  # Preserve fields omitted by SDK webhook models.
    except TelnyxError:
        raise HTTPException(status_code=503, detail="Webhook verifier dependency unavailable") from None
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=400, detail="Invalid webhook signature or body") from None
    try:
        result = await asyncio.to_thread(evidence_store.handle, event)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid evidence event") from None
    except OSError:
        raise HTTPException(status_code=503, detail="Evidence storage unavailable") from None
    if result == "download_failed":
        # Metadata is already durable; a retry can reattempt the MP3 download.
        raise HTTPException(status_code=503, detail="Recording download pending retry")
    if result == "downloaded":
        _schedule_transcription(event)
    return {"status": "ok", "evidence": result}


@app.get("/config-check")
def config_check():
    return {
        "TELNYX_API_KEY": bool(TELNYX_API_KEY),
        "TELNYX_PHONE_NUMBER": bool(TELNYX_PHONE_NUMBER),
        "TELNYX_CONNECTION_ID": bool(TELNYX_CONNECTION_ID),
        "PUBLIC_BASE_URL": bool(PUBLIC_BASE_URL),
        "ELEVENLABS_API_KEY": bool(ELEVENLABS_API_KEY),
        "ELEVENLABS_VOICE_ID_SCENARIO_01": bool(_scenario_voice_id("scenario_01")),
        "ELEVENLABS_VOICE_ID_SCENARIO_02": bool(_scenario_voice_id("scenario_02")),
        "ELEVENLABS_VOICE_ID_SCENARIO_02_SECONDARY": bool(
            _scenario_secondary_voice_id("scenario_02")
        ),
        "ELEVENLABS_VOICE_ID_SCENARIO_03": bool(
            _scenario_voice_id("scenario_03")
        ),
        "ELEVENLABS_VOICE_ID_SCENARIO_04": bool(
            _scenario_voice_id("scenario_04")
        ),
        "ELEVENLABS_VOICE_ID_SCENARIO_05": bool(
            _scenario_voice_id("scenario_05")
        ),
        "ELEVENLABS_VOICE_ID_SCENARIO_06": bool(
            _scenario_voice_id("scenario_06")
        ),
        "ELEVENLABS_VOICE_ID_SCENARIO_07": bool(
            _scenario_voice_id("scenario_07")
        ),
        "ELEVENLABS_VOICE_ID_SCENARIO_08": bool(
            _scenario_voice_id("scenario_08")
        ),
        "ELEVENLABS_VOICE_ID_SCENARIO_09": bool(
            _scenario_voice_id("scenario_09")
        ),

        "ELEVENLABS_VOICE_ID_SCENARIO_10": bool(
            _scenario_voice_id("scenario_10")
        ),

        "ELEVENLABS_VOICE_ID_SCENARIO_11": bool(
            _scenario_voice_id("scenario_11")
        ),

        "ELEVENLABS_VOICE_ID_SCENARIO_12": bool(
            _scenario_voice_id("scenario_12")
        ),
    }


@app.post("/dial-test")
def dial_test(scenario_id: str | None = None):
    return submit_scenario_call(scenario_id)


def submit_scenario_call(scenario_id: str | None = None):
    """Testable submission logic; route itself is never invoked by offline tests."""
    try:
        scenario = load_scenario('scenario_01' if scenario_id is None else scenario_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="A valid approved scenario persona is required") from None
    except OSError:
        raise HTTPException(status_code=503, detail="Scenario configuration unavailable") from None
    with evidence_store.submission_lock():
        if evidence_store.directory(scenario.scenario_id).exists():
            raise HTTPException(status_code=409, detail="Scenario evidence already exists; refusing to overwrite it")
        if not (telnyx_client and TELNYX_PHONE_NUMBER and TELNYX_CONNECTION_ID and PUBLIC_BASE_URL):
            raise HTTPException(status_code=503, detail="Telnyx configuration is incomplete")
        if not TELNYX_PUBLIC_KEY or importlib.util.find_spec("nacl") is None:
            raise HTTPException(status_code=503, detail="Configure webhook verification before dialing")
        # In-memory correlation exists before submission; no final files do.
        evidence = {"scenario_id": scenario.scenario_id, "attempt_id": uuid4().hex}
        client_state = evidence_store.client_state(evidence)
        voice_id = _scenario_voice_id(scenario.scenario_id)
        secondary_voice_id = _scenario_secondary_voice_id(
            scenario.scenario_id
        )
        if not ELEVENLABS_API_KEY or not voice_id:
            raise HTTPException(
                status_code=503,
                detail="ElevenLabs configuration is incomplete",
            )
        if scenario.scenario_id == "scenario_02" and not secondary_voice_id:
            raise HTTPException(
                status_code=503,
                detail="Scenario 2 secondary voice is incomplete",
            )
        placeholder_id, stream_token = media_token_store.stage(
            scenario.scenario_id,
            evidence["attempt_id"],
            client_state,
            voice_id,
            secondary_voice_id=secondary_voice_id,
        )
        websocket_url = (
            PUBLIC_BASE_URL.replace("https://", "wss://", 1).rstrip("/")
            + "/media-stream?"
            + urlencode({"call": placeholder_id, "token": stream_token})
        )
        try:
            # Disable retries: ambiguous errors must never trigger an automatic redial.
            response = telnyx_client.with_options(max_retries=0).calls.dial(
                from_=TELNYX_PHONE_NUMBER,
                to="+18054398008",
                connection_id=TELNYX_CONNECTION_ID,
                **RECORDING_OPTIONS,
                client_state=client_state,
                webhook_url=PUBLIC_BASE_URL.rstrip("/") + "/webhook",
                webhook_url_method="POST",
                stream_url=websocket_url,
                stream_auth_token=stream_token,
                stream_track="inbound_track",
                stream_codec="PCMU",
                stream_bidirectional_mode="rtp",
                stream_bidirectional_codec="PCMU",
                stream_bidirectional_sampling_rate=8000,
                stream_bidirectional_target_legs="self",
            )
            data = response.data
            if data is None:
                raise ValueError("Missing accepted call data")
            payload = data.model_dump(mode="json")
            for key in ("call_control_id", "call_leg_id", "call_session_id"):
                value = payload.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("Missing accepted call identifier")
        except Exception as exc:
            media_token_store.discard(placeholder_id)
            # No raw exception strings/bodies/headers: they can contain credentials.
            logging.getLogger(__name__).error(
                "Telnyx dial not confirmed scenario=%s attempt=%s error_type=%s status=%s",
                scenario.scenario_id, evidence["attempt_id"], type(exc).__name__,
                getattr(exc, "status_code", None),
            )
            raise HTTPException(status_code=502, detail="Telnyx dial request failed or acceptance was not confirmed") from None
        try:
            payload["connection_id"] = TELNYX_CONNECTION_ID
            evidence_store.publish_accepted(scenario.scenario_id, evidence["attempt_id"], scenario.provenance, payload)
        except Exception as exc:
            logging.getLogger(__name__).error(
                "Telnyx accepted but evidence publication failed scenario=%s attempt=%s error_type=%s; do not redial blindly",
                scenario.scenario_id, evidence["attempt_id"], type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail="Call accepted but evidence storage failed; reconcile before retrying") from None
        try:
            media_token_store.bind_accepted(placeholder_id, payload)
        except MediaValidationError as exc:
            logging.getLogger(__name__).error(
                "MEDIA_ASSOCIATION_REJECT %s; do_not_redial_blindly=true",
                format_media_validation_error(exc),
            )
            raise HTTPException(status_code=503, detail="Call accepted but media association failed; reconcile before retrying") from None
        except Exception:
            logging.getLogger(__name__).error(
                "MEDIA_ASSOCIATION_REJECT reason_code=unexpected_association_error "
                "reason_message='unexpected media association failure' "
                "do_not_redial_blindly=true"
            )
            raise HTTPException(status_code=503, detail="Call accepted but media association failed; reconcile before retrying") from None
        return {
            "status": "submitted",
            "scenario_id": scenario.scenario_id,
            "call_leg_id": payload["call_leg_id"],
            "call_session_id": payload["call_session_id"],
        }


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    placeholder_id = websocket.query_params.get("call")
    token = websocket.query_params.get("token")
    association = media_token_store.consume(placeholder_id, token)
    if association is None:
        await websocket.close(code=1008, reason="Invalid or expired media credential")
        return
    try:
        scenario = await asyncio.to_thread(load_scenario, association.scenario_id)
        if association.attempt_id == "" or scenario.scenario_id != association.scenario_id:
            raise ValueError("Invalid media scenario association")
    except (ValueError, OSError):
        await websocket.close(code=1008, reason="Invalid media scenario association")
        return
    client = getattr(websocket.app.state, "anthropic_client", None)
    if client is None or not TELNYX_API_KEY or not ELEVENLABS_API_KEY:
        await websocket.close(code=1011, reason="Live audio providers are not configured")
        return
    await websocket.accept()
    diagnostics = AudioDiagnostics(
        AUDIO_DIAGNOSTICS, DEBUG_AUDIO_ROOT, association.attempt_id,
    )
    session = MediaStreamSession(
        websocket,
        association,
        scenario,
        client,
        _create_stt_adapter(diagnostics),
        _create_tts_adapter(),
        media_token_store,
        diagnostics,
    )
    try:
        await session.run()
    except WebSocketDisconnect:
        pass
    except MediaValidationError as exc:
        logging.getLogger(__name__).warning(
            "MEDIA_START_REJECT %s", format_media_validation_error(exc),
        )
        await websocket.close(code=1008, reason="Invalid media stream association")
    except STTStartError as exc:
        logging.getLogger(__name__).error(
            "STT_START_FAILED %s", format_stt_transport_error(exc),
        )
        await websocket.close(code=1011, reason="Speech recognition unavailable")
    except STTRuntimeError as exc:
        logging.getLogger(__name__).error(
            "STT_RUNTIME_FAILED %s", format_stt_transport_error(exc),
        )
        await websocket.close(code=1011, reason="Speech recognition unavailable")


# Retained as an unregistered compatibility helper for the existing offline tests.
# The active dial path and FastAPI router expose only /media-stream.
async def conversation_relay(websocket: WebSocket):
    await websocket.accept()
    client = getattr(websocket.app.state, "anthropic_client", None)
    if client is None:
        await websocket.close(code=1011, reason="Claude configuration is incomplete")
        return

    # Each WebSocket owns its history, tasks, and pending playback independently.
    messages = []
    tasks = set()
    active_task = None
    generation = 0
    pending_reply = None
    scenario = None

    def cancel_generation(reason):
        nonlocal generation, pending_reply
        generation += 1
        discarded_pending_reply = pending_reply is not None
        pending_reply = None
        generation_active = active_task is not None and not active_task.done()
        if generation_active:
            active_task.cancel(reason)
        if generation_active or discarded_pending_reply:
            print(
                "CONVERSATION RELAY cancellation:",
                f"reason={reason}",
                f"generation_active={generation_active}",
                f"pending_reply_discarded={discarded_pending_reply}",
            )

    async def generate_reply(turn, history):
        nonlocal pending_reply
        parts = []
        outbound_characters = 0
        outbound_frames = 0
        final_frame_delivered = False
        buffered = None
        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=scenario.system_prompt,
                messages=history,
            ) as stream:
                async for text in stream.text_stream:
                    if turn != generation:
                        return
                    if not text:
                        continue
                    if buffered is not None:
                        await websocket.send_json({"type": "text", "token": buffered, "last": False})
                        outbound_characters += len(buffered)
                        outbound_frames += 1
                        if turn != generation:
                            return
                        parts.append(buffered)
                    buffered = text
                final = await stream.get_final_message()
                if turn != generation:
                    return
                if buffered is not None:
                    await websocket.send_json({"type": "text", "token": buffered, "last": True})
                    outbound_characters += len(buffered)
                    outbound_frames += 1
                    parts.append(buffered)
                    final_frame_delivered = True
                print(
                    "CONVERSATION RELAY outbound:",
                    f"characters={outbound_characters}",
                    f"frames={outbound_frames}",
                    f"final_frame_delivered={final_frame_delivered}",
                )
                if turn == generation and parts and final.stop_reason == "end_turn":
                    # Generation completion is not playback completion. An interrupt
                    # can still discard this reply before the caller's next turn.
                    reply = "".join(parts)
                    pending_reply = {"role": "assistant", "content": reply}
                    _log_dialogue("PATIENT", reply)
        except asyncio.CancelledError as exc:
            reason = exc.args[0] if exc.args else "cancelled"
            print(
                "CONVERSATION RELAY outbound cancelled:",
                f"reason={reason}",
                f"characters={outbound_characters}",
                f"frames={outbound_frames}",
                f"final_frame_delivered={final_frame_delivered}",
            )
            raise
        except Exception as exc:
            # Do not log provider payloads, caller text, or credentials.
            print("CONVERSATION RELAY generation failed:", type(exc).__name__, str(exc))

    safe_event_types = {"setup", "prompt", "interrupt", "dtmf", "error"}
    try:
        while True:
            try:
                message = await websocket.receive_json()
            except (ValueError, KeyError):
                print("CONVERSATION RELAY event: invalid_json")
                continue

            if not isinstance(message, dict):
                print("CONVERSATION RELAY event: invalid_message")
                continue

            event_type = message.get("type")
            safe_type = (
                event_type
                if isinstance(event_type, str) and event_type in safe_event_types
                else "unknown"
            )
            print("CONVERSATION RELAY event:", safe_type)

            if safe_type == "setup":
                if scenario is not None:
                    await websocket.close(code=1008, reason="Relay scenario is already bound")
                    return
                try:
                    scenario = await asyncio.to_thread(load_relay_scenario, evidence_store, message)
                except (ValueError, OSError):
                    await websocket.close(code=1008, reason="Invalid relay scenario association")
                    return
                continue

            if safe_type == "interrupt":
                print(
                    "CONVERSATION RELAY unexpected interrupt:",
                    "action=ignored",
                    "interruptible=none",
                )
                continue

            if safe_type == "error":
                code = message.get("code")
                description = message.get("description")
                safe_code = code if isinstance(code, (str, int)) else "unspecified"
                description_characters = len(description) if isinstance(description, str) else 0
                print(
                    "CONVERSATION RELAY Telnyx error:",
                    f"code={safe_code}",
                    f"description_characters={description_characters}",
                )
                continue

            if safe_type == "prompt":
                if scenario is None:
                    await websocket.close(code=1008, reason="Scenario setup is required")
                    return
                transcript = message.get("voicePrompt")
                if not isinstance(transcript, str) or not transcript.strip():
                    continue
                if message.get("last") is not True:
                    print(
                        "CONVERSATION RELAY prompt ignored:",
                        "reason=partial",
                        f"characters={len(transcript)}",
                    )
                    continue
                preamble_reason = _preamble_reason(transcript)
                if preamble_reason is not None:
                    print(
                        "CONVERSATION RELAY prompt ignored:",
                        f"reason={preamble_reason}",
                        f"characters={len(transcript)}",
                    )
                    _log_dialogue("PGAI", transcript, preamble_reason)
                    continue
                _log_dialogue("PGAI", transcript)
                if pending_reply is not None:
                    messages.append(pending_reply)
                cancel_generation("new_final_prompt")
                messages.append({"role": "user", "content": transcript})
                active_task = asyncio.create_task(generate_reply(generation, [dict(item) for item in messages]))
                tasks.add(active_task)
                active_task.add_done_callback(tasks.discard)
                print(
                    "CONVERSATION RELAY prompt accepted:",
                    f"characters={len(transcript)}",
                )
    except WebSocketDisconnect:
        pass
    finally:
        cancel_generation("websocket_closed")
        remaining = list(tasks)
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
