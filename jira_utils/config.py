from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Union


@dataclass
class JiraConfig:
    base_url: str
    email: str
    api_token: str
    project_keys: List[str] = field(default_factory=list)
    verify_ssl: bool = True
    page_size: int = 50

    @property
    def normalized_base_url(self) -> str:
        return self.base_url.rstrip("/")


def load_config(path: Union[str, Path]) -> JiraConfig:
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    data = json.loads(config_path.read_text())
    return JiraConfig(
        base_url=_require_string(data, "base_url"),
        email=_require_string(data, "email"),
        api_token=_require_string(data, "api_token"),
        project_keys=_get_string_list(data, "project_keys"),
        verify_ssl=bool(data.get("verify_ssl", True)),
        page_size=int(data.get("page_size", 50)),
    )


def _require_string(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Config field '{field}' must be a non-empty string.")
    return value.strip()


def _get_string_list(data: dict[str, Any], field: str) -> List[str]:
    value = data.get(field)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"Config field '{field}' must be a list if provided.")
    cleaned = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Config field '{field}' must contain only non-empty strings.")
        cleaned.append(item.strip())
    return cleaned
