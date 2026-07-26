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
from src.utils.poll import poll_until, timed, PollTimeoutError


class TradingService:
    """撤单服务"""

    # 撤单过程是否出现弹窗（类变量，跨实例持久化）
    # TaskQueue 读取此标志判断连续撤单能否跳过准备操作
    _had_dialog = False

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

        # 重置弹窗标志：本次执行过程中若遇到弹窗，设为 True
        TradingService._had_dialog = False

        # 连续同向跳过：上次撤单成功后窗口仍在 F3，无需重置/激活/按键
        from src.api.task_queue import TaskQueue
        _task_queue = TaskQueue.get_instance()
        _skip_setup = _task_queue.skip_window_setup
        if _skip_setup:
            self.logger.info("连续同向撤单，跳过窗口激活与 F3 导航")
            _task_queue.skip_window_setup = False
        else:
            with timed("激活窗口", self.logger):
                trading_paths = self.config.get_trading_app_paths()
                if not trading_paths:
                    raise Exception("未配置 xiadan.exe 路径")
                self.window_service.activate_window(trading_paths)

        window = self.window_service.get_trading_window()
        if window is None:
            raise Exception("未找到交易窗口")
        _descendants = list(window.descendants())

        if not _skip_setup:
            # 刷新数据（窗口已在激活步骤置前，用 background 跳过冗余激活）
            self.window_service.send_key("F5", background=True)
            time.sleep(0.1)

            # F3 打开撤单界面（带重试）
            # 非交易时段首次 F3 可能弹出 "Begin failed!" 弹窗导致撤单界面未加载，
            # 关闭弹窗后需重试 F3。以撤单按钮是否出现为成功判据。
            with timed("F3 打开撤单界面", self.logger):
                for f3_attempt in range(3):
                    self.window_service.send_key("F3", background=True)
                    self.logger.info(f"已发送 F3 打开委托撤单界面 (尝试 {f3_attempt + 1}/3)")
                    time.sleep(0.3)

                    window = self.window_service.get_trading_window()
                    if window is None:
                        raise Exception("打开撤单界面后窗口消失")
                    _descendants = list(window.descendants())

                    # 检查阻塞型弹窗（复用已有 descendants，无需重遍历）
                    _has_blocking = self._has_blocking_text(_descendants)
                    if _has_blocking and self._dismiss_blocking_popup(window):
                        window = self.window_service.get_trading_window()
                        if window is None:
                            raise Exception("弹窗关闭后窗口消失")
                        _descendants = list(window.descendants())

                    btn = self.window_service.find_element_in_window(
                        window, control_id, descendants=_descendants)
                    if btn is not None:
                        if not self._is_button_enabled(btn):
                            self.logger.info(f"撤单按钮 {control_id} 已存在但灰显（无委托可撤）")
                            return {
                                "cancel_type": operation_name,
                                "success": False,
                                "cancelled_count": 0,
                                "reason": "当前无可撤委托",
                            }
                        self.logger.info("撤单界面加载成功，撤单按钮已就绪")
                        break
                    self.logger.warning(f"撤单界面未加载（未找到撤单按钮），将重试 F3")
                else:
                    raise Exception(
                        f"打开撤单界面失败，未找到撤单按钮 control_id={control_id}（已重试 3 次）"
                    )

        # 点击对应的撤单按钮（复用缓存的 descendants）
        with timed("点击撤单按钮", self.logger):
            self.window_service.click_element(window, control_id, descendants=_descendants)
            self.logger.info(f"已点击 {operation_name} 按钮")

        # 统一弹窗检测与处理：sleep(0.4) 等待渲染 + 一次 descendants 遍历
        # 替代 poll_until(_has_cancel_dialog) + 二次遍历，与买入流程优化思路一致
        cancelled_count = None
        confirm_dialog_shown = False
        with timed("撤单弹窗检测与处理", self.logger):
            time.sleep(0.4)

            window = self.window_service.get_trading_window_fast()
            if window is not None:
                _descendants = list(window.descendants())
                text_el = self.window_service.find_element_in_window(
                    window, CANCEL_CONFIRM_TEXT_ID, descendants=_descendants
                )
                if text_el is not None:
                    prompt_text = text_el.window_text() or ""
                    self.logger.info(f"撤单后检测到弹窗文字: {prompt_text[:200]}")

                    if CANCEL_CONFIRM_TEXT_KEYWORD in prompt_text:
                        # 撤单确认弹窗
                        confirm_dialog_shown = True
                        cancelled_count = self._parse_cancelled_count(prompt_text)
                        try:
                            self.window_service.click_element(
                                window, CANCEL_CONFIRM_YES_BUTTON_ID, descendants=_descendants
                            )
                            self.logger.info("已点击 '是(Y)' 确认撤单")
                        except Exception as e:
                            self.logger.warning(f"点击 '是(Y)' 失败，尝试 Y 键: {e}")
                            self.window_service.send_key("Y")
                    else:
                        # 非撤单确认弹窗，用 Y 键关闭
                        self.logger.info("检测到非撤单确认弹窗，尝试用 Y 键关闭")
                        try:
                            self.window_service.click_element(
                                window, CANCEL_CONFIRM_YES_BUTTON_ID, descendants=_descendants
                            )
                        except Exception:
                            self.window_service.send_key("Y")
                else:
                    self.logger.info(
                        "未出现撤单确认弹窗（快速交易模式撤单已直接提交，"
                        "或当前无可撤委托）"
                    )

        self.logger.info(
            f"{operation_name} 操作完成, 撤单数量: {cancelled_count}, "
            f"出现确认弹窗: {confirm_dialog_shown}"
        )

        # 记录弹窗标志：有弹窗 = 下次同向不可跳过
        TradingService._had_dialog = confirm_dialog_shown

        return {
            "cancel_type": operation_name,
            "success": True,
            "cancelled_count": cancelled_count,
            "confirm_dialog_shown": confirm_dialog_shown
        }

    @staticmethod
    def _has_blocking_text(descendants) -> bool:
        """检查 descendants 中是否有阻塞型弹窗特征文本（不遍历 UIA 树）"""
        popup_keywords = ["Begin failed", "failed", "失败", "事务处理机"]
        try:
            for el in descendants:
                try:
                    text = el.window_text() or ""
                    if any(kw in text for kw in popup_keywords):
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _dismiss_blocking_popup(self, window) -> bool:
        """检测并关闭阻塞型提示弹窗（委托给 WindowService 统一处理）"""
        return self.window_service.dismiss_blocking_popup(window)

    @staticmethod
    def _is_button_enabled(btn) -> bool:
        """检查按钮是否可点击（非灰显）"""
        try:
            return btn.is_enabled()
        except Exception:
            # 降级：若 is_enabled() 不可用，假定可用
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
