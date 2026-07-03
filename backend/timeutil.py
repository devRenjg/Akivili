"""时间工具：数据库存 UTC（SQLite datetime('now')），返回给前端时统一转北京时间。

平台统一用北京时间展示。存储层保持 UTC（标准、可移植），只在 API 边界转换，
这样历史数据也能正确显示为北京时间。
"""
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
_FMT = "%Y-%m-%d %H:%M:%S"


def to_beijing(ts: str | None) -> str:
    """把数据库里的 UTC 时间字符串（'YYYY-MM-DD HH:MM:SS'）转成北京时间同格式字符串。

    解析失败时原样返回（不抛错），保证展示链路健壮。
    """
    if not ts:
        return ts or ""
    s = str(ts).strip()
    # 兼容带小数秒 / 带 T 分隔 / 带 Z 的情况
    core = s.replace("T", " ").split(".")[0].replace("Z", "").strip()
    try:
        dt = datetime.strptime(core, _FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return s
    return dt.astimezone(BEIJING).strftime(_FMT)
