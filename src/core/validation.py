"""下单数据校验（纯函数，无外部依赖）"""
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


def check_trading_hours(now: datetime = None) -> tuple:
    """交易时段预检（工作日 + 时段粗判，快速失败用）

    不含节假日历：法定节假日的工作日时段内会放行，由券商报错兜底
    （OUTSIDE_TRADING_HOURS / SERVER_CLEARING）。仅在配置
    order.reject_outside_trading_hours=true 时被调用。

    Args:
        now: 注入时刻（测试用），缺省取当前时间

    Returns:
        (是否可下单, 拒绝原因)——允许时原因为空串
    """
    n = now or datetime.now()
    if n.weekday() >= 5:  # 5=周六 6=周日
        return False, "周末非交易时段"
    t = n.time()
    for start, end in _TRADING_SESSIONS:
        if start <= t <= end:
            return True, ""
    return False, f"非交易时段（当前 {t.strftime('%H:%M')}，A股委托受理 9:15-11:30 / 13:00-15:00）"
