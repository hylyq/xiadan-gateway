"""窗口监控登录/启动期避让逻辑单测

不依赖真实 xiadan.exe：monkeypatch 掉 win32 层 / psutil / AppConfig，
直接驱动 _login_or_startup_hold 判定与 _process_window_state 分派。

背景（2026-10-08 事故）：先启动网关、后启动并登录券商客户端时，主窗口
在登录期以隐藏态预创建，被 exe 全量扫描兜底命中，监控把它当「隐藏到
托盘」强制 SW_SHOW + 抢前台，与客户端登录绘制竞争导致窗口元素图标错位。
"""
import time

import pytest

import src.services.window_monitor as wm
from src.services.window_monitor import WindowMonitor

PID_TARGET = 100
PID_OTHER = 200
HWND_TARGET = 1


class FakePsutil:
    """按 pid 映射进程年龄；未注册的 pid 抛 NoSuchProcess"""

    NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    AccessDenied = type("AccessDenied", (Exception,), {})
    ages = {}  # pid -> 进程年龄（秒），类变量便于各用例直接改写

    @staticmethod
    def Process(pid):
        if pid not in FakePsutil.ages:
            raise FakePsutil.NoSuchProcess()
        return type("P", (), {
            "create_time": staticmethod(
                lambda: time.time() - FakePsutil.ages[pid])})()


class FakeWin32Gui:
    """windows: hwnd -> {"pid","title","visible","iconic","cls"}"""

    def __init__(self, windows, foreground=0):
        self.windows = windows
        self.foreground = foreground

    def EnumWindows(self, cb, extra):
        for h in self.windows:
            cb(h, extra)

    def IsWindowVisible(self, h):
        return self.windows[h]["visible"]

    def IsIconic(self, h):
        return self.windows[h]["iconic"]

    def GetWindowText(self, h):
        return self.windows[h]["title"]

    def GetClassName(self, h):
        return self.windows[h].get("cls", "XiadanMain")

    def GetForegroundWindow(self):
        return self.foreground


class FakeWin32Process:
    def __init__(self, windows):
        self.windows = windows

    def GetWindowThreadProcessId(self, h):
        info = self.windows.get(h)
        return 0, info["pid"] if info else PID_OTHER


def make_world(monkeypatch, windows, foreground=0, ages=None):
    """装配假 win32/psutil/AppConfig 世界；返回 FakeWin32Gui 便于用例中途改态"""
    gui = FakeWin32Gui(windows, foreground)
    monkeypatch.setattr(wm, "win32gui", gui)
    monkeypatch.setattr(wm, "win32process", FakeWin32Process(windows))
    FakePsutil.ages = dict(ages or {})
    monkeypatch.setattr(wm, "psutil", FakePsutil)

    class FakeAppConfig:
        def get_window_monitor_config(self):
            return {"login_grace_seconds": 90}

    monkeypatch.setattr(wm, "AppConfig", FakeAppConfig)
    return gui


def hidden_target(**extra):
    win = {"pid": PID_TARGET, "title": "网上股票交易系统5.0",
           "visible": False, "iconic": False}
    win.update(extra)
    return {HWND_TARGET: win}


@pytest.fixture
def monitor():
    return WindowMonitor(check_interval=0.01)


def patch_actions(monkeypatch, monitor):
    """记录恢复/重拉调用，隔离真实 win32 副作用"""
    restores, relaunches = [], []

    def fake_restore(hwnd):
        restores.append(hwnd)
        return True

    def fake_relaunch(hwnd=None):
        relaunches.append(hwnd)
        return True

    monkeypatch.setattr(monitor, "_restore_window", fake_restore)
    monkeypatch.setattr(monitor, "_relaunch_app", fake_relaunch)
    return restores, relaunches


class TestLoginOrStartupHold:
    def test_young_process_holds(self, monkeypatch, monitor):
        """场景复现：登录期主窗口隐藏预创建，进程年轻 → 避让"""
        make_world(monkeypatch, hidden_target(), foreground=999,
                   ages={PID_TARGET: 10})
        assert monitor._login_or_startup_hold(HWND_TARGET) is True

    def test_login_dialog_visible_holds_beyond_grace(self, monkeypatch, monitor):
        """宽限期外仍停在登录框（标题含「登录」）→ 避让（长尾场景判据 3）"""
        windows = hidden_target()
        windows[2] = {"pid": PID_TARGET, "title": "用户登录",
                      "visible": True, "iconic": False}
        make_world(monkeypatch, windows, ages={PID_TARGET: 600})
        assert monitor._login_or_startup_hold(HWND_TARGET) is True

    def test_dialog_class_32770_holds(self, monkeypatch, monitor):
        """同进程可见 #32770 对话框（标题不含「登录」）→ 避让"""
        windows = hidden_target()
        windows[2] = {"pid": PID_TARGET, "title": "提示",
                      "visible": True, "iconic": False, "cls": "#32770"}
        make_world(monkeypatch, windows, ages={PID_TARGET: 600})
        assert monitor._login_or_startup_hold(HWND_TARGET) is True

    def test_same_process_foreground_holds(self, monkeypatch, monitor):
        """前台是同进程其他窗口（用户正在登录框交互）→ 避让（判据 2）"""
        windows = hidden_target()
        windows[2] = {"pid": PID_TARGET, "title": "Whatever",
                      "visible": True, "iconic": False}
        make_world(monkeypatch, windows, foreground=2, ages={PID_TARGET: 600})
        assert monitor._login_or_startup_hold(HWND_TARGET) is True

    def test_mature_hidden_tray_does_not_hold(self, monkeypatch, monitor):
        """生产自愈场景不能误伤：进程已老、无登录框、前台在别的程序 → 照常恢复"""
        make_world(monkeypatch, hidden_target(), foreground=999,
                   ages={PID_TARGET: 600})
        assert monitor._login_or_startup_hold(HWND_TARGET) is False

    def test_zero_grace_disables_age_check_only(self, monkeypatch, monitor):
        """login_grace_seconds=0 关闭年龄判据，登录框判据仍生效"""

        class ZeroGraceCfg:
            def get_window_monitor_config(self):
                return {"login_grace_seconds": 0}

        gui = FakeWin32Gui(hidden_target(), foreground=999)
        monkeypatch.setattr(wm, "win32gui", gui)
        monkeypatch.setattr(wm, "win32process", FakeWin32Process(gui.windows))
        FakePsutil.ages = {PID_TARGET: 5}
        monkeypatch.setattr(wm, "psutil", FakePsutil)
        monkeypatch.setattr(wm, "AppConfig", ZeroGraceCfg)

        assert monitor._login_or_startup_hold(HWND_TARGET) is False
        gui.windows[2] = {"pid": PID_TARGET, "title": "用户登录",
                          "visible": True, "iconic": False}
        gui.windows[HWND_TARGET]["title"] = "网上股票交易系统5.0"
        assert monitor._login_or_startup_hold(HWND_TARGET) is True

    def test_probe_failure_fails_open(self, monkeypatch, monitor):
        """判定链路异常 → 按不避让处理（不阻塞既有恢复）"""
        make_world(monkeypatch, hidden_target(), ages={})  # pid 未注册 → Process 抛错
        assert monitor._login_or_startup_hold(HWND_TARGET) is False


class TestProcessWindowStateDispatch:
    def test_hold_round_skips_restore_relaunch_and_bad_count(self, monkeypatch, monitor):
        """避让轮：不恢复、不累计坏状态轮（防登录慢误触发重拉）、进出各记一条日志"""
        gui = make_world(monkeypatch, hidden_target(), foreground=999,
                         ages={PID_TARGET: 10})
        restores, relaunches = patch_actions(monkeypatch, monitor)

        for _ in range(6):  # 超过 BAD_STATE_RELAUNCH_THRESHOLD=3 轮
            monitor._process_window_state(HWND_TARGET)

        assert restores == []
        assert relaunches == []
        assert monitor._bad_state_count == 0
        assert monitor._login_hold_active is True

        # 客户端自行完成登录显示 → 避让结束
        gui.windows[HWND_TARGET]["visible"] = True
        monitor._process_window_state(HWND_TARGET)
        assert monitor._login_hold_active is False

    def test_hold_exit_still_hidden_restores(self, monkeypatch, monitor):
        """避让到期（进程已老）且窗口仍隐藏 → 恢复干预照常，重拉升级链路不变"""
        gui = make_world(monkeypatch, hidden_target(), foreground=999,
                         ages={PID_TARGET: 10})
        restores, relaunches = patch_actions(monkeypatch, monitor)
        monitor._process_window_state(HWND_TARGET)
        assert restores == [] and monitor._login_hold_active is True

        FakePsutil.ages[PID_TARGET] = 600  # 进程变老，避让判据失效
        for _ in range(3):
            monitor._process_window_state(HWND_TARGET)

        assert len(restores) == 3
        assert len(relaunches) == 1  # 连续 3 轮软恢复无效 → 重拉兜底（原行为）
        assert monitor._login_hold_active is False

    def test_seen_visible_lifts_age_hold(self, monkeypatch, monitor):
        """用户场景（2026-10-08 复测）：重开客户端登录完成、窗口被客户端
        自行显示过，此后手动隐藏到托盘——进程仍年轻也立即恢复，不等 90s"""
        gui = make_world(monkeypatch, hidden_target(), foreground=999,
                         ages={PID_TARGET: 30})
        restores, _ = patch_actions(monkeypatch, monitor)
        # 登录期：隐藏预创建 → 避让
        monitor._process_window_state(HWND_TARGET)
        assert restores == [] and monitor._login_hold_active is True
        # 客户端自行完成登录显示
        gui.windows[HWND_TARGET]["visible"] = True
        monitor._process_window_state(HWND_TARGET)
        assert monitor._login_hold_active is False
        # 手动隐藏到托盘（进程仍 ~30s < 90s）→ 立即恢复
        gui.windows[HWND_TARGET]["visible"] = False
        monitor._process_window_state(HWND_TARGET)
        assert restores == [HWND_TARGET]

    def test_pid_change_rearms_age_hold(self, monkeypatch, monitor):
        """券商软件关闭重开（新 PID）：可见记账清零，年龄判据重新武装"""
        gui = make_world(monkeypatch, hidden_target(), foreground=999,
                         ages={PID_TARGET: 5})
        restores, _ = patch_actions(monkeypatch, monitor)
        gui.windows[HWND_TARGET]["visible"] = True
        monitor._process_window_state(HWND_TARGET)  # 旧进程：见过可见
        # 客户端重开：目标窗口换到新进程且隐藏（登录期）
        new_pid = PID_TARGET + 1
        gui.windows[HWND_TARGET]["pid"] = new_pid
        gui.windows[HWND_TARGET]["visible"] = False
        FakePsutil.ages[new_pid] = 5
        monitor._process_window_state(HWND_TARGET)
        assert restores == [] and monitor._login_hold_active is True

    def test_minimized_only_never_counts_as_shown(self, monkeypatch, monitor):
        """窗口一直最小化（iconic 且 WS_VISIBLE）≠ 客户端自行显示——年龄判据不解除"""
        gui = make_world(monkeypatch, hidden_target(iconic=True, visible=True),
                         foreground=999, ages={PID_TARGET: 10})
        restores, _ = patch_actions(monkeypatch, monitor)
        for _ in range(3):
            monitor._process_window_state(HWND_TARGET)
        assert restores == [] and monitor._login_hold_active is True
