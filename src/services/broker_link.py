"""券商连接态只读探测：状态栏「断开」字样定向读取（/health 用）

实测基线（2026-10-10 本地物理断网双周期，trace 在 logs/netcycle*）：
- 可靠信号 = 状态栏连接 Pane 内状态格文本「断开」（连接态该格空文本，
  Pane 含 时间格 + 状态格 + 主站名格（mncgXXX/专有云NNN…，命名不固定，
  不设关键字白名单）；全树扫描命中唯一）
- 主站名每次重连会变（实测 mncg 112/113/114/18；专有云部署另有命名），
  不可绑定——「断开」二字才是唯一可靠信号
- 断网 →「断开」出现延迟 24~27s（心跳周期量级）：connected=True 只代表
  「未观测到断开」（~30s 盲区），不等于网络正常
- 恢复后「断开」不必然自动消失：空闲客户端持续挂「断开」（45s+ 直至
  手动刷新）；有 UI 操作触发重连则秒级清除（实测 0.9s）
- 断开态下查询类操作静默返回客户端缓存数据（HTTP 200、网关日志无痕）——
  调用方判断数据新鲜度必须看本字段

实现：Pane 定位不走 UIA 全树（冷扫 ~1.2s），走 win32 几何判据——主窗口
底部 60px 内 class=AfxWnd140s、高<40、宽≥50 的子窗里取 left 最大者
（连接 Pane 恒在状态栏最右段，实测 window 移动/缩放下均成立）。
只缓存 win32 句柄（无 COM 线程亲和），UIA wrapper 每次在调用线程现建，
适配 waitress 多线程按需读取；定向读 ~6-25ms/次。

零输入不扰动：仅 UIA/win32 只读；按需读取不后台轮询
（架构铁律：单 UI 资源禁主动轮询）。
"""
import re
import threading
import time

import psutil
import win32gui
import win32process
from pywinauto import Desktop

from src.constants import TRADING_WINDOW_TITLE
from src.exceptions import ApiError, ErrorCode
from src.utils.logger import Logger
from src.utils.uia import safe_text

_TIME_PATTERN = re.compile(r"^\d{1,2}:\d{2}:\d{2}$")
_LINK_KEYWORD = "断开"

_lock = threading.Lock()
# 仅 win32 句柄（跨线程安全）；UIA COM 对象不跨线程缓存
_cache = {"main_hwnd": None, "pane_hwnd": None}

# F5 重连戳防抖（用户实测 2026-10-10：查询链路 F4+Ctrl+C 不发网络请求、
# 无法触发重连；F5 是可靠的重连触发器，网络恢复后立竿见影）
_POKE_DEBOUNCE_S = 30.0
_poke_lock = threading.Lock()
_last_poke_mono = -1e9

# 任意来源 F5 的最近发送时刻（重连戳/同页刷新共用，用于「一次查询只发
# 一次 F5」去重——前置戳发出后 5s 内同页刷新跳过）
_f5_stamp_lock = threading.Lock()
_last_f5_mono = -1e9


def mark_f5_sent():
    """记录一次 F5 发送（所有发送来源都要调用，供跨模块去重）"""
    global _last_f5_mono
    with _f5_stamp_lock:
        _last_f5_mono = time.monotonic()


def f5_sent_recently(within_s: float = 5.0) -> bool:
    with _f5_stamp_lock:
        return (time.monotonic() - _last_f5_mono) < within_s


def _find_main_hwnd():
    for proc in psutil.process_iter(["name", "pid"]):
        if (proc.info["name"] or "").lower() == "xiadan.exe":
            out = []

            def cb(h, _):
                _, wpid = win32process.GetWindowThreadProcessId(h)
                if wpid == proc.pid and win32gui.IsWindowVisible(h) \
                        and TRADING_WINDOW_TITLE in (win32gui.GetWindowText(h) or ""):
                    out.append(h)
                return True

            win32gui.EnumWindows(cb, None)
            if out:
                return out[0]
    return None


def _pick_pane_hwnd(main_hwnd):
    """主窗口底部 60px 内最靠右的 AfxWnd140s = 连接状态 Pane"""
    wr = win32gui.GetWindowRect(main_hwnd)
    best = None
    best_left = -1

    def cb(h, _):
        nonlocal best, best_left
        try:
            if win32gui.GetClassName(h) != "AfxWnd140s":
                return True
            l, t, r, b = win32gui.GetWindowRect(h)
            if t < wr[3] - 60 or (b - t) >= 40 or (r - l) < 50:
                return True
            if l > best_left:
                best_left = l
                best = h
        except Exception:
            pass
        return True

    win32gui.EnumChildWindows(main_hwnd, cb, None)
    return best


def _read_pane_texts(pane_hwnd):
    wrapper = Desktop(backend="uia").window(handle=pane_hwnd)
    return [safe_text(k) or "" for k in wrapper.children()]


def read_broker_link() -> dict:
    """读状态栏连接字样

    Returns:
        dict: connected — True=状态格无「断开」且 Pane 含时间格 + 任意非空
              主站格文本（未观测到断开，含 ~30s 盲区语义；主站命名不设
              关键字白名单，mncgXXX/专有云NNN/… 任意非空文本均认可）；
              False=「断开」在场（唯一可靠负信号，优先判定）；
              None=无法判定（窗口不在/定位失败/结构异常——无时间格或
              无非空主站格文本）
              status_text — Pane 全部子格文本拼接（诊断用）
              latency_ms — 本次读取耗时
    """
    t0 = time.perf_counter()
    with _lock:
        def _result(connected, status_text="", reason=None):
            r = {"connected": connected,
                 "status_text": status_text[:120],
                 "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
            if reason:
                r["reason"] = reason
            return r

        try:
            main_hwnd = _cache["main_hwnd"]
            pane_hwnd = _cache["pane_hwnd"]
            if main_hwnd is None or not win32gui.IsWindow(main_hwnd):
                main_hwnd = _find_main_hwnd()
                if main_hwnd is None:
                    _cache.update(main_hwnd=None, pane_hwnd=None)
                    return _result(None, reason="main_window_not_found")
                pane_hwnd = None
            if pane_hwnd is None or not win32gui.IsWindow(pane_hwnd):
                pane_hwnd = _pick_pane_hwnd(main_hwnd)
                if pane_hwnd is None:
                    _cache.update(main_hwnd=main_hwnd, pane_hwnd=None)
                    return _result(None, reason="status_pane_not_found")

            texts = _read_pane_texts(pane_hwnd)
            joined = " ".join(t for t in texts if t)
            has_time = any(_TIME_PATTERN.match(t) for t in texts)
            others = [t for t in texts if t and not _TIME_PATTERN.match(t)]
            if not has_time or not others:
                # 结构对不上（窗口重排/选中错 Pane）——弃缓存下次重定位
                _cache.update(main_hwnd=main_hwnd, pane_hwnd=None)
                return _result(None, joined, reason="pane_pattern_mismatch")
            _cache.update(main_hwnd=main_hwnd, pane_hwnd=pane_hwnd)
            # 放宽判定（用户 2026-10-10）：主站命名不设关键字白名单
            # （mncg112/专有云010/… 见过多种），「断开」是唯一可靠负信号——
            # 状态格无「断开」+ 时间格在场 + 任意非空主站格文本 = 已连接
            connected = not any(_LINK_KEYWORD in t for t in others)
            return _result(connected, joined)
        except Exception as e:
            _cache["pane_hwnd"] = None
            return _result(None, reason=f"{type(e).__name__}: {e}"[:120])


# ============================================================
# 查询前置自愈：断开在场 → F5 戳重连（防抖）→ 轮询等重连
# ============================================================

def _send_f5_poke():
    """F5 直发（独立函数便于测试打桩）。功能键走 keybd_event 前台发送，
    send_key 默认先自动激活交易窗口——绝不把 F5 发进错误窗口"""
    from src.services.window_service import WindowService
    WindowService().send_key("F5")


def poke_if_disconnected(max_wait_s: float = 3.0) -> dict:
    """查询前置自愈：「断开」在场 → F5 戳重连 → 轮询等重连后返回

    必须在任务工作线程调用（查询 UI 操作上下文）；禁止在 /health
    读取路径调用——waitress 线程前台无保证，F5 会落进错误窗口。
    防抖 30s：调用方高频重试时不刷屏；防抖命中时不再等待（上次戳
    尚在生效中，后置复查兜底）。
    """
    global _last_poke_mono
    link = read_broker_link()
    if link["connected"] is not False:
        return link
    with _poke_lock:
        now = time.monotonic()
        do_poke = now - _last_poke_mono >= _POKE_DEBOUNCE_S
        if do_poke:
            _last_poke_mono = now
    if not do_poke:
        return link
    Logger.get_instance().info(
        f"查询前置检测到券商断连（{link.get('status_text')!r}），F5 戳重连")
    _send_f5_poke()
    mark_f5_sent()
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        time.sleep(0.25)
        if read_broker_link()["connected"] is not False:
            Logger.get_instance().info("F5 后券商已重连")
            break
    return read_broker_link()


# ============================================================
# 业务端点事后门控（fail-closed：断开在场 → 不交付本结果）
#
# 后置复查保留：前置自愈后重连可能仍未完成（客户端重试周期有波动），
# 查询读到的仍是缓存网格——此时作废报错。fail-open：connected=None
# （读取失败/结构异常）一律放行，与 session 健康门同哲学；只有客户端
# 明确报告「断开」才拦截。
# ============================================================

def ensure_connected_or_discard(action: str) -> dict:
    """查询类 UI 操作完成后校验：「断开」在场 → 结果作废抛 BROKER_DISCONNECTED

    断开态下查询静默返回客户端缓存数据（HTTP 200、日志无痕，2026-10-10
    实测 3/3），本门控把新鲜度判断从调用方收回归网关。
    """
    link = read_broker_link()
    if link["connected"] is False:
        Logger.get_instance().warning(
            f"券商链路断开，{action}结果已作废: {link.get('status_text')!r}")
        raise ApiError(
            ErrorCode.BROKER_DISCONNECTED,
            f"{action}已完成，但客户端报告券商连接断开——返回的将是缓存旧值，已作废",
            suggestion=("直接重试即可，新鲜度由后置门控保证，不会交付缓存旧值；"
                        "前置 F5 重连戳全局防抖 30s——立即重试不会再发 F5，"
                        "稍候重试才触发新一轮重连尝试，网络恢复后的首个成功"
                        "请求即返回新鲜数据"
                        "（注意断网初期有 ~25-45s 盲期，客户端自身未察觉）"),
            details={"broker_link": link})
    return link


def check_after_order(action: str, suggestion: str = None,
                      extra_details: dict = None) -> None:
    """下单/撤单提交序列完成后校验：「断开」在场 → ORDER_STATE_UNKNOWN

    结果状态未知（可能已送达券商也可能没有），与提交后非业务异常同级：
    幂等记录保留（should_keep_record_on_error），恢复后先查单核实。
    """
    link = read_broker_link()
    if link["connected"] is False:
        Logger.get_instance().warning(
            f"{action}后客户端报告券商断连，转状态未知: {link.get('status_text')!r}")
        raise ApiError(
            ErrorCode.ORDER_STATE_UNKNOWN,
            f"{action}序列已完成，但客户端报告券商连接断开——"
            f"本次操作是否送达券商状态未知",
            suggestion=suggestion or (
                "请先查单核实（下单同 key 重试会被幂等拦截）："
                "1) GET /orders/pending 按代码+价格+数量匹配当日委托；"
                "2) GET /trades/today 查成交；"
                "3) 确认未生效 → 网络恢复后重新操作"),
            details={"broker_link": link, **(extra_details or {})})
