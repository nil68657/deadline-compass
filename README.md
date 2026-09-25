# Deadline Compass

**[Live Site →](https://nil68657.github.io/deadline-compass/)**

Deadline Compass is a static, searchable opportunity tracker. It covers
curated academic calls for papers and abstracts, summits, industry
conferences, open-source gatherings, and any hackathons added to the source.
It does not claim exhaustive IEEE, ACM, or industry coverage.

The product name and slug are centralized in `site/brand.js`.

## Architecture

- `data/events-source.json` is the sanitized, tracked source of truth.
- `scripts/build_data.py` validates that source and produces public outputs.
- `site/data/events.json` and `site/data/events.csv` are deterministic,
  generated public outputs.
- `site/` is a dependency-free HTML, CSS, and JavaScript application suitable
  for GitHub Pages.
- `site/logic.js` contains pure search, filter, sort, and countdown logic.
- `.github/workflows/deadline-compass.yml` verifies, refreshes, and deploys
  the site.

## Local development

Python 3.11+ and Node.js 20+ are sufficient. There are no package
dependencies to install.

```bash
cd deadline-compass
npm run build
python3 -m http.server 8000 --directory site
```

Open `http://localhost:8000`. A web server is required because browsers block
module/data fetching from `file://` URLs.

Run all checks:

```bash
npm run check
```

That command rebuilds the data, verifies the generated files are current,
checks the centralized brand, runs JavaScript unit tests, and runs Python unit
tests.

## Source and public data schema

Each source event contains:

- stable ID, acronym, full name, organization, organization group, and edition
- event type, topics, broad categories, location, and delivery mode
- optional indexing, event start, and event end
- abstract, paper, notification, and camera-ready deadlines
- confidence, source basis, and the public source URL

Dates use ISO `YYYY-MM-DD`; an empty string means unpublished. Deadline
timezones are explicit when known. The generator validates the normalized
organization group, event type, topic list, filter categories, delivery mode,
gates, provenance, and stable ID.

Confidence has three values:

- `verified`: read on the venue's primary page on the source review date
- `announced`: published through a secondary index
- `projected`: estimated from historical cadence

The browser computes countdowns from date-only values, formats dates in UTC
to prevent day shifts, and keeps a deadline open through the end of its stated
timezone. Unknown zones conservatively use Anywhere on Earth. The source page
remains authoritative for exact cutoff times.

## Archived calls

A call whose every gate has passed is **archived**. It stays in the index —
a closed venue is how next year's date gets anticipated rather than
rediscovered — but it is not an opportunity, so it is left out of the
default view and appears only under the *Archived — deadline passed*
filter. The result summary says how many are hidden and turns that count
into the control that shows them.

Archiving is computed in the browser from the record's gates, never stored:
a venue is archived when no gate remains in the future, which means a call
whose abstract deadline has passed but whose paper deadline has not is
still open, and shows the paper gate.

## Forwarded cycles

When every gate of a venue's cycle has passed, that cycle is **archived**,
and the venue database then forwards the record to its **next** cycle:
edition moves on, dates are projected from the archived cycle, confidence
becomes `projected`. Such a record carries an optional `previous_cycle`
(`{edition, closed}`), and its card says so — *"2027 cycle archived — its
call closed 24 Sep 2026. Forwarded to the 2028 cycle; these dates are
projected until its call is published."* The build rejects a forwarded
record whose gates do not fall after the archived close.

The venue database lives in a private repository; its
`cfp/compass_drift.py` compares it with `data/events-source.json` daily and
opens an issue there when they disagree.

## Adding or updating an event

1. Add or update the normalized record in `data/events-source.json`.
2. Use a direct venue CFP URL where possible.
3. Set confidence and `source_checked_at` honestly after a real review.
4. Run `npm run check`.
5. Inspect the generated JSON/CSV diff for accidental private content.

Choose one of the supported event types: `Academic conference`, `Summit`,
`Industry conference`, `Hackathon`, or `Open-source meetup`.

## Validation and deduplication

The build fails on missing or unknown fields, unsupported enumerated values,
non-public or credential-bearing source URLs, local paths, malformed ISO
dates, duplicate stable IDs, or duplicate name/source/deadline records.
Output ordering and JSON key ordering are deterministic, so automation can
distinguish genuine source changes from build noise.

## Automation and source strategy

The GitHub Actions workflow runs daily at 07:17 UTC, on relevant `main`
changes, and on manual dispatch. It:

1. rebuilds and validates the public data;
2. runs all tests and branding checks;
3. commits only changed generated JSON/CSV on scheduled/manual runs; and
4. uploads only `site/` to GitHub Pages.

No secret or third-party API is required. The workflow does not scrape venue
websites. Many conference pages are dynamic, inconsistent, or disallow broad
automation; pretending those pages form a reliable canonical feed would make
the tracker less trustworthy. If validation or the tracked source fails,
the workflow stops before committing or deploying, so the previous data and
live site remain intact.

Future network adapters must be opt-in, document source terms, retain a
per-source last-known-good snapshot, and merge successful sources without
deleting records from a failed source.

The website links directly to official IEEE, ACM, USENIX, and Linux Foundation
directories plus public Sessionize, MLH, Devpost, and Meetup discovery pages.
These links extend discovery without implying that their listings are fully
ingested.

## Limitations

- Coverage is curated, not exhaustive.
- Projected dates are planning aids and must be verified.
- A source review date describes the database review, not continuous
  monitoring.
- Date-only records may omit submission cutoff times and time zones.
- Provider search directories can contain opportunities absent from the
  index.

## GitHub Pages deployment

In repository settings, set Pages “Build and deployment” to **GitHub
Actions**. The workflow deploys only the `site/` directory.

The equivalent CLI setup is:

```bash
gh api --method POST repos/OWNER/REPO/pages -f build_type=workflow
gh workflow run "Deadline Compass"
```

If Pages is already configured, the POST may report that the site already
exists; update it instead:

```bash
gh api --method PUT repos/OWNER/REPO/pages -f build_type=workflow
```

Made with ❤️ and 🤖 by Nilanjan.
