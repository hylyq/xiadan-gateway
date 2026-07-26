"""轻量级验证码 OCR（基于模板匹配）

仅针对同花顺 4 位数字验证码：
  - 纯白背景，规则数字，带抗锯齿渲染
  - 92×38 像素 RGB 图片
  - 数字宽度约 10px（含抗锯齿），数字间距约 10px

原理：
  1. 灰度化 + 固定阈值二值化（阈值 200，抗锯齿像素 ≈200-240 视为背景）
  2. 垂直投影 → 按暗列分组 → 4 个数字区域
  3. 归一化数字到模板尺寸（28×38）
  4. 与预存模板做归一化互相关（NCC），取最高分

依赖: Pillow + NumPy（已在项目依赖中），零额外依赖
内存: < 5MB（1,200+ 模板预归一化加载为 float32 矩阵）
速度: < 0.01s/次（BLAS 批量矩阵乘法，一次 sgemv 调用）

模板积累策略：
  - 初始训练：用 scripts/generate_templates.py 配合 ddddocr 标注提取模板
  - 离线扩展：运行 scripts/train_ocr.py 利用真实交易过程中的验证码积累模板
  - 当 10 个数字全部覆盖后（当前已全覆盖），可完全脱离 ddddocr
"""
import os
import glob
import threading
from typing import Optional

import numpy as np
from PIL import Image


class LightweightCaptchaOCR:
    """轻量级验证码数字识别引擎

    纯模板匹配，无需 ONNX Runtime / 深度学习模型。
    专为同花顺 4 位数字验证码优化。
    """

    TEMPLATE_SIZE = (28, 38)  # (w, h) 归一化模板尺寸
    BIN_THRESHOLD = 200       # 二值化阈值: 抗锯齿边缘 ~200-240, 数字笔画 ~120-200
    MIN_NCC_SCORE = 0.25      # 最低置信度: NCC 低于此值视为未匹配，触发 ddddocr 兜底

    TEMPLATE_DIR = os.path.join(
        os.path.dirname(__file__), "..", "..", "assets", "digit_templates"
    )

    def __init__(self, template_dir: Optional[str] = None):
        if template_dir:
            self._template_dir = template_dir
        else:
            self._template_dir = os.path.normpath(self.TEMPLATE_DIR)

        self._lock = threading.Lock()
        self._templates: dict[int, list[np.ndarray]] = {}  # {digit: [template_arrays]}
        self._ready = False

        # 预归一化模板矩阵，用于批量匹配（加载时构建一次）
        self._template_matrix: Optional[np.ndarray] = None  # (N, 1064)
        self._template_labels: Optional[np.ndarray] = None  # (N,) int digit

    # ---- 公开 API ----

    def warmup(self) -> bool:
        """加载模板（必须在识别前调用）

        Returns: 是否加载成功（至少有 2 个不同数字的模板才视为成功）
        """
        with self._lock:
            if self._ready:
                return True
            self._load_templates()
            self._ready = len(self._templates) >= 2
            return self._ready

    @property
    def is_ready(self) -> bool:
        return self._ready

    def recognize(self, image_path: str) -> str:
        """识别验证码图片中的 4 位数字

        Returns: 4 位数字字符串，失败返回 ""
        """
        return self._recognize_from_path(image_path)

    def recognize_bytes(self, image_data: bytes) -> str:
        """识别图片字节流中的 4 位数字"""
        from io import BytesIO
        return self._recognize_from_file(BytesIO(image_data))

    # ---- 内部: 模板管理 ----

    def _load_templates(self):
        """加载所有模板到内存（一次硬盘读取，预归一化，构建批量匹配矩阵）"""
        if not os.path.isdir(self._template_dir):
            return

        pattern = os.path.join(self._template_dir, "*.png")
        files = sorted(glob.glob(pattern))

        # 仅加载 real_ 前缀的模板（真实验证码提取）
        real_files = [f for f in files if os.path.basename(f).startswith("real_")]

        all_flat = []
        all_labels = []

        for filepath in real_files:
            digit = self._parse_digit_from_filename(os.path.basename(filepath))
            if digit is None:
                continue

            try:
                img = Image.open(filepath).convert("L")
                arr = np.array(img, dtype=np.float32) / 255.0
                # 预归一化：零均值单位方差，匹配阶段无需重复计算
                arr_norm = self._normalize(arr)

                if digit not in self._templates:
                    self._templates[digit] = []
                self._templates[digit].append(arr_norm)

                all_flat.append(arr_norm.flatten())
                all_labels.append(digit)
            except Exception:
                continue

        if all_flat:
            self._template_matrix = np.stack(all_flat)           # (N, 1064)
            self._template_labels = np.array(all_labels, dtype=np.int8)  # (N,)

    @staticmethod
    def _parse_digit_from_filename(filename: str) -> Optional[int]:
        """从文件名解析数字标签

        real_5_0.png → 5
        real_0_2.png → 0
        """
        try:
            # 格式: real_{digit}_{index}.png
            parts = filename.replace('.png', '').split('_')
            if len(parts) >= 2 and parts[0] == 'real':
                d = int(parts[1])
                if 0 <= d <= 9:
                    return d
        except (ValueError, IndexError):
            pass
        return None

    # ---- 内部: 图片预处理 ----

    def _recognize_from_path(self, image_path: str) -> str:
        try:
            img = Image.open(image_path)
            return self._recognize(img)
        except Exception:
            return ""

    def _recognize_from_file(self, file_obj) -> str:
        try:
            img = Image.open(file_obj)
            return self._recognize(img)
        except Exception:
            return ""

    def _recognize(self, img: Image.Image) -> str:
        """核心识别流程"""
        if not self._ready:
            if not self.warmup():
                return ""

        # 1. 转灰度
        gray = img.convert("L")
        arr = np.array(gray, dtype=np.uint8)

        # 2. 分割数字区域
        digit_regions = self._segment_digits(arr)
        if len(digit_regions) == 0:
            return ""

        # 3. 对每个区域做模板匹配（含置信度检查）
        result = ""
        for region in digit_regions:
            best_digit, score = self._match_best(region)
            if score < self.MIN_NCC_SCORE:
                # 置信度不足，可能该数字尚未覆盖 → 返回空触发兜底
                return ""
            result += str(best_digit)

        return result

    def _segment_digits(self, arr: np.ndarray) -> list[np.ndarray]:
        """从灰度图中分割出 4 个数字

        策略:
          1. 固定阈值二值化（BIN_THRESHOLD=200）
             > 200 = 背景/抗锯齿边缘 → 忽略
             < 200 = 数字笔画核心
          2. 垂直投影 → 找连续暗列组（每个数字宽度约 10px）
          3. 水平投影 → 找数字行范围（约 16px 高）
          4. 裁剪每个数字区域 → 归一化到 TEMPLATE_SIZE

        Returns:
            归一化后的数字区域数组列表（float32, 范围 [0,1]）
        """
        h, w = arr.shape

        # 二值化: < 阈值 = 数字笔画
        binary = (arr < self.BIN_THRESHOLD).astype(np.uint8)

        # 垂直投影
        v_proj = np.sum(binary, axis=0)
        min_pixels = 2  # 至少 2 个暗像素才算数字列

        # 找暗列分组（间距 > 5px 视为不同数字）
        in_digit = False
        digit_start = 0
        raw_groups = []
        for col in range(w):
            if v_proj[col] >= min_pixels and not in_digit:
                in_digit = True
                digit_start = col
            elif v_proj[col] < min_pixels and in_digit:
                in_digit = False
                raw_groups.append((digit_start, col))
        if in_digit:
            raw_groups.append((digit_start, w))

        # 合并间隙 ≤ 5px 的相邻组（同一数字的断裂笔画，如 '5'）
        groups = self._merge_segments(raw_groups, max_gap=5)

        # 水平投影: 找数字的行范围
        h_proj = np.sum(binary, axis=1)
        dark_rows = np.where(h_proj >= 2)[0]
        if len(dark_rows) == 0:
            return []
        top = max(0, dark_rows[0] - 1)
        bottom = min(h, dark_rows[-1] + 2)

        # 如果分出超过 4 组，只取前 4 组（按宽度排序选最大的 4 组）
        if len(groups) > 4:
            groups.sort(key=lambda g: g[1] - g[0], reverse=True)
            groups = groups[:4]
            groups.sort(key=lambda g: g[0])  # 恢复左→右顺序

        # 裁剪 + 归一化
        regions = []
        for left, right in groups:
            if right - left < 4:  # 忽略太窄的（噪声）
                continue
            # 微扩展边界
            left = max(0, left - 1)
            right = min(w, right + 1)
            digit_arr = arr[top:bottom, left:right]

            # 归一化到模板尺寸
            digit_img = Image.fromarray(digit_arr)
            digit_img = digit_img.resize(self.TEMPLATE_SIZE, Image.LANCZOS)
            regions.append(np.array(digit_img, dtype=np.float32) / 255.0)

        return regions

    @staticmethod
    def _merge_segments(segments: list[tuple[int, int]], max_gap: int = 5) -> list[tuple[int, int]]:
        """合并间隙 ≤ max_gap 的相邻段"""
        if not segments:
            return []
        result = [list(segments[0])]
        for seg in segments[1:]:
            if seg[0] - result[-1][1] <= max_gap:
                result[-1][1] = seg[1]
            else:
                result.append(list(seg))
        return [(s, e) for s, e in result]

    # ---- 内部: 模板匹配 ----

    def _match_best(self, digit_arr: np.ndarray) -> tuple[int, float]:
        """对单个数字，返回 (最佳匹配数字, 置信度)

        模板已预归一化并堆叠为矩阵，一次 BLAS 矩阵乘法完成全部比对。
        """
        # 归一化输入（模板已预归一化，此步骤仅对输入执行）
        digit_flat = self._normalize(digit_arr).flatten()  # (1064,)

        # 批量计算余弦相似度: scores = T @ d, 一次 C 级 BLAS 调用
        scores = self._template_matrix @ digit_flat        # (N,)

        best_idx = int(np.argmax(scores))
        return int(self._template_labels[best_idx]), float(scores[best_idx])

    # ---- 数学工具 ----

    @staticmethod
    def _normalize(arr: np.ndarray) -> np.ndarray:
        """零均值单位方差归一化"""
        mean = np.mean(arr)
        std = np.std(arr)
        if std < 1e-8:
            return arr - mean
        return (arr - mean) / std

    # ---- 模板积累 API ----

    def add_sample(self, digit: int, region_arr: np.ndarray) -> bool:
        """添加一个数字样本到内存 + 磁盘模板库，同步更新匹配矩阵

        Args:
            digit: 数字 (0-9)
            region_arr: 归一化后的数字区域数组 (float32, 范围 [0,1])
        """
        try:
            # 预归一化后存储（与 _load_templates 一致）
            arr_norm = self._normalize(region_arr)

            if digit not in self._templates:
                self._templates[digit] = []
            self._templates[digit].append(arr_norm)

            # 更新批量匹配矩阵
            new_row = arr_norm.flatten()
            if self._template_matrix is not None:
                self._template_matrix = np.vstack([self._template_matrix, new_row])
                self._template_labels = np.append(self._template_labels, digit)

            # 保存到磁盘
            os.makedirs(self._template_dir, exist_ok=True)
            existing = len(glob.glob(
                os.path.join(self._template_dir, f"real_{digit}_*.png")
            ))
            save_path = os.path.join(
                self._template_dir, f"real_{digit}_{existing}.png"
            )
            img = Image.fromarray((region_arr * 255).astype(np.uint8))
            img.save(save_path)

            self._ready = len(self._templates) >= 2
            return True
        except Exception:
            return False

    def extract_and_save_samples(self, image_data: bytes, label: str) -> int:
        """从验证码图片中自动提取数字并保存为模板

        Args:
            image_data: 验证码图片字节流（PNG）
            label: ddddocr 标注结果（4 位数字字符串）

        Returns:
            成功保存的模板数量
        """
        from io import BytesIO

        if not label or len(label) != 4 or not label.isdigit():
            return 0

        try:
            img = Image.open(BytesIO(image_data)).convert("L")
            arr = np.array(img, dtype=np.uint8)
            regions = self._segment_digits(arr)

            if len(regions) != 4:
                return 0

            saved = 0
            for i, region in enumerate(regions):
                digit = int(label[i])
                if self.add_sample(digit, region):
                    saved += 1

            return saved
        except Exception:
            return 0

    @property
    def coverage(self) -> set[int]:
        """已覆盖的数字集合"""
        return set(self._templates.keys())

    @property
    def missing_digits(self) -> set[int]:
        """尚未覆盖的数字"""
        return set(range(10)) - set(self._templates.keys())

    @property
    def template_count(self) -> int:
        """模板总数"""
        return sum(len(v) for v in self._templates.values())
