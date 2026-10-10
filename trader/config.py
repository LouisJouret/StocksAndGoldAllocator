from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> dict:
    with open(Path(path) if path else ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def resolve(path: str | Path) -> Path:
    """A config path, relative to the project root unless absolute."""
    p = Path(path)
    return p if p.is_absolute() else ROOT / p
