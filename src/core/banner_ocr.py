"""横幅数字模板匹配引擎（与验证码轻量 OCR 同款思路）

针对下单横幅「您的买入委托已成功提交，合同编号：6246860043。」中的数字：
1. 文字掩码: G 通道 < 150（黄底 G≈255，红橙字 G≈56）
2. 列投影切分字形，取最长连续数字串（字宽 5-11px，天然过滤冒号/句号/汉字）
3. 模板: PIL 渲染微软雅黑 0-9 多尺寸，归一化 IoU 打分，取最高分

长度适配（2026-09-29）: 合同编号由券商生成、显示长度随券商/交易所而异
（沪深 A 股模拟盘显示 10 位，深交所规范合同序号 22 位），不做固定长度
假设——以末尾句号（半高字形）为终止锚，取紧邻其前的连续数字串；锚定
不可用时回退最长连续串，8 位仅作下限校验。

粘连拆分: 相邻数字可能共用边缘列（如"44"黏成 17px、"444"黏成 27px——
"4"字宽约 9px）。固定 7.5px/字估计对三连同号会错拆（27/7.5→4≠3，边界
错位产生 0 分子字形打断整串，见 tests/fixtures/banner/unreadable_110122），
改为多档字宽估计各拆一版、按子字形 IoU 总分取最优。

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
MIN_RUN_LEN = 8                    # 下限校验：显示长度随券商而异，仅拦明显非号码串
GLYPH_CONF_MIN = 0.40              # 单字 IoU 低于此值记为无法识别
SPLIT_ESTIMATES = (9.5, 9.0, 8.0, 7.5, 6.5)  # 粘连拆分字宽估计档（实测"4"≈9px）


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
        seq = []          # (digit, conf) 或 None（打断）
        tail_anchor = False  # 末字形是否为半高终止符（句号）
        for x0, x1, g in glyphs:
            tail_anchor = False
            subs = self._digit_subglyphs(g, x1 - x0, min_h)
            if not subs:
                if g.shape[0] < min_h:
                    tail_anchor = True  # 半高非数字字形（句号候选）
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

        # 句号锚定覆盖：横幅格式「……合同编号：<号码>。」，号码串紧邻句号
        # 之前且长度随券商/交易所而异——以终止符定位右边界，不依赖固定长度
        if tail_anchor and seq and seq[-1] is None:
            run = []
            for item in reversed(seq[:-1]):
                if item is None:
                    break
                run.append(item)
            run.reverse()
            if len(run) >= MIN_RUN_LEN:
                best = run

        if len(best) < MIN_RUN_LEN:
            return "", 0.0
        digits = "".join(str(d) for d, _ in best)
        conf = min(c for _, c in best)
        return digits, conf

    def _digit_subglyphs(self, g: np.ndarray, w: int, min_h: float):
        """单字形 → 数字子字形列表（粘连数字拆分）；非数字返回空列表

        粘连拆分按多档字宽估计各拆一版，要求整版全部子字形可识别为数字，
        按子字形 IoU 总分取最优——"44"黏 17px、"444"黏 27px（"4"字宽约
        9px）等不同宽度组合均能对准边界；全档失败视为非数字。
        """
        if DIGIT_W_MIN <= w <= DIGIT_W_MAX:
            return [g] if g.shape[0] >= min_h else []
        if not (DIGIT_W_MAX < w <= 30):
            return []
        best = None  # (IoU 总分, [子字形])
        for est in SPLIT_ESTIMATES:
            n = max(2, round(w / est))
            if n * DIGIT_W_MIN > w + 3 or n > 4:
                continue
            bounds = np.linspace(0, w, n + 1).astype(int)
            arrs, total, ok = [], 0.0, True
            for i in range(n):
                sub = g[:, bounds[i]:bounds[i + 1]]
                ys = np.where(sub.any(axis=1))[0]
                if not len(ys) or sub.shape[0] < min_h:
                    ok = False
                    break
                sub = sub[ys.min():ys.max() + 1]
                d, c = self._classify(sub)
                if d is None:
                    ok = False
                    break
                arrs.append(sub)
                total += c
            if ok and (best is None or total > best[0]):
                best = (total, arrs)
        return best[1] if best else []

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
