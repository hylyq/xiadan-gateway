"""WTS 会话连接状态 + 桌面僵死追踪（单一事实源）

四方共用：健康门（TaskQueue._session_gate_reject）、/health（routes）、
会话自愈（WindowMonitor._recover_session_if_disconnected）、桌面僵死
升级自愈（WindowMonitor._recover_desktop_if_wedged），避免状态码常量
与 tuple 解包逻辑多处漂移。

win32ts 延迟导入 + 异常统一返回 None：调用方一律 fail-open（按可用
处理），旁路故障不阻塞主链路——与 window_monitor._rdp_client_connecting
同哲学。自愈侧对 None fail-closed（不执行 tscon/tsdiscon）是刻意的
不对称：门的误放行由看门狗兜底，tscon/tsdiscon 的误动作撞重连是
图形栈僵死级事故。

【桌面僵死（形态二，2026-10-09 事故）】WTS=Active 但输入/图形路径
无响应：SetCursorPos error 0（pywinauto 改写为 "There is no active
desktop"）、screen grab 失败、GetForegroundWindow() 恒 0——与
「RDP 断开」的 UI 症状相同，但 ui_available（≡WTS≠4）恒真，健康门
放行、既有自愈（只认 state==4）永不触发。任务失败指纹反推是唯一
低成本检测面：连续 ≥2 次桌面级激活失败即标记 wedged，任一任务成功
即清除（成功本身就是「已复位」最清晰的探针），超时自动过期防残留。
"""
import threading
import time
from typing import Optional

# WTS_CONNECTSTATE_CLASS（wtsapi.h）。交互会话实际只出现 0/1/4：
# 0=Active（有活动桌面，UI 自动化可用）
# 1=Connected（附加过渡瞬态，放行）
# 4=Disconnected（RDP 断开未自愈——无活动桌面，click_input 落空）
WTS_DISCONNECTED = 4

WTS_STATE_NAMES = {
    0: "Active",
    1: "Connected",
    2: "ConnectQuery",
    3: "Shadow",
    4: "Disconnected",
    5: "Idle",
    6: "Listen",
    7: "Reset",
    8: "Down",
}

# ------------------------------------------------------------
# 桌面僵死追踪（会话级内存态，不持久化）
# 喂入方：TaskQueue._record_task_outcome（成功清零 / 指纹失败 +1）
# 消费方：session_health()（/health 消歧）、
#         WindowMonitor._recover_desktop_if_wedged（升级自愈触发）
# ------------------------------------------------------------

# 连续失败达标线：1 次可能是瞬态（前台恰在切换），2 次连续才值得断人 RDP
WEDGE_STREAK_THRESHOLD = 2
# 最后一次指纹失败距此时长超过该值即视为过期（任务流停了，标记不残留）
WEDGE_EXPIRY_SECONDS = 900.0

_wedge_lock = threading.Lock()
_wedge_streak = 0
_wedge_last_failure = 0.0


def record_desktop_failure() -> None:
    """记录一次桌面级激活失败（SESSION_DESKTOP_UNAVAILABLE 或
    "no active desktop" 指纹）"""
    global _wedge_streak, _wedge_last_failure
    with _wedge_lock:
        _wedge_streak += 1
        _wedge_last_failure = time.time()


def record_desktop_success() -> None:
    """任务成功 = 桌面可操作铁证，清零"""
    global _wedge_streak
    with _wedge_lock:
        _wedge_streak = 0


def desktop_wedge_status() -> dict:
    """当前僵死标记视图

    Returns:
        {"wedged": bool, "streak": int}——wedged=streak 达标且未过期
    """
    with _wedge_lock:
        expired = (time.time() - _wedge_last_failure) > WEDGE_EXPIRY_SECONDS
        return {
            "wedged": (_wedge_streak >= WEDGE_STREAK_THRESHOLD
                       and not expired),
            "streak": _wedge_streak,
        }


def get_session_state() -> Optional[int]:
    """查询本进程所在会话的 WTS 连接状态码

    Returns:
        状态码 int（0=Active ... 4=Disconnected）；任何异常返回 None（未知）
    """
    try:
        import win32ts

        state = win32ts.WTSQuerySessionInformation(
            win32ts.WTS_CURRENT_SERVER_HANDLE,
            win32ts.WTS_CURRENT_SESSION, win32ts.WTSConnectState)
        if isinstance(state, tuple):  # 部分 pywin32 版本返回 (code,)
            state = state[0]
        return int(state)
    except Exception:
        return None


def state_name(state: Optional[int]) -> str:
    """状态码 → 名称（None/未知码 → Unknown）"""
    if state is None:
        return "Unknown"
    return WTS_STATE_NAMES.get(state, f"Unknown({state})")


def session_health() -> dict:
    """/health 用视图：ui_available 语义 = 会话健康门是否会放行

    ui_available=None 表示查询失败（未知），非 false（确定不可用）——
    调用方据此区分「确定不可用」与「检测失效」，与门的 fail-open 一致。

    desktop_wedged 与 ui_available 正交：前者=会话挂接（WTS 层），
    后者=桌面可操作性（输入/图形层，任务失败指纹反推）。两者皆真
    即「挂接但僵死」态——ui_available 假绿灯的消歧字段。
    """
    state = get_session_state()
    wedge = desktop_wedge_status()
    return {
        "connect_state": state,
        "state_name": state_name(state),
        "ui_available": None if state is None else state != WTS_DISCONNECTED,
        "desktop_wedged": wedge["wedged"],
        "desktop_wedge_streak": wedge["streak"],
    }
