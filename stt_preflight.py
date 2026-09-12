"""Explicit Telnyx STT-only preflight; this script never places a phone call."""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from media_transport import (
    STTRuntimeError,
    STTStartError,
    TelnyxStreamingSTT,
    format_stt_transport_error,
)


PREFLIGHT_VALIDATION_SECONDS = 2.0


async def _ignore_result(_text: str, _is_final: bool) -> None:
    pass


async def preflight(*, adapter_factory=TelnyxStreamingSTT,
                    validation_interval=PREFLIGHT_VALIDATION_SECONDS) -> int:
    load_dotenv(Path(__file__).resolve().with_name(".env"))
    api_key = os.getenv("TELNYX_API_KEY")
    if not api_key:
        print("FAILURE: STT_START_FAILED stage=configuration error_type=MissingAPIKey status=none")
        return 2

    stt = adapter_factory(api_key)
    failure = None
    try:
        await stt.start(_ignore_result)
        await stt.wait_healthy(validation_interval)
    except STTStartError as exc:
        print(f"FAILURE: STT_START_FAILED {format_stt_transport_error(exc)}")
        return 1
    except STTRuntimeError as exc:
        failure = exc
    finally:
        try:
            await stt.close()
        except STTRuntimeError as exc:
            if failure is None:
                failure = exc

    if failure is not None:
        print(f"FAILURE: STT_RUNTIME_FAILED {format_stt_transport_error(failure)}")
        return 1

    print(
        "SUCCESS: Telnyx STT WebSocket connected, raw linear16 configured, "
        "and receiver remained healthy"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(preflight()))
