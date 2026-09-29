"""下单数据校验（无第三方依赖；节假日历三级降级链见 _is_market_holiday 说明）"""
from datetime import datetime, time


def sanitize_price(price: str) -> str:
    """对 A 股价格做格式校验，限制 2 位小数

    Args:
        price: 原始价格字符串

    Returns:
        格式化的价格字符串（最多 2 位小数）

    Raises:
        Exception: 价格格式无效
    """
    try:
        price_float = float(price)
        return f"{price_float:.2f}"
    except ValueError:
        raise Exception(f"价格格式无效: {price}")


# A 股委托受理时段：9:15 起集合竞价受理，9:30-11:30 连续竞价，
# 13:00-15:00 下午盘（15:00 收盘端点含入——收盘瞬间提交仍会被受理）
_TRADING_SESSIONS = (
    (time(9, 15), time(11, 30)),
    (time(13, 0), time(15, 0)),
)


def _is_market_holiday(n: datetime) -> tuple:
    """法定节假日判断（三级降级，任何一级失败优雅降级，不阻塞下单）

    一级 chinesecalendar：依赖缺失（ImportError）或数据未覆盖该年份
    （NotImplementedError）时进入下一级。
    二级 深交所官方月度日历（src/core/trading_calendar.py）：jybz 口径，
    当年按年缓存到本地文件，API 也不可用时进入下一级。
    三级 工作日粗判：节假日放行，由券商报错兜底（既有行为）。

    口径说明：只对「周一至周五但为法定节假日」生效。调休补班的
    周末 A 股同样休市，周末判断已在 check_trading_hours 前置处理，
    不受 is_workday 对调休周末返回 True 的影响。

    Returns:
        (是否节假日休市, 拒绝原因)——非节假日原因为空串
    """
    d = n.date()

    # 一级：chinesecalendar
    try:
        from chinese_calendar import is_workday
    except ImportError:
        is_workday = None
    if is_workday is not None:
        try:
            workday = is_workday(d)
        except NotImplementedError:
            workday = None  # 数据未覆盖该年份 → 进入二级
        except Exception:
            workday = None  # 节假日判断失败不阻塞下单主流程
        if workday is not None:
            return (False, "") if workday else (True, "法定节假日休市")

    # 二级：深交所官方月度日历（本地缓存优先，API 兜底）
    try:
        from src.core.trading_calendar import is_trading_day
        trading = is_trading_day(d)
    except Exception:
        trading = None  # 兜底层任何异常都不阻塞下单主流程
    if trading is not None:
        return (False, "") if trading else (True, "非交易日休市（深交所日历）")

    # 三级：工作日粗判（节假日放行，券商报错兜底）
    return False, ""


def check_trading_hours(now: datetime = None) -> tuple:
    """交易时段预检（周末/节假日 + 时段粗判，快速失败用）

    节假日判断走三级降级链（见 _is_market_holiday）：chinesecalendar →
    深交所官方月度日历（本地按年缓存）→ 仅周末/工作日粗判。全链路不可用时
    节假日放行，由券商报错兜底（OUTSIDE_TRADING_HOURS / SERVER_CLEARING）。
    仅在配置 order.reject_outside_trading_hours=true 时被调用。

    Args:
        now: 注入时刻（测试用），缺省取当前时间

    Returns:
        (是否可下单, 拒绝原因)——允许时原因为空串
    """
    n = now or datetime.now()
    if n.weekday() >= 5:  # 5=周六 6=周日（调休补班的周末同样休市）
        return False, "周末非交易时段"
    is_holiday, holiday_reason = _is_market_holiday(n)
    if is_holiday:
        return False, holiday_reason
    t = n.time()
    for start, end in _TRADING_SESSIONS:
        if start <= t <= end:
            return True, ""
    return False, f"非交易时段（当前 {t.strftime('%H:%M')}，A股委托受理 9:15-11:30 / 13:00-15:00）"
