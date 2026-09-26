import test from "node:test";
import assert from "node:assert/strict";

import {
  activeGate,
  deadlineExpired,
  daysUntil,
  deadlineLabel,
  deadlineStatus,
  matchesFilters,
  sortEvents,
  uniqueValues,
  withArchivedCycles,
} from "../site/logic.js";

const NOW = new Date(2026, 8, 18, 23, 30);

// No filter chosen in any field: what a first visit sees.
const NO_FILTERS = {
  query: "", organization: "", eventType: "", topic: "", mode: "", status: "",
};

function event(overrides = {}) {
  return {
    acronym: "TEST",
    name: "Test Systems Conference",
    organization: "ACM",
    organization_group: "ACM",
    event_type: "Academic conference",
    topics: ["Systems"],
    categories: ["Systems & cloud"],
    location: "Online",
    mode: "Online",
    edition: "2027",
    indexing: "DBLP",
    event_start: "2027-03-01",
    notes: "Synthetic test fixture",
    deadlines: {
      gates: [{ date: "2026-09-25", kind: "Paper" }],
      abstract: "",
      paper: "2026-09-25",
      notification: "",
      camera_ready: "",
      timezone: "AoE",
      precision: "date",
    },
    ...overrides,
  };
}

test("countdowns use calendar dates without time-of-day drift", () => {
  assert.equal(daysUntil("2026-09-18", NOW), 0);
  assert.equal(daysUntil("2026-09-19", NOW), 1);
  assert.equal(deadlineLabel(event(), NOW), "7 days left");
});

test("deadline statuses cover every window", () => {
  assert.equal(deadlineStatus(event(), NOW), "urgent");
  assert.equal(deadlineStatus(event({ deadlines: { gates: [{ date: "2026-10-20", kind: "Paper" }] } }), NOW), "open");
  assert.equal(deadlineStatus(event({ deadlines: { gates: [{ date: "2027-01-01", kind: "Paper" }] } }), NOW), "upcoming");
  assert.equal(deadlineStatus(event({ deadlines: { gates: [{ date: "2026-09-17", kind: "Paper" }] } }), NOW), "archived");
  assert.equal(deadlineStatus(event({ deadlines: { gates: [] } }), NOW), "unannounced");
});

test("active gate rolls from abstract to paper deadline", () => {
  const rollover = event({
    deadlines: {
      gates: [
        { date: "2026-09-17", kind: "Abstract" },
        { date: "2026-09-24", kind: "Paper" },
      ],
    },
  });
  assert.deepEqual(activeGate(rollover, NOW), {
    date: "2026-09-24",
    kind: "Paper",
    days: 6,
    expired: false,
  });
  assert.equal(deadlineStatus(rollover, NOW), "urgent");
});

test("AoE deadlines remain open through the full deadline day", () => {
  assert.equal(
    deadlineExpired("2026-09-18", "AoE", new Date("2026-09-19T00:30:00Z")),
    false,
  );
  assert.equal(
    deadlineExpired("2026-09-18", "AoE", new Date("2026-09-19T12:00:00Z")),
    true,
  );
});

test("status closes immediately after a stated timezone cutoff", () => {
  const zoned = event({
    deadlines: {
      gates: [{ date: "2026-10-11", kind: "Paper" }],
      timezone: "CEST",
    },
  });
  const afterCutoff = new Date("2026-10-11T22:30:00Z");
  assert.equal(deadlineStatus(zoned, afterCutoff), "archived");
  assert.equal(deadlineLabel(zoned, afterCutoff), "Closed today");
});

test("search and structured filters combine", () => {
  const filters = {
    query: "systems",
    organization: "ACM",
    eventType: "Academic conference",
    topic: "Systems & cloud",
    mode: "Online",
    status: "urgent",
  };
  assert.equal(matchesFilters(event(), filters, NOW), true);
  assert.equal(matchesFilters(event(), { ...filters, organization: "IEEE" }, NOW), false);
  assert.equal(matchesFilters(event(), { ...filters, query: "graphics" }, NOW), false);
  assert.equal(matchesFilters(event(), { ...filters, status: "open" }, NOW), true);
});

test("an archived call is out of the default view and in its own filter", () => {
  const live = event();
  const archived = event({ name: "Gone", deadlines: { gates: [{ date: "2026-09-17", kind: "Paper" }] } });
  // No status chosen means "show me what I can still submit to".
  assert.equal(matchesFilters(archived, NO_FILTERS, NOW), false);
  assert.equal(matchesFilters(live, NO_FILTERS, NOW), true);
  // Asking for archived by name shows archived only.
  const onlyArchived = { ...NO_FILTERS, status: "archived" };
  assert.equal(matchesFilters(archived, onlyArchived, NOW), true);
  assert.equal(matchesFilters(live, onlyArchived, NOW), false);
});

test("archived is excluded on its merits, not by the other filters", () => {
  // A search that names the archived venue still must not resurrect it: the
  // exclusion is about the deadline, not about how the row was reached.
  const archived = event({ name: "Gone", deadlines: { gates: [{ date: "2026-09-17", kind: "Paper" }] } });
  assert.equal(matchesFilters(archived, { ...NO_FILTERS, query: "Gone" }, NOW), false);
  assert.equal(matchesFilters(archived, { ...NO_FILTERS, query: "Gone", status: "archived" }, NOW), true);
});

test("a passed abstract does not archive a call whose paper gate is open", () => {
  const rolling = event({
    name: "Rolling",
    deadlines: {
      gates: [
        { date: "2026-09-17", kind: "Abstract" },
        { date: "2026-09-24", kind: "Paper" },
      ],
    },
  });
  assert.notEqual(deadlineStatus(rolling, NOW), "archived");
  assert.equal(matchesFilters(rolling, NO_FILTERS, NOW), true);
});

test("a forwarded venue keeps its archived cycle as its own archived entry", () => {
  const forwarded = event({
    id: "test-2028",
    edition: "2028",
    deadlines: { abstract: "", paper: "2027-09-17", notification: "", camera_ready: "",
      gates: [{ date: "2027-09-17", kind: "Paper" }], timezone: "AoE", precision: "date" },
    previous_cycle: {
      edition: "2027", closed: "2026-09-17", location: "Ghent, Belgium",
      event_start: "2027-03-01", event_end: "2027-03-03", abstract: "",
      paper: "2026-09-17", notification: "", camera_ready: "",
      confidence: "verified", source_url: "https://2027.example.org/cfp",
    },
  });
  const all = withArchivedCycles([forwarded]);
  assert.equal(all.length, 2);
  const [next, past] = all;
  assert.notEqual(deadlineStatus(next, NOW), "archived");
  assert.equal(deadlineStatus(past, NOW), "archived");
  assert.equal(past.edition, "2027");
  assert.equal(past.archived_cycle.next_edition, "2028");
  assert.equal(past.source_url, "https://2027.example.org/cfp");
  assert.equal("previous_cycle" in past, false);
  assert.notEqual(past.id, next.id);
  // Default view shows the next cycle only; Archived shows the past one only.
  assert.deepEqual(all.filter((e) => matchesFilters(e, NO_FILTERS, NOW)), [next]);
  assert.deepEqual(all.filter((e) => matchesFilters(e, { ...NO_FILTERS, status: "archived" }, NOW)), [past]);
});

test("the default sort lists archived calls most recently closed first", () => {
  const older = event({ name: "Older", deadlines: { gates: [{ date: "2026-09-10", kind: "Paper" }] } });
  const newer = event({ name: "Newer", deadlines: { gates: [{ date: "2026-09-17", kind: "Paper" }] } });
  assert.deepEqual(sortEvents([older, newer], "deadline", NOW).map((e) => e.name), ["Newer", "Older"]);
});

test("sorting is deterministic and does not mutate input", () => {
  const later = event({ name: "Beta", deadlines: { gates: [{ date: "2026-10-01", kind: "Paper" }] } });
  const sooner = event({ name: "Alpha", deadlines: { gates: [{ date: "2026-09-20", kind: "Paper" }] } });
  const closed = event({ name: "Closed", deadlines: { gates: [{ date: "2026-09-17", kind: "Paper" }] } });
  const input = [closed, later, sooner];
  const sorted = sortEvents(input, "deadline", NOW);
  assert.deepEqual(sorted.map((item) => item.name), ["Alpha", "Beta", "Closed"]);
  assert.deepEqual(input.map((item) => item.name), ["Closed", "Beta", "Alpha"]);
});

test("recently closed sorting handles multiple unannounced events", () => {
  const unknownB = event({ name: "Unknown B", deadlines: { gates: [] } });
  const unknownA = event({ name: "Unknown A", deadlines: { gates: [] } });
  const closed = event({ name: "Closed", deadlines: { gates: [{ date: "2026-09-17", kind: "Paper" }] } });
  assert.deepEqual(
    sortEvents([unknownB, closed, unknownA], "recently-closed", NOW).map((item) => item.name),
    ["Closed", "Unknown A", "Unknown B"],
  );
});

test("unique values flatten and sort filter facets", () => {
  const values = uniqueValues([
    event({ categories: ["Systems & cloud", "AI & machine learning"] }),
    event({ categories: ["AI & machine learning", "Security & privacy"] }),
  ], (item) => item.categories);
  assert.deepEqual(values, ["AI & machine learning", "Security & privacy", "Systems & cloud"]);
});
