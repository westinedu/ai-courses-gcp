"""Dynamic topic configuration loader built on the shared registry."""

from typing import Dict, Any, Optional
import logging

from settings import settings
from news_crawler.config_registry import ConfigRegistry


_registry = ConfigRegistry(
    logger_name="news_crawler_dynamic_config",
    local_path=settings.topic_config_local_path,
    gcs_blob=settings.topic_config_gcs_blob,
    default_group="macro",
    load_local=True,
    require_remote=False,
)


def refresh() -> None:
    """Reload topic configs from configured source(s)."""

    _registry.refresh()


def get_topic_config(topic_key: str) -> Optional[Dict[str, Any]]:
    return _registry.get(topic_key)


def get_all_topic_configs() -> Dict[str, Dict[str, Any]]:
    return _registry.all_configs()


# Auto-refresh on import if requested
if settings.topic_config_refresh_on_start:
    try:
        refresh()
    except Exception:
        logging.getLogger("news_crawler_dynamic_config").exception(
            "Failed to refresh dynamic topic configs on start"
        )
