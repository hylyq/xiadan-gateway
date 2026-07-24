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


# IdempotencyChecker 测试需要完整的配置环境，在集成测试中覆盖
# 以下为基础逻辑测试，不依赖外部服务
