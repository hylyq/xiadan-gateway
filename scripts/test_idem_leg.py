"""幂等腿自动演练：断开期下单→快速拒绝→自愈后同 key 重试→核实→撤单

用法: uv run python scripts/test_idem_leg.py
前提: 网关运行中；启动本脚本后直接关闭 RDP 客户端即可，全程自动。
注意: 演练逻辑在 main() 内 + __main__ 守卫——import 本模块（复用 req）
不会触发演练（2026-10-09 实测被坑：无守卫版本 import 即等待断开）。

2026-10-09 实测结果（模拟盘 601288）:
    断开期下单 SESSION_UNAVAILABLE 9ms（任务未执行）
    断开期同 key 重试 SESSION_UNAVAILABLE 7ms（非 DUPLICATE_ORDER=记录已清）
    自愈 ~38s；恢复后同 key 重试 success 6021ms，委托真实到达交易所
    （合同编号 6292352441）；cancel-all 清理成功（券商侧异步确认 ~秒级）
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:5000"
CFG = "config/app_config.json"
ORDER = {"code": "601288", "status": "1", "amount": "100",
         "price": "6.50", "price_type": "limit"}  # 低 ~8% 限价：只挂簿不成交


def req(method, path, body=None, headers=None, timeout=45):
    h = {"Content-Type": "application/json"}
    token = json.load(open(CFG, encoding="utf-8")).get("auth", {}).get("token", "")
    if token:
        h["Authorization"] = f"Bearer {token}"
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    t0 = time.time()
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        out = json.loads(resp.read().decode())
    return out, (time.time() - t0) * 1000


def log(m):
    print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)


def ui_available():
    h, _ = req("GET", "/health", timeout=5)
    return bool((h.get("data") or {}).get("session", {}).get("ui_available", True))


def wait_for(cond, desc, timeout_s, interval=2):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if cond():
                log(f"等待完成: {desc}")
                return True
        except Exception:
            pass
        time.sleep(interval)
    log(f"等待超时({timeout_s}s): {desc}")
    return False


def main():
    # 阶段0：等用户断开
    log("等待 RDP 断开（最长 150s）——请现在直接关闭 RDP 客户端")
    if not wait_for(lambda: not ui_available(), "检出断开态", 150, 1):
        sys.exit(1)

    # 阶段1：断开期下单（期望毫秒级 SESSION_UNAVAILABLE，任务未执行）
    key = "idem-leg-" + time.strftime("%H%M%S")
    r1, ms1 = req("POST", "/orders", ORDER, {"Idempotency-Key": key})
    log(f"[1] 断开期下单: status={r1.get('status')} error={r1.get('error_code')} ({ms1:.0f}ms)")
    log(f"    message: {(r1.get('message') or '')[:90]}")

    # 阶段2：断开期同 key 立即重试（应再被 SESSION_UNAVAILABLE 拒——证明记录已清除；
    # 若 DUPLICATE_ORDER 则记录被误保留，是 bug）
    r2, ms2 = req("POST", "/orders", ORDER, {"Idempotency-Key": key})
    log(f"[2] 断开期同key重试: error={r2.get('error_code')} ({ms2:.0f}ms)"
        f"  ← SESSION_UNAVAILABLE=记录已清(正确) / DUPLICATE_ORDER=误保留(bug)")

    # 阶段3：等自愈
    if not wait_for(ui_available, "会话自愈恢复 Active", 300):
        sys.exit(1)

    # 阶段4：恢复后同 key 重试（期望挂簿成功而非 DUPLICATE_ORDER）
    r3, ms3 = req("POST", "/orders", ORDER, {"Idempotency-Key": key})
    log(f"[3] 恢复后同key重试: status={r3.get('status')} error={r3.get('error_code')} ({ms3:.0f}ms)")
    if r3.get("status") == "success":
        log(f"    下单成功: {json.dumps(r3.get('data'), ensure_ascii=False)[:200]}")

    # 阶段5：核实委托簿（注意 /orders/pending 的 data 直接是列表，含已撤记录）
    time.sleep(2)  # 券商侧落账有秒级延迟（实测 1.5s 查不到）
    o, _ = req("GET", "/orders/pending")
    orders = o.get("data") or []
    hit = [x for x in orders if str(x.get("证券代码", "")).endswith("601288")]
    log(f"[4] 委托簿核实: 共 {len(orders)} 条，其中 601288 委托 {len(hit)} 条")
    for x in hit[:3]:
        log(f"    {json.dumps(x, ensure_ascii=False)[:160]}")

    # 阶段6：撤单清理（异步确认，~秒级生效）
    c, _ = req("POST", "/orders/cancel-all", {"type": "A"})
    log(f"[5] 撤单: {json.dumps(c.get('data') or c, ensure_ascii=False)[:160]}")
    log("=== 幂等腿结束：现在可以重新 RDP 连入（自愈后 300s 保护窗内）===")


if __name__ == "__main__":
    main()
