"""Where things live. Every function returns an absolute Path.

Per PC, outside the repo: the pipeline home (~/.novel-pipeline, or $NOVEL_HOME).
Book data lives under `data_dir` from <home>/settings.yaml.
"""

from __future__ import annotations

import os
from pathlib import Path

FIX_HINT = 'novel settings init --data-dir "<path>"'


class SetupError(SystemExit):
    """A clear, fixable setup problem (exits with the message when uncaught)."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def home() -> Path:
    env = os.environ.get("NOVEL_HOME")
    base = Path(env) if env else Path.home() / ".novel-pipeline"
    return base.expanduser().resolve()


def settings_file() -> Path:
    return home() / "settings.yaml"


def secrets_file() -> Path:
    return home() / "secrets.env"


def state_file() -> Path:
    return home() / "state.json"


def data_dir() -> Path:
    import yaml

    f = settings_file()
    if not f.exists():
        raise SetupError(f"{f} not found. Create it with: {FIX_HINT}")
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    d = data.get("data_dir")
    if not d:
        raise SetupError(f"data_dir is not set in {f}. Fix with: {FIX_HINT} --force")
    p = Path(str(d)).expanduser()
    if not p.exists():
        raise SetupError(f"data_dir {p} does not exist. Fix with: {FIX_HINT} --force")
    return p.resolve()


def book_dir(book: str) -> Path:
    return data_dir() / book


def episodes_dir(book: str) -> Path:
    return book_dir(book) / "episodes"


def ep_root(book: str, n: int) -> Path:
    return episodes_dir(book) / f"E{int(n):03d}"


def source_dir(book: str) -> Path:
    return book_dir(book) / "source"


def chapters_dir(book: str) -> Path:
    return book_dir(book) / "chapters"


def bible_dir(book: str) -> Path:
    return book_dir(book) / "bible"


def inbox_dir(book: str) -> Path:
    return book_dir(book) / "inbox"


def gemini_dir(book: str) -> Path:
    return book_dir(book) / "gemini"


def research_dir(book: str) -> Path:
    return book_dir(book) / "research"


def extras_dir(book: str) -> Path:
    return book_dir(book) / "extras"


def kaggle_work_dir(book: str) -> Path:
    return book_dir(book) / "_work" / "kaggle"


def assets_dir() -> Path:
    return repo_root() / "assets"


def fonts_dir() -> Path:
    return assets_dir() / "fonts"
