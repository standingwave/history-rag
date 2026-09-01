/* npm test — event-reminder logic: the due window, calendar-text title
   extraction, and wall-clock rendering across timezones. */
import test from "node:test";
import assert from "node:assert/strict";
import {
  due, eventTitle, reminderTitle, reminderBody, LEAD_MS, SWEEP_MS,
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

test("reminderBody rounds to minutes, floor 1", () => {
  assert.equal(reminderBody(NOW + 10 * 60_000, NOW), "in 10 min");
  assert.equal(reminderBody(NOW + 12.4 * 60_000, NOW), "in 12 min");
  assert.equal(reminderBody(NOW + 20_000, NOW), "in 1 min");
});
