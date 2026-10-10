"""二轮断网实验：断开态下自动调网关查询，观测操作链路行为与自愈

与 watch_disconnect_cycle.py（纯观测）不同，本脚本在断开态主动调
GET /positions（本地 127.0.0.1，不走代理），回答：
  1. 断开态下网关查询返回什么（错误码/报文/耗时——watchdog 是否打断）
  2. 查询期间是否出现 [主站]数据发送错误 等弹窗，出现/消失时刻
  3. 恢复网络后再查是否自愈（操作触发重连），状态格「断开」何时清掉

阶段: baseline(连接态查一次) → 等「断开」出现 → 断开态 +0/+40/+80s 各查一次
→ 等网络恢复 → 恢复 +10s/+45s 各查一次 → 等「断开」清掉 → 30s 缓冲收尾。
兜底: 网断了 45s 仍未出现「断开」字样也按断开态开始探测。

只读 UI + 只调 GET 查询（不下单/不撤单）。UI 采样与探测同 round 1：
0.5s 快照（TCP connect 223.5.5.5:443 真值 + 状态栏容器逐格 + 弹窗枚举），
5s 全树扫描。trace.jsonl 逐行 flush；查询结果单列 gw_query 记录。

用法（网关需已启动）:
    .venv/Scripts/python.exe scripts/experiment2_disconnect_op.py \
        [--timeout 900] [--port 5000] [--out DIR]
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from watch_disconnect_cycle import (Trace, enum_popups, find_main_hwnd,
                                    find_xiadan_pid, full_scan,
                                    locate_status_area, net_probe,
                                    snapshot_area)
from pywinauto import Desktop


def load_auth():
    cfg = json.load(open(os.path.join(PROJECT_ROOT, "config",
                                      "app_config.json"), encoding="utf-8"))
    a = cfg.get("auth") or {}
    return a.get("token", ""), bool(a.get("enabled"))


# 127.0.0.1 不走系统代理（空 ProxyHandler 覆盖环境代理设置）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call_positions(port, timeout_s=40):
    tok, enabled = load_auth()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/positions")
    if enabled and tok:
        req.add_header("Authorization", f"Bearer {tok}")
    t0 = time.perf_counter()
    try:
        with _OPENER.open(req, timeout=timeout_s) as r:
            body = r.read(2000).decode("utf-8", "replace")[:500]
            return {"http": r.status,
                    "elapsed_s": round(time.perf_counter() - t0, 2),
                    "body": body}
    except urllib.error.HTTPError as e:
        body = e.read(2000).decode("utf-8", "replace")[:500]
        return {"http": e.code,
                "elapsed_s": round(time.perf_counter() - t0, 2),
                "body": body}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}",
                "elapsed_s": round(time.perf_counter() - t0, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--full-every", type=int, default=10, dest="full_every")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = args.out or os.path.join(
        PROJECT_ROOT, "logs",
        f"netcycle2_{datetime.now():%Y%m%d_%H%M%S}")
    tr = Trace(out)

    pid = find_xiadan_pid()
    hwnd = find_main_hwnd(pid)
    win = Desktop(backend="uia").window(handle=hwnd)
    print(f"[exp2] pid={pid} main={hwnd:#x} out={out} port={args.port}",
          flush=True)
    tr.rec("start", pid=pid, hwnd=hwnd, port=args.port)

    status_parent = locate_status_area(win, tr)
    base = full_scan(win)
    tr.rec("full", phase="baseline", **base)

    # 状态机
    phase = "baseline"          # baseline -> armed -> disconnected -> restored
    baseline_done = False
    probe_schedule = []         # [(label, deadline_mono)]
    done_probes = {}
    net_down_since = None
    disc_seen_at = None
    restore_at = None
    disc_cleared_at = None
    n_reloc = 0
    seen_popups = {p["hwnd"] for p in enum_popups(pid)}
    prev_inet = None
    prev_disc = None

    deadline = time.time() + args.timeout
    tick = 0
    while time.time() < deadline:
        t_tick = time.time()
        tick += 1
        net = net_probe()
        now_mono = time.monotonic()

        for i, p in enumerate(enum_popups(pid)):
            if p["hwnd"] not in seen_popups:
                seen_popups.add(p["hwnd"])
                from watch_disconnect_cycle import dump_popup
                dump_popup(tr, p, len(seen_popups))

        snap = snapshot_area(status_parent) if status_parent else None
        if snap and isinstance(snap[0], dict) and "error" in snap[0]:
            snap = None
        if snap is None or tick % args.full_every == 0:
            fs = full_scan(win)
            tr.rec("full", phase=phase, **fs)
            if snap is None:
                n_reloc += 1
                status_parent = locate_status_area(win, tr)
                if status_parent:
                    snap = snapshot_area(status_parent)
                    if snap and isinstance(snap[0], dict) \
                            and "error" in snap[0]:
                        snap = None
                print(f"[exp2] #{tick} 重定位 -> {'OK' if status_parent else 'X'}",
                      flush=True)
        txt = " ".join(m.get("name", "") for m in (snap or []))
        disc = "断开" in txt
        tr.rec("sample", phase=phase, net=net, status=snap, disc=disc,
               npop=0)

        # ---- 状态机推进 ----
        if phase == "baseline" and not baseline_done and snap is not None \
                and not disc and net["inet"]:
            r = call_positions(args.port)
            tr.rec("gw_query", label="baseline", disc_before=disc, **r)
            print(f"[exp2] baseline 查询: {r}", flush=True)
            baseline_done = True
            phase = "armed"
            print("[exp2] >>> 就绪，等待断网（断开态自动探测）", flush=True)

        if net["inet"] is False:
            if net_down_since is None:
                net_down_since = now_mono
        else:
            if net_down_since is not None:
                tr.rec("event", name="net_up")
                print(f"[exp2] #{tick} >>> net_up", flush=True)
            net_down_since = None
        if prev_inet is not None and net["inet"] != prev_inet \
                and net["inet"] is False:
            tr.rec("event", name="net_down")
            tr.shot("net_down")
            print(f"[exp2] #{tick} >>> net_down", flush=True)
        prev_inet = net["inet"]

        if disc and disc_seen_at is None:
            disc_seen_at = now_mono
            tr.rec("event", name="disconnect_text_seen")
            tr.shot("disc_seen")
            print(f"[exp2] #{tick} >>> 断开字样出现，进入断开态探测", flush=True)
        if prev_disc is not None and disc != prev_disc and not disc \
                and disc_seen_at is not None and disc_cleared_at is None:
            disc_cleared_at = now_mono
            tr.rec("event", name="disconnect_text_gone")
            tr.shot("disc_gone")
            print(f"[exp2] #{tick} >>> 断开字样消失", flush=True)
        prev_disc = disc

        # 断开态判定：字样出现，或网断持续 45s 仍无字样（兜底）
        disconnected = disc_seen_at is not None or (
            net_down_since is not None and now_mono - net_down_since > 45)

        if disconnected and phase == "armed":
            phase = "disconnected"
            probe_schedule = [("disc+0s", now_mono),
                              ("disc+40s", now_mono + 40),
                              ("disc+80s", now_mono + 80)]
        if phase == "disconnected":
            for label, dl in probe_schedule:
                if label not in done_probes and now_mono >= dl:
                    r = call_positions(args.port)
                    done_probes[label] = r
                    tr.rec("gw_query", label=label,
                           disc=disc, inet=net["inet"], **r)
                    print(f"[exp2] 断开态查询 {label}: {r}", flush=True)
            if net["inet"] and net_down_since is None \
                    and all(l in done_probes for l, _ in probe_schedule):
                phase = "restored"
                restore_at = now_mono
                probe_schedule = [("restore+10s", now_mono + 10),
                                  ("restore+45s", now_mono + 45)]
                print("[exp2] >>> 网络已恢复，进入恢复态探测", flush=True)
        elif phase == "restored":
            for label, dl in probe_schedule:
                if label not in done_probes and now_mono >= dl:
                    r = call_positions(args.port)
                    done_probes[label] = r
                    tr.rec("gw_query", label=label,
                           disc=disc, inet=net["inet"], **r)
                    print(f"[exp2] 恢复态查询 {label}: {r}", flush=True)
            if disc_cleared_at is not None and \
                    all(l in done_probes for l, _ in probe_schedule) \
                    and now_mono - disc_cleared_at > 30:
                if restore_at and disc_cleared_at:
                    tr.rec("summary",
                           clear_latency_from_restore_s=round(
                               disc_cleared_at - restore_at, 1))
                tr.rec("done", reason="cycle")
                print("[exp2] DONE-CYCLE", flush=True)
                return 0

        if tick % 40 == 0:
            print(f"[exp2] #{tick} 心跳 phase={phase} inet={net['inet']} "
                  f"disc={disc} 重定位={n_reloc}", flush=True)
        wait = args.interval - (time.time() - t_tick)
        if wait > 0:
            time.sleep(wait)
        if snap is None or tick % args.full_every == 0:
            time.sleep(0.5)

    tr.rec("done", reason="timeout", phase=phase)
    print("[exp2] DONE-TIMEOUT", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
