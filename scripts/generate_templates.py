"""验证码数字模板生成工具

从 captcha 样本中提取 0-9 数字模板，供轻量 OCR 引擎（LightweightCaptchaOCR）使用。

用法:
  # 从单个 captcha.png 提取（交互式，需手动输入标签）
  uv run python scripts/generate_templates.py single logs/screenshots/captcha.png

  # 从 captcha 样本目录批量提取（需要 ddddocr 自动标注）
  uv run python scripts/generate_templates.py batch assets/captcha_samples/

  # 查看当前模板覆盖情况
  uv run python scripts/generate_templates.py status

工作流程:
  1. 启用 ddddocr 兜底期间，每次成功识别验证码会自动保存样本到
     assets/captcha_samples/
  2. 积累足够样本后，运行 batch 命令批量提取数字模板
  3. 当 10 个数字全部覆盖后，可安全移除 ddddocr 依赖
"""
import os
import sys
import glob

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def cmd_status():
    """查看模板库状态"""
    from src.core.ocr_lightweight import LightweightCaptchaOCR
    ocr = LightweightCaptchaOCR()
    ocr.warmup()

    print("=== 轻量 OCR 模板库状态 ===")
    print(f"模板目录: {ocr._template_dir}")
    print(f"模板总数: {ocr.template_count}")
    print(f"已覆盖数字 ({len(ocr.coverage)}/10): {sorted(ocr.coverage)}")
    print(f"缺失数字 ({len(ocr.missing_digits)}/10): {sorted(ocr.missing_digits)}")

    if len(ocr.coverage) < 2:
        print("\n[下一步] 当前模板不足以独立识别，需至少 2 个数字。")
        print("  1. 确保 ddddocr 已安装: uv add ddddocr")
        print("  2. 运行几次含验证码的操作（如 /positions 查询）")
        print("  3. 验证码样本会自动保存到 assets/captcha_samples/")
        print("  4. 运行: uv run python scripts/generate_templates.py batch")
    elif len(ocr.missing_digits) > 0:
        print(f"\n[下一步] 还需覆盖 {len(ocr.missing_digits)} 个数字: {sorted(ocr.missing_digits)}")
        print("  继续使用含 ddddocr 兜底的模式，样本会自动积累。")
        print("  也可手动运行: uv run python scripts/generate_templates.py batch")
    else:
        print("\n[OK] 全部 10 个数字已覆盖！可安全移除 ddddocr: uv remove ddddocr")


def cmd_single(image_path: str):
    """从单张验证码图片提取模板"""
    if not os.path.exists(image_path):
        print(f"[ERROR] 文件不存在: {image_path}")
        return

    # 尝试 ddddocr 自动标注
    label = None
    try:
        import ddddocr
        ocr = ddddocr.DdddOcr(show_ad=False)
        with open(image_path, "rb") as f:
            label = ocr.classification(f.read()) or ""
        label = "".join(filter(str.isdigit, label))
        print(f"ddddocr 识别: {label!r}")
    except ImportError:
        print("ddddocr 未安装，无法自动标注")

    if not label or len(label) != 4:
        label = input("请输入验证码 4 位数字: ").strip()

    if len(label) != 4 or not label.isdigit():
        print(f"[ERROR] 无效标签: {label!r}")
        return

    # 提取模板
    with open(image_path, "rb") as f:
        image_data = f.read()

    from src.core.ocr_lightweight import LightweightCaptchaOCR
    ocr = LightweightCaptchaOCR()
    saved = ocr.extract_and_save_samples(image_data, label)
    print(f"已提取 {saved} 个数字模板")

    cmd_status()


def cmd_batch(sample_dir: str = None):
    """批量从样本目录提取模板"""
    if sample_dir is None:
        sample_dir = os.path.join(
            os.path.dirname(__file__), "..", "assets", "captcha_samples"
        )

    if not os.path.isdir(sample_dir):
        print(f"[ERROR] 样本目录不存在: {sample_dir}")
        print("  请先确保 ddddocr 兜底已启用，运行几次含验证码的操作。")
        return

    try:
        import ddddocr
    except ImportError:
        print("[ERROR] 批量提取需要 ddddocr 进行自动标注。")
        print("  安装: uv sync --extra ocr")
        return

    png_files = sorted(glob.glob(os.path.join(sample_dir, "*.png")))
    if not png_files:
        print(f"样本目录为空: {sample_dir}")
        return

    print(f"找到 {len(png_files)} 个样本")

    dddd = ddddocr.DdddOcr(show_ad=False)
    from src.core.ocr_lightweight import LightweightCaptchaOCR
    lw = LightweightCaptchaOCR()
    lw.warmup()

    before = len(lw.coverage)
    success = 0

    for filepath in png_files:
        with open(filepath, "rb") as f:
            image_data = f.read()
        label = dddd.classification(image_data) or ""
        label = "".join(filter(str.isdigit, label))
        if len(label) != 4:
            print(f"  SKIP {os.path.basename(filepath)}: ddddocr 未识别到 4 位数字 ({label!r})")
            continue
        saved = lw.extract_and_save_samples(image_data, label)
        if saved > 0:
            success += 1
            print(f"  OK {os.path.basename(filepath)}: {label} → +{saved} 模板")

    after = len(lw.coverage)
    print(f"\n处理完成: {success}/{len(png_files)} 个样本成功")
    print(f"覆盖增长: {before}/10 → {after}/10")
    cmd_status()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "status":
        cmd_status()
    elif cmd == "single":
        if len(sys.argv) < 3:
            print("用法: python scripts/generate_templates.py single <image_path>")
            sys.exit(1)
        cmd_single(sys.argv[2])
    elif cmd == "batch":
        sample_dir = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_batch(sample_dir)
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
        sys.exit(1)
