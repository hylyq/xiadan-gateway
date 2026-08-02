"""UIA 控件安全访问工具

pywinauto 的 UIA 元素在窗口刷新/弹窗关闭后立即失效，
window_text() / element_info 读取会抛异常。历史代码用裸 try/except
静默跳过，故障时日志完全哑火，只能靠截图猜。

统一封装安全访问：失败返回空值并记录 debug 日志。
- 默认 INFO 级别下 debug 不可见，零噪音
- 排查时将 logging.level 改为 DEBUG，界面变化导致的控件失效一目了然
"""
from src.utils.logger import Logger


def safe_text(el) -> str:
    """安全读取控件文本，失败返回空串（记录 debug 日志）"""
    try:
        return el.window_text() or ""
    except Exception as e:
        Logger.get_instance().debug(
            f"控件文本读取失败: {type(el).__name__}: {e}"
        )
        return ""


def safe_control_type(el) -> str:
    """安全读取控件类型（如 "Tree"/"TreeItem"/"Button"），失败返回空串"""
    try:
        return el.element_info.control_type or ""
    except Exception as e:
        Logger.get_instance().debug(
            f"控件类型读取失败: {type(el).__name__}: {e}"
        )
        return ""
