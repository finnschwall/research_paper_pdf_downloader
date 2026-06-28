from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .models import (
    CitationGraphConfig,
    MetadataConfig,
    OutputConfig,
    RecoveryConfig,
    SemanticScholarConfig,
)

_REQUIRED_RECOVERY_FIELDS = [
    "similarity_threshold",
    "min_abstract_len",
    "request_delay",
    "scrape_timeout",
    "scrape_max_retries",
    "api_sleep_between_papers",
]


def load_config(config_path: str | Path) -> MetadataConfig:
    config_path = Path(config_path).resolve()

    env_path = config_path.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()

    with open(config_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    _validate_raw(raw, config_path)

    ss_raw = raw["semantic_scholar"]
    recovery_raw = raw["recovery"]
    output_raw = raw["output"]
    cg_raw = raw.get("citation_graph", {})

    scrape_timeout = recovery_raw["scrape_timeout"]
    if isinstance(scrape_timeout, list):
        scrape_timeout = tuple(scrape_timeout)

    ss_key_env = ss_raw.get("api_key_env", "SEMANTIC_SCHOLAR_API_KEY")
    core_key_env = recovery_raw.get("core_api_key_env", "CORE_API_KEY")

    _cg_max = cg_raw.get("max_results", None)
    if _cg_max is not None:
        _cg_max = int(_cg_max)

    return MetadataConfig(
        semantic_scholar=SemanticScholarConfig(
            fields=ss_raw["fields"],
        ),
        recovery=RecoveryConfig(
            similarity_threshold=float(recovery_raw["similarity_threshold"]),
            min_abstract_len=int(recovery_raw["min_abstract_len"]),
            request_delay=float(recovery_raw["request_delay"]),
            scrape_timeout=scrape_timeout,
            scrape_max_retries=int(recovery_raw["scrape_max_retries"]),
            api_sleep_between_papers=float(recovery_raw["api_sleep_between_papers"]),
        ),
        output=OutputConfig(
            base_dir=output_raw["base_dir"],
        ),
        citation_graph=CitationGraphConfig(
            fields=cg_raw.get(
                "fields",
                "paperId,title,year,authors,abstract,isInfluential,citationCount,externalIds",
            ),
            max_results=_cg_max,
            request_delay=float(cg_raw.get("request_delay", 1.0)),
        ),
        search_queries_path=raw.get(
            "search_queries_path",
            str(config_path.parent / "search_queries.json"),
        ),
        ss_api_key=os.environ.get(ss_key_env, ""),
        core_api_key=os.environ.get(core_key_env, ""),
    )


def _validate_raw(raw: dict, config_path: Path) -> None:
    if "semantic_scholar" not in raw:
        raise ValueError(
            f"config.json missing required section 'semantic_scholar' (path: {config_path})"
        )
    if "fields" not in raw["semantic_scholar"]:
        raise ValueError(
            f"config.json missing required key 'semantic_scholar.fields' (path: {config_path})"
        )

    if "recovery" not in raw:
        raise ValueError(
            f"config.json missing required section 'recovery' (path: {config_path})"
        )
    for key in _REQUIRED_RECOVERY_FIELDS:
        if key not in raw["recovery"]:
            raise ValueError(
                f"config.json missing required key 'recovery.{key}' (path: {config_path})"
            )

    if "output" not in raw or "base_dir" not in raw["output"]:
        raise ValueError(
            f"config.json missing required key 'output.base_dir' (path: {config_path})"
        )
