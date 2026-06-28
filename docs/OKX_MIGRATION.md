# Migración Bybit → OKX X-Perps

## Por qué

Bybit **bloquea derivados de cripto para residentes en España/UE** (ErrCode `10024`):
toda orden de futuros se rechaza. Bybit EU solo ofrece spot. La estrategia del bot es
de **futuros** (grid neutral apalancado + tendencia), así que se cambia el exchange a
**OKX X-Perps**, el "perpetuo" conforme a MiCA/MiFID: un **futuro lineal con vencimiento
a 5 años y funding** (legalmente un futuro, funcionalmente un perpetuo), disponible en el
EEE y con apalancamiento hasta 10x.

## Qué cambió (y qué NO)

**Cambió** — solo la capa de exchange y su configuración:
- `core/exchange_okx.py` — nuevo `OKXExchangeClient` sobre **ccxt** (REST async) +
  **ccxt.pro** (WebSocket de fills). Misma interfaz pública que el cliente de Bybit, así
  que el resto del bot no se entera.
- `config/settings.py` — `OKXSettings` (prefijo `OKX_`: `API_KEY`, `API_SECRET`,
  `API_PASSPHRASE`, `DEMO`).
- `main_futures.py` y `api/app.py` — construyen `OKXExchangeClient` desde `settings.okx`.
- `.env` / `.env.example` — sección OKX.

**NO cambió** — estrategia (`decide_trend`, geometría del grid), risk manager, sizing,
kill-switch, y toda la UI del dashboard.

## Modelo de contratos (lo importante)

El X-Perp de ETH se cotiza en **contratos**, no en ETH:

| Concepto | Valor (confirmado vía ccxt) |
|---|---|
| Símbolo ccxt | `ETH/USD:USD-310404` |
| instId OKX | `ETH-USD_UM_XPERP-310404` |
| 1 contrato | **0,001 ETH** |
| Mínimo | 1 contrato (0,001 ETH) |
| Tick de precio | 0,01 |
| Colateral | USDC (cuenta unificada) |

El cliente convierte ETH ↔ contratos internamente (`_to_contracts` / `_to_coin`), así que
el bot sigue razonando en ETH. El símbolo se resuelve **dinámicamente** (futuro lineal,
liquidado en USD, vencimiento más lejano), de modo que si OKX renueva la serie el bot se
adapta solo.

## Validación en DEMO (obligatoria antes de dinero real)

El cliente lleva marcados con `# DEMO:` los puntos que solo se pueden confirmar contra la
API con autenticación. **Sigue estos pasos en demo antes de operar en real.**

### 1. Crear claves DEMO en OKX
Las claves de demo son **distintas** de las de real.
1. App/web OKX → activa **"Trading Demo"** (Demo Trading).
2. Ya en demo: **Perfil → API → Crear API key** con permiso **Leer + Operar** (NO Retirar)
   y una **passphrase**.
3. La demo te da fondos virtuales automáticamente — no hay que transferir nada.

### 2. Configurar `.env` (en el VPS)
```
OKX_API_KEY=<clave demo>
OKX_API_SECRET=<secret demo>
OKX_API_PASSPHRASE=<passphrase demo>
OKX_DEMO=true
```
(Comentarios siempre en su propia línea; nunca al final de una línea `KEY=valor`.)

### 3. Arrancar y observar
```
git pull
# reiniciar bot (main_futures.py) y dashboard (run_dashboard.py)
```
En los logs del bot debe verse:
- `OKX client in DEMO (paper) mode`
- `OKX market resolved: ETH/USD:USD-310404 (instId ETH-USD_UM_XPERP-310404) contractSize=0.001 ...`

### 4. Checklist `# DEMO:` (confirmar uno a uno)
- [ ] **Equity** — el dashboard muestra los fondos demo (no 0). (`get_portfolio_equity`)
- [ ] **Tamaño de orden** — una orden de N ETH aparece en OKX como N/0,001 contratos.
      Verifica que la posición **no** sea 1000× ni 1/1000× lo esperado. (`_to_contracts`)
- [ ] **Apalancamiento** — 5x aislado aplicado al símbolo. (`set_leverage`)
- [ ] **Grid** — en RANGING se montan las órdenes límite (visibles en OKX demo).
- [ ] **Flip por WS** — al ejecutarse una rung aparece su partner; en logs
      `filled ... -> partner ...`. (`watch_orders` → `handle_fill`)
- [ ] **PnL** — los TP del grid registran `closedPnl ≠ 0` en las stats del dashboard.
- [ ] **Stop (tendencia)** — al abrir tendencia se coloca el stop de respaldo
      (`set_position_stop_loss`); si OKX lo rechaza, el stop por precio vivo del bot
      cierra igualmente (es el primario).
- [ ] **Funding** — se muestra como 0,0 (es solo informativo; el X-Perp no expone
      `fetch_funding_rate`). No bloquea nada.

### 5. Pasar a REAL
Cuando todo el checklist esté verde:
```
OKX_API_KEY=<clave real>          # la que ya creaste (Leer + Operar, sin Retirar)
OKX_API_SECRET=<secret real>
OKX_API_PASSPHRASE=<passphrase real>
OKX_DEMO=false
```
Reinicia. Arranca con tamaño/apalancamiento conservadores y vigila los primeros ciclos.

## Notas

- **DNS de aiohttp en Windows**: el resolver async por defecto de aiohttp falla en algunos
  Windows (`Could not contact DNS servers`). El cliente fuerza un `ThreadedResolver` solo
  en Windows (para correr el dashboard en local); en Linux (el VPS) no hace nada.
- **Carga de mercados**: el cliente solo carga futuros (`fetchMarkets=['future']`) para
  evitar el burst concurrente que OKX devolvía como 503.
- **Rollback**: revertir a Bybit no es una opción real (sigue bloqueado por regulación);
  la rama `feature/okx-migration` es el camino hacia adelante.
