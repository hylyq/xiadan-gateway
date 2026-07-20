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
from pywinauto.clipboard import GetData

from config.key_config import KEY_MAP
from src.utils.logger import Logger


# 交易窗口标题（用于查找目标窗口）
TRADING_WINDOW_TITLE = "网上股票交易系统5.0"


class WindowService:
    """窗口操作服务"""

    def __init__(self):
        self.logger = Logger.get_instance()

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

    def activate_window(self, app_path: str, foreground: bool = True) -> int:
        """根据 exe 完整路径激活窗口

        Args:
            app_path: 应用程序完整路径（如 xiadan.exe 的绝对路径）
            foreground: True=强制置前（默认，原有行为）；False=仅恢复窗口不抢焦点，
                        配合 PostMessage 后台按键使用

        Returns:
            窗口句柄

        Raises:
            Exception: 未找到匹配窗口
        """
        hwnd_found = None

        def callback(hwnd, extra):
            nonlocal hwnd_found
            if win32gui.IsWindowVisible(hwnd):
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                try:
                    proc = psutil.Process(pid)
                    if proc.exe().lower() == app_path.lower():
                        hwnd_found = hwnd
                        if win32gui.IsIconic(hwnd):
                            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                        if foreground:
                            self._set_foreground(hwnd)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return True

        win32gui.EnumWindows(callback, None)

        if hwnd_found:
            return hwnd_found
        raise Exception(f"未找到匹配窗口，路径: {app_path}")

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
        """获取同花顺交易窗口（网上股票交易系统5.0）"""
        return self.get_target_window({"title": TRADING_WINDOW_TITLE})

    # ------------------------------------------------------------
    # 控件查找（支持单个 / 批量）
    # ------------------------------------------------------------

    def find_element_in_window(self, window, control_id: Union[int, str, List, tuple]):
        """在窗口中查找控件

        注意: window.descendants() 会缓存控件树。界面变化后必须重新 get_target_window。

        Args:
            window: 目标窗口对象
            control_id: 单个 control_id（int/str）或 control_id 列表（list/tuple）

        Returns:
            单个 id 返回元素或 None；
            多个 id 返回元素列表（按找到顺序）
        """
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

    def send_key(self, keys: str, hwnd: Optional[int] = None) -> None:
        """发送按键（支持组合键）

        Args:
            keys: 按键序列，如 'F1', 'Y', '{CTRL+C}'
            hwnd: 目标窗口句柄。提供时用 PostMessage 后台发送，不抢焦点；
                  不提供时自动查找交易窗口句柄（优先 PostMessage），
                  找不到则 fallback 到 keybd_event（原有行为）

        注意:
            功能键（F1-F12）始终走 keybd_event 前台发送，因为这类键触发界面切换，
            PostMessage 可能无法正确触发窗口的加速键/快捷键处理。
            字母键（Y/N）、方向键、ESC、ENTER、组合键（Ctrl+C）等走 PostMessage。

        用法:
            send_key('F1')              # 单键
            send_key('Y')               # 字母键
            send_key('{CTRL+C}')        # 组合键（花括号内，+连接）
            send_key('CTRL C')          # 组合键（空格分隔，按下后释放）
        """
        # 功能键（F1-F12）始终前台发送，PostMessage 无法可靠触发窗口快捷键
        if self._contains_function_key(keys):
            self._send_key_foreground(keys)
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

    # ------------------------------------------------------------
    # 剪切板
    # ------------------------------------------------------------

    def get_clipboard(self, retries: int = 3, delay: float = 0.1) -> Optional[str]:
        """获取剪切板数据"""
        for i in range(retries):
            try:
                data = GetData()
                if data:
                    return data
            except Exception as e:
                if i == retries - 1:
                    self.logger.error(f"获取剪切板数据失败: {str(e)}")
                time.sleep(delay)
        return None
