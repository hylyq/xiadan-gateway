"""skill CLI（.agents/skills/xiadan-gateway/scripts/xiadan.py）单元测试

不启动真实网关/HTTP——桩掉 urllib.request.urlopen，覆盖:
- 子命令注册面：只读恒可用；buy/sell/cancel 随 XIADAN_MCP_TRADING 开关
- 交易前置校验（6 位代码/正整数数量/限价显式价格/2 位小数/市价禁价格）
- 下单语义映射（buy/sell→1/2、价格 2 位小数格式化、幂等键自动生成与复用）
- cancel 类型映射（all/buy/sell/last→A/X/C/L）
- 网关统一响应解包：成功取 data 打印 stdout；错误格式化 exit 1；连接失败 exit 3
- HTTP 契约：Bearer 认证（配置/环境变量）、无 Origin 头、Idempotency-Key 头
- SKILL.md 前置元数据与强制安全条款存在性（防文档漂移）
"""
import importlib.util
import json
import sys
import urllib.error
import uuid
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parent.parent
           / ".agents" / "skills" / "xiadan-gateway" / "scripts" / "xiadan.py")
_SKILL_DIR = _SCRIPT.parent.parent
# 指向不存在的配置文件，避免读到仓库真实 config/app_config.json（含真实 token）
_HERMETIC_CONFIG = "Z:/__no_such_config__.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("xiadan_skill_cli", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["xiadan_skill_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setenv("XIADAN_MCP_CONFIG", str(tmp_path / "no_config.json"))
    for var in ("XIADAN_MCP_URL", "XIADAN_MCP_TOKEN",
                "XIADAN_MCP_TRADING", "XIADAN_MCP_TIMEOUT_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    return _load_module()


@pytest.fixture
def cli_trading(cli, monkeypatch):
    monkeypatch.setenv("XIADAN_MCP_TRADING", "1")
    return cli


class FakeResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeUrlopen:
    """桩掉 urllib.request.urlopen：记录请求，按脚本回放响应/异常"""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.payload)


def _ok(data):
    return {"status": "success", "data": data}


def _err(code, message, **extra):
    return {"status": "error", "error_code": code, "message": message, **extra}


def run(cli, argv, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(argv)
    out = capsys.readouterr()
    return ei.value.code, out.out, out.err, out


# ============================================================
# 只读子命令与响应解包
# ============================================================

def test_health_success(cli, monkeypatch, capsys):
    fake = FakeUrlopen(_ok({"logged_in": True, "xiadan_running": True}))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    code, out, err, _ = run(cli, ["health"], capsys)

    assert code == 0
    assert json.loads(out) == {"logged_in": True, "xiadan_running": True}
    req = fake.requests[0]
    assert req.method == "GET"
    assert req.full_url == "http://127.0.0.1:5000/health"
    # 无认证配置时不带 Authorization；永不携带 Origin（跨站防御兼容）
    assert not req.headers.get("Authorization")
    assert "Origin" not in req.headers


def test_gateway_error_response_exits_1(cli, monkeypatch, capsys):
    fake = FakeUrlopen(_err("VALIDATION_ERROR", "code 参数不能为空",
                            suggestion="请提供股票代码", request_id="req_1"))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    code, out, err, _ = run(cli, ["balance"], capsys)

    assert code == 1
    assert out == ""
    assert "[VALIDATION_ERROR] code 参数不能为空" in err
    assert "建议: 请提供股票代码" in err
    assert "request_id: req_1" in err


def test_connection_failure_exits_3_with_startup_hint(cli, monkeypatch, capsys):
    fake = FakeUrlopen(error=urllib.error.URLError(OSError("connection refused")))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    code, out, err, _ = run(cli, ["health"], capsys)

    assert code == 3
    assert "无法连接交易网关" in err
    assert "uv run python main.py" in err


def test_http_error_fallback_exits_1(cli, monkeypatch, capsys):
    fake = FakeUrlopen(error=urllib.error.HTTPError(
        "http://127.0.0.1:5000/orders", 500, "Internal Error", None, None))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    code, _, err, _ = run(cli, ["positions"], capsys)

    assert code == 1
    assert "HTTP 500" in err


def test_order_status_path_and_validation(cli, monkeypatch, capsys):
    fake = FakeUrlopen(_ok({"found": True}))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    code, out, _, _ = run(cli, ["order-status", "6284424619"], capsys)
    assert code == 0
    assert fake.requests[0].full_url.endswith("/orders/6284424619/status")

    # 非数字合同编号在本地拒绝，不发请求
    fake.requests.clear()
    code, _, err, _ = run(cli, ["order-status", "62a44"], capsys)
    assert code == 2
    assert "纯数字" in err
    assert fake.requests == []


# ============================================================
# 交易开关：默认关闭
# ============================================================

def test_trading_commands_hidden_by_default(cli, monkeypatch, capsys):
    fake = FakeUrlopen(_ok({}))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    for argv in (["buy", "--code", "601991", "--amount", "100", "--price", "10.50"],
                 ["sell", "--code", "601991", "--amount", "100"],
                 ["cancel"]):
        code, _, err, _ = run(cli, argv, capsys)
        assert code == 2
        assert "XIADAN_MCP_TRADING" in err
    assert fake.requests == []  # 未发任何请求


# ============================================================
# 交易子命令：前置校验（不发请求）
# ============================================================

@pytest.mark.parametrize("argv, fragment", [
    (["buy", "--code", "60199", "--amount", "100", "--price", "10.50"], "6 位数字"),
    (["buy", "--code", "60199a", "--amount", "100", "--price", "10.50"], "6 位数字"),
    (["buy", "--code", "601991", "--amount", "0", "--price", "10.50"], "正整数"),
    (["sell", "--code", "601991", "--amount", "-100", "--price", "10.50"], "正整数"),
    (["buy", "--code", "601991", "--amount", "100"], "限价单必须显式指定"),
    (["buy", "--code", "601991", "--amount", "100", "--price", "10.505"], "2 位小数"),
    (["buy", "--code", "601991", "--amount", "100", "--price", "-1"], "正数"),
    (["buy", "--code", "601991", "--amount", "100", "--price", "10.50",
      "--price-type", "market"], "市价单不能指定"),
])
def test_order_validation_rejected_locally(cli_trading, monkeypatch, capsys,
                                           argv, fragment):
    fake = FakeUrlopen(_ok({}))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    code, _, err, _ = run(cli_trading, argv, capsys)

    assert code == 2
    assert fragment in err
    assert fake.requests == []


# ============================================================
# 交易子命令：成功路径与 HTTP 契约
# ============================================================

def test_buy_success_mapping_and_auto_idem_key(cli_trading, monkeypatch, capsys):
    fake = FakeUrlopen(_ok({"confirmed": True}))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    code, out, err, _ = run(cli_trading, ["buy", "--code", "601991",
                                          "--amount", "100", "--price", "10.5"], capsys)

    assert code == 0
    assert json.loads(out) == {"confirmed": True}
    req = fake.requests[0]
    assert req.method == "POST"
    assert req.full_url.endswith("/orders")
    body = json.loads(req.data.decode("utf-8"))
    assert body == {"code": "601991", "status": "1",
                    "amount": "100", "price": "10.50", "price_type": "limit"}
    # 自动生成的幂等键：请求头携带 + stderr 打印（供 agent 重试复用），uuid 可解析
    key = req.headers.get("Idempotency-key")
    assert key and len(key) == 36
    uuid.UUID(key)
    assert key in err
    assert "Idempotency-Key" in err


def test_sell_success_and_explicit_idem_key(cli_trading, monkeypatch, capsys):
    fake = FakeUrlopen(_ok({"confirmed": True}))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    code, _, err, _ = run(cli_trading, ["sell", "--code", "600000", "--amount", "200",
                                        "--price", "9.80", "--idem-key", "my-key-123"],
                          capsys)

    assert code == 0
    req = fake.requests[0]
    body = json.loads(req.data.decode("utf-8"))
    assert body["status"] == "2"          # sell → 2
    assert body["amount"] == "200"
    assert req.headers.get("Idempotency-key") == "my-key-123"
    assert "my-key-123" in err


@pytest.mark.parametrize("argv, expected_type", [
    (["cancel"], "A"),
    (["cancel", "all"], "A"),
    (["cancel", "buy"], "X"),
    (["cancel", "sell"], "C"),
    (["cancel", "last"], "L"),
])
def test_cancel_type_mapping(cli_trading, monkeypatch, capsys, argv, expected_type):
    fake = FakeUrlopen(_ok({"cancel_type": "全部撤单", "success": True,
                            "cancelled_count": 1}))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    code, out, _, _ = run(cli_trading, argv, capsys)

    assert code == 0
    req = fake.requests[0]
    assert req.full_url.endswith("/orders/cancel-all")
    assert json.loads(req.data.decode("utf-8")) == {"type": expected_type}
    # 按钮灰显（无可撤委托）success=false 也是正常响应，exit 0
    assert json.loads(out)["success"] is True


def test_cancel_grey_button_is_success(cli_trading, monkeypatch, capsys):
    fake = FakeUrlopen(_ok({"cancel_type": "全部撤单", "success": False,
                            "cancelled_count": 0}))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    code, out, _, _ = run(cli_trading, ["cancel"], capsys)

    assert code == 0
    assert json.loads(out)["success"] is False


# ============================================================
# 认证与网关地址
# ============================================================

def test_auth_token_from_config_file(cli, monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "app_config.json"
    cfg.write_text(json.dumps(
        {"host": "127.0.0.1", "port": 5100,
         "auth": {"enabled": True, "token": "secret-token"}}), encoding="utf-8")
    monkeypatch.setenv("XIADAN_MCP_CONFIG", str(cfg))
    fake = FakeUrlopen(_ok({}))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    code, _, _, _ = run(cli, ["balance"], capsys)

    assert code == 0
    req = fake.requests[0]
    assert req.full_url == "http://127.0.0.1:5100/account/balance"
    assert req.headers.get("Authorization") == "Bearer secret-token"


def test_auth_token_env_overrides_config(cli, monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "app_config.json"
    cfg.write_text(json.dumps(
        {"auth": {"enabled": True, "token": "from-config"}}), encoding="utf-8")
    monkeypatch.setenv("XIADAN_MCP_CONFIG", str(cfg))
    monkeypatch.setenv("XIADAN_MCP_TOKEN", "from-env")
    fake = FakeUrlopen(_ok({}))
    monkeypatch.setattr("urllib.request.urlopen", fake)

    run(cli, ["queue"], capsys)

    assert fake.requests[0].headers.get("Authorization") == "Bearer from-env"


# ============================================================
# SKILL.md / references 文档守卫（防漂移）
# ============================================================

def _frontmatter(text: str) -> dict:
    lines = text.splitlines()
    assert lines[0] == "---", "SKILL.md 必须以 YAML frontmatter 开头"
    end = lines.index("---", 1)
    fm = {}
    for ln in lines[1:end]:
        if ":" in ln:
            key, value = ln.split(":", 1)
            fm[key.strip()] = value.strip()
    return fm


def test_skill_md_frontmatter():
    text = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    fm = _frontmatter(text)
    assert fm.get("name") == "xiadan-gateway"      # 与目录名一致
    desc = fm.get("description", "")
    assert len(desc) >= 50
    # 触发词覆盖：查询与交易两类场景都要能触发
    for word in ("持仓", "撤单", "下单", "xiadan-gateway"):
        assert word in desc


def test_skill_md_safety_clauses_present():
    """SKILL.md 必须保留强制安全条款——删掉任何一条测试即红"""
    text = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    for fragment in (
        "logged_in",            # 登录门
        "逐字复述",             # 交易前人工确认工作流
        "XIADAN_MCP_TRADING",   # 交易开关
        "Idempotency-Key",      # 幂等键重试语义
        "TASK_TIMEOUT",         # 结果未知先查后试
        "DUPLICATE_ORDER",      # 重复提交不是故障
    ):
        assert fragment in text, f"SKILL.md 缺少安全条款: {fragment}"


def test_references_api_md_covers_key_contracts():
    text = (_SKILL_DIR / "references" / "api.md").read_text(encoding="utf-8")
    for fragment in (
        "/orders/cancel-all",       # 端点表
        "entrust_no",               # 响应字段语义
        "DUPLICATE_ORDER",          # 全量错误码
        "TASK_TIMEOUT_RECOVERY_FAILED",
        "Idempotency-Key",          # 幂等契约
        "60",                       # 去重窗口 60s
    ):
        assert fragment in text, f"references/api.md 缺少契约内容: {fragment}"


def test_cli_docstring_mentions_shared_env_and_exit_codes():
    """CLI 自述文档须写清与 MCP 共用的环境变量与退出码语义"""
    text = _SCRIPT.read_text(encoding="utf-8")
    for fragment in ("XIADAN_MCP_TRADING", "XIADAN_MCP_URL",
                     "退出码", "uv run --no-project"):
        assert fragment in text
