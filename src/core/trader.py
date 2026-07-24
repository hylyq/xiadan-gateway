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

        # 2. F1/F2 切换买卖界面
        with timed("F1/F2 切换买卖界面", self.logger):
            self.window_service.send_key("F1" if status == "1" else "F2")
            time.sleep(0.1)

        # 3. 获取交易窗口
        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("未找到交易窗口 '网上股票交易系统5.0'")

        # 4. 填写股票代码（必须先填代码，否则价格模式切换可能被禁用）
        with timed("填写股票代码", self.logger):
            self.window_service.input_text_to_element(window, CONTROL_ID_CODE, code)
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

        # 5. 切换限价/市价模式（必须在填完代码之后，券商要求先有代码才能切换）
        with timed("切换价格模式", self.logger):
            want_market = (price_type == "market")
            is_market = self._switch_price_type(window, want_market)
            # 切换后必须重新获取窗口（descendants() 会缓存控件树）
            window = self.window_service.get_trading_window()
            if window is None:
                raise Exception("切换价格模式后窗口消失")

        # 6. 填写价格（仅限价）
        if price_type == "limit" and price:
            with timed("填写价格", self.logger):
                self.window_service.input_text_to_element(window, CONTROL_ID_PRICE, price)
                time.sleep(0.1)

        # 7. 填写数量
        if amount:
            with timed("填写数量", self.logger):
                self.window_service.input_text_to_element(window, CONTROL_ID_AMOUNT, amount)
                time.sleep(0.1)

        # 8. 点击下单按钮并处理弹窗
        with timed("点击下单按钮", self.logger):
            self.window_service.click_element(window, CONTROL_ID_SUBMIT)
            self.logger.info("已点击下单按钮，等待弹窗")

        # 轮询等待弹窗出现（替代固定 sleep(0.5)），超时则走后续无弹窗逻辑。
        # 快速交易模式下（买入/卖出确认=否）不弹委托确认窗，仅可能弹警告窗。
        # 警告弹窗在点击下单按钮后立即出现，0.3s 足够检测；
        # 正常情况无弹窗则快速跳过，不再浪费等待时间。
        with timed("等待下单弹窗", self.logger):
            try:
                poll_until(
                    lambda: self._has_any_dialog(),
                    timeout=0.3, interval=0.1,
                    description="下单后弹窗"
                )
            except PollTimeoutError:
                pass

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

                # 每轮重置弹窗文本（避免上一轮的旧值污染兜底逻辑）
                order_detail_text = None

                # A/B) 检查是否有标题图（cid=1365）
                title_el = self.window_service.find_element_in_window(
                    window, CONFIRM_DIALOG_TITLE_ID
                )
                if title_el is not None:
                    title_text = title_el.window_text() or ""
                    self.logger.info(f"检测到弹窗标题: {title_text}")

                    # 读取弹窗详情文本（cid=1040）
                    # 弹窗刚出现时 detail text 控件可能尚未渲染，稍等再读
                    time.sleep(0.1)
                    detail_el = self.window_service.find_element_in_window(
                        window, CONFIRM_DETAIL_TEXT_ID
                    )
                    if detail_el is not None:
                        order_detail_text = detail_el.window_text() or ""
                    # 兜底：cid=1040 未找到或为空时，扫描所有可见文本
                    if not order_detail_text:
                        order_detail_text = self.window_service.get_all_visible_texts(window)
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
                            self.logger.warning(
                                f"检测到提交错误弹窗（非警告）: title={title_text}, "
                                f"detail={order_detail_text[:100]}，关闭后报错"
                            )
                            self._close_non_confirm_popup(window)
                            DiagnosticUtil().snapshot("order_submit_error")
                            raise ApiError(
                                ErrorCode.ORDER_SUBMIT_FAILED,
                                f"订单提交失败: {order_detail_text[:200]}",
                                suggestion="请检查交易条件（余额、交易时间、涨跌停限制等）后重试",
                                details={"popup_title": title_text, "popup_text": order_detail_text[:200]}
                            )
                        else:
                            self.logger.warning(
                                f"检测到警告弹窗（非委托确认）: {title_text}，"
                                f"关闭后继续等待委托确认"
                            )
                            self._close_non_confirm_popup(window)
                            warning_dismissed = True
                            time.sleep(0.2)
                            window = self.window_service.get_trading_window_fast()
                            continue  # 继续检测后续弹窗

                # C) 无标题图但有文本（cid=1040）+ 有"是(Y)"按钮 → 警告弹窗
                text_el = self.window_service.find_element_in_window(
                    window, CONFIRM_DETAIL_TEXT_ID
                )
                if text_el is not None:
                    dialog_text = text_el.window_text() or ""
                    if not dialog_text.strip():
                        time.sleep(0.1)
                        continue

                    yes_btn = self.window_service.find_element_in_window(
                        window, CONFIRM_YES_BUTTON_ID
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
                        self._close_non_confirm_popup(window)

                        DiagnosticUtil().snapshot("dialog_error")

                        if status == "2" and any(kw in dialog_text for kw in T1_RESTRICTION_KEYWORDS):
                            raise Exception(
                                f"A 股实行 T+1 交易制度，当日买入的股票次日才能卖出。"
                                f"弹窗信息: {dialog_text[:200]}"
                            )

                        raise Exception(f"下单失败: {dialog_text[:200]}")

                time.sleep(0.1)

        # 确认后检测"提交失败"弹窗
        # 点击"是(Y)"确认委托后（或快速交易模式直接提交后），券商可能返回"提交失败"弹窗
        if confirmed or not confirm_dialog_detected:
            with timed("检测提交失败弹窗", self.logger):
                time.sleep(0.3)  # 等待弹窗出现
                window = self.window_service.get_trading_window_fast()
                if window is not None:
                    # 检查是否有新的提示弹窗（cid=1365 标题）
                    title_el = self.window_service.find_element_in_window(
                        window, CONFIRM_DIALOG_TITLE_ID
                    )
                    if title_el is not None:
                        title_text = title_el.window_text() or ""
                        # 排除"委托确认"弹窗（正常流程中已处理）
                        if "委托确认" not in title_text:
                            # cid=1040 可能在提交失败弹窗中为空，用全量文本扫描兜底
                            detail_el = self.window_service.find_element_in_window(
                                window, CONFIRM_DETAIL_TEXT_ID
                            )
                            fail_reason = detail_el.window_text() if detail_el else ""
                            if not fail_reason.strip():
                                # 兜底：扫描所有控件文本，提取"提交失败"相关内容
                                all_texts = self.window_service.get_all_visible_texts(window)
                                for line in all_texts.split("\n"):
                                    if "提交失败" in line or "失败" in line:
                                        fail_reason = line.strip()
                                        break
                            self.logger.warning(
                                f"检测到提交失败弹窗: {title_text}, 原因: {fail_reason[:200]}"
                            )
                            # 关闭弹窗（"提示"类弹窗只有确定键，点按钮关闭）
                            self._close_non_confirm_popup(window)
                            DiagnosticUtil().snapshot("order_submit_failed", window)
                            raise ApiError(
                                ErrorCode.ORDER_SUBMIT_FAILED,
                                f"订单提交失败: {fail_reason[:200]}" if fail_reason else "订单提交失败（券商返回错误）",
                                suggestion="请检查交易条件（余额、交易时间、涨跌停限制等）后重试",
                                details={"popup_title": title_text, "popup_text": fail_reason}
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

        F1 买入界面内，点击 control_id=1400（"买入价格"按钮）触发服务器请求，
        网络正常时标签从"买入价格"变为"对手方最优"，网络异常时不变。
        用 poll_until 轮询等待标签变化，最多重试 3 次。

        Args:
            window: 当前窗口对象
            want_market: True=希望切到市价, False=希望切到限价

        Returns:
            切换后是否处于市价模式
        """
        is_market = self._is_market_mode(window)
        if want_market == is_market:
            return is_market

        if want_market:
            for attempt in range(3):
                self.logger.info(
                    f"尝试切换到市价模式（点击 1400, 尝试 {attempt + 1}/3）"
                )
                label = self.window_service.find_element_in_window(window, CONTROL_ID_PRICE_TYPE)
                if label is None:
                    raise Exception(f"未找到价格类型控件 control_id={CONTROL_ID_PRICE_TYPE}")
                label.click_input()

                # 轮询等待标签变化（服务器响应）
                try:
                    poll_until(
                        lambda: self._is_market_mode(
                            self.window_service.get_trading_window()
                        ),
                        timeout=5.0, interval=0.3,
                        description=f"价格模式切换（尝试 {attempt + 1}/3）"
                    )
                    self.logger.info("市价模式切换成功")
                    return True
                except PollTimeoutError:
                    self.logger.warning(
                        f"尝试 {attempt + 1}/3 超时，标签未变化（网络可能异常）"
                    )
                    # 重新获取窗口，检查是否有服务器错误弹窗阻塞
                    window = self.window_service.get_trading_window()
                    if window is None:
                        raise Exception("切换价格模式时窗口消失")

                    # 防御：点击 1400 触发服务器请求，若服务器维护会弹出错误弹窗
                    # （如 "事务处理机转发数据失败"、"Begin failed!" 等）
                    if self._dismiss_server_error_popup(window):
                        # 弹窗关闭后重新获取窗口
                        window = self.window_service.get_trading_window()
                        if window is None:
                            raise Exception("关闭弹窗后窗口消失")
                        # 服务器不可用，重试无意义，直接报错
                        raise ApiError(
                            ErrorCode.SERVER_UNAVAILABLE,
                            "券商服务器不可用（切换价格模式时服务器返回错误）",
                            suggestion="请确认券商服务器正常运行后重试。若为交易时段外，"
                                       "请等待交易时段再操作；若怀疑服务器维护，请联系券商确认。",
                            details={"phase": "switch_price_type", "attempt": attempt + 1}
                        )

            raise ApiError(
                ErrorCode.MODE_SWITCH_FAILED,
                "切换市价模式失败（已重试 3 次，网络可能异常）",
                suggestion="请检查网络连接后重试，或改用限价模式（price_type=limit）"
            )
        else:
            # 切换到限价：F1 重置到默认限价模式
            self.logger.info("切换到限价模式（发送 F1）")
            self.window_service.send_key("F1")
            time.sleep(0.3)

            # 验证
            window = self.window_service.get_trading_window()
            if not self._is_market_mode(window):
                return False

            raise Exception("切换限价模式失败")

    def _is_market_mode(self, window) -> bool:
        """检查当前是否处于市价模式

        control_id=1400 标签文本：
        - "买入价格" = 限价模式
        - "市价买入" / "对手方最优" 等 = 市价模式
        """
        label = self.window_service.find_element_in_window(window, CONTROL_ID_PRICE_TYPE)
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
        return (
            self.window_service.find_element_in_window(
                window, CONFIRM_DIALOG_TITLE_ID
            ) is not None
            or self.window_service.find_element_in_window(
                window, CONFIRM_DETAIL_TEXT_ID
            ) is not None
        )

    def _close_non_confirm_popup(self, window) -> None:
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
            btn = self.window_service.find_element_in_window(window, btn_id)
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
