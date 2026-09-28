from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_data  # noqa: E402


class BuildDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.event = {
            "id": "test-2027",
            "acronym": "TEST",
            "name": "Test Systems Conference",
            "organization": "Example Society",
            "organization_group": "Other",
            "event_type": "Academic conference",
            "topics": ["Systems", "Cloud"],
            "categories": ["Systems & cloud"],
            "location": "Online",
            "mode": "Online",
            "edition": "2027",
            "indexing": "DBLP",
            "event_start": "2027-03-01",
            "event_end": "2027-03-03",
            "deadlines": {
                "abstract": "2026-09-20",
                "paper": "2026-09-27",
                "notification": "",
                "camera_ready": "",
                "gates": [
                    {"date": "2026-09-20", "kind": "Abstract"},
                    {"date": "2026-09-27", "kind": "Paper"},
                ],
                "timezone": "AoE",
                "precision": "date",
            },
            "confidence": "verified",
            "source_url": "https://example.org/cfp",
            "source_basis": "Primary venue page",
        }

    def test_validation_rejects_duplicate_records(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_data.validate([self.event, copy.deepcopy(self.event)])

    def test_validation_rejects_invalid_dates(self) -> None:
        event = copy.deepcopy(self.event)
        event["deadlines"]["paper"] = "2027-02-31"
        with self.assertRaisesRegex(ValueError, "real YYYY-MM-DD date"):
            build_data.validate([event])

    def test_validation_rejects_private_paths_independently(self) -> None:
        for path in (
            r"See ..\private\notes.md",
            r"See \\server\share\notes.md",
            "See (/var/tmp/notes.md)",
            'See "/var/tmp/notes.md"',
            "See `research/notes`",
            "See papers/draft",
            "See private/notes.md",
            "See file://local/notes.md",
        ):
            with self.subTest(path=path):
                event = copy.deepcopy(self.event)
                event["name"] = path
                with self.assertRaisesRegex(ValueError, "local path"):
                    build_data.validate([event])

    def test_validation_rejects_unknown_fields_and_stale_gates(self) -> None:
        event = copy.deepcopy(self.event)
        event["unexpected"] = "not allowed"
        with self.assertRaisesRegex(ValueError, "unexpected=.*unexpected"):
            build_data.validate([event])

        event = copy.deepcopy(self.event)
        event["deadlines"]["gates"] = []
        with self.assertRaisesRegex(ValueError, "gates do not match"):
            build_data.validate([event])

    def test_validation_rejects_wrong_public_value_types(self) -> None:
        event = copy.deepcopy(self.event)
        event["topics"] = "Systems"
        with self.assertRaisesRegex(ValueError, "arrays of non-empty strings"):
            build_data.validate([event])

    def test_validation_rejects_malformed_urls(self) -> None:
        event = copy.deepcopy(self.event)
        event["source_url"] = "https://example.org:99999/cfp"
        with self.assertRaisesRegex(ValueError, "^TEST: invalid source URL$"):
            build_data.validate([event])

    def test_validation_rejects_non_public_sources_and_sensitive_queries(self) -> None:
        for url in (
            "https://localhost./cfp",
            "https://sub.localhost/cfp",
            "https://internal/cfp",
            "https://2130706433/cfp",
            "https://127.1/cfp",
            "https://0177.0.0.1/cfp",
        ):
            with self.subTest(url=url):
                event = copy.deepcopy(self.event)
                event["source_url"] = url
                with self.assertRaisesRegex(ValueError, "public|numeric"):
                    build_data.validate([event])
        event = copy.deepcopy(self.event)
        event["source_url"] = "https://example.org/cfp?access_token=value"
        with self.assertRaisesRegex(ValueError, "sensitive query key"):
            build_data.validate([event])
        event["source_url"] = "https://example.org/cfp#auth=value"
        with self.assertRaisesRegex(ValueError, "sensitive fragment"):
            build_data.validate([event])

    def test_validation_rejects_invalid_enums_ids_and_delimiters(self) -> None:
        event = copy.deepcopy(self.event)
        event["mode"] = "Teleport"
        event["id"] = ""
        event["categories"] = ["Unknown"]
        event["topics"] = ["Systems|Cloud"]
        with self.assertRaisesRegex(ValueError, "invalid mode"):
            build_data.validate([event])

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            build_data.reject_duplicate_keys([("id", "one"), ("id", "two")])

    def test_discovery_provenance_is_strict_and_public(self) -> None:
        event = copy.deepcopy(self.event)
        event["discovery"] = {
            "source_id": "fixture-source",
            "source_name": "Fixture source",
            "source_url": "https://example.org/docs",
            "external_id": "fixture-1",
            "discovered_at": "2026-09-19T12:00:00Z",
            "last_seen_at": "2026-09-19T12:00:00Z",
        }
        build_data.validate([event])
        for field, value in (
            ("source_url", "https://localhost/docs"),
            ("external_id", "<script>unsafe</script>"),
            ("source_id", "Not Valid"),
        ):
            with self.subTest(field=field):
                invalid = copy.deepcopy(event)
                invalid["discovery"][field] = value
                with self.assertRaisesRegex(ValueError, "discovery"):
                    build_data.validate([invalid])

    def test_discovery_state_schema_and_counts_are_strict(self) -> None:
        state = {
            "schema_version": 1,
            "last_scan_at": "2026-09-19T12:00:00Z",
            "summary": {
                "sources_attempted": 1,
                "sources_succeeded": 1,
                "candidates_seen": 2,
                "added": 0,
                "updated": 0,
                "duplicates": 2,
                "quarantined": 0,
            },
            "sources": [{
                "id": "fixture-source",
                "name": "Fixture source",
                "status": "ok",
                "last_attempt_at": "2026-09-19T12:00:00Z",
                "last_success_at": "2026-09-19T12:00:00Z",
                "candidates": 2,
                "requests": 1,
                "error": "",
            }],
            "quarantine": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            with patch.object(build_data, "DISCOVERY_STATE", path):
                self.assertEqual(
                    build_data.load_discovery_state()["summary"]["candidates_seen"],
                    2,
                )
                invalid = copy.deepcopy(state)
                invalid["summary"]["candidates_seen"] = 3
                path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "does not match"):
                    build_data.load_discovery_state()

    def test_generated_outputs_are_deterministic_and_valid(self) -> None:
        first = build_data.outputs()
        second = build_data.outputs()
        self.assertEqual(first, second)
        payload = json.loads(first["events.json"])
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["event_count"], len(payload["events"]))
        self.assertGreater(payload["event_count"], 100)
        self.assertTrue(first["events.csv"].startswith("id,acronym,name,"))
        self.assertIn("discovery_source,discovered_at,last_seen_at", first["events.csv"])


if __name__ == "__main__":
    unittest.main()
