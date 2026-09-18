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
} from "../site/logic.js";

const NOW = new Date(2026, 8, 18, 23, 30);

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
  assert.equal(deadlineStatus(event({ deadlines: { gates: [{ date: "2026-09-17", kind: "Paper" }] } }), NOW), "closed");
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
  assert.equal(deadlineStatus(zoned, afterCutoff), "closed");
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
