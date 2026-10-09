"""离线重放: 对探测帧逐帧复现网关截获管线（裁剪→黄色掩码→OCR）

probe_banner_pixels 保存的帧比网关裁剪框左扩 260px/上扩 24px，
据此换算出网关裁剪区后，精确复现 entrust_capture._grab_banner_digits
的掩码/条带定位/模板 OCR，输出每帧读数——定位黏连帧。
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PIL import Image

from src.core.banner_ocr import BannerDigitOCR

YELLOW = (200, 180, 120)
PROBE_LEFT_PAD = 260
PROBE_TOP_PAD = 24


def gateway_crop(img, rec):
    """probe 帧 → 网关裁剪区（窗口相对 55%×底部 32px）"""
    l, t, r, b = rec["rect"]
    x0 = l + int((r - l) * 0.55)
    gx0 = x0 - rec["box"][0]
    gy0 = (b - 34) - rec["box"][1]
    gw = (r - 4) - x0
    gh = (b - 2) - (b - 34)
    return img.crop((gx0, gy0, gx0 + gw, gy0 + gh))


def main():
    ocr = BannerDigitOCR()
    recs = {}
    with open("logs/probe_banner/frames.jsonl", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("png"):
                recs[r["png"]] = r
    frames = sorted(glob.glob("logs/probe_banner/frame_*.png"))
    results = {}
    for path in frames:
        rec = recs.get(os.path.basename(path))
        if rec is None:
            continue
        img = Image.open(path).convert("RGB")
        gw_img = gateway_crop(img, rec)
        arr = np.asarray(gw_img)
        mask = ((arr[:, :, 0] > YELLOW[0]) & (arr[:, :, 1] > YELLOW[1])
                & (arr[:, :, 2] < YELLOW[2]))
        ys = int(mask.sum())
        digits, conf = "", 0.0
        if ys >= 300:
            rows = np.where(mask.sum(axis=1) > 30)[0]
            cols = np.where(mask.sum(axis=0) > 5)[0]
            band = np.asarray(gw_img.crop((int(cols.min()), int(rows.min()),
                                           int(cols.max()) + 1,
                                           int(rows.max()) + 1)))
            digits, conf = ocr.read_digits(band)
        results[path] = (ys, digits, conf)
        flag = " <== 黏连" if digits.startswith("8119") else ""
        print(f"{os.path.basename(path)}  yellow={ys:5d}  digits={digits!r}"
              f"  conf={conf:.2f}{flag}")
    bad = [p for p, (_, d, _) in results.items() if d.startswith("8119")]
    print(f"\ntotal={len(results)} misread={len(bad)}")
    if bad:
        os.makedirs("tests/fixtures/banner", exist_ok=True)
        for p in bad[:3]:
            dst = os.path.join("tests/fixtures/banner",
                               "glued_" + os.path.basename(p))
            Image.open(p).save(dst)
            print(f"saved fixture: {dst}")


if __name__ == "__main__":
    main()
