/* Unified capture's pure half (wip/SPEC-unified-capture.md): the
   verbatim escape hatch and the creation/mutation tiering that decides
   whether a proposal auto-applies or asks via chips. */

/* A leading "note:" (or "note,") skips the parser entirely — verbatim
   capture is a deterministic guarantee, not a model behavior. Returns
   the prefix-stripped text, or null when the hatch doesn't apply. */
export function verbatim(text: string): string | null {
  const m = /^note[:,]\s*/i.exec(text);
  if (!m) return null;
  const rest = text.slice(m[0].length).trim();
  return rest || null;
}

/* Mutations aim at an existing object by id — the only place a
   misparse has teeth, so the only place that asks first. Everything
   else is a new object or disposable live state (a timer). */
const MUTATES = new Set(["toggle", "edit", "delete", "listSet", "listEdit", "listRemove", "timerCtl"]);

export function autoApplies(actions: { kind: string }[]): boolean {
  return actions.length > 0 && actions.every((a) => !MUTATES.has(a.kind));
}
