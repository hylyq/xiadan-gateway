# xiadan-gateway

同花顺 `xiadan.exe` 交易网关 — 通过 HTTP API 控制同花顺下单程序进行股票交易。

> 🌐 **English version**: [README.md](README.md)

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

## 目录

- [核心特性](#核心特性)
- [快速开始](#快速开始)（含[前置准备：券商软件设置](#前置准备券商软件设置) / [服务器无人值守运行（推荐 VNC，RDP 亦可）](#服务器无人值守运行推荐-vncrdp-亦可)）
- [上线前必改（安全检查）](#上线前必改安全检查)
- [配置](#配置)
- [API 接口](#api-接口)：[响应格式](#响应格式) / [错误码](#错误码) / [接口总表](#接口总表) / [下单](#post-orders--下单) / [委托状态与成交回报](#get-ordersentrust_nostatus--委托状态与成交回报) / [撤单](#post-orderscancel-all--撤单) / [辅助接口](#辅助接口) / [调用方 timeout 配置](#调用方-timeout-配置)
- [MCP 服务（agent 接入）](#mcp-服务agent-接入)
- [Skill 接入（任意 agent）](#skill-接入任意-agent)
- [原理](#原理)
- [关键设计](#关键设计)：[任务队列与看门狗](#任务队列与看门狗) / [幂等与价格校验](#幂等与价格校验) / [弹窗分类处理](#弹窗分类处理) / [市价/限价切换](#市价限价切换) / [查询面板标准化](#查询面板标准化) / [验证码 OCR](#验证码-ocr--轻量模板匹配) / [性能实测汇总](#性能实测汇总)
- [已知限制](#已知限制)
- [项目结构](#项目结构)
- [技术栈](#技术栈)
- [开发](#开发)（含[添加新的干净退出场景](#添加新的干净退出场景)）

## 核心特性

| 特性 | 说明 |
|------|------|
| 单实例运行 | Windows 全局互斥锁保证同一时刻只有一个实例 |
| 顺序执行 | 单 worker 线程任务队列，避免 `xiadan.exe` 并发冲突 |
| 连续干净跳过 | 上笔干净退出→跳过窗口重置+激活，**但窗口位置自愈不豁免**（出屏即移回，GetWindowRect 级开销）。同组同向全跳过，交叉方向只按 F1/F2；分组 `trade`(买/卖)/`cancel`(撤单)/`query`(查询)（见[任务队列与看门狗](#任务队列与看门狗)） |
| 查询复用遍历 | 持仓/成交/委托查询：一次 `descendants` 遍历同时用于 Tree 查找+弹窗检测+兜底扫描，减少 ~40% 导航耗时 |
| 消息级复制查询（实验） | `query.copy_method=message` 时表格复制走 `WM_COMMAND(0xE122)` 消息，免前台激活/免真实键盘——不受输入法拦截与 `GetAsyncKeyState` 延迟影响，窗口失焦/被遮挡时仍可复制。**验证码与发起方式无关，多数复制仍会触发**；收益来自复制阶段本身（数据见[性能实测汇总](#性能实测汇总)）。失败自动回退键盘法，默认仍为 keyboard |
| 模式切换流水线 | 限价↔市价切换时先点按钮不等待，立即填数量——数量填充的 ~0.7s 与标签变化重叠，验证在填数量之后自然就绪 |
| 弹窗分类处理 | 委托确认→点Y/N；警告→点Y继续；**价格超限→点N取消+返回`PRICE_OUT_OF_RANGE`**；错误→关闭+报错（见[弹窗分类处理](#弹窗分类处理)） |
| 看门狗恢复 | 任务超时自动截图 + 激活 + ESC×3，重置后返回错误 |
| 幂等检查 | **必填** `Idempotency-Key` 请求头（1–128 字符）——每个逻辑订单一个 key：窗口内同 key 拒绝（`DUPLICATE_ORDER`，HTTP 超时重试保护）；新 key = 新订单，含同参数多单 |
| OCR 验证码 | 轻量模板匹配引擎（噪声过滤保留窄而高的笔画——'1' 天生仅 3px 宽）；求解循环每轮重拍、检测客户端拒绝、存档自信误读（`wrong_*.png`）、耗尽安全关闭弹窗；失败自动存档，可选 ddddocr 离线训练 |
| 生产级服务器 | `waitress` WSGI + 优雅关闭（SIGINT/SIGTERM） |
| 配置热更新 | `POST /admin/reload-config` 无需重启 |
| 启动配置校验 | 启动时校验配置类型/值域（port/超时/路径），非法配置直接中止并打印修复指引 |
| 截图自动清理 | 启动时清理过期截图（保留 200 张 / 7 天内） |
| 认证安全 | Token 使用 `hmac.compare_digest` 常量时间比较 |
| 跨站防御 | 拒绝携带 `Origin` 头的请求（浏览器跨站请求必带，脚本客户端不带）——防恶意网页向本机网关发起交易，未开认证时同样生效 |
| 运行统计 | 按错误码聚合成功率（最近 1 小时窗口，`/health` 返回），连续 3 次失败日志告警；跟踪下单弹窗行为，客户端「快速交易」设置被重置（弹窗行为翻转）时告警 |
| 告警外推 | 连续任务失败≥3、下单弹窗行为漂移、任务超时 → POST webhook（generic JSON 或企业微信/钉钉/飞书 `text` 格式），后台线程发送不阻塞交易路径 |
| 证券名称联动校验 | 输入代码后轮询名称联动控件（cid=1036 Static）非空=代码被客户端完整解析；未联动自动清空重输一次，仍失败报 `INPUT_VERIFY_FAILED` 阻止提交；名称控件缺失时降级放行。可用 `order.verify_code_input` 关闭 |
| 委托号横幅截获 | 提交点击后后台线程抓取右下角黄色横幅（~12fps 抓屏；自绘覆盖层，`PrintWindow` 不可见）+黄色掩码定位+模板 OCR 读出合同编号，解析以尾部全角句号为锚、与字长无关（券商编号长度不一）。成功返回 `entrust_no`，失败返回 `null` 不影响下单，可配 `order.recover_entrust_no` 自动回补（见[下单响应](#post-orders--下单)）。真假数字按**字形高度类**判别（数字恒矮于文案满高——console 9/10、RDP 12/15），横幅字号随桌面重渲染变化也能稳定读取——已验证 RDP↔console 重挂循环（2026-10-09 事故：`tscon` 重挂 console 后字号变小，汉字混入数字字宽带，纯 IoU 阈值方案把 `8119` 假前缀黏上真号；实弹证伪后被高度门控取代）。现场诊断工具：`scripts/probe_banner_pixels.py`（随单会话内采帧）+ `scripts/replay_banner_frames.py`（对存帧离线重放截获管线）。修复后实弹验收（2026-10-10）：console 挂接会话（VNC）截获一次命中且 `entrust_no_verified: true`；RDP 断开态下高度门控拒绝读取——下单自动走 `order.recover_entrust_no` 回补，仍返回经对账的编号（`entrust_no_recovered: true`） |
| 委托状态与成交回报 | `GET /orders/{entrust_no}/status` 按合同编号 join 当日委托 × 当日成交：委托状态由数量推导（不依赖券商备注文本），成交聚合含加权均价与逐笔明细——下单响应的轮询侧对应物 |
| 窗口位置自愈 | 任务开始前检查窗口与工作区交集（阈值 60%），窗口被误拖出屏幕时自动移回（`click_input`/截图按屏幕坐标工作，出屏会失效） |
| 窗口可见性自愈 | 后台监控（每 2s）同时恢复最小化与**托盘隐藏**窗口（`IsIconic` **或** `IsWindowVisible`——托盘隐藏态非 iconic，曾是盲区）；软恢复连续 3 轮无效时，按**运行中进程**的 exe 重拉兜底（hwnd→PID→psutil 取路径，配置 `trading_app_paths` 兜底，60s 冷却）——单实例客户端会唤起既有窗口，多套安装并存也不会拉错程序。查询路径激活前同样对隐藏窗口 `SW_SHOW` |
| 会话断开自愈 | 后台每 10s 检查宿主会话状态（`win32ts` `WTSConnectState`），发现处于**已断开**态（RDP 客户端直接断开、未用 tscon 退出）时，自动对自己的会话执行 `tscon <id> /dest:console`——以会话属主身份重挂 console **并同步解除锁定**。仅在"已断开"时动作，绝不劫持正在交互使用的 RDP 会话。**防重连竞态四层防护**（`session_monitor` 配置热生效）：`enabled` 总开关（手动重连前可热关闭）+ 连续断开满 `debounce_seconds`（默认 30s）才动手 + `cooldown_seconds` 冷却（默认 300s=重连保护窗）+ **RDP 端口 TCP 判别**（断开态下 3389 有 ESTABLISHED=用户正停在密码界面/协商中，自愈让路；断开超 600s 让路上限自动失效，防 mstsc 挂密码框永久阻塞）——判别器经两轮真机实验验证（会话级 `WTSClientName` 在附加完成前恒为空不可用，TCP 层是唯一先行信号），竞态在判别器有效期内归零。实测：**VNC 服务端关闭 + RDP 直接断开，自动恢复，查询全绿** |
| 运行时弹窗自愈 | 每个任务开始（窗口复位/激活**之前**）先清扫残留弹窗（顶层 `#32770` 与主窗口的子 `#32770` 两种形态），**只关闭、绝不求解、绝不点「确认」**：残留验证码多为过期（实测正确识别正确输入仍被拒），求解徒增向券商提交错误验证码的风险；未知报错弹窗点确认更可能有副作用。关闭仅用安全手段——「取消」按钮或 WM_CLOSE（等同点 X，对话框默认走取消路径）。关窗前自动存档证据：桌面全域截图 + 交易窗口截图 + 弹窗控件文本入日志（弹窗一关即失，供事后排查；受截图清理策略约束）。新鲜验证码由复制流程当场触发当场求解。清扫先于复位是为了避免激活逻辑的真实鼠标点击落在弹窗按钮上（实测曾连按弹窗"确定"） |
| MCP 适配器 | `scripts/mcp_server.py` 将网关暴露为标准 MCP 工具供大模型 agent 调用——只读查询恒注册；`place_order`/`cancel_orders` 需 `XIADAN_MCP_TRADING=1`；`/actions/*` 裸操作永不暴露（见 [MCP 服务](#mcp-服务agent-接入)） |

## 快速开始

**环境**：Windows / Python 3.11+ / [uv](https://github.com/astral-sh/uv) / 已安装同花顺 `xiadan.exe`

```bash
uv sync                           # 安装依赖
uv run python main.py             # 启动服务（默认 http://localhost:5000）
uv run python main.py --dev       # 开发模式（热加载）
```

> **运行库前置提示**：pywin32 的 `win32ui` 依赖微软 MFC 运行库（`mfc140u.dll`）。纯净的 Windows / Windows Server 镜像通常没有——`uv sync` 能装完依赖，但启动时报 `ImportError: DLL load failed while importing win32ui`。解决办法：安装 [Visual C++ 2015-2022 Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe)（需交互式安装；无头服务器请在 RDP/VNC 桌面会话里运行，服务会话中会卡住），装完确认 `C:\Windows\System32\mfc140u.dll` 存在。

### 前置准备：券商软件设置

启动前必须手动配置以下设置，跳过确认弹窗以提升交易速度。

**打开方式**：独立下单窗口顶部菜单「设置」→ 选项卡「快速交易」，将以下 4 项全部设为「否」：

| 设置项 | 必须值 | 原因 |
|--------|:---:|------|
| 撤单前是否需要确认 | **否** | 跳过撤单确认弹窗 |
| 买入时是否需要确认 | **否** | 跳过买入委托确认弹窗 |
| 卖出时是否需要确认 | **否** | 跳过卖出委托确认弹窗 |
| 委托成功后是否弹出提示对话框 | **否** | 减少交易成功后的提示弹窗干扰 |

> 只需配置一次。关闭确认后（快速交易模式），委托直接提交不再弹窗，下单耗时减少 ~1.4s。

### 服务器无人值守运行（推荐 VNC，RDP 亦可）

本网关通过**真实鼠标/键盘输入**驱动 `xiadan.exe`（`SetForegroundWindow` + `click_input` + `keybd_event`），要求所在会话拥有**活动桌面**（console 挂接的交互会话）。**推荐使用 VNC 访问**——会话常驻 console，没有「断开+锁定」循环、没有自愈等待窗，横幅委托号截获直接可用（2026-10-10 服务器重启后纯 VNC 实测：查询/带截获下单/撤单全绿，横幅一次命中）。RDP 也可用——断开后会话自愈自动恢复——但 RDP 会话处于断开态期间横幅截获无法可靠读取，下单自动走回补路径（编号仍经对账，响应慢 ~8s）：

| 接入方式 | 客户端断开后 | 自动化 |
|---|---|---|
| **VNC（推荐）**——会话常驻 console | VNC 只是桌面的镜像，会话保持挂接在 console 上 | ✅ 持续可用，随时连/断；**横幅截获直接命中**（2026-10-10 实测：一次截获成功，`entrust_no_verified: true`，下单响应 ~13s） |
| RDP + 离开时执行 `tscon $env:SESSIONNAME /dest:console`（PowerShell）/ `tscon %sessionname% /dest:console`（cmd.exe） | 会话无缝落回 console，不锁屏 | ✅ 持续可用（零停机路径，对停顿敏感时用）；会话在 console 上→截获直接命中 |
| RDP 直接断开 | 会话「断开+锁定」→ **会话自愈自动 tscon 恢复（含解锁）** | ⚠️ 短暂失效（~10-30s）后自动恢复；失效窗口内任务被**会话健康门毫秒级快速拒绝**（`SESSION_UNAVAILABLE`，任务未执行），不再等 30s 看门狗超时；**断开态期间横幅截获拒绝读取→下单自动走回补路径**（`entrust_no_recovered: true`，响应 ~19s，编号仍经对账——2026-10-10 实测）✅ 实测：VNC 服务端关闭 + 直接断开，资金/持仓/成交查询全部恢复 |

**日常流程（推荐，VNC）**：

1. VNC 连入 →（首次/重启后）启动 `xiadan.exe` 并登录券商 → 在终端启动网关：`uv run python main.py`
2. 保持 VNC 会话在位（或直接关掉查看器——会话反正常驻 console）：查询、下单、横幅截获照常工作
3. RDP 替代方案：RDP 连入，用完**直接关闭 RDP 客户端**——~10-30s 后网关自动恢复，无需执行任何命令（实测验证：全程 VNC 服务端关闭）；自愈完成前下单走截获→回补兜底（~19s，编号仍经对账）

**规则**：

- **RDP 存活规则**——RDP 重连会把会话从 console 拉回 RDP 通道，普通方式断开会**断开并锁定**会话（Windows 安全设计；此时注入的点击落空、前台校验拒绝发键）。**网关自带会话断开自愈**：后台每 10s 检查（`win32ts` `WTSConnectState`），发现断开态**连续满 `debounce_seconds`（默认 30s）**即自动执行 `tscon <id> /dest:console`——以会话属主身份重挂 console 并同步解除锁定，仅在「已断开」时动作，绝不劫持正在交互使用的会话；两次自愈间隔 `cooldown_seconds`（默认 300s）。需要立即恢复也可手动执行 `tscon <id> /dest:console`（会话 id 用 `qwinsta` 查）

  **⚠️ 重连竞态与四层防护**：会话在「客户端走了」与「用户正在重新连接」（输凭据期间、console→RDP 换轨过渡）两种场景下**同为断开态**，仅凭会话状态无法区分——若自愈恰在重连过程中动作，tscon 会与 RDP 附加撞车，可能把会话图形栈撞进僵死（实测 2026-10-06：蓝屏「请稍后」→ 黑屏 → 只能注销会话重建）。防护层次：
  - **TCP 判别器（主防线，实验验证）**：客户端连上服务器即与 3389 建立 ESTABLISHED，输凭据全程保持；客户端真离开时连接关闭。断开态下检测到该连接=用户正在重连，自愈让路（仅作用于断开事件前 600s 内——防 mstsc 停在密码框无限期阻塞自愈；检测异常按无连接处理，退回下层防护）
  - 防抖（30s）+ 冷却（300s）：把「快速重连」与「分钟级短离开」移出危险区，冷却期=保护窗
  - **总开关**（最稳姿势，任选）：重连前热关闭自愈（`session_monitor.enabled: false` → `POST /admin/reload-config`），连上确认桌面正常后再开回——显式编排，竞态在构造上消失
  - 无配置时的等价舞步：关闭 RDP 客户端 → 等自愈执行（日志出现 `会话持续断开`，断开后 ~30-40s）→ 在保护窗内（自愈执行后 300s）重连
- **会话健康门（断开期快速拒绝）**——每个任务执行前直查一次 `WTSConnectState`（与自愈同一信号、亚毫秒本地查询），断开态任务**毫秒级返回 `SESSION_UNAVAILABLE`**（message 注明「任务未执行」），而非放行后在死桌面上挂 30s 看门狗超时。恢复时长按区间告知（通常 ~40s = 防抖 30s + 检查间隔 10s；冷却期内再次断开最长 ~340s）。要点：
  - **任务未执行 = 可安全重试**：下单的幂等记录在拒绝时自动清除，恢复后复用同一 `Idempotency-Key` 重试即正常执行（与 `TASK_TIMEOUT` 的「可能已执行、必须先查单」相反）
  - **监控信号**：`GET /health` 的 `session.ui_available`（断开期间 `false`，恢复即 `true`；`null` = 查询失败非确定不可用），轮询到 `true` 即可重试
  - **残余**（接受）：任务**执行中途**断开仍由看门狗兜底（30s 超时+恢复）——健康门只在任务开始前判定，无法安全抢占进行到一半的点击序列；RDP **重连**不打断进行中的任务（进程/窗口/句柄在 console→RDP 换轨中保留，仅有秒级分辨率切换的布局抖动风险）
  - **fail-open**：状态查询异常一律放行（最坏 = 无门时代）；只拦「已断开」态，重连过渡瞬态放行。逃生口 `task_queue.session_gate_enabled: false`（热重载生效）——与自愈开关 `session_monitor.enabled` 独立（失效域不同：一个管队列拒绝，一个管 tscon 动作）
  - 边角：下单成功后的委托号截获/对账等链式查询若恰在断开期被拒，各自降级记 warning，下单响应本身不受影响——恢复后用 `GET /orders/pending` 反查委托号
- **桌面僵死检测与升级自愈（挂接但僵死态）**——比直接断开更隐蔽的故障：会话保持**挂接**（WTS 报 Active，`session.ui_available` 恒 `true`）但服务端输入/图形路径全死——鼠标移动失败（`SetCursorPos` error 0，pywinauto 呈现为 "There is no active desktop"）、截屏失败、前台句柄恒 `0x0`。2026-10-09 实测：`tscon` 自愈 2 分钟后撞上 RDP 重连，僵死约 20 分钟直至真实点击复位（与 2026-10-06 图形栈僵死事故同族）。严重度有分层——同晚 23:44 RDP 重连后出现的轻量形态（前台 `0x0`、注入被拒、streak=2）**约 80 秒后未经干预自行清零**——先探针（`/actions/send-key` 发 ESC 是最廉价的活性探针）观察 ~2 分钟再升级处置。现由三层覆盖：
  - **错误码细分**：命中僵死指纹（前台 `0x0` 或 "no active desktop" 文本）的激活失败返回 `SESSION_DESKTOP_UNAVAILABLE`，不再误报 `WINDOW_NOT_FOUND`「当前前台窗口不是交易窗口」（窗口句柄找得到，死的是桌面）
  - **`/health` 消歧**：`session.desktop_wedged`（15 分钟内连续 ≥2 次指纹失败即 `true`；`desktop_wedge_streak` 为原始计数）——`ui_available: true` 且 `desktop_wedged: true` = 假绿灯
  - **升级自愈**：标记僵死且会话挂接时（`session_monitor.wedge_heal_enabled: true`；独立冷却 `wedge_cooldown_seconds` 默认 600s）执行 `tsdiscon <id>` 强制走一次 RDP 断开-重连周期（实测有效的复位手段）。有客户端时 mstsc 秒级自动重连；无人值守时由既有 tscon 链路按防抖+TCP 判别器接管 console（正是上文竞态毒源的两层防护）。任一任务成功即清除标记——成功本身就是最便宜的探针
- **券商连接态检测（`/health` 的 `broker` 字段）**——与上述两者正交的第三种故障态：会话完全健康，交易客户端却已失去与券商主站的链路（2026-10-10 本地物理断网实测，两个完整周期）。可靠信号是**状态栏状态格文本「断开」**（连接态该格空文本，夹在时钟格与 `mncgXXX` 主站名格之间——主站编号每次重连都变，检测绝不绑定它；弹窗不是信号：本地两轮周期零弹窗）。读取纯按需（架构铁律：单 UI 资源禁后台轮询）——状态栏 Pane 用 win32 几何判据定位（底部 60px 条带内最靠右的 Pane），子格文本经每次现建的 UIA wrapper 读取（~6-25ms）。`broker.connected` 语义：
  - `true` = 未观测到断开。注意真实断网后有 **~25-30s 盲区**（心跳周期级延迟，实测 27.1s/24.4s）——`true` ≠ 网络正常
  - `false` = 客户端报告连接断开。**网络恢复后不自动清零**：空闲客户端持续挂「断开」（实测 45s+），直至任意 UI 操作触发重连后才秒级清除（实测 0.9s）。网关的任何业务操作本身就是重连触发器——链路在下次调用时自愈
  - **业务端点对该信号 fail-closed**：每次查询类 UI 操作结束时重读状态格——「断开」在场 → 取到的（缓存）数据**作废**，调用直接返回 `BROKER_DISCONNECTED`；下单/撤单序列完成后触发则返回 `ORDER_STATE_UNKNOWN`（是否送达券商未知，幂等记录保留——先查单核实）。实测动机：断网期间 3/3 持仓查询 HTTP 200 静默返回缓存数据、网关日志零痕迹。**刻意不做操作前预检**：操作本身就是触发客户端重连的「戳」（实测网络恢复后 ~0.9s 自愈）——预检会拒绝掉恰好能治愈链路的那次操作（空闲客户端「断开」永不自清 → 死锁）。残余：断网头 ~25-30s 是盲区（客户端自己尚未察觉）——仍可能漏过一次缓存响应
  - `null` = 读取失败（窗口不在/Pane 结构对不上/UIA 异常）——未知，非确定已连接
  - 服务器事故中出现的 `[主站]数据发送错误 auth plugin ServiceId[...]` 弹窗归类为 `SERVER_UNAVAILABLE`（弹窗规则 + 输入阶段拦截关键词）——该弹窗只有「确定」键，落入通用警告点「是(Y)」兜底会无键可点
- **不要锁屏**（`Win+L` 或带锁定的屏保会切到安全桌面，自动化失效；锁定但挂接 console 的会话状态为 Active，自愈不处理）
- **注册为 Windows 服务 / 计划任务「不管用户是否登录都要运行」不可行**：它们落在 Session 0，看不到也无法操作交互会话的窗口（窗口枚举为空）。因此开机自启同样要求先有人登录——VNC（或 RDP）连入后手动启动 `xiadan.exe` 与网关
- **例外——`scripts/run_in_session.py`**：从一个*已在运行*的 Session 0 管理上下文（如 SSH 会话）把进程启动**进**交互会话，无人值守可用。原理：复制交易进程的 token，持 `SeDebugPrivilege` 模拟同会话 winlogon（SYSTEM）以满足 `CreateProcessAsUser` 对**调用方**的 `SeAssignPrimaryTokenPrivilege` 校验（管理员默认不持有；注意复制出的 token 特权是「持有但未启用」，须在模拟 token 上先启用），再在该会话的 `WinSta0\Default` 上创建子进程——无需密码、不建计划任务、零持久化。无人值守重启网关：先杀旧进程（单实例互斥），再 `.venv/Scripts/python.exe scripts/run_in_session.py --cwd <仓库> --stdout <日志> -- .venv/Scripts/python.exe main.py`。坑：`CreateProcessWithTokenW` 在此是死路——子进程落在**调用方**会话
- 服务器重启后：VNC（或 RDP）连入 → 登录 → 启动 `xiadan.exe` + 券商登录 → 启动网关

**VNC 部署（推荐接入方式）**：安装 [TightVNC Server](https://www.tightvnc.com/)（以 Windows 服务运行，镜像 console 会话），设置强 VNC 密码，并在防火墙限制 VNC 端口——**切勿暴露公网**（建议走 SSH 隧道访问）。严格说自动化不依赖 VNC（RDP + 自愈也能跑——服务端关停实测无影响），但 VNC 提供的常驻 console 会话是唯一能同时满足以下三点的形态：无断开锁定循环、无自愈等待窗、横幅截获一次命中——因此作为推荐接入方式（2026-10-10 重启 + 纯 VNC 实测：查询/带截获下单/撤单全绿）。镜像属性也让它成为最廉价的应急目检手段。

## 上线前必改（安全检查）

以下配置默认是开发便利取向，**对外提供服务前必须确认**：

| 检查项 | 默认 | 要求 |
|--------|:---:|------|
| `auth.enabled` | `false` | **改为 `true`** 并设置强 token。未开启时任何能访问服务的人都能下单/撤单 |
| token 传输方式 | 仅 Header | token 只通过 `Authorization: Bearer <token>` 或 `X-API-Key` 请求头传递，**不支持 query string**（`?token=xxx` 已移除——会泄漏到访问日志/浏览器历史） |
| 监听地址 | `127.0.0.1` | 局域网使用需确认防火墙策略；`0.0.0.0` 暴露到公网风险自负 |
| `/health` 信息暴露 | 公开 | 健康检查始终公开（监控探活），但已不返回 `trading_app_paths` 等本机路径信息 |

> 启用认证后，`/health` 仍无需 token（监控探活设计）。

## 配置

复制 `config/app_config.example.json` 为 `config/app_config.json`，修改 `trading_app_paths`：

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

| 关键配置 | 默认值 | 说明 |
|---------|--------|------|
| `trading_app_paths` | `[]` | `xiadan.exe` 完整路径列表（按优先级排序），**至少配一个** |
| `task_queue.watchdog_timeout_seconds` | 30 | 下单看门狗超时（秒） |
| `task_queue.query_timeout_seconds` | 30 | 查询操作超时（秒） |
| `task_queue.confirm_timeout_seconds` | 10 | 确认/按键操作超时（秒） |
| `task_queue.max_size` | 50 | 队列最大长度 |
| `idempotency.order_dedup_window_seconds` | 60 | 同 key 重试拦截窗（秒），与推荐客户端超时 40s 校准 |
| `alerts.webhook_url` | 空 | 告警外推 webhook（**空=禁用**）：连续任务失败≥3、下单弹窗行为漂移、任务看门狗超时时后台 POST JSON；支持热更新 |
| `alerts.format` | generic | `generic`=完整结构化 JSON（自建 receiver）；`text`=企业微信群机器人/钉钉自定义机器人文本格式；`feishu`=飞书/Lark 自定义机器人文本格式 |
| `alerts.timeout_seconds` | 5 | webhook POST 超时（秒）。后台 daemon 线程发送，不阻塞交易路径 |
| `ocr.max_retry` | 3 | 验证码识别最大重试次数 |
| `order.reject_outside_trading_hours` | false | 下单入口交易时段预检（工作日 + 法定节假日 + 9:15-11:30 / 13:00-15:00 粗判，节假日历三级降级：chinesecalendar → 深交所官方月度日历（当年 12 个月一次性拉取并缓存到 `data/trading_calendar/`，缓存齐全后离线读取）→ 仅周末/工作日粗判，节假日由券商报错兜底）。开启后非交易时段秒级返回 `OUTSIDE_TRADING_HOURS`，免走完整 UI 流程 ~11s；默认关闭以保留收盘后挂单行为 |
| `order.capture_entrust_no` | false | 下单成功后从右下角成功横幅截获合同编号（后台抓屏 + 模板 OCR，句号锚定——券商编号长度不一）。截获失败返回 `null`；下单成败与之无关（见响应说明）。成功路径加 ~1s，失败路径最多 5s 截获超时。配合 `order.verify_entrust_no` 对账使用。会话形态差异：console 挂接（VNC）与 RDP 挂接态直接命中（2026-10-10 实测）；RDP 断开态无法可靠读取——高度门控拒绝读取，下单自动落回 `order.recover_entrust_no` |
| `order.entrust_no_timeout_seconds` | 5.0 | 横幅截获等待超时（秒）；成功即返回，仅拖慢失败路径 |
| `order.recover_entrust_no` | true | 横幅截获失败时按**提交点击时刻×参数四元组**（操作+代码+价格+数量；对全精度点击时刻施加秒桶窗口 [-1,+2] 构成秒桶闭包，完整覆盖秒粒度「委托时间」）反查当日委托回补编号。仅唯一命中才采纳（0 或 ≥2 候选保持 `null`，绝不猜测）。以独立排队查询链式追加——仅截获失败路径多 ~6-8s，成功路径不受影响。采纳时置 `entrust_no_recovered: true` |
| `order.verify_entrust_no` | false | 下单成功拿到委托号后自动追加一笔当日委托查询对账（响应附加 `entrust_no_verified`）。未命中时先 F5 刷新当日委托页重拷一次才报 `false`——券商委托列表对新委托有秒级可见性延迟（2026-09-29 压测实测）。开启后接口耗时增加一次查询，调用方 timeout 需相应放大。需配合 `order.capture_entrust_no` 使用 |
| `order.verify_code_input` | true | 输入代码后校验证券名称联动（非空=代码被接受），未联动重输一次仍失败则拒绝提交（`INPUT_VERIFY_FAILED`）。失败路径增加约 3-6s |
| `ocr.ddddocr_enabled` | false | ddddocr 调试开关（开启后可启用双引擎质检+模板提取，需 `uv sync --extra ocr`） |
| `window_monitor.enabled` | true | 窗口最小化监控开关 |
| `window_monitor.login_grace_seconds` | 90 | 登录/启动期避让：券商客户端进程启动后此时长内，窗口隐藏/最小化不自动恢复——登录绘制期强制显示会与客户端自身绘制竞争，造成窗口元素图标错位（2026-10-08 实测：先启动网关、后登录客户端时触发）。年龄判据在客户端自行显示过窗口一次后即解除（首次自行显示=登录完成铁证，此后手动隐藏/最小化立即恢复，不等满宽限期）。另有两级与时长无关的独立判据同样避让：同进程存在可见登录框（#32770 或标题含「登录」）、前台窗口是该进程的其他窗口（用户正在登录框交互）。`0`=关闭年龄判据 |
| `auth.enabled` | false | Token 认证开关 |
| `logging.level` | INFO | 日志级别（排查 UIA 控件失效等问题时改 `DEBUG`，热生效无需重启） |

> 修改配置后 `POST /admin/reload-config` 热重载（部分路径变更需重启）。超时值参考最新性能实测（2026-08 模拟盘：下单最坏 ~8s、查询最坏 ~12s 含失败重试，30s 提供 2.5-3.5× 余量；卡死任务更快触发看门狗恢复、调用方更快失败）。

## API 接口

### 响应格式

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
| `DUPLICATE_ORDER` | 重试拦截窗口内同一 `Idempotency-Key` 重复提交 |
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
| `INSUFFICIENT_BALANCE` | 可用资金/余额不足（点「确定」关闭，干净退出，下次同向可跳过） |
| `SHORT_SELLING_FORBIDDEN` | 不允许卖空——无持仓或超出可卖数量（点「确定」关闭，干净退出） |
| `PRICE_OUT_OF_RANGE` | 价格超出涨跌停限制（点「否」取消，干净退出，下次同向可跳过） |
| `ORDER_PRICE_REQUIRED` | 券商要求填写委托价格（市价类型未选择/不受支持，或限价未传 price；建议改用限价模式） |
| `SERVER_UNAVAILABLE` | 券商服务器不可用（事务处理机转发失败等） |
| `OCR_FAILED` | 验证码识别失败 |
| `INPUT_VERIFY_FAILED` | 证券名称联动校验失败（代码未被客户端接受） |
| `INTERNAL_ERROR` | 未知异常 |
| `QUEUE_TIMEOUT` | 任务排队超时 |
| `QUEUE_FULL` | 队列已满 |
| `SESSION_UNAVAILABLE` | RDP 会话断开，任务**未执行**即毫秒级快速拒绝（与 `TASK_TIMEOUT` 相反：确定未触碰客户端，幂等记录自动清除，同 `Idempotency-Key` 恢复后重试即安全） |
| `SESSION_DESKTOP_UNAVAILABLE` | 会话已挂接但桌面不可操作（输入注入被拒/无活动输入桌面，激活失败命中僵死指纹）。任务**未执行**（交易窗口找得到，是桌面拒收输入）——幂等记录自动清除，恢复后重试即安全。通常 RDP 重连一次即复位；无人值守时由升级自愈（tsdiscon）自动处理 |
| `TASK_TIMEOUT` | 任务超时，恢复成功 |
| `TASK_TIMEOUT_RECOVERY_FAILED` | 任务超时，恢复也失败 |
| `ORDER_STATE_UNKNOWN` | 下单**点击提交后**发生非业务异常（如 RDP 断开瞬间桌面消亡）——订单可能已提交，状态未知；幂等记录**保留**，同 key 重试会被拦截，先查单核实（确认未提交后用新 key 重试）。下单/撤单序列完成后券商链路门控触发时同样抛本错误（见 `BROKER_DISCONNECTED`） |
| `BROKER_DISCONNECTED` | **查询**类 UI 操作结束时客户端报告券商主站链路断开（状态栏「断开」）——取到的将是客户端缓存旧值，已**作废**并直接报错。直接重试即可：每次调用都会触发客户端重连（实测网络恢复后 ~0.9s 自愈）；注意真实断网后有 ~25-30s 心跳盲区 |

> 弹窗类型与「干净退出」语义详见[弹窗分类处理](#弹窗分类处理)。

### 接口总表

| 方法 | 路径 | 说明 | 入队 | timeout |
|------|------|------|:---:|--------|
| GET | `/health` | 健康检查 + 登录态（`logged_in`）+ 会话状态（`session.ui_available`，RDP 断开期间为 `false`——监控轮询恢复信号用；`session.desktop_wedged`，`true` = 挂接但僵死，见桌面僵死章节）+ 券商连接态（`broker.connected`：`true` = 未观测到断开，真实断网后有 ~25-30s 盲区；`false` = 客户端报告券商链路断开——网络恢复后不自动清零，操作触发重连后 ~1s 清除；`null` = 读取失败。业务端点对该信号自动 fail-closed——查询 → `BROKER_DISCONNECTED`、下单/撤单 → `ORDER_STATE_UNKNOWN`——本字段供监控/面板用，调用方无需再自行核对）+ 推荐客户端 timeout + 运行统计（成功率/错误码聚合/连续失败/下单弹窗统计） | | 5s |
| GET | `/queue/status` | 任务队列状态 | | 5s |
| POST | `/admin/reload-config` | 热重载配置 | | 5s |
| GET | `/account/balance` | 资金余额 | ✓ | 40s |
| GET | `/positions` | 持仓查询 | ✓ | 40s |
| GET | `/trades/today` | 今日成交 | ✓ | 40s |
| GET | `/orders/pending` | 当日委托 | ✓ | 40s |
| GET | `/orders/{entrust_no}/status` | 按合同编号查委托状态 + 成交回报（join 当日委托 × 当日成交；每次调用两份表拷贝） | ✓ | 60s |
| POST | `/orders` | 下单（限价/市价） | ✓ | 40s |
| POST | `/orders/cancel-all` | 撤单（全部/撤买/撤卖/撤最后） | ✓ | 40s |
| POST | `/actions/send-key` | 手动发送按键 | ✓ | 30s |
| POST | `/actions/click` | 鼠标点击坐标 | ✓ | 30s |
| POST | `/actions/close-dialog` | 关闭买入/卖出子面板 | ✓ | 30s |
| GET | `/ocr/quality` | OCR 质检报告（准确率/模板/覆盖） | | 5s |
| GET | `/diagnostic/snapshot` | 截图 + UI 文本 + OCR（worker 忙时让位等待 2s 后照常执行，响应带 `worker_busy` 标记） | | 10s |
| GET | `/diagnostic/history` | 最近 N 步任务诊断历史 | | 5s |

> POST 接口支持三级传参通道（优先级从高到低）：JSON body（**不依赖 Content-Type**——`curl -d '{"type":"X"}'` 不带头也能解析）→ query string → form body（`curl -d type=X`，urlencoded/multipart）。

### POST /orders — 下单

| 参数 | 必填 | 说明 |
|------|:---:|------|
| `code` | ✓ | 股票代码 |
| `status` | ✓ | `1`=买入, `2`=卖出 |
| `amount` | | 委托数量 |
| `price` | | 委托价格（限价模式，最多 2 位小数） |
| `price_type` | | `limit`=限价(默认), `market`=市价 |
| `confirm` | | `true`=自动确认（默认）。`false`=仅在客户端弹出「委托确认」时点「否(N)」取消——推荐快速交易设置下（客户端确认全关，见[前置准备](#前置准备券商软件设置)）无弹窗直接成单，`false` **不是**可靠的预览/拦截 |

> **强制要求 `Idempotency-Key` 请求头（1–128 字符）**——见[幂等与价格校验](#幂等与价格校验)。key 生命周期：每个逻辑订单一个 key，超时重试复用同一 key；确要新单（含同参数多单）用新 key。

```bash
# 市价买入
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"code":"601991","status":"1","amount":"100","price_type":"market"}'

# 限价买入
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"code":"600000","status":"1","amount":"100","price":"10.50","price_type":"limit"}'

# 市价卖出
curl -X POST http://localhost:5000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"code":"601991","status":"2","amount":"100","price_type":"market"}'
```

**响应**（`data` 字段）：

| 字段 | 说明 |
|------|------|
| `action` / `mode` / `code` / `amount` / `price` | 回显下单参数 |
| `confirmed` | `true`=已提交（快速交易模式下无错误弹窗即判定成功） |
| `entrust_no` | 合同编号（委托号）。仅启用 `order.capture_entrust_no` 后返回，截获失败或未启用时为 `null`。开启 `order.recover_entrust_no`（默认开）时，截获失败会按点击时刻窗口×参数匹配反查当日委托一次——唯一命中才采纳并置 `entrust_no_recovered: true` |
| `entrust_no_verified` | 委托号对账结果。仅 `order.verify_entrust_no` 开启且拿到委托号时返回：`true`=当日委托落表命中 / `false`=F5 刷新重拷一次后仍未命中（横幅号可能不准，以查询为准）/ `null`=对账查询失败（不影响下单结果语义） |

> **`entrust_no: null` 不代表下单失败**——成败判定基于弹窗检测，与横幅截获解耦。
> `null` 的语义是"已提交（推断），编号未知"（横幅未出现/被遮挡/窗口最小化）。
> 此时**不要重试**（会重复下单），需要编号时用 `GET /orders/pending`
> 按 代码+价格+数量+时间 反查。另注意：券商服务器维护窗口等极端情况下
> 横幅号与最终落表号可能不一致，按单操作前应以当日委托查询复核；
> 开启 `order.verify_entrust_no` 可在下单响应中直接获得对账结果
> （`entrust_no_verified`）。

### GET /orders/{entrust_no}/status — 委托状态与成交回报

按**合同编号** join 当日委托 × 当日成交（两表间的外键；券商生成，长度随券商/交易所而异——模拟客户端 10 位，深交所规范 22 位；成交编号为交易所生成：上交所 16 位零填充，深交所 8 位）。单个排队任务跑两次表拷贝（通常 8–15s）。

- `found: false` —— 当日委托中无此合同编号（非当日下单，或编号有误）
- `order.status` —— 由数量推导，不依赖券商备注文本（跨券商稳定）：`全部成交` / `部分成交` / `部分成交后撤单` / `全部撤单` / `未成交` / `未知`（数量缺失）
- `order.is_final` —— `filled + cancelled >= amount`：该笔委托已不再留在市场
- `fills` —— 按合同编号聚合的成交：`count` / `total_qty` / `total_amount` / `avg_price`（按金额/数量加权，未成交为 `null`）/ `trades[]`（`time` / `trade_no` / `qty` / `price` / `amount`，按时间排序）

```bash
curl -H "X-API-Key: $TOKEN" http://localhost:5000/orders/6284424619/status
```

### POST /orders/cancel-all — 撤单

撤单统一在 F3 撤单页操作（一次 F3 按键即可到达），全撤/撤买/撤卖/撤最后 四按钮 control_id 跨页一致（实测 30001/30002/30003/1946）。点击撤单按钮后若出现确认弹窗（cid=1040 提示文字 + cid=6 "是(Y)" 按钮），自动点击确认并从弹窗文字解析撤单数量；快速交易模式（撤单不需要确认）无弹窗直接生效。按钮灰显（当前无可撤委托）直接返回，不占用队列重试。

| 参数 | 必填 | 说明 |
|------|:---:|------|
| `type` | | `A`=全部(默认), `X`=撤买, `C`=撤卖, `L`=撤最后（撤销最近一笔委托） |

```bash
curl -X POST http://localhost:5000/orders/cancel-all
curl -X POST http://localhost:5000/orders/cancel-all -d '{"type":"X"}'
```

**响应**（`data` 字段）：

| 字段 | 说明 |
|------|------|
| `cancel_type` | 操作名（全部撤单/撤买/撤卖/撤最后） |
| `success` | 是否执行了撤单（按钮灰显时为 `false`） |
| `cancelled_count` | 撤单数量（从确认弹窗文字解析；无弹窗/解析失败为 `null`，灰显路径为 0） |
| `confirm_dialog_shown` | 是否出现撤单确认弹窗 |
| `reason` | 仅按钮灰显时返回，如"当前无可撤委托" |

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

### 调用方 timeout 配置

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

## MCP 服务（agent 接入）

轻量 MCP 适配器（[`scripts/mcp_server.py`](scripts/mcp_server.py)）将网关能力包装为标准 MCP 工具，大模型 agent（Claude Desktop、ZCode 等任意 stdio MCP 客户端）无需了解 REST 细节即可查询与交易：

```
MCP 客户端 ──stdio──→ mcp_server.py ──HTTP──→ 网关(Flask) ──→ xiadan.exe
```

暴露面刻意分层：

| 层 | 工具 | 可用性 |
|----|------|--------|
| 只读查询 | `gateway_health` `get_queue_status` `get_balance` `get_positions` `get_today_trades` `get_today_orders` `get_order_status` | 恒注册 |
| 交易 | `place_order` `cancel_orders` | 仅 `XIADAN_MCP_TRADING=1` 时注册 |
| 裸 UI 操作（`/actions/*`）、`/admin/*` | — | 永不暴露给 agent |

安全设计：

- 适配器不 import `src/` 任何模块、不进交易路径——队列串行化、幂等、告警全部经 HTTP 层自动继承
- `place_order` 将 `buy`/`sell` 映射为 `1`/`2`，本地前置校验（6 位代码、正整数数量、价格最多 2 位小数），限价单**必须显式数值价格**（拒绝"最新价"等模糊语义），工具描述强制「先查询 → 向用户逐字复述参数并确认 → 下单 → `get_order_status()` 核对（单笔状态+成交；横幅号未截获时回退 `get_today_orders`）」工作流。API 强制的 `Idempotency-Key` 缺省自动填 uuid4（除非 agent 显式传 `idempotency_key`）——自动生成意味着超时后的再次调用会被视为**新订单**（不重用自有 key 就没有重试保护）
- 认证复用网关 token：优先 `XIADAN_MCP_TOKEN` 环境变量，缺省自动回读 `config/app_config.json`；请求永不携带 `Origin` 头（与跨站防御兼容）
- 网关错误以 MCP `isError` 结果返回，格式为 `[ERROR_CODE] message | 建议 | request_id`；网关未启动时返回带启动指引的提示而非堆栈

安装：

```bash
uv sync --extra mcp
```

客户端注册（Claude Desktop 或任意 stdio MCP 客户端，结构相同）：

```json
{"mcpServers": {"xiadan-gateway": {
    "command": "uv",
    "args": ["--directory", "C:/path/to/xiadan-gateway", "--extra", "mcp",
             "run", "python", "scripts/mcp_server.py"],
    "env": {"XIADAN_MCP_TRADING": "1"}
}}}
```

> ⚠️ 仅在支持「逐次工具调用人工确认」的 MCP 客户端中开启交易工具，且 `place_order`/`cancel_orders` 的确认不要关——显式价格规则与复述确认工作流，防的正是 LLM 自行决定交易参数这一失败模式。

配置（环境变量，均可缺省）：

| 变量 | 缺省值 | 含义 |
|------|--------|------|
| `XIADAN_MCP_URL` | 读 `config/app_config.json`，否则 `http://127.0.0.1:5000` | 网关基地址 |
| `XIADAN_MCP_TOKEN` | 配置文件 `auth.token`（启用认证时） | 认证 token |
| `XIADAN_MCP_CONFIG` | `config/app_config.json` | 网关配置文件路径 |
| `XIADAN_MCP_TRADING` | `0` | `1`/`true` 注册 `place_order`/`cancel_orders` |
| `XIADAN_MCP_TIMEOUT_SECONDS` | `60` | 对网关的 HTTP 超时（建议 ≥40） |

## Skill 接入（任意 agent）

MCP 之外，仓库还自带一个 [agent skill](.agents/skills/xiadan-gateway/SKILL.md)——不要求客户端支持 MCP 协议，任何能执行 shell 命令的 LLM 工具（ZCode、Claude Code、Cursor 等）都能通过它使用网关：

```
agent ──skill CLI──HTTP──→ 网关(Flask) ──→ xiadan.exe
```

skill 目录随仓库分发（`.agents/skills/xiadan-gateway/`），包含：

- `SKILL.md` — 触发条件与强制操作准则（登录门、交易前逐字复述确认、幂等键重试语义、错误码应对）
- `scripts/xiadan.py` — 纯标准库 CLI 薄适配层（agent 不手拼 curl，绕开 PowerShell 的 `curl` 别名陷阱与引号转义问题）
- `references/api.md` — 按需加载的完整 API 契约（参数 / 响应字段 / 全量错误码）

安装（按所用工具选一种）：

| 工具 | 方式 |
|------|------|
| ZCode（在本仓库内工作） | 零安装——`.agents/skills/` 是 ZCode 原生发现路径 |
| ZCode / Claude Code（任意项目可用） | 把 `.agents/skills/xiadan-gateway/` 复制或链接到 `~/.zcode/skills/` / `~/.claude/skills/` |
| 其它 agent | 将 SKILL.md 内容作为指令注入，或直接允许其调用 CLI |

用法（CLI 子命令与 MCP 工具一一对应）：

```bash
uv run python .agents/skills/xiadan-gateway/scripts/xiadan.py health
uv run --no-project python <skill目录>/scripts/xiadan.py positions

# 交易命令需显式开启（与 MCP 适配器共用同一开关与同一组环境变量）
XIADAN_MCP_TRADING=1 uv run python .agents/skills/xiadan-gateway/scripts/xiadan.py \
    buy --code 601991 --amount 100 --price 10.50
```

暴露面与 MCP 适配器同一套分层：只读命令（`health/queue/balance/positions/trades/orders/order-status`）恒可用；`buy/sell/cancel` 需 `XIADAN_MCP_TRADING=1`；`/actions/*` 与 `/admin/*` 永不暴露。退出码 `0/1/2/3` = 成功 / 网关错误 / 用法错误 / 无法连接。

安全设计：

- CLI 与 MCP 适配器同为「薄适配层」：纯标准库、不 import `src/` 任何模块，队列串行化/幂等/告警全部经 HTTP 层继承；环境变量（`XIADAN_MCP_URL/TOKEN/CONFIG/TIMEOUT/TRADING`）两套适配层通用
- 下单所用幂等键打印到 stderr——超时后重试必须复用同一键（同键被网关拦截，新键=新订单）
- `tests/test_skill_cli.py` 密封桩测（28 例）覆盖注册面/参数校验/语义映射/HTTP 契约，并守卫 SKILL.md 安全条款不漂移

> ⚠️ 与 MCP 同一条红线：仅在支持逐次命令人工确认的 agent 中开启交易命令，SKILL.md 的复述确认工作流不能省。

支持 MCP 的客户端仍建议优先用 [MCP 服务](#mcp-服务agent-接入)（结构化工具调用比 CLI 文本输出更易解析）；两个适配层背后是同一个网关进程。

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
4. **OCR 验证码**：Ctrl+C 几乎必然触发验证码弹窗（偶发不触发时有剪贴板兜底，见[查询面板标准化](#查询面板标准化)），轻量模板匹配引擎自动识别（详见 [验证码 OCR](#验证码-ocr--轻量模板匹配)）
5. **窗口监控**：后台线程定期检测交易窗口状态，最小化时自动恢复；客户端登录/启动期自动避让（进程年龄/登录框/同进程前台三判据），防止恢复动作与登录绘制竞争造成元素错位
6. **下单耗时优化**：通过复用 UIA 控件树遍历、流水线模式切换、连续干净跳过等手段大幅压缩单笔耗时（当前实测见[性能实测汇总](#性能实测汇总)）

## 关键设计

### 任务队列与看门狗

所有操作通过单 worker 线程 `TaskQueue` 顺序执行，避免 `xiadan.exe` 并发冲突。默认每个任务前调用 `WindowService.reset_window_state()` 重置窗口到 F1 买入基准态（激活 + ESC×5，含窗口位置自愈——被误拖出屏幕时自动移回）；连续同组干净退出时跳过重置（见核心特性表「连续干净跳过」）。任务超时后看门狗执行「截图存档 → 激活窗口 → ESC×3 重置」恢复流程，**完成所有恢复后才返回错误**，确保调用方收到 `TASK_TIMEOUT` 时 `xiadan.exe` 已恢复初始状态。

> **看门狗并发边界**：恢复流程（ESC 重置）在看门狗定时器线程执行，而超时任务的 worker 线程可能仍在跑。该保证只覆盖窗口最终状态，不覆盖被弃任务本身的副作用——僵尸任务可能在恢复完成、错误已返回之后仍驱动 UI（如点出下单按钮）。单 worker 设计确保下一个排队任务在僵尸任务返回前不会开始；调用方应把 `TASK_TIMEOUT` 视为"订单状态未知，先核实再重试"（幂等规则在超时场景保留去重记录正是为此）。

### 幂等与价格校验

- **幂等键必填**：每笔 `POST /orders` **必须**携带 `Idempotency-Key` 请求头（1–128 字符）。key 生命周期 = **每个逻辑订单一个 key**：HTTP 超时重试必须**复用同一 key**（窗口内同 key 拒绝 `DUPLICATE_ORDER`——这正是重试保护）；确要新单（含同参数多单）必须用**新 key**，不同 key 同参数立即放行。服务端不校验"随机性"（无法校验），契约是**唯一性**；uuid4 最省力，"策略ID+自增序号"同样合法。
- **窗口语义**：`idempotency.order_dedup_window_seconds`（默认 60，与推荐客户端超时 40s 校准）= 同 key 重试拦截窗。下单失败清除记录允许同 key 重试；超时不清除（防止重复提交）。key 缺失/空白 → `VALIDATION_ERROR` 并附指引。
- **价格**：API 层拦截超 2 位小数的价格（`VALIDATION_ERROR`），下单层自动 `sanitize_price()` 格式化为 2 位小数。

### 弹窗分类处理

下单/撤单过程中自动检测弹窗类型并分别处理：委托确认弹窗（点 Y/N）、警告弹窗（点「是(Y)」继续提交）、价格超限（点「否(N)」取消）、错误弹窗（关闭后报错）。分类由 `src/core/popup_rules.py` 规则表驱动（如何扩充见[添加新的干净退出场景](#添加新的干净退出场景)）。

**弹窗关闭机制**：非委托确认类弹窗统一用 `_close_non_confirm_popup()` 关闭——优先批量查找标准 Windows 按钮（IDOK=1 / IDCANCEL=2，一次遍历同时找两个），降级用 `keybd_event` 直发 ESC（不经过 `send_key`，避免前台窗口校验被模态弹窗阻断）。委托确认弹窗的「否(N)」降级也走同一方法。

**委托确认安全校验**：弹窗处理循环先检测「委托确认」标题（cid=1365 文本匹配）才点击「是(Y)」/「否(N)」——快速交易模式（无弹窗）时循环首轮即退出，Y 键不会泄漏到其他窗口。

#### 弹窗类型与「干净退出」

下单/撤单后可能出现多种弹窗，处理方式决定窗口状态是否可信：

| 弹窗类型 | 标题示例 | 按钮 | 处理 | 窗口状态 |
|---------|------|:---:|------|:---:|
| 委托确认 | 「委托确认」 | 是(Y) / 否(N) | 点 Y 确认 / 点 N 取消 | 可信 |
| 价格超限 | 「提示信息」 | 是(Y) / 否(N) | 点 N 取消 → `PRICE_OUT_OF_RANGE` | 可信（干净退出） |
| 单按钮提示 | 「提示」 | 确定 | 点击确定（只能用鼠标，Y 键无效） | 取决于内容 |
| 余额不足 | 「提示」 | 确定 | 点确定关闭 → `INSUFFICIENT_BALANCE`（组合关键字：「提交失败」+ 余额/资金 + 「还差」） | 可信（干净退出） |
| 卖空限制 | 「提示」 | 确定 | 点确定关闭 → `SHORT_SELLING_FORBIDDEN`（「不允许卖空」或 「提交失败」+「无证券」+「持仓信息」） | 可信（干净退出） |
| 致命错误 | 「提示信息」 | 确定 | 关闭 + 分类报错 | **不可信** |

> **注意**：标题为「提示」的单按钮弹窗只有「确定」按钮（cid=1），无法用字母键触发。调试时若发现 Y 键无效，检查是否为单按钮弹窗。

#### 提交失败弹窗的精细分类

点击买入后若券商返回「提示」弹窗（只有确定键），`_extract_popup_error_text()` 从控件树中提取干净弹窗文本（容器优先，黑名单兜底），`match_submit_error()` 按规则表（`SUBMIT_ERROR_RULES`）返回精确错误码和针对性建议：

| 弹窗关键词 | error_code | 建议 |
|-----------|-----------|------|
| 清算 | `SERVER_CLEARING` | 等待清算结束后重试 |
| 当前时间不允许委托 | `OUTSIDE_TRADING_HOURS` | 交易时段内操作 |
| T+1 / 当日买入 / 未交收 | `T1_RESTRICTION` | 当日买入的股票需到下一个交易日方可卖出 |
| 提交失败 + 余额/资金 + 还差 | `INSUFFICIENT_BALANCE` | 检查账户可用资金后调整数量或价格 |
| 不允许卖空 / 提交失败 + 无证券 + 持仓信息 | `SHORT_SELLING_FORBIDDEN` | A 股不允许卖空，检查持仓可卖数量 |
| 可卖数量 / 可用余额不足 | `INSUFFICIENT_SHARES` | 检查持仓可卖数量后调整 |
| 事务处理机转发失败 | `SERVER_UNAVAILABLE` | 确认券商服务器正常 |
| 其他 | `ORDER_SUBMIT_FAILED` | 通用建议 |

`details.popup_text` 返回弹窗原文供调用方自行解析，`details.popup_title` 返回弹窗标题。买入和卖出共享同一套 `place_order()` 流程，仅 F1/F2 切换不同，所有分类逻辑对买卖双方均等生效。

#### 弹窗文本提取与黑名单自学习

`order_detail_text` 优先从 cid=1040 读取，fallback 用 `_extract_dialog_text(title_el)` 从弹窗容器（`title_el.parent()`）内收集文本；`_extract_popup_error_text` 同样**容器优先**（纯净、不依赖硬编码 UI 标签黑名单，券商界面升级不受影响），容器提取为空才降级全局扫描 + 双层黑名单过滤（兜底防线）。黑名单第一层是每笔订单开始时对安静态主窗口文本拍的运行时快照（自学习：券商界面升级新增的标签自动被过滤，无需改代码），第二层是原有硬编码标签清单作静态底线；匹配统一用组合文本（primary + 兜底提取），cid=1040 提取不完整时错误弹窗仍能被精确分类，不会被当作通用警告点「是(Y)」。

#### 服务器错误弹窗防御

与券商服务器交互时（输入代码查询价格、切换价格模式、点击买入/卖出按钮），若服务器不可用或处于非交易时段，可能弹出「提示」弹窗。弹窗只有「确定」键，无法用 Y/N 键操作，统一用按钮点击（cid=1/2）或 ESC 关闭。

| 触发阶段 | 弹窗内容示例 | 处理 |
|----------|------------|------|
| 输入股票代码后 | 事务处理机转发数据失败 / Begin failed! | `_dismiss_server_error_popup()` 关闭 |
| 切换价格模式 | 同上（服务器无响应） | 超时后检测弹窗 → `SERVER_UNAVAILABLE` |
| 点击买入/卖出按钮 | 提交失败：清算中 / 当前时间不允许委托 / … | 提取文本 → 分类报错 |

关键词统一在 `constants.py:SERVER_ERROR_POPUP_KEYWORDS` 中维护（阻塞型提示弹窗关键词为 `constants.py:BLOCKING_POPUP_KEYWORDS`）。`WindowService.dismiss_blocking_popup()` 默认关键词覆盖中英文（`"失败"` / `"failed"` / `"事务处理机"`），所有调用方（Trader 代码输入后、价格模式切换超时后、F4 查询面板、F3 撤单界面）共享同一套检测逻辑。

### 市价/限价切换

点击"买入价格"标签（cid=1400）触发券商服务器请求，在限价/市价之间 toggle（双向通用）。输入股票代码后**自动检测实际界面模式**——券商可能记住每只股票上次交易模式并在代码输入后自动切换（如 000001 上次用市价卖出，界面变为「市价卖出」无法填价格）。

策略：`sleep(0.3)` 先检测弹窗（服务器拒绝时弹窗 <0.5s 即出现）→ 有弹窗直接分类报错 → 无弹窗缓存 label 元素引用后 `poll_until` 轮询文本变化（超时 3s，最多 2 次重试，每次轮询只读文本不再遍历 UIA）。两种失败场景分别处理：

| 场景 | 表现 | error_code |
|------|------|-----------|
| 服务器异常（维护） | 弹窗「事务处理机转发数据失败」 | `SERVER_UNAVAILABLE` |
| 模拟账户不支持市价 | 无弹窗，标签静默不变 | `MODE_SWITCH_FAILED`（建议改用限价） |

### 查询面板标准化

所有查询通过 `_prepare_query_panel()`（仅发 F4 切换到查询面板）进入查询面板，再显式导航到目标页（资金股票/当日成交/当日委托——不依赖"F4 默认页"假设，连续查询跨页时窗口可能停在其他页）。TaskQueue worker 在每个任务前已调用 `reset_window_state()`（ESC×5→F1），查询方法内不再重复重置，省 ~1.7s/次。导航动作本身触发券商服务器查询，无需额外 F5 刷新。空数据表格（仅有表头无数据行）正常返回空列表。

**查询假数据防御**：Ctrl+C 前清空剪贴板（复制失败时不读到上次任务残留数据）；复制后按特征列验证（持仓=`成本价`+`股票余额`、成交=`成交时间`+`成交编号`、委托=`委托价格`+`委托数量`——**实测表头**：委托表无"委托编号"列，此版本用"合同编号"，但成交表也有"合同编号"区分度差，委托表独有特征是"委托价格/委托数量"）——页面切换异常（窗口被遮挡/最小化、焦点未进入表格等）时复制到的是其他查询表，验证失败重试一次并记录实际表头，仍失败显式报错（`INTERNAL_ERROR`），绝不静默返回假数据。

**导航与验证码双兜底**（2026-09-05）：① 树节点点击后校验 `is_selected()` 选中态，未生效自动重试——实测模拟盘 `click_input` 偶发未注册，页面停留"当日委托"，而空表会绕过特征列验证、持仓被静默返回空列表，此修复堵住该路径；② 复制后未检测到验证码弹窗时兜底校验剪贴板——复制偶发未触发验证码（实测存在，多为间隔较久后的首笔）/弹窗漏检时，有效表格直接采用，不再空转两轮后误报 OCR 失败。

### 事件驱动等待

`src/utils/poll.py` 提供 `poll_until(condition, timeout, interval)` 替代固定 `time.sleep()`。下单后等弹窗、Ctrl+C 后等验证码、撤单后等确认弹窗等场景，每 0.1s 检测 UI 状态，条件满足立即继续，超时抛 `PollTimeoutError`。`timed` 上下文管理器记录每步耗时。

### 按键发送策略

| 方式 | 依赖前台 | 适用场景 |
|------|:---:|------|
| `keybd_event` + `background=True` | ✗ | 功能键（自校验前台：已在前台零开销直发；被切走自动补激活，绝不发错窗口） |
| `keybd_event` | ✓ | 功能键 F1-F12、Ctrl+C 组合键 |
| `PostMessage` | ✗ | 字母键 Y/N、ENTER（后台不抢焦点） |

功能键默认走前台发送（`PostMessage` 无法触发窗口快捷键），发送前用 `click_input()` + `GetForegroundWindow()` 句柄校验确保窗口在前台。`background=True` 表示"调用方已自行激活窗口"（如 `place_order()` 步骤 1），跳过冗余激活省 ~0.6s——但会先做句柄级前台自校验（微秒级）：窗口被切走时自动补完整激活流程再发，绝不把按键发进错误窗口（实测 F4 发空会导致查询页不对、树节点点击未注册，导航多花 ~1.5s）。

### Ctrl+C 双发机制

```text
Ctrl Down → sleep(0.1s) → C Down → C Up → Ctrl Up    (×2, 间隔 0.15s)

第 1 次 → 中文输入法 IME 拦截（取消组合状态）
第 2 次 → IME 已退出，正常送达券商 → 触发验证码弹窗
```

- **延迟 0.1s**：让 `GetAsyncKeyState` 感知 Ctrl 已被按下
- **双发**：绕过中文输入法对首次 Ctrl+C 的拦截
- **不用 SendInput**：券商可能通过 `LLKBF_INJECTED` 标志过滤注入输入
- **不用 PostMessage**：不更新键状态表，券商 `GetAsyncKeyState` 检测不到

### 验证码 OCR — 轻量模板匹配

同花顺 Ctrl+C **几乎必然触发验证码弹窗**（4 位数字，白底蓝字，92×38 像素，规则字体；偶发复制未触发——见[查询面板标准化](#查询面板标准化)的剪贴板兜底）。弹窗出现 = Ctrl+C 成功送达的确认信号。

**识别流程**：主动定时扫描检测弹窗 → 截图 → 截图合理性校验（文件 ≤5KB + 尺寸接近 92×38 + 白底占比 >50% + 暗像素水平跨度 15%-85%，拦截截到主窗口/弹窗边缘/隐藏控件的异常图）→ 灰度化 → 二值化 → 垂直投影分割 → 模板匹配 → 填入券商软件。外层最多 2 次尝试，内层 OCR 最多 3 次重试。

**求解循环加固**（全部来自实弹事故）：内层每次重试**重新截图**（分段成败与数字组合相关，同图重试无意义——实测出现过三张完全相同的失败存档）；提交后扫描弹窗内的拒绝文本——弹窗销毁**不**判成功，因为客户端拒绝错误验证码后会销毁重建弹窗，重建间隙曾误报"验证成功"；自信但被拒的识别存档为 `wrong_<识别值>_<时间戳>.png`（这是唯一不留证据的失败模式）；重试耗尽后安全关闭弹窗（取消/WM_CLOSE），绝不留守死锁前台。任务开始发现的残留弹窗**永不求解**——残留验证码大概率已过期（实测识别无误、输入正确仍被拒）——一律先存档后只关闭（见上方"运行时弹窗自愈"行）。排查弹窗控件结构可用探测工具 `scripts/probe_captcha_dialog.py`（须在与 xiadan.exe 同会话的桌面内运行）。

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
- **分割**：垂直投影 → 暗列分组 → 合并断裂笔画（如 '5' 的横竖间隙）→ 水平裁空白 → 归一化 28×38。噪声过滤只在**又窄（<4px）且又矮**（列高 <4）时才丢弃分组：'1' 天生约 3px 宽但笔画高，曾被纯宽度过滤误杀——`0102` 被切成 3 位（`002`）表现为"识别为空"；加高笔画豁免后，历史失败存档全部恢复正确识别（0102/7617/9315）
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

### 控件树缓存与性能优化

`pywinauto` 的 `descendants()` 遍历 UIA 树耗时约 1s（交易窗口包含数百个控件），原始下单流程中多次独立调用导致累积延迟严重。通过三级缓存策略消除冗余遍历：

**全流程共享**：`place_order()` 在获取窗口后调用一次 `descendants()`，将列表传递给 `input_text_to_element`（代码/价格/数量）和 `click_element`（下单按钮），各自省去内部的 `find_element_in_window` 遍历。

**轮询复用**：弹窗处理循环一次 `descendants()` 遍历同时完成检测（标题图 cid=1365、详情文本 cid=1040）+ 规则表分类（`match_popup_rule`）+ 弹窗关闭，各步骤共享遍历结果，省去重复遍历。

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

### 性能实测汇总

单笔操作当前耗时（最近全量复测 2026-09-05，窗口句柄跨请求缓存后）：

| 操作 | 冷启动 | 连续 |
|------|------|------|
| 买入 | ~6.9s | ~6.0s（同向） |
| 撤单 | ~4.9s | ~1.8s |
| 持仓 | — | ~6.4s |
| 成交 | — | ~5.8s |
| 资金 | — | ~2.2s |

- 服务重启后首查 17.0s → 9.0s：`WindowService` 单例化后句柄缓存跨请求复用，免去每请求 ~2s 全局窗口扫描
- 消息级复制（`query.copy_method=message`）：copy 阶段 5.5s→~1.2s，端到端成交 6.25s→3.20s、持仓 8.35s→5.92s（2026-09-09 基准；2026-09-22 复测持仓 ~3.5s、成交 ~4.5s，均含验证码）
- 单笔下单整体从 ~13.7s（优化前基线）逐步降至当前水平，优化手段与分步明细见[控件树缓存](#控件树缓存与性能优化)与[查询流程性能优化](#查询流程性能优化)两节

## 已知限制

### 菜单栏无法自动化

同花顺菜单栏使用完全自定义渲染，Win32 `GetMenu()` 返回 0，UIA 树中无 PopupMenu 子项。无法通过程序自动配置「系统设置→快速交易」，需用户**手动配置**（见[前置准备](#前置准备券商软件设置)）。

### 买入/卖出子面板关闭

不要用 ALT+F4（关闭整个程序）或 ESC（子面板不是独立对话框，无效）。正确方式：发送 F4 切换到查询视图。`/actions/close-dialog` 接口封装此逻辑。

### Logger 限制

项目自定义 Logger 只接受单个 message 参数，使用 f-string 传参（不支持 `%s` 占位符）。

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
│   │   ├── order_routes.py      # 下单/撤单 Blueprint（含 entrust_no 对账）
│   │   ├── action_routes.py     # 手动操作/诊断 Blueprint
│   │   ├── task_queue.py        # 全局任务队列 + 看门狗恢复 + 运行统计
│   │   ├── response.py          # 统一响应封装（success/error）
│   │   ├── helpers.py           # 路由层共享工具
│   │   └── idempotency.py       # 下单幂等检查（Idempotency-Key 必填）
│   ├── core/
│   │   ├── trader.py            # 下单编排器
│   │   ├── popup_rules.py       # 弹窗/提交错误分类规则表（动作 + 错误码）
│   │   ├── ocr.py               # OCR 服务（双引擎调度 + 质检）
│   │   ├── ocr_lightweight.py   # 轻量 OCR（模板匹配，纯 NumPy/Pillow）
│   │   ├── entrust_capture.py   # 下单横幅截获线程（右下角条带抓取）
│   │   ├── banner_ocr.py        # 横幅数字识别（微软雅黑模板 + IoU）
│   │   ├── validation.py        # 数据校验纯函数（价格/交易时段+节假日）
│   │   └── trading_calendar.py  # 深交所月度日历兜底（按年缓存，chinesecalendar 不可用时）
│   ├── services/
│   │   ├── window_service.py    # 窗口/控件操作基础服务
│   │   ├── window_monitor.py    # 窗口最小化监控线程
│   │   ├── position_service.py  # 持仓/资金/成交查询
│   │   └── trading_service.py   # 撤单服务
│   ├── models/
│   │   └── config.py            # AppConfig（单例 + 热重载）
│   └── utils/
│       ├── singleton.py         # 线程安全单例基类
│       ├── logger.py            # 日志器（文件轮转 + 控制台，级别可配置）
│       ├── uia.py               # UIA 控件安全访问（safe_text/safe_control_type）
│       ├── screenshot.py        # 截图工具 + 自动清理
│       ├── poll.py              # 轮询等待（poll_until / timed）
│       └── diagnostic.py        # 诊断工具（截图 + UI 文本 + OCR）
├── tests/
│   ├── test_core.py             # 核心逻辑单元测试（无需真实券商客户端）
│   ├── test_banner_ocr.py       # 横幅数字 OCR 单元测试（真实横幅条带样本夹具）
│   ├── test_mcp_server.py       # MCP 适配层单元测试（桩掉 HTTP，不启动真实服务）
│   └── test_skill_cli.py        # skill CLI 密封桩测 + SKILL.md 文档守卫
├── scripts/
│   ├── mcp_server.py           # MCP stdio 适配器（缺省只读；交易工具需 XIADAN_MCP_TRADING=1）
│   ├── diagnose_settings.py     # 券商 UI 结构诊断脚本
│   ├── generate_templates.py    # OCR 模板管理（查看/提取/批量标注）
│   ├── train_ocr.py             # OCR 迭代训练（自动触发验证码 + 追踪准确率）
│   ├── test_*.py / explore_*.py # 探索与实验调试脚本（开发期遗留，手工运行）
│   └── legacy/                  # 从 tests/ 移出的一次性手工测试脚本（pytest 不收集）
├── .agents/
│   └── skills/
│       └── xiadan-gateway/      # agent skill（SKILL.md + CLI + API 参考，见「Skill 接入」）
├── assets/
│   ├── digit_templates/          # 数字模板（Git 跟踪，离线训练生成）
│   └── captcha_archive/          # 失败验证码存档（gitignore，供离线训练使用）
├── data/                        # 深交所日历缓存（运行时生成，gitignore）
├── logs/                        # 运行时生成（gitignore）
├── main.py                      # 启动入口（waitress + 单实例 + 优雅关闭 + 控制台 UTF-8）
└── pyproject.toml               # 依赖与构建配置
```

## 技术栈

| 组件 | 用途 |
|------|------|
| **Python 3.11+** / **uv** | 语言 / 包管理 |
| **Flask** | HTTP 路由（Blueprint 模块化） |
| **waitress** | 生产级 WSGI 服务器 |
| **pywinauto** (UIA) | 窗口/控件自动化 |
| **pywin32** | Windows API（按键、窗口、互斥锁） |
| **psutil** | 进程枚举与路径匹配 |
| **pyautogui** | 鼠标点击、全屏截图 |
| **ddddocr** (ONNX Runtime) | 可选，仅用于离线 OCR 训练脚本（`uv sync --extra ocr`） |
| **Pillow + NumPy** | 轻量 OCR 模板匹配引擎 |
| **chinesecalendar** | 交易时段预检的法定节假日判断（数据未覆盖时自动降级） |
| **pytest** | 单元测试 |

> 📅 **chinesecalendar 年度维护**：节假日数据随国务院发布节奏更新，覆盖次年数据的新版一般在**每年 11 月前后**发布。数据未覆盖当前年份时，交易时段预检自动切换深交所官方月度日历兜底（当年 12 个月一次性拉取并缓存到 `data/trading_calendar/`，缓存齐全后离线读取；API 也不可用才退回仅周末/工作日粗判）——仍建议每年 11 月新版发布后手动升级一次，保持无网络依赖：
>
> ```bash
> uv lock --upgrade-package chinesecalendar && uv sync
> ```
>
> 兜底数据源：深交所官方月度交易日历接口 `https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=YYYY-MM`（无 `month` 参数返回当月），逐日返回 `jybz`（1=交易日，0=非交易日）与 `zrxh`（1=周日…7=周六）；未来仅覆盖当年已公布月份，次年日历约每年 12 月发布后可查。2026 全年与 chinesecalendar 逐日比对一致（242 个交易日）。

## 开发

### 常用命令

```bash
uv run pytest                          # 运行全部测试
uv run pytest tests/test_core.py -v    # 运行单元测试
uv run --extra mcp pytest tests/test_mcp_server.py -v  # MCP 适配层测试（未装 extra 时自动跳过）
uv run python main.py --dev            # 开发模式（热加载）
uv run python scripts/diagnose_settings.py  # 券商 UI 结构诊断
uv run python scripts/generate_templates.py status  # OCR 模板覆盖状态
uv run python scripts/train_ocr.py     # OCR 迭代训练（自动触发验证码）
```

### 添加新的干净退出场景

分类由 `src/core/popup_rules.py` 规则表驱动，不需要改动 `TaskQueue` 或跳过逻辑：

**1. 在 `src/exceptions.py` 新增错误码：**
```python
NEW_ERROR = "NEW_ERROR"   # 描述
```

**2. 在 `src/core/popup_rules.py` 加一条规则**（弹窗动作表 `POPUP_RULES` 或错误码表 `SUBMIT_ERROR_RULES`）：
```python
PopupRule(
    _or(("某关键词", "另一关键词")),   # 任一 AND 组全部命中即匹配
    "raise_error",                    # 动作: raise_error / click_no / click_yes
    ErrorCode.NEW_ERROR,
    "描述: {text}",                   # {text} 占位，自动替换为弹窗文本
    "建议",
    clean_dismiss=True,               # 弹窗正常关闭，窗口状态可信，下次同组可跳过
),
```

**3. 在 `tests/test_core.py` 补参数化测试用例。**

> 规则表**顺序敏感**：多个规则共享关键词（如"可卖数量"同时出现在 T1 与 `INSUFFICIENT_SHARES`），顺序决定归属——新规则要放在正确的优先级位置，并补测试防止回归。
