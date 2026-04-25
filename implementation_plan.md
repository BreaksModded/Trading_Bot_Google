# 🚀 Sprint: Desbloquear Bot — Eliminar Over-Protection y Maximizar Trades

El bot tiene una estrategia **rentable** (100% win rate, Sharpe 15.85) pero está paralizado por **13 capas de filtros acumulados** que se bloquean mutuamente. Este sprint elimina o relaja cada una para que el bot vuelva a operar activamente.

---

## Mapa de Restricciones Actual

He identificado **13 puntos de bloqueo** en el flujo de ejecución. Cada iteración del bot debe pasar TODOS para colocar un grid:

```
ITERATION START
 ├── [R1] Risk: daily_loss_pct ≥ 5%?          → RISK-BLOCK (pause 24h)
 ├── [R2] Risk: drawdown ≥ 15%?                → EMERGENCY STOP
 ├── [R3] Risk: price_shock 1h ≥ 8%?           → block_new_grids
 │
 ├── [F1] ADX > 30 (adx_threshold)?            → pause_new_grid=True → SKIP
 ├── [F2] Regime = TRENDING_DOWN?              → SKIP
 ├── [F3] Regime = TRANSITIONAL + SHORT bias?  → SKIP
 ├── [F4] Volume ratio < 0.20?                 → SKIP (NaN→0.00 bug)
 ├── [F5] Correlation > 0.80 with active pair? → SKIP
 │
 ├── [G1] max_active_pairs = 2?                → SKIP 3rd+ pair
 ├── [G2] Capital < min_viable (3 levels)?     → SKIP
 │
 ├── [H1] Grid refresh: has_unhedged_inventory?→ ABORT refresh
 ├── [H2] Grid refresh: G2_INVENTORY > 40%?    → BLOCKED refresh
 ├── [H3] Grid refresh: ADX>35 + SHORT trend?  → BLOCKED refresh
 └── ✅ PLACE GRID (almost never reached)
```

**Resultado en producción:** Los 5 pares bloqueados simultáneamente durante 37 días.

---

## User Review Required

> [!IMPORTANT]
> Este plan hace el bot significativamente **más agresivo**. Aumentará los trades pero también el riesgo. ¿Estás de acuerdo con los trade-offs?

> [!WARNING]  
> **FIX-1 (Liquidación forzada de XRP):** Propongo vender el inventario de XRP ($123) a market price la próxima vez que el bot ejecute el hedge. Esto puede resultar en una pequeña pérdida si XRP está por debajo de avg_cost ($1.5144). Actualmente XRP está ~$2.20 — lo que significaría un **beneficio de ~$57**. ¿Confirmas que quieres esta liquidación automática?

---

## Propuesta de Cambios

### Componente 1: Eliminar Dust Deadlock

**Problema:** `has_unhedged_inventory()` bloquea grid refresh por cantidades de polvo ($0.02 en ETH, $0.005 en SOL).

#### [MODIFY] [order_manager.py](file:///c:/Users/diego/Documents/Bot%20Trading%20Google/Trading_Bot_Google/core/order_manager.py)

Cambiar el threshold de `1e-6` a un mínimo de $1 de valor:

```diff
 def has_unhedged_inventory(self) -> bool:
-    if self._position_qty <= 1e-6:
+    # Ignore dust positions worth less than $1 at avg_cost
+    if self._position_qty <= 1e-6 or (self._avg_cost > 0 and self._position_qty * self._avg_cost < 1.0):
         return False
```

**Impacto:** Desbloquea ETH (0.00000739 × $1975 = $0.015) y SOL (0.0000496 × $95 = $0.005) inmediatamente.

---

### Componente 2: Relajar ADX Filter (F1)

**Problema:** `INDICATOR_ADX_THRESHOLD=30` bloquea grids cuando ADX > 30. En crypto, ADX > 30 es normal ~40-60% del tiempo.

#### [MODIFY] [.env](file:///c:/Users/diego/Documents/Bot%20Trading%20Google/Trading_Bot_Google/.env)

```diff
-INDICATOR_ADX_THRESHOLD=30
+INDICATOR_ADX_THRESHOLD=45
```

**Impacto:** El bot podrá operar en mercados con ADX 30-45 (que es donde hay más movimiento y oportunidades).

---

### Componente 3: Relajar Regime Filter (F2, F3)

**Problema:** `TRENDING_DOWN` bloquea completamente, y `TRANSITIONAL + SHORT` también. Con ADX_TRENDING=30 y RSI_LOWER=35, esto se activa demasiado.

#### [MODIFY] [.env](file:///c:/Users/diego/Documents/Bot%20Trading%20Google/Trading_Bot_Google/.env)

```diff
-GRID_REGIME_ADX_TRENDING=30
+GRID_REGIME_ADX_TRENDING=40
-GRID_REGIME_RSI_LOWER=35
+GRID_REGIME_RSI_LOWER=25
```

#### [MODIFY] [main.py](file:///c:/Users/diego/Documents/Bot%20Trading%20Google/Trading_Bot_Google/main.py) — `_place_new_grids`

Eliminar el bloqueo de `TRANSITIONAL + SHORT`. En spot trading, un grid con bias SHORT simplemente no coloca sell entries (solo buys), lo cual es seguro:

```diff
-            if signal.regime == MarketRegime.TRANSITIONAL and _bias not in ("long", "neutral"):
-                logger.info(
-                    "SKIP {}: regime=TRANSITIONAL trend_bias={} (only LONG/NEUTRAL allowed)",
-                    symbol, signal.trend_bias,
-                )
-                continue
+            # Removed: TRANSITIONAL + SHORT block was too restrictive for spot
+            # Grid strategy already handles SHORT by only placing BUY levels
```

**Impacto:** Elimina ~30% de los ciclos de bloqueo por regime.

---

### Componente 4: Desactivar Volume Filter (F4)

**Problema:** `GRID_VOLUME_FILTER_ENABLED=true` con `MIN_VOLUME_RATIO_FILTER=0.20`. El cálculo de volume_ratio todavía produce 0.00 en algunos casos (NaN de velas vacías post-fix). SOL y ADA bloqueados permanentemente.

#### [MODIFY] [.env](file:///c:/Users/diego/Documents/Bot%20Trading%20Google/Trading_Bot_Google/.env)

```diff
-GRID_VOLUME_FILTER_ENABLED=true
+GRID_VOLUME_FILTER_ENABLED=false
```

**Razonamiento:** Los pares que tradeas (BTC, ETH, SOL, XRP, ADA) son los más líquidos del mercado. El filtro de volumen tiene sentido para shitcoins de baja liquidez, pero NO para top-10 coins en Bybit.

**Impacto:** SOL y ADA dejan de bloquearse.

---

### Componente 5: Relajar Correlación (F5)

**Problema:** Correlación > 0.80 rechaza ETH cuando BTC está activo, y ADA cuando XRP está activo. Con solo 2 pares activos, esto elimina el 60% de los candidatos.

#### [MODIFY] [main.py](file:///c:/Users/diego/Documents/Bot%20Trading%20Google/Trading_Bot_Google/main.py) — `_place_new_grids`

```diff
-                    if corr > 0.8:
+                    if corr > 0.95:
```

**Razonamiento:** 0.95 solo rechaza pares casi idénticos (como BTCUSDC vs BTCUSDT). BTC y ETH tienen correlación ~0.80-0.85 pero se mueven de forma suficientemente independiente para grid trading.

**Impacto:** ETH y ADA pueden operar simultáneamente con BTC y XRP.

---

### Componente 6: Aumentar max_active_pairs (G1)

**Problema:** `GRID_MAX_ACTIVE_PAIRS=2` limita a solo 2 pares simultáneos de 5 disponibles.

#### [MODIFY] [.env](file:///c:/Users/diego/Documents/Bot%20Trading%20Google/Trading_Bot_Google/.env)

```diff
-GRID_MAX_ACTIVE_PAIRS=2
+GRID_MAX_ACTIVE_PAIRS=4
```

**Razonamiento:** Con $166 de equity, 4 pares × 3 niveles × $10/orden = $120 de despliegue máximo. Es agresivo pero viable. El bot ya tiene protección de capital con el sizing dinámico.

**Impacto:** Hasta 4 pares pueden operar simultáneamente.

---

### Componente 7: Relajar G2_INVENTORY Gate en Grid Refresh (H2)

**Problema:** `refresh_max_inventory_ratio=0.40` bloquea el refresh de XRP (ratio 390%). Esto crea un deadlock: no puede refrescar → no puede mover sell orders → inventario sigue atrapado.

#### [MODIFY] [.env](file:///c:/Users/diego/Documents/Bot%20Trading%20Google/Trading_Bot_Google/.env)

```diff
+GRID_REFRESH_MAX_INVENTORY_RATIO=0.80
```

#### [MODIFY] [grid_refresh.py](file:///c:/Users/diego/Documents/Bot%20Trading%20Google/Trading_Bot_Google/core/grid_refresh.py) — `evaluate_safety_gates`

Además, si el inventario está hedged (tiene sell order), el G2 gate no debería bloquear:

```diff
 def evaluate_safety_gates(
     *,
     adx_value: float,
     trend_bias: str,
     inventory_ratio: float,
     open_buy_count: int,
     time_since_last_refresh_s: float,
     price_move_since_last_pct: float,
     adx_block_threshold: float = 35.0,
     max_inventory_ratio: float = 0.40,
     cooldown_seconds: int = 1800,
     min_move_pct: float = 0.02,
     skip_if_orders_above: int = 2,
+    has_pending_sell: bool = False,
 ) -> tuple[bool, list[RefreshGateResult]]:
```

```diff
     # G2 — Inventory Cap
-    g2_passed = inventory_ratio <= max_inventory_ratio
+    # If inventory is already hedged (has pending sell), allow refresh
+    g2_passed = has_pending_sell or inventory_ratio <= max_inventory_ratio
```

**Impacto:** XRP con sell order pendiente puede hacer refresh. Rompe el deadlock principal.

---

### Componente 8: Relajar Risk Circuit Breakers (R1)

**Problema:** `RISK_MAX_DAILY_LOSS_PCT=0.05` (5%) activó circuit breaker 4 veces. Con crypto tan volátil, el 5% se alcanza por fluctuación normal sin que haya un trade perdedor.

#### [MODIFY] [.env](file:///c:/Users/diego/Documents/Bot%20Trading%20Google/Trading_Bot_Google/.env)

```diff
-RISK_MAX_DAILY_LOSS_PCT=0.05
+RISK_MAX_DAILY_LOSS_PCT=0.08
```

**Razonamiento:** Con 100% win rate en trades cerrados, las "pérdidas diarias" son solo fluctuaciones de mercado en inventario abierto. 8% es más razonable para crypto.

**Impacto:** Reduce circuit breaker triggers de ~4 veces/mes a ~1 vez/mes.

---

### Componente 9: Reducir Cooldowns y TTLs

**Problema:** Grid refresh cooldown de 90 min + 6h max age + 1800s gate cooldown. Demasiado lento para mercados rápidos.

#### [MODIFY] [.env](file:///c:/Users/diego/Documents/Bot%20Trading%20Google/Trading_Bot_Google/.env)

```diff
+GRID_GRID_REFRESH_COOLDOWN_MINUTES=30
+GRID_GRID_REFRESH_MAX_AGE_HOURS=3.0
+GRID_REFRESH_COOLDOWN_SECONDS=900
+GRID_GRID_REFRESH_MAX_PER_DAY=8
```

**Impacto:** Grids se recalibran 2-3× más rápido, capturando más movimientos.

---

### Componente 10: Reducir Grid Levels para Mayor Velocidad

**Problema:** Con 5 niveles y $10/orden, se necesitan $50 por par. Con 4 pares = $200 (más del equity total). Menos niveles = más rápido fill → más trades.

#### [MODIFY] [.env](file:///c:/Users/diego/Documents/Bot%20Trading%20Google/Trading_Bot_Google/.env)

```diff
+GRID_NUM_LEVELS=3
```

**Razonamiento:** 3 niveles con spacing dinámico es suficiente. Los niveles más lejanos casi nunca se llenan.

**Impacto:** Capital por par baja de $50 a $30. Más pares pueden operar simultáneamente.

---

## Resumen de Cambios en .env

```env
# ── CAMBIOS PARA DESBLOQUEAR ──
INDICATOR_ADX_THRESHOLD=45
GRID_REGIME_ADX_TRENDING=40
GRID_REGIME_RSI_LOWER=25
GRID_VOLUME_FILTER_ENABLED=false
GRID_MAX_ACTIVE_PAIRS=4
GRID_NUM_LEVELS=3
GRID_GRID_REFRESH_COOLDOWN_MINUTES=30
GRID_GRID_REFRESH_MAX_AGE_HOURS=3.0
GRID_REFRESH_COOLDOWN_SECONDS=900
GRID_REFRESH_MAX_INVENTORY_RATIO=0.80
GRID_GRID_REFRESH_MAX_PER_DAY=8
RISK_MAX_DAILY_LOSS_PCT=0.08
```

## Resumen de Cambios en Código

| Archivo | Cambio | Líneas |
|---------|--------|--------|
| `order_manager.py` | Dust threshold en `has_unhedged_inventory` | L262-269 |
| `main.py` | Eliminar bloqueo TRANSITIONAL+SHORT | L927-932 |
| `main.py` | Correlación 0.80 → 0.95 | L953 |
| `grid_refresh.py` | G2 bypasses si hay sell order pendiente | L172-252 |

---

## Verification Plan

### Automated Tests
- `pytest tests/ -x` — todos los tests existentes deben pasar
- Nuevos tests para `has_unhedged_inventory` con dust positions
- Nuevo test para G2 gate bypass con `has_pending_sell=True`

### Manual Verification
- Desplegar al servidor de producción
- Monitorear los primeros 30 minutos de logs buscando:
  - Grids colocados (ya no deberían verse solo SKIPs)
  - Grid refresh ejecutándose para XRP
  - Al menos 2-3 pares operando simultáneamente

---

## Estimación de Impacto

| Métrica | Antes | Después (estimado) |
|---------|-------|---------------------|
| Pares operando | 0/5 | 3-4/5 |
| Trades/día | 0 | 5-15 |
| PnL/día potencial | $0 | $0.50-2.00 |
| Iteraciones con grid placement | ~0% | ~40-60% |
| Capital en uso | 20% | 60-80% |
| Riesgo de drawdown | Bajo (paralizado) | Medio (activo) |
