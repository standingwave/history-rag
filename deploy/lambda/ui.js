/* history UI (wip/SPEC-ui-rebuild.md + wip/design_handoff_history_ui).
   A static client over the MCP endpoint: search_history / list_window /
   expand / history_stats via JSON-RPC fetch, ask via POST /ask. All DOM
   is built from tool JSON with textContent — no markup ever comes from
   data; the CSP (script-src 'self', connect-src 'self') backstops. */
'use strict';

/* ── tokens shared with ui.html ── */
const SC = {claude:'#fb923c', shell:'#4ade80', browser:'#38bdf8',
  git:'#f87171', obsidian:'#a78bfa', calendar:'#f472b6',
  appusage:'#2dd4bf', digest:'#94a3b8'};
const SOURCES = ['browser','claude','shell','git','obsidian','calendar',
  'appusage'];
const ICONS = {
  claude:[['path',{d:'M21 11.5a8.4 8.4 0 0 1-9 8.4 8.6 8.6 0 0 1-3.7-.9L3 20l1-5.3A8.4 8.4 0 1 1 21 11.5z'}]],
  shell:[['polyline',{points:'4 17 10 11 4 5'}],['line',{x1:12,y1:19,x2:20,y2:19}]],
  browser:[['circle',{cx:12,cy:12,r:9}],['path',{d:'M3 12h18'}],['path',{d:'M12 3a13.5 13.5 0 0 1 0 18 13.5 13.5 0 0 1 0-18z'}]],
  git:[['line',{x1:6,y1:3,x2:6,y2:15}],['circle',{cx:18,cy:6,r:3}],['circle',{cx:6,cy:18,r:3}],['path',{d:'M18 9a9 9 0 0 1-9 9'}]],
  obsidian:[['path',{d:'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z'}],['polyline',{points:'14 2 14 8 20 8'}]],
  calendar:[['rect',{x:3,y:4,width:18,height:17,rx:2}],['line',{x1:16,y1:2,x2:16,y2:6}],['line',{x1:8,y1:2,x2:8,y2:6}],['line',{x1:3,y1:10,x2:21,y2:10}]],
  appusage:[['circle',{cx:12,cy:12,r:9}],['polyline',{points:'12 7 12 12 15 14'}]],
  digest:[['polygon',{points:'12 2 2 7 12 12 22 7 12 2'}],['polyline',{points:'2 17 12 22 22 17'}],['polyline',{points:'2 12 12 17 22 12'}]]};

/* ── DOM helpers ── */
function el(tag, props, ...kids){
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})){
    if (v == null) continue;
    if (k === 'class') n.className = v;
    else if (k === 'style') n.style.cssText = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'value') n.value = v;
    else if (k === 'disabled') n.disabled = !!v;
    else n.setAttribute(k, v);
  }
  append(n, kids);
  return n;
}
function append(n, kids){
  for (const kid of kids){
    if (kid == null || kid === false) continue;
    if (Array.isArray(kid)) append(n, kid);
    else n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
}
function icon(name){
  const parts = ICONS[name];
  if (!parts) return null;
  const NS = 'http://www.w3.org/2000/svg';
  const s = document.createElementNS(NS, 'svg');
  s.setAttribute('viewBox', '0 0 24 24');
  s.setAttribute('aria-hidden', 'true');
  s.setAttribute('class', 'ic');
  for (const [tag, attrs] of parts){
    const p = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) p.setAttribute(k, v);
    s.append(p);
  }
  return s;
}
function badge(source){
  const c = SC[source] || '#a6a6af';
  return el('span', {class:'badge',
    style:`background:${c}1f;color:${c}`}, icon(source), source);
}
function mono(node){ node.classList.add('mono'); return node; }

/* ── formatting ── */
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct',
  'Nov','Dec'];
const DAYS = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
function localDate(ts){
  if (!ts) return null;
  const d = new Date(ts);
  return isNaN(d.getTime()) ? null : d;
}
function fmtTs(ts){
  const d = localDate(ts);
  if (!d) return '';
  let out = `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  if (d.getFullYear() !== new Date().getFullYear()) out += `, ${d.getFullYear()}`;
  if (d.getHours() || d.getMinutes())
    out += ` · ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  return out;
}
function timeOnly(ts){
  const d = localDate(ts);
  if (!d || (!d.getHours() && !d.getMinutes())) return '';
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
}
function dayLabel(ts){
  const d = localDate(ts);
  if (!d) return 'undated';
  let out = `${DAYS[d.getDay()]} ${MONTHS[d.getMonth()]} ${d.getDate()}`;
  if (d.getFullYear() !== new Date().getFullYear()) out += `, ${d.getFullYear()}`;
  return out;
}
function localDay(ts){
  const d = localDate(ts);
  if (!d) return '';
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
function dur(seconds){
  const m = Math.floor((seconds || 0) / 60), h = Math.floor(m / 60);
  return h ? `${h}h ${m % 60}m` : `${m}m`;
}
function age(minutes){
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.floor(minutes/60)}h`;
  return `${Math.floor(minutes/1440)}d`;
}
function relAge(t){
  return age(Math.max(0, Math.floor((Date.now() - t) / 60000)));
}
function iso(daysAgo){
  const d = new Date();
  d.setDate(d.getDate() - (daysAgo || 0));
  return localDay(d.toISOString());
}
function fmtDate(isoStr){
  if (!isoStr) return '';
  const [y, m, d] = isoStr.split('-');
  return `${MONTHS[+m-1]} ${+d} ${y}`;
}
function flat(text, n){
  const s = (text || '').split('\n')[0].replace(/\s+/g, ' ').trim();
  return s.length > (n || 200) ? s.slice(0, n || 200) + '…' : s;
}

/* ── transport ── */
let rpcId = 0;
async function tool(name, args, signal){
  const r = await fetch('mcp', {method:'POST', signal,
    headers:{'content-type':'application/json',
             'accept':'application/json, text/event-stream'},
    body: JSON.stringify({jsonrpc:'2.0', id:++rpcId, method:'tools/call',
                          params:{name, arguments:args || {}}})});
  if (r.status === 404) throw new Error('auth failed — reopen via your /login link');
  if (!r.ok) throw new Error(`service error ${r.status}`);
  const env = await r.json();
  if (env.error) throw new Error(env.error.message || 'MCP error');
  const data = JSON.parse(env.result.content[0].text);
  if (data && data.error) throw new Error(data.error);
  return data;
}

/* ── state ── */
const S = {
  tab:'search',
  search:{q:'', source:'', preset:'any', since:'', until:'', ran:false,
          searching:false, results:[], open:{}, exact:false},
  ask:{q:'', model:'', phase:'idle', answer:null, citations:[], chunks:{},
       usage:null, note:'', error:null, thinRevealed:false, openCites:{},
       menu:false, elapsed:0, ctrl:null},
  browse:{preset:'6', since:'', until:'', source:'', view:'everything',
          offset:0, data:null, open:{}, loading:false, ran:false},
  stats:null, models:[], toast:''
};

let toastTimer = null;
function toast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 1900);
}

/* ── recents (device-local; queries never persist server-side) ── */
function recents(){
  try { return JSON.parse(localStorage.getItem('hist.recents') || '[]'); }
  catch { return []; }
}
function remember(q){
  const list = [{q, t:Date.now()},
                ...recents().filter(r => r.q !== q)].slice(0, 8);
  try { localStorage.setItem('hist.recents', JSON.stringify(list)); }
  catch {}
}

/* ── range idiom (shared by search + browse) ── */
function rangeDates(st){
  if (st.preset === 'any') return {since:'', until:''};
  if (st.preset === 'custom') return {since:st.since, until:st.until};
  return {since:iso(+st.preset), until:iso(0)};
}
function rangeConflict(st){
  return st.preset === 'custom' && !!(st.since && st.until
    && st.until < st.since);
}
function rangeText(st, verb){
  if (st.preset === 'any'){
    const total = S.stats ? ` — all ${S.stats.total_chunks.toLocaleString()} chunks` : '';
    return `${verb} any time${total}`;
  }
  if (st.preset === '0') return `${verb} today`;
  if (st.preset === '6') return `${verb} the last 7 days`;
  if (st.preset === '29') return `${verb} the last 30 days`;
  if (!st.since && !st.until) return 'no dates set — pick a range';
  if (st.since && st.until)
    return `${verb} ${fmtDate(st.since)} → ${fmtDate(st.until)}`;
  return st.since ? `${verb} since ${fmtDate(st.since)}`
                  : `${verb} up to ${fmtDate(st.until)}`;
}
function presetChips(st, labels, onchange){
  return el('div', {role:'group', 'aria-label':'Date range', class:'chips wrap'},
    labels.map(([v, label]) =>
      el('button', {type:'button', class:'chip',
        'aria-pressed':String(st.preset === v),
        onClick:() => {
          st.preset = v;
          if (v !== 'custom'){ st.since = ''; st.until = ''; }
          onchange();
        }}, label)));
}
function customDates(st, onchange){
  if (st.preset !== 'custom') return null;
  const date = (key, label) => el('input', {type:'date', 'aria-label':label,
    value:st[key], onChange:e => { st[key] = e.target.value; onchange(); }});
  return el('div', {class:'dates'},
    date('since', 'since'), el('span', {class:'arrow'}, '→'),
    date('until', 'until'));
}
function rangeLine(st, verb){
  if (rangeConflict(st))
    return el('div', {role:'alert', class:'conflict'},
      el('span', {class:'bang', 'aria-hidden':'true'}, '!'),
      'until is before since — no window to search');
  return el('div', {class:'rangesum mono'}, rangeText(st, verb));
}

/* ── source chips ── */
function sourceChips(st, onchange){
  const opts = [['','All'], ...SOURCES.map(s => [s, s])];
  return el('div', {role:'group', 'aria-label':'Sources', class:'chips'},
    opts.map(([v, label]) =>
      el('button', {type:'button', class:'chip',
        'aria-pressed':String(st.source === v),
        onClick:() => { st.source = v; onchange(); }},
        v ? icon(v) : null, label)));
}

/* ── index state: header dot + banner + partial strip ── */
function health(){ return (S.stats && S.stats.health) || null; }
function indexState(){
  const h = health();
  if (!S.stats) return {dot:'#94949e', text:'connecting…'};
  if (!h) return {dot:'#4ade80',
    text:`${S.stats.total_chunks.toLocaleString()} chunks`};
  const ref = h.age_minutes != null ? `refreshed ${age(h.age_minutes)} ago` : '';
  if (h.note) return {dot:'#fbbf24',
    text:`index ${h.status || 'stale'} · ${ref}`,
    banner:{head:h.note, body:'results may be missing recent activity — ' +
      'the index refreshes on the home machine, not here.'}};
  return {dot:'#4ade80', text:`index ${h.status || 'ok'} · ${ref}`};
}
function partialStrip(){
  const h = health();
  if (!h || !h.failing_sources) return null;
  const names = Object.keys(h.failing_sources).sort();
  const total = S.stats.sources ? Object.keys(S.stats.sources).length : 0;
  const detail = names.map(n => `${n} (${h.failing_sources[n]})`).join(', ');
  return el('div', {role:'status', class:'strip'},
    el('span', {class:'bang', 'aria-hidden':'true'}, '!'),
    el('span', {class:'txt'},
      `searched ${Math.max(0, total - names.length)} of ${total} sources — ` +
      `failing: ${detail}`));
}

/* ── ledger row ── */
function metaLine(r){
  const bits = [];
  const ts = fmtTs(r.timestamp);
  if (ts) bits.push(ts);
  if (r.location) bits.push(r.location);
  const m = r.meta || {};
  if (r.source === 'browser' && m.visit_count) bits.push(`${m.visit_count} visits`);
  if (r.source === 'shell'){
    if (m.count) bits.push(`${m.count} runs`);
    if (m.cwd) bits.push(m.cwd);
    if (m.exit) bits.push(`exit ${m.exit}`);
  }
  if (r.source === 'git' && m.sha) bits.push(String(m.sha).slice(0, 12));
  if (r.source === 'claude' && m.role) bits.push(m.role);
  if (r.source === 'calendar' && m.attendees)
    bits.push('with ' + m.attendees.slice(0, 8).join(', '));
  if (r.source === 'digest' && m.digest_of) bits.push(`rollup · ${m.digest_of}`);
  return bits.join(' · ');
}
function whyLine(r, extras){
  if (r.distance == null) return null;
  const bits = [`d=${r.distance.toFixed(2)} · lower is closer`];
  const m = r.meta || {};
  if (m.visit_count > 1) bits.push(`${m.visit_count} visits`);
  if (m.count > 1) bits.push(`${m.count} runs`);
  if (extras && extras.exact) bits.push('every chunk in range ranked');
  const sameDay = extras && extras.sameDay && extras.sameDay > 1
    ? `same day as ${extras.sameDay - 1} other hit${extras.sameDay > 2 ? 's' : ''}` : null;
  if (sameDay) bits.push(sameDay);
  return el('div', {class:'why mono'}, bits.join(' · '));
}
function isBand(r){
  return r.source === 'digest'
    || (r.source === 'appusage' && (r.meta || {}).first != null);
}
function highlight(text, q){
  const terms = (q || '').toLowerCase().split(/\s+/).filter(w => w.length > 3);
  if (!terms.length) return [text];
  const re = new RegExp('(' + terms.map(t =>
    t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'ig');
  return text.split(re).filter(s => s !== '').map(s =>
    terms.includes(s.toLowerCase()) ? el('mark', {}, s) : s);
}

function renderRow(r, tabState, opts){
  const o = opts || {};
  const open = tabState.open[r.id];
  const isMono = r.source === 'shell';
  const title = el('span', {class:'title' + (isMono ? ' mono' : ''),
    style:`-webkit-line-clamp:${isMono ? 1 : 2};` +
      `font-size:${isMono ? '.84rem' : '.94rem'}`},
    highlight(flat(r.text, 300), o.highlight || ''));
  const tm = isBand(r) ? '' : (o.timeOnly ? timeOnly(r.timestamp)
                                          : fmtTs(r.timestamp));
  const head = el('button', {type:'button', class:'row',
    'aria-expanded':String(!!open),
    onClick:() => toggleRow(r, tabState)},
    badge(r.source), title,
    el('span', {class:'tm mono'}, tm),
    el('span', {class:'chev', 'aria-hidden':'true'}, '▾'));
  const wrap = el('div', {class:'it' + (isBand(r) ? ' band' : '')}, head);
  if (open) wrap.append(renderDetail(r, tabState, open, o));
  return wrap;
}
async function toggleRow(r, tabState){
  if (tabState.open[r.id]){
    delete tabState.open[r.id];
    render();
    return;
  }
  tabState.open[r.id] = {loading:true, n:5};
  render();
  try {
    const data = await tool('expand', {id:r.id, context:5});
    Object.assign(tabState.open[r.id] || {},
      {loading:false, chunk:data.chunk, ctx:data.context,
       ctxSrc:data.context_source});
  } catch (e) {
    Object.assign(tabState.open[r.id] || {}, {loading:false, err:e.message});
  }
  render();
}
async function moreContext(r, tabState){
  const o = tabState.open[r.id];
  if (!o) return;
  o.loading = true; o.n = 25;
  render();
  try {
    const data = await tool('expand', {id:r.id, context:25});
    Object.assign(o, {loading:false, chunk:data.chunk, ctx:data.context,
                      ctxSrc:data.context_source});
  } catch (e) { Object.assign(o, {loading:false, err:e.message}); }
  render();
}
function renderDetail(r, tabState, o, opts){
  const chunk = o.chunk || r;
  const det = el('div', {class:'det'});
  const full = el('div', {class:'full' + (r.source === 'shell' ? ' mono' : '')},
    chunk.text || r.text || '');
  det.append(full);
  const sub = metaLine(chunk);
  if (sub) det.append(el('div', {class:'sub'}, sub));
  if (opts && opts.why) det.append(opts.why);
  if (o.loading) det.append(el('div', {class:'sub'}, 'loading context…'));
  else if (o.err) det.append(el('div', {class:'sub'}, `context failed: ${o.err}`));
  else if (o.ctx != null){
    const label = 'context' + (o.ctxSrc ? ` (${o.ctxSrc})` : '');
    const ctxNode = renderContext(chunk.source || r.source, o.ctx);
    if (ctxNode){
      det.append(el('div', {class:'ctxlabel'}, label), ctxNode);
    }
  }
  const acts = el('div', {class:'acts'});
  if (o.n !== 25 && o.ctx != null)
    acts.append(el('button', {type:'button', class:'textbtn',
      onClick:() => moreContext(r, tabState)}, 'full context'));
  const url = ((chunk.meta || r.meta || {}).url || '');
  if (/^https?:\/\//.test(url))
    acts.append(el('button', {type:'button', class:'textbtn',
      onClick:() => window.open(url, '_blank', 'noopener')}, 'open ↗'));
  acts.append(el('button', {type:'button', class:'textbtn',
    onClick:() => copyText(r.id, `copied id ${r.id}`)}, 'copy id'));
  det.append(acts);
  return det;
}
function copyText(text, note){
  if (navigator.clipboard && navigator.clipboard.writeText)
    navigator.clipboard.writeText(text).then(
      () => toast(note), () => toast(text));
  else toast(text);
}

/* ── context renderers, keyed by source ── */
function ctxTable(rows){
  return el('table', {class:'ctx'}, el('tbody', {}, rows.map(([k, v, target]) =>
    el('tr', {},
      el('td', {class:'k mono'}, k),
      el('td', {class:target ? 'mark' : ''}, v)))));
}
function renderContext(source, ctx){
  if (typeof ctx !== 'object' || ctx === null)
    return el('pre', {class:'ctxpre mono'}, String(ctx));
  if (ctx.note && Object.keys(ctx).length <= 2)
    return el('div', {class:'sub'}, ctx.note);
  try {
    if (source === 'claude' && ctx.turns)
      return el('div', {}, ctx.turns.map(t =>
        el('div', {class:'turn' + (t.role === 'user' ? ' user' : '')
                    + (t.target ? ' target' : '')},
          el('div', {class:'who'},
            [t.role || '?', fmtTs(t.timestamp)].filter(Boolean).join(' · ')),
          el('div', {class:'txt'}, t.text || ''))));
    if (source === 'browser' && ctx.visits)
      return ctxTable(ctx.visits.map(v =>
        [timeOnly(v.timestamp), flat(v.text, 160), v.target]));
    if (source === 'calendar' && ctx.agenda)
      return ctxTable(ctx.agenda.map(v =>
        [timeOnly(v.timestamp) || 'all day', flat(v.text, 160), v.target]));
    if (source === 'shell' && ctx.commands)
      return ctxTable(ctx.commands.map(c => {
        const extra = [c.cwd, c.exit ? `exit ${c.exit}` : '']
          .filter(Boolean).join(' · ');
        const cell = el('span', {},
          mono(el('span', {}, c.command || '')),
          extra ? el('span', {class:'sub', style:'display:block;margin:0'},
                     extra) : null);
        return [timeOnly(c.timestamp), cell, c.target];
      }));
    if (source === 'git' && ctx.show)
      return el('pre', {class:'ctxpre mono'}, ctx.show);
    if (source === 'obsidian'){
      if (ctx.note_text != null)
        return el('pre', {class:'ctxpre mono'}, ctx.note_text);
      if (ctx.sections)
        return el('div', {}, ctx.sections.map(s =>
          el('div', {class:'turn'},
            el('div', {class:'who'}, s.location || ''),
            el('div', {class:'txt'}, s.text || ''))));
    }
    if (source === 'appusage' && ctx.seconds_by_app)
      return ctxTable(Object.entries(ctx.seconds_by_app).map(([app, s]) =>
        [dur(s), app]));
    if (source === 'digest' && ctx.rollup)
      return renderRollup(ctx.rollup);
  } catch (e) { /* fall through to raw JSON */ }
  return el('pre', {class:'ctxpre mono'},
    JSON.stringify(ctx, null, 2));
}
function renderRollup(r){
  const out = el('div', {});
  const stats = [];
  if (r.visits) stats.push(`${r.visits} visits`);
  if (r.total_turns) stats.push(`${r.total_turns} turns`);
  if (r.runs) stats.push(`${r.runs} commands`);
  if (stats.length) out.append(el('div', {class:'sub'}, stats.join(' · ')));
  const table = (label, rows) => {
    if (!rows || !rows.length) return;
    out.append(el('div', {class:'ctxlabel'}, label), ctxTable(rows));
  };
  table('sites', Object.entries(r.domains || {}).map(([d, n]) => [n, d]));
  table('searches', (r.searches || []).map(s => ['', s]));
  table('pages', (r.top_titles || []).map(t => [t.visits || '', t.title || '']));
  table('sessions', (r.sessions || []).map(s => {
    const cell = el('span', {}, el('span', {style:'display:block'},
      s.project || ''), el('span', {class:'sub', style:'display:block;margin:0'},
      flat(s.first_prompt || '', 120)));
    return [s.turns || '', cell];
  }));
  table('runs by directory', Object.entries(r.by_cwd || {}).map(([c, n]) => [n, c]));
  table('top commands', (r.top_commands || []).map(c =>
    [`x${c.runs || ''}`, mono(el('span', {}, c.command || ''))]));
  if (!out.childNodes.length)
    return el('pre', {class:'ctxpre mono'}, JSON.stringify(r, null, 2));
  return out;
}

/* ── SEARCH ── */
async function runSearch(){
  const st = S.search;
  if (!st.q.trim()) return;
  if (rangeConflict(st)){ toast('fix the date range first'); return; }
  const {since, until} = rangeDates(st);
  st.searching = true;
  render();
  try {
    const args = {query:st.q.trim(), k:10};
    if (st.source) args.source = st.source;
    if (since) args.since = since;
    if (until) args.until = until;
    const data = await tool('search_history', args);
    st.results = data.results || [];
    st.exact = !!data.exact;
    st.ran = true;
    st.open = {};
    remember(st.q.trim());
  } catch (e) { toast(e.message); }
  st.searching = false;
  render();
}
function searchLink(){
  const st = S.search, params = new URLSearchParams();
  params.set('q', st.q);
  if (st.source) params.set('source', st.source);
  const {since, until} = rangeDates(st);
  if (since) params.set('since', since);
  if (until) params.set('until', until);
  return location.origin + location.pathname + '#' + params.toString();
}
function renderSearch(){
  const st = S.search;
  const rerun = () => { if (st.ran && st.q.trim()) runSearch(); else render(); };
  const panel = el('section', {role:'tabpanel', 'aria-label':'Search'});
  panel.append(
    el('form', {class:'frow', onSubmit:e => { e.preventDefault(); runSearch(); }},
      el('input', {type:'search', value:st.q, placeholder:'search history',
        autocomplete:'off', 'aria-label':'Search history',
        onInput:e => { st.q = e.target.value; }}),
      el('button', {type:'submit', class:'go'},
        st.searching ? 'Searching…' : 'Search')),
    sourceChips(st, rerun),
    presetChips(st, [['any','Any time'],['6','Last 7 days'],
                     ['29','Last 30 days'],['custom','Custom…']], rerun),
    customDates(st, rerun),
    rangeLine(st, 'searching'));
  if (st.ran){
    const sameDayCounts = {};
    for (const r of st.results){
      const d = localDay(r.timestamp);
      if (d) sameDayCounts[d] = (sameDayCounts[d] || 0) + 1;
    }
    panel.append(
      el('div', {class:'rhead mono'},
        el('span', {},
          `${st.results.length} result${st.results.length === 1 ? '' : 's'}` +
          ' · ranked by relevance'),
        el('button', {type:'button', class:'textbtn mono',
          onClick:() => copyText(searchLink(), 'link copied')}, 'copy link')),
      partialStrip());
    for (const r of st.results)
      panel.append(renderRow(r, st, {highlight:st.q,
        why:whyLine(r, {exact:st.exact,
                        sameDay:sameDayCounts[localDay(r.timestamp)]})}));
    if (!st.results.length){
      const c = el('div', {class:'centered'},
        el('div', {}, 'no matches for this filter'));
      if (st.source) c.append(el('button', {type:'button', class:'ghostbtn',
        onClick:() => { st.source = ''; runSearch(); }},
        'search all sources instead'));
      panel.append(c);
    }
  } else {
    const idle = el('div', {style:'margin-top:18px'});
    const rs = recents();
    if (rs.length){
      idle.append(el('div', {class:'recentlab'}, 'recent'));
      for (const r of rs)
        idle.append(el('button', {type:'button', class:'recent',
          onClick:() => { st.q = r.q; runSearch(); }},
          el('span', {}, r.q),
          el('span', {class:'when mono'}, relAge(r.t))));
    }
    if (S.stats){
      const src = S.stats.sources || {};
      const years = Object.values(src).map(i => (i.earliest || '').slice(0, 4))
        .filter(Boolean);
      const span = years.length ? ` · ${years.sort()[0]} → today` : '';
      idle.append(el('div', {class:'idlestats mono'},
        `${S.stats.total_chunks.toLocaleString()} chunks · ` +
        `${Object.keys(src).length} sources${span}`));
    }
    panel.append(idle);
  }
  return panel;
}

/* ── ASK ── */
function preset(name){
  return S.models.find(m => m.name === name) || S.models[0] || null;
}
async function runAsk(strict){
  const st = S.ask;
  if (!st.q.trim() || st.phase === 'running') return;
  clearInterval(st.timer);
  const ctrl = new AbortController();
  Object.assign(st, {phase:'running', error:null, answer:null, note:'',
    thinRevealed:false, openCites:{}, chunks:{}, elapsed:0, ctrl,
    t0:Date.now()});
  render();
  st.timer = setInterval(() => {
    const n = document.getElementById('elapsed');
    if (n) n.textContent = ((Date.now() - st.t0) / 1000).toFixed(1) + 's';
  }, 100);
  let result;
  try {
    const body = {q:st.q.trim()};
    const p = preset(st.model);
    if (p) body.model = p.name;
    if (strict) body.strict = true;
    const r = await fetch('ask', {method:'POST', signal:ctrl.signal,
      headers:{'content-type':'application/json'},
      body:JSON.stringify(body)});
    result = await r.json();
  } catch (e) {
    clearInterval(st.timer);
    if (e.name === 'AbortError'){
      st.phase = 'idle';
      toast('stopped — the answer was discarded');
    } else {
      st.phase = 'error';
      st.error = {head:'the request failed', body:e.message, retry:true};
    }
    render();
    return;
  }
  clearInterval(st.timer);
  st.elapsed = (Date.now() - st.t0) / 1000;
  if (result.error){
    st.phase = 'error';
    const configErr = /configured/.test(result.error);
    st.error = {head: configErr ? 'ask isn’t configured' : 'the model call failed',
                body: result.error, retry: !configErr};
    render();
    return;
  }
  Object.assign(st, {phase:'done', answer:result.answer || '',
    citations:result.citations || [], usage:result.usage || {},
    note:result.note || ''});
  render();
  /* type the citation chips: fetch each cited chunk (id-only expand) so
     the chip shows its source and the excerpt reveals in place */
  const ids = st.citations.slice(0, 12);
  await Promise.all(ids.map(async id => {
    try { st.chunks[id] = (await tool('expand', {id, context:0})).chunk; }
    catch {}
  }));
  render();
}
function stopAsk(){
  const st = S.ask;
  if (st.ctrl) st.ctrl.abort();
}
function citeChip(id, n){
  const st = S.ask;
  const chunk = st.chunks[id];
  const source = chunk ? chunk.source : null;
  const c = source ? (SC[source] || '#a6a6af') : '#a6a6af';
  return el('button', {type:'button', class:'cite',
    style:`background:${c}1f;color:${c}`,
    'aria-expanded':String(!!st.openCites[id]),
    onClick:() => { st.openCites[id] = !st.openCites[id]; render(); }},
    source ? icon(source) : null, `${source || 'source'} ${n}`);
}
function citeBox(id){
  const chunk = S.ask.chunks[id];
  return el('div', {class:'citebox'},
    el('div', {class:'chead'}, chunk
      ? [fmtTs(chunk.timestamp), chunk.location].filter(Boolean).join(' · ')
      : `chunk ${id}`),
    el('div', {class:'ctext'}, chunk ? flat(chunk.text, 400)
                                     : 'excerpt unavailable'));
}
function renderAnswer(){
  const st = S.ask;
  const order = st.citations;
  const num = id => order.indexOf(id) + 1;
  const card = el('div', {class:'answer'});
  for (const para of (st.answer || '').split(/\n{2,}/)){
    if (!para.trim()) continue;
    const p = el('p', {});
    const parts = para.split(/(\[id:[^\]\s]+\])/);
    for (const part of parts){
      const m = part.match(/^\[id:([^\]\s]+)\]$/);
      if (m && order.includes(m[1])) p.append(' ', citeChip(m[1], num(m[1])));
      else if (m) p.append('');
      else p.append(part);
    }
    card.append(p);
    for (const part of parts){
      const m = part.match(/^\[id:([^\]\s]+)\]$/);
      if (m && st.openCites[m[1]]) card.append(citeBox(m[1]));
    }
  }
  return card;
}
function askStatusLine(extra){
  const st = S.ask, u = st.usage || {};
  const p = preset(u.model || st.model);
  const bits = [u.model || '', `${u.turns || 0} turns`,
    `${(u.in || 0).toLocaleString()}+${(u.out || 0).toLocaleString()} tokens`,
    st.elapsed ? `${st.elapsed.toFixed(1)}s` : ''];
  if (p && p.est_cost) bits.push(`est ${p.est_cost}`);
  if (extra) bits.push(extra);
  return el('p', {class:'statusline mono'}, bits.filter(Boolean).join(' · '));
}
function renderAsk(){
  const st = S.ask;
  const p = preset(st.model);
  const panel = el('section', {role:'tabpanel', 'aria-label':'Ask'});
  const split = el('div', {class:'asksplit'});
  split.append(
    el('button', {type:'submit', class:'askmain',
      disabled:st.phase === 'running' || !S.models.length},
      'Ask', p && p.est_cost
        ? el('span', {class:'askcost mono'}, p.est_cost) : null));
  if (S.models.length){
    split.append(el('button', {type:'button', class:'askmodel',
      'aria-haspopup':'listbox', 'aria-expanded':String(!!st.menu),
      'aria-label':'Change model',
      onClick:e => { e.stopPropagation(); st.menu = !st.menu; render(); }},
      (p && p.name) || 'model',
      el('span', {class:'mchev', 'aria-hidden':'true'}, '▾')));
    if (st.menu)
      split.append(el('div', {role:'listbox', 'aria-label':'Model', class:'menu'},
        S.models.map(m => el('button', {type:'button', role:'option',
          class:'opt', 'aria-selected':String(m === p),
          onClick:() => { st.model = m.name; st.menu = false; render(); }},
          el('span', {class:'tick', 'aria-hidden':'true'}, m === p ? '✓' : ''),
          el('span', {class:'name'}, m.name),
          el('span', {class:'lat mono'},
            [m.latency, m.est_cost].filter(Boolean).join(' · '))))));
  }
  panel.append(
    el('form', {class:'frow', onSubmit:e => { e.preventDefault(); runAsk(); }},
      el('input', {type:'search', value:st.q, placeholder:'ask your history',
        autocomplete:'off', 'aria-label':'Ask your history',
        onInput:e => { st.q = e.target.value; }}),
      split),
    el('p', {class:'helper'},
      'the model works your history tools and cites what it reads — ' +
      'Search and Browse are free, ',
      el('b', {}, 'Ask bills your API key'),
      S.models.length ? '; the estimate follows the model you pick'
                      : ' — no models are configured on this deployment'));
  if (st.phase === 'running'){
    panel.append(el('div', {class:'runrow mono'},
      el('span', {class:'spin', 'aria-hidden':'true'}),
      el('span', {id:'elapsed'}, '0.0s'),
      el('span', {}, '· ' + ((p && p.name) || 'the model') +
        (p && p.latency ? ` usually takes ${p.latency}` : ' is working')),
      el('button', {type:'button', class:'stop', onClick:stopAsk}, 'Stop')));
  }
  if (st.phase === 'error' && st.error){
    panel.append(el('div', {role:'alert', class:'errcard'},
      el('div', {class:'ehead'}, st.error.head),
      el('div', {class:'ebody'}, st.error.body),
      st.error.retry ? el('button', {type:'button', class:'ghostbtn',
        onClick:() => runAsk()}, 'Retry') : null));
  }
  if (st.phase === 'done'){
    const thin = !st.citations.length && !!st.answer;
    if (thin && !st.thinRevealed){
      panel.append(el('div', {class:'thincard'},
        el('div', {class:'thead'}, 'answered without citing anything'),
        el('div', {class:'tbody'},
          'the model returned an answer but cited no history chunk, so ' +
          'nothing here is grounded in your data — treat it as a guess. ' +
          'This usually means the question was too general, or the range ' +
          'excluded everything.'),
        el('div', {class:'tacts'},
          el('button', {type:'button', class:'ghostbtn', style:'margin-top:0',
            onClick:() => runAsk(true)}, 'Ask again, sources required'),
          el('button', {type:'button', class:'plain',
            onClick:() => { st.thinRevealed = true; render(); }},
            'show it anyway'))),
        askStatusLine('0 citations'));
    } else {
      panel.append(renderAnswer());
      if (st.note) panel.append(el('p', {class:'statusline mono'}, st.note));
      panel.append(askStatusLine(thin ? '0 citations' : ''));
    }
  }
  return panel;
}

/* ── BROWSE ── */
async function runBrowse(offset){
  const st = S.browse;
  if (rangeConflict(st)){ st.data = null; render(); return; }
  const {since, until} = rangeDates(st);
  if (!since && !until) return;
  st.loading = true;
  st.offset = offset || 0;
  render();
  try {
    const args = {since, until, include_meta:true, limit:50};
    if (st.source) args.source = st.source;
    if (st.view === 'summaries') args.summaries = true;
    if (st.offset) args.offset = st.offset;
    const data = await tool('list_window', args);
    st.data = data;
    st.ran = true;
    st.open = {};
  } catch (e) { toast(e.message); }
  st.loading = false;
  render();
}
function renderBrowse(){
  const st = S.browse;
  const rerun = () => runBrowse(0);
  const panel = el('section', {role:'tabpanel', 'aria-label':'Browse'});
  panel.append(
    presetChips(st, [['0','Today'],['6','Last 7 days'],
                     ['29','Last 30 days'],['custom','Custom…']], rerun),
    customDates(st, rerun),
    rangeLine(st, 'showing'),
    sourceChips(st, rerun));
  if (rangeConflict(st)) return panel;
  if (!st.ran && !st.loading){
    panel.append(el('div', {class:'centered'},
      'pick a range — presets apply immediately'));
    return panel;
  }
  if (st.loading && !st.data){
    panel.append(el('div', {class:'centered'}, 'loading…'));
    return panel;
  }
  const data = st.data || {};
  const rows = data.results || [];
  panel.append(partialStrip());
  if (!rows.length){
    const src = (S.stats && S.stats.sources) || {};
    const years = Object.values(src).map(i => (i.earliest || '').slice(0, 4))
      .filter(Boolean).sort();
    const cover = years.length ? ` — the index covers ${years[0]} → today` : '';
    panel.append(el('div', {class:'centered'},
      el('div', {}, `no activity in ${rangeText(st, '').trim()}${cover}`),
      el('button', {type:'button', class:'ghostbtn',
        onClick:() => { st.preset = '29'; st.since = ''; st.until = '';
                        runBrowse(0); }},
        'widen to the last 30 days')));
    return panel;
  }
  const every = st.view === 'everything';
  const viewPill = () => el('button', {type:'button', class:'daypill',
    onClick:() => { st.view = every ? 'summaries' : 'everything';
                    runBrowse(0); }},
    every ? 'summaries only' : 'show everything');
  let day = null, count = 0;
  const dayCounts = {};
  for (const r of rows){
    const d = localDay(r.timestamp) || 'undated';
    dayCounts[d] = (dayCounts[d] || 0) + 1;
  }
  for (const r of rows){
    const d = localDay(r.timestamp) || 'undated';
    if (d !== day){
      day = d;
      count = dayCounts[d];
      panel.append(el('h2', {class:'day mono'},
        el('span', {}, dayLabel(r.timestamp)),
        el('span', {class:'side'},
          el('span', {class:'n'},
            `${count} item${count === 1 ? '' : 's'}`),
          viewPill())));
    }
    panel.append(renderRow(r, st, {timeOnly:true}));
  }
  const shown = rows.length, total = data.total || shown;
  const pager = el('p', {class:'pager mono'},
    `${st.offset + 1}–${st.offset + shown} of ${total}`);
  if (st.offset + shown < total)
    pager.append(' · ', el('button', {type:'button', class:'textbtn mono',
      onClick:() => runBrowse(st.offset + shown)}, 'older →'));
  if (st.offset > 0)
    pager.append(' · ', el('button', {type:'button', class:'textbtn mono',
      onClick:() => runBrowse(Math.max(0, st.offset - 50))}, '← newer'));
  panel.append(pager);
  return panel;
}

/* ── shell: header, tabs, panels ── */
const TABS = [['search','Search'],['ask','Ask'],['browse','Browse']];
function render(){
  const app = document.getElementById('app');
  app.textContent = '';
  const ix = indexState();
  app.append(el('header', {class:'top'},
    el('div', {class:'brand'}, 'history'),
    el('div', {class:'status'},
      el('span', {class:'dot', style:`background:${ix.dot}`}), ix.text)));
  if (ix.banner)
    app.append(el('div', {role:'status', class:'banner amber'},
      el('div', {class:'bhead'}, ix.banner.head),
      el('div', {class:'bbody'}, ix.banner.body)));
  app.append(el('nav', {role:'tablist', 'aria-label':'Views', class:'tabs',
    onKeydown:onTabKey},
    TABS.map(([id, label]) => el('button', {type:'button', role:'tab',
      class:'tab', 'aria-selected':String(S.tab === id),
      tabindex:S.tab === id ? '0' : '-1',
      onClick:() => switchTab(id)}, label))));
  if (S.tab === 'search') app.append(renderSearch());
  else if (S.tab === 'ask') app.append(renderAsk());
  else app.append(renderBrowse());
  app.append(el('footer', {},
    'queries stay on this device · ask runs only when you press Ask'));
}
function switchTab(id){
  if (S.tab === 'ask') S.ask.menu = false;
  S.tab = id;
  /* carry the search query into an empty Ask box — prefill, never run */
  if (id === 'ask' && !S.ask.q) S.ask.q = S.search.q;
  /* browse has no submit button: first visit loads the default range */
  if (id === 'browse' && !S.browse.ran && !S.browse.loading){
    runBrowse(0);
    return;
  }
  render();
}
function onTabKey(e){
  if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
  const order = TABS.map(t => t[0]);
  const i = order.indexOf(S.tab);
  const next = order[(i + (e.key === 'ArrowRight' ? 1 : order.length - 1))
                     % order.length];
  switchTab(next);
  const btn = document.querySelectorAll('.tab')[order.indexOf(next)];
  if (btn) btn.focus();
}
document.addEventListener('click', e => {
  if (S.ask.menu && !e.target.closest('.menu,[aria-haspopup="listbox"]')){
    S.ask.menu = false;
    render();
  }
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && S.ask.menu){ S.ask.menu = false; render(); }
});

/* ── boot: hash restore (legacy /search redirects land here), then data ── */
function applyHash(){
  const h = location.hash.replace(/^#/, '');
  if (!h) return null;
  const p = new URLSearchParams(h);
  const mode = p.get('mode') || (p.get('q') ? 'search'
    : (p.get('since') || p.get('until')) ? 'browse' : 'search');
  const setRange = st => {
    if (p.get('since') || p.get('until')){
      st.preset = 'custom';
      st.since = p.get('since') || '';
      st.until = p.get('until') || '';
    }
  };
  if (mode === 'ask'){
    S.tab = 'ask';
    S.ask.q = p.get('q') || '';
    if (p.get('model')) S.ask.model = p.get('model');
    return null;                       /* prefill only — ask never auto-runs */
  }
  if (mode === 'browse'){
    S.tab = 'browse';
    S.browse.source = p.get('source') || '';
    if (p.get('view') === 'summaries') S.browse.view = 'summaries';
    setRange(S.browse);
    return () => runBrowse(parseInt(p.get('offset'), 10) || 0);
  }
  S.tab = 'search';
  S.search.q = p.get('q') || '';
  S.search.source = p.get('source') || '';
  setRange(S.search);
  if (p.get('expand')) return () => openExpandLink(p.get('expand'));
  return S.search.q ? () => runSearch() : null;
}
async function openExpandLink(id){
  /* legacy expand bookmark: show that one chunk, opened, on Search */
  try {
    const data = await tool('expand', {id, context:5});
    const st = S.search;
    st.results = [data.chunk];
    st.ran = true;
    st.open = {[id]:{loading:false, chunk:data.chunk, ctx:data.context,
                     ctxSrc:data.context_source, n:5}};
    render();
  } catch (e) { toast(e.message); }
}
async function boot(){
  const run = applyHash();
  if (location.hash)
    history.replaceState(null, '', location.pathname);   /* keep URLs clean */
  render();
  fetch('config').then(r => r.json()).then(cfg => {
    S.models = cfg.models || [];
    if (S.models.length && !S.models.some(m => m.name === S.ask.model))
      S.ask.model = S.models[0].name;
    render();
  }).catch(() => {});
  tool('history_stats').then(stats => {
    S.stats = stats;
    render();
  }).catch(e => { toast(e.message); });
  if (run) run();
}
boot();
