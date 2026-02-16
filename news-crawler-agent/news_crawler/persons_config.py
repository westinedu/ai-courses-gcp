"""Dynamic loader for person/celebrity configurations."""

from typing import Dict, Any, Optional
import logging

from settings import settings
from news_crawler.config_registry import ConfigRegistry


_registry = ConfigRegistry(
    logger_name="news_crawler_persons_config",
    local_path=settings.persons_config_local_path,
    gcs_blob=settings.persons_config_gcs_blob,
    default_group="celebrity",
    load_local=False,
    require_remote=True,
)


def refresh() -> None:
    _registry.refresh()


def get_person_config(person_key: str) -> Optional[Dict[str, Any]]:
    return _registry.get(person_key)


def get_all_person_configs() -> Dict[str, Dict[str, Any]]:
    return _registry.all_configs()


if settings.persons_config_refresh_on_start:
    try:
        refresh()
    except Exception:
        logging.getLogger("news_crawler_persons_config").exception(
            "Failed to refresh person configs on start"
        )
