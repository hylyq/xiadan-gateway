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
