"""novel quota: the free-tier calls each Gemini key has made today (gemini.free_tier)."""

from __future__ import annotations


def main(argv) -> int:
    from novel.core import keybudget, paths, secrets, settings
    if argv and argv[0] in ("-h", "--help"):
        print("usage: novel quota")
        return 0
    ft = settings.get("gemini.free_tier", default={}) or {}
    model = settings.get("gemini.api_model")
    keys = secrets.gemini_keys()
    if not keys:
        print(f"no GEMINI_API_KEY* in {paths.secrets_file()}")
        return 1
    b = keybudget.KeyBudget(paths.home() / keybudget.FILE, keys, model,
                            int(ft.get("per_minute") or 5), int(ft.get("per_day") or 20))
    print(f"{model}: free tier {'on' if ft.get('enabled') else 'OFF'}, {b.per_minute}/minute and {b.per_day}/day per key "
          f"(day resets at midnight Pacific)")
    left = 0
    for pos, today, minute in b.status():
        left += max(b.per_day - today, 0)
        print(f"  key {pos}: {today}/{b.per_day} today, {minute} in the last minute")
    print(f"calls left today: {left}" + ("" if left else "  (next calls go to OpenRouter)"))
    return 0
