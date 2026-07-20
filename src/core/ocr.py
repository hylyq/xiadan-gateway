"""OCR 服务（基于 ddddocr，预加载模式）

特性:
- 进程启动时预加载模型（避免每次识别都加载）
- 单例模式，全局共享一个 ddddocr 实例
- 仅识别数字（验证码场景）
"""
import threading
from typing import Optional

from src.utils.logger import Logger


class OcrService:
    """OCR 服务（单例）

    ddddocr 基于 ONNX Runtime，CPU 推理:
    - 模型文件几十 MB
    - 单次识别 10-50ms
    - 预加载后常驻内存约 100-200MB
    - 闲置时 CPU 占用为 0
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    @classmethod
    def get_instance(cls):
        if not cls._instance:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.logger = Logger.get_instance()
        self._ddddocr = None
        self._loaded = False

    def warmup(self) -> bool:
        """预热 OCR 引擎（加载模型）

        建议在服务启动时调用，避免首次识别的延迟。

        Returns:
            是否加载成功
        """
        with self._lock:
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
        """识别图片中的文字（仅数字）

        Args:
            image_path: 图片文件路径

        Returns:
            识别结果（已清理为纯数字），失败返回空字符串
        """
        if not self._loaded:
            if not self.warmup():
                return ""

        try:
            with open(image_path, "rb") as f:
                image_data = f.read()
            result = self._ddddocr.classification(image_data)
            # 清理为纯数字
            cleaned = "".join(filter(str.isdigit, result))
            return cleaned
        except Exception as e:
            self.logger.error(f"OCR 识别失败: {str(e)}")
            return ""

    def recognize_bytes(self, image_data: bytes) -> str:
        """识别图片字节流"""
        if not self._loaded:
            if not self.warmup():
                return ""
        try:
            result = self._ddddocr.classification(image_data)
            return "".join(filter(str.isdigit, result))
        except Exception as e:
            self.logger.error(f"OCR 识别失败: {str(e)}")
            return ""

    def is_loaded(self) -> bool:
        return self._loaded
