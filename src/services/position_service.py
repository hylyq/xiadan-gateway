"""持仓/资金/成交查询服务

资金余额: control_id 批量读取（无需 OCR）
持仓/今日成交: F4 + Ctrl+C 剪切板法 + OCR 验证码兜底
"""
import os
import time
from typing import Optional

from src.models.config import AppConfig
from src.services.window_service import WindowService
from src.utils.logger import Logger


# 资金余额字段对应的 control_id
BALANCE_FIELDS = {
    "资金余额": 1012,
    "冻结金额": 1013,
    "可用金额": 1016,
    "可取金额": 1017,
    "股票市值": 1014,
    "总资产": 1015,
    "持仓盈亏": 1027,
    "当日盈亏": 1026,
    "当日盈亏比": 1029,
}


class PositionService:
    """持仓/资金/成交查询服务"""

    def __init__(self, window_service: WindowService, ocr_service=None):
        self.window_service = window_service
        self.ocr_service = ocr_service  # 注入 OCR 服务（避免循环依赖）
        self.config = AppConfig()
        self.logger = Logger.get_instance()

    # ------------------------------------------------------------
    # 激活窗口（所有查询的公共前置步骤）
    # ------------------------------------------------------------

    def _activate_trading_window(self) -> None:
        """激活 xiadan.exe 窗口（不抢焦点，配合 PostMessage 后台按键）"""
        trading_path = self.config.get_trading_app_path()
        if not trading_path:
            raise Exception("未配置 xiadan.exe 路径，请检查 config/app_config.json")
        try:
            self.window_service.activate_window(trading_path, foreground=False)
        except Exception as e:
            raise Exception(
                f"激活窗口失败，请检查 xiadan.exe 是否已启动（不要进入精简模式）: {str(e)}"
            )

    def _get_focused_window(self):
        """获取交易窗口并点击聚焦（否则快捷键会失效）"""
        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("未找到交易窗口 '网上股票交易系统5.0'")
        window.click_input()
        time.sleep(0.3)
        return window

    # ------------------------------------------------------------
    # 资金余额（control_id 批量读取，无需 OCR）
    # ------------------------------------------------------------

    def get_balance(self) -> dict:
        """获取资金余额"""
        self.logger.info("开始获取资金余额")
        self._activate_trading_window()
        window = self._get_focused_window()

        # 刷新数据
        self.window_service.send_key("F5")
        time.sleep(0.3)
        self.window_service.send_key("F4")
        time.sleep(0.2)

        # 批量获取所有字段
        control_ids = list(BALANCE_FIELDS.values())
        elements = self.window_service.find_element_in_window(window, control_ids)

        result = {}
        for field_name, control_id in BALANCE_FIELDS.items():
            element = next((e for e in elements if e.control_id() == control_id), None)
            if element:
                result[field_name] = element.window_text()
            else:
                result[field_name] = None
                self.logger.warning(f"未找到 {field_name} 对应的控件 control_id={control_id}")

        self.logger.info(f"资金余额查询完成: {result}")
        return result

    # ------------------------------------------------------------
    # 持仓查询（F4 + Ctrl+C + 剪切板 + OCR 兜底）
    # ------------------------------------------------------------

    def get_position(self) -> list:
        """获取当前持仓"""
        self.logger.info("开始获取持仓")
        self._activate_trading_window()
        window = self._get_focused_window()

        # 刷新 + 切换到持仓界面
        self.window_service.send_key("F5")
        time.sleep(0.3)
        self.window_service.send_key("F4")
        time.sleep(0.2)

        # 点击内容区域 + 复制
        self.window_service.click_element(window, 1047)
        time.sleep(0.1)
        self.window_service.send_key("{CTRL+C}")

        # 检查是否有验证码弹窗
        image_element = self.window_service.find_element_in_window(window, 2405)
        if image_element is None:
            # 无验证码，直接读剪切板
            data = self.window_service.get_clipboard()
            return self._format_table_data(data)

        # 有验证码：OCR 识别
        return self._handle_captcha_and_get_data(window)

    # ------------------------------------------------------------
    # 今日成交（树形菜单 + Ctrl+C + OCR 兜底）
    # ------------------------------------------------------------

    def get_today_trades(self) -> list:
        """获取今日成交"""
        self.logger.info("开始获取今日成交")
        self._activate_trading_window()
        window = self._get_focused_window()

        # F4 进入查询界面
        self.window_service.send_key("F4")
        time.sleep(0.1)

        # 树形菜单导航: 按 control_type='Tree' 找根 -> "查询[F4]" -> "当日成交"
        button = self.window_service.find_element_by_tree_path(
            window, ('control_type', 'Tree'), ["查询[F4]", "当日成交"]
        )
        if button is None:
            raise Exception("未找到 '当日成交' 按钮")
        button.click_input()
        self.logger.info("已点击 '当日成交' 按钮")
        time.sleep(0.3)

        # 刷新
        self.window_service.send_key("F5")
        time.sleep(0.1)

        # 点击内容区域 + 复制
        self.window_service.click_element(window, 1047)
        time.sleep(0.1)
        self.window_service.send_key("{CTRL+C}")

        # 检查验证码
        image_element = self.window_service.find_element_in_window(window, 2405)
        if image_element is None:
            data = self.window_service.get_clipboard()
            return self._format_table_data(data)

        return self._handle_captcha_and_get_data(window)

    # ------------------------------------------------------------
    # 当日委托（树形菜单 + Ctrl+C + OCR 兜底）
    # ------------------------------------------------------------

    def get_today_orders(self) -> list:
        """获取当日委托

        返回字段：时间、委托号、证券代码、证券名称、操作、委托价格、委托数量、
                  成交数量、撤单数量、状态、交易市场
        """
        self.logger.info("开始获取当日委托")
        self._activate_trading_window()
        window = self._get_focused_window()

        # F4 进入查询界面
        self.window_service.send_key("F4")
        time.sleep(0.1)

        # 树形菜单导航 -> "查询[F4]" -> "当日委托"
        button = self.window_service.find_element_by_tree_path(
            window, ('control_type', 'Tree'), ["查询[F4]", "当日委托"]
        )
        if button is None:
            raise Exception("未找到 '当日委托' 按钮")
        button.click_input()
        self.logger.info("已点击 '当日委托' 按钮")
        time.sleep(0.3)

        # 刷新
        self.window_service.send_key("F5")
        time.sleep(0.1)

        # 点击内容区域 + 复制
        self.window_service.click_element(window, 1047)
        time.sleep(0.1)
        self.window_service.send_key("{CTRL+C}")

        # 检查验证码
        image_element = self.window_service.find_element_in_window(window, 2405)
        if image_element is None:
            data = self.window_service.get_clipboard()
            return self._format_table_data(data)

        return self._handle_captcha_and_get_data(window)

    # ------------------------------------------------------------
    # OCR 验证码处理
    # ------------------------------------------------------------

    def _handle_captcha_and_get_data(self, window) -> list:
        """处理验证码弹窗并获取数据

        流程:
        1. 截图保存验证码图片
        2. OCR 识别（最多重试 max_retry 次）
        3. 输入验证码 + 点击确定
        4. 验证成功后读剪切板
        5. 失败则点击取消并抛异常
        """
        if self.ocr_service is None:
            raise Exception("OCR 服务未初始化，无法处理验证码")

        image_element = self.window_service.find_element_in_window(window, 2405)
        if image_element is None:
            raise Exception("验证码图片元素消失")

        # 保存验证码图片
        cache_dir = "logs/screenshots"
        os.makedirs(cache_dir, exist_ok=True)
        image_path = os.path.join(cache_dir, "captcha.png")
        image_element.capture_as_image().save(image_path)
        self.logger.info(f"验证码图片已保存: {image_path}")

        max_retry = self.config.get_ocr_config().get("max_retry", 3)

        for attempt in range(max_retry):
            try:
                # OCR 识别
                ocr_text = self.ocr_service.recognize(image_path)
                if not ocr_text:
                    self.logger.warning(f"OCR 识别为空（尝试 {attempt + 1}/{max_retry}）")
                    continue

                self.logger.info(f"OCR 识别结果: {ocr_text}")

                # 输入验证码
                self.window_service.input_text_to_element(window, 2404, ocr_text)

                # 点击确定
                if not self._click_button(window, 1):
                    raise Exception("未找到验证码确定按钮")

                time.sleep(0.3)

                # 验证是否成功（输入框消失表示成功）
                if self._verify_captcha_success(window):
                    data = self.window_service.get_clipboard()
                    return self._format_table_data(data)

                # 失败：点击取消，重新识别
                self.logger.warning(f"验证码错误（尝试 {attempt + 1}/{max_retry}）")
                self._click_button(window, 2)
                time.sleep(0.2)

                # 重新获取验证码图片
                image_element = self.window_service.find_element_in_window(window, 2405)
                if image_element is None:
                    # 弹窗已关闭，可能是验证成功
                    data = self.window_service.get_clipboard()
                    return self._format_table_data(data)
                image_element.capture_as_image().save(image_path)

            except Exception as e:
                self.logger.error(f"验证码处理异常: {str(e)}")
                # 尝试点击取消
                try:
                    self._click_button(window, 2)
                except Exception:
                    pass

        raise Exception("OCR 验证码识别失败，已达到最大重试次数")

    def _click_button(self, window, control_id: int) -> bool:
        """点击按钮"""
        button = self.window_service.find_element_in_window(window, control_id)
        if button:
            button.click()
            return True
        return False

    def _verify_captcha_success(self, window) -> bool:
        """验证验证码是否成功（输入框消失=成功）"""
        input_element = self.window_service.find_element_in_window(window, 2406)
        return input_element is None

    # ------------------------------------------------------------
    # 数据格式化
    # ------------------------------------------------------------

    def _format_table_data(self, table_data: Optional[str]) -> list:
        """将 tab 分隔的表格文本转为 JSON 列表

        同花顺 Ctrl+C 复制出来的数据格式:
            表头1\t表头2\t表头3
            数据1\t数据2\t数据3
        """
        if not table_data:
            return []

        lines = table_data.splitlines()
        if len(lines) < 2:
            return []

        headers = lines[0].split("\t")
        result = []
        for line in lines[1:]:
            values = line.split("\t")
            if len(values) != len(headers):
                continue
            result.append({headers[i]: values[i] for i in range(len(headers))})

        return result
