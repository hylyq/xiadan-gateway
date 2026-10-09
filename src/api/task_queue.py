"""全局任务队列

核心特性:
- 单 worker 线程: 所有操作顺序执行，避免 xiadan.exe 并发冲突
- 看门狗机制: 任务超时触发截图+激活+ESC×3 恢复，完成后才返回 HTTP 错误
- 状态重置: 每个任务开始前 激活 + ESC×3（重置到 F1 买入界面）
- 僵尸检测: 超过阈值的任务被标记
- 队列限制: 最大 50 个待处理任务
- 会话健康门: 任务执行前检查 WTS 连接状态，RDP 断开态毫秒级快速拒绝
  （SESSION_UNAVAILABLE，任务未执行；fail-open，见 _session_gate_reject）

【关键设计】
看门狗触发后，必须完成所有恢复步骤后才 task['event'].set()，
确保 HTTP 调用方收到 TASK_TIMEOUT 错误时，xiadan.exe 已重置为初始状态。
"""
import functools
import queue
import threading
import time
from collections import deque
from typing import Callable, Any, Optional, List

from src.exceptions import TaskTimeoutError, ApiError, ErrorCode
from src.models.config import AppConfig
from src.services.window_monitor import WindowMonitor
from src.services.window_service import WindowService
from src.utils.session_state import (
    get_session_state, state_name, WTS_DISCONNECTED,
    record_desktop_failure, record_desktop_success
)
from src.utils.alert import send_alert
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
        # 会话健康门拒绝标记：任务因 RDP 断开未执行即被拒——
        # 统计上计入 recent_tasks 但豁免连续失败计数（那是「xiadan.exe
        # 异常」告警，文案会误导；会话断开是已知基础设施不可用）
        self.precheck_rejected: bool = False
        # 业务代码通过 report_window_state 装饰器写入的窗口状态
        # {"had_dialog": bool, "clean": bool} —— 连续同组跳过的决策依据
        self.window_state: Optional[dict] = None

    def elapsed(self) -> float:
        if self.start_time is None:
            return 0
        return time.time() - self.start_time


def report_window_state(fn):
    """业务方法装饰器：方法结束时把窗口状态同步到当前任务

    替代原先 TaskQueue 读取业务类变量的隐式契约——
    业务方法无需知道 TaskQueue 的读取时机，结束时自动上报。

    window_state = {"had_dialog": bool, "clean": bool}
    TaskQueue 据此决定连续同组操作能否跳过窗口准备。

    用法:
        @report_window_state
        def place_order(self, ...): ...
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        finally:
            TaskQueue.get_instance()._record_window_state(self)
    return wrapper


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

        # 运行统计（#12）：按错误码聚合成功率 + 连续失败告警
        self._stats_lock = threading.Lock()
        self._recent_tasks: deque = deque(maxlen=500)  # (timestamp, success, error_code)
        self._consecutive_failures = 0                 # 连续失败计数（告警阈值 3）

        # 下单确认弹窗行为跟踪：客户端「快速交易」设置漂移的被动检测。
        # place_order 的弹窗出现与否反映客户端确认弹窗设置——同一客户端
        # 会话内该行为突然翻转（无弹窗→有弹窗）通常意味着设置被重置/
        # 券商升级复原，需人工检查（只告警不拦截，判定仅作参考信号）
        self._order_dialog_stats: deque = deque(maxlen=200)  # (timestamp, had_dialog)
        self._last_order_had_dialog: Optional[bool] = None

        # 连续同向订单优化：跟踪上次任务状态，避免重复的准备操作
        # 如 买入→买入 时跳过 _reset_trading_window + 激活 + F1。
        # 业务代码通过 consume_window_setup_skip() / get_last_task_info()
        # 访问，不直接读写内部属性
        self._last_task_info: Optional[dict] = None
        self._skip_window_setup = False

        # 会话不可用事件日志去重（worker 线程私有，与 _login_hold_active
        # 同模式）：进入断开拒首个任务记一条 warning、恢复后首个任务记
        # 一条 info，不逐任务刷屏。仅在观察到「非 None 健康态」时复位——
        # None（查询失败）不复位，防查询抖动反复刷「恢复」日志
        self._session_unavailable_logged = False

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

    def _session_gate_reject(self, task: Task) -> bool:
        """会话健康门：任务执行前检查 WTS 连接状态，断开态快速拒绝

        背景：RDP 直接断开会话进入 WTSDisconnected 态（无活动桌面），
        click_input/前台发键全部失败，到自愈完成之间存在不可用阶段
        （典型 ~40s = 检查间隔 10s + 防抖 30s；冷却期内再次断开最长
        ~340s）。此前任务照常执行，落空/挂起后等 30s 看门狗报
        TASK_TIMEOUT——既慢又触发 task_timeout 告警噪音。

        判定：只拦 WTSDisconnected(4)——与 WindowMonitor 自愈动作条件
        精确镜像（单一事实源 src/utils/session_state.py）。直查而非读
        监控线程维护的标志位：无检测盲窗（断开即查得 4）、不依赖监控
        线程存活/开关（手动编排重连热关闭自愈时门照常工作）。

        fail-open（双重）：查询返回 None（win32ts 异常）放行，门自身
        任何异常放行——误放行由看门狗兜底（最坏 = 现状），误拒绝没有
        兜底。Connected(1) 等附加过渡瞬态放行：秒级瞬态撞上概率与
        无门时代相同，未变差。

        拒绝路径不触碰任何 UI：不起看门狗、不弹窗清扫、不做窗口准备
        （后两者本身是 UI 操作，正是要防的）；也不清 _last_task_info
        （窗口未被触碰，上笔干净退出的连续跳过依据跨不可用窗口仍成立，
        恢复后首个同组任务仍能享受跳过优化）。幂等语义：此错误不在
        should_keep_record_on_error 的保留列表 → 路由层自动清除记录，
        同 Idempotency-Key 立即可重试（与 TASK_TIMEOUT 相反：那是
        「可能已执行」必须保留）。

        开关 task_queue.session_gate_enabled 每次实时读取（支持
        /admin/reload-config 热切换逃生）。

        Returns:
            True=任务已被拒绝并完成善后（event/task_done/统计），worker
            应 continue；False=放行，正常执行
        """
        try:
            enabled = self.config.get_task_queue_config().get(
                "session_gate_enabled", True)
            if not enabled:
                return False
            state = get_session_state()
        except Exception as e:  # 双保险：门自身故障绝不阻塞 worker 主循环
            self.logger.warning(f"会话门检查异常（放行任务）: {e}")
            return False

        if state != WTS_DISCONNECTED:
            # None（查询失败）同样走到这里 → fail-open 放行
            if state is not None and self._session_unavailable_logged:
                self._session_unavailable_logged = False
                self.logger.info(
                    f"会话已恢复（{state_name(state)}），任务恢复正常执行")
            return False

        # 恢复时长按「区间+条件」表述（实时从配置算术得出），不承诺精确
        # ETA——worker 无从知晓监控线程当前处于防抖第几秒、是否在冷却期
        sm_cfg = self.config.get_session_monitor_config()
        debounce = float(sm_cfg.get("debounce_seconds", 30))
        cooldown = float(sm_cfg.get("cooldown_seconds", 300))
        interval = WindowMonitor.SESSION_CHECK_INTERVAL
        typical = debounce + interval
        worst = cooldown + debounce + interval

        task.precheck_rejected = True
        task.error = ApiError(
            error_code=ErrorCode.SESSION_UNAVAILABLE,
            message=(f"RDP 会话处于断开态（WTSConnectState=4/Disconnected），"
                     f"桌面不可用，任务 {task.name} 未执行"),
            suggestion=(
                f"任务确定未执行，未触碰券商客户端。会话自愈通常 ~{typical:.0f}s"
                f"内完成（防抖 {debounce:.0f}s + 检查间隔 {interval:.0f}s）；"
                f"若自愈冷却期（{cooldown:.0f}s）内再次断开，最长可延迟至 "
                f"~{worst:.0f}s。可轮询 GET /health 的 session.ui_available"
                f"=true 后重试；下单重试请复用同一 Idempotency-Key"
                f"（本次记录已自动清除，安全）"
            ),
            details={
                "task": task.name,
                "params": task.params,
                "connect_state": state,
                "state_name": state_name(state),
                "typical_recovery_seconds": round(typical),
                "worst_case_recovery_seconds": round(worst),
            },
        )
        if not self._session_unavailable_logged:
            self._session_unavailable_logged = True
            self.logger.warning(
                f"会话处于断开态，任务将被快速拒绝（SESSION_UNAVAILABLE）"
                f"直至自愈恢复（通常 ~{typical:.0f}s，冷却期场景最长 "
                f"~{worst:.0f}s）")

        # 善后全部完成后才释放调用方：submit 返回错误时统计已落账，
        # 调用方观察到的状态是确定性的（event.set() 放最后）
        self._queue.task_done()  # 每次 get() 必须配对，否则 queue.join() 永久挂起
        self._record_task_outcome(task)  # 计入统计，豁免连续失败计数
        task.event.set()
        return True

    def _worker_loop(self) -> None:
        """工作线程主循环"""
        self.logger.info("任务 worker 线程已启动")

        while True:
            task = self._queue.get()
            task.start_time = time.time()

            # 会话健康门：断开态快速拒绝（在 current_task 设置/看门狗启动/
            # 任何窗口准备之前——continue 在下方 try/finally 之外，拒绝路径
            # 天然跳过全部 UI 触碰与状态跟踪）
            if self._session_gate_reject(task):
                continue

            with self._lock:
                self._current_task = task

            # 启动看门狗
            watchdog = threading.Timer(task.timeout, self._handle_timeout, args=(task,))
            watchdog.daemon = True
            watchdog.start()

            try:
                # 残留弹窗清扫：必须在窗口复位/激活之前——弹窗持有前台时，
                # 激活逻辑的 click_input 真实点击会落在弹窗按钮上（实测：
                # 连按弹窗"确定"数次），既无效又危险；先安全关闭（含存档）
                # 恢复干净窗口，再做任何真实点击
                self._sweep_leftover_dialogs()

                # 连续同向订单优化：买入→买入 或 卖出→卖出 跳过窗口准备
                # 上次任务成功后窗口仍停留在对应界面，无需重置/激活/按键
                self._prepare_window_for_task(task)

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
                elif (task.window_state or {}).get("clean"):
                    self._update_task_state(task)
                else:
                    self._last_task_info = None
                    self.logger.debug(
                        f"任务 {task.name} 失败/状态不确定，清除连续跳过状态"
                    )

                # 自动诊断：仅在任务失败时记录界面状态
                # （快照在下方 event.set() 之后的 finally 尾部执行）

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
                self._record_task_outcome(task)

                # 自动诊断：仅任务失败时记录界面状态。
                # 放在 event.set() 之后——诊断是事后排查数据，不应阻塞 HTTP 响应
                # （截图+UIA 遍历 ~2s）。worker 串行执行，下一任务在本 finally
                # 结束前不会开始，快照界面状态仍与失败时刻一致。
                if task.error is not None:
                    self._auto_diagnostic(task)

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

    def consume_window_setup_skip(self) -> bool:
        """读取并清除窗口准备跳过标志（单次消耗语义）

        worker 在任务开始前依据「上笔同组干净退出」设置；业务方法
        （Trader.place_order / PositionService._prepare_query_panel）
        消费一次即复位，同一次任务内不会重复生效。
        """
        skip = self._skip_window_setup
        self._skip_window_setup = False
        return skip

    def get_last_task_info(self) -> dict:
        """上笔任务状态快照（只读副本；无记录时返回空 dict）

        键：name / group / had_dialog / status（仅 place_order 有 status）。
        """
        return dict(self._last_task_info) if self._last_task_info else {}

    @classmethod
    def _get_operation_group(cls, task_name: str) -> str:
        return cls._OPERATION_GROUPS.get(task_name, task_name)

    def _record_window_state(self, business_obj) -> None:
        """业务方法结束时回调：把业务窗口状态写入当前任务

        由 report_window_state 装饰器调用，业务对象通过实例属性
        _had_any_dialog（Trader）/ _had_dialog（TradingService）
        与 _clean_dismiss 上报窗口状态。
        查询等无装饰器任务 window_state 保持 None → had_dialog=False。
        """
        task = self._current_task
        if task is None:
            return
        had_dialog = getattr(business_obj, "_had_any_dialog", None)
        if had_dialog is None:
            had_dialog = getattr(business_obj, "_had_dialog", False)
        task.window_state = {
            "had_dialog": bool(had_dialog),
            "clean": bool(getattr(business_obj, "_clean_dismiss", False)),
        }

    def _can_skip_window_setup(self, task: Task) -> bool:
        """上笔干净退出 + 同组操作 → 跳过窗口重置

        debug 日志（logging.level=DEBUG 时可见）记录每次决策及原因，
        用于排查性能异常：该跳过没跳过（多余重置）或不该跳过却跳了
        （窗口状态可能被上笔失败污染）。
        """
        last = self._last_task_info
        if last is None:
            self.logger.debug(
                f"跳过窗口准备: 否（无上笔任务记录, 组={self._get_operation_group(task.name)}）"
            )
            return False
        can_skip = (
            self._get_operation_group(task.name) == last.get("group")
            and not last.get("had_dialog", True)
        )
        self.logger.debug(
            f"跳过窗口准备: {'是' if can_skip else '否'} "
            f"(任务={task.name}, 组={self._get_operation_group(task.name)}, "
            f"上笔组={last.get('group')}, 上笔弹窗={last.get('had_dialog')}, "
            f"上笔状态={last.get('status')})"
        )
        return can_skip

    def _update_task_state(self, task: Task) -> None:
        """记录任务成功后的窗口状态"""
        ws = task.window_state or {}
        state = {
            "name": task.name,
            "group": self._get_operation_group(task.name),
            "had_dialog": ws.get("had_dialog", False),
        }
        if task.name == "place_order":
            state["status"] = task.params.get("status")
        self._last_task_info = state
        self.logger.debug(f"任务状态更新（连续跳过依据）: {state}")

    def _sweep_leftover_dialogs(self) -> None:
        """任务开始前清扫残留弹窗（只关闭，绝不求解/点确认）

        复用 PositionService 的清扫能力（顶层 + 主窗口子弹窗两种形态，
        关窗前存档证据，安全关闭：取消按钮/WM_CLOSE）。无弹窗时开销 ~10ms。
        """
        try:
            from src.services.position_service import PositionService
            PositionService(self.window_service)._sweep_leftover_dialogs()
        except Exception as e:
            # 清扫失败不阻断任务——任务自身的激活/发键校验会兜底报错
            self.logger.debug(f"任务前弹窗清扫跳过: {e}")

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

    def _prepare_window_for_task(self, task: Task) -> None:
        """任务前窗口准备：决定重置或跳过；跳过路径仍做位置自愈

        跳过激活的优化不豁免位置自愈——连续同组任务期间窗口被拖出屏幕
        时，click_input/截图按屏幕坐标工作会落空（2026-10-06 实弹：窗口
        21% 可见时连续查询跳过了位置自愈，查询碰巧成功属侥幸）。检查
        本身是 GetWindowRect 级开销，仅在确实出屏时才发生实际移动。
        """
        self._skip_window_setup = self._can_skip_window_setup(task)
        if not self._skip_window_setup:
            self._reset_trading_window()
        else:
            self._ensure_window_in_workarea()

    def _ensure_window_in_workarea(self) -> None:
        """跳过窗口准备路径上的位置自愈（不激活、无按键，失败不阻塞任务）"""
        try:
            window = self.window_service.get_trading_window()
            if window is not None:
                self.window_service.ensure_window_onscreen(window.handle)
        except Exception as e:
            self.logger.warning(f"跳过路径窗口位置自愈失败: {e}")

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

        # 任务超时意味着「订单状态未知」——即使连续失败计数为 0 也要外推
        # （下一条成功任务会把计数清零，但这一单的风险不受影响）
        send_alert(
            "task_timeout",
            f"任务超时: {task.name}",
            f"任务 {task.name} 执行超时（{task.elapsed():.1f}s），"
            f"已自动恢复窗口，订单状态未知请先核实再重试",
            details={"task": task.name,
                     "params": task.params,
                     "elapsed_seconds": round(task.elapsed(), 2),
                     "screenshot": screenshot_path,
                     "recovery_error": recovery_error},
            level="error",
        )

    def _record_task_outcome(self, task: Task) -> None:
        """记录任务结果到运行统计（#12）

        错误码从异常提取（ApiError 自带；未知异常归 INTERNAL_ERROR）。
        连续失败 ≥3 次触发告警日志，之后每 10 次递增提醒一次，
        帮助尽早发现 xiadan.exe/券商端持续异常。
        """
        error_code = None
        if task.error is not None:
            error_code = getattr(task.error, "error_code", ErrorCode.INTERNAL_ERROR)
        with self._stats_lock:
            self._recent_tasks.append((time.time(), task.error is None, error_code))
            # 会话门拒绝豁免连续失败计数：那是「xiadan.exe/券商持续异常」
            # 告警（文案指向人工检查客户端），而会话断开是已知的基础设施
            # 不可用（进入断开时已有一条事件级 warning），计数只会产生
            # 误导噪音——计数器原值保留（不增也不清零：断开期不该稀释
            # 此前真实异常的计数语义）
            if task.error is None:
                self._consecutive_failures = 0
            elif not getattr(task, "precheck_rejected", False):
                self._consecutive_failures += 1
                if self._consecutive_failures == 3:
                    self.logger.warning(
                        f"⚠ 连续 {self._consecutive_failures} 次任务失败"
                        f"（最近: {error_code}）——请检查 xiadan.exe/券商状态！"
                    )
                    send_alert(
                        "consecutive_failures",
                        f"连续 {self._consecutive_failures} 次任务失败",
                        f"最近错误: {error_code}，请检查 xiadan.exe/券商状态",
                        details={"consecutive_failures": 3,
                                 "last_error_code": error_code},
                    )
                elif (self._consecutive_failures > 3
                      and self._consecutive_failures % 10 == 0):
                    self.logger.warning(
                        f"⚠ 任务已连续失败 {self._consecutive_failures} 次"
                        f"（最近: {error_code}）——建议立即人工检查！"
                    )
                    send_alert(
                        "consecutive_failures",
                        f"任务已连续失败 {self._consecutive_failures} 次",
                        f"最近错误: {error_code}，建议立即人工检查",
                        details={"consecutive_failures": self._consecutive_failures,
                                 "last_error_code": error_code},
                        level="error",
                    )

        # 桌面僵死追踪喂入（session_state 单一事实源；消费方=升级自愈+/health）。
        # 会话门拒绝豁免——门拒时未触碰任何 UI，不构成桌面可操作性证据
        # （与上方连续失败计数的豁免同源语义）。指纹双通道：分类码直接命中；
        # 裸 RuntimeError（reset_window_state 等未包裹路径）按文本兜底。
        if task.error is None:
            record_desktop_success()
        elif not getattr(task, "precheck_rejected", False):
            if (error_code == ErrorCode.SESSION_DESKTOP_UNAVAILABLE
                    or "no active desktop" in str(task.error).lower()):
                record_desktop_failure()

        self._track_order_dialog_behavior(task)

    def _track_order_dialog_behavior(self, task: Task) -> None:
        """跟踪下单确认弹窗行为，检测客户端快速交易设置漂移

        place_order 出现委托确认弹窗 = 客户端未开快速交易；完全无弹窗 =
        快速交易模式（下单成败判定依赖此前提，见 README 已知限制）。
        同一会话内行为突然翻转 → 大概率设置被重置/券商升级复原，
        告警提示人工检查。仅统计窗口状态已上报的任务（超时僵尸任务
        window_state=None，跳过不计）。
        """
        if task.name != "place_order" or task.window_state is None:
            return
        had_dialog = bool(task.window_state.get("had_dialog"))
        with self._stats_lock:
            self._order_dialog_stats.append((time.time(), had_dialog))
            previous = self._last_order_had_dialog
            self._last_order_had_dialog = had_dialog
        if previous is not None and previous != had_dialog:
            old_mode = "快速交易（无弹窗）" if not previous else "弹窗确认"
            new_mode = "快速交易（无弹窗）" if not had_dialog else "弹窗确认"
            self.logger.warning(
                f"⚠ 下单确认弹窗行为变化: {old_mode} → {new_mode} ——"
                f"客户端「快速交易」设置可能被重置/券商升级复原，"
                f"请人工检查客户端设置（影响无弹窗=已提交的判定）"
            )
            send_alert(
                "order_dialog_drift",
                "下单确认弹窗行为变化",
                f"{old_mode} → {new_mode}——客户端「快速交易」设置可能"
                f"被重置/券商升级复原，请人工检查（影响无弹窗=已提交判定）",
                details={"from": old_mode, "to": new_mode},
            )

    def get_stats(self, window_seconds: int = 3600) -> dict:
        """运行统计（#12）：按错误码聚合成功率 + 连续失败状态

        滑动窗口默认最近 1 小时（deque 保留最近 500 次）。
        返回 success_rate=None 表示窗口内无任务（无法计算）。

        Returns:
            {
                "window_seconds": 3600,
                "total_tasks": N,
                "success_count": N,
                "failure_count": N,
                "success_rate": 0.95,
                "error_counts": {"TASK_TIMEOUT": 2, ...},  # 按次数降序
                "consecutive_failures": 0,
            }
        """
        now = time.time()
        with self._stats_lock:
            recent = list(self._recent_tasks)
            consecutive = self._consecutive_failures
            order_dialogs = list(self._order_dialog_stats)
            last_order_had_dialog = self._last_order_had_dialog

        windowed = [t for t in recent if now - t[0] <= window_seconds]
        total = len(windowed)
        success = sum(1 for _, ok, _ in windowed if ok)

        error_counts = {}
        for _, ok, code in windowed:
            if not ok:
                error_counts[code] = error_counts.get(code, 0) + 1

        # 下单确认弹窗统计：无弹窗占比异常（如一直有弹窗）提示检查客户端设置
        windowed_dialogs = [hd for ts, hd in order_dialogs if now - ts <= window_seconds]
        with_dialog = sum(1 for hd in windowed_dialogs if hd)

        return {
            "window_seconds": window_seconds,
            "total_tasks": total,
            "success_count": success,
            "failure_count": total - success,
            "success_rate": round(success / total, 3) if total else None,
            "error_counts": dict(sorted(error_counts.items(), key=lambda x: -x[1])),
            "consecutive_failures": consecutive,
            "order_confirm_dialog": {
                "total_orders": len(windowed_dialogs),
                "with_confirm_dialog": with_dialog,
                "no_dialog_fast_trade": len(windowed_dialogs) - with_dialog,
                "last_order_had_dialog": last_order_had_dialog,
            },
        }

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
        """任务失败后自动诊断记录（成功任务不记录）

        自动捕获界面状态并保存到历史队列。
        让我（AI 助手）可以随时通过 /diagnostic/history 查看失败时的界面状态。
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
