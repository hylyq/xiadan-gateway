"""OCR 服务（基于 ddddocr，预加载模式）

特性:
- 进程启动时预加载模型（避免每次识别都加载）
- 单例模式，全局共享一个 ddddocr 实例
- 仅识别数字（验证码场景）
"""
import threading
from typing import Optional

from src.utils.logger import Logger
from src.utils.singleton import Singleton


class OcrService(Singleton):
    """单例 OCR 服务

    ddddocr 基于 ONNX Runtime，CPU 推理:
    - 模型文件几十 MB
    - 单次识别 10-50ms
    - 预加载后常驻内存约 100-200MB
    - 闲置时 CPU 占用为 0
    """

    @classmethod
    def get_instance(cls) -> "OcrService":
        return cls._get_instance()

    def _init(self):
        self.logger = Logger.get_instance()
        self._ddddocr = None
        self._loaded = False
        self._ocr_lock = threading.Lock()

    def warmup(self) -> bool:
        """预热 OCR 引擎（加载模型）

        建议在服务启动时调用，避免首次识别的延迟。

        Returns:
            是否加载成功
        """
        with self._ocr_lock:
            if self._loaded:
                return True

            try:
                self.logger.info("开始加载 ddddocr 模型...")
                import ddddocr
                # show_ad=False 避免控制台广告输出
                self._ddddocr = ddddocr.DdddOcr(show_ad=False)
                self._loaded = True
                self.logger.info("ddddocr 模型加载完成")
                return True
            except ImportError:
                self.logger.error("未安装 ddddocr，请运行: uv add ddddocr")
                return False
            except Exception as e:
                self.logger.error(f"加载 ddddocr 失败: {str(e)}")
                return False

    def recognize(self, image_path: str) -> str:
        """识别图片中的文字（仅数字，用于验证码场景）

        Args:
            image_path: 图片文件路径

        Returns:
            识别结果（已清理为纯数字），失败返回空字符串
        """
        result = self._ocr(image_path)
        if result:
            return "".join(filter(str.isdigit, result))
        return ""

    def recognize_bytes(self, image_data: bytes) -> str:
        """识别图片字节流（仅数字，用于验证码场景）"""
        result = self._ocr_bytes(image_data)
        if result:
            return "".join(filter(str.isdigit, result))
        return ""

    def recognize_text(self, image_path: str) -> str:
        """识别图片中的全部文字（全文本，用于诊断/调试）

        与 recognize() 的区别：不过滤数字，保留所有识别出的字符，
        包括中文、字母、标点等，适用于弹窗截图 OCR 诊断。

        Args:
            image_path: 图片文件路径

        Returns:
            识别出的全部文本，失败返回空字符串
        """
        return self._ocr(image_path) or ""

    def recognize_text_bytes(self, image_data: bytes) -> str:
        """识别图片字节流中的全部文字（全文本，用于诊断）"""
        return self._ocr_bytes(image_data) or ""

    def _ocr(self, image_path: str) -> str:
        """底层 OCR 识别（原始结果，不经任何过滤）"""
        if not self._loaded:
            if not self.warmup():
                return ""
        try:
            with open(image_path, "rb") as f:
                image_data = f.read()
            return self._ddddocr.classification(image_data) or ""
        except Exception as e:
            self.logger.error(f"OCR 识别失败: {str(e)}")
            return ""

    def _ocr_bytes(self, image_data: bytes) -> str:
        """底层 OCR 识别字节流（原始结果）"""
        if not self._loaded:
            if not self.warmup():
                return ""
        try:
            return self._ddddocr.classification(image_data) or ""
        except Exception as e:
            self.logger.error(f"OCR 识别失败: {str(e)}")
            return ""

    def is_loaded(self) -> bool:
        return self._loaded
