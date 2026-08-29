/* Search / Ask / Browse sheets and the reading view (wip/SPEC-convex-app.md).
   Search is native Convex over every source (stage 3, parity passed
   2026-08-29); Ask, Browse, and expand still go through the archive proxy
   until they're native too. */
import { useEffect, useState, type ReactNode } from "react";
import { useAction } from "convex/react";
import { api } from "../convex/_generated/api";
import { localDay } from "./Today";
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

function Sheet({ title, onBack, children }: { title: string; onBack: () => void; children: ReactNode }) {
  return (
    <section>
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ Today</button><span>{title}</span></div>
      {children}
    </section>
  );
}


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

  const toggle = (s: string) => setSel((old) => {
    const n = new Set(old);
    if (n.has(s)) n.delete(s); else n.add(s);
    return n;
  });
  const all = () => setSel(new Set(sel.size === SOURCES.length ? [] : SOURCES));

  const run = async () => {
    setBusy(true); const t0 = performance.now();
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
            style={sel.has(s) ? { color: color(s) } : undefined} onClick={() => toggle(s)}>{s}</button>
        ))}
      </div>
      {status && <p className="muted mono small">{status}</p>}
      {hits?.length === 0 && <p className="muted">nothing</p>}
      {hits?.map((r) => <Row key={r.id} c={r} right={r.score.toFixed(3)} onOpen={onOpen} />)}
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
      {res?.results?.map((r: any) => <Row key={r.id} c={r} right={r.timestamp ? r.timestamp.slice(11, 16) : ""} onOpen={onOpen} />)}
    </Sheet>
  );
}
