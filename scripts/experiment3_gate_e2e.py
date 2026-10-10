"""三轮断网实验：断连门控端到端验证（fail-closed 查询 + 自愈恢复）

前置：网关已运行（含 64e2bfd 门控代码）。阶段：
  1. baseline: 连接态 GET /positions（期望 200 数据）+ GET /health
  2. 等「断开」出现（或网断 45s 兜底）→ 断开态 +5s/+45s 各一次
     GET /positions（期望 error_code=BROKER_DISCONNECTED）+ GET /health
  3. 网络恢复 → +10s/+45s 各一次（期望自愈：200 数据；若首次仍
     BROKER_DISCONNECTED 属预期——重连进行中，第二次应成功）
  4. 「断开」清掉 + 20s 缓冲 → DONE-CYCLE

用法（网关需已启动）:
    .venv/Scripts/python.exe scripts/experiment3_gate_e2e.py \
        [--timeout 720] [--port 5000] [--out DIR]
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

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _load_token():
    cfg = json.load(open(os.path.join(PROJECT_ROOT, "config",
                                      "app_config.json"), encoding="utf-8"))
    a = cfg.get("auth") or {}
    return a.get("token", ""), bool(a.get("enabled"))


def _get(path, port, timeout_s=45, auth=True):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if auth:
        tok, enabled = _load_token()
        if enabled and tok:
            req.add_header("Authorization", f"Bearer {tok}")
    t0 = time.perf_counter()
    try:
        with _OPENER.open(req, timeout=timeout_s) as r:
            body = r.read(1200).decode("utf-8", "replace")
            return {"http": r.status,
                    "elapsed_s": round(time.perf_counter() - t0, 2),
                    "body": body[:400]}
    except urllib.error.HTTPError as e:
        return {"http": e.code,
                "elapsed_s": round(time.perf_counter() - t0, 2),
                "body": e.read(1200).decode("utf-8", "replace")[:400]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}",
                "elapsed_s": round(time.perf_counter() - t0, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=720)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = args.out or os.path.join(
        PROJECT_ROOT, "logs",
        f"netcycle3_{datetime.now():%Y%m%d_%H%M%S}")
    tr = Trace(out)

    pid = find_xiadan_pid()
    hwnd = find_main_hwnd(pid)
    win = Desktop(backend="uia").window(handle=hwnd)
    print(f"[exp3] pid={pid} main={hwnd:#x} out={out} port={args.port}",
          flush=True)
    tr.rec("start", pid=pid, hwnd=hwnd, port=args.port)

    status_parent = locate_status_area(win, tr)
    tr.rec("full", phase="baseline", **full_scan(win))

    phase = "baseline"
    baseline_done = False
    probe_schedule = []
    done_probes = {}
    net_down_since = None
    disc_seen_at = None
    restore_at = None
    disc_cleared_at = None
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

        for p in enum_popups(pid):
            if p["hwnd"] not in seen_popups:
                seen_popups.add(p["hwnd"])
                from watch_disconnect_cycle import dump_popup
                dump_popup(tr, p, len(seen_popups))

        snap = snapshot_area(status_parent) if status_parent else None
        if snap and isinstance(snap[0], dict) and "error" in snap[0]:
            snap = None
        if snap is None:
            status_parent = locate_status_area(win, tr)
            if status_parent:
                snap = snapshot_area(status_parent)
                if snap and isinstance(snap[0], dict) \
                        and "error" in snap[0]:
                    snap = None
        txt = " ".join(m.get("name", "") for m in (snap or []))
        disc = "断开" in txt
        tr.rec("sample", phase=phase, net=net, status=snap, disc=disc)

        # ---- 阶段推进 ----
        if phase == "baseline" and not baseline_done and snap is not None \
                and not disc and net["inet"]:
            r = _get("/positions", args.port)
            tr.rec("probe", label="baseline", ep="/positions", **r)
            print(f"[exp3] baseline /positions: http={r.get('http')} "
                  f"{r.get('elapsed_s')}s body={r.get('body', r.get('error',''))[:120]}",
                  flush=True)
            h = _get("/health", args.port, auth=False, timeout_s=10)
            tr.rec("probe", label="baseline", ep="/health", **h)
            baseline_done = True
            phase = "armed"
            print("[exp3] >>> 就绪，等待断网", flush=True)

        if net["inet"] is False:
            if net_down_since is None:
                net_down_since = now_mono
        else:
            if net_down_since is not None:
                tr.rec("event", name="net_up")
                print(f"[exp3] #{tick} >>> net_up", flush=True)
            net_down_since = None
        if prev_inet is not None and net["inet"] != prev_inet \
                and net["inet"] is False:
            tr.rec("event", name="net_down")
            tr.shot("net_down")
            print(f"[exp3] #{tick} >>> net_down", flush=True)
        prev_inet = net["inet"]

        if disc and disc_seen_at is None:
            disc_seen_at = now_mono
            tr.rec("event", name="disconnect_text_seen")
            tr.shot("disc_seen")
            print(f"[exp3] #{tick} >>> 断开字样出现，进入断开态探测", flush=True)
        if prev_disc is not None and disc != prev_disc and not disc \
                and disc_seen_at is not None and disc_cleared_at is None:
            disc_cleared_at = now_mono
            tr.rec("event", name="disconnect_text_gone")
            tr.shot("disc_gone")
            print(f"[exp3] #{tick} >>> 断开字样消失", flush=True)
        prev_disc = disc

        disconnected = disc_seen_at is not None or (
            net_down_since is not None and now_mono - net_down_since > 45)

        if disconnected and phase == "armed":
            phase = "disconnected"
            probe_schedule = [("disc+5s", now_mono + 5),
                              ("disc+45s", now_mono + 45)]
        if phase == "disconnected":
            for label, dl in probe_schedule:
                if label not in done_probes and now_mono >= dl:
                    r = _get("/positions", args.port)
                    done_probes[label] = r
                    tr.rec("probe", label=label, ep="/positions",
                           disc=disc, inet=net["inet"], **r)
                    print(f"[exp3] 断开态 {label}: http={r.get('http')} "
                          f"body={r.get('body', r.get('error',''))[:200]}",
                          flush=True)
                    h = _get("/health", args.port, auth=False, timeout_s=10)
                    tr.rec("probe", label=label, ep="/health", **h)
            if net["inet"] and net_down_since is None \
                    and all(l in done_probes for l, _ in probe_schedule):
                phase = "restored"
                restore_at = now_mono
                probe_schedule = [("restore+10s", now_mono + 10),
                                  ("restore+45s", now_mono + 45)]
                print("[exp3] >>> 网络恢复，进入自愈验证", flush=True)
        elif phase == "restored":
            for label, dl in probe_schedule:
                if label not in done_probes and now_mono >= dl:
                    r = _get("/positions", args.port)
                    done_probes[label] = r
                    tr.rec("probe", label=label, ep="/positions",
                           disc=disc, inet=net["inet"], **r)
                    print(f"[exp3] 恢复态 {label}: http={r.get('http')} "
                          f"body={r.get('body', r.get('error',''))[:200]}",
                          flush=True)
                    h = _get("/health", args.port, auth=False, timeout_s=10)
                    tr.rec("probe", label=label, ep="/health", **h)
            if disc_cleared_at is not None \
                    and all(l in done_probes for l, _ in probe_schedule) \
                    and now_mono - disc_cleared_at > 20:
                if restore_at and disc_cleared_at:
                    tr.rec("summary",
                           clear_latency_from_restore_s=round(
                               disc_cleared_at - restore_at, 1))
                tr.rec("done", reason="cycle")
                print("[exp3] DONE-CYCLE", flush=True)
                return 0

        if tick % 40 == 0:
            print(f"[exp3] #{tick} 心跳 phase={phase} inet={net['inet']} "
                  f"disc={disc}", flush=True)
        wait = args.interval - (time.time() - t_tick)
        if wait > 0:
            time.sleep(wait)

    tr.rec("done", reason="timeout", phase=phase)
    print("[exp3] DONE-TIMEOUT", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
