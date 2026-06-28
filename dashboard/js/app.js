/* ============================================================================
   GRIDBOT · Panel de Futuros — lógica de la aplicación
   Vanilla JS. Auth JWT, capa de lectura de la API, router de pantallas y gráficos
   con Lightweight Charts. Refresco: polling frugal como base; el WebSocket solo
   actúa como señal de cambio cuando hay contraseña en memoria (no tras recargar
   con token), nunca como transporte de datos.
   ========================================================================== */
(() => {
'use strict';

const TOKEN_KEY = 'gridbot_token';
const POLL_FAST = 5000;   // sin WS (el bot escribe estado cada ~10s → 5s sobra)
const POLL_SLOW = 12000;  // con WS (latido de seguridad)

const S = {
  token: localStorage.getItem(TOKEN_KEY),
  pw: null,                 // contraseña en memoria (solo para auth del WS)
  panel: 'resumen',
  overview: null,           // {state, risk, bot, stats}
  trades: [],
  cfg: null,
  opFilter: 'all', opSearch: '',
  logFilter: 'all', logSearch: '',
  tf: '60',
  ws: null, wsAlive: false, pollTimer: null, refreshT: null,
  charts: {},
};

/* ── DOM helpers ─────────────────────────────────────────── */
const $  = (id) => document.getElementById(id);
const txt = (id, v) => { const e = $(id); if (e) e.textContent = v; };
const show = (id, on) => { const e = $(id); if (e) e.hidden = !on; };

/* ── Formatters (tabular-nums vía CSS) ───────────────────── */
const MINUS = '−';
const nf = (dp) => new Intl.NumberFormat('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp });
function money(v, dp = 2) {
  if (v == null || isNaN(v)) return '—';
  const s = nf(dp).format(Math.abs(v));
  return (v < 0 ? MINUS + '$' : '$') + s;
}
function signedMoney(v, dp = 2) {
  if (v == null || isNaN(v) || v === 0) return '—';
  const s = nf(dp).format(Math.abs(v));
  return (v < 0 ? MINUS + '$' : '+$') + s;
}
function pct(v, dp = 2) { return (v == null || isNaN(v)) ? '—' : nf(dp).format(v) + '%'; }
function signedPct(v, dp = 1) {
  if (v == null || isNaN(v)) return '—';
  return (v < 0 ? MINUS : '+') + nf(dp).format(Math.abs(v)) + '%';
}
function num(v, dp = 2) { return (v == null || isNaN(v)) ? '—' : nf(dp).format(v); }
function pnlClass(v) { return v > 0 ? 'pos' : (v < 0 ? 'neg' : 'neu'); }
function pfLabel(s) {
  if (!s) return '—';
  if (s.profit_factor > 0) return num(s.profit_factor, 2);
  if ((s.total_closed_trades || 0) > 0 && (s.gross_loss || 0) === 0) return '∞';  // racha 100% ganadora
  return '—';                                                                      // sin cierres
}

function regimeKind(r) {
  const k = String(r || '').toLowerCase();
  if (k.includes('up') || k === 'trending_up') return 'up';
  if (k.includes('down')) return 'down';
  if (k.includes('rang')) return 'range';
  if (k.includes('transition')) return 'trans';
  return 'flat';
}
const REG_LABEL = { up: 'Tendencia ↑', down: 'Tendencia ↓', range: 'Rango', trans: 'Transicional', flat: '—' };
const REG_COLOR = { up: '#0f7a52', down: '#c8453a', range: '#c98a2b', trans: '#9a9384', flat: '#d8d2c4' };
const regimeLabel = (r) => REG_LABEL[regimeKind(r)];
const regimeColor = (r) => REG_COLOR[regimeKind(r)];
const MODE_LABEL = { trend: 'Tendencia', grid: 'Grid', flat: 'Plano' };
const modeLabel = (m) => MODE_LABEL[m] || (m || '—');
const REASON_LABEL = {
  chandelier_stop: 'Stop Chandelier',
  trend_reversal: 'Cambio de tendencia',
  htf_conflict: 'Conflicto TF superior',
  range_entry: 'Reasignado a rango',
  grid_atr_stop: 'Stop ATR de grid',
  kill_switch: 'Kill-switch',
  transitional: 'Zona transicional',
  manual_flatten: 'Cierre manual',
  grid_entry: 'Grid · entrada',
  grid_tp: 'Grid · toma de beneficio',
  trend_entry: 'Entrada tendencia',
};
function reasonLabel(code, side) {
  if (!code) return '—';
  const base = REASON_LABEL[code] || code;   // fallback: muestra el código crudo
  if (code === 'trend_entry') return base + (side === 'Buy' ? ' ↑' : side === 'Sell' ? ' ↓' : '');
  return base;
}

function timeHM(iso) {
  const d = parseTs(iso); if (!d) return '—';
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}
const MON = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];
function dateHM(iso) {
  const d = parseTs(iso); if (!d) return '—';
  return d.getDate() + ' ' + MON[d.getMonth()] + ' · ' + timeHM(iso);
}
function relTime(iso) {
  const d = parseTs(iso); if (!d) return '—';
  let s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}
function parseTs(iso) {
  if (!iso) return null;
  // Acepta ISO; si no trae zona, el backend escribe UTC → trátalo como UTC.
  let s = String(iso);
  if (!/[zZ]|[+\-]\d\d:?\d\d$/.test(s) && s.includes('T')) s += 'Z';
  const d = new Date(s.includes('T') ? s : s.replace(' ', 'T') + 'Z');
  return isNaN(d.getTime()) ? null : d;
}
const unix = (iso) => { const d = parseTs(iso); return d ? Math.floor(d.getTime() / 1000) : null; };

/* ── API ─────────────────────────────────────────────────── */
async function api(path, opts = {}) {
  const res = await fetch('/api' + path, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      ...(S.token ? { Authorization: 'Bearer ' + S.token } : {}),
      ...(opts.headers || {}),
    },
  });
  if (res.status === 401) { handle401(); throw new Error('401'); }
  if (!res.ok) throw new Error(path + ' → ' + res.status);
  return res.json();
}

/* ── Auth ────────────────────────────────────────────────── */
async function doLogin() {
  const u = $('login-user').value.trim();
  const p = $('login-pass').value;
  $('login-error').textContent = '';
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: u, password: p }),
    });
    if (!res.ok) {
      $('login-error').textContent = res.status === 401 ? 'Usuario o contraseña incorrectos.' : 'Error de acceso (' + res.status + ').';
      return;
    }
    const data = await res.json();
    S.token = data.access_token || data.token;
    S.pw = p;
    localStorage.setItem(TOKEN_KEY, S.token);
    startApp();
  } catch (e) {
    $('login-error').textContent = 'No se pudo conectar con el servidor.';
  }
}
function handle401() {
  S.token = null; localStorage.removeItem(TOKEN_KEY);
  stopData();
  show('app', false); show('login-overlay', true);
}
function logout() {
  S.token = null; S.pw = null; localStorage.removeItem(TOKEN_KEY);
  stopData();
  S.panel = 'resumen';
  show('app', false); show('login-overlay', true);
}

/* ── Arranque / parada del ciclo de datos ────────────────── */
let TICK = 0;
function startApp() {
  show('login-overlay', false); show('app', true);
  setPanel(S.panel);
  tick();
  connectWS();
  startPolling();
}
const cfgPct = (k) => (S.cfg && S.cfg[k] != null) ? S.cfg[k] * 100 : undefined;
function stopData() {
  if (S.pollTimer) clearInterval(S.pollTimer);
  S.pollTimer = null;
  if (S.ws) { try { S.ws.close(); } catch (e) {} S.ws = null; }
  S.wsAlive = false;
}
function startPolling() {
  if (S.pollTimer) clearInterval(S.pollTimer);
  S.pollTimer = setInterval(tick, S.wsAlive ? POLL_SLOW : POLL_FAST);
}

/* ── WebSocket: señal de cambio (no dependemos de su payload) ─ */
function connectWS() {
  if (!S.pw) return;  // sin contraseña en memoria (recarga con token) → solo polling
  try {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const user = encodeURIComponent($('login-user').value.trim() || 'admin');
    const ws = new WebSocket(`${proto}://${location.host}/ws?username=${user}`);
    S.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: 'auth', password: S.pw }));
    ws.onmessage = () => {           // cualquier mensaje = "algo cambió" → refresco la forma correcta
      if (!S.wsAlive) { S.wsAlive = true; startPolling(); }
      scheduleRefresh();
    };
    ws.onclose = () => { S.wsAlive = false; S.ws = null; startPolling(); setTimeout(connectWS, 8000); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  } catch (e) { /* polling cubre el caso */ }
}
function scheduleRefresh() {
  if (S.refreshT) return;
  S.refreshT = setTimeout(() => { S.refreshT = null; tick(); }, 600);
}

/* ── Ciclo de datos (frugal: ≤60 req/min del rate limiter) ──
   Cada tick (3s con WS caído, 12s con WS): SIEMPRE overview; trades cada ~3
   ticks; latencia cada ~4; config se reintenta hasta cargar. Las cargas
   pesadas (equity, circuit-breakers) van por entrada de pantalla + throttle. */
async function tick() {
  if (!S.token) return;
  TICK++;
  try {
    const wantTrades = (TICK % 3 === 1);
    const reqs = [api('/futures/overview')];
    if (wantTrades) reqs.push(api('/futures/trades?limit=100'));
    const res = await Promise.all(reqs);
    S.overview = res[0];
    if (wantTrades && res[1]) S.trades = res[1].trades || [];
    renderShell();
    renderPanel(S.panel);
  } catch (e) { /* 401 ya gestionado; 429/otros: reintenta en el siguiente tick */ }
  if (!S.cfg) loadConfig();                 // reintenta hasta éxito (config es estática)
}

/* ── Render: shell (sidebar + topbar, siempre) ───────────── */
function renderShell() {
  const ov = S.overview; if (!ov) return;
  const st = ov.state || {}, risk = ov.risk || {}, bot = ov.bot || {};
  const fresh = st.updated_at && (Date.now() - (parseTs(st.updated_at)?.getTime() || 0)) < 45000;

  // Estado del bot: bot.status es la fuente PRIMARIA (running|paused|stopped, mantenido
  // por update_bot_state); la frescura de updated_at solo detecta "sin contacto"
  // (status=running pero sin updates recientes → posible caída del proceso).
  const status = String(bot.status || '').toLowerCase();
  let botLabel, botDot;
  if (status === 'paused' || risk.halted) { botLabel = 'Pausado · kill-switch'; botDot = 'dot warn'; }
  else if (status === 'stopped') { botLabel = 'Detenido'; botDot = 'dot off'; }
  else if (status === 'running' && !fresh) { botLabel = 'Sin contacto'; botDot = 'dot warn'; }
  else if (status === 'running') { botLabel = 'Bot en marcha'; botDot = 'dot'; }
  else { botLabel = fresh ? 'Bot en marcha' : 'Sin datos'; botDot = fresh ? 'dot' : 'dot off'; }

  // sidebar
  $('side-dot').className = botDot;
  txt('side-bot-status', botLabel);
  txt('side-bot-detail', `MODO ${(modeLabel(st.mode) || '—').toUpperCase()} · ${st.leverage ? st.leverage + 'x' : '—'}`);

  // topbar
  txt('top-symbol', (st.symbol || '—') + (st.symbol ? ' · PERP' : ''));
  txt('top-regime', regimeLabel(st.regime));
  txt('top-mode', modeLabel(st.mode));
  txt('top-latency', ov.latency_ms ? Math.round(ov.latency_ms) + ' ms' : '—');

  const halted = !!risk.halted;
  show('halted-banner', halted);
  if (halted) txt('halted-text', 'Bot detenido por kill-switch — requiere reanudación manual. Pulsa «Reanudar» para rebasar el pico y continuar.');
  const resumeBtn = $('btn-resume');
  resumeBtn.disabled = !halted;
  resumeBtn.title = halted
    ? 'Reanudar: rebasa el pico de equity y continúa tras el kill-switch.'
    : 'Reanudar solo está disponible con el kill-switch activo. No revive un bot parado: reinícialo en el servidor.';
}

/* ── Router ──────────────────────────────────────────────── */
const PANEL_TITLE = { resumen: 'Resumen', grafico: 'Gráfico', operaciones: 'Operaciones', riesgo: 'Riesgo', logs: 'Logs', config: 'Configuración' };
function setPanel(p) {
  S.panel = p;
  for (const a of document.querySelectorAll('#nav a')) a.classList.toggle('active', a.dataset.panel === p);
  for (const sec of document.querySelectorAll('.screen')) sec.hidden = (sec.id !== 'screen-' + p);
  txt('page-title', PANEL_TITLE[p] || p);
  window.scrollTo(0, 0);
  renderPanel(p);
  if (p === 'logs') loadLogs();
  if (p === 'config') loadConfig();
  if (p === 'riesgo') loadCircuitBreakers();
}
function renderPanel(p) {
  if (!S.overview && p !== 'logs' && p !== 'config') return;
  if (p === 'resumen') renderResumen();
  else if (p === 'grafico') renderGrafico();
  else if (p === 'operaciones') renderOperaciones();
  else if (p === 'riesgo') renderRiesgo();
}

/* ── RESUMEN ─────────────────────────────────────────────── */
function renderResumen() {
  const ov = S.overview || {}, st = ov.state || {}, risk = ov.risk || {}, stats = ov.stats || {};
  const pos = st.position || null;
  const hasPos = pos && pos.side && pos.side !== 'flat' && Math.abs(pos.size || 0) > 1e-9;

  // KPIs
  txt('kpi-equity', money(st.equity));
  txt('kpi-equity-sub', 'libre ' + money(st.free));
  txt('kpi-pnl', signedMoney(stats.total_pnl)); applyClass('kpi-pnl', pnlClass(stats.total_pnl));
  txt('kpi-pnl-sub', '24h ' + signedMoney(stats.pnl_24h));
  txt('kpi-upnl', hasPos ? signedMoney(pos.uPnL) : '—'); applyClass('kpi-upnl', pnlClass(hasPos ? pos.uPnL : 0));
  const roe = hasPos && pos.margin ? (pos.uPnL / pos.margin) * 100 : null;
  txt('kpi-roe', 'ROE ' + (roe == null ? '—' : signedPct(roe)));
  txt('kpi-win', stats.win_rate != null ? pct(stats.win_rate, 1) : '—');
  txt('kpi-pf', 'PF ' + pfLabel(stats));

  // Posición / grid (panel estrella adaptativo al modo). En grid mandamos el resumen del
  // grid SIEMPRE: si una rung dejó posición neta, se muestra como una línea dentro del
  // resumen (no saltamos al panel de tendencia, que es para direccionales con Chandelier).
  const isGrid = st.mode === 'grid' && !!st.grid;
  show('pos-grid', isGrid);
  show('pos-detail', !isGrid && hasPos);
  show('pos-flat', !isGrid && !hasPos);
  txt('pos-side-label', isGrid ? 'GRID ACTIVO' : (hasPos ? (pos.side === 'long' ? 'LARGO' : 'CORTO') : 'SIN POSICIÓN'));
  if (isGrid) renderGridSummary(st);
  if (hasPos) {
    const notional = (pos.size || 0) * (pos.mark || 0);
    txt('pos-side', pos.side === 'long' ? 'Largo' : 'Corto');
    txt('pos-qty', num(pos.size, 4));
    txt('pos-lev', (pos.leverage || st.leverage || '—') + 'x');
    txt('pos-notional', money(notional));
    txt('pos-entry', money(pos.entry, priceDp(pos.entry)));
    txt('pos-mark', money(pos.mark, priceDp(pos.mark)));
    txt('pos-stop', st.trend_stop ? money(st.trend_stop, priceDp(st.trend_stop)) : '—');
    txt('pos-margin', money(pos.margin));
    txt('pos-upnl', signedMoney(pos.uPnL)); applyClass('pos-upnl', '', true);
    $('pos-upnl').style.color = pos.uPnL >= 0 ? '#3fbf83' : '#e0655c';
    txt('pos-roe', roe == null ? '—' : signedPct(roe));
    txt('pos-time', posTime(pos));
    txt('pos-funding', st.funding_rate != null ? signedPct(st.funding_rate * 100, 4) : '—');
    // margen a liquidación
    if (pos.liq && pos.mark) {
      const dist = Math.abs(pos.mark - pos.liq) / pos.mark * 100;
      txt('liq-pct', num(dist, 2) + '%');
      const fill = Math.max(4, Math.min(100, dist * 2.5));   // 40% dist ≈ barra llena
      const bar = $('liq-bar-fill'); bar.style.width = fill + '%';
      bar.style.background = dist < 8 ? '#e0655c' : (dist < 20 ? '#e9b34a' : '#3fbf83');
    } else { txt('liq-pct', '—'); $('liq-bar-fill').style.width = '0%'; }
  }

  // Decisión + régimen
  renderDecision(st);
  renderTimeline(st.regime_history || []);

  // Gauges riesgo (resumen)
  gauge('g-daily', risk.daily_loss_pct, risk.max_daily_loss_pct ?? cfgPct('max_daily_loss_pct'), 'límite');
  gauge('g-dd', risk.drawdown_pct, risk.max_total_drawdown_pct ?? cfgPct('max_total_drawdown_pct'), 'límite');
  txt('g-state', risk.halted ? 'DETENIDO' : 'Operando');
  $('g-state').style.color = risk.halted ? '#c8453a' : '#0f7a52';

  // Órdenes activas (escalera del grid en reposo)
  renderOpenOrders(st);

  // Tabla recientes
  renderRecent();

  // Chart
  ensurePriceChart('chart-resumen', 308, S.tf, st);
}
function posTime(pos) {
  const want = pos.side === 'long' ? 'Buy' : 'Sell';
  for (const t of S.trades) {              // reliable source: the recorded trend entry
    if (t.side === want && meta(t).reason === 'trend_entry') return relTime(t.timestamp);
  }
  for (const t of S.trades) {              // fallback heuristic: grid fills also have pnl≈0 → approx
    if (t.side === want && Math.abs(t.pnl || 0) < 1e-9) return '~' + relTime(t.timestamp);
  }
  return '—';
}
function renderDecision(st) {
  const k = regimeKind(st.regime), adx = st.indicators?.adx;
  const ema = st.indicators ? (st.indicators.ema_fast >= st.indicators.ema_slow ? 'alcista' : 'bajista') : '—';
  let s;
  if (k === 'up') s = `El mercado está en <b>tendencia alcista</b> (ADX ${num(adx, 1)}). El bot busca o mantiene posiciones <b>largas</b> y sale con el stop Chandelier o al revertir la tendencia.`;
  else if (k === 'down') s = `El mercado está en <b>tendencia bajista</b> (ADX ${num(adx, 1)}). El bot busca o mantiene posiciones <b>cortas</b> y sale con el stop Chandelier o al revertir la tendencia.`;
  else if (k === 'range') s = `Mercado en <b>rango</b> (ADX ${num(adx, 1)}). El bot opera una <b>grid neutral</b>, capturando oscilaciones en ambos lados dentro de la banda.`;
  else s = `Zona <b>transicional</b> (ADX ${num(adx, 1)}, entre umbrales). El bot se mantiene <b>al margen</b> hasta que se confirme un régimen.`;
  $('dec-text').innerHTML = s;
  txt('dec-adx', num(adx, 1));
  txt('dec-ema', ema);
  const htf = st.regime_htf;
  if (k !== 'up' && k !== 'down') {
    txt('dec-htf', 'N/A');                  // la confirmación TF superior solo aplica en tendencia
  } else if (htf == null) {
    txt('dec-htf', '—');
  } else {
    const aligned = regimeKind(htf) === k;
    txt('dec-htf', aligned ? 'Confirma (' + regimeLabel(htf) + ')' : 'No confirma (' + regimeLabel(htf) + ')');
  }
  txt('dec-atr', st.indicators?.atr_pct != null ? pct(st.indicators.atr_pct * 100, 2) : '—');
}
function renderTimeline(hist) {
  const host = $('regime-timeline'); if (!host) return;
  const seq = hist.slice(-28);
  host.innerHTML = seq.length
    ? seq.map((r) => `<i style="background:${regimeColor(r)}" title="${regimeLabel(r)}"></i>`).join('')
    : '<i style="background:#e2ddd0"></i>';
}
function renderRecent() {
  const body = $('tbody-recent');
  const rows = S.trades.slice(0, 8);
  if (!rows.length) { body.innerHTML = '<tr class="empty-row"><td colspan="6">Sin operaciones todavía.</td></tr>'; return; }
  body.innerHTML = rows.map((t) => {
    const m = meta(t), buy = t.side === 'Buy';
    return `<tr>
      <td class="mono">${timeHM(t.timestamp)}</td>
      <td><span class="tag ${buy ? 'buy' : 'sell'}">${buy ? 'Compra' : 'Venta'}</span></td>
      <td class="r mono">${money(t.price, priceDp(t.price))}</td>
      <td class="r mono">${num(t.qty, 4)}</td>
      <td class="r mono ${pnlClass(t.pnl)}">${signedMoney(t.pnl)}</td>
      <td>${escapeHtml(reasonLabel(m.reason, t.side))}</td></tr>`;
  }).join('');
}

/* Órdenes activas: la escalera del grid en reposo (lado/precio/qty/notional/distancia). */
function renderOpenOrders(st) {
  const orders = st.open_orders || [];
  const body = $('tbody-orders');
  const ref = (st.indicators && st.indicators.price) || (st.grid && st.grid.mid) || 0;
  if (!orders.length) {
    body.innerHTML = '<tr class="empty-row"><td colspan="5">Sin órdenes activas.</td></tr>';
    txt('orders-summary', '—');
    return;
  }
  const total = orders.reduce((a, o) => a + (o.notional || 0), 0);
  txt('orders-summary', orders.length + ' órdenes · ' + money(total));
  body.innerHTML = orders.map((o) => {
    const buy = o.side === 'Buy';
    const dist = ref ? ((o.price - ref) / ref) * 100 : null;
    const tp = o.is_partner ? ' <span class="mono" style="color:var(--muted-3);font-size:10px;margin-left:6px">TP</span>' : '';
    return `<tr>
      <td><span class="tag ${buy ? 'buy' : 'sell'}">${buy ? 'Compra' : 'Venta'}</span>${tp}</td>
      <td class="r mono">${money(o.price, priceDp(o.price))}</td>
      <td class="r mono">${num(o.qty, 4)}</td>
      <td class="r mono">${money(o.notional)}</td>
      <td class="r mono">${dist == null ? '—' : signedPct(dist, 2)}</td></tr>`;
  }).join('');
}

/* Resumen del grid para el panel estrella cuando el bot está en modo grid. */
function renderGridSummary(st) {
  const g = st.grid || {}, orders = st.open_orders || [];
  const ref = (st.indicators && st.indicators.price) || g.mid || 0;
  const buys = orders.filter((o) => o.side === 'Buy').length;
  const sells = orders.filter((o) => o.side === 'Sell').length;
  const total = orders.reduce((a, o) => a + (o.notional || 0), 0);
  txt('grid-mid', money(g.mid, priceDp(g.mid)));
  txt('grid-buys', String(buys));
  txt('grid-sells', String(sells));
  txt('grid-notional', money(total));
  txt('grid-spacing', num((g.spacing_pct || 0) * 100, 2) + '%');
  let near = null, nd = Infinity;
  for (const o of orders) { const d = Math.abs(o.price - ref); if (d < nd) { nd = d; near = o; } }
  txt('grid-next', near ? (money(near.price, priceDp(near.price)) + ' · ' + signedPct((near.price - ref) / ref * 100, 2)) : '—');
  txt('grid-band', money(g.lower_bound, priceDp(g.lower_bound)) + ' – ' + money(g.upper_bound, priceDp(g.upper_bound)));
  txt('grid-sl', money(g.sl_lower, priceDp(g.sl_lower)) + ' – ' + money(g.sl_upper, priceDp(g.sl_upper)));
  // Posición neta: una rung llena deja neto ≠ 0 hasta que su TP la cierra. Se muestra aquí
  // dentro (sin saltar al panel de tendencia); "Neutral" cuando el grid está equilibrado.
  const pos = st.position || {};
  const hasNet = pos.side && pos.side !== 'flat' && Math.abs(pos.size || 0) > 1e-9;
  const npEl = $('grid-netpos');
  if (hasNet) {
    txt('grid-netpos', (pos.side === 'long' ? 'Largo' : 'Corto') + ' ' + num(pos.size, 4) + ' · ' + signedMoney(pos.uPnL || 0));
    if (npEl) npEl.style.color = (pos.uPnL || 0) >= 0 ? '#3fbf83' : '#e0655c';
  } else {
    txt('grid-netpos', 'Neutral');
    if (npEl) npEl.style.color = '';
  }
}

/* ── GRÁFICO ─────────────────────────────────────────────── */
function renderGrafico() {
  const st = (S.overview || {}).state || {};
  ensurePriceChart('chart-grafico', 460, S.tf, st);
  ensureEquityChart();
}

/* ── OPERACIONES ─────────────────────────────────────────── */
function renderOperaciones() {
  const stats = (S.overview || {}).stats || {};
  txt('st-count', stats.total_closed_trades != null ? stats.total_closed_trades : '—');
  txt('st-win', stats.win_rate != null ? pct(stats.win_rate, 1) : '—');
  txt('st-avgwin', stats.avg_win ? '+' + money(stats.avg_win) : '—');
  txt('st-avgloss', stats.avg_loss ? MINUS + money(stats.avg_loss) : '—');
  txt('st-pf', pfLabel(stats));

  const lev = ((S.overview || {}).state || {}).leverage || 1;
  let rows = S.trades.slice();
  if (S.opFilter === 'buy') rows = rows.filter((t) => t.side === 'Buy');
  else if (S.opFilter === 'sell') rows = rows.filter((t) => t.side === 'Sell');
  if (S.opSearch) {
    const q = S.opSearch.toLowerCase();
    rows = rows.filter((t) => (meta(t).reason || '').toLowerCase().includes(q));
  }
  const body = $('tbody-ops');
  if (!rows.length) { body.innerHTML = '<tr class="empty-row"><td colspan="9">Sin operaciones.</td></tr>'; return; }
  body.innerHTML = rows.map((t) => {
    const m = meta(t), buy = t.side === 'Buy';
    const notional = (t.price || 0) * (t.qty || 0);
    const roe = (t.pnl && notional) ? (t.pnl / (notional / lev)) * 100 : null;
    const rc = m.regime ? regimeColor(m.regime) : '#9a9384';
    const rl = m.regime ? regimeLabel(m.regime) : '—';
    return `<tr>
      <td class="mono">${dateHM(t.timestamp)}</td>
      <td><span class="tag ${buy ? 'buy' : 'sell'}">${buy ? 'Compra' : 'Venta'}</span></td>
      <td class="r mono">${money(t.price, priceDp(t.price))}</td>
      <td class="r mono">${num(t.qty, 4)}</td>
      <td class="r mono">${money(notional)}</td>
      <td class="r mono ${pnlClass(t.pnl)}">${signedMoney(t.pnl)}</td>
      <td class="r mono ${pnlClass(t.pnl)}">${roe == null ? '—' : signedPct(roe)}</td>
      <td>${escapeHtml(reasonLabel(m.reason, t.side))}</td>
      <td><span class="reg-dot"><span class="d" style="background:${rc}"></span>${escapeHtml(rl)}</span></td></tr>`;
  }).join('');
}
function exportCSV() {
  const lev = ((S.overview || {}).state || {}).leverage || 1;
  const head = ['fecha_hora', 'lado', 'precio', 'qty', 'notional', 'pnl', 'roe_aprox', 'motivo', 'regimen'];
  const lines = [head.join(',')];
  for (const t of S.trades) {
    const m = meta(t), notional = (t.price || 0) * (t.qty || 0);
    const roe = (t.pnl && notional) ? (t.pnl / (notional / lev)) * 100 : '';
    lines.push([t.timestamp, t.side, t.price, t.qty, notional.toFixed(2), (t.pnl || 0).toFixed(2),
      roe === '' ? '' : roe.toFixed(2), '"' + (m.reason || '') + '"', m.regime || ''].join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'operaciones.csv'; a.click();
  URL.revokeObjectURL(a.href);
}

/* ── RIESGO ──────────────────────────────────────────────── */
function renderRiesgo() {
  const ov = S.overview || {}, risk = ov.risk || {}, st = ov.state || {}, pos = st.position || {};
  const halted = !!risk.halted;
  $('risk-banner-dot').style.background = halted ? '#c8453a' : '#3fbf83';
  txt('risk-banner-title', halted ? 'Kill-switch DISPARADO · bot detenido' : 'Kill-switch armado · operando con normalidad');
  txt('risk-banner-sub', halted ? 'Posiciones aplanadas. Requiere reanudación manual.' : 'Sin disparos activos.');

  gauge('rk-daily', risk.daily_loss_pct, risk.max_daily_loss_pct ?? cfgPct('max_daily_loss_pct'), 'umbral');
  gauge('rk-dd', risk.drawdown_pct, risk.max_total_drawdown_pct ?? cfgPct('max_total_drawdown_pct'), 'umbral');

  const hasPos = pos && pos.side && pos.side !== 'flat' && Math.abs(pos.size || 0) > 1e-9 && pos.liq;
  show('liq-flat', !hasPos); show('liq-detail', hasPos);
  if (hasPos) {
    txt('rk-mark', money(pos.mark, priceDp(pos.mark)));
    txt('rk-liq', money(pos.liq, priceDp(pos.liq)));
    txt('rk-stop', st.trend_stop ? money(st.trend_stop, priceDp(st.trend_stop)) : '—');
    const dist = Math.abs(pos.mark - pos.liq) / pos.mark * 100;
    txt('rk-liq-dist', num(dist, 2) + '%');
  }

  txt('lim-daily', pct(cfgPct('max_daily_loss_pct') ?? risk.max_daily_loss_pct, 2));
  txt('lim-dd', pct(cfgPct('max_total_drawdown_pct') ?? risk.max_total_drawdown_pct, 2));
  txt('lim-risk', pct(cfgPct('risk_per_trade_pct'), 2));
  txt('lim-liqbuf', pct(cfgPct('min_liquidation_buffer_pct'), 1));
}
async function loadCircuitBreakers() {
  try {
    const r = await api('/logs/circuit-breakers?limit=50');
    const evs = (r && r.events) || [];
    const body = $('tbody-cb');
    if (!evs.length) { body.innerHTML = '<tr class="empty-row"><td colspan="5">Sin disparos registrados.</td></tr>'; return; }
    body.innerHTML = evs.map((e) => {
      const bt = e.breaker_type === 'max_daily_loss' ? 'Pérdida diaria' : (e.breaker_type === 'max_total_drawdown' ? 'Drawdown total' : e.breaker_type);
      return `<tr>
        <td class="mono">${dateHM(e.timestamp)}</td>
        <td>${escapeHtml(bt)}</td>
        <td class="r mono neg">${pct((e.trigger_value || 0) * 100, 2)}</td>
        <td class="r mono">${pct((e.threshold || 0) * 100, 2)}</td>
        <td class="mono">${escapeHtml(e.action_taken || '—')}</td></tr>`;
    }).join('');
  } catch (e) {}
}

/* ── LOGS ────────────────────────────────────────────────── */
async function loadLogs() {
  const term = $('term');
  let qp = '?hours=72&limit=300';
  if (S.logFilter !== 'all') qp += '&level=' + S.logFilter;
  if (S.logSearch) qp += '&search=' + encodeURIComponent(S.logSearch);
  try {
    const r = await api('/logs/events' + qp);
    const evs = (r && r.events) || [];
    if (!evs.length) { term.innerHTML = '<div class="ln"><span class="ms muted">Sin eventos para este filtro.</span></div>'; return; }
    term.innerHTML = evs.map((e) => {
      const lv = String(e.level || 'INFO').toUpperCase();
      return `<div class="ln">
        <span class="t">${timeHM(e.timestamp)}</span>
        <span class="lv lv-${lv.toLowerCase()}">${escapeHtml(lv)}</span>
        <span class="md">${escapeHtml(e.module || '')}</span>
        <span class="ms">${escapeHtml(e.message || '')}</span></div>`;
    }).join('');
  } catch (e) { term.innerHTML = '<div class="ln"><span class="ms muted">No se pudieron cargar los eventos.</span></div>'; }
}

/* ── CONFIG ──────────────────────────────────────────────── */
async function loadConfig() {
  try {
    const r = await api('/futures/config');
    const v = r.values || {}; S.cfg = v;
    if (r.reason) txt('cfg-reason', r.reason);
    txt('cfg-tf', tfLabel(v.timeframe));
    txt('cfg-htf', tfLabel(v.higher_timeframe));
    txt('cfg-adx-trend', num(v.adx_trend_threshold, 0));
    txt('cfg-adx-range', num(v.adx_range_threshold, 0));
    txt('cfg-ema-fast', num(v.ema_fast, 0));
    txt('cfg-ema-slow', num(v.ema_slow, 0));
    $('cfg-htf-toggle').classList.toggle('on', !!v.require_higher_tf_confirmation);
    txt('cfg-lev', v.leverage + 'x');
    txt('cfg-risk', pct(v.risk_per_trade_pct * 100, 2));
    txt('cfg-minord', num(v.min_order_usdt, 0));
    txt('cfg-chper', num(v.chandelier_period, 0));
    txt('cfg-chmult', num(v.chandelier_atr_mult, 1));
    txt('cfg-levels', num(v.grid_levels, 0));
    txt('cfg-maxdaily', pct(v.max_daily_loss_pct * 100, 2));
    txt('cfg-maxdd', pct(v.max_total_drawdown_pct * 100, 2));
    txt('cfg-slpct', pct(v.stop_loss_pct * 100, 1));
    if (S.overview) renderPanel(S.panel);   // límites/derivados ya pueden poblarse
  } catch (e) { /* p.ej. 429 transitorio: tick() reintenta hasta cargar */ }
}
const TF_LBL = { '1': '1m', '5': '5m', '15': '15m', '60': '1h', '240': '4h', 'D': '1d' };
const tfLabel = (t) => TF_LBL[String(t)] || (t + 'm');

/* ── Gauges ──────────────────────────────────────────────── */
function gauge(id, val, limit, footWord) {
  txt(id + '-val', pct(val, 2));
  const ratio = (limit && limit > 0) ? Math.max(0, val) / limit : 0;
  const bar = $(id + '-bar');
  if (bar) {
    bar.style.width = Math.max(0, Math.min(100, ratio * 100)) + '%';
    bar.className = ratio >= 0.85 ? 'crit' : (ratio >= 0.5 ? 'warn' : '');
  }
  txt(id + '-foot', `${footWord} ${pct(limit, 2)}`);
}

/* ── Gráficos (Lightweight Charts) ───────────────────────── */
function chartTheme(h, interactive) {
  return {
    height: h,
    layout: { background: { color: '#ffffff' }, textColor: '#9a9384', fontFamily: "'IBM Plex Mono', monospace", fontSize: 10 },
    grid: { vertLines: { color: '#f4f0e7' }, horzLines: { color: '#f4f0e7' } },
    rightPriceScale: { borderColor: '#e2ddd0' },
    timeScale: { borderColor: '#e2ddd0', timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0, vertLine: { color: '#cabfa8', width: 1, style: 2, labelBackgroundColor: '#16140f' }, horzLine: { color: '#cabfa8', width: 1, style: 2, labelBackgroundColor: '#16140f' } },
    handleScroll: interactive, handleScale: interactive,
  };
}
function emaData(candles, period) {
  if (!candles.length) return [];
  const k = 2 / (period + 1); let e = candles[0].close;
  return candles.map((c, i) => { e = i === 0 ? c.close : c.close * k + e * (1 - k); return { time: c.time, value: e }; });
}
function priceDp(p) { if (p == null) return 2; const a = Math.abs(p); return a >= 1000 ? 2 : (a >= 1 ? 2 : 5); }

async function ensurePriceChart(hostId, height, tf, st) {
  if (!window.LightweightCharts) { setTimeout(() => ensurePriceChart(hostId, height, tf, st), 200); return; }
  const host = $(hostId); if (!host || host.offsetParent === null) return;  // panel oculto
  let C = S.charts[hostId];
  if (!C) {
    if (!host.clientWidth) { requestAnimationFrame(() => ensurePriceChart(hostId, height, tf, st)); return; }
    const chart = LightweightCharts.createChart(host, { width: host.clientWidth, ...chartTheme(height, hostId === 'chart-grafico') });
    const candle = chart.addCandlestickSeries({ upColor: '#0f7a52', downColor: '#c8453a', borderUpColor: '#0f7a52', borderDownColor: '#c8453a', wickUpColor: '#3fbf83', wickDownColor: '#e0655c' });
    const ema50 = chart.addLineSeries({ color: '#16140f', lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    const ema200 = chart.addLineSeries({ color: '#c98a2b', lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false });
    C = S.charts[hostId] = { chart, candle, ema50, ema200, entry: null, stop: null, tf: null, loadedSym: null };
    window.addEventListener('resize', () => { if (host.clientWidth) chart.applyOptions({ width: host.clientWidth }); });
  }
  C.chart.applyOptions({ width: host.clientWidth || 600 });
  const sym = st.symbol;
  if (C.tf !== tf || C.loadedSym !== sym) {
    const candles = await loadKlines(sym, tf);
    if (candles.length) {                 // marca cargado SOLO con éxito → reintenta si vino vacío
      C.tf = tf; C.loadedSym = sym; C._candles = candles;
      C.candle.setData(candles);
      C.ema50.setData(emaData(candles, 50));
      C.ema200.setData(emaData(candles, 200));
      C.chart.timeScale().fitContent();
    }
  }
  if (C._candles && C._candles.length) updatePriceOverlays(C, st);
}
function updatePriceOverlays(C, st) {
  if (!C._candles || !C._candles.length) return;
  const pos = st.position || {};
  const hasPos = pos.side && pos.side !== 'flat' && Math.abs(pos.size || 0) > 1e-9;
  const entryPrice = (hasPos && pos.entry) ? pos.entry : null;
  const stopPrice = (hasPos && st.trend_stop) ? st.trend_stop : null;

  // Líneas de entrada/stop: recrear SOLO si el precio cambia (anti-parpadeo por tick).
  if (entryPrice !== C._entryAt) {
    if (C.entry) { C.candle.removePriceLine(C.entry); C.entry = null; }
    if (entryPrice != null) C.entry = C.candle.createPriceLine({ price: entryPrice, color: '#16140f', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'Entrada' });
    C._entryAt = entryPrice;
  }
  if (stopPrice !== C._stopAt) {
    if (C.stop) { C.candle.removePriceLine(C.stop); C.stop = null; }
    if (stopPrice != null) C.stop = C.candle.createPriceLine({ price: stopPrice, color: '#c98a2b', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'Stop' });
    C._stopAt = stopPrice;
  }

  // Zona del grid: techo / centro / suelo (3 líneas claras). No saturamos el gráfico con las
  // 12 rungs (a 0.4% quedan ilegibles); el detalle por nivel está en la tabla "Órdenes activas".
  // Recrear SOLO si la zona cambia (firma) → anti-parpadeo.
  const g = st.grid;
  const gridSig = g ? (g.lower_bound + '|' + g.mid + '|' + g.upper_bound) : '';
  if (gridSig !== C._gridSig) {
    if (C._gridLines) for (const ln of C._gridLines) C.candle.removePriceLine(ln);
    C._gridLines = [];
    if (g) {
      const add = (price, title, style) => { if (price) C._gridLines.push(C.candle.createPriceLine({ price, color: '#9a9384', lineWidth: 1, lineStyle: style, axisLabelVisible: true, title })); };
      add(g.upper_bound, 'Techo', 2);
      add(g.mid, 'Centro', 1);
      add(g.lower_bound, 'Suelo', 2);
    }
    C._gridSig = gridSig;
  }

  // Marcadores (máx 30, snap a la vela más cercana): re-llamar setMarkers SOLO si cambian.
  const times = C._candles.map((c) => c.time);
  const lo = times[0], hi = times[times.length - 1];
  const mk = [];
  for (const t of S.trades) {
    if (mk.length >= 30) break;
    const u = unix(t.timestamp); if (u == null || u < lo - 86400 || u > hi + 86400) continue;
    const snap = nearest(times, u);
    const buy = t.side === 'Buy';
    mk.push({ time: snap, position: buy ? 'belowBar' : 'aboveBar', color: buy ? '#0f7a52' : '#c8453a', shape: buy ? 'arrowUp' : 'arrowDown', text: buy ? 'C' : 'V' });
  }
  mk.sort((a, b) => a.time - b.time);
  const sig = mk.map((m) => m.time + m.shape).join('|');
  if (sig !== C._mkSig) { C.candle.setMarkers(mk); C._mkSig = sig; }
}
function nearest(arr, v) { let best = arr[0], bd = Math.abs(arr[0] - v); for (const x of arr) { const d = Math.abs(x - v); if (d < bd) { bd = d; best = x; } } return best; }

async function loadKlines(sym, tf) {
  try {
    const qp = `?interval=${tf}&limit=200` + (sym ? `&symbol=${encodeURIComponent(sym)}` : '');
    const r = await api('/trading/klines' + qp);
    const rows = (r && r.klines) || [];
    const out = [];
    const seen = new Set();
    for (const k of rows) {
      const t = unix(k.timestamp || k.time || k.start);
      if (t == null || seen.has(t)) continue; seen.add(t);
      out.push({ time: t, open: +k.open, high: +k.high, low: +k.low, close: +k.close });
    }
    out.sort((a, b) => a.time - b.time);
    return out;
  } catch (e) { return []; }
}

async function ensureEquityChart() {
  if (!window.LightweightCharts) { setTimeout(ensureEquityChart, 200); return; }
  const host = $('chart-equity'); if (!host || host.offsetParent === null || !host.clientWidth) return;
  let C = S.charts['chart-equity'];
  if (!C) {
    const chart = LightweightCharts.createChart(host, { width: host.clientWidth, ...chartTheme(200, false) });
    const area = chart.addAreaSeries({ lineColor: '#0f7a52', topColor: 'rgba(15,122,82,0.18)', bottomColor: 'rgba(15,122,82,0)', lineWidth: 2 });
    C = S.charts['chart-equity'] = { chart, area };
    window.addEventListener('resize', () => { if (host.clientWidth) chart.applyOptions({ width: host.clientWidth }); });
  }
  C.chart.applyOptions({ width: host.clientWidth });
  const nowMs = Date.now();
  if (C._eqAt && nowMs - C._eqAt < 20000) return;   // throttle: refrescar equity cada ~20s
  C._eqAt = nowMs;
  try {
    const r = await api('/futures/equity?limit=400');
    const pts = (r && r.points) || [];
    const data = []; const seen = new Set();
    for (const p of pts) {
      const t = unix(p.timestamp); if (t == null || seen.has(t)) continue; seen.add(t);
      data.push({ time: t, value: +p.capital });
    }
    data.sort((a, b) => a.time - b.time);
    if (data.length) {
      C.area.setData(data);
      C.chart.timeScale().fitContent();
      const vals = data.map((d) => d.value);
      const peak = Math.max(...vals), min = Math.min(...vals), last = vals[vals.length - 1];
      txt('eq-peak', money(peak)); txt('eq-min', money(min));
      txt('eq-dd', pct(peak > 0 ? (peak - last) / peak * 100 : 0, 2));
    }
  } catch (e) { C._eqAt = 0; }   // permite reintento en el próximo render
}

/* ── Controles ───────────────────────────────────────────── */
const CONFIRM = {
  flatten: 'Aplanar cierra la posición o el grid abiertos. El bot SIGUE corriendo y '
         + 'volverá a operar en el próximo ciclo. ¿Aplanar ahora?',
  stop: 'Parar APAGA el proceso del bot en el servidor. Tendrás que reiniciarlo '
      + 'manualmente allí: «Reanudar» NO revive un bot parado. ¿Apagar el bot?',
};
async function control(action) {
  const labels = { resume: 'reanudar', flatten: 'aplanar', stop: 'parar el bot' };
  if (CONFIRM[action] && !confirm(CONFIRM[action])) return;
  try {
    await api('/futures/control', { method: 'POST', body: JSON.stringify({ action }) });
    toast('Orden enviada: ' + labels[action]);
    setTimeout(tick, 800);
  } catch (e) { toast('No se pudo enviar la orden', true); }
}

/* ── Utilidades varias ───────────────────────────────────── */
function meta(t) { try { return t.metadata_json ? JSON.parse(t.metadata_json) : {}; } catch (e) { return {}; } }
function applyClass(id, cls, keep) { const e = $(id); if (!e) return; if (!keep) e.className = e.className.replace(/\b(pos|neg|neu)\b/g, '').trim(); if (cls) e.classList.add(cls); }
function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
let toastT;
function toast(msg, err) {
  const t = $('toast'); t.textContent = msg; t.className = 'show' + (err ? ' err' : '');
  clearTimeout(toastT); toastT = setTimeout(() => { t.className = ''; }, 2600);
}

/* ── Wiring ──────────────────────────────────────────────── */
function init() {
  $('login-btn').addEventListener('click', doLogin);
  $('login-pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
  $('logout-btn').addEventListener('click', logout);
  for (const a of document.querySelectorAll('#nav a')) a.addEventListener('click', () => setPanel(a.dataset.panel));
  $('btn-resume').addEventListener('click', () => control('resume'));
  $('btn-flatten').addEventListener('click', () => control('flatten'));
  $('btn-stop').addEventListener('click', () => control('stop'));
  $('btn-csv').addEventListener('click', exportCSV);
  $('btn-logs-refresh').addEventListener('click', loadLogs);

  for (const b of document.querySelectorAll('#tf-grafico button')) b.addEventListener('click', () => {
    S.tf = b.dataset.tf;
    for (const x of document.querySelectorAll('#tf-grafico button')) x.classList.toggle('active', x === b);
    renderGrafico();
  });
  for (const b of document.querySelectorAll('#opfilter button')) b.addEventListener('click', () => {
    S.opFilter = b.dataset.f;
    for (const x of document.querySelectorAll('#opfilter button')) x.classList.toggle('active', x === b);
    renderOperaciones();
  });
  for (const b of document.querySelectorAll('#logfilter button')) b.addEventListener('click', () => {
    S.logFilter = b.dataset.l;
    for (const x of document.querySelectorAll('#logfilter button')) x.classList.toggle('active', x === b);
    loadLogs();
  });
  $('opsearch').addEventListener('input', (e) => { S.opSearch = e.target.value; renderOperaciones(); });
  $('logsearch').addEventListener('input', debounce((e) => { S.logSearch = e.target.value; loadLogs(); }, 350));

  if (S.token) startApp(); else { show('login-overlay', true); show('app', false); }
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
})();
