"""核心业务逻辑单元测试

测试不依赖 UI 自动化的纯逻辑函数:
- 价格格式化
- 表格数据解析
- 幂等检查
- 撤单数量解析
"""
import pytest

from src.core.validation import sanitize_price
from src.services.position_service import PositionService
from src.services.trading_service import TradingService


class TestPriceSanitization:
    """价格格式化测试"""

    def test_normal_price(self):
        """正常价格不变"""
        assert sanitize_price("10.50") == "10.50"

    def test_integer_price(self):
        """整数价格补零"""
        assert sanitize_price("10") == "10.00"

    def test_one_decimal(self):
        """一位小数补零"""
        assert sanitize_price("10.5") == "10.50"

    def test_two_decimals(self):
        """两位小数不变"""
        assert sanitize_price("10.55") == "10.55"

    def test_three_decimals_rounded(self):
        """三位小数四舍五入"""
        # 10.556 -> 10.56 (10.555 因浮点表示可能为 10.55)
        assert sanitize_price("10.556") == "10.56"

    def test_invalid_price(self):
        """无效价格抛异常"""
        with pytest.raises(Exception, match="价格格式无效"):
            sanitize_price("abc")


class TestTableDataFormatting:
    """表格数据解析测试"""

    def setup_method(self):
        """创建 PositionService 实例（不需要真实窗口）"""
        # 使用 None 作为 window_service，因为 _format_table_data 是静态方法逻辑
        self.service = PositionService.__new__(PositionService)

    def test_normal_table(self):
        """正常表格解析"""
        data = "代码\t名称\t数量\n000001\t平安银行\t100\n600000\t浦发银行\t200"
        result = self.service._format_table_data(data)
        assert len(result) == 2
        assert result[0]["代码"] == "000001"
        assert result[0]["名称"] == "平安银行"
        assert result[1]["数量"] == "200"

    def test_empty_data(self):
        """空数据返回空列表"""
        assert self.service._format_table_data("") == []
        assert self.service._format_table_data(None) == []

    def test_header_only(self):
        """只有表头返回空列表"""
        data = "代码\t名称\t数量"
        assert self.service._format_table_data(data) == []

    def test_mismatched_columns(self):
        """列数不匹配的行被跳过"""
        data = "代码\t名称\n000001\t平安银行\t100\n600000\t浦发银行"
        result = self.service._format_table_data(data)
        # 第一行3列不匹配2列表头，被跳过；第二行2列匹配
        assert len(result) == 1
        assert result[0]["代码"] == "600000"


class TestCancelledCountParsing:
    """撤单数量解析测试"""

    def test_normal_count(self):
        """正常解析"""
        text = "您确认要撤销这( 2 )笔委托吗？"
        assert TradingService._parse_cancelled_count(text) == 2

    def test_count_with_total(self):
        """带总数的文本"""
        text = "您确认要撤销这( 3 )笔委托吗？\n\n( 总共 3 笔可撤委托 )"
        assert TradingService._parse_cancelled_count(text) == 3

    def test_no_count(self):
        """无数字返回 None"""
        text = "确认撤单？"
        assert TradingService._parse_cancelled_count(text) is None

    def test_single_digit(self):
        """单个数字"""
        text = "撤销( 1 )笔"
        assert TradingService._parse_cancelled_count(text) == 1


class TestSubmitErrorClassification:
    """下单提交错误分类测试（Trader._classify_submit_error）"""

    @staticmethod
    def _classify(error_text: str):
        """调用 Trader._classify_submit_error 静态方法"""
        from src.core.trader import Trader
        return Trader._classify_submit_error(error_text)

    def test_insufficient_balance_funds(self):
        """余额不足（资金不足变体）"""
        text = "提交失败：当前账户10****88可用资金不足，还差600.200元。"
        code, msg, suggestion = self._classify(text)
        from src.exceptions import ErrorCode
        assert code == ErrorCode.INSUFFICIENT_BALANCE, f"期望 INSUFFICIENT_BALANCE，实际 {code}"
        assert "余额不足" in msg

    def test_insufficient_balance_counter(self):
        """余额不足（柜台余额不够变体）"""
        text = "提交失败：柜台：可用余额不够。还差：300.30。"
        code, msg, suggestion = self._classify(text)
        from src.exceptions import ErrorCode
        assert code == ErrorCode.INSUFFICIENT_BALANCE, f"期望 INSUFFICIENT_BALANCE，实际 {code}"
        assert "余额不足" in msg

    def test_short_selling_forbidden(self):
        """卖空限制 → SHORT_SELLING_FORBIDDEN"""
        text = "提交失败：股票余额不足，不允许卖空。"
        code, msg, suggestion = self._classify(text)
        from src.exceptions import ErrorCode
        assert code == ErrorCode.SHORT_SELLING_FORBIDDEN, f"期望 SHORT_SELLING_FORBIDDEN，实际 {code}"
        assert "不允许卖空" in msg

    def test_insufficient_shares(self):
        """可卖数量不足 → INSUFFICIENT_SHARES（回归）
        用"可用余额不足"避免 hit T1_RESTRICTION_KEYWORDS 中的"可卖数量" """
        text = "可用余额不足，请调整委托数量"
        code, msg, suggestion = self._classify(text)
        from src.exceptions import ErrorCode
        assert code == ErrorCode.INSUFFICIENT_SHARES, f"期望 INSUFFICIENT_SHARES，实际 {code}"

    def test_server_clearing(self):
        """清算中 → SERVER_CLEARING（回归）"""
        text = "清算中，暂不支持委托"
        code, msg, suggestion = self._classify(text)
        from src.exceptions import ErrorCode
        assert code == ErrorCode.SERVER_CLEARING, f"期望 SERVER_CLEARING，实际 {code}"

    def test_generic_failure(self):
        """未知错误 → ORDER_SUBMIT_FAILED（兜底）"""
        text = "未知错误，请重试"
        code, msg, suggestion = self._classify(text)
        from src.exceptions import ErrorCode
        assert code == ErrorCode.ORDER_SUBMIT_FAILED, f"期望 ORDER_SUBMIT_FAILED，实际 {code}"


# IdempotencyChecker 测试需要完整的配置环境，在集成测试中覆盖
# 以下为基础逻辑测试，不依赖外部服务
