"""横幅数字模板匹配引擎（与验证码轻量 OCR 同款思路）

针对下单横幅「您的买入委托已成功提交，合同编号：6246860043。」中的数字：
1. 文字掩码: G 通道 < 150（黄底 G≈255，红橙字 G≈56）
2. 列投影切分字形，取最长连续数字串（字宽 5-11px，天然过滤冒号/句号/汉字）
3. 模板: PIL 渲染微软雅黑 0-9 多尺寸，归一化 IoU 打分，取最高分

原型实测（2026-09-09 模拟盘真值样本）: 10 位数字逐位全对。
相比 ddddocr: 零外部依赖、~1-5ms、内存可忽略（模板为 10×6 张 12×16 位图）。
"""
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",     # 微软雅黑（横幅实测字体）
    "C:/Windows/Fonts/msyhbd.ttc",   # 微软雅黑 Bold
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
]
SIZES = (11, 12, 13, 14, 15, 16)
NORM_W, NORM_H = 12, 16
DIGIT_W_MIN, DIGIT_W_MAX = 3, 11    # 数字字形宽度（实测:多数 7-9px，"1"仅 ~3-4px）
DIGIT_H_RATIO = 0.55               # 数字字形高度占行高比例下限（过滤句号/冒号）
MIN_RUN_LEN = 8                    # 合同编号为 10 位，允许个别字形粘连缺失
GLYPH_CONF_MIN = 0.40              # 单字 IoU 低于此值记为无法识别


def _make_font(size):
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _render_digit(d: int, size: int) -> np.ndarray:
    im = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(im).text((size // 2, size // 2), str(d), fill=255,
                            font=_make_font(size))
    a = np.array(im) > 128
    ys, xs = np.where(a)
    if len(ys) == 0:
        return np.zeros((NORM_H, NORM_W), dtype=bool)
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


class BannerDigitOCR:
    """横幅数字模板匹配器（模板首次使用时构建，之后常驻 ~几十 KB）"""

    def __init__(self):
        self._templates = None  # {digit: [归一化二值模板, ...]}

    def _ensure_templates(self):
        if self._templates is not None:
            return
        self._templates = {}
        for d in range(10):
            variants = []
            for size in SIZES:
                t = _render_digit(d, size)
                im = Image.fromarray((t * 255).astype(np.uint8)).resize(
                    (NORM_W, NORM_H))
                variants.append(np.array(im) > 100)
            self._templates[d] = variants

    def read_digits(self, band_rgb: np.ndarray) -> tuple:
        """输入条带 RGB 数组，返回 (数字串, 最低单字置信度)；无数字返回 ("", 0.0)"""
        self._ensure_templates()
        if band_rgb.ndim != 3 or band_rgb.shape[2] < 3:
            return "", 0.0
        dark = band_rgb[:, :, 1] < 150
        if int(dark.sum()) < 50:
            return "", 0.0

        glyphs = self._segment(dark)
        if not glyphs:
            return "", 0.0
        # 行高 = 最高字形（汉字/数字同高）；句号、冒号仅约半高，据此排除
        tallest = max(g.shape[0] for _, _, g in glyphs)
        min_h = tallest * DIGIT_H_RATIO
        best_run = self._longest_digit_run(glyphs, min_h)
        if len(best_run) < MIN_RUN_LEN:
            return "", 0.0

        digits, confs = [], []
        for g in best_run:
            d, conf = self._classify(g)
            if d is None:
                return "", 0.0
            digits.append(str(d))
            confs.append(conf)
        return "".join(digits), float(min(confs))

    # ------------------------------------------------------------

    @staticmethod
    def _segment(dark: np.ndarray):
        """列投影切分字形，返回 [(x0, x1, glyph_bool_array)]（按 x 升序）

        严格按零列切分（原型实测有效）：横幅字形间距 ~1-2px，
        空隙合并会把整行黏成大块导致宽度过滤全部丢弃。
        """
        colsum = dark.sum(axis=0)
        spans, start = [], None
        for x, v in enumerate(list(colsum) + [0]):
            if v > 0:
                if start is None:
                    start = x
            elif start is not None:
                spans.append((start, x))
                start = None
        out = []
        for x0, x1 in spans:
            w = x1 - x0
            if not (2 <= w <= 30):
                continue
            g = dark[:, x0:x1]
            ys = np.where(g.any(axis=1))[0]
            if len(ys) == 0:
                continue
            out.append((x0, x1, g[ys.min():ys.max() + 1]))
        return out

    @staticmethod
    def _longest_digit_run(glyphs, min_h: float):
        """最长连续数字字形组（宽度+高度过滤，非数字字形打断连续性）"""
        longest, cur, prev_x1 = [], [], None
        for x0, x1, g in glyphs:
            is_digit = (DIGIT_W_MIN <= x1 - x0 <= DIGIT_W_MAX
                        and g.shape[0] >= min_h)
            if is_digit:
                if prev_x1 is not None and x0 - prev_x1 > 12 and len(cur) > len(longest):
                    longest = cur
                    cur = []
                cur.append(g)
                prev_x1 = x1
            else:
                if len(cur) > len(longest):
                    longest = cur
                cur, prev_x1 = [], None
        if len(cur) > len(longest):
            longest = cur
        return longest

    def _classify(self, g: np.ndarray):
        """单字形分类，返回 (digit, iou) 或 (None, 0.0)"""
        im = Image.fromarray((g * 255).astype(np.uint8)).resize((NORM_W, NORM_H))
        ga = np.array(im) > 100
        best_d, best_iou = None, 0.0
        for d, variants in self._templates.items():
            for ta in variants:
                union = (ga | ta).sum()
                iou = (ga & ta).sum() / union if union else 0.0
                if iou > best_iou:
                    best_d, best_iou = d, iou
        if best_iou < GLYPH_CONF_MIN:
            return None, 0.0
        return best_d, best_iou
