"""窗口监控服务

后台线程定期检查 xiadan.exe 是否最小化，自动恢复到前台
支持多个可能的 xiadan.exe 路径（免费版/远航版等）
"""
import threading
import time
from typing import Optional, Union, List

import psutil
import win32con
import win32gui
import win32process

from src.utils.logger import Logger
from src.constants import TRADING_WINDOW_TITLE
from src.models.config import AppConfig


class WindowMonitor:
    """窗口监控器

    监控目标窗口是否最小化或隐藏（托盘），自动恢复到前台。
    防止交易窗口不可见导致点击落空/快捷键失效。

    恢复两级：
    1. SW_SHOW/SW_RESTORE 软恢复（最小化到任务栏、隐藏到托盘的常规态）
    2. 软恢复连续 BAD_STATE_RELAUNCH_THRESHOLD 轮无效时，按 trading_app_paths
       重拉 exe 兜底——单实例客户端被再次启动会唤起既有窗口
       （不依赖屏幕坐标，桌面图标位置变化无影响）
    """

    # 软恢复连续无效多少轮后触发重拉（每轮间隔 check_interval）
    BAD_STATE_RELAUNCH_THRESHOLD = 3
    # 重拉冷却秒数，防止异常状态下高频拉起进程
    RELAUNCH_COOLDOWN_SECONDS = 60.0
    # 会话断开自愈：检查间隔（秒）；防抖与冷却时长见 session_monitor 配置
    SESSION_CHECK_INTERVAL = 10.0
    # WTS 连接状态枚举（win32ts）：0=Active 1=Connected 4=Disconnected
    WTS_STATE_DISCONNECTED = 4

    def __init__(self, check_interval: float = 2.0):
        self.logger = Logger.get_instance()
        self.check_interval = check_interval
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._target_app_paths: List[str] = []
        self._target_hwnd: Optional[int] = None
        self._lock = threading.Lock()
        self._bad_state_count = 0
        self._last_relaunch = 0.0
        self._last_session_recovery = 0.0
        # 会话首次被观察到断开的时刻（None=当前未处于断开态）——防抖起表点
        self._disconnected_since: Optional[float] = None

    def start(self, app_paths: Union[str, List[str]]) -> bool:
        """启动监控

        Args:
            app_paths: xiadan.exe 完整路径，支持单个 str 或 List[str]
        """
        with self._lock:
            if self._running:
                self.logger.warning("窗口监控已在运行中")
                return False

            if isinstance(app_paths, str):
                self._target_app_paths = [app_paths]
            else:
                self._target_app_paths = list(app_paths)

            if not self._target_app_paths:
                self.logger.warning("未配置 xiadan.exe 路径，窗口监控未启动")
                return False

            self._running = True

            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name="Window-Monitor"
            )
            self._monitor_thread.start()
            self.logger.info(f"窗口监控已启动，目标程序: {self._target_app_paths}")
            return True

    def stop(self) -> None:
        """停止监控"""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._target_hwnd = None
            self.logger.info("窗口监控已停止")

    def is_running(self) -> bool:
        return self._running

    def _find_target_window(self) -> Optional[int]:
        """根据 exe 路径列表查找窗口句柄（按配置顺序优先）

        快速路径：先按主窗口标题"网上股票交易系统5.0"精确匹配（微秒级），
        命中后才对候选窗口查进程 exe——避免每 2s 为每个顶层窗口创建
        psutil.Process。标题不匹配时（券商升级改标题/托盘子窗口）退回
        全量扫描（不检查 IsWindowVisible — 窗口可能被隐藏到系统托盘）。
        """
        paths_lower = [p.lower() for p in self._target_app_paths]
        if not paths_lower:
            return None

        found_windows = {}  # exe_lower -> hwnd

        title_matches = []

        def title_callback(hwnd, extra):
            if win32gui.GetWindowText(hwnd) == TRADING_WINDOW_TITLE:
                title_matches.append(hwnd)
            return True

        win32gui.EnumWindows(title_callback, None)

        for hwnd in title_matches:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                exe = psutil.Process(pid).exe().lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if exe in paths_lower:
                found_windows[exe] = hwnd

        if not found_windows:
            def callback(hwnd, extra):
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    proc = psutil.Process(pid)
                    exe = proc.exe().lower()
                    if exe in paths_lower:
                        found_windows.setdefault(exe, hwnd)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                return True

            win32gui.EnumWindows(callback, None)

        # 按配置顺序返回第一个匹配的窗口句柄
        if found_windows:
            for path in paths_lower:
                if path in found_windows:
                    return found_windows[path]
        return None

    def _restore_window(self, hwnd: int) -> bool:
        """恢复最小化或隐藏的窗口"""
        try:
            if not win32gui.IsWindow(hwnd):
                self.logger.warning("窗口句柄无效，尝试重新查找")
                return False

            # 先确保窗口可见（可能隐藏到系统托盘）
            if not win32gui.IsWindowVisible(hwnd):
                self.logger.info("窗口不可见，正在显示...")
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                time.sleep(0.1)

            # 恢复最小化
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.1)

            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                self._force_foreground(hwnd)

            self.logger.info("已自动恢复最小化窗口")
            return True
        except Exception as e:
            self.logger.error(f"恢复窗口失败: {str(e)}")
            return False

    def _force_foreground(self, hwnd: int) -> None:
        """强制将窗口置于前台（备用方案）"""
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            time.sleep(0.05)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception as e:
            self.logger.error(f"强制前台失败: {str(e)}")

    def _recover_session_if_disconnected(self) -> None:
        """会话断开自愈：RDP 直接断开（未执行 tscon 退出）时自动重挂 console

        RDP 普通断开会话进入"已断开"态（无活动桌面），click_input 全部
        失败。检测到本会话处于断开态时，对自己的会话执行
        tscon <id> /dest:console——以会话属主（Administrator）身份执行，
        重挂 console 并同步解除锁定（实测验证）。

        只在"已断开"（WTSDisconnected）时动作：有人正在交互使用
        （WTSActive/Connected）绝不干预——不会把正在使用的 RDP 会话
        劫持到 console 导致对方客户端掉线。

        防重连竞态（2026-10-06 实弹事故）：会话在「客户端走了」与
        「用户正在重新连接」两种场景下都处于断开态，仅凭状态无法区分
        ——重连换轨过渡（console→RDP）与输凭据期间均为断开态，此刻
        tscon 会与 RDP 附加撞车，可能把会话图形栈撞进不可自愈的僵死
        状态（实测：蓝屏「请稍后」→ 黑屏，最终只能注销重建）。三级
        防护均为概率消减而非根除，重连前最稳姿势仍是先关闭本自愈：
        1. session_monitor.enabled=false 完全禁用（reload-config 热生效）
        2. debounce_seconds：连续断开满此时长才动手——换轨过渡是秒级
           窗口，快速重连（凭据保存）不会满足防抖
        3. cooldown_seconds：两次自愈最小间隔——冷却期构成「保护窗」，
           分钟级短离开的重连天然落在窗内
        配置每次实时读取（含热重载），未配置时用内置默认值。
        """
        cfg = AppConfig().get_session_monitor_config()
        if not cfg.get("enabled", True):
            self._disconnected_since = None
            return
        cooldown = float(cfg.get("cooldown_seconds", 300))
        if time.time() - self._last_session_recovery < cooldown:
            return  # 保护窗内绝不动作（断开计时不清：持续性断开在冷却到期后立即满足防抖）
        try:
            import os
            import subprocess

            import win32ts

            state = win32ts.WTSQuerySessionInformation(
                win32ts.WTS_CURRENT_SERVER_HANDLE,
                win32ts.WTS_CURRENT_SESSION, win32ts.WTSConnectState)
            if isinstance(state, tuple):
                state = state[0]
            if state != self.WTS_STATE_DISCONNECTED:
                self._disconnected_since = None  # 会话恢复活动，防抖重新起表
                return

            now = time.time()
            debounce = float(cfg.get("debounce_seconds", 30))
            if self._disconnected_since is None:
                self._disconnected_since = now
                self.logger.info(f"会话进入断开态，{debounce:.0f}s 防抖计时开始")
                return
            if now - self._disconnected_since < debounce:
                return  # 断开不满防抖时长：可能是重连换轨的瞬时断开态

            sid = win32ts.ProcessIdToSessionId(os.getpid())
            self.logger.warning(
                f"会话持续断开已满 {debounce:.0f}s（state={state}），自愈执行: "
                f"tscon {sid} /dest:console")
            result = subprocess.run(
                ["tscon", str(sid), "/dest:console"],
                capture_output=True, text=True, timeout=15)
            self._last_session_recovery = time.time()
            self._disconnected_since = None  # 本次断开事件已处理，下次断开重新防抖
            if result.returncode == 0:
                self.logger.info("会话已重挂 console（含解除锁定），自动化恢复")
            else:
                self.logger.warning(
                    f"tscon 执行失败 rc={result.returncode}: "
                    f"{(result.stderr or '').strip()}")
        except Exception as e:
            self.logger.warning(f"会话断开自愈失败: {e}")

    def _process_window_state(self, hwnd: int) -> None:
        """按窗口可见性分派恢复动作（最小化/隐藏 → 软恢复 → 重拉兜底）"""
        if win32gui.IsIconic(hwnd) or not win32gui.IsWindowVisible(hwnd):
            state = "最小化" if win32gui.IsIconic(hwnd) else "隐藏（托盘）"
            self.logger.info(f"检测到目标窗口已{state}，正在恢复...")
            self._bad_state_count += 1
            if not self._restore_window(hwnd):
                self._target_hwnd = None
            # 软恢复连续多轮无效 → 按路径重拉兜底
            if self._bad_state_count >= self.BAD_STATE_RELAUNCH_THRESHOLD:
                self._relaunch_app(hwnd)
                self._bad_state_count = 0
        else:
            self._bad_state_count = 0

    def _relaunch_app(self, hwnd: Optional[int] = None) -> bool:
        """重拉交易程序（软恢复无效时的兜底）

        单实例客户端被再次启动不会开第二个实例，而是唤起既有窗口，
        对"隐藏到托盘且 SW_SHOW 不生效"等异常态是最可靠的恢复手段。

        路径优先级：
        1. 正在运行进程自己的 exe（hwnd→PID→psutil）——多套安装并存时，
           按 trading_app_paths 顺序可能命中"存在但不是正在运行的那套"，
           启动另一套客户端只会弹自己的登录框、抢前台，恢复不了目标窗口
        2. trading_app_paths 中第一个存在的路径（进程已死/句柄无效时的兜底）

        Returns:
            True=已发起重拉，False=冷却中或所有候选路径均不存在
        """
        import os

        now = time.time()
        if now - self._last_relaunch < self.RELAUNCH_COOLDOWN_SECONDS:
            self.logger.info(
                f"重拉冷却中（{self.RELAUNCH_COOLDOWN_SECONDS:.0f}s），跳过"
            )
            return False

        candidates: List[str] = []
        if hwnd:
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                running_exe = psutil.Process(pid).exe()
                if running_exe:
                    candidates.append(running_exe)
            except Exception as e:
                self.logger.warning(f"获取运行中进程路径失败，退回配置路径: {e}")
        # 去重，保持优先级顺序
        for path in self._target_app_paths:
            if path not in candidates:
                candidates.append(path)

        for path in candidates:
            if not os.path.exists(path):
                continue
            self.logger.warning(f"窗口持续不可达，按路径重拉交易程序: {path}")
            try:
                os.startfile(path)
                self._last_relaunch = now
                return True
            except Exception as e:
                self.logger.error(f"重拉交易程序失败 ({path}): {e}")

        self.logger.error(
            f"候选路径均不存在: {candidates}，无法重拉"
        )
        return False

    def _monitor_loop(self) -> None:
        self.logger.info("窗口监控线程已启动")
        consecutive_failures = 0
        startup_skip = True  # 启动初期跳过日志噪音
        next_session_check = 0.0

        while self._running:
            # 会话断开自愈（时间触发，独立于窗口检查节奏）
            try:
                if time.time() >= next_session_check:
                    self._recover_session_if_disconnected()
                    next_session_check = time.time() + self.SESSION_CHECK_INTERVAL
            except Exception as e:
                self.logger.warning(f"会话自愈检查异常: {e}")

            try:
                hwnd = self._find_target_window()
                if hwnd is None:
                    consecutive_failures += 1
                    self._bad_state_count = 0
                    if consecutive_failures >= 5:
                        if not startup_skip:
                            self.logger.warning("连续5次未找到目标窗口，请检查 xiadan.exe 是否已启动")
                        consecutive_failures = 0
                        startup_skip = False
                else:
                    consecutive_failures = 0
                    startup_skip = False
                    self._target_hwnd = hwnd
                    self._process_window_state(hwnd)
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    self.logger.warning(f"窗口监控连续失败 {consecutive_failures} 次: {str(e)}")
                if consecutive_failures >= 10:
                    self.logger.error(
                        f"窗口监控已连续失败 {consecutive_failures} 次，可能 xiadan.exe 未启动"
                    )

            time.sleep(self.check_interval)

        self.logger.info("窗口监控线程已退出")

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "target_apps": self._target_app_paths,
            "target_hwnd": self._target_hwnd,
            "check_interval": self.check_interval
        }
