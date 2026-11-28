"""Read operator configuration without loading credentials or environment files."""
from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    except yaml.YAMLError as exc:
        raise ValueError(f'invalid YAML in {path}') from exc
    if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
        raise ValueError('configuration must be a mapping with string keys')
    return data
