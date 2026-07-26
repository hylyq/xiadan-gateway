"""OCR 服务 — 轻量模板匹配（ddddocr 可选调试）

架构:
  - 生产模式 (ddddocr_enabled=false): 仅轻量引擎（< 5MB，< 5ms）
  - 调试模式 (ddddocr_enabled=true):  轻量引擎 + ddddocr 质检员（~150MB）

生产模式流程:
  1. 轻量引擎识别 → 成功(4位)直接返回
  2. 失败 → 存档 assets/captcha_archive/failed_*.png → 返回 ""
  3. 调用方返回 HTTP OCR_FAILED 错误

调试模式流程:
  1. 轻量引擎识别 → ddddocr 质检 → 存档 + 模板提取 + 准确率对比
  2. 全覆盖 → 信任轻量（快）；否则 → 以 ddddocr 为准

效果: 日常 0 额外内存开销，ddddocr 保留作离线训练/调试工具。
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

        # ddddocr 调试开关（默认关闭，省 ~150MB）
        self._ddddocr_enabled = False

        # 主引擎: 轻量模板匹配
        self._lightweight: Optional[LightweightCaptchaOCR] = None
        self._light_ready = False

        # 质检员: ddddocr
        self._ddddocr = None
        self._ddddocr_loaded = False

        # 统计
        self._total_recognitions = 0
        self._ocr_failures = 0
        self._light_correct = 0
        self._light_mismatch = 0
        self._consecutive_correct = 0
        self._extract_count = 0
        self._extract_last_logged_coverage: set[int] = set()

        # 存档目录
        self._archive_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "assets", "captcha_archive"
        )

    def configure(self, ddddocr_enabled: bool = False):
        """设置 ddddocr 调试开关（必须在 warmup() 之前调用）"""
        self._ddddocr_enabled = ddddocr_enabled

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

            # 2. ddddocr 质检员（仅调试模式）
            if self._ddddocr_enabled:
                try:
                    import ddddocr
                    self._ddddocr = ddddocr.DdddOcr(show_ad=False)
                    self._ddddocr_loaded = True
                    self.logger.info("ddddocr 质检员就绪（调试模式）")
                except ImportError:
                    self.logger.info("ddddocr 未安装，调试模式不可用")
                except Exception as e:
                    self.logger.warning(f"ddddocr 加载失败: {e}")
            else:
                self.logger.info("ddddocr 已禁用（生产模式），省 ~150MB 内存")

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
        """识别逻辑:
        - ddddocr 启用 → 完整双引擎（质检+存档+模板提取）
        - ddddocr 禁用 → 仅轻量引擎，失败存档返回空
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

        # ---- 2. ddddocr 质检员（仅调试模式） ----
        if self._ddddocr_enabled and self._ddddocr_loaded:
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

        # ---- ddddocr 禁用: 仅轻量引擎 ----
        if len(light_digits) == 4:
            return light_digits

        # 识别失败: 存档 + 返回空
        self._ocr_failures += 1
        self._save_failed_archive(image_data)
        return ""

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
        """质检报告（格式随 ddddocr 开关变化）"""
        base = {
            "total_recognitions": self._total_recognitions,
            "ddddocr_enabled": self._ddddocr_enabled,
            "templates": self._lightweight.template_count if self._lightweight else 0,
            "coverage": sorted(self._lightweight.coverage) if self._lightweight else [],
            "full_coverage": len(self._lightweight.coverage) >= 10 if self._lightweight else False,
        }
        if self._ddddocr_enabled:
            base.update({
                "correct": self._light_correct,
                "mismatch": self._light_mismatch,
                "accuracy_rate": self.accuracy_rate,
                "consecutive_correct": self._consecutive_correct,
            })
        else:
            base["failures"] = self._ocr_failures
        return base

    # ================================================================
    # 内部: 存档 + 模板提取 + 质检日志
    # ================================================================

    def _save_to_archive(self, image_data: bytes, label: str):
        """存档验证码图片，文件名含 ddddocr 标准答案（仅调试模式）"""
        try:
            import time
            os.makedirs(self._archive_dir, exist_ok=True)
            ts = int(time.time() * 1000)
            path = os.path.join(self._archive_dir, f"captcha_{ts}_{label}.png")
            with open(path, "wb") as f:
                f.write(image_data)
        except Exception:
            pass

    def _save_failed_archive(self, image_data: bytes):
        """存档识别失败的验证码图片（生产模式，供离线训练）"""
        try:
            import time
            os.makedirs(self._archive_dir, exist_ok=True)
            ts = int(time.time() * 1000)
            path = os.path.join(self._archive_dir, f"failed_{ts}.png")
            with open(path, "wb") as f:
                f.write(image_data)
            self.logger.info(f"验证码识别失败，已存档: {path}")
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
