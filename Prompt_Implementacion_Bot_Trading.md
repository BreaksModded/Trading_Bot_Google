# PROMPT MAESTRO — Plan de Implementación Completo: Bot de Trading Algorítmico con Dashboard Profesional

---

> **Instrucciones para la IA:** Este prompt describe un proyecto completo de bot de trading algorítmico con interfaz gráfica profesional. Tu objetivo es generar un **plan de implementación detallado, exhaustivo y listo para ejecutar** que cubra absolutamente todos los componentes descritos a continuación. El plan debe incluir: estructura de archivos, dependencias exactas con versiones, código fuente de cada módulo, diseño de la interfaz gráfica, tests, configuración de despliegue y verificación end-to-end.

---

## CONTEXTO DEL PROYECTO

Soy un desarrollador que va a construir un **bot de trading algorítmico** para operar en **Bybit** con una estrategia de **Grid Dinámico basado en ATR con filtros ADX + EMA**. El capital inicial es de **500€** (150€ en el exchange en spot sin apalancamiento, 350€ en reserva externa). El bot operará el par **BTC/USDT** y comenzará en **testnet** antes de pasar a producción real.

El proyecto debe ser **extremadamente profesional, robusto y production-ready**, no un prototipo ni un MVP. Necesito que el plan de implementación cubra tanto el **motor de trading backend** como una **interfaz gráfica (GUI/Dashboard)** completa que me permita monitorizar, configurar y controlar el bot de forma visual, intuitiva y profesional.

---

## SECCIÓN 1: MOTOR DE TRADING (BACKEND)

### 1.1 Estrategia: Grid Dinámico con ATR + ADX + EMA

Implementa la siguiente estrategia con todos sus componentes:

- **Grid Trading Dinámico:** Coloca órdenes limit de compra y venta a niveles equidistantes alrededor del precio actual. Cuando una orden se ejecuta, coloca la orden inversa en el siguiente nivel.
- **ATR (Average True Range, período 14):** El spacing entre niveles del grid se calcula como `max(0.6%, 1.5 × ATR%)`. Nunca inferior al 0.6% para garantizar margen positivo tras comisiones.
- **ADX (Average Directional Index, período 14):** Si ADX > 25, el bot **pausa** la apertura de nuevas mallas (mercado en tendencia fuerte, no favorable para grid).
- **EMA 50/200:** Define el sesgo direccional. Cruce alcista (EMA50 > EMA200) = solo operaciones Long. Cruce bajista = solo operaciones Short.
- **Recalibración dinámica:** El spacing y los filtros se recalculan en cada ciclo del loop principal usando los últimos datos de mercado.

**Parámetros de configuración (deben ser editables desde la GUI):**

```
symbol:               "BTCUSDT"
capital_usdt:         150
num_levels:           4-6 (ajustable)
min_spacing_pct:      0.006 (0.6%)
atr_multiplier:       1.5
order_size_usdt:      20-30 USDT (ajustable)
leverage:             1 (spot)
adx_threshold:        25
ema_fast:             50
ema_slow:             200
max_drawdown_pct:     0.15 (15%)
max_daily_loss_pct:   0.01 (1%)
```

### 1.2 Exchange: Bybit

- **Librería principal:** `pybit 5.x` (SDK oficial de Bybit) para conexión, WebSocket y ejecución de órdenes.
- **Librería auxiliar:** `ccxt` únicamente para descarga de datos históricos OHLCV en backtesting.
- **Soporte de Testnet:** El bot debe poder cambiar entre testnet y mainnet con un solo parámetro de configuración.

**Conexiones requeridas:**
- WebSocket público (`wss://stream.bybit.com/v5/public/spot`): klines (velas), orderbook, ticker en tiempo real.
- WebSocket privado (`wss://stream.bybit.com/v5/private`): notificaciones de fills, cambios de balance, actualizaciones de posición.
- REST privado (`https://api.bybit.com/v5/`): crear/cancelar órdenes, consultar balance, estado de posiciones.

**Implementa reconexión automática robusta para WebSocket:**
- 3 reintentos con backoff exponencial.
- Si falla → cancela todas las órdenes → alerta Telegram → guarda estado en SQLite → cierre seguro.

### 1.3 Gestión de Riesgo (Circuit Breakers)

Implementa un `RiskManager` con los siguientes circuit breakers que se evalúan cada ciclo:

1. **Drawdown máximo (15%):** Si el capital actual cae un 15% respecto al pico máximo registrado → cancelar todas las órdenes → apagar el bot → alerta.
2. **Pérdida diaria máxima (1%):** Si la pérdida del día supera el 1% → pausar el bot 24h → alerta.
3. **Movimiento brusco de precio:** Si el precio se mueve más del 8% en 1 hora → cancelar todo → alerta.
4. **Stop Loss hard:** Configurado directamente en el exchange (no solo en el bot).
5. **Verificación de posiciones al arranque:** `recover_from_crash()` detecta órdenes huérfanas de sesiones anteriores.

### 1.4 Dead Man's Switch (DMS)

- **Proceso completamente independiente** del bot principal.
- Monitoriza un archivo de heartbeat (`bot_heartbeat.txt`) que el bot actualiza cada ciclo.
- Si el heartbeat no se actualiza en 120 segundos → cancela todas las órdenes de emergencia via REST directo a Bybit.
- No depende de ningún módulo del bot — usa su propia conexión `pybit` independiente.

### 1.5 Graceful Shutdown

- Captura `SIGTERM` y `SIGINT`.
- Cancela todas las órdenes abiertas antes de cerrar.
- Guarda el estado completo en SQLite.
- Envía notificación a Telegram confirmando el apagado correcto.

### 1.6 Base de Datos (SQLite)

Diseña un schema que almacene:
- **Trades ejecutados:** timestamp, side, price, qty, fee, pnl, status.
- **Estado del grid:** niveles activos, órdenes pendientes, last_sync_time.
- **Métricas de rendimiento:** PnL acumulado, drawdown, win rate, Sharpe ratio (cálculo rolling).
- **Logs de eventos:** circuit breakers activados, reconexiones, errores.
- **Configuración histórica:** snapshots de los parámetros usados en cada periodo.

### 1.7 Notificaciones (Telegram)

Bot de Telegram (`python-telegram-bot 20.x`) con alertas para:
- Inicio/parada del bot.
- Circuit breaker activado (urgente).
- Resumen diario: PnL, trades ejecutados, drawdown, uptime.
- Errores críticos en tiempo real.
- Dead Man's Switch activado.
- Comandos interactivos: `/status`, `/stop`, `/start`, `/resume`, `/config`.

### 1.8 Backtesting

Motor de backtesting event-driven que:
- Descarga mínimo 6 meses de datos OHLCV de Bybit via `ccxt`.
- Simula la ejecución del grid con comisiones reales (0.01% maker) y slippage estimado (0.01%).
- Walk-forward testing: entrena en primeros 4 meses, valida en últimos 2.
- Genera métricas: net PnL, max drawdown, Sharpe ratio, total trades, win rate.
- Exporta resultados en formato compatible con la GUI para visualización.

### 1.9 Monitorización Externa

- Integración con **healthchecks.io** (free tier): el bot envía un ping periódico.
- Si el ping falta → healthchecks.io envía alerta por email.
- Es una capa de seguridad adicional independiente del DMS.

---

## SECCIÓN 2: INTERFAZ GRÁFICA (GUI / DASHBOARD)

### 2.1 Requisitos Generales de Diseño

La interfaz debe seguir la **estética de plataformas profesionales de trading y gestión financiera** como TradingView, Bloomberg Terminal, eToro, Interactive Brokers o Binance Pro. Específicamente:

- **Tema oscuro (Dark Mode) como predeterminado**, con opción de tema claro. Los colores oscuros dominantes deben ser negros profundos (#0a0a0f, #12131a) y grises oscuros (#1a1b26, #1e1f2e), no grises medianos genéricos.
- **Paleta de colores financiera profesional:**
  - Verde para beneficios/positivo: `#00c087` o similar (verde esmeralda, no verde chillón).
  - Rojo para pérdidas/negativo: `#ef4444` o `#ff4757`.
  - Azul para acciones principales y acentos: `#3b82f6` o `#6366f1`.
  - Amarillo/ámbar para advertencias: `#f59e0b`.
  - Texto principal blanco sobre fondo oscuro, texto secundario en gris claro (`#94a3b8`).
- **Tipografía profesional:** Inter, JetBrains Mono (para datos numéricos y precios), o similar sans-serif moderna.
- **Bordes sutiles** con `rgba(255,255,255,0.06)` o similar, no bordes sólidos gruesos.
- **Glassmorphism sutil** en paneles: `backdrop-filter: blur()` con transparencia ligera, sin exagerar.
- **Micro-animaciones fluidas:** Transiciones en hover, aparición progresiva de paneles, indicadores de carga elegantes.
- **Diseño responsive** que funcione en desktop (1920×1080 optimizado) y sea usable en tablets.
- **Espaciado generoso** entre elementos. No amontonar datos — priorizar legibilidad.
- **Gráficos y charts** con librerías profesionales: Lightweight Charts (TradingView), Chart.js, o Recharts.

### 2.2 Tecnología de la GUI

Evalúa y recomienda la mejor opción entre:

**Opción A — Web Dashboard (recomendada):**
- Backend API: **FastAPI** (Python) sirviendo los datos del bot via REST + WebSocket.
- Frontend: **React/Next.js** o **HTML/CSS/JS vanilla** con diseño premium.
- Ventaja: accesible desde cualquier dispositivo, incluyendo el móvil. Despliegue en el mismo VPS que el bot.

**Opción B — Desktop App:**
- **PyQt6** o **PySide6** con QSS (Qt Style Sheets) personalizados.
- Ventaja: integración directa con el proceso del bot.

**Opción C — Hybrid (Electron + Python backend):**
- Frontend web dentro de una app de escritorio.
- Mayor complejidad de despliegue.

> **Nota para la IA:** Elige la opción que maximice la profesionalidad visual, la facilidad de uso y la viabilidad técnica con el stack Python ya definido. Justifica tu elección y diseña la arquitectura completa de la GUI.

### 2.3 Paneles y Vistas del Dashboard

El dashboard debe tener las siguientes vistas/pestañas principales:

#### 2.3.1 Dashboard Principal (Overview)

Vista general con los KPIs más importantes en cards/widgets:
- **Capital actual** vs. capital inicial (con porcentaje y flecha de tendencia).
- **PnL del día / semana / mes / total** (con gráfico sparkline inline).
- **Drawdown actual** vs. drawdown máximo permitido (barra de progreso visual).
- **Estado del bot:** Running / Paused / Stopped / Emergency (con indicador LED animado).
- **Uptime** del bot.
- **Último trade ejecutado** con detalles.
- **Grid activo:** tabla visual mostrando los niveles actuales del grid con precios, estado (pending/filled/cancelled).
- **Mini-chart de precio** con los niveles del grid superpuestos (como líneas horizontales).

#### 2.3.2 Panel de Trading / Grid View

- **Gráfico de velas (candlestick)** interactivo estilo TradingView con:
  - Velas en tiempo real.
  - Líneas horizontales mostrando los niveles del grid activo.
  - Indicadores superpuestos: EMA 50 (línea), EMA 200 (línea), bandas ATR.
  - Marcadores visuales de trades ejecutados (triángulos buy/sell en el chart).
- **Libro de órdenes simplificado** (depth chart o tabla de bid/ask).
- **Panel lateral** con:
  - ADX actual + indicador visual (verde si < 25 "Grid activo", rojo si > 25 "Grid pausado").
  - ATR actual y spacing calculado.
  - Sesgo de tendencia (Long/Short basado en EMAs).

#### 2.3.3 Panel de Rendimiento (Performance)

- **Gráfico de equity curve:** Evolución del capital en el tiempo (línea suave).
- **Gráfico de PnL diario:** Barras (verde/rojo) por día.
- **Tabla de métricas estadísticas:**
  - Total trades / Trades ganadores / Trades perdedores.
  - Win rate (%).
  - Ganancia promedio por trade.
  - Pérdida promedio por trade.
  - Ratio ganancia/pérdida.
  - Sharpe ratio.
  - Max drawdown (%) y fecha.
  - Profit factor.
  - Calmar ratio.
- **Gráfico de distribución de retornos** (histograma).
- **Selector de período:** 24h / 7d / 30d / 90d / All time.

#### 2.3.4 Panel de Gestión de Riesgo

- **Indicadores visuales de circuit breakers:**
  - Drawdown actual vs. límite (gauge/semicircle chart con zonas verde/amarillo/rojo).
  - Pérdida diaria actual vs. límite.
  - Movimiento de precio en la última hora.
- **Historial de activaciones de circuit breakers** (tabla con timestamp, tipo, motivo, acción tomada).
- **Estado del Dead Man's Switch** (último heartbeat, tiempo desde el último).
- **Estado de la conexión API** (latencia, último ping exitoso, reconexiones recientes).

#### 2.3.5 Panel de Configuración (Settings)

- **Formulario visual** para editar todos los parámetros del grid en tiempo real:
  - Sliders para `num_levels`, `min_spacing_pct`, `atr_multiplier`, `order_size_usdt`.
  - Toggles para activar/desactivar filtros (ADX, EMA).
  - Input numéricos para umbrales de riesgo.
- **Validación en tiempo real:** Los campos deben validar que los valores son coherentes (ej: `order_size × num_levels ≤ capital`).
- **Preview del impacto:** Antes de aplicar cambios, mostrar una visualización de cómo quedaría el grid con los nuevos parámetros.
- **Gestión de API Keys:** Interfaz segura para configurar las claves (campos password, nunca mostrar el secret completo).
- **Configuración de Telegram:** Token, chat ID, test de conexión.
- **Selector testnet/mainnet** con confirmación doble para mainnet.

#### 2.3.6 Panel de Backtesting

- **Formulario de configuración del backtest:**
  - Par, timeframe, rango de fechas, parámetros del grid a testear.
  - Botón "Ejecutar Backtest" con barra de progreso.
- **Resultados visuales:**
  - Equity curve del backtest.
  - Tabla de métricas completa.
  - Chart de trades simulados sobre velas históricas.
  - Comparativa side-by-side de diferentes configuraciones.

#### 2.3.7 Panel de Logs y Eventos

- **Log en tiempo real** estilo terminal con colores por nivel (INFO=blanco, WARNING=amarillo, ERROR=rojo, CRITICAL=rojo pulsante).
- **Filtros:** Por nivel, por módulo (strategy, exchange, risk, notifier), por rango de fechas.
- **Búsqueda** dentro de los logs.
- **Exportación** a archivo.

#### 2.3.8 Historial de Trades

- **Tabla interactiva** con todos los trades ejecutados:
  - Columnas: timestamp, side, price, qty, fee, PnL, estado, tipo de orden.
  - Ordenable y filtrable por cualquier columna.
  - Paginación.
  - Exportación a CSV/Excel.
- **Detalle expandible** de cada trade al hacer clic.

### 2.4 Controles Globales del Bot

Siempre visibles en un **header fijo o sidebar**:
- **Botón Start/Stop** del bot (con confirmación).
- **Botón Pause/Resume** (pausa sin cancelar órdenes existentes).
- **Botón Emergency Stop** (rojo, prominente, cancela todo inmediatamente).
- **Indicador de estado** del bot (LED verde/amarillo/rojo + texto).
- **Indicador de conexión** al exchange (latencia actual).

---

## SECCIÓN 3: STACK TECNOLÓGICO COMPLETO

### Backend (Motor de Trading)

```
Python 3.12+
pybit 5.x          — SDK oficial Bybit (ejecución + WebSocket)
ccxt                — Solo para datos históricos en backtesting
pandas + pandas-ta  — Análisis técnico (ATR, ADX, EMA)
SQLite + sqlite3    — Base de datos local
python-dotenv       — Gestión de API keys
python-telegram-bot 20.x — Alertas
loguru              — Logging avanzado
APScheduler 3.x     — Tareas periódicas
pytest              — Tests
aiohttp / httpx     — Requests async si necesario
```

### Frontend / GUI (según la opción seleccionada)

```
# Si Web Dashboard (recomendado):
FastAPI             — API backend
uvicorn             — Servidor ASGI
websockets          — Datos en tiempo real al frontend
Jinja2 o React      — Renderizado
Lightweight Charts  — Gráfico de velas (librería de TradingView, gratuita)
Chart.js / ApexCharts — Otros gráficos
```

### Infraestructura (Producción: VPS Hetzner Cloud)

```
Desarrollo:       Local (Windows) — testnet de Bybit
Producción:       VPS Hetzner Cloud — CX22 (2 vCPU ARM, 4GB RAM, 40GB SSD NVMe)
Ubicación:        Datacenter Falkenstein o Frankfurt (Alemania) — baja latencia a Bybit EU
Sistema operativo: Ubuntu 24.04 LTS (soporte hasta 2029)
Precio:           ~4.51€/mes (IVA incluido)
Proceso bot:      systemd service (auto-restart, logs nativos con journalctl)
Proceso DMS:      systemd service separado e independiente
Dashboard:        Nginx como reverse proxy + SSL (Let's Encrypt gratuito)
Monitoring:       healthchecks.io (free tier) + UptimeRobot (free tier)
Backups:          Hetzner snapshots automáticos (~0.01€/GB/mes) + export SQLite diario
```

> **¿Por qué Hetzner Cloud?** Es el proveedor europeo con mejor relación calidad/precio para este caso de uso. Datacenters en Alemania (GDPR compliant), latencia ~15-30ms a los servidores de Bybit en Frankfurt, precio imbatible (4.51€/mes), y sin sorpresas en facturación. Alternativas como DigitalOcean (~6$/mes) o Linode (~5$/mes) también serían válidas pero son más caras y con datacenters más lejanos.

---

## SECCIÓN 4: ESTRUCTURA DE ARCHIVOS COMPLETA

Genera la estructura de archivos completa del proyecto, incluyendo backend y frontend. Ejemplo orientativo:

```
trading_bot/
├── .env.example              # Template de variables de entorno
├── .gitignore
├── requirements.txt          # Dependencias Python con versiones exactas
├── README.md                 # Documentación profesional del proyecto
│
├── config/
│   ├── settings.py           # Configuración central (Pydantic Settings)
│   └── defaults.json         # Valores por defecto
│
├── core/
│   ├── __init__.py
│   ├── exchange.py           # Wrapper pybit — conexión, órdenes, balance
│   ├── strategy.py           # Grid dinámico + filtros ATR/ADX/EMA
│   ├── risk_manager.py       # Circuit breakers, position sizing, drawdown
│   ├── order_manager.py      # Gestión del ciclo de vida de órdenes
│   └── indicators.py         # Cálculos técnicos (ATR, ADX, EMA)
│
├── data/
│   ├── __init__.py
│   ├── database.py           # SQLite — schema, queries, estado persistente
│   ├── models.py             # Modelos de datos (dataclasses / Pydantic)
│   └── migrations/           # Migraciones de schema si necesario
│
├── services/
│   ├── __init__.py
│   ├── notifier.py           # Alertas Telegram
│   ├── dead_mans_switch.py   # Proceso independiente de seguridad
│   ├── health_monitor.py     # Healthchecks.io + estado interno
│   └── scheduler.py          # APScheduler — tareas periódicas
│
├── backtesting/
│   ├── __init__.py
│   ├── data_loader.py        # Descarga OHLCV con ccxt
│   ├── engine.py             # Motor de backtesting event-driven
│   └── reporter.py           # Generación de reportes y métricas
│
├── api/                      # API para el dashboard
│   ├── __init__.py
│   ├── app.py                # FastAPI app principal
│   ├── routes/
│   │   ├── dashboard.py      # Endpoints overview
│   │   ├── trading.py        # Endpoints de trading/grid
│   │   ├── performance.py    # Endpoints de métricas
│   │   ├── config.py         # Endpoints de configuración
│   │   ├── backtest.py       # Endpoints de backtesting
│   │   └── logs.py           # Endpoints de logs
│   ├── websocket.py          # WebSocket para datos en tiempo real
│   └── middleware.py         # Auth, CORS, rate limiting
│
├── dashboard/                # Frontend
│   ├── index.html
│   ├── css/
│   │   └── styles.css        # Design system completo
│   ├── js/
│   │   ├── app.js            # Lógica principal
│   │   ├── charts.js         # Configuración de gráficos
│   │   ├── websocket.js      # Conexión WebSocket
│   │   └── components/       # Componentes reutilizables
│   └── assets/
│       ├── fonts/
│       └── icons/
│
├── tests/
│   ├── test_strategy.py
│   ├── test_risk_manager.py
│   ├── test_exchange.py
│   ├── test_database.py
│   └── test_api.py
│
├── main.py                   # Entry point del bot + graceful shutdown
└── run_dashboard.py          # Entry point del dashboard
```

---

## SECCIÓN 5: REQUISITOS DE CALIDAD Y PROFESIONALISMO

### Código

- **Type hints** en todas las funciones y clases.
- **Docstrings** profesionales (Google style) en todos los módulos, clases y funciones públicas.
- **Manejo de errores exhaustivo:** try/except específicos, nunca `except Exception` genérico sin re-raise o logging.
- **Logging consistente** con `loguru` en todos los módulos.
- **Principios SOLID** aplicados donde sea relevante.
- **Patrón de inyección de dependencias** para facilitar testing.
- **Async/await** donde mejore el rendimiento (WebSocket handling, API serves).

### Tests

- **Tests unitarios** para: lógica del grid, cálculos de indicadores, risk manager, circuit breakers.
- **Tests de integración** para: conexión mock al exchange, flujo completo de un ciclo del bot.
- **Coverage mínimo:** 80% en módulos core.
- **Fixtures** con datos de mercado reales para tests reproducibles.

### Seguridad

- API keys **nunca hardcoded**, siempre en `.env` (incluido en `.gitignore`).
- El dashboard debe tener **autenticación** (al menos usuario/contraseña básica o JWT).
- **Rate limiting** en la API del dashboard.
- **CORS** configurado correctamente.
- **Input validation** con Pydantic en todos los endpoints.

---

## SECCIÓN 6: FASES DE IMPLEMENTACIÓN

El plan de implementación debe dividir el trabajo en fases claras y secuenciales, cada una con:
- **Archivos a crear/modificar.**
- **Código fuente completo** (no pseudocódigo ni esqueletos).
- **Criterios de éxito verificables.**
- **Tests asociados.**

### Fases sugeridas:

1. **FASE 0 — Proyecto base:** Estructura de carpetas, dependencias, configuración, `.env`, logging, database schema.
2. **FASE 1 — Conexión Exchange:** `exchange.py` completo con pybit, WebSocket público y privado, reconexión automática.
3. **FASE 2 — Estrategia Grid:** `strategy.py`, `indicators.py`, `order_manager.py` completos con lógica de grid dinámico + filtros.
4. **FASE 3 — Gestión de Riesgo:** `risk_manager.py` completo con todos los circuit breakers.
5. **FASE 4 — Servicios auxiliares:** Telegram, Dead Man's Switch, healthchecks.io, scheduler.
6. **FASE 5 — Entry Point:** `main.py` con loop principal, graceful shutdown, recovery.
7. **FASE 6 — Backtesting:** Motor completo con descarga de datos, simulación y generación de reportes.
8. **FASE 7 — API Backend del Dashboard:** FastAPI con todos los endpoints, WebSocket, auth.
9. **FASE 8 — Frontend Dashboard:** HTML/CSS/JS completo con todos los paneles descritos en la Sección 2.
10. **FASE 9 — Tests y QA:** Suite de tests completa, verificación end-to-end.
11. **FASE 10 — Documentación:** README profesional, guía de usuario, guía de contribución.
12. **FASE 11 — Despliegue en VPS (Hetzner Cloud):** Provisión del servidor, hardening de seguridad, configuración de systemd para bot + DMS, Nginx reverse proxy con SSL para el dashboard, firewall UFW, backups automáticos, scripts de deploy automatizados. Ver Sección 9 para el detalle completo.

---

## SECCIÓN 7: CONTEXTO REGULATORIO Y FISCAL

El bot será operado desde **España** por un individuo con capital personal. Los aspectos legales y fiscales relevantes son:

- Las ganancias tributan como **ganancias patrimoniales** en el IRPF (19%-27% según tramo).
- No es necesario registro ante ningún organismo con 500€ de capital personal.
- El Modelo 721 (declaración de criptos en el extranjero) solo aplica si el saldo supera 50.000€.
- Los modelos 172/173 los presenta el exchange (Bybit), no el usuario.
- Las comisiones pagadas son gasto deducible.
- Se recomienda usar **Koinly** o **CoinTracking** para el cálculo fiscal anual.
- La GUI debería incluir una opción de **exportar el historial de trades en CSV** compatible con estas herramientas.

---

## SECCIÓN 8: DESPLIEGUE EN VPS — GUÍA COMPLETA (HETZNER CLOUD)

Esta sección detalla **todo lo necesario** para que el bot y el dashboard funcionen 24/7 en un servidor en la nube, sin depender del PC del usuario.

### 8.1 Provisión del Servidor

**Proveedor elegido: Hetzner Cloud** (https://www.hetzner.com/cloud)

```
Plan:               CX22 (ARM64, Ampere Altra)
CPU:                2 vCPU
RAM:                4 GB
Disco:              40 GB SSD NVMe
Tráfico:            20 TB/mes incluidos
Ubicación:          Falkenstein (fsn1) o Frankfurt (nbg1), Alemania
Sistema operativo:  Ubuntu 24.04 LTS
IP:                 IPv4 pública incluida
Coste:              3.79€/mes + IVA = ~4.51€/mes
Facturación:        Por hora (puedes apagarlo si necesitas pausar)
```

**Genera los siguientes scripts e instrucciones paso a paso:**

1. Crear cuenta en Hetzner Cloud y dar de alta el proyecto.
2. Crear el servidor CX22 desde la consola web o via CLI (`hcloud`).
3. Configurar la clave SSH para acceso seguro (nunca contraseña).
4. Primer acceso via SSH y actualización del sistema.

### 8.2 Hardening de Seguridad del Servidor

El plan debe incluir un **script de hardening** (`scripts/server_setup.sh`) que ejecute:

```
1. Actualizar todos los paquetes:        apt update && apt upgrade -y
2. Crear usuario no-root para el bot:    adduser botuser (sin privilegios sudo innecesarios)
3. Deshabilitar login de root por SSH:   PermitRootLogin no
4. Deshabilitar autenticación por password: PasswordAuthentication no
5. Cambiar puerto SSH (opcional):        Port 2222 o similar
6. Instalar y configurar UFW (firewall):
   - Permitir SSH (puerto elegido)
   - Permitir HTTPS (443) para el dashboard
   - Permitir HTTP (80) solo para redirección a HTTPS
   - Denegar todo lo demás
7. Instalar fail2ban:                    Protección contra fuerza bruta SSH
8. Configurar actualizaciones automáticas de seguridad: unattended-upgrades
```

### 8.3 Instalación del Entorno Python

```bash
# Instrucciones que el plan debe generar:
sudo apt install python3.12 python3.12-venv python3-pip -y
cd /home/botuser
git clone <repo-url> trading_bot
cd trading_bot
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env   # Configurar API_KEY, API_SECRET, TELEGRAM_TOKEN, etc.
```

### 8.4 Configuración de systemd — Bot Principal

Genera el archivo de servicio `/etc/systemd/system/trading-bot.service`:

```ini
[Unit]
Description=Trading Bot - Grid Dinámico ATR/ADX
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=botuser
Group=botuser
WorkingDirectory=/home/botuser/trading_bot
ExecStart=/home/botuser/trading_bot/venv/bin/python main.py
Restart=always
RestartSec=15
StartLimitIntervalSec=300
StartLimitBurst=5
EnvironmentFile=/home/botuser/trading_bot/.env
StandardOutput=journal
StandardError=journal
SyslogIdentifier=trading-bot

[Install]
WantedBy=multi-user.target
```

### 8.5 Configuración de systemd — Dead Man's Switch

Genera `/etc/systemd/system/trading-dms.service` como servicio **completamente independiente**:

```ini
[Unit]
Description=Trading Bot - Dead Man's Switch (Seguridad Independiente)
After=network-online.target
# NO depende del trading-bot.service — es deliberado

[Service]
Type=simple
User=botuser
Group=botuser
WorkingDirectory=/home/botuser/trading_bot
ExecStart=/home/botuser/trading_bot/venv/bin/python -m services.dead_mans_switch
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 8.6 Configuración de systemd — Dashboard (FastAPI + Uvicorn)

Genera `/etc/systemd/system/trading-dashboard.service`:

```ini
[Unit]
Description=Trading Bot Dashboard (FastAPI)
After=network-online.target trading-bot.service

[Service]
Type=simple
User=botuser
Group=botuser
WorkingDirectory=/home/botuser/trading_bot
ExecStart=/home/botuser/trading_bot/venv/bin/uvicorn api.app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 8.7 Nginx como Reverse Proxy + SSL

El dashboard **no debe exponerse directamente**. Nginx actúa de intermediario con HTTPS:

```
1. Instalar Nginx:                     sudo apt install nginx -y
2. Instalar Certbot (Let's Encrypt):   sudo apt install certbot python3-certbot-nginx -y
3. Configurar el virtual host de Nginx para:
   - Servir el dashboard en el puerto 443 (HTTPS)
   - Hacer proxy_pass a 127.0.0.1:8000 (Uvicorn)
   - Soporte de WebSocket (proxy_set_header Upgrade, Connection)
   - Redirigir HTTP → HTTPS automáticamente
4. Obtener certificado SSL gratuito:   sudo certbot --nginx -d tu-dominio.com
5. Configurar renovación automática:   certbot renew (cron cada 12h)
```

**Si el usuario no tiene dominio propio**, el plan debe incluir la alternativa de acceder por IP directa con un certificado autofirmado, o usar un servicio gratuito de DNS dinámico como **DuckDNS** (gratuito, sin registro de dominio).

### 8.8 Firewall (UFW) — Configuración Final

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 2222/tcp    # SSH (puerto personalizado)
sudo ufw allow 80/tcp      # HTTP (redirige a HTTPS)
sudo ufw allow 443/tcp     # HTTPS (dashboard)
sudo ufw enable
```

### 8.9 Scripts de Deploy Automatizado

Genera un script `scripts/deploy.sh` que el usuario pueda ejecutar desde su PC para actualizar el bot en producción:

```bash
#!/bin/bash
# deploy.sh — Ejecutar desde el PC local
# Uso: ./scripts/deploy.sh

SERVER="botuser@<IP_SERVIDOR>"
SSH_PORT=2222
REMOTE_DIR="/home/botuser/trading_bot"

echo "🚀 Desplegando actualización..."
ssh -p $SSH_PORT $SERVER "cd $REMOTE_DIR && git pull origin main"
ssh -p $SSH_PORT $SERVER "cd $REMOTE_DIR && source venv/bin/activate && pip install -r requirements.txt"
ssh -p $SSH_PORT $SERVER "sudo systemctl restart trading-bot trading-dms trading-dashboard"
echo "✅ Despliegue completado."
```

### 8.10 Backups y Recuperación

```
1. Backup de la base de datos SQLite:
   - Cron job diario que copia trading_bot.db a /home/botuser/backups/ con timestamp
   - Retención: últimos 30 días
   - Script: scripts/backup_db.sh

2. Snapshots del servidor (Hetzner):
   - Snapshot semanal automático desde la consola de Hetzner
   - Coste: ~0.01€/GB/mes (~0.40€/mes para 40GB)
   - Permite restaurar el servidor completo en minutos si algo falla

3. Repositorio Git:
   - Todo el código está en Git (GitHub/GitLab privado)
   - Si el servidor se pierde, se puede reconstruir desde cero en ~30 minutos
```

### 8.11 Monitorización en Producción

```
Capa 1 — Bot interno:        Heartbeat file + Dead Man's Switch
Capa 2 — healthchecks.io:    Ping cada 60s desde el bot → alerta email/Telegram si falla
Capa 3 — UptimeRobot:        Monitoriza que el dashboard (HTTPS) responda → alerta si cae
Capa 4 — Hetzner alertas:    Alertas nativas de CPU/RAM/disco si superan umbrales
```

### 8.12 Resumen de Costes Mensuales de Infraestructura

| Servicio | Coste |
|---|---|
| VPS Hetzner CX22 | 4.51€/mes |
| Snapshots (~40GB) | ~0.40€/mes |
| SSL (Let's Encrypt) | Gratis |
| healthchecks.io | Gratis (free tier) |
| UptimeRobot | Gratis (free tier) |
| DuckDNS (DNS dinámico) | Gratis |
| **TOTAL** | **~4.91€/mes** |

> Con un PnL estimado del 3.5%-5.2% mensual sobre 150€ (5.25€-7.80€), el coste de infraestructura se cubre con el primer mes de operaciones. A medida que el capital crece, el ratio coste/beneficio mejora exponencialmente.

---

## SECCIÓN 9: CRITERIOS DE ACEPTACIÓN GLOBALES

El plan de implementación estará completo cuando:

- [ ] Todos los módulos backend tienen código fuente completo y funcional.
- [ ] La GUI/Dashboard tiene todas las vistas descritas con diseño profesional dark-mode.
- [ ] El bot puede ejecutarse en testnet y colocar/gestionar órdenes automáticamente.
- [ ] Los circuit breakers funcionan y se han testeado.
- [ ] El Dead Man's Switch funciona como proceso independiente.
- [ ] Las notificaciones de Telegram funcionan.
- [ ] El backtester genera métricas con datos reales de Bybit.
- [ ] La GUI muestra datos en tiempo real via WebSocket.
- [ ] La configuración del bot es editable desde la GUI.
- [ ] Existen tests unitarios para los componentes críticos.
- [ ] El dashboard tiene autenticación.
- [ ] El proyecto tiene documentación README profesional.
- [ ] La estructura del proyecto sigue buenas prácticas de ingeniería de software.
- [ ] El VPS está provisionado con hardening de seguridad completo.
- [ ] Los tres servicios (bot, DMS, dashboard) corren como servicios systemd independientes.
- [ ] Nginx sirve el dashboard con HTTPS (SSL gratuito via Let's Encrypt o certificado autofirmado).
- [ ] El firewall UFW está configurado y activo.
- [ ] Existen scripts de deploy automatizado y backup.
- [ ] La monitorización multicapa está operativa (healthchecks.io + UptimeRobot).

---

## INSTRUCCIONES FINALES PARA LA IA

1. **Genera un plan de implementación completo**, no un resumen. Cada fase debe tener código funcional.
2. **Prioriza la profesionalidad visual del dashboard.** Este es un diferenciador clave. Debe verse como una herramienta financiera real, no como un proyecto de estudiante.
3. **No uses placeholders ni `TODO`.** Si un componente es necesario, impleméntalo completamente.
4. **Justifica las decisiones técnicas** cuando haya alternativas (ej: por qué FastAPI sobre Flask, por qué Lightweight Charts sobre Chart.js para velas).
5. **El código debe ser production-ready:** type hints, error handling, logging, validación de inputs.
6. **Respeta el stack tecnológico definido** (Python, pybit, SQLite, etc.). No cambies tecnologías sin justificación sólida.
7. **El usuario final no es un desarrollador experto.** La GUI debe ser intuitiva, con tooltips, estados claros y feedback visual inmediato.
8. **Incluye instrucciones de despliegue completas** tanto local (Windows, para desarrollo) como en producción (Ubuntu VPS Hetzner con systemd, Nginx, SSL, firewall).
9. **La sección de VPS (Sección 8) debe generar scripts ejecutables** (`server_setup.sh`, `deploy.sh`, `backup_db.sh`) listos para copiar y pegar, no solo explicaciones teóricas.
10. **Incluye un diagrama de arquitectura** (Mermaid o similar) mostrando cómo interactúan: el usuario → Nginx → Dashboard → FastAPI → Bot → Bybit API, y cómo el DMS y healthchecks.io son independientes.
