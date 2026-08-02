"""核心业务逻辑单元测试

测试不依赖 UI 自动化的纯逻辑函数:
- 价格格式化
- 表格数据解析
- 幂等检查
- 撤单数量解析
"""
import pytest

from src.core.popup_rules import match_popup_rule, match_submit_error
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
        """卖空限制（超出可卖数量）→ SHORT_SELLING_FORBIDDEN"""
        text = "提交失败：股票余额不足，不允许卖空。"
        code, msg, suggestion = self._classify(text)
        from src.exceptions import ErrorCode
        assert code == ErrorCode.SHORT_SELLING_FORBIDDEN, f"期望 SHORT_SELLING_FORBIDDEN，实际 {code}"
        assert "不允许卖空" in msg

    def test_short_selling_no_position(self):
        """卖空限制（无持仓）→ SHORT_SELLING_FORBIDDEN
        关键字: "提交失败" + "无证券" + "持仓信息" """
        text = "提交失败：当前账户10****88无证券601991的持仓信息。"
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


class TestSubmitErrorRules:
    """规则表 match_submit_error 参数化测试（全部错误码 + 变体 + 顺序陷阱）

    规则表顺序敏感，以下场景必须锚定：
    - "可卖数量" 被 T1 规则先命中（先于 INSUFFICIENT_SHARES）——反直觉但符合原行为
    - "可用余额不足" → SHARES（精确短语）；"可用余额不够" → BALANCE
    - "不允许卖空" 先于 "余额不足"（"股票余额不足，不允许卖空" 必须归卖空）
    """

    @pytest.mark.parametrize("text, expected_code", [
        # 清算
        ("清算中，暂不支持委托", "SERVER_CLEARING"),
        ("提交失败：系统正在清算", "SERVER_CLEARING"),
        # 非交易时段
        ("当前时间不允许委托", "OUTSIDE_TRADING_HOURS"),
        ("当前为非交易时段", "OUTSIDE_TRADING_HOURS"),
        # T+1 制度限制
        ("T+1 制度限制，当日买入次日可卖", "T1_RESTRICTION"),
        ("t+1 规则", "T1_RESTRICTION"),
        ("当日买入的股票不能当日卖出", "T1_RESTRICTION"),
        ("未交收证券", "T1_RESTRICTION"),
        # 顺序陷阱：T1 的"可卖数量"先于 INSUFFICIENT_SHARES
        ("可卖数量不足", "T1_RESTRICTION"),
        # 卖空（变体1：不允许卖空）
        ("提交失败：股票余额不足，不允许卖空。", "SHORT_SELLING_FORBIDDEN"),
        ("不允许卖空", "SHORT_SELLING_FORBIDDEN"),
        # 卖空（变体2：无证券持仓信息）
        ("提交失败：当前账户10****88无证券601991的持仓信息。", "SHORT_SELLING_FORBIDDEN"),
        # 余额不足（精确短语）
        ("提交失败：当前账户10****88可用资金不足，还差600.200元。", "INSUFFICIENT_BALANCE"),
        ("提交失败：柜台：可用余额不够。还差：300.30。", "INSUFFICIENT_BALANCE"),
        # 可卖数量不足（"可用余额不足"精确短语，避免误入 T1/BALANCE）
        ("可用余额不足，请调整委托数量", "INSUFFICIENT_SHARES"),
        # 服务器不可用
        ("事务处理机转发数据失败", "SERVER_UNAVAILABLE"),
        ("事务处理机转发失败，请稍后重试", "SERVER_UNAVAILABLE"),
        # 兜底
        ("未知错误，请重试", "ORDER_SUBMIT_FAILED"),
        ("提交失败：系统内部错误", "ORDER_SUBMIT_FAILED"),
    ])
    def test_submit_error_classification(self, text, expected_code):
        """各类弹窗文本 → 精确错误码"""
        rule = match_submit_error(text)
        assert rule.error_code == expected_code, (
            f"text={text!r} 期望 {expected_code}，实际 {rule.error_code}"
        )

    @pytest.mark.parametrize("text, expected_code", [
        ("提交失败：股票余额不足，不允许卖空。", "SHORT_SELLING_FORBIDDEN"),
        ("可用余额不足，请调整委托数量", "INSUFFICIENT_SHARES"),
        ("提交失败：柜台：可用余额不够。还差：300.30。", "INSUFFICIENT_BALANCE"),
        ("可卖数量不足", "T1_RESTRICTION"),
    ])
    def test_order_sensitive_keywords(self, text, expected_code):
        """顺序敏感陷阱：共享关键词的规则归属"""
        rule = match_submit_error(text)
        assert rule.error_code == expected_code

    @pytest.mark.parametrize("text, expected_code, expected_prefix", [
        ("提交失败：股票余额不足，不允许卖空。", "SHORT_SELLING_FORBIDDEN", "不允许卖空: "),
        ("清算中，暂不支持委托", "SERVER_CLEARING", "券商系统清算中: "),
    ])
    def test_message_template_rendering(self, text, expected_code, expected_prefix):
        """message 模板 {text} 占位渲染（截断前 150 字符）"""
        rule = match_submit_error(text)
        assert rule.error_code == expected_code
        message = rule.message_template.replace("{text}", text[:150])
        assert message.startswith(expected_prefix)
        assert text[:150] in message


class TestPopupDispatchRules:
    """规则表 match_popup_rule 参数化测试（弹窗处理动作）"""

    @pytest.mark.parametrize("primary, extract, expected_action, expected_code, expected_clean", [
        # 余额不足三连：提交失败 + 余额/资金 + 还差 → 干净退出
        ("提交失败：当前账户10****88可用资金不足，还差600.200元。", "",
         "raise_error", "INSUFFICIENT_BALANCE", True),
        ("提交失败：柜台：可用余额不够。还差：300.30。", "",
         "raise_error", "INSUFFICIENT_BALANCE", True),
        # 卖空限制 → 干净退出
        ("提交失败：股票余额不足，不允许卖空。", "",
         "raise_error", "SHORT_SELLING_FORBIDDEN", True),
        # 余额不足变体（无提交失败前缀）→ 委托 match_submit_error 分类（error_code=None）
        ("可用余额不足，请调整委托数量", "",
         "raise_error", None, True),
        # 提交失败类（清算/非交易等）→ 委托 classify，不干净
        ("提交失败：清算中", "",
         "raise_error", None, False),
        ("提交失败：当前时间不允许委托", "",
         "raise_error", None, False),
        # 价格超限 → 点「否」取消，干净退出
        ("价格超出涨跌停限制，请调整委托价格", "",
         "click_no", "PRICE_OUT_OF_RANGE", True),
        ("委托价格超出范围", "",
         "click_no", "PRICE_OUT_OF_RANGE", True),
        # 通用警告 → 无规则命中（调用方点「是(Y)」继续）
        ("您确定要提交这笔委托吗？", "",
         None, None, False),
        ("确认提交委托？", "",
         None, None, False),
    ])
    def test_popup_dispatch(self, primary, extract, expected_action, expected_code, expected_clean):
        """弹窗文本 → 处理动作 + 错误码 + 干净退出标志"""
        rule = match_popup_rule(primary, extract)
        if expected_action is None:
            assert rule is None, f"text={primary!r} 应无规则命中，实际 {rule}"
            return
        assert rule is not None, f"text={primary!r} 应有规则命中"
        assert rule.action == expected_action, (
            f"text={primary!r} 期望动作 {expected_action}，实际 {rule.action}"
        )
        assert rule.error_code == expected_code, (
            f"text={primary!r} 期望 {expected_code}，实际 {rule.error_code}"
        )
        assert rule.clean_dismiss == expected_clean

    @pytest.mark.parametrize("primary, extract, expected_code", [
        # cid=1040（primary）为空时，extract 兜底仍能精确分类（卖空/余额不足）
        ("", "提交失败：当前账户10****88无证券601991的持仓信息。", "SHORT_SELLING_FORBIDDEN"),
        ("", "股票余额不足，不允许卖空", "SHORT_SELLING_FORBIDDEN"),
        ("", "提交失败：可用余额不足，还差100元", "INSUFFICIENT_BALANCE"),
        # primary 不完整时 combo 检测兜底
        ("提交失败", "可用余额不足，还差100元", "INSUFFICIENT_BALANCE"),
    ])
    def test_extract_text_fallback(self, primary, extract, expected_code):
        """组合文本兜底：primary 提取不完整时 extract 仍能命中"""
        rule = match_popup_rule(primary, extract)
        assert rule is not None
        assert rule.action == "raise_error"
        assert rule.error_code == expected_code

    def test_price_warning_loses_to_submit_error(self):
        """顺序陷阱：提交失败 + 价格关键词 → 归提交失败类而非价格超限"""
        rule = match_popup_rule("提交失败：价格超出涨跌停限制", "")
        assert rule is not None
        assert rule.action == "raise_error"
        assert rule.error_code is None  # 委托 match_submit_error → 兜底

    def test_short_selling_wins_over_balance(self):
        """顺序陷阱：'股票余额不足，不允许卖空' → 卖空而非余额不足"""
        rule = match_popup_rule("股票余额不足，不允许卖空", "")
        assert rule is not None
        assert rule.error_code == "SHORT_SELLING_FORBIDDEN"
        assert rule.clean_dismiss is True


class TestConfigValidation:
    """启动期配置校验测试（monkeypatch CONFIG_PATH + 单例重置隔离）"""

    @staticmethod
    def _make_config(monkeypatch, tmp_path, overrides):
        import json

        from src.models import config as config_module

        cfg = {
            "trading_app_paths": ["C:\\xiadan.exe"],
            "host": "127.0.0.1",
            "port": 5000,
        }
        cfg.update(overrides)
        p = tmp_path / "app_config.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()
        return config_module.AppConfig()

    def test_valid_config_passes(self, monkeypatch, tmp_path):
        """合法配置通过校验"""
        c = self._make_config(monkeypatch, tmp_path, {})
        assert c.validate() == []

    def test_valid_config_minimal(self, monkeypatch, tmp_path):
        """最小配置（仅必填项）通过校验"""
        c = self._make_config(monkeypatch, tmp_path, {
            "trading_app_paths": None, "host": "0.0.0.0", "port": 8080,
        })
        assert c.validate() == []

    def test_invalid_port_type(self, monkeypatch, tmp_path):
        """port 非数字 → 报错"""
        c = self._make_config(monkeypatch, tmp_path, {"port": "abc"})
        errors = c.validate()
        assert any("port" in e for e in errors)

    def test_invalid_port_range(self, monkeypatch, tmp_path):
        """port 越界 → 报错"""
        c = self._make_config(monkeypatch, tmp_path, {"port": 70000})
        errors = c.validate()
        assert any("port" in e for e in errors)

    def test_port_string_acceptable(self, monkeypatch, tmp_path):
        """port 数字字符串（如 '5000'）→ 通过（int() 转换）"""
        c = self._make_config(monkeypatch, tmp_path, {"port": "5000"})
        assert c.validate() == []

    def test_invalid_paths_type(self, monkeypatch, tmp_path):
        """trading_app_paths 传字符串（应为列表）→ 报错"""
        c = self._make_config(monkeypatch, tmp_path, {"trading_app_paths": "C:\\xiadan.exe"})
        errors = c.validate()
        assert any("trading_app_paths" in e for e in errors)

    def test_empty_path_element(self, monkeypatch, tmp_path):
        """路径列表含空元素 → 报错"""
        c = self._make_config(monkeypatch, tmp_path, {"trading_app_paths": ["C:\\xiadan.exe", ""]})
        errors = c.validate()
        assert any("trading_app_paths[1]" in e for e in errors)

    def test_negative_timeout(self, monkeypatch, tmp_path):
        """看门狗超时为负 → 报错"""
        c = self._make_config(monkeypatch, tmp_path, {
            "task_queue": {"watchdog_timeout_seconds": -5},
        })
        errors = c.validate()
        assert any("watchdog_timeout_seconds" in e for e in errors)

    def test_invalid_host(self, monkeypatch, tmp_path):
        """host 为空 → 报错"""
        c = self._make_config(monkeypatch, tmp_path, {"host": ""})
        errors = c.validate()
        assert any("host" in e for e in errors)
