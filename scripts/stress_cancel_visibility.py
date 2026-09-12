"""压力测试第二步：可见边界测量 + 梯度位置按单撤单 + 误撤单完整性核验

1. F3 撤单页：行带检测得到可见行数 vs 表格复制得到总行数
2. 依次撤销不同位置的委托：顶部 / 中部 / 最后可见行 / 超出一屏（预期安全拒绝）
3. 每次撤销后完整性核验：目标编号消失 且 其余编号全部还在（误撤单检测）
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.core.ocr import OcrService
from src.models.config import AppConfig
from src.services.position_service import PositionService
from src.services.trading_service import TradingService
from src.services.window_service import WindowService

OUT = os.path.join("logs", "screenshots", "stress_cancel")


def main():
    ws = WindowService()
    ocr = OcrService.get_instance()
    ocr.configure(ddddocr_enabled=False)
    ocr.warmup()
    ps = PositionService(ws, ocr)
    ts = TradingService(ws, ps)

    ts.window_service.activate_window(AppConfig().get_trading_app_paths())
    window, _ = ts._open_cancel_interface()
    grid = ws.find_element_in_window(window, 1047)

    band_centers = ts._grid_row_band_centers(grid)
    print(f"[测量] 可见行带数: {len(band_centers)} (y={band_centers})")

    rows = ps.copy_current_grid()
    total = len(rows)
    print(f"[测量] 表格复制行数: {total}")

    os.makedirs(OUT, exist_ok=True)
    from PIL import ImageGrab
    rect = grid.rectangle()
    ImageGrab.grab(bbox=(rect.left, rect.top, rect.right,
                         min(rect.bottom, rect.top + 700))).save(
        os.path.join(OUT, "grid_full.png"))

    numbers = [r.get("合同编号") for r in rows]
    alive = set(numbers)

    # 目标位置：顶部 / 中部 / 最后可见 / 超出一屏
    last_visible = len(band_centers) - 1
    targets = [
        ("顶部 row0", 0),
        ("中部 row" + str(len(band_centers) // 2), len(band_centers) // 2),
        ("最后可见行 row" + str(last_visible), last_visible),
        ("第一超出行 row" + str(last_visible + 1), last_visible + 1),
    ]

    print("\n===== 梯度撤销测试 =====")
    for name, k in targets:
        if k >= total:
            print(f"[{name}] 行号超出总行数({total})，跳过")
            continue
        entrust_no = numbers[k]
        state = "可见区内" if k < len(band_centers) else "超出一屏（预期安全拒绝）"
        print(f"\n--- {name}（{state}）: {entrust_no} ---")
        try:
            r = ts.cancel_order(entrust_no)
            success = r.get("success")
            print(f"  cancel_order 返回: success={success}")
        except Exception as e:
            code = getattr(e, "error_code", "")
            success = False
            print(f"  返回异常: {code or e}")

        # 完整性核验：目标消失（成功时）且其余编号完好
        rows2 = ps.copy_current_grid()
        now_numbers = [r2.get("合同编号") for r2 in rows2]
        expect_alive = alive - {entrust_no} if success else alive
        actual_alive = set(now_numbers)
        missing_extra = expect_alive - actual_alive
        no_wrong_cancel = len(missing_extra) == 0
        target_gone = entrust_no not in actual_alive
        if success:
            ok = target_gone and no_wrong_cancel
            print(f"  [{'OK' if ok else 'FAIL'}] 目标消失={target_gone}，"
                  f"误撤={sorted(missing_extra) if missing_extra else '无'}")
        else:
            # 预期失败：目标应该还在
            print(f"  [{'OK' if entrust_no in actual_alive else 'FAIL'}] "
                  f"按预期拒绝且委托仍在")
        alive = actual_alive

    print("\n===== 清理：撤销剩余委托 =====")
    TradingService(ws).cancel_all_orders("A")
    print("已撤单清理")


if __name__ == "__main__":
    main()
