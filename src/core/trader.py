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

    # 上次下单是否出现过弹窗（类变量，跨实例持久化）
    # TaskQueue 读取此标志判断连续同向订单能否跳过准备操作
    _had_any_dialog = False
    # 价格超限等警告是否被干净关闭（点「否」而非 ESC 乱关）
    # True 时窗口状态仍可信，TaskQueue 不会清除 _last_task_info
    _clean_dismiss = False

    def __init__(self, window_service: WindowService):
        self.window_service = window_service
        self.config = AppConfig()
        self.logger = Logger.get_instance()

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

        # 重置弹窗标志：place_order 执行过程中若遇到任何弹窗，设为 True
        Trader._had_any_dialog = False
        Trader._clean_dismiss = False

        # 0. 价格格式校验（A 股限 2 位小数）
        if price_type == "limit" and price:
            sanitized_price = sanitize_price(price)
            if sanitized_price != price:
                self.logger.warning(
                    f"价格 {price} 已自动修正为 {sanitized_price}（A股限 2 位小数）"
                )
            price = sanitized_price

        # 1. 激活窗口 + F1/F2（上笔干净退出时可跳过重置+激活）
        from src.api.task_queue import TaskQueue
        _task_queue = TaskQueue.get_instance()
        if _task_queue.skip_window_setup:
            _task_queue.skip_window_setup = False  # 单次消耗
            _last_status = (_task_queue._last_task_info or {}).get("status")
            if status != _last_status:
                # 交叉方向（买→卖 或 卖→买）：只需按 F 键切换
                self.logger.info(
                    f"跳过窗口重置，按 {'F1' if status == '1' else 'F2'} 切换方向"
                )
                self.window_service.send_key(
                    "F1" if status == "1" else "F2", background=True)
                time.sleep(0.1)
            else:
                self.logger.info("连续同向订单，跳过窗口激活与 F1/F2")
        else:
            with timed("激活窗口", self.logger):
                trading_paths = self.config.get_trading_app_paths()
                if not trading_paths:
                    raise Exception("未配置 xiadan.exe 路径，请检查 config/app_config.json")
                self.window_service.activate_window(trading_paths)
                time.sleep(0.2)

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
            # 轮询等待券商自动填充价格（多数 <0.1s），替代固定 sleep(0.3)
            try:
                _price_el = self.window_service.find_element_in_window(
                    window, CONTROL_ID_PRICE, descendants=_descendants)
                poll_until(
                    lambda: _price_el and bool((_price_el.window_text() or "").strip()),
                    timeout=0.3, interval=0.05,
                    description="券商价格自动填充"
                )
            except PollTimeoutError:
                pass  # 券商无自动填充或填充较慢，继续执行

        # 4.1 防御：检查服务器错误弹窗（复用步骤3的 _descendants，无需重遍历）
        # 如 "事务处理机转发数据失败"、"Begin failed!" 等，95%+ 订单无此弹窗
        with timed("代码输入后弹窗防御", self.logger):
            _has_server_error = False
            try:
                for el in _descendants:
                    try:
                        text = el.window_text() or ""
                        if any(kw in text for kw in SERVER_ERROR_POPUP_KEYWORDS):
                            _has_server_error = True
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            if _has_server_error:
                # 检测到弹窗关键词，走完整关闭流程并重新获取窗口
                dismissed = self._dismiss_server_error_popup(window)
                if dismissed:
                    window = self.window_service.get_trading_window()
                    if window is None:
                        raise Exception("关闭弹窗后窗口消失")
                    _descendants = list(window.descendants())

        # 5. 确保价格模式匹配
        # 策略：模式不匹配时先点击切换按钮（不等待），立即进入填数量——数量填充的
        # ~0.7s 与模式切换的 <0.2s 重叠，之后验证时已自然就绪，无需额外等待。
        with timed("切换价格模式", self.logger):
            want_market = (price_type == "market")
            is_market = self._is_market_mode(window, descendants=_descendants)
            _mode_switch_pending = False
            if want_market != is_market:
                # 阶段 1/2：点击切换按钮（立即返回）
                self._click_price_type_toggle(window, descendants=_descendants)
                _mode_switch_pending = True

        # 6. 填写数量（与模式切换并行，数量控件不受限价/市价模式影响）
        if amount:
            with timed("填写数量", self.logger):
                self.window_service.input_text_to_element(
                    window, CONTROL_ID_AMOUNT, amount, descendants=_descendants, delay=0.05)
                time.sleep(0.05)

        # 5b. 阶段 2/2：验证模式切换（填数量期间已自然等待 ~0.7s，切换早已完成）
        if _mode_switch_pending:
            with timed("验证价格模式切换", self.logger):
                ok, _fresh_win, _fresh_desc = self._verify_price_type_switch(want_market)
                if ok:
                    is_market = want_market
                    # 复用验证返回的新鲜窗口+descendants，省去重复刷新（~0.8s）
                    window, _descendants = _fresh_win, _fresh_desc
                else:
                    # 切换未成功，重试完整切换流程
                    is_market = self._switch_price_type(
                        window, want_market, descendants=_descendants)
                    window = self.window_service.get_trading_window()
                    if window is None:
                        raise Exception("切换价格模式后窗口消失")
                    _descendants = list(window.descendants())

        # 7. 填写价格（仅限价，此时模式已确认）
        if price_type == "limit" and price:
            with timed("填写价格", self.logger):
                self.window_service.input_text_to_element(
                    window, CONTROL_ID_PRICE, price, descendants=_descendants, delay=0.05)
                time.sleep(0.05)

        # 8. 点击下单按钮并处理弹窗
        with timed("点击下单按钮", self.logger):
            self.window_service.click_element(
                window, CONTROL_ID_SUBMIT, descendants=_descendants)
            self.logger.info("已点击下单按钮，等待弹窗")

        # 统一弹窗检测与处理：sleep(0.4) 等待渲染 + 一次 UIA 遍历完成检测+处理
        # 替代原来的 poll_until(_has_any_dialog) + 弹窗循环两次遍历。
        #
        # 快速交易模式（确认=否）：sleep → 遍历无弹窗 → 立即返回
        # 确认模式：sleep → 遍历找到弹窗 → 点 Y/N → 返回
        # 警告+确认：遍历找到警告 → 关闭 → 继续检测后续委托确认弹窗
        #
        # 弹窗类型优先级：
        # A) cid=1365 标题 + 标题含"委托确认" → 确认弹窗（点 Y/N 后结束）
        # B) cid=1365 标题 + 标题不含"委托确认" → 警告弹窗（关闭后继续检测）
        # C) cid=1040 文本 + cid=6 按钮 → 警告弹窗无标题图（关闭后继续检测）
        # D) cid=1040 文本（无按钮）→ 纯错误弹窗 → 报错退出
        # E) 首轮无弹窗 → 快速交易模式，直接返回
        confirm_dialog_detected = False
        order_detail_text = None
        confirmed = False
        warning_dismissed = False

        with timed("弹窗检测与处理", self.logger):
            time.sleep(0.4)  # 等待弹窗渲染，比 poll+descendants 遍历快

            for check_attempt in range(5):
                window = self.window_service.get_trading_window_fast()
                if window is None:
                    time.sleep(0.1)
                    continue

                # 一次 descendants 遍历：检测 + 处理
                _descendants = list(window.descendants())

                # A/B) 检查是否有标题图（cid=1365）
                title_el = self.window_service.find_element_in_window(
                    window, CONFIRM_DIALOG_TITLE_ID, descendants=_descendants
                )
                if title_el is not None:
                    title_text = title_el.window_text() or ""
                    self.logger.info(f"检测到弹窗标题: {title_text}")

                    # 读取弹窗文本：先试 cid=1040，失败则从弹窗容器内提取
                    # （避免 get_all_visible_texts 混入主窗口 UI 标签）
                    time.sleep(0.1)
                    detail_el = self.window_service.find_element_in_window(
                        window, CONFIRM_DETAIL_TEXT_ID, descendants=_descendants
                    )
                    if detail_el is not None:
                        order_detail_text = detail_el.window_text() or ""
                    if not order_detail_text:
                        order_detail_text = self._extract_dialog_text(title_el)
                    if order_detail_text:
                        self.logger.info(f"弹窗详情: {order_detail_text[:200]}")

                    if "委托确认" in title_text:
                        # A) 真正的委托确认弹窗 — 订单已提交
                        confirm_dialog_detected = True
                        if confirm:
                            try:
                                self.window_service.click_element(
                                    window, CONFIRM_YES_BUTTON_ID, descendants=_descendants)
                                confirmed = True
                                self.logger.info("已点击 '是(Y)' 确认委托")
                            except Exception as e:
                                self.logger.warning(f"点击 '是(Y)' 失败，尝试 Y 键: {e}")
                                self.window_service.send_key("Y")
                                confirmed = True
                        else:
                            try:
                                self.window_service.click_element(
                                    window, CONFIRM_NO_BUTTON_ID, descendants=_descendants)
                                self.logger.info("已点击 '否(N)' 取消委托（预览模式）")
                            except Exception as e:
                                self._close_non_confirm_popup(window, descendants=_descendants)
                                self.logger.warning(f"点击 '否(N)' 失败，降级关闭弹窗: {e}")
                        break  # 委托确认处理完毕，结束循环
                    else:
                        # B) 非委托确认弹窗 → 区分致命错误 / 干净错误 / 价格警告 / 普通警告
                        _error_keywords = ["提交失败", "清算中", "暂不支持"]
                        _is_submit_error = (
                            order_detail_text
                            and any(kw in order_detail_text for kw in _error_keywords)
                        )
                        # 干净错误：用户侧问题（余额不足等），窗口状态未损坏
                        _clean_error_keywords = ["余额不足", "不允许卖空"]
                        _is_clean_error = (
                            order_detail_text
                            and any(kw in order_detail_text for kw in _clean_error_keywords)
                        )
                        if _is_clean_error:
                            # 点「确定」关闭单按钮弹窗，干净退出
                            # order_detail_text 来自弹窗容器内提取，不含主窗口 UI
                            self.logger.warning(
                                f"检测到干净错误弹窗: {order_detail_text[:100]}"
                            )
                            self._close_non_confirm_popup(window, descendants=_descendants)
                            _error_code, _message, _suggestion = self._classify_submit_error(
                                order_detail_text
                            )
                            Trader._clean_dismiss = True
                            raise ApiError(
                                _error_code, _message, suggestion=_suggestion,
                                details={"popup_title": title_text,
                                         "popup_text": order_detail_text}
                            )
                        elif _is_submit_error:
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
                            # B-warning) 非委托确认弹窗 + 无错误关键词 → 区分价格警告/通用警告
                            _price_keywords = ["涨跌停", "超出", "价格"]
                            _is_price_warning = (
                                order_detail_text
                                and any(kw in order_detail_text for kw in _price_keywords)
                            )
                            if _is_price_warning:
                                # 价格超限警告 → 点「否(N)」取消，干净退出
                                # 弹窗是正常关闭的，窗口状态可信，下次同向可跳过
                                self.logger.warning(
                                    f"价格超限警告: {order_detail_text[:100]}，"
                                    f"点击 '否(N)' 取消委托"
                                )
                                try:
                                    self.window_service.click_element(
                                        window, CONFIRM_NO_BUTTON_ID, descendants=_descendants)
                                except Exception:
                                    self._close_non_confirm_popup(window, descendants=_descendants)
                                Trader._clean_dismiss = True
                                raise ApiError(
                                    ErrorCode.PRICE_OUT_OF_RANGE,
                                    f"价格超出涨跌停限制: {order_detail_text[:150]}",
                                    suggestion="请调整委托价格至涨跌停范围内后重试。"
                                )
                            else:
                                # 通用警告 → 点「是(Y)」继续提交
                                self.logger.warning(
                                    f"检测到警告弹窗: {title_text}，"
                                    f"点击 '是(Y)' 继续"
                                )
                                try:
                                    self.window_service.click_element(
                                        window, CONFIRM_YES_BUTTON_ID, descendants=_descendants)
                                except Exception:
                                    self._close_non_confirm_popup(window, descendants=_descendants)
                                warning_dismissed = True
                                time.sleep(0.2)
                                continue  # 继续检测后续弹窗（委托确认）

                # C) 无标题图但有文本（cid=1040）+ 有"是(Y)"按钮 → 警告弹窗
                text_el = self.window_service.find_element_in_window(
                    window, CONFIRM_DETAIL_TEXT_ID, descendants=_descendants
                )
                if text_el is not None:
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

                # 当前轮未找到任何弹窗
                if check_attempt == 0:
                    # 首轮 sleep(0.4) 后无弹窗 → 快速交易模式，订单已直接提交
                    self.logger.info("未检测到弹窗，快速交易模式下订单已直接提交")
                    break
                # check_attempt > 0 且无弹窗：前几轮关闭了警告弹窗，但委托确认未出现
                # 可能是提交失败，跳出循环走后续检测
                break

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
                # 关闭了警告弹窗但未出现委托确认 → 警告本身含确认语义
                # （如价格超限警告点 Y = 继续提交），无错误弹窗即视为已提交
                self.logger.info("警告已关闭且无错误弹窗，视为已提交")
                confirmed = True
            else:
                # 无弹窗 = 快速交易模式（确认已关闭），订单已直接提交
                self.logger.info("未检测到弹窗，快速交易模式下订单已直接提交")
                confirmed = True  # 无弹窗时视为已提交

        # 记录是否有弹窗出现：仅快速交易模式（完全无弹窗）可让下次同向订单跳过准备
        Trader._had_any_dialog = confirm_dialog_detected or warning_dismissed

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

    def _click_price_type_toggle(self, window, descendants=None) -> None:
        """点击价格类型切换按钮（控制 ID=1400），不等待确认

        从已有 descendants 中查找 label 元素，极快（~0.01s）。
        调用后标签文字几乎立即变化，验证阶段（_verify_price_type_switch）
        在填数量之后执行，已自然等待足够时间。
        """
        label = self.window_service.find_element_in_window(
            window, CONTROL_ID_PRICE_TYPE, descendants=descendants)
        if label is None:
            raise Exception(f"未找到价格类型控件 control_id={CONTROL_ID_PRICE_TYPE}")
        label.click_input()

    def _verify_price_type_switch(self, want_market: bool):
        """快速验证价格模式是否已切换，返回 (成功, 新鲜窗口, 新鲜descendants)

        用新鲜窗口连接检查标签文本（避免 pywinauto UIA 缓存）。
        调用时机在填数量之后，已自然等待足够时间让标签变化。
        成功时返回新鲜窗口+descendants，caller 可直接复用，省去重复刷新。
        """
        window = self.window_service.get_trading_window_fast()
        if window is None:
            return False, None, None
        if want_market != self._is_market_mode(window):
            return False, None, None
        return True, window, list(window.descendants())

    def _switch_price_type(self, window, want_market: bool, descendants=None) -> bool:
        """切换限价/市价模式

        点击 control_id=1400 在限价/市价之间切换（toggle），双向通用。
        策略：click → 短 sleep + 新鲜窗口检查标签变化（避免 pywinauto UIA 缓存）。

        Args:
            window: 当前窗口对象
            want_market: True=希望切到市价
            descendants: 可选，已有的 descendants 列表（省一次 UIA 遍历）
        """
        _mode_name = "市价" if want_market else "限价"
        for attempt in range(2):
            self.logger.info(
                f"尝试切换到{_mode_name}模式（点击 1400, 尝试 {attempt + 1}/2）"
            )
            label = self.window_service.find_element_in_window(
                window, CONTROL_ID_PRICE_TYPE, descendants=descendants)
            if label is None:
                raise Exception(f"未找到价格类型控件 control_id={CONTROL_ID_PRICE_TYPE}")
            label.click_input()

            # 标签切换在 <0.2s 内完成，用短 sleep + 新鲜窗口检查
            # pywinauto 的 element ref 有 UIA 缓存问题，必须重新连接窗口
            time.sleep(0.25)
            _fresh = self.window_service.get_trading_window_fast()
            if _fresh is not None and want_market == self._is_market_mode(_fresh):
                self.logger.info(f"{_mode_name}模式切换成功")
                return want_market

            # 一次未就绪，再等 0.3s 重试（极少数慢网络场景）
            time.sleep(0.3)
            _fresh = self.window_service.get_trading_window_fast()
            if _fresh is not None and want_market == self._is_market_mode(_fresh):
                self.logger.info(f"{_mode_name}模式切换成功")
                return want_market

            self.logger.warning(
                f"尝试 {attempt + 1}/2 标签未变化，检查是否有服务器错误弹窗"
            )
            # 标签不变 → 罕见路径：服务器可能拒绝了切换（弹窗）
            window = self.window_service.get_trading_window()
            if window is None:
                raise Exception("切换价格模式时窗口消失")
            if self._dismiss_server_error_popup(window):
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

        检测 cid=1365（标题图）或 cid=1040（详情文本），用于 confirm_order 等场景。
        """
        window = self.window_service.get_trading_window_fast()
        if window is None:
            return False
        try:
            for el in window.descendants():
                try:
                    if el.control_id() in (CONFIRM_DIALOG_TITLE_ID, CONFIRM_DETAIL_TEXT_ID):
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    @staticmethod
    def _extract_dialog_text(title_el) -> str:
        """从弹窗标题元素的父容器中提取弹窗文本

        只在弹窗容器内查找，避免混入主窗口 UI 标签。
        与 _extract_popup_error_text（全局扫描+黑名单过滤）互补：
        前者用于获取纯净弹窗文本，后者用于错误分类时的兜底提取。
        """
        try:
            parent = title_el.parent()
            texts = []
            for c in parent.descendants():
                try:
                    t = (c.window_text() or "").strip()
                    # 过滤：空文本、过长文本、标题本身、菜单标签
                    if not t or len(t) > 200:
                        continue
                    if t in ("提示", "提示信息", "委托确认", "是(Y)", "否(N)",
                              "确定", "取消"):
                        continue
                    texts.append(t)
                except Exception:
                    continue
            return "\n".join(texts)
        except Exception:
            return ""

    @staticmethod
    def _extract_popup_error_text(descendants) -> str:
        """从 descendants 列表中提取弹窗内的错误文本（过滤窗口 UI 标签）

        弹窗文本通常只有几行（标题 + 错误内容 + 按钮文字），
        过滤掉交易窗口的大量 UI 标签（证券代码、买入价格 等）。
        """
        # 交易窗口 UI 标签 + 侧边栏菜单项（不应出现在弹窗文本中）
        _ui_labels = {
            "证券代码", "证券名称", "买入价格", "卖出价格", "买入数量",
            "卖出数量", "可买(股)", "可卖(股)", "买入股票", "卖出股票",
            "市价买入", "市价卖出", "对手方最优", "HexinScrollWnd",
            "Custom1", "Custom2", "Spin2", "HK$", "价格跟随",
            "全撤", "撤买", "撤卖", "撤最后", "水平滚动条", "垂直滚动条",
            "左移一列", "右移一列", "上一行", "下一行",
            "向上翻页", "向下翻页", "向左翻页", "向右翻页",
            "买入[F1]", "卖出[F2]", "撤单[F3]", "查询[F4]",
            # 侧边栏菜单
            "买入", "卖出", "自选股", "条件单", "组合交易", "科创板",
            "盘后定价委托", "批量下单", "申购", "北交所交易", "双向委托",
            "市价委托", "资金股票", "当日成交", "当日委托", "历史成交",
            "历史持仓", "历史委托", "资金明细", "对 帐 单", "交 割 单",
            "证券在途业务查询", "账户分析", "通用回购", "银证转账",
            "场内基金", "资金管理", "其他业务", "修改密码",
            "风险承受能力测评", "查询风险承受能力等级测评结果", "开通极速版",
            "刷新当前页面", "HexinScrollWnd2", "EmbChart",
            # 券商名称/状态栏
            "恒泰证券欢迎您", "内蒙", "报告问题",
        }
        lines = []
        for el in descendants:
            try:
                t = (el.window_text() or "").strip()
                if not t or len(t) > 200:
                    continue
                if t in _ui_labels:
                    continue
                if t in ("多", "少", "位置", "添加", "打开", "关闭",
                          "专业", "精简", "退出", "登录", "系统",
                          "最小化", "最大化", "分析", "风控", "条件",
                          "双向", "零"):
                    continue
                # 过滤纯数字/时间戳（如 "00:00:22", "1/1", "1/2" 等分页信息）
                if t.replace(":", "").replace("/", "").replace(".", "").isdigit():
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
        # 一次遍历同时找两个按钮（find_element_in_window 支持批量 control_id）
        buttons = self.window_service.find_element_in_window(
            window, (1, 2), descendants=descendants)
        for btn in (buttons or []):
            try:
                btn.click_input()
                self.logger.info(f"已点击按钮 cid={btn.control_id()} 关闭弹窗")
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
        """单独发送 Y 键确认委托（用于 confirm=false 的下单后续确认）

        注意：仅当委托确认弹窗确实存在时才发送 Y 键，
        避免快速交易模式下 Y 键泄漏到其他窗口。
        """
        self.logger.info("发送 Y 键确认委托")
        if not self._has_any_dialog():
            self.logger.warning("未检测到委托确认弹窗，跳过 Y 键发送")
            return {"confirmed": False}
        self.window_service.send_key("Y")
        time.sleep(0.5)
        return {"confirmed": True}
