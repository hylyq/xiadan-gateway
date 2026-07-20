"""全局任务队列

核心特性:
- 单 worker 线程: 所有操作顺序执行，避免 xiadan.exe 并发冲突
- 看门狗机制: 任务超时触发截图+ESC+激活+F1 恢复，完成后才返回 HTTP 错误
- 状态重置: 每个任务开始前 ESC×2 + 激活 + F1
- 僵尸检测: 超过阈值的任务被标记
- 队列限制: 最大 50 个待处理任务

【关键设计】
看门狗触发后，必须完成所有恢复步骤后才 task['event'].set()，
确保 HTTP 调用方收到 TASK_TIMEOUT 错误时，xiadan.exe 已重置为初始状态。
"""
import queue
import threading
import time
from typing import Callable, Any, Optional

from src.api.response import TaskTimeoutError
from src.models.config import AppConfig
from src.services.window_service import WindowService
from src.utils.logger import Logger
from src.utils.screenshot import ScreenshotUtil


class Task:
    """任务对象"""

    def __init__(self, func: Callable, name: str, params: dict, timeout: int):
        self.func = func
        self.name = name
        self.params = params
        self.timeout = timeout
        self.result: Any = None
        self.error: Optional[Exception] = None
        self.event = threading.Event()
        self.start_time: Optional[float] = None
        self.screenshot: Optional[str] = None
        # 标记是否已被看门狗判定为超时
        # 用于 worker 在 finally 中丢弃迟到的 result，避免状态污染
        self.is_timeout: bool = False

    def elapsed(self) -> float:
        if self.start_time is None:
            return 0
        return time.time() - self.start_time


class TaskQueue:
    """全局任务队列（单例）"""

    _instance = None

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
        self.config = AppConfig()
        self.window_service = WindowService()
        self.screenshot_util = ScreenshotUtil(
            self.config.get_logging_config().get("screenshot_dir", "logs/screenshots")
        )

        queue_config = self.config.get_task_queue_config()
        self._max_size = queue_config.get("max_size", 50)

        self._queue: queue.Queue = queue.Queue(maxsize=self._max_size)
        self._current_task: Optional[Task] = None
        self._lock = threading.Lock()

        # 启动 worker 线程
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="Task-Worker")
        self._worker.start()
        self.logger.info(f"任务队列已启动，最大队列长度: {self._max_size}")

    def submit(self, func: Callable, task_name: str, params: dict,
               timeout: Optional[int] = None) -> Any:
        """提交任务并同步等待结果

        Args:
            func: 任务函数
            task_name: 任务名称（用于错误信息）
            params: 任务参数（用于错误信息和幂等检查）
            timeout: 超时秒数，None 则使用看门狗默认值

        Returns:
            任务执行结果

        Raises:
            TaskTimeoutError: 任务超时（已恢复）
            Exception: 任务执行失败
        """
        if timeout is None:
            timeout = self.config.get_task_queue_config().get("watchdog_timeout_seconds", 30)

        task = Task(func, task_name, params, timeout)

        # 入队（非阻塞，满了立即报错）
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            from src.api.response import ApiError, ErrorCode
            raise ApiError(
                error_code=ErrorCode.QUEUE_FULL,
                message=f"任务队列已满（最大 {self._max_size}），请稍后重试",
                suggestion="调用 GET /queue/status 查看队列状态"
            )

        # 同步等待结果
        # 注意: 调用方 timeout 必须 > 服务端 timeout + 恢复耗时（约5秒）
        # 推荐调用方 timeout = watchdog_timeout + 10
        if not task.event.wait(timeout=timeout + 10):
            # 极端情况：连看门狗都没触发（不应该发生）
            from src.api.response import ApiError, ErrorCode
            raise ApiError(
                error_code=ErrorCode.QUEUE_TIMEOUT,
                message="任务排队或执行超时（看门狗未触发）",
                suggestion="请检查服务端日志和 xiadan.exe 状态"
            )

        if task.error:
            raise task.error
        return task.result

    def _worker_loop(self) -> None:
        """工作线程主循环"""
        self.logger.info("任务 worker 线程已启动")

        while True:
            task = self._queue.get()
            task.start_time = time.time()

            with self._lock:
                self._current_task = task

            # 启动看门狗
            watchdog = threading.Timer(task.timeout, self._handle_timeout, args=(task,))
            watchdog.daemon = True
            watchdog.start()

            try:
                # 任务开始前重置 xiadan.exe 状态
                self._reset_trading_window()

                # 执行任务
                task.result = task.func()
                self.logger.info(f"任务完成: {task.name}, 耗时 {task.elapsed():.2f}s")

            except Exception as e:
                task.error = e
                self.logger.error(f"任务失败: {task.name}, 错误: {str(e)}")

            finally:
                watchdog.cancel()
                with self._lock:
                    self._current_task = None

                # 若看门狗已判定超时并设置错误，丢弃迟到的 result/error
                # 避免：HTTP 已返回超时错误后，原任务又完成并覆盖状态
                if task.is_timeout:
                    self.logger.warning(
                        f"任务 {task.name} 在超时后才完成，丢弃迟到结果 "
                        f"(耗时 {task.elapsed():.2f}s)"
                    )
                    # 保持看门狗设置的 TaskTimeoutError，不覆盖
                    task.result = None

                task.event.set()
                self._queue.task_done()

    def _reset_trading_window(self) -> None:
        """重置 xiadan.exe 到初始状态

        每个任务开始前执行:
        1. ESC×2 关闭可能存在的弹窗
        2. 重新激活窗口
        3. F1 切回买入默认界面
        """
        try:
            self.window_service.send_key("ESC")
            time.sleep(0.2)
            self.window_service.send_key("ESC")
            time.sleep(0.2)
        except Exception as e:
            self.logger.warning(f"重置时 ESC 失败: {str(e)}")

        try:
            trading_path = self.config.get_trading_app_path()
            if trading_path:
                # foreground=False: 仅恢复最小化窗口，不抢焦点
                # 后续 F1 按键通过 PostMessage 后台发送
                self.window_service.activate_window(trading_path, foreground=False)
                time.sleep(0.1)
        except Exception as e:
            self.logger.warning(f"重置时激活窗口失败: {str(e)}")

        try:
            self.window_service.send_key("F1")
            time.sleep(0.2)
        except Exception as e:
            self.logger.warning(f"重置时 F1 失败: {str(e)}")

    def _handle_timeout(self, task: Task) -> None:
        """看门狗：任务超时后的恢复流程

        【关键顺序】
        必须完成所有恢复步骤后才 task.event.set()，
        确保 HTTP 调用方收到错误时 xiadan.exe 已重置为初始状态。

        恢复步骤:
        1. 截图存档
        2. ESC×2 关闭弹窗
        3. 重新激活窗口
        4. F1 切回默认界面
        5. 设置错误并释放等待（最后执行）
        """
        screenshot_path = None
        recovery_error = None

        self.logger.warning(
            f"任务超时！开始恢复流程 - 任务: {task.name}, "
            f"参数: {task.params}, 已耗时: {task.elapsed():.2f}s"
        )

        # 步骤 1: 截图存档
        try:
            screenshot_path = self.screenshot_util.capture_trading_window(
                prefix=f"timeout_{task.name}"
            )
            task.screenshot = screenshot_path
        except Exception as e:
            self.logger.error(f"超时截图失败: {str(e)}")

        # 步骤 2: 发送 ESC 关闭可能存在的弹窗
        try:
            self.window_service.send_key("ESC")
            time.sleep(0.3)
            self.window_service.send_key("ESC")
            time.sleep(0.3)
        except Exception as e:
            self.logger.error(f"超时 ESC 失败: {str(e)}")

        # 步骤 3: 重新激活 xiadan.exe 窗口
        try:
            trading_path = self.config.get_trading_app_path()
            if trading_path:
                self.window_service.activate_window(trading_path)
                time.sleep(0.2)
        except Exception as e:
            self.logger.error(f"超时激活窗口失败: {str(e)}")
            recovery_error = str(e)

        # 步骤 4: 切回 F1 默认界面
        try:
            self.window_service.send_key("F1")
            time.sleep(0.3)
        except Exception as e:
            self.logger.error(f"超时 F1 重置失败: {str(e)}")
            recovery_error = str(e)

        # 步骤 5: 所有恢复步骤完成，现在才释放 HTTP 等待
        task.is_timeout = True
        task.error = TaskTimeoutError(
            task_name=task.name,
            params=task.params,
            elapsed=task.elapsed(),
            screenshot=screenshot_path,
            recovery_error=recovery_error
        )
        task.event.set()

        self.logger.info(
            f"超时恢复完成 - 任务: {task.name}, 截图: {screenshot_path}, "
            f"xiadan.exe 已重置为初始状态，HTTP 错误已返回给调用方"
        )

    def get_status(self) -> dict:
        """获取队列状态"""
        with self._lock:
            current = self._current_task
            current_duration = current.elapsed() if current else None

        return {
            "queue_size": self._queue.qsize(),
            "max_size": self._max_size,
            "worker_alive": self._worker.is_alive(),
            "current_task": current.name if current else None,
            "current_task_duration": current_duration,
            "is_zombie": current_duration is not None and current_duration > 60
        }
