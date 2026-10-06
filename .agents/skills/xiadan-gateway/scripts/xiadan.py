"""xiadan-gateway skill CLI — 任意 LLM agent 操作本机交易网关的统一入口

    agent ──本 CLI──HTTP(127.0.0.1)──→ xiadan-gateway(Flask) ──→ xiadan.exe

定位与 scripts/mcp_server.py 相同的「薄适配层」：
- 纯标准库零依赖，不 import src/ 任何模块、不进交易路径——网关的任务
  队列串行化、幂等、告警全部经由 HTTP 层自动继承
- 暴露面分层：只读查询恒可用；buy/sell/cancel 需 XIADAN_MCP_TRADING=1
  显式开启（与 MCP 适配器共用同一开关）；/actions/* 与 /admin/* 永不暴露
- 认证复用网关 token（环境变量优先，缺省回落 config/app_config.json）

为什么是 CLI 而不是教 agent 手拼 curl：Windows 下 PowerShell 的 curl 是
Invoke-WebRequest 的别名、引号转义规则各 shell 不一，CLI 一次封装绕开。

环境变量（与 MCP 适配器通用，均可缺省）：
    XIADAN_MCP_URL              网关基地址，缺省读配置文件或 http://127.0.0.1:5000
    XIADAN_MCP_TOKEN            认证 token，缺省读配置文件 auth.token（enabled 时）
    XIADAN_MCP_CONFIG           配置文件路径，缺省 config/app_config.json
    XIADAN_MCP_TRADING          1/true=启用 buy/sell/cancel（缺省只读）
    XIADAN_MCP_TIMEOUT_SECONDS  HTTP 超时秒数，缺省 60（网关推荐 ≥40）

用法（在网关仓库内）：
    uv run python .agents/skills/xiadan-gateway/scripts/xiadan.py health
在任意目录（skill 装到用户级时）：
    uv run --no-project python <skill目录>/scripts/xiadan.py positions

退出码：0=成功  1=网关返回错误  2=用法/参数校验错误  3=无法连接网关
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

DEFAULT_TIMEOUT_SECONDS = 60

EXIT_OK = 0
EXIT_GATEWAY_ERROR = 1
EXIT_USAGE = 2
EXIT_CONNECT = 3

# 需 XIADAN_MCP_TRADING=1 才可用的子命令（与 MCP 交易工具同一开关）
TRADING_COMMANDS = ("buy", "sell", "cancel")

CANCEL_TYPE_MAP = {"all": "A", "buy": "X", "sell": "C", "last": "L"}

# 仓库根（本脚本位于 <repo>/.agents/skills/xiadan-gateway/scripts/）。
# skill 被复制/链接到用户级目录时该路径无 config，回落 cwd 或环境变量。
REPO_ROOT = Path(__file__).resolve().parents[4]


class GatewayError(Exception):
    """网关返回 error 响应或连接失败——message 已含错误码/建议/request_id"""

    def __init__(self, message: str, exit_code: int = EXIT_GATEWAY_ERROR):
        super().__init__(message)
        self.exit_code = exit_code


def _trading_enabled() -> bool:
    return os.environ.get("XIADAN_MCP_TRADING", "0").strip().lower() in ("1", "true", "yes")


def _load_gateway_config() -> dict:
    """读取网关配置文件（仅取 host/port/auth，缺失或损坏返回空 dict）"""
    env_path = os.environ.get("XIADAN_MCP_CONFIG")
    candidates = [Path(env_path)] if env_path else [
        REPO_ROOT / "config" / "app_config.json",
        Path("config") / "app_config.json",
    ]
    for cand in candidates:
        try:
            if cand.is_file():
                return json.loads(cand.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"[xiadan] 配置文件读取失败，忽略: {cand}", file=sys.stderr)
    return {}


class GatewayClient:
    """极简 HTTP 客户端：认证 + 统一响应解包 + 错误格式化为 GatewayError"""

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

    def call(self, method: str, path: str, body: Optional[dict] = None,
             extra_headers: Optional[dict] = None) -> dict:
        """发起请求并解包网关统一响应。

        成功返回 data（dict）；失败抛 GatewayError（含错误码/建议/
        request_id，exit_code 区分网关错误与连接失败）。
        """
        url = self.base_url + path
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
            raise GatewayError(f"网关返回 HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise GatewayError(
                f"无法连接交易网关 {self.base_url}: {e.reason}。"
                f"请先启动网关: uv run python main.py", EXIT_CONNECT) from e
        except json.JSONDecodeError as e:
            raise GatewayError(f"网关响应不是有效 JSON: {e}", EXIT_CONNECT) from e
        return self._unwrap(payload)

    @staticmethod
    def _unwrap(payload: dict) -> dict:
        if payload.get("status") == "success":
            return payload.get("data")
        parts = [f"[{payload.get('error_code', 'UNKNOWN')}] "
                 f"{payload.get('message', '未知错误')}"]
        if payload.get("suggestion"):
            parts.append(f"建议: {payload['suggestion']}")
        if payload.get("request_id"):
            parts.append(f"request_id: {payload['request_id']}")
        if payload.get("screenshot"):
            parts.append(f"截图: {payload['screenshot']}")
        raise GatewayError(" | ".join(parts))


# ============================================================
# 只读查询子命令（恒可用，零风险）
# ============================================================

READ_COMMANDS = {
    "health":   ("/health",          "健康检查：xiadan.exe 进程/登录态/队列/错误统计"),
    "queue":    ("/queue/status",    "任务队列状态（排队数/是否空闲）"),
    "balance":  ("/account/balance", "资金余额（可用/冻结/市值；速度最快）"),
    "positions": ("/positions",      "当前持仓明细（约 3~8 秒）"),
    "trades":   ("/trades/today",    "今日成交明细（约 4~5 秒）"),
    "orders":   ("/orders/pending",  "当日全部委托（含已成交/已撤）"),
}


# ============================================================
# 交易子命令参数（XIADAN_MCP_TRADING=1 才注册）
# ============================================================

def _add_order_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--code", required=True, metavar="CODE",
                   help="6 位证券代码，如 601991")
    p.add_argument("--amount", required=True, type=int, metavar="N",
                   help="委托数量（股，正整数；A 股主板通常须为 100 的整数倍）")
    p.add_argument("--price", type=float, default=None, metavar="P",
                   help="委托价格，最多 2 位小数。限价单必填且必须是用户给出的"
                        "显式价格——禁止自行估算「最新价/现价」；市价单不填")
    p.add_argument("--price-type", choices=["limit", "market"], default="limit",
                   help="limit=限价（默认），market=市价。实测该券商市价委托"
                        "常被拒（ORDER_PRICE_REQUIRED），优先使用限价")
    p.add_argument("--idem-key", default=None, metavar="KEY",
                   help="幂等键。缺省自动生成并打印到 stderr——超时后重试"
                        "必须复用同一键（同键重试会被拦截，新键=新订单）")


def _validate_order_args(code: str, amount: int, price: Optional[float],
                         price_type: str) -> None:
    """本地前置校验（与 MCP 适配器 place_order 同规则），失败 exit 2"""
    if len(code) != 6 or not code.isdigit():
        _die(f"证券代码必须是 6 位数字，收到: {code!r}")
    if not isinstance(amount, int) or amount <= 0:
        _die(f"委托数量必须是正整数（股），收到: {amount}")
    if price_type == "market" and price is not None:
        _die("市价单不能指定 --price（由系统以最优价成交）；"
             "如需指定价格请改用 --price-type limit")
    if price_type == "limit":
        if price is None:
            _die("限价单必须显式指定 --price——不接受「最新价/现价」等模糊语义，"
                 "请先向用户询问确切价格")
        if round(price, 2) != price:
            _die(f"A 股价格最多 2 位小数，收到: {price}")
    if price is not None and price <= 0:
        _die(f"委托价格必须为正数，收到: {price}")


def _die(message: str) -> None:
    print(f"[xiadan] {message}", file=sys.stderr)
    sys.exit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xiadan",
        description="xiadan-gateway 交易网关 CLI（skill 薄适配层，只读为缺省姿态）",
        epilog="交易子命令(buy/sell/cancel)需环境变量 XIADAN_MCP_TRADING=1；"
               "详细操作准则见本 skill 的 SKILL.md")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    for name, (path, helper) in READ_COMMANDS.items():
        sp = sub.add_parser(name, help=helper)
        if name == "health":
            sp.help = "健康检查：xiadan.exe 登录态/队列/错误统计（会话内首个命令）"

    ps = sub.add_parser("order-status",
                        help="按合同编号查委托状态+成交回报（约 8~15 秒）")
    ps.add_argument("entrust_no", metavar="NO",
                    help="纯数字合同编号（不同券商长度不同）")

    if _trading_enabled():
        for side, helper in (("buy", "买入委托（实盘）"), ("sell", "卖出委托（实盘）")):
            sp = sub.add_parser(side, help=helper)
            _add_order_args(sp)
        pc = sub.add_parser("cancel", help="撤单（实盘）")
        pc.add_argument("cancel_type", nargs="?", default="all",
                        choices=list(CANCEL_TYPE_MAP),
                        help="all=撤销全部（默认），buy=只撤买入，sell=只撤卖出，"
                             "last=撤销最近一笔")

    return parser


def _run_order(client: GatewayClient, side: str, args: argparse.Namespace) -> dict:
    _validate_order_args(args.code, args.amount, args.price, args.price_type)
    body = {
        "code": args.code,
        "status": "1" if side == "buy" else "2",
        "amount": str(args.amount),
        "price_type": args.price_type,
    }
    if args.price is not None:
        body["price"] = f"{args.price:.2f}"
    # 幂等键必填（API 契约）：未提供时自动生成——先打印再调用，
    # 保证超时/异常场景下 agent 也拿得到 key 用于重试
    key = (args.idem_key or "").strip() or str(uuid.uuid4())
    print(f"[xiadan] Idempotency-Key: {key}（超时重试必须复用此键，新键=新订单）",
          file=sys.stderr)
    return client.call("POST", "/orders", body=body,
                       extra_headers={"Idempotency-Key": key})


def main(argv=None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # 交易开关拦截放在 argparse 之前——buy/sell/cancel 未注册时,
    # argparse 的 "invalid choice" 报错对 agent 不友好
    if not _trading_enabled():
        hit = next((a for a in (argv if argv is not None else sys.argv[1:])
                    if a in TRADING_COMMANDS), None)
        if hit:
            _die(f"子命令 {hit!r} 是实盘交易操作，默认关闭。"
                 f"确需启用请设置环境变量 XIADAN_MCP_TRADING=1 "
                 f"（与 MCP 适配器共用该开关），并遵守 SKILL.md 的强制工作流")

    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help()
        sys.exit(EXIT_USAGE)

    client = GatewayClient()
    try:
        if args.command in READ_COMMANDS:
            data = client.call("GET", READ_COMMANDS[args.command][0])
        elif args.command == "order-status":
            if not args.entrust_no.isdigit():
                _die(f"合同编号必须是纯数字，收到: {args.entrust_no!r}")
            data = client.call("GET", f"/orders/{args.entrust_no}/status")
        elif args.command in ("buy", "sell"):
            data = _run_order(client, args.command, args)
        else:  # cancel（能走到这里说明交易开关已开）
            data = client.call("POST", "/orders/cancel-all",
                               body={"type": CANCEL_TYPE_MAP[args.cancel_type]})
    except GatewayError as e:
        print(f"[xiadan] {e}", file=sys.stderr)
        sys.exit(e.exit_code)

    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
