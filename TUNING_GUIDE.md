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
