"""桌面僵死态（形态二）：追踪器 / session_health 合并 / 任务失败喂入 /
激活失败分类 / tsdiscon 升级自愈

背景（2026-10-09 事故）：tscon 自愈后 RDP 重连撞车，会话 Active 但
输入/图形路径全死——ui_available 假绿、WINDOW_NOT_FOUND 假象。本文件
覆盖为该形态补的三件套：指纹细分错误码、session_state 僵死追踪、
window_monitor 的 tsdiscon 升级自愈。

不碰真实 UI：e2e 用例 no-op 掉 _sweep_leftover_dialogs /
_prepare_window_for_task / _auto_diagnostic（否则生产机上有真
xiadan.exe 时会对真实交易窗口操作+真实截图）。TaskQueue 复用真单例，
finally 手工清理（test_session_gate.py 同款模式，绝不 _reset_instance()）。
"""
import time
from types import SimpleNamespace

import pytest

import src.api.task_queue as tq_module
import src.services.window_monitor as wm_module
import src.services.window_service as ws_module
import src.utils.session_state as session_state
from src.api.task_queue import TaskQueue
from src.exceptions import ApiError, ErrorCode
from src.services.window_monitor import WindowMonitor
from src.services.window_service import WindowService

DESKTOP_DEAD_TEXT = "There is no active desktop required for moving mouse cursor!"


# ── 共用 ────────────────────────────────────────────────────

def _reset_wedge():
    """追踪器是模块级全局态，用例间必须归零"""
    session_state._wedge_streak = 0
    session_state._wedge_last_failure = 0.0


def _cleanup(tq):
    """还原共享单例状态（test_session_gate.py 同款）"""
    tq._recent_tasks.clear()
    tq._consecutive_failures = 0
    tq._session_unavailable_logged = False
    tq._last_task_info = None
    _reset_wedge()


@pytest.fixture(autouse=True)
def _isolate_wedge_state():
    _reset_wedge()
    yield
    _reset_wedge()


# ── 追踪器：阈值 / 清零 / 过期 ──────────────────────────────

class TestWedgeTracker:
    def test_below_threshold_not_wedged(self):
        session_state.record_desktop_failure()
        status = session_state.desktop_wedge_status()
        assert status == {"wedged": False, "streak": 1}

    def test_two_consecutive_failures_wedged(self):
        session_state.record_desktop_failure()
        session_state.record_desktop_failure()
        status = session_state.desktop_wedge_status()
        assert status == {"wedged": True, "streak": 2}

    def test_success_clears(self):
        session_state.record_desktop_failure()
        session_state.record_desktop_failure()
        session_state.record_desktop_success()
        assert session_state.desktop_wedge_status() == {
            "wedged": False, "streak": 0}

    def test_expiry(self, monkeypatch):
        """任务流停了标记不残留：最后一次指纹失败超 15min 即过期"""
        session_state.record_desktop_failure()
        session_state.record_desktop_failure()
        real_time = time.time
        monkeypatch.setattr(
            session_state.time, "time",
            lambda: real_time() + session_state.WEDGE_EXPIRY_SECONDS + 1)
        status = session_state.desktop_wedge_status()
        assert status["wedged"] is False
        assert status["streak"] == 2  # streak 保留（观测用），wedged 失效


class TestSessionHealthMerge:
    def test_wedged_merged_into_health(self, monkeypatch):
        monkeypatch.setattr(session_state, "get_session_state", lambda: 0)
        session_state.record_desktop_failure()
        session_state.record_desktop_failure()
        health = session_state.session_health()
        # 挂接但僵死：ui_available 假绿 + desktop_wedged 消歧
        assert health["ui_available"] is True
        assert health["desktop_wedged"] is True
        assert health["desktop_wedge_streak"] == 2


# ── e2e：任务失败喂入（真 worker 线程） ─────────────────────

def _submit_failing_and_wait(tq, monkeypatch, exc, timeout=5):
    """提交一个抛 exc 的任务并轮询追踪器落账

    _record_task_outcome 在 event.set()（submit 返回）之后才喂追踪器，
    轮询至 recent_tasks 增长 + 追踪器稳定，而非直接断言。
    """
    before = len(tq._recent_tasks)
    with pytest.raises(type(exc)):
        tq.submit(lambda: (_ for _ in ()).throw(exc), "get_balance", {}, 10)
    deadline = time.time() + timeout
    while time.time() < deadline:
        with tq._stats_lock:
            landed = len(tq._recent_tasks) > before
        if landed:
            return
        time.sleep(0.02)
    pytest.fail("失败任务统计未落账")


def _make_safe(tq, monkeypatch):
    """放行/失败路径安全化：真实 UI 操作与真实截图一律 no-op"""
    monkeypatch.setattr(tq, "_sweep_leftover_dialogs", lambda: None)
    monkeypatch.setattr(tq, "_prepare_window_for_task", lambda task: None)
    monkeypatch.setattr(tq, "_auto_diagnostic", lambda task: None)


class TestTaskQueueFeeds:
    def test_wedge_code_failures_trip_flag(self, monkeypatch):
        """连续 2 笔 SESSION_DESKTOP_UNAVAILABLE 任务 → wedged"""
        tq = TaskQueue.get_instance()
        monkeypatch.setattr(tq_module, "get_session_state", lambda: 0)
        _make_safe(tq, monkeypatch)
        exc = ApiError(ErrorCode.SESSION_DESKTOP_UNAVAILABLE, "桌面不可操作")
        try:
            _submit_failing_and_wait(tq, monkeypatch, exc)
            assert session_state.desktop_wedge_status()["wedged"] is False
            _submit_failing_and_wait(tq, monkeypatch, exc)
            assert session_state.desktop_wedge_status() == {
                "wedged": True, "streak": 2}
        finally:
            _cleanup(tq)

    def test_raw_pywinauto_text_also_feeds(self, monkeypatch):
        """未包裹路径冒泡的裸 RuntimeError（reset_window_state 等）按文本兜底"""
        tq = TaskQueue.get_instance()
        monkeypatch.setattr(tq_module, "get_session_state", lambda: 0)
        _make_safe(tq, monkeypatch)
        try:
            _submit_failing_and_wait(
                tq, monkeypatch, RuntimeError(DESKTOP_DEAD_TEXT))
            _submit_failing_and_wait(
                tq, monkeypatch, RuntimeError(DESKTOP_DEAD_TEXT))
            assert session_state.desktop_wedge_status()["wedged"] is True
        finally:
            _cleanup(tq)

    def test_unrelated_failure_does_not_feed(self, monkeypatch):
        tq = TaskQueue.get_instance()
        monkeypatch.setattr(tq_module, "get_session_state", lambda: 0)
        _make_safe(tq, monkeypatch)
        try:
            _submit_failing_and_wait(tq, monkeypatch, RuntimeError("boom"))
            assert session_state.desktop_wedge_status() == {
                "wedged": False, "streak": 0}
        finally:
            _cleanup(tq)

    def test_success_clears_wedge(self, monkeypatch):
        tq = TaskQueue.get_instance()
        monkeypatch.setattr(tq_module, "get_session_state", lambda: 0)
        _make_safe(tq, monkeypatch)
        try:
            session_state.record_desktop_failure()
            session_state.record_desktop_failure()
            assert tq.submit(lambda: "ok", "get_balance", {}, 10) == "ok"
            assert session_state.desktop_wedge_status() == {
                "wedged": False, "streak": 0}
        finally:
            _cleanup(tq)

    def test_gate_reject_neither_feeds_nor_clears(self, monkeypatch):
        """会话门拒绝（precheck_rejected）：未触碰 UI——不喂指纹也不清零"""
        tq = TaskQueue.get_instance()
        _drain(tq, monkeypatch)
        session_state.record_desktop_failure()
        session_state.record_desktop_failure()
        monkeypatch.setattr(tq_module, "get_session_state", lambda: 4)
        try:
            with pytest.raises(ApiError):
                tq.submit(lambda: "ok", "get_position", {}, 10)
            assert session_state.desktop_wedge_status() == {
                "wedged": True, "streak": 2}
        finally:
            _cleanup(tq)


def _drain(tq, monkeypatch):
    """同步任务落账静止点（test_session_gate.py 同款）"""
    monkeypatch.setattr(tq_module, "get_session_state", lambda: 0)
    _make_safe(tq, monkeypatch)
    before = len(tq._recent_tasks)
    tq.submit(lambda: "sync", "get_balance", {}, 10)
    deadline = time.time() + 5
    while time.time() < deadline:
        with tq._stats_lock:
            if len(tq._recent_tasks) > before:
                return
        time.sleep(0.02)
    pytest.fail("同步任务统计未落账")


# ── 激活失败分类（window_service） ──────────────────────────

class TestActivationFailureClassification:
    def test_foreground_zero_is_desktop_dead(self):
        with pytest.raises(ApiError) as ei:
            WindowService._raise_activation_failure("F4", 0, None)
        assert ei.value.error_code == ErrorCode.SESSION_DESKTOP_UNAVAILABLE
        assert "桌面不可操作" in ei.value.message
        assert ei.value.details["foreground_hwnd"] == 0

    def test_pywinauto_fingerprint_is_desktop_dead(self):
        """前台句柄非 0 但 click_input 抛 pywinauto 指纹——同判桌面僵死；
        消息保留原始文本（grep 诊断指纹）"""
        exc = RuntimeError(DESKTOP_DEAD_TEXT)
        with pytest.raises(ApiError) as ei:
            WindowService._raise_activation_failure("F4", 0x3005c, exc)
        assert ei.value.error_code == ErrorCode.SESSION_DESKTOP_UNAVAILABLE
        assert DESKTOP_DEAD_TEXT in ei.value.message

    def test_other_window_in_front_keeps_window_not_found(self):
        """前台确有其他窗口（无指纹）——WINDOW_NOT_FOUND 原语义，文案准确"""
        with pytest.raises(ApiError) as ei:
            WindowService._raise_activation_failure("F4", 0x999, None)
        assert ei.value.error_code == ErrorCode.WINDOW_NOT_FOUND
        assert "当前前台窗口不是交易窗口" in ei.value.message

    def test_activate_before_keybd_end_to_end(self, monkeypatch):
        """假窗口 click_input 抛指纹 + 前台恒 0 → 分类码冒泡"""
        svc = WindowService()

        class FakeWindow:
            handle = 0x1111

            def click_input(self):
                raise RuntimeError(DESKTOP_DEAD_TEXT)

        monkeypatch.setattr(svc, "get_trading_window", lambda: FakeWindow())
        monkeypatch.setattr(ws_module.win32gui, "IsIconic", lambda hwnd: False)
        monkeypatch.setattr(ws_module.win32gui, "GetForegroundWindow",
                            lambda: 0)
        monkeypatch.setattr(ws_module.win32gui, "ShowWindow", lambda h, f: None)
        with pytest.raises(ApiError) as ei:
            svc._activate_window_before_keybd("F4")
        assert ei.value.error_code == ErrorCode.SESSION_DESKTOP_UNAVAILABLE


# ── 升级自愈（window_monitor._recover_desktop_if_wedged） ───

class FakeSessionMonitorCfg:
    """AppConfig 桩：代码里是 AppConfig()（类调用），故以类形态替换，
    覆盖项经闭包注入"""

    overrides: dict = {}

    def __init__(self):
        pass

    def get_session_monitor_config(self):
        base = {"enabled": True, "debounce_seconds": 30,
                "cooldown_seconds": 300, "wedge_heal_enabled": True,
                "wedge_cooldown_seconds": 600}
        base.update(self.overrides)
        return base


def _make_wedged(monkeypatch, state=0):
    FakeSessionMonitorCfg.overrides = {}
    monkeypatch.setattr(wm_module, "AppConfig", FakeSessionMonitorCfg)
    monkeypatch.setattr(wm_module, "desktop_wedge_status",
                        lambda: {"wedged": True, "streak": 2})
    monkeypatch.setattr(wm_module, "get_session_state", lambda: state)


class TestWedgeEscalation:
    def _make_monitor(self):
        return WindowMonitor(check_interval=0.1)

    def test_fires_tsdiscon(self, monkeypatch):
        wm = self._make_monitor()
        calls = []
        FakeSessionMonitorCfg.overrides = {}
        monkeypatch.setattr(wm_module, "AppConfig", FakeSessionMonitorCfg)
        monkeypatch.setattr(wm_module, "desktop_wedge_status",
                            lambda: {"wedged": True, "streak": 2})
        monkeypatch.setattr(wm_module, "get_session_state", lambda: 0)
        monkeypatch.setattr(wm_module, "send_alert", lambda *a, **k: None)

        import subprocess as sp
        import win32ts
        monkeypatch.setattr(win32ts, "ProcessIdToSessionId",
                            lambda pid: 999, raising=False)

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sp, "run", fake_run)
        wm._recover_desktop_if_wedged()
        assert calls == [["tsdiscon", "999"]]
        assert wm._last_wedge_recovery > 0  # 冷却已起表（含成功路径）

    def test_cooldown_blocks_second_call(self, monkeypatch):
        wm = self._make_monitor()
        _make_wedged(monkeypatch)
        monkeypatch.setattr(wm_module, "send_alert", lambda *a, **k: None)
        calls = []

        import subprocess as sp
        monkeypatch.setattr(sp, "run",
                            lambda cmd, **k: calls.append(cmd)
                            or SimpleNamespace(returncode=0, stderr=""))

        wm._recover_desktop_if_wedged()
        assert len(calls) == 1
        wm._recover_desktop_if_wedged()  # 冷却期内
        assert len(calls) == 1

    def test_disabled_config_noop(self, monkeypatch):
        wm = self._make_monitor()
        FakeSessionMonitorCfg.overrides = {"wedge_heal_enabled": False}
        monkeypatch.setattr(wm_module, "AppConfig", FakeSessionMonitorCfg)
        monkeypatch.setattr(wm_module, "desktop_wedge_status",
                            lambda: {"wedged": True, "streak": 2})
        monkeypatch.setattr(wm_module, "get_session_state", lambda: 0)
        calls = []

        import subprocess as sp
        monkeypatch.setattr(sp, "run",
                            lambda cmd, **k: calls.append(cmd)
                            or SimpleNamespace(returncode=0, stderr=""))

        wm._recover_desktop_if_wedged()
        assert calls == []

    def test_not_wedged_noop(self, monkeypatch):
        wm = self._make_monitor()
        monkeypatch.setattr(wm_module, "AppConfig", FakeSessionMonitorCfg)
        monkeypatch.setattr(wm_module, "desktop_wedge_status",
                            lambda: {"wedged": False, "streak": 1})
        monkeypatch.setattr(wm_module, "get_session_state", lambda: 0)
        calls = []

        import subprocess as sp
        monkeypatch.setattr(sp, "run",
                            lambda cmd, **k: calls.append(cmd)
                            or SimpleNamespace(returncode=0, stderr=""))

        wm._recover_desktop_if_wedged()
        assert calls == []

    def test_disconnected_state_yields_to_existing_heal(self, monkeypatch):
        """state=4 归既有 tscon 链路管，升级路径不动作（避免双动作竞态）"""
        wm = self._make_monitor()
        _make_wedged(monkeypatch, state=4)
        calls = []

        import subprocess as sp
        monkeypatch.setattr(sp, "run",
                            lambda cmd, **k: calls.append(cmd)
                            or SimpleNamespace(returncode=0, stderr=""))

        wm._recover_desktop_if_wedged()
        assert calls == []

    def test_unknown_state_fail_closed(self, monkeypatch):
        """state 查询失败（None）不动作——tsdiscon 误动作代价不对称"""
        wm = self._make_monitor()
        _make_wedged(monkeypatch, state=None)
        calls = []

        import subprocess as sp
        monkeypatch.setattr(sp, "run",
                            lambda cmd, **k: calls.append(cmd)
                            or SimpleNamespace(returncode=0, stderr=""))

        wm._recover_desktop_if_wedged()
        assert calls == []


# ── /health 字段透传 ────────────────────────────────────────

class TestHealthWedgeField:
    def test_health_exposes_wedge_fields(self, monkeypatch):
        from src.api.routes import create_app
        import src.api.routes as routes_module
        monkeypatch.setattr(
            routes_module, "session_health",
            lambda: {"connect_state": 0, "state_name": "Active",
                     "ui_available": True, "desktop_wedged": True,
                     "desktop_wedge_streak": 3})
        app = create_app()
        app.config["TESTING"] = True
        body = app.test_client().get("/health").get_json()
        session = body["data"]["session"]
        assert session["ui_available"] is True  # 假绿灯
        assert session["desktop_wedged"] is True  # 消歧字段
        assert session["desktop_wedge_streak"] == 3
