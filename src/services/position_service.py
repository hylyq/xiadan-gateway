"""持仓/资金/成交查询服务

资金余额: control_id 批量读取（无需 OCR）
持仓/今日成交: F4 + Ctrl+C 剪切板法 + OCR 验证码兜底
"""
import os
import time
from typing import Optional

from src.constants import (
    BALANCE_FIELDS, TABLE_CONTENT_AREA_ID,
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

    @staticmethod
    def _send_ctrl_c():
        """前台发送 Ctrl+C（keybd_event，不依赖 PostMessage）"""
        import win32api
        import win32con
        VK_CONTROL = 0x11
        VK_C = 0x43
        win32api.keybd_event(VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(VK_C, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(VK_C, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.3)

    def _copy_table_via_clipboard(self, window) -> list:
        """通过剪贴板读取表格数据（含验证码弹窗检测与处理）

        流程：click_input 激活窗口 → keybd_event Ctrl+C → 读剪贴板
        PostMessage 无法可靠触发剪贴板（需要窗口焦点和系统输入队列），
        因此用 keybd_event 前台发送，配合 click_input 将窗口带到前台。

        验证码弹窗检测：先尝试 control_id=2405，失败后回退到文本匹配
        （"验证码"/"检测到"），避免因控件 ID 变化或窗口缓存导致漏检。

        若 Ctrl+C 触发验证码弹窗，自动进行 OCR 识别处理。
        """
        # 1. 激活窗口到前台（必须成功，否则 keybd_event 会发到错误窗口）
        try:
            window.click_input()
        except Exception as e:
            self.logger.warning(f"直接 click_input 失败: {e}，尝试通过 get_trading_window 重新获取")
            try:
                fresh_window = self.window_service.get_trading_window()
                if fresh_window is not None:
                    fresh_window.click_input()
                    window = fresh_window  # 更新引用
                else:
                    raise Exception("重新获取窗口也失败")
            except Exception as e2:
                self.logger.warning(f"备用窗口激活也失败，Ctrl+C 可能发到错误窗口: {e2}")
        time.sleep(0.3)

        # 2. 点击表格内容区域聚焦控件
        try:
            self.window_service.click_element(window, TABLE_CONTENT_AREA_ID)
        except Exception as e:
            self.logger.warning(f"点击内容区域(1047)失败: {e}")
        time.sleep(0.3)

        # 2.5 检测并关闭提示弹窗（如非交易时段的 "Begin failed!"）
        self._dismiss_popup_if_present(window)

        # 3. 检测是否有验证码弹窗（混合检测：control_id + 文本匹配）
        if self._detect_captcha(window):
            self.logger.info("检测到验证码弹窗（control_id 2405 或文本匹配），启动 OCR 处理")
            return self._handle_captcha_and_get_data(None)  # None 触发内部窗口刷新

        # 4. 无验证码：keybd_event Ctrl+C
        self._send_ctrl_c()
        data = self.window_service.get_clipboard()
        if data:
            self.logger.info(f"剪贴板读取成功，长度={len(data)}，前100字符: {repr(data[:100])}")
            # Ctrl+C 后可能触发了验证码（同花顺的安全机制异步弹出）
            self._refresh_window_ref()
            window = self._cached_window
            if self._detect_captcha(window):
                self.logger.info("Ctrl+C 后检测到验证码弹窗，启动 OCR 处理")
                return self._handle_captcha_and_get_data(None)
            return self._format_table_data(data)

        # 5. 重试一次（可能验证码在 Ctrl+C 后才弹出）
        self.logger.warning("剪贴板读取为空，重试...")

        # 重新获取窗口（刷新 pywinauto 控件树缓存）
        self._refresh_window_ref()
        window = self._cached_window

        # 再次检查验证码（混合检测）
        if self._detect_captcha(window):
            self.logger.info("重试时检测到验证码弹窗，启动 OCR 处理")
            return self._handle_captcha_and_get_data(None)

        self._send_ctrl_c()
        data = self.window_service.get_clipboard()
        if data:
            return self._format_table_data(data)

        # 剪贴板始终为空，截图诊断记录当前窗口状态
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
    # 资金余额（control_id 批量读取，无需 OCR）
    # ------------------------------------------------------------

    def get_balance(self) -> dict:
        """获取资金余额"""
        self.logger.info("开始获取资金余额")
        self._activate_trading_window()
        window = self._get_focused_window()

        # 确保从主界面开始（关闭可能已打开的查询面板）
        self.window_service.send_key("F4")
        time.sleep(0.3)

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

    def _ensure_query_panel_open(self, window) -> None:
        """确保查询面板已打开（F4 是切换键，不能盲按两次）

        通过检测窗口中是否存在查询面板的树形菜单项（如"资金股票"、"当日委托"）
        来判断面板状态。若未打开则按 F4 打开。
        """
        query_markers = ["资金股票", "当日成交", "当日委托", "历史成交"]
        try:
            for ctrl in window.descendants():
                try:
                    text = ctrl.window_text() or ""
                    if any(marker in text for marker in query_markers):
                        self.logger.info("查询面板已打开，无需按 F4")
                        return
                except Exception:
                    continue
        except Exception:
            pass

        # 查询面板未打开，按 F4 打开
        self.logger.info("查询面板未打开，按 F4 打开")
        self.window_service.send_key("F4")
        time.sleep(0.5)

    # ------------------------------------------------------------
    # 持仓查询（F4 + Ctrl+C + 剪切板 + OCR 兜底）
    # ------------------------------------------------------------

    def get_position(self) -> list:
        """获取当前持仓"""
        self.logger.info("开始获取持仓")
        self._activate_trading_window()
        window = self._get_focused_window()

        # 确保查询面板已打开（检测状态，不盲按 F4）
        self._ensure_query_panel_open(window)
        self.window_service.send_key("F5")
        time.sleep(0.3)

        # 界面切换后重新获取窗口（避免缓存控件树）
        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("切换查询界面后窗口消失")

        # 通过剪贴板读取表格数据
        return self._copy_table_via_clipboard(window)

    # ------------------------------------------------------------
    # 今日成交（树形菜单 + Ctrl+C + OCR 兜底）
    # ------------------------------------------------------------

    def get_today_trades(self) -> list:
        """获取今日成交"""
        self.logger.info("开始获取今日成交")
        self._activate_trading_window()
        window = self._get_focused_window()

        # 确保查询面板已打开
        self._ensure_query_panel_open(window)

        # 导航到"当日成交"页面（树形菜单 + 键盘兜底）
        self._navigate_to_query_page(window, "当日成交")

        # 导航后重新获取窗口（避免缓存控件树）
        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("页面导航后窗口消失")

        # 刷新
        self.window_service.send_key("F5")
        time.sleep(0.3)

        # 通过剪贴板读取表格数据
        return self._copy_table_via_clipboard(window)

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

        # 确保查询面板已打开
        self._ensure_query_panel_open(window)

        # 导航到"当日委托"页面（树形菜单 + 键盘兜底）
        self._navigate_to_query_page(window, "当日委托")

        # 导航后重新获取窗口（避免缓存控件树）
        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("页面导航后窗口消失")

        # 刷新
        self.window_service.send_key("F5")
        time.sleep(0.3)

        # 通过剪贴板读取表格数据
        return self._copy_table_via_clipboard(window)

    # ------------------------------------------------------------
    # OCR 验证码处理
    # ------------------------------------------------------------

    def _handle_captcha_and_get_data(self, window) -> list:
        """处理验证码弹窗并获取数据

        Args:
            window: 窗口对象，传递 None 时自动刷新窗口引用

        流程:
        1. 截图保存验证码图片
        2. OCR 识别（最多重试 max_retry 次）
        3. 输入验证码 + 点击确定
        4. 验证成功后重新 Ctrl+C → 读剪切板
        5. 失败则点击取消并抛异常
        """
        if self.ocr_service is None:
            raise Exception("OCR 服务未初始化，无法处理验证码")

        # 若 window 为 None，自动刷新
        if window is None:
            self._refresh_window_ref()
            window = self._cached_window
        if window is None:
            raise Exception("交易窗口未找到，无法处理验证码")

        # 查找验证码图片控件
        image_element = self.window_service.find_element_in_window(window, CAPTCHA_IMAGE_ID)
        if image_element is None:
            # control_id 未找到，尝试文本匹配
            matches = self.window_service.find_element_by_text(
                window, CAPTCHA_TEXT_KEYWORDS
            )
            if matches:
                self.logger.info(f"通过文本匹配找到验证码相关控件（{len(matches)} 个），使用第一个")
                image_element = matches[0]
            else:
                raise Exception("验证码图片元素未找到（control_id 和文本匹配均失败）")

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
                self.window_service.input_text_to_element(window, CAPTCHA_INPUT_ID, ocr_text)

                # 点击确定
                if not self._click_button(window, CAPTCHA_OK_BUTTON_ID):
                    raise Exception("未找到验证码确定按钮")

                time.sleep(0.3)

                # 刷新窗口后验证验证码结果
                try:
                    fresh = self.window_service.get_trading_window()
                    if fresh is not None:
                        window = fresh
                except Exception:
                    pass

                # 验证是否成功（输入框消失表示成功）
                if self._verify_captcha_success(window):
                    # 验证码已输入成功，重新 Ctrl+C 复制表格数据
                    self.logger.info("验证码通过，重新 Ctrl+C 复制表格数据")
                    try:
                        window.click_input()
                    except Exception:
                        pass
                    time.sleep(0.2)
                    self._send_ctrl_c()
                    data = self.window_service.get_clipboard()
                    return self._format_table_data(data) if data else []

                # 失败：点击取消，重新识别
                self.logger.warning(f"验证码错误（尝试 {attempt + 1}/{max_retry}）")
                self._click_button(window, CAPTCHA_CANCEL_BUTTON_ID)
                time.sleep(0.2)

                # 重新获取窗口（刷新控件树缓存——弹窗状态已变）
                try:
                    fresh = self.window_service.get_trading_window()
                    if fresh is not None:
                        window = fresh
                        self.logger.info("验证码重试：已刷新窗口控件树")
                        time.sleep(0.2)
                except Exception as e:
                    self.logger.warning(f"验证码重试时刷新窗口失败: {e}")

                # 重新获取验证码图片
                image_element = self.window_service.find_element_in_window(window, CAPTCHA_IMAGE_ID)
                if image_element is None:
                    # 弹窗已关闭，可能是验证成功
                    data = self.window_service.get_clipboard()
                    return self._format_table_data(data) if data else []
                image_element.capture_as_image().save(image_path)

            except Exception as e:
                self.logger.error(f"验证码处理异常: {str(e)}")
                try:
                    self._click_button(window, CAPTCHA_CANCEL_BUTTON_ID)
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
        input_element = self.window_service.find_element_in_window(window, CAPTCHA_VERIFY_ID)
        return input_element is None

    # ------------------------------------------------------------
    # 数据格式化
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # 树形菜单导航兜底
    # ------------------------------------------------------------

    def _navigate_to_query_page(self, window, page_name: str) -> None:
        """导航到查询页面（树形菜单 + 多层兜底）

        流程:
        1. 树形菜单文本导航（find_element_by_tree_path）
        2. 兜底1: 全量扫描 TreeItem 按文本匹配（不依赖父节点）
        3. 兜底2: 键盘导航（TAB 聚焦树 → DOWN 选目标 → ENTER 展开/确认）

        Args:
            window: 交易窗口
            page_name: 目标页面名称，如 "当日成交"、"当日委托"

        Raises:
            Exception: 所有兜底策略均失败
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
                            return
                except Exception:
                    continue
        except Exception as e:
            self.logger.warning(f"TreeItem 扫描失败: {str(e)}")

        self.logger.warning(f"文本匹配失败，尝试键盘导航兜底: {page_name}")

        # 策略3: 键盘导航兜底
        # 先 TAB 切换到树控件，再 UP/DOWN 导航
        for _ in range(5):
            self.window_service.send_key("{TAB}")
            time.sleep(0.15)

        # 尝试找到目标页面（通常在同花顺树中是第2-3项）
        for _ in range(4):
            self.window_service.send_key("{DOWN}")
            time.sleep(0.15)

        # 尝试 ENTER 展开/确认
        self.window_service.send_key("{ENTER}")
        time.sleep(0.3)
        self.logger.info(f"已通过键盘导航尝试选中 '{page_name}'")

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
