"""OCR 迭代训练脚本

核心: 每次验证码都自动存档 + 提取模板（服务器默认行为）。
此脚本只负责触发查询 + 监控进度，直到 20 次连续正确。

用法:
  uv run python scripts/train_ocr.py
"""
import os
import sys
import time
import json
import glob
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_URL = "http://127.0.0.1:5000"
TARGET = 166

ENDPOINTS = [
    ("GET", "/positions", "pos"),
    ("GET", "/trades/today", "trades"),
    ("GET", "/orders/pending", "orders"),
]


def api(method, path):
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def get_disk_state():
    """从磁盘读取训练状态"""
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    templates = glob.glob(os.path.join(project, "assets", "digit_templates", "real_*.png"))
    coverage = set()
    for t in templates:
        name = os.path.basename(t)
        parts = name.split("_")
        if len(parts) >= 2 and parts[0] == "real":
            try:
                d = int(parts[1])
                if 0 <= d <= 9:
                    coverage.add(d)
            except ValueError:
                pass
    archives = glob.glob(os.path.join(project, "assets", "captcha_archive", "*.png"))
    return {
        "templates": len(templates),
        "coverage": sorted(coverage),
        "coverage_count": len(coverage),
        "missing": sorted(set(range(10)) - coverage),
        "archives": len(archives),
    }


def check_latest_captcha():
    """对最新 captcha 做轻量 vs ddddocr 对比（ddddocr 可选）"""
    try:
        import ddddocr
    except ImportError:
        print("ddddocr 未安装，跳过质检对比。安装: uv sync --extra ocr")
        return None, None, None

    from src.core.ocr_lightweight import LightweightCaptchaOCR

    captcha_path = "logs/screenshots/captcha.png"
    if not os.path.exists(captcha_path):
        return None, None, None

    with open(captcha_path, "rb") as f:
        data = f.read()

    dddd = ddddocr.DdddOcr(show_ad=False)
    truth = "".join(filter(str.isdigit, dddd.classification(data) or ""))

    lw = LightweightCaptchaOCR()
    lw.warmup()
    light = "".join(filter(str.isdigit, lw.recognize_bytes(data) or ""))

    match = (len(truth) == 4 and light == truth)
    return light, truth, match


def fmt(sec):
    return f"{int(sec//60)}m{int(sec%60)}s"


def main():
    print("=" * 60)
    print("  OCR 训练")
    print(f"  目标: 连续 {TARGET} 次正确 + 10/10 覆盖")
    print(f"  每次验证码自动: 存档 + 提取模板 + 实时更新")
    print("=" * 60)

    health = api("GET", "/health")
    if health.get("status") != "success":
        print(f"[ERROR] 服务未启动: {health}")
        return
    print("服务已就绪\n")

    state = get_disk_state()
    print(f"初始: 覆盖 {state['coverage_count']}/10, "
          f"模板 {state['templates']}, 存档 {state['archives']}")

    consecutive = 0
    best = 0
    total_ok = 0
    total_chk = 0
    round_num = 0
    start = time.time()

    try:
        while consecutive < TARGET or state["coverage_count"] < 10:
            round_num += 1
            method, path, label = ENDPOINTS[round_num % 3]

            t0 = time.time()
            api(method, path)
            dur = time.time() - t0
            time.sleep(0.5)

            # 磁盘状态
            state = get_disk_state()

            # 轻量 vs ddddocr 对比
            light, truth, match = check_latest_captcha()
            if truth and len(truth) == 4:
                total_chk += 1
                if match:
                    consecutive += 1
                    total_ok += 1
                else:
                    if consecutive:
                        print(f"  !! 连续 {consecutive} 次中断! "
                              f"轻量={light} 标准={truth}")
                    consecutive = 0
                if consecutive > best:
                    best = consecutive

            bar = "#" * min(consecutive, 20) + "-" * max(0, 20 - consecutive)
            elapsed = int(time.time() - start)
            m = "OK" if match else ("XX" if match is False else "??")
            print(f"  [{round_num:03d}] {label:6s} | {m} {light}={truth} | "
                  f"连对:{consecutive:2d}/{TARGET} [{bar}] | "
                  f"覆盖:{state['coverage_count']}/10 | "
                  f"模板:{state['templates']} | 存档:{state['archives']} | "
                  f"准确:{total_ok}/{total_chk} | {fmt(elapsed)}",
                  flush=True)

            if round_num % 5 == 0:
                print(f"  --- {fmt(elapsed)}, "
                      f"最佳连对:{best}/{TARGET} ---", flush=True)

    except KeyboardInterrupt:
        print("\n[中断]")

    state = get_disk_state()
    print(f"\n=== 训练{'完成' if consecutive >= TARGET else '中断'} ===")
    print(f"  轮次: {round_num}")
    print(f"  覆盖: {state['coverage_count']}/10")
    print(f"  连续正确: {consecutive}")
    print(f"  准确率: {total_ok}/{total_chk}")
    print(f"  模板: {state['templates']}, 存档: {state['archives']}")
    print(f"  总耗时: {fmt(elapsed)}")


if __name__ == "__main__":
    main()
