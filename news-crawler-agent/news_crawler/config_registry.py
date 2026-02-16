"""Shared configuration loader for topics, persons and other entity feeds."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import json
import logging
import os
from pathlib import Path


def _normalize_key(raw: str) -> str:
    return raw.strip().lower().replace("\\", "/")


def _normalize_keywords(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [v.strip() for v in values.split(",") if v.strip()]
    return [v.strip().lower() for v in values if isinstance(v, str) and v.strip()]


def _ensure_list(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        return [values.strip()] if values.strip() else []
    return [str(v).strip() for v in values if str(v).strip()]


class ConfigRegistry:
    """Generic JSON-backed configuration registry used by the crawler.

    The registry loads configuration from a local JSON file (and optionally a GCS
    blob) and maintains alias indexes so callers can reference configs by various
    identifiers.
    """

    def __init__(
        self,
        *,
        logger_name: str,
        local_path: str,
        gcs_blob: Optional[str],
        default_group: str,
        load_local: bool = True,
        require_remote: bool = False,
    ) -> None:
        self._logger = logging.getLogger(logger_name)
        self._local_path = local_path
        self._gcs_blob = gcs_blob
        self._default_group = default_group
        self._load_local_enabled = load_local
        self._require_remote = require_remote
        self._configs: Dict[str, Dict[str, Any]] = {}
        self._alias_index: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Loading helpers

    def _load_local(self, path: str) -> Dict[str, Any]:
        p = Path(path)
        if not p.exists():
            self._logger.info("Local config not found: %s", path)
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            self._logger.error("Failed to read local config %s: %s", path, exc)
            return {}

    def _load_gcs(self, blob_name: str) -> Dict[str, Any]:
        try:
            from google.cloud import storage  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            self._logger.error("GCS client import failed: %s", exc)
            return {}

        bucket_name = os.getenv("GCS_BUCKET_NAME")
        if not bucket_name:
            self._logger.error("GCS_BUCKET_NAME not configured for config registry")
            return {}

        try:
            client = storage.Client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            if not blob.exists():
                self._logger.info("GCS blob not found: gs://%s/%s", bucket_name, blob_name)
                return {}
            data = blob.download_as_text(encoding="utf-8")
            return json.loads(data)
        except Exception as exc:
            self._logger.error("Failed to load config from GCS %s: %s", blob_name, exc)
            return {}

    # ------------------------------------------------------------------
    # Normalisation helpers

    def _register_aliases(self, key_norm: str, config: Dict[str, Any]) -> None:
        aliases = set()
        aliases.add(key_norm)

        identifier = config.get("topic_identifier") or config.get("person_identifier") or key_norm
        if identifier:
            identifier_norm = _normalize_key(identifier)
            aliases.add(identifier_norm)
            aliases.add(identifier_norm.replace(".", "/"))

        storage_path = config.get("topic_storage_path") or config.get("person_storage_path")
        if storage_path:
            storage_norm = _normalize_key(storage_path)
            aliases.add(storage_norm)
            if "/" in storage_norm:
                aliases.add(storage_norm.split("/")[-1])

        for alias in aliases:
            self._alias_index[alias] = key_norm
        config["aliases"] = sorted(aliases)

    def _normalise_entry(self, raw_key: str, raw_config: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
        if not isinstance(raw_config, dict):
            self._logger.warning("Config for key '%s' is not a dictionary, skipping", raw_key)
            return None

        key_norm = _normalize_key(raw_key)
        config = dict(raw_config)

        topic_identifier = config.get("topic_identifier") or config.get("person_identifier") or key_norm
        topic_identifier = topic_identifier.strip() if isinstance(topic_identifier, str) else key_norm
        if not topic_identifier:
            topic_identifier = key_norm

        storage_path = config.get("topic_storage_path") or config.get("person_storage_path") or topic_identifier
        storage_path = str(storage_path).strip().replace(".", "/")

        rss_sources = _ensure_list(config.get("rss_sources") or config.get("feed_urls"))

        topic_group = config.get("topic_group") or (
            storage_path.split("/")[0] if "/" in storage_path else self._default_group
        )

        max_articles = config.get("max_articles")
        try:
            max_articles = int(max_articles) if max_articles is not None else None
        except Exception:
            self._logger.warning(
                "Invalid max_articles for key '%s': %r. Using None.", raw_key, max_articles
            )
            max_articles = None

        max_age_hours = config.get("max_age_hours")
        try:
            max_age_hours = int(max_age_hours) if max_age_hours is not None else None
        except Exception:
            self._logger.warning(
                "Invalid max_age_hours for key '%s': %r. Using None.", raw_key, max_age_hours
            )
            max_age_hours = None

        try:
            min_content_length = int(config.get("min_content_length") or 0)
        except Exception:
            self._logger.warning(
                "Invalid min_content_length for key '%s': %r. Using 0.",
                raw_key,
                config.get("min_content_length"),
            )
            min_content_length = 0

        try:
            min_summary_length = int(config.get("min_summary_length") or 0)
        except Exception:
            self._logger.warning(
                "Invalid min_summary_length for key '%s': %r. Using 0.",
                raw_key,
                config.get("min_summary_length"),
            )
            min_summary_length = 0

        normalized_config: Dict[str, Any] = {
            **config,
            "key": key_norm,
            "topic_identifier": topic_identifier,
            "topic_storage_path": storage_path,
            "topic_group": topic_group,
            "rss_sources": rss_sources,
            "required_keywords": _normalize_keywords(config.get("required_keywords")),
            "excluded_keywords": _normalize_keywords(config.get("excluded_keywords")),
            "highlight_keywords": _normalize_keywords(config.get("highlight_keywords")),
            "source_allowlist": _ensure_list(config.get("source_allowlist")),
            "source_blocklist": _ensure_list(config.get("source_blocklist")),
            "min_content_length": min_content_length,
            "min_summary_length": min_summary_length,
            "require_full_text": bool(config.get("require_full_text", False)),
            "enforce_content_filters": bool(config.get("enforce_content_filters", False)),
            "max_articles": max_articles,
            "max_age_hours": max_age_hours,
        }

        return key_norm, normalized_config

    # ------------------------------------------------------------------
    # Public API

    def refresh(self) -> None:
        configs: Dict[str, Any] = {}
        if self._load_local_enabled:
            try:
                configs = self._load_local(self._local_path) or {}
            except Exception:
                configs = {}

        if self._gcs_blob:
            gcs_conf = self._load_gcs(self._gcs_blob) or {}
            configs.update(gcs_conf)
        elif self._require_remote:
            raise RuntimeError(
                f"Config registry '{self._logger.name}' requires GCS blob but none configured"
            )

        normalized: Dict[str, Dict[str, Any]] = {}

        for raw_key, raw_config in configs.items():
            normalized_entry = self._normalise_entry(raw_key, raw_config)
            if not normalized_entry:
                continue
            key_norm, topic_config = normalized_entry
            normalized[key_norm] = topic_config

        self._configs = normalized
        self._alias_index = {}

        for key_norm, topic_config in self._configs.items():
            self._register_aliases(key_norm, topic_config)

        if self._require_remote and not self._configs:
            raise RuntimeError(
                f"Config registry '{self._logger.name}' failed to load any configs from GCS"
            )

        self._logger.info("Loaded %s configs", len(self._configs))

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        key_norm = _normalize_key(key)

        direct = self._configs.get(key_norm)
        if direct:
            return direct

        alias_key = self._alias_index.get(key_norm)
        if alias_key:
            return self._configs.get(alias_key)

        if "/" in key_norm:
            tail = key_norm.split("/")[-1]
            alias_key = self._alias_index.get(tail)
            if alias_key:
                return self._configs.get(alias_key)

        if "." in key_norm:
            tail = key_norm.split(".")[-1]
            alias_key = self._alias_index.get(tail)
            if alias_key:
                return self._configs.get(alias_key)

        self._logger.warning("No configuration found for key: '%s'", key)
        return None

    def all_configs(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._configs)
