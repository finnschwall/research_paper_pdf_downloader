from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from paper_downloader.config.models import PipelineConfig
from paper_downloader.core.exceptions import ConfigurationError


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(override=False)
    except ImportError:
        pass


def _apply_env_overrides(payload: dict[str, Any]) -> dict[str, Any]:
   
    apis = dict(payload.get("apis") or {})

    env_map = {
        "SEMANTIC_SCHOLAR_API_KEY": "semantic_scholar_api_key",
        "OPENALEX_API_KEY":         "openalex_api_key",
        "UNPAYWALL_EMAIL":          "unpaywall_email",
        "CORE_API_KEY":             "core_api_key",
        "CROSSREF_EMAIL":           "crossref_email",
        "WILEY_TDM_TOKEN":          "wiley_tdm_token",
        "ELSEVIER_API_KEY":         "elsevier_api_key",
        "ELSEVIER_INST_TOKEN":      "elsevier_inst_token",
    }

    for env_var, config_key in env_map.items():
        value = os.environ.get(env_var)
        if value:
            apis[config_key] = value

    payload = dict(payload)
    payload["apis"] = apis
    return payload


def load_config(path: str | Path) -> PipelineConfig:
    _load_dotenv()

    config_path = Path(path)
    if not config_path.exists():
        raise ConfigurationError(f"Config file not found: {config_path}")

    suffix = config_path.suffix.lower()
    raw_text = config_path.read_text(encoding="utf-8")

    if suffix == ".json":
        payload = _load_json(raw_text, config_path)
    elif suffix in {".yaml", ".yml"}:
        payload = _load_yaml(raw_text, config_path)
    else:
        raise ConfigurationError(f"Unsupported config file type: {config_path.suffix}")

    if not isinstance(payload, dict):
        raise ConfigurationError("Config root must be a JSON/YAML object")

    payload = _apply_env_overrides(payload)

    try:
        return PipelineConfig.from_dict(payload)
    except Exception as exc:
        raise ConfigurationError(f"Invalid config structure in: {config_path}") from exc


def _load_json(raw_text: str, path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Invalid JSON config: {path}") from exc

    if not isinstance(payload, dict):
        raise ConfigurationError(f"JSON config must be an object: {path}")
    return payload


def _load_yaml(raw_text: str, path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigurationError(
            "PyYAML is required to load YAML config files"
        ) from exc

    try:
        payload = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML config: {path}") from exc

    if not isinstance(payload, dict):
        raise ConfigurationError(f"YAML config must be an object: {path}")
    return payload
