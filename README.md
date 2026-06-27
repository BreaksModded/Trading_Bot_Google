# 🤖 Bot de Trading de Futuros — Bybit

Bot algorítmico **regime-switching** para perpetuos lineales de Bybit (USDT-margined),
con dashboard web "Editorial" y kill-switch de cuenta.

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Bybit](https://img.shields.io/badge/Bybit-API_v5_linear-F7A600)](https://bybit.com)
[![Tests](https://img.shields.io/badge/Tests-100_passing-brightgreen)](tests/)

> El bot de **spot** anterior (`main.py`, grid spot) queda como **legacy**. El sistema
> vivo es el de **futuros** (`main_futures.py`).

---

## Qué hace

Cada ciclo (~10 s) clasifica el régimen de mercado y cambia de estrategia:

| Régimen | Detección | Estrategia |
|---|---|---|
| **RANGING** | ADX bajo | **Grid neutral** (long debajo / short encima del mid) |
| **TRENDING_UP/DOWN** | ADX alto + dirección EMA | **Tendencia**: long/short con sizing *fixed-fractional* + stop Chandelier |
| **TRANSITIONAL** | ADX entre umbrales | Al margen (flat) |

- **Sizing fixed-fractional:** arriesga un % fijo del equity por operación, dimensionado desde la distancia al stop.
- **Stop Chandelier** (22 / 3×ATR) en modo tendencia (trailing, solo trinquetea a favor).
- **Confirmación de timeframe superior** (4H) para entrar en tendencia (corta whipsaws).
- **Kill-switch de cuenta:** aplana + HALT si la pérdida diaria o el drawdown total superan el límite, con persistencia anti *death-loop* (resume manual rebasa el pico).

---

## Arquitectura

Dos procesos independientes que comparten una SQLite (modo WAL):

- **Bot:** `main_futures.py` → `asyncio.run(run_bot())`.
- **Dashboard:** `run_dashboard.py` → `uvicorn api.app:app`.
- **IPC:** el bot persiste su estado en `runtime_config` (`futures_state`, `futures_risk_status`) y en las tablas `trades` / `equity_curve` / `event_logs`; el dashboard los lee (no comparten memoria).

```
core/
├── regime.py            # clasificador de régimen (ADX + EMA)
├── grid.py              # geometría de la grid neutral
├── trend.py             # decide_trend() — MISMA lógica live == backtest, fixed-fractional, Chandelier
├── position_manager.py  # FuturesPositionManager (fills, partner orders del grid)
├── futures_risk.py      # kill-switch de cuenta (daily loss + drawdown, anti death-loop)
├── indicators.py        # ATR, ADX, EMA, Chandelier Exit
└── exchange.py          # wrapper Bybit v5 (REST + WebSocket)
main_futures.py          # orquestador del bot de futuros
api/
├── app.py               # FastAPI + sirve los estáticos del dashboard
├── routes/futures.py    # /api/futures/{overview,equity,trades,config,control}
└── middleware.py        # auth JWT + rate limiting
dashboard/
├── index.html           # shell + 6 pantallas (sistema "Editorial")
├── css/dashboard.css
├── js/app.js            # auth, capa de lectura, router, Lightweight Charts
└── assets/              # fuentes (Archivo + IBM Plex Mono) y Lightweight Charts AUTO-ALOJADOS
backtesting/             # motor de backtest fiel al bot (comparte decide_trend)
```

---

## Quick Start

```bash
# 1) Entorno + dependencias
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2) Configura el .env (ver sección Configuración)
cp .env.example .env   # y edítalo

# 3) Arranca (dos terminales)
python main_futures.py     # bot
python run_dashboard.py    # dashboard → http://localhost:8000
```

---

## Configuración (`.env`)

| Variable | Ejemplo | Notas |
|---|---|---|
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | … | clave **mainnet** con permiso **Lectura-Editar + Derivados/Contrato** (NO withdraw) |
| `BYBIT_TESTNET` | `false` | real (testnet = `true`) |
| `FUTURES_SYMBOL` | `ETHUSDT` | perpetuo lineal a operar |
| `FUTURES_LEVERAGE` | `5` | 1–10 (el bot lo fija al arrancar) |
| `FUTURES_RISK_PER_TRADE_PCT` | `0.015` | % del equity arriesgado por operación |
| `FUTURES_MAX_DAILY_LOSS_PCT` | `0.06` | kill-switch diario |
| `FUTURES_MAX_TOTAL_DRAWDOWN_PCT` | `0.20` | kill-switch por drawdown |
| `DASHBOARD_PASSWORD` / `JWT_SECRET_KEY` | … | seguridad del panel (no dejes los defaults) |
| `DATABASE_PATH` | `data/futures_bot.db` | SQLite del bot + dashboard |

> `config/settings.py` carga el `.env` con `load_dotenv()` antes de instanciar la config.

---

## Dashboard (sistema "Editorial")

6 pantallas, login JWT, charts con **Lightweight Charts**, fuentes y librería de gráficos
**auto-alojadas** (sin CDN), cero emojis:

| Pantalla | Contenido |
|---|---|
| **Resumen** | KPIs, posición y liquidación, gráfico de precio, régimen y decisión, gauges de riesgo, ops recientes |
| **Gráfico** | velas + selector de timeframe + curva de equity |
| **Operaciones** | tabla completa, filtros, búsqueda, export CSV |
| **Riesgo** | kill-switch, circuit breakers, riesgo de liquidación, historial |
| **Logs** | terminal de eventos con filtros |
| **Config** | parámetros vivos (solo lectura) |

Controles: **Reanudar / Aplanar / Parar** (cola de comandos que el bot consume cada ciclo).

---

## Riesgo

| Control | Umbral (default) | Acción |
|---|---|---|
| Pérdida diaria | 6 % | aplana + HALT (requiere *Reanudar* manual) |
| Drawdown total | 20 % desde el pico | aplana + HALT |
| Stop Chandelier | 3×ATR | cierra la posición de tendencia |
| Buffer de liquidación | — | la liquidación debe quedar más allá del stop |

---

## Tests

```bash
python -m pytest tests/unit -q     # 100 passing
```

---

## Deployment

- Bot y dashboard corren como **procesos/servicios independientes**.
- ⚠️ **El servicio del bot debe ejecutar `main_futures.py`** (no el `main.py` legacy de spot).
- Actualizar: `git pull` + **reiniciar ambos**. **Sin migración de BD** (`init_schema` crea/migra las tablas al arrancar). Sin dependencias ni `.env` nuevos.
- Nginx (opcional): proxy a `127.0.0.1:8000`.

---

## ⚠️ Disclaimer

Software **experimental**. El trading de futuros **con apalancamiento** puede **liquidar** tu
posición y amplifica pérdidas. No operes con dinero que no puedas permitirte perder.
