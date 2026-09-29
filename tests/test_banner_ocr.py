"""横幅数字 OCR 单元测试

夹具为模拟盘真实横幅条带样本（logs/banner_samples 的副本，无敏感数据）：
- unreadable_110122: 委托号 6284449826 含三连同号"444"，黏连字形曾被
  固定 7.5px/字拆分估计错拆导致识别失败（2026-09-29 盘中实失败例）
- unreadable_104946 / unreadable_104952: 动画帧偶发失败例，离线可读
"""
from pathlib import Path

import numpy as np
from PIL import Image

import pytest

from src.core.banner_ocr import BannerDigitOCR

FIXTURES = Path(__file__).parent / "fixtures" / "banner"

# 样本 → 当日委托复核过的真实合同编号
EXPECTED = {
    "unreadable_104946.png": "6253364341",
    "unreadable_104952.png": "6253364342",
    "unreadable_110122.png": "6284449826",
}


def _load(name: str) -> np.ndarray:
    return np.asarray(Image.open(FIXTURES / name).convert("RGB"))


@pytest.fixture(scope="module")
def ocr() -> BannerDigitOCR:
    return BannerDigitOCR()


class TestRealBannerSamples:
    """真实横幅样本：三连同号黏连与句号终止锚定"""

    def test_triple_four_merged_glyph(self, ocr):
        """三连同号"444"黏连 27px 仍应正确拆分读出（回归核心用例）"""
        digits, conf = ocr.read_digits(_load("unreadable_110122.png"))
        assert digits == EXPECTED["unreadable_110122.png"]
        assert conf >= 0.40

    def test_animation_frame_samples(self, ocr):
        """动画帧偶发样本不回归"""
        for name in ("unreadable_104946.png", "unreadable_104952.png"):
            digits, _ = ocr.read_digits(_load(name))
            assert digits == EXPECTED[name], name

    def test_period_terminator_anchors_run(self, ocr):
        """数字串应以句号前紧邻的字形收尾——句号右侧不存在数字串"""
        band = _load("unreadable_110122.png")
        dark = band[:, :, 1] < 150
        glyphs = ocr._segment(dark)
        # 句号是最后一个字形（半高小点），紧邻其前是号码末位
        x0, x1, g = glyphs[-1]
        assert g.shape[0] < 8  # 半高
        assert (x1 - x0) <= 6


class TestSplitterRobustness:
    """粘连拆分：多档字宽估计"""

    def test_various_widths_classified(self, ocr):
        """不同宽度的黏连字形（2-4 字）均可拆分分类，不打断序列"""
        band = _load("unreadable_110122.png")
        dark = band[:, :, 1] < 150
        glyphs = ocr._segment(dark)
        tallest = max(g.shape[0] for _, _, g in glyphs)
        min_h = tallest * 0.55
        wide = [(x0, x1, g) for x0, x1, g in glyphs
                if (x1 - x0) > 11 and g.shape[0] >= min_h]
        assert wide, "样本中应存在黏连字形"
        for x0, x1, g in wide:
            subs = ocr._digit_subglyphs(g, x1 - x0, min_h)
            # 每个宽字形要么拆出完整子字形序列，要么整体判非数字，
            # 不允许拆出一半（None 打断号码串即失败）
            if subs:
                for sub in subs:
                    d, c = ocr._classify(sub)
                    assert d is not None, f"x{x0}-{x1} 拆出不可识别子字形"
