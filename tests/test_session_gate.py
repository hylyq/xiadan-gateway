"""会话健康门测试：断开态快速拒绝 / 瞬态与查询失败放行 / 统计豁免 / /health 字段

不碰真实 UI：拒绝路径任务体断言不被调用；放行路径 monkeypatch 掉实例的
_sweep_leftover_dialogs / _prepare_window_for_task（否则生产机上有真
xiadan.exe 时 worker 会对真实交易窗口执行 click_input+ESC——唯一危险点）。

TaskQueue 复用真单例（worker 线程真实启动，test_core.py 同款模式），
共享状态在 finally 手工清理；绝不 _reset_instance()（会遗留旧 worker
线程并再起一个）。
"""
import time

import pytest

import src.api.task_queue as tq_module
from src.api.task_queue import TaskQueue
from src.exceptions import ApiError, ErrorCode
from src.utils import session_state


# ── 纯单元：session_state 模块 ──────────────────────────────

class TestGetSessionState:
    """win32ts 查询的三种形态：int / tuple / 异常"""

    def _patch_query(self, monkeypatch, value):
        import win32ts
        if isinstance(value, Exception):
            def fake(*a, **k):
                raise value
        else:
            def fake(*a, **k):
                return value
        monkeypatch.setattr(win32ts, "WTSQuerySessionInformation", fake)

    def test_int_form(self, monkeypatch):
        self._patch_query(monkeypatch, 0)
        assert session_state.get_session_state() == 0

    def test_tuple_form(self, monkeypatch):
        """部分 pywin32 版本返回 (code,)——必须解包"""
        self._patch_query(monkeypatch, (4,))
        assert session_state.get_session_state() == 4

    def test_exception_returns_none(self, monkeypatch):
        self._patch_query(monkeypatch, RuntimeError("wts down"))
        assert session_state.get_session_state() is None

    def test_state_name(self):
        assert session_state.state_name(None) == "Unknown"
        assert session_state.state_name(0) == "Active"
        assert session_state.state_name(4) == "Disconnected"
        assert session_state.state_name(99) == "Unknown(99)"

    def test_session_health(self, monkeypatch):
        monkeypatch.setattr(session_state, "get_session_state", lambda: 4)
        assert session_state.session_health() == {
            "connect_state": 4, "state_name": "Disconnected",
            "ui_available": False,
            "desktop_wedged": False, "desktop_wedge_streak": 0}
        monkeypatch.setattr(session_state, "get_session_state", lambda: 0)
        assert session_state.session_health()["ui_available"] is True
        monkeypatch.setattr(session_state, "get_session_state", lambda: None)
        # null=未知（查询失败），非 false（确定不可用）
        assert session_state.session_health()["ui_available"] is None


# ── e2e：经 submit 走真 worker 线程 ──────────────────────────

def _cleanup(tq):
    """还原共享单例状态（test_core.py 同款手工清理模式）"""
    tq._recent_tasks.clear()
    tq._consecutive_failures = 0
    tq._session_unavailable_logged = False
    tq._last_task_info = None


def _drain(tq, monkeypatch):
    """等待 worker 空闲至统计落账完成

    正常任务路径 event.set() 先于 _record_task_outcome 执行——前一用例
    的统计写可能迟到。本用例若随即预置计数器/快照再断言，会被迟到写
    污染（实测竞态）。提交一个同步任务并轮询至其统计落账，即获得
    「队列无在途任务」的静止点。

    同步任务必须 patch 成健康态：pytest 运行会话（如 Session 0）本身
    可能就处于 WTSDisconnected——门按设计拒绝，同步任务永远无法落账
    （实测实证了门的真实行为）。
    """
    monkeypatch.setattr(tq_module, "get_session_state", lambda: 0)
    monkeypatch.setattr(tq, "_sweep_leftover_dialogs", lambda: None)
    monkeypatch.setattr(tq, "_prepare_window_for_task", lambda task: None)
    before = len(tq._recent_tasks)
    tq.submit(lambda: "sync", "get_balance", {}, 10)
    deadline = time.time() + 5
    # 必须在 _stats_lock 内观察：append 是 GIL 原子的，无锁读会在 worker
    # 仍持锁、计数器复位未执行时就看到增长（实测竞态）。透过锁看到
    # append = 整个临界区（含 consecutive_failures 复位）已完成
    while time.time() < deadline:
        with tq._stats_lock:
            if len(tq._recent_tasks) > before:
                return
        time.sleep(0.02)
    pytest.fail("同步任务统计未落账")


class TestGateRejects:
    """断开态（WTSDisconnected=4）：毫秒级快速拒绝，任务体不执行"""

    def test_reject_disconnected(self, monkeypatch):
        tq = TaskQueue.get_instance()
        monkeypatch.setattr(tq_module, "get_session_state", lambda: 4)
        calls = []
        try:
            with pytest.raises(ApiError) as ei:
                tq.submit(lambda: calls.append(1) or "ok",
                          "get_position", {}, 10)
            err = ei.value
            assert err.error_code == ErrorCode.SESSION_UNAVAILABLE
            assert calls == []  # 任务体未执行（与 TASK_TIMEOUT 的本质区别）
            assert "未执行" in err.message
            assert "Idempotency-Key" in (err.suggestion or "")
            assert err.details["connect_state"] == 4
            assert err.details["typical_recovery_seconds"] > 0
            assert err.details["worst_case_recovery_seconds"] > \
                err.details["typical_recovery_seconds"]
        finally:
            _cleanup(tq)


class TestGatePasses:
    """健康态放行；查询失败（None）fail-open 放行"""

    @staticmethod
    def _make_safe(tq, monkeypatch):
        """放行路径安全化：窗口准备/弹窗清扫都是真实 UI 操作，必须 no-op 化"""
        monkeypatch.setattr(tq, "_sweep_leftover_dialogs", lambda: None)
        monkeypatch.setattr(tq, "_prepare_window_for_task", lambda task: None)

    def test_pass_active(self, monkeypatch):
        tq = TaskQueue.get_instance()
        monkeypatch.setattr(tq_module, "get_session_state", lambda: 0)
        self._make_safe(tq, monkeypatch)
        try:
            assert tq.submit(lambda: "ok", "get_balance", {}, 10) == "ok"
        finally:
            _cleanup(tq)

    def test_pass_query_failure_fail_open(self, monkeypatch):
        """win32ts 查询失败（None）→ 放行，最坏 = 无门时代（看门狗兜底）"""
        tq = TaskQueue.get_instance()
        monkeypatch.setattr(tq_module, "get_session_state", lambda: None)
        self._make_safe(tq, monkeypatch)
        try:
            assert tq.submit(lambda: "ok", "get_balance", {}, 10) == "ok"
        finally:
            _cleanup(tq)


class TestGateDisabled:
    """逃生口：session_gate_enabled=false 时断开态也放行"""

    def test_disabled_passes(self, monkeypatch):
        tq = TaskQueue.get_instance()

        class FakeCfg:
            @staticmethod
            def get_task_queue_config():
                return {"session_gate_enabled": False}

            @staticmethod
            def get_session_monitor_config():
                return {"debounce_seconds": 30, "cooldown_seconds": 300}

        monkeypatch.setattr(tq, "config", FakeCfg)
        monkeypatch.setattr(tq_module, "get_session_state", lambda: 4)
        TestGatePasses._make_safe(tq, monkeypatch)
        try:
            assert tq.submit(lambda: "ok", "get_balance", {}, 10) == "ok"
        finally:
            _cleanup(tq)


class TestGateStats:
    """门拒绝计入统计（成功率反映真实不可用）但豁免连续失败告警计数"""

    def test_reject_exempt_from_consecutive_failures(self, monkeypatch):
        tq = TaskQueue.get_instance()
        _drain(tq, monkeypatch)
        monkeypatch.setattr(tq_module, "get_session_state", lambda: 4)
        try:
            tq._consecutive_failures = 2  # 预置：再 +1 就触发 ==3 告警
            with pytest.raises(ApiError):
                tq.submit(lambda: "ok", "get_position", {}, 10)
            assert tq._consecutive_failures == 2  # 未递增 → 未触发告警
            last = tq._recent_tasks[-1]
            assert last[1] is False  # 失败计入 recent_tasks
            assert last[2] == ErrorCode.SESSION_UNAVAILABLE
        finally:
            _cleanup(tq)


class TestLastTaskInfoPreserved:
    """门拒绝不清连续跳过状态：窗口未被触碰，上笔干净退出的依据仍有效"""

    def test_last_task_info_kept(self, monkeypatch):
        tq = TaskQueue.get_instance()
        _drain(tq, monkeypatch)
        monkeypatch.setattr(tq_module, "get_session_state", lambda: 4)
        snapshot = {"name": "place_order", "group": "trade",
                    "had_dialog": False, "status": "1"}
        try:
            tq._last_task_info = dict(snapshot)
            with pytest.raises(ApiError):
                tq.submit(lambda: "ok", "cancel_all_orders", {}, 10)
            assert tq._last_task_info == snapshot
        finally:
            _cleanup(tq)


class TestHealthSessionField:
    """/health 暴露 session 三键（监控轮询 ui_available 的落点）"""

    def test_health_session(self, monkeypatch):
        from src.api.routes import create_app
        import src.api.routes as routes_module
        monkeypatch.setattr(
            routes_module, "session_health",
            lambda: {"connect_state": 4, "state_name": "Disconnected",
                     "ui_available": False})
        app = create_app()
        app.config["TESTING"] = True
        body = app.test_client().get("/health").get_json()
        session = body["data"]["session"]
        assert session["connect_state"] == 4
        assert session["state_name"] == "Disconnected"
        assert session["ui_available"] is False
