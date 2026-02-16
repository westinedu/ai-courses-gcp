# news_crawler/utils.py
import logging
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

from settings import settings # 导入 settings

logger = logging.getLogger("news_crawler_agent")

def _parse_date(date_str: Optional[str]) -> date:
    """解析日期字符串为 ``datetime.date`` 对象。
    如果未提供 ``date_str``，则使用当前时间的日期，按照配置的
    时区转换。
    """
    tz = ZoneInfo(settings.timezone)
    if date_str and date_str.strip():
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            raise ValueError("Invalid date format. Use YYYY-MM-DD.")
        return dt.date()
    now = datetime.now(tz)
    return now.date()


def _get_date_dir(target_date: date) -> str:
    """返回日期目录名，格式为 ``YYYY-MM-DD``。"""
    return target_date.strftime("%Y-%m-%d")