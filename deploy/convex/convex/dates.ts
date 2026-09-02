/* `since`/`until` reach the day filters from models and connectors as
   either local days or full ISO timestamps. The filters compare strings
   against YYYY-MM-DD, and a timestamp sorts after its own day — so an
   ISO `since` silently drops that whole day. Truncate anything date-led
   to the bare day; other strings pass through untouched. */
export function dayArg(s?: string): string | undefined {
  return s && /^\d{4}-\d{2}-\d{2}/.test(s) ? s.slice(0, 10) : s;
}
