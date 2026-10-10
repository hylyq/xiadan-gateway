"""券商链路事后门控单元测试

不依赖 UI：monkeypatch read_broker_link 的返回值，验证
- 查询门控：断开在场 → BROKER_DISCONNECTED（结果作废语义）
- 下单/撤单门控：断开在场 → ORDER_STATE_UNKNOWN（状态未知语义）
- fail-open：connected=None（读取失败）与 True 一律放行
"""
import pytest

from src.exceptions import ApiError, ErrorCode
import src.services.broker_link as bl


def _link(connected):
    d = {"status_text": "断开" if connected is False else "mncg18",
         "latency_ms": 1.0}
    if connected is not None:
        d["connected"] = connected
    else:
        d["connected"] = None
        d["reason"] = "pane_pattern_mismatch"
    return d


class TestQueryGate:
    """ensure_connected_or_discard：查询结果作废门控"""

    def test_discard_on_disconnected(self, monkeypatch):
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(False))
        with pytest.raises(ApiError) as ei:
            bl.ensure_connected_or_discard("查询")
        assert ei.value.error_code == ErrorCode.BROKER_DISCONNECTED
        assert "已作废" in ei.value.message
        assert ei.value.details["broker_link"]["connected"] is False

    def test_failopen_on_read_failure(self, monkeypatch):
        """读取失败（None）放行——未知 ≠ 断开"""
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(None))
        assert bl.ensure_connected_or_discard("查询")["connected"] is None

    def test_pass_on_connected(self, monkeypatch):
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(True))
        assert bl.ensure_connected_or_discard("查询")["connected"] is True


class TestOrderGate:
    """check_after_order：下单/撤单状态未知门控"""

    def test_state_unknown_on_disconnected(self, monkeypatch):
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(False))
        with pytest.raises(ApiError) as ei:
            bl.check_after_order("下单提交",
                                 extra_details={"entrust_no": "624686"})
        assert ei.value.error_code == ErrorCode.ORDER_STATE_UNKNOWN
        assert ei.value.details["entrust_no"] == "624686"
        assert "查单" in ei.value.suggestion

    def test_cancel_custom_suggestion(self, monkeypatch):
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(False))
        with pytest.raises(ApiError) as ei:
            bl.check_after_order("撤单", suggestion="自定义指引")
        assert ei.value.suggestion == "自定义指引"

    def test_failopen_on_read_failure(self, monkeypatch):
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(None))
        bl.check_after_order("下单提交")  # 不抛


class TestIdempotencyInteraction:
    """ORDER_STATE_UNKNOWN 必须保留幂等记录（与 TASK_TIMEOUT 同级）"""

    def test_keep_record_includes_order_state_unknown(self):
        from src.api.idempotency import should_keep_record_on_error
        assert should_keep_record_on_error(
            ApiError(ErrorCode.ORDER_STATE_UNKNOWN, "x"))
        # BROKER_DISCONNECTED 是查询语义错误，不在保留清单（下单路径
        # 抛的是 ORDER_STATE_UNKNOWN；查询无幂等记录可保留）
        assert not should_keep_record_on_error(
            ApiError(ErrorCode.BROKER_DISCONNECTED, "x"))


class TestPokeIfDisconnected:
    """查询前置自愈：断开在场 → F5 戳（防抖 30s）→ 轮询等重连"""

    @pytest.fixture(autouse=True)
    def _reset_poke_state(self, monkeypatch):
        monkeypatch.setattr(bl, "_last_poke_mono", -1e9)

    def test_no_poke_when_connected(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(True))
        monkeypatch.setattr(bl, "_send_f5_poke",
                            lambda: calls.append(1))
        bl.poke_if_disconnected(max_wait_s=0.1)
        assert calls == []

    def test_poke_and_wait_until_healed(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bl, "_send_f5_poke",
                            lambda: calls.append(1))
        # 首读=断开，F5 后轮询读到已重连
        states = [_link(False), _link(True), _link(True), _link(True)]
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: states.pop(0) if states else _link(True))
        link = bl.poke_if_disconnected(max_wait_s=2.0)
        assert calls == [1]
        assert link["connected"] is True

    def test_poke_once_when_stays_disconnected(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bl, "_send_f5_poke",
                            lambda: calls.append(1))
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(False))
        bl.poke_if_disconnected(max_wait_s=0.3)
        assert calls == [1]

    def test_debounce_second_call_within_30s(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bl, "_send_f5_poke",
                            lambda: calls.append(1))
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(False))
        bl.poke_if_disconnected(max_wait_s=0.1)
        bl.poke_if_disconnected(max_wait_s=0.1)
        assert calls == [1]  # 防抖：第二发被拦


class _StubWindowService:
    def __init__(self):
        self.keys = []

    def send_key(self, keys, **kwargs):
        self.keys.append(keys)


class TestSamePageRefresh:
    """同页连续查询：第二次起必须先 F5 刷新再复制（用户要求 2026-10-10）

    - 不同页/首次 → 直接复制（真实页面切换会重新拉数据）
    - 同页 + 连接态 → F5 + 等待后复制
    - 同页 + 断连 → 跳过（断开场景整次查询只有前置重连戳一次 F5）
    - 同页 + 5s 内已发过 F5 → 跳过（前置戳已顺带刷新）
    """

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        import time as _time
        from src.services.position_service import PositionService
        monkeypatch.setattr(PositionService, "_last_copied_page", None)
        monkeypatch.setattr(_time, "sleep", lambda s: None)  # 掉 0.8s 等待
        self.ws = _StubWindowService()
        self.svc = PositionService(self.ws)
        self.marks = []
        monkeypatch.setattr(bl, "mark_f5_sent",
                            lambda: self.marks.append(1))

    def test_different_page_no_f5(self, monkeypatch):
        from src.services.position_service import PositionService
        PositionService._last_copied_page = "当日委托"
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(True))
        self.svc._f5_refresh_if_same_page("资金股票")
        assert self.ws.keys == []

    def test_first_query_no_f5(self, monkeypatch):
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(True))
        self.svc._f5_refresh_if_same_page("资金股票")
        assert self.ws.keys == []

    def test_same_page_connected_sends_f5(self, monkeypatch):
        from src.services.position_service import PositionService
        PositionService._last_copied_page = "资金股票"
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(True))
        monkeypatch.setattr(bl, "f5_sent_recently", lambda w: False)
        self.svc._f5_refresh_if_same_page("资金股票")
        assert self.ws.keys == ["F5"]
        assert self.marks == [1]

    def test_same_page_disconnected_skips(self, monkeypatch):
        """断开场景整次查询只有前置重连戳那一次 F5"""
        from src.services.position_service import PositionService
        PositionService._last_copied_page = "资金股票"
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(False))
        self.svc._f5_refresh_if_same_page("资金股票")
        assert self.ws.keys == []

    def test_same_page_recent_f5_skips(self, monkeypatch):
        from src.services.position_service import PositionService
        PositionService._last_copied_page = "资金股票"
        monkeypatch.setattr(bl, "read_broker_link",
                            lambda: _link(True))
        monkeypatch.setattr(bl, "f5_sent_recently", lambda w: True)
        self.svc._f5_refresh_if_same_page("资金股票")
        assert self.ws.keys == []

    def test_copy_verified_tracks_last_page(self, monkeypatch):
        from src.services.position_service import PositionService
        self.svc._copy_table = lambda: [{"代码": "600000"}]
        self.svc._is_table_matching = lambda data, req, require_all=True: True
        out = self.svc._copy_table_verified("持仓", set(),
                                            page_name="资金股票")
        assert out == [{"代码": "600000"}]
        assert PositionService._last_copied_page == "资金股票"
