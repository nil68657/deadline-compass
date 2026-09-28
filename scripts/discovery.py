#!/usr/bin/env python3
"""Discover public event opportunities and merge them without erasing known data."""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import signal
import ssl
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from build_data import SOURCE, deadline_gates, reject_duplicate_keys, validate

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "data" / "discovery-sources.json"
STATE = ROOT / "data" / "discovery-state.json"
USER_AGENT = "DeadlineCompass/1.0 (+https://github.com/nil68657/deadline-compass)"
MAX_RESPONSE_BYTES = 2_000_000
CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 8.0
DEFAULT_SOURCE_TIMEOUT_SECONDS = 18.0
DEFAULT_OVERALL_TIMEOUT_SECONDS = 60.0
MAX_REDIRECTS = 2
SSL_CONTEXT = ssl.create_default_context()
ALLOWED_FETCH_HOSTS = {
    "api.github.com",
    "raw.githubusercontent.com",
    "developers.events",
    "hackathons.hackclub.com",
    "api2.openreview.net",
}
ADAPTERS: dict[str, Callable[..., list["Candidate"]]] = {}


class DiscoveryError(RuntimeError):
    """A bounded source or payload failure."""


class TransientFetchError(DiscoveryError):
    """A transport failure that may succeed on one retry."""


class SourceTimeout(DiscoveryError):
    """A hard request, source, or scan deadline was reached."""


class TotalSourceFailure(DiscoveryError):
    """Every enabled source failed while the catalog remained unchanged."""

    def __init__(self, message: str, state: dict[str, Any]) -> None:
        super().__init__(message)
        self.state = state


@dataclass(frozen=True)
class Candidate:
    external_id: str
    title: str
    event_type: str
    organizer: str
    venue_url: str
    source_url: str
    deadline: str = ""
    event_start: str = ""
    event_end: str = ""
    timezone: str = "See source"
    location: str = "TBA"
    mode: str = "TBA"
    topics: tuple[str, ...] = ()
    categories: tuple[str, ...] = ("Other",)
    confidence: str = "announced"


@dataclass
class FetchBudget:
    source_id: str
    max_requests: int
    fetch: Callable[[str], Any]
    requests: int = 0

    def __call__(self, url: str) -> Any:
        if self.requests >= self.max_requests:
            raise DiscoveryError(
                f"{self.source_id} exceeded its {self.max_requests}-request budget"
            )
        self.requests += 1
        return self.fetch(url)


def adapter(name: str) -> Callable[[Callable[..., list[Candidate]]], Callable[..., list[Candidate]]]:
    def register(function: Callable[..., list[Candidate]]) -> Callable[..., list[Candidate]]:
        ADAPTERS[name] = function
        return function

    return register


def clean_text(value: Any, limit: int = 300) -> str:
    if not isinstance(value, str):
        return ""
    value = html.unescape(re.sub(r"<[^>]*>", " ", value))
    value = unicodedata.normalize("NFKC", value)
    value = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in value
    )
    return re.sub(r"\s+", " ", value).strip()[:limit]


def iso_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    if not isinstance(value, str):
        return ""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value)
    if match:
        try:
            return dt.date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            return ""
    return ""


def canonical_url(value: str) -> str:
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f]", value):
        return ""
    try:
        parsed = urlparse(value.strip())
        port = parsed.port
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").casefold()
    except (UnicodeError, ValueError):
        return ""
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or parsed.fragment
        or host == "localhost"
        or host.endswith((".localhost", ".local"))
        or "." not in host
    ):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9.]+)", host, re.I):
        return ""
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    try:
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return ""
    sensitive = {"token", "key", "secret", "password", "auth", "credential"}
    if any(any(marker in key.casefold() for marker in sensitive) for key, _ in query_pairs):
        return ""
    query_pairs = [
        (key, value)
        for key, value in query_pairs
        if not key.casefold().startswith("utm_")
        and key.casefold() not in {"ref", "source"}
    ]
    return urlunparse(("https", host, path, "", urlencode(sorted(query_pairs)), ""))


def validate_fetch_url(value: str) -> str:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as error:
        raise DiscoveryError(f"Fetch URL is malformed: {value}") from error
    host = (parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme != "https"
        or host not in ALLOWED_FETCH_HOSTS
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise DiscoveryError(f"Fetch URL is not allowlisted: {value}")
    return value


@contextlib.contextmanager
def hard_timeout(seconds: float, label: str) -> Iterator[None]:
    """Interrupt blocking DNS/socket work on the Unix runners this project supports."""
    if seconds <= 0:
        raise SourceTimeout(f"{label} deadline exhausted")
    if not hasattr(signal, "setitimer") or not hasattr(signal, "SIGALRM"):
        raise DiscoveryError("Strict deadlines require a Unix SIGALRM implementation")
    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    duration = min(seconds, previous_timer[0]) if previous_timer[0] > 0 else seconds

    def raise_timeout(_signum: int, _frame: Any) -> None:
        raise SourceTimeout(f"{label} exceeded {duration:.1f}s")

    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, duration)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            elapsed = time.monotonic() - started
            remaining = max(0.001, previous_timer[0] - elapsed)
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


def _remaining(deadline: float, cap: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SourceTimeout("request deadline exhausted")
    return max(0.001, min(cap, remaining))


def _fetch_once(
    url: str,
    *,
    connect_timeout: float,
    read_timeout: float,
    overall_timeout: float,
    max_bytes: int,
) -> bytes:
    current = validate_fetch_url(url)
    deadline = time.monotonic() + overall_timeout
    for redirect_count in range(MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        host = parsed.hostname or ""
        target = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
        connection = http.client.HTTPSConnection(
            host,
            port=parsed.port or 443,
            timeout=_remaining(deadline, connect_timeout),
            context=SSL_CONTEXT,
        )
        try:
            connection.connect()
            if connection.sock is None:
                raise TransientFetchError("HTTPS connection has no socket")
            connection.sock.settimeout(_remaining(deadline, read_timeout))
            connection.request(
                "GET",
                target,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                if redirect_count >= MAX_REDIRECTS:
                    raise DiscoveryError("Response exceeded redirect limit")
                location = response.headers.get("Location")
                if not location:
                    raise DiscoveryError("Redirect response omitted Location")
                current = validate_fetch_url(urljoin(current, location))
                continue
            if response.status in {408, 425, 429, 500, 502, 503, 504}:
                raise TransientFetchError(f"HTTP {response.status}")
            if not 200 <= response.status < 300:
                raise DiscoveryError(f"HTTP {response.status}")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError as error:
                    raise DiscoveryError("Response Content-Length is invalid") from error
                if declared_length < 0 or declared_length > max_bytes:
                    raise DiscoveryError("Response exceeds declared size limit")
            payload = bytearray()
            while len(payload) <= max_bytes:
                connection.sock.settimeout(_remaining(deadline, read_timeout))
                chunk = response.read(min(65_536, max_bytes + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > max_bytes:
                raise DiscoveryError("Response exceeds size limit")
            return bytes(payload)
        except SourceTimeout:
            raise
        except DiscoveryError:
            raise
        except (http.client.HTTPException, OSError) as error:
            raise TransientFetchError(clean_text(str(error), 160) or type(error).__name__) from error
        finally:
            connection.close()
    raise DiscoveryError("Response exceeded redirect limit")


def fetch_json(
    url: str,
    *,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = READ_TIMEOUT_SECONDS,
    overall_timeout: float = REQUEST_TIMEOUT_SECONDS,
    retries: int = 1,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> Any:
    validate_fetch_url(url)
    if not 0 <= retries <= 2:
        raise ValueError("retries must be between 0 and 2")
    last_error: DiscoveryError | None = None
    for attempt in range(retries + 1):
        try:
            with hard_timeout(overall_timeout, "request"):
                payload = _fetch_once(
                    url,
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    overall_timeout=overall_timeout,
                    max_bytes=max_bytes,
                )
            try:
                return json.loads(
                    payload.decode("utf-8"),
                    object_pairs_hook=reject_duplicate_keys,
                )
            except (UnicodeError, ValueError) as error:
                raise DiscoveryError(f"Response JSON is invalid: {error}") from error
        except SourceTimeout:
            raise
        except TransientFetchError as error:
            last_error = error
            if attempt < retries:
                time.sleep(0.2 * (2**attempt))
        except DiscoveryError:
            raise
    raise DiscoveryError(f"Fetch failed after {retries + 1} attempts: {last_error}")


def infer_type(name: str) -> str:
    lowered = name.casefold()
    if "hackathon" in lowered or re.search(r"\bhack\b", lowered):
        return "Hackathon"
    if "summit" in lowered:
        return "Summit"
    if any(term in lowered for term in ("meetup", "community day", "fosdem", "open source", "kcd ")):
        return "Open-source meetup"
    return "Industry conference"


def infer_categories(text: str) -> tuple[str, ...]:
    lowered = f" {text.casefold()} "
    mapping = (
        ("Data & databases", ("data", "database", "analytics", "stream")),
        ("Systems & cloud", ("system", "cloud", "distributed", "kubernetes", "devops")),
        ("AI & machine learning", (" ai ", "machine learning", " ml ", "llm")),
        ("Security & privacy", ("security", "privacy", "owasp", "bsides")),
        ("Software engineering", ("software", "testing", "developer")),
        ("Programming languages", ("python", "java", "rust", "javascript", "php")),
        ("Open source & community", ("open source", "community", "fosdem", "hackathon")),
    )
    found = [category for category, terms in mapping if any(term in lowered for term in terms)]
    return tuple(found or ["Other"])


def mode_from(location: str, online: bool = False, hybrid: bool = False) -> str:
    if hybrid:
        return "Hybrid"
    if online or any(term in location.casefold() for term in ("online", "virtual")):
        return "Online"
    return "In person" if location and location != "TBA" else "TBA"


def has_current_or_future_date(
    *, deadline: str = "", start: str = "", end: str = "", today: dt.date
) -> bool:
    cutoff = today.isoformat()
    return any(value and value >= cutoff for value in (deadline, start, end))


def evenly_spaced(items: list[Any], limit: int) -> list[Any]:
    if limit <= 0:
        return []
    if len(items) <= limit:
        return items
    if limit == 1:
        return [items[0]]
    indexes = {
        round(index * (len(items) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [items[index] for index in sorted(indexes)]


@adapter("developers_events")
def discover_developers_events(source: dict[str, Any], *, fetch: Callable[[str], Any] = fetch_json, today: dt.date) -> list[Candidate]:
    payload = fetch(source["endpoint"])
    if not isinstance(payload, list):
        raise DiscoveryError("developers.events payload must be an array")
    candidates: list[Candidate] = []
    scan_limit = min(len(payload), max(source["max_items"] * 12, 500), 2_000)
    for row in payload[:scan_limit]:
        if not isinstance(row, dict) or not isinstance(row.get("conf"), dict):
            continue
        conference = row["conf"]
        dates = conference.get("date")
        start = iso_date(dates[0]) if isinstance(dates, list) and dates else ""
        end = iso_date(dates[-1]) if isinstance(dates, list) and dates else start
        deadline = iso_date(row.get("untilDate"))
        name = clean_text(conference.get("name"))
        venue_url = canonical_url(clean_text(row.get("link"))) or canonical_url(clean_text(conference.get("hyperlink")))
        if (
            not name
            or not venue_url
            or not has_current_or_future_date(
                deadline=deadline,
                start=start,
                end=end,
                today=today,
            )
        ):
            continue
        location = clean_text(conference.get("location")) or "TBA"
        candidates.append(Candidate(
            external_id=hashlib.sha256(f"{name}|{start}|{venue_url}".encode()).hexdigest()[:16],
            title=name, event_type=infer_type(name), organizer="See venue source",
            venue_url=venue_url, source_url=source["documentation_url"],
            deadline=deadline, event_start=start, event_end=end,
            location=location, mode=mode_from(location), topics=("Developer events",),
            categories=infer_categories(name),
        ))
        if len(candidates) >= source["max_items"]:
            break
    return candidates


@adapter("hackclub")
def discover_hackclub(source: dict[str, Any], *, fetch: Callable[[str], Any] = fetch_json, today: dt.date) -> list[Candidate]:
    payload = fetch(source["endpoint"])
    if not isinstance(payload, list):
        raise DiscoveryError("Hack Club payload must be an array")
    candidates: list[Candidate] = []
    scan_limit = min(len(payload), source["max_items"] * 4, 500)
    for row in payload[:scan_limit]:
        if not isinstance(row, dict):
            continue
        name = clean_text(row.get("name"))
        venue_url = canonical_url(clean_text(row.get("website")))
        start = iso_date(row.get("start"))
        end = iso_date(row.get("end")) or start
        if (
            not name
            or not venue_url
            or not start
            or not has_current_or_future_date(start=start, end=end, today=today)
        ):
            continue
        place = ", ".join(part for part in (clean_text(row.get("city")), clean_text(row.get("state")), clean_text(row.get("country"))) if part) or "TBA"
        candidates.append(Candidate(
            external_id=clean_text(row.get("id")) or hashlib.sha256(venue_url.encode()).hexdigest()[:16],
            title=name, event_type="Hackathon", organizer="Hack Club community",
            venue_url=venue_url, source_url=source["documentation_url"],
            event_start=start, event_end=end,
            location=place,
            mode=mode_from(place, row.get("virtual") is True, row.get("hybrid") is True),
            topics=("Student hackathons",), categories=("Open source & community",),
        ))
        if len(candidates) >= source["max_items"]:
            break
    return candidates


def parse_confs_tech_rows(
    payload: Any,
    *,
    topic: str,
    source: dict[str, Any],
    today: dt.date,
) -> list[Candidate]:
    if not isinstance(payload, list):
        raise DiscoveryError("confs.tech topic payload must be an array")
    candidates: list[Candidate] = []
    for row in payload[: min(len(payload), source["max_items"] * 4, 1_000)]:
        if not isinstance(row, dict):
            continue
        name = clean_text(row.get("name"))
        venue_url = canonical_url(clean_text(row.get("cfpUrl"))) or canonical_url(clean_text(row.get("url")))
        start = iso_date(row.get("startDate"))
        end = iso_date(row.get("endDate")) or start
        deadline = iso_date(row.get("cfpEndDate"))
        if (
            not name
            or not venue_url
            or not start
            or not has_current_or_future_date(
                deadline=deadline,
                start=start,
                end=end,
                today=today,
            )
        ):
            continue
        location = ", ".join(part for part in (clean_text(row.get("city")), clean_text(row.get("country"))) if part) or ("Online" if row.get("online") else "TBA")
        candidates.append(Candidate(
            external_id=hashlib.sha256(f"{name}|{start}|{venue_url}".encode()).hexdigest()[:16],
            title=name, event_type=infer_type(name), organizer="See venue source",
            venue_url=venue_url, source_url=source["documentation_url"],
            deadline=deadline, event_start=start,
            event_end=end, location=location,
            mode=mode_from(location, row.get("online") is True),
            topics=(topic.replace(".json", "").replace("-", " ").title(),),
            categories=infer_categories(f"{topic} {name}"),
        ))
        if len(candidates) >= source["max_items"]:
            break
    return candidates


@adapter("confs_tech")
def discover_confs_tech(source: dict[str, Any], *, fetch: Callable[[str], Any] = fetch_json, today: dt.date) -> list[Candidate]:
    files: list[tuple[int, str, str]] = []
    for year in (today.year, today.year + 1):
        listing = fetch(source["endpoint"].format(year=year))
        if not isinstance(listing, list):
            raise DiscoveryError("confs.tech index payload must be an array")
        for row in listing[:500]:
            if not isinstance(row, dict):
                continue
            name = clean_text(row.get("name"), 120)
            download_url = clean_text(row.get("download_url"), 1_000)
            if (
                name.endswith(".json")
                and canonical_url(download_url)
                and urlparse(download_url).hostname == "raw.githubusercontent.com"
            ):
                files.append((year, name, download_url))
    detail_budget = max(0, source["max_requests"] - 2)
    selected = evenly_spaced(sorted(set(files)), detail_budget)
    candidates: list[Candidate] = []
    for _year, name, download_url in selected:
        remaining = source["max_items"] - len(candidates)
        if remaining <= 0:
            break
        parsed = parse_confs_tech_rows(
            fetch(download_url),
            topic=name,
            source={**source, "max_items": remaining},
            today=today,
        )
        candidates.extend(parsed)
    return candidates[: source["max_items"]]


def content_value(content: Any, key: str) -> Any:
    return content[key].get("value") if isinstance(content, dict) and isinstance(content.get(key), dict) else None


@adapter("openreview")
def discover_openreview(source: dict[str, Any], *, fetch: Callable[[str], Any] = fetch_json, today: dt.date) -> list[Candidate]:
    payload = fetch(source["endpoint"])
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list) or not groups or not isinstance(groups[0], dict):
        raise DiscoveryError("OpenReview active venue payload is malformed")
    members = groups[0].get("members")
    if not isinstance(members, list):
        raise DiscoveryError("OpenReview active venue members are missing")
    years = {str(today.year), str(today.year + 1), str(today.year + 2)}
    eligible = sorted({
        member
        for member in members[:5_000]
        if isinstance(member, str)
        and any(f"/{year}/" in member for year in years)
        and member.count("/") >= 2
    })
    venue_ids = evenly_spaced(
        eligible,
        min(source["max_items"], max(0, source["max_requests"] - 1)),
    )
    candidates: list[Candidate] = []
    detail_failures = 0
    for venue_id in venue_ids:
        try:
            detail = fetch("https://api2.openreview.net/groups?" + urlencode({"id": venue_id}))
        except DiscoveryError:
            detail_failures += 1
            continue
        detail_groups = detail.get("groups") if isinstance(detail, dict) else None
        if not isinstance(detail_groups, list) or not detail_groups:
            continue
        content = detail_groups[0].get("content") if isinstance(detail_groups[0], dict) else None
        name = clean_text(content_value(content, "title") or content_value(content, "subtitle"))
        venue_url = canonical_url(clean_text(content_value(content, "website")))
        start = iso_date(content_value(content, "start_date"))
        deadline = iso_date(content_value(content, "submission_deadline"))
        if not name or not venue_url or not (start or deadline):
            continue
        location = clean_text(content_value(content, "location")) or "TBA"
        candidates.append(Candidate(
            external_id=venue_id, title=name, event_type="Academic conference",
            organizer="OpenReview venue chairs", venue_url=venue_url,
            source_url=source["documentation_url"], deadline=deadline,
            event_start=start, event_end=start, location=location,
            mode=mode_from(location), topics=("Peer-reviewed research",),
            categories=infer_categories(name),
        ))
    if venue_ids and detail_failures == len(venue_ids):
        raise DiscoveryError("Every bounded OpenReview venue detail request failed")
    return candidates


def load_registry(path: Path = REGISTRY) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "sources"} or payload.get("schema_version") != 1 or not isinstance(payload.get("sources"), list):
        raise DiscoveryError("Source registry top-level schema is invalid")
    required = {
        "id", "name", "adapter", "endpoint", "documentation_url", "license",
        "attribution", "enabled", "max_items", "max_requests",
    }
    seen: set[str] = set()
    sources: list[dict[str, Any]] = []
    for source in payload["sources"]:
        if not isinstance(source, dict) or set(source) != required:
            raise DiscoveryError("Source registry entry schema is invalid")
        text_fields = (
            "id", "name", "adapter", "endpoint", "documentation_url",
            "license", "attribution",
        )
        if any(
            not isinstance(source.get(field), str)
            or not clean_text(source[field], 1_000)
            for field in text_fields
        ):
            raise DiscoveryError("Source registry text fields are invalid")
        if (
            source["id"] in seen
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", source["id"])
            or source["adapter"] not in ADAPTERS
            or not isinstance(source["enabled"], bool)
            or not isinstance(source["max_items"], int)
            or isinstance(source["max_items"], bool)
            or not 1 <= source["max_items"] <= 500
            or not isinstance(source["max_requests"], int)
            or isinstance(source["max_requests"], bool)
            or not 1 <= source["max_requests"] <= 32
        ):
            raise DiscoveryError(f"Source registry entry is invalid: {source.get('id')}")
        validate_fetch_url(source["endpoint"].replace("{year}", "2027"))
        if not canonical_url(source["documentation_url"]):
            raise DiscoveryError(f"Source documentation URL is invalid: {source['id']}")
        seen.add(source["id"])
        sources.append(source)
    return sources


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def normalized_identity(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def candidate_to_event(candidate: Candidate, *, source: dict[str, Any], scan_at: str, today: dt.date) -> tuple[dict[str, Any] | None, str | None]:
    name, venue_url = clean_text(candidate.title), canonical_url(candidate.venue_url)
    deadline, start = iso_date(candidate.deadline), iso_date(candidate.event_start)
    end = iso_date(candidate.event_end) or start
    if not name or not venue_url or not (deadline or start):
        return None, "malformed"
    if deadline and deadline < today.isoformat() and (not end or end < today.isoformat()):
        return None, "past-only"
    if not deadline and end and end < today.isoformat():
        return None, "past-only"
    if start and start > (today + dt.timedelta(days=1095)).isoformat():
        return None, "low-confidence"
    edition = (start or deadline)[:4]
    identity = hashlib.sha256(f"{venue_url}|{edition}".encode()).hexdigest()[:10]
    acronym = re.sub(r"[^A-Za-z0-9+]", "", name.split()[0])[:18] or "Event"
    external_id = clean_text(candidate.external_id, 200) or hashlib.sha256(
        f"{name}|{venue_url}|{start}|{deadline}".encode()
    ).hexdigest()[:16]
    source_basis = {
        "verified": "Primary venue page",
        "announced": "Published via a secondary index",
        "projected": "Historical cadence projection",
    }.get(candidate.confidence, "")
    event = {
        "id": f"discovered-{slug(acronym)}-{edition}-{identity}",
        "acronym": acronym, "name": name,
        "organization": clean_text(candidate.organizer) or "See venue source",
        "organization_group": "OpenReview" if source["adapter"] == "openreview" else "Other",
        "event_type": candidate.event_type,
        "topics": sorted(set(filter(None, (clean_text(item, 80) for item in candidate.topics)))),
        "categories": sorted(set(filter(None, (
            clean_text(item, 80) for item in (candidate.categories or ("Other",))
        )))),
        "location": clean_text(candidate.location) or "TBA", "mode": candidate.mode,
        "edition": edition, "indexing": "", "event_start": start, "event_end": end,
        "deadlines": {"abstract": "", "paper": deadline, "notification": "", "camera_ready": "", "gates": deadline_gates("", deadline), "timezone": candidate.timezone, "precision": "date"},
        "confidence": candidate.confidence, "source_url": venue_url,
        "source_basis": source_basis,
        "discovery": {"source_id": source["id"], "source_name": source["name"], "source_url": canonical_url(source["documentation_url"]), "external_id": external_id, "discovered_at": scan_at, "last_seen_at": scan_at},
    }
    try:
        validate([event])
    except ValueError:
        return None, "malformed"
    return event, None


def event_dates(event: dict[str, Any]) -> set[str]:
    deadlines = event.get("deadlines", {})
    values = (
        event.get("event_start", ""),
        event.get("event_end", ""),
        deadlines.get("abstract", "") if isinstance(deadlines, dict) else "",
        deadlines.get("paper", "") if isinstance(deadlines, dict) else "",
    )
    return {value for value in values if isinstance(value, str) and value}


def merge_candidates(events: list[dict[str, Any]], batches: list[tuple[dict[str, Any], list[Candidate]]], *, scan_at: str, today: dt.date, max_additions: int, max_per_source: int = 8) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
    merged = copy.deepcopy(events)
    url_index: dict[tuple[str, str], dict[str, Any]] = {}
    identity_index: dict[tuple[str, str], dict[str, Any]] = {}
    external_index: dict[tuple[str, str], dict[str, Any]] = {}

    def add_to_indexes(event: dict[str, Any]) -> None:
        dates = event_dates(event)
        url = canonical_url(event["source_url"])
        identity = normalized_identity(event["name"])
        for date in dates:
            if url:
                url_index.setdefault((url, date), event)
            identity_index.setdefault((identity, date), event)
        discovery = event.get("discovery")
        if isinstance(discovery, dict):
            external_index.setdefault(
                (discovery.get("source_id", ""), discovery.get("external_id", "")),
                event,
            )

    for event in merged:
        add_to_indexes(event)
    stats = {"added": 0, "updated": 0, "duplicates": 0, "quarantined": 0}
    reasons: dict[str, int] = {}
    per_source: dict[str, int] = {}
    for source, candidates in batches:
        ordered_candidates = sorted(
            candidates,
            key=lambda candidate: (
                iso_date(candidate.event_start) or iso_date(candidate.deadline),
                clean_text(candidate.title).casefold(),
                clean_text(candidate.external_id),
            ),
        )
        for candidate in ordered_candidates:
            event, reason = candidate_to_event(candidate, source=source, scan_at=scan_at, today=today)
            if event is None:
                stats["quarantined"] += 1
                reasons[reason or "malformed"] = reasons.get(reason or "malformed", 0) + 1
                continue
            dates = sorted(event_dates(event))
            discovery = event["discovery"]
            existing = external_index.get(
                (discovery["source_id"], discovery["external_id"])
            )
            if existing is None:
                url = canonical_url(event["source_url"])
                existing = next(
                    (url_index[(url, date)] for date in dates if (url, date) in url_index),
                    None,
                )
            if existing is None:
                identity = normalized_identity(event["name"])
                existing = next(
                    (
                        identity_index[(identity, date)]
                        for date in dates
                        if (identity, date) in identity_index
                    ),
                    None,
                )
            if existing is not None:
                stats["duplicates"] += 1
                existing_discovery = existing.get("discovery")
                if isinstance(existing_discovery, dict) and existing_discovery.get("source_id") == source["id"] and existing_discovery.get("last_seen_at") != scan_at:
                    existing_discovery["last_seen_at"] = scan_at
                    stats["updated"] += 1
                continue
            source_additions = per_source.get(source["id"], 0)
            if stats["added"] >= max_additions or source_additions >= max_per_source:
                stats["quarantined"] += 1
                reasons["addition-limit"] = reasons.get("addition-limit", 0) + 1
                continue
            merged.append(event)
            add_to_indexes(event)
            per_source[source["id"]] = source_additions + 1
            stats["added"] += 1
    validate(merged)
    return merged, stats, reasons


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def source_health(
    source: dict[str, Any],
    *,
    status: str,
    scan_at: str,
    last_success_at: str,
    candidates: int,
    requests: int,
    error: str = "",
) -> dict[str, Any]:
    return {
        "id": source["id"],
        "name": source["name"],
        "status": status,
        "last_attempt_at": scan_at if status != "not-run" else "",
        "last_success_at": last_success_at,
        "candidates": candidates,
        "requests": requests,
        "error": clean_text(error, 240),
    }


def run(
    *,
    registry_path: Path = REGISTRY,
    source_path: Path = SOURCE,
    state_path: Path = STATE,
    fetch: Callable[[str], Any] = fetch_json,
    scan_at: str | None = None,
    today: dt.date | None = None,
    max_additions: int = 20,
    dry_run: bool = False,
    require_any_success: bool = True,
    source_timeout: float = DEFAULT_SOURCE_TIMEOUT_SECONDS,
    overall_timeout: float = DEFAULT_OVERALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    scan_at, today = scan_at or utc_now(), today or dt.date.today()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", scan_at):
        raise DiscoveryError("scan_at must use UTC RFC3339 seconds")
    if not 0 <= max_additions <= 100:
        raise DiscoveryError("max_additions must be between 0 and 100")
    if not 0 < source_timeout <= 60 or not 0 < overall_timeout <= 300:
        raise DiscoveryError("source/overall timeouts are outside safe bounds")
    sources = [source for source in load_registry(registry_path) if source["enabled"]]
    previous_success: dict[str, str] = {}
    if state_path.exists():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
            previous_success = {
                row["id"]: row.get("last_success_at", "")
                for row in previous.get("sources", [])
                if isinstance(row, dict) and isinstance(row.get("id"), str)
            }
        except (OSError, ValueError, TypeError):
            previous_success = {}
    payload = json.loads(source_path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
    events = payload.get("events")
    if not isinstance(events, list):
        raise DiscoveryError("Tracked source has no events array")
    validate(events)
    health: list[dict[str, Any]] = []
    batches: list[tuple[dict[str, Any], list[Candidate]]] = []
    attempted = 0
    overall_deadline = time.monotonic() + overall_timeout
    for source in sources:
        remaining = overall_deadline - time.monotonic()
        if remaining <= 0:
            health.append(source_health(
                source,
                status="not-run",
                scan_at=scan_at,
                last_success_at=previous_success.get(source["id"], ""),
                candidates=0,
                requests=0,
                error="Overall scan deadline exhausted",
            ))
            continue
        attempted += 1
        budget = FetchBudget(source["id"], source["max_requests"], fetch)
        try:
            with hard_timeout(
                min(source_timeout, remaining),
                f"{source['id']} source",
            ):
                candidates = ADAPTERS[source["adapter"]](
                    source,
                    fetch=budget,
                    today=today,
                )
            if (
                not isinstance(candidates, list)
                or len(candidates) > source["max_items"]
                or not all(isinstance(candidate, Candidate) for candidate in candidates)
            ):
                raise DiscoveryError("Adapter violated its candidate result cap/schema")
            batches.append((source, candidates))
            health.append(source_health(
                source,
                status="ok",
                scan_at=scan_at,
                last_success_at=scan_at,
                candidates=len(candidates),
                requests=budget.requests,
            ))
        except (DiscoveryError, KeyError, TypeError, ValueError) as error:
            health.append(source_health(
                source,
                status="failed",
                scan_at=scan_at,
                last_success_at=previous_success.get(source["id"], ""),
                candidates=0,
                requests=budget.requests,
                error=str(error) or type(error).__name__,
            ))
    successes = len(batches)
    candidates_seen = sum(len(candidates) for _source, candidates in batches)
    if successes:
        merged, merge_stats, reasons = merge_candidates(events, batches, scan_at=scan_at, today=today, max_additions=max_additions)
    else:
        merged, merge_stats, reasons = (
            events,
            {"added": 0, "updated": 0, "duplicates": 0, "quarantined": 0},
            {},
        )
    summary = {
        "sources_attempted": attempted,
        "sources_succeeded": successes,
        "candidates_seen": candidates_seen,
        **merge_stats,
    }
    state = {"schema_version": 1, "last_scan_at": scan_at, "summary": summary, "sources": health, "quarantine": [{"reason": reason, "count": count} for reason, count in sorted(reasons.items())]}
    if not dry_run:
        if successes:
            payload["events"] = sorted(merged, key=lambda event: (event["deadlines"]["gates"][0]["date"] if event["deadlines"]["gates"] else "9999-12-31", event["name"].casefold(), event["id"]))
            payload["event_count"] = len(payload["events"])
            atomic_write(
                source_path,
                json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            )
        atomic_write(
            state_path,
            json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        )
    if not successes and require_any_success:
        raise TotalSourceFailure(
            "All enabled discovery sources failed; known data was preserved",
            state,
        )
    return state


def print_state(state: dict[str, Any]) -> None:
    print(json.dumps(state["summary"], sort_keys=True))
    for source in state["sources"]:
        suffix = f": {source['error']}" if source["error"] else ""
        print(
            f"{source['id']}: {source['status']} "
            f"({source['candidates']} candidates, {source['requests']} requests)"
            f"{suffix}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bounded public-feed discovery for Deadline Compass",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-total-failure", action="store_true")
    parser.add_argument("--max-additions", type=int, default=20)
    parser.add_argument(
        "--source-timeout",
        type=float,
        default=DEFAULT_SOURCE_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--overall-timeout",
        type=float,
        default=DEFAULT_OVERALL_TIMEOUT_SECONDS,
    )
    args = parser.parse_args()
    if not 0 <= args.max_additions <= 100:
        parser.error("--max-additions must be between 0 and 100")
    if not 0 < args.source_timeout <= 60:
        parser.error("--source-timeout must be greater than 0 and at most 60")
    if not 0 < args.overall_timeout <= 300:
        parser.error("--overall-timeout must be greater than 0 and at most 300")
    try:
        state = run(
            dry_run=args.dry_run,
            require_any_success=not args.allow_total_failure,
            max_additions=args.max_additions,
            source_timeout=args.source_timeout,
            overall_timeout=args.overall_timeout,
        )
    except TotalSourceFailure as error:
        print_state(error.state)
        print(f"discovery failed: {error}")
        return 2
    except DiscoveryError as error:
        print(f"discovery failed: {error}")
        return 2
    print_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
