# xiadan-gateway

同花顺 `xiadan.exe` 交易网关 - 通过 HTTP API 控制同花顺下单程序进行股票交易。

## 核心特性

| 特性 | 说明 |
|------|------|
| 单实例运行 | 通过 Windows 全局互斥锁保证同一时刻只有一个实例运行 |
| 顺序执行 | 单 worker 线程的任务队列，避免 `xiadan.exe` 并发冲突 |
| 看门狗机制 | 任务超时自动触发截图 + ESC + 激活 + F1 恢复 |
| 幂等检查 | 60 秒窗口内相同参数的下单请求会被拒绝，防止 HTTP 超时重试导致重复下单 |
| OCR 验证码 | 基于 `ddddocr` 自动识别同花顺查询时的验证码 |
| 一键清仓 | `/orders/sell-all` 自动查询持仓并市价卖出指定股票全部可用数量 |
| 价格自动校验 | A 股价格限制 2 位小数，API 层拦截 + 输入前自动修正 |

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

编辑 `config/app_config.json`，**必须配置 `trading_app_path`** 指向你的 `xiadan.exe` 路径。

```json
{
  "trading_app_path": "C:\\Users\\xxx\\同花顺远航版\\transaction\\xiadan.exe",
  "window_monitor": { "enabled": true, "check_interval": 5 },
  "task_queue": {
    "max_size": 50,
    "watchdog_timeout_seconds": 30,
    "query_timeout_seconds": 15,
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
| `trading_app_path` | "" | `xiadan.exe` 完整路径，**必须配置** |
| `task_queue.watchdog_timeout_seconds` | 30 | 下单看门狗超时（秒） |
| `task_queue.query_timeout_seconds` | 15 | 查询类操作超时（秒） |
| `task_queue.confirm_timeout_seconds` | 10 | 确认/按键类操作超时（秒） |
| `task_queue.max_size` | 50 | 任务队列最大长度 |
| `idempotency.order_dedup_window_seconds` | 60 | 下单去重窗口（秒） |
| `ocr.max_retry` | 3 | 验证码识别最大重试次数 |
| `window_monitor.enabled` | true | 是否启用窗口最小化监控 |

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

### 错误码与 HTTP 状态码

| 错误码 | HTTP | 说明 |
|--------|------|------|
| `VALIDATION_ERROR` | 400 | 参数校验失败（含价格格式、缺少必填参数等） |
| `DUPLICATE_ORDER` | 409 | 60 秒内重复下单 |
| `AUTH_REQUIRED` | 401 | 认证未开启时请求需认证的接口 |
| `AUTH_FAILED` | 401 | 认证 token 无效 |
| `WINDOW_NOT_FOUND` | 503 | 交易窗口未找到 |
| `CONTROL_NOT_FOUND` | 500 | 控件未找到 |
| `MODE_SWITCH_FAILED` | 500 | 限价/市价切换失败 |
| `OCR_FAILED` | 500 | 验证码识别失败 |
| `INTERNAL_ERROR` | 500 | 未知异常 |
| `QUEUE_TIMEOUT` | 503 | 任务排队超时 |
| `QUEUE_FULL` | 503 | 队列已满 |
| `TASK_TIMEOUT` | 504 | 任务超时，恢复成功 |
| `TASK_TIMEOUT_RECOVERY_FAILED` | 504 | 任务超时，恢复也失败 |

## API 接口

| 方法 | 路径 | 说明 | 入队 | 推荐 timeout |
|------|------|------|------|-------------|
| GET | `/health` | 健康检查 | 否 | 5s |
| GET | `/queue/status` | 任务队列状态 | 否 | 5s |
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
- 输入价格前先通过 `WM_SETTEXT` 完全清空控件（解决券商自动填充价格的干扰），再通过 `{HOME}+{END}{BACKSPACE}` 双重清空兜底
- 输入股票代码后等 0.5 秒让自动填充完成，再清空价格框填入新价格
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

# 只填单不确认
curl -X POST http://localhost:5000/orders/sell-all \
  -H "Content-Type: application/json" \
  -d '{"code": "002366", "confirm": "false"}'
```

**流程：**
1. 查询当前持仓
2. 按 `code` 匹配到对应股票
3. 提取可用余额（兼容 `可用余额` / `可用数量` / `可卖数量` / `卖出数量` 等字段名）
4. 市价提交卖出委托

**错误场景：**
- 股票 `code` 不在持仓中 → 返回 `VALIDATION_ERROR`
- 可用余额 = 0 → 返回 `VALIDATION_ERROR`（提示无需卖出）

### POST /orders/pending 当日委托

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

**说明：**
- 进入撤单界面后自动读取「撤单不需要确认」复选框状态，未勾选才点击勾选（避免误反选）
- 勾选后自动处理二次确认弹窗（"您取消了撤单前确认提示功能"）
- 点击撤单按钮后**始终检测**确认弹窗，不依赖复选框状态
- 弹窗文字完整读取并记录到日志，通过关键词「委托」区分撤单确认弹窗和其他弹窗

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
│   ├── api/                  # API 层
│   │   ├── routes.py         # Flask 路由 + 应用工厂
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
│   │   └── config.py         # AppConfig 配置管理（单例）
│   └── utils/
│       ├── logger.py         # 单例日志器
│       └── screenshot.py     # 截图工具
├── logs/                     # 日志目录（运行时生成）
│   ├── app.log
│   └── screenshots/
├── main.py                   # 启动入口
└── pyproject.toml            # 依赖与构建配置
```

## 技术栈

- **Python 3.11+** / **uv**（包管理）
- **Flask** + **flask-cors**（HTTP 服务）
- **pywinauto**（UIA backend，窗口/控件自动化）
- **pywin32**（Windows API：按键、窗口、互斥锁）
- **psutil**（进程枚举与 exe 路径匹配）
- **pyautogui**（鼠标点击、全屏截图）
- **ddddocr**（验证码识别，基于 ONNX Runtime）

## 关键设计要点

- **单 worker 线程任务队列**：所有写操作（下单/撤单/查询）通过 `TaskQueue` 顺序执行，避免 `xiadan.exe` 并发冲突。`/health` 和 `/queue/status` 不入队。
- **看门狗恢复**：任务超时后必须完成所有恢复步骤（截图 + ESC + 激活 + F1）才返回错误，确保 HTTP 调用方收到 `TASK_TIMEOUT` 时 `xiadan.exe` 已重置为初始状态。
- **任务前状态重置**：每个任务开始前执行 ESC×2 + 激活 + F1，确保从已知初始状态开始。
- **幂等检查**：60 秒窗口内相同 `code+status+amount+price+price_type` 的下单请求会被拒绝（HTTP 409）。
- **控件树缓存**：`pywinauto` 的 `descendants()` 会缓存控件树，界面变化后必须重新 `get_trading_window()`。
- **后台按键（PostMessage）**：字母键（Y/N）、ESC、ENTER、组合键（Ctrl+C 等）通过 `PostMessage` 直接发送到 `xiadan.exe` 的消息队列，**不改变前台窗口、不抢焦点**。
  - `send_key()` 自动检测交易窗口句柄，有则走 PostMessage 后台发送，无则 fallback 到 `keybd_event`（前台发送）。
  - **功能键（F1-F12）始终走前台 `keybd_event`**，因为这些键触发界面切换（买入/卖出/撤单/查询），PostMessage 无法可靠触发窗口的加速键处理。
  - **局限性**：鼠标点击（`click_input()`）和文本输入（`type_keys()`）仍需窗口在前台。下单（`place_order`）和撤单（`cancel_all_orders`）因此仍会激活窗口。
- **A 股价格自动修正**：限价模式下，传入的价格自动修正为 2 位小数（如 `1.20100` → `1.20`）。API 层也会校验，超 2 位小数的价格返回 400 错误。
  - 输入价格前先通过 `WM_SETTEXT` 完全清空控件，再通过 `{HOME}+{END}{BACKSPACE}` 双重清空兜底，解决券商自动填充价格的干扰。
  - 输入股票代码后等 0.5 秒让自动填充完成，再清空价格框填入新价格。
- **弹窗感知与处理**：下单和撤单过程中自动检测弹窗类型：
  - **委托确认弹窗**：检测 `cid=1365` 标题 → 读取委托详情（`cid=1040`）→ 点击「是(Y)」或「否(N)」
  - **警告弹窗**（如价格格式有误，含 Y/N 按钮）：点击「是(Y)」继续，然后继续检测委托确认弹窗
  - **纯错误弹窗**（无 Y/N 按钮）：ENTER 关闭后报错退出
  - **撤单确认弹窗**：读取完整弹窗文字，解析可撤委托数量，点击「是(Y)」确认
- **撤单调复选框**：进入撤单界面后读取「撤单不需要确认」复选框的实际勾选状态（`get_toggle_state()`），未勾选才点击勾选（避免误反选）。点击撤单按钮后始终检测确认弹窗，不依赖复选框状态。
- **幂等回滚**：下单失败时清除幂等记录，允许客户端重试。超时不清除（防止重复提交），客户端应通过 `/orders/pending` 确认状态。
- **`sell-all` 流程**：先查持仓定位股票代码 → 自动提取可用余额（兼容多种字段名）→ 市价卖出全部可用数量 → 失败时清除幂等记录。
