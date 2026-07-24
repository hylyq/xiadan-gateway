"""窗口操作服务

提供窗口枚举、激活、控件查找、点击、输入、按键、剪切板等基础能力
"""
import ctypes
import time
from typing import Optional, Union, List

import psutil
import win32api
import win32con
import win32gui
import win32process
from pywinauto import Application, Desktop

from config.key_config import KEY_MAP
from src.constants import TRADING_WINDOW_TITLE
from src.utils.logger import Logger
from src.utils.poll import timed


class WindowService:
    """窗口操作服务"""

    def __init__(self):
        self.logger = Logger.get_instance()
        self._cached_hwnd = None  # 缓存窗口句柄，避免重复 Desktop().windows() 开销

    # ------------------------------------------------------------
    # 窗口枚举与激活
    # ------------------------------------------------------------

    def get_window_info(self, hwnd: int) -> dict:
        """获取窗口详细信息"""
        try:
            title = win32gui.GetWindowText(hwnd)
            class_name = win32gui.GetClassName(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return {
                "hwnd": hwnd,
                "title": title,
                "class_name": class_name,
                "rect": {
                    "left": rect[0], "top": rect[1],
                    "right": rect[2], "bottom": rect[3],
                    "width": rect[2] - rect[0],
                    "height": rect[3] - rect[1],
                },
                "pid": pid,
                "is_minimized": bool(win32gui.IsIconic(hwnd)),
                "is_maximized": bool(ctypes.windll.user32.IsZoomed(hwnd)),
                "is_visible": bool(win32gui.IsWindowVisible(hwnd)),
            }
        except Exception as e:
            self.logger.error(f"获取窗口信息失败: {str(e)}")
            raise Exception(f"获取窗口信息失败: {str(e)}")

    def activate_window(self, app_path, foreground: bool = True) -> int:
        """根据 exe 完整路径激活窗口（支持单个路径或路径列表）

        Args:
            app_path: 应用程序完整路径，如 xiadan.exe 的绝对路径。
                      支持单个 str 或 List[str]（按列表顺序决定优先级）。
            foreground: True=强制置前（默认，原有行为）；False=仅恢复窗口不抢焦点，
                        配合 PostMessage 后台按键使用

        Returns:
            窗口句柄

        Raises:
            Exception: 未找到匹配窗口
        """
        # 统一转为列表
        if isinstance(app_path, str):
            paths = [app_path]
        else:
            paths = list(app_path)
        paths_lower = [p.lower() for p in paths]

        # 一轮扫描收集所有匹配窗口
        # 不要求 IsWindowVisible — 窗口可能被隐藏到系统托盘
        # 优先按主窗口标题匹配，子窗口作为 fallback
        found_windows = {}  # exe_lower -> hwnd

        def callback(hwnd, extra):
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                proc = psutil.Process(pid)
                exe = proc.exe().lower()
                if exe in paths_lower:
                    title = win32gui.GetWindowText(hwnd)
                    # 主窗口标题优先覆盖（如"网上股票交易系统5.0"）
                    if title == TRADING_WINDOW_TITLE:
                        found_windows[exe] = hwnd
                    elif exe not in found_windows:
                        found_windows[exe] = hwnd
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            return True

        win32gui.EnumWindows(callback, None)

        if found_windows:
            # 按配置顺序选优先级最高的（第一个匹配的路径）
            for path in paths_lower:
                if path in found_windows:
                    hwnd = found_windows[path]
                    # 如果窗口不可见（如系统托盘），先显示
                    if not win32gui.IsWindowVisible(hwnd):
                        self.logger.info(f"目标窗口不可见 (title='{win32gui.GetWindowText(hwnd)}')，正在显示...")
                        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                        time.sleep(0.1)
                    if win32gui.IsIconic(hwnd):
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    if foreground:
                        self._set_foreground(hwnd)
                    return hwnd

        raise Exception(f"未找到匹配窗口，路径: {paths}")

    def activate_window_by_pid(self, pid: int, retries: int = 3, delay: float = 0.5) -> int:
        """根据进程 ID 激活窗口"""
        hwnd_found = None

        def callback(hwnd, extra):
            nonlocal hwnd_found
            if win32gui.IsWindowVisible(hwnd):
                _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
                if window_pid == pid:
                    hwnd_found = hwnd
                    if win32gui.IsIconic(hwnd):
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    self._set_foreground(hwnd)
                    return False
            return True

        for attempt in range(retries):
            win32gui.EnumWindows(callback, None)
            if hwnd_found:
                active = win32gui.GetForegroundWindow()
                if active == hwnd_found:
                    return hwnd_found
                self._set_foreground(hwnd_found)
                time.sleep(delay)
            else:
                time.sleep(delay)

        raise Exception(f"未找到匹配窗口，PID: {pid}")

    def _set_foreground(self, hwnd: int) -> None:
        """将窗口置于前台（带降级方案）"""
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception as e:
            self.logger.warning(
                f"SetForegroundWindow 失败，降级到 pywinauto.set_focus()，"
                f"句柄: {hwnd}，错误: {str(e)}"
            )
            try:
                app = Application(backend="uia").connect(handle=hwnd)
                app.window(handle=hwnd).set_focus()
            except Exception as e2:
                raise Exception(
                    f"设置前台窗口失败，句柄: {hwnd}，"
                    f"错误1: {str(e)}，错误2: {str(e2)}"
                )

    # ------------------------------------------------------------
    # 目标窗口查找
    # ------------------------------------------------------------

    def get_target_window(self, window_params: dict, retries: int = 3, delay: float = 0.5):
        """根据参数获取目标窗口

        Args:
            window_params: pywinauto Desktop.windows() 的查找参数
                          常用: {'title': '网上股票交易系统5.0'}

        Returns:
            找到的窗口对象，未找到返回 None
        """
        for i in range(retries):
            try:
                dialogs = Desktop(backend="uia").windows(**window_params)
                if dialogs:
                    return dialogs[0]
                time.sleep(delay)
            except Exception as e:
                if i == retries - 1:
                    self.logger.error(f"查找窗口失败: {str(e)}")
                time.sleep(delay)
        return None

    def get_trading_window(self):
        """获取同花顺交易窗口（网上股票交易系统5.0）

        优先通过缓存的窗口句柄重连（快），失败时走 Desktop().windows() 搜索（慢）。
        """
        # 优先通过缓存的句柄重连（避免 Desktop().windows() 的 ~2s 开销）
        if self._cached_hwnd is not None:
            try:
                if win32gui.IsWindow(self._cached_hwnd):
                    app = Application(backend="uia").connect(handle=self._cached_hwnd)
                    return app.window(handle=self._cached_hwnd)
            except Exception:
                self._cached_hwnd = None

        window = self.get_target_window({"title": TRADING_WINDOW_TITLE})
        if window is not None:
            try:
                self._cached_hwnd = window.handle
            except Exception:
                pass
        return window

    def get_trading_window_fast(self):
        """获取交易窗口（不重试，用于轮询检测场景）

        优先通过缓存的窗口句柄重连。
        """
        if self._cached_hwnd is not None:
            try:
                if win32gui.IsWindow(self._cached_hwnd):
                    app = Application(backend="uia").connect(handle=self._cached_hwnd)
                    return app.window(handle=self._cached_hwnd)
            except Exception:
                self._cached_hwnd = None

        try:
            dialogs = Desktop(backend="uia").windows(title=TRADING_WINDOW_TITLE)
            if dialogs:
                self._cached_hwnd = dialogs[0].handle
                return dialogs[0]
        except Exception:
            pass
        return None

    def invalidate_window_cache(self):
        """使窗口缓存失效（窗口可能被关闭/重建后调用）"""
        self._cached_hwnd = None

    # ------------------------------------------------------------
    # 弹窗处理（统一方法，供 PositionService / TradingService 共用）
    # ------------------------------------------------------------

    def dismiss_blocking_popup(self, window, popup_keywords: list = None) -> bool:
        """检测并关闭阻塞型提示弹窗（如非交易时段的 "Begin failed!"）

        同花顺在非交易时段进入撤单/查询界面时可能弹出提示窗（标题"提示"，
        内容如 "Begin failed!"），阻挡后续操作。此方法检测并关闭这类弹窗。

        Args:
            window: 交易窗口对象（若为 None 则直接返回 False）
            popup_keywords: 弹窗检测关键词，默认 ["Begin failed", "failed", "提示"]

        Returns:
            是否关闭了弹窗
        """
        if popup_keywords is None:
            popup_keywords = ["Begin failed", "failed", "失败", "事务处理机"]

        if window is None:
            return False

        try:
            for ctrl in window.descendants():
                try:
                    text = ctrl.window_text() or ""
                    if any(kw in text for kw in popup_keywords):
                        self.logger.info(f"检测到提示弹窗: {text[:80]}，尝试关闭")
                        # 尝试点击"确定"按钮（标准对话框 IDOK=1, IDCANCEL=2）
                        for btn_id in (1, 2):
                            btn = self.find_element_in_window(window, btn_id)
                            if btn is not None:
                                btn.click_input()
                                self.logger.info(f"已点击按钮 cid={btn_id} 关闭弹窗")
                                # 轮询等待弹窗消失（替代固定 sleep）
                                try:
                                    from src.utils.poll import poll_until_not
                                    poll_until_not(
                                        lambda: self._has_popup_text(popup_keywords),
                                        timeout=2.0, interval=0.1,
                                        description="弹窗关闭"
                                    )
                                except Exception:
                                    pass
                                return True
                        # 找不到按钮则用 ENTER 关闭
                        self.send_key("{ENTER}")
                        try:
                            from src.utils.poll import poll_until_not
                            poll_until_not(
                                lambda: self._has_popup_text(popup_keywords),
                                timeout=2.0, interval=0.1,
                                description="弹窗关闭(ENTER)"
                            )
                        except Exception:
                            pass
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _has_popup_text(self, popup_keywords: list) -> bool:
        """检查交易窗口中是否存在弹窗特征文本（用于 poll_until_not）

        Returns:
            True=弹窗仍存在, False=弹窗已关闭或窗口不可用
        """
        window = self.get_trading_window()
        if window is None:
            return False
        try:
            for ctrl in window.descendants():
                text = ctrl.window_text() or ""
                if any(kw in text for kw in popup_keywords):
                    return True
        except Exception:
            pass
        return False

    def get_all_visible_texts(self, window, descendants=None) -> str:
        """获取窗口中所有控件的可见文本（用于诊断和弹窗文本提取）"""
        if descendants is None:
            descendants = window.descendants()
        try:
            texts = []
            for c in descendants:
                try:
                    t = c.window_text()
                    if t and t.strip():
                        texts.append(t.strip())
                except Exception:
                    pass
            return "\n".join(texts)
        except Exception:
            return ""

    # ------------------------------------------------------------
    # 控件查找（支持单个 / 批量）
    # ------------------------------------------------------------

    def find_element_in_window(self, window, control_id: Union[int, str, List, tuple],
                               descendants=None):
        """在窗口中查找控件

        注意: window.descendants() 会缓存控件树。界面变化后必须重新 get_target_window。

        Args:
            window: 目标窗口对象
            control_id: 单个 control_id（int/str）或 control_id 列表（list/tuple）
            descendants: 可选，预获取的 descendants 列表（避免重复 UIA 遍历）

        Returns:
            单个 id 返回元素或 None；
            多个 id 返回元素列表（按找到顺序）
        """
        if descendants is None:
            descendants = window.descendants()

        if isinstance(control_id, (int, str)):
            for element in descendants:
                if element.control_id() == control_id:
                    return element
            return None

        if isinstance(control_id, (list, tuple)):
            result = []
            id_set = set(control_id)
            for element in descendants:
                if element.control_id() in id_set:
                    result.append(element)
                    if len(result) == len(id_set):
                        break
            return result

        raise TypeError("control_id 参数类型错误，应为 int/str 或 list/tuple")

    def find_element_by_text(self, window, text_keywords: list,
                             control_type: str = None) -> list:
        """按文本关键词查找控件（不依赖 control_id）

        用于查找验证码弹窗等 control_id 可能变化或不可靠的场景。

        Args:
            window: 目标窗口对象
            text_keywords: 文本关键词列表，如 ["验证码", "检测到"]
            control_type: 可选的控件类型过滤，如 "Button", "Text"

        Returns:
            匹配的控件列表
        """
        result = []
        try:
            for ctrl in window.descendants():
                try:
                    ctrl_text = ctrl.window_text() or ""
                    if not ctrl_text.strip():
                        continue
                    if control_type:
                        try:
                            if ctrl.element_info.control_type != control_type:
                                continue
                        except Exception:
                            continue
                    for kw in text_keywords:
                        if kw in ctrl_text:
                            result.append(ctrl)
                            break
                except Exception:
                    continue
        except Exception:
            pass
        return result

    def find_element_by_tree_path(self, window, root_control_id, path_names: list):
        """在树形结构中按路径查找元素

        Args:
            window: 目标窗口
            root_control_id: 根节点 control_id（int/str）
                             或 ('control_type', 'Tree') 这种 (key, value) 元组
            path_names: 路径名称列表，如 ["查询[F4]", "当日成交"]

        Returns:
            找到的元素，未找到返回 None
        """
        try:
            # 支持按 control_type 查找根节点（解决 cid 在 UIA 下不唯一的问题）
            if isinstance(root_control_id, tuple) and root_control_id[0] == 'control_type':
                target_type = root_control_id[1]
                root = None
                for el in window.descendants():
                    try:
                        if el.element_info.control_type == target_type:
                            root = el
                            break
                    except Exception:
                        continue
            else:
                root = self.find_element_in_window(window, root_control_id)
            if root is None:
                return None

            current = root
            for name in path_names:
                children = current.children()
                found = None
                for child in children:
                    if name in (child.window_text() or ""):
                        found = child
                        break
                if found is None:
                    return None
                current = found

            return current
        except Exception as e:
            self.logger.error(f"树形路径查找失败: {str(e)}")
            return None

    # ------------------------------------------------------------
    # 控件操作
    # ------------------------------------------------------------

    def click_element(self, window, control_id, retries: int = 3, delay: float = 0.5) -> None:
        """点击控件"""
        for i in range(retries):
            try:
                element = self.find_element_in_window(window, control_id)
                if element is None:
                    raise Exception(f"未找到 control_id={control_id} 的控件")
                element.click_input()
                return
            except Exception as e:
                if i == retries - 1:
                    raise Exception(f"点击控件失败 control_id={control_id}: {str(e)}")
                time.sleep(delay)

    def input_text_to_element(self, window, control_id, text: str, delay: float = 0.3) -> bool:
        """向输入框输入文本（先清空已有内容，再输入新内容）"""
        try:
            element = self.find_element_in_window(window, control_id)
            if element is None:
                raise Exception(f"未找到 control_id={control_id} 的输入框")
            element.set_focus()
            time.sleep(0.1)

            # 方案一：先用 WM_SETTEXT 直接设置空文本（最快）
            cleared = False
            try:
                ctrl_hwnd = element.handle
                win32gui.SendMessage(ctrl_hwnd, win32con.WM_SETTEXT, 0, "")
                cleared = True
            except Exception:
                pass

            # 方案二：用 type_keys 做双重清空
            # 先用 {HOME}+{END}{BACKSPACE}（Home→Shift+End 全选→删除）
            # 比 ^a{BACKSPACE}（Ctrl+A）更可靠，某些控件不支持 Ctrl+A
            element.type_keys("{HOME}+{END}{BACKSPACE}")
            time.sleep(0.15)
            # 第二次清空：应对自动填充在第一次清空后重新写入的情况
            element.type_keys("{HOME}+{END}{BACKSPACE}")
            time.sleep(delay)

            element.type_keys(text)
            self.logger.info(
                f"输入文本 control_id={control_id}: {text}"
                f"{' (WM_SETTEXT)' if cleared else ''}"
            )
            return True
        except Exception as e:
            self.logger.error(f"输入文本失败 control_id={control_id}: {str(e)}")
            raise

    # ------------------------------------------------------------
    # 按键发送
    # ------------------------------------------------------------

    def send_key(self, keys: str, hwnd: Optional[int] = None, background: bool = False) -> None:
        """发送按键（支持组合键）

        Args:
            keys: 按键序列，如 'F1', 'Y', '{CTRL+C}'
            hwnd: 目标窗口句柄。提供时用 PostMessage 后台发送，不抢焦点；
                  不提供时自动查找交易窗口句柄（优先 PostMessage），
                  找不到则 fallback 到 keybd_event（原有行为）
            background: 为 True 时跳过窗口激活，直接用 keybd_event 前台发送。
                       用于调用方已自行激活窗口的场景，避免冗余激活。

        注意:
            功能键（F1-F12）始终走 keybd_event 前台发送，因为这类键触发界面切换，
            PostMessage 可能无法正确触发窗口的加速键/快捷键处理。
            默认（background=False）发送功能键前会自动激活交易窗口到前台，
            避免 F1 泄漏到桌面触发 Windows 帮助。

        用法:
            send_key('F1')                    # 单键（自动激活）
            send_key('F1', background=True)   # 单键（跳过激活）
            send_key('Y')                     # 字母键
            send_key('{CTRL+C}')              # 组合键（花括号内，+连接）
            send_key('CTRL C')                # 组合键（空格分隔，按下后释放）
        """
        # background=True 时跳过激活，直接用 keybd_event 前台发送
        if background:
            self._send_key_foreground(keys)
            return

        # 功能键（F1-F12）必须前台发送，PostMessage 无法可靠触发窗口快捷键
        # 但必须先将交易窗口带到前台，否则 keybd_event 会发到错误窗口（如桌面、浏览器）
        if self._contains_function_key(keys):
            self._activate_window_before_keybd(keys)
            return

        # ESC 也走前台发送，某些子对话框/模式窗口不响应 PostMessage 的 ESC
        if self._is_escape_key(keys):
            self._activate_window_before_keybd(keys)
            return

        # 如果没有提供 hwnd，尝试自动获取交易窗口句柄
        if hwnd is None:
            window = self.get_trading_window()
            if window is not None:
                try:
                    hwnd = window.handle
                except Exception:
                    pass

        # 有目标窗口句柄 → 用 PostMessage 后台发送（不抢焦点）
        if hwnd is not None:
            self._send_key_postmessage(hwnd, keys)
            return

        # 无目标窗口 → 用 keybd_event 发到当前前台窗口（原有行为，向后兼容）
        self._send_key_foreground(keys)

    @staticmethod
    def _contains_function_key(keys: str) -> bool:
        """检查按键序列中是否包含功能键 F1-F12"""
        upper = keys.upper()
        for i in range(1, 13):
            if f"F{i}" in upper:
                return True
        return False

    @staticmethod
    def _is_escape_key(keys: str) -> bool:
        """检查是否为 ESC 键（PostMessage 无法可靠关闭子对话框）"""
        upper = keys.upper().strip()
        return upper in ("{ESC}", "ESC", "{ESCAPE}", "ESCAPE")

    def _send_key_foreground(self, keys: str) -> None:
        """通过 keybd_event 发送到前台窗口（原有方式，会抢焦点）"""
        key_sequence = [k.strip().upper() for k in keys.split(" ")]
        for key in key_sequence:
            if key.startswith("{") and key.endswith("}"):
                self._keybd_combination(key[1:-1])
            else:
                self._keybd_single(key)

    def _keybd_single(self, key: str) -> None:
        if key == "":
            time.sleep(0.5)
            return

        if len(key) == 1:
            vk = ord(key.upper())
        elif key in KEY_MAP:
            vk = KEY_MAP[key]
        else:
            for char in key:
                vk = ord(char.upper())
                win32api.keybd_event(vk, 0, 0, 0)
                win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.05)
            return

        win32api.keybd_event(vk, 0, 0, 0)
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)

    def _keybd_combination(self, keys: str, delay: float = 0.1) -> None:
        """通过 keybd_event 发送组合键"""
        key_sequence = [k.strip().upper() for k in keys.split("+")]
        key_sequence = [k.replace("\\PLUS", "+") for k in key_sequence]

        vk_codes = []
        for key in key_sequence:
            if len(key) == 1:
                vk_codes.append(ord(key.upper()))
            elif key in KEY_MAP:
                vk_codes.append(KEY_MAP[key])
            else:
                raise ValueError(f"无效的按键: {key}")

        for vk in vk_codes[:-1]:
            win32api.keybd_event(vk, 0, 0, 0)
            time.sleep(delay)
        win32api.keybd_event(vk_codes[-1], 0, 0, 0)
        time.sleep(delay)
        win32api.keybd_event(vk_codes[-1], 0, win32con.KEYEVENTF_KEYUP, 0)
        for vk in reversed(vk_codes[:-1]):
            win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(delay)

    # ------------------------------------------------------------
    # PostMessage 后台按键（不抢焦点，方案 A）
    # ------------------------------------------------------------

    def _send_key_postmessage(self, hwnd: int, keys: str) -> None:
        """通过 PostMessage 向指定窗口后台发送按键（不抢焦点）

        将按键消息直接发送到目标窗口的消息队列，由窗口自行处理。
        不改变前台窗口，不干扰用户当前操作。
        """
        key_sequence = [k.strip().upper() for k in keys.split(" ")]
        for key in key_sequence:
            if key.startswith("{") and key.endswith("}"):
                self._post_key_combination(hwnd, key[1:-1])
            else:
                self._post_single_key(hwnd, key)

    def _post_single_key(self, hwnd: int, key: str) -> None:
        """通过 PostMessage 发送单个按键"""
        if key == "":
            time.sleep(0.5)
            return

        if len(key) == 1:
            vk = ord(key.upper())
        elif key in KEY_MAP:
            vk = KEY_MAP[key]
        else:
            for char in key:
                vk = ord(char.upper())
                win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 0)
                win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, 0)
                time.sleep(0.05)
            return

        win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 0)
        win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, 0)
        time.sleep(0.05)

    def _post_key_combination(self, hwnd: int, keys: str, delay: float = 0.1) -> None:
        """通过 PostMessage 发送组合键（如 CTRL+C）"""
        key_sequence = [k.strip().upper() for k in keys.split("+")]
        key_sequence = [k.replace("\\PLUS", "+") for k in key_sequence]

        vk_codes = []
        for key in key_sequence:
            if len(key) == 1:
                vk_codes.append(ord(key.upper()))
            elif key in KEY_MAP:
                vk_codes.append(KEY_MAP[key])
            else:
                raise ValueError(f"无效的按键: {key}")

        # 按下修饰键
        for vk in vk_codes[:-1]:
            win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 0)
            time.sleep(delay)

        # 按下并释放最后一个键
        win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk_codes[-1], 0)
        time.sleep(delay)
        win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk_codes[-1], 0)

        # 释放修饰键（逆序）
        for vk in reversed(vk_codes[:-1]):
            win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, 0)
            time.sleep(delay)

    def _activate_window_before_keybd(self, keys: str) -> None:
        """发送前台按键前确保交易窗口在前台（避免泄漏到桌面/其他窗口）

        功能键（F1-F12）和 ESC 必须用 keybd_event 前台发送，但 keybd_event
        将按键发送到当前前台窗口。如果交易窗口不在前台，F1 会触发
        Windows 帮助（打开 Edge），造成用户描述的问题。

        防御逻辑：
        1. 获取交易窗口
        2. 如果窗口被最小化，先 ShowWindow(SW_RESTORE) 恢复
        3. click_input() 强制带到前台
        4. GetForegroundWindow() 句柄级校验（最多 2 次重试）
        5. 校验通过后才用 keybd_event 发送按键
        6. 窗口未找到或激活失败，**禁止发送按键**

        Raises:
            Exception: 交易窗口未找到或激活失败，不发送按键
        """
        window = self.get_trading_window()
        if window is None:
            self.logger.error(
                f"交易窗口未找到，禁止发送按键 '{keys}'（防止泄漏到桌面/其他窗口）"
            )
            raise Exception(
                f"交易窗口未找到，无法发送按键 '{keys}'。"
                f"请确认券商程序（网上股票交易系统5.0）已启动且窗口可见。"
            )

        hwnd = window.handle

        # 如果窗口被最小化，先恢复（click_input 对最小化窗口可能点击到无效坐标）
        if win32gui.IsIconic(hwnd):
            self.logger.info(f"检测到交易窗口已最小化，恢复后再发送按键 '{keys}'")
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.2)
            except Exception as e:
                self.logger.warning(f"恢复最小化窗口失败: {e}")
                # 继续尝试 click_input，这可能通过点击任务栏按钮恢复

        # click_input 带到前台 + 句柄级校验
        for attempt in range(2):
            try:
                window.click_input()
                time.sleep(0.3)
            except Exception as e:
                self.logger.warning(f"click_input 失败 (attempt {attempt + 1}): {e}")
                continue

            if win32gui.GetForegroundWindow() == hwnd:
                break

            self.logger.warning(
                f"激活后前台句柄 {win32gui.GetForegroundWindow():#x} ≠ "
                f"目标 {hwnd:#x} (attempt {attempt + 1})"
            )

        # 最终校验：确保前台确实是目标窗口
        if win32gui.GetForegroundWindow() != hwnd:
            self.logger.error(
                f"无法将交易窗口带到前台，前台={win32gui.GetForegroundWindow():#x} "
                f"目标={hwnd:#x}，禁止发送按键 '{keys}'"
            )
            raise Exception(
                f"无法激活交易窗口到前台，无法发送按键 '{keys}'。"
                f"当前前台窗口不是交易窗口。"
            )

        self._send_key_foreground(keys)

    def close_child_dialog(self, dialog_title: str = "") -> bool:
        """安全关闭子面板/对话框（如买入/卖出窗口），绝不关闭整个程序

        在 同花顺 xiadan.exe 中，按 F1/F2 打开的子面板（买入/卖出窗口）
        是嵌入在主窗口内的子视图，不是独立对话框。
        ESC/WM_CLOSE 均无法关闭这些子面板。

        正确的关闭方式：切换到其他视图（F4 = 查询/持仓视图）。

        Args:
            dialog_title: 面板标识（如 "买入"），仅用于日志记录

        Returns:
            是否成功关闭

        Raises:
            Exception: 交易窗口不存在
        """
        window = self.get_trading_window()
        if window is None:
            raise Exception("交易窗口未找到，无法关闭子面板")

        self.logger.info(
            f"关闭子面板 '{dialog_title}'：发送 F4 切换到查询视图"
        )

        # 在 同花顺 xiadan.exe 中，按 F4 切换回查询/持仓视图即可关闭买入/卖出子面板
        self.send_key("F4")
        time.sleep(0.5)

        # 验证：买入/卖出面板是否已关闭（检查是否有 "证券代码" 字段）
        try:
            for ctrl in window.descendants():
                try:
                    text = ctrl.window_text() or ""
                    if "证券代码" in text or "买入价格" in text or "卖出价格" in text:
                        self.logger.warning(
                            f"F4 后子面板仍存在（检测到 '{text[:20]}'），尝试再按一次 F4"
                        )
                        self.send_key("F4")
                        time.sleep(0.5)
                        break
                except Exception:
                    continue
        except Exception as e:
            self.logger.warning(f"验证子面板关闭状态时出错: {e}")

        self.logger.info(f"子面板 '{dialog_title}' 已通过 F4 切换关闭")
        return True

    # ------------------------------------------------------------
    # 状态重置
    # ------------------------------------------------------------

    def reset_window_state(self) -> None:
        """重置交易窗口到基准态（F1 买入界面）

        click_input 激活窗口 + ESC×5 确保从任意状态回退到 F1 买入界面。
        后续操作（下单/撤单/查询）依赖此函数确保窗口在前台且处于基准态，
        无需再关心窗口激活状态。

        每个 TaskQueue 任务开始前自动调用一次，查询方法内部也会调用。
        """
        window = self.get_trading_window()
        if window is None:
            raise Exception(
                "未找到交易窗口 '网上股票交易系统5.0'。"
                "请确认券商程序已启动且窗口可见。"
            )

        with timed("click_input 激活", self.logger):
            window.click_input()
            time.sleep(0.3)

        # ESC×5 确保从任意子面板/弹窗回退到 F1 买入界面
        # 使用 background=True 跳过冗余激活（click_input 已激活）
        with timed("ESC×5 回退到 F1", self.logger):
            for _ in range(5):
                self.send_key("ESC", background=True)
                time.sleep(0.1)

    # ------------------------------------------------------------
    # 剪切板
    # ------------------------------------------------------------

    def get_clipboard(self, retries: int = 3, delay: float = 0.1) -> Optional[str]:
        """获取剪切板数据（尝试多种格式：CF_UNICODETEXT → CF_TEXT）"""
        import win32clipboard
        import win32con
        formats = [win32con.CF_UNICODETEXT, win32con.CF_TEXT]

        for i in range(retries):
            for fmt in formats:
                try:
                    win32clipboard.OpenClipboard(0)
                    try:
                        data = win32clipboard.GetClipboardData(fmt)
                        if data:
                            return data
                    finally:
                        win32clipboard.CloseClipboard()
                except Exception:
                    continue
            if i < retries - 1:
                time.sleep(delay)

        self.logger.error(f"获取剪切板数据失败（已重试 {retries} 次）")
        return None

    def clear_clipboard(self) -> None:
        """清空剪贴板

        Ctrl+C 前清空，避免焦点丢失时读到陈旧内容伪装成"复制成功"。
        """
        import win32clipboard
        try:
            win32clipboard.OpenClipboard(0)
            try:
                win32clipboard.EmptyClipboard()
            finally:
                win32clipboard.CloseClipboard()
        except Exception as e:
            self.logger.warning(f"清空剪贴板失败: {e}")

    def get_foreground_window_exe(self) -> Optional[str]:
        """获取当前前台窗口所属进程的 exe 完整路径

        用于 Ctrl+C 前验证焦点是否在目标程序上，避免 keybd_event
        把按键发到错误窗口（如 IDE、浏览器），导致复制失败且无验证码弹窗。

        Returns:
            前台窗口进程的 exe 路径，获取失败返回 None
        """
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid)
            return proc.exe()
        except Exception as e:
            self.logger.warning(f"获取前台窗口进程路径失败: {e}")
            return None

    def is_foreground(self, app_path: str) -> bool:
        """检查当前前台窗口是否属于指定程序"""
        fg_exe = self.get_foreground_window_exe()
        if fg_exe is None or not app_path:
            return False
        return fg_exe.lower() == app_path.lower()
