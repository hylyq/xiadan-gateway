"""深交所月度交易日历兜底源（chinesecalendar 不可用时启用）

数据源：深交所官方接口 monthList?month=YYYY-MM（无参数返回当月），逐日返回
jybz（1=交易日，0=非交易日）。接口仅能返回当年已公布的月份（次年日历约每年
12 月发布后可查），因此本模块只服务「当前年份」的兜底查询。

缓存策略：按年缓存到 data/trading_calendar/szse_calendar_{year}.json。
首次兜底逐月拉取当年 12 个月写入缓存；部分成功也落盘，下次只补缺失月份；
12 个月齐（完整缓存）后直接读文件，不再访问 API。整体拉取失败进入进程内
冷却（默认 30 分钟），冷却期内直接降级，避免连续下单时反复打接口。

线程安全：模块级锁串行化「读缓存 → 拉取 → 写缓存」流程。
"""
import json
import os
import threading
import time
import urllib.request
from datetime import date
from pathlib import Path

_SZSE_MONTH_URL = "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList"
_FETCH_TIMEOUT_SECONDS = 3.0  # 单请求超时——兜底层不能拖慢下单主流程
_RETRY_COOLDOWN_SECONDS = 1800.0  # 整体拉取失败后的进程内冷却（30 分钟）

# 缓存目录（项目根 data/trading_calendar/，gitignore；测试可 monkeypatch）
_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "trading_calendar"

_lock = threading.Lock()
_last_fail_monotonic = 0.0  # 最近一次整体拉取失败时刻（monotonic 秒）


def _cache_path(year: int) -> Path:
    return _CACHE_DIR / f"szse_calendar_{year}.json"


def _load_cache(year: int) -> dict:
    """读缓存文件；缺失或损坏返回空结构（触发重新拉取）"""
    try:
        with open(_cache_path(year), encoding="utf-8") as f:
            data = json.load(f)
        months = data.get("months")
        days = data.get("days")
        if isinstance(months, list) and isinstance(days, dict):
            return {
                "months": [str(m) for m in months],
                "days": {str(k): int(v) for k, v in days.items()},
            }
    except (OSError, ValueError):
        pass
    return {"months": [], "days": {}}


def _save_cache(year: int, cache: dict) -> None:
    """原子写缓存（tmp + replace）；写失败不致命，下次兜底重新拉取"""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(year)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(
                {"year": year, "months": cache["months"], "days": cache["days"]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass


def _fetch_month(year: int, month: int) -> dict:
    """拉取单月日历；网络/结构异常向上抛，由调用方止损"""
    url = f"{_SZSE_MONTH_URL}?month={year}-{month:02d}"
    with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"深交所日历 {year}-{month:02d} 无数据")
    prefix = f"{year}-{month:02d}-"
    days = {}
    for row in rows:
        row = row or {}
        day = str(row.get("jyrq", ""))
        flag = str(row.get("jybz"))
        if day.startswith(prefix) and flag in ("0", "1"):
            days[day] = int(flag)
    if not days:
        raise ValueError(f"深交所日历 {year}-{month:02d} 字段异常")
    return days


def is_trading_day(d: date):
    """查询某日是否交易日；返回 None 表示数据不可用（调用方继续降级）

    优先读本地缓存（含部分缓存，已取到的月份直接判定）；缺失月份现场
    逐月补拉，整体失败（一个月份都没取到）进入冷却期。
    """
    global _last_fail_monotonic
    with _lock:
        cache = _load_cache(d.year)
        flag = cache["days"].get(d.isoformat())
        if flag is not None:
            return bool(flag)
        if len(cache["months"]) >= 12:
            return None  # 完整缓存仍缺该日期（文件异常）→ 不重拉，直接降级
        if time.monotonic() - _last_fail_monotonic < _RETRY_COOLDOWN_SECONDS:
            return None  # 冷却期内：API 近期刚失败过，直接降级
        got_any = False
        for month in range(1, 13):
            key = f"{month:02d}"
            if key in cache["months"]:
                continue
            try:
                cache["days"].update(_fetch_month(d.year, month))
            except Exception:
                break  # API 不可用：立即止损（避免 12 次超时拖慢下单），进入冷却
            cache["months"].append(key)
            got_any = True
        if got_any:
            _save_cache(d.year, cache)
        if not got_any:
            _last_fail_monotonic = time.monotonic()
        flag = cache["days"].get(d.isoformat())
        return bool(flag) if flag is not None else None
