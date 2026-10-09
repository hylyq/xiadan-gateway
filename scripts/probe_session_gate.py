"""会话健康门演练探针——从 Session 0 观察整个断开→自愈→恢复时间线

用法:
    uv run python scripts/probe_session_gate.py [时长秒=240] [间隔秒=3] [日志路径]

轮询 GET /health（session 三键）与 GET /positions（门行为+耗时），
同时写 stdout 与日志文件（默认 logs/probe_session_gate.log）。
认证 token 直接从 config/app_config.json 读取（不回显）。

预期时间线（RDP 直接断开）:
    断开后数秒内   positions → SESSION_UNAVAILABLE（毫秒级，任务未执行）
                  health   → Disconnected / ui_available=false
    ~40s 后(自愈)  positions → OK（真实 UI 查询，3~8s）
    全程不应出现 TASK_TIMEOUT
"""
import datetime
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:5000"
CFG_PATH = r"C:\Users\Administrator\xiadan-gateway\config\app_config.json"
DEFAULT_LOG = r"C:\Users\Administrator\xiadan-gateway\logs\probe_session_gate.log"


def call(path: str, timeout: float):
    """GET 并返回 (json, 耗时ms)；网关统一 HTTP 200，业务错误也在 body"""
    req = urllib.request.Request(BASE + path)
    token = json.load(open(CFG_PATH, encoding="utf-8")) \
        .get("auth", {}).get("token", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8"))
    return body, (time.time() - t0) * 1000


def main() -> None:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 240
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 3
    log_path = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_LOG

    def log(msg: str) -> None:
        line = f"{datetime.datetime.now().strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    first_reject_at = None
    first_recover_at = None
    log(f"=== 探针启动：每 {interval}s 轮询，共 {duration:.0f}s ===")
    end = time.time() + duration
    while time.time() < end:
        # 1) /health：会话三键
        try:
            h, ms = call("/health", 5)
            s = (h.get("data") or {}).get("session") or {}
            hline = (f"health: {s.get('state_name')}/"
                     f"ui={s.get('ui_available')} ({ms:.0f}ms)")
        except Exception as e:
            hline = f"health: 请求失败 {e}"

        # 2) /positions：门行为 + 耗时
        try:
            p, ms = call("/positions", 45)
            if p.get("status") == "success":
                outcome = f"positions OK ({ms:.0f}ms)"
                if first_reject_at and not first_recover_at:
                    first_recover_at = time.time()
                    outcome += (f"  <<< 已恢复：首次拒绝→恢复成功 "
                                f"共 {first_recover_at - first_reject_at:.0f}s")
            else:
                if not first_reject_at:
                    first_reject_at = time.time()
                outcome = f"positions {p.get('error_code')} ({ms:.0f}ms)"
        except Exception as e:
            outcome = f"positions 异常: {e}"

        log(f"{hline}  |  {outcome}")
        time.sleep(interval)

    log("=== 探针结束 ===")


if __name__ == "__main__":
    main()
