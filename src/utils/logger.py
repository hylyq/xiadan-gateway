"""日志工具

特性:
- 单例模式
- 文件 + 控制台双输出
- 文件日志轮转（10MB × 5 份）
- deque 缓存最近 1000 条日志
- 线程安全
"""
import logging
import os
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from threading import Lock

from src.utils.singleton import Singleton


class Logger(Singleton):
    """单例日志器"""
    MAX_CACHE_SIZE = 1000

    @classmethod
    def get_instance(cls) -> "Logger":
        return cls._get_instance()

    def _init(self):
        self.log_cache = deque(maxlen=self.MAX_CACHE_SIZE)
        self.lock = Lock()

        # 文件日志器
        self.file_logger = logging.getLogger("xiadan_gateway")
        self.file_logger.setLevel(logging.INFO)
        self.file_logger.propagate = False

        # 避免重复添加 handler
        if not self.file_logger.handlers:
            # 延迟导入避免循环依赖
            from src.models.config import BASE_DIR

            # 默认日志路径基于项目根目录
            # 不依赖 AppConfig（会循环依赖），使用约定路径
            log_file = os.path.join(BASE_DIR, "logs", "app.log")

            os.makedirs(os.path.dirname(log_file), exist_ok=True)

            # 轮转文件 handler：10MB × 5 份
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8"
            )
            file_handler.setLevel(logging.INFO)
            file_formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            )
            file_handler.setFormatter(file_formatter)
            self.file_logger.addHandler(file_handler)

            # 控制台输出
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(file_formatter)
            self.file_logger.addHandler(console_handler)

    def add_log(self, message: str, level: str = "INFO") -> None:
        """添加日志（线程安全）

        Args:
            message: 日志内容
            level: 日志级别 (INFO/WARNING/ERROR/DEBUG)
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"[{timestamp}] {message}"

        with self.lock:
            self.log_cache.append(formatted)

        log_method = {
            "INFO": self.file_logger.info,
            "WARNING": self.file_logger.warning,
            "ERROR": self.file_logger.error,
            "DEBUG": self.file_logger.debug,
        }.get(level.upper(), self.file_logger.info)
        log_method(message)

    def info(self, message: str) -> None:
        self.add_log(message, "INFO")

    def warning(self, message: str) -> None:
        self.add_log(message, "WARNING")

    def error(self, message: str) -> None:
        self.add_log(message, "ERROR")

    def debug(self, message: str) -> None:
        self.add_log(message, "DEBUG")

    def get_recent_logs(self, count: int = 100) -> list:
        """获取最近 N 条日志"""
        with self.lock:
            return list(self.log_cache)[-count:]
