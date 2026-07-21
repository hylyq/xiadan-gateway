"""持仓/资金/成交查询服务

资金余额: control_id 批量读取（无需 OCR）
持仓/今日成交: F4 + Ctrl+C 剪切板法 + OCR 验证码兜底
"""
import os
import time
from typing import Optional

from src.constants import (
    BALANCE_FIELDS,
    CAPTCHA_IMAGE_ID, CAPTCHA_INPUT_ID, CAPTCHA_OK_BUTTON_ID,
    CAPTCHA_CANCEL_BUTTON_ID, CAPTCHA_VERIFY_ID, CAPTCHA_TEXT_KEYWORDS
)
from src.models.config import AppConfig
from src.services.window_service import WindowService
from src.utils.diagnostic import DiagnosticUtil
from src.utils.logger import Logger


class PositionService:
    """持仓/资金/成交查询服务"""

    def __init__(self, window_service: WindowService, ocr_service=None):
        self.window_service = window_service
        self.ocr_service = ocr_service  # 注入 OCR 服务（避免循环依赖）
        self.config = AppConfig()
        self.logger = Logger.get_instance()
        self._cached_window = None  # 用于窗口引用刷新

    def _send_ctrl_c(self):
        """click_input 激活 → 前台发送 Ctrl+C（keybd_event）

        keybd_event 把按键发到当前前台窗口。必须确保交易窗口在后台前，
        否则 Ctrl+C 会发到错误窗口（IDE/浏览器），既不复制数据也不触发验证码弹窗。

        click_input 是激活窗口最可靠的方式，配合句柄级校验确保目标正确。
        SetForegroundWindow 因 Windows UIPI 限制可能静默失败，不使用。
        """
        import win32api
        import win32con
        import win32gui

        self._refresh_window_ref()
        window = self._cached_window
        if window is None:
            raise Exception("交易窗口未找到，无法发送 Ctrl+C")

        target_handle = window.handle
        for _ in range(2):
            window.click_input()
            time.sleep(0.3)
            if win32gui.GetForegroundWindow() == target_handle:
                break
            self.logger.warning(f"激活后前台句柄 {win32gui.GetForegroundWindow():#x} ≠ 目标 {target_handle:#x}，重试")

        if win32gui.GetForegroundWindow() != target_handle:
            self.logger.error(
                f"无法将交易窗口带到前台，前台={win32gui.GetForegroundWindow():#x} 目标={target_handle:#x}"
            )
            raise Exception("无法将交易窗口带到前台，放弃发送 Ctrl+C 避免按键泄漏")

        VK_CONTROL = 0x11
        VK_C = 0x43
        win32api.keybd_event(VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(VK_C, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(VK_C, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.3)

    def _is_valid_table_data(self, data: str) -> bool:
        """校验剪贴板内容是否为有效的表格数据

        同花顺 Ctrl+C 复制的表格数据特征：
        - 表头行 + 至少一行数据行（用 \\r\\n 分隔）
        - 每行用 \\t 分隔列
        - 至少 2 行（表头 + 数据），每行都含 \\t

        仅校验含 \\t 不足以区分——普通文本也可能含 \\t。
        """
        if not data:
            return False
        lines = data.strip().split("\r\n")
        if len(lines) < 2:
            return False
        # 每行都必须含制表符（列分隔符）
        return all("\t" in line for line in lines if line.strip())

    def _copy_table_via_clipboard(self) -> list:
        """通过剪贴板读取表格数据（含验证码处理）

        流程：Ctrl+C → 验证码弹窗 → OCR 识别 → 读剪贴板，最多 3 次。

        Ctrl+C 必然触发验证码弹窗。验证码弹窗的出现 = Ctrl+C 已正确发送到
        目标窗口的确认信号。无验证码弹窗 = Ctrl+C 未到达目标窗口，Ctrl+C 失败。
        """
        for attempt in range(3):
            self.logger.info(f"第 {attempt + 1}/3 次尝试 Ctrl+C 读取表格数据")

            self._send_ctrl_c()
            time.sleep(1.0)  # 等待验证码弹窗异步弹出

            self._refresh_window_ref()
            window = self._cached_window

            # Ctrl+C 必然触发验证码，无验证码 = Ctrl+C 未到达目标窗口
            if not self._detect_captcha(window):
                self.logger.warning(f"第 {attempt + 1} 次 Ctrl+C 后未检测到验证码弹窗，可能焦点丢失")
                continue

            if not self._solve_captcha(window):
                self.logger.warning(f"第 {attempt + 1} 次验证码处理失败")
                continue

            data = self.window_service.get_clipboard()
            if self._is_valid_table_data(data):
                self.logger.info(f"第 {attempt + 1} 次成功获取表格数据")
                return self._format_table_data(data)

            self.logger.warning(
                f"第 {attempt + 1} 次剪贴板内容无效: {repr(data[:100]) if data else '空'}"
            )

        # 所有尝试失败，截图诊断
        DiagnosticUtil().snapshot("query_empty_clipboard")
        return []

    def _refresh_window_ref(self):
        """刷新缓存的窗口引用（解决 pywinauto descendants 缓存问题）"""
        try:
            fresh = self.window_service.get_trading_window()
            if fresh is not None:
                self._cached_window = fresh
                self.logger.info("已刷新窗口引用")
        except Exception as e:
            self.logger.warning(f"刷新窗口引用失败: {e}")

    def _detect_captcha(self, window) -> bool:
        """检测验证码弹窗（control_id + 文本匹配双重检测）

        Args:
            window: 窗口对象（若为 None 则先刷新）

        Returns:
            是否检测到验证码弹窗
        """
        if window is None:
            self._refresh_window_ref()
            window = self._cached_window
        if window is None:
            return False

        # 方法1: control_id=2405 检测
        try:
            image = self.window_service.find_element_in_window(window, CAPTCHA_IMAGE_ID)
            if image is not None:
                self.logger.info("通过 control_id=2405 检测到验证码弹窗")
                return True
        except Exception as e:
            self.logger.warning(f"control_id 检测验证码失败: {e}")

        # 方法2: 文本匹配（关键词检测）
        try:
            matches = self.window_service.find_element_by_text(
                window, CAPTCHA_TEXT_KEYWORDS
            )
            if matches:
                self.logger.info(f"通过文本匹配检测到验证码弹窗（{len(matches)} 个匹配控件）")
                return True
        except Exception as e:
            self.logger.warning(f"文本匹配检测验证码失败: {e}")

        self.logger.info("未检测到验证码弹窗（control_id 和文本匹配均未命中）")
        return False

    def _dismiss_popup_if_present(self, window) -> bool:
        """检测并关闭常见提示弹窗（如非交易时段的 'Begin failed!' 提示）

        同花顺在非交易时段查询时可能弹出提示窗（标题"提示"，内容如 "Begin failed!"），
        阻挡表格数据的复制。此方法检测并关闭这类弹窗。

        Returns:
            是否关闭了弹窗
        """
        if window is None:
            return False
        try:
            # 检测弹窗特征：查找"确定"按钮（标准对话框 cid=1 或 cid=2）
            popup_keywords = ["Begin failed", "failed", "提示"]
            for ctrl in window.descendants():
                try:
                    text = ctrl.window_text() or ""
                    if any(kw in text for kw in popup_keywords):
                        self.logger.info(f"检测到提示弹窗: {text[:80]}，尝试关闭")
                        # 尝试点击"确定"按钮
                        for btn_id in (1, 2):  # IDOK=1, IDCANCEL=2
                            btn = self.window_service.find_element_in_window(window, btn_id)
                            if btn is not None:
                                btn.click_input()
                                self.logger.info(f"已点击按钮 cid={btn_id} 关闭弹窗")
                                time.sleep(0.5)
                                return True
                        # 找不到按钮则用 ENTER 关闭
                        self.window_service.send_key("{ENTER}")
                        time.sleep(0.5)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    # ------------------------------------------------------------
    # 查询面板准备（所有查询的公共前置步骤）
    # ------------------------------------------------------------

    def _prepare_query_panel(self):
        """激活窗口 → ESC×3 → F4 打开查询面板

        标准查询流程：确保从 F1 买入界面切换到查询面板，F4 永远是「打开」动作。
        每个 keybd_event 都带 click_input 激活，确保窗口稳态在前台。
        """
        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("未找到交易窗口 '网上股票交易系统5.0'")
        window.click_input()
        time.sleep(0.3)

        for _ in range(3):
            self.window_service.send_key("ESC")
            time.sleep(0.1)

        self.window_service.send_key("F4")
        time.sleep(0.5)

    # ------------------------------------------------------------
    # 资金余额（control_id 批量读取，无需 OCR）
    # ------------------------------------------------------------

    def get_balance(self) -> dict:
        """获取资金余额"""
        self.logger.info("开始获取资金余额")
        self._prepare_query_panel()

        # 批量获取所有字段
        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("未找到交易窗口 '网上股票交易系统5.0'")
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
        self._prepare_query_panel()

        # F4 打开后默认就是"资金股票"页面，无需额外导航
        return self._copy_table_via_clipboard()

    # ------------------------------------------------------------
    # 今日成交（树形菜单 + Ctrl+C + OCR 兜底）
    # ------------------------------------------------------------

    def get_today_trades(self) -> list:
        """获取今日成交"""
        self.logger.info("开始获取今日成交")
        self._prepare_query_panel()

        self._refresh_window_ref()
        window = self._cached_window
        self._navigate_to_query_page(window, "当日成交")

        return self._copy_table_via_clipboard()

    # ------------------------------------------------------------
    # 当日委托（树形菜单 + Ctrl+C + OCR 兜底）
    # ------------------------------------------------------------

    def get_today_orders(self) -> list:
        """获取当日委托

        返回字段：时间、委托号、证券代码、证券名称、操作、委托价格、委托数量、
                  成交数量、撤单数量、状态、交易市场
        """
        self.logger.info("开始获取当日委托")
        self._prepare_query_panel()

        self._refresh_window_ref()
        window = self._cached_window
        self._navigate_to_query_page(window, "当日委托")

        return self._copy_table_via_clipboard()

    # ------------------------------------------------------------
    # OCR 验证码处理
    # ------------------------------------------------------------

    def _solve_captcha(self, window) -> bool:
        """处理验证码弹窗（OCR 识别 + 输入 + 确定）

        仅负责验证码的 OCR 识别和输入提交，不获取表格数据。
        数据获取由 _copy_table_via_clipboard 的循环统一处理，避免递归。

        Args:
            window: 窗口对象

        Returns:
            True=验证码处理成功，False=失败
        """
        if self.ocr_service is None:
            self.logger.error("OCR 服务未初始化，无法处理验证码")
            return False

        if window is None:
            self.logger.error("窗口为 None，无法处理验证码")
            return False

        # 查找验证码图片控件
        image_element = self.window_service.find_element_in_window(window, CAPTCHA_IMAGE_ID)
        if image_element is None:
            matches = self.window_service.find_element_by_text(window, CAPTCHA_TEXT_KEYWORDS)
            if matches:
                self.logger.info(f"通过文本匹配找到验证码控件（{len(matches)} 个）")
                image_element = matches[0]
            else:
                self.logger.error("验证码图片元素未找到")
                return False

        # 保存验证码图片
        cache_dir = "logs/screenshots"
        os.makedirs(cache_dir, exist_ok=True)
        image_path = os.path.join(cache_dir, "captcha.png")
        image_element.capture_as_image().save(image_path)
        self.logger.info(f"验证码图片已保存: {image_path}")

        max_retry = self.config.get_ocr_config().get("max_retry", 3)

        for attempt in range(max_retry):
            try:
                ocr_text = self.ocr_service.recognize(image_path)
                if not ocr_text:
                    self.logger.warning(f"OCR 识别为空（尝试 {attempt + 1}/{max_retry}）")
                    continue

                self.logger.info(f"OCR 识别结果: {ocr_text}")

                # 输入验证码 + 点击确定
                self.window_service.input_text_to_element(window, CAPTCHA_INPUT_ID, ocr_text)
                if not self._click_button(window, CAPTCHA_OK_BUTTON_ID):
                    self.logger.warning("未找到验证码确定按钮")
                    continue

                time.sleep(0.3)

                # 刷新窗口验证结果
                self._refresh_window_ref()
                window = self._cached_window

                if self._verify_captcha_success(window):
                    self.logger.info("验证码验证成功")
                    return True

                # 失败：点击取消，重新识别
                self.logger.warning(f"验证码错误（尝试 {attempt + 1}/{max_retry}）")
                self._click_button(window, CAPTCHA_CANCEL_BUTTON_ID)
                time.sleep(0.2)

                # 重新获取验证码图片
                self._refresh_window_ref()
                window = self._cached_window
                image_element = self.window_service.find_element_in_window(window, CAPTCHA_IMAGE_ID)
                if image_element is None:
                    # 弹窗已关闭，视为成功
                    return True
                image_element.capture_as_image().save(image_path)

            except Exception as e:
                self.logger.error(f"验证码处理异常: {e}")
                try:
                    self._click_button(window, CAPTCHA_CANCEL_BUTTON_ID)
                except Exception:
                    pass

        self.logger.warning(f"验证码处理失败，已达到最大重试次数 {max_retry}")
        return False

    def _click_button(self, window, control_id: int) -> bool:
        """点击按钮"""
        button = self.window_service.find_element_in_window(window, control_id)
        if button:
            button.click()
            return True
        return False

    def _verify_captcha_success(self, window) -> bool:
        """验证验证码是否成功（输入框消失=成功）"""
        input_element = self.window_service.find_element_in_window(window, CAPTCHA_VERIFY_ID)
        return input_element is None

    # ------------------------------------------------------------
    # 数据格式化
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # 树形菜单导航兜底
    # ------------------------------------------------------------

    def _navigate_to_query_page(self, window, page_name: str) -> None:
        """导航到查询页面（树形菜单 + 兜底）

        流程:
        1. 树形菜单文本导航（find_element_by_tree_path）
        2. 兜底: 全量扫描 TreeItem 按文本匹配（不依赖父节点）

        所有标签页切换都会触发服务器数据交换，可能弹"Begin failed!"提示弹窗，
        因此每个策略成功后都调用 _dismiss_popup_if_present 检测并关闭弹窗。

        Args:
            window: 交易窗口
            page_name: 目标页面名称，如 "当日成交"、"当日委托"

        Raises:
            Exception: 所有策略均失败
        """
        parent_name = "查询[F4]"

        # 策略1: 树形菜单文本导航
        button = self.window_service.find_element_by_tree_path(
            window, ('control_type', 'Tree'), [parent_name, page_name]
        )
        if button is not None:
            button.click_input()
            self.logger.info(f"已通过树形路径点击 '{page_name}'")
            time.sleep(0.3)
            self._dismiss_popup_if_present(window)
            return

        self.logger.warning(f"树形路径导航失败，尝试扫描 TreeItem 文本匹配: {page_name}")

        # 策略2: 全量扫描 TreeItem 按文本匹配
        try:
            for el in window.descendants():
                try:
                    if el.element_info.control_type == "TreeItem":
                        text = el.window_text() or ""
                        if page_name in text:
                            el.click_input()
                            self.logger.info(f"已通过 TreeItem 文本匹配点击 '{page_name}'")
                            time.sleep(0.3)
                            self._dismiss_popup_if_present(window)
                            return
                except Exception:
                    continue
        except Exception as e:
            self.logger.warning(f"TreeItem 扫描失败: {str(e)}")

        raise Exception(
            f"导航到 '{page_name}' 失败：树形路径和 TreeItem 扫描均未找到目标页面"
        )

    def _format_table_data(self, table_data: Optional[str]) -> list:
        """将 tab 分隔的表格文本转为 JSON 列表

        同花顺 Ctrl+C 复制出来的数据格式:
            表头1\t表头2\t表头3
            数据1\t数据2\t数据3

        自动过滤空占位行（关键字段全为空的行）。
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
            row = {headers[i]: values[i] for i in range(len(headers))}
            # 过滤空占位行：关键字段全为空则跳过
            key_fields = ["证券代码", "委托时间", "时间", "代码"]
            if any(row.get(k, "").strip() for k in key_fields):
                result.append(row)

        return result
