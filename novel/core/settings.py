"""Layered settings.

Layers, lowest to highest:
  defaults   config/defaults.yaml (in the repo)
  home       <home>/settings.yaml (per PC)
  book       <data_dir>/<book>/book.yaml
  episode    <data_dir>/<book>/episodes/E###/episode.yaml
  chapter    the `chapters: {N: {...}}` block of episode.yaml (only CHAPTER_SCOPE keys)

Dicts merge key by key; lists and scalars replace. Missing files are skipped.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from . import paths

CHAPTER_SCOPE = (
    "video.logo",
    "video.subscribe",
    "video.subscribe.silent_after",
    "video.captions",
    "video.panels.captions",
    "images.max_seconds",
    "images.paced_seconds",
)

_MISSING = object()


class SettingsError(SystemExit):
    pass


def defaults_file() -> Path:
    return paths.repo_root() / "config" / "defaults.yaml"


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: expected a mapping at the top level")
    return data


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def flatten(d: dict, prefix: str = "") -> dict:
    """Leaf values by dotted key. An empty dict is a leaf; lists are leaves."""
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def _in_scope(key: str) -> bool:
    return any(key == s or key.startswith(s + ".") for s in CHAPTER_SCOPE)


def check_chapter_block(block: dict, where: str) -> None:
    for key in flatten(block):
        if not _in_scope(key):
            raise SettingsError(
                f"{where}: '{key}' cannot be overridden per chapter. "
                f"Allowed: {', '.join(CHAPTER_SCOPE)}")


def _chapters_block(ep_data: dict, ep_file: Path) -> dict[int, dict]:
    raw = ep_data.get("chapters") or {}
    out = {}
    for n, block in raw.items():
        block = block or {}
        if not isinstance(block, dict):
            raise SettingsError(f"{ep_file}: chapters.{n} must be a mapping")
        check_chapter_block(block, f"{ep_file} chapters.{n}")
        out[int(n)] = block
    return out


def layers(book=None, ep=None, ch=None) -> list[tuple[str, dict]]:
    """[(layer_name, data)] lowest first. Missing files give no data."""
    out = [("defaults", _read(defaults_file())),
           ("home", _read(paths.settings_file()))]
    if book is None:
        from . import context
        book = context.read_state().get("book")
    if book:
        try:
            bdir = paths.book_dir(book)
        except SystemExit:
            bdir = None
        if bdir is not None:
            out.append((f"book:{book}", _read(bdir / "book.yaml")))
            if ep is not None:
                ep_file = paths.ep_root(book, ep) / "episode.yaml"
                ep_data = _read(ep_file)
                chapters = _chapters_block(ep_data, ep_file)
                ep_data = {k: v for k, v in ep_data.items() if k != "chapters"}
                out.append((f"episode:E{int(ep):03d}", ep_data))
                if ch is not None:
                    out.append((f"chapter:{int(ch)}", chapters.get(int(ch), {})))
    return out


def effective(book=None, ep=None, ch=None) -> dict:
    merged: dict = {}
    for _, data in layers(book, ep, ch):
        merged = deep_merge(merged, data)
    return merged


def explain(book=None, ep=None, ch=None) -> dict[str, tuple]:
    """{dotted_key: (value, layer_name)} for every leaf of the effective settings."""
    ls = layers(book, ep, ch)
    eff: dict = {}
    for _, data in ls:
        eff = deep_merge(eff, data)
    flat_layers = [(name, flatten(data)) for name, data in ls]
    out = {}
    for key, value in flatten(eff).items():
        src = "defaults"
        for name, fl in flat_layers:
            if key in fl:
                src = name
        out[key] = (value, src)
    return out


def get(dotted_key: str, book=None, ep=None, ch=None, default=_MISSING):
    node = effective(book, ep, ch)
    for part in dotted_key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif default is _MISSING:
            raise KeyError(dotted_key)
        else:
            return default
    return node


def asset_path(value) -> Path:
    """Relative `assets/...` values resolve against the repo root."""
    s = str(value)
    if s.replace("\\", "/").startswith("assets/"):
        return paths.repo_root() / s
    return Path(s)
