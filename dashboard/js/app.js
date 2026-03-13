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
    selectedSymbol: 'Overview',
    selectedInterval: '60',
    activeSymbolsCache: [],
    username: localStorage.getItem('bot_user') || null,
    password: sessionStorage.getItem('bot_pass') || null,
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
        APP.username = user;
        APP.password = pass;
        localStorage.setItem('bot_token', APP.token);
        localStorage.setItem('bot_user', user);
        sessionStorage.setItem('bot_pass', pass);
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
    APP.username = null;
    APP.password = null;
    localStorage.removeItem('bot_token');
    localStorage.removeItem('bot_user');
    sessionStorage.removeItem('bot_pass');
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
    if (panel === 'overview') loadActivityFeed();
    if (panel === 'trading') loadTrades();
    if (panel === 'performance') loadPerformance();
    if (panel === 'risk') loadRiskData();
    if (panel === 'logs') loadLogs();
    if (panel === 'config') loadConfig();
    if (panel === 'grid') {
        if (APP.selectedSymbol === 'Overview' && APP.activeSymbolsCache.length > 0) {
            // Auto switch to first symbol if entering grid from overview
            switchSymbol(APP.activeSymbolsCache[0]);
        }
        loadGrid();
    }
}

// ── Symbol Selection ─────────────────────────────────────────
function renderSymbolTabs(symbols, botState = 'running') {
    if (!symbols || !Array.isArray(symbols)) return;
    APP.activeSymbolsCache = symbols;

    const container = document.getElementById('symbol-tabs-container');
    if (!container) return;

    let html = `<div class="symbol-tab ${APP.selectedSymbol === 'Overview' ? 'active' : ''}" data-symbol="Overview">Overview</div>`;

    symbols.forEach(sym => {
        const isActive = APP.selectedSymbol === sym;
        // In the future we can map specific symbol states, for now we mirror global state
        const dotClass = botState === 'running' ? 'active' : botState;
        html += `
            <div class="symbol-tab ${isActive ? 'active' : ''}" data-symbol="${sym}">
                ${sym} <span class="symbol-status-dot ${dotClass}"></span>
            </div>
        `;
    });

    container.innerHTML = html;

    // Attach listeners
    container.querySelectorAll('.symbol-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            const sym = tab.dataset.symbol;
            switchSymbol(sym);
        });
    });
}

function switchSymbol(symbol) {
    if (APP.selectedSymbol === symbol) return;
    APP.selectedSymbol = symbol;

    // Update active class on tabs
    document.querySelectorAll('.symbol-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.symbol === symbol);
    });

    // If we are currently on the trading panel, just reload the trades/chart
    if (APP.currentPanel === 'trading') {
        loadTrades();
        return;
    }

    // If "Overview", switch to Overview panel, else switch to Grid panel to show the DOM
    if (symbol === 'Overview') {
        switchPanel('overview');
    } else {
        if (APP.currentPanel !== 'grid') switchPanel('grid');
        else loadGrid(); // force refresh
    }
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

    // Auth
    const btnLogout = document.getElementById('btn-logout');
    if (btnLogout) btnLogout.addEventListener('click', logout);

    // Performance panel — period selector
    document.querySelectorAll('#perf-period-selector .period-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#perf-period-selector .period-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            loadPerformance(btn.dataset.period);
        });
    });

    // Trading panel — kline interval selector (1M/5M/15M/1H/4H)
    document.querySelectorAll('#trading-period-selector .period-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#trading-period-selector .period-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            APP.selectedInterval = btn.dataset.interval;
            loadTrades();
        });
    });

    // Overview panel — equity chart period selector (7D/30D/90D)
    document.querySelectorAll('#equity-period-selector .period-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#equity-period-selector .period-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            const daysMap = { '7d': 7, '30d': 30, '90d': 90 };
            loadEquityChart(daysMap[btn.dataset.period] || 30);
        });
    });
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
    if (APP.currentPanel === 'overview') {
        loadActivityFeed();
    }
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

    // Update Symbol Tabs
    if (data.active_symbols) {
        renderSymbolTabs(data.active_symbols, status);
    }

    // Update Portfolio Health Bar
    const healthText = document.getElementById('health-bar-text');
    if (healthText) {
        const activeCount = data.active_symbols ? data.active_symbols.length : 0;
        const dailyPnl = data.pnl?.daily || 0;
        const sign = dailyPnl >= 0 ? '+' : '';
        const pnlStr = `$${dailyPnl.toFixed(2)}`;

        if (status === 'running') {
            healthText.innerHTML = `— ${activeCount} pares operando — Beneficio Hoy: <span style="color:${dailyPnl >= 0 ? 'var(--green)' : 'var(--red)'}">${sign}${pnlStr}</span>`;
        } else {
            healthText.innerHTML = `— Bot ${status === 'paused' ? 'Pausado' : 'Detenido'}`;
        }
    }
}

function updateKPIs(data) {
    const pnl = data.pnl || {};
    const capital = data.capital || {};

    setText('kpi-capital', `$${(capital.current || 0).toFixed(2)}`);

    setPnlText('kpi-pnl-24h', pnl.daily || 0);
    setPnlText('kpi-pnl-total', pnl.total || 0);
    setText('kpi-drawdown', `${(data.drawdown_pct || 0).toFixed(2)}%`);

    const ddBar = document.getElementById('drawdown-bar');
    if (ddBar) {
        const ddPct = data.drawdown_pct || 0;
        const ddLimit = 15;
        ddBar.style.width = `${Math.min(100, (ddPct / ddLimit) * 100)}%`;
        ddBar.className = 'progress-fill ' + (ddPct > ddLimit * 0.7 ? 'progress-danger' : ddPct > ddLimit * 0.4 ? 'progress-warning' : 'progress-safe');
    }

    setText('kpi-trades-today', data.total_trades || 0);

    // Render Per-Pair Mini Cards
    renderMiniCards(data);
}

// ── WebSocket ────────────────────────────────────────────────
function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${protocol}://${location.host}/ws?username=${encodeURIComponent(APP.username || '')}`;
    APP.ws = new WebSocket(wsUrl);

    APP.ws.onopen = () => {
        console.log('WebSocket connected');
        // FIX-4 Send auth message
        APP.ws.send(JSON.stringify({ 
            type: "auth", 
            password: APP.password 
        }));
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
        case 'overview':
            if (msg.data && APP.currentPanel === 'grid') {
                updateGridPanel(msg.data);
            }
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

        new ResizeObserver(() => {
            if (eqContainer.clientWidth > 0) {
                chart.applyOptions({
                    width: eqContainer.clientWidth,
                    height: eqContainer.clientHeight || 400
                });
            }
        }).observe(eqContainer);

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

        new ResizeObserver(() => {
            if (priceContainer.clientWidth > 0) {
                chart.applyOptions({
                    width: priceContainer.clientWidth,
                    height: priceContainer.clientHeight || 400
                });
            }
        }).observe(priceContainer);
    }

    // Daily PnL histogram (performance panel)
    const dailyPnlContainer = document.getElementById('daily-pnl-chart');
    if (dailyPnlContainer && typeof LightweightCharts !== 'undefined') {
        const chart = LightweightCharts.createChart(dailyPnlContainer, chartOptions(dailyPnlContainer));
        APP.charts.dailyPnl = chart.addHistogramSeries({
            priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
            color: '#00c087',
        });
        new ResizeObserver(() => {
            if (dailyPnlContainer.clientWidth > 0) {
                chart.applyOptions({
                    width: dailyPnlContainer.clientWidth,
                    height: dailyPnlContainer.clientHeight || 280,
                });
            }
        }).observe(dailyPnlContainer);
    }

    // Equity evolution chart (performance panel)
    const perfEqContainer = document.getElementById('perf-equity-chart');
    if (perfEqContainer && typeof LightweightCharts !== 'undefined') {
        const chart = LightweightCharts.createChart(perfEqContainer, chartOptions(perfEqContainer));
        APP.charts.perfEquity = chart.addAreaSeries({
            topColor: 'rgba(59, 130, 246, 0.3)',
            bottomColor: 'rgba(59, 130, 246, 0.01)',
            lineColor: '#3b82f6',
            lineWidth: 2,
        });
        new ResizeObserver(() => {
            if (perfEqContainer.clientWidth > 0) {
                chart.applyOptions({
                    width: perfEqContainer.clientWidth,
                    height: perfEqContainer.clientHeight || 280,
                });
            }
        }).observe(perfEqContainer);
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

async function loadPerfCharts() {
    // Daily PnL histogram
    if (APP.charts.dailyPnl) {
        const pnlData = await api('/performance/daily-pnl?days=30');
        if (pnlData && pnlData.data && pnlData.data.length > 0) {
            const bars = pnlData.data.map(d => ({
                time: d.date,
                value: d.pnl,
                color: d.pnl >= 0 ? '#00c087' : '#ef4444',
            }));
            APP.charts.dailyPnl.setData(bars);
        }
    }

    // Performance equity curve
    if (APP.charts.perfEquity) {
        const eqData = await api('/performance/equity?days=30');
        if (eqData && eqData.points && eqData.points.length > 0) {
            const series = eqData.points.map(p => ({
                time: new Date(p.timestamp).getTime() / 1000,
                value: p.capital,
            }));
            APP.charts.perfEquity.setData(series);
        }
    }
}

// ── Panel Loaders ────────────────────────────────────────────
async function loadTrades() {
    // Update chart title dynamically immediately
    const chartTitleContainer = document.querySelector('#panel-trading .card-header .card-title');
    if (chartTitleContainer) {
        chartTitleContainer.textContent = `📈 ${APP.selectedSymbol} Price Chart`;
    }

    // Load price chart
    const klinesParams = APP.selectedSymbol !== 'Overview' ? `&symbol=${APP.selectedSymbol}` : '';
    const interval = APP.selectedInterval || '60';
    const klines = await api(`/trading/klines?interval=${interval}&limit=200${klinesParams}`);
    if (APP.charts.price) {
        if (klines && klines.klines && klines.klines.length > 0) {
            const series = klines.klines.map(k => {
                const timeVal = (typeof k.timestamp === 'string' ? new Date(k.timestamp).getTime() : k.timestamp) / 1000;
                return {
                    time: timeVal,
                    open: k.open, high: k.high, low: k.low, close: k.close,
                };
            });
            APP.charts.price.setData(series);
        } else {
            console.log(`[Chart] No klines returned for ${APP.selectedSymbol}`);
            APP.charts.price.setData([]);
        }
    }

    // Load recent trades
    const symParam = APP.selectedSymbol !== 'Overview' ? `&symbol=${APP.selectedSymbol}` : '';
    const data = await api(`/trading/trades?limit=50${symParam}`);
    const tbody = document.getElementById('trades-table-body');
    if (!tbody) return;

    if (!data || !data.trades || data.trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted);">Sin trades</td></tr>';
        return;
    }

    tbody.innerHTML = data.trades.map(t => `
    <tr>
      <td>${new Date(t.timestamp).toLocaleString()}</td>
      <td><span class="tag tag-${t.side.toLowerCase()}">${t.side === 'Buy' || t.side === 'BUY' ? 'Compra 🟢' : 'Venta 🔴'}</span></td>
      <td>$${t.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>
      <td>${t.qty.toFixed(6)}</td>
      <td>$${t.fee.toFixed(4)}</td>
      <td style="color:${t.pnl >= 0 ? 'var(--green)' : 'var(--red)'};">${t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(4)}</td>
    </tr>
  `).join('');
}

async function loadPerformance(period = '7d') {
    const data = await api(`/performance/metrics?period=${period}`);
    if (!data) return;

    // ── KPI cards ────────────────────────────────────────────
    setText('perf-total-trades', data.total_trades || 0);
    setPnlText('perf-net-pnl', data.pnl_total || 0);
    setText('perf-win-rate', `${(data.win_rate || 0).toFixed(1)}%`);
    setText('perf-profit-factor', (data.profit_factor || 0).toFixed(2));
    setText('perf-sharpe', (data.sharpe_ratio || 0).toFixed(2));
    setText('perf-max-dd', `${(data.drawdown_pct || 0).toFixed(2)}%`);

    // ── Detailed stats grid ───────────────────────────────────
    const statsEl = document.getElementById('perf-detailed-stats');
    if (statsEl) {
        const avgPnl = data.average_pnl || 0;
        const pnlDaily = data.pnl_daily || 0;
        const winColor = avgPnl >= 0 ? 'var(--green)' : 'var(--red)';
        const dailyColor = pnlDaily >= 0 ? 'var(--green)' : 'var(--red)';
        statsEl.innerHTML = `
            <div class="stat-item"><span class="stat-label">Operaciones Ejecutadas</span><span class="stat-value">${data.total_executions || 0}</span></div>
            <div class="stat-item"><span class="stat-label">Ganadoras / Perdedoras</span><span class="stat-value">${data.winners || 0} / ${data.losers || 0}</span></div>
            <div class="stat-item"><span class="stat-label">PnL Medio / Operación</span><span class="stat-value" style="color:${winColor}">${avgPnl >= 0 ? '+' : ''}$${avgPnl.toFixed(4)}</span></div>
            <div class="stat-item"><span class="stat-label">Beneficio Bruto</span><span class="stat-value positive">+$${(data.gross_profit || 0).toFixed(2)}</span></div>
            <div class="stat-item"><span class="stat-label">Pérdida Bruta</span><span class="stat-value negative">-$${(data.gross_loss || 0).toFixed(2)}</span></div>
            <div class="stat-item"><span class="stat-label">PnL Últimas 24h</span><span class="stat-value" style="color:${dailyColor}">${pnlDaily >= 0 ? '+' : ''}$${pnlDaily.toFixed(2)}</span></div>
        `;
    }

    // ── Charts (always show last 30 days for visual context) ─
    loadPerfCharts();
}

async function loadRiskData() {
    const data = await api('/logs/risk-status');
    if (!data || data.error) return;

    // Handle bot not running (no risk data persisted yet)
    if (data.available === false) {
        const alertsContainer = document.getElementById('risk-alerts-container');
        if (alertsContainer) {
            alertsContainer.innerHTML = `<div style="background:rgba(100,116,139,0.1); border:1px solid rgba(100,116,139,0.4); color:var(--text-muted); padding:12px; border-radius:6px; font-weight:600;">ℹ️ Risk data unavailable — bot is not running.</div>`;
        }
        return;
    }

    setText('risk-drawdown', `${(data.drawdown_pct || 0).toFixed(2)}%`);
    setText('risk-daily-loss', `${(data.daily_loss_pct || 0).toFixed(2)}%`);
    setText('risk-dd-limit', `${data.drawdown_limit_pct || 15}%`);
    setText('risk-daily-limit', `${data.daily_loss_limit_pct || 1}%`);
    updateProgressBar('risk-dd-bar', data.drawdown_pct, data.drawdown_limit_pct);
    updateProgressBar('risk-daily-bar', data.daily_loss_pct, data.daily_loss_limit_pct);

    const priceMove1h = data.price_move_1h || 0;
    const priceMoveLimit = data.price_move_limit_pct || 8;
    setText('risk-price-move', `${priceMove1h.toFixed(2)}%`);
    setText('risk-price-limit', `${priceMoveLimit}%`);
    updateProgressBar('risk-price-bar', priceMove1h, priceMoveLimit);

    // 4.2 Alert Center & Emergency Banners
    const alertsContainer = document.getElementById('risk-alerts-container');
    if (alertsContainer) {
        let alertsHtml = '';
        if (data.drawdown_pct > (data.drawdown_limit_pct * 0.8)) {
            alertsHtml += `<div style="background:rgba(239, 68, 68, 0.1); border:1px solid var(--red); color:var(--red); padding:12px; border-radius:6px; font-weight:600;">⚠️ ALERTA: Drawdown cerca del límite preventivo. Sistema listo para pausar.</div>`;
        }
        if (data.daily_loss_pct > (data.daily_loss_limit_pct * 0.8)) {
            alertsHtml += `<div style="background:rgba(239, 68, 68, 0.1); border:1px solid var(--red); color:var(--red); padding:12px; border-radius:6px; font-weight:600;">⚠️ ALERTA: Pérdida diaria cerca del máximo admitido (Circuit Breaker).</div>`;
        }
        if (data.price_shock_paused) {
            alertsHtml += `<div style="background:rgba(245, 158, 11, 0.1); border:1px solid #f59e0b; color:#f59e0b; padding:12px; border-radius:6px; font-weight:600;">⏸ PAUSA DE VOLATILIDAD: Nuevos grids bloqueados. Órdenes existentes activas. Reanudación automática.</div>`;
        }
        if (alertsHtml === '') {
            alertsHtml = `<div style="background:rgba(0, 192, 135, 0.1); border:1px solid var(--green); color:var(--green); padding:12px; border-radius:6px; font-weight:600;">✅ Sistema Estable: El bot está gestionando el riesgo automáticamente.</div>`;
        }
        alertsContainer.innerHTML = alertsHtml;
    }

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
          <td>${(e.threshold * 100).toFixed(2)}%</td>
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
async function loadGrid() {
    const data = await api('/dashboard/status');
    if (data && data.grid_levels) {
        updateGridPanel(data);
    } else {
        updateGridPanel(null);
    }
}

function updateGridPanel(data) {
    if (!data) return;

    // Filter levels by Selected Symbol
    const allLevels = data.grid_levels || [];
    const levels = allLevels.filter(l => l.symbol === APP.selectedSymbol);

    const sells = levels.filter(l => l.side === 'Sell' || l.side === 'SELL').reverse();
    const buys = levels.filter(l => l.side === 'Buy' || l.side === 'BUY');

    setText('grid-levels-count', (sells.length + buys.length) || '--');
    setText('grid-active-orders', levels.filter(l => l.status === 'pending').length || '--');

    // Populate grid spacing if available
    let spacingStr = '--';
    if (data.grid_states && data.grid_states.length > 0) {
        const state = data.grid_states.find(s => s.symbol === APP.selectedSymbol) || data.grid_states[0];
        if (state && state.spacing_pct !== undefined) {
            spacingStr = `${(state.spacing_pct * 100).toFixed(2)}%`;
        }
    }
    setText('grid-spacing', spacingStr);

    // Populate indicators if available
    let targetSymbol = APP.selectedSymbol;
    if (targetSymbol === 'Overview' && data.active_symbols && data.active_symbols.length > 0) {
        targetSymbol = data.active_symbols[0]; // fallback so it doesn't break
    }

    let currentPrice = 0;
    if (data.latest_indicators && targetSymbol) {
        const inds = data.latest_indicators[targetSymbol];
        if (inds) {
            currentPrice = inds.current_price || 0;
            setText('grid-center', `$${inds.current_price ? inds.current_price.toLocaleString() : '--'}`);

            // 3.2 Translated Stats
            const adx = inds.adx || 0;
            const atrPct = (inds.atr_pct || 0) * 100;
            const trend = inds.trend; // 'long', 'short', 'neutral'
            const rsi = inds.rsi !== undefined && inds.rsi !== null ? parseFloat(inds.rsi) : null;
            const regime = inds.regime || 'transitional';

            let volatilityStr = 'baja';
            if (atrPct > 4) volatilityStr = 'extrema';
            else if (atrPct > 2) volatilityStr = 'alta';
            else if (atrPct > 1) volatilityStr = 'moderada';

            let forceStr = 'Débil';
            if (adx > 40) forceStr = 'Muy Fuerte';
            else if (adx > 25) forceStr = 'Fuerte';

            let trendStr = 'Neutral';
            let trendColor = 'var(--text-muted)';
            if (trend === 'long') { trendStr = 'Alcista'; trendColor = 'var(--green)'; }
            else if (trend === 'short') { trendStr = 'Bajista'; trendColor = 'var(--red)'; }

            const regimeMap = {
                'ranging': 'Lateral (Rango)',
                'trending_up': 'Tendencia Alcista',
                'trending_down': 'Tendencia Bajista',
                'transitional': 'Transición'
            };
            const regimeText = regimeMap[regime] || 'Desconocido';

            let regimeColor = 'var(--blue)';
            if (regime === 'ranging') regimeColor = 'var(--orange)';
            else if (regime === 'trending_up') regimeColor = 'var(--green)';
            else if (regime === 'trending_down') regimeColor = 'var(--red)';

            const translatedTrendEl = document.getElementById('translated-trend');
            if (translatedTrendEl) {
                translatedTrendEl.textContent = regimeText;
                translatedTrendEl.style.color = regimeColor;
                translatedTrendEl.style.borderColor = regimeColor;
            }

            const rsiText = rsi !== null ? rsi.toFixed(1) : '--';

            const summaryEl = document.getElementById('translated-summary');
            if (summaryEl) {
                summaryEl.innerHTML = `Régimen: <strong style="color:${regimeColor}">${regimeText}</strong>. El mercado actual presenta fuerza <strong>${forceStr.toLowerCase()}</strong> (ADX: ${adx.toFixed(1)}, RSI: ${rsiText}). Volatilidad: <strong>${volatilityStr}</strong> (ATR ${atrPct.toFixed(2)}%).`;
                summaryEl.style.borderLeftColor = regimeColor;
            }
        }
    }

    // 3.1 Visual DOM Ladder
    const container = document.getElementById('dom-ladder-container');
    if (!container) return;

    if (!sells.length && !buys.length) {
        container.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:40px;">Sin grid activo para este par</div>';
        _updateFcBanner(data, currentPrice);
        return;
    }

    let html = '';

    // Calculate max qty for progress bar scaling
    const maxQty = Math.max(...levels.map(l => parseFloat(l.qty) || 0));

    sells.forEach(l => {
        const price = parseFloat(l.price) || 0;
        const qty = parseFloat(l.qty) || 0;
        const fillPct = maxQty > 0 ? (qty / maxQty) * 100 : 0;
        const distanceStr = currentPrice > 0 ? `a ${((price - currentPrice) / currentPrice * 100).toFixed(2)}%` : '';

        html += `
        <div style="position:relative; display:flex; justify-content:space-between; padding:8px 12px; margin-bottom:2px; background:rgba(239, 68, 68, 0.05); border-radius:4px; overflow:hidden;">
            <div style="position:absolute; top:0; right:0; bottom:0; width:${fillPct}%; background:rgba(239, 68, 68, 0.15); z-index:0;"></div>
            <div style="position:relative; z-index:1; display:flex; flex-direction:column; gap:2px;">
                <span style="font-weight:700; color:var(--red);">Venta en $${price.toLocaleString()}</span>
                <span style="font-size:0.75rem; color:var(--text-muted);">${distanceStr} (Nivel ${l.level_id || '?'})</span>
            </div>
            <div style="position:relative; z-index:1; font-weight:600; font-family:var(--font-mono);">
                ${qty.toFixed(6)}
            </div>
        </div>`;
    });

    html += `
    <div style="display:flex; justify-content:center; align-items:center; padding:12px 0; margin:4px 0; border-top:1px dashed rgba(255,255,255,0.1); border-bottom:1px dashed rgba(255,255,255,0.1);">
        <span style="background:var(--blue-dim); color:var(--blue); padding:4px 12px; border-radius:12px; font-weight:700; font-size:0.85rem;">
            PRECIO ACTUAL: $${currentPrice.toLocaleString(undefined, { maximumFractionDigits: 2 })}
        </span>
    </div>`;

    buys.forEach(l => {
        const price = parseFloat(l.price) || 0;
        const qty = parseFloat(l.qty) || 0;
        const fillPct = maxQty > 0 ? (qty / maxQty) * 100 : 0;
        const distanceStr = currentPrice > 0 ? `a ${((currentPrice - price) / currentPrice * 100).toFixed(2)}%` : '';

        html += `
        <div style="position:relative; display:flex; justify-content:space-between; padding:8px 12px; margin-bottom:2px; background:rgba(0, 192, 135, 0.05); border-radius:4px; overflow:hidden;">
            <div style="position:absolute; top:0; left:0; bottom:0; width:${fillPct}%; background:rgba(0, 192, 135, 0.15); z-index:0;"></div>
            <div style="position:relative; z-index:1; display:flex; flex-direction:column; gap:2px;">
                <span style="font-weight:700; color:var(--green);">Compra en $${price.toLocaleString()}</span>
                <span style="font-size:0.75rem; color:var(--text-muted);">${distanceStr} (Nivel ${l.level_id || '?'})</span>
            </div>
            <div style="position:relative; z-index:1; font-weight:600; font-family:var(--font-mono);">
                ${qty.toFixed(6)}
            </div>
        </div>`;
    });

    container.innerHTML = html;
    _updateFcBanner(data, currentPrice);
}

function _updateFcBanner(data, currentPrice) {
    const fcBannerEl = document.getElementById('fc-banner');
    if (!fcBannerEl) return;
    const pos = (data && data.positions && data.positions[APP.selectedSymbol]) || {};
    const posQty = pos.qty || 0;
    const posAvg = pos.avg_cost || 0;
    if (posQty > 0.000001) {
        const botStatus = APP._lastBotStatus || 'stopped';
        const disabled = botStatus !== 'running';
        const pnlStr = currentPrice > 0 && posAvg > 0
            ? (() => { const p = (currentPrice - posAvg) * posQty; return ` | PnL: <span style="color:${p >= 0 ? 'var(--green)' : 'var(--red)'}">$${p.toFixed(2)}</span>`; })()
            : '';
        fcBannerEl.style.display = 'block';
        fcBannerEl.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
                <span style="font-size:0.8rem;">
                    Posición abierta: <strong>${posQty.toFixed(6)}</strong> | Avg: <strong>$${posAvg.toLocaleString(undefined,{maximumFractionDigits:4})}</strong>${pnlStr}
                </span>
                <button
                    onclick="${disabled ? '' : `forceCloseSymbol('${APP.selectedSymbol}', ${posQty}, ${posAvg}, ${currentPrice})`}"
                    ${disabled ? 'disabled title="El bot debe estar RUNNING"' : ''}
                    style="padding:5px 14px;font-size:0.75rem;font-weight:700;text-transform:uppercase;border:none;border-radius:4px;cursor:${disabled ? 'not-allowed' : 'pointer'};background:${disabled ? 'rgba(239,68,68,0.25)' : 'rgba(239,68,68,0.85)'};color:${disabled ? 'rgba(255,255,255,0.4)' : '#fff'};">
                    Force Close
                </button>
            </div>`;
    } else {
        fcBannerEl.style.display = 'none';
    }
}

function renderSymbolTabs(symbols, status) {
    const container = document.getElementById('symbol-tabs-container');
    if (!container || !symbols || !symbols.length) return;

    let html = '';
    // Always add the Overview tab first
    const overviewActive = APP.selectedSymbol === 'Overview' ? 'active' : '';
    html += `<div class="symbol-tab ${overviewActive}" data-symbol="Overview" onclick="switchSymbol('Overview')">Overview</div>`;

    // Add a tab for each active symbol
    symbols.forEach(sym => {
        const isActive = APP.selectedSymbol === sym ? 'active' : '';
        const ledClass = status === 'running' ? 'status-led running' : 'status-led ' + status;
        html += `
        <div class="symbol-tab ${isActive}" data-symbol="${sym}" onclick="switchSymbol('${sym}')">
            ${sym}
            <div class="${ledClass}" style="width:6px;height:6px;display:inline-block;margin-left:6px;box-shadow:none;"></div>
        </div>`;
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
    el.textContent = `${value >= 0 ? '+' : ''}$${value.toFixed(2)}`;
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

// ── Force Close ───────────────────────────────────────────────
async function forceCloseSymbol(symbol, qty, avgCost, currentPrice) {
    const botStatus = APP._lastBotStatus || 'stopped';
    if (botStatus !== 'running') {
        alert('El bot no está en estado RUNNING. No se puede enviar force close.');
        return;
    }

    const pnlUsdt = currentPrice > 0 && avgCost > 0
        ? ((currentPrice - avgCost) * qty)
        : null;
    const pnlStr = pnlUsdt !== null
        ? `\nPnL estimado: $${pnlUsdt.toFixed(2)} (${pnlUsdt >= 0 ? 'ganancia' : 'pérdida'})`
        : '';

    const msg = `⚠️ FORCE CLOSE: ${symbol}\n\n` +
        `Posición: ${qty.toFixed(8)} unidades\n` +
        `Costo promedio: $${avgCost.toLocaleString(undefined, { maximumFractionDigits: 4 })}\n` +
        `Precio actual: $${currentPrice > 0 ? currentPrice.toLocaleString(undefined, { maximumFractionDigits: 2 }) : 'desconocido'}` +
        pnlStr +
        `\n\n¿Confirmar venta al mercado y resetear grid? (~15s)`;

    if (!confirm(msg)) return;

    const result = await api(`/trading/force-close/${symbol}`, { method: 'POST' });
    if (result && result.status === 'command_queued') {
        _showToast(`Force close enviado para ${symbol}. Ejecutando en ~15s.`);
    } else {
        alert(`Error al enviar force close: ${result?.message || 'Error desconocido'}`);
    }
}

function _showToast(message) {
    let toast = document.getElementById('_fc-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = '_fc-toast';
        toast.style.cssText = 'position:fixed;bottom:24px;right:24px;background:#1e40af;color:#fff;padding:12px 20px;border-radius:8px;font-size:0.85rem;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,0.4);transition:opacity 0.4s;';
        document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.style.opacity = '1';
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { toast.style.opacity = '0'; }, 4000);
}

// ── New UX Redesign Features ──────────────────────────────────
function renderMiniCards(data) {
    const container = document.getElementById('mini-cards-container');
    if (!container || !data.active_symbols) return;

    const positions = data.positions || {};
    const botStatus = data.bot_state?.status || 'stopped';
    APP._lastBotStatus = botStatus;

    let html = '';
    data.active_symbols.forEach(sym => {
        const inds = (data.latest_indicators || {})[sym] || {};
        const pos = positions[sym] || {};
        const priceStr = inds.current_price ? `$${inds.current_price.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : '--';
        const trend = inds.trend === 'long' ? 'alcista' : (inds.trend === 'short' ? 'bajista' : 'neutral');
        const color = inds.trend === 'long' ? 'var(--green)' : (inds.trend === 'short' ? 'var(--red)' : 'var(--text-muted)');

        const qty = pos.qty || 0;
        const avgCost = pos.avg_cost || 0;
        const currentPrice = inds.current_price || 0;
        const hasPosition = qty > 0.000001;

        let fcBtn = '';
        if (hasPosition) {
            const disabled = botStatus !== 'running';
            const title = disabled ? 'El bot debe estar RUNNING para ejecutar force close' : `Vender ${qty.toFixed(6)} al mercado`;
            fcBtn = `<button
                onclick="${disabled ? '' : `forceCloseSymbol('${sym}', ${qty}, ${avgCost}, ${currentPrice})`}"
                ${disabled ? 'disabled' : ''}
                title="${title}"
                style="margin-top:4px;width:100%;padding:4px 0;font-size:0.7rem;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;border:none;border-radius:4px;cursor:${disabled ? 'not-allowed' : 'pointer'};background:${disabled ? 'rgba(239,68,68,0.2)' : 'rgba(239,68,68,0.8)'};color:${disabled ? 'rgba(255,255,255,0.4)' : '#fff'};">
                Force Close (${qty.toFixed(4)})
            </button>`;
        }

        html += `
        <div class="kpi-card" style="flex:1; min-width:180px; display:flex; flex-direction:column; gap:8px;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <span style="font-weight:700; font-size:1rem;">${sym}</span>
                <span style="font-size:0.7rem; color:${color}; background:rgba(255,255,255,0.05); padding:2px 6px; border-radius:4px; text-transform:uppercase;">${trend}</span>
            </div>
            <div style="display:flex; justify-content:space-between; font-size:0.8rem; color:var(--text-secondary);">
                <span>Precio: ${priceStr}</span>
                <span>ADX: ${inds.adx ? inds.adx.toFixed(1) : '--'}</span>
            </div>
            ${fcBtn}
        </div>
        `;
    });
    container.innerHTML = html;
}

async function loadActivityFeed() {
    const data = await api('/trading/trades?limit=15');
    const container = document.getElementById('activity-feed-container');
    if (!container || !data || !data.trades) return;

    if (data.trades.length === 0) {
        container.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:20px;">Sin actividad reciente</div>';
        return;
    }

    container.innerHTML = data.trades.map(t => {
        const timeStr = new Date(t.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const sideStr = t.side === 'BUY' || t.side === 'Buy' ? 'Compró' : 'Vendió';
        const emoji = t.side === 'BUY' || t.side === 'Buy' ? '🟢' : '🔴';
        const pnlStr = t.pnl ? ` — ${t.pnl >= 0 ? 'ganó' : 'perdió'} <span style="color:${t.pnl >= 0 ? 'var(--green)' : 'var(--red)'}">$${Math.abs(t.pnl).toFixed(2)}</span>` : '';

        return `
        <div style="padding:10px 14px; background:var(--bg-surface); border-radius:8px; border:1px solid rgba(255,255,255,0.02); display:flex; align-items:center; gap:12px;">
            <span style="font-size:1.1rem; filter:grayscale(0.5);">${emoji}</span>
            <div style="flex:1; display:flex; flex-direction:column; gap:2px;">
                <span style="font-size:0.75rem; color:var(--text-muted);">${timeStr}</span>
                <span style="font-size:0.9rem;">
                    ${sideStr} <strong>${t.qty.toFixed(6)} ${t.symbol || 'BTC'}</strong> a $${t.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                    ${pnlStr}
                </span>
            </div>
        </div>
        `;
    }).join('');
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
                APP.username = localStorage.getItem('bot_user');
                APP.password = localStorage.getItem('bot_pass');
                document.getElementById('login-modal').style.display = 'none';
                initDashboard();
            } else {
                logout();
            }
        });
    }
});
