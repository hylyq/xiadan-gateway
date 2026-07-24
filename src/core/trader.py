"""下单编排服务

完整下单流程:
1. 激活 xiadan.exe
2. F1/F2 切换买卖界面
3. 点击 control_id=1400 切换限价/市价（每次重新获取窗口避免缓存）
4. 填写代码(1032)、价格(1033, 仅限价)、数量(1034)
5. 点击下单按钮(1006)
6. 发送 Y 键确认委托（confirm=true 时）
"""
import time
from typing import Optional

from src.constants import (
    CONTROL_ID_CODE, CONTROL_ID_PRICE, CONTROL_ID_AMOUNT,
    CONTROL_ID_SUBMIT, CONTROL_ID_PRICE_TYPE,
    CONFIRM_DIALOG_TITLE_ID, CONFIRM_YES_BUTTON_ID,
    CONFIRM_NO_BUTTON_ID, CONFIRM_DETAIL_TEXT_ID,
    CANCEL_CONFIRM_TEXT_ID, T1_RESTRICTION_KEYWORDS,
    SERVER_ERROR_POPUP_KEYWORDS
)
from src.core.validation import sanitize_price
from src.exceptions import ApiError, ErrorCode
from src.models.config import AppConfig
from src.services.window_service import WindowService
from src.utils.diagnostic import DiagnosticUtil
from src.utils.logger import Logger
from src.utils.poll import poll_until, timed, PollTimeoutError


class Trader:
    """下单编排器"""

    def __init__(self, window_service: WindowService):
        self.window_service = window_service
        self.config = AppConfig()
        self.logger = Logger.get_instance()
        self._cached_descendants = None  # _has_any_dialog 找到弹窗时缓存，供循环复用

    def place_order(
        self,
        code: str,
        status: str,
        amount: Optional[str] = None,
        price: Optional[str] = None,
        price_type: str = "limit",
        confirm: bool = True
    ) -> dict:
        """下单

        Args:
            code: 股票代码
            status: '1'=买入, '2'=卖出
            amount: 委托数量（可选）
            price: 委托价格（仅限价模式有效）
            price_type: 'limit'=限价, 'market'=市价
            confirm: 是否自动发送 Y 键确认委托

        Returns:
            {
                "action": "买入"/"卖出",
                "mode": "限价"/"市价",
                "code": "601991",
                "amount": "100",
                "price": "10.50" or None,
                "confirmed": True/False
            }
        """
        self.logger.info(
            f"开始下单: code={code}, status={status}, amount={amount}, "
            f"price={price}, price_type={price_type}, confirm={confirm}"
        )

        # 0. 价格格式校验（A 股限 2 位小数）
        if price_type == "limit" and price:
            sanitized_price = sanitize_price(price)
            if sanitized_price != price:
                self.logger.warning(
                    f"价格 {price} 已自动修正为 {sanitized_price}（A股限 2 位小数）"
                )
            price = sanitized_price

        # 1. 激活 xiadan.exe（下单需要前台激活，因为 type_keys() 和 click_input() 需要焦点）
        with timed("激活窗口", self.logger):
            trading_paths = self.config.get_trading_app_paths()
            if not trading_paths:
                raise Exception("未配置 xiadan.exe 路径，请检查 config/app_config.json")
            self.window_service.activate_window(trading_paths)
            time.sleep(0.2)

        # 2. F1/F2 切换买卖界面（步骤 1 已激活窗口，用 background 跳过冗余激活）
        with timed("F1/F2 切换买卖界面", self.logger):
            self.window_service.send_key(
                "F1" if status == "1" else "F2", background=True)
            time.sleep(0.1)

        # 3. 获取交易窗口 + 缓存 descendants（下单流程中 input/click 共用）
        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("未找到交易窗口 '网上股票交易系统5.0'")
        _descendants = list(window.descendants())

        # 4. 填写股票代码（必须先填代码，否则价格模式切换可能被禁用）
        with timed("填写股票代码", self.logger):
            self.window_service.input_text_to_element(
                window, CONTROL_ID_CODE, code, descendants=_descendants, delay=0.1)
            time.sleep(0.3)  # 等待券商自动填充价格完成

        # 4.1 防御：输入代码后券商自动查询价格，若服务器维护会弹出错误弹窗
        # （如 "事务处理机转发数据失败"、"Begin failed!" 等）
        with timed("代码输入后弹窗防御", self.logger):
            dismissed = self._dismiss_server_error_popup(window)
            if dismissed:
                # 弹窗关闭后重新获取窗口，因为弹窗可能改变了控件树
                window = self.window_service.get_trading_window()
                if window is None:
                    raise Exception("关闭弹窗后窗口消失")
                _descendants = list(window.descendants())

        # 5. 确保价格模式匹配（券商可能记住上次模式，输入代码后自动切换）
        with timed("切换价格模式", self.logger):
            want_market = (price_type == "market")
            # 刷新窗口 + descendants 一次性完成，同时用于 _is_market_mode 检测
            window = self.window_service.get_trading_window()
            if window is None:
                raise Exception("切换价格模式后窗口消失")
            _descendants = list(window.descendants())
            is_market = self._is_market_mode(window, descendants=_descendants)
            if want_market != is_market:
                is_market = self._switch_price_type(window, want_market)
                # 模式切换后重新刷新（控件树可能已变化）
                window = self.window_service.get_trading_window()
                if window is None:
                    raise Exception("切换价格模式后窗口消失")
                _descendants = list(window.descendants())

        # 6. 填写价格（仅限价，步骤 5 已确保处于限价模式）
        if price_type == "limit" and price:
            with timed("填写价格", self.logger):
                self.window_service.input_text_to_element(
                    window, CONTROL_ID_PRICE, price, descendants=_descendants, delay=0.05)
                time.sleep(0.05)

        # 7. 填写数量
        if amount:
            with timed("填写数量", self.logger):
                self.window_service.input_text_to_element(
                    window, CONTROL_ID_AMOUNT, amount, descendants=_descendants, delay=0.05)
                time.sleep(0.05)

        # 8. 点击下单按钮并处理弹窗
        with timed("点击下单按钮", self.logger):
            self.window_service.click_element(
                window, CONTROL_ID_SUBMIT, descendants=_descendants)
            self.logger.info("已点击下单按钮，等待弹窗")

        # 轮询等待弹窗出现（替代固定 sleep(0.5)），超时则走后续无弹窗逻辑。
        # 快速交易模式下（买入/卖出确认=否）不弹委托确认窗，仅可能弹警告窗。
        # 警告弹窗在点击下单按钮后立即出现，0.3s 足够检测；
        # 正常情况无弹窗则快速跳过，不再浪费等待时间。
        _dialog_detected = True
        with timed("等待下单弹窗", self.logger):
            try:
                poll_until(
                    lambda: self._has_any_dialog(),
                    timeout=0.3, interval=0.1,
                    description="下单后弹窗"
                )
            except PollTimeoutError:
                _dialog_detected = False

        # 检测弹窗类型（按优先级）：
        # A) cid=1365 标题 + 标题含"委托确认" → 真正的委托确认弹窗
        #    → confirm=true 点"是(Y)"提交, confirm=false 点"否(N)"取消
        # B) cid=1365 标题 + 标题不含"委托确认"（如"提示信息"）→ 警告弹窗
        #    → 点"是(Y)"关闭警告，继续等待后续"委托确认"弹窗
        #    → 【关键】此时不能设置 confirmed=True，因为订单尚未提交
        # C) cid=1040 文本 + cid=6 按钮（无 cid=1365）→ 警告弹窗（无标题图）
        #    → 点"是(Y)"继续
        # D) cid=1040 文本（无按钮）→ 纯错误弹窗 → 报错退出
        # E) 无弹窗 → 提交失败
        #
        # 同花顺可能连续弹出多个弹窗（先警告→再委托确认），
        # 关闭警告后必须继续检测后续弹窗。
        confirm_dialog_detected = False  # 是否检测到真正的"委托确认"弹窗
        order_detail_text = None
        confirmed = False  # 仅在真正的"委托确认"弹窗上点Y后才设为True
        warning_dismissed = False  # 是否关闭过警告/提示弹窗

        with timed("弹窗处理循环", self.logger):
            window = self.window_service.get_trading_window_fast()  # poll_until 已确认窗口存在，快速获取
            for check_attempt in range(5):
                if window is None:
                    time.sleep(0.1)
                    window = self.window_service.get_trading_window_fast()
                    continue

                # 快速交易模式：轮询超时（0.3s 无弹窗）+ 缓存未命中 →
                # 确认无弹窗，省去后续 4 轮 descendants 遍历（~4s）
                if check_attempt > 0 and not _dialog_detected and self._cached_descendants is None:
                    self.logger.info("未检测到任何弹窗，跳过后续检测")
                    break

                # 每轮重置弹窗文本（避免上一轮的旧值污染兜底逻辑）
                order_detail_text = None

                # 缓存 descendants 一次遍历供本轮所有查找复用
                # 优先复用 _has_any_dialog 找到弹窗时缓存的列表，避免第二次 UIA 遍历
                if self._cached_descendants is not None:
                    _descendants = self._cached_descendants
                    self._cached_descendants = None  # 用完即清，防止复用脏数据
                else:
                    _descendants = list(window.descendants())

                # A/B) 检查是否有标题图（cid=1365）
                title_el = self.window_service.find_element_in_window(
                    window, CONFIRM_DIALOG_TITLE_ID, descendants=_descendants
                )
                if title_el is not None:
                    _dialog_detected = True  # 标记已检测到弹窗，防止后续轮次错误跳出
                    title_text = title_el.window_text() or ""
                    self.logger.info(f"检测到弹窗标题: {title_text}")

                    # 读取弹窗详情文本（cid=1040）
                    # 弹窗刚出现时 detail text 控件可能尚未渲染，稍等再读
                    time.sleep(0.1)
                    detail_el = self.window_service.find_element_in_window(
                        window, CONFIRM_DETAIL_TEXT_ID, descendants=_descendants
                    )
                    if detail_el is not None:
                        order_detail_text = detail_el.window_text() or ""
                    # 兜底：cid=1040 未找到或为空时，扫描所有可见文本
                    if not order_detail_text:
                        order_detail_text = self.window_service.get_all_visible_texts(
                            window, descendants=_descendants
                        )
                    if order_detail_text:
                        self.logger.info(f"弹窗详情: {order_detail_text[:200]}")

                    if "委托确认" in title_text:
                        # A) 真正的委托确认弹窗 — 订单已提交，等待用户确认
                        confirm_dialog_detected = True
                        if confirm:
                            try:
                                self.window_service.click_element(window, CONFIRM_YES_BUTTON_ID)
                                confirmed = True
                                self.logger.info("已点击 '是(Y)' 确认委托")
                            except Exception as e:
                                self.logger.warning(f"点击 '是(Y)' 失败，尝试 Y 键: {e}")
                                self.window_service.send_key("Y")
                                confirmed = True
                        else:
                            try:
                                self.window_service.click_element(window, CONFIRM_NO_BUTTON_ID)
                                self.logger.info("已点击 '否(N)' 取消委托（预览模式）")
                            except Exception as e:
                                self.window_service.send_key("{ESC}")
                                self.logger.warning(f"点击 '否(N)' 失败，尝试 ESC: {e}")
                        break  # 委托确认处理完毕，结束循环
                    else:
                        # B) 非委托确认弹窗 — 需要区分"警告"和"错误"
                        #    警告（如价格超限提醒）：点Y关闭后继续等待委托确认
                        #    错误（如提交失败/清算中）：点Y关闭后立即跳出，由后续
                        #    「提交失败检测」统一处理，避免反复重试浪费 30s+
                        _error_keywords = ["提交失败", "清算中", "暂不支持"]
                        _is_submit_error = (
                            order_detail_text
                            and any(kw in order_detail_text for kw in _error_keywords)
                        )
                        if _is_submit_error:
                            # 提取干净的弹窗文本（过滤 UI 标签），分类报错
                            _popup_text = self._extract_popup_error_text(_descendants)
                            _error_code, _message, _suggestion = self._classify_submit_error(_popup_text)
                            self.logger.warning(
                                f"检测到提交错误弹窗: title={title_text}, "
                                f"error_code={_error_code}, text={_popup_text[:100]}"
                            )
                            self._close_non_confirm_popup(window, descendants=_descendants)
                            raise ApiError(
                                _error_code, _message, suggestion=_suggestion,
                                details={"popup_title": title_text, "popup_text": _popup_text}
                            )
                        else:
                            self.logger.warning(
                                f"检测到警告弹窗（非委托确认）: {title_text}，"
                                f"关闭后继续等待委托确认"
                            )
                            self._close_non_confirm_popup(window, descendants=_descendants)
                            warning_dismissed = True
                            time.sleep(0.2)
                            window = self.window_service.get_trading_window_fast()
                            continue  # 继续检测后续弹窗

                # C) 无标题图但有文本（cid=1040）+ 有"是(Y)"按钮 → 警告弹窗
                text_el = self.window_service.find_element_in_window(
                    window, CONFIRM_DETAIL_TEXT_ID, descendants=_descendants
                )
                if text_el is not None:
                    _dialog_detected = True  # 标记已检测到弹窗（无标题图的弹窗）
                    dialog_text = text_el.window_text() or ""
                    if not dialog_text.strip():
                        time.sleep(0.1)
                        continue

                    yes_btn = self.window_service.find_element_in_window(
                        window, CONFIRM_YES_BUTTON_ID, descendants=_descendants
                    )
                    if yes_btn is not None:
                        # C) 警告弹窗（含Y/N按钮，无标题图）
                        self.logger.warning(
                            f"检测到警告弹窗: {dialog_text[:100]}，点击 '是(Y)' 继续"
                        )
                        yes_btn.click_input()
                        warning_dismissed = True
                        time.sleep(0.2)
                        window = self.window_service.get_trading_window_fast()  # 弹窗关闭后重新获取
                        continue  # 继续检测后续弹窗（如委托确认）
                    else:
                        # D) 纯错误弹窗（无Y/N按钮），关闭并报错
                        self.logger.warning(f"检测到错误弹窗: {dialog_text[:100]}")
                        self._close_non_confirm_popup(window, descendants=_descendants)
                        DiagnosticUtil().snapshot("dialog_error")
                        _error_code, _message, _suggestion = self._classify_submit_error(dialog_text)
                        raise ApiError(
                            _error_code, _message, suggestion=_suggestion,
                            details={"popup_title": "（无标题）", "popup_text": dialog_text[:200]}
                        )

                time.sleep(0.1)

        # 检测"提交失败"弹窗：仅在未出现委托确认弹窗且未主动确认时检查。
        # 弹窗确认后订单已由券商前端校验；快速交易模式无弹窗则订单已直接提交。
        # 只有关闭过警告弹窗但委托确认未出现时，才可能存在提交失败风险。
        if not confirm_dialog_detected and not confirmed and warning_dismissed:
            with timed("检测提交失败弹窗", self.logger):
                time.sleep(0.3)  # 等待弹窗出现
                window = self.window_service.get_trading_window_fast()
                if window is not None:
                    # 一次 descendants 遍历：同时检查弹窗 + 提取文本
                    _descendants = list(window.descendants())
                    title_el = self.window_service.find_element_in_window(
                        window, CONFIRM_DIALOG_TITLE_ID, descendants=_descendants
                    )
                    if title_el is not None:
                        title_text = title_el.window_text() or ""
                        # 排除"委托确认"弹窗（正常流程中已处理）
                        if "委托确认" not in title_text:
                            _popup_text = self._extract_popup_error_text(_descendants)
                            _error_code, _message, _suggestion = self._classify_submit_error(_popup_text)
                            self.logger.warning(
                                f"检测到提交失败弹窗: {title_text}, "
                                f"error_code={_error_code}, text={_popup_text[:200]}"
                            )
                            self._close_non_confirm_popup(window, descendants=_descendants)
                            DiagnosticUtil().snapshot("order_submit_failed", window)
                            raise ApiError(
                                _error_code, _message, suggestion=_suggestion,
                                details={"popup_title": title_text, "popup_text": _popup_text}
                            )

        # 结果判定
        if not confirm_dialog_detected:
            if warning_dismissed:
                # 关闭了警告弹窗，但始终未出现"委托确认"→ 订单未提交
                self.logger.warning(
                    "关闭了警告弹窗但未出现委托确认弹窗，订单可能未提交"
                )
                DiagnosticUtil().snapshot("warning_but_no_confirm")
            else:
                # 无弹窗 = 快速交易模式（确认已关闭），订单已直接提交
                self.logger.info("未检测到弹窗，快速交易模式下订单已直接提交")
                confirmed = True  # 无弹窗时视为已提交

        action = "买入" if status == "1" else "卖出"
        mode = "市价" if is_market else "限价"
        result = {
            "action": action,
            "mode": mode,
            "code": code,
            "amount": amount,
            "price": price if price_type == "limit" else None,
            "confirmed": confirmed
        }
        self.logger.info(f"下单完成: {result}")
        return result

    def _switch_price_type(self, window, want_market: bool) -> bool:
        """切换限价/市价模式

        点击 control_id=1400 在限价/市价之间切换（toggle），双向通用。
        服务器正常时标签在「买入价格」↔「对手方最优」之间切换，
        服务器异常时不变或弹窗。

        策略：点击后先用短超时检测弹窗（服务器拒绝时弹窗 <0.5s），
        无弹窗再用长超时等待标签变化。

        Args:
            window: 当前窗口对象
            want_market: True=希望切到市价, False=希望切到限价

        Returns:
            切换后是否处于市价模式
        """
        _mode_name = "市价" if want_market else "限价"
        for attempt in range(2):
            self.logger.info(
                f"尝试切换到{_mode_name}模式（点击 1400, 尝试 {attempt + 1}/2）"
            )
            label = self.window_service.find_element_in_window(window, CONTROL_ID_PRICE_TYPE)
            if label is None:
                raise Exception(f"未找到价格类型控件 control_id={CONTROL_ID_PRICE_TYPE}")
            label.click_input()

            # 第一步：短超时检测弹窗（服务器拒绝时弹窗立即出现）
            time.sleep(0.3)
            window = self.window_service.get_trading_window()
            if window is not None and self._dismiss_server_error_popup(window):
                window = self.window_service.get_trading_window()
                try:
                    _desc = list(window.descendants()) if window else []
                    _text = self._extract_popup_error_text(_desc)
                except Exception:
                    _text = ""
                _code, _msg, _sug = self._classify_submit_error(_text)
                raise ApiError(
                    _code,
                    f"切换{_mode_name}模式失败: {_msg}",
                    suggestion=_sug + (" 或改用限价模式（price_type=limit）。" if want_market else ""),
                    details={"phase": "switch_price_type", "attempt": attempt + 1}
                )

            # 第二步：等待标签变化到目标状态
            try:
                poll_until(
                    lambda: (self._is_market_mode(
                        self.window_service.get_trading_window()
                    ) == want_market),
                    timeout=3.0, interval=0.3,
                    description=f"切换到{_mode_name}（尝试 {attempt + 1}/2）"
                )
                self.logger.info(f"{_mode_name}模式切换成功")
                return want_market
            except PollTimeoutError:
                self.logger.warning(
                    f"尝试 {attempt + 1}/2 超时，标签未变化"
                )
                window = self.window_service.get_trading_window()
                if window is None:
                    raise Exception("切换价格模式时窗口消失")

        raise ApiError(
            ErrorCode.MODE_SWITCH_FAILED,
            f"切换{_mode_name}模式失败（已重试 2 次）",
            suggestion="可能原因：券商服务器异常/模拟账户不支持市价/网络问题。"
                       + ("建议改用限价模式（price_type=limit）重试。" if want_market else "")
        )

    def _is_market_mode(self, window, descendants=None) -> bool:
        """检查当前是否处于市价模式

        control_id=1400 标签文本：
        - "买入价格" = 限价模式
        - "市价买入" / "对手方最优" 等 = 市价模式
        """
        label = self.window_service.find_element_in_window(
            window, CONTROL_ID_PRICE_TYPE, descendants=descendants)
        if label is None:
            return False
        text = label.window_text() or ""
        return "市价" in text or "最优" in text

    def _has_any_dialog(self) -> bool:
        """检查下单后是否有弹窗出现

        检测 cid=1365（标题图）或 cid=1040（详情文本），
        用于 poll_until 替代固定 sleep。
        使用快速窗口获取（不重试），避免轮询时每 0.1s 产生 1.5s 开销。
        """
        window = self.window_service.get_trading_window_fast()
        if window is None:
            return False
        # 一次 descendants 遍历同时检查两个 cid（原来分两次需要 2s）
        # 找到弹窗时缓存列表，供后续弹窗处理循环复用，省去第二次遍历
        try:
            _descendants = list(window.descendants())
            for el in _descendants:
                try:
                    cid = el.control_id()
                    if cid in (CONFIRM_DIALOG_TITLE_ID, CONFIRM_DETAIL_TEXT_ID):
                        self._cached_descendants = _descendants
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    @staticmethod
    def _extract_popup_error_text(descendants) -> str:
        """从 descendants 列表中提取弹窗内的错误文本（过滤窗口 UI 标签）

        弹窗文本通常只有几行（标题 + 错误内容 + 按钮文字），
        过滤掉交易窗口的大量 UI 标签（证券代码、买入价格 等）。
        """
        # 交易窗口 UI 标签（不应出现在弹窗文本中）
        _ui_labels = {
            "证券代码", "证券名称", "买入价格", "卖出价格", "买入数量",
            "卖出数量", "可买(股)", "可卖(股)", "买入股票", "卖出股票",
            "市价买入", "市价卖出", "对手方最优", "HexinScrollWnd",
            "Custom1", "Custom2", "Spin2", "HK$", "价格跟随",
            "全撤", "撤买", "撤卖", "撤最后", "水平滚动条", "垂直滚动条",
            "左移一列", "右移一列", "上一行", "下一行",
            "向上翻页", "向下翻页", "向左翻页", "向右翻页",
            "买入[F1]", "卖出[F2]", "撤单[F3]", "查询[F4]",
        }
        lines = []
        for el in descendants:
            try:
                t = (el.window_text() or "").strip()
                if not t or len(t) > 200:
                    continue
                # 过滤 UI 标签
                if t in _ui_labels:
                    continue
                # 过滤纯数字/时间/位置等
                if t in ("多", "少", "位置", "添加", "打开", "关闭",
                          "专业", "精简", "退出", "登录", "系统",
                          "最小化", "最大化", "分析", "风控", "条件",
                          "双向", "报告问题", "买入", "卖出"):
                    continue
                lines.append(t)
            except Exception:
                pass
        return "\n".join(lines)

    @staticmethod
    def _classify_submit_error(error_text: str):
        """根据弹窗文本分类提交错误，返回 (error_code, message, suggestion)"""
        if "清算" in error_text:
            return (
                ErrorCode.SERVER_CLEARING,
                f"券商系统清算中: {error_text[:150]}",
                "请等待券商清算结束后重试（通常交易日 15:30-次日 9:00）。"
            )
        if "当前时间不允许委托" in error_text or "非交易" in error_text:
            return (
                ErrorCode.OUTSIDE_TRADING_HOURS,
                f"非交易时段: {error_text[:150]}",
                "请在交易时段内操作（工作日 9:30-11:30, 13:00-15:00）。"
            )
        # T+1 制度限制（仅卖出）：当日买入的股票次日才能卖出
        if any(kw in error_text for kw in T1_RESTRICTION_KEYWORDS):
            return (
                ErrorCode.T1_RESTRICTION,
                f"T+1 制度限制: {error_text[:150]}",
                "A 股实行 T+1 交易制度，当日买入的股票需至下一个交易日方可卖出。"
            )
        # 可卖数量不足（仅卖出）
        if "可卖数量" in error_text or "可用余额不足" in error_text:
            return (
                ErrorCode.INSUFFICIENT_SHARES,
                f"可卖数量不足: {error_text[:150]}",
                "请检查持仓的可卖数量（冻结数量/当日买入不可卖出）后调整委托数量。"
            )
        if "事务处理机" in error_text or "转发数据失败" in error_text:
            return (
                ErrorCode.SERVER_UNAVAILABLE,
                f"券商服务器不可用: {error_text[:150]}",
                "请确认券商服务器正常运行后重试。若为交易时段外，请等待交易时段再操作。"
            )
        return (
            ErrorCode.ORDER_SUBMIT_FAILED,
            f"订单提交失败: {error_text[:150]}",
            "请检查交易条件（余额、交易时间、涨跌停限制等）后重试"
        )

    def _close_non_confirm_popup(self, window, descendants=None) -> None:
        """关闭非委托确认类弹窗（"提示"等，只有"确定"键，无 Y/N 键）

        尝试点击标准 Windows 对话框按钮（IDOK=1, IDCANCEL=2），
        降级用 keybd_event 直接发 ESC（不经过 send_key，避免前台窗口校验失败）。

        不能用 send_key("{ESC}") —— ESC 强制激活交易窗口到前台，
        但模态弹窗遮盖了交易窗口导致激活失败。
        """
        import win32api
        import win32con

        # 方案1: 点击标准 Windows 按钮（IDOK=1="确定", IDCANCEL=2="取消"）
        for btn_id in (1, 2):
            btn = self.window_service.find_element_in_window(window, btn_id, descendants=descendants)
            if btn is not None:
                try:
                    btn.click_input()
                    self.logger.info(f"已点击按钮 cid={btn_id} 关闭弹窗")
                    return
                except Exception:
                    continue

        # 方案2: keybd_event 直接发 ESC（不经过 send_key，免去前台窗口校验）
        self.logger.info("未找到标准按钮，用 keybd_event 发送 ESC 关闭弹窗")
        win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.1)

    def _dismiss_server_error_popup(self, window) -> bool:
        """检测并关闭券商服务器错误弹窗（如服务器维护时的提示）

        在输入股票代码后自动查询价格、或切换价格模式时，若券商服务器维护，
        可能弹出"事务处理机转发数据失败"、"Begin failed!" 等错误弹窗。

        此方法委托 window_service.dismiss_blocking_popup() 统一检测并关闭，
        使用 SERVER_ERROR_POPUP_KEYWORDS 作为匹配关键词。

        与 trading_service / position_service 的 _dismiss_blocking_popup /
        _dismiss_popup_if_present 保持一致，统一走 WindowService 的 centralized 处理。

        Args:
            window: 交易窗口对象

        Returns:
            True=检测到并关闭了弹窗, False=无相关弹窗
        """
        return self.window_service.dismiss_blocking_popup(
            window, popup_keywords=list(SERVER_ERROR_POPUP_KEYWORDS)
        )

    def confirm_order(self) -> dict:
        """单独发送 Y 键确认委托（用于 confirm=false 的下单后续确认）"""
        self.logger.info("发送 Y 键确认委托")
        self.window_service.send_key("Y")
        time.sleep(0.5)
        return {"confirmed": True}
