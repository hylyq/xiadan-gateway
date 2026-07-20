"""配置管理

从 config/app_config.json 读取配置，并提供默认值
"""
import json
import os
from typing import Any

from src.utils.logger import Logger


# 项目根目录（main.py 所在目录），所有相对路径基于此计算
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "app_config.json")

DEFAULT_CONFIG = {
    "trading_app_path": "",
    "host": "127.0.0.1",
    "port": 5000,
    "auth": {
        "enabled": False,
        "token": ""
    },
    "window_monitor": {
        "enabled": True,
        "check_interval": 5
    },
    "task_queue": {
        "max_size": 50,
        "watchdog_timeout_seconds": 30,
        "query_timeout_seconds": 15,
        "confirm_timeout_seconds": 10
    },
    "idempotency": {
        "order_dedup_window_seconds": 60
    },
    "ocr": {
        "warmup_on_start": True,
        "max_retry": 3
    },
    "logging": {
        "level": "INFO",
        "file": "logs/app.log",
        "screenshot_dir": "logs/screenshots"
    }
}


class AppConfig:
    """应用配置（单例）"""

    _instance = None

    def __new__(cls):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.logger = Logger.get_instance()
        self._config = self._load_config()

    def _load_config(self) -> dict:
        """加载配置文件，与默认配置合并"""
        config = DEFAULT_CONFIG.copy()

        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
            # 深度合并
            for key, value in loaded.items():
                if key in config and isinstance(config[key], dict) and isinstance(value, dict):
                    config[key] = {**config[key], **value}
                else:
                    config[key] = value
            self.logger.info(f"配置加载完成: {CONFIG_PATH}")
        except FileNotFoundError:
            self.logger.warning(f"配置文件不存在，使用默认配置: {CONFIG_PATH}")
        except Exception as e:
            self.logger.error(f"加载配置失败: {str(e)}，使用默认配置")

        return config

    def _save_config(self) -> None:
        """保存配置到文件"""
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self._config, f, ensure_ascii=False, indent=2)

    def get(self, key: str, default: Any = None) -> Any:
        """获取顶层配置项"""
        return self._config.get(key, default)

    def get_trading_app_path(self) -> str:
        """获取 xiadan.exe 路径"""
        return self._config.get("trading_app_path", "")

    def set_trading_app_path(self, path: str) -> None:
        """设置 xiadan.exe 路径"""
        self._config["trading_app_path"] = path
        self._save_config()

    def get_host(self) -> str:
        """获取 HTTP 服务监听地址"""
        return self._config.get("host", "127.0.0.1")

    def get_port(self) -> int:
        """获取 HTTP 服务监听端口"""
        return int(self._config.get("port", 5000))

    def get_auth_config(self) -> dict:
        """获取认证配置"""
        return self._config.get("auth", {"enabled": False, "token": ""})

    def get_window_monitor_config(self) -> dict:
        return self._config.get("window_monitor", {"enabled": True, "check_interval": 5})

    def get_task_queue_config(self) -> dict:
        return self._config.get("task_queue", {
            "max_size": 50,
            "watchdog_timeout_seconds": 30,
            "query_timeout_seconds": 15,
            "confirm_timeout_seconds": 10
        })

    def get_idempotency_config(self) -> dict:
        return self._config.get("idempotency", {"order_dedup_window_seconds": 60})

    def get_ocr_config(self) -> dict:
        return self._config.get("ocr", {"warmup_on_start": True, "max_retry": 3})

    def get_logging_config(self) -> dict:
        cfg = self._config.get("logging", {
            "level": "INFO",
            "file": "logs/app.log",
            "screenshot_dir": "logs/screenshots"
        })
        # 将相对路径转为基于项目根目录的绝对路径
        if not os.path.isabs(cfg.get("file", "")):
            cfg["file"] = os.path.join(BASE_DIR, cfg["file"])
        if not os.path.isabs(cfg.get("screenshot_dir", "")):
            cfg["screenshot_dir"] = os.path.join(BASE_DIR, cfg["screenshot_dir"])
        return cfg

    def get_all(self) -> dict:
        """获取全部配置（用于 /health 接口）"""
        return self._config.copy()
