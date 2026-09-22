"""scripts/mcp_server.py（MCP 适配层）单元测试

不启动真实 MCP 服务/HTTP——桩掉 GatewayClient.call 或 urlopen，覆盖:
- 工具注册面：只读恒注册；交易工具随 XIADAN_MCP_TRADING 开关
- place_order 参数校验与语义映射（buy/sell→1/2、限价必填显式价格等）
- cancel_orders 类型映射（all/buy/sell/last→A/X/C/L）
- 网关统一响应解包：成功取 data；错误格式化为 ToolError
- 连接失败/HTTP 头（认证 Bearer、无 Origin）
"""
import asyncio
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

try:  # 与 scripts/mcp_server.py 相同的双版本兼容导入（保证异常类同源）
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:
    try:
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:  # 未安装 mcp extra 时整体跳过，保持裸 `uv run pytest` 绿
        pytest.skip("mcp 依赖未安装——用 `uv run --extra mcp pytest` 运行本文件",
                    allow_module_level=True)

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mcp_server.py"
# 指向不存在的配置文件，避免读到仓库真实 config/app_config.json（含真实 token）
_HERMETIC_CONFIG = "Z:/__no_such_config__.json"


def _load_module():
    """按当前环境变量重新加载适配器模块（工具注册发生在 import 期）"""
    sys.modules.pop("mcp_server", None)
    spec = importlib.util.spec_from_file_location("mcp_server", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mcp_server"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod(monkeypatch, tmp_path):
    monkeypatch.setenv("XIADAN_MCP_CONFIG", str(tmp_path / "no_config.json"))
    monkeypatch.delenv("XIADAN_MCP_TRADING", raising=False)
    return _load_module()


@pytest.fixture
def mod_trading(monkeypatch, tmp_path):
    monkeypatch.setenv("XIADAN_MCP_CONFIG", str(tmp_path / "no_config.json"))
    monkeypatch.setenv("XIADAN_MCP_TRADING", "1")
    return _load_module()


class FakeClient:
    """桩掉 GatewayClient.call，记录调用并按脚本回放响应/异常"""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def call(self, method, path, params=None, body=None, extra_headers=None):
        self.calls.append({"method": method, "path": path, "params": params,
                           "body": body, "headers": extra_headers})
        if not self.responses:
            raise AssertionError("FakeClient 队列已空，出现未预期的额外调用")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _tool_names(mod) -> set:
    tools = asyncio.run(mod.mcp.list_tools())
    return {t.name for t in tools}


def _call_tool(mod, name: str, args: dict):
    return asyncio.run(mod.mcp.call_tool(name, args))


def _tool_text(result) -> str:
    # mcp 2.x 返回 CallToolResult（.content 列表）；1.x 返回 list[Content]
    if hasattr(result, "content"):
        return result.content[0].text
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)
    first = result[0]
    return getattr(first, "text", str(first))


# ============================================================
# 工具注册面
# ============================================================

READ_TOOLS = {"gateway_health", "get_queue_status", "get_balance",
              "get_positions", "get_today_trades", "get_today_orders"}
TRADING_TOOLS = {"place_order", "cancel_orders"}


def test_readonly_tools_always_registered(mod):
    assert READ_TOOLS <= _tool_names(mod)


def test_trading_tools_disabled_by_default(mod):
    assert not (TRADING_TOOLS & _tool_names(mod))


def test_trading_tools_enabled_by_env(mod_trading):
    assert TRADING_TOOLS <= _tool_names(mod_trading)


@pytest.mark.parametrize("value", ["1", "true", "YES", "True"])
def test_trading_flag_truthy_variants(monkeypatch, tmp_path, value):
    monkeypatch.setenv("XIADAN_MCP_CONFIG", str(tmp_path / "no_config.json"))
    monkeypatch.setenv("XIADAN_MCP_TRADING", value)
    m = _load_module()
    assert TRADING_TOOLS <= _tool_names(m)


@pytest.mark.parametrize("value", ["0", "", "off", "no"])
def test_trading_flag_falsy_variants(monkeypatch, tmp_path, value):
    monkeypatch.setenv("XIADAN_MCP_CONFIG", str(tmp_path / "no_config.json"))
    monkeypatch.setenv("XIADAN_MCP_TRADING", value)
    m = _load_module()
    assert not (TRADING_TOOLS & _tool_names(m))


# ============================================================
# 只读工具透传
# ============================================================

def test_readonly_tool_calls_gateway(mod, monkeypatch):
    fake = FakeClient(responses=['{"rows": []}'])
    monkeypatch.setattr(mod, "client", fake)
    result = _call_tool(mod, "get_positions", {})
    assert _tool_text(result) == '{"rows": []}'
    assert fake.calls == [{"method": "GET", "path": "/positions",
                           "params": None, "body": None, "headers": None}]


# ============================================================
# place_order 校验与映射
# ============================================================

def _fake_for_place(mod, monkeypatch, responses=('"ok"',)):
    fake = FakeClient(responses=responses)
    monkeypatch.setattr(mod, "client", fake)
    return fake


def test_place_order_buy_mapping(mod_trading, monkeypatch):
    fake = _fake_for_place(mod_trading, monkeypatch)
    _call_tool(mod_trading, "place_order", {
        "code": "601991", "side": "buy", "amount": 100, "price": 10.5})
    assert fake.calls[0]["method"] == "POST"
    assert fake.calls[0]["path"] == "/orders"
    assert fake.calls[0]["body"] == {
        "code": "601991", "status": "1", "amount": "100",
        "price": "10.50", "price_type": "limit"}
    assert fake.calls[0]["headers"] is None


def test_place_order_sell_mapping(mod_trading, monkeypatch):
    fake = _fake_for_place(mod_trading, monkeypatch)
    _call_tool(mod_trading, "place_order", {
        "code": "000001", "side": "sell", "amount": 200,
        "price": 12.34, "price_type": "limit"})
    assert fake.calls[0]["body"]["status"] == "2"


def test_place_order_market_no_price(mod_trading, monkeypatch):
    fake = _fake_for_place(mod_trading, monkeypatch)
    _call_tool(mod_trading, "place_order", {
        "code": "601991", "side": "buy", "amount": 100,
        "price_type": "market"})
    assert fake.calls[0]["body"] == {
        "code": "601991", "status": "1", "amount": "100",
        "price_type": "market"}


def test_place_order_idempotency_key_header(mod_trading, monkeypatch):
    fake = _fake_for_place(mod_trading, monkeypatch)
    _call_tool(mod_trading, "place_order", {
        "code": "601991", "side": "buy", "amount": 100, "price": 10.5,
        "idempotency_key": "retry-abc"})
    assert fake.calls[0]["headers"] == {"Idempotency-Key": "retry-abc"}


@pytest.mark.parametrize("args,match", [
    ({"code": "60199", "side": "buy", "amount": 100, "price": 10.5}, "6 位数字"),
    ({"code": "6019a1", "side": "buy", "amount": 100, "price": 10.5}, "6 位数字"),
    ({"code": "601991", "side": "buy", "amount": 0, "price": 10.5}, "正整数"),
    ({"code": "601991", "side": "buy", "amount": -100, "price": 10.5}, "正整数"),
    ({"code": "601991", "side": "buy", "amount": 100, "price": 10.555}, "2 位小数"),
    ({"code": "601991", "side": "buy", "amount": 100}, "显式指定 price"),
    ({"code": "601991", "side": "buy", "amount": 100, "price": 10.5,
      "price_type": "market"}, "市价单不能指定"),
    ({"code": "601991", "side": "buy", "amount": 100, "price": 0}, "正数"),
])
def test_place_order_rejects_bad_args(mod_trading, monkeypatch, args, match):
    _fake_for_place(mod_trading, monkeypatch)
    with pytest.raises(Exception, match=match):
        _call_tool(mod_trading, "place_order", args)


# ============================================================
# cancel_orders 映射
# ============================================================

@pytest.mark.parametrize("arg,gateway_type", [
    ({}, "A"),
    ({"cancel_type": "all"}, "A"),
    ({"cancel_type": "buy"}, "X"),
    ({"cancel_type": "sell"}, "C"),
    ({"cancel_type": "last"}, "L"),
])
def test_cancel_orders_type_mapping(mod_trading, monkeypatch, arg, gateway_type):
    fake = FakeClient(responses=['"ok"'])
    monkeypatch.setattr(mod_trading, "client", fake)
    _call_tool(mod_trading, "cancel_orders", arg)
    assert fake.calls[0] == {"method": "POST", "path": "/orders/cancel-all",
                             "params": None, "body": {"type": gateway_type},
                             "headers": None}


# ============================================================
# GatewayClient：解包 / 错误格式化 / 请求头 / 配置解析
# ============================================================

class FakeHttpResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _client_with_env(monkeypatch, tmp_path, url="http://127.0.0.1:5000",
                     token=None):
    monkeypatch.setenv("XIADAN_MCP_CONFIG", str(tmp_path / "no_config.json"))
    monkeypatch.setenv("XIADAN_MCP_URL", url)
    if token is None:
        monkeypatch.delenv("XIADAN_MCP_TOKEN", raising=False)
    else:
        monkeypatch.setenv("XIADAN_MCP_TOKEN", token)
    return _load_module().GatewayClient()


def test_unwrap_success_returns_data_json(mod, monkeypatch, tmp_path):
    client = _client_with_env(monkeypatch, tmp_path)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return FakeHttpResponse({"status": "success", "request_id": "r1",
                                 "data": {"可用金额": "1000.00"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = client.call("GET", "/account/balance")
    assert json.loads(out) == {"可用金额": "1000.00"}
    req = captured["req"]
    assert req.get_header("Content-type") is None  # GET 无 body 不设 Content-Type
    assert req.headers.get("Origin") is None  # 跨站防御：绝不携带 Origin


def test_unwrap_error_raises_tool_error(mod, monkeypatch, tmp_path):
    client = _client_with_env(monkeypatch, tmp_path)

    def fake_urlopen(req, timeout=None):
        return FakeHttpResponse({
            "status": "error", "request_id": "r2",
            "error_code": "PRICE_OUT_OF_RANGE",
            "message": "价格超出涨跌停限制",
            "suggestion": "调整价格后重试",
            "screenshot": "logs/screenshots/x.png"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ToolError) as ei:
        client.call("POST", "/orders", body={"code": "601991"})
    text = str(ei.value)
    assert "[PRICE_OUT_OF_RANGE]" in text
    assert "价格超出涨跌停限制" in text
    assert "建议: 调整价格后重试" in text
    assert "request_id: r2" in text
    assert "logs/screenshots/x.png" in text


def test_connection_error_mentions_gateway_start(mod, monkeypatch, tmp_path):
    client = _client_with_env(monkeypatch, tmp_path, url="http://127.0.0.1:59999")

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ToolError, match="main.py"):
        client.call("GET", "/health")


def test_post_sends_bearer_and_content_type(mod, monkeypatch, tmp_path):
    client = _client_with_env(monkeypatch, tmp_path, token="secret-token")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return FakeHttpResponse({"status": "success", "data": {}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client.call("POST", "/orders", body={"code": "601991"},
                extra_headers={"Idempotency-Key": "k1"})
    req = captured["req"]
    assert req.get_header("Authorization") == "Bearer secret-token"
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("Idempotency-key") == "k1"
    assert req.headers.get("Origin") is None


def test_token_from_config_file(monkeypatch, tmp_path):
    cfg = tmp_path / "app_config.json"
    cfg.write_text(json.dumps({
        "host": "127.0.0.1", "port": 5100,
        "auth": {"enabled": True, "token": "cfg-token"}}), encoding="utf-8")
    monkeypatch.delenv("XIADAN_MCP_URL", raising=False)
    monkeypatch.delenv("XIADAN_MCP_TOKEN", raising=False)
    monkeypatch.setenv("XIADAN_MCP_CONFIG", str(cfg))
    client = _load_module().GatewayClient()
    assert client.base_url == "http://127.0.0.1:5100"
    assert client.token == "cfg-token"


def test_token_env_overrides_config(monkeypatch, tmp_path):
    cfg = tmp_path / "app_config.json"
    cfg.write_text(json.dumps({
        "auth": {"enabled": True, "token": "cfg-token"}}), encoding="utf-8")
    monkeypatch.setenv("XIADAN_MCP_URL", "http://10.0.0.2:5000")
    monkeypatch.setenv("XIADAN_MCP_TOKEN", "env-token")
    monkeypatch.setenv("XIADAN_MCP_CONFIG", str(cfg))
    client = _load_module().GatewayClient()
    assert client.base_url == "http://10.0.0.2:5000"
    assert client.token == "env-token"


def test_disabled_auth_yields_no_token(monkeypatch, tmp_path):
    cfg = tmp_path / "app_config.json"
    cfg.write_text(json.dumps({
        "auth": {"enabled": False, "token": "should-be-ignored"}}),
        encoding="utf-8")
    monkeypatch.setenv("XIADAN_MCP_CONFIG", str(cfg))
    client = _load_module().GatewayClient()
    assert client.token is None
