/* ═══════════════════════════════════════════════════════════
   Trading Bot Dashboard — Application Logic
   API client, WebSocket, charts, and panel management
   ═══════════════════════════════════════════════════════════ */

// ── State ────────────────────────────────────────────────────
const APP = {
    token: localStorage.getItem('bot_token') || null,
    ws: null,
    apiBase: '/api',
    refreshInterval: null,
    charts: {},
    currentPanel: 'overview',
};

// ── API Client ───────────────────────────────────────────────
async function api(endpoint, options = {}) {
    const url = `${APP.apiBase}${endpoint}`;
    const headers = { 'Content-Type': 'application/json' };
    if (APP.token) headers['Authorization'] = `Bearer ${APP.token}`;
    try {
        const resp = await fetch(url, { ...options, headers });
        if (resp.status === 401) { logout(); return null; }
        return await resp.json();
    } catch (e) {
        console.error(`API error: ${endpoint}`, e);
        return null;
    }
}

// ── Auth ─────────────────────────────────────────────────────
async function login() {
    const user = document.getElementById('login-user').value;
    const pass = document.getElementById('login-pass').value;
    const data = await api('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username: user, password: pass }),
    });
    if (data && data.access_token) {
        APP.token = data.access_token;
        localStorage.setItem('bot_token', APP.token);
        document.getElementById('login-modal').style.display = 'none';
        initDashboard();
    } else {
        const err = document.getElementById('login-error');
        err.style.display = 'block';
        err.textContent = data?.detail || 'Credenciales inválidas';
    }
}

function logout() {
    APP.token = null;
    localStorage.removeItem('bot_token');
    document.getElementById('login-modal').style.display = 'flex';
    if (APP.refreshInterval) clearInterval(APP.refreshInterval);
    if (APP.ws) APP.ws.close();
}

// ── Init ─────────────────────────────────────────────────────
function initDashboard() {
    setupNavigation();
    setupButtons();
    setupSliders();
    connectWebSocket();
    refreshData();
    APP.refreshInterval = setInterval(refreshData, 15000);
    initCharts();
}

// ── Navigation ───────────────────────────────────────────────
function setupNavigation() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => {
            const panel = item.dataset.panel;
            switchPanel(panel);
        });
    });
}

function switchPanel(panelName) {
    APP.currentPanel = panelName;
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    const navItem = document.querySelector(`.nav-item[data-panel="${panelName}"]`);
    const panel = document.getElementById(`panel-${panelName}`);
    if (navItem) navItem.classList.add('active');
    if (panel) panel.classList.add('active');
    onPanelSwitch(panelName);
}

function onPanelSwitch(panel) {
    if (panel === 'trading') loadTrades();
    if (panel === 'performance') loadPerformance();
    if (panel === 'risk') loadRiskData();
    if (panel === 'logs') loadLogs();
    if (panel === 'config') loadConfig();
}

// ── Buttons ──────────────────────────────────────────────────
function setupButtons() {
    document.getElementById('btn-start').addEventListener('click', () => api('/dashboard/bot/start', { method: 'POST' }));
    document.getElementById('btn-stop').addEventListener('click', () => api('/dashboard/bot/stop', { method: 'POST' }));
    document.getElementById('btn-pause').addEventListener('click', () => api('/dashboard/bot/pause', { method: 'POST' }));
    document.getElementById('btn-emergency').addEventListener('click', async () => {
        if (confirm('⚠️ ¿Cancelar TODAS las órdenes inmediatamente?')) {
            await api('/dashboard/bot/emergency', { method: 'POST' });
        }
    });
    document.getElementById('btn-run-backtest').addEventListener('click', runBacktest);
    document.getElementById('btn-export-csv').addEventListener('click', exportCSV);
    document.getElementById('btn-save-config').addEventListener('click', saveConfig);
    document.getElementById('btn-refresh-logs').addEventListener('click', loadLogs);
}

function setupSliders() {
    ['bt-months', 'bt-levels', 'cfg-levels'].forEach(id => {
        const slider = document.getElementById(id);
        const valEl = document.getElementById(`${id}-val`);
        if (slider && valEl) {
            slider.addEventListener('input', () => {
                const suffix = id.includes('months') ? ' meses' : '';
                valEl.textContent = slider.value + suffix;
            });
        }
    });
}

// ── Data Refresh ─────────────────────────────────────────────
async function refreshData() {
    const data = await api('/dashboard/status');
    if (!data) return;
    updateStatus(data);
    updateKPIs(data);
}

function updateStatus(data) {
    const status = data.bot_state?.status || 'stopped';
    const leds = ['header-led', 'sidebar-led'];
    leds.forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.className = 'status-led ' + status; }
    });
    const statusText = document.getElementById('header-status');
    if (statusText) statusText.textContent = status.toUpperCase();
    const sidebarStatus = document.getElementById('sidebar-status');
    if (sidebarStatus) sidebarStatus.textContent = status === 'running' ? 'Operando' : status;
    const uptimeEl = document.getElementById('sidebar-uptime');
    if (uptimeEl) uptimeEl.textContent = `Uptime: ${(data.uptime_hours || 0).toFixed(1)}h`;
    const modeBadge = document.getElementById('mode-badge');
    if (modeBadge) modeBadge.textContent = data.testnet ? 'TESTNET' : '🔴 MAINNET';
    const latency = document.getElementById('latency-badge');
    if (latency) latency.textContent = (data.exchange_latency_ms || '--') + 'ms';
}

function updateKPIs(data) {
    const pnl = data.pnl || {};
    const capital = data.capital || {};
    const risk = data.risk || {};

    setText('kpi-capital', `$${(capital.current || 0).toFixed(2)}`);
    const changePct = capital.change_pct || 0;
    const changeEl = document.getElementById('kpi-capital-change');
    if (changeEl) {
        changeEl.textContent = `${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%`;
        changeEl.className = `kpi-change ${changePct >= 0 ? 'up' : 'down'}`;
    }

    setPnlText('kpi-pnl-24h', pnl['24h'] || 0);
    setPnlText('kpi-pnl-total', pnl.total || 0);
    setText('kpi-drawdown', `${(risk.drawdown_pct || 0).toFixed(2)}%`);

    const ddBar = document.getElementById('drawdown-bar');
    if (ddBar) {
        const ddPct = risk.drawdown_pct || 0;
        const ddLimit = risk.drawdown_limit_pct || 15;
        ddBar.style.width = `${Math.min(100, (ddPct / ddLimit) * 100)}%`;
        ddBar.className = 'progress-fill ' + (ddPct > ddLimit * 0.7 ? 'progress-danger' : ddPct > ddLimit * 0.4 ? 'progress-warning' : 'progress-safe');
    }

    // Quick risk
    setText('risk-dd-quick', `${(risk.drawdown_pct || 0).toFixed(1)}%`);
    setText('risk-daily-quick', `${(risk.daily_loss_pct || 0).toFixed(2)}%`);

    // Price badge
    if (data.last_trade) {
        const price = document.getElementById('price-badge');
        if (price) price.textContent = `BTC: $${(data.last_trade.price || 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
    }
}

// ── WebSocket ────────────────────────────────────────────────
function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${protocol}://${location.host}/ws`;
    APP.ws = new WebSocket(wsUrl);

    APP.ws.onopen = () => {
        console.log('WebSocket connected');
        APP.ws.send(JSON.stringify({ type: 'subscribe', channel: 'all' }));
    };

    APP.ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleWSMessage(msg);
        } catch (e) { /* ignore */ }
    };

    APP.ws.onclose = () => {
        console.log('WebSocket disconnected — reconnecting in 5s');
        setTimeout(connectWebSocket, 5000);
    };

    // Ping every 30s
    setInterval(() => {
        if (APP.ws && APP.ws.readyState === WebSocket.OPEN) {
            APP.ws.send(JSON.stringify({ type: 'ping' }));
        }
    }, 30000);
}

function handleWSMessage(msg) {
    switch (msg.type) {
        case 'status': updateStatus(msg.data); break;
        case 'price':
            const pBadge = document.getElementById('price-badge');
            if (pBadge && msg.data.price) pBadge.textContent = `BTC: $${Number(msg.data.price).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
            break;
        case 'trade':
            if (APP.currentPanel === 'trading') loadTrades();
            break;
        case 'grid':
            if (APP.currentPanel === 'grid') updateGridPanel(msg.data);
            break;
    }
}

// ── Charts ───────────────────────────────────────────────────
function initCharts() {
    // Equity chart (overview)
    const eqContainer = document.getElementById('equity-chart');
    if (eqContainer && typeof LightweightCharts !== 'undefined') {
        const chart = LightweightCharts.createChart(eqContainer, chartOptions(eqContainer));
        APP.charts.equity = chart.addAreaSeries({
            topColor: 'rgba(59, 130, 246, 0.3)',
            bottomColor: 'rgba(59, 130, 246, 0.01)',
            lineColor: '#3b82f6',
            lineWidth: 2,
        });
        loadEquityChart();
    }

    // Price chart (trading)
    const priceContainer = document.getElementById('price-chart');
    if (priceContainer && typeof LightweightCharts !== 'undefined') {
        const chart = LightweightCharts.createChart(priceContainer, chartOptions(priceContainer));
        APP.charts.price = chart.addCandlestickSeries({
            upColor: '#00c087', downColor: '#ef4444',
            borderUpColor: '#00c087', borderDownColor: '#ef4444',
            wickUpColor: '#00c087', wickDownColor: '#ef4444',
        });
    }
}

function chartOptions(container) {
    return {
        width: container.clientWidth,
        height: container.clientHeight || 400,
        layout: {
            background: { type: 'solid', color: 'transparent' },
            textColor: '#94a3b8',
            fontSize: 11,
            fontFamily: 'Inter, sans-serif',
        },
        grid: {
            vertLines: { color: 'rgba(255,255,255,0.04)' },
            horzLines: { color: 'rgba(255,255,255,0.04)' },
        },
        crosshair: { mode: 0 },
        timeScale: {
            borderColor: 'rgba(255,255,255,0.06)',
            timeVisible: true,
        },
        rightPriceScale: { borderColor: 'rgba(255,255,255,0.06)' },
    };
}

async function loadEquityChart(days = 30) {
    if (!APP.charts.equity) return;
    const data = await api(`/performance/equity?days=${days}`);
    if (data && data.points) {
        const series = data.points.map(p => ({
            time: new Date(p.timestamp).getTime() / 1000,
            value: p.capital,
        }));
        APP.charts.equity.setData(series);
    }
}

// ── Panel Loaders ────────────────────────────────────────────
async function loadTrades() {
    const data = await api('/trading/trades?limit=50');
    if (!data || !data.trades) return;
    const tbody = document.getElementById('trades-table-body');
    if (!tbody) return;

    if (data.trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted);">Sin trades</td></tr>';
        return;
    }

    tbody.innerHTML = data.trades.map(t => `
    <tr>
      <td>${new Date(t.timestamp).toLocaleString()}</td>
      <td><span class="tag tag-${t.side.toLowerCase()}">${t.side}</span></td>
      <td>$${t.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>
      <td>${t.quantity.toFixed(6)}</td>
      <td>$${t.fee.toFixed(4)}</td>
      <td style="color:${t.pnl >= 0 ? 'var(--green)' : 'var(--red)'};">${t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(4)}</td>
    </tr>
  `).join('');

    // Load price chart
    const klines = await api('/trading/klines?interval=60&limit=200');
    if (klines && klines.klines && APP.charts.price) {
        const series = klines.klines.map(k => ({
            time: k.timestamp / 1000,
            open: k.open, high: k.high, low: k.low, close: k.close,
        }));
        APP.charts.price.setData(series);
    }
}

async function loadPerformance(period = '7d') {
    const data = await api(`/performance/metrics?period=${period}`);
    if (!data) return;
    setText('perf-total-trades', data.total_trades || 0);
    setPnlText('perf-net-pnl', data.net_pnl || 0);
    setText('perf-win-rate', `${(data.win_rate || 0).toFixed(1)}%`);
    setText('perf-profit-factor', (data.profit_factor || 0).toFixed(2));
    setText('perf-sharpe', (data.sharpe_ratio || 0).toFixed(2));
    setText('perf-max-dd', `${(data.max_drawdown_pct || 0).toFixed(2)}%`);
}

async function loadRiskData() {
    const data = await api('/logs/risk-status');
    if (!data || data.error) return;
    setText('risk-drawdown', `${(data.drawdown_pct || 0).toFixed(2)}%`);
    setText('risk-daily-loss', `${(data.daily_loss_pct || 0).toFixed(2)}%`);
    setText('risk-dd-limit', `${data.drawdown_limit_pct || 15}%`);
    setText('risk-daily-limit', `${data.daily_loss_limit_pct || 1}%`);
    updateProgressBar('risk-dd-bar', data.drawdown_pct, data.drawdown_limit_pct);
    updateProgressBar('risk-daily-bar', data.daily_loss_pct, data.daily_loss_limit_pct);

    const cbData = await api('/logs/circuit-breakers?limit=20');
    if (cbData && cbData.events) {
        const tbody = document.getElementById('cb-history-body');
        if (cbData.events.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted);">Sin eventos de circuit breaker ✅</td></tr>';
        } else {
            tbody.innerHTML = cbData.events.map(e => `
        <tr>
          <td>${new Date(e.timestamp).toLocaleString()}</td>
          <td><span class="tag" style="background:var(--red-dim);color:var(--red)">${e.breaker_type}</span></td>
          <td>${(e.trigger_value * 100).toFixed(2)}%</td>
          <td>${(e.threshold_value * 100).toFixed(2)}%</td>
          <td>${e.action_taken}</td>
        </tr>
      `).join('');
        }
    }
}

async function loadLogs() {
    const level = document.getElementById('log-filter-level').value;
    const search = document.getElementById('log-search').value;
    const params = new URLSearchParams({ hours: 24, limit: 200 });
    if (level) params.set('level', level);
    if (search) params.set('search', search);

    const data = await api(`/logs/events?${params}`);
    if (!data || !data.events) return;

    const terminal = document.getElementById('log-terminal');
    if (data.events.length === 0) {
        terminal.innerHTML = '<div class="log-entry info"><span class="level">INFO</span> Sin eventos en las últimas 24h</div>';
        return;
    }

    terminal.innerHTML = data.events.map(e => `
    <div class="log-entry ${e.level}">
      <span class="timestamp">${new Date(e.timestamp).toLocaleTimeString()}</span>
      <span class="level">${e.level.toUpperCase().padEnd(8)}</span>
      <span class="module">[${e.module}]</span>
      ${e.message}
    </div>
  `).join('');
    terminal.scrollTop = terminal.scrollHeight;
}

async function loadConfig() {
    const data = await api('/config/current');
    if (!data) return;
    setVal('cfg-levels', data.num_levels || 5);
    setVal('cfg-spacing', (data.min_spacing_pct || 0.006) * 100);
    setVal('cfg-atr-mult', data.atr_multiplier || 1.5);
    setVal('cfg-order-size', data.order_size_usdt || 25);
    setVal('cfg-adx-threshold', data.adx_threshold || 25);
    setVal('cfg-max-dd', (data.max_drawdown_pct || 0.15) * 100);
    setVal('cfg-max-daily', (data.max_daily_loss_pct || 0.01) * 100);
    setText('cfg-levels-val', data.num_levels || 5);
}

async function saveConfig() {
    const config = {
        num_levels: parseInt(getVal('cfg-levels')),
        min_spacing_pct: parseFloat(getVal('cfg-spacing')) / 100,
        atr_multiplier: parseFloat(getVal('cfg-atr-mult')),
        order_size_usdt: parseFloat(getVal('cfg-order-size')),
        adx_threshold: parseFloat(getVal('cfg-adx-threshold')),
        max_drawdown_pct: parseFloat(getVal('cfg-max-dd')) / 100,
        max_daily_loss_pct: parseFloat(getVal('cfg-max-daily')) / 100,
    };
    await api('/config/update', { method: 'PUT', body: JSON.stringify(config) });
}

// ── Backtest ─────────────────────────────────────────────────
async function runBacktest() {
    const btn = document.getElementById('btn-run-backtest');
    btn.disabled = true;
    btn.textContent = '⏳ Ejecutando...';

    const params = {
        months: parseInt(getVal('bt-months')),
        timeframe: getVal('bt-timeframe'),
        initial_capital: parseFloat(getVal('bt-capital')),
        num_levels: parseInt(getVal('bt-levels')),
        walk_forward: document.getElementById('bt-walkforward').checked,
    };

    await api('/backtest/run', { method: 'POST', body: JSON.stringify(params) });

    // Poll for results
    const poll = setInterval(async () => {
        const status = await api('/backtest/status');
        if (!status) return;

        const resultsDiv = document.getElementById('bt-results');
        if (status.running) {
            resultsDiv.innerHTML = `
        <div style="text-align:center;padding:30px;">
          <div class="spinner"></div>
          <div style="margin-top:16px;color:var(--text-secondary);">Progreso: ${status.progress}%</div>
          <div class="progress-bar" style="margin-top:8px;"><div class="progress-fill progress-safe" style="width:${status.progress}%;"></div></div>
        </div>`;
        } else {
            clearInterval(poll);
            btn.disabled = false;
            btn.textContent = '🚀 Ejecutar Backtest';

            if (status.error) {
                resultsDiv.innerHTML = `<div style="color:var(--red);padding:20px;">Error: ${status.error}</div>`;
            } else {
                loadBacktestResults();
            }
        }
    }, 2000);
}

async function loadBacktestResults() {
    const data = await api('/backtest/results');
    if (!data || data.error) return;

    const s = data.summary || data;
    const v = data.verdict || {};
    const resultsDiv = document.getElementById('bt-results');

    resultsDiv.innerHTML = `
    <div class="stats-grid" style="margin-bottom:16px;">
      <div class="stat-item"><span class="stat-label">Trades</span><span class="stat-value">${s.total_trades || 0}</span></div>
      <div class="stat-item"><span class="stat-label">Net PnL</span><span class="stat-value" style="color:${(s.net_pnl_usdt || 0) >= 0 ? 'var(--green)' : 'var(--red)'}">${(s.net_pnl_usdt || 0) >= 0 ? '+' : ''}$${(s.net_pnl_usdt || 0).toFixed(4)}</span></div>
      <div class="stat-item"><span class="stat-label">Return</span><span class="stat-value">${(s.capital_return_pct || 0).toFixed(2)}%</span></div>
      <div class="stat-item"><span class="stat-label">Max DD</span><span class="stat-value">${(s.max_drawdown_pct || 0).toFixed(2)}%</span></div>
      <div class="stat-item"><span class="stat-label">Sharpe</span><span class="stat-value">${(s.sharpe_ratio || 0).toFixed(2)}</span></div>
      <div class="stat-item"><span class="stat-label">Win Rate</span><span class="stat-value">${(s.win_rate_pct || 0).toFixed(1)}%</span></div>
      <div class="stat-item"><span class="stat-label">Profit Factor</span><span class="stat-value">${(s.profit_factor || 0).toFixed(2)}</span></div>
    </div>
    ${v.overall ? `<div style="text-align:center;font-size:1.2rem;font-weight:700;color:${v.overall === 'PASS' ? 'var(--green)' : 'var(--red)'};">${v.overall === 'PASS' ? '✅ PASS' : '❌ FAIL'}</div>` : ''}
  `;
}

async function exportCSV() {
    window.open(`${APP.apiBase}/performance/export`, '_blank');
}

// ── Grid Panel ───────────────────────────────────────────────
function updateGridPanel(data) {
    if (!data) return;
    setText('grid-center', data.center_price ? `$${data.center_price.toLocaleString()}` : '--');
    setText('grid-spacing', data.spacing_pct ? `${(data.spacing_pct * 100).toFixed(2)}%` : '--');
    setText('grid-levels-count', data.num_levels || '--');
    setText('grid-active-orders', data.pending_orders || '--');

    const container = document.getElementById('grid-levels-container');
    if (!container) return;

    const sells = (data.sell_levels || []).reverse();
    const buys = data.buy_levels || [];

    if (!sells.length && !buys.length) {
        container.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:40px;">Sin grid activo</div>';
        return;
    }

    let html = '';
    sells.forEach(l => {
        html += `<div class="grid-level sell"><span>SELL Lv${l.level || '?'}</span><span>$${l.price?.toLocaleString() || '--'}</span><span>${l.qty?.toFixed(6) || '--'}</span></div>`;
    });
    html += `<div class="grid-level" style="background:var(--blue-dim);border-left:3px solid var(--blue);font-weight:700;"><span>─── CENTRO ───</span><span>$${data.center_price?.toLocaleString() || '--'}</span></div>`;
    buys.forEach(l => {
        html += `<div class="grid-level buy"><span>BUY Lv${l.level || '?'}</span><span>$${l.price?.toLocaleString() || '--'}</span><span>${l.qty?.toFixed(6) || '--'}</span></div>`;
    });
    container.innerHTML = html;
}

// ── Helpers ──────────────────────────────────────────────────
function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

function setPnlText(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = `${value >= 0 ? '+' : ''}$${value.toFixed(4)}`;
    el.className = el.className.replace(/positive|negative/g, '').trim() + ` ${value >= 0 ? 'positive' : 'negative'}`;
}

function setVal(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value;
}

function getVal(id) {
    const el = document.getElementById(id);
    return el ? el.value : '';
}

function updateProgressBar(id, value, limit) {
    const bar = document.getElementById(id);
    if (!bar) return;
    const pct = limit > 0 ? Math.min(100, (value / limit) * 100) : 0;
    bar.style.width = `${pct}%`;
    bar.className = 'progress-fill ' + (pct > 70 ? 'progress-danger' : pct > 40 ? 'progress-warning' : 'progress-safe');
}

// ── Boot ─────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    // Basic setup needed for login screen
    document.getElementById('btn-login').addEventListener('click', login);
    const passInput = document.getElementById('login-pass');
    if (passInput) {
        passInput.addEventListener('keypress', e => { if (e.key === 'Enter') login(); });
    }

    if (APP.token) {
        // Validate token
        api('/auth/me').then(data => {
            if (data && data.username) {
                document.getElementById('login-modal').style.display = 'none';
                initDashboard();
            } else {
                logout();
            }
        });
    }
});
