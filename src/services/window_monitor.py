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
from src.utils.alert import send_alert
from src.utils.session_state import (
    WTS_DISCONNECTED, get_session_state, state_name, desktop_wedge_status
)


class WindowMonitor:
    """窗口监控器

    监控目标窗口是否最小化或隐藏（托盘），自动恢复到前台。
    防止交易窗口不可见导致点击落空/快捷键失效。

    恢复两级：
    1. SW_SHOW/SW_RESTORE 软恢复（最小化到任务栏、隐藏到托盘的常规态）
    2. 软恢复连续 BAD_STATE_RELAUNCH_THRESHOLD 轮无效时，按 trading_app_paths
       重拉 exe 兜底——单实例客户端被再次启动会唤起既有窗口
       （不依赖屏幕坐标，桌面图标位置变化无影响）

    恢复前置的登录/启动期避让（_login_or_startup_hold）：先启动网关、
    后启动并登录券商客户端时，主窗口在登录期以隐藏态预创建，被 exe
    全量扫描兜底命中——此时 SW_SHOW/抢前台会与客户端登录绘制竞争，
    造成窗口元素图标错位（2026-10-08 实测）。避让期不软恢复、不累计
    坏状态轮数、不重拉，等客户端自行完成登录绘制。窗口被客户端自行
    显示过一次即视为登录完成，年龄判据对该进程永久解除（用户此后手动
    隐藏/最小化立即恢复，不等满宽限期——同日复测：重开客户端登录后
    手动隐藏被误等 90s）。
    """

    # 软恢复连续无效多少轮后触发重拉（每轮间隔 check_interval）
    BAD_STATE_RELAUNCH_THRESHOLD = 3
    # 重拉冷却秒数，防止异常状态下高频拉起进程
    RELAUNCH_COOLDOWN_SECONDS = 60.0
    # 会话断开自愈：检查间隔（秒）；防抖与冷却时长见 session_monitor 配置
    SESSION_CHECK_INTERVAL = 10.0
    # 「疑似重连让路」只作用于断开事件的此时长内（秒）：mstsc 停在密码框
    # 挂几小时时不能无限期阻塞自愈——超过上限照常动作，撞车概率回到
    # ①+② 三级防护水平（防抖+冷却仍生效）
    RECONNECT_HOLD_MAX_SECONDS = 600.0
    # RDP 监听端口（判别「客户端正在连接」用；默认 3389）
    RDP_LISTEN_PORT = 3389
    # WTS 断开态常量（与任务队列会话健康门同源；见 src/utils/session_state.py）
    WTS_STATE_DISCONNECTED = WTS_DISCONNECTED
    # 登录/启动期避让：目标进程年龄低于此时长时不干预窗口（秒）。
    # 另有两级与时长无关的独立判据（登录框/同进程前台窗口），
    # 见 _login_or_startup_hold；0=关闭年龄判据（另两级判据仍生效）
    LOGIN_GRACE_SECONDS = 90.0

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
        # 桌面僵死升级自愈（tsdiscon）上次执行时刻——独立于 tscon 冷却，
        # 两套冷却互不挤占（tsdiscon 后的 state=4 阶段交给既有链路接管）
        self._last_wedge_recovery = 0.0
        # 会话首次被观察到断开的时刻（None=当前未处于断开态）——防抖起表点
        self._disconnected_since: Optional[float] = None
        # 登录/启动期避让中（进出避让各记一条日志，避免每 2s 刷屏）
        self._login_hold_active = False
        # 本进程生命周期内目标窗口是否被见过可见（客户端自行显示过一次
        # = 登录完成铁证，年龄判据即解除）；按 PID 记账，见 _process_window_state
        self._target_pid: Optional[int] = None
        self._seen_visible = False

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
            self._login_hold_active = False
            self._target_pid = None
            self._seen_visible = False
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

        防重连竞态——设计与存档（2026-10-06 事故 → 四轮实验 → 四层防护）：

        【事故】分钟级短离开后返回，自愈冷却恰在重连途中到期，tscon 与
        RDP 附加并发执行——会话图形栈僵死（蓝屏「请稍后」→ 黑屏，dwm
        不再重生，重连无法修复），只能 logoff 会话重建（全程 ~10 分钟）。
        根因：会话在「客户端走了」与「用户正在重连」（输凭据期间 /
        console→RDP 换轨过渡）两种场景下同为断开态，仅凭状态不可判别。

        【实验结论】（scripts/probe_session_signals.py + netstat 采样）
        - 会话级信号不可用：密码界面期间 WTSClientName 恒为空、
          protocol=0，与「客户端走了」读数完全相同——附加完成那一刻才
          原子填充（Round A 证伪）
        - TCP 层可判别：客户端连上服务器即与 3389 建立 ESTABLISHED，
          输凭据全程保持；客户端真离开 ~1s 归零（Round B 证实）
        - 先自愈后连接 = 安全：附加到已挂 console 的活动会话是干净
          路径（Round C 实测）；危险的只有 tscon 与附加「并发」
          （Round C2 实测判别器让路、零 tscon）

        【四层防护与各自失效域】
        1. TCP 判别器（主防线）：并发附加的直接拦截，见下方检查点
        2. debounce（默认 30s）：吸收瞬时断开态——mstsc 自动重连窗口
           与常见短断网（<30s，含 WiFi 重关联 10-25s 档）。断网未恢复
           期间 TCP 建立不起来，判别器在断网全程是盲的，本层是该场景
           唯一防层——**不可设 0**（每次网络闪断都可能触发隐形 tscon），
           不建议低于 10s
        3. cooldown（默认 300s）：判别器失效（psutil 异常按无连接处理）
           时的硬保证窗；console 挂接后会话不会自发再断，实际代价≈0
        4. enabled 开关：运维逃生口（实验 / 手动编排重连）

        【残余（已接受）】TCP 检查与 tscon 执行非原子（TOCTOU：子进程
        拉起 + 换轨共 ~1-2s），Windows 不提供与 RDP broker 的协作锁，
        用户态无法归零。触发需「离开 >30s + 冷却过期 + 客户端恰在
        1-2s 缝内建连」三重巧合，单操作者频率≈噪音；真撞上有已验证
        的人工恢复路径（logoff 重建 ~10 分钟）。

        【已否决方案】客户端侧 reconnect 脚本（先热关自愈再拉 mstsc）
        ——重连时人不在会话内，脚本只能跑在本地机器上调网关 API，网关
        就必须监听非 127.0.0.1。为封秒级理论缝把交易网关暴露到网络，
        负收益，不做。架构级归零的唯一路径是 VNC-only 工作流（无 RDP
        传输 → 无需 tscon → 竞态存在前提消失）。

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

            import win32ts  # ProcessIdToSessionId 仍需

            state = get_session_state()
            if state is None:
                # 查询失败跳过本周期（防抖计时状态不动）。与任务队列会话门
                # 的 fail-open 不对称是刻意的：门的误放行由看门狗兜底，
                # tscon 的误动作撞重连是图形栈僵死级事故（见本方法 docstring
                # 存档）——对不确定状态宁可不动
                self.logger.debug("会话状态查询失败，跳过本周期自愈检查")
                return
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
                # 瞬时断开态不动作：mstsc 自动重连窗口 / 网络闪断（<30s）。
                # 断网未恢复期间 TCP 建立不起来，TCP 判别器在该时段是盲区，
                # 本层是其唯一兜底——不可设 0（依据见方法 docstring 存档）
                return

            # 重连判别（实测 2026-10-06 两轮实验）：会话级信号（WTSClientName）
            # 在附加完成前不可见——密码界面期间 client_name 恒为空，与「客户端
            # 走了」无法区分；TCP 层是唯一先行信号：客户端连上即与 3389 建立
            # ESTABLISHED，输凭据全程保持。断开态下检测到该连接 = 用户正在
            # 重连，本次让路。仅作用于断开事件初期（上限内），防 mstsc 挂在
            # 密码框无限期阻塞自愈。
            if (now - self._disconnected_since <= self.RECONNECT_HOLD_MAX_SECONDS
                    and self._rdp_client_connecting()):
                self.logger.info(
                    "断开态下 RDP 端口存在 ESTABLISHED 连接——疑似用户正在"
                    "重连，本次自愈让路")
                return

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

    def _recover_desktop_if_wedged(self) -> None:
        """桌面僵死升级自愈（形态二：WTS 已挂接但输入/图形路径无响应）

        2026-10-09 事故：tscon 自愈（20:05:31）后用户 RDP 重连（20:07:42）
        撞车，会话呈 Active 但 SetCursorPos error 0 / screen grab 失败 /
        前台恒 0，全部 UI 任务失败约 20 分钟（用户真实点击才复位）。既有
        自愈只认 state==4，本态永不触发；且 tscon 在已挂 console 时是
        no-op，治不了本态。本态与 2026-10-06 图形栈僵死事故（见
        _recover_session_if_disconnected docstring 存档）同族——都是
        tscon/附加竞态的产物，只是程度更轻（真实输入可复位 vs logoff 重建）。

        动作：tsdiscon <sid> 强制走一次 RDP 断开-重连周期（「重连RDP复位」
        实测有效）。有客户端时 mstsc 自动重连（秒级）；无客户端时进入的
        state=4 阶段完全交给 _recover_session_if_disconnected——30s 防抖 +
        TCP 判别器让路（ESTABLISHED ≤600s 不 tscon）天然防止 tscon 撞
        客户端重连（本次事故毒源）。本方法不碰 _last_session_recovery，
        只用自己的独立冷却（wedge_cooldown_seconds，默认 600s）。

        触发条件（session_state 桌面僵死追踪器）：连续 ≥2 次桌面级激活
        失败（SESSION_DESKTOP_UNAVAILABLE / "no active desktop" 指纹）
        且 15min 内有复现。成功任务即清零——成功本身就是「已复位」
        最清晰的探针，无需额外 UI 探测。

        fail-closed：state 未知（None）不动作。门的误放行由任务失败兜底，
        tsdiscon 的误动作断的是用户正在使用的 RDP——代价不对称，对
        不确定状态宁可不动（与 tscon 侧同一哲学）。
        """
        cfg = AppConfig().get_session_monitor_config()
        if not cfg.get("wedge_heal_enabled", True):
            return
        cooldown = float(cfg.get("wedge_cooldown_seconds", 600))
        if time.time() - self._last_wedge_recovery < cooldown:
            return
        wedge = desktop_wedge_status()
        if not wedge.get("wedged"):
            return
        state = get_session_state()
        if state is None or state == self.WTS_STATE_DISCONNECTED:
            return  # 断开态归既有链路管（避免双动作竞态）；未知态 fail-closed
        try:
            import os
            import subprocess

            import win32ts  # ProcessIdToSessionId 仍需

            sid = win32ts.ProcessIdToSessionId(os.getpid())
            self.logger.warning(
                f"检测到桌面僵死（会话挂接正常 state={state_name(state)}，"
                f"连续 {wedge['streak']} 次桌面级激活失败），自愈升级: "
                f"tsdiscon {sid}（RDP 断开-重连周期复位输入栈）")
            result = subprocess.run(
                ["tsdiscon", str(sid)],
                capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                self.logger.info(
                    "tsdiscon 执行成功——有客户端时等待其自动重连（秒级），"
                    "无客户端时由 tscon 链路按防抖+冷却重挂 console")
            else:
                self.logger.warning(
                    f"tsdiscon 执行失败 rc={result.returncode}: "
                    f"{(result.stderr or '').strip()}")
            send_alert(
                "desktop_wedge_recovery",
                "桌面僵死升级自愈已触发（tsdiscon）",
                f"会话挂接正常但连续 {wedge['streak']} 次桌面级激活失败，"
                f"已执行 tsdiscon 强制 RDP 断开-重连周期",
                details={"wedge_streak": wedge["streak"],
                         "connect_state": state},
                level="error",
            )
        except Exception as e:
            self.logger.warning(f"桌面僵死升级自愈失败: {e}")
        finally:
            self._last_wedge_recovery = time.time()  # 含失败路径，防热循环

    def _rdp_client_connecting(self) -> bool:
        """RDP 监听端口上是否存在 ESTABLISHED 连接（客户端已连上服务器）

        断开态 + 有 ESTABLISHED = 客户端在密码界面/协商中（用户正在重连）；
        客户端真离开时 TCP 随之关闭（实测：关闭客户端后 ~1s 内归零）。
        检测失败按「无连接」处理——判别器失效时退回 ①+② 三级防护，
        不因旁路故障阻塞自愈。
        """
        try:
            for conn in psutil.net_connections(kind="tcp"):
                if (conn.laddr and conn.laddr.port == self.RDP_LISTEN_PORT
                        and conn.status == psutil.CONN_ESTABLISHED):
                    return True
        except Exception as e:
            self.logger.warning(f"RDP 连接检测失败（按无连接处理）: {e}")
        return False

    def _login_or_startup_hold(self, hwnd: int) -> bool:
        """登录/启动期避让判定：True=客户端正在登录/初始化，暂不干预窗口

        【事故】2026-10-08：先启动网关、后启动并登录券商客户端。登录期
        主窗口以隐藏态预创建（此时标题未必就绪，窗口查找由 exe 全量扫描
        兜底命中），监控把它当「隐藏到托盘」执行 SW_SHOW+抢前台，与客户
        端自身的登录绘制竞争，造成窗口元素图标错位。

        【三判据】任一命中即避让（按开销从低到高探测）：
        1. 本进程生命周期内目标窗口尚未被见过可见（客户端自行显示过
           一次 = 登录完成铁证，本判据对该进程即永久解除——用户此后
           手动隐藏/最小化立即恢复，不等满宽限期），且进程年龄 <
           login_grace_seconds（配置项，默认 90s；登录全程通常 <60s，
           留余量）——覆盖登录绘制的常规时窗
        2. 前台窗口属于同进程且不是目标窗口——用户正在该程序的登录框/
           验证码等其他窗口上交互，抢前台会打断输入（标题无关，兜住
           登录框标题不含「登录」字样的情况）
        3. 同进程存在可见的对话框（#32770）或标题含「登录」的顶层窗口
           ——用户停在登录界面时（不论停留多久）都不应强制显示主窗口，
           覆盖判据 1 超时后仍停在登录框的长尾场景

        【失效域】三判据全空（进程已老 + 无登录框 + 前台在别处）而主窗口
        仍隐藏时照常恢复——这正是网关先于客户端重启、窗口停在托盘的
        生产自愈场景，不能误伤。残余：登录框标题不含「登录」且非 #32770
        且用户晾在登录界面超过宽限期且前台在别的程序——理论窗口，后果
        回到图标错位级别，接受。

        判定异常按「不避让」处理，不因旁路故障阻塞既有恢复链路
        （与 _rdp_client_connecting 同哲学）。配置每次实时读取（含热重载）。
        """
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid)

            cfg = AppConfig().get_window_monitor_config()
            grace = float(cfg.get("login_grace_seconds", self.LOGIN_GRACE_SECONDS))
            # 年龄判据只作用于「本进程内窗口尚未被见过可见」——见过即
            # 登录完成，此后手动隐藏/最小化立即恢复（记账见 _process_window_state）
            if (not self._seen_visible and grace > 0
                    and time.time() - proc.create_time() < grace):
                return True

            fg = win32gui.GetForegroundWindow()
            if fg and fg != hwnd:
                _, fg_pid = win32process.GetWindowThreadProcessId(fg)
                if fg_pid == pid:
                    return True

            dialogs: List[int] = []

            def dialog_callback(h, _):
                if h == hwnd or not win32gui.IsWindowVisible(h):
                    return True
                _, p = win32process.GetWindowThreadProcessId(h)
                if p == pid:
                    title = win32gui.GetWindowText(h)
                    if (win32gui.GetClassName(h) == "#32770"
                            or (title and "登录" in title)):
                        dialogs.append(h)
                return True

            win32gui.EnumWindows(dialog_callback, None)
            return bool(dialogs)
        except Exception as e:
            self.logger.warning(f"登录/启动期判定失败（按不避让处理）: {e}")
            return False

    def _process_window_state(self, hwnd: int) -> None:
        """按窗口可见性分派恢复动作（最小化/隐藏 → 软恢复 → 重拉兜底）

        恢复前置登录/启动期避让（_login_or_startup_hold）：避让轮不算
        「软恢复无效」轮（防登录慢时误触发重拉弹第二个登录框抢前台），
        进出避让各记一条日志。

        顺带维护「本进程生命周期内目标窗口是否被见过可见」：客户端自行
        显示过一次 = 登录铁定完成，此后年龄判据永久解除（用户再手动
        隐藏/最小化立即恢复，不等满宽限期）。按 PID 记账——窗口句柄
        可能被客户端重建，进程身份才是稳定键；最小化（iconic）不算
        「自行显示」。
        """
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid != self._target_pid:
            self._target_pid = pid
            self._seen_visible = False
        visible = win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd)
        if visible:
            self._seen_visible = True
        if not visible:
            if self._login_or_startup_hold(hwnd):
                self._bad_state_count = 0
                if not self._login_hold_active:
                    self._login_hold_active = True
                    state = "最小化" if win32gui.IsIconic(hwnd) else "隐藏（托盘）"
                    self.logger.info(
                        f"目标窗口{state}，但客户端处于登录/启动期，暂不恢复"
                        f"（避免与登录绘制竞争造成元素错位）")
                return
            if self._login_hold_active:
                self._login_hold_active = False
                self.logger.info("登录/启动期结束，恢复窗口监控干预")
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
            if self._login_hold_active:
                self._login_hold_active = False
                self.logger.info("目标窗口已由客户端自行显示，登录/启动期避让结束")
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
            # 会话断开自愈 + 桌面僵死升级自愈（时间触发，独立于窗口检查节奏）
            try:
                if time.time() >= next_session_check:
                    self._recover_session_if_disconnected()
                    self._recover_desktop_if_wedged()
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
