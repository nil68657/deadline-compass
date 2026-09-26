#!/usr/bin/env python3
"""Validate tracked public event data and emit deterministic site artifacts."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import ipaddress
import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlparse

APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = APP_ROOT / "site" / "data"
SOURCE = APP_ROOT / "data" / "events-source.json"
ALLOWED_CONFIDENCE = {"verified", "announced", "projected"}
EVENT_TYPES = {
    "Academic conference",
    "Summit",
    "Industry conference",
    "Hackathon",
    "Open-source meetup",
}
MODES = {"In person", "Online", "Hybrid", "TBA"}
ORGANIZATION_GROUPS = {
    "IEEE", "ACM", "IEEE / ACM", "USENIX", "Springer", "SCITEPRESS",
    "Linux Foundation", "OpenReview", "Other",
}
CATEGORIES = {
    "Data & databases", "Systems & cloud", "AI & machine learning",
    "Security & privacy", "Software engineering", "Architecture & hardware",
    "Networking", "Graphics, vision & XR", "HCI", "Programming languages",
    "Open source & community", "Other",
}
SOURCE_BASIS = {
    "verified": "Primary venue page",
    "announced": "Published via a secondary index",
    "projected": "Historical cadence projection",
}
COVERAGE_NOTICE = (
    "Curated index, not exhaustive coverage. Always confirm dates, times, "
    "eligibility, and submission rules on the linked source."
)
Event = dict[str, Any]
EVENT_FIELDS = {
    "id", "acronym", "name", "organization", "organization_group",
    "event_type", "topics", "categories", "location", "mode", "edition",
    "indexing", "event_start", "event_end", "deadlines",
    "confidence", "source_url", "source_basis",
}
DEADLINE_FIELDS = {
    "abstract", "paper", "notification", "camera_ready", "gates",
    "timezone", "precision",
}
# Present only on a record whose last cycle was archived (every gate passed)
# and which now tracks the next cycle: what the archived cycle was, and when
# its call closed, so the page can say so rather than silently changing year.
OPTIONAL_EVENT_FIELDS = {"previous_cycle"}
PREVIOUS_CYCLE_FIELDS = {
    "edition", "closed", "location", "event_start", "event_end", "abstract",
    "paper", "notification", "camera_ready", "confidence", "source_url",
}
PREVIOUS_CYCLE_DATES = (
    "closed", "event_start", "event_end", "abstract", "paper",
    "notification", "camera_ready",
)
EVENT_STRING_FIELDS = EVENT_FIELDS - {"topics", "categories", "deadlines"}
DEADLINE_STRING_FIELDS = DEADLINE_FIELDS - {"gates"}


def deadline_gates(abstract_due: str, paper_due: str) -> list[dict[str, str]]:
    return [
        {"date": date, "kind": gate}
        for date, gate in sorted((
            (abstract_due, "Abstract"),
            (paper_due, "Paper"),
        ))
        if date
    ]


def contains_local_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    boundary = r"(?:^|[\s('\"`])"
    if re.search(rf"{boundary}(?:/[^/\s]+|\.\.?/|~/|[A-Za-z]:/)", normalized):
        return True
    if re.search(rf"{boundary}//[^/\s]", normalized) or "file://" in normalized.casefold():
        return True
    if re.search(
        rf"{boundary}(?:papers?|research|data-talk|conf-talk|private|internal)/",
        normalized,
        flags=re.I,
    ):
        return True
    if re.search(
        r"(?:^|\s)(?:private|internal|docs?|notes?|secrets?|config)/"
        r"[A-Za-z0-9_.~/-]+\.[A-Za-z0-9]+(?:$|\s)",
        normalized,
        flags=re.I,
    ):
        return True
    return False


def validate(events: list[Event]) -> None:
    errors: list[str] = []
    ids: set[str] = set()
    records: set[tuple[str, str, str, str]] = set()
    for event in events:
        if not isinstance(event, dict):
            errors.append("<unknown>: each event must be an object")
            continue
        label_value = event.get("acronym", "<unknown>")
        label = label_value if isinstance(label_value, str) else "<invalid acronym>"
        if set(event) - OPTIONAL_EVENT_FIELDS != EVENT_FIELDS:
            allowed = EVENT_FIELDS | OPTIONAL_EVENT_FIELDS
            errors.append(
                f"{label}: event fields must exactly match the public schema "
                f"(unexpected={sorted(set(event) - allowed)}, "
                f"missing={sorted(EVENT_FIELDS - set(event))})"
            )
        if "previous_cycle" in event:
            previous = event["previous_cycle"]
            if (
                not isinstance(previous, dict)
                or set(previous) != PREVIOUS_CYCLE_FIELDS
                or not all(isinstance(v, str) for v in previous.values())
                or not all(previous.get(k) for k in ("edition", "closed", "location", "source_url"))
                or not all(
                    not previous.get(k) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", previous[k])
                    for k in PREVIOUS_CYCLE_DATES
                )
                or previous.get("confidence") not in SOURCE_BASIS
                or not previous.get("source_url", "").startswith("https://")
            ):
                errors.append(f"{label}: previous_cycle must describe the archived cycle in full")
            elif any(
                gate["date"] <= previous["closed"]
                for gate in event.get("deadlines", {}).get("gates", [])
                if isinstance(gate, dict) and isinstance(gate.get("date"), str)
            ):
                errors.append(f"{label}: a forwarded cycle's gates must fall after the archived call closed")
            continue
        if any(not isinstance(event[field], str) for field in EVENT_STRING_FIELDS):
            errors.append(f"{label}: all scalar event fields must be strings")
            continue
        if any(
            not isinstance(event[field], list)
            or not all(isinstance(value, str) and value for value in event[field])
            for field in ("topics", "categories")
        ):
            errors.append(f"{label}: topics and categories must be arrays of non-empty strings")
            continue
        deadlines = event["deadlines"]
        if not isinstance(deadlines, dict) or set(deadlines) != DEADLINE_FIELDS:
            errors.append(f"{label}: deadline fields must exactly match the public schema")
            continue
        if any(not isinstance(deadlines[field], str) for field in DEADLINE_STRING_FIELDS):
            errors.append(f"{label}: all scalar deadline fields must be strings")
            continue
        if not isinstance(deadlines["gates"], list):
            errors.append(f"{label}: deadline gates must be an array")
            continue
        expected_gates = deadline_gates(deadlines["abstract"], deadlines["paper"])
        if deadlines["gates"] != expected_gates:
            errors.append(f"{label}: deadline gates do not match abstract/paper dates")
        if any(
            not isinstance(gate, dict)
            or set(gate) != {"date", "kind"}
            or not all(isinstance(value, str) for value in gate.values())
            for gate in deadlines["gates"]
        ):
            errors.append(f"{label}: each deadline gate must contain only date and kind")
        if event["id"] in ids:
            errors.append(f"{label}: duplicate id {event['id']}")
        ids.add(event["id"])
        malformed_url = False
        try:
            parsed = urlparse(event["source_url"])
        except ValueError:
            malformed_url = True
            parsed = urlparse("https://invalid.invalid")
        try:
            port = parsed.port
        except ValueError:
            port = -1
        if (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443):
            port = None
        canonical_url = (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower(),
            port,
            parsed.path.rstrip("/"),
            parsed.query,
        )
        key = (
            event["name"].casefold(),
            repr(canonical_url),
            event["deadlines"]["abstract"],
            event["deadlines"]["paper"],
        )
        if key in records:
            errors.append(f"{label}: duplicate event/source/deadlines")
        records.add(key)
        if event["confidence"] not in ALLOWED_CONFIDENCE:
            errors.append(f"{label}: invalid confidence {event['confidence']!r}")
        if event["event_type"] not in EVENT_TYPES:
            errors.append(f"{label}: invalid event type {event['event_type']!r}")
        if event["mode"] not in MODES:
            errors.append(f"{label}: invalid mode {event['mode']!r}")
        if event["organization_group"] not in ORGANIZATION_GROUPS:
            errors.append(f"{label}: invalid organization group")
        if not event["categories"] or not set(event["categories"]) <= CATEGORIES:
            errors.append(f"{label}: invalid categories")
        if any("|" in value for value in (*event["topics"], *event["categories"])):
            errors.append(f"{label}: topics/categories may not contain the CSV list delimiter")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", event["id"]):
            errors.append(f"{label}: invalid stable id")
        if event["deadlines"]["precision"] != "date":
            errors.append(f"{label}: unsupported deadline precision")
        timezone = event["deadlines"]["timezone"]
        if not (
            timezone in {"See source", "AoE", "CEST", "CET", "EDT", "EST", "PDT", "PST", "GMT"}
            or re.fullmatch(r"UTC[+-](?:[0-9]|1[0-4])(?::[0-5][0-9])?", timezone)
        ):
            errors.append(f"{label}: invalid deadline timezone")
        if event["source_basis"] != SOURCE_BASIS.get(event["confidence"]):
            errors.append(f"{label}: source basis does not match confidence")
        if (
            malformed_url
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or port == -1
            or parsed.username is not None
            or parsed.password is not None
            or re.search(r"\s", event["source_url"])
        ):
            errors.append(f"{label}: invalid source URL")
        host = (parsed.hostname or "").rstrip(".").casefold()
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            errors.append(f"{label}: source URL must use a public host")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            errors.append(f"{label}: source URL must use a public host")
        if address is None and re.fullmatch(r"(?:0x[0-9a-f]+|[0-9.]+)", host, flags=re.I):
            errors.append(f"{label}: source URL uses a non-canonical numeric host")
        if address is None and "." not in host:
            errors.append(f"{label}: source URL must use a public FQDN")
        sensitive_keys = {"token", "key", "secret", "password", "auth", "credential"}
        if any(
            any(marker in key.casefold() for marker in sensitive_keys)
            for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
        ):
            errors.append(f"{label}: source URL contains a sensitive query key")
        if any(marker in parsed.fragment.casefold() for marker in sensitive_keys):
            errors.append(f"{label}: source URL contains a sensitive fragment")
        date_fields = {
            "event_start": event["event_start"],
            "event_end": event["event_end"],
            **{key: event["deadlines"][key] for key in (
                "abstract", "paper", "notification", "camera_ready"
            )},
        }
        for field, value in date_fields.items():
            if value:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    errors.append(f"{label}: {field} must use exact YYYY-MM-DD format")
                    continue
                try:
                    dt.date.fromisoformat(value)
                except ValueError:
                    errors.append(f"{label}: {field} must be a real YYYY-MM-DD date, got {value!r}")
        if event["event_start"] and event["event_end"] and event["event_end"] < event["event_start"]:
            errors.append(f"{label}: event_end precedes event_start")
        if (
            event["deadlines"]["abstract"]
            and event["deadlines"]["paper"]
            and event["deadlines"]["paper"] < event["deadlines"]["abstract"]
        ):
            errors.append(f"{label}: paper deadline precedes abstract deadline")
        for value in _strings(event):
            if value != event["source_url"] and contains_local_path(value):
                errors.append(f"{label}: public field contains a local path")
    if errors:
        raise ValueError("\n".join(errors))


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _strings(nested)


def render_json(events: list[Event], checked: str) -> str:
    payload = {
        "schema_version": 2,
        "source_checked_at": checked,
        "event_count": len(events),
        "coverage_notice": COVERAGE_NOTICE,
        "events": events,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def render_csv(events: list[Event]) -> str:
    fields = [
        "id", "acronym", "name", "organization", "organization_group",
        "event_type", "topics", "categories", "location", "mode", "edition",
        "indexing", "event_start", "event_end", "abstract_due", "paper_due",
        "notification", "camera_ready", "first_deadline", "first_gate",
        "deadline_timezone", "confidence", "source_basis", "source_url",
        "previous_cycle_edition", "previous_cycle_closed",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for event in events:
        deadlines = event["deadlines"]
        first_gate = deadlines["gates"][0] if deadlines["gates"] else {
            "date": "",
            "kind": "Unannounced",
        }
        writer.writerow({
            **{field: event.get(field, "") for field in fields},
            "topics": "|".join(event["topics"]),
            "categories": "|".join(event["categories"]),
            "abstract_due": deadlines["abstract"],
            "paper_due": deadlines["paper"],
            "notification": deadlines["notification"],
            "camera_ready": deadlines["camera_ready"],
            "first_deadline": first_gate["date"],
            "first_gate": first_gate["kind"],
            "deadline_timezone": deadlines["timezone"],
            "previous_cycle_edition": event.get("previous_cycle", {}).get("edition", ""),
            "previous_cycle_closed": event.get("previous_cycle", {}).get("closed", ""),
        })
    return stream.getvalue()


def sorted_events(events: list[Event]) -> list[Event]:
    return sorted(events, key=lambda event: (
        event["deadlines"]["gates"][0]["date"] if event["deadlines"]["gates"] else "9999-12-31",
        event["name"].casefold(),
        event["id"],
    ))


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_source() -> tuple[str, list[Event]]:
    payload = json.loads(
        SOURCE.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    expected_fields = {
        "schema_version", "source_checked_at", "event_count",
        "coverage_notice", "events",
    }
    if set(payload) != expected_fields:
        raise ValueError("Source top-level fields must exactly match schema version 2")
    if (
        payload.get("schema_version") != 2
        or not isinstance(payload.get("event_count"), int)
        or isinstance(payload.get("event_count"), bool)
        or not isinstance(payload.get("coverage_notice"), str)
        or not isinstance(payload.get("events"), list)
    ):
        raise ValueError("Source must use schema version 2 and contain an events array")
    checked = payload.get("source_checked_at", "")
    if not isinstance(checked, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked):
        raise ValueError("source_checked_at must use exact YYYY-MM-DD format")
    dt.date.fromisoformat(checked)
    if payload["event_count"] != len(payload["events"]):
        raise ValueError("Source event_count does not match events")
    if payload["coverage_notice"] != COVERAGE_NOTICE:
        raise ValueError("Source coverage_notice must match the public disclosure")
    events = sorted_events(payload["events"])
    validate(events)
    return checked, events


def outputs() -> dict[str, str]:
    checked, events = load_source()
    return {
        "events.json": render_json(events, checked),
        "events.csv": render_csv(events),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    generated = outputs()
    if args.check:
        stale = [
            name for name, content in generated.items()
            if not (args.output_dir / name).exists()
            or (args.output_dir / name).read_bytes() != content.encode("utf-8")
        ]
        if stale:
            print(f"Generated data is stale: {', '.join(stale)}", file=sys.stderr)
            return 1
        print(f"Validated {len(json.loads(generated['events.json'])['events'])} events")
        return 0
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in generated.items():
        (args.output_dir / name).write_text(content, encoding="utf-8", newline="")
    print(f"Wrote {len(generated)} files to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
