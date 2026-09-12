"""压力测试第一步：批量下 22 笔测试单（不成交价，梯度递减）"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.core.ocr import OcrService
from src.core.trader import Trader
from src.services.position_service import PositionService
from src.services.window_service import WindowService


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
    print(f"601991 市价: {market}")

    # 22 笔：从 市价×0.962 开始每次 -0.01（远离市价不成交，且高于跌停价）
    top = round(market * 0.962, 2)
    prices = [round(top - 0.01 * i, 2) for i in range(22)]
    print(f"委托价区间: {prices[0]} ~ {prices[-1]}")

    for i, price in enumerate(prices):
        r = trader.place_order(code="601991", status="1", amount="100",
                               price=str(price), price_type="limit",
                               confirm=False)
        no = r.get("entrust_no")
        print(f"  [{i + 1}/22] @{price} entrust_no={no}",
              flush=True)
        if not no:
            print("  [WARN] 本笔未截获委托号（不影响挂单本身）")


if __name__ == "__main__":
    main()
