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
from src.utils.uia import safe_control_type, safe_text


class PositionService:
    """持仓/资金/成交查询服务"""

    # 各查询表的特征列（用于 _copy_table_verified 验证页面切换是否成功）
    POSITION_TABLE_COLUMNS = {"成本价", "股票余额"}
    TRADES_TABLE_COLUMNS = {"成交时间", "成交编号"}
    ORDERS_TABLE_COLUMNS = {"委托编号"}

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
                time.sleep(0.15)  # SetForegroundWindow <0.1s，0.3s 冗余
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

    @staticmethod
    def _is_table_matching(rows: list, required_columns: set) -> bool:
        """表格特征列检测：首行含全部 required_columns 列

        页面切换失败时会复制到其他查询表（持仓/当日成交/当日委托），
        各自特征列不同（成本价/成交编号/委托编号）。空表无法判断，
        视为正确（可能当日无数据），不阻断流程。
        """
        if not rows:
            return True
        keys = set(rows[0].keys())
        return required_columns.issubset(keys)

    def _copy_table_verified(self, table_name: str, required_columns: set) -> list:
        """复制表格 + 特征列验证，失败重试一次，仍失败显式报错

        防御场景：非交易时段/服务器异常时树节点点击不触发页面切换，
        Ctrl+C 复制到的是其他查询表——不验证会静默返回假数据。
        """
        for attempt in range(2):
            data = self._copy_table_via_clipboard()
            if self._is_table_matching(data, required_columns):
                return data
            self.logger.warning(
                f"第 {attempt + 1} 次复制到的不是{table_name}表"
                f"（页面切换失败），重试"
            )
        DiagnosticUtil().snapshot(f"{table_name}_page_switch_failed")
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            f"{table_name}查询失败：未检测到{table_name}表特征列"
            "（页面切换异常——窗口可能被遮挡/最小化，或焦点未进入表格）",
            suggestion="请确认券商窗口完整可见后重试"
        )

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

        流程：Ctrl+C → 验证码弹窗 → OCR 识别 → 读剪贴板，最多 2 次
        （内层 _solve_captcha 已有 max_retry 次 OCR 重试，外层无需 3 次）。
        """
        max_attempts = 2
        for attempt in range(max_attempts):
            self.logger.info(f"第 {attempt + 1}/{max_attempts} 次尝试 Ctrl+C 读取表格数据")

            # 复制前清空剪贴板：焦点丢失/表格未就绪时 Ctrl+C 会复制失败，
            # 不清空会读到上次任务残留的数据（如持仓表）伪装成"复制成功"
            self.window_service.clear_clipboard()

            with timed("Ctrl+C 发送", self.logger):
                self._send_ctrl_c()

            # 等待验证码弹窗出现（主动定时完整扫描）
            # 验证码弹窗通常在 Ctrl+C 后 <500ms 出现。
            # 策略: 先立即扫一次（可能已出现）, 未命中则睡 0.3s 再扫, 最多 3 次。
            # 首次: ~0.8s → 后续: 0.3+0.8=1.1s/次 → 最坏 0.8+1.1+1.1=3.0s
            # 弹窗正常<500ms出现 → 第 1-2 次命中 → ~0.8-1.9s
            with timed("等待验证码弹窗", self.logger):
                captcha_found = False
                for scan in range(3):
                    if scan > 0:
                        time.sleep(0.3)
                    self._refresh_window_ref()
                    if self._detect_captcha_full(self._cached_window):
                        captcha_found = True
                        break
                if not captcha_found:
                    self.logger.warning(
                        f"第 {attempt + 1} 次 Ctrl+C 后未检测到验证码弹窗"
                        f"（已扫描 4 次），可能焦点丢失"
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
        """快速检测验证码弹窗（仅 Desktop 窗口标题枚举，无 UIA 树遍历）

        poll_until 高频轮询专用（~10ms/次）。
        仅通过 Desktop 窗口标题搜索独立弹窗（如"提示"），
        不扫描主窗口 UIA 树（~0.8s 太慢，留给超时兜底）。
        """
        if window is None:
            self._refresh_window_ref()
            window = self._cached_window
        if window is None:
            return False

        try:
            from pywinauto import Desktop
            for w in Desktop(backend="uia").windows(visible_only=True):
                win_text = self._safe_window_text(w)
                if CAPTCHA_DIALOG_TITLE in win_text:
                    image = self.window_service.find_element_in_window(
                        w, CAPTCHA_IMAGE_ID)
                    if image is not None:
                        self.logger.info(
                            f"通过窗口标题'提示'+control_id=2405检测到验证码弹窗"
                        )
                        self._captcha_window = w
                        return True
        except Exception:
            pass

        return False

    def _detect_captcha_full(self, window) -> bool:
        """完整检测验证码弹窗（含主窗口 UIA 树扫描）

        仅用于 poll_until 超时后的兜底检测（~0.8s，低频调用）。
        """
        # 先试快速检测
        if self._detect_captcha(window):
            return True

        # 兜底：主窗口子孙控件扫描
        if window is None:
            self._refresh_window_ref()
            window = self._cached_window
        if window is not None:
            try:
                image = self.window_service.find_element_in_window(
                    window, CAPTCHA_IMAGE_ID)
                if image is not None:
                    self.logger.info(
                        "通过 control_id=2405 在主窗口中检测到验证码弹窗"
                    )
                    self._captcha_window = window
                    return True
            except Exception:
                pass

        return False

    @staticmethod
    def _safe_window_text(w) -> str:
        """安全获取窗口标题文本（委托 uia.safe_text，失败记录 debug 日志）"""
        return safe_text(w)

    def _dismiss_popup_if_present(self, window) -> bool:
        """检测并关闭常见提示弹窗（委托给 WindowService 统一处理）"""
        return self.window_service.dismiss_blocking_popup(window)

    # ------------------------------------------------------------
    # 查询面板准备（所有查询的公共前置步骤）
    # ------------------------------------------------------------

    def _prepare_query_panel(self):
        """切换到 F4 查询面板

        连续查询跳过时窗口已在 F4，无需重发 F4。
        """
        from src.api.task_queue import TaskQueue
        _tq = TaskQueue.get_instance()
        if _tq.skip_window_setup:
            self.logger.info("连续查询跳过，窗口已在 F4 面板")
            _tq.skip_window_setup = False
            return
        with timed("F4 打开查询面板", self.logger):
            self.window_service.send_key("F4", background=True)
            time.sleep(0.2)  # F4 切换 <0.15s

    # ------------------------------------------------------------
    # 资金余额（control_id 批量读取，无需 OCR）
    # ------------------------------------------------------------

    def _read_balance_fields(self, window) -> dict:
        """从资金股票页读取资金概览字段（control_id 批量读取，无需 OCR）"""
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
        return result

    def get_balance(self) -> dict:
        """获取资金余额

        显式导航到"资金股票"页（资金概览栏只在资金股票页可见——
        连续查询时窗口可能停在当日成交/当日委托页，全部字段会读取失败）。
        全部字段为 None = 页面未切换（非交易时段/服务器异常），
        重试一次导航，仍失败显式报错，绝不静默返回 None 字段。
        """
        self.logger.info("开始获取资金余额")

        with timed("_prepare_query_panel", self.logger):
            self._prepare_query_panel()

        with timed("导航到资金股票", self.logger):
            self._refresh_window_ref()
            window = self._cached_window
            self._navigate_to_query_page(window, "资金股票")

        with timed("control_id 批量读取", self.logger):
            window = self.window_service.get_trading_window()
            if window is None:
                raise Exception("未找到交易窗口 '网上股票交易系统5.0'")
            result = self._read_balance_fields(window)

            if all(v is None for v in result.values()):
                # 资金概览栏不可见 = 页面未切换到资金股票（连续查询/非交易时段）
                self.logger.warning(
                    "资金概览字段全部为空（可能窗口不在资金股票页），重试导航"
                )
                self._refresh_window_ref()
                window = self._cached_window
                if window is not None:
                    self._navigate_to_query_page(window, "资金股票")
                    result = self._read_balance_fields(window)
                if all(v is None for v in result.values()):
                    DiagnosticUtil().snapshot("balance_page_switch_failed")
                    raise ApiError(
                        ErrorCode.INTERNAL_ERROR,
                        "资金余额查询失败：资金概览字段全部为空"
                        "（页面切换异常——窗口可能被遮挡/最小化，或焦点未进入表格）",
                        suggestion="请确认券商窗口完整可见后重试"
                    )

        self.logger.info(f"资金余额查询完成: {result}")
        return result

    # ------------------------------------------------------------
    # 持仓查询（F4 + Ctrl+C + 剪切板 + OCR 兜底）
    # ------------------------------------------------------------

    def get_position(self) -> list:
        """获取当前持仓

        显式导航到"资金股票"页（不依赖"F4 默认页"假设——连续查询时
        窗口可能停在当日成交/当日委托页，直接 Ctrl+C 会复制错表）。
        复制后验证表头特征：页面切换失败时（如非交易时段服务器不响应）
        返回的是其他查询表，验证失败重试一次，仍失败则显式报错，
        绝不静默返回假数据。
        """
        self.logger.info("开始获取持仓")

        with timed("_prepare_query_panel", self.logger):
            self._prepare_query_panel()

        with timed("导航到资金股票", self.logger):
            self._refresh_window_ref()
            window = self._cached_window
            self._navigate_to_query_page(window, "资金股票")

        # 特征列验证：页面切换失败时（非交易时段）复制到的是其他查询表，
        # 验证失败重试一次，仍失败显式报错，绝不静默返回假数据
        return self._copy_table_verified("持仓", self.POSITION_TABLE_COLUMNS)

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

        return self._copy_table_verified("成交", self.TRADES_TABLE_COLUMNS)

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

        return self._copy_table_verified("委托", self.ORDERS_TABLE_COLUMNS)

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

        # 缓存 UIA 控件树：验证码弹窗控件少（~20个），一次遍历全流程复用
        _descendants = list(window.descendants())

        # 查找验证码图片控件（精确 control_id=2405 匹配）
        image_element = self.window_service.find_element_in_window(
            window, CAPTCHA_IMAGE_ID, descendants=_descendants)
        if image_element is None:
            self.logger.error("验证码图片元素未找到（control_id=2405）")
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

                # 输入验证码 + 点击确定（复用缓存的 descendants）
                with timed("输入验证码", self.logger):
                    self.window_service.input_text_to_element(
                        window, CAPTCHA_INPUT_ID, ocr_text, descendants=_descendants)
                if not self._click_button(window, CAPTCHA_OK_BUTTON_ID, descendants=_descendants):
                    self.logger.warning("未找到验证码确定按钮")
                    continue

                # 轮询等待验证结果（服务器通常即时响应，1s足够）
                with timed("等待验证码确认", self.logger):
                    try:
                        poll_until(
                            lambda: self._verify_captcha_success(
                                self.window_service.get_trading_window()
                            ),
                            timeout=1.0, interval=0.15,
                            description="验证码验证结果"
                        )
                        self.logger.info("验证码验证成功")
                        return True
                    except PollTimeoutError:
                        # poll_until 已轮询 ~7次都失败，无需再查
                        pass

                # 失败：点击取消，重新触发验证码
                self.logger.warning(f"验证码错误（尝试 {attempt + 1}/{max_retry}）")
                self._click_button(window, CAPTCHA_CANCEL_BUTTON_ID, descendants=_descendants)
                time.sleep(0.2)

                # 重新检测验证码弹窗
                window = self.window_service.get_trading_window()
                if window and self._detect_captcha_full(window):
                    window = self._captcha_window or window
                    # 窗口切换后刷新 UIA 树缓存
                    _descendants = list(window.descendants())
                    image_element = self.window_service.find_element_in_window(
                        window, CAPTCHA_IMAGE_ID, descendants=_descendants)
                    if image_element is None:
                        self.logger.info("验证码弹窗已消失（image_element=None），视为处理成功")
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
    def _is_captcha_image_valid(image_path: str, max_bytes: int = 5000) -> bool:
        """校验验证码图片是否合理（非主窗口截图）

        真实验证码: 92×38 白底蓝字 4 位数字（~1KB）。
        主窗口截图 > 50KB 且尺寸大得多。截到隐藏控件/弹窗边缘时
        可能尺寸合格但内容异常（全白/暗像素集中边缘）。

        启发式（任一不满足 = 异常）:
        1. 文件大小 ≤ 5KB（原有）
        2. 尺寸接近 92×38（允许 ±40% 偏差）
        3. 白底占比 > 50%（与二值化阈值 200 一致）
        4. 暗像素水平跨度在 15%-85% 宽度之间（数字集中中间区域，
           截到空白/边缘时分布异常）

        Returns:
            True=合理，False=异常
        """
        try:
            size = os.path.getsize(image_path)
            if size > max_bytes:
                return False

            from PIL import Image
            import numpy as np

            img = Image.open(image_path).convert("L")
            w, h = img.size
            if w * h == 0:
                return False
            # 尺寸合理性：真实验证码 92×38，允许 ±40% 偏差
            if not (55 <= w <= 130 and 22 <= h <= 54):
                return False

            arr = np.array(img, dtype=np.uint8)
            # 白底占比（>200 为白/浅色背景，与二值化阈值一致）
            white_ratio = float((arr > 200).mean())
            if white_ratio < 0.5:
                return False

            # 暗像素水平分布：数字笔画集中在中间区域
            dark_cols = np.where((arr < 200).any(axis=0))[0]
            if len(dark_cols) == 0:
                return False  # 全白 = 隐藏控件未渲染
            span = int(dark_cols.max()) - int(dark_cols.min()) + 1
            if not (0.15 * w <= span <= 0.85 * w):
                return False  # 暗像素集中在边缘/角落 = 截到边缘

            return True
        except Exception:
            return True  # 无法判断时假定有效

    def _click_button(self, window, control_id: int, descendants=None) -> bool:
        """点击按钮（可选复用缓存的 descendants）"""
        button = self.window_service.find_element_in_window(window, control_id, descendants=descendants)
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
        """导航到查询页面

        一次 descendants 遍历同时用于 Tree 查找 + 弹窗检测（省去两次分
        别遍历的 ~2s 开销）。TreeItem 兜底复用同一次遍历。
        """
        parent_name = "查询[F4]"

        with timed(f"导航到 {page_name}", self.logger):
            _descendants = list(window.descendants())  # 唯一一次遍历

            # 策略1: 树形路径导航
            tree_root = None
            for el in _descendants:
                if safe_control_type(el) == "Tree":
                    tree_root = el
                    break

            if tree_root is not None:
                current = tree_root
                for name in [parent_name, page_name]:
                    found = None
                    for child in current.children():
                        if name in (child.window_text() or ""):
                            found = child
                            break
                    if found is None:
                        break
                    current = found

                if found is not None and page_name in (found.window_text() or ""):
                    found.click_input()
                    self.logger.info(f"已通过树形路径点击 '{page_name}'")
                    time.sleep(0.15)
                    self._check_blocking_popup(_descendants, window)
                    return

            # 策略2: TreeItem 文本扫描（复用 _descendants）
            self.logger.info(f"树形路径未匹配，TreeItem 扫描: {page_name}")
            for el in _descendants:
                if safe_control_type(el) == "TreeItem":
                    if page_name in safe_text(el):
                        el.click_input()
                        self.logger.info(f"已通过 TreeItem 匹配点击 '{page_name}'")
                        time.sleep(0.15)
                        self._check_blocking_popup(_descendants, window)
                        return

        raise Exception(
            f"导航到 '{page_name}' 失败：树形路径和 TreeItem 扫描均未找到目标页面"
        )

    def _check_blocking_popup(self, descendants, window) -> None:
        """用缓存的 descendants 检测阻塞弹窗，命中才走完整关闭流程"""
        popup_keywords = ["Begin failed", "failed", "失败", "事务处理机"]
        for el in descendants:
            text = safe_text(el)
            if any(kw in text for kw in popup_keywords):
                self._dismiss_popup_if_present(window)
                return

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
