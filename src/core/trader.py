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

from src.models.config import AppConfig
from src.services.window_service import WindowService
from src.utils.logger import Logger


# 关键控件 control_id
CONTROL_ID_CODE = 1032        # 股票代码输入框
CONTROL_ID_PRICE = 1033       # 价格输入框（仅限价）
CONTROL_ID_AMOUNT = 1034      # 数量输入框
CONTROL_ID_SUBMIT = 1006      # 下单按钮
CONTROL_ID_PRICE_TYPE = 1400  # 价格类型标签（点击切换限价/市价）

# 委托确认弹窗控件
CONFIRM_DIALOG_TITLE_ID = 1365  # "委托确认"标题 Image
CONFIRM_YES_BUTTON_ID = 6       # "是(Y)" 按钮
CONFIRM_NO_BUTTON_ID = 7        # "否(N)" 按钮
CONFIRM_DETAIL_TEXT_ID = 1040   # 委托详细信息 Text（含资金帐号、股票、价格、数量）

# 窗口标题中识别市价模式的关键词
MARKET_KEYWORD = "市价"


class Trader:
    """下单编排器"""

    def __init__(self, window_service: WindowService):
        self.window_service = window_service
        self.config = AppConfig()
        self.logger = Logger.get_instance()

    @staticmethod
    def _sanitize_price(price: str) -> str:
        """对 A 股价格做格式校验，限制 2 位小数

        Args:
            price: 原始价格字符串

        Returns:
            格式化的价格字符串（最多 2 位小数）
        """
        try:
            price_float = float(price)
            sanitized = f"{price_float:.2f}"
            return sanitized
        except ValueError:
            raise Exception(f"价格格式无效: {price}")

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
            sanitized_price = self._sanitize_price(price)
            if sanitized_price != price:
                self.logger.warning(
                    f"价格 {price} 已自动修正为 {sanitized_price}（A股限 2 位小数）"
                )
            price = sanitized_price

        # 1. 激活 xiadan.exe（下单需要前台激活，因为 type_keys() 和 click_input() 需要焦点）
        trading_path = self.config.get_trading_app_path()
        if not trading_path:
            raise Exception("未配置 xiadan.exe 路径，请检查 config/app_config.json")
        self.window_service.activate_window(trading_path)
        time.sleep(0.2)

        # 2. F1/F2 切换买卖界面
        self.window_service.send_key("F1" if status == "1" else "F2")
        time.sleep(0.2)

        # 3. 获取交易窗口
        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("未找到交易窗口 '网上股票交易系统5.0'")

        # 4. 切换限价/市价模式
        want_market = (price_type == "market")
        is_market = self._switch_price_type(window, want_market)
        # 切换后必须重新获取窗口（descendants() 会缓存控件树）
        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("切换价格模式后窗口消失")

        # 5. 填写股票代码
        self.window_service.input_text_to_element(window, CONTROL_ID_CODE, code)
        time.sleep(0.5)  # 等待券商自动填充价格完成

        # 6. 填写价格（仅限价）
        if price_type == "limit" and price:
            self.window_service.input_text_to_element(window, CONTROL_ID_PRICE, price)
            time.sleep(0.1)

        # 7. 填写数量
        if amount:
            self.window_service.input_text_to_element(window, CONTROL_ID_AMOUNT, amount)
            time.sleep(0.1)

        # 8. 点击下单按钮并处理弹窗
        self.window_service.click_element(window, CONTROL_ID_SUBMIT)
        self.logger.info("已点击下单按钮，等待弹窗")
        time.sleep(0.5)

        # 检测弹窗类型：
        # - 有 "委托确认" 标题 (cid=1365) → 订单确认弹窗，点击 "是(Y)" 提交
        # - 有 1040 文本 + 有 6 按钮（无 1365 标题）→ 警告弹窗（如价格格式有误，含Y/N），
        #   点击 "是(Y)" 继续，然后下一轮循环检查委托确认
        # - 仅有 1040 文本（无 6 按钮）→ 纯错误弹窗，报错退出
        # - 无弹窗 → 提交失败（控件未正确操作）
        confirm_dialog_detected = False
        order_detail_text = None
        confirmed = False
        warning_dismissed = False  # 是否已关闭过警告弹窗

        for check_attempt in range(3):
            window = self.window_service.get_trading_window()
            if window is None:
                time.sleep(0.3)
                continue

            # A) 检查是否有"委托确认"标题（cid=1365 Image）
            title_el = self.window_service.find_element_in_window(
                window, CONFIRM_DIALOG_TITLE_ID
            )
            if title_el is not None:
                confirm_dialog_detected = True
                title_text = title_el.window_text() or ""
                self.logger.info(f"检测到订单确认弹窗: {title_text}")

                # 读取委托详情（cid=1040）
                detail_el = self.window_service.find_element_in_window(
                    window, CONFIRM_DETAIL_TEXT_ID
                )
                if detail_el is not None:
                    order_detail_text = detail_el.window_text() or ""
                    self.logger.info(f"委托详情: {order_detail_text}")

                if confirm:
                    # 点击"是(Y)"确认委托
                    try:
                        self.window_service.click_element(window, CONFIRM_YES_BUTTON_ID)
                        confirmed = True
                        self.logger.info("已点击 '是(Y)' 确认委托")
                    except Exception as e:
                        # 退路：发送 Y 键
                        self.logger.warning(f"点击 '是(Y)' 失败，尝试 Y 键: {e}")
                        self.window_service.send_key("Y")
                        confirmed = True
                else:
                    # 点击"否(N)"取消委托
                    try:
                        self.window_service.click_element(window, CONFIRM_NO_BUTTON_ID)
                        self.logger.info("已点击 '否(N)' 取消委托")
                    except Exception as e:
                        self.window_service.send_key("{ESC}")
                        self.logger.warning(f"点击 '否(N)' 失败，尝试 ESC: {e}")
                break

            # B) 检查是否有警告/错误弹窗（cid=1040 文本，无 "委托确认" 标题）
            text_el = self.window_service.find_element_in_window(
                window, CONFIRM_DETAIL_TEXT_ID
            )
            if text_el is not None:
                dialog_text = text_el.window_text() or ""
                if not dialog_text.strip():
                    time.sleep(0.3)
                    continue

                # 检查是否有 "是(Y)" 按钮 → 警告弹窗（含Y/N）
                yes_btn = self.window_service.find_element_in_window(
                    window, CONFIRM_YES_BUTTON_ID
                )
                if yes_btn is not None:
                    self.logger.warning(
                        f"检测到警告弹窗: {dialog_text[:100]}，点击 '是(Y)' 继续"
                    )
                    yes_btn.click_input()
                    warning_dismissed = True
                    time.sleep(0.3)
                    continue  # 继续检测后续弹窗（如委托确认）
                else:
                    # 纯错误弹窗（无Y/N按钮），关闭并报错
                    self.logger.warning(f"检测到错误弹窗: {dialog_text[:100]}")
                    self.window_service.send_key("{ENTER}")
                    raise Exception(f"下单失败: {dialog_text[:200]}")

            time.sleep(0.3)

        if not confirm_dialog_detected:  # 未出现任何弹窗
            self.logger.warning("下单后未检测到弹窗，可能提交失败")

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

        通过点击 control_id=1400 标签切换:
        - 标签 name 含 "市价" = 当前市价模式
        - 标签 name 不含 "市价" = 当前限价模式

        每次点击后必须重新 get_target_window，因为 descendants() 会缓存控件树。

        Args:
            window: 当前窗口对象
            want_market: True=希望切到市价, False=希望切到限价

        Returns:
            切换后是否处于市价模式
        """
        is_market = False
        for attempt in range(3):
            # 每次循环都重新获取窗口
            window = self.window_service.get_trading_window()
            if window is None:
                raise Exception("切换价格模式时窗口消失")

            label = self.window_service.find_element_in_window(window, CONTROL_ID_PRICE_TYPE)
            if label is None:
                self.logger.warning(f"未找到价格类型标签 control_id={CONTROL_ID_PRICE_TYPE}")
                break

            current_name = label.window_text() or ""
            is_market = MARKET_KEYWORD in current_name

            if want_market == is_market:
                # 已是目标模式
                break

            self.logger.info(
                f"点击 {CONTROL_ID_PRICE_TYPE} 切换价格模式 "
                f"(尝试 {attempt + 1}/3), 当前: {current_name}"
            )
            label.click_input()
            time.sleep(0.4)

            # 重新获取窗口读取最新状态
            window = self.window_service.get_trading_window()
            if window is not None:
                label2 = self.window_service.find_element_in_window(window, CONTROL_ID_PRICE_TYPE)
                if label2 is not None:
                    new_name = label2.window_text() or ""
                    is_market = MARKET_KEYWORD in new_name
                    if want_market == is_market:
                        break

        if want_market != is_market:
            mode_text = "市价" if want_market else "限价"
            raise Exception(f"切换 {mode_text} 模式失败（已重试 3 次）")

        return is_market

    def confirm_order(self) -> dict:
        """单独发送 Y 键确认委托（用于 confirm=false 的下单后续确认）"""
        self.logger.info("发送 Y 键确认委托")
        self.window_service.send_key("Y")
        time.sleep(0.5)
        return {"confirmed": True}
