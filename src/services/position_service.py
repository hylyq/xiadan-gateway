"""持仓/资金/成交查询服务

资金余额: control_id 批量读取（无需 OCR）
持仓/今日成交: F4 + Ctrl+C 剪切板法 + OCR 验证码兜底
"""
import os
import time
from typing import Optional

from src.utils.poll import poll_until, timed, PollTimeoutError
from src.constants import (
    BALANCE_FIELDS,
    CAPTCHA_IMAGE_ID, CAPTCHA_INPUT_ID, CAPTCHA_OK_BUTTON_ID,
    CAPTCHA_CANCEL_BUTTON_ID, CAPTCHA_VERIFY_ID,
    CAPTCHA_DIALOG_TITLE, CAPTCHA_TEXT_KEYWORDS,
    MAIN_WINDOW_TITLE_KEYWORD
)
from src.exceptions import ApiError, ErrorCode
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
        self._cached_window = None   # 用于窗口引用刷新
        self._captcha_window = None  # 验证码弹窗引用（可能是独立顶层窗口）

    def _send_ctrl_c(self):
        """激活窗口 → keybd_event 发送两次 Ctrl+C（绕过中文输入法）

        中文输入法在中文模式下会拦截第一次 Ctrl+C（用于取消组合状态），
        第二次才真正送达。因此连续发送两次 Ctrl+C。

        使用 keybd_event（虚拟键码）而非 SendInput（扫描码），因为券商软件
        可能通过 LLKHF_INJECTED 标志过滤 SendInput 注入的键盘输入。
        """
        import win32api
        import win32con
        import win32gui

        self._refresh_window_ref()
        window = self._cached_window
        if window is None:
            raise Exception("交易窗口未找到，无法发送 Ctrl+C")

        target_handle = window.handle

        # 如果窗口被最小化，先恢复
        if win32gui.IsIconic(target_handle):
            self.logger.info("检测到交易窗口已最小化，恢复后再发送 Ctrl+C")
            try:
                win32gui.ShowWindow(target_handle, win32con.SW_RESTORE)
                time.sleep(0.2)
            except Exception as e:
                self.logger.warning(f"恢复最小化窗口失败: {e}")

        with timed("click_input 激活窗口", self.logger):
            # 先用 SetForegroundWindow 强制置前，再 click_input 确保焦点
            try:
                win32gui.SetForegroundWindow(target_handle)
                time.sleep(0.15)
            except Exception:
                pass
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

        with timed("keybd_event Ctrl+C ×2", self.logger):
            VK_CONTROL = win32con.VK_CONTROL  # 0x11
            VK_C = ord('C')                   # 0x43

            def _send_one():
                # 关键：Ctrl 按下后延迟 0.1s，让系统键盘状态更新为"Ctrl 已按住"，
                # 券商软件用 GetAsyncKeyState 检测 Ctrl 是否真的在按下状态，
                # 无延迟则检测不到组合键，Ctrl+C 无效。
                win32api.keybd_event(VK_CONTROL, 0, 0, 0)
                time.sleep(0.1)
                win32api.keybd_event(VK_C, 0, 0, 0)
                time.sleep(0.05)
                win32api.keybd_event(VK_C, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.05)
                win32api.keybd_event(VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)

            # 第一次：取消中文输入法组合状态（可能被 IME 拦截）
            _send_one()
            time.sleep(0.15)
            # 第二次：真正送达券商软件
            _send_one()
            time.sleep(0.3)

    def _is_valid_table_data(self, data: str) -> bool:
        """校验剪贴板内容是否为有效的表格数据

        同花顺 Ctrl+C 复制的表格数据特征：
        - 表头行用 \\t 分隔列，数据行用 \\r\\n 分隔
        - 至少要有一行表头，且含 \\t（至少 2 列）

        接受仅有表头无数据行的情况（账户当日无数据时正常现象），
        由 _format_table_data 负责返回空列表。
        """
        if not data:
            return False
        lines = data.strip().split("\r\n")
        if not lines:
            return False
        # 首行必须有制表符（至少 2 列），其余行有内容的也必须含制表符
        return "\t" in lines[0] and all(
            "\t" in line for line in lines[1:] if line.strip()
        )

    def _copy_table_via_clipboard(self) -> list:
        """通过剪贴板读取表格数据（含验证码处理）

        流程：Ctrl+C → 验证码弹窗 → OCR 识别 → 读剪贴板，最多 3 次。
        """
        for attempt in range(3):
            self.logger.info(f"第 {attempt + 1}/3 次尝试 Ctrl+C 读取表格数据")

            with timed("Ctrl+C 发送", self.logger):
                self._send_ctrl_c()

            # 轮询等待验证码弹窗出现
            with timed("等待验证码弹窗", self.logger):
                try:
                    poll_until(
                        lambda: self._detect_captcha(
                            self.window_service.get_trading_window()
                        ),
                        timeout=3.0, interval=0.1,
                        description="Ctrl+C 后验证码弹窗"
                    )
                except PollTimeoutError:
                    self._refresh_window_ref()
                    if not self._detect_captcha(self._cached_window):
                        self.logger.warning(
                            f"第 {attempt + 1} 次 Ctrl+C 后未检测到验证码弹窗"
                            f"（{3.0}s 超时），可能焦点丢失"
                        )
                        continue

            # 使用检测到的验证码弹窗（可能是独立顶层窗口）
            captcha_win = self._captcha_window or self._cached_window

            with timed("验证码 OCR + 输入 + 确认", self.logger):
                try:
                    self._solve_captcha(captcha_win)
                except ApiError as e:
                    if e.error_code == ErrorCode.OCR_FAILED:
                        self.logger.warning(f"第 {attempt + 1} 次验证码处理失败: {e.message}")
                        continue
                    raise  # 其他 ApiError 继续向上传播

            with timed("读取剪贴板数据", self.logger):
                data = self.window_service.get_clipboard()
                if self._is_valid_table_data(data):
                    self.logger.info(f"第 {attempt + 1} 次成功获取表格数据")
                    return self._format_table_data(data)
                self.logger.warning(
                    f"第 {attempt + 1} 次剪贴板内容无效: {repr(data[:100]) if data else '空'}"
                )

        # 所有尝试失败，截图诊断 + 返回 OCR 错误
        DiagnosticUtil().snapshot("query_empty_clipboard")
        raise ApiError(
            ErrorCode.OCR_FAILED,
            "验证码识别失败，已重试 3 次",
            suggestion="可稍后重试查询，或检查交易窗口是否被遮挡"
        )

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
        """检测验证码弹窗（主窗口 + 独立弹出窗口双重检测）

        验证码弹窗可能是独立顶层窗口（独立于主交易窗口），
        仅搜索主窗口子孙控件会漏掉。此方法同时搜索主窗口和所有可见 Desktop 窗口。

        Args:
            window: 主交易窗口对象（若为 None 则先刷新）

        Returns:
            是否检测到验证码弹窗。找到后弹窗引用存入 self._captcha_window 供 OCR 使用。
        """
        if window is None:
            self._refresh_window_ref()
            window = self._cached_window
        if window is None:
            return False

        # 构建搜索列表: 优先独立弹窗(验证码弹窗标题"提示"),再主窗口
        popups = []
        try:
            from pywinauto import Desktop
            for w in Desktop(backend="uia").windows(visible_only=True):
                if w.handle != window.handle:
                    popups.append(w)
        except Exception:
            pass
        windows_to_search = popups + [window]

        for w in windows_to_search:
            win_text = self._safe_window_text(w)
            is_main_window = MAIN_WINDOW_TITLE_KEYWORD in win_text

            # 方法1: control_id=2405 检测
            try:
                image = self.window_service.find_element_in_window(w, CAPTCHA_IMAGE_ID)
                if image is not None:
                    # 主窗口中可能残留隐藏的 2405 控件，跳过
                    if is_main_window:
                        self.logger.debug(
                            f"control_id=2405 命中主窗口（疑似残留控件），跳过"
                        )
                        continue
                    self.logger.info(
                        f"通过 control_id=2405 检测到验证码弹窗（title='{win_text}'）"
                    )
                    self._captcha_window = w
                    return True
            except Exception:
                pass

            # 方法2: 文本匹配（精确关键词，"检测到您正在拷贝数据"）
            try:
                matches = self.window_service.find_element_by_text(
                    w, CAPTCHA_TEXT_KEYWORDS
                )
                if matches:
                    # 排除主窗口误报
                    if is_main_window:
                        self.logger.debug(
                            f"文本匹配命中主窗口（title='{win_text}'），跳过"
                        )
                        continue
                    self.logger.info(
                        f"通过文本匹配检测到验证码弹窗（{len(matches)} 个匹配, "
                        f"title='{win_text}'）"
                    )
                    self._captcha_window = w
                    return True
            except Exception:
                pass

        self.logger.info("未检测到验证码弹窗（control_id 和文本匹配均未命中）")
        return False

    @staticmethod
    def _safe_window_text(w) -> str:
        """安全获取窗口标题文本"""
        try:
            return w.window_text() or ""
        except Exception:
            return ""

    def _dismiss_popup_if_present(self, window) -> bool:
        """检测并关闭常见提示弹窗（委托给 WindowService 统一处理）"""
        return self.window_service.dismiss_blocking_popup(window)

    # ------------------------------------------------------------
    # 查询面板准备（所有查询的公共前置步骤）
    # ------------------------------------------------------------

    def _prepare_query_panel(self):
        """重置到 F1 → F4 打开查询面板

        调用 window_service.reset_window_state() 确保窗口在前台且处于 F1 基准态，
        然后发送 F4 切换到查询面板。
        """
        with timed("reset_window_state", self.logger):
            self.window_service.reset_window_state()

        with timed("F4 打开查询面板", self.logger):
            self.window_service.send_key("F4", background=True)
            time.sleep(0.5)

    # ------------------------------------------------------------
    # 资金余额（control_id 批量读取，无需 OCR）
    # ------------------------------------------------------------

    def get_balance(self) -> dict:
        """获取资金余额"""
        self.logger.info("开始获取资金余额")

        with timed("_prepare_query_panel", self.logger):
            self._prepare_query_panel()

        with timed("control_id 批量读取", self.logger):
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

        with timed("_prepare_query_panel", self.logger):
            self._prepare_query_panel()

        # F4 打开后默认就是"资金股票"页面，无需额外导航
        return self._copy_table_via_clipboard()

    # ------------------------------------------------------------
    # 今日成交（树形菜单 + Ctrl+C + OCR 兜底）
    # ------------------------------------------------------------

    def get_today_trades(self) -> list:
        """获取今日成交"""
        self.logger.info("开始获取今日成交")

        with timed("_prepare_query_panel", self.logger):
            self._prepare_query_panel()

        with timed("导航到当日成交", self.logger):
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

        with timed("_prepare_query_panel", self.logger):
            self._prepare_query_panel()

        with timed("导航到当日委托", self.logger):
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
            raise ApiError(ErrorCode.OCR_FAILED, "OCR 服务未初始化，无法处理验证码")

        if window is None:
            self.logger.error("窗口为 None，无法处理验证码")
            raise ApiError(ErrorCode.OCR_FAILED, "窗口为 None，无法处理验证码")

        # 查找验证码图片控件（仅精确 control_id 匹配，须可见）
        # 注意: 主窗口可能存在多个 2405（隐藏残留 + 真实弹窗），
        # find_element_in_window 返回第一个，必须过滤隐藏元素
        image_element = self.window_service.find_element_in_window(window, CAPTCHA_IMAGE_ID)
        if image_element is not None and not self._is_element_visible(image_element):
            self.logger.warning("找到 control_id=2405 但不可见（疑似主窗口残留控件），视为未找到")
            image_element = None
        if image_element is None:
            self.logger.error("验证码图片元素未找到（control_id=2405 可见）")
            raise ApiError(ErrorCode.OCR_FAILED, "验证码图片元素未找到",
                           suggestion="请确认交易窗口是否正常显示验证码弹窗")

        # 保存验证码图片
        with timed("验证码截图保存", self.logger):
            cache_dir = self.config.get_logging_config().get("screenshot_dir", "logs/screenshots")
            os.makedirs(cache_dir, exist_ok=True)
            image_path = os.path.join(cache_dir, "captcha.png")
            image_element.capture_as_image().save(image_path)
            self.logger.info(f"验证码图片已保存: {image_path}")

        max_retry = self.config.get_ocr_config().get("max_retry", 3)

        for attempt in range(max_retry):
            try:
                with timed("OCR 识别", self.logger):
                    # 截图尺寸校验：真实验证码 ~1KB，主窗口截图 > 50KB
                    if not self._is_captcha_image_valid(image_path):
                        self.logger.warning(
                            f"验证码截图异常（{os.path.getsize(image_path)} bytes，"
                            f"疑似截取到主窗口），将重试（尝试 {attempt + 1}/{max_retry}）"
                        )
                        continue
                    ocr_text = self.ocr_service.recognize(image_path)
                if not ocr_text:
                    self.logger.warning(f"OCR 识别为空（尝试 {attempt + 1}/{max_retry}）")
                    continue

                self.logger.info(f"OCR 识别结果: {ocr_text}")

                # 输入验证码 + 点击确定
                with timed("输入验证码", self.logger):
                    self.window_service.input_text_to_element(window, CAPTCHA_INPUT_ID, ocr_text)
                if not self._click_button(window, CAPTCHA_OK_BUTTON_ID):
                    self.logger.warning("未找到验证码确定按钮")
                    continue

                # 轮询等待验证结果
                with timed("等待验证码确认", self.logger):
                    try:
                        poll_until(
                            lambda: self._verify_captcha_success(
                                self.window_service.get_trading_window()
                            ),
                            timeout=2.0, interval=0.1,
                            description="验证码验证结果"
                        )
                        self.logger.info("验证码验证成功")
                        return True
                    except PollTimeoutError:
                        self._refresh_window_ref()
                        window = self._cached_window
                        if self._verify_captcha_success(window):
                            self.logger.info("验证码验证成功")
                            return True

                # 失败：点击取消，重新触发验证码
                self.logger.warning(f"验证码错误（尝试 {attempt + 1}/{max_retry}）")
                self._click_button(window, CAPTCHA_CANCEL_BUTTON_ID)
                time.sleep(0.2)

                # 重新检测验证码弹窗（避免用 _refresh_window_ref 切到主窗口）
                window = self.window_service.get_trading_window()
                if window and self._detect_captcha(window):
                    window = self._captcha_window or window
                    image_element = self.window_service.find_element_in_window(
                        window, CAPTCHA_IMAGE_ID)
                    if image_element is None:
                        return True
                    image_element.capture_as_image().save(image_path)
                else:
                    # 验证码弹窗已消失，视为成功
                    self.logger.info("验证码弹窗已消失，视为处理成功")
                    return True

            except Exception as e:
                self.logger.error(f"验证码处理异常: {e}")
                try:
                    self._click_button(window, CAPTCHA_CANCEL_BUTTON_ID)
                except Exception:
                    pass

        self.logger.warning(f"验证码处理失败，已达到最大重试次数 {max_retry}")
        raise ApiError(
            ErrorCode.OCR_FAILED,
            f"验证码识别失败，已重试 {max_retry} 次",
            suggestion="可稍后重试查询，或检查交易窗口是否被遮挡"
        )

    @staticmethod
    def _is_element_visible(element) -> bool:
        """检查 UIA 元素是否可见（非隐藏/非残留控件）"""
        try:
            return element.is_visible()
        except Exception:
            return True  # 无法判断时假定可见

    @staticmethod
    def _is_captcha_image_valid(image_path: str, max_bytes: int = 5000) -> bool:
        """校验验证码图片是否合理（非主窗口截图）

        真实验证码图片约 1KB，主窗口截图 > 50KB。
        文件过大说明截图对象错误（隐藏控件/主窗口）。

        Returns:
            True=合理，False=异常
        """
        try:
            size = os.path.getsize(image_path)
            return size <= max_bytes
        except Exception:
            return True  # 无法判断时假定有效

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

        with timed(f"树形菜单导航 → {page_name}", self.logger):
            # 策略1: 树形菜单文本导航
            button = self.window_service.find_element_by_tree_path(
                window, ('control_type', 'Tree'), [parent_name, page_name]
            )
            if button is not None:
                button.click_input()
                self.logger.info(f"已通过树形路径点击 '{page_name}'")
                time.sleep(0.3)
                with timed("弹窗防御", self.logger):
                    self._dismiss_popup_if_present(window)
                return

        self.logger.warning(f"树形路径导航失败，尝试扫描 TreeItem 文本匹配: {page_name}")

        with timed(f"TreeItem 全量扫描 → {page_name}", self.logger):
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
