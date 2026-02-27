# Plan Maestro: Bot de Trading Algorítmico con 500€ — Versión Final (2026)

> **Versión consolidada** con todas las mejoras acordadas: grid dinámico ATR, filtros ADX, capital protegido, inicio en spot sin apalancamiento, y correcciones fiscales y técnicas.

---

## 1. ESTRATEGIA: GRID DINÁMICO CON FILTRO ADX/ATR

### Evaluación de viabilidad con 500€

| Estrategia | Viabilidad | Motivo |
|---|---|---|
| **Grid Trading Dinámico (ATR)** | ✅ Alta | Espaciado adaptativo, riesgo predecible, compatible con spot |
| **Mean Reversion** | ⚠️ Media | Requiere filtros de volumen adicionales para evitar cuchillos cayendo |
| **Scalping** | ❌ Inviable | Comisiones destruyen el margen con capital < 5.000€ |
| **Market Making** | ❌ Inviable | Requiere capital sostenido en ambas direcciones simultáneamente |
| **Arbitraje** | ❌ Inviable | Rentable solo con > 50.000€ y latencia colocada junto al exchange |
| **Momentum** | ❌ No ahora | Drawdowns agresivos peligrosos con capital pequeño sin experiencia previa |

### Estrategia seleccionada: Grid Dinámico basado en ATR con filtro ADX + EMA

**Justificación:**
- El grid puro fracasa en tendencias fuertes. Esta versión lo resuelve con tres capas de filtro:
  - **ATR (Average True Range):** Ajusta la distancia entre órdenes según la volatilidad real del mercado, evitando spacing demasiado estrecho que genera overtrading.
  - **ADX (Average Directional Index):** Si ADX > 25, el bot pausa la apertura de nuevas mallas (mercado en tendencia fuerte, no en rango).
  - **EMA 50/200:** Define el sesgo direccional. Cruce alcista = solo órdenes long. Cruce bajista = solo órdenes short.

- El spacing **mínimo del 0.6%** (o 1.5× ATR, lo que sea mayor) es el umbral correcto para que el margen bruto sea positivo tras comisiones + slippage real en BTC/USDT.

### Parámetros de riesgo

```
Capital total:              500€
Capital en exchange:        150€ (spot, sin apalancamiento)
Capital reserva (fuera):    350€ — no entra hasta validación de 3 meses
Asignación al grid:         150€ USDT
Apalancamiento inicial:     0x — SPOT únicamente
Número de niveles:          4-6 (ajustado al capital disponible)
Tamaño por orden:           20-30 USDT
Grid spacing mínimo:        0.6% o 1.5 × ATR (el mayor de los dos)
Drawdown máximo (circuit breaker): 15% del capital en exchange (22.5€)
Stop Loss hard en exchange: Obligatorio, no solo en el bot
```

**Cuándo pasar a futuros con apalancamiento:**
Solo tras cumplir los tres criterios siguientes de forma simultánea:
1. Mínimo 3 meses de producción real con PnL neto positivo
2. Drawdown máximo real < 10% en ese período
3. Cero errores operativos críticos (órdenes duplicadas, desincronización de posición, bugs de ejecución)

---

## 2. EXCHANGE Y API

### Recomendación: Bybit

| Criterio | Bybit | Binance | OKX |
|---|---|---|---|
| Licencia MiCA/UE | ✅ VASP Chipre (UE) | ⚠️ Polonia + tensiones regulatorias | ✅ Malta |
| Accesible desde España | ✅ | ✅ (con restricciones) | ✅ |
| KYC completo | ✅ Obligatorio | ✅ | ✅ |
| **Maker fee futuros** | **0.01%** | 0.02% | 0.02% |
| Taker fee futuros | 0.06% | 0.05% | 0.05% |
| Capital mínimo spot | ~1 USDT | ~1 USDT | ~1 USDT |
| API testnet funcional | ✅ Completa | ✅ | ⚠️ Limitada |
| Documentación API | ✅ Muy buena | ✅ Muy buena | ⚠️ Aceptable |
| WebSocket estable | ✅ Excelente | ✅ Excelente | ✅ Bueno |

**Por qué Bybit:**
- Maker fee del **0.01%** — el más bajo de los tres. Con capital pequeño, cada décima de porcentaje importa.
- Licencia dentro de la UE (Chipre) — la más sólida regulatoriamente para operar desde España bajo MiCA.
- Testnet completamente operativa para paper trading sin dinero real.

### Endpoints a usar

```
# Datos de mercado en tiempo real
WebSocket público:   wss://stream.bybit.com/v5/public/spot
  → Suscripciones: kline (velas 1m/5m), orderbook, ticker

# Ejecución de órdenes
REST privado:        https://api.bybit.com/v5/order/create
  → POST: crear orden limit
  → DELETE: cancelar orden
  → GET /position/list: estado de posición

# Eventos de ejecución (más eficiente que polling REST)
WebSocket privado:  wss://stream.bybit.com/v5/private
  → Eventos: order fill, balance change, position update
```

---

## 3. STACK TECNOLÓGICO

### Lenguaje: Python 3.12+

Ecosistema más maduro para trading algorítmico. Desarrollo 3× más rápido que Go/Rust para este nivel técnico y este caso de uso.

### Librerías

| Categoría | Librería | Uso |
|---|---|---|
| Exchange (ejecución) | `pybit 5.x` | SDK oficial Bybit — más estable para WebSocket privado y ejecución que ccxt |
| Exchange (datos históricos) | `ccxt` | Solo para descarga de OHLCV histórico en backtesting |
| Análisis técnico | `pandas-ta` | ATR, ADX, EMA en una línea |
| Base de datos | `SQLite` + `sqlite3` | Sin dependencias externas, suficiente para logs y estado del bot |
| Configuración | `python-dotenv` | API keys en `.env`, nunca hardcoded en el código |
| Alertas | `python-telegram-bot 20.x` | Notificaciones instantáneas al móvil |
| Logging | `loguru` | Más completo que el logging estándar |
| Scheduler | `APScheduler 3.x` | Tareas periódicas (health checks, force sync) |
| Monitorización | `healthchecks.io` (free tier) | Heartbeat externo — detecta si el bot se cae |
| Tests | `pytest` | Tests unitarios de la lógica de riesgo y ejecución |

> **Nota crítica:** `pybit` para ejecución, `ccxt` solo para históricos. No mezclar los dos para órdenes en vivo — `pybit` tiene mejor soporte de WebSocket privado y menos breaking changes en Bybit.

### Infraestructura

**Desarrollo y paper trading:** Local (tu máquina)

**Producción:** VPS en Frankfurt

```
Proveedor:    Hetzner Cloud (alemán, GDPR compliant)
Plan:         CX22 — 2 vCPU, 4GB RAM, 40GB SSD
Precio:       ~4.50€/mes
OS:           Ubuntu 22.04 LTS
Latencia a Bybit Frankfurt: ~15-30ms (aceptable para grid en spot)
```

```bash
# Gestión del proceso en producción (systemd — más robusto que screen)
sudo nano /etc/systemd/system/tradingbot.service

[Unit]
Description=Trading Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/trading_bot
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target

sudo systemctl enable tradingbot
sudo systemctl start tradingbot
```

---

## 4. ESTRUCTURA DEL PROYECTO

```
trading_bot/
├── .env                  # API_KEY, API_SECRET, TELEGRAM_TOKEN — nunca en Git
├── .gitignore            # Incluye .env y logs/
├── config.py             # Parámetros del grid, riesgo, símbolos
├── exchange.py           # Wrapper pybit — conexión, órdenes, balance
├── strategy.py           # Lógica del grid dinámico + filtros ADX/ATR/EMA
├── risk_manager.py       # Circuit breakers, position sizing, drawdown
├── database.py           # SQLite — estado persistente y log de trades
├── notifier.py           # Alertas Telegram
├── backtester.py         # Motor de backtesting con ccxt
├── dead_mans_switch.py   # Proceso independiente de seguridad
└── main.py               # Entry point + graceful shutdown
```

---

## 5. IMPLEMENTACIÓN TÉCNICA

### 5.1 Configuración central

```python
# config.py
GRID_CONFIG = {
    "symbol":               "BTCUSDT",
    "capital_usdt":         150,          # capital total asignado (spot)
    "num_levels":           5,            # niveles en cada dirección
    "min_spacing_pct":      0.006,        # 0.6% mínimo entre niveles
    "atr_multiplier":       1.5,          # spacing = max(0.6%, 1.5 × ATR%)
    "order_size_usdt":      25,           # tamaño por orden
    "leverage":             1,            # SPOT — sin apalancamiento
    "adx_threshold":        25,           # ADX > 25 → pausar grid
    "ema_fast":             50,
    "ema_slow":             200,
    "max_drawdown_pct":     0.15,         # 15% → circuit breaker total
    "max_daily_loss_pct":   0.01,         # 1% pérdida diaria → pausa 24h
}
```

### 5.2 Conexión al exchange

```python
# exchange.py
import os
from pybit.unified_trading import HTTP, WebSocket
from dotenv import load_dotenv

load_dotenv()

class BybitClient:
    def __init__(self, testnet: bool = True):
        self.testnet = testnet
        self.session = HTTP(
            testnet=testnet,
            api_key=os.getenv("API_KEY"),
            api_secret=os.getenv("API_SECRET"),
        )

    def get_balance(self) -> float:
        resp = self.session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        return float(resp["result"]["list"][0]["totalAvailableBalance"])

    def get_ticker(self, symbol: str) -> float:
        resp = self.session.get_tickers(category="spot", symbol=symbol)
        return float(resp["result"]["list"][0]["lastPrice"])

    def place_limit_order(self, symbol: str, side: str, qty: float, price: float) -> str:
        resp = self.session.place_order(
            category="spot",
            symbol=symbol,
            side=side,
            orderType="Limit",
            qty=str(qty),
            price=str(round(price, 1)),
            timeInForce="GTC",
        )
        return resp["result"]["orderId"]

    def cancel_all_orders(self, symbol: str):
        self.session.cancel_all_orders(category="spot", symbol=symbol)
```

### 5.3 Lógica del grid dinámico con filtros

```python
# strategy.py
import pandas_ta as ta
import pandas as pd

class GridStrategy:
    def __init__(self, config: dict, client):
        self.config = config
        self.client = client
        self.active_orders: dict = {}

    def compute_spacing(self, df: pd.DataFrame) -> float:
        """Calcula el spacing dinámico: max(mínimo fijo, 1.5 × ATR%)."""
        atr = ta.atr(df["high"], df["low"], df["close"], length=14).iloc[-1]
        current_price = df["close"].iloc[-1]
        atr_pct = (atr / current_price) * self.config["atr_multiplier"]
        return max(self.config["min_spacing_pct"], atr_pct)

    def get_filters(self, df: pd.DataFrame) -> dict:
        """Evalúa los tres filtros de tendencia."""
        adx_df  = ta.adx(df["high"], df["low"], df["close"], length=14)
        adx_val = adx_df["ADX_14"].iloc[-1]
        ema_fast = ta.ema(df["close"], length=self.config["ema_fast"]).iloc[-1]
        ema_slow = ta.ema(df["close"], length=self.config["ema_slow"]).iloc[-1]

        return {
            "is_trending":      adx_val > self.config["adx_threshold"],
            "adx":              adx_val,
            "trend_direction":  "Long" if ema_fast > ema_slow else "Short",
        }

    def should_operate(self, filters: dict) -> bool:
        """El bot solo opera si el mercado está en rango (no tendencia)."""
        if filters["is_trending"]:
            return False
        return True

    def compute_grid_levels(self, center_price: float, spacing: float) -> tuple:
        n = self.config["num_levels"]
        buy_levels  = [center_price * (1 - spacing * i) for i in range(1, n + 1)]
        sell_levels = [center_price * (1 + spacing * i) for i in range(1, n + 1)]
        return buy_levels, sell_levels

    def place_grid(self, df: pd.DataFrame):
        filters = self.get_filters(df)

        if not self.should_operate(filters):
            # Mercado en tendencia — no abrir nuevas mallas
            return

        spacing      = self.compute_spacing(df)
        center_price = df["close"].iloc[-1]
        qty = round(self.config["order_size_usdt"] / center_price, 5)
        buy_levels, sell_levels = self.compute_grid_levels(center_price, spacing)

        for price in buy_levels:
            order_id = self.client.place_limit_order(
                self.config["symbol"], "Buy", qty, price
            )
            self.active_orders[order_id] = {"price": price, "side": "Buy"}

        for price in sell_levels:
            order_id = self.client.place_limit_order(
                self.config["symbol"], "Sell", qty, price
            )
            self.active_orders[order_id] = {"price": price, "side": "Sell"}

    def on_order_filled(self, order_id: str, side: str, fill_price: float, df: pd.DataFrame):
        """Al ejecutarse una orden, coloca la orden contraria en el nivel siguiente."""
        spacing = self.compute_spacing(df)
        qty = round(self.config["order_size_usdt"] / fill_price, 5)

        if side == "Buy":
            new_price    = fill_price * (1 + spacing)
            counter_side = "Sell"
        else:
            new_price    = fill_price * (1 - spacing)
            counter_side = "Buy"

        new_id = self.client.place_limit_order(
            self.config["symbol"], counter_side, qty, new_price
        )
        self.active_orders[new_id] = {"price": new_price, "side": counter_side}
        del self.active_orders[order_id]
```

### 5.4 Gestión de riesgo y circuit breakers

```python
# risk_manager.py
class RiskManager:
    def __init__(self, total_capital: float, config: dict):
        self.total_capital    = total_capital
        self.peak_capital     = total_capital
        self.daily_start      = total_capital
        self.config           = config

    def update_peak(self, current: float):
        if current > self.peak_capital:
            self.peak_capital = current

    def check_drawdown(self, current: float) -> tuple[bool, str]:
        self.update_peak(current)
        dd = (self.peak_capital - current) / self.peak_capital

        if dd >= self.config["max_drawdown_pct"]:
            return True, f"DRAWDOWN_MAXIMO: {dd:.1%} alcanzado"
        return False, ""

    def check_daily_loss(self, current: float) -> tuple[bool, str]:
        daily_loss = (self.daily_start - current) / self.daily_start
        if daily_loss >= self.config["max_daily_loss_pct"]:
            return True, f"PERDIDA_DIARIA_MAXIMA: {daily_loss:.1%}"
        return False, ""

    def check_all(self, current: float, price_change_1h: float) -> tuple[bool, str]:
        # Drawdown total
        stop, reason = self.check_drawdown(current)
        if stop: return stop, reason

        # Pérdida diaria
        stop, reason = self.check_daily_loss(current)
        if stop: return stop, reason

        # Movimiento brusco de precio (flash crash / pump)
        if abs(price_change_1h) > 0.08:
            return True, f"MOVIMIENTO_BRUSCO: {price_change_1h:.1%} en 1h"

        return False, ""
```

### 5.5 Dead Man's Switch

Proceso independiente que cancela todas las órdenes si el proceso principal del bot se detiene inesperadamente.

```python
# dead_mans_switch.py
# Ejecutar como proceso separado: python dead_mans_switch.py
import time
import os
import signal
from pybit.unified_trading import HTTP
from dotenv import load_dotenv

load_dotenv()

HEARTBEAT_FILE  = "/tmp/bot_heartbeat.txt"
MAX_SILENCE_SEC = 120   # Si el bot no actualiza el heartbeat en 2 min → emergencia
SYMBOL          = "BTCUSDT"

session = HTTP(
    testnet=False,
    api_key=os.getenv("API_KEY"),
    api_secret=os.getenv("API_SECRET"),
)

def cancel_all_emergency():
    print("[DMS] ⚠️ Bot sin respuesta. Cancelando todas las órdenes de emergencia.")
    session.cancel_all_orders(category="spot", symbol=SYMBOL)
    print("[DMS] ✅ Órdenes canceladas.")

while True:
    time.sleep(30)
    if not os.path.exists(HEARTBEAT_FILE):
        cancel_all_emergency()
        break

    last_beat = os.path.getmtime(HEARTBEAT_FILE)
    if time.time() - last_beat > MAX_SILENCE_SEC:
        cancel_all_emergency()
        break
```

```python
# En main.py — el bot actualiza el heartbeat cada ciclo
import time

def update_heartbeat():
    with open("/tmp/bot_heartbeat.txt", "w") as f:
        f.write(str(time.time()))
```

### 5.6 Graceful shutdown y recuperación

```python
# main.py
import signal
import sys
from loguru import logger

def graceful_shutdown(signum, frame):
    logger.warning("Apagado solicitado. Cancelando órdenes abiertas...")
    client.cancel_all_orders(SYMBOL)
    notifier.send("🛑 Bot apagado correctamente. Todas las órdenes canceladas.")
    sys.exit(0)

signal.signal(signal.SIGTERM, graceful_shutdown)
signal.signal(signal.SIGINT,  graceful_shutdown)

def recover_from_crash():
    """Se ejecuta al inicio para detectar posiciones abiertas de sesiones anteriores."""
    open_orders = client.session.get_open_orders(category="spot", symbol=SYMBOL)
    num_orders  = len(open_orders["result"]["list"])

    if num_orders > 0:
        logger.warning(f"Detectadas {num_orders} órdenes abiertas de sesión anterior.")
        notifier.send(f"⚠️ {num_orders} órdenes previas detectadas. Revisando estado...")
        # Opción conservadora: cancelar todo y reiniciar el grid desde cero
        client.cancel_all_orders(SYMBOL)
```

---

## 6. BACKTESTING

### Descarga de datos históricos (gratuita)

```python
# backtester.py
import ccxt
import pandas as pd

def download_ohlcv(symbol: str = "BTC/USDT", timeframe: str = "1h", limit: int = 2000) -> pd.DataFrame:
    exchange = ccxt.bybit()
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df

def backtest_grid(df: pd.DataFrame, config: dict) -> dict:
    """Backtesting event-driven simplificado para grid con filtros ATR/ADX."""
    trades      = []
    maker_fee   = 0.0001   # 0.01% Bybit maker (CORRECTO)
    slippage    = 0.0001   # 0.01% slippage estimado conservador en spot BTC

    for i in range(200, len(df)):
        window = df.iloc[:i+1]
        # ... lógica de simulación de fills y contabilidad
        pass

    total_pnl   = sum(t["pnl"] for t in trades)
    total_fees  = sum(t["fee"] for t in trades)

    return {
        "total_trades":  len(trades),
        "gross_pnl":     total_pnl,
        "fees_paid":     total_fees,
        "slippage_cost": sum(t["slippage"] for t in trades),
        "net_pnl":       total_pnl - total_fees,
        "max_drawdown":  max_drawdown(trades),
        "sharpe_ratio":  sharpe(trades),
    }
```

**Criterios de éxito del backtesting:**
- Mínimo 6 meses de datos históricos (varios regímenes de mercado: lateral, alcista, bajista)
- Net PnL positivo con comisiones + slippage incluidos
- Max drawdown < 15%
- Sharpe ratio > 1.0
- Backtest separado en períodos de alta volatilidad (ej. correcciones > 20%)

---

## 7. FASES DE IMPLEMENTACIÓN

### FASE 1 — Entorno y conexión API
**Duración: 3-5 días**

- Configurar estructura de proyecto y Git
- Conexión autenticada a testnet de Bybit
- Consulta de balance, precios y libro de órdenes funcionando
- Sistema de logging y base de datos SQLite operativos
- Bot de Telegram configurado y recibiendo mensajes de prueba

**Criterio de éxito:** `python main.py --testnet` conecta, lee balance y precio sin errores.

---

### FASE 2 — Implementación de la estrategia
**Duración: 1-2 semanas**

- Lógica del grid dinámico con ATR y filtros ADX/EMA
- Gestión de órdenes: colocación, fill handler, reposicionamiento
- Risk manager y circuit breakers
- Dead Man's Switch como proceso independiente
- Graceful shutdown ante SIGTERM/SIGINT

**Criterio de éxito:** El bot coloca y gestiona órdenes en testnet correctamente durante 48h sin errores.

---

### FASE 3 — Backtesting
**Duración: 1 semana**

- Descargar mínimo 6 meses de datos OHLCV de Bybit via ccxt (gratis)
- Backtesting con comisiones reales (0.01% maker) y slippage estimado (0.01%)
- Walk-forward testing: entrenar en primeros 4 meses, validar en últimos 2
- Ajuste de parámetros (sin sobreoptimizar — máximo 3-4 variables)

**Criterio de éxito:** Net PnL positivo en período out-of-sample, drawdown < 15%, Sharpe > 1.0.

---

### FASE 4 — Paper Trading (testnet en tiempo real)
**Duración mínima: 8 semanas**

- Bot corriendo 24/7 en testnet con datos de mercado real
- Monitorización diaria de métricas

**Métricas para pasar a producción (TODAS deben cumplirse):**

| Métrica | Umbral |
|---|---|
| PnL neto acumulado | > 0 en las 8 semanas |
| Max drawdown real | < 10% |
| Órdenes con error o fallo | < 2% del total |
| Uptime del bot | > 98% |
| Circuit breakers disparados sin causa | 0 |
| Sharpe ratio (8 semanas) | > 0.8 |
| Heartbeat monitoring | Sin alertas de caída inesperada |

---

### FASE 5 — Producción con capital real (gradual)

```
DISTRIBUCIÓN INICIAL DEL CAPITAL:
  En el exchange (spot):    150€ — capital operativo
  Fuera del exchange:       350€ — reserva intocable

DESPLIEGUE GRADUAL:
  Mes 1:  50€ reales en el grid (0x leverage, spot)
  Mes 2:  100€ si métricas OK en mes 1
  Mes 3:  150€ si métricas OK en mes 2

CONDICIONES PARA AÑADIR LOS 350€ DE RESERVA:
  → 3 meses consecutivos de PnL neto positivo
  → Drawdown máximo real < 10%
  → Cero errores operativos críticos

CONDICIONES PARA PASAR A FUTUROS/APALANCAMIENTO:
  → Cumplir todo lo anterior
  → Mínimo 3 meses adicionales de operativa estable con capital completo
  → Apalancamiento máximo inicial en futuros: 2x
```

---

## 8. MODELO DE COSTES Y RENTABILIDAD

### Cálculo de margen por operación

```
Spacing mínimo:             0.6%
Comisión maker ida:         0.01%
Comisión maker vuelta:      0.01%
Slippage real estimado:     0.01% (spot BTC — alta liquidez)
─────────────────────────────────
Coste total por operación:  0.03%
Margen neto por operación:  0.57%
```

```
Ejemplo concreto con orden de 25 USDT:
  Ganancia bruta (0.6%):   0.15 USDT
  Comisiones totales:      0.005 USDT
  Slippage:                0.0025 USDT
  Ganancia neta:           ~0.143 USDT por operación completada
```

### Proyección mensual realista

| Concepto | Valor |
|---|---|
| Operaciones completadas / mes | 80-120 |
| Ganancia neta / operación | ~0.143 USDT |
| **PnL mensual estimado** | **3.5% – 5.2%** |
| Coste VPS Hetzner | 4.50€ |
| Healthchecks.io (free tier) | 0€ |
| Telegram Bot | 0€ |
| **Coste fijo mensual** | **~4.50€** |
| **Break-even (operaciones necesarias)** | **~32 operaciones/mes** |

> ⚠️ La estimación del 8.5% del plan original era optimista. El rango de **3.5%-5.2%** asume mercado lateral activo con el ATR calibrado correctamente. En mercado muy quieto o tendencia fuerte (ADX > 25), el bot pausará y el PnL será cercano a 0 ese período — esto es correcto, no un fallo.

---

## 9. GESTIÓN DEL RIESGO

### Por qué NO empezar con el 100% del capital

- En trading algorítmico, los primeros fallos son casi siempre de infraestructura (órdenes duplicadas, bugs de reconexión, fill parciales), no de la estrategia
- Un solo bug catastrófico con 500€ puede borrar la cuenta antes de tener datos válidos
- La varianza a corto plazo es muy alta: puedes ganar los primeros 15 días por pura suerte y perder luego — no es información válida
- En spot sin apalancamiento el peor caso con 150€ es perder 150€, no los 500€ completos

### Matriz de riesgos

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Mercado en tendencia fuerte destruye el grid | Alto | ADX > 25 pausa el bot automáticamente |
| Bug en el código con posición abierta | Alto | Dead Man's Switch + graceful shutdown + testnet exhaustivo |
| Flash crash / movimiento brusco | Muy alto | Stop Loss hard en el exchange (no solo en el bot) + circuit breaker 8%/h |
| Caída de API / pérdida de conexión | Medio | Reconexión automática × 3 → cancel-all → alerta Telegram |
| Slippage en cascada | Alto | Operar solo con liquidez > 50M USD en 24h (BTC/ETH únicamente) |
| Liquidación forzada | — | No aplica en spot 1x — este es el principal motivo para empezar en spot |
| Desincronización de estado | Medio | Force sync cada 10 minutos + recover_from_crash() al arrancar |

### Protocolo ante caída de API

```
1. Bot detecta timeout o error de conexión
2. Intenta reconectar: 3 intentos en 60 segundos
3. Si falla → cancela TODAS las órdenes abiertas via REST
4. Si no puede cancelar → envía alerta Telegram urgente con instrucciones manuales
5. Guarda estado completo en SQLite y cierra el proceso
6. Dead Man's Switch detecta la caída → cancela órdenes de forma independiente
7. Al reiniciar → recover_from_crash() detecta estado previo
```

---

## 10. MARCO LEGAL Y FISCAL (ESPAÑA 2026)

### Registro del bot

No existe obligación legal de registrar el bot como herramienta automatizada ante ningún organismo español. El bot es software personal. Las obligaciones legales recaen sobre el individuo que realiza las operaciones.

Con 500€ y operativa personal, estás completamente por debajo del umbral que convertiría al operador en PSCA (Proveedor de Servicios de Criptoactivos) bajo MiCA.

### Tratamiento fiscal en el IRPF

Las ganancias de operaciones con criptoactivos tributan como **ganancias y pérdidas patrimoniales** en la base imponible del ahorro:

| Tramo | Tipo |
|---|---|
| 0 – 6.000€ | 19% |
| 6.000 – 50.000€ | 21% |
| 50.000 – 200.000€ | 23% |
| > 200.000€ | 27% |

**Reglas importantes:**
- Cada par de operaciones cerradas (compra + venta) genera un hecho imponible
- Las pérdidas compensan ganancias del mismo ejercicio y los 4 años siguientes
- Las comisiones pagadas son gasto deducible del cálculo de la ganancia neta
- El coste del VPS y software son deducibles **solo si estás dado de alta como actividad económica** (autónomo); de lo contrario, no son deducibles directamente

### Obligaciones de reporte a la AEAT

**Modelo 721 (criptomonedas en el extranjero):**
- Obligatorio solo si el saldo en exchanges extranjeros supera **50.000€** a 31 de diciembre
- Con 500€: **no aplica**

**Modelos 172 y 173:**
- Estos modelos los presentan los propios exchanges registrados bajo MiCA ante la AEAT, no el usuario
- El usuario **no tiene obligación directa** de presentarlos él mismo
- Tu obligación es declarar las ganancias en la Renta (IRPF) correctamente

**Declaración de la Renta:**
- Si las ganancias netas del año superan 1.600€: declaración obligatoria
- Por debajo de ese umbral, si ya eres declarante habitual, inclúyelas igualmente

**Herramienta recomendada para calcular ganancias:**
Exporta el historial completo de operaciones de Bybit en CSV a final de año y usa **Koinly** (gratis hasta 25 transacciones) o **CoinTracking** para calcular las ganancias en el formato que acepta la AEAT.

### Exchanges conformes con MiCA para operar desde España

| Exchange | Estatus | Nota |
|---|---|---|
| **Bybit** | ✅ VASP Chipre (UE) | Recomendado |
| **Kraken** | ✅ Múltiples licencias UE | Conservador, menos pares |
| **Bitstamp** | ✅ Luxemburgo (histórico) | Muy regulado, comisiones más altas |
| **Coinbase** | ✅ Múltiples licencias UE | Comisiones altas para este volumen |
| **Binance** | ⚠️ VASP Polonia + tensiones regulatorias | Riesgo regulatorio pendiente |

---

## 11. HOJA DE RUTA

```
SEMANA    1    2    3    4    5    6    7    8    9   10   11   12   13   14   15   16
          │    │    │    │    │    │    │    │    │    │    │    │    │    │    │    │
FASE 1    ████████
Entorno + API

FASE 2         ████████████████
Estrategia + Risk Manager + DMS

FASE 3                   ████████████
Backtesting (6+ meses datos históricos)

FASE 4                             ████████████████████████████████████████
Paper Trading (mínimo 8 semanas)

FASE 5                                                               ████████████...
Producción gradual: 50€ → 100€ → 150€ → (tras 3 meses) → capital completo

TOTAL ESTIMADO HASTA PRODUCCIÓN COMPLETA: 14-16 semanas
```

---

## RESUMEN DE CAMBIOS RESPECTO A VERSIONES ANTERIORES

| Aspecto | Versión anterior | Esta versión |
|---|---|---|
| Spacing del grid | 0.4% fijo | 0.6% mínimo o 1.5×ATR — dinámico |
| Filtro de tendencia | Solo EMA200 | ADX + EMA50/200 |
| Maker fee Bybit | ~~0.02%~~ (incorrecto) | **0.01%** (correcto) |
| Librería de ejecución | ccxt para todo | pybit (ejecución) + ccxt (históricos) |
| Instrumento inicial | Futuros perpetuos con 2-3x | **Spot 1x** hasta 3 meses de datos reales |
| Capital en exchange | 500€ desde el inicio | **150€** — 350€ en reserva externa |
| PnL mensual estimado | ~~8.5%~~ (irreal) | **3.5%-5.2%** (realista) |
| Paper trading mínimo | 4-6 semanas | **8 semanas** |
| Monitorización externa | No contemplada | Heartbeat con healthchecks.io |
| Modelos 172/173 | Obligación directa del usuario | Los presenta el exchange, no el usuario |
| Dead Man's Switch | No incluido | Proceso independiente incluido |
