"""演示：免验证码复制（消息级复制偶发跳过验证码弹窗）

背景（2026-09-22 模拟盘实测）：查询表格复制多数情况必弹验证码，与发起
方式（键盘/消息级）无关；但间隔较久后的首笔复制偶发不触发——剪贴板
直接出数据，跳过整个验证码 OCR 流程。本脚本用三轮复制演示这一现象：

  第 1 轮（距上次复制间隔较久）→ 可能免验证码
  第 2 轮（紧跟第 1 轮）      → 大概率弹验证码（自动 OCR 解决）
  第 3 轮（间隔 60 秒后）     → 观察间隔是否重置券商限频

只读演示：仅查询页面切换与表格复制，不下单不撤单。
验证码弹出时自动 OCR 处理，无需人工干预。
"""
import sys
import os
import json
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config", "app_config.json")

GAP_SECONDS = 60  # 第 3 轮前的等待间隔

CAPTCHA_MARKERS = ("检测到验证码弹窗", "验证码 OCR", "输入验证码", "验证码验证成功")
FREE_MARKERS = ("消息级复制成功", "但剪贴板已有有效表格数据，直接使用")

QUERIES = []  # 运行时填充 [(名称, 方法), ...]


class RoundCollector(logging.Handler):
    """收集每轮查询产生的日志行（用于判定是否触发验证码）"""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records = []  # [( HH:MM:SS, message ), ...]

    def emit(self, record):
        self.records.append(
            (time.strftime("%H:%M:%S", time.localtime(record.created)),
             record.getMessage()))


def classify(records):
    """判定本轮复制结果：free / captcha / fallback"""
    msgs = [m for _, m in records]
    captcha = any(any(k in m for k in CAPTCHA_MARKERS) for m in msgs)
    free = (not captcha) and any(any(k in m for k in FREE_MARKERS) for m in msgs)
    fallback = any("回退键盘法" in m for m in msgs)
    return captcha, free, fallback


def show(round_no, name, gap_desc, collector, duration, rows):
    captcha, free, fallback = classify(collector.records)
    print()
    print("═" * 64)
    print(f"第 {round_no} 轮复制 · {name} · {gap_desc}")
    print("─" * 64)
    for ts, msg in collector.records:
        # 只展示与复制/验证码相关的关键行，过滤导航计时等噪音
        if any(k in msg for k in ("复制", "验证码", "剪贴板")):
            print(f"  {ts}  {msg}")
    if captcha:
        verdict = "🔐 触发验证码 → 已自动 OCR 识别并填写"
    elif free:
        verdict = "★ 免验证码复制 —— 剪贴板直接出数据，未弹任何弹窗"
    else:
        verdict = "？ 未观察到复制成功标记" + ("（已回退键盘法）" if fallback else "")
    print(f"  判定: {verdict}")
    print(f"  数据: {rows} 行 · 本轮耗时 {duration:.2f}s")
    print("═" * 64)
    return captcha, free


def main():
    import argparse

    parser = argparse.ArgumentParser(description="免验证码复制演示")
    parser.add_argument("--initial-wait", type=int, default=0,
                        help="首轮复制前先等待 N 秒（模拟间隔较久后的首笔）")
    parser.add_argument("--gap", type=int, default=60,
                        help="第 3 轮前的等待间隔（秒）")
    args = parser.parse_args()

    original = None
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            original = f.read()
        cfg = json.loads(original)
        cfg.setdefault("query", {})["copy_method"] = "message"
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

        from src.core.ocr import OcrService
        from src.services.position_service import PositionService
        from src.services.window_service import WindowService

        ocr = OcrService.get_instance()
        ocr.configure(ddddocr_enabled=False)
        ocr.warmup()
        ps = PositionService(WindowService(), ocr)

        collector = RoundCollector()
        logging.getLogger("xiadan_gateway").addHandler(collector)

        global GAP_SECONDS
        GAP_SECONDS = args.gap

        if args.initial_wait > 0:
            print(f"先等待 {args.initial_wait}s 再开始首轮复制（模拟长间隔）…")
            time.sleep(args.initial_wait)

        print("演示开始：观察每轮复制是否触发验证码")
        print(f"（第 3 轮前等待 {GAP_SECONDS}s，测试间隔是否重置券商验证码限频）")

        results = []
        last_copy_end = None
        for round_no, (name, fn) in enumerate(QUERIES, start=1):
            if round_no == 3:
                print(f"\n… 等待 {GAP_SECONDS}s（模拟间隔较久后的再次复制）…")
                time.sleep(GAP_SECONDS)

            gap_desc = "（首次复制，间隔较久）" if last_copy_end is None \
                else f"（距上次复制 {time.time() - last_copy_end:.0f} 秒）"
            collector.records.clear()

            t0 = time.perf_counter()
            rows = len(fn())
            duration = time.perf_counter() - t0
            last_copy_end = time.time()

            captcha, free = show(round_no, name, gap_desc, collector, duration, rows)
            results.append((round_no, name, "免验证码" if free else
                            ("触发验证码" if captcha else "未判定")))

        print("\n总结")
        for round_no, name, verdict in results:
            print(f"  第 {round_no} 轮 {name}: {verdict}")
    finally:
        if original is not None:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(original)
            print("\n[OK] 已恢复原配置")


if __name__ == "__main__":
    main()
