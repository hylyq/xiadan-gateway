"""撤单服务

通过 F3 打开撤单界面，点击对应按钮:
- 30001: 全部撤单
- 30002: 撤买
- 30003: 撤卖

每次点击撤单按钮后都会弹出确认框（cid=1040 提示文字 + cid=6 "是(Y)" 按钮），
脚本自动点击"是(Y)"确认，无需依赖"撤单不需要确认"复选框。

进入撤单界面时若出现阻塞型提示弹窗（如非交易时段的 "Begin failed!"），
会先检测并关闭，避免弹窗遮挡撤单按钮导致点击失败。
"""
import time

from src.constants import (
    CANCEL_TYPE_MAP,
    CANCEL_CONFIRM_TEXT_ID, CANCEL_CONFIRM_YES_BUTTON_ID,
    CANCEL_CONFIRM_TEXT_KEYWORD
)
from src.models.config import AppConfig
from src.services.window_service import WindowService
from src.utils.logger import Logger
from src.utils.poll import poll_until, poll_until_not, timed, PollTimeoutError


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
        with timed("激活窗口", self.logger):
            trading_paths = self.config.get_trading_app_paths()
            if not trading_paths:
                raise Exception("未配置 xiadan.exe 路径")
            self.window_service.activate_window(trading_paths)

        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("未找到交易窗口")

        # 点击窗口聚焦
        window.click_input()
        time.sleep(0.1)

        # 刷新数据
        self.window_service.send_key("F5")
        time.sleep(0.1)

        # F3 打开撤单界面（带重试）
        # 非交易时段首次 F3 可能弹出 "Begin failed!" 弹窗导致撤单界面未加载，
        # 关闭弹窗后需重试 F3。以撤单按钮是否出现为成功判据。
        with timed("F3 打开撤单界面", self.logger):
            for f3_attempt in range(3):
                self.window_service.send_key("F3")
                self.logger.info(f"已发送 F3 打开委托撤单界面 (尝试 {f3_attempt + 1}/3)")
                time.sleep(0.3)

                window = self.window_service.get_trading_window()
                if window is None:
                    raise Exception("打开撤单界面后窗口消失")

                # 关闭可能出现的阻塞型提示弹窗（如非交易时段的 "Begin failed!"）
                self._dismiss_blocking_popup(window)

                # 重新获取窗口（弹窗关闭后控件树已变化），检查撤单按钮是否存在
                window = self.window_service.get_trading_window()
                if window is not None:
                    btn = self.window_service.find_element_in_window(window, control_id)
                    if btn is not None:
                        self.logger.info("撤单界面加载成功，撤单按钮已就绪")
                        break
                self.logger.warning(f"撤单界面未加载（未找到撤单按钮），将重试 F3")
            else:
                raise Exception(
                    f"打开撤单界面失败，未找到撤单按钮 control_id={control_id}（已重试 3 次）"
                )

        # 点击对应的撤单按钮
        with timed("点击撤单按钮", self.logger):
            self.window_service.click_element(window, control_id)
            self.logger.info(f"已点击 {operation_name} 按钮")

        # 轮询等待撤单确认弹窗出现（替代固定 sleep(0.5)）。
        # 快速交易模式下撤单也不弹确认窗（"撤单时是否需要确认"=否），
        # 仅可能弹通用提示窗（如非交易时段），1.0s 足够检测。
        with timed("等待撤单确认弹窗", self.logger):
            try:
                poll_until(
                    lambda: self.window_service.get_trading_window() is not None
                            and self.window_service.find_element_in_window(
                                self.window_service.get_trading_window(),
                                CANCEL_CONFIRM_TEXT_ID
                            ) is not None,
                    timeout=1.0, interval=0.1,
                    description=f"{operation_name} 确认弹窗"
                )
            except PollTimeoutError:
                pass

        # 检测撤单确认弹窗（每次撤单都会弹出，脚本自动点"是(Y)"确认）
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

                # 区分两种弹窗：撤单确认弹窗包含"委托"，其他弹窗不包含
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
                    # 非撤单确认弹窗，尝试用 Y 键关闭
                    self.logger.info("检测到非撤单确认弹窗，尝试用 Y 键关闭")
                    try:
                        self.window_service.click_element(
                            window, CANCEL_CONFIRM_YES_BUTTON_ID
                        )
                    except Exception:
                        self.window_service.send_key("Y")
                    time.sleep(0.3)

        if not confirm_dialog_shown:
            self.logger.info("未出现撤单确认弹窗（可能无委托可撤）")

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

    def _dismiss_blocking_popup(self, window) -> bool:
        """检测并关闭阻塞型提示弹窗（如非交易时段的 "Begin failed!"）

        同花顺在非交易时段进入撤单界面时可能弹出提示窗（标题"提示"，
        内容如 "Begin failed!"），阻挡撤单按钮。此方法检测并关闭这类弹窗。

        Returns:
            是否关闭了弹窗
        """
        if window is None:
            return False
        popup_keywords = ["Begin failed", "failed", "提示"]
        try:
            for ctrl in window.descendants():
                try:
                    text = ctrl.window_text() or ""
                    if any(kw in text for kw in popup_keywords):
                        self.logger.info(f"检测到提示弹窗: {text[:80]}，尝试关闭")
                        # 尝试点击"确定"按钮（标准对话框 IDOK=1, IDCANCEL=2）
                        for btn_id in (1, 2):
                            btn = self.window_service.find_element_in_window(window, btn_id)
                            if btn is not None:
                                btn.click_input()
                                self.logger.info(f"已点击按钮 cid={btn_id} 关闭弹窗")
                                # 轮询等待弹窗消失（替代固定 sleep(0.5)）
                                try:
                                    poll_until_not(
                                        lambda: self._has_popup_text(popup_keywords),
                                        timeout=2.0, interval=0.1,
                                        description="弹窗关闭"
                                    )
                                except (PollTimeoutError, Exception):
                                    pass
                                return True
                        # 找不到按钮则用 ENTER 关闭
                        self.window_service.send_key("{ENTER}")
                        try:
                            poll_until_not(
                                lambda: self._has_popup_text(popup_keywords),
                                timeout=2.0, interval=0.1,
                                description="弹窗关闭(ENTER)"
                            )
                        except (PollTimeoutError, Exception):
                            pass
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _has_popup_text(self, popup_keywords: list) -> bool:
        """检查窗口中是否存在弹窗特征文本（用于 poll_until_not）
        
        Returns:
            True=弹窗仍存在, False=弹窗已关闭或窗口不可用
        """
        window = self.window_service.get_trading_window()
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
