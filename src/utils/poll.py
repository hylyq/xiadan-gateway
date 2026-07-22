"""轮询等待与计时工具

- poll_until / poll_until_not: 事件驱动等待，替代固定 time.sleep()
- timed: 上下文管理器，记录代码块耗时

用法:
    from src.utils.poll import poll_until, poll_until_not, timed, PollTimeoutError

    # 等待控件出现
    poll_until(lambda: find_element(window, cid), timeout=3.0)

    # 记录代码块耗时
    with timed("Ctrl+C 发送", logger):
        _send_ctrl_c()
"""
import time
from contextlib import contextmanager
from typing import Optional


@contextmanager
def timed(description: str, logger: Optional[object] = None):
    """记录代码块耗时

    with timed("重置窗口", log):
        reset_window_state()

    # 日志输出: [计时] 重置窗口: 1.23s
    """
    start = time.time()
    try:
        yield
    finally:
        elapsed = time.time() - start
        msg = f"[计时] {description}: {elapsed:.2f}s"
        if logger:
            logger.info(msg)
        else:
            print(msg)


class PollTimeoutError(TimeoutError):
    """轮询超时异常"""
    pass


def poll_until(condition, timeout=5.0, interval=0.1, description=""):
    """轮询等待条件满足

    Args:
        condition: 可调用对象，返回 truthy 值表示条件满足
        timeout: 超时秒数
        interval: 轮询间隔（秒）
        description: 描述信息，超时时用于错误消息

    Returns:
        condition() 的返回值

    Raises:
        PollTimeoutError: 超时后仍未满足
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(interval)
    raise PollTimeoutError(
        f"等待超时 ({timeout}s): {description or '条件未满足'}"
    )


def poll_until_not(condition, timeout=5.0, interval=0.1, description=""):
    """轮询等待条件不满足（condition 返回 falsy）

    Args:
        condition: 可调用对象，返回 falsy 值表示条件已消失
        timeout: 超时秒数
        interval: 轮询间隔（秒）
        description: 描述信息，超时时用于错误消息

    Returns:
        True

    Raises:
        PollTimeoutError: 超时后条件仍未消失
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not condition():
            return True
        time.sleep(interval)
    raise PollTimeoutError(
        f"等待超时 ({timeout}s): {description or '条件未消失'}"
    )
