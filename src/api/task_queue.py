"""全局任务队列

核心特性:
- 单 worker 线程: 所有操作顺序执行，避免 xiadan.exe 并发冲突
- 看门狗机制: 任务超时触发截图+激活+ESC×3 恢复，完成后才返回 HTTP 错误
- 状态重置: 每个任务开始前 激活 + ESC×3（重置到 F1 买入界面）
- 僵尸检测: 超过阈值的任务被标记
- 队列限制: 最大 50 个待处理任务

【关键设计】
看门狗触发后，必须完成所有恢复步骤后才 task['event'].set()，
确保 HTTP 调用方收到 TASK_TIMEOUT 错误时，xiadan.exe 已重置为初始状态。
"""
import queue
import threading
import time
from collections import deque
from typing import Callable, Any, Optional, List

from src.exceptions import TaskTimeoutError, ApiError, ErrorCode
from src.models.config import AppConfig
from src.services.window_service import WindowService
from src.utils.diagnostic import DiagnosticUtil
from src.utils.logger import Logger
from src.utils.screenshot import ScreenshotUtil
from src.utils.singleton import Singleton


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


class TaskQueue(Singleton):
    """全局任务队列（单例）"""

    @classmethod
    def get_instance(cls) -> "TaskQueue":
        return cls._get_instance()

    def _init(self):
        self.logger = Logger.get_instance()
        self.config = AppConfig()
        self.window_service = WindowService()

        logging_cfg = self.config.get_logging_config()
        self.screenshot_util = ScreenshotUtil(
            logging_cfg.get("screenshot_dir", "logs/screenshots")
        )

        queue_config = self.config.get_task_queue_config()
        self._max_size = queue_config.get("max_size", 50)

        self._queue: queue.Queue = queue.Queue(maxsize=self._max_size)
        self._current_task: Optional[Task] = None
        self._lock = threading.Lock()

        # 诊断快照历史（自动记录最近 20 步操作后的界面状态）
        self._diagnostic_history: deque = deque(maxlen=20)
        self._diag_lock = threading.Lock()

        # 连续同向订单优化：跟踪上次任务状态，避免重复的准备操作
        # 如 买入→买入 时跳过 _reset_trading_window + 激活 + F1
        self._last_task_info: Optional[dict] = None
        self.skip_window_setup = False  # Trader 读取此标志决定是否跳过准备

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
                # 连续同向订单优化：买入→买入 或 卖出→卖出 跳过窗口准备
                # 上次任务成功后窗口仍停留在对应界面，无需重置/激活/按键
                self.skip_window_setup = self._can_skip_window_setup(task)
                if not self.skip_window_setup:
                    self._reset_trading_window()

                # 执行任务
                task.result = task.func()
                self.logger.info(f"任务完成: {task.name}, 耗时 {task.elapsed():.2f}s")

            except Exception as e:
                task.error = e
                self.logger.error(f"任务失败: {task.name}, 错误: {str(e)}")

            finally:
                watchdog.cancel()

                # 跟踪任务状态：成功则记录，失败则清除（状态不确定）
                # 例外：价格超限等「干净退出」——窗口状态可信，保留以便下次同向跳过
                if task.error is None and not task.is_timeout:
                    self._update_task_state(task)
                elif self._is_clean_dismiss():
                    self._update_task_state(task)
                else:
                    self._last_task_info = None

                # 自动诊断：仅在任务失败时记录界面状态
                if task.error is not None:
                    self._auto_diagnostic(task)

                with self._lock:
                    self._current_task = None

                # 若看门狗已判定超时并设置错误，丢弃迟到的 result/error
                if task.is_timeout:
                    self.logger.warning(
                        f"任务 {task.name} 在超时后才完成，丢弃迟到结果 "
                        f"(耗时 {task.elapsed():.2f}s)"
                    )
                    # 保持看门狗设置的 TaskTimeoutError，不覆盖
                    task.result = None

                task.event.set()
                self._queue.task_done()

    # ── 连续跳过：操作分组 ──────────────────────────────────
    # 同组内上笔干净退出 → 跳过窗口重置。不同组 = 接口不同 → 必须重置。
    # 例：trade 组内买→卖只需 F2，但 trade→cancel 必须完整重置（F1≠F3）。

    _OPERATION_GROUPS = {
        "place_order": "trade",
        "cancel_all_orders": "cancel",
        # 查询类操作：都基于 F4 面板，同组内连续可跳过 F4+导航
        "get_position": "query",
        "get_balance": "query",
        "get_today_trades": "query",
        "get_today_orders": "query",
    }

    @classmethod
    def _get_operation_group(cls, task_name: str) -> str:
        return cls._OPERATION_GROUPS.get(task_name, task_name)

    @staticmethod
    def _read_had_dialog(task: Task) -> bool:
        """读取任务执行后的弹窗标志（统一入口）"""
        if task.name == "place_order":
            from src.core.trader import Trader
            return Trader._had_any_dialog
        if task.name == "cancel_all_orders":
            from src.services.trading_service import TradingService
            return TradingService._had_dialog
        # 查询/内部子任务：不涉及弹窗，窗口状态始终可信
        return False

    @staticmethod
    def _is_clean_dismiss() -> bool:
        """价格超限等「干净退出」——弹窗正常关闭，窗口状态可信"""
        from src.core.trader import Trader
        if Trader._clean_dismiss:
            Trader._clean_dismiss = False
            return True
        return False

    def _can_skip_window_setup(self, task: Task) -> bool:
        """上笔干净退出 + 同组操作 → 跳过窗口重置"""
        if self._last_task_info is None:
            return False
        if self._get_operation_group(task.name) != self._last_task_info.get("group"):
            return False
        return not self._last_task_info.get("had_dialog", True)

    def _update_task_state(self, task: Task) -> None:
        """记录任务成功后的窗口状态"""
        state = {
            "name": task.name,
            "group": self._get_operation_group(task.name),
            "had_dialog": self._read_had_dialog(task),
        }
        if task.name == "place_order":
            state["status"] = task.params.get("status")
        self._last_task_info = state

    def _reset_trading_window(self) -> None:
        """重置 xiadan.exe 到基准态（F1 买入界面）

        使用 WindowService.reset_window_state() 统一处理窗口激活 + ESC×5，
        确保窗口在前台且处于 F1 基准态。后续下单/撤单/查询方法各自发送
        F1/F3/F4 切换到目标视图。
        """
        try:
            self.window_service.reset_window_state()
        except Exception as e:
            self.logger.warning(f"重置交易窗口到基准态失败: {str(e)}")

    def _handle_timeout(self, task: Task) -> None:
        """看门狗：任务超时后的恢复流程

        【关键顺序】
        必须完成所有恢复步骤后才 task.event.set()，
        确保 HTTP 调用方收到错误时 xiadan.exe 已重置为初始状态。

        恢复步骤:
        1. 截图存档
        2. 重新激活窗口
        3. ESC×3 重置到 F1 买入界面（默认起点）
        4. 设置错误并释放等待（最后执行）
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

        # 步骤 2: 重新激活 xiadan.exe 窗口（恢复最小化 + 置前，确保 ESC 发到目标窗口）
        try:
            trading_paths = self.config.get_trading_app_paths()
            if trading_paths:
                self.window_service.activate_window(trading_paths)
                time.sleep(0.2)
        except Exception as e:
            self.logger.error(f"超时激活窗口失败: {str(e)}")
            recovery_error = str(e)

        # 步骤 3: ESC×3 重置到 F1 买入界面
        try:
            for i in range(3):
                self.window_service.send_key("ESC")
                time.sleep(0.2)
        except Exception as e:
            self.logger.error(f"超时 ESC 重置失败: {str(e)}")
            recovery_error = str(e)

        # 步骤 4: 所有恢复步骤完成，现在才释放 HTTP 等待
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

    def _auto_diagnostic(self, task: Task) -> None:
        """任务执行后自动诊断记录（无论成功失败）

        自动捕获界面状态并保存到历史队列。
        让我（AI 助手）可以随时通过 /diagnostic/history 查看每一步的界面状态。
        """
        try:
            info = DiagnosticUtil().snapshot(f"task_{task.name}")
            entry = {
                "task_name": task.name,
                "task_params": task.params,
                "elapsed_seconds": round(task.elapsed(), 2),
                "success": task.error is None,
                "error": str(task.error) if task.error else None,
                "timestamp": time.strftime("%H:%M:%S"),
                "ui_text": info.get("ui_text", ""),
                "ocr_text": info.get("ocr_text", ""),
                "screenshot": info.get("screenshot"),
            }
            with self._diag_lock:
                self._diagnostic_history.append(entry)
            self.logger.info(
                f"自动诊断 [{task.name}] 完成: "
                f"UI文本={len(info.get('ui_text','').split(chr(10)))}项"
            )
        except Exception as e:
            self.logger.warning(f"自动诊断失败 [{task.name}]: {e}")

    def get_diagnostic_history(self, n: int = 5) -> List[dict]:
        """获取最近的诊断历史

        Args:
            n: 返回最近几条记录（默认 5，最大 20）

        Returns:
            诊断记录列表（按时间倒序，最新的在前）
        """
        with self._diag_lock:
            history = list(self._diagnostic_history)
        # 按时间倒序返回（最新的在前）
        history.reverse()
        return history[:n]
