"""端到端验证：copy_method=message 时的持仓/成交查询（perf/wmcopy 分支）

临时把 config/app_config.json 的 query.copy_method 切到 message，
走生产代码完整路径（导航 → 缓存表格句柄 → 消息级复制 → 验证码 OCR →
特征列验证），测完自动恢复原配置。

对照数据（2026-09-09 模拟盘）：
- 键盘法 get_position 连续 ~6.4s（README 复测）
- 键盘法 copy 阶段单独 5.54s
- 消息级 copy 阶段：免验证码 0.50s / 含验证码 ~3.8s
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config", "app_config.json")


def main():
    original = None
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            original = f.read()
        cfg = json.loads(original)
        cfg.setdefault("query", {})["copy_method"] = "message"
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("[OK] 已切换 query.copy_method=message\n")

        from src.core.ocr import OcrService
        from src.services.position_service import PositionService
        from src.services.window_service import WindowService

        ocr = OcrService.get_instance()
        ocr.configure(ddddocr_enabled=False)
        ocr.warmup()
        ps = PositionService(WindowService(), ocr)

        # 运行期间验证码会自动 OCR 处理，无需人工干预
        t0 = time.perf_counter()
        positions = ps.get_position()
        print(f"\n[持仓] {len(positions)} 行，耗时 {time.perf_counter() - t0:.2f}s")
        t0 = time.perf_counter()
        trades = ps.get_today_trades()
        print(f"[成交] {len(trades)} 行，耗时 {time.perf_counter() - t0:.2f}s")

        print("\n[抽样] 持仓首行字段:",
              sorted(positions[0].keys()) if positions else "（空表）")
    finally:
        if original is not None:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(original)
            print("\n[OK] 已恢复原配置 copy_method=keyboard")


if __name__ == "__main__":
    main()
