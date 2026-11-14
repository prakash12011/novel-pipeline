"""novel meta [--book B] [--ep N] [--ai claude|gemini|none] [--refresh-research] [--teaser "..."]

Writes <episode>/publish/publish.json and publish.txt: title, description with chapter
timestamps, tags, hashtags and three thumbnail hook lines, checked against YouTube's limits.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from datetime import timezone

from novel.core import context, paths, settings
from novel.publish import meta, research, validate

AI_CHOICES = ("claude", "gemini", "none")


def _fail(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


def _research(book: str, refresh: bool, opener, pause, warnings: list[str]):
    """Newest research cache (fresh, or refreshed through the research command). None if there is none."""
    rdir = paths.research_dir(book)
    cached = research.newest_cache(rdir)
    fresh = (cached is not None and not refresh and research.age_days(cached) < meta.RESEARCH_MAX_AGE_DAYS)
    if not fresh:
        seeds = settings.get("publish.research_seeds", book=book, default=[]) or []
        if not seeds:
            warnings.append("no publish.research_seeds in book.yaml: research skipped"
                            + (f" (using older cache {cached.name})" if cached else ""))
        else:
            from novel.commands import research_cmd
            args = ["--book", book, "--max-age-days", str(meta.RESEARCH_MAX_AGE_DAYS)]
            if refresh:
                args.append("--refresh")
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    code = research_cmd.main(args, opener=opener, pause=pause)
            except Exception as e:                     # offline must never stop the pack
                code = 1
                warnings.append(f"research failed ({type(e).__name__})")
            if code != 0:
                warnings.append("research unavailable (offline or blocked): continuing without new research"
                                + (f", older cache {cached.name} used" if cached else ""))
        cached = research.newest_cache(rdir)
    if cached is None:
        warnings.append("no research cache: the AI gets no search phrases")
        return None
    data = research.load_cache(cached)
    phrases = [{"text": p.get("text", ""), "count": p.get("count", 0)} for p in (data.get("phrases") or [])]
    return {"file": cached.name, "fetched_at": data.get("fetched_at"),
            "age_days": round(research.age_days(cached), 1), "top_phrases": phrases[:30]}


def main(argv, *, opener=None, pause=research.PAUSE_SECONDS) -> int:
    ap = argparse.ArgumentParser(prog="novel meta")
    ap.add_argument("--book")
    ap.add_argument("--ep", type=int)
    ap.add_argument("--ai", choices=AI_CHOICES)
    ap.add_argument("--refresh-research", action="store_true")
    ap.add_argument("--teaser")
    a = ap.parse_args(argv)

    book, ep = context.resolve(a.book, a.ep)
    if ep is None:
        return _fail("No episode. Pass --ep N, or pick one with: novel use <book> --ep N")
    ep_dir = paths.ep_root(book, ep)
    info_file = ep_dir / "episode.json"
    if not info_file.is_file():
        return _fail(f"{info_file} not found: create the episode first (novel episode add)")
    try:
        info = json.loads(info_file.read_text(encoding="utf-8"))
        start, end = int(info["start"]), int(info["end"])
    except (ValueError, KeyError, TypeError):
        return _fail(f"{info_file} needs numeric keys start and end")

    cfg = settings.effective(book, ep)
    pub_cfg = cfg.get("publish") or {}
    title_tpl, desc_tpl = pub_cfg.get("title_template"), pub_cfg.get("description_template")
    tags_tpl, hashtags = pub_cfg.get("tags") or [], pub_cfg.get("hashtags") or []
    if not title_tpl or not desc_tpl:
        return _fail(f"publish.title_template and publish.description_template are not set for '{book}'. "
                     f"Add them to {paths.book_dir(book) / 'book.yaml'}")
    ai_mode = a.ai or pub_cfg.get("ai") or "claude"
    if ai_mode not in AI_CHOICES:
        return _fail(f"publish.ai must be one of {', '.join(AI_CHOICES)}, got '{ai_mode}'")
    if ai_mode == "none" and not (a.teaser and a.teaser.strip()):
        return _fail('--ai none needs --teaser "..." (two or three Hinglish sentences)')

    book_cfg = cfg.get("book") or {}
    book_title = book_cfg.get("title") or book
    warnings: list[str] = []

    # --- facts gathered by code
    vol, found = meta.volume_for(book_cfg.get("volumes"), start)
    if not found:
        warnings.append(f"chapter {start} is in no book.volumes range: volume 1 assumed")
    elif meta.volume_for(book_cfg.get("volumes"), end)[0] != vol:
        warnings.append(f"chapters {start}-{end} cross volumes: volume {vol} (of chapter {start}) used")
    vals = {"N": ep, "X": start, "Y": end, "V": vol}
    prev_url = meta.neighbour(ep_dir, ep - 1) if ep > 1 else None
    next_url = meta.neighbour(ep_dir, ep + 1)
    playlist = (pub_cfg.get("playlist_url") or "").strip() or None
    if not playlist:
        warnings.append("publish.playlist_url is not set: the playlist line is left out")

    ts, w = meta.load_chapter_starts(ep_dir, ep, start, end)
    warnings += w
    stamps, w = meta.build_stamps(ts["rows"], ts["duration"], ts["kind"])
    warnings += w
    stamp_block = meta.stamp_lines(stamps, ts["duration"])

    chapters = meta.chapter_texts(book, ep_dir, start, end)
    if not chapters:
        warnings.append(f"no English chapter files for {start}-{end}: the teaser cannot be checked against the novel")
    titles = [t for _, t, _ in chapters if t]
    sources = [body for _, _, body in chapters]
    condensed, sent = meta.condense(chapters)

    # --- research
    snapshot = _research(book, a.refresh_research, opener, pause, warnings)
    phrases = [p["text"] for p in (snapshot or {}).get("top_phrases", [])][:meta.RESEARCH_PHRASES_FOR_AI]

    # --- AI text
    pub_dir = ep_dir / "publish"
    pub_dir.mkdir(parents=True, exist_ok=True)
    ai = None
    ai_state = {"ai": ai_mode, "ai_note": None}
    character = None
    if ai_mode == "none":
        teaser, question, hooks = a.teaser.strip(), meta.GENERIC_QUESTION, []
        ai_state["ai_note"] = "no AI: teaser from --teaser, generic comment question, hooks left empty"
        warnings.append("--ai none: no thumbnail hooks written. Pass --hook to novel thumb, or run novel meta with an AI.")
    else:
        ai = meta.AI(ai_mode, cfg.get("gemini", {}).get("api_model") or "gemini-3.5-flash-lite",
                     pub_dir / "gemini-calls.log")
        print(f"AI: {ai_mode}  (sending {len(chapters)} chapters condensed to {sent:,} characters"
              f" of {sum(len(s) for s in sources):,})", flush=True)
        prompt_args = dict(book_title=book_title, ep=ep, start=start, end=end, condensed=condensed,
                           sent_chars=sent, research=phrases, idea=a.teaser)
        try:
            got = ai.ask(meta.build_prompt(**prompt_args))
        except meta.AIFailure as e:
            return _fail(f"no AI answer ({e}). Re-run with --ai none --teaser \"...\" to write the pack by hand.")
        teaser, question = got["teaser"].strip(), got["comment_question"].strip()
        hooks = [h.strip() for h in got["thumbnail_hooks"]]
        character = (got.get("thumbnail_character") or "").strip() or None

    prev_clean_url = prev_url
    series = meta.series_phrase(title_tpl)

    def assemble():
        title = meta.fill_title(title_tpl, vals)
        desc = meta.fill_description(desc_tpl, vals, teaser=teaser, question=question, stamp_block=stamp_block,
                                     playlist_url=playlist, prev_url=prev_clean_url, next_url=next_url,
                                     ep_number=ep)
        return {"title": title, "description": desc, "tags": meta.fill_tags(tags_tpl, vals),
                "hashtags": list(hashtags), "teaser": teaser, "comment_question": question,
                "thumbnail_hooks": hooks}

    def check(pack):
        return validate.validate_pack(pack, series_phrase=series, expected_tags=len(tags_tpl),
                                      english_sources=sources, chapter_titles=titles,
                                      check_hook_text=(ai_mode != "none"))

    pack = assemble()
    errors = check(pack)
    bad = validate.ai_errors(errors)
    if bad and ai is not None:
        print(f"The AI text failed {len(bad)} check(s); asking once more:", flush=True)
        for e in bad:
            print(f"  - {e}")
        try:
            got = ai.ask(meta.build_prompt(**prompt_args, errors=bad))
            teaser, question = got["teaser"].strip(), got["comment_question"].strip()
            hooks = [h.strip() for h in got["thumbnail_hooks"]]
            character = (got.get("thumbnail_character") or "").strip() or character
            pack = assemble()
            errors = check(pack)
        except meta.AIFailure as e:
            warnings.append(f"the second AI call failed ({e}); the first answer is kept")
    if ai is not None:
        ai_state = {"ai": ai.engine, "ai_note": ai.reason}

    # --- outputs
    now = meta.now_utc()
    rows = meta.checklist(book_cfg.get("series_label") or book_title, pub_cfg.get("category"),
                          pub_cfg.get("playlist_name"), pub_cfg.get("publish_time"))
    out = dict(pack)
    out.update({
        "episode": ep, "chapters": {"start": start, "end": end}, "volume": vol,
        "timestamps": [{"seconds": s, "label": label} for s, label in stamps],
        "timestamp_source": ts["file"],
        "thumbnail_character": character,
        "ai": ai_state["ai"], "ai_note": ai_state["ai_note"],
        "ai_context": {"chapters": len(chapters), "chars_sent": sent},
        "research_snapshot": snapshot,
        "valid": not errors, "errors": errors, "warnings": warnings,
        "generated_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    archived = meta.archive_previous(pub_dir, now)
    jf, tf = pub_dir / "publish.json", pub_dir / "publish.txt"
    jf.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tf.write_text(meta.render_txt(out, book=book, ep=ep, checklist_rows=rows, character=character),
                  encoding="utf-8")

    # --- console
    print(f"\n{context.header(book, ep)}")
    print(f"Title: {pack['title']}")
    print(f"  title        {len(pack['title']):>5} / {validate.TITLE_MAX}")
    print(f"  description  {len(pack['description']):>5} / {validate.DESCRIPTION_MAX}")
    print(f"  tags         {len(pack['tags']):>5} tags, {validate.tags_length(pack['tags'])} / {validate.TAGS_MAX} characters")
    print(f"  hashtags     {len(pack['hashtags']):>5}")
    print(f"AI: {ai_state['ai']}" + (f"  ({ai_state['ai_note']})" if ai_state["ai_note"] else ""))
    print("Timestamps:" + ("" if stamp_block else " none"))
    for line in stamp_block:
        print(f"  {line}")
    for wmsg in warnings:
        print(f"warning: {wmsg}")
    if archived:
        print(f"previous pair moved to {archived}")
    print(f"wrote {jf}")
    print(f"wrote {tf}")
    if errors:
        print("NOT VALID:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("valid. Next: paste publish.txt into Studio, then: novel thumb")
    return 0
