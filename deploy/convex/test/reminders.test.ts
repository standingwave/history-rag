/* npm test — event-reminder logic: the due window, calendar-text title
   extraction, and wall-clock rendering across timezones. */
import test from "node:test";
import assert from "node:assert/strict";
import {
  due, eventTitle, reminderTitle, reminderBody, LEAD_MS, SWEEP_MS,
  localDayHour, digestTitle, digestBody,
} from "../convex/reminderMath.ts";

const NOW = Date.parse("2026-09-01T20:00:00Z");

test("due window: lead + one sweep of slack, never after start", () => {
  assert.equal(due(NOW + 1, NOW), true);                       // about to start
  assert.equal(due(NOW + LEAD_MS, NOW), true);                 // exactly at lead
  assert.equal(due(NOW + LEAD_MS + SWEEP_MS, NOW), true);      // edge of slack
  assert.equal(due(NOW + LEAD_MS + SWEEP_MS + 1, NOW), false); // too far out
  assert.equal(due(NOW, NOW), false);                          // started
  assert.equal(due(NOW - 60_000, NOW), false);                 // in the past
});

test("eventTitle strips the calendar-text scaffolding", () => {
  assert.equal(eventTitle(
    "Calendar event on 2026-09-01 (Tuesday) 13:30–14:30: Dentist at Valley Dental" +
    " — with Alice, Bob (apple:Work). Notes: bring insurance card"),
    "Dentist at Valley Dental");
  assert.equal(eventTitle(
    "Calendar event on 2026-09-01 (Tuesday), all day: Team offsite (google:Personal)."),
    "Team offsite");
  assert.equal(eventTitle(
    "Recurring calendar event (weekly) starting 2026-06-16 (Monday) 09:00–09:15: Stand-up (apple:Work)."),
    "Stand-up");
  // unparseable text falls back to its first line
  assert.equal(eventTitle("something unexpected\nsecond line"), "something unexpected");
});

test("reminderTitle renders the pref's wall clock", () => {
  const at = "2026-09-01T20:30:00+00:00";
  assert.equal(reminderTitle(at, "Dentist", "America/Los_Angeles"), "⏰ 13:30 Dentist");
  assert.equal(reminderTitle(at, "Dentist", "Europe/Lisbon"), "⏰ 21:30 Dentist");
  // crosses midnight between the two zones; only the time is shown
  const late = "2026-09-01T06:30:00+00:00";
  assert.equal(reminderTitle(late, "Red-eye", "America/Los_Angeles"), "⏰ 23:30 Red-eye");
  assert.equal(reminderTitle(late, "Red-eye", "Europe/Lisbon"), "⏰ 07:30 Red-eye");
  // invalid zone falls back to Pacific instead of throwing
  assert.equal(reminderTitle(at, "Dentist", "Not/A_Zone"), "⏰ 13:30 Dentist");
});

test("localDayHour: the zone's own calendar day and hour", () => {
  const t = Date.parse("2026-09-01T13:30:00Z");
  assert.deepEqual(localDayHour(t, "America/Los_Angeles"), { day: "2026-09-01", hour: 6 });
  assert.deepEqual(localDayHour(t, "Europe/Lisbon"), { day: "2026-09-01", hour: 14 });
  // 02:00 UTC is yesterday evening in LA, early morning in Lisbon
  const m = Date.parse("2026-09-02T02:00:00Z");
  assert.deepEqual(localDayHour(m, "America/Los_Angeles"), { day: "2026-09-01", hour: 19 });
  assert.deepEqual(localDayHour(m, "Europe/Lisbon"), { day: "2026-09-02", hour: 3 });
  assert.deepEqual(localDayHour(t, "Not/A_Zone"), { day: "2026-09-01", hour: 6 });
});

test("digest strings", () => {
  const t = Date.parse("2026-09-01T13:30:00Z");
  assert.equal(digestTitle(t, "America/Los_Angeles"), "☀️ Tuesday, Sep 1");
  assert.equal(digestBody({
    events: 3, allDay: 1, firstStart: "2026-09-01T17:00:00Z", firstTitle: "Stand-up",
    tasksOpen: 5, tasksDay: "2026-08-31",
  }, "America/Los_Angeles"), "4 events · first 10:00 Stand-up · 5 tasks to carry from Aug 31");
  assert.equal(digestBody({ events: 0, allDay: 0, tasksOpen: 0 }, "America/Los_Angeles"),
    "no events · no open tasks");
  assert.equal(digestBody({
    events: 1, allDay: 0, firstStart: "2026-09-01T21:00:00Z", firstTitle: "Dentist", tasksOpen: 1,
  }, "Europe/Lisbon"), "1 event · first 22:00 Dentist · 1 open task");
});

test("reminderBody rounds to minutes, floor 1", () => {
  assert.equal(reminderBody(NOW + 10 * 60_000, NOW), "in 10 min");
  assert.equal(reminderBody(NOW + 12.4 * 60_000, NOW), "in 12 min");
  assert.equal(reminderBody(NOW + 20_000, NOW), "in 1 min");
});
