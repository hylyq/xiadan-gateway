"""xiadan-gateway MCP 适配器（stdio transport）

大模型 agent（Claude Desktop / ZCode 等支持 MCP 的客户端）通过本适配器
以标准工具调用访问交易网关，无需了解 REST 细节：

    MCP 客户端 ──stdio──→ 本适配器 ──HTTP(127.0.0.1)──→ xiadan-gateway(Flask)

定位是「薄适配层」：
- 不 import src/ 任何模块、不进交易路径——网关的任务队列串行化、幂等、
  告警全部经由 HTTP 层自动继承
- 暴露面刻意收窄：只读查询恒注册；下单/撤单需 XIADAN_MCP_TRADING=1
  显式开启；/actions/* 裸 UI 操作与 /admin/* 永不暴露给 agent
- 认证复用网关 token（环境变量优先，缺省回落 config/app_config.json）

配置（环境变量，均可缺省）:
    XIADAN_MCP_URL              网关基地址，缺省读配置文件或 http://127.0.0.1:5000
    XIADAN_MCP_TOKEN            认证 token，缺省读配置文件 auth.token（enabled 时）
    XIADAN_MCP_CONFIG           配置文件路径，缺省 config/app_config.json
    XIADAN_MCP_TRADING          1/true=注册下单/撤单工具（缺省 0 只读）
    XIADAN_MCP_TIMEOUT_SECONDS  HTTP 超时秒数，缺省 60（网关推荐 ≥40）

启动:
    uv run --extra mcp python scripts/mcp_server.py

客户端注册示例（Claude Desktop / 其他 stdio MCP 客户端同理）:
    {"mcpServers": {"xiadan-gateway": {
        "command": "uv",
        "args": ["--directory", "C:/path/to/xiadan-gateway", "--extra", "mcp",
                 "run", "python", "scripts/mcp_server.py"],
        "env": {"XIADAN_MCP_TRADING": "1"}
    }}}
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal, Optional

try:
    try:
        from mcp.server.mcpserver import MCPServer as McpServer      # mcp 2.x
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:
        from mcp.server.fastmcp import FastMCP as McpServer          # mcp 1.x
        from mcp.server.fastmcp.exceptions import ToolError
except ImportError as e:  # pragma: no cover - 环境缺依赖时的引导
    raise SystemExit(
        "缺少 mcp 依赖——请用 `uv run --extra mcp python scripts/mcp_server.py` 启动"
    ) from e

DEFAULT_BASE_URL = "http://127.0.0.1:5000"
DEFAULT_TIMEOUT_SECONDS = 60

INSTRUCTIONS = """同花顺 xiadan.exe 交易网关（本服务是 HTTP API 的 MCP 薄适配层）。

操作准则:
1. 会话内首次操作前先 gateway_health() 确认 logged_in=true；为 false 时告知
   用户登录同花顺下单程序，不要重试交易类工具。
2. 查询类工具返回 JSON——基于返回内容解读，不要凭记忆编造数字。
3. place_order / cancel_orders 是实盘真实委托：调用前必须向用户逐字复述
   参数（代码/方向/数量/价格/类型）并取得明确同意；调用后用
   get_today_orders() 核对委托号与状态。
4. 错误以 [ERROR_CODE] message 形式返回，常见码:
   DUPLICATE_ORDER=60 秒内相同参数被幂等拦截（先查委托确认是否已提交）;
   TASK_TIMEOUT=结果未知（必须查 get_today_orders 核实，不可直接重试下单）;
   PRICE_OUT_OF_RANGE=价格超涨跌停; INSUFFICIENT_BALANCE/SHARES=资金或份额不足;
   T1_RESTRICTION=当日买入次日才可卖; ORDER_PRICE_REQUIRED=券商要求显式价格。
5. 网关单 worker 串行执行，任何工具耗时 2~10 秒属正常，勿因慢而并发重试。"""


def _eprint(*args) -> None:
    # stdout 是 MCP 协议通道，适配器自身日志/告警一律走 stderr
    print(*args, file=sys.stderr, flush=True)


def _load_gateway_config() -> dict:
    """读取网关配置文件（仅取 host/port/auth，缺失或损坏返回空 dict）"""
    env_path = os.environ.get("XIADAN_MCP_CONFIG")
    candidates = [Path(env_path)] if env_path else [
        Path("config") / "app_config.json",
        Path(__file__).resolve().parent.parent / "config" / "app_config.json",
    ]
    for cand in candidates:
        try:
            if cand.is_file():
                return json.loads(cand.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _eprint(f"[mcp] 配置文件读取失败，忽略: {cand}")
    return {}


class GatewayClient:
    """极简 HTTP 客户端：认证 + 统一响应解包 + 错误格式化为 ToolError"""

    def __init__(self):
        cfg = _load_gateway_config()
        base = os.environ.get("XIADAN_MCP_URL")
        if not base:
            base = f"http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 5000)}"
        self.base_url = base.rstrip("/")
        auth = cfg.get("auth", {}) if isinstance(cfg.get("auth"), dict) else {}
        self.token = (os.environ.get("XIADAN_MCP_TOKEN")
                      or (auth.get("token") if auth.get("enabled") else None))
        self.timeout = float(
            os.environ.get("XIADAN_MCP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))

    def call(self, method: str, path: str, params: Optional[dict] = None,
             body: Optional[dict] = None,
             extra_headers: Optional[dict] = None) -> str:
        """发起请求并解包网关统一响应

        成功返回 data 的 JSON 字符串；失败抛 ToolError（含错误码/建议/
        request_id），由 MCP 框架转为 isError 工具结果反馈给 agent。
        """
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = (json.dumps(body, ensure_ascii=False).encode("utf-8")
                if body is not None else None)
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra_headers:
            headers.update(extra_headers)
        # 刻意不携带 Origin 头——网关跨站防御会拒绝带 Origin 的请求
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 网关统一返回 HTTP 200（兼容 PowerShell 客户端），此分支仅为兜底
            raise ToolError(f"网关返回 HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise ToolError(
                f"无法连接交易网关 {self.base_url}: {e.reason}。"
                f"请先启动网关: uv run python main.py"
            ) from e
        except json.JSONDecodeError as e:
            raise ToolError(f"网关响应不是有效 JSON: {e}") from e
        return self._unwrap(payload)

    @staticmethod
    def _unwrap(payload: dict) -> str:
        if payload.get("status") == "success":
            return json.dumps(payload.get("data"), ensure_ascii=False)
        parts = [f"[{payload.get('error_code', 'UNKNOWN')}] "
                 f"{payload.get('message', '未知错误')}"]
        if payload.get("suggestion"):
            parts.append(f"建议: {payload['suggestion']}")
        if payload.get("request_id"):
            parts.append(f"request_id: {payload['request_id']}")
        if payload.get("screenshot"):
            parts.append(f"截图: {payload['screenshot']}")
        raise ToolError(" | ".join(parts))


mcp = McpServer("xiadan-gateway", instructions=INSTRUCTIONS)
client = GatewayClient()


# ============================================================
# 只读查询工具（恒注册，零风险）
# ============================================================

@mcp.tool()
def gateway_health() -> str:
    """网关健康检查：xiadan.exe 进程/登录态、队列状态、近 1 小时各错误码
    成功率、推荐客户端超时。会话内首次操作前先调用，确认 logged_in=true。"""
    return client.call("GET", "/health")


@mcp.tool()
def get_queue_status() -> str:
    """网关任务队列状态（排队任务数/是否空闲）。交易操作前可查看忙闲。"""
    return client.call("GET", "/queue/status")


@mcp.tool()
def get_balance() -> str:
    """查询资金余额（可用金额/冻结/市值等；control_id 直读，速度最快）。"""
    return client.call("GET", "/account/balance")


@mcp.tool()
def get_positions() -> str:
    """查询当前持仓明细（证券代码/名称/余额/成本价/盈亏等）。
    耗时约 3~8 秒；返回空列表通常表示无持仓。"""
    return client.call("GET", "/positions")


@mcp.tool()
def get_today_trades() -> str:
    """查询今日成交明细（成交时间/编号/价格/数量等）。耗时约 4~5 秒。"""
    return client.call("GET", "/trades/today")


@mcp.tool()
def get_today_orders() -> str:
    """查询当日全部委托（含已成交/已撤）。表头无独立状态列——委托状态通过
    「备注」（如"全部撤单"）与「撤消数量/成交数量」体现。"""
    return client.call("GET", "/orders/pending")


# ============================================================
# 交易工具（XIADAN_MCP_TRADING=1 才注册；描述内强制人工确认工作流）
# ============================================================

def _trading_enabled() -> bool:
    return os.environ.get("XIADAN_MCP_TRADING", "0").strip().lower() in ("1", "true", "yes")


def _register_trading_tools() -> None:
    if not _trading_enabled():
        return

    @mcp.tool()
    def place_order(
        code: str,
        side: Literal["buy", "sell"],
        amount: int,
        price: Optional[float] = None,
        price_type: Literal["limit", "market"] = "limit",
        idempotency_key: Optional[str] = None,
    ) -> str:
        """提交委托单——实盘真实下单，资金/持仓即时变动，不可撤销只能撤单。

        强制工作流（调用前必须完成，顺序不可省略）:
        1. get_positions() / get_balance() 确认可交易份额或资金充足
        2. 向用户逐字复述「代码/买入或卖出/数量/价格/限价或市价」，
           取得明确同意后才允许调用本工具
        3. 调用成功后用 get_today_orders() 核对委托号与状态并向用户汇报

        Args:
            code: 6 位证券代码，如 "601991"
            side: buy=买入，sell=卖出
            amount: 委托数量（股，正整数；A 股主板通常须为 100 的整数倍）
            price: 委托价格，最多 2 位小数。限价单必填且必须是用户给出的
                显式价格——禁止自行估算"最新价/现价"；市价单不填。
            price_type: limit=限价（默认），market=市价。实测该券商市价委托
                常被拒（ORDER_PRICE_REQUIRED），优先使用限价。
            idempotency_key: 可选幂等键（≤128 字符）。同一键 60 秒内重试
                不会重复下单——本工具超时后重试时请复用同一键。
        """
        code = (code or "").strip()
        if len(code) != 6 or not code.isdigit():
            raise ToolError(f"证券代码必须是 6 位数字，收到: {code!r}")
        if not isinstance(amount, int) or amount <= 0:
            raise ToolError(f"委托数量必须是正整数（股），收到: {amount}")
        if price_type == "market" and price is not None:
            raise ToolError(
                "市价单不能指定 price（由系统以最优价成交）；"
                "如需指定价格请改用 price_type='limit'"
            )
        if price_type == "limit":
            if price is None:
                raise ToolError(
                    "限价单必须显式指定 price——不接受「最新价/现价」等模糊语义，"
                    "请先向用户询问确切价格"
                )
            if round(price, 2) != price:
                raise ToolError(f"A 股价格最多 2 位小数，收到: {price}")
        if price is not None and price <= 0:
            raise ToolError(f"委托价格必须为正数，收到: {price}")

        body = {
            "code": code,
            "status": "1" if side == "buy" else "2",
            "amount": str(amount),
            "price_type": price_type,
        }
        if price is not None:
            body["price"] = f"{price:.2f}"
        headers = None
        if idempotency_key and idempotency_key.strip():
            headers = {"Idempotency-Key": idempotency_key.strip()}
        return client.call("POST", "/orders", body=body, extra_headers=headers)

    @mcp.tool()
    def cancel_orders(
        cancel_type: Literal["all", "buy", "sell", "last"] = "all",
    ) -> str:
        """撤销委托——实盘操作，调用前须向用户复述撤单范围并取得确认。

        Args:
            cancel_type: all=撤销全部委托（默认），buy=只撤买入委托，
                sell=只撤卖出委托，last=撤销最近一笔委托
        """
        mapping = {"all": "A", "buy": "X", "sell": "C", "last": "L"}
        return client.call("POST", "/orders/cancel-all",
                           body={"type": mapping[cancel_type]})


_register_trading_tools()


def main() -> None:
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    _eprint(f"[mcp] xiadan-gateway 适配器启动: base_url={client.base_url} "
            f"auth={'on' if client.token else 'off'} "
            f"trading={'on' if _trading_enabled() else 'off(只读)'} "
            f"timeout={client.timeout}s")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
