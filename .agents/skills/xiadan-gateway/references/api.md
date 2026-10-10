# xiadan-gateway API 参考（CLI 子命令背后的 HTTP 契约）

CLI 子命令与 HTTP 端点一一对应；本文供需要理解响应字段全貌/排错时按需阅读。
网关所有响应统一 HTTP 200，JSON `status` 字段区分成功（`success` + `data`）与
失败（`error` + `error_code`/`message`/`suggestion`/`request_id`）。CLI 已自动
解包 `data`、格式化错误。

## 端点总表

| CLI 命令 | HTTP | 路径 | 参考耗时 |
|----------|------|------|--------|
| `health` | GET | `/health` | <1s |
| `queue` | GET | `/queue/status` | <1s |
| `balance` | GET | `/account/balance` | 2~4s |
| `positions` | GET | `/positions` | 3~8s |
| `trades` | GET | `/trades/today` | 4~5s |
| `orders` | GET | `/orders/pending` | 3~5s |
| `order-status <NO>` | GET | `/orders/{entrust_no}/status` | 8~15s |
| `buy` / `sell` | POST | `/orders`（头 `Idempotency-Key` 必填） | 2~10s |
| `cancel [type]` | POST | `/orders/cancel-all` | 2~5s |

> 网关另有 `/actions/*`（裸 UI 操作）、`/admin/*`（管理）、`/diagnostic/*`（诊断）、
> `/ocr/quality`——skill 刻意不暴露，需要时由用户手动操作。
>
> 查询类端点自带数据新鲜度保障（2026-10-10 断网实测落地）：执行前客户端报告
> 「断开」则先 F5 戳重连再查询——F5 全局防抖 30s（防抖窗内的调用不重复戳、
> 也不等待），戳后轮询等「断开」清除最多 3s（实测重连 ~0.9s），超时照常查询、
> 由后置门控兜底；结束后复查仍断开则结果作废抛 `BROKER_DISCONNECTED`。
> 同页连续查询自第二次起先 F5 刷新再复制（复合查询如 `order-status` 耗时 +~1s）。

## health 响应字段

- `xiadan_running` / `logged_in`：交易客户端进程在跑 / 主窗口存在（≈已登录）。
  登录前或 RDP 会话断开时窗口不存在，`logged_in=false`
- `session`：RDP 会话连接状态（`connect_state` 码 + `state_name` +
  `ui_available` + `desktop_wedged`）。`ui_available=false` = 会话断开，
  任务会被健康门快速拒绝（`SESSION_UNAVAILABLE`）；`true` = 正常；`null` =
  查询失败（未知，非确定不可用）。与 `logged_in=false` 配合消歧：
  `session=false` 是会话问题（等自愈），`session=true 且 logged_in=false`
  是券商登录问题。`desktop_wedged=true`（`desktop_wedge_streak`=连续指纹
  失败计数）= **挂接但僵死**：`ui_available` 假绿（会话在，桌面输入/图形
  路径死），任务会以 `SESSION_DESKTOP_UNAVAILABLE` 失败——网关自动
  tsdiscon 升级自愈（独立冷却 600s），稍后重试即可
- `broker`：券商主站链路连接态。`connected` 三态：`true`=未观测到断开（真实
  断网后有 **~25-45s 盲区**——`true` ≠ 网络正常）；`false`=客户端状态栏报告
  「断开」（网络恢复后不自动清零，查询端点会前置 F5 自动戳重连）；`null`=
  读取失败（fail-open 不拦截业务）。另有 `status_text`（状态栏诊断文本）与
  `latency_ms`。业务端点对该信号 fail-closed：断开时查询结果作废抛
  `BROKER_DISCONNECTED`，下单/撤单转 `ORDER_STATE_UNKNOWN`
- `queue_status`：队列忙闲
- `stats`：近 1 小时各错误码成功率、连续失败计数、下单弹窗统计
- `config.recommended_client_timeout_seconds`：官方推荐客户端超时

## orders / order-status 响应字段

当日委托表（`orders`）**无独立状态列**——委托状态通过「备注」（如「全部撤单」）
与「撤消数量/成交数量」体现。

`POST /orders` 响应 `data`：

| 字段 | 说明 |
|------|------|
| `action`/`mode`/`code`/`amount`/`price` | 回显下单参数 |
| `confirmed` | `true`=已提交——判定依据：提交后右下角出现**黄色成功横幅**（客户端对提交成功的唯一主动视觉确认；不要求读出委托号——横幅可见但读数失败时 `entrust_no` 为 null 并自动回补）。横幅未出现（含客户端内联校验静默拒绝——不弹窗、按钮不置灰）→ `false`，大概率未提交，查当日委托/资金冻结核实后再决定重试。仅截获关闭时退化为旧语义（无错误弹窗即视为成功） |
| `entrust_no` | 合同编号。仅启用截获时返回；截获失败或未启用为 `null`。**`null` 不代表下单失败**——看 `confirmed`：`true`+`null`=横幅已出现但读数失败（已提交、编号未知），**不要重试**（会重复下单），需要编号时用 `orders` 按代码+价格+数量+时间反查；`false`+`null`=横幅未出现，大概率未提交 |
| `entrust_no_recovered` | 截获失败后按点击时刻窗口×参数匹配反查当日委托（含多轮 F5 重查），唯一命中才为 `true`；`confirmed: false` 时不回补 |
| `entrust_no_verified` | 委托号对账结果（开启时返回）：`true`=当日委托落表命中 / `false`=按 1/2/4s 指数退避 F5 多轮重拷后仍未命中（以查询为准）/ `null`=对账查询失败（不影响下单结果语义） |

`GET /orders/{entrust_no}/status` 响应 `data`：

- `found: false`——当日委托中无此合同编号（非当日下单，或编号有误）
- `order.status`——由数量推导、跨券商稳定：`全部成交` / `部分成交` /
  `部分成交后撤单` / `全部撤单` / `未成交` / `未知`（数量缺失）
- `order.is_final`——`filled + cancelled >= amount`，该笔委托已不再留在市场
- `fills`——按合同编号聚合的成交：`count`/`total_qty`/`total_amount`/
  `avg_price`（加权均价，未成交为 `null`）/`trades[]`（时间/成交编号/数量/价格/金额）

## cancel 响应字段

- `cancel_type`：操作名（全部撤单/撤买/撤卖/撤最后）
- `success`：是否执行了撤单（按钮灰显=当前无可撤委托时为 `false`，不算错误）
- `cancelled_count`：撤单数量（从确认弹窗解析；无弹窗/解析失败为 `null`，灰显路径为 0）
- `confirm_dialog_shown`：是否出现撤单确认弹窗（快速交易模式无弹窗为 `false`）
- `reason`：仅按钮灰显时返回，如"当前无可撤委托"

## 幂等键契约（POST /orders）

- 请求头 `Idempotency-Key`（1–128 字符）**必填**，CLI 缺省自动生成 uuid4 并打印到
  stderr；`--idem-key` 显式指定
- key 生命周期 = 每个逻辑订单一个 key：**超时重试必须复用同一 key**（窗口内同 key
  拒绝 = 重试保护，`DUPLICATE_ORDER`）；确要新单（含同参数多单）用新 key
- 去重窗口默认 60s（`order_dedup_window_seconds`）

## 认证

- 网关启用认证时接受 `Authorization: Bearer <token>` 或 `X-API-Key: <token>`；
  CLI 自动携带（环境变量优先，缺省回落 `config/app_config.json` 的 `auth.token`）
- 任何请求**不带 Origin 头**（网关跨站防御会拒绝带 Origin 的请求）——CLI 已处理

## 环境变量（与 MCP 适配器通用，均可缺省）

| 变量 | 缺省值 | 含义 |
|------|--------|------|
| `XIADAN_MCP_URL` | 读配置文件，否则 `http://127.0.0.1:5000` | 网关基地址 |
| `XIADAN_MCP_TOKEN` | 配置文件 `auth.token`（启用认证时） | 认证 token |
| `XIADAN_MCP_CONFIG` | `config/app_config.json` | 网关配置文件路径 |
| `XIADAN_MCP_TRADING` | `0` | `1`/`true` 启用 buy/sell/cancel |
| `XIADAN_MCP_TIMEOUT_SECONDS` | `60` | HTTP 超时（建议 ≥40） |

## 全量错误码

`VALIDATION_ERROR` 参数校验失败；`DUPLICATE_ORDER` 重试拦截窗口内同 key 重复提交；
`AUTH_REQUIRED`/`AUTH_FAILED` 认证缺失/无效；`WINDOW_NOT_FOUND` 交易窗口未找到；
`CONTROL_NOT_FOUND` 控件未找到；`MODE_SWITCH_FAILED` 限价/市价切换失败；
`ORDER_SUBMIT_FAILED` 提交失败（通用，含弹窗原文）；`SERVER_CLEARING` 券商清算中；
`OUTSIDE_TRADING_HOURS` 非交易时段；`T1_RESTRICTION` T+1 限制；
`STOCK_NOT_FOUND` 证券代码不存在（弹窗高特异性短语，区别于 T1 的「可卖数量」
文案）——报告用户核实代码，勿按 T1 处理；
`INSUFFICIENT_SHARES`/`INSUFFICIENT_BALANCE` 份额/资金不足；
`SHORT_SELLING_FORBIDDEN` 不允许卖空；`PRICE_OUT_OF_RANGE` 价格超涨跌停；
`ORDER_PRICE_REQUIRED` 券商要求显式价格（改限价）；`SERVER_UNAVAILABLE` 券商
服务器不可用；`BROKER_DISCONNECTED` 客户端与券商主站链路断开——查询结果为
缓存旧值已**作废**，直接重试即可（每次调用前置 F5 自动触发重连，实测网络恢复
后 ~0.9s 自愈；真实断网后有 ~25-45s 心跳盲区）；`OCR_FAILED` 验证码识别失败；`INPUT_VERIFY_FAILED` 证券名称联动
校验失败；`INTERNAL_ERROR` 未知异常；`QUEUE_TIMEOUT` 排队超时（任务稍后仍可能
被执行——同幂等键重试安全）；`QUEUE_FULL` 队列已满；`SESSION_UNAVAILABLE`
RDP 会话断开，任务**确定未执行**即被毫秒级拒绝（与 `TASK_TIMEOUT` 相反：
幂等记录已自动清除，轮询 health `session.ui_available=true` 后同幂等键重试
即安全，无需查单）；`SESSION_DESKTOP_UNAVAILABLE` 会话挂接但桌面不可操作
（激活失败命中僵死指纹，任务确定未执行，幂等记录已清除——通常 RDP 重连
一次即复位，无人值守时网关 tsdiscon 升级自愈自动处理）；`TASK_TIMEOUT` 任务超时
恢复成功（结果未知，查单核实）；`TASK_TIMEOUT_RECOVERY_FAILED` 超时且恢复失败
（结果未知，查单核实）；`ORDER_STATE_UNKNOWN` 下单点击提交后发生非业务异常
（如 RDP 断开瞬间），或下单/撤单序列完成后券商链路门控触发——订单**可能已
提交**（结果未知，查单核实；幂等记录保留，同 key 重试被拦截，确认未提交后
用新 key）。
