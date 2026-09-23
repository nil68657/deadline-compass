const DAY_MS = 86_400_000;
const ZONE_OFFSETS = {
  AoE: "-12:00",
  CEST: "+02:00",
  CET: "+01:00",
  EDT: "-04:00",
  EST: "-05:00",
  PDT: "-07:00",
  PST: "-08:00",
  GMT: "+00:00",
};

function calendarUtc(date) {
  return Date.UTC(date.getFullYear(), date.getMonth(), date.getDate());
}

export function daysUntil(isoDate, now = new Date()) {
  if (!isoDate) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (!match) return null;
  const target = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Math.round((target - calendarUtc(now)) / DAY_MS);
}

function cutoffOffset(timezone) {
  if (ZONE_OFFSETS[timezone]) return ZONE_OFFSETS[timezone];
  const match = /^UTC([+-])(\d{1,2})(?::(\d{2}))?$/.exec(timezone);
  if (!match) return ZONE_OFFSETS.AoE;
  return `${match[1]}${match[2].padStart(2, "0")}:${match[3] || "00"}`;
}

export function deadlineExpired(isoDate, timezone, now = new Date()) {
  if (!isoDate) return false;
  const cutoff = Date.parse(`${isoDate}T23:59:59${cutoffOffset(timezone)}`);
  return Number.isFinite(cutoff) && now.getTime() > cutoff;
}

export function activeGate(event, now = new Date()) {
  const gates = (event.deadlines.gates || [])
    .map((gate) => {
      const days = daysUntil(gate.date, now);
      const expired = deadlineExpired(gate.date, event.deadlines.timezone, now);
      return { ...gate, days: expired ? days : Math.max(days, 0), expired };
    })
    .filter((gate) => gate.days !== null)
    .sort((a, b) => a.days - b.days);
  return gates.find((gate) => !gate.expired) || gates.at(-1) || null;
}

export function deadlineStatus(event, now = new Date()) {
  const gate = activeGate(event, now);
  if (!gate) return "unannounced";
  const days = gate.days;
  if (gate.expired) return "archived";
  if (days <= 14) return "urgent";
  if (days <= 45) return "open";
  return "upcoming";
}

export function deadlineLabel(event, now = new Date()) {
  const gate = activeGate(event, now);
  if (!gate) return "Date not announced";
  const days = gate.days;
  if (gate.expired && days < -1) return `Closed ${Math.abs(days)} days ago`;
  if (gate.expired && days === -1) return "Closed yesterday";
  if (gate.expired) return "Closed today";
  if (days === 0) return "Due today";
  if (days === 1) return "1 day left";
  return `${days} days left`;
}

export function searchText(event) {
  return [
    event.acronym,
    event.name,
    event.organization,
    event.organization_group,
    event.event_type,
    event.location,
    event.mode,
    event.edition,
    event.indexing,
    ...event.topics,
    ...event.categories,
  ].join(" ").toLocaleLowerCase();
}

export function matchesFilters(event, filters, now = new Date()) {
  const query = filters.query.trim().toLocaleLowerCase();
  return (
    (!query || searchText(event).includes(query))
    && (!filters.organization || event.organization_group === filters.organization)
    && (!filters.eventType || event.event_type === filters.eventType)
    && (!filters.topic || event.categories.includes(filters.topic))
    && (!filters.mode || event.mode === filters.mode)
    && (
      // An archived call is one whose every gate has passed. It stays in the
      // index — a closed venue is how you find next year's date — but it is
      // not an opportunity, so it appears only when asked for by name.
      filters.status
        ? deadlineStatus(event, now) === filters.status
          || (filters.status === "open" && deadlineStatus(event, now) === "urgent")
        : deadlineStatus(event, now) !== "archived"
    )
  );
}

function deadlineSort(event, now) {
  const gate = activeGate(event, now);
  if (!gate) return { bucket: 1, days: Infinity };
  if (gate.expired) return { bucket: 2, days: gate.days };
  return { bucket: 0, days: gate.days };
}

export function sortEvents(events, sortBy, now = new Date()) {
  const rows = [...events];
  rows.sort((a, b) => {
    if (sortBy === "name") {
      return a.name.localeCompare(b.name);
    }
    if (sortBy === "event") {
      return (a.event_start || "9999-12-31").localeCompare(b.event_start || "9999-12-31")
        || a.name.localeCompare(b.name);
    }
    if (sortBy === "recently-closed") {
      const aGate = activeGate(a, now);
      const bGate = activeGate(b, now);
      const aBucket = aGate ? (aGate.expired ? 0 : 1) : 2;
      const bBucket = bGate ? (bGate.expired ? 0 : 1) : 2;
      if (aBucket !== bBucket) return aBucket - bBucket;
      if (aBucket === 2) return a.name.localeCompare(b.name);
      return (aBucket === 0 ? bGate.days - aGate.days : aGate.days - bGate.days)
        || a.name.localeCompare(b.name);
    }
    const aOrder = deadlineSort(a, now);
    const bOrder = deadlineSort(b, now);
    return aOrder.bucket - bOrder.bucket
      || (aOrder.bucket === 2 ? bOrder.days - aOrder.days : aOrder.days - bOrder.days)
      || a.name.localeCompare(b.name);
  });
  return rows;
}

export function uniqueValues(events, accessor) {
  return [...new Set(events.flatMap(accessor).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b));
}
