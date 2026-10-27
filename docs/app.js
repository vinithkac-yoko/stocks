import { createStore } from './store.js';

const CFG = Object.assign({ batchSize: 10 }, window.APP_CONFIG || {});
const BATCH = CFG.batchSize;
const REL_VOL_BAR_MAX = 5; // a bar is full at 5x the 20-day average
const MIN_SESSIONS_FOR_RELVOL = 5;

const S = {
  store: null, data: null, stockMap: new Map(),
  cycles: [], picks: [], refresh: null, ephemeral: false,
  tab: 'today', rank: 'relVol', q: '', investingId: null, sellingId: null,
};

// ------------------------------------------------------------- helpers ----

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const todayIST = () => new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
const inr = (n, d = 0) => '₹' + Number(n).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
const price = (n) => (n == null ? '—' : Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

function money(n) {
  const a = Math.abs(n), sign = n < 0 ? '−' : '';
  if (a >= 1e7) return `${sign}₹${(a / 1e7).toFixed(2)} Cr`;
  if (a >= 1e5) return `${sign}₹${(a / 1e5).toFixed(2)} L`;
  return `${sign}₹${Math.round(a).toLocaleString('en-IN')}`;
}
function qtyCompact(n) {
  if (n == null) return '—';
  if (n >= 1e7) return (n / 1e7).toFixed(2) + ' Cr';
  if (n >= 1e5) return (n / 1e5).toFixed(2) + ' L';
  return Math.round(n).toLocaleString('en-IN');
}
function fmtDate(iso, weekday = false) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString('en-IN', { weekday: weekday ? 'short' : undefined, day: 'numeric', month: 'short', year: 'numeric' });
}
// Direction is shown by arrow + sign as well as colour.
function delta(value, text) {
  if (value == null || Number.isNaN(value)) return '<span class="muted">—</span>';
  const cls = value > 0 ? 'up' : value < 0 ? 'down' : 'muted';
  const arrow = value > 0 ? '▲' : value < 0 ? '▼' : '';
  return `<span class="${cls} num">${arrow} ${text}</span>`;
}
// Compact date for tables: "27 Oct", plus the year only when it isn't this year.
function fmtShort(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  const opts = { day: 'numeric', month: 'short' };
  if (y !== new Date().getFullYear()) opts.year = '2-digit';
  return new Date(y, m - 1, d).toLocaleDateString('en-IN', opts);
}
const pct = (n) => `${Math.abs(n).toFixed(2)}%`;
const changePct = (s) => (s && s.prevClose ? ((s.close - s.prevClose) / s.prevClose) * 100 : null);

const soldQty = (p) => p.sells.reduce((a, s) => a + s.qty, 0);
const openQty = (p) => (p.status === 'invested' ? p.qty - soldQty(p) : 0);

let toastTimer;
function toast(msg) {
  const el = $('#toast');
  el.textContent = msg; el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

function applyTheme() {
  let t = 'auto';
  try { t = localStorage.getItem('theme') || 'auto'; } catch { /* storage blocked */ }
  if (t === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
}
function cycleTheme() {
  let t = 'auto';
  try { t = localStorage.getItem('theme') || 'auto'; } catch { /* storage blocked */ }
  const next = { auto: 'light', light: 'dark', dark: 'auto' }[t];
  try { localStorage.setItem('theme', next); } catch { /* storage blocked */ }
  applyTheme(); toast(`Theme: ${next}`);
}

// --------------------------------------------------------------- state ----

const currentCycle = () => S.cycles[S.cycles.length - 1];

function stats() {
  const cyc = currentCycle(), today = todayIST();
  const cyclePicks = S.picks.filter((p) => p.cycleId === cyc.id);
  const universe = S.data ? S.data.stocks.length : 0;
  const reviewed = cyclePicks.filter((p) => S.stockMap.has(p.symbol)).length;
  const todays = cyclePicks.filter((p) => p.pickedOn === today);
  const open = S.picks.filter((p) => openQty(p) > 0);

  let deployed = 0, costPriced = 0, unrealised = 0;
  for (const p of open) {
    const cost = p.entryPrice * openQty(p); deployed += cost;
    const s = S.stockMap.get(p.symbol);
    if (s) { costPriced += cost; unrealised += (s.close - p.entryPrice) * openQty(p); }
  }
  let realised = 0, sales = 0, wins = 0;
  for (const p of S.picks) for (const s of p.sells) { const pl = (s.price - p.entryPrice) * s.qty; realised += pl; sales++; if (pl > 0) wins++; }

  return {
    cyc, cyclePicks, universe, reviewed, remaining: Math.max(0, universe - reviewed),
    daysUsed: new Set(cyclePicks.map((p) => p.pickedOn)).size,
    cycleDays: Math.ceil(universe / BATCH) || 0,
    todays, todaysInvested: todays.filter((p) => p.status === 'invested').length,
    investedInCycle: cyclePicks.filter((p) => p.status === 'invested').length,
    open, deployed, unrealised, unrealisedPct: costPriced ? (unrealised / costPriced) * 100 : null,
    realised, sales, wins,
  };
}

function ranked(pickedSymbols) {
  const key = S.rank, q = S.q.trim().toLowerCase();
  const val = (s) => (s[key] == null ? -1 : s[key]);
  return S.data.stocks
    .filter((s) => !pickedSymbols.has(s.symbol))
    .filter((s) => !q || s.symbol.toLowerCase().includes(q) || s.name.toLowerCase().includes(q))
    .sort((a, b) => val(b) - val(a) || (b.turnoverCr ?? 0) - (a.turnoverCr ?? 0));
}

// Optimistic update: show the change at once, roll back if saving fails.
async function act(apply, persist, failMsg) {
  const snap = { picks: S.picks.map((p) => ({ ...p, sells: p.sells.map((s) => ({ ...s })) })), cycles: S.cycles.map((c) => ({ ...c })) };
  apply(); render();
  try { await persist(); } catch (e) {
    if (e.status === 401) return showLogin('Session expired. Enter your passcode.');
    S.picks = snap.picks; S.cycles = snap.cycles; render();
    toast(`${failMsg}: ${e.message}`);
  }
}

// ---------------------------------------------------------- rendering ----

function render() {
  if (!S.data) return;
  const st = stats();
  renderChrome();
  const views = { today: renderToday, portfolio: renderPortfolio, history: renderHistory };
  $('#app').innerHTML = views[S.tab](st);
}

function renderChrome() {
  const asOf = $('#asOf');
  asOf.hidden = false; asOf.textContent = `Close · ${fmtDate(S.data.asOf)}`;
  $('#signout').hidden = false;
  $('#tabs').hidden = false;
  for (const b of document.querySelectorAll('#tabs button')) {
    if (b.dataset.tab === S.tab) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  }
  const age = (Date.now() - new Date(S.data.asOf).getTime()) / 864e5;
  const msgs = [];
  if (S.ephemeral) msgs.push('<b>No volume attached.</b> Your picks and sales will be erased on the next deploy. Add a Railway volume (see README).');
  if (S.data.demo) msgs.push('<b>Demo data.</b> Made-up stocks and numbers, until the first NSE download succeeds.');
  else if (age > 5) msgs.push(`Data is from ${fmtDate(S.data.asOf)}; the daily update may be failing.`);
  if (S.refresh && S.refresh.error) msgs.push(`Last NSE update failed: ${esc(S.refresh.error)}`);
  const banner = $('#banner');
  banner.hidden = !msgs.length; banner.innerHTML = msgs.map((m) => `<div>${m}</div>`).join('');
}

function kpi(label, value, sub, extra = '') {
  return `<div class="kpi"><div class="label">${label}</div><div class="value num">${value}</div>${extra}<div class="sub">${sub}</div></div>`;
}

function renderKpis(st) {
  const progress = st.universe ? (st.reviewed / st.universe) * 100 : 0;
  const pnlLine = st.open.length && st.unrealisedPct != null
    ? delta(st.unrealised, `${money(Math.abs(st.unrealised))} (${pct(st.unrealisedPct)})`)
    : 'No open positions';
  return `<div class="kpis">
    ${kpi("Today's batch", `${st.todays.length} <small>/ ${BATCH}</small>`, `${st.todaysInvested} invested · ${st.todays.length - st.todaysInvested} to decide`)}
    ${kpi('Cycle progress', `${st.reviewed} <small>/ ${st.universe}</small>`,
      st.daysUsed ? `Day ${st.daysUsed} of ${st.cycleDays} · ${st.remaining} left` : `${st.cycleDays} days at ${BATCH} a day`,
      `<div class="track" role="progressbar" aria-label="Cycle progress" aria-valuenow="${st.reviewed}" aria-valuemin="0" aria-valuemax="${st.universe}"><i style="width:${progress.toFixed(1)}%"></i></div>`)}
    ${kpi('Invested this cycle', `${st.investedInCycle}`, st.reviewed ? `${Math.round((st.investedInCycle / st.reviewed) * 100)}% of ${st.reviewed} reviewed` : 'Nothing reviewed yet')}
    ${kpi('Open positions', `${st.open.length}`, `${st.open.length ? money(st.deployed) + ' deployed · ' : ''}${pnlLine}`)}
  </div>`;
}

function renderPortfolioKpis(st) {
  const total = st.unrealised + st.realised;
  const pnl = (v) => delta(v, money(Math.abs(v)));
  return `<div class="kpis">
    ${kpi('Open positions', `${st.open.length}`, `${money(st.deployed)} at cost`)}
    ${kpi('Unrealised P&amp;L', st.open.length ? pnl(st.unrealised) : '—', st.unrealisedPct != null ? `${delta(st.unrealised, pct(st.unrealisedPct))} on open positions` : 'Nothing open')}
    ${kpi('Realised P&amp;L', st.sales ? pnl(st.realised) : '—', st.sales ? `${st.wins} of ${st.sales} sales in profit` : 'No sales yet')}
    ${kpi('Total P&amp;L', st.open.length || st.sales ? pnl(total) : '—', 'Realised + unrealised, before charges')}
  </div>`;
}

function badge(p) {
  const open = openQty(p), sold = p.qty - open;
  if (open === 0) return '<span class="badge">Sold</span>';
  return `<span class="badge in">✓ ${open} × ${price(p.entryPrice)}${sold ? ` · ${sold} sold` : ''}</span>`;
}

function batchRow(p) {
  const s = S.stockMap.get(p.symbol), ch = changePct(s);
  const action = p.status === 'invested'
    ? `${badge(p)}<button class="btn quiet sm" data-action="invest" data-id="${p.id}">Edit</button>`
    : `<button class="btn primary sm" data-action="invest" data-id="${p.id}">Invest</button>`;
  return `<div class="batch-row">
    <div class="who"><span class="sym">${esc(p.symbol)}</span><span class="name">${esc(s ? s.name : '')}</span></div>
    <div class="price num">${s ? price(s.close) : '—'}<small>${delta(ch, ch == null ? '' : pct(ch))}</small></div>
    ${action}
    <button class="btn quiet sm" data-action="remove" data-id="${p.id}" aria-label="Remove ${esc(p.symbol)} from today's batch" title="Remove">✕</button>
  </div>`;
}

function poolRows(st) {
  const picked = new Set(st.cyclePicks.map((p) => p.symbol));
  const full = st.todays.length >= BATCH;
  const rows = ranked(picked);
  if (!rows.length) return `<tr><td colspan="8" class="empty">${S.q ? 'No match in the remaining list.' : 'Everything has been reviewed this cycle.'}</td></tr>`;
  return rows.map((s, i) => {
    const ch = changePct(s);
    const rv = s.relVol == null ? '<span class="muted">—</span>'
      : `<div class="rv"><span class="bar"><i style="width:${Math.min(s.relVol / REL_VOL_BAR_MAX, 1) * 100}%"></i></span><b class="num">${s.relVol.toFixed(2)}×</b></div>`;
    return `<tr>
      <td class="rank num">${i + 1}</td>
      <td><span class="sym">${esc(s.symbol)}</span><span class="name">${esc(s.name)}</span></td>
      <td class="r num">${price(s.close)}</td>
      <td class="r">${delta(ch, ch == null ? '' : pct(ch))}</td>
      <td class="r">${rv}</td>
      <td class="r num opt">${qtyCompact(s.volume)}</td>
      <td class="r num opt">${s.delivPct == null ? '—' : s.delivPct.toFixed(1) + '%'}</td>
      <td class="r"><button class="btn sm" data-action="add" data-sym="${esc(s.symbol)}" ${full ? 'disabled title="Today\'s batch is full"' : ''} aria-label="Add ${esc(s.symbol)} to today's batch">Add</button></td>
    </tr>`;
  }).join('');
}

function renderToday(st) {
  const lowHistory = S.data.sessionsUsed < MIN_SESSIONS_FOR_RELVOL;
  const batch = st.todays.length
    ? st.todays.map(batchRow).join('')
    : `<div class="empty">Tap <b>Add</b> on stocks in the ranked list to build today's batch of ${BATCH}.</div>`;
  const done = st.universe > 0 && st.remaining === 0;
  const seg = [['relVol', 'Rel. volume'], ['turnoverCr', 'Turnover'], ['volume', 'Shares']]
    .map(([k, l]) => `<button data-action="rank" data-rank="${k}" aria-pressed="${S.rank === k}">${l}</button>`).join('');
  return `${renderKpis(st)}
  ${done ? `<div class="panel" style="margin-bottom:20px"><div class="empty"><b>Cycle complete.</b> All ${st.universe} stocks reviewed.<br><br>
    <button class="btn primary" data-action="newcycle">Start a new cycle</button></div></div>` : ''}
  <div class="cols">
    <section class="panel" aria-label="Today's batch">
      <div class="panel-head"><h2>Today's batch</h2><span class="muted num">${st.todays.length} / ${BATCH}</span></div>
      ${batch}
    </section>
    <section class="panel" aria-label="Ranked list">
      <div class="panel-head">
        <h2>Ranked list</h2>
        <div class="seg" role="group" aria-label="Rank by">${seg}</div>
        <input class="search" type="search" id="search" placeholder="Search symbol or name" value="${esc(S.q)}" aria-label="Search">
      </div>
      ${lowHistory ? `<div class="pad muted" style="padding-bottom:0">Only ${S.data.sessionsUsed} earlier session(s) loaded, so relative volume is not available yet. Use Turnover for now.</div>` : ''}
      <div class="scroll"><table>
        <thead><tr><th>#</th><th>Stock</th><th class="r">Price</th><th class="r">Change</th>
          <th class="r" title="Today's volume ÷ 20-day average">Rel. volume</th><th class="r opt">Volume</th><th class="r opt" title="Delivery % of traded quantity">Deliv.</th><th></th></tr></thead>
        <tbody id="poolBody">${poolRows(st)}</tbody>
      </table></div>
    </section>
  </div>`;
}

function renderPortfolio(st) {
  const closed = S.picks.flatMap((p) => p.sells.map((s) => ({ p, s })))
    .sort((a, b) => b.s.soldOn.localeCompare(a.s.soldOn) || String(b.s.id).localeCompare(String(a.s.id), undefined, { numeric: true }));
  if (!st.open.length && !closed.length) {
    return `${renderPortfolioKpis(st)}<section class="panel"><div class="empty">No positions yet. Mark a stock as <b>Invested</b> from Today and it will appear here.</div></section>`;
  }

  let cost = 0, value = 0, unpriced = 0;
  const rows = [...st.open].sort((a, b) => (b.investedOn || '').localeCompare(a.investedOn || '') || String(b.id).localeCompare(String(a.id), undefined, { numeric: true }));
  const openBody = rows.map((p) => {
    const s = S.stockMap.get(p.symbol), q = openQty(p), held = p.entryPrice * q, sold = p.qty - q;
    cost += held;
    let pl = '<span class="muted">—</span>', plp = pl, val = '—';
    if (s) {
      const v = s.close * q; value += v; val = money(v);
      pl = delta(v - held, money(Math.abs(v - held)));
      plp = delta(v - held, pct(((s.close - p.entryPrice) / p.entryPrice) * 100));
    } else unpriced += held;
    return `<tr>
      <td><span class="sym">${esc(p.symbol)}</span><span class="name">${esc(s ? s.name : 'Not in latest data')}</span></td>
      <td class="opt num">${fmtShort(p.investedOn)}</td>
      <td class="r num">${q}${sold ? `<span class="name" style="margin-left:auto">of ${p.qty}</span>` : ''}</td>
      <td class="r num opt2">${price(p.entryPrice)}</td>
      <td class="r num opt2">${s ? price(s.close) : '—'}</td>
      <td class="r num opt">${money(held)}</td>
      <td class="r num opt">${val}</td>
      <td class="r">${pl}</td>
      <td class="r opt2">${plp}</td>
      <td class="r actions"><button class="btn sm" data-action="sell" data-id="${p.id}">Sell</button>
        <button class="btn quiet sm" data-action="invest" data-id="${p.id}">Edit</button>
        ${sold ? '' : `<button class="btn quiet sm" data-action="undo" data-id="${p.id}" title="Move back to 'to decide'">Undo</button>`}</td>
    </tr>`;
  }).join('');
  const priced = cost - unpriced, openPl = value - priced;
  const openPanel = st.open.length ? `<section class="panel" style="margin-bottom:20px"><div class="panel-head"><h2>Open positions</h2><span class="muted">Prices are the close of ${fmtDate(S.data.asOf)}</span></div>
  <div class="scroll"><table>
    <thead><tr><th>Stock</th><th class="opt">Bought</th><th class="r">Qty</th><th class="r opt2">Entry</th><th class="r opt2">Last</th>
      <th class="r opt">Cost</th><th class="r opt">Value</th><th class="r">P&amp;L</th><th class="r opt2">P&amp;L %</th><th></th></tr></thead>
    <tbody>${openBody}</tbody>
    <tfoot><tr><td>Total</td><td class="opt"></td><td></td><td class="opt2"></td><td class="opt2"></td>
      <td class="r num opt">${money(cost)}</td><td class="r num opt">${money(value)}</td>
      <td class="r">${delta(openPl, money(Math.abs(openPl)))}</td>
      <td class="r opt2">${priced ? delta(openPl, pct((openPl / priced) * 100)) : '—'}</td><td></td></tr></tfoot>
  </table></div></section>` : '';

  const closedBody = closed.map(({ p, s }) => {
    const pl = (s.price - p.entryPrice) * s.qty, plPct = ((s.price - p.entryPrice) / p.entryPrice) * 100;
    return `<tr>
      <td><span class="sym">${esc(p.symbol)}</span></td>
      <td class="num opt">${fmtShort(s.soldOn)}</td>
      <td class="r num">${s.qty}</td>
      <td class="r num opt">${price(p.entryPrice)}</td>
      <td class="r num">${price(s.price)}</td>
      <td class="r">${delta(pl, money(Math.abs(pl)))}</td>
      <td class="r opt2">${delta(pl, pct(plPct))}</td>
      <td class="r"><button class="btn quiet sm" data-action="undosale" data-id="${s.id}" title="Delete this sale">Undo</button></td>
    </tr>`;
  }).join('');
  const closedPanel = closed.length ? `<section class="panel"><div class="panel-head"><h2>Sales</h2><span class="muted">${st.wins} of ${st.sales} in profit</span></div>
  <div class="scroll"><table>
    <thead><tr><th>Stock</th><th class="opt">Sold</th><th class="r">Qty</th><th class="r opt">Entry</th><th class="r">Exit</th><th class="r">P&amp;L</th><th class="r opt2">P&amp;L %</th><th></th></tr></thead>
    <tbody>${closedBody}</tbody>
    <tfoot><tr><td>Realised</td><td class="opt"></td><td></td><td class="opt"></td><td></td><td class="r">${delta(st.realised, money(Math.abs(st.realised)))}</td><td class="opt2"></td><td></td></tr></tfoot>
  </table></div></section>` : '';
  return `${renderPortfolioKpis(st)}${openPanel}${closedPanel}`;
}

function renderHistory(st) {
  const groups = new Map();
  for (const p of st.cyclePicks) { if (!groups.has(p.pickedOn)) groups.set(p.pickedOn, []); groups.get(p.pickedOn).push(p); }
  const days = [...groups.keys()].sort().reverse().map((d) => {
    const ps = groups.get(d);
    const pills = ps.map((p) => `<span class="pill"><span class="sym">${esc(p.symbol)}</span>
      ${p.status === 'invested'
        ? `${badge(p)}<button class="btn quiet" data-action="invest" data-id="${p.id}">Edit</button>`
        : `<button class="btn" data-action="invest" data-id="${p.id}">Invest</button>`}
      <button class="btn quiet" data-action="remove" data-id="${p.id}" aria-label="Remove ${esc(p.symbol)}">✕</button></span>`).join('');
    return `<div class="day"><h3>${fmtDate(d, true)} · ${ps.length} picked · ${ps.filter((p) => p.status === 'invested').length} invested</h3><div class="items">${pills}</div></div>`;
  }).join('');
  const past = S.cycles.slice(0, -1).map((c) => {
    const ps = S.picks.filter((p) => p.cycleId === c.id);
    return `<div class="day"><h3>Cycle ${S.cycles.indexOf(c) + 1} · started ${fmtDate(c.startedOn)}</h3>
      <span class="muted">${ps.length} reviewed · ${ps.filter((p) => p.status === 'invested').length} invested</span></div>`;
  }).reverse().join('');
  return `${renderKpis(st)}
  <section class="panel" style="margin-bottom:20px"><div class="panel-head"><h2>Cycle ${S.cycles.length} · started ${fmtDate(st.cyc.startedOn)}</h2>
    <button class="btn" data-action="newcycle">Start a new cycle</button></div>
    ${days || '<div class="empty">No picks yet in this cycle.</div>'}</section>
  ${past ? `<section class="panel"><div class="panel-head"><h2>Earlier cycles</h2></div>${past}</section>` : ''}`;
}

function showLogin(error = '') {
  $('#tabs').hidden = true; $('#asOf').hidden = true; $('#signout').hidden = true; $('#banner').hidden = true;
  $('#app').innerHTML = `<div class="login"><h2>Enter passcode</h2>
    <form id="loginForm"><input type="password" name="pass" autocomplete="current-password" aria-label="Passcode" required autofocus>
    <p class="err" role="alert">${esc(error)}</p><button class="btn primary" type="submit">Unlock</button></form></div>`;
}

// -------------------------------------------------------------- dialogs ----

const findPick = (id) => S.picks.find((p) => p.id === id);

function openInvest(id) {
  const p = findPick(id);
  if (!p || p._pending) return;
  S.investingId = id;
  const s = S.stockMap.get(p.symbol), f = $('#investForm');
  $('#investTitle').textContent = p.status === 'invested' ? `Edit ${p.symbol}` : `Invest in ${p.symbol}`;
  $('#investSub').textContent = (s ? `${s.name} · last close ${inr(s.close, 2)}` : '') + (soldQty(p) ? ` · ${soldQty(p)} already sold` : '');
  f.price.value = p.entryPrice ?? (s ? s.close : '');
  f.qty.min = String(Math.max(1, soldQty(p)));
  f.qty.value = p.qty ?? '';
  f.date.value = p.investedOn ?? todayIST();
  updateInvestTotal();
  $('#investDialog').showModal();
  f.qty.focus();
}
function updateInvestTotal() {
  const f = $('#investForm'), t = Number(f.price.value) * Number(f.qty.value);
  $('#investTotal').textContent = t > 0 ? `Total: ${inr(t, 2)}` : '';
}

function openSell(id) {
  const p = findPick(id);
  if (!p || p._pending || openQty(p) <= 0) return;
  S.sellingId = id;
  const s = S.stockMap.get(p.symbol), f = $('#sellForm');
  $('#sellTitle').textContent = `Sell ${p.symbol}`;
  $('#sellSub').textContent = `${openQty(p)} held · bought at ${inr(p.entryPrice, 2)}${s ? ` · last close ${inr(s.close, 2)}` : ''}`;
  f.qty.max = String(openQty(p));
  f.qty.value = openQty(p);
  f.price.value = s ? s.close : '';
  f.date.value = todayIST();
  updateSellPreview();
  $('#sellDialog').showModal();
  f.price.focus();
}
function updateSellPreview() {
  const p = findPick(S.sellingId), f = $('#sellForm'), el = $('#sellPreview');
  const q = Number(f.qty.value), px = Number(f.price.value);
  if (!p || !(q > 0) || !(px > 0)) { el.textContent = ''; el.className = ''; return; }
  const pl = (px - p.entryPrice) * q, plPct = ((px - p.entryPrice) / p.entryPrice) * 100;
  const word = pl > 0 ? 'Profit' : pl < 0 ? 'Loss' : 'Break-even';
  el.className = pl > 0 ? 'up' : pl < 0 ? 'down' : 'muted';
  el.textContent = pl === 0 ? word : `${word} ${money(Math.abs(pl))} (${pl > 0 ? '+' : '−'}${pct(plPct)}) before charges`;
}

// -------------------------------------------------------------- events ----

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const { action, id, sym } = el.dataset, pid = id ? Number(id) : null;
  const pick = pid != null ? findPick(pid) : null;
  switch (action) {
    case 'tab': S.tab = el.dataset.tab; history.replaceState(null, '', '#' + S.tab); render(); break;
    case 'rank': S.rank = el.dataset.rank; render(); break;
    case 'theme': cycleTheme(); break;
    case 'close-dialog': el.closest('dialog').close(); break;
    case 'signout': S.store.logout().then(() => showLogin()); break;
    case 'invest': openInvest(pid); break;
    case 'sell': openSell(pid); break;
    case 'add': addPick(sym); break;
    case 'remove':
      if (!pick || pick._pending) break;
      if (soldQty(pick)) { toast('This stock has sales recorded. Undo them in Portfolio first.'); break; }
      if (pick.status === 'invested' && !confirm(`Remove ${pick.symbol}? It is marked as invested and will leave your portfolio.`)) break;
      act(() => { S.picks = S.picks.filter((p) => p.id !== pid); }, () => S.store.removePick(pid), 'Could not remove');
      break;
    case 'undo':
      if (!pick || pick._pending || !confirm(`Mark ${pick.symbol} as not invested? Its entry details are cleared.`)) break;
      act(() => Object.assign(pick, { status: 'picked', entryPrice: null, qty: null, investedOn: null }), () => S.store.clearInvested(pid), 'Could not update');
      break;
    case 'undosale': {
      const owner = S.picks.find((p) => p.sells.some((s) => s.id === pid));
      if (!owner || !confirm(`Delete this ${owner.symbol} sale? The shares return to your open position.`)) break;
      act(() => { owner.sells = owner.sells.filter((s) => s.id !== pid); }, () => S.store.removeSell(pid), 'Could not undo sale');
      break;
    }
    case 'newcycle': {
      const st = stats();
      if (st.remaining > 0 && !confirm(`Only ${st.reviewed} of ${st.universe} reviewed. Start a new cycle anyway? Your portfolio is kept.`)) break;
      act(() => S.cycles.push({ id: -1, startedOn: todayIST() }), async () => { const c = await S.store.startCycle(); currentCycle().id = c.id; }, 'Could not start cycle');
      break;
    }
  }
});

function addPick(symbol) {
  const st = stats();
  if (st.todays.length >= BATCH) return toast(`Today's batch is full (${BATCH}).`);
  if (st.cyclePicks.some((p) => p.symbol === symbol)) return;
  const pick = { id: `tmp${Math.random()}`, cycleId: st.cyc.id, symbol, pickedOn: todayIST(), status: 'picked', entryPrice: null, qty: null, investedOn: null, sells: [], _pending: true };
  act(() => S.picks.push(pick), async () => { pick.id = await S.store.addPick(pick.cycleId, symbol, pick.pickedOn); delete pick._pending; render(); }, 'Could not add');
}

document.addEventListener('input', (e) => {
  if (e.target.id === 'search') {
    S.q = e.target.value;
    $('#poolBody').innerHTML = poolRows(stats());
  } else if (e.target.closest('#investForm')) updateInvestTotal();
  else if (e.target.closest('#sellForm')) updateSellPreview();
});

document.addEventListener('submit', async (e) => {
  const form = e.target;
  if (form.id === 'investForm') {
    e.preventDefault();
    const entryPrice = Number(form.price.value), qty = Math.floor(Number(form.qty.value)), investedOn = form.date.value;
    const pick = findPick(S.investingId);
    if (!pick) return;
    if (!(entryPrice > 0) || !(qty > 0) || !investedOn) return toast('Enter a price, quantity and date.');
    if (qty < soldQty(pick)) return toast(`Quantity can't be below the ${soldQty(pick)} already sold.`);
    $('#investDialog').close();
    act(() => Object.assign(pick, { status: 'invested', entryPrice, qty, investedOn }), () => S.store.setInvested(pick.id, { entryPrice, qty, investedOn }), 'Could not save');
  } else if (form.id === 'sellForm') {
    e.preventDefault();
    const qty = Math.floor(Number(form.qty.value)), px = Number(form.price.value), soldOn = form.date.value;
    const pick = findPick(S.sellingId);
    if (!pick) return;
    if (!(qty > 0) || !(px > 0) || !soldOn) return toast('Enter a quantity, price and date.');
    if (qty > openQty(pick)) return toast(`You only hold ${openQty(pick)}.`);
    $('#sellDialog').close();
    const sale = { id: `tmp${Math.random()}`, qty, price: px, soldOn, _pending: true };
    act(() => pick.sells.push(sale), async () => { sale.id = await S.store.addSell(pick.id, { qty, price: px, soldOn }); delete sale._pending; render(); }, 'Could not record sale');
  } else if (form.id === 'loginForm') {
    e.preventDefault();
    try { await S.store.login(form.pass.value); await start(); } catch (err) { showLogin(err.message); }
  }
});

// ---------------------------------------------------------------- boot ----

async function loadData() {
  try {
    const res = await fetch('data/latest.json', { cache: 'no-cache' });
    if (!res.ok) throw new Error(res.status);
    S.data = await res.json();
    S.stockMap = new Map(S.data.stocks.map((s) => [s.symbol, s]));
    if (S.data.sessionsUsed < MIN_SESSIONS_FOR_RELVOL) S.rank = 'turnoverCr';
  } catch {
    $('#app').innerHTML = '<p class="pad">No market data yet. It downloads automatically after the NSE close; check the service logs if this persists.</p>';
    return false;
  }
  return true;
}

async function start() {
  const state = await S.store.load();
  S.cycles = state.cycles; S.picks = state.picks; S.refresh = state.refresh; S.ephemeral = state.ephemeral;
  render();
}

async function init() {
  applyTheme();
  const hash = location.hash.slice(1);
  if (['today', 'portfolio', 'history'].includes(hash)) S.tab = hash;
  try {
    S.store = createStore();
    if (!(await loadData())) return;
    if (await S.store.needsLogin()) return showLogin();
    await start();
  } catch (e) {
    $('#app').innerHTML = `<p class="pad">Could not start: ${esc(e.message)}</p>`;
  }
}
init();
