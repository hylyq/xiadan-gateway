"""撤单服务

通过 F3 打开撤单界面，点击对应按钮:
- 30001: 全部撤单
- 30002: 撤买
- 30003: 撤卖

进入撤单界面后会勾选"撤单不需要确认"复选框（cid=2411），
这样点击撤单按钮后不再弹出确认框，直接执行撤单。
若未勾选则点击后会弹出确认框（cid=1040 提示文字 + cid=6 "是(Y)" 按钮），
作为退路仍会处理。
"""
import time

from src.constants import (
    CANCEL_TYPE_MAP, NO_CONFIRM_CHECKBOX_ID,
    CANCEL_CONFIRM_TEXT_ID, CANCEL_CONFIRM_YES_BUTTON_ID,
    CANCEL_CONFIRM_TEXT_KEYWORD
)
from src.models.config import AppConfig
from src.services.window_service import WindowService
from src.utils.logger import Logger


class TradingService:
    """撤单服务"""

    def __init__(self, window_service: WindowService):
        self.window_service = window_service
        self.config = AppConfig()
        self.logger = Logger.get_instance()

    def cancel_all_orders(self, cancel_type: str = "A") -> dict:
        """撤单

        Args:
            cancel_type: 撤单类型
                - 'A': 全部撤单（默认）
                - 'X': 撤买
                - 'C': 撤卖

        Returns:
            {
                "cancel_type": "全部撤单",
                "success": True,
                "cancelled_count": N or None,
                "confirm_dialog_shown": bool  # 是否出现撤单确认弹窗
            }
        """
        cancel_type = (cancel_type or "A").upper()
        if cancel_type not in CANCEL_TYPE_MAP:
            raise Exception(f"无效的撤单类型: {cancel_type}，可选: A(全部)/X(撤买)/C(撤卖)")

        control_id, operation_name = CANCEL_TYPE_MAP[cancel_type]
        self.logger.info(f"开始撤单: {operation_name}")

        # 激活窗口（撤单需要前台激活，因为 click_input() 需要焦点）
        trading_path = self.config.get_trading_app_path()
        if not trading_path:
            raise Exception("未配置 xiadan.exe 路径")
        self.window_service.activate_window(trading_path)

        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("未找到交易窗口")

        # 点击窗口聚焦
        window.click_input()
        time.sleep(0.1)

        # 刷新数据
        self.window_service.send_key("F5")
        time.sleep(0.1)

        # F3 打开撤单界面
        self.window_service.send_key("F3")
        self.logger.info("已打开委托撤单界面")
        time.sleep(0.3)

        # 重新获取窗口（界面已切换）
        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("打开撤单界面后窗口消失")

        # 勾选"撤单不需要确认"复选框
        # 首次勾选会弹出"您取消了撤单前确认提示功能"的二次确认框，需要点"是(Y)"
        no_confirm = self.window_service.find_element_in_window(
            window, NO_CONFIRM_CHECKBOX_ID
        )
        checkbox_checked = False
        if no_confirm is not None:
            try:
                # 读取当前勾选状态（0=未勾选, 1=已勾选, 2=不定）
                toggle_state = no_confirm.get_toggle_state()
                is_checked = (toggle_state == 1)
                self.logger.info(
                    "'撤单不需要确认' 复选框当前状态: %s",
                    "已勾选" if is_checked else "未勾选"
                )

                if not is_checked:
                    no_confirm.click_input()
                    self.logger.info("已点击 '撤单不需要确认' 复选框")
                    time.sleep(0.4)

                    # 处理可能出现的"取消确认提示功能"二次确认弹窗
                    self._handle_disable_confirm_dialog()

                checkbox_checked = True
            except Exception as e:
                self.logger.warning(f"处理 '撤单不需要确认' 复选框失败: {e}")
        else:
            self.logger.warning("未找到 '撤单不需要确认' 复选框")

        # 重新获取窗口
        window = self.window_service.get_trading_window()

        # 点击对应的撤单按钮
        self.window_service.click_element(window, control_id)
        self.logger.info(f"已点击 {operation_name} 按钮")
        time.sleep(0.5)

        # 检测是否出现撤单确认弹窗（始终检测，不依赖复选框状态）
        cancelled_count = None
        confirm_dialog_shown = False
        window = self.window_service.get_trading_window()
        if window is not None:
            text_el = self.window_service.find_element_in_window(
                window, CANCEL_CONFIRM_TEXT_ID
            )
            if text_el is not None:
                prompt_text = text_el.window_text() or ""
                self.logger.info(f"撤单后检测到弹窗文字: {prompt_text[:200]}")

                # 区分两种弹窗：撤单确认弹窗包含"委托"，关闭提示功能的弹窗不包含
                if CANCEL_CONFIRM_TEXT_KEYWORD in prompt_text:
                    confirm_dialog_shown = True
                    cancelled_count = self._parse_cancelled_count(prompt_text)

                    try:
                        self.window_service.click_element(
                            window, CANCEL_CONFIRM_YES_BUTTON_ID
                        )
                        self.logger.info("已点击 '是(Y)' 确认撤单")
                        time.sleep(0.3)
                    except Exception as e:
                        self.logger.warning(f"点击 '是(Y)' 失败，尝试 Y 键: {e}")
                        self.window_service.send_key("Y")
                        time.sleep(0.3)
                else:
                    # 可能是"取消提示功能"的二次确认弹窗（兜底处理）
                    self.logger.info("检测到非撤单确认弹窗，尝试用 Y 键关闭")
                    try:
                        self.window_service.click_element(
                            window, CANCEL_CONFIRM_YES_BUTTON_ID
                        )
                    except Exception:
                        self.window_service.send_key("Y")
                    time.sleep(0.3)

        if not confirm_dialog_shown:
            self.logger.info("未出现撤单确认弹窗（'撤单不需要确认'已生效 或 无委托可撤）")

        self.logger.info(
            f"{operation_name} 操作完成, 撤单数量: {cancelled_count}, "
            f"出现确认弹窗: {confirm_dialog_shown}"
        )

        return {
            "cancel_type": operation_name,
            "success": True,
            "cancelled_count": cancelled_count,
            "confirm_dialog_shown": confirm_dialog_shown
        }

    def _handle_disable_confirm_dialog(self) -> bool:
        """处理勾选"撤单不需要确认"后出现的二次确认弹窗

        弹窗文字: "您取消了撤单前确认提示功能。您确定理解该项设置的用法..."
        这是首次关闭确认提示时的安全确认，点击"是(Y)"确认关闭。

        Returns:
            是否出现了该弹窗并已处理
        """
        window = self.window_service.get_trading_window()
        if window is None:
            return False

        text_el = self.window_service.find_element_in_window(
            window, CANCEL_CONFIRM_TEXT_ID
        )
        if text_el is None:
            return False

        prompt_text = text_el.window_text() or ""
        # 通过关键词区分：这个弹窗包含"取消"或"风险"，不包含"委托"
        if "委托" in prompt_text:
            # 这是撤单确认弹窗，不该在这里处理
            return False

        self.logger.info(f"检测到关闭确认提示的二次确认弹窗: {prompt_text}")
        try:
            self.window_service.click_element(window, DISABLE_CONFIRM_YES_BUTTON_ID)
            self.logger.info("已点击 '是(Y)' 确认关闭撤单确认提示")
            time.sleep(0.3)
            return True
        except Exception as e:
            self.logger.warning(f"点击 '是(Y)' 失败，尝试 Y 键: {e}")
            self.window_service.send_key("Y")
            time.sleep(0.3)
            return True

    @staticmethod
    def _parse_cancelled_count(text: str):
        """从撤单确认弹窗文本解析可撤委托数

        示例文本: "您确认要撤销这( 2 )笔委托吗？\\n\\n( 总共 2 笔可撤委托 )"
        """
        import re
        match = re.search(r"\(\s*(\d+)\s*\)", text)
        if match:
            return int(match.group(1))
        return None
