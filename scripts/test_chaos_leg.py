"""混沌腿演练：连续下单 + 用户随机瞬间断开 RDP——实测「下单中途断开」边界

用法: uv run python scripts/test_chaos_leg.py
流程: 启动后连续限价挂簿单（只挂不成交），用户任意挑一瞬关闭 RDP；
      断开后停止下单、等自愈，然后对账委托簿验证「每个错误响应的语义
      与订单真实状态一致」，最后撤单清理。

语义核对矩阵（核心）:
    POST 返回错误时，委托簿中是否存在该时间窗内的新委托？
    - SESSION_UNAVAILABLE → 必须不存在（任务开始前被拒，未执行）
    - ORDER_STATE_UNKNOWN → 存在与否都自洽（可能已提交）
    - 点击前 INTERNAL_ERROR/WINDOW_NOT_FOUND → 必须不存在
    - TASK_TIMEOUT → 存在与否都自洽（可能已执行）
    「必须不存在」类若实际存在 = bug，重点盯。
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, "scripts")
from test_idem_leg import ORDER, log, req, ui_available, wait_for  # noqa: E402

MAX_ORDERS = 30          # 30 × ~5.5s ≈ 165s 下单窗口（650 元/单，资金上限内）


def pending_orders():
    """查询当日委托；失败时显式报错退出——绝不能把「查询失败」当「空簿」

    （2026-10-09 实测教训：模拟盘在会话抖动+高频查询后触发验证码挑战，
    OCR 识别值与图片一致仍被客户端拒绝（输入/确认环节失败）→ 查询报错
    data=None，被 or [] 吞成「0 条」，差点误判为「成功单未达交易所」
    ——资金冻结金额才是那时的铁证；人工在客户端交互一次后挑战消失）
    """
    o, _ = req("GET", "/orders/pending")
    if o.get("status") != "success":
        log(f"当日委托查询失败: {o.get('error_code')} - {(o.get('message') or '')[:80]}")
        log("（常见原因：客户端验证码挑战 OCR 未通过——人工在客户端输入一次后重试）")
        sys.exit(2)
    return o.get("data") or []


def main():
    # 基线快照：区分既有委托（含已撤历史）与本轮新增
    baseline = {str(x.get("合同编号")) for x in pending_orders()
                if str(x.get("证券代码", "")).endswith("601288")}
    log(f"基线委托簿: 601288 既有 {len(baseline)} 条（合同编号集合已记录）")
    log(f"开始连续下单（最多 {MAX_ORDERS} 单）——请在此期间任意一瞬直接关闭 RDP 客户端")

    results = []  # (seq, key, t_start, t_end, status, error_code, ms)
    boundary = None
    for i in range(1, MAX_ORDERS + 1):
        key = f"chaos-{time.strftime('%H%M%S')}-{i}"
        t0 = time.strftime("%H:%M:%S")
        r, ms = req("POST", "/orders", ORDER, {"Idempotency-Key": key}, timeout=50)
        t1 = time.strftime("%H:%M:%S")
        ok = r.get("status") == "success"
        results.append((i, key, t0, t1, r.get("status"),
                        r.get("error_code"), round(ms)))
        if ok:
            log(f"#{i:02d} OK ({ms:.0f}ms) {t0}→{t1}")
            time.sleep(0.3)
            continue
        # 任意错误 = 边界事件（断开撞上下单/其他异常），记录并停止下单
        boundary = results[-1]
        log(f"#{i:02d} 边界! error={r.get('error_code')} ({ms:.0f}ms) {t0}→{t1}")
        log(f"    message: {(r.get('message') or '')[:100]}")
        break

    if boundary is None:
        log("未撞上断开（全部成功）——撤单清理后可重跑本脚本")
    else:
        # 等自愈（冷却期感知：上次自愈后 300s 内再断开，恢复可到 ~340s）
        wait_for(ui_available, "会话自愈恢复 Active", 420)

    # 对账：本轮新增委托
    time.sleep(2)
    final = [x for x in pending_orders()
             if str(x.get("证券代码", "")).endswith("601288")
             and str(x.get("合同编号")) not in baseline]
    log(f"对账: 本轮新增委托 {len(final)} 条")
    book = {}  # "HH:MM:SS" -> 记录（委托时间秒级匹配）
    for x in final:
        ts = str(x.get("委托时间", ""))
        book.setdefault(ts, []).append(x)
        log(f"    {ts} {x.get('合同编号')} {x.get('备注')} "
            f"撤{x.get('撤消数量')}/成{x.get('成交数量')}")

    # 语义核对
    def window_hits(t0, t1):
        """委托时间落在 POST 时间窗（含响应返回前后 2s 余量）内的委托数"""
        from datetime import datetime
        a = datetime.strptime(t0, "%H:%M:%S")
        b = datetime.strptime(t1, "%H:%M:%S")
        n = 0
        for ts, xs in book.items():
            t = datetime.strptime(ts, "%H:%M:%S")
            if a <= t <= b:
                n += len(xs)
        return n

    log("── 语义核对 ──")
    for (i, key, t0, t1, st, ec, ms) in results:
        if st == "success":
            n = window_hits(t0, t1)
            flag = "OK" if n >= 1 else "注意:成功但簿上未见(落账延迟?)"
            log(f"#{i:02d} success->簿{n}条 [{flag}]")
        else:
            n = window_hits(t0, t1)
            if ec == "SESSION_UNAVAILABLE":
                flag = "自洽" if n == 0 else "BUG:被门拒但委托实际存在"
            elif ec == "ORDER_STATE_UNKNOWN":
                flag = "自洽(可能已提交)" if n >= 1 else "自洽(实际未提交)"
            elif ec in ("TASK_TIMEOUT", "TASK_TIMEOUT_RECOVERY_FAILED"):
                flag = "自洽(可能已执行)" if n >= 1 else "自洽(实际未执行)"
            else:
                flag = "自洽(未执行)" if n == 0 else f"注意: {ec} 但委托存在，需复核"
            log(f"#{i:02d} {ec}->簿{n}条 [{flag}]")

    # 清理
    if final:
        c, _ = req("POST", "/orders/cancel-all", {"type": "A"})
        log(f"撤单清理: {json.dumps(c.get('data') or c, ensure_ascii=False)[:160]}")
    log("=== 混沌腿结束：可以重新 RDP 连入 ===")


if __name__ == "__main__":
    main()
