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
        """输入条带 RGB 数组，返回 (数字串, 最低单字置信度)；无数字返回 ("", 0.0)

        粘连处理: 相邻数字可能共用边缘列（如 "64" 黏成 17px 宽字形），
        先按预估字宽拆分为多个子字形，再逐个分类，最后取最长连续数字串。
        """
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

        # 先分类再找串：拆分子字形进入候选序列，非数字字形打断连续性
        seq = []  # (digit, conf) 或 None（打断）
        for x0, x1, g in glyphs:
            subs = self._digit_subglyphs(g, x1 - x0, min_h)
            if not subs:
                seq.append(None)
                continue
            for sub in subs:
                d, c = self._classify(sub)
                seq.append((d, c) if d is not None else None)

        best, cur = [], []
        for item in seq:
            if item is None:
                if len(cur) > len(best):
                    best = cur
                cur = []
            else:
                cur.append(item)
        if len(cur) > len(best):
            best = cur
        if len(best) < MIN_RUN_LEN:
            return "", 0.0
        digits = "".join(str(d) for d, _ in best)
        conf = min(c for _, c in best)
        return digits, conf

    def _digit_subglyphs(self, g: np.ndarray, w: int, min_h: float):
        """单字形 → 数字子字形列表（粘连数字拆分）；非数字返回空列表"""
        if DIGIT_W_MIN <= w <= DIGIT_W_MAX:
            return [g] if g.shape[0] >= min_h else []
        if not (DIGIT_W_MAX < w <= 30):
            return []
        n = max(2, round(w / 7.5))  # 实测数字字宽 ~7-9px
        bounds = np.linspace(0, w, n + 1).astype(int)
        subs = []
        for i in range(n):
            sub = g[:, bounds[i]:bounds[i + 1]]
            ys = np.where(sub.any(axis=1))[0]
            if len(ys) and sub.shape[0] >= min_h:
                subs.append(sub[ys.min():ys.max() + 1])
        return subs if len(subs) == n else []  # 拆分不完整视为非数字

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
