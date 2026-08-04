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
4. **OCR captcha**: Ctrl+C always triggers a captcha popup; a lightweight template-matching engine recognizes it automatically (see [Captcha OCR — lightweight template matching](#captcha-ocr--lightweight-template-matching))
5. **Window monitoring**: a background thread periodically checks the trading window state and restores it if minimized
6. **Order latency optimization**: by reusing UIA control-tree traversals, pipelined mode switching, and consecutive-clean-skip, a single order dropped from ~13.7s to: buy ~7.8s (cold) / ~5.5s (same-direction) / ~6.0s (cross-direction), cancel ~6.0s / 2.2s (consecutive), positions ~7.9s / 5.5s (consecutive), trades ~9.2s / 6.6s (consecutive)

## Core Features

| Feature | Description |
|---------|-------------|
| Single instance | Windows global mutex guarantees only one instance runs at a time |
| Sequential execution | Single-worker task queue avoids concurrent conflicts on `xiadan.exe` |
| Consecutive clean skip | Clean exit from previous task → skip `_reset_trading_window` + activation. **Fully skipped within the same group and direction; cross-direction only re-presses F1/F2.** Full preparation on popups/failures/cross-group. Groups: `trade` (buy/sell), `cancel`, `query` |
| Query traversal reuse | Position/trades/orders queries: one `descendants` traversal serves tree lookup + popup detection + fallback scan, cutting ~40% of navigation time |
| Pipelined mode switching | Click the limit/market toggle without waiting, immediately fill the quantity — the ~0.7s fill overlaps the label change; verification is naturally ready after filling |
| Classified popup handling | Order confirm → Y/N; warning → Y to continue; **price out of range → N to cancel + `PRICE_OUT_OF_RANGE`**; error → close + report |
| Watchdog recovery | On task timeout: screenshot + activate + ESC×5, reset, then return an error |
| Idempotency | Orders with identical parameters within a 60s window are rejected, preventing duplicate orders from HTTP timeout retries |
| OCR captcha | Lightweight template-matching engine; failures auto-archived; optional ddddocr offline training |
| Production server | `waitress` WSGI + graceful shutdown (SIGINT/SIGTERM) |
| Hot config reload | `POST /admin/reload-config` without restart |
| Startup config validation | Validates config types/ranges (port/timeouts/paths) at startup; aborts with fix guidance on invalid config |
| Screenshot auto-cleanup | Cleans expired screenshots at startup (keeps 200 / last 7 days) |
| Auth security | Token compared with `hmac.compare_digest` (constant-time) |
| Runtime stats | Per-error-code success rates (1-hour window, via `/health`); log alert after 3 consecutive failures |
| Window position self-healing | Before each task, checks window/workarea intersection (60% threshold); auto-moves the window back if it was dragged off-screen (`click_input`/screenshots are coordinate-based and fail off-screen) |

## Prerequisites: Broker Software Settings

Configure the following manually before starting — disabling confirmation popups speeds up trading.

**How**: In the standalone order window, top menu 「设置」(Settings) → tab 「快速交易」(Quick Trading), set all 4 options to 「否」(No):

| Setting | Required value | Reason |
|---------|:---:|--------|
| 撤单前是否需要确认 (Confirm before cancel) | **No** | Skip the cancel confirmation popup |
| 买入时是否需要确认 (Confirm on buy) | **No** | Skip the buy order confirmation popup |
| 卖出时是否需要确认 (Confirm on sell) | **No** | Skip the sell order confirmation popup |
| 委托成功后是否弹出提示对话框 (Prompt dialog after order success) | **No** | Reduce post-trade popup interference |

> Configure once. With confirmations off (quick-trading mode), orders submit directly with no popups, cutting ~1.4s per order.

## Quick Start

**Environment**: Windows / Python 3.11+ / [uv](https://github.com/astral-sh/uv) / TongHuaShun `xiadan.exe` installed

```bash
uv sync                           # install dependencies
uv run python main.py             # start the service (default http://localhost:5000)
uv run python main.py --dev       # dev mode (hot reload)
```

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
  "window_monitor": { "enabled": true, "check_interval": 2 },
  "task_queue": {
    "max_size": 50,
    "watchdog_timeout_seconds": 30,
    "query_timeout_seconds": 15,
    "confirm_timeout_seconds": 10
  },
  "idempotency": { "order_dedup_window_seconds": 60 },
  "ocr": { "warmup_on_start": true, "max_retry": 3, "ddddocr_enabled": false },
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
| `idempotency.order_dedup_window_seconds` | 60 | Order dedup window (seconds) |
| `ocr.max_retry` | 3 | Max captcha OCR retries |
| `ocr.ddddocr_enabled` | false | ddddocr debug switch (dual-engine verification + template extraction; requires `uv sync --extra ocr`) |
| `window_monitor.enabled` | true | Window-minimized monitoring switch |
| `auth.enabled` | false | Token auth switch |
| `logging.level` | INFO | Log level (set `DEBUG` for troubleshooting UIA control failures; hot-applies without restart) |

> Config reloads via `POST /admin/reload-config` (some path changes need a restart). Timeouts follow the latest performance measurements (2026-08 simulated-market: worst-case order ~8s, worst-case query ~12s incl. failure retries; 30s gives 2.5–3.5× headroom; hung tasks trigger watchdog recovery sooner and callers fail faster).

## API Response Format

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
| `DUPLICATE_ORDER` | Duplicate order within 60s |
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
| `SERVER_UNAVAILABLE` | Broker server unavailable (e.g. transaction-processor forwarding failed) |
| `OCR_FAILED` | Captcha recognition failed |
| `INTERNAL_ERROR` | Unknown exception |
| `QUEUE_TIMEOUT` | Task queuing timeout |
| `QUEUE_FULL` | Queue is full |
| `TASK_TIMEOUT` | Task timeout, recovery succeeded |
| `TASK_TIMEOUT_RECOVERY_FAILED` | Task timeout, recovery also failed |

### Popup Handling and "Clean Exit"

Several popups may appear after placing/canceling orders; how they are handled determines whether the window state can be trusted:

| Popup type | Example title | Buttons | Handling | Window state |
|------------|--------------|:---:|----------|:---:|
| Order confirm | 「委托确认」 | Yes(Y) / No(N) | Click Y to confirm / N to cancel | Trusted |
| Price out of range | 「提示信息」 | Yes(Y) / No(N) | Click N to cancel → `PRICE_OUT_OF_RANGE` | Trusted (clean exit) |
| Single-button notice | 「提示」 | OK | Click OK (mouse only; Y key does nothing) | Depends on content |
| Insufficient balance | 「提示」 | OK | Click OK to close → `INSUFFICIENT_BALANCE` (keyword combo: 「提交失败」+ balance/funds + 「还差」) | Trusted (clean exit) |
| Short-selling restriction | 「提示」 | OK | Click OK to close → `SHORT_SELLING_FORBIDDEN` (「不允许卖空」or 「提交失败」+「无证券」+「持仓信息」) | Trusted (clean exit) |
| Fatal error | 「提示信息」 | OK | Close + classified error | **Untrusted** |

> **Note**: single-button popups titled 「提示」 have only an OK button (cid=1) and cannot be triggered with letter keys. When debugging, if the Y key appears ineffective, check whether it's a single-button popup.

#### Adding a New Clean-Exit Scenario

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

> **Popup text extraction**: `order_detail_text` reads from cid=1040 first, falling back to `_extract_dialog_text(title_el)` which collects text from the popup container (`title_el.parent()`); `_extract_popup_error_text` likewise prefers the **container first** (clean, no hard-coded UI-label blacklist, unaffected by broker UI upgrades), falling back to a global scan + blacklist filter only when container extraction is empty (last line of defense); matching always uses the combined text (primary + fallback extraction), so error popups are still precisely classified when cid=1040 extraction is incomplete, instead of being treated as generic warnings and clicking Y.

## API Endpoints

| Method | Path | Description | Queued | timeout |
|--------|------|-------------|:---:|--------|
| GET | `/health` | Health check + recommended client timeout + runtime stats (success rates / error-code aggregates / consecutive failures) | | 5s |
| GET | `/queue/status` | Task queue status | | 5s |
| POST | `/admin/reload-config` | Hot reload config | | 5s |
| GET | `/account/balance` | Account balance | ✓ | 40s |
| GET | `/positions` | Position query | ✓ | 40s |
| GET | `/trades/today` | Today's trades | ✓ | 40s |
| GET | `/orders/pending` | Today's orders | ✓ | 40s |
| POST | `/orders` | Place order (limit/market) | ✓ | 40s |
| POST | `/orders/cancel-all` | Cancel orders (all / buys / sells) | ✓ | 40s |
| POST | `/actions/send-key` | Send a key manually | ✓ | 30s |
| POST | `/actions/click` | Mouse click at coordinates | ✓ | 30s |
| POST | `/actions/close-dialog` | Close the buy/sell sub-panel | ✓ | 30s |
| GET | `/ocr/quality` | OCR quality report (accuracy/templates/coverage) | | 5s |
| GET | `/diagnostic/snapshot` | Screenshot + UI text + OCR | | 10s |
| GET | `/diagnostic/history` | Diagnostic history of the last N tasks | | 5s |

> POST endpoints accept both JSON body and query-string parameters.

### POST /orders — Place Order

| Parameter | Required | Description |
|-----------|:---:|-------------|
| `code` | ✓ | Stock code |
| `status` | ✓ | `1`=buy, `2`=sell |
| `amount` | | Order quantity |
| `price` | | Order price (limit mode, max 2 decimals) |
| `price_type` | | `limit`=limit (default), `market`=market |
| `confirm` | | `true`=auto-confirm (default), `false`=preview mode (clicks N to cancel) |

```bash
# Market buy
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -d '{"code":"601991","status":"1","amount":"100","price_type":"market"}'

# Limit buy
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -d '{"code":"600000","status":"1","amount":"100","price":"10.50","price_type":"limit"}'

# Market sell
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -d '{"code":"601991","status":"2","amount":"100","price_type":"market"}'
```

### POST /orders/cancel-all — Cancel Orders

| Parameter | Required | Description |
|-----------|:---:|-------------|
| `type` | | `A`=all (default), `X`=cancel buys, `C`=cancel sells |

```bash
curl -X POST http://localhost:5000/orders/cancel-all
curl -X POST http://localhost:5000/orders/cancel-all -d '{"type":"X"}'
```

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

## Client Timeout Configuration

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
│   │   ├── order_routes.py      # order/cancel Blueprint
│   │   ├── action_routes.py     # manual-action/diagnostic Blueprint
│   │   ├── task_queue.py        # global task queue + watchdog recovery
│   │   ├── response.py          # unified response wrapper (success/error)
│   │   ├── helpers.py           # route-layer shared utilities
│   │   └── idempotency.py       # order idempotency check
│   ├── core/
│   │   ├── trader.py            # order orchestration
│   │   ├── popup_rules.py       # popup/submit-error classification rule table (action + error code)
│   │   ├── ocr.py               # OCR service (dual-engine scheduling + quality checks)
│   │   ├── ocr_lightweight.py   # lightweight OCR (template matching, pure NumPy/Pillow)
│   │   └── validation.py        # pure data-validation functions
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
│   └── test_core.py             # core-logic unit tests
├── scripts/
│   ├── diagnose_settings.py     # broker UI structure diagnostic
│   ├── generate_templates.py    # OCR template management (view/extract/batch-annotate)
│   ├── train_ocr.py             # iterative OCR training (auto-triggers captchas, tracks accuracy)
│   └── test_*_menu.py           # menu-structure exploration scripts (dev leftovers)
├── assets/
│   ├── digit_templates/          # digit templates (git-tracked, produced by offline training)
│   └── captcha_archive/          # failed-captcha archive (gitignored, for offline training)
├── logs/                        # generated at runtime (gitignored)
├── main.py                      # entry point (waitress + single instance + graceful shutdown + UTF-8 console)
└── pyproject.toml               # dependencies and build config
```

## Tech Stack

| Component | Purpose |
|-----------|---------|
| **Python 3.11+** / **uv** | Language / package manager |
| **Flask** + **flask-cors** | HTTP routing (modular Blueprints) |
| **waitress** | Production-grade WSGI server |
| **pywinauto** (UIA) | Window/control automation |
| **pywin32** | Windows APIs (keys, windows, mutex) |
| **psutil** | Process enumeration and path matching |
| **pyautogui** | Mouse clicks, full-screen screenshots |
| **ddddocr** (ONNX Runtime) | Optional, offline OCR training scripts only (`uv sync --extra ocr`) |
| **Pillow + NumPy** | Lightweight OCR template-matching engine |
| **pytest** | Unit tests |

## Key Design

### Task Queue and Watchdog

All operations run sequentially on a single worker thread (`TaskQueue`) to avoid concurrent conflicts on `xiadan.exe`. By default `WindowService.reset_window_state()` runs before each task, resetting the window to the F1-buy baseline (including window position self-healing — auto-returns the window if dragged off-screen); consecutive clean exits in the same group skip the reset (see 「Consecutive clean skip」 in Core Features). On task timeout, the watchdog performs 「screenshot archive → activate window → ESC×5 reset」 recovery and **returns the error only after all recovery completes**, guaranteeing that when the caller receives `TASK_TIMEOUT`, `xiadan.exe` is already back to its initial state.

### Event-Driven Waiting

`src/utils/poll.py` provides `poll_until(condition, timeout, interval)` instead of fixed `time.sleep()`. Scenarios like waiting for the popup after placing an order, waiting for the captcha after Ctrl+C, and waiting for the confirm popup after canceling poll the UI state every 0.1s and continue as soon as the condition holds; `PollTimeoutError` is raised on timeout. The `timed` context manager records per-step durations.

### Key-Sending Strategy

| Method | Needs foreground | Use case |
|--------|:---:|----------|
| `keybd_event` + `background=True` | ✗ | Function keys when the window is already confirmed foreground (skips redundant activation) |
| `keybd_event` | ✓ | Function keys F1–F12, Ctrl+C combos |
| `PostMessage` | ✗ | Letter keys Y/N, ENTER (no focus stealing in background) |

Function keys go through foreground sending by default (`PostMessage` cannot trigger window shortcuts), with `click_input()` + `GetForegroundWindow()` handle verification before sending to ensure the window is in the foreground. If the caller already activated the window (e.g. `place_order()` step 1), pass `background=True` to skip the redundant activation, saving `click_input()` + `sleep(0.3s)` ×2 (~0.6s).

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

THS Ctrl+C **always triggers a captcha popup** (4 digits, white background with blue digits, 92×38 px, regular font). The popup appearing is the confirmation that Ctrl+C was delivered.

**Recognition flow**: proactive periodic scan detects the popup → screenshot → screenshot sanity check (file ≤5KB + dimensions near 92×38 + white-pixel ratio >50% + dark-pixel horizontal span 15%–85%, rejecting shots of the main window/popup edges/hidden controls) → grayscale → binarize → vertical projection segmentation → template matching → fill into the broker software. Up to 2 outer attempts; up to 3 inner OCR retries.

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
- **Segmentation**: vertical projection → dark-column grouping → merge broken strokes (e.g. the horizontal/vertical gap in '5') → trim horizontal whitespace → normalize to 28×38
- **Matching**: normalized cross-correlation (NCC). Treat the 28×38 = 1064 pixels as a 1064-dim vector; after normalization each template has unit length, so NCC = the dot product of the two vectors = cos(angle). Smaller angle = more similar, independent of brightness/contrast.

  **Batch matrix multiplication**: templates are pre-normalized at load time and stacked into an (N, 1064) matrix; matching is one step:

  ```
  scores = T @ d    # (1208, 1064) × (1064,) → (1208,)   single BLAS call
  best = argmax(scores)
  ```

  No Python loops, no per-template re-normalization. Takes < 0.01s (logs show 0.00s).

  ```
  input digit '5' → T @ d →
    template0: 0.12   template1: -0.05   ...   template5: 0.91 ✓
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

### Classified Popup Handling

Popups during order/cancel are auto-detected and handled by type: order-confirm popups (click Y/N), warning popups (click Y to continue), price-out-of-range (click N to cancel), error popups (close then report). Classification is driven by the rule table in `src/core/popup_rules.py` (see [Adding a New Clean-Exit Scenario](#adding-a-new-clean-exit-scenario)); popups are closed via `_close_non_confirm_popup()` (batch lookup of standard buttons IDOK=1/IDCANCEL=2, ESC fallback), and server-error popups via `WindowService.dismiss_blocking_popup()` (keywords cover Chinese and English: `"失败"` / `"failed"` / `"事务处理机"`).

**Fine-grained classification of submission-failure popups**: if the broker returns a 「提示」 popup (OK-only) after clicking buy, `_extract_popup_error_text()` extracts clean popup text from the control tree (container first, blacklist fallback), and `match_submit_error()` returns a precise error code and targeted suggestion via the rule table (`SUBMIT_ERROR_RULES`):

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

### Idempotency and Price Validation

- **Idempotency**: orders with the same `code+status+amount+price+price_type` within 60s are rejected (`DUPLICATE_ORDER`). A failed order clears its record to allow retry; a timed-out order does not (prevents duplicate submission).
- **Price**: the API layer rejects prices with more than 2 decimals (`VALIDATION_ERROR`); the order layer auto-formats via `sanitize_price()` to 2 decimals.

### Query Panel Standardization

All queries enter the query panel via `_prepare_query_panel()` (sends F4 to switch to the query panel), then navigate explicitly to the target page (balance / today's trades / today's orders — no reliance on the "F4 default page" assumption, since the window may be parked on another page across consecutive queries). The TaskQueue worker already calls `reset_window_state()` (ESC×5→F1) before each task, so query methods don't reset again, saving ~1.7s each. Navigation itself triggers the broker server query; no extra F5 refresh needed. Empty tables (header only, no data rows) return an empty list.

**Fake-data defense**: clipboard is cleared before Ctrl+C (so a failed copy never reads residue from the previous task); after copying, results are validated against feature columns (positions=`成本价`+`股票余额`, trades=`成交时间`+`成交编号`, orders=`委托价格`+`委托数量` — **measured headers**: the order table has no 「委托编号」 column; this version uses 「合同编号」, but the trades table also has 「合同编号」 so it lacks discrimination — the order table's unique features are 「委托价格/委托数量」). If page switching failed (window obscured/minimized, focus never entered the table, etc.) the copy comes from another query table; validation retries once and records the actual headers, and if it still fails, reports an explicit error (`INTERNAL_ERROR`) — never silently returns fake data.

## Known Limitations and Defenses

### Menu Bar Cannot Be Automated

THS's menu bar uses fully custom rendering: Win32 `GetMenu()` returns 0 and the UIA tree has no PopupMenu children. The 「系统设置→快速交易」 (System Settings → Quick Trading) configuration cannot be automated and must be set **manually** (see [Prerequisites](#prerequisites-broker-software-settings)).

### Closing Buy/Sell Sub-Panels

Don't use ALT+F4 (closes the whole app) or ESC (the sub-panel is not a standalone dialog and ignores it). The correct way: send F4 to switch to the query view. `/actions/close-dialog` encapsulates this logic.

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

**Popup-dismissal mechanism**: non-order-confirm popups are closed via `_close_non_confirm_popup()` — batch lookup of standard Windows buttons first (IDOK=1 / IDCANCEL=2, one traversal finds both), falling back to sending ESC directly via `keybd_event` (bypassing `send_key` so foreground-window verification isn't blocked by the modal popup). The order-confirm popup's N-key fallback uses the same method.

**Order-confirm safety check**: the popup-handling loop verifies the 「委托确认」 title (cid=1365 text match) before clicking Y/N — in quick-trading mode (no popup) the loop exits on the first round, so the Y key never leaks to other windows.

### Limit/Market Mode Switching

Clicking the "买入价格" (buy price) label (cid=1400) triggers a broker server request, toggling between limit/market (bidirectional). After entering the stock code, the **actual UI mode is auto-detected** — the broker may remember each stock's last mode and switch automatically after the code is entered (e.g. 000001 was last sold at market, so the UI shows 「市价卖出」 and won't accept a price).

Strategy: `sleep(0.3)` then check for a popup first (server rejection popups appear in <0.5s) → if a popup exists, classify and report immediately → otherwise cache the label element reference and `poll_until` for text change (3s timeout, max 2 retries; each poll reads text only, no UIA traversal). The two failure scenarios are handled separately:

| Scenario | Symptom | error_code |
|----------|---------|-----------|
| Server anomaly (maintenance) | Popup 「事务处理机转发数据失败」 | `SERVER_UNAVAILABLE` |
| Simulated account doesn't support market | No popup, label silently unchanged | `MODE_SWITCH_FAILED` (suggests limit mode) |

### Server-Error Popup Defense

When interacting with the broker server (querying price after entering code, switching price mode, clicking buy/sell), a 「提示」 popup may appear if the server is unavailable or outside trading hours. These popups only have an OK button and can't be operated with Y/N keys — they are closed via button clicks (cid=1/2) or ESC.

**Defense coverage points**:

| Trigger stage | Example popup content | Handling |
|---------------|----------------------|----------|
| After entering stock code | 事务处理机转发数据失败 / Begin failed! | `_dismiss_server_error_popup()` closes it |
| Switching price mode | Same as above (server unresponsive) | Detect popup after timeout → `SERVER_UNAVAILABLE` |
| Clicking buy/sell | 提交失败：清算中 / 当前时间不允许委托 / … | Extract text → classified error |

Keywords are centralized in `constants.py:SERVER_ERROR_POPUP_KEYWORDS`. `WindowService.dismiss_blocking_popup()` defaults to bilingual keywords (`"失败"` / `"failed"` / `"事务处理机"`); all callers (after Trader code entry, after price-mode-switch timeout, F4 query panel, F3 cancel screen) share the same detection logic.

### Logger Constraint

The project's custom Logger accepts only a single message argument — pass parameters via f-strings (`%s` placeholders are not supported).

## Development

```bash
uv run pytest                          # run all tests
uv run pytest tests/test_core.py -v    # run unit tests
uv run python main.py --dev            # dev mode (hot reload)
uv run python scripts/diagnose_settings.py  # broker UI structure diagnostic
uv run python scripts/generate_templates.py status  # OCR template coverage status
uv run python scripts/train_ocr.py     # iterative OCR training (auto-triggers captchas)
```
