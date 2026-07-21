"""线程安全的单例基类

用法:
    class MyService(Singleton):
        def _init(self):
            # 初始化逻辑（只执行一次）
            self.data = {}

        @classmethod
        def get_instance(cls) -> "MyService":
            return cls._get_instance()
"""
import threading


class Singleton:
    """线程安全的单例基类

    子类需实现 _init() 方法进行初始化（替代 __init__）。
    通过 get_instance() 或 _get_instance() 获取单例。
    """

    _instance = None
    _lock = threading.RLock()
    _initialized = False

    def __init_subclass__(cls, **kwargs):
        """确保每个子类有独立的 _instance 和 _lock"""
        super().__init_subclass__(**kwargs)
        cls._instance = None
        cls._lock = threading.RLock()
        cls._initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                # 双重检查锁定
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(self, *args, **kwargs):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._init(*args, **kwargs)
            self._initialized = True

    def _init(self, *args, **kwargs):
        """子类实现初始化逻辑（只执行一次）"""
        pass

    @classmethod
    def _get_instance(cls):
        """获取单例实例（线程安全）"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def _reset_instance(cls):
        """重置单例（仅用于测试）"""
        with cls._lock:
            cls._instance = None
            cls._initialized = False
