/* Search / Ask / Browse sheets and the reading view (wip/SPEC-convex-app.md).
   Search is native Convex over every source (stage 3, parity passed
   2026-08-29); Ask, Browse, and expand still go through the archive proxy
   until they're native too. */
import { useEffect, useState, type ReactNode } from "react";
import { useAction, useConvex, useQuery } from "convex/react";
import { api } from "../convex/_generated/api";
import { localDay, useTick, useErrors, Skeleton } from "./Today";
import { describe, color, DetailBody, type Chunk } from "./render";

export const SOURCES = ["tasks", "obsidian", "calendar", "email", "browser",
  "claude", "git", "shell", "appusage", "digest"] as const;
export type Hit = {
  id: string; source: string; score: number; day: string; timestamp?: string;
  location?: string; text: string; meta?: any; live: boolean;
};

export function Src({ s }: { s: string }) {
  return <span className="mono src" style={{ color: color(s) }}>{s}</span>;
}

/* One list row for every source: title, then sub-line with the date and
   an optional right-hand figure (score). */
export function Row({ c, right, onOpen }: { c: Chunk; right?: string; onOpen: (id: string) => void }) {
  const d = describe(c);
  return (
    <div className="row" onClick={() => onOpen(c.id)}>
      <div className="r1"><Src s={c.source} /><span className="title">{d.title}</span></div>
      <div className="r2 muted small"><span className="clip">{d.sub}</span>
        <span className="mono">{[d.when, right].filter(Boolean).join(" · ")}</span></div>
    </div>
  );
}

/* A date input that says what it is when empty: label, calendar glyph, and
   the chosen date (or "any"); the native picker sits invisibly on top so a
   tap still opens it. */
export function DateField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="datef">
      <span className="dlab">{label}</span>
      <span className={`dval ${value ? "" : "muted"}`}>{value || "any"}</span>
      <span className="dglyph" aria-hidden>📅</span>
      <input type="date" value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

function kfmt(n: number) { return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n); }

/* history_stats as a strip: total, span, and a chip per source with its
   count; with `sel`/`onToggle` the chips double as the source selector. */
export function StatsStrip({ sel, onToggle }: { sel?: Set<string>; onToggle?: (s: string) => void }) {
  const st = useQuery(api.archive.stats, {});
  if (!st) return <div className="stats"><span className="skel" style={{ width: "55%", margin: "2px 0 8px" }} /><div className="chips">{[52, 60, 64, 56, 48].map((w, i) => <span key={i} className="chip skel" style={{ width: w, height: "1.6em", border: 0 }} />)}</div></div>;
  const lo = st.sources.reduce((m, r) => (r.earliestDay && (!m || r.earliestDay < m) ? r.earliestDay : m), "");
  const hi = st.sources.reduce((m, r) => (r.latestDay > m ? r.latestDay : m), "");
  return (
    <div className="stats">
      <p className="muted small mono">{kfmt(st.total)} chunks · {st.sources.length} sources · {lo.slice(0, 7)} → {hi}</p>
      <div className="chips">
        {st.sources.map((r) => {
          const on = sel ? sel.has(r.source) : true;
          return (
            <button key={r.source} type="button" className={`chip ${on ? "on" : ""}`}
              style={on ? { color: color(r.source) } : undefined}
              onClick={onToggle ? () => onToggle(r.source) : undefined}
              title={`${r.count} · ${r.earliestDay} → ${r.latestDay}`}>
              {r.source} <span className="cnt">{kfmt(r.count)}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function Sheet({ title, onBack, children, stats }:
  { title: string; onBack: () => void; children: ReactNode; stats?: ReactNode }) {
  return (
    <section>
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ Oriel</button><span>{title}</span></div>
      {stats ?? <StatsStrip />}
      {children}
    </section>
  );
}


/* ── reading view ─────────────────────────────────────────────────────── */

export function Detail({ id, onBack }: { id: string; onBack: () => void }) {
  const res = useQuery(api.archive.expand, { id, context: 5 });
  const c = res?.chunk;
  return (
    <Sheet title="Detail" onBack={onBack} stats={<></>}>
      {res === undefined && (<>
        <span className="skel" style={{ width: "75%", height: "1.3em", margin: "6px 0 14px" }} />
        <Skeleton widths={[40, 55]} style={{ marginBottom: 14 }} />
        <Skeleton widths={[96, 88, 92, 60]} />
      </>)}
      {res?.error && <p className="err">{res.error}</p>}
      {c && (
        <>
          <p className="muted mono small"><Src s={c.source} /> {c.timestamp ? `${c.timestamp.slice(0, 10)} ${c.timestamp.slice(11, 16)}` : ""}</p>
          <DetailBody chunk={c} context={res.context} contextSource={res.context_source}
            onOpen={(nid) => { history.pushState(null, "", `#w=x:${encodeURIComponent(nid)}`); dispatchEvent(new PopStateEvent("popstate")); }} />
        </>
      )}
    </Sheet>
  );
}

/* ── search ───────────────────────────────────────────────────────────── */

export function SearchSheet({ onBack, onOpen }: { onBack: () => void; onOpen: (id: string) => void }) {
  const live = useAction(api.search.search);
  const [q, setQ] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [k, setK] = useState(10);
  const [sel, setSel] = useState<Set<string>>(new Set(SOURCES));
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [t0, setT0] = useState(0);
  useTick(250, busy);
  const { push: fail, view: errView } = useErrors();

  const toggle = (s: string) => setSel((old) => {
    const n = new Set(old);
    if (n.has(s)) n.delete(s); else n.add(s);
    return n;
  });
  const all = () => setSel(new Set(sel.size === SOURCES.length ? [] : SOURCES));

  const run = async () => {
    setBusy(true); const t0 = performance.now(); setT0(t0); setStatus("");
    try {
      const r = await live({ query: q, sources: [...sel], limit: k,
                             since: since || undefined, until: until || undefined });
      const merged: Hit[] = r.results.map((x: any) => ({
        id: x.id, source: x.source, score: x.score, day: x.day, timestamp: x.timestamp,
        location: x.location, text: x.text, meta: x.meta, live: true }));
      setHits(merged);
      const ms = Math.round(performance.now() - t0);
      setStatus(`${merged.length} shown · ${r.candidates} candidates` +
        (r.dropped ? ` · ${r.dropped} dropped by window` : "") +
        ` · ${ms}ms (embed ${r.timing.embedMs} · search ${r.timing.searchMs} · join ${r.timing.joinMs})`);
    } catch (e) { fail(e); }
    finally { setBusy(false); }
  };
  const elapsed = busy ? (performance.now() - t0) / 1000 : 0;

  return (
    <Sheet title="Search" onBack={onBack} stats={<StatsStrip sel={sel} onToggle={toggle} />}>
      <form className="frow" onSubmit={(e) => { e.preventDefault(); void run(); }}>
        <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="anything you did, read, wrote, or planned" />
        <button className="primary" disabled={busy || !q}>go</button>
      </form>
      <div className="frow">
        <DateField label="from" value={since} onChange={setSince} />
        <DateField label="to" value={until} onChange={setUntil} />
        <input type="number" min={1} max={50} value={k} onChange={(e) => setK(Number(e.target.value) || 10)} aria-label="results" />
      </div>
      <p className="small"><button type="button" className="lnk" onClick={all}>{sel.size === SOURCES.length ? "none" : "all sources"}</button></p>
      {errView}
      {busy && <p className="muted mono small"><span className="pulse">searching {sel.size} source{sel.size === 1 ? "" : "s"}…</span>
        {elapsed >= 1.5 && <span> {elapsed.toFixed(1)} s · warming the embedding host</span>}</p>}
      {!busy && status && <p className="muted mono small">{status}</p>}
      {hits?.length === 0 && !busy && <p className="muted">nothing</p>}
      <div className={busy ? "stale" : ""}>
        {hits?.map((r) => <Row key={r.id} c={r} right={r.score.toFixed(3)} onOpen={onOpen} />)}
      </div>
    </Sheet>
  );
}

/* ── ask ──────────────────────────────────────────────────────────────── */

export function AskSheet({ onBack, onOpen }: { onBack: () => void; onOpen: (id: string) => void }) {
  const ask = useAction(api.ask.ask);
  const cfg = useQuery(api.archive.config, {});
  const models: any[] = cfg?.models ?? [];
  const [model, setModel] = useState("");
  const [q, setQ] = useState("");
  const [strict, setStrict] = useState(false);
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [t0, setT0] = useState(0);
  useTick(1000, busy);

  const run = async () => {
    setBusy(true); setRes(null); setT0(Date.now());
    try { setRes(await ask({ q, model: model || undefined, strict })); }
    catch (e) { setRes({ error: String(e) }); }
    finally { setBusy(false); }
  };
  const cur = models.find((m) => m.name === model) ?? models[0];
  const est = Number(/\d+/.exec(String(cur?.latency ?? ""))?.[0] ?? 0);   // "~10 s" → 10
  const secs = busy ? Math.round((Date.now() - t0) / 1000) : 0;

  const render = (text: string) => {
    const parts = text.split(/(\[id:[^\]\s]+\])/g);
    return parts.map((p, i) => {
      const m = /^\[id:([^\]\s]+)\]$/.exec(p);
      return m ? <span key={i} className="cite" onClick={() => onOpen(m[1])}>[{i}]</span> : <span key={i}>{p}</span>;
    });
  };

  return (
    <Sheet title="Ask" onBack={onBack}>
      <form className="frow" onSubmit={(e) => { e.preventDefault(); void run(); }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="a question about your history" />
        <button className="primary" disabled={busy || !q || !models.length}>ask</button>
      </form>
      <div className="frow">
        <select value={model} onChange={(e) => setModel(e.target.value)}>
          {models.map((m) => <option key={m.name} value={m.name}>{m.name}{m.latency ? ` · ${m.latency}` : ""}{m.est_cost ? ` · ${m.est_cost}` : ""}</option>)}
        </select>
        <label><input type="checkbox" checked={strict} onChange={(e) => setStrict(e.target.checked)} /> sources required</label>
      </div>
      {cfg && !models.length && <p className="muted small">ask isn't configured (ASK_MODELS)</p>}
      {busy && <p className="muted"><span className="pulse">thinking with {cur?.name}</span> · <span className="mono" style={est && secs > est ? { color: "#facc15" } : undefined}>{secs} s</span>{est ? <span className="small"> of ~{est}</span> : null}</p>}
      {res?.error && <p className="err">{res.error}</p>}
      {res?.answer && (
        <>
          <p className="answer">{render(res.answer)}</p>
          <p className="muted mono small">
            {res.usage?.model} · {res.usage?.turns} turns · {res.usage?.in}/{res.usage?.out} tokens
            {res.note ? ` · ${res.note}` : ""}
            {res.citations?.length === 0 ? " · no sources cited" : ""}
          </p>
        </>
      )}
    </Sheet>
  );
}

/* ── browse ───────────────────────────────────────────────────────────── */

export function BrowseSheet({ onBack, onOpen }: { onBack: () => void; onOpen: (id: string) => void }) {
  const convex = useConvex();
  const today = localDay();
  const [since, setSince] = useState(today);
  const [until, setUntil] = useState(today);
  const [source, setSource] = useState("");
  const [summaries, setSummaries] = useState(false);
  const [rows, setRows] = useState<any[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const LIMIT = 50;

  const run = async (append = false, win?: { since: string; until: string }) => {
    const s = win?.since ?? since, u = win?.until ?? until;
    setBusy(true); setErr("");
    try {
      const r = await convex.query(api.archive.window, {
        since: s || undefined, until: u || undefined, source: source || undefined,
        summaries, limit: LIMIT, cursor: append ? cursor : null });
      setRows((old) => (append && old ? [...old, ...r.results] : r.results));
      setCursor(r.cursor);
    } catch (e) { setErr(String(e)); }
    finally { setBusy(false); }
  };
  useEffect(() => { void run(); }, []);

  const shift = (days: number) => {
    const s = new Date(since + "T12:00"); s.setDate(s.getDate() + days);
    const u = new Date(until + "T12:00"); u.setDate(u.getDate() + days);
    setSince(localDay(s)); setUntil(localDay(u));
    void run(false, { since: localDay(s), until: localDay(u) });
  };
  const label = since === until ? since : `${since} – ${until}`;

  return (
    <Sheet title="Browse" onBack={onBack}>
      <form className="frow" onSubmit={(e) => { e.preventDefault(); void run(); }}>
        <DateField label="from" value={since} onChange={setSince} />
        <DateField label="to" value={until} onChange={setUntil} />
        <button className="primary" disabled={busy}>go</button>
      </form>
      <div className="frow">
        <button className="chip" type="button" onClick={() => shift(-1)}>‹ day</button>
        <button className="chip" type="button" onClick={() => shift(1)}>day ›</button>
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="">all sources</option>
          {SOURCES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <label><input type="checkbox" checked={summaries} onChange={(e) => setSummaries(e.target.checked)} /> summaries</label>
      </div>
      {err && <div className="errbox"><span>{err}</span><button className="lnk" onClick={() => setErr("")}>✕</button></div>}
      {busy && !rows && <Skeleton widths={[80, 62, 71, 55]} style={{ margin: "8px 0" }} />}
      {busy && rows && <p className="muted mono small pulse">loading {label}…</p>}
      {!busy && rows && <p className="muted mono small">{rows.length} shown{cursor ? " · more below" : rows.length ? " · end" : ""}</p>}
      {rows?.length === 0 && !busy && <p className="muted">nothing</p>}
      <div className={busy ? "stale" : ""}>
        {rows?.map((r: any) => <Row key={r.id} c={r} right={r.timestamp ? r.timestamp.slice(11, 16) : ""} onOpen={onOpen} />)}
      </div>
      {cursor && <p><button className={`chip ${busy ? "pulse" : ""}`} disabled={busy} onClick={() => void run(true)}>{busy ? "loading…" : "more ›"}</button></p>}
    </Sheet>
  );
}
