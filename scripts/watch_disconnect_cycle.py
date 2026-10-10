"""断网全周期监测：物理断网实测——状态栏/弹窗/系统网络态 双时间线

与 wait_and_probe_disconnect.py（触发即取证退出）不同，本脚本记录完整
断网→重连周期，用于回答：
  1. 断开后「断开」字样出现在哪个元素（类名/控件类型/auto_id/矩形/全文）
  2. 断网→UI 反应的延迟，以及重连后文本如何恢复（是否清掉「断开」）
  3. 报错弹窗是否必然出现（可否作为信号，还是只算附带现象）

系统网络真值：每 tick 对 223.5.5.5:443 做一次 TCP connect（连通时
<30ms、流量可忽略），配合网卡 isup 列表，给出与 UI 时间线对齐的
断网/恢复时刻。只读零输入：不点击/不发键/不激活。

输出 logs/netcycle_<ts>/trace.jsonl 逐行 flush（中途断网断电不丢），
关键转变（断网/恢复/断开字样出现与消失/新弹窗）自动桌面截图（上限 12 张）。

用法:
    .venv/Scripts/python.exe scripts/watch_disconnect_cycle.py \
        [--timeout 900] [--interval 0.5] [--full-every 10] [--out DIR]
    后台启动即可：观察到完整周期（断网+恢复+缓冲）自动退出并打
    DONE-CYCLE；到 --timeout 秒仍未断网则打 DONE-TIMEOUT 退出。
"""
import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import psutil
import pyautogui
import win32gui
import win32process
from pywinauto import Desktop

from src.utils.uia import safe_text

KEYWORDS = ("断开", "mncg", "连接", "电信", "联通", "移动", "错误", "失败", "超时")
DIALOG_CLASS = "#32770"
MAIN_TITLE_KEYWORD = "网上股票交易系统"
PROBE_IP_PORT = ("223.5.5.5", 443)


class Trace:
    def __init__(self, outdir):
        self.dir = outdir
        os.makedirs(outdir, exist_ok=True)
        self.f = open(os.path.join(outdir, "trace.jsonl"), "a",
                      encoding="utf-8")
        self.n_screens = 0

    def rec(self, kind, **kw):
        r = {"t": datetime.now().isoformat(timespec="milliseconds"),
             "mono": round(time.monotonic(), 3), "kind": kind, **kw}
        self.f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        self.f.flush()
        return r

    def shot(self, name):
        if self.n_screens >= 12:
            return None
        self.n_screens += 1
        p = os.path.join(self.dir, f"shot_{self.n_screens:02d}_{name}.png")
        try:
            pyautogui.screenshot(p)
            return p
        except Exception:
            return None


def net_probe():
    ups = []
    try:
        for name, s in psutil.net_if_stats().items():
            if s.isup and "loopback" not in name.lower():
                ups.append(name)
    except Exception:
        pass
    inet = False
    lat = None
    err = None
    try:
        c = socket.socket()
        c.settimeout(0.8)
        t0 = time.perf_counter()
        c.connect(PROBE_IP_PORT)
        lat = round((time.perf_counter() - t0) * 1000, 1)
        inet = True
        c.close()
    except Exception as e:
        err = f"{type(e).__name__}"
    return {"up_ifaces": ups, "inet": inet, "lat_ms": lat, "err": err}


def find_xiadan_pid():
    for proc in psutil.process_iter(["name", "pid"]):
        if (proc.info["name"] or "").lower() == "xiadan.exe":
            return proc.pid
    raise RuntimeError("xiadan.exe 未找到")


def find_main_hwnd(pid):
    out = []

    def cb(h, _):
        _, wpid = win32process.GetWindowThreadProcessId(h)
        if wpid == pid and win32gui.IsWindowVisible(h) \
                and MAIN_TITLE_KEYWORD in (win32gui.GetWindowText(h) or ""):
            out.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    if not out:
        raise RuntimeError("主窗口未找到")
    return out[0]


def enum_popups(pid):
    out = []

    def cb(h, _):
        try:
            _, wpid = win32process.GetWindowThreadProcessId(h)
            if wpid == pid and win32gui.IsWindowVisible(h) \
                    and win32gui.GetClassName(h) == DIALOG_CLASS:
                out.append({
                    "hwnd": h,
                    "title": win32gui.GetWindowText(h),
                    "rect": list(win32gui.GetWindowRect(h)),
                })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def elmeta(d):
    try:
        r = d.rectangle()
        rect = [r.left, r.top, r.right, r.bottom]
    except Exception:
        rect = None
    try:
        return {"name": safe_text(d) or "", "cls": d.class_name(),
                "ctype": d.element_info.control_type,
                "auto_id": d.element_info.automation_id, "rect": rect}
    except Exception as e:
        return {"error": str(e)}


def locate_status_area(win, tr):
    """全树找 mncg/断开 锚点元素，返回其父容器 wrapper（快照单位）"""
    try:
        desc = win.descendants()
    except Exception:
        return None
    for d in desc:
        try:
            name = safe_text(d) or ""
        except Exception:
            continue
        if "mncg" in name or "断开" in name:
            try:
                par = d.parent()
            except Exception:
                par = None
            tr.rec("locate", anchor=elmeta(d),
                   parent=elmeta(par) if par else None)
            return par or d
    return None


def snapshot_area(parent):
    """父容器全部直接子元素的当前文本/位置——「断开」新出现也能被抓到"""
    out = []
    try:
        kids = parent.children()
    except Exception as e:
        return [{"error": str(e)}]
    for k in kids:
        m = elmeta(k)
        if "error" not in m:
            out.append(m)
    return out


def full_scan(win):
    """全树关键词命中 + 窗口底部 60px 条带内全部带文本元素"""
    try:
        desc = win.descendants()
    except Exception as e:
        return {"error": str(e), "hits": [], "strip": [], "n": 0}
    hits = []
    strip = []
    try:
        mb = win.rectangle().bottom
    except Exception:
        mb = None
    for d in desc:
        try:
            name = safe_text(d) or ""
            cls = d.class_name()
            ctype = d.element_info.control_type
            aid = d.element_info.automation_id
            r = d.rectangle()
            rect = [r.left, r.top, r.right, r.bottom]
        except Exception:
            continue
        if any(k in name for k in KEYWORDS):
            hits.append({"name": name, "cls": cls, "ctype": ctype,
                         "auto_id": aid, "rect": rect})
        if mb and r.top >= mb - 60 and name:
            strip.append({"name": name, "cls": cls, "rect": rect})
    return {"hits": hits, "strip": strip, "n": len(desc)}


def dump_popup(tr, p, idx):
    try:
        dlg = Desktop(backend="uia").window(handle=p["hwnd"])
        items = []
        for d in dlg.descendants():
            m = elmeta(d)
            if "error" not in m:
                items.append(m)
        sp = tr.shot(f"popup{idx}")
        tr.rec("popup", hwnd=p["hwnd"], title=p["title"],
               rect=p["rect"], items=items, shot=sp)
        print(f"[watch] 弹窗出现 title={p['title']!r} "
              f"元素={len(items)} 截图={sp}", flush=True)
    except Exception as e:
        tr.rec("popup_error", hwnd=p["hwnd"], error=str(e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--full-every", type=int, default=10, dest="full_every")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = args.out or os.path.join(
        PROJECT_ROOT, "logs",
        f"netcycle_{datetime.now():%Y%m%d_%H%M%S}")
    tr = Trace(out)

    pid = find_xiadan_pid()
    hwnd = find_main_hwnd(pid)
    win = Desktop(backend="uia").window(handle=hwnd)
    print(f"[watch] pid={pid} main={hwnd:#x} out={out} 超时={args.timeout}s "
          f"间隔={args.interval}s 全扫每={args.full_every}tick", flush=True)
    tr.rec("start", pid=pid, hwnd=hwnd, timeout=args.timeout)

    # 初始定位 + 一次基线全扫
    status_parent = locate_status_area(win, tr)
    base = full_scan(win)
    tr.rec("full", phase="baseline", **base)

    deadline = time.time() + args.timeout
    tick = 0
    seen_popups = {p["hwnd"] for p in enum_popups(pid)}
    prev_inet = None
    prev_disc = None
    saw_disc = False
    saw_netdown = False
    end_at = None
    n_reloc = 0
    while time.time() < deadline:
        t_tick = time.time()
        tick += 1
        net = net_probe()

        pops = enum_popups(pid)
        for i, p in enumerate(pops):
            if p["hwnd"] not in seen_popups:
                seen_popups.add(p["hwnd"])
                dump_popup(tr, p, len(seen_popups))

        # 状态栏容器快照（失效则重定位）
        snap = snapshot_area(status_parent) if status_parent else None
        if snap and isinstance(snap[0], dict) and "error" in snap[0]:
            snap = None
        did_full = False
        if snap is None or tick % args.full_every == 0:
            fs = full_scan(win)
            tr.rec("full", phase="periodic", **fs)
            did_full = True
            if snap is None:
                n_reloc += 1
                status_parent = locate_status_area(win, tr)
                if status_parent:
                    snap = snapshot_area(status_parent)
                    if snap and isinstance(snap[0], dict) \
                            and "error" in snap[0]:
                        snap = None
                print(f"[watch] #{tick} 重定位状态栏容器 -> "
                      f"{'OK' if status_parent else '未找到'}", flush=True)

        txt = " ".join(m.get("name", "") for m in (snap or []))
        disc = "断开" in txt
        title = ""
        try:
            title = win32gui.GetWindowText(hwnd) or ""
        except Exception:
            pass
        tr.rec("sample", net=net, status=snap, disc=disc,
               title=title, npop=len(pops))

        # 转变事件
        if prev_inet is not None and net["inet"] != prev_inet:
            state = "net_up" if net["inet"] else "net_down"
            tr.rec("event", name=state, net=net)
            sp = tr.shot(state)
            print(f"[watch] #{tick} >>> {state} inet={net['inet']} "
                  f"截图={sp}", flush=True)
        if prev_disc is not None and disc != prev_disc:
            state = "disconnect_text_seen" if disc else "disconnect_text_gone"
            tr.rec("event", name=state, status=snap)
            sp = tr.shot(state)
            print(f"[watch] #{tick} >>> {state} 文本={txt!r} 截图={sp}",
                  flush=True)
        if net["inet"] is False:
            saw_netdown = True
        if disc:
            saw_disc = True
        prev_inet = net["inet"]
        prev_disc = disc

        # 收尾条件：断网已发生且网络恢复，缓冲后结束
        if saw_netdown and net["inet"] and end_at is None:
            end_at = time.time() + (45 if saw_disc else 60)
            print(f"[watch] 网络已恢复，{end_at - time.time():.0f}s 后收尾",
                  flush=True)
        if end_at and time.time() > end_at:
            tr.rec("done", reason="cycle", saw_disc=saw_disc,
                   saw_netdown=saw_netdown)
            fs = full_scan(win)
            tr.rec("full", phase="final", **fs)
            print("[watch] DONE-CYCLE", flush=True)
            return 0

        if tick % 40 == 0:
            print(f"[watch] #{tick} 心跳 inet={net['inet']} disc={disc} "
                  f"pop={len(pops)} 重定位={n_reloc}", flush=True)
        wait = args.interval - (time.time() - t_tick)
        if wait > 0:
            time.sleep(wait)
        if did_full:
            time.sleep(0.5)  # 全扫 tick 额外喘息，降低 UIA 压力

    tr.rec("done", reason="timeout", saw_disc=saw_disc,
           saw_netdown=saw_netdown)
    print("[watch] DONE-TIMEOUT: 未观察到断网周期", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
