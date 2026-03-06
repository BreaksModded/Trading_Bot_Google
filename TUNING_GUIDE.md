# Guía de Ajuste Avanzado (Phase G)

Esta guía documenta la habilitación, sintonización y monitoreo de las dos nuevas capacidades añadidas en la Fase G: **Sesgo de Cuadrícula Asimétrico** (Asymmetric Grid Bias) y **Reinicio Dinámico de Beneficios** (Dynamic Profit Reinvestment).

## 1. Dynamic Profit Reinvestment (Interés Compuesto)

### ¿Qué hace?

En lugar de operar siempre con la misma configuración de capital, el motor captura tus ganancias (o pérdidas) y escala de manera inteligente tu capital de trading asignado sin saltos bruscos que puedan exponer tu riesgo, permitiendo un verdadero interés compuesto.

### Cómo habilitarlo

Edita tu `.env` (o `settings.py` directamente) para asegurar:

```bash
GRID_ENABLE_PROFIT_REINVESTMENT=True
```

### Parámetros Clave (`settings.py`)

* `reinvestment_equity_allocation_pct`: (Valor recomendado: **0.90**). Esto asigna el 90% del capital actual al bot, dejando un 10% de margen constante para financiar comisiones.
* `reinvestment_max_step_growth_pct`: (Valor recomendado: **0.05**). Protege el bot de *super-dimensionar* las operaciones si el precio sube bruscamente (ej. un +40% real). Limita el crecimiento del capital operativo a un +5% por redibujo, alisando la subida.
* `reinvestment_min_baseline_floor_pct`: (Valor recomendado: **0.80**). Si empieza el mercado bajista (downtrend) y el bot pierde dinero, este parámetro asegura que nunca tranzará con un capital inferior al 80% de su depósito original, protegiéndolo de que los grids se pongan a 0 de capital en un ciclo infinito de pérdidas.

### Cómo verificar que funciona (Ejemplos en Log)

Cada 1 hora (`reinvestment_recalc_interval_seconds=3600`) verás un log *INFO*:

```text
[Reinvestment] Baseline recalculated | Previous: 1000.00 | New: 1047.50 | Free equity: 1163.89 | Allocation: 90% | Growth this step: +4.75% | Floor: 800.00 | Next recalc in: 3600s
```

---

## 2. Asymmetric Grid Bias (Sesgo Dimensional)

### ¿Qué hace?

Al usar la lectura del ADX y las EMAs, ajusta microscópicamente el espaciado entre operaciones (spacing) dependiendo del momento del mercado en la vida real. Si estamos en tendencia bajista, va a intentar atrapar los precios mucho más abajo, pero saldrá de ellos rápidamente en la primera corrección que encuentre arriba, minimizando la carga de inventario en contra.

### Cómo habilitarlo

Edita tu `.env` (o `settings.py` directamente) para asegurar:

```bash
GRID_ENABLE_ASYMMETRIC_GRID=True
```

### Parámetros Clave (`settings.py`)

* **Bajista/Downtrend:**
  * `asymmetric_bearish_buy_factor` = 1.35 (Tus `Buys` se separarán un 35% extra. No caemos al cuchillo tan rápido)
  * `asymmetric_bearish_sell_factor` = 0.70 (Tus `Sells` se apretarán un 30% más rápido para liberar inventario y volver a dólares).
* **Alcista/Uptrend:**
  * `asymmetric_bullish_buy_factor` = 0.80 (Compramos un 20% más apretado, atrapando los pequeños dips)
  * `asymmetric_bullish_sell_factor` = 1.25 (Vendemos un 25% más holgado. Dejamos correr las ganancias antes de cortarlas).
* **Asymmetric_min_profit_multiple = 1.15:** Evita que factores muy agresivos reduzcan la venta por debajo del Break-Even (comisiones), forzando siempre las ventas a un mínimo del 115% del "Coste de operar".

### Cómo verificar que funciona (Ejemplos en Log)

Cada vez que el ADX pasa de 15.0 y entra en juego un sesgo bajista (SHORT) o alcista (LONG), el bot imprimirá antes de cada cuadricula:

```text
[AsymmetricGrid] BTCUSDC | Bias: SHORT | ADX: 32.4 | Buy spacing: 1.42% (base: 1.05% × 1.35) | Sell spacing: 0.74% (base: 1.05% × 0.70) | ADX strength: 0.69
```

### Reglas para Sintonizar en Testnet ⚠️

La mejor práctica es comenzar el bot con ambos perfiles en `False`. Luego:

1. Activa `GRID_ENABLE_PROFIT_REINVESTMENT=True` por unos días y comprueba su curva con las reglas fijas vigentes en el modo original.
2. Si el incremento de balance aumenta el tamaño de la orden saludablemente, enciende luego `GRID_ENABLE_ASYMMETRIC_GRID=True`.
3. Si el bot realiza **demasiadas compras** en una caída, aumenta tu `asymmetric_bearish_buy_factor` (ej: a 1.45 o 1.50).
4. Si el bot se queda atascado en el fondo y no logra realizar una venta de escape tras una caída pesada, entonces reduce tu `asymmetric_bearish_sell_factor` (ej: a 0.60), para que descargue inventario todavía antes.

---

## 3. Grid Refresh Híbrido con Circuit Breakers (Phase H)

### ¿Qué hace?

Cuando el precio se aleja significativamente de la cuadrícula actual (o la cuadrícula lleva demasiado tiempo sin fills), el bot cancela las órdenes de compra obsoletas y re-dibuja una nueva cuadrícula centrada en el precio actual. Esto permite capturar **muchas más operaciones** en mercados laterales, siguiendo el precio en lugar de esperar a que vuelva.

**Crucial:** A diferencia del bot legacy, Phase H incluye **5 puertas de seguridad obligatorias** que previenen el "toxic bagholding" (acumulación tóxica de inventario en tendencias bajistas).

### Cómo habilitarlo

```bash
GRID_ENABLE_GRID_REFRESH=True
```

> ⚠️ **Por defecto está desactivado (`False`)**. Con `False`, el bot se comporta exactamente igual que antes — cero cambio de comportamiento.

### Perfiles de Configuración

| Parámetro | Conservador | Moderado | Agresivo |
|-----------|-------------|----------|----------|
| `GRID_ENABLE_GRID_REFRESH` | `True` | `True` | `True` |
| `GRID_GRID_REFRESH_PRICE_DISTANCE_PCT` | 0.05 (5%) | 0.04 (4%) | 0.03 (3%) |
| `GRID_GRID_REFRESH_MAX_AGE_HOURS` | 8.0 | 6.0 | 4.0 |
| `GRID_REFRESH_ADX_BLOCK_THRESHOLD` | 30.0 | 35.0 | 45.0 |
| `GRID_REFRESH_MAX_INVENTORY_RATIO` | 0.30 | 0.40 | 0.60 |
| `GRID_REFRESH_COOLDOWN_SECONDS` | 3600 | 1800 | 900 |
| `GRID_REFRESH_MIN_MOVE_PCT` | 0.03 | 0.02 | 0.015 |
| `GRID_REFRESH_SKIP_IF_ORDERS_ABOVE` | 3 | 2 | 1 |

**Recomendación:** Empieza con el perfil **Conservador** las primeras 48h y monitorea los logs.

### Las 5 Puertas de Seguridad

1. **G1 — Filtro ADX de Tendencia:** Bloquea refresh si ADX > umbral en tendencia bajista (SHORT). Previene comprar en caída libre.
2. **G2 — Tope de Inventario:** Bloquea si más del X% de tu capital está atrapado como activo base. Previene acumulación infinita.
3. **G3 — Cooldown:** Mínimo tiempo entre refreshes por par. Previene quema de comisiones.
4. **G4 — Movimiento Mínimo:** Bloquea si el precio apenas se ha movido desde el último refresh. Evita micro-refreshes inútiles.
5. **G5 — Conteo de Órdenes:** Bloquea si aún quedan muchas órdenes activas (la cuadrícula sigue en rango).

### Cómo verificar que funciona (Ejemplos en Log)

```text
# Refresh exitoso:
[REFRESH] BTCUSDC — All gates PASSED. Executing refresh. ADX=18.3 (long) | Inventory=12% | Move=4.21% | BuyOrders=1
[REFRESH OK] BTCUSDC — cancelled 4 entry orders

# Bloqueado por tendencia bajista:
[REFRESH BLOCKED] BTCUSDC — G1_ADX_TREND (BLOCKED: ADX=38.2 > 35.0 in short trend)

# Bloqueado por inventario alto:
[REFRESH BLOCKED] BTCUSDC — G2_INVENTORY (BLOCKED: ratio=45.30% > cap=40.00%)
```

### Cuándo desactivar inmediatamente 🔴

* Si ves >3 `[REFRESH ERROR]` consecutivos → pon `GRID_ENABLE_GRID_REFRESH=False`
* Si el equity cae >5% en las primeras 24h tras activar → desactiva y revisa inventario
* Si ves `[REFRESH ABORT]` más de una vez → indica problema de tracking de fills

---

## 4. Dynamic Order Sizing — Tamaño Proporcional al Capital (Phase I)

### ¿Qué hace?

En vez de usar una cifra fija de USDT por orden (`GRID_ORDER_SIZE_USDT=300`), cada orden se calcula como un **porcentaje de tu capital disponible**:

```
order_size = capital_disponible × order_size_pct_per_level
```

Si tu cuenta crece, las órdenes crecen automáticamente. Si pierde, se reducen solas. **Interés compuesto real, sin configuración manual.**

### Cómo habilitarlo

```bash
GRID_ENABLE_DYNAMIC_ORDER_SIZING=True
GRID_ORDER_SIZE_PCT_PER_LEVEL=0.05   # 5% por nivel
```

> ⚠️ **Por defecto está desactivado (`False`).** Con `False`, el bot usa `GRID_ORDER_SIZE_USDT` exactamente como antes.

### Tabla de referencia para elegir el porcentaje

| Pares × Niveles | Conservador | Moderado | Agresivo |
| ---------------- | ----------- | -------- | -------- |
| 1 par × 5 lvl   | 4%          | 6%       | 9%       |
| 2 pares × 5 lvl  | 3%          | 5%       | 7%       |
| 3 pares × 5 lvl  | 2%          | 4%       | 6%       |
| 5 pares × 5 lvl  | 1.5%        | 2.5%     | 4%       |

**¿Por qué importa el número de pares?** Porque si tienes 3 pares × 5 niveles = 15 posiciones máximas. Con 5% cada una → 75% del capital en uso. Si además más de un par quiere hacer grid al mismo tiempo, el bot ya tiene protección interna que escala o rechaza grids cuando no hay capital suficiente.

### Ejemplo práctico

```
Capital libre: €2,000
Pares activos: 2 (BTCUSDC, ETHUSDC)
Niveles: 5 por par
Porcentaje: 5%

→ Orden por nivel = €2,000 × 0.05 = €100
→ Capital máximo desplegado = 2 × 5 × €100 = €1,000 (50%)
→ Queda €1,000 de reserva ✅
```

### Parámetros de seguridad

* `GRID_DYNAMIC_SIZING_MIN_ORDER_USDT = 10` — Nunca pone una orden por debajo de 10 USDT, aunque el porcentaje dé menos.
* `GRID_DYNAMIC_SIZING_MAX_ORDER_USDT = 0` — Sin tope (pon un número \> 0 para limitar, ej: 500).

### Cómo leer los logs

```text
# Modo dinámico activo:
[DynamicSizing] Capital: 2000.00 | pct: 5.0% | Raw: 100.00 | Final: 100.00/order | Mode: DYNAMIC

# Modo fijo (desactivado):
[DynamicSizing] Mode: FIXED (300.00/order) | Reinvestment ×1.03 | Effective: 309.00/order

# Advertencia (capital bajo):
[DynamicSizing] Computed size 7.50 USDT below minimum 10.00. Using minimum floor.
```

### Señales de alerta 🔴

* Si ves `below minimum` frecuentemente → tu capital es muy bajo para el porcentaje configurado
* Si las órdenes crecen rápido → reduce `order_size_pct_per_level` o pon un `max_order_usdt`

---

## 5. Price Shock Circuit Breaker — Rediseño Inteligente (Phase J)

### ¿Qué hace?

Antes de Phase J, si el precio se movía más del 8% en una hora el bot hacía un **emergency stop permanente** — cancelaba todo y requería reinicio manual. Esto era desproporcionado para volatilidad normal de crypto.

Phase J reemplaza ese comportamiento con un sistema de tres etapas:

```
ETAPA 1 — PAUSA (movimiento detectado)
  Trigger: precio_move_1h > threshold (ej. 8%)
  Acción:  Pausa colocación de nuevos grids
           Mantiene todas las órdenes existentes activas
           Inicia temporizador de pausa
           Telegram informativo (no alarma)

ETAPA 2 — MONITOREO (durante la pausa)
  Cada ciclo: re-evalúa el movimiento de precio
  Contador: ciclos consecutivos BAJO el threshold

ETAPA 3A — AUTO-RESUMIR (mercado estabilizado)
  Trigger: precio_move_1h < threshold durante N ciclos consecutivos
  Acción:  Resume nuevos grids
           Log + Telegram informativo

ETAPA 3B — ESCALACIÓN A EMERGENCY STOP (volatilidad sostenida)
  Trigger: pausa activa > max_pause_duration_seconds (defecto: 2h)
  Acción:  Emergency stop completo (mismo comportamiento anterior)
           Telegram urgente
```

**Corrección de bug adicional:** En la versión anterior, si el bot se reiniciaba durante un período de alta volatilidad, el primer precio registrado se comparaba contra el precio actual, produciendo un "movimiento de 1 hora" falso que en realidad ocurrió durante horas. Phase J añade un **período de warmup** que espera N muestras de precio antes de activar la evaluación.

### Cómo funciona el warmup (bug fix cold-start)

Al arrancar, el bot necesita acumular suficientes muestras de precio para que la medición del "movimiento de 1 hora" sea estadísticamente válida. El parámetro `RISK_PRICE_SHOCK_MIN_SAMPLES` controla cuántas se necesitan (por defecto: 10).

En el log verás:

```text
# Al arrancar (warmup activo):
[RiskManager] Price shock circuit breaker in warmup mode. Collecting price samples (0/10). Will activate after 10 samples.

# Cada ciclo durante warmup (DEBUG, no spam INFO):
[RiskManager] Price shock evaluation skipped: only 3 samples collected, minimum is 10. Warmup period active.

# Cuando se completa el warmup:
[RiskManager] Price shock circuit breaker now active. Sufficient price history collected (10/10 samples).
```

### Parámetros clave

| Variable de entorno | Default | Rango válido | Descripción |
| ------------------- | ------- | ------------ | ----------- |
| `RISK_PRICE_SHOCK_MIN_SAMPLES` | `10` | 3–60 | Muestras mínimas antes de activar la evaluación |
| `RISK_PRICE_SHOCK_RESUME_CONSECUTIVE_CYCLES` | `3` | 2–10 | Ciclos limpios consecutivos para auto-resumir |
| `RISK_PRICE_SHOCK_MAX_PAUSE_DURATION_SECONDS` | `7200` | 1800–14400 | Segundos antes de escalar a emergency stop |
| `RISK_PRICE_SHOCK_NOTIFY_TELEGRAM` | `True` | True/False | Notificaciones Telegram para pausa/reanudación |

### Cómo ajustar `RISK_PRICE_SHOCK_MIN_SAMPLES`

Depende de **con qué frecuencia reinicias el bot**:

* **Reinicios infrecuentes (producción estable):** Usa `10`–`15`. Da ~10–15 minutos de warmup robusto.
* **Reinicios frecuentes (desarrollo/debug):** Baja a `5`. El warmup dura ~5 minutos.
* **Nunca pongas menos de 3:** Con solo 2 puntos, cualquier pequeño movimiento puede disparar falsos positivos.
* **No pongas más de 30:** El bot puede operar sin protección de precio durante 30 minutos tras un reinicio.

### Cómo ajustar `RISK_PRICE_SHOCK_RESUME_CONSECUTIVE_CYCLES`

Controla qué tan "cauteloso" es el bot al reanudar tras una pausa:

* **2 ciclos** (~10s a 5s/ciclo): Reanudación rápida. Bueno para mercados que recuperan rápido. Riesgo de falsa reanudación en mercado choppy.
* **3 ciclos** (por defecto, ~15s): Balance razonable.
* **5 ciclos** (~25s): Más conservador. Requiere más confirmación de estabilidad.
* **10 ciclos** (~50s): Para alta volatilidad crónica. Rara vez necesario.

### Cómo ajustar `RISK_PRICE_SHOCK_MAX_PAUSE_DURATION_SECONDS`

Define cuándo la volatilidad pasa de "normal en crypto" a "evento estructural real":

* **1800s (30 min):** Agresivo. Cualquier volatilidad de 30+ minutos detiene el bot. Conservador en capital, puede parar muchas veces.
* **7200s (2h, defecto):** Equilibrado. Solo eventos como el crash de FTX (Nov 2022) o Marzo 2020 duran este nivel de volatilidad extrema.
* **14400s (4h):** Tolerante. Solo para operadores que prefieren menos interrupciones y aceptan más exposición durante volatilidad sostenida.

### Secuencia de log completa: ciclo pausa → reanudación

Así se ve un episodio normal en los logs:

```text
# 1. Shock detectado (LOG WARNING + Telegram INFO):
[RiskManager] Price shock detected: 20.16% move (threshold: 8.00%). Pausing new grid placements. Monitoring for stabilization.

# 2. Cada ciclo de evaluación durante la pausa (LOG DEBUG):
[RiskManager] Price move below threshold (6.34%), waiting for 3 consecutive clean cycles (1/3).
[RiskManager] Price move below threshold (5.12%), waiting for 3 consecutive clean cycles (2/3).

# 3. Auto-reanudación (LOG INFO + Telegram INFO):
[RiskManager] Price shock resolved. Market stabilized (3 consecutive clean cycles). Resuming grid placements. Pause duration: 847s.

# 4. En el loop principal durante la pausa (LOG INFO):
[iter 42] Grid placements skipped — price shock pause active (price_shock_pause_active:0.2016)

# 5. El sync de órdenes SÍ corre durante la pausa:
[iter 42] OK -- equity=1850.23 active_orders=8
```

### Lo que NO cambia durante una pausa de price shock

* Las órdenes de venta existentes **siguen activas** — el inventario comprado está protegido.
* El sync con el exchange corre normalmente.
* Los fills por WebSocket se procesan normalmente — si se llena una orden de compra, su orden inversa de venta se coloca igual.
* Las notificaciones de fills por Telegram siguen activas.
* Los circuit breakers de drawdown y daily loss **siguen evaluándose** normalmente.

### Confirmación de escenarios críticos

| Escenario | Comportamiento |
| --------- | -------------- |
| Bot reinicia, deque vacío, precio movió 20% mientras estaba offline | Warmup activo → sin pausa falsa durante `min_samples` ciclos |
| Precio sube 20%, recupera en 15 min | Pausa → 3 ciclos limpios → auto-reanuda sin intervención |
| Precio sube 20%, se mantiene volátil 3 horas | Pausa → a las 2h escala a emergency stop (comportamiento anterior) |
| Precio spikea 10%, recupera, vuelve a spikear | Pausa/reanuda/pausa correctamente; contador de ciclos limpios resetea en cada spike |
| Fill de compra entra mientras está en pausa | Fill procesado normalmente; orden inversa de venta colocada sin restricciones |

### Garantías de seguridad (sin cambios)

* `emergency_stop=True` **sigue disparándose** para: max_drawdown, daily_loss, AND price shock sostenido (> max_pause_duration_seconds).
* `emergency_stop=False` para: price shock transitorio (nueva conducta).
* Las órdenes de venta sobre inventario existente **nunca se cancelan** durante una pausa de price shock.
