"""探测验证码/风控弹窗的控件树（宽松版：进程内所有顶层窗口）

用法（必须在 VNC/RDP 桌面会话内运行，与 xiadan.exe 同会话）:
    uv run python scripts/probe_captcha_dialog.py

输出: logs/dialog_probe.txt（UTF-8）
- xiadan 进程所有顶层窗口: 标题/类名/句柄/可见性
- 非主窗口的弹窗: 全部控件 control_id/类名/类型/文本/矩形
- 主窗口: 仅直接子控件（弹窗若挂在主窗口内也能看到）
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import psutil
from pywinauto import Desktop

OUT = Path(__file__).resolve().parent.parent / "logs" / "dialog_probe.txt"
MAIN_TITLE_KEYWORD = "网上股票"


def _safe(fn, default="?"):
    try:
        return fn()
    except Exception as e:
        return f"{default}({type(e).__name__})"


def _dump_controls(lines, w, prefix="  "):
    desc = _safe(lambda: list(w.descendants()), None)
    if not isinstance(desc, list):
        lines.append(f"{prefix}<descendants 获取失败: {desc}>")
        return
    lines.append(f"{prefix}控件总数: {len(desc)}")
    for el in desc:
        rid = _safe(el.control_id)
        cls = _safe(el.class_name)
        txt = str(_safe(el.window_text, "") or "").strip()
        ctype = _safe(lambda e=el: e.element_info.control_type)
        rect = _safe(lambda e=el: e.rectangle())
        rect_s = (f"({rect.left},{rect.top},{rect.right},{rect.bottom})"
                  if hasattr(rect, "left") else str(rect))
        lines.append(
            f"{prefix}id={str(rid):>8}  class={str(cls):<26} type={str(ctype):<14} "
            f"rect={rect_s:<30} text={txt[:40]!r}")


def main():
    xiadan_pids = {p.pid for p in psutil.process_iter(["name"])
                   if (p.info["name"] or "").lower() == "xiadan.exe"}
    print(f"xiadan.exe 进程: {xiadan_pids or '未找到'}")

    lines = [f"xiadan pids: {xiadan_pids}"]
    tops = []
    for w in Desktop(backend="uia").windows():  # 不限可见性
        if xiadan_pids and _safe(w.process_id, None) not in xiadan_pids:
            continue
        tops.append(w)

    lines.append(f"xiadan 顶层窗口数: {len(tops)}")
    for i, w in enumerate(tops):
        title = str(_safe(w.window_text, "") or "")
        cls = str(_safe(w.class_name, "") or "")
        visible = _safe(w.is_visible, "?")
        lines.append("")
        lines.append(f"===== 窗口[{i}] title={title[:50]!r} class={cls!r} "
                     f"handle={w.handle:#x} visible={visible} =====")
        if MAIN_TITLE_KEYWORD in title:
            lines.append("  （主窗口，仅列直接子控件）")
            for ch in _safe(lambda: w.children(), []) or []:
                rid = _safe(ch.control_id)
                ccls = str(_safe(ch.class_name, "") or "")
                ctxt = str(_safe(ch.window_text, "") or "").strip()
                lines.append(f"  id={str(rid):>8}  class={ccls:<26} "
                             f"text={ctxt[:40]!r}")
                if ccls == "#32770":  # 子弹窗：全量 dump 控件树
                    lines.append(f"  ---- 子弹窗 {ccls} 控件树 ----")
                    _dump_controls(lines, ch, prefix="    ")
        else:
            _dump_controls(lines, w)

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写入 {OUT}（{len(lines)} 行）")


if __name__ == "__main__":
    main()
