from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import discovery  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = dt.date(2026, 9, 19)
SCAN_AT = "2026-09-19T12:00:00Z"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def source(adapter: str) -> dict:
    return next(item for item in discovery.load_registry() if item["adapter"] == adapter)


class AdapterTests(unittest.TestCase):
    def test_developers_events_adapter(self) -> None:
        rows = fixture("developers_events.json")
        candidates = discovery.discover_developers_events(
            source("developers_events"), fetch=lambda _url: rows, today=TODAY
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].event_type, "Summit")
        self.assertEqual(candidates[0].deadline, "2026-12-01")

    def test_hackclub_adapter(self) -> None:
        rows = fixture("hackclub.json")
        candidates = discovery.discover_hackclub(
            source("hackclub"), fetch=lambda _url: rows, today=TODAY
        )
        self.assertEqual(candidates[0].event_type, "Hackathon")
        self.assertEqual(candidates[0].location, "Toronto, Ontario, Canada")

    def test_confs_tech_adapter(self) -> None:
        rows = fixture("confs_tech.json")

        def fake_fetch(url: str):
            if "api.github.com" in url:
                return [
                    {
                        "name": "data.json",
                        "download_url": "https://raw.githubusercontent.com/tech-conferences/conference-data/main/conferences/2027/data.json",
                    }
                ]
            return rows

        candidates = discovery.discover_confs_tech(
            source("confs_tech"), fetch=fake_fetch, today=TODAY
        )
        self.assertGreaterEqual(len(candidates), 1)
        self.assertEqual(candidates[0].event_type, "Open-source meetup")
        self.assertEqual(candidates[0].deadline, "2026-11-01")

    def test_openreview_adapter(self) -> None:
        payload = fixture("openreview.json")

        def fake_fetch(url: str):
            return payload["active"] if "active_venues" in url else payload["group"]

        candidates = discovery.discover_openreview(
            source("openreview"), fetch=fake_fetch, today=TODAY
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].event_type, "Academic conference")
        self.assertEqual(candidates[0].venue_url, "https://example.org/research")

    def test_confs_tech_fanout_stays_within_request_budget(self) -> None:
        rows = fixture("confs_tech.json")
        calls: list[str] = []

        def fake_fetch(url: str):
            calls.append(url)
            if "api.github.com" in url:
                return [
                    {
                        "name": f"topic-{index:03d}.json",
                        "download_url": (
                            "https://raw.githubusercontent.com/"
                            "tech-conferences/conference-data/main/"
                            f"conferences/topic-{index:03d}.json"
                        ),
                    }
                    for index in range(100)
                ]
            return rows

        confs_source = source("confs_tech")
        discovery.discover_confs_tech(
            confs_source,
            fetch=fake_fetch,
            today=TODAY,
        )
        self.assertEqual(len(calls), confs_source["max_requests"])

    def test_openreview_detail_fanout_stays_within_request_budget(self) -> None:
        payload = fixture("openreview.json")
        calls: list[str] = []
        active = copy.deepcopy(payload["active"])
        active["groups"][0]["members"] = [
            f"Example.org/2027/Conference-{index}"
            for index in range(100)
        ]

        def fake_fetch(url: str):
            calls.append(url)
            return active if "active_venues" in url else payload["group"]

        openreview_source = source("openreview")
        discovery.discover_openreview(
            openreview_source,
            fetch=fake_fetch,
            today=TODAY,
        )
        self.assertEqual(len(calls), openreview_source["max_requests"])


class HttpSafetyTests(unittest.TestCase):
    class Socket:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def settimeout(self, value: float) -> None:
            self.timeouts.append(value)

    class Response:
        def __init__(
            self,
            payload: bytes,
            *,
            status: int = 200,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.payload = payload
            self.status = status
            self.headers = headers or {"Content-Length": str(len(payload))}
            self.offset = 0

        def read(self, amount: int) -> bytes:
            chunk = self.payload[self.offset:self.offset + amount]
            self.offset += len(chunk)
            return chunk

    class Connection:
        def __init__(self, response: "HttpSafetyTests.Response") -> None:
            self.response = response
            self.sock = HttpSafetyTests.Socket()
            self.request_headers: dict[str, str] = {}
            self.closed = False

        def connect(self) -> None:
            return None

        def request(
            self,
            _method: str,
            _target: str,
            *,
            headers: dict[str, str],
        ) -> None:
            self.request_headers = headers

        def getresponse(self) -> "HttpSafetyTests.Response":
            return self.response

        def close(self) -> None:
            self.closed = True

    def test_fetch_retries_and_sets_user_agent(self) -> None:
        connection = self.Connection(self.Response(b'{"ok": true}'))
        with patch(
            "discovery._fetch_once",
            side_effect=[
                discovery.TransientFetchError("temporary"),
                b'{"ok": true}',
            ],
        ) as mocked:
            with patch("discovery.time.sleep"):
                payload = discovery.fetch_json(
                    "https://developers.events/all-cfps.json", retries=1
                )
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(mocked.call_count, 2)

        with patch("discovery.http.client.HTTPSConnection", return_value=connection):
            discovery.fetch_json(
                "https://developers.events/all-cfps.json",
                retries=0,
            )
        self.assertEqual(connection.request_headers["User-Agent"], discovery.USER_AGENT)
        self.assertTrue(connection.closed)

    def test_fetch_rejects_oversized_and_unlisted_sources(self) -> None:
        response = self.Response(
            b"{}",
            headers={"Content-Length": str(discovery.MAX_RESPONSE_BYTES + 1)},
        )
        connection = self.Connection(response)
        with patch("discovery.http.client.HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(discovery.DiscoveryError, "size limit"):
                discovery.fetch_json("https://developers.events/all-cfps.json", retries=0)
        with self.assertRaisesRegex(discovery.DiscoveryError, "allowlisted"):
            discovery.validate_fetch_url("https://localhost/events.json")

    def test_redirect_is_validated_before_following(self) -> None:
        response = self.Response(
            b"",
            status=302,
            headers={"Location": "https://127.0.0.1/private"},
        )
        connection = self.Connection(response)
        with patch("discovery.http.client.HTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(discovery.DiscoveryError, "allowlisted"):
                discovery.fetch_json(
                    "https://developers.events/all-cfps.json",
                    retries=0,
                )

    def test_invalid_json_and_duplicate_keys_are_not_retried(self) -> None:
        for payload in (b"{", b'{"id": 1, "id": 2}'):
            with self.subTest(payload=payload):
                with patch("discovery._fetch_once", return_value=payload) as mocked:
                    with self.assertRaisesRegex(discovery.DiscoveryError, "JSON is invalid"):
                        discovery.fetch_json(
                            "https://developers.events/all-cfps.json",
                            retries=2,
                        )
                self.assertEqual(mocked.call_count, 1)

    def test_normalization_removes_unsafe_text_and_url_parts(self) -> None:
        self.assertEqual(
            discovery.clean_text("<b>Safe</b>\u202e\n title"),
            "Safe title",
        )
        self.assertEqual(
            discovery.canonical_url(
                "https://Example.org/path/?utm_source=x&b=2&a=1"
            ),
            "https://example.org/path?a=1&b=2",
        )
        for value in (
            "javascript:alert(1)",
            "https://user:pass@example.org/path",
            "https://localhost/path",
            "https://example.org/path?access_token=secret",
            "https://example.org/path#secret=value",
        ):
            with self.subTest(value=value):
                self.assertEqual(discovery.canonical_url(value), "")

    def test_hard_timeout_interrupts_blocking_source_work(self) -> None:
        started = time.monotonic()
        with self.assertRaises(discovery.SourceTimeout):
            with discovery.hard_timeout(0.03, "fixture"):
                time.sleep(0.2)
        self.assertLess(time.monotonic() - started, 0.15)


def manual_fixture() -> dict:
    payload = json.loads((ROOT / "data" / "events-source.json").read_text(encoding="utf-8"))
    # A fixed hand-curated record. The live file changes daily (the scan adds
    # records, venues move cycle), so it only lends the schema shape here.
    template = next(event for event in payload["events"] if "discovery" not in event)
    manual = copy.deepcopy(template)
    manual.pop("previous_cycle", None)
    manual.update({
        "id": "manual-fixture-2027",
        "acronym": "MFX",
        "name": "Manual Fixture Conference",
        "edition": "2027",
        "source_url": "https://example.org/manual-fixture",
        "event_start": "2027-03-01",
        "event_end": "2027-03-03",
    })
    manual["deadlines"] = {
        **manual["deadlines"],
        "abstract": "", "paper": "2026-12-15", "notification": "", "camera_ready": "",
        "gates": [{"date": "2026-12-15", "kind": "Paper"}],
    }
    return manual


class MergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manual = manual_fixture()

    def candidate(self, **changes) -> discovery.Candidate:
        values = {
            "external_id": "new-1",
            "title": "New Cloud Conference",
            "event_type": "Industry conference",
            "organizer": "See venue source",
            "venue_url": "https://example.org/new-cloud",
            "source_url": source("confs_tech")["documentation_url"],
            "deadline": "2026-12-01",
            "event_start": "2027-02-01",
            "event_end": "2027-02-02",
            "location": "Berlin, Germany",
            "mode": "In person",
            "topics": ("Cloud",),
            "categories": ("Systems & cloud",),
        }
        values.update(changes)
        return discovery.Candidate(**values)

    def test_merge_is_idempotent(self) -> None:
        batch = [(source("confs_tech"), [self.candidate()])]
        merged, first, _ = discovery.merge_candidates(
            [copy.deepcopy(self.manual)], batch, scan_at=SCAN_AT, today=TODAY, max_additions=10
        )
        rerun, second, _ = discovery.merge_candidates(
            merged, batch, scan_at=SCAN_AT, today=TODAY, max_additions=10
        )
        self.assertEqual(first["added"], 1)
        self.assertEqual(second["added"], 0)
        self.assertEqual(merged, rerun)

    def test_dedupes_url_and_identity_date_without_overwriting_manual(self) -> None:
        same_url = self.candidate(
            title="Different title",
            venue_url=self.manual["source_url"],
            event_start=self.manual["event_start"],
            event_end=self.manual["event_end"],
        )
        same_identity = self.candidate(
            title=self.manual["name"],
            venue_url="https://example.org/alternate",
            event_start=self.manual["event_start"],
            deadline=self.manual["deadlines"]["paper"],
            event_end=self.manual["event_end"],
        )
        original = copy.deepcopy(self.manual)
        merged, stats, _ = discovery.merge_candidates(
            [self.manual],
            [(source("confs_tech"), [same_url, same_identity])],
            scan_at=SCAN_AT,
            today=TODAY,
            max_additions=10,
        )
        self.assertEqual(stats["duplicates"], 2)
        self.assertEqual(merged[0], original)
        self.assertEqual(self.manual, original)

    def test_recurring_url_with_a_different_edition_is_not_dropped(self) -> None:
        recurring = self.candidate(
            venue_url=self.manual["source_url"],
            event_start="2028-02-01",
            event_end="2028-02-02",
            deadline="2027-12-01",
        )
        merged, stats, _ = discovery.merge_candidates(
            [copy.deepcopy(self.manual)],
            [(source("confs_tech"), [recurring])],
            scan_at=SCAN_AT,
            today=TODAY,
            max_additions=10,
        )
        self.assertEqual(stats["added"], 1)
        self.assertEqual(len(merged), 2)

    def test_quarantines_past_and_malicious_candidates(self) -> None:
        past = self.candidate(
            deadline="2020-01-01", event_start="2020-02-01", event_end="2020-02-02"
        )
        malicious = self.candidate(
            title="<script>alert(1)</script>", venue_url="javascript:alert(1)"
        )
        _merged, stats, reasons = discovery.merge_candidates(
            [copy.deepcopy(self.manual)],
            [(source("confs_tech"), [past, malicious])],
            scan_at=SCAN_AT,
            today=TODAY,
            max_additions=10,
        )
        self.assertEqual(stats["quarantined"], 2)
        self.assertEqual(reasons["past-only"], 1)
        self.assertEqual(reasons["malformed"], 1)

    def test_global_and_per_source_addition_caps_are_deterministic(self) -> None:
        candidates = [
            self.candidate(
                external_id=f"candidate-{index}",
                title=f"Cloud Conference {index}",
                venue_url=f"https://example.org/cloud-{index}",
            )
            for index in range(20)
        ]
        first, stats, reasons = discovery.merge_candidates(
            [copy.deepcopy(self.manual)],
            [(source("confs_tech"), list(reversed(candidates)))],
            scan_at=SCAN_AT,
            today=TODAY,
            max_additions=20,
            max_per_source=3,
        )
        second, _, _ = discovery.merge_candidates(
            [copy.deepcopy(self.manual)],
            [(source("confs_tech"), candidates)],
            scan_at=SCAN_AT,
            today=TODAY,
            max_additions=20,
            max_per_source=3,
        )
        self.assertEqual(stats["added"], 3)
        self.assertEqual(reasons["addition-limit"], 17)
        self.assertEqual(first, second)


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        source_payload = json.loads(
            (ROOT / "data" / "events-source.json").read_text(encoding="utf-8")
        )
        source_payload["events"] = [manual_fixture()]
        source_payload["event_count"] = 1
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source_path, self.state_path = root / "events.json", root / "state.json"
        self.registry_path = root / "registry.json"
        self.source_path.write_text(json.dumps(source_payload), encoding="utf-8")
        self.registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sources": [source("hackclub"), source("developers_events")],
                }
            ),
            encoding="utf-8",
        )

    def test_partial_failure_keeps_successful_results(self) -> None:
        rows = fixture("hackclub.json")

        def fake_fetch(url: str):
            if "hackathons.hackclub.com" in url:
                return rows
            raise discovery.DiscoveryError("fixture outage")

        state = discovery.run(
            registry_path=self.registry_path,
            source_path=self.source_path,
            state_path=self.state_path,
            fetch=fake_fetch,
            scan_at=SCAN_AT,
            today=TODAY,
            max_additions=5,
        )
        self.assertEqual(state["summary"]["sources_succeeded"], 1)
        self.assertEqual(state["summary"]["added"], 1)
        self.assertEqual(len(json.loads(self.source_path.read_text())["events"]), 2)
        health = {item["id"]: item for item in state["sources"]}
        self.assertEqual(health["hackclub-hackathons"]["requests"], 1)
        self.assertEqual(health["developers-events-cfps"]["status"], "failed")

    def test_total_failure_preserves_known_catalog(self) -> None:
        before = self.source_path.read_bytes()
        with self.assertRaisesRegex(discovery.DiscoveryError, "known data was preserved"):
            discovery.run(
                registry_path=self.registry_path,
                source_path=self.source_path,
                state_path=self.state_path,
                fetch=lambda _url: (_ for _ in ()).throw(
                    discovery.DiscoveryError("fixture outage")
                ),
                scan_at=SCAN_AT,
                today=TODAY,
            )
        self.assertEqual(self.source_path.read_bytes(), before)
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["summary"]["sources_succeeded"], 0)
        self.assertTrue(all(item["status"] == "failed" for item in state["sources"]))

    def test_failed_scan_preserves_prior_source_success_timestamp(self) -> None:
        rows = fixture("hackclub.json")

        def first_fetch(url: str):
            if "hackathons.hackclub.com" in url:
                return rows
            raise discovery.DiscoveryError("fixture outage")

        discovery.run(
            registry_path=self.registry_path,
            source_path=self.source_path,
            state_path=self.state_path,
            fetch=first_fetch,
            scan_at=SCAN_AT,
            today=TODAY,
        )
        later_scan = "2026-09-20T12:00:00Z"
        with self.assertRaises(discovery.TotalSourceFailure):
            discovery.run(
                registry_path=self.registry_path,
                source_path=self.source_path,
                state_path=self.state_path,
                fetch=lambda _url: (_ for _ in ()).throw(
                    discovery.DiscoveryError("fixture outage")
                ),
                scan_at=later_scan,
                today=TODAY,
            )
        state = json.loads(self.state_path.read_text())
        hackclub = next(
            item for item in state["sources"]
            if item["id"] == "hackclub-hackathons"
        )
        self.assertEqual(hackclub["last_success_at"], SCAN_AT)
        self.assertEqual(hackclub["last_attempt_at"], later_scan)

    def test_rerun_with_same_scan_is_catalog_idempotent(self) -> None:
        rows = fixture("hackclub.json")

        def fake_fetch(url: str):
            if "hackathons.hackclub.com" in url:
                return rows
            raise discovery.DiscoveryError("fixture outage")

        discovery.run(
            registry_path=self.registry_path,
            source_path=self.source_path,
            state_path=self.state_path,
            fetch=fake_fetch,
            scan_at=SCAN_AT,
            today=TODAY,
        )
        first = self.source_path.read_bytes()
        discovery.run(
            registry_path=self.registry_path,
            source_path=self.source_path,
            state_path=self.state_path,
            fetch=fake_fetch,
            scan_at=SCAN_AT,
            today=TODAY,
        )
        self.assertEqual(self.source_path.read_bytes(), first)

    def test_dry_run_does_not_write_catalog_or_state(self) -> None:
        before = self.source_path.read_bytes()
        state = discovery.run(
            registry_path=self.registry_path,
            source_path=self.source_path,
            state_path=self.state_path,
            fetch=lambda url: (
                fixture("hackclub.json")
                if "hackathons.hackclub.com" in url
                else (_ for _ in ()).throw(discovery.DiscoveryError("outage"))
            ),
            scan_at=SCAN_AT,
            today=TODAY,
            dry_run=True,
        )
        self.assertEqual(state["summary"]["sources_succeeded"], 1)
        self.assertEqual(self.source_path.read_bytes(), before)
        self.assertFalse(self.state_path.exists())

    def test_source_timeout_terminates_blocking_adapter(self) -> None:
        adapter_name = "fixture_hang"

        def hanging_adapter(
            _source: dict,
            *,
            fetch,
            today: dt.date,
        ) -> list[discovery.Candidate]:
            del fetch, today
            time.sleep(1)
            return []

        discovery.ADAPTERS[adapter_name] = hanging_adapter
        self.addCleanup(discovery.ADAPTERS.pop, adapter_name, None)
        hanging_source = copy.deepcopy(source("hackclub"))
        hanging_source.update({
            "adapter": adapter_name,
            "id": "fixture-hang",
            "name": "Fixture hanging source",
        })
        self.registry_path.write_text(
            json.dumps({"schema_version": 1, "sources": [hanging_source]}),
            encoding="utf-8",
        )
        started = time.monotonic()
        state = discovery.run(
            registry_path=self.registry_path,
            source_path=self.source_path,
            state_path=self.state_path,
            scan_at=SCAN_AT,
            today=TODAY,
            source_timeout=0.03,
            overall_timeout=0.2,
            require_any_success=False,
        )
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertEqual(state["sources"][0]["status"], "failed")
        self.assertIn("exceeded", state["sources"][0]["error"])


class AcronymTests(unittest.TestCase):
    def test_derived_acronyms(self) -> None:
        cases = {
            "The Asia-Pacific Satellite Event of ACM WebSci - WebSciX 2026": "WebSciX",
            "Tenth Annual Conference on Machine Learning and Systems": "MLS",
            "Workshop on Data Systems (DSW)": "DSW",
            "CityJS Conference Athens": "CityJS",
            "AMTSO Cyber Research Conference": "AMTSO",
            "All Day AI": "DayAI",
            "Blank Page": "BlankPage",
            "Jax London": "JaxLondon",
            "JavaScript & Angular Days": "JAD",
            "Minds in Motion: Deep dive into physical AI 2026": "MindsMotion",
            "Capitol": "Capitol",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(discovery.derive_acronym(name), expected)

    def test_acronym_skips_leading_stopwords(self) -> None:
        for name in ("The Asia-Pacific Satellite Event of ACM WebSci - WebSciX 2026", "All Day AI", "Tenth Annual Conference on Machine Learning and Systems"):
            self.assertNotIn(discovery.derive_acronym(name), {"The", "All", "Tenth", "Annual"})


class CuratedDuplicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.curated = manual_fixture()
        self.curated.update({
            "id": "mlsys-2027", "acronym": "MLSys",
            "name": "Conference on Machine Learning and Systems",
            "edition": "2027", "event_start": "", "event_end": "",
        })

    def candidate(self, **changes) -> discovery.Candidate:
        values = {
            "external_id": "mlsys-hackclub", "title": "Tenth Annual Conference on Machine Learning and Systems",
            "event_type": "Industry conference", "organizer": "See venue source",
            "venue_url": "https://mlsys.org/", "source_url": source("confs_tech")["documentation_url"],
            "event_start": "2027-06-20", "event_end": "2027-06-20",
        }
        values.update(changes)
        return discovery.Candidate(**values)

    def merge(self, candidate: discovery.Candidate, curated: dict):
        return discovery.merge_candidates(
            [curated], [(source("confs_tech"), [candidate])], scan_at=SCAN_AT, today=TODAY, max_additions=10
        )

    def test_name_containing_curated_name_is_duplicate(self) -> None:
        merged, stats, _ = self.merge(self.candidate(), self.curated)
        self.assertEqual((stats["added"], stats["duplicates"]), (0, 1))
        self.assertEqual(len(merged), 1)

    def test_name_containing_curated_acronym_is_duplicate(self) -> None:
        _, stats, _ = self.merge(self.candidate(title="MLSys 2027 Main Track", venue_url="https://example.org/x"), self.curated)
        self.assertEqual(stats["added"], 0)

    def test_overlapping_event_ranges_required(self) -> None:
        curated = {**self.curated, "event_start": "2027-05-17", "event_end": "2027-05-20"}
        _, stats, _ = self.merge(self.candidate(), curated)
        self.assertEqual(stats["added"], 1)
        _, stats, _ = self.merge(self.candidate(event_start="2027-05-19", event_end="2027-05-22"), curated)
        self.assertEqual(stats["added"], 0)

    def test_other_edition_is_not_duplicate(self) -> None:
        _, stats, _ = self.merge(self.candidate(event_start="2026-12-01", event_end="2026-12-02"), self.curated)
        self.assertEqual(stats["added"], 1)


if __name__ == "__main__":
    unittest.main()
