# xiadan-gateway

同花顺 `xiadan.exe` 交易网关 - 通过 HTTP API 控制同花顺下单程序进行股票交易。

## 核心特性

| 特性 | 说明 |
|------|------|
| 单实例运行 | 通过 Windows 全局互斥锁保证同一时刻只有一个实例运行 |
| 顺序执行 | 单 worker 线程的任务队列，避免 `xiadan.exe` 并发冲突 |
| 看门狗机制 | 任务超时自动触发截图 + ESC + 激活 + F4 恢复 |
| 幂等检查 | 60 秒窗口内相同参数的下单请求会被拒绝，防止 HTTP 超时重试导致重复下单 |
| OCR 验证码 | 基于 `ddddocr` 自动识别同花顺查询时的验证码 |
| 一键清仓 | `/orders/sell-all` 自动查询持仓并市价卖出指定股票全部可用数量 |
| 价格自动校验 | A 股价格限制 2 位小数，API 层拦截 + 输入前自动修正 |
| 生产级服务器 | 基于 `waitress` WSGI 服务器，支持优雅关闭（SIGINT/SIGTERM） |
| 配置热更新 | `POST /admin/reload-config` 无需重启即可重载配置 |
| 截图自动清理 | 启动时自动清理过期截图（保留 200 张 / 7 天内） |
| 安全认证 | Token 使用 `hmac.compare_digest` 常量时间比较，防时序攻击 |
| 事件驱动等待 | 用 `poll_until` 轮询检测弹窗/控件状态替代固定 `time.sleep()`，减少不必要延迟 |
| 详细计时 | 用 `timed` 上下文管理器记录每个操作步骤的精确耗时，方便定位性能瓶颈 |

## 前置准备：券商软件设置

启动网关前，建议在券商软件中手动完成以下设置，使下单和撤单流程跳过确认弹窗，大幅提升执行速度。

**操作路径**：右上角「系统」→「系统设置」→「快速交易」标签页

| 设置项 | 推荐值 | 原因 |
|--------|--------|------|
| 买入时是否需要确认 | **否** | 跳过委托确认弹窗，单次下单快 1-2 秒 |
| 卖出时是否需要确认 | **否** | 同上 |
| 撤单时是否需要确认（如有） | **否** | 跳过撤单确认弹窗，撤单更快 |
| 价格超限提醒（如有） | 关闭 | 减少警告弹窗干扰下单流程 |

> 这些设置只需配置一次，永久生效。关闭确认弹窗后，委托将直接提交不再弹窗确认——`confirm=false` 参数也**不再生效**（因为没有弹窗可供点击「否(N)」）。如需预览委托详情后再确认，需在券商软件中重新开启确认弹窗。

## 快速开始

**环境要求**：Windows 11 / Python 3.11+ / [uv](https://github.com/astral-sh/uv) / 已安装同花顺 `xiadan.exe`

```bash
# 安装依赖（uv sync 会自动创建虚拟环境并安装所有依赖）
uv sync

# 启动服务
uv run python main.py

# 开发模式（热加载，文件改动自动重启）
uv run python main.py --dev
```

默认监听 `http://localhost:5000`。

## 配置

编辑 `config/app_config.json`（不存在则复制 `config/app_config.example.json` 创建），**必须配置 `trading_app_paths`** 指向你的 `xiadan.exe` 路径。

```json
{
  "trading_app_paths": [
    "C:\\Users\\xxx\\同花顺远航版\\transaction\\xiadan.exe",
    "C:\\同花顺软件\\同花顺\\xiadan.exe"
  ],
  "window_monitor": { "enabled": true, "check_interval": 2 },
  "task_queue": {
    "max_size": 50,
    "watchdog_timeout_seconds": 30,
    "query_timeout_seconds": 35,
    "confirm_timeout_seconds": 10
  },
  "idempotency": { "order_dedup_window_seconds": 60 },
  "ocr": { "warmup_on_start": true, "max_retry": 3 },
  "logging": { "level": "INFO", "file": "logs/app.log", "screenshot_dir": "logs/screenshots" }
}
```

**关键配置项：**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `trading_app_paths` | [] | `xiadan.exe` 完整路径列表，支持多个版本（远航版/免费版），**至少配置一个**。列表顺序决定优先级：同时装有多个版本时，排前面的路径优先被使用 |
| `task_queue.watchdog_timeout_seconds` | 45 | 下单看门狗超时（秒） |
| `task_queue.query_timeout_seconds` | 60 | 查询类操作超时（秒，含验证码/弹窗处理） |
| `task_queue.confirm_timeout_seconds` | 10 | 确认/按键类操作超时（秒） |
| `task_queue.max_size` | 50 | 任务队列最大长度 |
| `idempotency.order_dedup_window_seconds` | 60 | 下单去重窗口（秒） |
| `ocr.max_retry` | 3 | 验证码识别最大重试次数 |
| `window_monitor.enabled` | true | 是否启用窗口最小化监控 |

> **热更新**：修改配置后可调用 `POST /admin/reload-config` 热重载，无需重启服务。部分配置（如 `trading_app_paths`）仍需重启才能完全生效。通过 `GET /health` 确认服务状态。

## 统一响应格式

**成功：**

```json
{
  "status": "success",
  "request_id": "req_20260721_120000_a1b2c3d4",
  "timestamp": "2026-07-21 12:00:00",
  "data": { ... }
}
```

**失败：**

```json
{
  "status": "error",
  "request_id": "req_20260721_120000_a1b2c3d4",
  "timestamp": "2026-07-21 12:00:00",
  "error_code": "VALIDATION_ERROR",
  "message": "code 参数不能为空",
  "suggestion": "请提供股票代码，如: /xiadan?code=601991"
}
```

### 错误码

所有响应（成功和失败）统一返回 **HTTP 200**，通过 JSON 中的 `status` 字段（`"success"` / `"error"`）和 `error_code` 区分。这样做是为了避免 PowerShell 的 `Invoke-RestMethod` 等客户端因 HTTP 4xx/5xx 状态码直接抛异常，导致调用方无法获取错误详情。

| 错误码 | 说明 |
|--------|------|
| `VALIDATION_ERROR` | 参数校验失败（含价格格式、缺少必填参数等） |
| `DUPLICATE_ORDER` | 60 秒内重复下单 |
| `AUTH_REQUIRED` | 认证未开启时请求需认证的接口 |
| `AUTH_FAILED` | 认证 token 无效 |
| `WINDOW_NOT_FOUND` | 交易窗口未找到 |
| `CONTROL_NOT_FOUND` | 控件未找到 |
| `MODE_SWITCH_FAILED` | 限价/市价切换失败 |
| `ORDER_SUBMIT_FAILED` | 订单提交失败（券商返回错误，含弹窗原文） |
| `OCR_FAILED` | 验证码识别失败 |
| `INTERNAL_ERROR` | 未知异常 |
| `QUEUE_TIMEOUT` | 任务排队超时 |
| `QUEUE_FULL` | 队列已满 |
| `TASK_TIMEOUT` | 任务超时，恢复成功 |
| `TASK_TIMEOUT_RECOVERY_FAILED` | 任务超时，恢复也失败 |

## API 接口

| 方法 | 路径 | 说明 | 入队 | 推荐 timeout |
|------|------|------|------|-------------|
| GET | `/health` | 健康检查 | 否 | 5s |
| GET | `/queue/status` | 任务队列状态 | 否 | 5s |
| POST | `/admin/reload-config` | 热重载配置文件 | 否 | 5s |
| GET | `/account/balance` | 资金余额 | 是 | 40s |
| GET | `/positions` | 持仓查询 | 是 | 40s |
| GET | `/trades/today` | 今日成交 | 是 | 40s |
| GET | `/orders/pending` | 当日委托查询 | 是 | 40s |
| POST | `/orders` | 下单（限价/市价） | 是 | 40s |
| POST | `/orders/sell-all` | 一键清仓（市价卖出全部可用持仓） | 是 | 40s |
| POST | `/orders/cancel-all` | 撤单（全部/撤买/撤卖） | 是 | 40s |
| POST | `/orders/confirm` | Y 键确认委托 | 是 | 30s |
| POST | `/actions/send-key` | 手动发送按键 | 是 | 30s |
| POST | `/actions/click` | 鼠标点击坐标 | 是 | 30s |
| POST | `/actions/close-dialog` | 安全关闭子面板（买入/卖出） | 是 | 30s |
| GET | `/diagnostic/snapshot` | 当前窗口截图+UI文本+OCR（调试用） | **否** | 10s |
| GET | `/diagnostic/history` | 最近N步操作后的界面状态历史（调试用） | **否** | 5s |

> **命名规范**：采用 RESTful 资源风格 — 资源用名词复数（`/orders`、`/positions`），子操作用连字符路径（`/orders/cancel-all`、`/orders/sell-all`），辅助操作归入 `/actions/` 命名空间。
> 查询类接口（无副作用）使用 GET，操作类接口（有副作用）使用 POST。
> POST 接口同时支持 `application/json` body 和 query string 传参，便于 curl 调试。

### POST /orders 下单

| 参数 | 必填 | 说明 |
|------|------|------|
| `code` | 是 | 股票代码，如 `601991` |
| `status` | 是 | `1`=买入, `2`=卖出 |
| `amount` | 否 | 委托数量 |
| `price` | 否 | 委托价格（仅限价模式，最多 2 位小数） |
| `price_type` | 否 | `limit`=限价(默认), `market`=市价 |
| `confirm` | 否 | `true`=自动确认(默认), `false`=不确认 |

```bash
# 市价买入 100 股 601991
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -d '{"code": "601991", "status": "1", "amount": "100", "price_type": "market", "confirm": "true"}'

# 限价买入 100 股 600000，价格 10.5
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -d '{"code": "600000", "status": "1", "amount": "100", "price": "10.5", "price_type": "limit"}'

# 市价卖出 100 股 601991
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -d '{"code": "601991", "status": "2", "amount": "100", "price_type": "market"}'
```

**说明：**
- 价格 `price` 传入时自动修正为 2 位小数（如 `1.20100` → `1.20`），并记录警告日志
- API 层也会校验，超 2 位小数的价格直接返回 400 错误，不进任务队列
- `confirm=false` 时：弹委托确认窗后点击「否(N)」取消，可用于预览委托详情

### POST /orders/sell-all 一键清仓

根据股票代码自动查询持仓，提取可用余额，以市价卖出全部数量。

| 参数 | 必填 | 说明 |
|------|------|------|
| `code` | 是 | 股票代码，如 `002366` |
| `confirm` | 否 | `true`=自动确认(默认), `false`=不确认 |

```bash
# 一键清仓 002366（市价卖出全部可用持仓）
curl -X POST http://localhost:5000/orders/sell-all \
  -H "Content-Type: application/json" \
  -d '{"code": "002366"}'
```

**流程：** 查询持仓 → 按 `code` 匹配 → 提取可用余额（兼容多种字段名）→ 市价卖出。股票不在持仓或可用余额为 0 时返回 `VALIDATION_ERROR`。

### GET /orders/pending 当日委托

```bash
curl http://localhost:5000/orders/pending
```

返回字段：时间、委托号、证券代码、证券名称、操作、委托价格、委托数量、成交数量、撤单数量、状态、交易市场。

### POST /orders/cancel-all 撤单

| 参数 | 必填 | 说明 |
|------|------|------|
| `type` | 否 | `A`=全部撤单(默认), `X`=撤买, `C`=撤卖 |

```bash
curl -X POST http://localhost:5000/orders/cancel-all
curl -X POST http://localhost:5000/orders/cancel-all -H "Content-Type: application/json" -d '{"type": "X"}'
```

**说明：** 进入撤单界面后自动检测并关闭阻塞型提示弹窗（如 "Begin failed!"），点击撤单按钮后自动检测确认弹窗并点击「是(Y)」确认。

### POST /orders/confirm

用于 `POST /orders {"confirm": "false"}` 后的单独确认。

```bash
curl -X POST http://localhost:5000/orders/confirm
```

### POST /actions/send-key 手动发送按键

| 参数 | 必填 | 说明 |
|------|------|------|
| `key` | 是 | 按键，如 `F1`、`Y`、`{CTRL+C}` |

```bash
curl -X POST http://localhost:5000/actions/send-key -H "Content-Type: application/json" -d '{"key": "F1"}'
curl -X POST http://localhost:5000/actions/send-key -H "Content-Type: application/json" -d '{"key": "{CTRL+C}"}'
```

### POST /actions/click 鼠标点击坐标

| 参数 | 必填 | 说明 |
|------|------|------|
| `x` | 是 | 横坐标（整数） |
| `y` | 是 | 纵坐标（整数） |

```bash
curl -X POST http://localhost:5000/actions/click -H "Content-Type: application/json" -d '{"x": 100, "y": 200}'
```

### POST /actions/close-dialog 安全关闭子面板

关闭买入/卖出等嵌入子面板（通过 F4 切换视图实现，绝不关闭整个程序）。同花顺的买入/卖出窗口是嵌入主窗口的子视图（不是独立对话框），ESC/WM_CLOSE 均无效，本接口发送 F4 切换到查询视图来实现关闭。

| 参数 | 必填 | 说明 |
|------|------|------|
| `title` | 否 | 面板名称（如 `"买入"`），仅用于日志 |

```bash
curl -X POST http://localhost:5000/actions/close-dialog -H "Content-Type: application/json" -d '{"title": "买入"}'
```

### GET /diagnostic/snapshot 诊断快照

当前窗口的截图路径 + UI 控件文本 + OCR 全文识别。不入队，立即返回。

```bash
curl http://localhost:5000/diagnostic/snapshot
```

### GET /diagnostic/history 诊断历史

返回最近 N 个任务执行后的界面状态快照（UI 文本 + 截图路径）。每个任务执行后自动记录，无需手动调用。

| 参数 | 必填 | 说明 |
|------|------|------|
| `n` | 否 | 返回条数（默认 5，最大 20） |

```bash
curl "http://localhost:5000/diagnostic/history?n=3"
```

## 测试命令集

以下 curl 命令覆盖所有接口，可直接复制到 PowerShell 执行：

```powershell
# 基础查询
curl.exe http://127.0.0.1:5000/health
curl.exe http://127.0.0.1:5000/queue/status
curl.exe http://127.0.0.1:5000/account/balance -H "Authorization: Bearer test-token"
curl.exe http://127.0.0.1:5000/positions -H "Authorization: Bearer test-token"
curl.exe http://127.0.0.1:5000/orders/pending -H "Authorization: Bearer test-token"
curl.exe http://127.0.0.1:5000/trades/today -H "Authorization: Bearer test-token"

# 市价下单
curl.exe -X POST http://127.0.0.1:5000/orders -H "Content-Type: application/json" -H "Authorization: Bearer test-token" -d '{"code":"601991","status":"1","amount":"100","price_type":"market"}'

# 限价下单
curl.exe -X POST http://127.0.0.1:5000/orders -H "Content-Type: application/json" -H "Authorization: Bearer test-token" -d '{"code":"601991","status":"1","amount":"100","price":"10.50","price_type":"limit"}'

# 一键清仓
curl.exe -X POST http://127.0.0.1:5000/orders/sell-all -H "Content-Type: application/json" -H "Authorization: Bearer test-token" -d '{"code":"601991"}'

# 撤单
curl.exe -X POST http://127.0.0.1:5000/orders/cancel-all -H "Authorization: Bearer test-token"
curl.exe -X POST http://127.0.0.1:5000/orders/cancel-all -H "Content-Type: application/json" -H "Authorization: Bearer test-token" -d '{"type":"X"}'

# 确认委托
curl.exe -X POST http://127.0.0.1:5000/orders/confirm -H "Authorization: Bearer test-token"

# 诊断
curl.exe http://127.0.0.1:5000/diagnostic/snapshot
curl.exe "http://127.0.0.1:5000/diagnostic/history?n=3"
```

## 调用方 timeout 配置

**关键约束**：调用方 HTTP timeout 必须 > 服务端看门狗 timeout + 恢复耗时（约 5 秒）。否则会出现 HTTP 超时但服务端仍在恢复的中间状态。

推荐通过 `GET /health` 动态获取推荐 timeout:

```python
import requests

base_url = "http://localhost:5000"

# 动态获取推荐 timeout
health = requests.get(f"{base_url}/health", timeout=5).json()
client_timeout = health["data"]["config"]["recommended_client_timeout_seconds"]

# 认证 header（auth.enabled=true 时必填）
headers = {"X-API-Key": "your-token-here"}

# 下单：POST + JSON body
resp = requests.post(
    f"{base_url}/orders",
    json={
        "code": "601991",
        "status": "1",
        "amount": "100",
        "price_type": "market",
    },
    headers=headers,
    timeout=client_timeout,
)
print(resp.json())
```

## 项目结构

```
xiadan-gateway/
├── config/
│   ├── app_config.json       # 运行时配置文件
│   └── key_config.py         # Windows 虚拟键码映射表
├── src/
│   ├── constants.py          # 集中管理控件ID/窗口标题/关键词常量
│   ├── api/                  # API 层
│   │   ├── routes.py         # Flask 应用工厂 + 系统路由
│   │   ├── query_routes.py   # 查询类 Blueprint（持仓/资金/成交/委托）
│   │   ├── order_routes.py   # 下单/撤单/清仓 Blueprint
│   │   ├── action_routes.py  # 手动操作/诊断 Blueprint
│   │   ├── task_queue.py     # 全局任务队列 + 看门狗
│   │   ├── response.py       # 统一响应封装 + 错误码
│   │   └── idempotency.py    # 下单幂等检查
│   ├── core/                 # 核心业务编排
│   │   ├── trader.py         # 下单编排器
│   │   └── ocr.py            # OCR 服务（ddddocr 单例）
│   ├── services/             # 业务服务层
│   │   ├── window_service.py # 窗口/控件操作基础服务
│   │   ├── window_monitor.py # 窗口最小化监控
│   │   ├── position_service.py # 持仓/资金/成交查询
│   │   └── trading_service.py  # 撤单服务
│   ├── models/
│   │   └── config.py         # AppConfig 配置管理（单例 + 热重载）
│   └── utils/
│   │       ├── singleton.py      # 线程安全单例基类（双重检查锁定）
│   │       ├── logger.py         # 单例日志器
│   │       ├── screenshot.py     # 截图工具 + 自动清理
│   │       ├── poll.py           # 轮询等待工具（poll_until / poll_until_not / timed）
│   │       └── diagnostic.py     # 诊断工具（截图+UI文本+OCR）
├── tests/
│   └── test_core.py          # 核心逻辑单元测试（pytest）
├── logs/                     # 日志目录（运行时生成）
│   ├── app.log
│   └── screenshots/
├── main.py                   # 启动入口（waitress + 优雅关闭）
├── .python-version           # Python 版本锁定（3.11）
└── pyproject.toml            # 依赖与构建配置
```

## 技术栈

- **Python 3.11+** / **uv**（包管理）
- **Flask** + **flask-cors**（HTTP 路由，Blueprint 模块化）
- **waitress**（生产级 WSGI 服务器）
- **pywinauto**（UIA backend，窗口/控件自动化）
- **pywin32**（Windows API：按键、窗口、互斥锁）
- **psutil**（进程枚举与 exe 路径匹配）
- **pyautogui**（鼠标点击、全屏截图）
- **ddddocr**（验证码识别，基于 ONNX Runtime）
- **pytest**（单元测试）

## 开发

```bash
# 运行单元测试
uv run pytest

# 运行指定测试文件
uv run pytest tests/test_core.py -v

# 开发模式（热加载）
uv run python main.py --dev
```

## 关键设计要点

- **单 worker 线程任务队列**：所有写操作（下单/撤单/查询）通过 `TaskQueue` 顺序执行，避免 `xiadan.exe` 并发冲突。`/health`、`/queue/status`、`/admin/reload-config` 不入队。
- **看门狗恢复**：任务超时后必须完成所有恢复步骤（截图 + 激活 + ESC×5）才返回错误，确保 HTTP 调用方收到 `TASK_TIMEOUT` 时 `xiadan.exe` 已重置为初始状态。
- **任务前状态重置**：`WindowService.reset_window_state()` 统一处理 `click_input` 激活窗口 + ESC×5 回退到 F1 买入界面。TaskQueue 的 `_reset_trading_window` 和查询方法的 `_prepare_query_panel` 均调用此方法，确保窗口在前台且处于基准态。ESC×5 比 ESC×3 更保守，但总耗时仅 ~0.75s，几乎无成本。
- **查询方法标准化**：所有查询方法通过 `_prepare_query_panel`（`reset_window_state` + F4）进入查询面板，自包含的初始状态保证，不依赖 TaskQueue 前置重置。导航即触发服务器查询，无需 F5 刷新。
- **幂等检查**：60 秒窗口内相同 `code+status+amount+price+price_type` 的下单请求会被拒绝（返回 `DUPLICATE_ORDER` 错误）。下单失败时清除幂等记录允许重试，超时不清除（防止重复提交）。
- **A 股价格自动修正**：限价模式下，传入的价格自动修正为 2 位小数。输入价格前先通过 `WM_SETTEXT` 完全清空控件，再通过 `{HOME}+{END}{BACKSPACE}` 双重清空兜底，解决券商自动填充价格的干扰。
- **弹窗感知与处理**：下单和撤单过程中自动检测弹窗类型（委托确认弹窗、警告弹窗、纯错误弹窗、撤单确认弹窗、通用提示弹窗如 "Begin failed!"），分别处理。
- **事件驱动等待（`poll_until`）**：用 `src/utils/poll.py` 的 `poll_until(condition, timeout, interval)` 替代大部分固定 `time.sleep()`。在下单后等弹窗、Ctrl+C 后等验证码、撤单后等确认弹窗等场景，每 0.1s 检测一次 UI 状态，条件满足立即继续，不再等满固定时长。检测条件写为 lambda 表达式，超时有 `PollTimeoutError` 异常兜底。
- **步骤级计时（`timed`）**：`poll.py` 提供的 `timed` 上下文管理器嵌入所有关键方法的每个步骤，日志输出 `[计时] xxx: N.NNs`。覆盖范围：窗口重置、F4 切换、Ctrl+C 发送、验证码 OCR+输入+确认、树形导航、下单/撤单全流程等。无需额外调试工具即可精准定位每个步骤的耗时。
- **空数据表格兼容**：`_is_valid_table_data` 接受仅有表头无数据行的情况（如当日无成交/无委托时），`_format_table_data` 返回空列表。避免因数据为空而触发 3 次重试（每次 ~11s 验证码处理），从 54s 降到 ~3s。
- **窗口隐藏自动恢复**：`activate_window()` 和 `WindowMonitor` 均不依赖 `IsWindowVisible` 查找窗口。如果 `xiadan.exe` 窗口被隐藏到系统托盘（`visible=0`），`EnumWindows` 仍能匹配到主窗口（按标题 `"网上股票交易系统5.0"` 优先），自动调用 `ShowWindow(SW_SHOW)` 显示后再操作，无需用户手动还原。
- **`sell-all` 流程**：先查持仓定位股票代码 → 自动提取可用余额（兼容多种字段名）→ 市价卖出全部可用数量 → 失败时清除幂等记录。

## 已知陷阱与防御

以下是在开发测试过程中发现的关键陷阱与防御措施。

### 按键安全：前台 keybd_event 泄漏

`send_key()` 对功能键（F1-F12）和 Ctrl+C 使用 `keybd_event` 前台发送，这些按键会发到**当前前台窗口**。如果交易窗口不在前台，F1 会触发 Windows 帮助（打开 Edge）。

**三层防御**（从应急到兜底）：
1. **应急层（即时恢复）**：发送前台按键前先通过 `EnumWindows`（不依赖 `IsWindowVisible`）查找目标窗口。如果窗口隐藏到系统托盘（`visible=0`），自动 `ShowWindow(SW_SHOW)` 显示；如果最小化（`IsIconic`），自动 `ShowWindow(SW_RESTORE)` 恢复。0.2s 内完成。
2. **校验层（句柄验证）**：`click_input()` 激活后，用 `GetForegroundWindow()` 精确验证前台窗口句柄是否匹配，最多 2 次重试。校验失败**抛异常阻止按键发送**，绝不静默泄漏。
3. **兜底层（后台监控）**：`WindowMonitor` 守护线程每 2 秒轮询查找窗口，检测到最小化或隐藏自动恢复。任务执行间歇期的异常窗口状态无需用户干预。

> 进程路径匹配太宽，会命中 `xiadan.exe` 的子窗口/弹窗导致误判，不使用。始终以窗口句柄为校验依据。

同花顺买入/卖出窗口关闭方式：
- **不要使用 ALT+F4**：会关闭整个 `xiadan.exe` 窗口（最小化到系统托盘），导致后续按键全部泄漏到桌面/IDE
- **不要使用 ESC**：买入/卖出子面板不是独立对话框，ESC 无效
- **正确方式**：发送 F4 切换到查询/持仓视图即可关闭买入/卖出子面板

### F4 查询面板：盲按安全

任务前 ESC×5 统一重置到 F1 买入界面，查询面板**必然关闭**。F4 从 F1 切换到查询面板，永远是「打开」动作，盲按 F4 安全，无需状态检测。

### 后台按键 vs 前台按键

| 发送方式 | 依赖前台 | 适用场景 |
|---------|---------|---------|
| `keybd_event` | 是 | 功能键 F1-F12、Ctrl+C 等组合键 |
| `PostMessage` | 否 | 字母键 Y/N、方向键、ENTER、ESC（非子面板） |

功能键（F1-F12）始终走前台 `keybd_event`，因为这些键触发界面切换（买入/卖出/撤单/查询），PostMessage 无法可靠触发窗口的加速键处理。鼠标点击（`click_input()`）和文本输入（`type_keys()`）仍需窗口在前台。

### 控件树缓存

`pywinauto` 的 `descendants()` 会缓存控件树，界面变化后必须重新 `get_trading_window()` 获取新引用。

### 验证码弹窗机制

同花顺的 Ctrl+C 复制操作**必然触发验证码弹窗**（弹窗文本："检测到文本复制，为了数据安全，请输入验证码"）。验证码弹窗的出现 = Ctrl+C 已正确发送到目标窗口的**确认信号**。无验证码弹窗 = Ctrl+C 未到达目标窗口（焦点丢失），本次尝试失败。

**焦点激活**：`_send_ctrl_c` 使用 `click_input()` + `GetForegroundWindow()` 句柄级精确校验，最多 2 次重试，确保 Ctrl+C 到达正确的交易窗口。不依赖进程路径匹配（子窗口/弹窗误判问题）。

处理流程：Ctrl+C → `poll_until` 轮询检测验证码弹窗（每 0.1s 检测一次，3s 超时）→ OCR 识别 → 输入验证码 + 确定 → 读剪贴板 → 校验 `\t` 制表符。验证码处理失败时重试，最多 3 次。相比固定 `time.sleep(1.0)`，轮询方式在验证码弹窗快速弹出时（~0.3s）即可立即继续，无需等满 1 秒。

### Ctrl+C 发送机制：为什么需要延迟和双发

`_send_ctrl_c` 使用 `keybd_event`（虚拟键码）而非 `SendInput`（扫描码），原因如下：

**1. 延迟 0.1s — GetAsyncKeyState 键状态同步**

```
Ctrl Down → sleep(0.1s) → C Down → C Up → Ctrl Up
```

券商软件使用 `GetAsyncKeyState(VK_CONTROL)` 检测 Ctrl 是否物理按住。`keybd_event` 是异步更新系统键状态的，如果按下 Ctrl 后立即（0ms）按 C，系统键状态表还未更新，券商检测不到 Ctrl 被按住→视为普通按键→Ctrl+C 无效。加入 0.1s 延迟让键状态表完成同步。

**2. 双发 — 绕过中文输入法 IME**

每次发送连续两次 Ctrl+C，间隔 0.15s：

```
第一次 Ctrl+C → 被中文 IME 拦截（取消输入法组合状态）
第二次 Ctrl+C → IME 已退出组合模式，正常送达券商
```

中文输入法（微信输入法/微软拼音等）在中文模式下会将首次 Ctrl+C 用于取消自身组合态，不传递给应用程序。第二次 Ctrl+C 时 IME 已转为英文直通模式，正常触发复制。这与用户手动操作的体验一致：中文模式下按第一次 Ctrl+C 无效，第二次才弹验证码。

**3. 为什么不用 SendInput**

`SendInput` + `KEYEVENTF_SCANCODE`（硬件扫描码）理论上可绕过 IME，但会在底层键盘钩子中设置 `LLKHF_INJECTED` 标志。券商软件作为金融安全软件，可能通过 `SetWindowsHookEx(WH_KEYBOARD_LL)` 过滤注入输入。实测 `SendInput` 无法触发验证码弹窗。

**4. 为什么不用 PostMessage**

`PostMessage(hwnd, WM_KEYDOWN, ...)` 将按键直接投递到窗口消息队列，完全绕过 IME 和键状态检查。但券商软件的 Ctrl+C 处理器可能依赖 `GetAsyncKeyState` 或 `GetKeyState` 验证物理按键，`PostMessage` 不更新这些状态，因而无效。

| 方式 | GetAsyncKeyState | LLKHF_INJECTED | 实测结果 |
|------|:---:|:---:|------|
| `keybd_event`（延迟 0.1s） | ✅ 更新 | ❌ 不设 | **成功** |
| `keybd_event`（无延迟） | ❌ 来不及 | ❌ 不设 | 失败 |
| `SendInput` + 扫描码 | ✅ 更新 | ✅ 设置 | 失败（被过滤） |
| `PostMessage` | ❌ 不更新 | ❌ 不设 | 失败 |

### "Begin failed!" 弹窗

券商服务器查询/更新数据失败时弹出的提示弹窗。**所有标签页切换都会触发服务器数据交换，因此都可能出现"Begin failed!"弹窗**——包括 F4 查询面板下点击任意标签页、F3 撤单页面切换、F5 刷新、切换下拉菜单分组等。防御原则：所有标签页切换/服务器交互点后都调用 `_dismiss_popup_if_present` 检测并关闭弹窗，`_is_valid_table_data`（校验 `\t` 制表符）作为内容层面的兜底校验。

### Ctrl+C 复制表单全部数据的适用范围

**适用页面**：F4 查询面板下所有标签页面（当日委托、当日成交、资金股票、历史成交等）、F3 撤单页面。这些页面点击对应标签/切换到对应视图后，直接 Ctrl+C 即可复制表单内的全部股票信息。导航动作本身触发券商服务器查询，无需额外 F5 刷新。

**不适用页面**：F1 买入页面、F2 卖出页面（这两个是下单输入面板，不是表格查询页）。

### Custom Logger 不支持 %s 格式化

项目自定义的 Logger（`src/utils/logger.py`）只接受单个 `message` 参数，**不支持**标准 Python logger 的 `%s` 格式占位符。必须使用 f-string：

```python
# 正确
self.logger.warning(f"错误信息: {e}")
# 错误 - 会抛出 TypeError
self.logger.warning("错误信息: %s", e)
```

### 市价/限价切换机制

F1 买入页面中，点击"买入价格"标签（control_id=1400）**触发的是券商服务器请求**，而非本地下拉菜单。切换成功后，标签文本从"买入价格"（限价模式）变为"市价买入"或"对手方最优"（市价模式，不同券商账户显示可能不同）。切换失败时标签不变。

**检测方式**：用 `poll_until` 轮询标签文本中是否包含 `"市价"` 或 `"最优"` 关键字，timeout 5s，3 次重试。

**注意**：ESC×5 重置窗口状态**不会重置价格模式**。如果此前已切换到市价模式，重置后仍保持市价模式。因此每次切换前必须先读取标签文本判断当前模式，避免重复点击导致 toggle 回限价模式。

**网络异常处理**：3 次重试均超时（标签未变化）时抛出 `MODE_SWITCH_FAILED` 异常，返回错误响应。此时 `xiadan.exe` 保持在限价模式，不会进入不一致状态。

### 提交失败弹窗

点击"是(Y)"确认委托后，券商可能返回"提交失败"弹窗（如余额不足、非交易时间、涨跌停限制等）。系统在确认后 0.3s 检测是否出现新弹窗（cid=1365 标题图 + cid=1040 详情文本），若标题不含"委托确认"则判定为提交失败。

**返回格式**：

```json
{
  "status": "error",
  "error_code": "ORDER_SUBMIT_FAILED",
  "message": "订单提交失败: <弹窗原文>",
  "suggestion": "请检查交易条件（余额、交易时间、涨跌停限制等）后重试",
  "details": {
    "popup_title": "提示",
    "popup_text": "<弹窗内完整文本>"
  }
}
```

检测到失败弹窗后自动按 ENTER 关闭，拍摄截图存档，然后返回错误响应。

### HTTP 200 统一响应

所有 API 响应（成功和失败）统一返回 HTTP 200。`response.py` 中 `error_response` 虽保留 `HTTP_STATUS` 映射表（用于内部记录），但实际返回的 HTTP 状态码固定为 200。

**原因**：PowerShell 的 `Invoke-RestMethod` 在收到 HTTP 4xx/5xx 时会直接抛出 `HttpResponseException`，调用方无法获取 `response.json()` 中的错误详情。HTTP 200 统一后，调用方通过 `response.status` 字段（`"success"` / `"error"`）判断结果，`error_code` 和 `message` 始终可读。

### 菜单栏自定义渲染：Win32 API 和 UIA 均不可见

同花顺 xiadan.exe 的菜单栏（右上角「系统」按钮及其下拉菜单）使用**完全自定义渲染**，不是标准 Win32 HMENU，也不在 UIA 树中暴露下拉菜单项：

| 方法 | 结果 |
|------|------|
| `win32gui.GetMenu(hwnd)` | 返回 0（无标准菜单句柄） |
| `win32gui.GetMenuString()` | 无效 |
| UIA `Desktop().windows()` | 找不到 PopupMenu 弹窗 |
| UIA `window.descendants()` | MenuItem "系统" 的 children 为空 |
| `ExpandCollapsePattern` | 不支持 |
| `LegacyIAccessiblePattern.DoDefaultAction()` | 不支持 |

**结论**：无法通过程序自动化操作菜单栏（如自动打开「系统设置→快速交易」）。相关自动配置代码已移除，用户需在启动网关前**手动配置**快速交易设置（见 [前置准备：券商软件设置](#前置准备券商软件设置)）。

### 诊断方式选择

| 方式 | 可靠性 | 说明 |
|------|--------|------|
| **UI 控件文本提取**（pywinauto） | 高 | 直接枚举窗口控件文本，精确可靠 |
| **全文本 OCR**（ddddocr） | 低 | 对整屏截图中文识别极差，仅验证码场景（小图数字）可用 |

开发测试时应优先使用 UI 控件文本提取判断界面状态。每个任务执行后自动记录诊断到 20 条循环历史队列，通过 `GET /diagnostic/history` 查看。
