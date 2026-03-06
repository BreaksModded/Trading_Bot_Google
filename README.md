# 🤖 Grid Trading Bot — Bybit

**Bot de trading algorítmico** con estrategia de grid dinámico, dashboard web profesional, y gestión de riesgo avanzada.

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Bybit](https://img.shields.io/badge/Bybit-API_v5-F7A600?logo=data:image/svg+xml;base64,PHN2Zy8+&logoColor=white)](https://bybit.com)
[![Tests](https://img.shields.io/badge/Tests-93_passing-brightgreen)](tests/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

---

## ✨ Características

| Módulo | Descripción |
|--------|-------------|
| **Grid Dinámico Asimétrico** | Spacing basado en ATR con filtros ADX/EMA y spacing asimétrico adaptativo |
| **Grid Refresh Híbrido** | Re-centra la cuadrícula cuando el precio se aleja, con 5 puertas de seguridad anti-bagholding |
| **Interés Compuesto Dinámico**| Auto-reinversión de beneficios en el tamaño de las órdenes (Profit Reinvestment) |
| **Sizing Proporcional** | Órdenes como % del capital: crecen con tus ganancias, se reducen en drawdown |
| **3 Circuit Breakers** | Max Drawdown (15%), Daily Loss (1%), Price Movement (8%/h) |
| **Dashboard Web** | 8 paneles con dark-mode, charts en tiempo real, y control del bot |
| **Backtesting** | Motor event-driven con fees reales, slippage, y walk-forward testing |
| **Dead Man's Switch** | Proceso independiente que cancela órdenes si el bot se cae |
| **Telegram** | Alertas de trades, circuit breakers, resumen diario |
| **API REST** | FastAPI con JWT auth, WebSocket, y exportación CSV |
| **93+ Tests** | Cobertura de indicadores, riesgo, estrategia, DB, refresh, sizing, y API |

---

## 🏗️ Arquitectura

```
proyecto_bot/
├── config/               # Configuración centralizada (Pydantic Settings)
│   ├── settings.py       # 9 sub-configs con validación
│   └── defaults.json     # Parámetros por defecto del grid
├── core/                 # Motor de trading
│   ├── exchange.py       # Wrapper Bybit (REST + WebSocket)
│   ├── indicators.py     # ATR, ADX, EMA (pandas-ta)
│   ├── strategy.py       # Grid dinámico + filtros
│   ├── order_manager.py  # Ciclo de vida de órdenes
│   ├── grid_refresh.py   # Refresh híbrido con 5 safety gates
│   ├── dynamic_sizing.py # Sizing proporcional al capital (Phase I)
│   └── risk_manager.py   # Circuit breakers
├── data/                 # Persistencia
│   ├── database.py       # SQLite (WAL mode, 7 tablas)
│   └── models.py         # 15+ modelos Pydantic
├── services/             # Servicios auxiliares
│   ├── notifier.py       # Telegram con alertas formateadas
│   ├── dead_mans_switch.py  # Safety net independiente
│   ├── health_monitor.py # healthchecks.io
│   └── scheduler.py      # APScheduler para tareas periódicas
├── api/                  # Backend del dashboard
│   ├── app.py            # FastAPI principal
│   ├── middleware.py      # JWT auth (bcrypt + jose)
│   ├── websocket.py      # Real-time broadcasts
│   └── routes/           # 6 módulos de rutas
├── backtesting/          # Motor de backtesting
│   ├── data_loader.py    # Descarga OHLCV (ccxt + parquet cache)
│   ├── engine.py         # Simulador event-driven
│   └── reporter.py       # Métricas + verdicts
├── dashboard/            # Frontend web
│   ├── index.html        # 8 paneles + login modal
│   ├── css/styles.css    # Design system dark-mode
│   └── js/app.js         # API client + WebSocket + charts
├── tests/                # 112 tests (pytest)
├── main.py               # Entry point del bot
├── run_dashboard.py      # Entry point del dashboard
└── requirements.txt      # Dependencias
```

---

## ⚡ Quick Start

### 1. Clonar y configurar entorno

```bash
git clone <repo-url> && cd proyecto_bot
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configurar variables de entorno

```bash
cp .env.example .env
```

Editar `.env` con tus credenciales:

```env
# Bybit
BYBIT_API_KEY=tu_api_key
BYBIT_API_SECRET=tu_api_secret
BYBIT_TESTNET=true          # ⚠️ Empezar SIEMPRE en testnet

# Telegram (opcional)
TELEGRAM_BOT_TOKEN=tu_bot_token
TELEGRAM_CHAT_ID=tu_chat_id

# Dashboard
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=tu_password_seguro
JWT_SECRET_KEY=un_string_aleatorio_de_32_chars_minimo

# Phase G: Advanced Features (opcional)
GRID_ENABLE_PROFIT_REINVESTMENT=True
GRID_ENABLE_ASYMMETRIC_GRID=True
```

### 3. Ejecutar en testnet

```bash
# Bot de trading
python main.py --testnet

# Dashboard (en otra terminal)
python run_dashboard.py
# → http://localhost:8000
```

### 4. Ejecutar tests

```bash
python -m pytest tests/ -v
# ✅ 112 passed in ~7s
```

---

## 📊 Dashboard

El dashboard web ofrece 8 paneles con diseño dark-mode profesional:

| Panel | Funcionalidad |
|-------|---------------|
| **Overview** | KPIs en tiempo real, curva de equity, estado del bot |
| **Trading** | Gráfico de velas (Lightweight Charts), tabla de trades |
| **Grid** | Visualización de niveles buy/sell activos |
| **Performance** | Métricas agregadas, PnL diario, profit factor, Sharpe |
| **Risk** | Barras de progreso de circuit breakers, historial de activaciones |
| **Backtest** | Ejecutor async con progreso, resultados con verdict PASS/FAIL |
| **Config** | Edición en vivo de parámetros del grid |
| **Logs** | Terminal con filtro por nivel y búsqueda |

**Acceso:** `http://localhost:8000` → Login con credenciales de `.env`

---

## 🎯 Estrategia de Trading

### Grid Dinámico con ATR

```
         SELL Lv5  ─── $50,300
         SELL Lv4  ─── $50,240
         SELL Lv3  ─── $50,180
         SELL Lv2  ─── $50,120
         SELL Lv1  ─── $50,060
    ───── CENTRO ────── $50,000 ─────
         BUY Lv1   ─── $49,940
         BUY Lv2   ─── $49,880
         BUY Lv3   ─── $49,820
         BUY Lv4   ─── $49,760
         BUY Lv5   ─── $49,700
```

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `num_levels` | 5 | Niveles por lado (buy + sell) |
| `min_spacing_pct` | 0.6% | Separación mínima entre niveles |
| `atr_multiplier` | 1.5 | Multiplica ATR% para spacing dinámico |
| `order_size_usdt` | 25 | Tamaño de base en USDT por nivel |
| `adx_threshold` | 25 | Pausa el grid si ADX > este valor |
| `ema_fast/slow` | 50/200 | EMAs para detectar dirección del mercado |

**Fórmula de spacing base:** `max(min_spacing, ATR% × multiplier)`

### 🌟 Fase G: Interés Compuesto y Asimetría

El motor incluye dos capacidades avanzadas de escalado dinámico:

#### 1. Dynamic Profit Reinvestment (Interés Compuesto)

En lugar de operar con un tamaño estático, el `ReinvestmentEngine` capitaliza las ganancias escalando automáticamente el `order_size_usdt` en función del *Equity* libre.

- **Protección de Suelo (`reinvestment_min_baseline_floor_pct`)**: Evita que el bot reduzca su tamaño operativo por debajo de un umbral en mercados bajistas severos.
- **Cap de Crecimiento (`reinvestment_max_step_growth_pct`)**: Suaviza picos explosivos limitando el escalado abrupto por ciclo.

#### 2. Asymmetric Grid Bias

Con `GRID_ENABLE_ASYMMETRIC_GRID=True`, el bot desvincula el spacing de compras y ventas calculando la asimetría basada en la fuerza de la tendencia local (ADX).

- **En mercado bajista (Trend=SHORT)**: Expande el *Buy Spacing* para atrapar caídas profundas sin saturarse, y aprieta el *Sell Spacing* para huir rápido en el rebote.
- **En mercado alcista (Trend=LONG)**: Aprieta el *Buy Spacing* comprando caídas cortas, y expande el *Sell Spacing* dejando correr las ganancias.
*(Consulta el `TUNING_GUIDE.md` incluido para ejemplos de configuración avanzada).*

### Filtros de mercado

- **ADX < 25** → Mercado en rango → Grid ACTIVO ✅
- **ADX ≥ 25** → Tendencia fuerte → Grid PAUSADO ⏸️
- **EMA 50 > 200** → Sesgo alcista (aplica Asimetría Bullish)
- **EMA 50 < 200** → Sesgo bajista (aplica Asimetría Bearish)

---

## 🛡️ Gestión de Riesgo

| Circuit Breaker | Umbral | Acción |
|----------------|--------|--------|
| **Max Drawdown** | 15% desde el peak | 🔴 Emergency stop — cancela todo |
| **Daily Loss** | 1% del capital | ⏸️ Pausa 24h automática |
| **Price Movement** | 8% en 1 hora | 🔴 Emergency stop inmediato |

Todos los eventos se registran en la DB y se notifican por Telegram.

---

## 🔬 Backtesting

```bash
# Via API (ejecutando el dashboard)
curl -X POST http://localhost:8000/api/backtest/run \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"months": 3, "timeframe": "1h", "initial_capital": 150}'
```

**Criterios de aceptación:**

- PnL neto > 0
- Max drawdown < 15%
- Sharpe ratio > 1.0

Incluye: fees reales de Bybit (0.01%), slippage estimado, y opción de walk-forward testing.

---

## 🧪 Tests

```
tests/
├── conftest.py           # 8 fixtures compartidos
├── test_indicators.py    # 19 tests — ATR, ADX, EMA, validación
├── test_risk_manager.py  # 25 tests — circuit breakers, pause/resume
├── test_strategy.py      # 23 tests — grid levels, fills, condiciones
├── test_database.py      # 30 tests — CRUD, métricas, CSV export
└── test_api.py           # 23 tests — JWT auth, todas las rutas
```

```bash
python -m pytest tests/ -v          # Verbose
python -m pytest tests/ --tb=short  # Tracebacks cortos
```

---

## 🔧 API Reference

**Base URL:** `http://localhost:8000/api`
**Auth:** Bearer token JWT (`POST /api/auth/login`)

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/auth/login` | Login → JWT token |
| `GET` | `/auth/me` | Usuario actual |
| `GET` | `/dashboard/status` | Estado del bot + KPIs |
| `POST` | `/dashboard/bot/start\|stop\|pause\|emergency` | Control del bot |
| `GET` | `/trading/trades?limit=50` | Historial de trades |
| `GET` | `/trading/klines?interval=60` | Velas OHLCV |
| `GET` | `/trading/grid` | Estado del grid activo |
| `GET` | `/performance/metrics?period=7d` | Métricas de rendimiento |
| `GET` | `/performance/equity?days=30` | Curva de equity |
| `GET` | `/performance/export` | Exportar CSV (Koinly) |
| `GET` | `/config/current` | Configuración actual |
| `PUT` | `/config/update` | Actualizar parámetros |
| `POST` | `/backtest/run` | Ejecutar backtest async |
| `GET` | `/backtest/status` | Progreso del backtest |
| `GET` | `/logs/events?hours=24` | Event logs filtrados |
| `GET` | `/logs/risk-status` | Estado de riesgo actual |
| `WS` | `/ws` | WebSocket real-time |

---

## 🚀 Deployment (Hetzner VPS)

### Requisitos del servidor

- Ubuntu 22.04 LTS
- 2 vCPU / 4 GB RAM (CPX21 — ~€6/mes)
- Python 3.11+

### Servicios systemd

El bot y el dashboard se ejecutan como servicios independientes:

```bash
# Bot de trading
sudo systemctl start trading-bot
sudo systemctl enable trading-bot

# Dashboard API
sudo systemctl start trading-dashboard
sudo systemctl enable trading-dashboard

# Dead Man's Switch
sudo systemctl start trading-dms
sudo systemctl enable trading-dms
```

### Nginx reverse proxy

```nginx
server {
    listen 443 ssl;
    server_name tu-dominio.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

---

## ⚠️ Disclaimer

> Este software es **experimental** y se proporciona «tal cual». El trading de criptomonedas implica riesgo significativo de pérdida. No uses este bot con dinero que no puedas permitirte perder. **Prueba siempre en testnet primero.**

---

## 📄 Licencia

MIT License — ver [LICENSE](LICENSE) para detalles.
