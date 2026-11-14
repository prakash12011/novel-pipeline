"""Secrets live in <home>/secrets.env (KEY=VALUE lines), never in the repo."""

from __future__ import annotations

from . import paths


def load() -> dict[str, str]:
    f = paths.secrets_file()
    out: dict[str, str] = {}
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("\"'")
    return out


def gemini_keys() -> list[str]:
    s = load()
    return [s[k] for k in sorted(s) if k.startswith("GEMINI_API_KEY") and s[k]]
