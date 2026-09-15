"""撤单服务

F1/F2/F3 页面均有 全撤/撤买/撤卖/撤最后 按钮（cid 跨页一致，实测
30001/30002/30003/1946），当前页面直接点击即可，无需切换到 F3 撤单页:
- 30001: 全部撤单（type=A）
- 30002: 撤买（type=X）
- 30003: 撤卖（type=C）
- 1946:  撤最后（type=L，撤销最近一笔委托）

每次点击撤单按钮后都会弹出确认框（cid=1040 提示文字 + cid=6 "是(Y)" 按钮），
脚本自动点击"是(Y)"确认，无需依赖"撤单不需要确认"复选框。

进入撤单界面时若出现阻塞型提示弹窗（如非交易时段的 "Begin failed!"），
会先检测并关闭，避免弹窗遮挡撤单按钮导致点击失败。
"""
import time

from src.api.task_queue import report_window_state
from src.constants import (
    CANCEL_TYPE_MAP,
    CANCEL_CONFIRM_TEXT_ID, CANCEL_CONFIRM_YES_BUTTON_ID,
    CANCEL_CONFIRM_TEXT_KEYWORD,
    BLOCKING_POPUP_KEYWORDS,
)
from src.models.config import AppConfig
from src.services.window_service import WindowService
from src.utils.logger import Logger
from src.utils.poll import poll_until, timed, PollTimeoutError
from src.utils.uia import safe_text


class TradingService:
    """撤单服务"""

    def __init__(self, window_service: WindowService):
        self.window_service = window_service
        self.config = AppConfig()
        self.logger = Logger.get_instance()
        # 撤单过程是否出现弹窗（@report_window_state 装饰器上报给 TaskQueue，
        # 连续撤单能否跳过准备操作的依据）
        self._had_dialog = False

    @report_window_state
    def cancel_all_orders(self, cancel_type: str = "A") -> dict:
        """撤单

        Args:
            cancel_type: 撤单类型
                - 'A': 全部撤单（默认）
                - 'X': 撤买
                - 'C': 撤卖
                - 'L': 撤最后（撤销最近一笔委托）

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
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"无效的撤单类型: {cancel_type}，可选: A(全部)/X(撤买)/C(撤卖)/L(撤最后)"
            )

        control_id, operation_name = CANCEL_TYPE_MAP[cancel_type]
        self.logger.info(f"开始撤单: {operation_name}")

        # 重置弹窗标志：本次执行过程中若遇到弹窗，设为 True
        self._had_dialog = False

        # 撤单统一在 F3 页操作: 该页已勾选"撤单不需要确认"（客户端设置），
        # 点击按钮直接生效；F1/F2 页的同类按钮会弹确认框（结构未知），
        # 且四按钮 cid 跨页一致（实测 30001/30002/30003/1946），F3 一次
        # 按键即可到达。按钮灰显 = 当前无可撤委托。
        window = self.window_service.get_trading_window()
        if window is None:
            raise ApiError(ErrorCode.WINDOW_NOT_FOUND, "未找到交易窗口")

        self.window_service.send_key("F3", background=True)
        btn = None
        for attempt in range(3):
            time.sleep(0.15 if attempt == 0 else 0.3)
            window = self.window_service.get_trading_window()
            if window is None:
                continue
            if self._has_blocking_text(list(window.descendants()))                     and self._dismiss_blocking_popup(window):
                window = self.window_service.get_trading_window()
            btn = self.window_service.find_element_in_window(window, control_id)
            if btn is not None:
                break
        if btn is None:
            raise ApiError(
                ErrorCode.CONTROL_NOT_FOUND,
                f"未找到 {operation_name} 按钮 control_id={control_id}（F3 界面未加载）",
                suggestion="F3 撤单页未就绪，请稍后重试或人工确认券商界面正常"
            )

        if not self._is_button_enabled(btn):
            self.logger.info(f"{operation_name} 按钮灰显（当前无可撤委托）")
            return {
                "cancel_type": operation_name,
                "success": False,
                "cancelled_count": 0,
                "reason": "当前无可撤委托",
            }

        with timed("点击撤单按钮", self.logger):
            self.window_service.click_element(window, control_id)
            self.logger.info(f"已点击 {operation_name} 按钮")

        # 统一弹窗检测与处理：sleep(0.2) 等待渲染 + 一次 descendants 遍历
        cancelled_count = None
        confirm_dialog_shown = False
        with timed("撤单弹窗检测与处理", self.logger):
            time.sleep(0.2)  # 弹窗 <0.15s 出现

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
        self._had_dialog = confirm_dialog_shown

        return {
            "cancel_type": operation_name,
            "success": True,
            "cancelled_count": cancelled_count,
            "confirm_dialog_shown": confirm_dialog_shown
        }

    @staticmethod
    def _has_blocking_text(descendants) -> bool:
        """检查 descendants 中是否有阻塞型弹窗特征文本（不遍历 UIA 树）"""
        for el in descendants:
            text = safe_text(el)
            if any(kw in text for kw in BLOCKING_POPUP_KEYWORDS):
                return True
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
