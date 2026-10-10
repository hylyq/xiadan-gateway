"""券商连接态只读探测：状态栏「断开」字样定向读取（/health 用）

实测基线（2026-10-10 本地物理断网双周期，trace 在 logs/netcycle*）：
- 可靠信号 = 状态栏连接 Pane 内状态格文本「断开」（连接态该格空文本，
  Pane 含 时间格 + 状态格 + mncgXXX 格；全树扫描命中唯一）
- mncg 主站编号每次重连会变（实测 112/113/114/18），不可绑定
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
_NAME_KEYWORDS = ("mncg", "断开")

_lock = threading.Lock()
# 仅 win32 句柄（跨线程安全）；UIA COM 对象不跨线程缓存
_cache = {"main_hwnd": None, "pane_hwnd": None}


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
        dict: connected — True=状态格空且有主站名（未观测到断开，含 ~30s
              盲区语义）；False=「断开」在场；None=无法判定（窗口不在/
              定位失败/结构异常）
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
            has_link_kw = any(k in t for t in texts
                              for k in _NAME_KEYWORDS)
            has_time = any(_TIME_PATTERN.match(t) for t in texts)
            if not has_link_kw and not has_time:
                # 结构对不上（窗口重排/选中错 Pane）——弃缓存下次重定位
                _cache.update(main_hwnd=main_hwnd, pane_hwnd=None)
                return _result(None, joined, reason="pane_pattern_mismatch")
            _cache.update(main_hwnd=main_hwnd, pane_hwnd=pane_hwnd)
            connected = False if _LINK_KEYWORD in joined \
                else (True if has_link_kw else None)
            return _result(connected, joined)
        except Exception as e:
            _cache["pane_hwnd"] = None
            return _result(None, reason=f"{type(e).__name__}: {e}"[:120])


# ============================================================
# 业务端点事后门控（fail-closed：断开在场 → 不交付本结果）
#
# 只做事后校验、不做操作前预检——操作本身就是客户端重连的触发器
# （实测恢复网络后 0.9s 自愈），预检拒绝会造成
# 「拒绝操作 → 客户端永不被戳 → 永不重连」死锁（2026-10-10 断网实测：
# 空闲客户端「断开」永不自清）。
#
# fail-open：connected=None（读取失败/结构异常）一律放行，与
# session 健康门同哲学；只有客户端明确报告「断开」才拦截。
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
            suggestion=("直接重试即可：每次调用都会触发客户端重连尝试，"
                        "网络恢复后的首个成功请求即返回新鲜数据"
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
