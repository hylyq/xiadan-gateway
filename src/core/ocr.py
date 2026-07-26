"""OCR 服务 — 轻量模板匹配 + ddddocr 质检员

架构:
  - 主引擎: LightweightCaptchaOCR（模板匹配，< 5MB，< 5ms）
  - 质检员: ddddocr（ONNX 模型，~150MB，每次并行运行）

每次识别流程:
  1. 轻量引擎识别 → 结果用于填入券商软件
  2. ddddocr 识别 → 标准答案，用于:
     a. 存档验证码图片（assets/captcha_archive/）
     b. 提取数字模板（assets/digit_templates/）
     c. 对比轻量结果，追踪准确率
  3. 10/10 覆盖后，返回值用轻量引擎结果（快）
     覆盖不全时，返回值用 ddddocr 结果（准）

效果: 越用越准，每次使用都在自我完善。
"""
import os
import threading
from typing import Optional

from src.utils.logger import Logger
from src.utils.singleton import Singleton
from src.core.ocr_lightweight import LightweightCaptchaOCR


class OcrService(Singleton):
    """单例 OCR 服务"""

    @classmethod
    def get_instance(cls) -> "OcrService":
        return cls._get_instance()

    def _init(self):
        self.logger = Logger.get_instance()
        self._lock = threading.Lock()

        # 主引擎: 轻量模板匹配
        self._lightweight: Optional[LightweightCaptchaOCR] = None
        self._light_ready = False

        # 质检员: ddddocr
        self._ddddocr = None
        self._ddddocr_loaded = False

        # 统计
        self._total_recognitions = 0
        self._light_correct = 0
        self._light_mismatch = 0
        self._consecutive_correct = 0
        self._extract_count = 0
        self._extract_last_logged_coverage: set[int] = set()

        # 存档目录
        self._archive_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "assets", "captcha_archive"
        )

    # ================================================================
    # 预热
    # ================================================================

    def warmup(self) -> bool:
        """预热 OCR 引擎"""
        with self._lock:
            # 1. 轻量模板
            self._lightweight = LightweightCaptchaOCR()
            self._light_ready = self._lightweight.warmup()
            if self._light_ready:
                self.logger.info(
                    f"轻量 OCR 就绪 | 覆盖 {len(self._lightweight.coverage)}/10 | "
                    f"{self._lightweight.template_count} 模板"
                )
                self._extract_last_logged_coverage = self._lightweight.coverage.copy()
            else:
                self.logger.info("轻量 OCR 模板不足，依赖 ddddocr")

            # 2. ddddocr 质检员
            try:
                import ddddocr
                self._ddddocr = ddddocr.DdddOcr(show_ad=False)
                self._ddddocr_loaded = True
                self.logger.info("ddddocr 质检员就绪")
            except ImportError:
                self.logger.info("ddddocr 未安装（无质检员）")
            except Exception as e:
                self.logger.warning(f"ddddocr 加载失败: {e}")

            return self._light_ready or self._ddddocr_loaded

    @property
    def is_loaded(self) -> bool:
        return self._light_ready or self._ddddocr_loaded

    @property
    def lightweight_coverage(self) -> set[int]:
        if self._lightweight:
            return self._lightweight.coverage
        return set()

    # ================================================================
    # 公开 API
    # ================================================================

    def recognize(self, image_path: str) -> str:
        """识别验证码 4 位数字（填入券商软件的结果）"""
        result = self._recognize_internal(image_path, "path")
        return "".join(filter(str.isdigit, result)) if result else ""

    def recognize_bytes(self, image_data: bytes) -> str:
        """识别图片字节流中的 4 位数字"""
        result = self._recognize_internal(image_data, "bytes")
        return "".join(filter(str.isdigit, result)) if result else ""

    def recognize_text(self, image_path: str) -> str:
        """全文本识别（诊断用）"""
        return self._recognize_internal(image_path, "path") or ""

    def recognize_text_bytes(self, image_data: bytes) -> str:
        """全文本识别（诊断用）"""
        return self._recognize_internal(image_data, "bytes") or ""

    # ================================================================
    # 核心识别逻辑
    # ================================================================

    def _recognize_internal(self, source, source_type: str) -> str:
        """每次识别都执行完整流程:
        轻量引擎 → ddddocr质检 → 存档 → 提取模板 → 对比统计
        """
        if not self._light_ready and not self._ddddocr_loaded:
            if not self.warmup():
                return ""

        # 读取图片
        if source_type == "path":
            try:
                with open(source, "rb") as f:
                    image_data = f.read()
            except Exception:
                return ""
        else:
            image_data = source

        full_coverage = self._light_ready and len(self._lightweight.coverage) >= 10
        self._total_recognitions += 1

        # ---- 1. 轻量引擎识别 ----
        light_result = ""
        if self._light_ready:
            try:
                light_result = self._lightweight.recognize_bytes(image_data) or ""
            except Exception:
                pass
        light_digits = "".join(filter(str.isdigit, light_result))

        # ---- 2. ddddocr 质检员 ----
        if self._ddddocr_loaded:
            try:
                dddd_result = self._ddddocr.classification(image_data) or ""
                dddd_digits = "".join(filter(str.isdigit, dddd_result))

                if len(dddd_digits) == 4:
                    self._save_to_archive(image_data, dddd_digits)
                    if self._lightweight is not None:
                        self._extract_templates(image_data, dddd_digits)

                if self._light_ready and len(dddd_digits) == 4:
                    self._log_quality_check(light_digits, dddd_digits)

                if len(light_digits) == 4 and len(dddd_digits) == 4:
                    if light_digits == dddd_digits:
                        self._light_correct += 1
                        self._consecutive_correct += 1
                    else:
                        self._light_mismatch += 1
                        self._consecutive_correct = 0
                        self.logger.warning(
                            f"质检告警: 轻量({light_digits}) != ddddocr({dddd_digits}) "
                            f"| 累计准确率: {self.accuracy_rate:.1%}"
                        )

                # 全覆盖 → 信任轻量（快）；否则 → 以 ddddocr 为准
                if full_coverage and len(light_digits) == 4:
                    return light_digits
                return dddd_result

            except Exception as e:
                self.logger.error(f"ddddocr 异常: {e}")

        return light_result

    # ================================================================
    # 质检统计
    # ================================================================

    @property
    def accuracy_rate(self) -> float:
        total = self._light_correct + self._light_mismatch
        return self._light_correct / total if total > 0 else 1.0

    @property
    def consecutive_correct(self) -> int:
        return self._consecutive_correct

    @property
    def quality_report(self) -> dict:
        """质检报告"""
        return {
            "total_recognitions": self._total_recognitions,
            "correct": self._light_correct,
            "mismatch": self._light_mismatch,
            "accuracy_rate": self.accuracy_rate,
            "consecutive_correct": self._consecutive_correct,
            "templates": self._lightweight.template_count if self._lightweight else 0,
            "coverage": sorted(self._lightweight.coverage) if self._lightweight else [],
        }

    # ================================================================
    # 内部: 存档 + 模板提取 + 质检日志
    # ================================================================

    def _save_to_archive(self, image_data: bytes, label: str):
        """存档验证码图片，文件名含 ddddocr 标准答案"""
        try:
            import time
            os.makedirs(self._archive_dir, exist_ok=True)
            ts = int(time.time() * 1000)
            path = os.path.join(self._archive_dir, f"captcha_{ts}_{label}.png")
            with open(path, "wb") as f:
                f.write(image_data)
        except Exception:
            pass

    def _extract_templates(self, image_data: bytes, label: str):
        """从验证码实时提取数字模板"""
        saved = self._lightweight.extract_and_save_samples(image_data, label)
        self._extract_count += saved

        current = self._lightweight.coverage
        if current != self._extract_last_logged_coverage:
            new_digits = sorted(current - self._extract_last_logged_coverage)
            missing = sorted(self._lightweight.missing_digits)
            self.logger.info(
                f"模板更新: +{saved} 样本, 覆盖 {len(current)}/10"
                + (f", 新增数字: {new_digits}" if new_digits else "")
                + (f", 仍缺: {missing}" if missing else ", 全覆盖")
            )
            self._extract_last_logged_coverage = current.copy()
        elif len(current) == 10 and self._extract_count % 100 == 0:
            self.logger.info(
                f"模板积累: {self._lightweight.template_count} 样本 | "
                f"质检: {self._light_correct + self._light_mismatch} 次, "
                f"准确率 {self.accuracy_rate:.1%}"
            )

    def _log_quality_check(self, light_digits: str, dddd_digits: str):
        """质检日志: 每 50 次或连续正确里程碑时输出"""
        checked = self._light_correct + self._light_mismatch + 1
        if checked <= 3 or checked % 50 == 0:
            status = "OK" if light_digits == dddd_digits else "MISMATCH"
            self.logger.info(
                f"质检 #{checked}: {status} | "
                f"轻量={light_digits or '?'} ddddocr={dddd_digits} | "
                f"准确率 {self.accuracy_rate:.1%}"
            )
