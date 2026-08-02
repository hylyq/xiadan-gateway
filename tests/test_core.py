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
        """当日委托表（委托编号列）→ True"""
        rows = [{"委托编号": "1001", "证券代码": "000001", "操作": "买入"}]
        assert PositionService._is_table_matching(
            rows, PositionService.ORDERS_TABLE_COLUMNS) is True

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
