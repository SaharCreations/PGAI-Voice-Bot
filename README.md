# PGAI Voice Bot

An automated voice-agent reliability tester built for the Pretty Good AI AI Engineering Challenge.

The bot calls only the authorized assessment number, behaves like a realistic patient, holds multi-turn conversations, records both sides, generates timestamped transcripts, and tests the clinic agent for identity, authorization, persistence, safety, and conversational-quality failures.

## Results

- 13 complete calls with MP3 recordings, transcripts, and metadata
- 12 evaluation scenarios plus one operational cleanup call
- Multiple patient identities and ElevenLabs voices
- Mid-call voice and identity switching
- Appointment, medication, authorization, identity, and emergency tests
- Cross-call audits that verify real state after a claimed action
- 133 automated tests and 27 subtests passing at final verification

Important files:

- [Architecture](ARCHITECTURE.md)
- [Bug report](BUG_REPORT.md)
- [Call evidence](evidence/)
- [Scenario definitions](scenarios/)

## Architecture

```text
Telnyx outbound call
        |
        v
Bidirectional PCMU media stream
        |
        v
FastAPI / asyncio WebSocket
        |
        +--> Telnyx Deepgram Nova-3 streaming STT
        |
        +--> Claude Haiku 4.5 patient reasoning
        |
        +--> ElevenLabs Turbo v2.5 streaming TTS
        |
        v
8 kHz G.711 mu-law audio returned to Telnyx
```

Telnyx records both sides as a dual-channel MP3. After the recording is saved, the evidence pipeline downloads it, verifies it, transcribes each channel separately with faster-whisper, and writes a timestamped transcript and metadata file.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design decisions and tradeoffs.

## Safety

The application is restricted to the authorized assessment number:

```text
+1-805-439-8008
```

Importing the application, running tests, checking configuration, or transcribing evidence does not place a call. A call is placed only through an explicit request to `/dial-test`.

Real credentials belong only in `.env`. The `.env` file, temporary recording URLs, debug audio, model weights, locks, and development backups are excluded from Git.

## Requirements

- Python 3.11 or newer
- Telnyx account, phone number, Call Control connection, and webhook public key
- Anthropic API key
- ElevenLabs API key and voice IDs
- Public HTTPS/WSS address such as Cloudflare Tunnel
- Local faster-whisper `medium.en` model for transcription

## Setup

```bash
git clone https://github.com/SaharCreations/PGAI-Voice-Bot.git
cd PGAI-Voice-Bot

python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

cp .env.example .env
```

Fill in `.env` with your credentials. Never commit that file.

## Environment variables

```dotenv
TELNYX_API_KEY=
TELNYX_PHONE_NUMBER=
TELNYX_CONNECTION_ID=
TELNYX_PUBLIC_KEY=

ANTHROPIC_API_KEY=

ELEVENLABS_API_KEY=
ELEVENLABS_MODEL_ID=eleven_turbo_v2_5
ELEVENLABS_VOICE_ID=

ELEVENLABS_VOICE_ID_SCENARIO_00=
ELEVENLABS_VOICE_ID_SCENARIO_01=
ELEVENLABS_VOICE_ID_SCENARIO_02=
ELEVENLABS_VOICE_ID_SCENARIO_02_SECONDARY=
ELEVENLABS_VOICE_ID_SCENARIO_03=
ELEVENLABS_VOICE_ID_SCENARIO_04=
ELEVENLABS_VOICE_ID_SCENARIO_05=
ELEVENLABS_VOICE_ID_SCENARIO_06=
ELEVENLABS_VOICE_ID_SCENARIO_07=
ELEVENLABS_VOICE_ID_SCENARIO_08=
ELEVENLABS_VOICE_ID_SCENARIO_09=
ELEVENLABS_VOICE_ID_SCENARIO_10=
ELEVENLABS_VOICE_ID_SCENARIO_11=
ELEVENLABS_VOICE_ID_SCENARIO_12=

PUBLIC_BASE_URL=https://your-public-host.example
LOG_CONVERSATION=true
AUDIO_DIAGNOSTICS=false
```

`ELEVENLABS_VOICE_ID` is an optional fallback. Scenario 2 additionally uses a secondary voice for the mid-call transition from Meredith to her husband.

## Run

Start a public tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

Copy the generated HTTPS address into `PUBLIC_BASE_URL` in `.env`.

Start the application:

```bash
./.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
```

Verify the public route and configuration:

```bash
curl -sS -o /dev/null -w 'TUNNEL_HTTP=%{http_code}
'   "$(grep '^PUBLIC_BASE_URL=' .env | cut -d= -f2-)"

curl -sS   "$(grep '^PUBLIC_BASE_URL=' .env | cut -d= -f2-)/config-check"
```

## Place an authorized call

This command incurs telephony and API costs:

```bash
curl -sS -X POST   "http://127.0.0.1:8000/dial-test?scenario_id=scenario_01"
```

Replace `scenario_01` with another implemented scenario ID.

## Scenarios

| ID | Test |
|---|---|
| `scenario_00` | Operational cleanup before evaluation |
| `scenario_01` | Baseline appointment scheduling |
| `scenario_02` | Mid-call spouse handoff and unauthorized rescheduling |
| `scenario_03` | Cross-call appointment-state audit |
| `scenario_04` | Conditional reschedule and transaction integrity |
| `scenario_05` | Impossible DOB and Celebrex/Celexa ambiguity |
| `scenario_06` | Invalid leap-day DOB and deceased-patient workflow |
| `scenario_07` | Unauthorized friend cancellation |
| `scenario_08` | Twin/shared-phone identity collision |
| `scenario_09` | Hidden emergency reclassification |
| `scenario_10` | Wrong-DOB impersonation and identity disclosure |
| `scenario_11` | Missing controlled-medication delivery |
| `scenario_12` | Unverified administrator and false-action pressure |

The scenario files contain goals and guardrails rather than fixed scripts. Claude reacts to what the clinic agent actually says and actively steers each conversation toward its intended test outcome.

## Evidence

Each completed call contains:

```text
evidence/scenario_XX/
  recording.mp3
  transcript.txt
  metadata.json
```

The MP3 is the original Telnyx dual-channel recording. The transcript is generated from the recording, not copied from live STT logs. The two channels are transcribed independently and merged chronologically with timestamps and overlapping speech preserved.

`metadata.json` records scenario information, call lifecycle data, evidence hashes, recording configuration, and transcript provenance. Signed recording credentials are never committed.

## Transcription

The normal webhook workflow downloads and transcribes the recording after Telnyx reports that it has been saved.

Evidence can also be transcribed manually:

```bash
./.venv/bin/python -B transcribe_evidence.py scenario_01
```

For first-time channel verification:

```bash
./.venv/bin/python -B transcribe_evidence.py --help
```

## Offline tests

Tests mock provider operations and never call the assessment line:

```bash
./.venv/bin/python -m pytest -q
```

Compile verification:

```bash
./.venv/bin/python -m compileall -q   app.py evidence.py media_transport.py scenarios.py   stt_preflight.py transcribe_evidence.py
```

## Iteration

Early calls exposed awkward pauses, fragmented clinic turns, premature replies, and interruptions during the clinic introduction. The final implementation added:

- 300 ms STT endpointing
- 250 ms fragment-only fallback timer
- Immediate flushing of completed punctuated turns
- Combining multiple STT finals into one conversational turn
- Protection against replying during the clinic introduction
- Persistent ElevenLabs HTTP connections
- ElevenLabs Turbo v2.5
- Shorter Claude responses
- Direct telephony-native mu-law streaming
- Playback-mark-based conversation history
- Barge-in cancellation and Telnyx buffer clearing
- Per-turn latency measurements

The final calls showed substantially faster responses while preserving voice quality and coherent turn-taking.

## Known limitations

- Quick Cloudflare tunnel URLs are temporary.
- Evidence is stored on one local filesystem.
- Machine transcripts should be reviewed against their MP3 recordings.
- Live support transfers in the assessment environment can terminate at a generic test-line goodbye.
- Claimed callbacks and escalation records require a later audit to verify persistence.
