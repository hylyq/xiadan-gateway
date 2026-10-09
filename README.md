# xiadan-gateway

A trading gateway for TongHuaShun `xiadan.exe` — controls the THS order-entry program through an HTTP API.

> 🌐 **中文文档**：[README.zh-CN.md](README.zh-CN.md)

> ## ⚠️ Disclaimer
>
> **This project is provided for learning and research purposes only. Users assume all risks and responsibilities arising from its use.**
>
> - This project is **not investment advice**; it does not recommend stocks, predict market movements, or provide trading strategies
> - Stock investing carries the risk of **total loss of principal**; past performance does not guarantee future results
> - Any trades executed with this software and their profits or losses are **entirely the user's responsibility**
> - The author is **not liable** for any direct or indirect losses resulting from the use or misuse of this software
> - Ensure your trading activities **comply with local laws and regulations** and your broker's terms of service
> - **The market carries risk; invest with caution. Fully understand the risks before entering the market.**

## Table of Contents

- [Core Features](#core-features)
- [Quick Start](#quick-start) (incl. [Prerequisites: Broker Software Settings](#prerequisites-broker-software-settings) / [Unattended Operation on a Server (RDP Works; VNC Optional)](#unattended-operation-on-a-server-rdp-works-vnc-optional))
- [Required Before Going Live (Security Checklist)](#required-before-going-live-security-checklist)
- [Configuration](#configuration)
- [API](#api): [Response Format](#response-format) / [Error Codes](#error-codes) / [Endpoints](#endpoints) / [Place Order](#post-orders--place-order) / [Order Status & Fill Report](#get-ordersentrust_nostatus--order-status--fill-report) / [Cancel Orders](#post-orderscancel-all--cancel-orders) / [Auxiliary Endpoints](#auxiliary-endpoints) / [Client Timeout Configuration](#client-timeout-configuration)
- [MCP Server (Agent Access)](#mcp-server-agent-access)
- [Skill Access (Any LLM Tool)](#skill-access-any-llm-tool)
- [How It Works](#how-it-works)
- [Key Design](#key-design): [Task Queue and Watchdog](#task-queue-and-watchdog) / [Idempotency and Price Validation](#idempotency-and-price-validation) / [Classified Popup Handling](#classified-popup-handling) / [Limit/Market Mode Switching](#limitmarket-mode-switching) / [Query Panel Standardization](#query-panel-standardization) / [Captcha OCR](#captcha-ocr--lightweight-template-matching) / [Performance Measurements](#performance-measurements)
- [Known Limitations](#known-limitations)
- [Project Structure](#project-structure)
- [Tech Stack](#tech-stack)
- [Development](#development) (incl. [Adding a New Clean-Exit Scenario](#adding-a-new-clean-exit-scenario))

## Core Features

| Feature | Description |
|---------|-------------|
| Single instance | Windows global mutex guarantees only one instance runs at a time |
| Sequential execution | Single-worker task queue avoids concurrent conflicts on `xiadan.exe` |
| Consecutive clean skip | Clean exit from previous task → skip window reset + activation. Fully skipped within the same group and direction; cross-direction only re-presses F1/F2. Groups: `trade` (buy/sell), `cancel`, `query` (see [Task Queue and Watchdog](#task-queue-and-watchdog)) |
| Query traversal reuse | Position/trades/orders queries: one `descendants` traversal serves tree lookup + popup detection + fallback scan, cutting ~40% of navigation time |
| Message-based table copy (experimental) | With `query.copy_method=message`, table copy is sent as a `WM_COMMAND(0xE122)` message — no foreground activation, no synthetic keystrokes, immune to IME interception and `GetAsyncKeyState` delays, works while the window is obscured or unfocused. **The captcha is independent of the invocation method — most copies still trigger it**; the gain comes from the copy stage itself (numbers in [Performance Measurements](#performance-measurements)). Falls back to keyboard automatically on failure; default remains keyboard |
| Pipelined mode switching | Click the limit/market toggle without waiting, immediately fill the quantity — the ~0.7s fill overlaps the label change; verification is naturally ready after filling |
| Classified popup handling | Order confirm → Y/N; warning → Y to continue; **price out of range → N to cancel + `PRICE_OUT_OF_RANGE`**; error → close + report (see [Classified Popup Handling](#classified-popup-handling)) |
| Watchdog recovery | On task timeout: screenshot + activate + ESC×3, reset, then return an error |
| Idempotency | **Required** `Idempotency-Key` header (1–128 chars) — one key per logical order: same key within the window is rejected (`DUPLICATE_ORDER`, HTTP-timeout retry protection); a new key = a new order, identical-parameter multi-orders included |
| OCR captcha | Lightweight template-matching engine (noise filter keeps thin-but-tall strokes — a '1' is 3px wide); solve loop re-captures per retry, detects client-side rejection, archives confident misreads (`wrong_*.png`), and safely closes the dialog on exhaustion; failures auto-archived; optional ddddocr offline training |
| Production server | `waitress` WSGI + graceful shutdown (SIGINT/SIGTERM) |
| Hot config reload | `POST /admin/reload-config` without restart |
| Startup config validation | Validates config types/ranges (port/timeouts/paths) at startup; aborts with fix guidance on invalid config |
| Screenshot auto-cleanup | Cleans expired screenshots at startup (keeps 200 / last 7 days) |
| Auth security | Token compared with `hmac.compare_digest` (constant-time) |
| Cross-site defense | Requests carrying an `Origin` header are rejected (browser cross-site requests always carry it; script clients never do) — prevents malicious web pages from firing trades at the local gateway; active even when auth is disabled |
| Runtime stats | Per-error-code success rates (1-hour window, via `/health`); log alert after 3 consecutive failures; tracks order-confirm-dialog behavior and warns when the client's fast-trade setting appears reset (behavior flip) |
| Alert webhook | Consecutive task failures ≥3, order-dialog drift, and task timeouts → POST to a webhook (generic JSON or WeCom/DingTalk/Feishu `text` format) on a background thread, never blocking the trading path |
| Stock-name linkage verification | After typing the code, polls the name-linkage control (cid=1036 Static) for non-empty text — a reliable signal that the client fully parsed the code (1032 is a shell control whose read-back is always empty). On no linkage, clears and retypes once; still failing raises `INPUT_VERIFY_FAILED` and blocks submission; degrades to pass-through if the name control is missing. Disable via `order.verify_code_input` |
| Entrust-no banner capture | After the submit click, a background thread screen-grabs the bottom-right yellow banner (~12fps; it is a self-drawn overlay invisible to `PrintWindow`), localizes it by yellow mask, and reads the contract number via template OCR — parsing is length-agnostic, anchored on the trailing fullwidth period (broker/exchange display formats vary). Returns `entrust_no` on success; `null` on failure without affecting the order; optional auto-recovery via `order.recover_entrust_no` (see the [order response](#post-orders--place-order)) |
| Order status & fill report | `GET /orders/{entrust_no}/status` joins today's orders × today's fills by contract number: order status derived from quantities (not broker remark text), fill aggregation with weighted avg price and per-trade numbers — the polling counterpart of the order response for strategy callers |
| Window position self-healing | Before each task, checks window/workarea intersection (60% threshold); auto-moves the window back if it was dragged off-screen (`click_input`/screenshots are coordinate-based and fail off-screen) |
| Window visibility self-healing | Background monitor (every 2s) restores both minimized and **tray-hidden** windows (`IsIconic` **or** `IsWindowVisible` — hidden-to-tray is not iconic and was a blind spot); if soft restore keeps failing (3 consecutive rounds), relaunches the exe **of the running process** (hwnd→PID→psutil; configured `trading_app_paths` as fallback, 60s cooldown) — single-instance clients bring their existing window back, and multi-install machines never launch the wrong copy. The query path also `SW_SHOW`s hidden windows before activation |
| Session-disconnect self-healing | A background check every 10s (`win32ts` `WTSConnectState`) detects when the hosting session enters the *disconnected* state (plain RDP client disconnect, no `tscon` exit) and runs `tscon <own-id> /dest:console` itself — as the session owner this reattaches the session to the console **and clears the lock in one step**. Only acts on disconnected sessions — an interactively-used RDP session is never hijacked. **Four-layer reconnect-race protection** (`session_monitor` config, hot-reloadable): an `enabled` master switch, a `debounce_seconds` continuous-disconnect requirement (default 30s), a `cooldown_seconds` between heals (default 300s — doubles as a reconnect protection window), and an **RDP-port TCP discriminator** — an ESTABLISHED connection on 3389 while disconnected means a client is sitting at the credential prompt / negotiating, so the heal yields; the yield expires once the disconnect episode exceeds 600s (an mstsc left on the password box must not block self-healing forever). The discriminator was validated by two live experiments (session-level `WTSClientName` stays empty until the attach completes, so TCP is the only early signal); within its validity window the race is eliminated. Verified: **VNC server stopped + plain RDP disconnect → auto-recovery, all queries green** |
| Runtime popup self-healing | Every task starts (before window reset/activation) by sweeping leftover dialogs (both forms: top-level `#32770` and child-of-main-window `#32770`) — **close-only, never solve, never click OK**: leftover captchas are usually expired (a correctly-read, correctly-typed code was still rejected in practice), so solving only adds the risk of submitting wrong codes to the broker; clicking OK on unknown error popups could have side effects. Closing uses safe means only — the Cancel button or WM_CLOSE (equivalent to clicking X, dialogs default to the cancel path). Before closing, evidence is auto-archived: full-desktop screenshot + trading-window screenshot + dialog control texts into the log (a closed popup is gone forever; bounded by the screenshot cleanup policy). Fresh captchas are triggered and solved in-flow by the copy flow. Sweeping before reset prevents the activation logic's real-mouse clicks from landing on dialog buttons (observed pressing a dialog's OK repeatedly) |
| MCP adapter | `scripts/mcp_server.py` exposes the gateway as standard MCP tools for LLM agents — read-only queries always registered; `place_order`/`cancel_orders` only with `XIADAN_MCP_TRADING=1`; raw `/actions/*` never exposed (see [MCP Server](#mcp-server-agent-access)) |

## Quick Start

**Environment**: Windows / Python 3.11+ / [uv](https://github.com/astral-sh/uv) / TongHuaShun `xiadan.exe` installed

```bash
uv sync                           # install dependencies
uv run python main.py             # start the service (default http://localhost:5000)
uv run python main.py --dev       # dev mode (hot reload)
```

> **Runtime library prerequisite**: pywin32's `win32ui` depends on the Microsoft MFC runtime (`mfc140u.dll`). Clean Windows / Windows Server images often lack it — `uv sync` succeeds, but startup then fails with `ImportError: DLL load failed while importing win32ui`. Fix: install the [Visual C++ 2015-2022 Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe) (interactive install; on a headless server run it from an RDP/VNC desktop session — it can hang in a service session), then verify `C:\Windows\System32\mfc140u.dll` exists.

### Prerequisites: Broker Software Settings

Configure the following manually before starting — disabling confirmation popups speeds up trading.

**How**: In the standalone order window, top menu 「设置」(Settings) → tab 「快速交易」(Quick Trading), set all 4 options to 「否」(No):

| Setting | Required value | Reason |
|---------|:---:|--------|
| 撤单前是否需要确认 (Confirm before cancel) | **No** | Skip the cancel confirmation popup |
| 买入时是否需要确认 (Confirm on buy) | **No** | Skip the buy order confirmation popup |
| 卖出时是否需要确认 (Confirm on sell) | **No** | Skip the sell order confirmation popup |
| 委托成功后是否弹出提示对话框 (Prompt dialog after order success) | **No** | Reduce post-trade popup interference |

> Configure once. With confirmations off (quick-trading mode), orders submit directly with no popups, cutting ~1.4s per order.

### Unattended Operation on a Server (RDP Works; VNC Optional)

The gateway drives `xiadan.exe` with **real mouse/keyboard input** (`SetForegroundWindow` + `click_input` + `keybd_event`), which requires the hosting session to have an **active desktop** (a console-attached interactive session). **Plain RDP is the default and sufficient** — the gateway self-recovers after disconnect (verified); VNC is an optional convenience, not a dependency:

| Access mode | After the client disconnects | Automation |
|---|---|---|
| **RDP plain disconnect (default path)** | Session disconnects + locks → **session self-healing runs `tscon` automatically (lock cleared too)** | ⚠️ Brief outage (~10-30s), then auto-recovery; during the outage window tasks are **rejected in milliseconds by the session health gate** (`SESSION_UNAVAILABLE`, task not executed) instead of waiting out the 30s watchdog ✅ Verified with the VNC server stopped: balance/positions/trades queries all recovered |
| RDP + `tscon $env:SESSIONNAME /dest:console` (PowerShell) / `tscon %sessionname% /dest:console` (cmd.exe) on exit | Session lands on the console seamlessly, no lock | ✅ Keeps working (zero-downtime path, for outage-sensitive setups) |
| VNC (optional convenience) — session lives on the console | VNC is only a mirror; the session stays attached to the console | ✅ Keeps working — connect/disconnect anytime |

**Daily flow (default, RDP)**:

1. RDP in → (first time / after reboot) start `xiadan.exe` + broker login → start the gateway in a terminal: `uv run python main.py`
2. **Just close the RDP client when done** — the gateway self-recovers within ~10-30s, no command needed (verified with the VNC server stopped throughout)
3. To come back, simply RDP in again; after handling things (e.g. broker re-login), disconnect plainly and self-healing takes over

**Rules**:

- **RDP survival rule** — an RDP reconnect pulls the session off the console back onto the RDP transport, and a plain disconnect **both disconnects and locks** the session (Windows security design; injected clicks land nowhere and foreground checks reject key delivery). **The gateway self-heals this**: a background check every 10s (`win32ts` `WTSConnectState`) detects the disconnected state and, once it has persisted for `debounce_seconds` (default 30s), runs `tscon <id> /dest:console` — as the session owner this reattaches the console and clears the lock in one step; it only acts on *disconnected* sessions and never hijacks one in interactive use; at least `cooldown_seconds` (default 300s) between heals. For instant recovery you can also run `tscon <id> /dest:console` manually (find the id with `qwinsta`)

  **⚠️ Reconnect race and the four protection layers**: a session reads *disconnected* both when the client is gone **and** while a user is reconnecting (typing credentials, console→RDP retarget transition) — session state alone cannot tell these apart, and a heal firing mid-reconnect collides with the in-flight RDP attach, potentially wedging the session's graphics stack beyond repair (observed 2026-10-06: blue "please wait" → black screen → session logoff was the only fix). Protection layers:
  - **TCP discriminator (primary, experiment-validated)**: the client's TCP connection to port 3389 is ESTABLISHED from the moment it connects and stays so through the whole credential phase; it closes when the client leaves. ESTABLISHED-on-3389 while disconnected = someone is reconnecting → the heal yields (only within the first 600s of a disconnect episode — an mstsc left on the password box must not block self-healing forever; detection errors fall back to the lower layers)
  - debounce (30s) + cooldown (300s): move "quick reconnects" and "~1-minute absences" out of the danger zone; the cooldown doubles as a protection window
  - **Master switch** (always available): before reconnecting, hot-disable the heal (`session_monitor.enabled: false` → `POST /admin/reload-config`), reconnect, confirm the desktop renders, re-enable — explicit choreography removes the race by construction
  - Drill without touching config: close the RDP client → wait for the heal to fire (log line `会话持续断开`, ~30-40s after disconnect) → reconnect inside the protection window (300s from the heal)
- **Session health gate (fast rejection during a disconnect)** — every task queries `WTSConnectState` once before execution (the same signal self-healing uses; a sub-millisecond local query). While disconnected, tasks **return `SESSION_UNAVAILABLE` in milliseconds** (the message states "task not executed") instead of being released onto a dead desktop to hang until the 30s watchdog. Recovery time is reported as a range (typically ~40s = 30s debounce + 10s check interval; up to ~340s for a re-disconnect inside the cooldown). Key points:
  - **Not executed = safe to retry**: an order's idempotency record is cleared on rejection, so after recovery reusing the same `Idempotency-Key` just executes the order (opposite of `TASK_TIMEOUT`, where it "may have executed" and must be checked first)
  - **Monitoring signal**: `session.ui_available` from `GET /health` (`false` while disconnected, `true` once recovered; `null` = query failed, not definitely down) — retry once it reads `true`
  - **Residual (accepted)**: a disconnect **mid-task** is still caught by the watchdog (30s timeout + recovery) — the gate only checks before a task starts and cannot safely preempt a half-finished click sequence; an RDP **reconnect** does not interrupt a running task (processes/windows/handles survive the console→RDP switch; only seconds-scale layout churn during the resolution change)
  - **fail-open**: any state-query error releases the task (worst case = pre-gate behavior); only the *disconnected* state is blocked — reconnect transition states pass. Escape hatch `task_queue.session_gate_enabled: false` (hot-reloadable) — independent from `session_monitor.enabled` (different failure domains: queue rejection vs. tscon action)
  - Edge case: post-order chained queries (entrust-no capture / verification) rejected during a disconnect only log a warning — the order response itself is unaffected; recover the entrust no later via `GET /orders/pending`
- **Do not lock the desktop** (`Win+L` or a locking screensaver switches to the secure desktop — automation fails; a locked-but-console-attached session reports Active and is not self-healed)
- **A Windows service / scheduled task "run whether user is logged on or not" does not work**: those run in Session 0 and cannot see or operate the windows of an interactive session (window enumeration comes up empty). Boot auto-start therefore also requires an interactive logon first — log in via RDP or VNC, then start `xiadan.exe` and the gateway
- After a reboot: RDP (or VNC) in → log on → start `xiadan.exe` + broker login → start the gateway

**VNC (optional convenience)**: install [TightVNC Server](https://www.tightvnc.com/) (runs as a Windows service and serves the console session), set a strong VNC password, and restrict the VNC port in the firewall — never expose it to the public internet (prefer an SSH tunnel). Automation does not depend on it (verified running with the server stopped); its remaining value is: console-native login after reboot (no self-healing window), zero-session-churn continuity, and mirror-style emergency inspection. Entirely optional.

## Required Before Going Live (Security Checklist)

The defaults below favor development convenience — **verify them before exposing the service**:

| Check | Default | Requirement |
|-------|:---:|------|
| `auth.enabled` | `false` | **Change to `true`** and set a strong token. Without auth, anyone who can reach the service can place/cancel orders |
| Token transport | Header only | Token only via `Authorization: Bearer <token>` or `X-API-Key` request headers — **no query string** (`?token=xxx` removed — it would leak into access logs/browser history) |
| Bind address | `127.0.0.1` | For LAN use, review firewall policy; exposing `0.0.0.0` to the public internet is at your own risk |
| `/health` info exposure | Public | Health check is always public (for monitoring probes), but no longer returns local machine info like `trading_app_paths` |

> With auth enabled, `/health` still requires no token (designed for monitoring probes).

## Configuration

Copy `config/app_config.example.json` to `config/app_config.json` and edit `trading_app_paths`:

```json
{
  "trading_app_paths": [
    "C:\\同花顺远航版\\transaction\\xiadan.exe",
    "C:\\同花顺软件\\同花顺\\xiadan.exe"
  ],
  "window_monitor": { "enabled": true, "check_interval": 2, "login_grace_seconds": 90 },
  "task_queue": {
    "max_size": 50,
    "watchdog_timeout_seconds": 30,
    "query_timeout_seconds": 30,
    "confirm_timeout_seconds": 10
  },
  "idempotency": { "order_dedup_window_seconds": 60 },
  "alerts": { "webhook_url": "", "format": "generic", "timeout_seconds": 5 },
  "ocr": { "warmup_on_start": true, "max_retry": 3, "ddddocr_enabled": false },
  "query": { "copy_method": "keyboard" },
  "order": { "capture_entrust_no": false, "entrust_no_timeout_seconds": 5.0, "verify_entrust_no": false, "verify_code_input": true, "reject_outside_trading_hours": false },
  "auth": { "enabled": false, "token": "" },
  "logging": { "level": "INFO", "file": "logs/app.log", "screenshot_dir": "logs/screenshots" }
}
```

| Key config | Default | Description |
|------------|---------|-------------|
| `trading_app_paths` | `[]` | Full paths to `xiadan.exe` (in priority order), **at least one required** |
| `task_queue.watchdog_timeout_seconds` | 30 | Order watchdog timeout (seconds) |
| `task_queue.query_timeout_seconds` | 30 | Query operation timeout (seconds) |
| `task_queue.confirm_timeout_seconds` | 10 | Confirm/keypress operation timeout (seconds) |
| `task_queue.max_size` | 50 | Max queue length |
| `idempotency.order_dedup_window_seconds` | 60 | Same-key retry-block window (seconds), calibrated to the recommended 40s client timeout |
| `alerts.webhook_url` | empty | Alert webhook (**empty=disabled**): consecutive task failures ≥3, order-dialog behavior drift, and watchdog timeouts POST JSON in the background; hot-reloadable |
| `alerts.format` | generic | `generic`=full structured JSON (custom receiver); `text`=WeCom group-bot / DingTalk custom-bot text format; `feishu`=Feishu/Lark custom-bot text format |
| `alerts.timeout_seconds` | 5 | Webhook POST timeout (seconds). Sent on a background daemon thread, never blocks the trading path |
| `ocr.max_retry` | 3 | Max captcha OCR retries |
| `order.reject_outside_trading_hours` | false | Fail fast at `place_order` entry outside trading hours (weekday + statutory holidays + 9:15-11:30 / 13:00-15:00; three-tier holiday calendar: chinesecalendar → SZSE official monthly calendar (current year fetched once and cached under `data/trading_calendar/`, offline reads once complete) → weekday-only — broker errors remain the fallback). Off by default to preserve after-hours order queuing |
| `order.capture_entrust_no` | false | After a successful order, capture the contract number from the bottom-right success banner (background screen-grab + template OCR, trailing-period anchored — broker number lengths vary). `null` on capture failure; order success is independent (see the response note). Adds ~1s on success, up to the 5s capture timeout on failure. Pair with `order.verify_entrust_no` for reconciliation |
| `order.entrust_no_timeout_seconds` | 5.0 | Banner-capture wait timeout (seconds); returns immediately on success, only slows the failure path |
| `order.recover_entrust_no` | true | When banner capture fails, look the contract number up in today's orders by **submit-click moment × parameter quadruple** (action+code+price+amount; the click second-bucket window [-1,+2] is applied to the full-precision click time as a second-bucket closure so second-granularity 委托时间 is fully covered). Adopted only on a unique match (0 or ≥2 candidates → stays `null`, never guesses). Chained as a separate queued query — adds ~6-8s only on the capture-failure path; successful captures are unaffected. Sets `entrust_no_recovered: true` when adopted |
| `order.verify_entrust_no` | false | After a successful order with a captured entrust number, automatically query today's orders to reconcile (response gains `entrust_no_verified`). On a miss, the query page is refreshed (F5) and re-copied once before reporting `false` — the broker's order list can lag a few seconds for new orders (2026-09-29 stress-test observed). Adds one query to the response time — enlarge client timeout accordingly. Requires `order.capture_entrust_no` |
| `order.verify_code_input` | true | After typing the code, verify the stock-name linkage (non-empty = code accepted); on no linkage it retypes once, then rejects submission (`INPUT_VERIFY_FAILED`). Adds ~3-6s on the failure path |
| `ocr.ddddocr_enabled` | false | ddddocr debug switch (dual-engine verification + template extraction; requires `uv sync --extra ocr`) |
| `window_monitor.enabled` | true | Window-minimized monitoring switch |
| `window_monitor.login_grace_seconds` | 90 | Login/startup stand-down: while the broker process is younger than this many seconds, a hidden/minimized window is left alone — forcing it visible during login drawing races the client's own drawing and misaligns window elements (observed 2026-10-08 when the gateway starts before the broker login). The age check lifts permanently once the client has shown the window itself (first self-show = login done; a later manual hide restores immediately, no 90s wait). Two duration-independent checks also hold: a visible login dialog (#32770 or title containing 登录) of the same process, or the foreground window being another window of the same process (user mid-login). `0` disables the age check |
| `auth.enabled` | false | Token auth switch |
| `logging.level` | INFO | Log level (set `DEBUG` for troubleshooting UIA control failures; hot-applies without restart) |

> Config reloads via `POST /admin/reload-config` (some path changes need a restart). Timeouts follow the latest performance measurements (2026-08 simulated-market: worst-case order ~8s, worst-case query ~12s incl. failure retries; 30s gives 2.5–3.5× headroom; hung tasks trigger watchdog recovery sooner and callers fail faster).

## API

### Response Format

All responses return HTTP 200; success/failure is distinguished by the JSON `status` field.

**Success**:
```json
{
  "status": "success",
  "request_id": "req_20260721_120000_a1b2c3d4e5f6",
  "timestamp": "2026-07-21 12:00:00",
  "data": { ... }
}
```

**Failure**:
```json
{
  "status": "error",
  "request_id": "req_20260721_120000_a1b2c3d4e5f6",
  "timestamp": "2026-07-21 12:00:00",
  "error_code": "VALIDATION_ERROR",
  "message": "code 参数不能为空",
  "suggestion": "请提供股票代码，如: POST /orders {\"code\": \"601991\"}"
}
```

### Error Codes

| Error code | Description |
|------------|-------------|
| `VALIDATION_ERROR` | Parameter validation failed |
| `DUPLICATE_ORDER` | Same `Idempotency-Key` submitted within the retry-block window |
| `AUTH_REQUIRED` | Auth token missing |
| `AUTH_FAILED` | Auth token invalid |
| `WINDOW_NOT_FOUND` | Trading window not found |
| `CONTROL_NOT_FOUND` | Control not found |
| `MODE_SWITCH_FAILED` | Limit/market mode switch failed |
| `ORDER_SUBMIT_FAILED` | Order submission failed (generic, includes popup text) |
| `SERVER_CLEARING` | Broker system is clearing |
| `OUTSIDE_TRADING_HOURS` | Outside trading hours |
| `T1_RESTRICTION` | T+1 restriction (bought today, sellable tomorrow) |
| `INSUFFICIENT_SHARES` | Insufficient sellable shares |
| `INSUFFICIENT_BALANCE` | Insufficient available balance (clicked OK to close, clean exit, next same-direction task can skip) |
| `SHORT_SELLING_FORBIDDEN` | Short selling not allowed — no position or exceeds sellable shares (clicked OK to close, clean exit) |
| `PRICE_OUT_OF_RANGE` | Price outside daily limit (clicked N to cancel, clean exit, next same-direction task can skip) |
| `ORDER_PRICE_REQUIRED` | Broker requires an order price (market-order type unselected/unsupported, or limit mode without a price; suggests limit mode) |
| `SERVER_UNAVAILABLE` | Broker server unavailable (e.g. transaction-processor forwarding failed) |
| `OCR_FAILED` | Captcha recognition failed |
| `INPUT_VERIFY_FAILED` | Stock-name linkage verification failed (code not accepted by the client) |
| `INTERNAL_ERROR` | Unknown exception |
| `QUEUE_TIMEOUT` | Task queuing timeout |
| `QUEUE_FULL` | Queue is full |
| `SESSION_UNAVAILABLE` | RDP session disconnected — task **not executed**, rejected in milliseconds (opposite of `TASK_TIMEOUT`: the client was definitely untouched, the idempotency record is auto-cleared, and retrying with the same `Idempotency-Key` after recovery is safe) |
| `TASK_TIMEOUT` | Task timeout, recovery succeeded |
| `TASK_TIMEOUT_RECOVERY_FAILED` | Task timeout, recovery also failed |
| `ORDER_STATE_UNKNOWN` | A non-business exception **after the submit click** (e.g. the desktop dying the instant RDP drops) — the order may already be submitted, state unknown; the idempotency record is **kept** (same-key retry is blocked), verify via order query first (use a new key once confirmed unsubmitted) |

> For popup types and "clean exit" semantics, see [Classified Popup Handling](#classified-popup-handling).

### Endpoints

| Method | Path | Description | Queued | timeout |
|--------|------|-------------|:---:|--------|
| GET | `/health` | Health check + login state (`logged_in`) + session state (`session.ui_available`, `false` while RDP-disconnected — the polling signal for recovery) + recommended client timeout + runtime stats (success rates / error-code aggregates / consecutive failures / order-dialog stats) | | 5s |
| GET | `/queue/status` | Task queue status | | 5s |
| POST | `/admin/reload-config` | Hot reload config | | 5s |
| GET | `/account/balance` | Account balance | ✓ | 40s |
| GET | `/positions` | Position query | ✓ | 40s |
| GET | `/trades/today` | Today's trades | ✓ | 40s |
| GET | `/orders/pending` | Today's orders | ✓ | 40s |
| GET | `/orders/{entrust_no}/status` | Order status + fill report by contract number (joins today's orders × trades; two table copies per call) | ✓ | 60s |
| POST | `/orders` | Place order (limit/market) | ✓ | 40s |
| POST | `/orders/cancel-all` | Cancel orders (all / buys / sells / last) | ✓ | 40s |
| POST | `/actions/send-key` | Send a key manually | ✓ | 30s |
| POST | `/actions/click` | Mouse click at coordinates | ✓ | 30s |
| POST | `/actions/close-dialog` | Close the buy/sell sub-panel | ✓ | 30s |
| GET | `/ocr/quality` | OCR quality report (accuracy/templates/coverage) | | 5s |
| GET | `/diagnostic/snapshot` | Screenshot + UI text + OCR (yields to a busy worker for up to 2s, then runs anyway; response carries a `worker_busy` flag) | | 10s |
| GET | `/diagnostic/history` | Diagnostic history of the last N tasks | | 5s |

> POST endpoints accept parameters via three channels (highest priority first): JSON body (parsed regardless of `Content-Type` — plain `curl -d '{"type":"X"}'` without headers works), query string, and form body (`curl -d type=X`, urlencoded/multipart).

### POST /orders — Place Order

| Parameter | Required | Description |
|-----------|:---:|-------------|
| `code` | ✓ | Stock code |
| `status` | ✓ | `1`=buy, `2`=sell |
| `amount` | | Order quantity |
| `price` | | Order price (limit mode, max 2 decimals) |
| `price_type` | | `limit`=limit (default), `market`=market |
| `confirm` | | `true`=auto-confirm (default). `false`=click N to cancel **only if the client pops the「委托确认」dialog** — with the recommended quick-trading setup (client confirmations off, see [Prerequisites](#prerequisites-broker-software-settings)) no dialog appears and the order submits directly, so `false` is NOT a guaranteed preview/interception |

> **A required `Idempotency-Key` header (1–128 chars) is enforced** — see [Idempotency and Price Validation](#idempotency-and-price-validation). Key lifecycle: one key per logical order, reuse it on timeout retries; use a new key for each genuinely new order (identical-parameter multi-orders included).

```bash
# Market buy
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"code":"601991","status":"1","amount":"100","price_type":"market"}'

# Limit buy
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"code":"600000","status":"1","amount":"100","price":"10.50","price_type":"limit"}'

# Market sell
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"code":"601991","status":"2","amount":"100","price_type":"market"}'
```

**Response** (`data` fields):

| Field | Description |
|-------|-------------|
| `action` / `mode` / `code` / `amount` / `price` | Echo of order parameters |
| `confirmed` | `true`=submitted (in fast-trade mode, no error popup means success) |
| `entrust_no` | Contract number. Returned only when `order.capture_entrust_no` is enabled; `null` on capture failure or when disabled. With `order.recover_entrust_no` (default on), a failed capture is retried once against today's bookings via click-time window × parameter match — adopted only on a unique hit, with `entrust_no_recovered: true` |
| `entrust_no_verified` | Reconciliation result. Returned only when `order.verify_entrust_no` is enabled and a banner number was captured: `true`=matched in today's booked orders / `false`=still not matched after one page refresh (F5) and re-copy — the banner number may be wrong, trust the query / `null`=reconciliation query failed (order-result semantics unchanged) |

> **`entrust_no: null` does NOT mean the order failed** — success is judged by
> popup detection and is decoupled from banner capture. `null` means "submitted
> (inferred), number unknown" (banner absent / occluded / window minimized).
> **Do not retry** in that case (it would duplicate the order); if you need the
> number, look it up via `GET /orders/pending` by code+price+amount+time.
> Note: under extreme conditions (broker server maintenance windows) the banner
> number may differ from the final booked number — re-verify via the day's
> order query before per-order operations, or enable `order.verify_entrust_no`
> to get the reconciliation result (`entrust_no_verified`) directly in the
> order response.

### GET /orders/{entrust_no}/status — Order Status & Fill Report

Joins today's orders × today's fills by **contract number** (the join key between the two tables; broker-generated, length varies by broker/exchange — sim client shows 10 digits, SZSE spec is 22; trade numbers are exchange-generated: SSE 16-digit zero-padded, SZSE 8-digit). One queued task runs both table copies (typically 8–15s).

- `found: false` — no order with this contract number in today's bookings (not placed today, or wrong number)
- `order.status` — derived from quantities, not broker remark text (stable across brokers): `全部成交` / `部分成交` / `部分成交后撤单` / `全部撤单` / `未成交` / `未知`（quantities missing）
- `order.is_final` — `filled + cancelled >= amount`: the order is no longer live in the market
- `fills` — per-contract fill aggregation: `count` / `total_qty` / `total_amount` / `avg_price` (weighted by amount/qty, `null` when unfilled) / `trades[]` (`time` / `trade_no` / `qty` / `price` / `amount`, sorted by time)

```bash
curl -H "X-API-Key: $TOKEN" http://localhost:5000/orders/6284424619/status
```

### POST /orders/cancel-all — Cancel Orders

Cancels run on the F3 cancel page (reachable with one F3 keypress); the cancel-all/cancel-buys/cancel-sells/cancel-last buttons share the same control IDs across pages (measured 30001/30002/30003/1946). When a confirmation popup appears after clicking (cid=1040 text + cid=6 "Yes(Y)" button), it is auto-confirmed and the cancelled count is parsed from the popup text; in quick-trading mode (no cancel confirmation) the cancel takes effect with no popup. Grayed-out buttons (nothing to cancel) return immediately.

| Parameter | Required | Description |
|-----------|:---:|-------------|
| `type` | | `A`=all (default), `X`=cancel buys, `C`=cancel sells, `L`=cancel last order |

```bash
curl -X POST http://localhost:5000/orders/cancel-all
curl -X POST http://localhost:5000/orders/cancel-all -d '{"type":"X"}'
```

**Response** (`data` fields):

| Field | Description |
|-------|-------------|
| `cancel_type` | Operation name (cancel all / buys / sells / last) |
| `success` | Whether a cancel was executed (`false` when buttons were grayed out) |
| `cancelled_count` | Cancelled count (parsed from the confirmation popup text; `null` when no popup or parse failed, `0` on the grayed-out path) |
| `confirm_dialog_shown` | Whether a cancel-confirmation popup appeared |
| `reason` | Only present when buttons were grayed out, e.g. 「当前无可撤委托」 (nothing to cancel) |

### Auxiliary Endpoints

```bash
# Send a key manually
curl -X POST http://localhost:5000/actions/send-key -d '{"key":"F1"}'

# Mouse click
curl -X POST http://localhost:5000/actions/click -d '{"x":100,"y":200}'

# Close sub-panel (switches view via F4, does not close the whole app)
curl -X POST http://localhost:5000/actions/close-dialog -d '{"title":"买入"}'

# Diagnostic snapshot (screenshot + UI control text + OCR)
curl http://localhost:5000/diagnostic/snapshot

# Diagnostic history (UI state after the last N tasks)
curl "http://localhost:5000/diagnostic/history?n=3"
```

### Client Timeout Configuration

The caller's HTTP timeout **must exceed the server's watchdog timeout + recovery time (~5s)**. Recommended: fetch it dynamically from `/health`:

```python
import requests

base_url = "http://localhost:5000"
health = requests.get(f"{base_url}/health", timeout=5).json()
timeout = health["data"]["config"]["recommended_client_timeout_seconds"]

resp = requests.post(
    f"{base_url}/orders",
    json={"code": "601991", "status": "1", "amount": "100", "price_type": "market"},
    headers={"X-API-Key": "your-token"},
    timeout=timeout,
)
print(resp.json())
```

## MCP Server (Agent Access)

A thin MCP adapter ([`scripts/mcp_server.py`](scripts/mcp_server.py)) exposes the gateway as standard MCP tools, so LLM agents (Claude Desktop, ZCode, or any stdio MCP client) can query and trade without knowing the REST details:

```
MCP client ──stdio──→ mcp_server.py ──HTTP──→ gateway (Flask) ──→ xiadan.exe
```

The exposure surface is deliberately layered:

| Layer | Tools | Availability |
|-------|-------|--------------|
| Read-only queries | `gateway_health` `get_queue_status` `get_balance` `get_positions` `get_today_trades` `get_today_orders` `get_order_status` | always registered |
| Trading | `place_order` `cancel_orders` | only with `XIADAN_MCP_TRADING=1` |
| Raw UI actions (`/actions/*`), `/admin/*` | — | never exposed to agents |

Safety design:

- The adapter imports nothing from `src/` and never touches the trading path — queue serialization, idempotency, and alerting are all inherited via the HTTP layer
- `place_order` maps `buy`/`sell` to `1`/`2`, validates locally (6-digit code, positive integer amount, ≤2-decimal price), **requires an explicit numeric price for limit orders** ("latest price" is rejected), and its description forces a confirm-then-verify workflow: query positions/balance → repeat the parameters verbatim to the user and get consent → place → verify with `get_order_status` (per-order status + fills; falls back to `get_today_orders` when the banner number wasn't captured). The API's mandatory `Idempotency-Key` is auto-filled with a uuid4 unless the agent passes `idempotency_key` — auto-generation means a re-invocation after timeout counts as a NEW order (no retry protection unless the agent reuses its own key)
- Gateway auth is reused: token from `XIADAN_MCP_TOKEN`, auto-read from `config/app_config.json` if unset; requests never carry an `Origin` header (compatible with the cross-site defense)
- Gateway errors surface as MCP `isError` results formatted as `[ERROR_CODE] message | suggestion | request_id`; an unreachable gateway returns actionable guidance instead of a stack trace

Setup:

```bash
uv sync --extra mcp
```

Client registration (Claude Desktop or any stdio MCP client, same shape):

```json
{"mcpServers": {"xiadan-gateway": {
    "command": "uv",
    "args": ["--directory", "C:/path/to/xiadan-gateway", "--extra", "mcp",
             "run", "python", "scripts/mcp_server.py"],
    "env": {"XIADAN_MCP_TRADING": "1"}
}}}
```

> ⚠️ Enable trading tools only in MCP clients that support per-call human approval, and keep approval on for `place_order`/`cancel_orders`. An LLM deciding trade parameters on its own is exactly the failure mode the explicit-price rule and the confirm-then-verify workflow exist to prevent.

Configuration (environment variables, all optional):

| Variable | Default | Meaning |
|----------|---------|---------|
| `XIADAN_MCP_URL` | from `config/app_config.json`, else `http://127.0.0.1:5000` | gateway base URL |
| `XIADAN_MCP_TOKEN` | `auth.token` from the config file (when auth is enabled) | auth token |
| `XIADAN_MCP_CONFIG` | `config/app_config.json` | gateway config file path |
| `XIADAN_MCP_TRADING` | `0` | `1`/`true` registers `place_order`/`cancel_orders` |
| `XIADAN_MCP_TIMEOUT_SECONDS` | `60` | HTTP timeout toward the gateway (recommended ≥40) |

## Skill Access (Any LLM Tool)

Besides MCP, the repo ships an [agent skill](.agents/skills/xiadan-gateway/SKILL.md) — no MCP protocol support required: any LLM tool that can execute shell commands (ZCode, Claude Code, Cursor, ...) can operate the gateway through it:

```
agent ──skill CLI──HTTP──→ gateway (Flask) ──→ xiadan.exe
```

The skill directory is distributed with the repo (`.agents/skills/xiadan-gateway/`):

- `SKILL.md` — trigger conditions and mandatory operating rules (login gate, verbatim confirm-then-trade workflow, idempotency-key retry semantics, error-code reactions)
- `scripts/xiadan.py` — a pure-stdlib thin CLI adapter (agents never hand-write `curl` — sidesteps the PowerShell `curl` alias trap and quoting issues)
- `references/api.md` — the full API contract loaded on demand (params, response fields, complete error-code table)

Installation (pick whichever matches your tool):

| Tool | How |
|------|-----|
| ZCode (working inside this repo) | zero-install — `.agents/skills/` is a native ZCode discovery path |
| ZCode / Claude Code (any project) | copy or link `.agents/skills/xiadan-gateway/` into `~/.zcode/skills/` / `~/.claude/skills/` |
| Other agents | inject SKILL.md as instructions, or simply allow the agent to call the CLI |

Usage (CLI subcommands map 1:1 to the MCP tools):

```bash
uv run python .agents/skills/xiadan-gateway/scripts/xiadan.py health
uv run --no-project python <skill-dir>/scripts/xiadan.py positions

# Trading commands are opt-in (same switch and env vars as the MCP adapter)
XIADAN_MCP_TRADING=1 uv run python .agents/skills/xiadan-gateway/scripts/xiadan.py \
    buy --code 601991 --amount 100 --price 10.50
```

Exposure is layered exactly like the MCP adapter: read-only commands (`health/queue/balance/positions/trades/orders/order-status`) are always available; `buy/sell/cancel` require `XIADAN_MCP_TRADING=1`; `/actions/*` and `/admin/*` are never exposed. Exit codes `0/1/2/3` = success / gateway error / usage error / gateway unreachable.

Safety design:

- The CLI is a thin adapter like the MCP server: pure stdlib, imports nothing from `src/`; queue serialization, idempotency, and alerting are all inherited via the HTTP layer. The env vars (`XIADAN_MCP_URL/TOKEN/CONFIG/TIMEOUT/TRADING`) are shared by both adapters
- The idempotency key used for an order is printed to stderr — after a timeout, a retry must reuse the same key (same key gets intercepted by the gateway; a new key = a new order)
- `tests/test_skill_cli.py` (28 hermetic cases) covers the registration surface, argument validation, semantic mapping, and HTTP contract, and guards the SKILL.md safety clauses against drift

> ⚠️ Same red line as MCP: enable trading commands only in agents that support per-command human approval, and never skip the confirm-then-verify workflow in SKILL.md.

MCP-capable clients should still prefer the [MCP server](#mcp-server-agent-access) (structured tool calls parse better than CLI text output); both adapters front the same gateway process.

## How It Works

```
Browser/script ──HTTP──→ Flask + waitress ──→ TaskQueue ──→ pywinauto ──→ xiadan.exe
                          │                    │               │
                      Auth/routes/resp   single-threaded   UIA automation
                                                     │
                                              ┌──────┴──────┐
                                          Queries (read)   Trades (write)
                                    Ctrl+C clipboard copy  F1/F2 form fill
                                    + OCR captcha solving  + popup detect/confirm
```

1. **HTTP API layer**: Flask + waitress provides a REST API with token auth and a unified JSON response format
2. **Task queue**: a single worker thread executes tasks sequentially to prevent concurrent access to `xiadan.exe` from conflicting on the UI
3. **UI automation**: pywinauto (UIA backend) manipulates controls — reading text, filling inputs, clicking buttons
4. **OCR captcha**: Ctrl+C almost always triggers a captcha popup (occasionally a copy goes through without one — clipboard fallback, see [Query Panel Standardization](#query-panel-standardization)); a lightweight template-matching engine recognizes it automatically (see [Captcha OCR](#captcha-ocr--lightweight-template-matching))
5. **Window monitoring**: a background thread periodically checks the trading window state and restores it if minimized; it stands down during client login/startup (process age / login dialog / same-process foreground checks) so restoration never races the login drawing
6. **Order latency optimization**: reusing UIA control-tree traversals, pipelined mode switching, and consecutive-clean-skip cut single-order latency dramatically (current measurements in [Performance Measurements](#performance-measurements))

## Key Design

### Task Queue and Watchdog

All operations run sequentially on a single worker thread (`TaskQueue`) to avoid concurrent conflicts on `xiadan.exe`. By default `WindowService.reset_window_state()` runs before each task, resetting the window to the F1-buy baseline (activation + ESC×5, including window position self-healing — auto-returns the window if dragged off-screen); consecutive clean exits in the same group skip the reset — but window position self-healing is never skipped (off-screen windows are moved back even on the skip path; the check costs a GetWindowRect) (see 「Consecutive clean skip」 in Core Features). On task timeout, the watchdog performs 「screenshot archive → activate window → ESC×3 reset」 recovery and **returns the error only after all recovery completes**, guaranteeing that when the caller receives `TASK_TIMEOUT`, `xiadan.exe` is already back to its initial state.

> **Watchdog concurrency boundary**: recovery (ESC reset) runs on the watchdog timer thread while the timed-out task's worker thread may still be executing. The guarantee covers the window's final state, not the side effects of the abandoned task — a zombie task can still drive the UI (e.g. click buy) *after* recovery completed and the error was returned. The single-worker design ensures the next queued task only starts after the zombie returns; treat TASK_TIMEOUT as "order state unknown, verify before retrying" (see the idempotency rule that keeps dedup records on timeout).

### Idempotency and Price Validation

- **Mandatory idempotency key**: every `POST /orders` **requires** an `Idempotency-Key` header (1–128 chars). Key lifecycle = **one key per logical order**: an HTTP-timeout retry must **reuse the same key** (a same-key request within the window is rejected with `DUPLICATE_ORDER` — that is the retry protection); a genuinely new order (including multiple identical-parameter orders) must use a **new key**, and distinct keys pass immediately even with identical parameters. The server cannot verify randomness — the contract is **uniqueness**; uuid4 is the easiest way, "strategy-id + sequence number" works too.
- **Window semantics**: `idempotency.order_dedup_window_seconds` (default 60, calibrated to the recommended 40s client timeout) is the same-key retry-block window. A failed order clears its record to allow same-key retry; a timed-out order does not (prevents duplicate submission). Missing/blank key → `VALIDATION_ERROR` with guidance.
- **Price**: the API layer rejects prices with more than 2 decimals (`VALIDATION_ERROR`); the order layer auto-formats via `sanitize_price()` to 2 decimals.

### Classified Popup Handling

Popups during order/cancel are auto-detected and handled by type: order-confirm popups (click Y/N), warning popups (click Y to continue), price-out-of-range (click N to cancel), error popups (close then report). Classification is driven by the rule table in `src/core/popup_rules.py` (how to extend it: [Adding a New Clean-Exit Scenario](#adding-a-new-clean-exit-scenario)).

**Popup-dismissal mechanism**: non-order-confirm popups are closed via `_close_non_confirm_popup()` — batch lookup of standard Windows buttons first (IDOK=1 / IDCANCEL=2, one traversal finds both), falling back to sending ESC directly via `keybd_event` (bypassing `send_key` so foreground-window verification isn't blocked by the modal popup). The order-confirm popup's N-key fallback uses the same method.

**Order-confirm safety check**: the popup-handling loop verifies the 「委托确认」 title (cid=1365 text match) before clicking Y/N — in quick-trading mode (no popup) the loop exits on the first round, so the Y key never leaks to other windows.

#### Popup Types and "Clean Exit"

Several popups may appear after placing/canceling orders; how they are handled determines whether the window state can be trusted:

| Popup type | Example title | Buttons | Handling | Window state |
|------------|--------------|:---:|----------|:---:|
| Order confirm | 「委托确认」 | Yes(Y) / No(N) | Click Y to confirm / N to cancel | Trusted |
| Price out of range | 「提示信息」 | Yes(Y) / No(N) | Click N to cancel → `PRICE_OUT_OF_RANGE` | Trusted (clean exit) |
| Single-button notice | 「提示」 | OK | Click OK (mouse only; Y key does nothing) | Depends on content |
| Insufficient balance | 「提示」 | OK | Click OK to close → `INSUFFICIENT_BALANCE` (keyword combo: 「提交失败」+ balance/funds + 「还差」) | Trusted (clean exit) |
| Short-selling restriction | 「提示」 | OK | Click OK to close → `SHORT_SELLING_FORBIDDEN` (「不允许卖空」or 「提交失败」+「无证券」+「持仓信息」) | Trusted (clean exit) |
| Fatal error | 「提示信息」 | OK | Close + classified error | **Untrusted** |

> **Note**: single-button popups titled 「提示」 have only an OK button (cid=1) and cannot be triggered with letter keys. When debugging, if the Y key seems ineffective, check whether it's a single-button popup.

#### Fine-Grained Classification of Submission-Failure Popups

If the broker returns a 「提示」 popup (OK-only) after clicking buy, `_extract_popup_error_text()` extracts clean popup text from the control tree (container first, blacklist fallback), and `match_submit_error()` returns a precise error code and targeted suggestion via the rule table (`SUBMIT_ERROR_RULES`):

| Popup keywords | error_code | Suggestion |
|----------------|-----------|------------|
| 清算 (clearing) | `SERVER_CLEARING` | Retry after clearing finishes |
| 当前时间不允许委托 (not allowed at this time) | `OUTSIDE_TRADING_HOURS` | Operate within trading hours |
| T+1 / 当日买入 / 未交收 (bought today / unsettled) | `T1_RESTRICTION` | Shares bought today can only be sold the next trading day |
| 提交失败 + 余额/资金 + 还差 (submission failed + balance + shortfall) | `INSUFFICIENT_BALANCE` | Check available funds, adjust quantity or price |
| 不允许卖空 / 提交失败 + 无证券 + 持仓信息 (no short selling / no securities) | `SHORT_SELLING_FORBIDDEN` | A-shares don't allow short selling; check sellable shares |
| 可卖数量 / 可用余额不足 (sellable quantity / insufficient balance) | `INSUFFICIENT_SHARES` | Check sellable shares and adjust |
| 事务处理机转发失败 (transaction-processor forwarding failed) | `SERVER_UNAVAILABLE` | Confirm the broker server is healthy |
| Other | `ORDER_SUBMIT_FAILED` | Generic suggestion |

`details.popup_text` returns the raw popup text for callers to parse themselves; `details.popup_title` returns the popup title. Buy and sell share the same `place_order()` flow, differing only in the F1/F2 switch; all classification logic applies equally to both.

#### Popup Text Extraction and Blacklist Self-Learning

`order_detail_text` reads from cid=1040 first, falling back to `_extract_dialog_text(title_el)` which collects text from the popup container (`title_el.parent()`); `_extract_popup_error_text` likewise prefers the **container first** (clean, no hard-coded UI-label blacklist, unaffected by broker UI upgrades), falling back to a global scan + two-layer blacklist only when container extraction is empty (last line of defense). The blacklist's first layer is a runtime snapshot of the quiet-time main-window texts taken at the start of each order (self-learning: new labels introduced by broker UI upgrades are filtered automatically, no code change needed); the second layer is the original hard-coded label list as a static floor; matching always uses the combined text (primary + fallback extraction), so error popups are still precisely classified when cid=1040 extraction is incomplete, instead of being treated as generic warnings and clicking Y.

#### Server-Error Popup Defense

When interacting with the broker server (querying price after entering code, switching price mode, clicking buy/sell), a 「提示」 popup may appear if the server is unavailable or outside trading hours. These popups only have an OK button and can't be operated with Y/N keys — they are closed via button clicks (cid=1/2) or ESC.

| Trigger stage | Example popup content | Handling |
|---------------|----------------------|----------|
| After entering stock code | 事务处理机转发数据失败 / Begin failed! | `_dismiss_server_error_popup()` closes it |
| Switching price mode | Same as above (server unresponsive) | Detect popup after timeout → `SERVER_UNAVAILABLE` |
| Clicking buy/sell | 提交失败：清算中 / 当前时间不允许委托 / … | Extract text → classified error |

Keywords are centralized in `constants.py:SERVER_ERROR_POPUP_KEYWORDS` (blocking popup keywords in `constants.py:BLOCKING_POPUP_KEYWORDS`). `WindowService.dismiss_blocking_popup()` defaults to bilingual keywords (`"失败"` / `"failed"` / `"事务处理机"`); all callers (after Trader code entry, after price-mode-switch timeout, F4 query panel, F3 cancel screen) share the same detection logic.

### Limit/Market Mode Switching

Clicking the "买入价格" (buy price) label (cid=1400) triggers a broker server request, toggling between limit/market (bidirectional). After entering the stock code, the **actual UI mode is auto-detected** — the broker may remember each stock's last mode and switch automatically after the code is entered (e.g. 000001 was last sold at market, so the UI shows 「市价卖出」 and won't accept a price).

Strategy: `sleep(0.3)` then check for a popup first (server rejection popups appear in <0.5s) → if a popup exists, classify and report immediately → otherwise cache the label element reference and `poll_until` for text change (3s timeout, max 2 retries; each poll reads text only, no UIA traversal). The two failure scenarios are handled separately:

| Scenario | Symptom | error_code |
|----------|---------|-----------|
| Server anomaly (maintenance) | Popup 「事务处理机转发数据失败」 | `SERVER_UNAVAILABLE` |
| Simulated account doesn't support market | No popup, label silently unchanged | `MODE_SWITCH_FAILED` (suggests limit mode) |

### Query Panel Standardization

All queries enter the query panel via `_prepare_query_panel()` (sends F4 to switch to the query panel), then navigate explicitly to the target page (balance / today's trades / today's orders — no reliance on the "F4 default page" assumption, since the window may be parked on another page across consecutive queries). The TaskQueue worker already calls `reset_window_state()` (ESC×5→F1) before each task, so query methods don't reset again, saving ~1.7s each. Navigation itself triggers the broker server query; no extra F5 refresh needed. Empty tables (header only, no data rows) return an empty list.

**Fake-data defense**: clipboard is cleared before Ctrl+C (so a failed copy never reads residue from the previous task); after copying, results are validated against feature columns (positions=`成本价`+`股票余额`, trades=`成交时间`+`成交编号`, orders=`委托价格`+`委托数量` — **measured headers**: the order table has no 「委托编号」 column; this version uses 「合同编号」, but the trades table also has 「合同编号」 so it lacks discrimination — the order table's unique features are 「委托价格/委托数量」). If page switching failed (window obscured/minimized, focus never entered the table, etc.) the copy comes from another query table; validation retries once and records the actual headers, and if it still fails, reports an explicit error (`INTERNAL_ERROR`) — never silently returns fake data.

**Navigation & captcha double fallback** (2026-09-05): (1) after clicking a tree node, the selection state is verified via `is_selected()` and the click is retried when it did not register — on the simulated account, `click_input` occasionally failed to register, leaving the window on 「当日委托」 while an empty table bypassed feature-column validation and positions were silently returned as an empty list; this fix closes that path. (2) when no captcha popup is detected after the copy, the clipboard is validated as a fallback — when a copy happened not to trigger a captcha (occurs occasionally, typically the first copy after a longer idle gap), or the popup detection missed, valid table data is adopted directly instead of spinning through retries and mis-reporting OCR failure.

### Event-Driven Waiting

`src/utils/poll.py` provides `poll_until(condition, timeout, interval)` instead of fixed `time.sleep()`. Scenarios like waiting for the popup after placing an order, waiting for the captcha after Ctrl+C, and waiting for the confirm popup after canceling poll the UI state every 0.1s and continue as soon as the condition holds; `PollTimeoutError` is raised on timeout. The `timed` context manager records per-step durations.

### Key-Sending Strategy

| Method | Needs foreground | Use case |
|--------|:---:|----------|
| `keybd_event` + `background=True` | ✗ | Function keys (self-verifies foreground: zero-overhead direct send when already foreground; auto-activates if the window was switched away — keys never leak into the wrong window) |
| `keybd_event` | ✓ | Function keys F1–F12, Ctrl+C combos |
| `PostMessage` | ✗ | Letter keys Y/N, ENTER (no focus stealing in background) |

Function keys go through foreground sending by default (`PostMessage` cannot trigger window shortcuts), with `click_input()` + `GetForegroundWindow()` handle verification before sending to ensure the window is in the foreground. `background=True` means "the caller already activated the window" (e.g. `place_order()` step 1) and skips the redundant activation, saving `click_input()` + `sleep(0.3s)` ×2 (~0.6s) — but it first runs a handle-level foreground self-check (microseconds): if the window was switched away, the full activation flow runs before sending, so keys never go into the wrong window (measured: an F4 sent into the void left the query page wrong and tree-node clicks unregistered, adding ~1.5s of navigation retries).

### Ctrl+C Double-Send Mechanism

```text
Ctrl Down → sleep(0.1s) → C Down → C Up → Ctrl Up    (×2, 0.15s apart)

1st send → Chinese IME intercepts (cancels combo state)
2nd send → IME exited, delivered to broker → triggers captcha popup
```

- **0.1s delay**: lets `GetAsyncKeyState` see that Ctrl is held
- **Double send**: bypasses the Chinese IME's interception of the first Ctrl+C
- **No SendInput**: the broker may filter injected input via the `LLKHF_INJECTED` flag
- **No PostMessage**: doesn't update the key-state table, which the broker's `GetAsyncKeyState` would not detect

### Captcha OCR — Lightweight Template Matching

THS Ctrl+C **almost always triggers a captcha popup** (4 digits, white background with blue digits, 92×38 px, regular font; rarely a copy goes through without one — see the clipboard fallback in [Query Panel Standardization](#query-panel-standardization)). The popup appearing is the confirmation that Ctrl+C was delivered.

**Recognition flow**: proactive periodic scan detects the popup → screenshot → screenshot sanity check (file ≤5KB + dimensions near 92×38 + white-pixel ratio >50% + dark-pixel horizontal span 15%–85%, rejecting shots of the main window/popup edges/hidden controls) → grayscale → binarize → vertical projection segmentation → template matching → fill into the broker software. Up to 2 outer attempts; up to 3 inner OCR retries.

**Solve-loop hardening** (all from live-fire incidents): each inner retry **re-captures a fresh screenshot** (segmentation outcome depends on the digit combination — retrying the same image is meaningless; three identical archived failures were observed); after submitting, the popup is scanned for the rejection text — a destroyed dialog is **not** treated as success, because the client destroys-and-recreates it right after rejecting a wrong code (this recreate gap once produced a false "verified" log); confident-but-rejected reads are archived as `wrong_<value>_<ts>.png` (the only failure mode that leaves no evidence otherwise); when retries are exhausted the dialog is closed safely (Cancel/WM_CLOSE) so it cannot deadlock the foreground. Leftover dialogs found at task start are **never solved** — leftover captchas are usually expired (a correctly-read, correctly-typed code was still rejected in practice) — they are closed-only with evidence archived first (see the Runtime popup self-healing row above). To inspect dialog control structures, use the probe tool `scripts/probe_captcha_dialog.py` (run inside a desktop session shared with xiadan.exe).

#### Recognition Principle (pure NumPy/Pillow, no deep learning)

```
Raw image (92×38 RGB)        Grayscale              Binarize (threshold 200)
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│ 2 5 8 0         │  →   │ ■ ■ ■ ■         │  →   │ █ █ █ █         │
│ white bg, blue  │      │ luminance only   │      │ strokes=black   │
└─────────────────┘      └─────────────────┘      └─────────────────┘

                              ↓ vertical projection
                         ┌─────────────────┐
                         │ ██  ██  ██  ██  │  4 dark-column groups = 4 digits
                         │ ██  ██  ██  ██  │  gap >5px = different digit
                         │ ██  ██  ██  ██  │  gap ≤5px merged (broken strokes)
                         └─────────────────┘
                              ↓ normalize to 28×38
                              ↓ template matching (NCC normalized cross-correlation)

    segmented digit ──→ cosine similarity against 1,200+ templates ──→ highest score
                        essence: vector dot product = cos(angle)
```

- **Grayscale**: `.convert("L")` removes color; blue digits become gray, keeping only luminance
- **Binarize**: threshold 200 — background/anti-aliased edges (>200) dropped, stroke cores (<200) kept
- **Segmentation**: vertical projection → dark-column grouping → merge broken strokes (e.g. the horizontal/vertical gap in '5') → trim horizontal whitespace → normalize to 28×38. The noise filter drops a group only when it is **both narrow (<4px) and short** (column height <4): a '1' is naturally ~3px wide but tall, and a pure width filter used to kill it — segmenting `0102` into 3 digits (`002`) and surfacing as "empty recognition"; with the tall-stroke exception, all historical failure archives recovered correctly (0102/7617/9315)
- **Matching**: normalized cross-correlation (NCC). Treat the 28×38 = 1064 pixels as a 1064-dim vector; after normalization each template has unit length, so NCC = the dot product of the two vectors = cos(angle). Smaller angle = more similar, independent of brightness/contrast.

  **Batch matrix multiplication**: templates are pre-normalized at load time and stacked into an (N, 1064) matrix; matching is one step:

  ```
  scores = T @ d    # (1208, 1064) × (1064,) → (1208,)   single BLAS call
  best = argmax(scores)
  ```

  No Python loops, no per-template re-normalization. Takes < 0.01s (logs show 0.00s).

  ```
  input digit '5' → T @ d →
    template0: 0.12   template1: -0.05   ...   template5₁: 0.91 ✓
                                                 ↑ argmax → recognized as 5
  ```

#### Engine Comparison

| Engine | Memory | Speed | Principle | Role |
|--------|--------|-------|-----------|------|
| Lightweight template matching | < 5MB | < 0.01s | NCC + BLAS batch matrix multiplication | The only engine in production |
| ddddocr (optional) | ~150MB | 10–50ms | ONNX deep learning | Quality checker in debug mode, not loaded in production |

1,200+ templates have been accumulated, covering all 10 digits; ddddocr is unnecessary for daily use.

#### Offline Training

Templates are no longer extracted at runtime, nor compared against ddddocr. Template training is now an offline operation:
1. Failed captchas are auto-archived to `assets/captcha_archive/failed_*.png`
2. Run `uv sync --extra ocr && uv run python scripts/train_ocr.py` to trigger real trading captchas and accumulate samples
3. Run `uv run python scripts/generate_templates.py batch` to batch-extract templates from the archive

#### Debug Mode

`ddddocr_enabled: true` + `uv sync --extra ocr`: restores dual-engine behavior — ddddocr parallel verification, auto-archiving labeled captchas, live template extraction, accuracy comparison. Memory usage ~230–300MB.

`GET /ocr/quality` returns runtime stats (recognition count, failure count, template count, covered digits, ddddocr mode status).

### Control-Tree Caching and Performance

`pywinauto`'s `descendants()` traversal of the UIA tree takes ~1s (the trading window has hundreds of controls); multiple independent calls in the original flow caused heavy cumulative latency. A three-level caching strategy eliminates redundant traversals:

**Shared across the whole flow**: `place_order()` calls `descendants()` once after getting the window and passes the list to `input_text_to_element` (code/price/quantity) and `click_element` (order button), each avoiding its own `find_element_in_window` traversal.

**Polling reuse**: the popup-handling loop performs one `descendants()` traversal that simultaneously covers detection (title image cid=1365, detail text cid=1040) + rule-table classification (`match_popup_rule`) + popup dismissal, all sharing the traversal result.

| Optimization | Before | After |
|--------------|--------|-------|
| Fill stock code | 2.09s | 1.22s |
| Fill quantity | 1.76s | 0.87s |
| Click order button | 1.05s | 0.57s |
| F1/F2 switch (skip redundant activation) | 1.10s | 0.16s |
| Wait for order popup (merged two traversals) | 2.20s | 1.10s |
| Popup-handling loop (cached reuse) | 33.60s | 0.62s |
| Market-switch failure (retries 3→2, timeout 5→3s) | ~18s | ~12s |
| Market-switch poll (cached label reference) | ~0.5s/poll | ~0ms/poll |
| Submission-failure popup detection (skipped on happy path) | 1.17s | 0s |
| **Total response (happy path)** | **~51s** | **~13.5s** |
| **Total response (error path)** | **~51s** | **~14s** |

`input_text_to_element` / `click_element` / `find_element_in_window` / `get_all_visible_texts` all accept an optional `descendants` parameter; on cache miss they degrade to a fresh scan.

### Query Flow Performance

The query flow (`_copy_table_via_clipboard` → `_solve_captcha`) implements the same class of optimizations independently:

| Optimization | Description | Saved |
|--------------|-------------|:--:|
| UIA cache reuse | one `descendants()` in `_solve_captcha` shared across the whole flow (image/input/button) | ~1.5s each |
| Deduped reset | TaskQueue worker already calls `reset_window_state()`, query methods don't repeat it | ~1.7s each |
| Proactive periodic scan | Captcha detection uses timed scanning instead of idle `poll_until` | ~0.3s each |
| Slimmed outer retries | Outer 3→2 attempts (inner OCR already retries 3×) | ~3s on failure path |
| Faster verify polling | 2.0s→1.0s, removed redundant re-query after timeout | ~1s each |
| On-demand diagnostics | `_auto_diagnostic` runs only on failure, skipped on success | ~0.5s/task |

| Query type | Before | After | Reduction |
|------------|--------|-------|:--:|
| Balance | ~8s | ~4s | -50% |
| Positions/trades/orders | ~15s | ~8–10s | -35% |

### Performance Measurements

Current single-operation latency (latest full re-measure 2026-09-05, after cross-request window-handle caching):

| Operation | Cold start | Consecutive |
|-----------|-----------|-------------|
| Buy | ~6.9s | ~6.0s (same-direction) |
| Cancel | ~4.9s | ~1.8s |
| Positions | — | ~6.4s |
| Trades | — | ~5.8s |
| Balance | — | ~2.2s |

- First query after a service restart: 17.0s → 9.0s — `WindowService` is a singleton, so the handle cache is reused across requests, eliminating the ~2s global window scan per request
- Message-based copy (`query.copy_method=message`): copy stage 5.5s→~1.2s, end-to-end trades 6.25s→3.20s, positions 8.35s→5.92s (2026-09-09 baseline; 2026-09-22 re-test positions ~3.5s, trades ~4.5s, both incl. captcha)
- A single order dropped from ~13.7s (pre-optimization baseline) to the current level — techniques and per-step breakdown in [Control-Tree Caching](#control-tree-caching-and-performance) and [Query Flow Performance](#query-flow-performance)

## Known Limitations

### Menu Bar Cannot Be Automated

THS's menu bar uses fully custom rendering: Win32 `GetMenu()` returns 0 and the UIA tree has no PopupMenu children. The 「系统设置→快速交易」 (System Settings → Quick Trading) configuration cannot be automated and must be set **manually** (see [Prerequisites](#prerequisites-broker-software-settings)).

### Closing Buy/Sell Sub-Panels

Don't use ALT+F4 (closes the whole app) or ESC (the sub-panel is not a standalone dialog and ignores it). The correct way: send F4 to switch to the query view. `/actions/close-dialog` encapsulates this logic.

### Logger Constraint

The project's custom Logger accepts only a single message argument — pass parameters via f-strings (`%s` placeholders are not supported).

## Project Structure

```
xiadan-gateway/
├── config/
│   ├── app_config.json          # runtime config (gitignored)
│   ├── app_config.example.json  # config template
│   └── key_config.py            # Windows virtual-key-code mapping
├── src/
│   ├── exceptions.py            # ErrorCode / ApiError / TaskTimeoutError
│   ├── constants.py             # control IDs / window titles / keyword constants
│   ├── api/
│   │   ├── routes.py            # Flask app factory + system routes + auth middleware
│   │   ├── query_routes.py      # query Blueprint (positions/balance/trades/orders)
│   │   ├── order_routes.py      # order/cancel Blueprint (incl. entrust_no reconciliation)
│   │   ├── action_routes.py     # manual-action/diagnostic Blueprint
│   │   ├── task_queue.py        # global task queue + watchdog recovery + runtime stats
│   │   ├── response.py          # unified response wrapper (success/error)
│   │   ├── helpers.py           # route-layer shared utilities
│   │   └── idempotency.py       # order idempotency check (mandatory Idempotency-Key)
│   ├── core/
│   │   ├── trader.py            # order orchestration
│   │   ├── popup_rules.py       # popup/submit-error classification rule table (action + error code)
│   │   ├── ocr.py               # OCR service (dual-engine scheduling + quality checks)
│   │   ├── ocr_lightweight.py   # lightweight OCR (template matching, pure NumPy/Pillow)
│   │   ├── entrust_capture.py   # order banner capture thread (bottom-right strip grab)
│   │   ├── banner_ocr.py        # banner digit recognition (MS YaHei templates + IoU)
│   │   ├── validation.py        # pure validation functions (price / trading hours + holidays)
│   │   └── trading_calendar.py  # SZSE monthly calendar fallback (per-year cache, when chinesecalendar is unavailable)
│   ├── services/
│   │   ├── window_service.py    # base window/control operations
│   │   ├── window_monitor.py    # window-minimized monitor thread
│   │   ├── position_service.py  # position/balance/trades queries
│   │   └── trading_service.py   # cancel service
│   ├── models/
│   │   └── config.py            # AppConfig (singleton + hot reload)
│   └── utils/
│       ├── singleton.py         # thread-safe singleton base class
│       ├── logger.py            # logger (file rotation + console, configurable level)
│       ├── uia.py               # safe UIA access (safe_text/safe_control_type)
│       ├── screenshot.py        # screenshot utility + auto-cleanup
│       ├── poll.py              # poll-based waiting (poll_until / timed)
│       └── diagnostic.py        # diagnostic tools (screenshot + UI text + OCR)
├── tests/
│   ├── test_core.py             # core-logic unit tests (no real broker client needed)
│   ├── test_banner_ocr.py       # banner digit OCR unit tests (real banner-strip sample fixtures)
│   ├── test_mcp_server.py       # MCP adapter unit tests (HTTP stubbed, no live server needed)
│   └── test_skill_cli.py        # skill CLI hermetic tests + SKILL.md doc guard
├── scripts/
│   ├── mcp_server.py           # MCP stdio adapter (read-only by default; trading tools behind XIADAN_MCP_TRADING=1)
│   ├── diagnose_settings.py     # broker UI structure diagnostic
│   ├── generate_templates.py    # OCR template management (view/extract/batch-annotate)
│   ├── train_ocr.py             # iterative OCR training (auto-triggers captchas, tracks accuracy)
│   ├── test_*.py / explore_*.py # exploration & experiment debug scripts (dev leftovers, run manually)
│   └── legacy/                  # one-off manual test scripts moved out of tests/ (not collected by pytest)
├── .agents/
│   └── skills/
│       └── xiadan-gateway/      # agent skill (SKILL.md + CLI + API reference, see "Skill Access")
├── assets/
│   ├── digit_templates/          # digit templates (git-tracked, produced by offline training)
│   └── captcha_archive/          # failed-captcha archive (gitignored, for offline training)
├── data/                        # SZSE calendar cache (generated at runtime, gitignored)
├── logs/                        # generated at runtime (gitignored)
├── main.py                      # entry point (waitress + single instance + graceful shutdown + UTF-8 console)
└── pyproject.toml               # dependencies and build config
```

## Tech Stack

| Component | Purpose |
|-----------|---------|
| **Python 3.11+** / **uv** | Language / package manager |
| **Flask** | HTTP routing (modular Blueprints) |
| **waitress** | Production-grade WSGI server |
| **pywinauto** (UIA) | Window/control automation |
| **pywin32** | Windows APIs (keys, windows, mutex) |
| **psutil** | Process enumeration and path matching |
| **pyautogui** | Mouse clicks, full-screen screenshots |
| **ddddocr** (ONNX Runtime) | Optional, offline OCR training scripts only (`uv sync --extra ocr`) |
| **Pillow + NumPy** | Lightweight OCR template-matching engine |
| **chinesecalendar** | Statutory-holiday awareness for trading-hours precheck (auto-degrades when data year is uncovered) |
| **pytest** | Unit tests |

> 📅 **chinesecalendar annual maintenance**: holiday data follows the State Council's release cadence; a new version covering the next year usually ships around **November** each year. When installed data doesn't cover the current year, the trading-hours precheck automatically falls back to the SZSE official monthly calendar (the current year's 12 months are fetched once and cached under `data/trading_calendar/`, then read offline; only if the API is also unavailable does it degrade to weekend/weekday-only checks) — upgrading once after each November release is still recommended to stay network-free:
>
> ```bash
> uv lock --upgrade-package chinesecalendar && uv sync
> ```
>
> Fallback source: SZSE official monthly trading calendar API `https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=YYYY-MM` (no `month` param = current month), returning per-day `jybz` (1=trading day, 0=non-trading) and `zrxh` (1=Sunday…7=Saturday); future months are covered only within the published year, and next year's calendar becomes queryable after its ~December release. Day-by-day identical to chinesecalendar across all of 2026 (242 trading days).

## Development

### Common Commands

```bash
uv run pytest                          # run all tests
uv run pytest tests/test_core.py -v    # run unit tests
uv run --extra mcp pytest tests/test_mcp_server.py -v  # MCP adapter tests (auto-skipped without the extra)
uv run python main.py --dev            # dev mode (hot reload)
uv run python scripts/diagnose_settings.py  # broker UI structure diagnostic
uv run python scripts/generate_templates.py status  # OCR template coverage status
uv run python scripts/train_ocr.py     # iterative OCR training (auto-triggers captchas)
```

### Adding a New Clean-Exit Scenario

Classification is driven by the rule table in `src/core/popup_rules.py` — no changes to `TaskQueue` or the skip logic are needed:

**1. Add an error code in `src/exceptions.py`:**
```python
NEW_ERROR = "NEW_ERROR"   # description
```

**2. Add a rule in `src/core/popup_rules.py`** (the popup action table `POPUP_RULES` or the error-code table `SUBMIT_ERROR_RULES`):
```python
PopupRule(
    _or(("keyword1", "keyword2")),      # matches when ANY AND-group is fully hit
    "raise_error",                      # action: raise_error / click_no / click_yes
    ErrorCode.NEW_ERROR,
    "Description: {text}",              # {text} placeholder, replaced with popup text
    "Suggestion",
    clean_dismiss=True,                 # popup closed normally, window trusted, next same-group task can skip
),
```

**3. Add a parameterized test case in `tests/test_core.py`.**

> The rule table is **order-sensitive**: multiple rules may share keywords (e.g. 「可卖数量」 appears in both T1 and `INSUFFICIENT_SHARES`); order decides classification — place new rules at the correct priority and add tests to prevent regressions.
