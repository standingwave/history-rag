/* Search / Ask / Browse sheets and the reading view (wip/SPEC-convex-app.md
   stage 1). Search merges the live Convex sources with the archive proxy;
   Ask, Browse, and expand are the archive's, through the proxy, until
   stage 3 makes them native. */
import { useEffect, useState, type ReactNode } from "react";
import { useAction } from "convex/react";
import { api } from "../convex/_generated/api";
import { localDay } from "./Today";

export const SOURCES = ["tasks", "obsidian", "calendar", "email", "browser",
  "claude", "git", "shell", "appusage", "digest"] as const;
const LIVE = new Set(["tasks", "obsidian", "calendar"]);
const COLOR: Record<string, string> = {
  tasks: "#facc15", obsidian: "#a78bfa", calendar: "#f472b6", email: "#60a5fa",
  browser: "#34d399", claude: "#fb923c", git: "#f87171", shell: "#a3e635",
  appusage: "#22d3ee", digest: "#e879f9",
};

export type Hit = {
  id: string; source: string; score: number; day: string; timestamp?: string;
  location?: string; text: string; meta?: any; live: boolean;
};

export function Src({ s }: { s: string }) {
  return <span className="mono time" style={{ color: COLOR[s] ?? "#a6a6af" }}>{s}</span>;
}

function Sheet({ title, onBack, children }: { title: string; onBack: () => void; children: ReactNode }) {
  return (
    <section>
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ Today</button><span>{title}</span></div>
      {children}
    </section>
  );
}

function firstLine(t: string) { return t.split("\n", 1)[0].slice(0, 140); }
function dayOf(ts?: string) { return ts ? localDay(new Date(ts)) : ""; }
/* The archive reports L2 distance; Convex reports cosine. Same unit
   vectors, so cos = 1 − d²/2 puts both on one scale. */
function distToScore(d: number) { return 1 - (d * d) / 2; }

/* ── reading view ─────────────────────────────────────────────────────── */

export function Detail({ id, onBack }: { id: string; onBack: () => void }) {
  const expand = useAction(api.archive.expand);
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    setRes(null); setErr("");
    expand({ id, context: 5 }).then(setRes).catch((e) => setErr(String(e)));
  }, [id]);
  const c = res?.chunk;
  return (
    <Sheet title="Detail" onBack={onBack}>
      {err && <p className="err">{err}</p>}
      {!res && !err && <p className="muted">…</p>}
      {res?.error && <p className="err">{res.error}</p>}
      {c && (
        <>
          <p className="muted mono small"><Src s={c.source} /> {c.timestamp?.slice(0, 16)} · {c.location}</p>
          <pre className="body">{c.text}</pre>
          {res.context && (
            <>
              <p className="sect">CONTEXT · {res.context_source ?? "index"}</p>
              <pre className="body">{typeof res.context === "string" ? res.context : JSON.stringify(res.context, null, 1)}</pre>
            </>
          )}
          {c.meta && Object.keys(c.meta).length > 0 && (
            <>
              <p className="sect">META</p>
              <pre className="body">{JSON.stringify(c.meta, null, 1)}</pre>
            </>
          )}
        </>
      )}
    </Sheet>
  );
}

/* ── search ───────────────────────────────────────────────────────────── */

export function SearchSheet({ onBack, onOpen }: { onBack: () => void; onOpen: (id: string) => void }) {
  const live = useAction(api.search.search);
  const archive = useAction(api.archive.search);
  const [q, setQ] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [k, setK] = useState(10);
  const [sel, setSel] = useState<Set<string>>(new Set(SOURCES));
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

  const toggle = (s: string) => setSel((old) => {
    const n = new Set(old);
    if (n.has(s)) n.delete(s); else n.add(s);
    return n;
  });
  const all = () => setSel(new Set(sel.size === SOURCES.length ? [] : SOURCES));

  const run = async () => {
    setBusy(true); const t0 = performance.now();
    const wantLive = [...sel].filter((s) => LIVE.has(s));
    const wantArchive = [...sel].filter((s) => !LIVE.has(s));
    const win = { since: since || undefined, until: until || undefined };
    try {
      const [l, a] = await Promise.all([
        wantLive.length ? live({ query: q, sources: wantLive, limit: k, ...win }) : null,
        wantArchive.length
          ? archive({ query: q, k: Math.max(k, 20), ...win,
                      source: wantArchive.length === 1 ? wantArchive[0] : undefined })
          : null,
      ]);
      const byId = new Map<string, Hit>();
      for (const r of l?.results ?? []) {
        byId.set(r.id, { id: r.id, source: r.source, score: r.score, day: r.day,
          timestamp: r.timestamp, location: r.location, text: r.text, meta: r.meta, live: true });
      }
      for (const r of a?.results ?? []) {
        if (byId.has(r.id) || !sel.has(r.source) || LIVE.has(r.source)) continue;
        byId.set(r.id, { id: r.id, source: r.source, score: distToScore(r.distance),
          day: dayOf(r.timestamp), timestamp: r.timestamp, location: r.location,
          text: r.text, meta: r.meta, live: false });
      }
      const merged = [...byId.values()].sort((p, s) => s.score - p.score).slice(0, k);
      setHits(merged);
      const ms = Math.round(performance.now() - t0);
      setStatus(`${merged.length} shown · live ${l?.results.length ?? 0}` +
        (l ? ` (${l.dropped} dropped by window)` : "") +
        ` · archive ${a?.results?.length ?? 0}${a?.truncated ? " (pool exhausted)" : ""} · ${ms}ms`);
    } catch (e) { alert(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <Sheet title="Search" onBack={onBack}>
      <form className="frow" onSubmit={(e) => { e.preventDefault(); void run(); }}>
        <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="anything you did, read, wrote, or planned" />
        <button className="primary" disabled={busy || !q}>go</button>
      </form>
      <div className="frow">
        <input type="date" value={since} onChange={(e) => setSince(e.target.value)} />
        <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
        <input type="number" min={1} max={50} value={k} onChange={(e) => setK(Number(e.target.value) || 10)} />
      </div>
      <div className="chips">
        <button className={`chip ${sel.size === SOURCES.length ? "on" : ""}`} onClick={all}>all</button>
        {SOURCES.map((s) => (
          <button key={s} className={`chip ${sel.has(s) ? "on" : ""}`}
            style={sel.has(s) ? { color: COLOR[s] } : undefined} onClick={() => toggle(s)}>{s}</button>
        ))}
      </div>
      {status && <p className="muted mono small">{status}</p>}
      {hits?.length === 0 && <p className="muted">nothing</p>}
      {hits?.map((r) => (
        <div key={r.id} className="lrow">
          <Src s={r.source} />
          <button className="rowbtn grow" onClick={() => onOpen(r.id)}>{firstLine(r.text)}</button>
          <span className="muted mono small">{r.day} · {r.score.toFixed(3)}</span>
        </div>
      ))}
    </Sheet>
  );
}

/* ── ask ──────────────────────────────────────────────────────────────── */

export function AskSheet({ onBack, onOpen }: { onBack: () => void; onOpen: (id: string) => void }) {
  const ask = useAction(api.archive.ask);
  const config = useAction(api.archive.config);
  const [models, setModels] = useState<any[]>([]);
  const [model, setModel] = useState("");
  const [q, setQ] = useState("");
  const [strict, setStrict] = useState(false);
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { config({}).then((c) => setModels(c.models ?? [])).catch(() => setModels([])); }, []);

  const run = async () => {
    setBusy(true); setRes(null);
    try { setRes(await ask({ q, model: model || undefined, strict })); }
    catch (e) { setRes({ error: String(e) }); }
    finally { setBusy(false); }
  };
  const cur = models.find((m) => m.name === model) ?? models[0];

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
      {!models.length && <p className="muted small">ask isn't configured on the archive</p>}
      {busy && <p className="muted">thinking with {cur?.name}…</p>}
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
  const list = useAction(api.archive.listWindow);
  const today = localDay();
  const [since, setSince] = useState(today);
  const [until, setUntil] = useState(today);
  const [source, setSource] = useState("");
  const [summaries, setSummaries] = useState(false);
  const [offset, setOffset] = useState(0);
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const LIMIT = 50;

  const run = async (off = 0) => {
    setBusy(true);
    try {
      setRes(await list({ since: since || undefined, until: until || undefined,
        source: source || undefined, summaries, limit: LIMIT, offset: off }));
      setOffset(off);
    } catch (e) { alert(String(e)); }
    finally { setBusy(false); }
  };
  useEffect(() => { void run(0); }, []);

  const shift = (days: number) => {
    const s = new Date(since + "T12:00"); s.setDate(s.getDate() + days);
    const u = new Date(until + "T12:00"); u.setDate(u.getDate() + days);
    setSince(localDay(s)); setUntil(localDay(u));
  };

  return (
    <Sheet title="Browse" onBack={onBack}>
      <form className="frow" onSubmit={(e) => { e.preventDefault(); void run(0); }}>
        <input type="date" value={since} onChange={(e) => setSince(e.target.value)} />
        <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
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
      {res?.error && <p className="err">{res.error}</p>}
      {res && !res.error && (
        <p className="muted mono small">
          {offset + 1}–{offset + res.count} of {res.total}
          {offset > 0 && <> · <button className="lnk" onClick={() => void run(Math.max(0, offset - LIMIT))}>‹ prev</button></>}
          {offset + res.count < res.total && <> · <button className="lnk" onClick={() => void run(offset + LIMIT)}>next ›</button></>}
        </p>
      )}
      {res?.results?.map((r: any) => (
        <div key={r.id} className="lrow">
          <span className="mono time">{r.timestamp ? r.timestamp.slice(11, 16) : "—"}</span>
          <Src s={r.source} />
          <button className="rowbtn grow" onClick={() => onOpen(r.id)}>{firstLine(r.text)}</button>
        </div>
      ))}
    </Sheet>
  );
}
