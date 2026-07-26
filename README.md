# xiadan-gateway

同花顺 `xiadan.exe` 交易网关 — 通过 HTTP API 控制同花顺下单程序进行股票交易。

> ## ⚠️ 免责声明
>
> **本项目仅供学习和研究用途。使用者应自行承担使用本软件的一切风险和责任。**
>
> - 本项目**不构成任何投资建议**，不推荐任何股票、不预测市场走势、不提供交易策略
> - 股票投资存在**本金全部损失的风险**，过往业绩不代表未来表现
> - 使用本软件进行的任何交易操作及其盈亏结果，**完全由使用者自行承担**
> - 作者**不承担**因使用或误用本软件而导致的任何直接或间接损失
> - 请确保您的交易行为**符合当地法律法规**及券商服务条款
> - **市场有风险，投资需谨慎。入市前请充分了解风险，理性投资。**

## 原理

```
浏览器/脚本 ──HTTP──→ Flask + waitress ──→ TaskQueue ──→ pywinauto ──→ xiadan.exe
                         │                    │               │
                    认证/路由/响应         单线程顺序执行    UIA 控件自动化
                                                     │
                                              ┌──────┴──────┐
                                         查询(读)          交易(写)
                                    Ctrl+C 剪切板复制    F1/F2 填写表单
                                    + OCR 验证码识别     + 弹窗检测/确认
```

1. **HTTP 接口层**：Flask + waitress 提供 REST API，Token 认证，统一 JSON 响应格式
2. **任务队列**：单 worker 线程顺序执行，避免并发操作 `xiadan.exe` 导致 UI 冲突
3. **UI 自动化**：pywinauto (UIA 后端) 操控控件——读取文本、填写输入框、点击按钮
4. **OCR 验证码**：Ctrl+C 必然触发验证码弹窗，轻量模板匹配引擎自动识别（详见 [验证码 OCR](#验证码-ocr--轻量模板匹配)）
5. **窗口监控**：后台线程定期检测交易窗口状态，最小化时自动恢复
6. **下单耗时优化**：通过复用 UIA 控件树遍历、跳过无变化步骤、模式切换流水线、连续干净跳过等手段，单笔限价买入从 ~13.7s 降至 ~8.0s（冷启动）/~5.7s（同向）/~6.0s（交叉方向），详见 [核心特性](#核心特性)「连续干净跳过」

## 核心特性

| 特性 | 说明 |
|------|------|
| 单实例运行 | Windows 全局互斥锁保证同一时刻只有一个实例 |
| 顺序执行 | 单 worker 线程任务队列，避免 `xiadan.exe` 并发冲突 |
| 连续干净跳过 | 上笔干净退出→跳过 `_reset_trading_window`+激活。**同组内同向完全跳过，交叉方向只按 F1/F2**。有弹窗/失败/跨组则完整准备。操作分组：`trade`(买/卖)、`cancel`(撤单)，其余独立 |
| 模式切换流水线 | 限价↔市价切换时先点按钮不等待，立即填数量——数量填充的 ~0.7s 与标签变化重叠，验证在填数量之后自然就绪 |
| 弹窗分类处理 | 委托确认→点Y/N；警告→点Y继续；**价格超限→点N取消+返回`PRICE_OUT_OF_RANGE`**；错误→关闭+报错 |
| 看门狗恢复 | 任务超时自动截图 + 激活 + ESC×5，重置后返回错误 |
| 幂等检查 | 60s 窗口内相同参数的下单被拒绝，防 HTTP 超时重试重复下单 |
| OCR 验证码 | 轻量模板匹配引擎，失败自动存档，可选 ddddocr 离线训练 |
| 一键清仓 | `/orders/sell-all` 自动查持仓 → 市价卖出全部可用数量 |
| 生产级服务器 | `waitress` WSGI + 优雅关闭（SIGINT/SIGTERM） |
| 配置热更新 | `POST /admin/reload-config` 无需重启 |
| 截图自动清理 | 启动时清理过期截图（保留 200 张 / 7 天内） |
| 认证安全 | Token 使用 `hmac.compare_digest` 常量时间比较 |

## 前置准备：券商软件设置

启动前必须手动配置以下设置，跳过确认弹窗以提升交易速度。

**打开方式**：独立下单窗口顶部菜单「设置」→ 选项卡「快速交易」，将以下 4 项全部设为「否」：

| 设置项 | 必须值 | 原因 |
|--------|:---:|------|
| 撤单前是否需要确认 | **否** | 跳过撤单确认弹窗 |
| 买入时是否需要确认 | **否** | 跳过买入委托确认弹窗 |
| 卖出时是否需要确认 | **否** | 跳过卖出委托确认弹窗 |
| 委托成功后是否弹出提示对话框 | **否** | 减少交易成功后的提示弹窗干扰 |

> 只需配置一次。关闭确认后，委托直接提交不再弹窗——`confirm=false` 参数不再生效。如需预览委托详情，需在券商软件中重新开启。

## 快速开始

**环境**：Windows / Python 3.11+ / [uv](https://github.com/astral-sh/uv) / 已安装同花顺 `xiadan.exe`

```bash
uv sync                           # 安装依赖
uv run python main.py             # 启动服务（默认 http://localhost:5000）
uv run python main.py --dev       # 开发模式（热加载）
```

## 配置

复制 `config/app_config.example.json` 为 `config/app_config.json`，修改 `trading_app_paths`：

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

| 关键配置 | 默认值 | 说明 |
|---------|--------|------|
| `trading_app_paths` | `[]` | `xiadan.exe` 完整路径列表（按优先级排序），**至少配一个** |
| `task_queue.watchdog_timeout_seconds` | 30 | 下单看门狗超时（秒） |
| `task_queue.query_timeout_seconds` | 15 | 查询操作超时（秒） |
| `task_queue.confirm_timeout_seconds` | 10 | 确认/按键操作超时（秒） |
| `task_queue.max_size` | 50 | 队列最大长度 |
| `idempotency.order_dedup_window_seconds` | 60 | 下单去重窗口（秒） |
| `ocr.max_retry` | 3 | 验证码识别最大重试次数 |
| `ocr.ddddocr_enabled` | false | ddddocr 调试开关（开启后可启用双引擎质检+模板提取，需 `uv sync --extra ocr`） |
| `window_monitor.enabled` | true | 窗口最小化监控开关 |
| `auth.enabled` | false | Token 认证开关 |

> 修改配置后 `POST /admin/reload-config` 热重载（部分路径变更需重启）。

## API 响应格式

所有响应统一返回 HTTP 200，通过 JSON `status` 字段区分成功/失败。

**成功**：
```json
{
  "status": "success",
  "request_id": "req_20260721_120000_a1b2c3d4e5f6",
  "timestamp": "2026-07-21 12:00:00",
  "data": { ... }
}
```

**失败**：
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

### 错误码

| 错误码 | 说明 |
|--------|------|
| `VALIDATION_ERROR` | 参数校验失败 |
| `DUPLICATE_ORDER` | 60s 内重复下单 |
| `AUTH_REQUIRED` | 缺少认证 token |
| `AUTH_FAILED` | 认证 token 无效 |
| `WINDOW_NOT_FOUND` | 交易窗口未找到 |
| `CONTROL_NOT_FOUND` | 控件未找到 |
| `MODE_SWITCH_FAILED` | 限价/市价切换失败 |
| `ORDER_SUBMIT_FAILED` | 订单提交失败（通用，含弹窗原文） |
| `SERVER_CLEARING` | 券商系统清算中 |
| `OUTSIDE_TRADING_HOURS` | 非交易时段 |
| `T1_RESTRICTION` | T+1 制度限制（当日买入次日可卖） |
| `INSUFFICIENT_SHARES` | 可卖数量不足 |
| `PRICE_OUT_OF_RANGE` | 价格超出涨跌停限制（点「否」取消，干净退出，下次同向可跳过） |
| `SERVER_UNAVAILABLE` | 券商服务器不可用（事务处理机转发失败等） |
| `OCR_FAILED` | 验证码识别失败 |
| `INTERNAL_ERROR` | 未知异常 |
| `QUEUE_TIMEOUT` | 任务排队超时 |
| `QUEUE_FULL` | 队列已满 |
| `TASK_TIMEOUT` | 任务超时，恢复成功 |
| `TASK_TIMEOUT_RECOVERY_FAILED` | 任务超时，恢复也失败 |

### 弹窗处理与「干净退出」

下单/撤单后可能出现多种弹窗，处理方式决定窗口状态是否可信：

| 弹窗类型 | 标题示例 | 按钮 | 处理 | 窗口状态 |
|---------|------|:---:|------|:---:|
| 委托确认 | 「委托确认」 | 是(Y) / 否(N) | 点 Y 确认 / 点 N 取消 | 可信 |
| 价格超限 | 「提示信息」 | 是(Y) / 否(N) | 点 N 取消 → `PRICE_OUT_OF_RANGE` | 可信（干净退出） |
| 单按钮提示 | 「提示」 | 确定 | 点击确定（只能用鼠标，Y 键无效） | 取决于内容 |
| 余额不足 | 「提示」 | 确定 | 点确定关闭 → `ORDER_SUBMIT_FAILED` | 可信（干净退出） |
| 致命错误 | 「提示信息」 | 确定 | 关闭 + 分类报错 | **不可信** |

> **注意**：标题为「提示」的单按钮弹窗只有「确定」按钮（cid=1），无法用字母键触发。调试时若发现 Y 键无效，检查是否为单按钮弹窗。

#### 添加新的干净退出场景

只需两步，不需要改动 `TaskQueue` 或跳过逻辑：

**1. 在 `src/exceptions.py` 新增错误码：**
```python
NEW_ERROR = "NEW_ERROR"   # 描述
```

**2. 在 `src/core/trader.py` 的弹窗处理循环中，`_is_clean_error` 旁新增检测：**
```python
# 检测条件：order_detail_text 含特定关键词
# （order_detail_text 由 _extract_dialog_text 从弹窗容器内提取，不含主窗口 UI）
elif "某关键词" in (order_detail_text or ""):
    # 点关闭弹窗
    self._close_non_confirm_popup(window, descendants=_descendants)
    Trader._clean_dismiss = True
    raise ApiError(ErrorCode.NEW_ERROR, "描述", suggestion="建议")
```

`_clean_dismiss = True` 通知 `TaskQueue`：虽是错误，但弹窗已正常关闭，窗口状态仍可信，下次同组操作可跳过准备。

> **弹窗文本提取说明**：`order_detail_text` 优先从 cid=1040 读取，fallback 用 `_extract_dialog_text(title_el)` 从弹窗容器（`title_el.parent()`）内收集文本，确保不混入主窗口 UI 标签。

## API 接口

| 方法 | 路径 | 说明 | 入队 | timeout |
|------|------|------|:---:|--------|
| GET | `/health` | 健康检查 + 推荐客户端 timeout | | 5s |
| GET | `/queue/status` | 任务队列状态 | | 5s |
| POST | `/admin/reload-config` | 热重载配置 | | 5s |
| GET | `/account/balance` | 资金余额 | ✓ | 40s |
| GET | `/positions` | 持仓查询 | ✓ | 40s |
| GET | `/trades/today` | 今日成交 | ✓ | 40s |
| GET | `/orders/pending` | 当日委托 | ✓ | 40s |
| POST | `/orders` | 下单（限价/市价） | ✓ | 40s |
| POST | `/orders/sell-all` | 一键清仓 | ✓ | 40s |
| POST | `/orders/cancel-all` | 撤单（全部/撤买/撤卖） | ✓ | 40s |
| POST | `/orders/confirm` | Y 键确认委托 | ✓ | 30s |
| POST | `/actions/send-key` | 手动发送按键 | ✓ | 30s |
| POST | `/actions/click` | 鼠标点击坐标 | ✓ | 30s |
| POST | `/actions/close-dialog` | 关闭买入/卖出子面板 | ✓ | 30s |
| GET | `/ocr/quality` | OCR 质检报告（准确率/模板/覆盖） | | 5s |
| GET | `/diagnostic/snapshot` | 截图 + UI 文本 + OCR | | 10s |
| GET | `/diagnostic/history` | 最近 N 步任务诊断历史 | | 5s |

> POST 接口同时支持 JSON body 和 query string 传参。

### POST /orders — 下单

| 参数 | 必填 | 说明 |
|------|:---:|------|
| `code` | ✓ | 股票代码 |
| `status` | ✓ | `1`=买入, `2`=卖出 |
| `amount` | | 委托数量 |
| `price` | | 委托价格（限价模式，最多 2 位小数） |
| `price_type` | | `limit`=限价(默认), `market`=市价 |
| `confirm` | | `true`=自动确认(默认), `false`=不确认 |

```bash
# 市价买入
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -d '{"code":"601991","status":"1","amount":"100","price_type":"market"}'

# 限价买入
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -d '{"code":"600000","status":"1","amount":"100","price":"10.50","price_type":"limit"}'

# 市价卖出
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -d '{"code":"601991","status":"2","amount":"100","price_type":"market"}'
```

### POST /orders/sell-all — 一键清仓

自动查询持仓 → 按 code 匹配 → 提取可用余额 → 市价卖出全部。

| 参数 | 必填 | 说明 |
|------|:---:|------|
| `code` | ✓ | 股票代码 |
| `confirm` | | `true`=自动确认(默认) |

```bash
curl -X POST http://localhost:5000/orders/sell-all \
  -H "Content-Type: application/json" \
  -d '{"code":"002366"}'
```

### POST /orders/cancel-all — 撤单

| 参数 | 必填 | 说明 |
|------|:---:|------|
| `type` | | `A`=全部(默认), `X`=撤买, `C`=撤卖 |

```bash
curl -X POST http://localhost:5000/orders/cancel-all
curl -X POST http://localhost:5000/orders/cancel-all -d '{"type":"X"}'
```

### 辅助接口

```bash
# 手动发送按键
curl -X POST http://localhost:5000/actions/send-key -d '{"key":"F1"}'

# 鼠标点击
curl -X POST http://localhost:5000/actions/click -d '{"x":100,"y":200}'

# 关闭子面板（通过 F4 切换视图，不关闭整个程序）
curl -X POST http://localhost:5000/actions/close-dialog -d '{"title":"买入"}'

# 诊断快照（截图 + UI 控件文本 + OCR）
curl http://localhost:5000/diagnostic/snapshot

# 诊断历史（最近 N 步任务后的界面状态）
curl "http://localhost:5000/diagnostic/history?n=3"
```

## 调用方 timeout 配置

调用方 HTTP timeout **必须 > 服务端看门狗 timeout + 恢复耗时（~5s）**。推荐通过 `/health` 动态获取：

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

## 项目结构

```
xiadan-gateway/
├── config/
│   ├── app_config.json          # 运行时配置（gitignore）
│   ├── app_config.example.json  # 配置模板
│   └── key_config.py            # Windows 虚拟键码映射
├── src/
│   ├── exceptions.py            # ErrorCode / ApiError / TaskTimeoutError
│   ├── constants.py             # 控件 ID / 窗口标题 / 关键词常量
│   ├── api/
│   │   ├── routes.py            # Flask 应用工厂 + 系统路由 + 认证中间件
│   │   ├── query_routes.py      # 查询 Blueprint（持仓/资金/成交/委托）
│   │   ├── order_routes.py      # 下单/撤单/清仓 Blueprint
│   │   ├── action_routes.py     # 手动操作/诊断 Blueprint
│   │   ├── task_queue.py        # 全局任务队列 + 看门狗恢复
│   │   ├── response.py          # 统一响应封装（success/error）
│   │   ├── helpers.py           # 路由层共享工具
│   │   └── idempotency.py       # 下单幂等检查
│   ├── core/
│   │   ├── trader.py            # 下单编排器
│   │   ├── ocr.py               # OCR 服务（双引擎调度 + 质检）
│   │   ├── ocr_lightweight.py   # 轻量 OCR（模板匹配，纯 NumPy/Pillow）
│   │   └── validation.py        # 数据校验纯函数
│   ├── services/
│   │   ├── window_service.py    # 窗口/控件操作基础服务
│   │   ├── window_monitor.py    # 窗口最小化监控线程
│   │   ├── position_service.py  # 持仓/资金/成交查询
│   │   └── trading_service.py   # 撤单服务
│   ├── models/
│   │   └── config.py            # AppConfig（单例 + 热重载）
│   └── utils/
│       ├── singleton.py         # 线程安全单例基类
│       ├── logger.py            # 日志器（文件轮转 + 控制台）
│       ├── screenshot.py        # 截图工具 + 自动清理
│       ├── poll.py              # 轮询等待（poll_until / timed）
│       └── diagnostic.py        # 诊断工具（截图 + UI 文本 + OCR）
├── tests/
│   └── test_core.py             # 核心逻辑单元测试
├── scripts/
│   ├── diagnose_settings.py     # 券商 UI 结构诊断脚本
│   ├── generate_templates.py    # OCR 模板管理（查看/提取/批量标注）
│   └── train_ocr.py             # OCR 迭代训练（自动触发验证码 + 追踪准确率）
├── assets/
│   ├── digit_templates/          # 数字模板（Git 跟踪，离线训练生成）
│   └── captcha_archive/          # 失败验证码存档（gitignore，供离线训练使用）
├── logs/                        # 运行时生成（gitignore）
├── main.py                      # 启动入口（waitress + 单实例 + 优雅关闭）
└── pyproject.toml               # 依赖与构建配置
```

## 技术栈

| 组件 | 用途 |
|------|------|
| **Python 3.11+** / **uv** | 语言 / 包管理 |
| **Flask** + **flask-cors** | HTTP 路由（Blueprint 模块化） |
| **waitress** | 生产级 WSGI 服务器 |
| **pywinauto** (UIA) | 窗口/控件自动化 |
| **pywin32** | Windows API（按键、窗口、互斥锁） |
| **psutil** | 进程枚举与路径匹配 |
| **pyautogui** | 鼠标点击、全屏截图 |
| **ddddocr** (ONNX Runtime) | 可选，仅用于离线 OCR 训练脚本（`uv sync --extra ocr`） |
| **Pillow + NumPy** | 轻量 OCR 模板匹配引擎 |
| **pytest** | 单元测试 |

## 关键设计

### 任务队列与看门狗

所有操作通过单 worker 线程 `TaskQueue` 顺序执行，避免 `xiadan.exe` 并发冲突。每个任务前自动调用 `WindowService.reset_window_state()` 重置窗口到 F1 买入基准态。任务超时后看门狗执行「截图存档 → 激活窗口 → ESC×5 重置」恢复流程，**完成所有恢复后才返回错误**，确保调用方收到 `TASK_TIMEOUT` 时 `xiadan.exe` 已恢复初始状态。

### 事件驱动等待

`src/utils/poll.py` 提供 `poll_until(condition, timeout, interval)` 替代固定 `time.sleep()`。下单后等弹窗、Ctrl+C 后等验证码、撤单后等确认弹窗等场景，每 0.1s 检测 UI 状态，条件满足立即继续，超时抛 `PollTimeoutError`。`timed` 上下文管理器记录每步耗时。

### 按键发送策略

| 方式 | 依赖前台 | 适用场景 |
|------|:---:|------|
| `keybd_event` + `background=True` | ✗ | 已确认窗口在前台时的功能键（跳过冗余激活） |
| `keybd_event` | ✓ | 功能键 F1-F12、Ctrl+C 组合键 |
| `PostMessage` | ✗ | 字母键 Y/N、ENTER（后台不抢焦点） |

功能键默认走前台发送（`PostMessage` 无法触发窗口快捷键），发送前用 `click_input()` + `GetForegroundWindow()` 句柄校验确保窗口在前台。若调用方已自行激活窗口（如 `place_order()` 步骤 1），可传 `background=True` 跳过冗余激活，省去 `click_input()` + `sleep(0.3s)` ×2 的开销（~0.6s）。

### Ctrl+C 双发机制

```text
Ctrl Down → sleep(0.1s) → C Down → C Up → Ctrl Up    (×2, 间隔 0.15s)

第 1 次 → 中文输入法 IME 拦截（取消组合状态）
第 2 次 → IME 已退出，正常送达券商 → 触发验证码弹窗
```

- **延迟 0.1s**：让 `GetAsyncKeyState` 感知 Ctrl 已被按下
- **双发**：绕过中文输入法对首次 Ctrl+C 的拦截
- **不用 SendInput**：券商可能通过 `LLKHF_INJECTED` 标志过滤注入输入
- **不用 PostMessage**：不更新键状态表，券商 `GetAsyncKeyState` 检测不到

### 验证码 OCR — 轻量模板匹配

同花顺 Ctrl+C **必然触发验证码弹窗**（4 位数字，白底蓝字，92×38 像素，规则字体）。弹窗出现 = Ctrl+C 成功送达的确认信号。

**识别流程**：主动定时扫描检测弹窗 → 截图 → 灰度化 → 二值化 → 垂直投影分割 → 模板匹配 → 填入券商软件。外层最多 2 次尝试，内层 OCR 最多 3 次重试。

#### 识别原理（纯 NumPy/Pillow，无深度学习）

```
原始图片 (92×38 RGB)        灰度化              二值化 (阈值 200)
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│ 2 5 8 0         │  →   │ ■ ■ ■ ■         │  →   │ █ █ █ █         │
│ 白底蓝字         │      │ 灰阶图像         │      │ 笔画=黑 背景=白  │
└─────────────────┘      └─────────────────┘      └─────────────────┘
                                                        
                              ↓ 垂直投影                
                         ┌─────────────────┐           
                         │ ██  ██  ██  ██  │  4 个暗列分组 = 4 个数字
                         │ ██  ██  ██  ██  │  间距 >5px = 不同数字
                         │ ██  ██  ██  ██  │  间隙 ≤5px 合并(断裂笔画)
                         └─────────────────┘           
                              ↓ 归一化到 28×38           
                              ↓ 模板匹配 (NCC 归一化互相关)
                         
    分割出的数字 ──→ 与 1,200+ 张模板逐一计算余弦相似度 ──→ 取最高分
                    本质: 向量点积 = cos(夹角)
```

- **灰度化**：`.convert("L")` 消除颜色信息，蓝字变灰阶，只保留亮度
- **二值化**：阈值 200，背景/抗锯齿边缘（>200）丢弃，笔画核心（<200）保留
- **分割**：垂直投影 → 暗列分组 → 合并断裂笔画（如 '5' 的横竖间隙）→ 水平裁空白 → 归一化 28×38
- **匹配**：归一化互相关 (NCC)。将 28×38=1064 个像素视为 1064 维向量，归一化后每个模板长度为 1，NCC = 两向量点积 = cos(夹角)。夹角越小越相似，与亮度/对比度无关。

  **批量矩阵乘法**：模板加载时预归一化并堆叠为 (N, 1064) 矩阵，匹配一步完成：

  ```
  scores = T @ d    # (1208, 1064) × (1064,) → (1208,)  一次 BLAS 调用
  best = argmax(scores)
  ```

  无需 Python 循环，无需逐模板重复归一化。耗时 < 0.01s（日志显示 0.00s）。

  ```
  输入数字 '5' → T @ d →
    模板0: 0.12   模板1: -0.05   ...   模板5₁: 0.91 ✓
                                            ↑ argmax → 识别为 5
  ```

#### 引擎对比

| 引擎 | 内存 | 速度 | 原理 | 角色 |
|------|------|------|------|------|
| 轻量模板匹配 | < 5MB | < 0.01s | NCC + BLAS 批量矩阵乘法 | 生产模式唯一引擎 |
| ddddocr（可选） | ~150MB | 10-50ms | ONNX 深度学习 | 调试模式质检员，生产不加载 |

当前已积累 1,200+ 模板，全部 10 个数字均已覆盖，日常使用无需 ddddocr。

#### 离线训练

运行时不再自动提取模板或比对 ddddocr。模板训练转为离线操作：
1. 失败验证码自动存档到 `assets/captcha_archive/failed_*.png`
2. 运行 `uv sync --extra ocr && uv run python scripts/train_ocr.py` 触发真实交易验证码并积累样本
3. 运行 `uv run python scripts/generate_templates.py batch` 从存档批量提取模板

#### 调试模式

`ddddocr_enabled: true` + `uv sync --extra ocr`：恢复双引擎行为——ddddocr 并行质检、自动存档含标签验证码、实时提取模板、准确率对比。内存约 230-300MB。

`GET /ocr/quality` 返回运行统计（识别次数、失败次数、模板数、覆盖数字、ddddocr 模式状态）。

### 弹窗分类处理

下单/撤单过程中自动检测弹窗类型并分别处理：委托确认弹窗（点 Y/N）、警告弹窗（点 ESC 关闭后继续等待）、纯错误弹窗（提取文本后报错）。弹窗关闭逻辑统一由 `WindowService.dismiss_blocking_popup()` 处理，关键词覆盖中英文（`"失败"` / `"failed"` / `"事务处理机"`）。

**提交失败弹窗的精细分类**：点击买入后若券商返回「提示」弹窗（只有确定键），`_extract_popup_error_text()` 从控件树中提取干净弹窗文本，`_classify_submit_error()` 根据关键词返回精确错误码和针对性建议：

| 弹窗关键词 | error_code | 建议 |
|-----------|-----------|------|
| 清算 | `SERVER_CLEARING` | 等待清算结束后重试 |
| 当前时间不允许委托 | `OUTSIDE_TRADING_HOURS` | 交易时段内操作 |
| T+1 / 当日买入 / 未交收 | `T1_RESTRICTION` | 当日买入的股票需到下一个交易日方可卖出 |
| 可卖数量 / 可用余额不足 | `INSUFFICIENT_SHARES` | 检查持仓可卖数量后调整 |
| 事务处理机转发失败 | `SERVER_UNAVAILABLE` | 确认券商服务器正常 |
| 其他 | `ORDER_SUBMIT_FAILED` | 通用建议 |

`details.popup_text` 返回弹窗原文供调用方自行解析，`details.popup_title` 返回弹窗标题。买入和卖出共享同一套 `place_order()` 流程，仅 F1/F2 切换不同，所有分类逻辑对买卖双方均等生效。

### 幂等与价格校验

- **幂等**：60s 内相同 `code+status+amount+price+price_type` 下单被拒绝（`DUPLICATE_ORDER`）。下单失败清除记录允许重试，超时不清除（防止重复提交）。
- **价格**：API 层拦截超 2 位小数的价格（`VALIDATION_ERROR`），下单层自动 `sanitize_price()` 格式化为 2 位小数。

### 查询面板标准化

所有查询通过 `_prepare_query_panel()`（仅发 F4 切换到查询面板）进入查询面板。TaskQueue worker 在每个任务前已调用 `reset_window_state()`（ESC×5→F1），查询方法内不再重复重置，省 ~1.7s/次。导航动作本身触发券商服务器查询，无需额外 F5 刷新。空数据表格（仅有表头无数据行）正常返回空列表。

## 已知限制与防御

### 菜单栏无法自动化

同花顺菜单栏使用完全自定义渲染，Win32 `GetMenu()` 返回 0，UIA 树中无 PopupMenu 子项。无法通过程序自动配置「系统设置→快速交易」，需用户**手动配置**（见[前置准备](#前置准备券商软件设置)）。

### 买入/卖出子面板关闭

不要用 ALT+F4（关闭整个程序）或 ESC（子面板不是独立对话框，无效）。正确方式：发送 F4 切换到查询视图。`/actions/close-dialog` 接口封装此逻辑。

### 控件树缓存与性能优化

`pywinauto` 的 `descendants()` 遍历 UIA 树耗时约 1s（交易窗口包含数百个控件），原始下单流程中多次独立调用导致累积延迟严重。通过三级缓存策略消除冗余遍历：

**全流程共享**：`place_order()` 在获取窗口后调用一次 `descendants()`，将列表传递给 `input_text_to_element`（代码/价格/数量）和 `click_element`（下单按钮），各自省去内部的 `find_element_in_window` 遍历。

**轮询复用**：`_has_any_dialog()` 检测弹窗时缓存遍历结果，后续弹窗处理循环直接复用，省去第二次遍历。

**单次合并**：`_has_any_dialog()` 原来分别查 cid=1365 和 cid=1040（两次遍历），改为一次遍历同时检查。

| 优化项 | 原始 | 优化后 |
|--------|------|--------|
| 填写股票代码 | 2.09s | 1.22s |
| 填写数量 | 1.76s | 0.87s |
| 点击下单按钮 | 1.05s | 0.57s |
| F1/F2 切换（跳过冗余激活） | 1.10s | 0.16s |
| 等待下单弹窗（合并两次遍历） | 2.20s | 1.10s |
| 弹窗处理循环（缓存复用） | 33.60s | 0.62s |
| 市价切换失败（重试 3→2，超时 5→3s） | ~18s | ~12s |
| 市价切换 poll 轮询（缓存 label 引用） | ~0.5s/次 | ~0ms/次 |
| 检测提交失败弹窗（happy path 跳过） | 1.17s | 0s |
| **总响应（happy path）** | **~51s** | **~13.5s** |
| **总响应（error path）** | **~51s** | **~14s** |

`input_text_to_element` / `click_element` / `find_element_in_window` / `get_all_visible_texts` 均支持可选 `descendants` 参数，缓存未命中时自动降级 fresh scan。

### 查询流程性能优化

查询流程（`_copy_table_via_clipboard` → `_solve_captcha`）独立实施了同类优化：

| 优化项 | 说明 | 节省 |
|--------|------|:--:|
| UIA 缓存复用 | `_solve_captcha` 内一次 `descendants()` 全流程共享（图片/输入框/按钮） | ~1.5s/次 |
| 去重复 reset | TaskQueue worker 已调用 `reset_window_state()`，查询方法不再重复 | ~1.7s/次 |
| 主动定时扫描 | 验证码检测用定时扫描替代 `poll_until` 空等 | ~0.3s/次 |
| 外层重试精简 | 外层 3→2 次（内层 OCR 已有 3 次重试） | 失败路径 ~3s |
| 验证轮询加速 | 2.0s→1.0s，移除超时后冗余重查 | ~1s/次 |
| 诊断按需触发 | `_auto_diagnostic` 仅失败时执行，成功跳过 | ~0.5s/任务 |

| 查询类型 | 优化前 | 优化后 | 降幅 |
|----------|--------|--------|:--:|
| 资金余额 | ~8s | ~4s | -50% |
| 持仓/成交/委托 | ~15s | ~8-10s | -35% |

**弹窗关闭机制**：非委托确认类弹窗统一用 `_close_non_confirm_popup()` 关闭——优先批量查找标准 Windows 按钮（IDOK=1 / IDCANCEL=2，一次遍历同时找两个），降级用 `keybd_event` 直发 ESC（不经过 `send_key`，避免前台窗口校验被模态弹窗阻断）。委托确认弹窗的「否(N)」降级也走同一方法。

**`confirm_order` 安全校验**：发送 Y 键前先通过 `_has_any_dialog()` 验证弹窗存在，避免快速交易模式（无弹窗）Y 键泄漏到其他窗口。

### 市价/限价切换

点击"买入价格"标签（cid=1400）触发券商服务器请求，在限价/市价之间 toggle（双向通用）。输入股票代码后**自动检测实际界面模式**——券商可能记住每只股票上次交易模式并在代码输入后自动切换（如 000001 上次用市价卖出，界面变为「市价卖出」无法填价格）。

策略：`sleep(0.3)` 先检测弹窗（服务器拒绝时弹窗 <0.5s 即出现）→ 有弹窗直接分类报错 → 无弹窗缓存 label 元素引用后 `poll_until` 轮询文本变化（超时 3s，最多 2 次重试，每次轮询只读文本不再遍历 UIA）。两种失败场景分别处理：

| 场景 | 表现 | error_code |
|------|------|-----------|
| 服务器异常（维护） | 弹窗「事务处理机转发数据失败」 | `SERVER_UNAVAILABLE` |
| 模拟账户不支持市价 | 无弹窗，标签静默不变 | `MODE_SWITCH_FAILED`（建议改用限价） |

### 服务器错误弹窗防御

与券商服务器交互时（输入代码查询价格、切换价格模式、点击买入/卖出按钮），若服务器不可用或处于非交易时段，可能弹出「提示」弹窗。弹窗只有「确定」键，无法用 Y/N 键操作，统一用按钮点击（cid=1/2）或 ESC 关闭。

**防御覆盖点**：

| 触发阶段 | 弹窗内容示例 | 处理 |
|----------|------------|------|
| 输入股票代码后 | 事务处理机转发数据失败 / Begin failed! | `_dismiss_server_error_popup()` 关闭 |
| 切换价格模式 | 同上（服务器无响应） | 超时后检测弹窗 → `SERVER_UNAVAILABLE` |
| 点击买入/卖出按钮 | 提交失败：清算中 / 当前时间不允许委托 / … | 提取文本 → 分类报错 |

关键词统一在 `constants.py:SERVER_ERROR_POPUP_KEYWORDS` 中维护。`WindowService.dismiss_blocking_popup()` 默认关键词覆盖中英文（`"失败"` / `"failed"` / `"事务处理机"`），所有调用方（Trader 代码输入后、价格模式切换超时后、F4 查询面板、F3 撤单界面）共享同一套检测逻辑。

### Logger 限制

项目自定义 Logger 只接受单个 message 参数，使用 f-string 传参（不支持 `%s` 占位符）。

## 开发

```bash
uv run pytest                          # 运行全部测试
uv run pytest tests/test_core.py -v    # 运行单元测试
uv run python main.py --dev            # 开发模式（热加载）
uv run python scripts/diagnose_settings.py  # 券商 UI 结构诊断
uv run python scripts/generate_templates.py status  # OCR 模板覆盖状态
uv run python scripts/train_ocr.py     # OCR 迭代训练（自动触发验证码）
```
