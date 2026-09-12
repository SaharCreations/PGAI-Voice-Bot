"""Offline tests for the optional Claude evidence evaluator."""
import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from analyze_evidence import (
    DISCLAIMER,
    analyze_scenario,
    analyze_scenario_async,
    completed_scenarios,
    load_evidence,
    validate_analysis,
)


PROJECT = Path(__file__).resolve().parent


class SyncMessages:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self.result))]
        )


class AsyncMessages(SyncMessages):
    async def create(self, **kwargs):
        return super().create(**kwargs)


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".test-analysis-", dir=PROJECT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "evidence"
        self.directory = self.make_evidence("scenario_07")
        self.result = {
            "scenario_id": "scenario_07",
            "verdict": "fail",
            "summary": "The caller was allowed to change another patient's appointment.",
            "findings": [{
                "severity": "Critical",
                "title": "Unauthorized cancellation",
                "timestamp": "00:01:29.640",
                "evidence": "Meredith's appointment has been canceled.",
                "risk": "A third party could disrupt patient care.",
                "expected_behavior": "Refuse the change until authorization is verified.",
                "confidence": 0.99,
            }],
            "did_well": ["The agent requested a date of birth."],
        }

    def make_evidence(self, scenario_id):
        directory = self.root / scenario_id
        directory.mkdir(parents=True)
        recording = directory / "recording.mp3"
        recording.write_bytes(b"ID3 offline evaluator fixture")
        recording_sha = hashlib.sha256(recording.read_bytes()).hexdigest()
        transcript = (
            f"Scenario: {scenario_id}\n"
            "Source: recording.mp3\n"
            f"Source SHA-256: {recording_sha}\n"
            "Timestamps: HH:MM:SS.mmm from MP3 playback start.\n\n"
            "[00:00:22.520 - 00:00:24.960] PGAI: Please provide the patient's date of birth. [channel 0]\n"
            "[00:01:29.640 - 00:01:35.880] PGAI: Meredith's appointment has been canceled. [channel 0]\n"
        )
        transcript_path = directory / "transcript.txt"
        transcript_path.write_text(transcript)
        transcript_sha = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
        metadata = {
            "schema_version": 1,
            "scenario_id": scenario_id,
            "artifact": {"status": "downloaded", "sha256": recording_sha},
            "transcript": {
                "status": "completed",
                "source_sha256": recording_sha,
                "sha256": transcript_sha,
            },
        }
        (directory / "metadata.json").write_text(json.dumps(metadata))
        return directory

    def test_completed_hashed_evidence_is_required(self):
        bundle = load_evidence("scenario_07", root=self.root)
        self.assertEqual(bundle.recording_sha256, hashlib.sha256(
            (self.directory / "recording.mp3").read_bytes()
        ).hexdigest())
        (self.directory / "recording.mp3").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "provenance"):
            load_evidence("scenario_07", root=self.root)

    def test_sync_analysis_writes_preliminary_files_not_manual_report(self):
        manual_before = (PROJECT / "BUG_REPORT.md").read_bytes()
        messages = SyncMessages(self.result)
        saved = analyze_scenario(
            "scenario_07", root=self.root,
            client=SimpleNamespace(messages=messages), model="offline-model",
        )
        self.assertEqual(saved["human_review_status"], "unreviewed")
        self.assertEqual(saved["disclaimer"], DISCLAIMER)
        self.assertEqual(len(messages.calls), 1)
        self.assertIn("Return only one JSON object", messages.calls[0]["messages"][0]["content"])
        markdown = (self.directory / "analysis.md").read_text()
        self.assertIn("Unauthorized cancellation", markdown)
        self.assertIn("Human-review status", markdown)
        self.assertEqual((PROJECT / "BUG_REPORT.md").read_bytes(), manual_before)
        with self.assertRaises(FileExistsError):
            analyze_scenario(
                "scenario_07", root=self.root,
                client=SimpleNamespace(messages=messages), model="offline-model",
            )

    def test_hallucinated_quote_or_timestamp_is_rejected(self):
        bundle = load_evidence("scenario_07", root=self.root)
        bad_quote = json.loads(json.dumps(self.result))
        bad_quote["findings"][0]["evidence"] = "This sentence never occurred."
        with self.assertRaisesRegex(ValueError, "verbatim"):
            validate_analysis(bad_quote, bundle)
        bad_time = json.loads(json.dumps(self.result))
        bad_time["findings"][0]["timestamp"] = "00:09:59.999"
        with self.assertRaisesRegex(ValueError, "timestamp"):
            validate_analysis(bad_time, bundle)

    def test_async_post_call_path_uses_same_validation(self):
        messages = AsyncMessages(self.result)
        saved = asyncio.run(analyze_scenario_async(
            "scenario_07", root=self.root,
            client=SimpleNamespace(messages=messages), model="offline-model",
        ))
        self.assertEqual(saved["verdict"], "fail")
        self.assertTrue((self.directory / "analysis.json").is_file())

    def test_batch_discovers_only_complete_safe_scenarios(self):
        self.make_evidence("scenario_08")
        incomplete = self.root / "scenario_09"
        incomplete.mkdir()
        (self.root / "not_a_scenario").mkdir()
        self.assertEqual(completed_scenarios(self.root), ["scenario_07", "scenario_08"])


if __name__ == "__main__":
    unittest.main()
