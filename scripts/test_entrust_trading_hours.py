"""交易时段验证：委托号回传 + 横幅号与落表号一致性（feat/entrust-no 已合并）

流程：
1. 持仓查询取 601991 实时市价 → 推算不成交委托价（市价 × 0.97 / 0.96）
2. 连续 2 笔真实测试单，生产路径截获横幅委托号
3. 当日委托表按价格取真值（合同编号/委托编号双列），逐位核验
4. 撤单清理

凌晨维护窗口曾出现横幅号与落表号不一致——本脚本验证交易时段是否一致。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.core.ocr import OcrService
from src.core.trader import Trader
from src.models.config import AppConfig
from src.services.position_service import PositionService
from src.services.trading_service import TradingService
from src.services.window_service import WindowService


def get_market_price(ps):
    rows = ps.get_position()
    for row in rows:
        if row.get("证券代码") == "601991":
            return float(row["市价"])
    raise Exception(f"持仓中未找到 601991（当前持仓: {[r.get('证券代码') for r in rows]}）")


def main():
    cfg = AppConfig()
    cfg._config["order"] = {"capture_entrust_no": True,
                            "entrust_no_timeout_seconds": 3.0}
    ws = WindowService()
    ocr = OcrService.get_instance()
    ocr.configure(ddddocr_enabled=False)
    ocr.warmup()
    ps = PositionService(ws, ocr)
    trader = Trader(ws)

    market = get_market_price(ps)
    base = round(market * 0.97, 2)
    prices = [base, round(base - 0.01, 2)]
    print(f"[OK] 601991 市价 {market}，测试委托价 {prices}（低于市价，不会成交）\n")

    results = []
    for i, price in enumerate(prices):
        print(f"===== 第 {i + 1} 笔 买入 601991 @{price} ×100 =====")
        try:
            result = trader.place_order(code="601991", status="1", amount="100",
                                        price=str(price), price_type="limit",
                                        confirm=False)
            no = result.get("entrust_no")
            print(f"  entrust_no = {no}"
                  f"{'  [OK] 截获成功' if no else '  [FAIL] 未截获'}")
            if no:
                results.append((price, no))
        except Exception as e:
            print(f"  [FAIL] 下单失败: {e}")

    print("\n===== 真值核验（当日委托表）=====")
    all_ok = True
    for price, captured in results:
        truth = None
        for o in ps.get_today_orders():
            if o.get("证券代码") != "601991":
                continue
            try:
                if abs(float(o.get("委托价格", 0)) - float(price)) < 1e-6:
                    truth = o.get("合同编号") or o.get("委托编号")
                    break
            except (TypeError, ValueError):
                continue
        ok = (captured == truth) if truth else False
        all_ok &= ok
        print(f"  @{price}: 横幅号={captured} 落表号={truth} "
              f"{'一致' if ok else '不一致!'}")

    print("\n===== 清理：撤销测试委托 =====")
    try:
        r = TradingService(ws).cancel_all_orders("A")
        print(f"  撤单结果: {r}")
    except Exception as e:
        print(f"  [WARN] 撤单失败（请手动检查）: {e}")

    print(f"\n===== 结论：横幅号与落表号{'全部一致，交易时段验证通过' if all_ok else '存在不一致'} =====")


if __name__ == "__main__":
    main()
