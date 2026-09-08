"""端到端验证：下单横幅截获委托号回传（feat/entrust-no）

内存中启用 order.capture_entrust_no → 真实下单（模拟盘，不成交价）→
校验 place_order 返回 entrust_no → 撤单清理。配置文件不落盘修改。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.models.config import AppConfig
from src.core.trader import Trader
from src.services.trading_service import TradingService
from src.services.window_service import WindowService


def main():
    cfg = AppConfig()
    cfg._config["order"] = {"capture_entrust_no": True,
                            "entrust_no_timeout_seconds": 3.0}
    ws = WindowService()
    trader = Trader(ws)

    print("===== 测试单（买入 601991 @5.14 ×100，capture_entrust_no=True）=====")
    try:
        result = trader.place_order(code="601991", status="1", amount="100",
                                    price="5.14", price_type="limit",
                                    confirm=False)
        print(f"  place_order 返回: {result}")
        entrust_no = result.get("entrust_no")
        if entrust_no:
            print(f"  [OK] 委托号回传成功: {entrust_no}")
        else:
            print("  [FAIL] 委托号未截获")
    except Exception as e:
        print(f"  [FAIL] 下单失败: {e}")
        return

    print("\n===== 第二笔（验证连续下单稳定性）=====")
    try:
        result2 = trader.place_order(code="601991", status="1", amount="100",
                                     price="5.15", price_type="limit",
                                     confirm=False)
        print(f"  place_order 返回: {result2}")
        print(f"  [OK] 委托号: {result2.get('entrust_no')}"
              if result2.get("entrust_no") else "  [FAIL] 委托号未截获")
    except Exception as e:
        print(f"  [FAIL] 下单失败: {e}")

    print("\n===== 清理：撤销测试委托 =====")
    try:
        r = TradingService(ws).cancel_all_orders("A")
        print(f"  撤单结果: {r}")
    except Exception as e:
        print(f"  [WARN] 撤单失败（请手动检查）: {e}")


if __name__ == "__main__":
    main()
