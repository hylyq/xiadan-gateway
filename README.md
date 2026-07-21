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
| `trading_app_path` | "" | `xiadan.exe` 完整路径，**必须配置** |
| `task_queue.watchdog_timeout_seconds` | 45 | 下单看门狗超时（秒） |
| `task_queue.query_timeout_seconds` | 60 | 查询类操作超时（秒，含验证码/弹窗处理） |
| `task_queue.confirm_timeout_seconds` | 10 | 确认/按键类操作超时（秒） |
| `task_queue.max_size` | 50 | 任务队列最大长度 |
| `idempotency.order_dedup_window_seconds` | 60 | 下单去重窗口（秒） |
| `ocr.max_retry` | 3 | 验证码识别最大重试次数 |
| `window_monitor.enabled` | true | 是否启用窗口最小化监控 |

> **热更新**：修改配置后可调用 `POST /admin/reload-config` 热重载，无需重启服务。部分配置（如 `trading_app_path`）仍需重启才能完全生效。通过 `GET /health` 确认服务状态。

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

### POST /actions/close-dialog 安全关闭子面板

关闭买入/卖出等嵌入子面板（通过 F4 切换视图实现，绝不关闭整个程序）。

| 参数 | 必填 | 说明 |
|------|------|------|
| `title` | 否 | 面板名称（如 `"买入"`），仅用于日志 |

```bash
curl -X POST http://localhost:5000/actions/close-dialog -H "Content-Type: application/json" -d '{"title": "买入"}'
```

**说明：** 同花顺的买入/卖出窗口是嵌入主窗口的子视图（不是独立对话框），ESC/WM_CLOSE 均无效。本接口发送 F4 切换到查询视图来实现关闭。

### GET /diagnostic/snapshot 诊断快照

当前窗口的截图路径 + UI 控件文本 + OCR 全文识别。不入队，立即返回。

```bash
curl http://localhost:5000/diagnostic/snapshot
```

响应示例：
```json
{
  "status": "success",
  "data": {
    "screenshot": "logs/screenshots/api_diagnostic_20260721_120000.png",
    "ui_text": "[窗口标题] 网上股票交易系统5.0\n可用金额\n148444.77\n...",
    "ocr_text": "",
    "ocr_failed": true
  }
}
```

### GET /diagnostic/history 诊断历史

返回最近 N 个任务执行后的界面状态快照。让我（AI 助手）可以查看每一步操作后的窗口状态，从而自信地编写和调试代码。

| 参数 | 必填 | 说明 |
|------|------|------|
| `n` | 否 | 返回条数（默认 5，最大 20） |

```bash
curl "http://localhost:5000/diagnostic/history?n=3"
```

响应示例：
```json
{
  "status": "success",
  "data": {
    "total_returned": 3,
    "max_available": 20,
    "entries": [
      {
        "task_name": "get_position",
        "elapsed_seconds": 14.22,
        "success": true,
        "timestamp": "17:01:13",
        "ui_text": "[窗口标题] 网上股票交易系统5.0\n可用金额\n148444.77\n..."
      }
    ]
  }
}
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
│       ├── singleton.py      # 线程安全单例基类（双重检查锁定）
│       ├── logger.py         # 单例日志器
│       ├── screenshot.py     # 截图工具 + 自动清理
│       └── diagnostic.py     # 诊断工具（截图+UI文本+OCR）
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
- **看门狗恢复**：任务超时后必须完成所有恢复步骤（截图 + ESC + 激活 + F4）才返回错误，确保 HTTP 调用方收到 `TASK_TIMEOUT` 时 `xiadan.exe` 已重置为初始状态。
- **任务前状态重置**：每个任务开始前执行 ESC×2 + 激活 + F4，确保从查询面板（安全起点）开始。
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
  - **委托确认弹窗**：检测 `cid=1365` 标题且含"委托确认"文字 → 读取委托详情（`cid=1040`）→ 点击「是(Y)」或「否(N)」，**仅此情况设置 `confirmed=true`**
  - **警告弹窗**（标题为"提示信息"等，如价格超限提醒）：点击「是(Y)」关闭警告，**不设置 confirmed**，继续等待后续委托确认弹窗
  - **纯错误弹窗**（无 Y/N 按钮）：ENTER 关闭后报错退出
  - **撤单确认弹窗**：读取完整弹窗文字，解析可撤委托数量，点击「是(Y)」确认
  - **通用提示弹窗**（如 "Begin failed!"）：查询流程中自动检测并关闭（点击确定/ENTER）
- **撤单调复选框**：进入撤单界面后读取「撤单不需要确认」复选框的实际勾选状态（`get_toggle_state()`），未勾选才点击勾选（避免误反选）。点击撤单按钮后始终检测确认弹窗，不依赖复选框状态。
- **幂等回滚**：下单失败时清除幂等记录，允许客户端重试。超时不清除（防止重复提交），客户端应通过 `/orders/pending` 确认状态。
- **`sell-all` 流程**：先查持仓定位股票代码 → 自动提取可用余额（兼容多种字段名）→ 市价卖出全部可用数量 → 失败时清除幂等记录。
- **F4 切换键状态检测**：F4 是查询面板的切换键（按一次开、再按一次关），不能盲按两次。通过检测窗口中是否存在查询面板树形菜单项（"资金股票"、"当日委托"等）判断面板状态，仅在未打开时按 F4。
- **当日委托"全部"视图**：同花顺"当日委托"页面默认"分组"视图将表格分为上下两区（等待中/已撤单），Ctrl+C 仅复制下半区。查询前自动通过 `cid=2410` 下拉框切换为"全部"视图，查询后恢复"分组"。

## 已知问题与注意事项

以下是在开发测试过程中发现的关键陷阱与防御措施。

### 按键安全：前台 keybd_event 泄漏

`send_key()` 对功能键（F1-F12）使用 `keybd_event` 前台发送，这些按键会发到**当前前台窗口**。如果交易窗口不在前台，F1 会触发 Windows 帮助（打开 Edge）。

**防御**：发送前台按键前必须调用 `click_input()` 将交易窗口带到前台。窗口未找到或激活失败时**必须抛出异常**，绝不静默发送按键。

```python
# window_service.py :: _activate_window_before_keybd()
window = self.get_trading_window()
if window is None:
    raise Exception("交易窗口未找到，禁止发送按键")  # 不发送！
window.click_input()  # 确保在前台
self._send_key_foreground(keys)
```

同花顺买入/卖出窗口关闭方式：
- **不要使用 ALT+F4**：会关闭整个 `xiadan.exe` 窗口（最小化到系统托盘），导致后续按键全部泄漏到桌面/IDE，可能造成 IDE 关闭等连锁反应
- **不要使用 ESC**：买入/卖出子面板不是独立对话框，ESC 无效
- **正确方式**：发送 F4 切换到查询/持仓视图即可关闭买入/卖出子面板

### F4 切换键状态管理

在 `xiadan.exe` 中，F4 是查询面板的**切换键**：
- 按一次 F4：打开查询面板
- 再按一次 F4：关闭查询面板

如果上一个任务（如 `get_balance()`）已经按了 F4 打开了查询面板，下一个任务（如 `get_position()`）再按 F4 会**关闭**查询面板，导致 Ctrl+C 复制的是交易主面板数据（空数据）。

**防御**：每个查询方法开头先按一次 F4（确保关闭可能已打开的面板），再按 F5 刷新，最后再按 F4 打开查询面板：

```python
self.send_key("F4")  # 关闭可能已打开的查询面板
time.sleep(0.3)
self.send_key("F5")  # 刷新
time.sleep(0.3)
self.send_key("F4")  # 打开查询面板（默认进入持仓视图）
```

### Custom Logger 不支持 %s 格式化

项目自定义的 Logger（`src/utils/logger.py`）只接受单个 `message` 参数，**不支持**标准 Python logger 的 `%s` 格式占位符：

```python
# 正确 - 使用 f-string
self.logger.warning(f"错误信息: {e}")
self.logger.info(f"状态: {status}")

# 错误 - 不支持 %s 语法（会抛出 TypeError）
self.logger.warning("错误信息: %s", e)  # TypeError!
```

### 诊断截图 + OCR 的局限性

诊断工具 `DiagnosticUtil` 提供两种方式确认界面状态：

| 方式 | 可靠性 | 说明 |
|------|--------|------|
| **UI 控件文本提取**（pywinauto） | 高 | 直接枚举窗口控件文本，精确可靠 |
| **全文本 OCR**（ddddocr） | 低 | 对整屏截图中文识别极差，仅验证码场景（小图数字）可用 |

开发测试时应优先使用 UI 控件文本提取来判断界面状态（如检测 `证券代码` 字段判断买入窗口是否打开）。

### 验证码弹窗触发机制（重要）

同花顺的验证码弹窗**不是随机出现的**，而是由 **Ctrl+C 剪贴板复制操作确定性触发**的安全机制：

> 弹窗文本："检测到文本复制，为了数据安全，请输入验证码"

**触发链路**：脚本执行 Ctrl+C 复制表格数据 → 同花顺检测到剪贴板复制行为 → 弹出验证码窗口（`cid=2405` 图片 + `cid=2404` 输入框）

**影响范围**：所有通过 Ctrl+C 读取数据的查询操作（持仓、资金、成交、委托）都会触发，OCR 验证码处理是查询流程的**必要环节**而非可选兜底。

**当前处理流程**（`_copy_table_via_clipboard`）：
1. Ctrl+C 复制表格数据
2. 检测验证码弹窗（`cid=2405` 控件 + 文本关键词双重检测）
3. 截图验证码图片 → ddddocr OCR 识别（最多重试 `max_retry` 次）
4. 输入验证码 + 点击确定
5. 验证成功后重新 Ctrl+C 读取数据

**性能影响**：每次验证码处理增加约 3-5 秒（截图 + OCR + 输入 + 验证），这是查询操作耗时 20-30 秒的主要原因之一。

**后续优化方向**：探索非 Ctrl+C 的数据读取方式（如 UIA 控件树直接读取、内存读取等），从根本上避免触发验证码。

### 自动诊断历史

每个任务执行后（无论成功失败），系统自动调用 `DiagnosticUtil` 记录界面状态（UI 文本 + 截图路径），保存到 20 条循环历史队列。可以通过 `GET /diagnostic/history` 随时查看最近 N 步操作后的窗口状态，无需手动调用诊断。

开发测试流程：
1. 执行 API 操作（如下单、查询）
2. 调用 `GET /diagnostic/history?n=1` 查看操作后的界面状态
3. 通过 UI 文本确认窗口是否正确切换、数据是否正确显示
4. 若发现问题，UI 文本会明确显示当前窗口上的所有控件文字，帮助快速定位

```bash
# 执行操作后自动记录诊断，随时查看历史
curl "http://localhost:5000/diagnostic/history?n=3"
```

### keybd_event 窗口焦点依赖

`keybd_event` 系列函数（Ctrl+C、功能键等）必须确保目标窗口在前台。`PostMessage` 系列函数（普通字母键）可后台发送，不依赖焦点。

| 发送方式 | 依赖前台 | 适用场景 |
|---------|---------|---------|
| `keybd_event` | 是 | 功能键 F1-F12、Ctrl+C 等组合键 |
| `PostMessage` | 否 | 字母键 Y/N、方向键、ENTER、ESC（非子面板） |

在任务 worker 线程中调用 `click_input()` 可以正常将窗口带到前台（与主线程不同，`SetForegroundWindow` 在 worker 线程无效）。
