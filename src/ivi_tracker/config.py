"""Loads config.yaml from the repo root."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    with path.open() as fh:
        return yaml.safe_load(fh)
