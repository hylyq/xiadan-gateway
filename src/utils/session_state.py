"""WTS 会话连接状态查询（单一事实源）

健康门（TaskQueue._session_gate_reject）、/health（routes）、会话自愈
（WindowMonitor._recover_session_if_disconnected）三方共用，避免状态码
常量与 tuple 解包逻辑三处漂移。

win32ts 延迟导入 + 异常统一返回 None：调用方一律 fail-open（按可用
处理），旁路故障不阻塞主链路——与 window_monitor._rdp_client_connecting
同哲学。自愈侧对 None fail-closed（不执行 tscon）是刻意的不对称：
门的误放行由看门狗兜底，tscon 的误动作撞重连是图形栈僵死级事故。
"""
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
    """
    state = get_session_state()
    return {
        "connect_state": state,
        "state_name": state_name(state),
        "ui_available": None if state is None else state != WTS_DISCONNECTED,
    }
