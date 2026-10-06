---
name: xiadan-gateway
description: 操作本机同花顺 xiadan.exe 交易网关（xiadan-gateway）：查询 A 股持仓、资金余额、当日委托、当日成交、按合同编号查委托状态与成交回报；限价/市价下单与撤单。凡用户要求查持仓/查资金/查今日委托或成交/查单/买卖 A 股股票/撤单，或提到 xiadan-gateway、同花顺下单程序时使用——前提是网关在本机运行。交易子命令默认关闭，需 XIADAN_MCP_TRADING=1 启用。
---

# xiadan-gateway 交易网关操作

```
你(agent) ──本 skill 的 CLI──HTTP(127.0.0.1)──→ xiadan-gateway ──UI自动化──→ 同花顺 xiadan.exe
```

网关把「操控交易客户端」封装成了 HTTP API：单 worker 串行执行、下单强制幂等键、
验证码自动识别、错误码体系。CLI 是薄适配层（纯标准库），安全机制全部在网关侧。

## 第一步永远是健康检查

会话内首次操作前先跑 `health`，确认 `logged_in: true`：

- `false` → 告知用户登录同花顺下单程序，**不要重试交易类命令**（登录前下单必然失败）
- 返回中的 `config.recommended_client_timeout_seconds` 是官方推荐超时，缺省 60s 够用

## 调用方式

```bash
# 在网关仓库内
uv run python .agents/skills/xiadan-gateway/scripts/xiadan.py <命令>

# 在任意目录（skill 装到用户级目录时，路径换成该目录）
uv run --no-project python <skill目录>/scripts/xiadan.py <命令>
```

- **不要手拼 curl**——Windows 下 PowerShell 的 `curl` 是 Invoke-WebRequest 别名、
  引号转义各 shell 不一，一律走本 CLI
- stdout = 结果 JSON（已解包 `data` 字段）；stderr = 幂等键提示 / 错误信息
- 错误格式 `[ERROR_CODE] message | 建议: ... | request_id: ...`（网关错误时退出码 1）
- 退出码：`0` 成功 / `1` 网关返回错误 / `2` 用法或参数校验错误 / `3` 无法连接网关
  （先启动：在网关仓库跑 `uv run python main.py`）

## 子命令速查

| 命令 | 说明 | 参考耗时 |
|------|------|--------|
| `health` | 登录态/队列/近 1 小时错误统计（会话内首个命令） | <1s |
| `queue` | 任务队列忙闲 | <1s |
| `balance` | 资金余额（可用/冻结/市值） | 2~4s |
| `positions` | 当前持仓明细 | 3~8s |
| `trades` | 今日成交明细 | 4~5s |
| `orders` | 当日全部委托（含已成交/已撤） | 3~5s |
| `order-status <合同编号>` | 单笔委托状态 + 成交回报（加权均价/逐笔） | 8~15s |

交易命令（默认关闭，`XIADAN_MCP_TRADING=1` 后可用）：

| 命令 | 说明 |
|------|------|
| `buy --code 601991 --amount 100 --price 10.50` | 限价买入（`--price-type market` 为市价，常被拒） |
| `sell --code 601991 --amount 100 --price 9.80` | 限价卖出 |
| `cancel [all\|buy\|sell\|last]` | 撤全部/撤买/撤卖/撤最近一笔（默认 all） |

下单前 CLI 会把本次使用的幂等键打印到 stderr——重试语义见下方准则 5。

## 操作准则（必须遵守）

1. **登录门**：`health` 的 `logged_in=false` 时只报告用户，不重试交易命令。
2. **以返回为准**：查询结果基于返回 JSON 解读，不要凭记忆编造数字。
3. **交易强制工作流**（顺序不可省略）：先 `positions`/`balance` 核实可交易份额或
   资金 → 向用户**逐字复述**「代码 / 买入或卖出 / 数量 / 价格 / 限价或市价」并取得
   明确同意 → 执行 → 用 `orders` 或 `order-status` 核对委托号与状态并向用户汇报。
   这是实盘真实委托，提交后不可撤销、只能撤单。
4. **显式价格**：限价单必须用用户给出的确切价格，禁止自行估算「最新价/现价」
   （CLI 会直接拒绝无价格限价单）。市价单实测常被该券商拒绝
   （`ORDER_PRICE_REQUIRED`），优先限价。
5. **结果未知时先查后试**：超时 / `TASK_TIMEOUT` / 连接中断后**不要直接重试下单**
   ——先 `orders`（按代码+价格+数量+时间反查）或 `order-status` 核实是否已提交；
   确要重试必须复用 stderr 打印的**同一 Idempotency-Key**（同键重试会被网关拦截，
   换新键=新订单，会造成重复下单）。
6. **DUPLICATE_ORDER 不是故障**：60 秒内同键重复提交被拦截——先查当日委托确认
   是否已提交，再决定下一步。
7. **串行耐心**：网关单 worker 串行执行，任何命令 2~10 秒属正常，勿因慢而并发重试。
8. **不越界**：CLI 不暴露也不支持 `/actions/*`（裸 UI 操作）与 `/admin/*`——
   不要绕过 CLI 直接 HTTP 调用它们。

## 错误码速查（影响行为的子集）

| 错误码 | 应对 |
|--------|------|
| `TASK_TIMEOUT` / `TASK_TIMEOUT_RECOVERY_FAILED` | 结果未知——查单核实，禁止直接重试 |
| `DUPLICATE_ORDER` | 同键重复提交被拦截——查单确认是否已提交 |
| `PRICE_OUT_OF_RANGE` | 价格超涨跌停——向用户复核价格 |
| `INSUFFICIENT_BALANCE` / `INSUFFICIENT_SHARES` | 资金/份额不足——报告用户 |
| `T1_RESTRICTION` | 当日买入次日才可卖——报告用户 |
| `ORDER_PRICE_REQUIRED` | 券商要求显式价格——改限价单 |
| `OUTSIDE_TRADING_HOURS` / `SERVER_CLEARING` | 非交易时段/券商清算——稍后再试或报告用户 |

完整参数、响应字段（`entrust_no: null` 语义、`entrust_no_verified` 等）与全量错误码表
见 [references/api.md](references/api.md)。

## 交易开关与安全边界

- `XIADAN_MCP_TRADING=1`（环境变量）启用 `buy`/`sell`/`cancel`，与仓库的 MCP 适配器
  共用同一开关；缺省只读是刻意设计——未获用户明确许可不要替用户开启
- 仅在支持「逐次命令人工确认」的 agent 环境中开启交易命令，且准则 3 的复述确认
  不能省——防的正是 LLM 自行决定交易参数这一失败模式
- 所用客户端若支持 MCP，可改注册仓库自带的 MCP 适配器（`scripts/mcp_server.py`，
  同一网关、同一开关、同一套工作流），工具调用比 CLI 更结构化
