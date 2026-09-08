"""直观对比演示：键盘法 vs 消息级复制（perf/wmcopy 分支）

同一进程内依次用两种方式执行真实的持仓/成交查询（模拟盘），
分阶段计时（导航 / 复制含验证码），最后输出对比表。

演示期间请看屏幕：F4 切面板 → 树导航切页 → 复制 → 验证码弹出
→ 自动 OCR 识别填写 → 数据入库，全程无需人工干预。
只读演示：仅查询，不下单。配置仅在内存中切换，不改配置文件。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.core.ocr import OcrService
from src.models.config import AppConfig
from src.services.position_service import PositionService
from src.services.window_service import WindowService

QUERIES = [
    ("持仓查询", "资金股票", "持仓", PositionService.POSITION_TABLE_COLUMNS),
    ("成交查询", "当日成交", "成交", PositionService.TRADES_TABLE_COLUMNS),
]


def query_timed(ps, ws, page, table, cols):
    """复刻生产调用序列，分阶段计时: F4+导航 / 复制(含验证码)"""
    t_all = time.perf_counter()
    ps._prepare_query_panel()
    ps._refresh_window_ref()
    window = ws.get_trading_window()
    t0 = time.perf_counter()
    ps._navigate_to_query_page(window, page)
    nav = time.perf_counter() - t0
    t0 = time.perf_counter()
    rows = ps._copy_table_verified(table, cols, page_name=page)
    copy_t = time.perf_counter() - t0
    total = time.perf_counter() - t_all
    return rows, nav, copy_t, total


def run_group(ps, ws, name):
    results = []
    for i, (label, page, table, cols) in enumerate(QUERIES):
        rows, nav, copy_t, total = query_timed(ps, ws, page, table, cols)
        tag = ""
        if name.startswith("消息级"):
            tag = "（含验证码路径学习）" if i == 0 else "（预测路径生效）"
        print(f"  ▶ {label}{tag}")
        print(f"    {len(rows)} 行 | 导航 {nav:.2f}s + 复制(含验证码) "
              f"{copy_t:.2f}s = {total:.2f}s", flush=True)
        results.append((label, nav, copy_t, total))
    return results


def main():
    ws = WindowService()
    ocr = OcrService.get_instance()
    ocr.configure(ddddocr_enabled=False)
    print("OCR 引擎预热...", flush=True)
    ocr.warmup()
    ps = PositionService(ws, ocr)
    cfg = AppConfig()

    print()
    print("█" * 62)
    print("█  第一组：键盘法（main 当前生产路径, keybd_event 双Ctrl+C）")
    print("█" * 62)
    kb = run_group(ps, ws, "键盘法")

    print()
    print("█" * 62)
    print("█  第二组：消息级复制（perf/wmcopy, WM_COMMAND 0xE122 免前台免键盘）")
    print("█" * 62)
    cfg._config["query"] = {"copy_method": "message"}  # 演示：仅内存切换
    try:
        msg = run_group(ps, ws, "消息级")
    finally:
        cfg._config["query"] = {"copy_method": "keyboard"}  # 恢复

    # 汇总
    print()
    print("=" * 62)
    print(f"{'查询':<10}{'阶段':<16}{'键盘法':>8}{'消息级':>8}{'变化':>10}")
    print("-" * 62)
    for i, label in enumerate(["持仓查询", "成交查询"]):
        kb_nav, kb_copy, kb_total = kb[i][1], kb[i][2], kb[i][3]
        msg_nav, msg_copy, msg_total = msg[i][1], msg[i][2], msg[i][3]
        print(f"{label:<10}{'导航':<16}{kb_nav:>7.2f}s{msg_nav:>7.2f}s{'—':>9}")
        delta = (msg_copy - kb_copy) / kb_copy * 100
        print(f"{label:<10}{'复制(含验证码)':<16}{kb_copy:>7.2f}s{msg_copy:>7.2f}s{delta:>+9.0f}%")
        delta_t = (msg_total - kb_total) / kb_total * 100
        print(f"{label:<10}{'端到端合计':<16}{kb_total:>7.2f}s{msg_total:>7.2f}s{delta_t:>+9.0f}%")
    print("=" * 62)
    print("说明: 两种方式导航阶段相同；消息级复制免前台激活/免真实键盘，")
    print("      验证码必弹但检测走预测路径缓存（首轮学习，次轮 ~10-50ms 命中）。")


if __name__ == "__main__":
    main()
