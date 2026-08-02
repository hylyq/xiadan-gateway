"""配置管理

从 config/app_config.json 读取配置，并提供默认值
"""
import json
import os
from typing import Any

from src.utils.logger import Logger
from src.utils.singleton import Singleton


# 项目根目录（main.py 所在目录），所有相对路径基于此计算
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "app_config.json")
EXAMPLE_CONFIG_PATH = os.path.join(BASE_DIR, "config", "app_config.example.json")

DEFAULT_CONFIG = {
    "trading_app_paths": [],
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
        "query_timeout_seconds": 30,
        "confirm_timeout_seconds": 10
    },
    "idempotency": {
        "order_dedup_window_seconds": 60
    },
    "ocr": {
        "warmup_on_start": True,
        "max_retry": 3,
        "ddddocr_enabled": False
    },
    "logging": {
        "level": "INFO",
        "file": "logs/app.log",
        "screenshot_dir": "logs/screenshots"
    }
}


class AppConfig(Singleton):
    """应用配置（单例）"""

    @classmethod
    def get_instance(cls) -> "AppConfig":
        return cls._get_instance()

    def _init(self):
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
            self.logger.warning(
                f"配置文件不存在: {CONFIG_PATH}"
            )
            self.logger.info(
                f"请复制 {EXAMPLE_CONFIG_PATH} 为 {CONFIG_PATH} 并按实际路径修改"
            )
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
        """获取第一个配置的 xiadan.exe 路径（向后兼容，用于显示/日志）"""
        paths = self.get_trading_app_paths()
        return paths[0] if paths else ""

    def get_trading_app_paths(self) -> list:
        """获取所有配置的 xiadan.exe 路径列表

        支持向后兼容：新格式 trading_app_paths（列表）或旧格式 trading_app_path（字符串）
        """
        # 新格式：列表
        paths = self._config.get("trading_app_paths")
        if paths:
            return list(paths)
        # 旧格式：单个字符串（向后兼容）
        single = self._config.get("trading_app_path")
        if single:
            return [single]
        return []

    def set_trading_app_paths(self, paths: list) -> None:
        """设置 xiadan.exe 路径列表"""
        self._config["trading_app_paths"] = list(paths)
        # 清理旧格式字段
        self._config.pop("trading_app_path", None)
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

    def validate(self) -> list:
        """配置校验，返回错误描述列表（空列表 = 通过）

        启动时调用（main.py）：非法配置直接中止启动并打印修复指引，
        避免 waitress 启动后运行期爆炸（如 port 类型错、看门狗超时为负）。

        校验项：
        - trading_app_paths: 必须是字符串列表，元素为非空字符串
        - host: 非空字符串
        - port: 1-65535 的整数（接受数字字符串）
        - task_queue 超时/队列字段: 必须 > 0
        """
        errors = []

        paths = self._config.get("trading_app_paths")
        if paths is not None and not isinstance(paths, list):
            errors.append(
                f"trading_app_paths 必须是字符串列表，当前类型: {type(paths).__name__}"
            )
        elif paths:
            for i, p in enumerate(paths):
                if not isinstance(p, str) or not p.strip():
                    errors.append(
                        f"trading_app_paths[{i}] 无效（应为非空字符串）: {p!r}"
                    )

        host = self._config.get("host")
        if not isinstance(host, str) or not host.strip():
            errors.append(f"host 必须是非空字符串，当前: {host!r}")

        try:
            port = int(self._config.get("port"))
            if not 1 <= port <= 65535:
                errors.append(f"port 必须在 1-65535 之间，当前: {port}")
        except (TypeError, ValueError):
            errors.append(f"port 必须是数字，当前: {self._config.get('port')!r}")

        qcfg = self._config.get("task_queue") or {}
        for field in ("watchdog_timeout_seconds", "query_timeout_seconds",
                      "confirm_timeout_seconds", "max_size"):
            v = qcfg.get(field)
            if v is None:
                continue
            try:
                if float(v) <= 0:
                    errors.append(f"task_queue.{field} 必须 > 0，当前: {v}")
            except (TypeError, ValueError):
                errors.append(f"task_queue.{field} 必须是数字，当前: {v!r}")

        return errors

    def reload(self) -> dict:
        """热重载配置文件

        重新读取 config/app_config.json 并合并到当前配置。
        注意: trading_app_path 等路径变更需重启服务才能完全生效。

        Returns:
            重载后的配置摘要
        """
        old_config = self._config.copy()
        self._config = self._load_config()

        # 计算变更项
        changes = []
        for key in self._config:
            if self._config.get(key) != old_config.get(key):
                changes.append(key)

        if changes:
            self.logger.info(f"配置热重载完成，变更项: {changes}")
        else:
            self.logger.info("配置热重载完成，无变更")

        return {
            "reloaded": True,
            "changes": changes,
            "config_path": CONFIG_PATH
        }
