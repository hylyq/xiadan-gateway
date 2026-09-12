"""按钮撤单端到端验证（feat/cancel-buttons）

1. 下 3 笔不成交限价单（市价 ×0.95/0.94/0.93）
2. cancel_all_orders("L") 撤最后 → 校验只有第 3 笔（@×0.93）被撤
3. cancel_all_orders("X") 撤买 → 清掉剩余两笔买入
全程从 F1 下单页直接操作，不切换 F3。
"""
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.core.ocr import OcrService
from src.core.trader import Trader
from src.services.position_service import PositionService
from src.services.trading_service import TradingService
from src.services.window_service import WindowService


def cancel_state(ps, price):
    """查某价格委托的撤单状态: 返回 (撤单数量int, 状态文本)"""
    for o in ps.get_today_orders():
        if o.get("证券代码") != "601991":
            continue
        try:
            if abs(float(o.get("委托价格", 0)) - float(price)) < 1e-6:
                cancelled = o.get("撤消数量") or o.get("撤单数量") or "0"
                try:
                    cancelled = int(float(cancelled))
                except ValueError:
                    cancelled = 100 if cancelled.strip() else 0
                return cancelled, o.get("状态", "?")
        except (TypeError, ValueError):
            continue
    return None, "未找到委托"


def main():
    ws = WindowService()
    ocr = OcrService.get_instance()
    ocr.configure(ddddocr_enabled=False)
    ocr.warmup()
    ps = PositionService(ws, ocr)
    trader = Trader(ws)

    market = None
    for row in ps.get_position():
        if row.get("证券代码") == "601991":
            market = float(row["市价"])
            break
    if market is None:
        raise Exception("持仓中未找到 601991")

    p1, p2, p3 = (round(market * f, 2) for f in (0.95, 0.94, 0.93))
    prices = [p1, p2, p3]
    print(f"[OK] 市价 {market}，三笔测试委托价: {prices}\n")

    for p in prices:
        trader.place_order(code="601991", status="1", amount="100",
                           price=str(p), price_type="limit", confirm=False)
    print(f"3 笔委托已提交: {prices}\n")

    ts = TradingService(ws)

    print("===== 撤最后（type=L）=====")
    r = ts.cancel_all_orders("L")
    print(f"  返回: {r}\n")
    time.sleep(0.5)

    c3, s3 = cancel_state(ps, p3)
    c1, _ = cancel_state(ps, p1)
    c2, _ = cancel_state(ps, p2)
    ok_l = (c3 or 0) > 0 and not (c1 or 0) and not (c2 or 0)
    print(f"  @×0.95: 撤消={c1}（应未撤）  @×0.94: 撤消={c2}（应未撤）  "
          f"@×0.93: 撤消={c3}（应已撤）")
    print(f"  [{'OK' if ok_l else 'FAIL'}] 撤最后语义校验\n")

    print("===== 撤买（type=X）=====")
    r = ts.cancel_all_orders("X")
    print(f"  返回: {r}\n")
    time.sleep(0.5)

    c1, _ = cancel_state(ps, p1)
    c2, _ = cancel_state(ps, p2)
    ok_x = (c1 or 0) > 0 and (c2 or 0) > 0
    print(f"  @×0.95: 撤消={c1}  @×0.94: 撤消={c2}")
    print(f"  [{'OK' if ok_x else 'FAIL'}] 撤买语义校验")


if __name__ == "__main__":
    main()
