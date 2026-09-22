"""核心业务逻辑单元测试

测试不依赖 UI 自动化的纯逻辑函数:
- 价格格式化
- 表格数据解析
- 幂等检查
- 撤单数量解析
"""
import pytest

from src.core.popup_rules import match_popup_rule, match_submit_error
from src.core.validation import check_trading_hours, sanitize_price
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


class TestTradingHoursCheck:
    """交易时段预检测试（order.reject_outside_trading_hours 开启时的快速失败）

    纯函数注入时刻判定，不含节假日历——法定节假日需依赖券商报错兜底，
    或临时关闭该开关。
    """

    @staticmethod
    def _at(year, month, day, hour, minute):
        from datetime import datetime
        return datetime(year, month, day, hour, minute)

    def test_weekday_morning_session(self):
        """工作日上午盘（9:15-11:30）→ 允许"""
        ok, _reason = check_trading_hours(self._at(2026, 9, 15, 10, 0))  # 周二
        assert ok is True

    def test_weekday_afternoon_session(self):
        """工作日下午盘（13:00-15:00）→ 允许"""
        ok, _reason = check_trading_hours(self._at(2026, 9, 15, 14, 30))  # 周二
        assert ok is True

    def test_pre_market_too_early(self):
        """早于 9:15（集合竞价受理开始）→ 拒绝"""
        ok, reason = check_trading_hours(self._at(2026, 9, 15, 9, 10))
        assert ok is False
        assert "非交易" in reason

    def test_call_auction_open(self):
        """9:15 集合竞价受理开始 → 允许"""
        ok, _reason = check_trading_hours(self._at(2026, 9, 15, 9, 15))
        assert ok is True

    def test_lunch_break(self):
        """午间休市（12:00）→ 拒绝"""
        ok, _reason = check_trading_hours(self._at(2026, 9, 15, 12, 0))
        assert ok is False

    def test_after_close(self):
        """收盘后（15:30）→ 拒绝"""
        ok, _reason = check_trading_hours(self._at(2026, 9, 15, 15, 30))
        assert ok is False

    def test_market_close_boundary(self):
        """15:00 收盘边界 → 允许（含端点）"""
        ok, _reason = check_trading_hours(self._at(2026, 9, 15, 15, 0))
        assert ok is True

    def test_saturday_rejected(self):
        """周六 → 拒绝"""
        ok, reason = check_trading_hours(self._at(2026, 9, 12, 10, 0))
        assert ok is False
        assert "周末" in reason

    def test_sunday_rejected(self):
        """周日 → 拒绝"""
        ok, _reason = check_trading_hours(self._at(2026, 9, 13, 14, 0))
        assert ok is False

    # ── 法定节假日（chinesecalendar，优雅降级） ──────────────────

    @staticmethod
    def _fake_calendar(monkeypatch, is_workday_impl=None, absent=False):
        """伪造/移除 chinese_calendar 模块，模拟依赖缺失与数据边界"""
        import sys
        import types

        if absent:
            # sys.modules 中置 None → from ... import 抛 ImportError
            monkeypatch.setitem(sys.modules, "chinese_calendar", None)
            return
        mod = types.ModuleType("chinese_calendar")
        mod.is_workday = is_workday_impl or (lambda d: True)
        monkeypatch.setitem(sys.modules, "chinese_calendar", mod)

    def test_weekday_holiday_rejected(self, monkeypatch):
        """工作日但为法定节假日 → 拒绝（依赖可用时）"""
        self._fake_calendar(monkeypatch, is_workday_impl=lambda d: False)
        ok, reason = check_trading_hours(self._at(2026, 10, 1, 10, 0))
        assert ok is False
        assert "节假日" in reason

    def test_weekday_workday_allowed(self, monkeypatch):
        """工作日且非节假日 → 正常按时段判定（上午盘放行）"""
        self._fake_calendar(monkeypatch, is_workday_impl=lambda d: True)
        ok, _reason = check_trading_hours(self._at(2026, 9, 15, 10, 0))
        assert ok is True

    def test_calendar_data_uncovered_degrades_to_weekday(self, monkeypatch):
        """数据未覆盖该年份（NotImplementedError）→ 退回工作日粗判放行"""
        def _raise(d):
            raise NotImplementedError("no data for year")
        self._fake_calendar(monkeypatch, is_workday_impl=_raise)
        ok, _reason = check_trading_hours(self._at(2027, 1, 5, 10, 0))
        assert ok is True

    def test_calendar_absent_degrades_to_weekday(self, monkeypatch):
        """未安装 chinesecalendar → 行为与旧版一致（工作日放行）"""
        self._fake_calendar(monkeypatch, absent=True)
        ok, _reason = check_trading_hours(self._at(2026, 9, 15, 10, 0))
        assert ok is True

    def test_real_calendar_national_day_2026(self):
        """集成锚点：2026-10-01（周四，国庆）→ 真实数据判定拒绝"""
        ok, reason = check_trading_hours(self._at(2026, 10, 1, 10, 0))
        assert ok is False
        assert "节假日" in reason

    def test_swap_weekend_still_rejected(self):
        """调休补班的周末（2026-10-10 周六 is_workday=True）→ 仍按周末拒绝"""
        ok, reason = check_trading_hours(self._at(2026, 10, 10, 10, 0))
        assert ok is False
        assert "周末" in reason


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

    def test_empty_header_column_dropped(self):
        """空表头列被过滤——持仓/委托首列空表头不再产出 "" 键（实测数据质量问题）"""
        data = "\t证券代码\t证券名称\n\t000001\t平安银行"
        result = self.service._format_table_data(data)
        assert result == [{"证券代码": "000001", "证券名称": "平安银行"}]

    def test_empty_header_in_middle_dropped(self):
        """中间位置的空表头列同样过滤，后续列值不错位"""
        data = "证券代码\t\t证券名称\n000001\tX\t平安银行"
        result = self.service._format_table_data(data)
        assert result == [{"证券代码": "000001", "证券名称": "平安银行"}]

    def test_whitespace_header_dropped(self):
        """纯空白表头（"  "）同样过滤"""
        data = "  \t证券代码\n\t000001"
        result = self.service._format_table_data(data)
        assert result == [{"证券代码": "000001"}]


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

    def test_price_required(self):
        """请输入委托价格 → ORDER_PRICE_REQUIRED（市价类型未选择/限价未传价格）"""
        code, msg, suggestion = self._classify("请输入委托价格")
        from src.exceptions import ErrorCode
        assert code == ErrorCode.ORDER_PRICE_REQUIRED, f"期望 ORDER_PRICE_REQUIRED，实际 {code}"
        assert "限价" in suggestion

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
        # 券商要求填写委托价格（市价类型未选择/不受支持，或限价未传价格）
        # → 必须先于价格超限规则命中（"价格"关键词会截胡），2026-09-22 实测回归
        ("请输入委托价格", "",
         "raise_error", "ORDER_PRICE_REQUIRED", True),
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


class TestAuthMiddleware:
    """认证中间件测试（Flask test client + monkeypatch 配置）

    锚定行为变更：query string 传 token（?token=xxx）已移除，
    只接受 Authorization: Bearer / X-API-Key 请求头。
    """

    @staticmethod
    def _make_client(monkeypatch, tmp_path, auth_enabled, token):
        import json

        from src.models import config as config_module

        cfg = {"auth": {"enabled": auth_enabled, "token": token}}
        p = tmp_path / "app_config.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()

        from src.api.routes import create_app
        app = create_app()
        app.config["TESTING"] = True
        return app.test_client()

    def test_auth_disabled_no_token_needed(self, monkeypatch, tmp_path):
        """auth.enabled=false → 无认证要求"""
        client = self._make_client(monkeypatch, tmp_path, False, "")
        r = client.get("/queue/status")
        assert r.get_json()["status"] == "success"

    def test_missing_token_rejected(self, monkeypatch, tmp_path):
        """启用认证后缺 token → AUTH_REQUIRED"""
        client = self._make_client(monkeypatch, tmp_path, True, "secret")
        r = client.get("/queue/status")
        body = r.get_json()
        assert body["status"] == "error"
        assert body["error_code"] == "AUTH_REQUIRED"

    def test_wrong_token_rejected(self, monkeypatch, tmp_path):
        """错误 token → AUTH_FAILED"""
        client = self._make_client(monkeypatch, tmp_path, True, "secret")
        r = client.get("/queue/status", headers={"X-API-Key": "wrong"})
        assert r.get_json()["error_code"] == "AUTH_FAILED"

    def test_bearer_token_accepted(self, monkeypatch, tmp_path):
        """Authorization: Bearer <token> 通过"""
        client = self._make_client(monkeypatch, tmp_path, True, "secret")
        r = client.get("/queue/status", headers={"Authorization": "Bearer secret"})
        assert r.get_json()["status"] == "success"

    def test_api_key_header_accepted(self, monkeypatch, tmp_path):
        """X-API-Key: <token> 通过"""
        client = self._make_client(monkeypatch, tmp_path, True, "secret")
        r = client.get("/queue/status", headers={"X-API-Key": "secret"})
        assert r.get_json()["status"] == "success"

    def test_query_string_token_rejected(self, monkeypatch, tmp_path):
        """行为锚定：?token=xxx 不再被接受（已移除 query string 传参）"""
        client = self._make_client(monkeypatch, tmp_path, True, "secret")
        r = client.get("/queue/status?token=secret")
        assert r.get_json()["error_code"] == "AUTH_REQUIRED"

    def test_health_always_public(self, monkeypatch, tmp_path):
        """/health 始终公开（监控探活不受认证影响）"""
        client = self._make_client(monkeypatch, tmp_path, True, "secret")
        r = client.get("/health")
        assert r.get_json()["status"] == "success"

    def test_health_hides_app_paths(self, monkeypatch, tmp_path):
        """/health 不返回 trading_app_paths（不对未认证访客暴露本机路径）"""
        client = self._make_client(monkeypatch, tmp_path, False, "")
        body = client.get("/health").get_json()
        assert "trading_app_paths" not in body["data"]


class TestRouteErrorPaths:
    """路由层错误路径测试（Flask test client + monkeypatch 任务失败）

    锚定 2026-09-15 模拟盘实测发现的 P0 回归：下单路由 except 分支误调
    IdempotencyChecker 实例上不存在的 should_keep_record_on_error（实为
    模块级函数），任务一失败 except 自身抛 AttributeError → Flask HTML
    500，且幂等记录不清除（失败订单 60s 内重试被 DUPLICATE_ORDER 误拦）。
    纯函数单测覆盖不到这条集成路径，此测试类专职堵住。
    """

    @staticmethod
    def _make_client(monkeypatch, tmp_path):
        import json

        from src.models import config as config_module

        p = tmp_path / "app_config.json"
        p.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()

        from src.api.routes import create_app
        app = create_app()
        app.config["TESTING"] = True
        return app.test_client()

    @staticmethod
    def _fail_submit(monkeypatch, exc):
        """让 TaskQueue.submit 直接抛指定异常（模拟任务执行失败/超时）"""
        from src.api.task_queue import TaskQueue

        def _raise(self, *args, **kwargs):
            raise exc

        monkeypatch.setattr(TaskQueue, "submit", _raise)

    def test_order_failure_returns_json_and_clears_record(self, monkeypatch, tmp_path):
        """下单任务失败（干净退出类错误）→ 统一 JSON + 幂等记录清除允许重试"""
        from src.api.idempotency import IdempotencyChecker
        from src.exceptions import ApiError, ErrorCode

        client = self._make_client(monkeypatch, tmp_path)
        self._fail_submit(monkeypatch, ApiError(
            ErrorCode.PRICE_OUT_OF_RANGE, "价格超出涨跌停限制", suggestion="调整价格"))
        IdempotencyChecker._reset_instance()

        r = client.post("/orders", json={
            "code": "601991", "status": "1", "amount": "100", "price": "1.00"})

        body = r.get_json()
        assert body is not None, "失败响应必须是 JSON（曾回归为 Flask HTML 500）"
        assert body["status"] == "error"
        assert body["error_code"] == "PRICE_OUT_OF_RANGE"
        # 失败订单必须清除幂等记录：60s 内重试不被 DUPLICATE_ORDER 误拦
        assert IdempotencyChecker.get_instance()._records == {}

    def test_order_timeout_keeps_record(self, monkeypatch, tmp_path):
        """看门狗超时（TASK_TIMEOUT）→ JSON 错误 + 幂等记录保留（防重试重复下单）"""
        from src.api.idempotency import IdempotencyChecker
        from src.exceptions import ApiError, ErrorCode

        client = self._make_client(monkeypatch, tmp_path)
        self._fail_submit(monkeypatch, ApiError(
            ErrorCode.TASK_TIMEOUT, "任务执行超时", suggestion="检查订单状态"))
        IdempotencyChecker._reset_instance()

        r = client.post("/orders", json={
            "code": "601991", "status": "1", "amount": "100", "price": "1.00"})

        body = r.get_json()
        assert body is not None, "失败响应必须是 JSON（曾回归为 Flask HTML 500）"
        assert body["status"] == "error"
        assert body["error_code"] == "TASK_TIMEOUT"
        # 超时订单可能仍在执行 → 记录必须保留
        assert len(IdempotencyChecker.get_instance()._records) == 1

    def test_query_failure_returns_json(self, monkeypatch, tmp_path):
        """查询任务失败（OCR_FAILED）→ 统一 JSON 错误"""
        from src.exceptions import ApiError, ErrorCode

        client = self._make_client(monkeypatch, tmp_path)
        self._fail_submit(monkeypatch, ApiError(
            ErrorCode.OCR_FAILED, "验证码识别失败", suggestion="稍后重试"))

        r = client.get("/positions")

        body = r.get_json()
        assert body is not None, "失败响应必须是 JSON"
        assert body["status"] == "error"
        assert body["error_code"] == "OCR_FAILED"

    def test_cancel_generic_failure_returns_json(self, monkeypatch, tmp_path):
        """撤单任务未知异常 → 统一 JSON（INTERNAL_ERROR），不泄漏 HTML 500"""
        client = self._make_client(monkeypatch, tmp_path)
        self._fail_submit(monkeypatch, Exception("窗口操作意外失败"))

        r = client.post("/orders/cancel-all", json={"type": "A"})

        body = r.get_json()
        assert body is not None, "失败响应必须是 JSON"
        assert body["status"] == "error"
        assert body["error_code"] == "INTERNAL_ERROR"


class TestWindowStateReporting:
    """窗口状态通道测试（#6 重构锚定）

    业务方法通过 @report_window_state 装饰器在结束时把状态写入
    task.window_state，TaskQueue 不再读取业务类变量（隐式契约已消除）。
    """

    @staticmethod
    def _make_task_queue():
        from src.api.task_queue import TaskQueue
        return TaskQueue.get_instance()

    def test_record_window_state_trader(self):
        """Trader 实例属性 → task.window_state（had_dialog + clean）"""
        from src.api.task_queue import Task
        tq = self._make_task_queue()
        task = Task(lambda: None, "place_order", {}, 30)
        tq._current_task = task
        try:
            class FakeTrader:
                _had_any_dialog = True
                _clean_dismiss = True
            tq._record_window_state(FakeTrader())
            assert task.window_state == {"had_dialog": True, "clean": True}
        finally:
            tq._current_task = None

    def test_record_window_state_cancel(self):
        """TradingService 用 _had_dialog 属性 → 同样上报"""
        from src.api.task_queue import Task
        tq = self._make_task_queue()
        task = Task(lambda: None, "cancel_all_orders", {}, 30)
        tq._current_task = task
        try:
            class FakeCancelService:
                _had_dialog = True
            tq._record_window_state(FakeCancelService())
            assert task.window_state == {"had_dialog": True, "clean": False}
        finally:
            tq._current_task = None

    def test_no_window_state_defaults_to_clean(self):
        """无装饰器任务（查询）window_state=None → had_dialog=False 可跳过"""
        from src.api.task_queue import Task
        tq = self._make_task_queue()
        task = Task(lambda: None, "get_position", {}, 30)
        tq._last_task_info = None
        try:
            tq._update_task_state(task)
            assert tq._last_task_info == {
                "name": "get_position",
                "group": "query",
                "had_dialog": False,
            }
        finally:
            tq._last_task_info = None

    def test_can_skip_same_group_no_dialog(self):
        """同组 + 上笔无弹窗 → 可跳过窗口准备"""
        from src.api.task_queue import Task
        tq = self._make_task_queue()
        tq._last_task_info = {"name": "place_order", "group": "trade",
                              "had_dialog": False, "status": "1"}
        try:
            task = Task(lambda: None, "place_order", {"status": "1"}, 30)
            assert tq._can_skip_window_setup(task) is True
        finally:
            tq._last_task_info = None

    def test_can_skip_cross_direction(self):
        """同组买→卖（状态不同）仍可跳过重置（只按 F1/F2）"""
        from src.api.task_queue import Task
        tq = self._make_task_queue()
        tq._last_task_info = {"name": "place_order", "group": "trade",
                              "had_dialog": False, "status": "1"}
        try:
            task = Task(lambda: None, "place_order", {"status": "2"}, 30)
            assert tq._can_skip_window_setup(task) is True
        finally:
            tq._last_task_info = None

    def test_cannot_skip_different_group(self):
        """不同组（trade→cancel）→ 不可跳过"""
        from src.api.task_queue import Task
        tq = self._make_task_queue()
        tq._last_task_info = {"name": "place_order", "group": "trade",
                              "had_dialog": False, "status": "1"}
        try:
            task = Task(lambda: None, "cancel_all_orders", {}, 30)
            assert tq._can_skip_window_setup(task) is False
        finally:
            tq._last_task_info = None

    def test_cannot_skip_after_dialog(self):
        """上笔有弹窗 → 不可跳过"""
        from src.api.task_queue import Task
        tq = self._make_task_queue()
        tq._last_task_info = {"name": "place_order", "group": "trade",
                              "had_dialog": True, "status": "1"}
        try:
            task = Task(lambda: None, "place_order", {"status": "1"}, 30)
            assert tq._can_skip_window_setup(task) is False
        finally:
            tq._last_task_info = None

    def test_report_decorator_writes_task_state(self):
        """装饰器端到端：业务方法执行后 task.window_state 已写入"""
        from src.api.task_queue import Task, TaskQueue, report_window_state
        tq = TaskQueue.get_instance()

        class FakeBusiness:
            def __init__(self):
                self._had_any_dialog = False
                self._clean_dismiss = True

            @report_window_state
            def run(self):
                return "ok"

        task = Task(lambda: None, "place_order", {}, 30)
        tq._current_task = task
        try:
            result = FakeBusiness().run()
            assert result == "ok"
            assert task.window_state == {"had_dialog": False, "clean": True}
        finally:
            tq._current_task = None

    def test_report_decorator_on_exception(self):
        """异常路径也上报状态（finally 语义）"""
        from src.api.task_queue import Task, TaskQueue, report_window_state
        tq = TaskQueue.get_instance()

        class FakeBusiness:
            def __init__(self):
                self._had_any_dialog = True
                self._clean_dismiss = True

            @report_window_state
            def run(self):
                raise RuntimeError("boom")

        task = Task(lambda: None, "place_order", {}, 30)
        tq._current_task = task
        try:
            with pytest.raises(RuntimeError):
                FakeBusiness().run()
            assert task.window_state == {"had_dialog": True, "clean": True}
        finally:
            tq._current_task = None


class _FakeEl:
    """最小 UIA 元素模拟（window_text 返回固定文本）"""

    def __init__(self, text):
        self._text = text

    def window_text(self):
        return self._text


class TestRunStats:
    """运行统计测试（#12：错误码聚合 + 连续失败告警）"""

    @staticmethod
    def _make_task(name="place_order"):
        from src.api.task_queue import Task
        return Task(lambda: None, name, {}, 30)

    def test_aggregate_error_counts(self):
        """按错误码聚合 + 成功率计算"""
        from src.api.task_queue import TaskQueue
        from src.exceptions import ApiError, ErrorCode
        tq = TaskQueue.get_instance()
        tq._recent_tasks.clear()
        tq._consecutive_failures = 0
        try:
            for _ in range(3):
                task = self._make_task()
                task.error = ApiError(ErrorCode.SERVER_CLEARING, "清算中")
                tq._record_task_outcome(task)
            for _ in range(2):
                task = self._make_task("get_balance")
                tq._record_task_outcome(task)

            stats = tq.get_stats()
            assert stats["total_tasks"] == 5
            assert stats["success_count"] == 2
            assert stats["failure_count"] == 3
            assert stats["success_rate"] == 0.4
            assert stats["error_counts"].get("SERVER_CLEARING") == 3
            assert stats["consecutive_failures"] == 0  # 最后成功已清零
        finally:
            tq._recent_tasks.clear()
            tq._consecutive_failures = 0

    def test_unknown_error_defaults_to_internal(self):
        """未知异常归 INTERNAL_ERROR"""
        from src.api.task_queue import TaskQueue
        tq = TaskQueue.get_instance()
        tq._recent_tasks.clear()
        try:
            task = self._make_task()
            task.error = RuntimeError("boom")
            tq._record_task_outcome(task)
            stats = tq.get_stats()
            assert stats["error_counts"].get("INTERNAL_ERROR") == 1
        finally:
            tq._recent_tasks.clear()

    def test_empty_window_returns_none_rate(self):
        """窗口内无任务 → success_rate=None（无法计算）"""
        from src.api.task_queue import TaskQueue
        tq = TaskQueue.get_instance()
        tq._recent_tasks.clear()
        try:
            stats = tq.get_stats()
            assert stats["total_tasks"] == 0
            assert stats["success_rate"] is None
        finally:
            tq._recent_tasks.clear()

    def test_consecutive_failure_alert_at_3(self):
        """连续 3 次失败 → 告警日志"""
        from src.api.task_queue import TaskQueue
        from src.exceptions import ApiError, ErrorCode
        from src.utils.logger import Logger
        tq = TaskQueue.get_instance()
        tq._recent_tasks.clear()
        tq._consecutive_failures = 0
        Logger.get_instance().log_cache.clear()
        try:
            for _ in range(3):
                task = self._make_task()
                task.error = ApiError(ErrorCode.INTERNAL_ERROR, "测试")
                tq._record_task_outcome(task)
            logs = "\n".join(Logger.get_instance().log_cache)
            assert "连续 3 次任务失败" in logs
            assert tq._consecutive_failures == 3
        finally:
            tq._recent_tasks.clear()
            tq._consecutive_failures = 0

    def test_success_resets_consecutive_counter(self):
        """成功任务清零连续失败计数"""
        from src.api.task_queue import TaskQueue
        from src.exceptions import ApiError, ErrorCode
        tq = TaskQueue.get_instance()
        tq._recent_tasks.clear()
        tq._consecutive_failures = 0
        try:
            for _ in range(2):
                task = self._make_task()
                task.error = ApiError(ErrorCode.INTERNAL_ERROR, "测试")
                tq._record_task_outcome(task)
            ok_task = self._make_task()
            tq._record_task_outcome(ok_task)
            assert tq._consecutive_failures == 0
        finally:
            tq._recent_tasks.clear()
            tq._consecutive_failures = 0

    def test_health_returns_stats(self, monkeypatch, tmp_path):
        """/health 响应包含 stats 字段"""
        from src.api.routes import create_app
        from src.models import config as config_module
        import json

        p = tmp_path / "app_config.json"
        p.write_text(json.dumps({"trading_app_paths": []}), encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()

        app = create_app()
        app.config["TESTING"] = True
        body = app.test_client().get("/health").get_json()
        stats = body["data"]["stats"]
        assert "total_tasks" in stats
        assert "success_rate" in stats
        assert "error_counts" in stats
        assert "consecutive_failures" in stats


class TestPopupTextExtraction:
    """弹窗文本提取测试（#8：容器优先 + 黑名单降级兜底）"""

    @staticmethod
    def _extract(descendants, title_el=None):
        from src.core.trader import Trader
        return Trader._extract_popup_error_text(descendants, title_el=title_el)

    def test_container_first_strategy(self):
        """title_el 提供且容器有文本 → 容器优先（纯净，黑名单不参与）"""
        container_descendants = [
            _FakeEl("提交失败：可用余额不足，还差100元。"),
            _FakeEl("是(Y)"),  # 容器提取过滤按钮文字
        ]

        class FakeContainer:
            def descendants(self):
                return container_descendants

        class FakeTitle:
            def parent(self):
                return FakeContainer()

        # 全局扫描路径包含 UI 标签（若走兜底会污染结果）
        descendants = [
            _FakeEl("证券代码"), _FakeEl("买入价格"),
            _FakeEl("提交失败：可用余额不足，还差100元。"),
        ]
        result = self._extract(descendants, title_el=FakeTitle())
        assert "提交失败：可用余额不足，还差100元。" in result
        assert "是(Y)" not in result
        assert "证券代码" not in result
        assert "买入价格" not in result

    def test_blacklist_fallback_without_title_el(self):
        """无 title_el → 全局扫描 + 黑名单过滤（UI 标签被过滤）"""
        descendants = [
            _FakeEl("证券代码"), _FakeEl("买入价格"),
            _FakeEl("提交失败：可用余额不足，还差100元。"),
        ]
        result = self._extract(descendants)
        assert "提交失败：可用余额不足，还差100元。" in result
        assert "证券代码" not in result
        assert "买入价格" not in result

    def test_empty_container_falls_back_to_scan(self):
        """容器提取为空 → 降级全局扫描兜底"""
        class FakeEmptyContainer:
            def descendants(self):
                return [_FakeEl("")]

        class FakeTitle:
            def parent(self):
                return FakeEmptyContainer()

        descendants = [_FakeEl("提交失败：清算中")]
        result = self._extract(descendants, title_el=FakeTitle())
        assert "提交失败：清算中" in result

    def test_filters_digits_and_timestamps(self):
        """纯数字/时间戳（分页信息）被过滤"""
        descendants = [
            _FakeEl("00:00:22"), _FakeEl("1/1"), _FakeEl("12345"),
            _FakeEl("提交失败：当前时间不允许委托"),
        ]
        result = self._extract(descendants)
        assert "提交失败：当前时间不允许委托" in result
        assert "00:00:22" not in result
        assert "1/1" not in result
        assert "12345" not in result


class TestUiBaselineSelfLearning:
    """UI 标签自学习黑名单测试（安静态快照 → 兜底提取动态过滤）

    place_order 开始时（重置后/干净退出后、无弹窗）对主窗口全部可见
    文本拍快照，兜底全局扫描时用作动态黑名单：券商升级新增的界面标签
    只要被快照收录即被过滤，错误分类不再依赖人工补硬编码清单。
    """

    def test_new_label_filtered_via_baseline(self):
        """快照收录的新标签（券商升级新增，不在硬编码清单）被过滤"""
        from src.core.trader import Trader
        baseline = Trader._snapshot_ui_texts(
            [_FakeEl("智能盯盘"), _FakeEl("证券代码"), _FakeEl("买入数量")])
        descendants = [
            _FakeEl("智能盯盘"),  # 仅存在于快照（不在硬编码清单）
            _FakeEl("提交失败：可用资金不足"),
        ]
        result = Trader._extract_popup_error_text(
            descendants, ui_baseline=baseline)
        assert "提交失败：可用资金不足" in result
        assert "智能盯盘" not in result

    def test_static_floor_still_applies_without_baseline(self):
        """无快照（ui_baseline=None）→ 硬编码清单仍兜底（原行为不变）"""
        from src.core.trader import Trader
        descendants = [_FakeEl("证券代码"), _FakeEl("清算中")]
        result = Trader._extract_popup_error_text(descendants)
        assert "清算中" in result
        assert "证券代码" not in result

    def test_snapshot_collects_and_skips_empty(self):
        """快照收集非空文本，跳过空文本"""
        from src.core.trader import Trader
        snapshot = Trader._snapshot_ui_texts(
            [_FakeEl("撤买"), _FakeEl(""), _FakeEl("买入[F1]")])
        assert snapshot == {"撤买", "买入[F1]"}

    def test_baseline_plus_floor_combined(self):
        """快照与硬编码清单叠加过滤（新标签 + 老标签同时在场）"""
        from src.core.trader import Trader
        descendants = [
            _FakeEl("智能盯盘"),   # 仅快照（新标签）
            _FakeEl("证券代码"),   # 仅硬编码清单（老标签）
            _FakeEl("提交失败：清算中"),
        ]
        result = Trader._extract_popup_error_text(
            descendants, ui_baseline={"智能盯盘"})
        assert result == "提交失败：清算中"

    def test_popup_text_not_in_baseline_survives(self):
        """弹窗错误文本不在快照中（弹窗出现于快照之后）→ 正常保留"""
        from src.core.trader import Trader
        descendants = [_FakeEl("提交失败：事务处理机转发数据失败")]
        result = Trader._extract_popup_error_text(
            descendants, ui_baseline={"证券代码", "买入价格"})
        assert "事务处理机转发数据失败" in result


class TestOnscreenRatio:
    """窗口与工作区交集比例计算测试（窗口位置自愈）"""

    # 1920×1080 屏幕，任务栏 40px
    WORK = (0, 0, 1920, 1040)
    WIN = (560, 220, 1360, 820)  # 800×600 窗口

    def test_window_fully_inside(self):
        """窗口完全在工作区内 → 1.0"""
        from src.services.window_service import WindowService
        assert WindowService._onscreen_ratio(self.WIN, self.WORK) == 1.0

    def test_window_fully_outside(self):
        """窗口完全出屏（在工作区右侧外）→ 0.0"""
        from src.services.window_service import WindowService
        rect = (2000, 300, 2800, 900)
        assert WindowService._onscreen_ratio(rect, self.WORK) == 0.0

    def test_window_half_visible(self):
        """窗口一半可见 → 0.5"""
        from src.services.window_service import WindowService
        # 800 宽窗口，右 400px 出屏
        rect = (1520, 300, 2320, 900)
        assert WindowService._onscreen_ratio(rect, self.WORK) == 0.5

    def test_window_two_fifths_visible(self):
        """窗口 2/5 可见（用户今天遇到的场景）→ 0.4，低于阈值触发自愈"""
        from src.services.window_service import WindowService
        # 800 宽窗口，左 320px 可见（320/800 = 0.4）
        rect = (1600, 300, 2400, 900)
        assert WindowService._onscreen_ratio(rect, self.WORK) == 0.4
        assert 0.4 < WindowService.ONSCREEN_MIN_RATIO

    def test_window_partially_off_top(self):
        """窗口顶部出屏 → 比例按交集面积算"""
        from src.services.window_service import WindowService
        # 600 高窗口，顶部 200px 出屏 → 400/600 ≈ 0.667
        rect = (560, -200, 1360, 400)
        ratio = WindowService._onscreen_ratio(rect, self.WORK)
        assert 0.66 <= ratio <= 0.67

    def test_ensure_onscreen_moves_window_back(self, mocker):
        """窗口出屏时 ensure_window_onscreen 调用 SetWindowPos 移回"""
        from src.services.window_service import WindowService
        ws = WindowService()
        ws.logger = mocker.MagicMock()

        mocker.patch("win32api.MonitorFromWindow", return_value=1)
        mocker.patch("win32api.GetMonitorInfo", return_value={"Work": (0, 0, 1920, 1040)})
        mocker.patch("win32gui.GetWindowRect", return_value=(2000, 300, 2800, 900))
        mock_setpos = mocker.patch("win32gui.SetWindowPos")

        assert ws.ensure_window_onscreen(12345) is True
        mock_setpos.assert_called_once()
        # SetWindowPos(hwnd, hWndInsertAfter, x, y, cx, cy, flags)
        args = mock_setpos.call_args[0]
        assert args[0] == 12345
        # 800×600 窗口 → x = (1920-800)//2 = 560, y = (1040-600)//2 = 220
        assert args[2] == 560
        assert args[3] == 220
        ws.logger.warning.assert_called_once()

    def test_ensure_onscreen_no_move_when_inside(self, mocker):
        """窗口在屏内时不移位（不调用 SetWindowPos）"""
        from src.services.window_service import WindowService
        ws = WindowService()
        ws.logger = mocker.MagicMock()

        mocker.patch("win32api.MonitorFromWindow", return_value=1)
        mocker.patch("win32api.GetMonitorInfo", return_value={"Work": (0, 0, 1920, 1040)})
        mocker.patch("win32gui.GetWindowRect", return_value=(560, 220, 1360, 820))
        mock_setpos = mocker.patch("win32gui.SetWindowPos")

        assert ws.ensure_window_onscreen(12345) is True
        mock_setpos.assert_not_called()


class TestTableColumnDetection:
    """查询表特征列检测测试（#修复：跨页复制错表的防御）"""

    def test_position_table_detected(self):
        """持仓表（成本价+股票余额列）→ True"""
        rows = [{"证券代码": "000001", "成本价": "11.091", "股票余额": "2100"}]
        assert PositionService._is_table_matching(
            rows, PositionService.POSITION_TABLE_COLUMNS) is True

    def test_trades_table_detected(self):
        """当日成交表（成交时间+成交编号列）→ True"""
        rows = [{"成交时间": "09:30:00", "成交编号": "123", "证券代码": "000001"}]
        assert PositionService._is_table_matching(
            rows, PositionService.TRADES_TABLE_COLUMNS) is True

    def test_orders_table_detected(self):
        """当日委托表（实测表头：委托价格/委托数量/合同编号）→ True"""
        rows = [{"委托时间": "09:30", "委托价格": "11.63", "委托数量": "100",
                 "合同编号": "1001", "证券代码": "000001", "操作": "买入"}]
        assert PositionService._is_table_matching(
            rows, PositionService.ORDERS_TABLE_COLUMNS) is True

    def test_orders_table_rejected_when_no_header(self):
        """持仓表（无委托价格/委托数量）→ False（不误判为委托表）"""
        rows = [{"证券代码": "000001", "成本价": "11.091", "股票余额": "2100"}]
        assert PositionService._is_table_matching(
            rows, PositionService.ORDERS_TABLE_COLUMNS) is False

    def test_orders_table_rejected_for_trades(self):
        """成交表（有合同编号但无委托价格/委托数量）→ False"""
        rows = [{"成交时间": "09:30", "成交均价": "11.60", "成交数量": "100",
                 "合同编号": "1001"}]
        assert PositionService._is_table_matching(
            rows, PositionService.ORDERS_TABLE_COLUMNS) is False

    def test_position_table_rejected_as_trades(self):
        """持仓表不是成交表（特征列不匹配）→ False"""
        rows = [{"证券代码": "000001", "成本价": "11.091", "股票余额": "2100"}]
        assert PositionService._is_table_matching(
            rows, PositionService.TRADES_TABLE_COLUMNS) is False

    def test_empty_table_assumed_valid(self):
        """空表无法判断 → True（不阻断流程）"""
        assert PositionService._is_table_matching(
            [], PositionService.POSITION_TABLE_COLUMNS) is True


class TestCaptchaImageValidation:
    """验证码截图启发式校验测试

    真实验证码: 92×38 白底蓝字。启发式拦截:
    主窗口截图（尺寸大）、隐藏控件未渲染（全白）、截到边缘（暗像素
    集中在角落）、大文件（>5KB）。
    """

    @staticmethod
    def _make_png(tmp_path, name, size, dark_region=None):
        """生成测试图片：白底 + 可选暗色区域"""
        from PIL import Image
        import numpy as np

        w, h = size
        arr = np.full((h, w), 255, dtype=np.uint8)
        if dark_region:
            x0, x1 = dark_region
            arr[:, x0:x1] = 100  # 暗像素（模拟数字笔画）
        p = tmp_path / name
        Image.fromarray(arr).save(p)
        return str(p)

    def test_valid_captcha(self, tmp_path):
        """92×38 白底 + 中间数字笔画 → 有效"""
        p = self._make_png(tmp_path, "valid.png", (92, 38), dark_region=(30, 62))
        assert PositionService._is_captcha_image_valid(p) is True

    def test_full_white_hidden_control(self, tmp_path):
        """92×38 全白（隐藏控件未渲染）→ 异常"""
        p = self._make_png(tmp_path, "white.png", (92, 38))
        assert PositionService._is_captcha_image_valid(p) is False

    def test_dark_pixels_at_edge(self, tmp_path):
        """暗像素集中在左缘（截到弹窗边缘）→ 异常"""
        p = self._make_png(tmp_path, "edge.png", (92, 38), dark_region=(0, 8))
        assert PositionService._is_captcha_image_valid(p) is False

    def test_main_window_screenshot(self, tmp_path):
        """主窗口截图尺寸（800×600）→ 异常"""
        p = self._make_png(tmp_path, "main.png", (800, 600), dark_region=(300, 500))
        assert PositionService._is_captcha_image_valid(p) is False

    def test_too_much_dark_area(self, tmp_path):
        """暗色面积过大（白底占比 <50%，如深色背景图）→ 异常"""
        p = self._make_png(tmp_path, "dark.png", (92, 38), dark_region=(0, 80))
        assert PositionService._is_captcha_image_valid(p) is False

    def test_missing_file_assumed_valid(self):
        """文件不存在无法判断 → 假定有效（保持原行为，不阻断流程）"""
        assert PositionService._is_captcha_image_valid("nonexistent.png") is True


class TestWindowServiceSingleton:
    """WindowService 单例测试（#1：跨请求共享句柄缓存的前提）"""

    def test_same_instance(self):
        """多次构造返回同一实例（路由层每请求 WindowService() 不再各自为政）"""
        from src.services.window_service import WindowService
        ws1 = WindowService()
        ws2 = WindowService()
        assert ws1 is ws2

    def test_shared_hwnd_cache(self):
        """一处更新句柄缓存，另一处引用可见（缓存真正跨请求生效）"""
        from src.services.window_service import WindowService
        ws1 = WindowService()
        ws2 = WindowService()
        ws1._cached_hwnd = 123456
        try:
            assert ws2._cached_hwnd == 123456
        finally:
            ws1._cached_hwnd = None


class TestIsValidTableData:
    """剪贴板表格数据校验测试（#2 兜底读取的判定依据）"""

    def setup_method(self):
        self.service = PositionService.__new__(PositionService)

    def test_valid_table(self):
        assert self.service._is_valid_table_data("代码\t名称\n000001\t平安银行") is True

    def test_header_only(self):
        """仅表头无数据行（当日无数据）→ 有效"""
        assert self.service._is_valid_table_data("代码\t名称\t数量") is True

    def test_empty(self):
        assert self.service._is_valid_table_data("") is False
        assert self.service._is_valid_table_data(None) is False

    def test_no_tab_in_header(self):
        assert self.service._is_valid_table_data("单列内容") is False

    def test_data_row_missing_tab(self):
        """数据行缺制表符（复制不完整）→ 无效"""
        assert self.service._is_valid_table_data("代码\t名称\r\n000001") is False


class TestIdempotencyRecordRetention:
    """幂等记录保留判定测试（#7：任务可能仍在执行时不清除，防重复下单）"""

    def _checker(self):
        from src.api.idempotency import IdempotencyChecker
        chk = IdempotencyChecker.__new__(IdempotencyChecker)
        chk._records = {}
        import threading
        chk._records_lock = threading.Lock()

        class _Cfg:
            def get_idempotency_config(self):
                return {"order_dedup_window_seconds": 60}

        class _Logger:
            def info(self, msg):
                pass

            def warning(self, msg):
                pass
        chk.config = _Cfg()
        chk.logger = _Logger()
        return chk

    def test_keep_on_task_timeout(self):
        """看门狗超时：任务可能仍在执行 → 保留"""
        from src.api.idempotency import should_keep_record_on_error
        from src.exceptions import TaskTimeoutError
        e = TaskTimeoutError("place_order", {}, elapsed=30.0)
        assert should_keep_record_on_error(e) is True

    def test_keep_on_queue_timeout(self):
        """队列超时：任务仍在队列稍后会执行 → 保留"""
        from src.api.idempotency import should_keep_record_on_error
        from src.exceptions import ApiError, ErrorCode
        e = ApiError(ErrorCode.QUEUE_TIMEOUT, "任务排队或执行超时")
        assert should_keep_record_on_error(e) is True

    def test_clear_on_business_error(self):
        """业务失败（任务确定未执行）→ 清除以便重试"""
        from src.api.idempotency import should_keep_record_on_error
        from src.exceptions import ApiError, ErrorCode
        for code in (ErrorCode.QUEUE_FULL, ErrorCode.MODE_SWITCH_FAILED,
                     ErrorCode.VALIDATION_ERROR, ErrorCode.OCR_FAILED):
            e = ApiError(code, "x")
            assert should_keep_record_on_error(e) is False, code

    def test_clear_on_unknown_error(self):
        from src.api.idempotency import should_keep_record_on_error
        assert should_keep_record_on_error(ValueError("x")) is False

    def test_duplicate_rejected_then_cleared_allows_retry(self):
        """记录→重复被拒→清除→可重新记录（失败重试路径）"""
        from src.exceptions import ApiError, ErrorCode
        chk = self._checker()
        chk.check_and_record("601991", "1", "100", "10.50", "limit")
        with pytest.raises(ApiError) as exc_info:
            chk.check_and_record("601991", "1", "100", "10.50", "limit")
        assert exc_info.value.error_code == ErrorCode.DUPLICATE_ORDER
        # 不同参数不受影响
        chk.check_and_record("601991", "2", "100", None, "market")
        # 清除后同参数可再次记录
        assert chk.clear_record("601991", "1", "100", "10.50", "limit") is True
        chk.check_and_record("601991", "1", "100", "10.50", "limit")

    def test_expired_window_allows_retry(self):
        """超过去重窗口后允许再次下单"""
        chk = self._checker()
        chk._records["601991_1_100__limit"] = 0  # 1970 年 → 必然过期
        chk.check_and_record("601991", "1", "100", "10.50", "limit")

    def test_client_key_dedup_independent_of_params(self):
        """客户端幂等键：同 key 不同参数也去重；不同 key 同参数不互撞"""
        from src.exceptions import ApiError, ErrorCode
        chk = self._checker()

        chk.check_and_record("601991", "1", "100", "10.50", "limit", idem_key="retry-abc")
        # 同 key 不同参数 → 仍拒绝（键优先于参数指纹）
        with pytest.raises(ApiError) as exc_info:
            chk.check_and_record("600000", "2", "200", None, "market", idem_key="retry-abc")
        assert exc_info.value.error_code == ErrorCode.DUPLICATE_ORDER

        # 不同 key 同参数 → 不互撞（参数指纹模式的痛点）
        chk.check_and_record("601991", "1", "100", "10.50", "limit", idem_key="strategy-b")

    def test_client_key_clear_uses_same_key(self):
        """失败清除必须走同一把客户端键，重试才能通过"""
        chk = self._checker()
        chk.check_and_record("601991", "1", "100", "10.50", "limit", idem_key="retry-xyz")
        # 按参数指纹清除（旧调用方式）→ 清不掉客户端键记录
        assert chk.clear_record("601991", "1", "100", "10.50", "limit") is False
        with pytest.raises(Exception):
            chk.check_and_record("601991", "1", "100", "10.50", "limit", idem_key="retry-xyz")
        # 用同一把键清除 → 重试放行
        assert chk.clear_record("601991", "1", "100", "10.50", "limit",
                                idem_key="retry-xyz") is True
        chk.check_and_record("601991", "1", "100", "10.50", "limit", idem_key="retry-xyz")


class TestDiagnosticSnapshotConcurrency:
    """诊断快照并发保护测试：响应携带 worker_busy，忙时告警不 500"""

    @staticmethod
    def _make_client(monkeypatch, tmp_path):
        import json

        from src.models import config as config_module

        p = tmp_path / "app_config.json"
        p.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()

        from src.api.routes import create_app
        app = create_app()
        app.config["TESTING"] = True

        # 快照不真正截图/UIA 遍历（测试环境无交易窗口）
        from src.utils.diagnostic import DiagnosticUtil
        monkeypatch.setattr(
            DiagnosticUtil, "snapshot",
            lambda self, prefix, window=None: {
                "screenshot": None, "ui_text": "fake",
                "ocr_text": "", "ocr_failed": True})

        return app.test_client()

    @staticmethod
    def _set_worker_status(monkeypatch, current_task):
        from src.api.task_queue import TaskQueue
        monkeypatch.setattr(
            TaskQueue, "get_status",
            lambda self: {
                "queue_size": 0, "max_size": 50, "worker_alive": True,
                "current_task": current_task,
                "current_task_duration": 1.0 if current_task else None,
                "is_zombie": False})

    def test_idle_worker_snapshot_not_busy(self, monkeypatch, tmp_path):
        """worker 空闲 → 快照正常返回，worker_busy=False"""
        client = self._make_client(monkeypatch, tmp_path)
        self._set_worker_status(monkeypatch, None)

        r = client.get("/diagnostic/snapshot")
        body = r.get_json()
        assert body["status"] == "success"
        assert body["data"]["worker_busy"] is False
        assert body["data"]["current_task"] is None

    def test_busy_worker_snapshot_marks_concurrent(self, monkeypatch, tmp_path):
        """worker 忙 → 快照仍返回（诊断不被卡死任务阻塞），但标记 worker_busy"""
        client = self._make_client(monkeypatch, tmp_path)
        self._set_worker_status(monkeypatch, "place_order")

        r = client.get("/diagnostic/snapshot")
        body = r.get_json()
        assert body["status"] == "success", "快照必须不被繁忙 worker 阻塞为错误"
        assert body["data"]["worker_busy"] is True
        assert body["data"]["current_task"] == "place_order"


class TestIdempotencyKeyRoute:
    """下单路由 Idempotency-Key 请求头集成测试（Flask test client）"""

    @staticmethod
    def _make_client(monkeypatch, tmp_path):
        import json

        from src.models import config as config_module

        p = tmp_path / "app_config.json"
        p.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()

        from src.api.idempotency import IdempotencyChecker
        IdempotencyChecker._reset_instance()

        from src.api.routes import create_app
        app = create_app()
        app.config["TESTING"] = True
        return app.test_client()

    @staticmethod
    def _ok_submit(monkeypatch, results):
        """按调用顺序返回预设结果，记录每次 task_name"""
        from src.api.task_queue import TaskQueue
        calls = []

        def _submit(self, func, task_name, params, timeout=None):
            calls.append(task_name)
            return results[len([c for c in calls if c == task_name]) - 1]

        monkeypatch.setattr(TaskQueue, "submit", _submit)
        return calls

    def test_same_key_twice_rejected(self, monkeypatch, tmp_path):
        """同 Idempotency-Key 两次请求（参数不同）→ 第二次 DUPLICATE_ORDER"""
        self._ok_submit(monkeypatch, [{"confirmed": True}])
        client = self._make_client(monkeypatch, tmp_path)

        r1 = client.post("/orders", json={"code": "601991", "status": "1", "amount": "100"},
                         headers={"Idempotency-Key": "op-1"})
        assert r1.get_json()["status"] == "success"

        r2 = client.post("/orders", json={"code": "600000", "status": "2", "amount": "200"},
                         headers={"Idempotency-Key": "op-1"})
        body = r2.get_json()
        assert body["status"] == "error"
        assert body["error_code"] == "DUPLICATE_ORDER"

    def test_failure_clears_client_key_allowing_retry(self, monkeypatch, tmp_path):
        """带 key 的下单失败 → 记录按 key 清除，同 key 重试放行"""
        from src.api.idempotency import IdempotencyChecker
        from src.exceptions import ApiError, ErrorCode

        client = self._make_client(monkeypatch, tmp_path)
        from src.api.task_queue import TaskQueue

        def _fail(self, *args, **kwargs):
            raise ApiError(ErrorCode.PRICE_OUT_OF_RANGE, "价格超限", suggestion="调整")

        monkeypatch.setattr(TaskQueue, "submit", _fail)

        r1 = client.post("/orders", json={"code": "601991", "status": "1", "amount": "100"},
                         headers={"Idempotency-Key": "op-9"})
        assert r1.get_json()["error_code"] == "PRICE_OUT_OF_RANGE"
        assert IdempotencyChecker.get_instance()._records == {}

        # 重试成功路径
        self._ok_submit(monkeypatch, [{"confirmed": True}])
        r2 = client.post("/orders", json={"code": "601991", "status": "1", "amount": "100"},
                         headers={"Idempotency-Key": "op-9"})
        assert r2.get_json()["status"] == "success"

    def test_oversized_key_rejected(self, monkeypatch, tmp_path):
        """超长 Idempotency-Key → VALIDATION_ERROR"""
        self._ok_submit(monkeypatch, [{"confirmed": True}])
        client = self._make_client(monkeypatch, tmp_path)
        r = client.post("/orders", json={"code": "601991", "status": "1"},
                        headers={"Idempotency-Key": "k" * 129})
        assert r.get_json()["error_code"] == "VALIDATION_ERROR"

    def test_no_header_falls_back_to_param_fingerprint(self, monkeypatch, tmp_path):
        """不带头时行为不变：相同参数 60s 内仍被参数指纹拦截"""
        self._ok_submit(monkeypatch, [{"confirmed": True}])
        client = self._make_client(monkeypatch, tmp_path)
        payload = {"code": "601991", "status": "1", "amount": "100"}
        assert client.post("/orders", json=payload).get_json()["status"] == "success"
        r2 = client.post("/orders", json=payload)
        assert r2.get_json()["error_code"] == "DUPLICATE_ORDER"


class TestEntrustNoVerification:
    """entrust_no 自动对账测试（order.verify_entrust_no，链式入队查询）"""

    @staticmethod
    def _make_client(monkeypatch, tmp_path, verify=True):
        import json

        from src.models import config as config_module

        cfg = {"order": {"capture_entrust_no": True, "verify_entrust_no": verify}}
        p = tmp_path / "app_config.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()

        from src.api.idempotency import IdempotencyChecker
        IdempotencyChecker._reset_instance()

        from src.api.routes import create_app
        app = create_app()
        app.config["TESTING"] = True
        return app.test_client()

    @staticmethod
    def _stub_submit(monkeypatch, handler):
        """handler(task_name, params) → 返回值 / 抛异常"""
        from src.api.task_queue import TaskQueue
        monkeypatch.setattr(TaskQueue, "submit",
                            lambda self, func, task_name, params, timeout=None:
                            handler(task_name, params))

    def test_verified_hit(self, monkeypatch, tmp_path):
        """下单拿到委托号 → 查当日委托命中 → entrust_no_verified=True"""
        def _handler(task_name, params):
            if task_name == "place_order":
                return {"confirmed": True, "entrust_no": "6246860043"}
            assert params["verify_entrust_no"] == "6246860043"
            return [{"合同编号": "6246860043", "证券代码": "601991"}]

        self._stub_submit(monkeypatch, _handler)
        client = self._make_client(monkeypatch, tmp_path)
        r = client.post("/orders", json={"code": "601991", "status": "1", "amount": "100"})
        body = r.get_json()
        assert body["status"] == "success"
        assert body["data"]["confirmed"] is True
        assert body["data"]["entrust_no_verified"] is True

    def test_verified_miss(self, monkeypatch, tmp_path):
        """落表号不含横幅号 → entrust_no_verified=False（下单仍 success）"""
        def _handler(task_name, params):
            if task_name == "place_order":
                return {"confirmed": True, "entrust_no": "6246860043"}
            return [{"合同编号": "999", "证券代码": "601991"}]

        self._stub_submit(monkeypatch, _handler)
        client = self._make_client(monkeypatch, tmp_path)
        r = client.post("/orders", json={"code": "601991", "status": "1", "amount": "100"})
        body = r.get_json()
        assert body["status"] == "success", "对账未命中不改变下单成功语义"
        assert body["data"]["entrust_no_verified"] is False

    def test_verify_query_failure_returns_none(self, monkeypatch, tmp_path):
        """对账查询失败 → verified=None，不影响下单成功响应"""
        from src.exceptions import ApiError, ErrorCode

        def _handler(task_name, params):
            if task_name == "get_today_orders":
                raise ApiError(ErrorCode.OCR_FAILED, "验证码识别失败", suggestion="重试")
            return {"confirmed": True, "entrust_no": "6246860043"}

        self._stub_submit(monkeypatch, _handler)
        client = self._make_client(monkeypatch, tmp_path)
        r = client.post("/orders", json={"code": "601991", "status": "1", "amount": "100"})
        body = r.get_json()
        assert body["status"] == "success"
        assert body["data"]["entrust_no_verified"] is None

    def test_disabled_or_no_entrust_no_skips_query(self, monkeypatch, tmp_path):
        """未开启对账 / 未拿到委托号 → 不追加查询任务"""
        submitted = []

        def _handler(task_name, params):
            submitted.append(task_name)
            return {"confirmed": True}  # 无 entrust_no

        self._stub_submit(monkeypatch, _handler)

        # 开启但无 entrust_no → 不查询
        client = self._make_client(monkeypatch, tmp_path, verify=True)
        client.post("/orders", json={"code": "601991", "status": "1", "amount": "100"})
        assert "get_today_orders" not in submitted

        # 关闭对账 → 不查询
        submitted.clear()
        client = self._make_client(monkeypatch, tmp_path, verify=False)
        client.post("/orders", json={"code": "601991", "status": "1", "amount": "100"})
        assert "get_today_orders" not in submitted


class TestCrossSiteRejection:
    """跨站防御测试（#12：带 Origin 头的浏览器请求一律拒绝，防恶意网页触发交易）"""

    @staticmethod
    def _make_client(monkeypatch, tmp_path, auth_enabled=False, token=""):
        import json

        from src.models import config as config_module

        cfg = {"auth": {"enabled": auth_enabled, "token": token}}
        p = tmp_path / "app_config.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()

        from src.api.routes import create_app
        app = create_app()
        app.config["TESTING"] = True
        return app.test_client()

    def test_origin_header_rejected_even_without_auth(self, monkeypatch, tmp_path):
        """未开认证也拒绝带 Origin 的请求（恶意网页无法触发下单）"""
        client = self._make_client(monkeypatch, tmp_path)
        r = client.get("/queue/status", headers={"Origin": "http://evil.example"})
        body = r.get_json()
        assert body["status"] == "error"
        assert body["error_code"] == "AUTH_FAILED"

    def test_no_origin_allowed(self, monkeypatch, tmp_path):
        """脚本客户端（无 Origin 头）不受影响"""
        client = self._make_client(monkeypatch, tmp_path)
        r = client.get("/queue/status")
        assert r.get_json()["status"] == "success"

    def test_health_public_with_origin(self, monkeypatch, tmp_path):
        """/health 探活不受 Origin 防御影响"""
        client = self._make_client(monkeypatch, tmp_path)
        r = client.get("/health", headers={"Origin": "http://evil.example"})
        assert r.get_json()["status"] == "success"


class TestSendKeyBackgroundVerification:
    """send_key(background=True) 前台自校验测试

    锚定 2026-09-22 实测回归：background=True 原先盲目信任调用方，
    窗口被切走时 F4/F3/F1 经 keybd_event 发进错误前台窗口——F4 发空后
    查询页不对，树节点点击未注册，导航多花 ~1.5s。现自校验前台：
    匹配零开销直发，不匹配自动补激活。
    """

    @staticmethod
    def _make_service(monkeypatch, window_handle, foreground_handle):
        import win32gui

        from src.services.window_service import WindowService

        ws = WindowService()
        calls = []

        class _Win:
            handle = window_handle

        monkeypatch.setattr(win32gui, "GetForegroundWindow",
                            lambda: foreground_handle)
        monkeypatch.setattr(ws, "get_trading_window", lambda: _Win() if window_handle else None)
        monkeypatch.setattr(ws, "_send_key_foreground",
                            lambda keys: calls.append(("send", keys)))
        monkeypatch.setattr(ws, "_activate_window_before_keybd",
                            lambda keys: calls.append(("activate", keys)))
        return ws, calls

    def test_foreground_match_sends_directly(self, monkeypatch):
        """已在前台 → 零开销直发，不触发激活"""
        ws, calls = self._make_service(monkeypatch, 0x111, 0x111)
        ws.send_key("F4", background=True)
        assert calls == [("send", "F4")]

    def test_foreground_mismatch_activates_first(self, monkeypatch):
        """窗口被切走 → 先走完整激活路径再发，不盲发"""
        ws, calls = self._make_service(monkeypatch, 0x111, 0x222)
        ws.send_key("F4", background=True)
        assert calls == [("activate", "F4")]

    def test_window_missing_goes_through_activate_guard(self, monkeypatch):
        """窗口未找到 → 走激活路径（其内部有禁止发送防御），不盲发"""
        ws, calls = self._make_service(monkeypatch, None, 0x222)
        ws.send_key("F3", background=True)
        assert calls == [("activate", "F3")]


class TestHealthLoggedIn:
    """/health 登录态检测测试：主窗口存在 ≈ 已登录"""

    @staticmethod
    def _make_client(monkeypatch, tmp_path):
        import json

        from src.models import config as config_module

        p = tmp_path / "app_config.json"
        p.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()

        from src.api.routes import create_app
        app = create_app()
        app.config["TESTING"] = True
        return app.test_client()

    def test_logged_in_when_window_exists(self, monkeypatch, tmp_path):
        """主窗口存在 → logged_in=True"""
        import win32gui
        monkeypatch.setattr(win32gui, "FindWindow", lambda cls, title: 0x555)

        client = self._make_client(monkeypatch, tmp_path)
        body = client.get("/health").get_json()
        assert body["data"]["logged_in"] is True

    def test_not_logged_in_when_window_absent(self, monkeypatch, tmp_path):
        """进程在但主窗口不在（登录页/断线）→ logged_in=False"""
        import win32gui
        monkeypatch.setattr(win32gui, "FindWindow", lambda cls, title: 0)

        client = self._make_client(monkeypatch, tmp_path)
        body = client.get("/health").get_json()
        assert body["data"]["logged_in"] is False


class TestAlertWebhook:
    """告警 webhook 测试（无网络：同步线程捕获 payload / 校验门控）"""

    @staticmethod
    def _sync_threads(monkeypatch):
        """把 alert 模块的 Thread 替换为同步执行（start 即跑 target）"""
        from src.utils import alert as alert_mod

        class SyncThread:
            def __init__(self, target=None, args=None, kwargs=None,
                         daemon=None, name=None):
                self._target = target
                self._args = args or ()
                self._kwargs = kwargs or {}

            def start(self):
                self._target(*self._args, **self._kwargs)

        monkeypatch.setattr(alert_mod.threading, "Thread", SyncThread)

    @staticmethod
    def _with_config(monkeypatch, tmp_path, alerts):
        import json

        from src.models import config as config_module

        p = tmp_path / "app_config.json"
        p.write_text(json.dumps({"alerts": alerts}, ensure_ascii=False),
                     encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()

    def test_disabled_when_url_empty(self, monkeypatch, tmp_path):
        """webhook_url 为空（默认）→ 整体禁用，不发起任何请求"""
        from src.utils import alert as alert_mod
        self._with_config(monkeypatch, tmp_path, {"webhook_url": ""})

        def _boom(*a, **k):
            raise AssertionError("未配置 URL 不应发起请求")
        monkeypatch.setattr(alert_mod, "_deliver", _boom)
        self._sync_threads(monkeypatch)

        alert_mod.send_alert("task_timeout", "t", "m")  # 不应触发 _boom

    def test_generic_payload_fields(self, monkeypatch, tmp_path):
        """generic 格式 → 完整结构化 JSON（alert_type/title/details/timestamp）"""
        from src.utils import alert as alert_mod
        self._with_config(monkeypatch, tmp_path,
                          {"webhook_url": "http://127.0.0.1:9/hook",
                           "format": "generic"})
        captured = {}
        monkeypatch.setattr(
            alert_mod, "_deliver",
            lambda url, timeout, payload: captured.update(
                url=url, timeout=timeout, payload=payload))
        self._sync_threads(monkeypatch)

        alert_mod.send_alert("task_timeout", "任务超时", "正文",
                             {"task": "place_order"}, level="error")

        assert captured["url"] == "http://127.0.0.1:9/hook"
        body = captured["payload"]
        assert body["service"] == "xiadan-gateway"
        assert body["alert_type"] == "task_timeout"
        assert body["level"] == "error"
        assert body["title"] == "任务超时"
        assert body["details"] == {"task": "place_order"}
        assert body["timestamp"]

    def test_text_format_for_bots(self, monkeypatch, tmp_path):
        """text 格式 → 企业微信/钉钉机器人 {"msgtype":"text"} 结构"""
        from src.utils import alert as alert_mod
        self._with_config(monkeypatch, tmp_path,
                          {"webhook_url": "https://qyapi.weixin.qq.com/x",
                           "format": "text"})
        captured = {}
        monkeypatch.setattr(
            alert_mod, "_deliver",
            lambda url, timeout, payload: captured.update(payload=payload))
        self._sync_threads(monkeypatch)

        alert_mod.send_alert("consecutive_failures", "连续失败", "正文",
                             {"n": 3})
        body = captured["payload"]
        assert body["msgtype"] == "text"
        assert "[xiadan-gateway] 连续失败" in body["text"]["content"]
        assert "正文" in body["text"]["content"]

    def test_feishu_format(self, monkeypatch, tmp_path):
        """feishu 格式 → 飞书机器人 {"msg_type":"text","content":{...}} 结构
        （与企业微信的 msgtype/text 结构不同，不能混用）"""
        from src.utils import alert as alert_mod
        self._with_config(monkeypatch, tmp_path,
                          {"webhook_url": "https://open.larksuite.com/x",
                           "format": "feishu"})
        captured = {}
        monkeypatch.setattr(
            alert_mod, "_deliver",
            lambda url, timeout, payload: captured.update(payload=payload))
        self._sync_threads(monkeypatch)

        alert_mod.send_alert("task_timeout", "任务超时", "正文", {"a": 1})
        body = captured["payload"]
        assert body["msg_type"] == "text"
        assert "content" in body and "msgtype" not in body
        assert "[xiadan-gateway] 任务超时" in body["content"]["text"]

    def test_deliver_detects_bot_business_error(self, monkeypatch):
        """飞书/企微业务错误（HTTP 200 + code≠0）→ 识别为失败而非误报成功"""
        import urllib.request

        from src.utils import alert as alert_mod

        class _Resp:
            status = 200

            def read(self, n):
                return b'{"code": 19003, "msg": "signature mismatch"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())
        alert_mod._deliver("https://open.larksuite.com/x", 5,
                           {"msg_type": "text", "content": {"text": "x"}})
        # 不抛异常即通过；失败细节已记 warning 日志

    def test_unknown_format_not_sent(self, monkeypatch, tmp_path):
        """未知 format → 不发送（启动校验也会拦，此处兜底）"""
        from src.utils import alert as alert_mod
        self._with_config(monkeypatch, tmp_path,
                          {"webhook_url": "http://x/hook", "format": "xml"})

        def _boom(*a, **k):
            raise AssertionError("未知格式不应发送")
        monkeypatch.setattr(alert_mod, "_deliver", _boom)
        self._sync_threads(monkeypatch)
        alert_mod.send_alert("task_timeout", "t", "m")

    def test_deliver_never_raises(self, monkeypatch, tmp_path):
        """webhook 不可达/非 2xx → 只记日志，异常绝不外泄"""
        import urllib.request

        from src.utils import alert as alert_mod

        def _refuse(req, timeout=None):
            raise OSError("connection refused")
        monkeypatch.setattr(urllib.request, "urlopen", _refuse)
        alert_mod._deliver("http://127.0.0.1:9/hook", 1, {"alert_type": "x"})

        def _non2xx(req, timeout=None):
            import io

            class _Resp:
                status = 500

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return _Resp()
        monkeypatch.setattr(urllib.request, "urlopen", _non2xx)
        alert_mod._deliver("http://127.0.0.1:9/hook", 1, {"alert_type": "x"})

    def test_config_validation_rejects_bad_alerts(self, monkeypatch, tmp_path):
        """启动校验：非法 format / 非 http URL / 非正超时 → 报错"""
        import json

        from src.models import config as config_module

        cfg = {"alerts": {"webhook_url": "ftp://x", "format": "xml",
                          "timeout_seconds": 0}}
        p = tmp_path / "app_config.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()

        errors = config_module.AppConfig().validate()
        joined = "\n".join(errors)
        assert "alerts.webhook_url" in joined
        assert "alerts.format" in joined
        assert "alerts.timeout_seconds" in joined


class TestAlertCallSites:
    """告警调用点测试：连续失败 ≥3 与弹窗漂移触发 send_alert"""

    @staticmethod
    def _capture_alerts(monkeypatch):
        import src.api.task_queue as tq_mod
        captured = []
        monkeypatch.setattr(tq_mod, "send_alert",
                            lambda *a, **k: captured.append((a, k)))
        return captured

    def test_consecutive_failures_trigger_alert(self, monkeypatch):
        """连续失败第 3 次 → send_alert(consecutive_failures)"""
        from src.api.task_queue import TaskQueue
        from src.exceptions import ApiError, ErrorCode
        captured = self._capture_alerts(monkeypatch)
        tq = TaskQueue.get_instance()
        tq._recent_tasks.clear()
        tq._consecutive_failures = 0
        try:
            for _ in range(3):
                from src.api.task_queue import Task
                task = Task(lambda: None, "get_balance", {}, 30)
                task.error = ApiError(ErrorCode.WINDOW_NOT_FOUND, "窗口没了")
                tq._record_task_outcome(task)

            assert any(a[0] == "consecutive_failures" for a, k in captured)
        finally:
            tq._consecutive_failures = 0
            tq._recent_tasks.clear()

    def test_dialog_drift_triggers_alert(self, monkeypatch):
        """弹窗行为翻转 → send_alert(order_dialog_drift)"""
        from src.api.task_queue import Task, TaskQueue
        captured = self._capture_alerts(monkeypatch)
        tq = TaskQueue.get_instance()
        tq._order_dialog_stats.clear()
        tq._last_order_had_dialog = False
        try:
            task = Task(lambda: None, "place_order", {"status": "1"}, 30)
            task.window_state = {"had_dialog": True, "clean": False}
            tq._record_task_outcome(task)
            assert any(a[0] == "order_dialog_drift" for a, k in captured)
        finally:
            tq._order_dialog_stats.clear()
            tq._last_order_had_dialog = None


class TestInputVerification:
    """输入回读校验测试：防 type_keys 静默截断（2026-09-22 实测代码字段
    只留下 1-3 位且无任何报错——焦点被抢/自动补全重写导致）"""

    @staticmethod
    def _make_service(monkeypatch, element):
        """构造 WindowService 并把控件查找接到脚本化假元素上"""
        import win32gui

        from src.services.window_service import WindowService

        ws = WindowService()
        monkeypatch.setattr(ws, "find_element_in_window",
                            lambda *a, **k: element)
        monkeypatch.setattr(win32gui, "SendMessage",
                            lambda hwnd, msg, w, s: None)
        return ws

    @staticmethod
    def _make_fake_element(monkeypatch, typing_script):
        """脚本化假输入框：type_keys 按 script 决定落盘内容

        typing_script: [(匹配的 keys 前缀, 落盘文本), ...] 按序消费，
                       未命中时落盘空串（模拟清空）
        window_text 返回当前落盘内容
        """
        calls = []

        class FakeEl:
            handle = 0x1234

            def __init__(self):
                self.text = ""
                self._pending = list(typing_script)

            def set_focus(self):
                pass

            def type_keys(self, keys):
                calls.append(("type_keys", keys))
                for i, (prefix, result) in enumerate(self._pending):
                    if keys.startswith(prefix):
                        self.text = result
                        self._pending.pop(i)
                        return
                if "{BACKSPACE}" in keys or keys == "":
                    self.text = ""

            def window_text(self):
                calls.append(("window_text", self.text))
                return self.text

        return FakeEl(), calls

    def test_verify_numeric_tolerates_format_diff(self, monkeypatch):
        """numeric 模式：10.5 vs 10.50 视为一致；非数字/不一致为 False"""
        from src.services.window_service import WindowService

        class _El:
            def window_text(self):
                return " 10.5 "

        assert WindowService._verify_input_text(_El(), "10.50", "numeric") is True
        assert WindowService._verify_input_text(_El(), "11.00", "numeric") is False

        class _Bad:
            def window_text(self):
                return "abc"

        assert WindowService._verify_input_text(_Bad(), "10.50", "numeric") is False

        class _Boom:
            def window_text(self):
                raise RuntimeError("控件失效")

        assert WindowService._verify_input_text(_Boom(), "10.50", "exact") is False

    def test_truncated_input_retried_and_recovered(self, monkeypatch):
        """首次键入被截断（模拟焦点抢占）→ 自动清空重输 → 校验通过"""
        el, calls = self._make_fake_element(
            monkeypatch,
            [("601991", "601"), ("601991", "601991")])
        ws = self._make_service(monkeypatch, el)

        ws.input_text_to_element(None, 1032, "601991",
                                 descendants=[], verify="exact")
        type_text_calls = [c for c in calls if c == ("type_keys", "601991")]
        assert len(type_text_calls) == 2, "截断后应自动重输一次"

    def test_persistent_truncation_raises(self, monkeypatch):
        """两次键入均截断 → INPUT_VERIFY_FAILED，携带期望/实际内容"""
        import pytest

        from src.exceptions import ApiError, ErrorCode
        el, _calls = self._make_fake_element(
            monkeypatch,
            [("601991", "601"), ("601991", "60")])  # 两次都截断
        ws = self._make_service(monkeypatch, el)

        with pytest.raises(ApiError) as exc_info:
            ws.input_text_to_element(None, 1032, "601991",
                                     descendants=[], verify="exact")
        assert exc_info.value.error_code == ErrorCode.INPUT_VERIFY_FAILED
        assert exc_info.value.details["expected"] == "601991"
        assert exc_info.value.details["actual"] == "60"

    def test_clean_input_single_pass(self, monkeypatch):
        """输入正常 → 只键入一次，不触发重输"""
        el, calls = self._make_fake_element(
            monkeypatch,
            [("601991", "601991")])
        ws = self._make_service(monkeypatch, el)

        ws.input_text_to_element(None, 1032, "601991",
                                 descendants=[], verify="exact")
        assert calls.count(("type_keys", "601991")) == 1

    def test_no_verify_keeps_legacy_behavior(self, monkeypatch):
        """verify=None（默认）→ 不做回读，行为与旧版一致"""
        el, calls = self._make_fake_element(
            monkeypatch,
            [("100", "1")])  # 截断也不管
        ws = self._make_service(monkeypatch, el)

        ws.input_text_to_element(None, 1034, "100", descendants=[])
        assert calls.count(("type_keys", "100")) == 1  # 无重试


class TestWindowSetupSkipAccessors:
    """TaskQueue 跳过状态公共访问器测试（收口私有属性直接读写）"""

    def test_consume_is_single_shot(self):
        """consume_window_setup_skip 消费一次即复位"""
        from src.api.task_queue import TaskQueue
        tq = TaskQueue.get_instance()
        tq._skip_window_setup = True
        try:
            assert tq.consume_window_setup_skip() is True
            assert tq.consume_window_setup_skip() is False
        finally:
            tq._skip_window_setup = False

    def test_get_last_task_info_returns_copy(self):
        """get_last_task_info 返回副本，外部修改不影响内部状态；无记录返回空"""
        from src.api.task_queue import TaskQueue
        tq = TaskQueue.get_instance()
        tq._last_task_info = None
        try:
            assert tq.get_last_task_info() == {}

            tq._last_task_info = {"name": "place_order", "group": "trade",
                                  "had_dialog": False, "status": "1"}
            snapshot = tq.get_last_task_info()
            snapshot["status"] = "MUTATED"
            assert tq.get_last_task_info()["status"] == "1"
        finally:
            tq._last_task_info = None


class TestOrderDialogDrift:
    """下单确认弹窗行为跟踪测试：/health 统计 + 快速交易设置漂移告警"""

    @staticmethod
    def _outcome(tq, had_dialog, task_name="place_order", error=None):
        from src.api.task_queue import Task
        task = Task(lambda: None, task_name, {"status": "1"}, 30)
        task.error = error
        task.window_state = None if had_dialog is None else {
            "had_dialog": had_dialog, "clean": False}
        tq._record_task_outcome(task)

    def test_stats_expose_order_dialog_counts(self):
        """get_stats 暴露 order_confirm_dialog 统计（弹窗/无弹窗计数）"""
        from src.api.task_queue import TaskQueue
        tq = TaskQueue.get_instance()
        tq._recent_tasks.clear()
        tq._order_dialog_stats.clear()
        tq._last_order_had_dialog = None
        try:
            self._outcome(tq, False)
            self._outcome(tq, False)
            self._outcome(tq, True)

            stats = tq.get_stats()
            dlg = stats["order_confirm_dialog"]
            assert dlg["total_orders"] == 3
            assert dlg["no_dialog_fast_trade"] == 2
            assert dlg["with_confirm_dialog"] == 1
            assert dlg["last_order_had_dialog"] is True
        finally:
            tq._order_dialog_stats.clear()
            tq._last_order_had_dialog = None

    def test_non_order_tasks_not_counted(self):
        """查询/撤单任务不计入弹窗统计；无窗口状态的任务（僵尸）也跳过"""
        from src.api.task_queue import TaskQueue
        tq = TaskQueue.get_instance()
        tq._recent_tasks.clear()
        tq._order_dialog_stats.clear()
        tq._last_order_had_dialog = None
        try:
            self._outcome(tq, False, task_name="get_balance")
            self._outcome(tq, None)  # window_state=None（超时僵尸任务）
            stats = tq.get_stats()
            assert stats["order_confirm_dialog"]["total_orders"] == 0
            assert tq._last_order_had_dialog is None
        finally:
            tq._order_dialog_stats.clear()
            tq._last_order_had_dialog = None

    def test_drift_detection_flips_state(self):
        """弹窗行为翻转 → _last_order_had_dialog 更新（告警日志路径）"""
        from src.api.task_queue import TaskQueue
        tq = TaskQueue.get_instance()
        tq._recent_tasks.clear()
        tq._order_dialog_stats.clear()
        tq._last_order_had_dialog = None
        try:
            self._outcome(tq, False)   # 快速交易
            assert tq._last_order_had_dialog is False
            self._outcome(tq, True)    # 突然出现弹窗 → 漂移
            assert tq._last_order_had_dialog is True
        finally:
            tq._order_dialog_stats.clear()
            tq._last_order_had_dialog = None


class TestCancelValidation:
    """撤单参数校验测试

    锚定 2026-09-22 发现的回归：trading_service.py 使用 ApiError/ErrorCode
    但从未 import——非法 type / 窗口丢失时服务层抛 NameError，被路由
    except 兜成 INTERNAL_ERROR + traceback，错误语义丢失。
    现校验前置到路由层，且服务层 import 补齐。
    """

    @staticmethod
    def _make_client(monkeypatch, tmp_path):
        import json

        from src.models import config as config_module

        p = tmp_path / "app_config.json"
        p.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_PATH", str(p))
        config_module.AppConfig._reset_instance()

        from src.api.routes import create_app
        app = create_app()
        app.config["TESTING"] = True
        return app.test_client()

    @staticmethod
    def _forbid_submit(monkeypatch):
        """让 TaskQueue.submit 被调用即失败（校验必须发生在入队之前）"""
        from src.api.task_queue import TaskQueue

        def _no(self, *args, **kwargs):
            raise AssertionError("参数校验失败的任务不应入队")

        monkeypatch.setattr(TaskQueue, "submit", _no)

    @pytest.mark.parametrize("bad_type", ["Q", "B", "买", "  "])
    def test_invalid_cancel_type_rejected_before_queue(self, monkeypatch, tmp_path, bad_type):
        """非法撤单类型 → VALIDATION_ERROR，且任务不入队（空串视为默认 A，不在此列）"""
        client = self._make_client(monkeypatch, tmp_path)
        self._forbid_submit(monkeypatch)

        r = client.post("/orders/cancel-all", json={"type": bad_type})

        body = r.get_json()
        assert body["status"] == "error"
        assert body["error_code"] == "VALIDATION_ERROR"

    def test_lowercase_type_normalized(self, monkeypatch, tmp_path):
        """小写类型自动大写（'x' 视为 'X'），正常入队不报校验错"""
        client = self._make_client(monkeypatch, tmp_path)

        from src.api.task_queue import TaskQueue

        def _fake_submit(self, func, task_name, params, timeout=None):
            assert params["type"] == "X"
            return {"cancel_type": "撤买", "success": True}

        monkeypatch.setattr(TaskQueue, "submit", _fake_submit)
        r = client.post("/orders/cancel-all", json={"type": "x"})
        assert r.get_json()["status"] == "success"

    def test_service_raises_structured_error_not_nameerror(self):
        """服务层非法类型 → 结构化 ApiError(VALIDATION_ERROR)，不再 NameError

        直接实例化（跳过 __init__ 的 WindowService 依赖）：校验分支在
        触碰任何窗口服务之前就应抛出。
        """
        from src.exceptions import ApiError, ErrorCode
        from src.services.trading_service import TradingService

        service = TradingService.__new__(TradingService)
        with pytest.raises(ApiError) as exc_info:
            service.cancel_all_orders("Q")
        assert exc_info.value.error_code == ErrorCode.VALIDATION_ERROR
