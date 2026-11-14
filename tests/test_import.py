"""novel import: the Gemini reply contract (fixture book 'tide', invented text, temp NOVEL_HOME)."""

import hashlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from novel import bible as B
from novel.commands import import_cmd, prompt_cmd
from novel.core import paths
from novel.gemini import contract as C
from novel.gemini import prompt as P
from novel.stages import episode as E
from novel.stages import episode_scenes as S
from novel.stages import episode_translate as T

FIX = Path(__file__).parent / "fixtures" / "gemini"
BATCH = "B001-002"
SHOT_CYCLE = ["wide", "medium", "low-angle", "over-the-shoulder", "high-angle", "detail", "establishing"]


# ---------------------------------------------------------------- fixture book

def setup_book(tmp_path, monkeypatch, name="tide", settings=None):
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir(exist_ok=True)
    data.mkdir(exist_ok=True)
    monkeypatch.setenv("NOVEL_HOME", str(home))
    (home / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    bd = data / name
    shutil.copytree(FIX / "chapters", bd / "chapters")
    shutil.copytree(FIX / "bible", bd / "bible")
    book_yaml = {"book": {"title": "Tide Tales"}}
    if settings:
        book_yaml.update(settings)
    (bd / "book.yaml").write_text(yaml.safe_dump(book_yaml), encoding="utf-8")
    ep = bd / "episodes" / "E001"
    shutil.copytree(FIX / "hindi", ep / "hindi")
    (ep / "episode.json").write_text(json.dumps({"episode": 1, "start": 1, "end": 2}), encoding="utf-8")
    monkeypatch.setattr(E, "EPISODES", paths.episodes_dir(name))
    T.cmd_lock(SimpleNamespace(episode=1, chapters="1-2"))           # the real lock code
    r = P.generate(name, 1, 2, "scenes")                              # the real `novel prompt` code
    assert r["batch"] == BATCH and r["mode"] == "scenes"
    return bd


@pytest.fixture
def book(tmp_path, monkeypatch):
    return setup_book(tmp_path, monkeypatch)


def counts(bd):
    meta = json.loads((bd / "gemini" / BATCH / "batch.json").read_text(encoding="utf-8"))
    return {int(k): v for k, v in meta["windows"].items()}


def ep(bd):
    return bd / "episodes" / "E001"


def run(monkeypatch, capsys, text=None, args=(), book="tide"):
    """novel import with `text` as the clipboard. Returns (exit code, stdout+stderr)."""
    if text is not None:
        monkeypatch.setattr(import_cmd, "read_clipboard", lambda: text)
    from novel.cli import main
    code = main(["import", "--book", book, *args])
    cap = capsys.readouterr()
    return code, cap.out + cap.err


def say(out):
    lines = [l for l in out.splitlines() if l.startswith("Say to Gemini: ")]
    assert lines, out
    return lines[-1][len("Say to Gemini: "):]


# ---------------------------------------------------------------- reply builders

def cast(key, var="base", hands="empty, at the sides", pose="standing", mood="calm", wearing=None):
    return {"key": key, "var": var, "pose": pose, "hands": hands, "mood": mood, "wearing": wearing}


def group(gid="g01", loc="harbour_quay", present=("maren_vale", "tobias_reed", "old_harl"), **kw):
    g = {"type": "group", "group": gid, "loc": loc, "time": "dawn", "light": "pale light from the left",
         "weather": "clear", "present": list(present),
         "staging": "Maren at the quay edge on the left, Tobias walking in from the right, Harl by his boat"}
    g.update(kw)
    return g


def action(n):
    return (f"Picture {n}: Maren Vale stands at the edge of the stone quay looking over the grey water while gulls "
            f"circle above the harbour wall and the first light spreads over the boats.")


def image(n, gid="g01", shot=None, cast_=None, **kw):
    im = {"type": "image", "n": n, "group": gid, "shot": shot or SHOT_CYCLE[(n - 1) % len(SHOT_CYCLE)],
          "cast": cast_ if cast_ is not None else [cast("maren_vale")] if n % 2 else
          [cast("maren_vale"), cast("tobias_reed")],
          "others": "", "action": action(n), "focus": [0.5, 0.45]}
    im.update(kw)
    return im


def chapter_lines(count, chapter=1, start=1, end=None, groups=True):
    end = end or count
    loc = "harbour_quay" if chapter == 1 else "net_loft"
    half = max(1, count // 2)
    out = []
    for n in range(start, end + 1):
        gid = "g01" if n <= half else "g02"
        if groups and (n == start or n == half + 1):
            out.append(group(gid, loc=loc))
        out.append(image(n, gid))
    return out


def jl(objs):
    return "\n".join(json.dumps(o, ensure_ascii=False) for o in objs)


def block(kind, bid, body, fence=True, end=True, arrow="->", file=None):
    names = {"BIBLE": f"bible-{bid}.jsonl", "IMAGES": f"{bid}.images.jsonl", "PATCH": f"{bid}.patch.jsonl",
             "HINDI": f"{bid}.hindi.txt"}
    if not isinstance(body, str):
        body = jl(body)
    inner = f"```jsonl\n{body}\n```" if fence else body
    text = f"===== BEGIN {kind} {bid} {arrow} inbox/{file or names[kind]} =====\n{inner}\n"
    if end:
        text += f"===== END {kind} {bid} =====\n"
    return text


BELL_TOWER = {"type": "location", "key": "bell_tower", "name": "Bell Tower", "kind": "interior", "chapters": "1",
              "block": ("A narrow stone tower room at the end of the quay. A great bronze bell hangs from a heavy oak "
                        "beam, a spiral stair winds down through a hole in the floor, four arched openings look out "
                        "over the harbour on every side and pigeons nest along the ledges under the slate roof."),
              "default_light": "cold daylight through the arches"}
NAME_LINE = {"type": "name", "en": "Bell Tower", "hi": "घंटाघर"}


def bible_reply():
    return "Here is the bible.\n\n" + block("BIBLE", BATCH, [BELL_TOWER, NAME_LINE])


def full_run(monkeypatch, capsys, bd):
    c = counts(bd)
    code, out = run(monkeypatch, capsys, bible_reply())
    assert code == 0, out
    for ch in (1, 2):
        code, out = run(monkeypatch, capsys, block("IMAGES", f"c{ch:04d}", chapter_lines(c[ch], ch)))
        assert code == 0, out
    return out


def listing(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
            for p in sorted(root.rglob("*"))}


# ---------------------------------------------------------------- the clean run

def test_fixture_has_several_windows(book):
    c = counts(book)
    assert c[1] >= 4 and c[2] >= 3


def test_clean_run_end_to_end(book, monkeypatch, capsys):
    c = counts(book)
    code, out = run(monkeypatch, capsys, bible_reply())
    assert code == 0, out
    assert "BIBLE B001-002  ok: 1 location, 1 name applied to the bible" in out
    assert "Ignored 1 line outside blocks." in out
    assert say(out) == "next"
    assert "bell_tower" in B.load_locations(book / "bible")["locations"]
    assert list((book / "bible" / "history").glob("locations-*.json"))          # snapshot before the write
    assert (book / "inbox" / "_done" / f"bible-{BATCH}.jsonl").exists()
    raws = sorted((book / "gemini" / BATCH / "raw").glob("*.txt"))
    assert len(raws) == 1 and raws[0].name.startswith("001-")

    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(c[1], 1)))
    assert code == 0, out
    assert f"chapter 1 complete: {c[1]} images in 2 groups" in out
    assert say(out) == "next"
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0002", chapter_lines(c[2], 2)))
    assert code == 0, out
    assert say(out) == "nothing: batch B001-002 is complete. Next: novel qa --chapters 1-2"
    assert "bible     done" in out and f"{c[2]} of {c[2]}" in out

    for n in (1, 2):
        doc = json.loads(S.scenes_path(ep(book), n).read_text(encoding="utf-8"))
        assert doc["version"] == 2 and doc["pacing"] == "7s" and doc["chapter"] == n
        assert doc["pacing_params"] == {"paced_seconds": 7.0, "max_seconds": 7.5, "chars_per_second": 15.3}
        assert doc["hindi_sha256"] == E.sha256(ep(book) / "hindi" / f"{n:05d}.txt")
        assert doc["source"]["batch"] == BATCH
        assert set(doc["groups"]) == {"g01", "g02"}
        wins = S.chapter_windows(S.chapter_paras(ep(book), n), "7s")
        assert [s["para"] for s in doc["scenes"]] == [w[0] for w in wins]
        for s in doc["scenes"]:
            assert s["scene"] == s["action"] and s["characters"] == [x["key"] for x in s["cast"]]
            assert {"id", "para", "group", "shot", "cast", "others", "action", "focus", "characters",
                    "scene"} <= set(s)
        assert S.check_chapter(ep(book), n) == []
        assert not (book / "inbox" / f"c{n:04d}.images.jsonl").exists()
        assert (book / "inbox" / "_done" / f"c{n:04d}.images.jsonl").exists()
    prog = json.loads((book / "gemini" / BATCH / "progress.json").read_text(encoding="utf-8"))
    assert prog["bible"]["done"] and prog["chapters"]["1"]["status"] == "done"


def test_narration_reads_a_v2_scene_file(book, monkeypatch, capsys):
    """narration() needs images, an mp3 and a timing map: fakes for those; the scene file is the real one."""
    from novel.stages import episode_video as V
    full_run(monkeypatch, capsys, book)
    root = ep(book)
    paras = S.chapter_paras(root, 1)
    doc = json.loads(S.scenes_path(root, 1).read_text(encoding="utf-8"))
    img = root / "images" / "c0001"
    img.mkdir(parents=True)
    for s in doc["scenes"]:
        (img / f"s{s['id']:02d}-2k.jpg").write_bytes(b"jpg")
    (root / "audio").mkdir()
    (root / "audio" / "00001.mp3").write_bytes(b"mp3")
    chunks, t = [], 0.0
    for i, p in enumerate(paras):
        d = len(p) / 15.3
        chunks.append({"para": i, "start_sec": round(t, 3), "end_sec": round(t + d, 3)})
        t += d + 0.4
    (root / "timing").mkdir()
    (root / "timing" / "00001.json").write_text(json.dumps({"total_seconds": t, "chunks": chunks}), encoding="utf-8")
    monkeypatch.setattr(V, "probe", lambda path: {"format": {"duration": str(t)}})
    nar = V.narration(root, 1)
    assert len(nar["starts"]) == len(nar["scenes"]) == counts(book)[1]
    assert nar["starts"] == sorted(nar["starts"])
    assert nar["images"][0].name == "s01-2k.jpg"


# ---------------------------------------------------------------- tolerant parsing

def test_unfenced_crlf_arrow_and_markdown_delimiters(book, monkeypatch, capsys):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    body = jl(chapter_lines(c[1], 1))
    text = (f"**===== BEGIN images c1 → inbox/whatever.txt =====**\n{body}\n"
            f"`=======  END IMAGES c0001  =======`\n")
    code, out = run(monkeypatch, capsys, text.replace("\n", "\r\n"))
    assert code == 0, out
    assert "chapter 1 complete" in out
    assert "using inbox/c0001.images.jsonl" in out                    # wrong file name: warned, ignored


def test_quoted_block_with_leading_gt(book, monkeypatch, capsys):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    raw = block("IMAGES", "c0001", chapter_lines(c[1], 1))
    text = "\n".join("> " + l for l in raw.splitlines())
    code, out = run(monkeypatch, capsys, text)
    assert code == 0, out
    assert "chapter 1 complete" in out


def test_pretty_printed_and_trailing_comma_repaired(book, monkeypatch, capsys):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    lines = chapter_lines(c[1], 1)
    parts = [json.dumps(lines[0], indent=2, ensure_ascii=False)]                 # pretty-printed group
    parts += [json.dumps(o, ensure_ascii=False) + "," for o in lines[1:3]]      # trailing commas
    parts += [json.dumps(o, ensure_ascii=False).replace(", ", ", ") for o in lines[3:]]  # NBSP
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", "\n".join(parts)))
    assert code == 0, out
    assert "re-joined" in out and "trailing comma at the end of the line removed" in out
    assert "non-breaking spaces replaced" in out
    assert "chapter 1 complete" in out


def test_smart_quotes_rejected_with_specific_message(book, monkeypatch, capsys):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    lines = chapter_lines(c[1], 1)
    body = jl(lines).split("\n")
    body[1] = body[1].replace('"', "“", 3).replace('"', "”", 1)
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", "\n".join(body)))
    assert code == 1
    assert "IMAGES c0001  REJECTED" in out
    assert "curly quotes" in out and "straight double quotes" in out and "line 3" in out
    assert say(out).startswith("repeat c0001 IMAGES")
    assert not S.scenes_path(ep(book), 1).exists()


def test_missing_end_is_cut_off(book, monkeypatch, capsys):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(c[1], 1)[:3], end=False))
    assert code == 1
    assert "cut off: no END line" in out
    assert say(out) == "repeat c0001 IMAGES"
    assert not (book / "inbox" / "c0001.images.jsonl").exists()


def test_bible_cut_off_says_repeat_bible(book, monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, block("BIBLE", BATCH, [NAME_LINE], end=False))
    assert code == 1 and say(out) == f"repeat BIBLE {BATCH}"


def test_more_and_images_split_across_two_replies(book, monkeypatch, capsys):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    k = c[1] // 2
    part1 = block("IMAGES", "c0001", chapter_lines(c[1], 1, 1, k)) + ">>> MORE c0001 IMAGES\n"
    code, out = run(monkeypatch, capsys, part1)
    assert code == 0, out
    assert f"chapter 1: {k} of {c[1]} images (waiting for {k + 1}-{c[1]})" in out
    assert say(out) == "next"
    assert f"{k} of {c[1]}" in out and "partial (Gemini owes the rest)" in out
    assert (book / "inbox" / "c0001.images.jsonl").exists()
    assert not S.scenes_path(ep(book), 1).exists()
    part2 = block("IMAGES", "c0001", chapter_lines(c[1], 1, k + 1, c[1]))
    code, out = run(monkeypatch, capsys, part2)
    assert code == 0, out
    assert "chapter 1 complete" in out and S.check_chapter(ep(book), 1) == []
    assert say(out) == "next"


def test_restart_from_image_one_replaces_the_staged_part(book, monkeypatch, capsys):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(c[1], 1, 1, 2)) + ">>> MORE c0001 IMAGES\n")
    lines = chapter_lines(c[1], 1)
    lines[1]["action"] = action(1) + " A second take of the same moment."
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines))
    assert code == 0, out
    assert "starts again at image 1" in out and "chapter 1 complete" in out
    assert list((book / "inbox" / "_done").glob("c0001.images.superseded*.jsonl"))


# ---------------------------------------------------------------- counting

def test_duplicate_n_with_different_content(book, monkeypatch, capsys):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    lines = chapter_lines(c[1], 1)
    dup = image(2, action=action(2) + " Another version.")
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines + [dup]))
    assert code == 1
    assert "image 2 appears twice in this block with different content" in out


def test_duplicate_n_against_an_earlier_part(book, monkeypatch, capsys):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(c[1], 1, 1, 3)) + ">>> MORE c0001 IMAGES\n")
    part2 = chapter_lines(c[1], 1, 4, c[1])
    part2.insert(0, image(3, action=action(3) + " Changed."))
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", part2))
    assert code == 1
    assert "image 3 was already received in an earlier part with different content" in out


def test_too_few_images(book, monkeypatch, capsys):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    lines = [l for l in chapter_lines(c[1], 1) if l.get("n") not in (2, 3)]
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines))
    assert code == 1
    assert "too few images: windows 2-3 have no image line" in out
    assert say(out) == f"repeat c0001 IMAGES. FIX: chapter 1 has {c[1]} windows; image lines are missing for windows 2-3"
    assert not (book / "inbox" / "c0001.images.jsonl").exists()


def test_too_many_images(book, monkeypatch, capsys):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    lines = chapter_lines(c[1], 1) + [image(c[1] + 1, "g02")]
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines))
    assert code == 1
    assert f"window {c[1] + 1} does not exist: chapter 1 has {c[1]}" in out


# ---------------------------------------------------------------- content errors

def _with(book, monkeypatch, capsys, change, chapter=1):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    lines = chapter_lines(c[chapter], chapter)
    change(lines)
    return run(monkeypatch, capsys, block("IMAGES", f"c{chapter:04d}", lines))


def test_unknown_group(book, monkeypatch, capsys):
    code, out = _with(book, monkeypatch, capsys, lambda L: L[1].update(group="g09"))
    assert code == 1 and "unknown group 'g09'" in out


def test_unknown_location(book, monkeypatch, capsys):
    code, out = _with(book, monkeypatch, capsys, lambda L: L[0].update(loc="harbour_qauy"))
    assert code == 1
    assert "location 'harbour_qauy' is not in the location registry (did you mean harbour_quay?)" in out


def test_unknown_character_and_variant(book, monkeypatch, capsys):
    def change(L):
        L[1]["cast"] = [cast("maren_vael")]
        L[2]["cast"] = [cast("tobias_reed", var="winter")]
    code, out = _with(book, monkeypatch, capsys, change)
    assert code == 1
    assert "unknown character 'maren_vael' (did you mean maren_vale?)" in out
    assert "character 'tobias_reed' has no variant 'winter' (has: base)" in out


def test_variant_outside_its_range(book, monkeypatch, capsys):
    code, out = _with(book, monkeypatch, capsys, lambda L: L[1].update(cast=[cast("maren_vale", var="storm_coat")]))
    assert code == 1
    assert "variant 'storm_coat' covers chapters 2, not chapter 1" in out
    # ...and is accepted in chapter 2
    c = counts(book)
    lines = chapter_lines(c[2], 2)
    lines[1]["cast"] = [cast("maren_vale", var="storm_coat")]
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0002", lines))
    assert code == 0, out


def test_missing_hands_and_pose(book, monkeypatch, capsys):
    def change(L):
        del L[1]["cast"][0]["hands"]
        del L[2]["cast"][0]["pose"]
    code, out = _with(book, monkeypatch, capsys, change)
    assert code == 1
    assert "line 3 (image 1 cast[0] maren_vale.hands): missing or empty" in out
    assert "cast[0] maren_vale.pose): missing" in out
    assert "FIX: image 1 cast[0] maren_vale.hands: missing or empty" in say(out)


def test_attached_limit_ignores_text_only_people(tmp_path, monkeypatch, capsys):
    bd = setup_book(tmp_path, monkeypatch, settings={"images": {"max_characters": 2}})
    three = [cast("maren_vale"), cast("tobias_reed"), cast("old_harl")]
    code, out = _with(bd, monkeypatch, capsys, lambda L: L[1].update(cast=three))
    assert code == 0, out                                    # old_harl is tier 3, text only: not counted
    (bd / "book.yaml").write_text(yaml.safe_dump({"book": {"title": "Tide Tales"},
                                                  "images": {"max_characters": 1}}), encoding="utf-8")
    lines = chapter_lines(counts(bd)[2], 2)
    lines[1]["cast"] = three
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0002", lines))
    assert code == 1
    assert "2 attached characters (with a Flow reference); at most 1" in out


def test_banned_word_and_devanagari(book, monkeypatch, capsys):
    def change(L):
        L[1]["action"] = action(1) + " Nobody is naked here."
        L[2]["action"] = action(2) + " मैरेन देखती है।"
    code, out = _with(book, monkeypatch, capsys, change)
    assert code == 1
    assert "uses the banned word 'naked'" in out and "must be English (Devanagari found)" in out


def test_short_action_and_bad_focus_and_shot(book, monkeypatch, capsys):
    def change(L):
        L[1]["action"] = "Too short."
        L[2]["focus"] = [0.5, 1.4]
        L[3]["shot"] = "close-up"
    code, out = _with(book, monkeypatch, capsys, change)
    assert code == 1
    assert "10 characters; needs 60-600" in out and "between 0 and 1" in out and "'close-up' is not one of" in out


def test_bible_key_in_action_is_replaced(book, monkeypatch, capsys):
    code, out = _with(book, monkeypatch, capsys,
                      lambda L: L[1].update(action="tobias_reed hands a net to maren_vale at the edge of harbour_quay "
                                                   "while the gulls circle overhead."))
    assert code == 0, out
    assert "bible keys replaced by display names" in out and "tobias_reed -> Tobias Reed" in out
    doc = json.loads(S.scenes_path(ep(book), 1).read_text(encoding="utf-8"))
    assert doc["scenes"][0]["action"].startswith("Tobias Reed hands a net to Maren Vale at the edge of Harbour Quay")


def test_chapter_warnings(tmp_path, monkeypatch, capsys):
    bd = setup_book(tmp_path, monkeypatch)
    c = counts(bd)
    run(monkeypatch, capsys, bible_reply())
    lines = [group("g01", present=("maren_vale",))]
    for n in range(1, c[1] + 1):
        lines.append(image(n, "g01", shot="medium",
                           cast_=[cast("maren_vale", hands="holding her lantern up", wearing="a red scarf")]
                           + ([cast("tobias_reed")] if n == 1 else [])))
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", lines))
    assert code == 0, out
    assert "tobias_reed is in the cast but not in group g01's present" in out
    assert "shot 'medium' three times in a row" in out
    assert c[1] == 7                                         # the fixture's chapter 1 has 7 windows
    assert "maren_vale wears \"a red scarf\" on 7 images" in out and "make a variant" in out
    assert "group g01: 'medium' on 7 of 7 images (at most a third)" in out
    assert "maren_vale: a signature prop (lantern) is in the hands" in out


def test_wearing_warning_needs_more_than_six(monkeypatch):
    ctx = C.ChapterCtx(1, 8, {"characters": {"characters": {}}, "locations": {"locations": {}}, "names": {}})
    imgs = {n: {"n": n, "group": "g01", "shot": SHOT_CYCLE[n % 7], "cast": [cast("a", wearing="pyjamas")]}
            for n in range(1, 8)}
    assert any("make a variant" in w for w in C.chapter_warnings({}, imgs, ctx))
    imgs.pop(7)
    assert not any("make a variant" in w for w in C.chapter_warnings({}, imgs, ctx))


# ---------------------------------------------------------------- bible blocks

def test_chapter_extra_bible_applied_before_images(book, monkeypatch, capsys):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    newcomer = {"type": "character", "key": "gull_keeper", "display": "Gull Keeper", "hi": "गल कीपर", "tier": 2,
                "minor": False, "role": "Feeds the gulls.", "first_chapter": 1,
                "look": "short round man, bald, bushy grey eyebrows, a knitted cardigan", "props": ["bread bag"]}
    lines = chapter_lines(c[1], 1)
    lines[0]["present"].append("gull_keeper")
    lines[1]["cast"] = [cast("gull_keeper")]
    reply = block("BIBLE", f"{BATCH}-c0001", [newcomer]) + "\n" + block("IMAGES", "c0001", lines)
    code, out = run(monkeypatch, capsys, reply)
    assert code == 0, out
    assert f"BIBLE {BATCH}-c0001  ok: 1 character applied" in out and "chapter 1 complete" in out
    assert "gull_keeper" in B.load_characters(book / "bible")["characters"]


def test_bible_errors_map_to_block_lines(book, monkeypatch, capsys):
    bad = dict(BELL_TOWER, block="far too short")
    code, out = run(monkeypatch, capsys, block("BIBLE", BATCH, [NAME_LINE, bad]))
    assert code == 1
    assert "line 3 (location bell_tower): block has 3 words, needs 40-120" in out    # line 1 is the fence
    assert say(out).startswith(f"repeat BIBLE {BATCH}. FIX: location bell_tower: block has 3 words")
    assert "bell_tower" not in B.load_locations(book / "bible")["locations"]


def test_images_before_bible_says_repeat_bible(book, monkeypatch, capsys):
    c = counts(book)
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(c[1], 1)))
    assert code == 0, out
    assert say(out) == f"repeat BIBLE {BATCH}"


# ---------------------------------------------------------------- PATCH

def test_patch_replaces_two_images_and_moves_their_files(book, monkeypatch, capsys):
    full_run(monkeypatch, capsys, book)
    img = ep(book) / "images" / "c0001"
    (img / "clean").mkdir(parents=True)
    for k in (1, 2, 3):
        for suffix in ("-2k.jpg", ".prompt.txt", ".flow.json", ".used.txt"):
            (img / f"s{k:02d}{suffix}").write_text("x", encoding="utf-8")
        (img / "clean" / f"s{k:02d}.jpg").write_text("x", encoding="utf-8")
    new1 = image(1, "g01", action=action(1) + " Patched: she turns toward the bell tower.")
    new3 = image(3, "g01", shot="detail", action=action(3) + " Patched as a detail of the rope.")
    code, out = run(monkeypatch, capsys, block("PATCH", "c0001", [new1, new3]))
    assert code == 0, out
    assert "2 image lines and 0 group lines replaced; pictures to make again: 1, 3" in out
    doc = json.loads(S.scenes_path(ep(book), 1).read_text(encoding="utf-8"))
    assert doc["scenes"][0]["action"].endswith("toward the bell tower.") and doc["scenes"][2]["shot"] == "detail"
    assert doc["scenes"][0]["scene"] == doc["scenes"][0]["action"]
    assert doc["source"]["patched"][0]["images"] == [1, 3]
    olds = list((img / "old").iterdir())
    assert len(olds) == 1
    moved = sorted(str(p.relative_to(olds[0])).replace("\\", "/") for p in olds[0].rglob("*") if p.is_file())
    assert moved == sorted([f"s{k:02d}{s}" for k in (1, 3) for s in ("-2k.jpg", ".prompt.txt", ".flow.json",
                                                                     ".used.txt")] + ["clean/s01.jpg", "clean/s03.jpg"])
    assert (img / "s02-2k.jpg").exists() and not (img / "s01-2k.jpg").exists()
    assert S.check_chapter(ep(book), 1) == []
    assert list((ep(book) / "scenes" / "history").glob("00001-*.json"))
    assert (book / "inbox" / "_done" / "c0001.patch.jsonl").exists()


def test_patch_before_import(book, monkeypatch, capsys):
    run(monkeypatch, capsys, bible_reply())
    code, out = run(monkeypatch, capsys, block("PATCH", "c0001", [image(1)]))
    assert code == 1 and "has not been imported yet" in out
    assert say(out) == "repeat c0001 IMAGES"


def test_patch_with_a_bad_line_changes_nothing(book, monkeypatch, capsys):
    full_run(monkeypatch, capsys, book)
    before = S.scenes_path(ep(book), 1).read_bytes()
    code, out = run(monkeypatch, capsys, block("PATCH", "c0001", [image(1, cast_=[cast("tobias_reed", hands="")])]))
    assert code == 1 and S.scenes_path(ep(book), 1).read_bytes() == before


# ---------------------------------------------------------------- dry run, idempotence, replace

def test_dry_run_writes_nothing(book, monkeypatch, capsys):
    c = counts(book)
    root = book.parent.parent
    before = listing(root)
    reply = bible_reply() + block("IMAGES", "c0001", chapter_lines(c[1], 1))
    code, out = run(monkeypatch, capsys, reply, args=["--dry-run"])
    assert code == 0, out
    assert "dry run" in out and "not archived (--dry-run)" in out and "chapter 1 complete" in out
    assert "bible     done" in out and "done (dry run: not written)" in out and say(out) == "next"
    assert listing(root) == before                    # data folder and NOVEL_HOME: not one byte changed
    # a rejected block in a dry run also writes nothing, and still exits 1
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(c[1], 1)[:2]), args=["--dry-run"])
    assert code == 1 and listing(root) == before


def test_single_word_keys_only_when_the_name_is_the_same_word():
    state = {"characters": {"characters": {"leah": {"display": "Leah"}, "owl": {"display": "The Owl"},
                                           "maren_vale": {"display": "Maren Vale"}}},
             "locations": {"locations": {}}, "names": {}}
    ctx = C.ChapterCtx(1, 3, state)
    text, found = C.replace_keys("leah watches an owl land beside maren_vale.", ctx)
    assert text == "Leah watches an owl land beside Maren Vale."
    assert found == ["maren_vale -> Maren Vale", "leah -> Leah"]


def test_same_paste_twice_is_idempotent(book, monkeypatch, capsys):
    c = counts(book)
    reply = bible_reply() + block("IMAGES", "c0001", chapter_lines(c[1], 1))
    code, out1 = run(monkeypatch, capsys, reply)
    assert code == 0, out1
    doc1 = S.scenes_path(ep(book), 1).read_bytes()
    bible1 = (book / "bible" / "locations.json").read_bytes()
    code, out2 = run(monkeypatch, capsys, reply)
    assert code == 0, out2
    assert "already archived; processed again" in out2
    assert "already imported (identical); nothing to do" in out2
    assert "already imported (identical to" in out2
    assert S.scenes_path(ep(book), 1).read_bytes() == doc1
    assert (book / "bible" / "locations.json").read_bytes() == bible1
    assert len(list((book / "gemini" / BATCH / "raw").glob("*.txt"))) == 1
    assert say(out1) == say(out2) == "next"


def test_existing_scene_file_with_images_needs_replace(book, monkeypatch, capsys):
    full_run(monkeypatch, capsys, book)
    img = ep(book) / "images" / "c0001"
    img.mkdir(parents=True)
    (img / "s01-2k.jpg").write_text("x", encoding="utf-8")
    c = counts(book)
    lines = chapter_lines(c[1], 1)
    lines[1]["action"] = action(1) + " A new take."
    reply = block("IMAGES", "c0001", lines)
    code, out = run(monkeypatch, capsys, reply)
    assert code == 1
    assert "already imported" in out and "1 image" in out and "novel import --replace" in out
    assert say(out) == "nothing yet. First run: novel import --replace"
    code, out = run(monkeypatch, capsys, reply, args=["--replace"])
    assert code == 0, out
    old = ep(book) / "old" / "v1"
    assert (old / "scenes" / "00001.json").exists() and (old / "images" / "c0001" / "s01-2k.jpg").exists()
    doc = json.loads(S.scenes_path(ep(book), 1).read_text(encoding="utf-8"))
    assert doc["scenes"][0]["action"].endswith("A new take.")
    code, out = run(monkeypatch, capsys, reply.replace("A new take.", "A third take."), args=["--replace"])
    assert code == 0 and (ep(book) / "old" / "v2" / "scenes" / "00001.json").exists()


def test_hindi_changed_after_prompt(book, monkeypatch, capsys):
    run(monkeypatch, capsys, bible_reply())
    f = ep(book) / "hindi" / "00001.txt"
    f.write_text(f.read_text(encoding="utf-8") + "\nएक नई लाइन यहां जुड़ती है और कहानी आगे बढ़ती है।\n",
                 encoding="utf-8")
    T.cmd_lock(SimpleNamespace(episode=1, chapters="1"))            # re-locked: only the batch knows
    c = counts(book)
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(c[1], 1)))
    assert code == 1
    assert "changed after `novel prompt` made batch B001-002" in out
    assert say(out) == "nothing yet. First run: novel prompt --chapters 1-2 (then paste the new prompt into a new Gemini chat)"


def test_hindi_changed_after_lock(book, monkeypatch, capsys):
    f = ep(book) / "hindi" / "00001.txt"
    f.write_text(f.read_text(encoding="utf-8") + "\nएक लाइन।\n", encoding="utf-8")
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0001", chapter_lines(counts(book)[1], 1)))
    assert code == 1 and "changed after it was locked" in out


def test_no_batch_names_the_prompt_command(book, monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, block("BIBLE", "B031-040", [NAME_LINE]))
    assert code == 1
    assert "no prompt batch B031-040" in out and say(out) == "nothing yet. First run: novel prompt --chapters 31-40"
    assert list((book / "gemini" / "_unmatched" / "raw").glob("*.txt"))      # still archived


def test_no_blocks_at_all(book, monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, "Sure! Here you go.")
    assert code == 1 and "no blocks found" in out


def test_hindi_block_is_saved_not_imported(book, monkeypatch, capsys):
    run(monkeypatch, capsys, bible_reply())
    text = "===== BEGIN HINDI c0001 -> inbox/c0001.hindi.txt =====\nचैप्टर एक: टेस्ट\n\n{1} कुछ।\n===== END HINDI c0001 =====\n"
    code, out = run(monkeypatch, capsys, text)
    assert code == 0, out
    assert "HINDI import is not built yet" in out
    assert (book / "inbox" / "c0001.hindi.txt").read_text(encoding="utf-8").startswith("चैप्टर एक")


def test_only_more_line_says_next(book, monkeypatch, capsys):
    run(monkeypatch, capsys, bible_reply())
    code, out = run(monkeypatch, capsys, ">>> MORE c0001 IMAGES\n")
    assert code == 0 and say(out) == "next"


def test_skipped_chapter_says_repeat_it(book, monkeypatch, capsys):
    run(monkeypatch, capsys, bible_reply())
    code, out = run(monkeypatch, capsys, block("IMAGES", "c0002", chapter_lines(counts(book)[2], 2)))
    assert code == 0 and say(out) == "repeat c0001 IMAGES"


# ---------------------------------------------------------------- the inbox path

def test_inbox_gives_the_same_result_as_the_clipboard(tmp_path, monkeypatch, capsys):
    a = setup_book(tmp_path, monkeypatch, name="tide")
    c = counts(a)
    full_run(monkeypatch, capsys, a)
    b = setup_book(tmp_path, monkeypatch, name="tide2")
    inbox = b / "inbox"
    inbox.mkdir()
    (inbox / f"bible-{BATCH}.jsonl").write_text(jl([BELL_TOWER, NAME_LINE]) + "\n", encoding="utf-8")
    (inbox / "c0001.images.jsonl").write_text("```jsonl\n" + jl(chapter_lines(c[1], 1)) + "\n```\n", encoding="utf-8")
    (inbox / "c0002.images.jsonl").write_text(jl(chapter_lines(c[2], 2)), encoding="utf-8")
    (inbox / "notes.txt").write_text("mine", encoding="utf-8")
    code, out = run(monkeypatch, capsys, args=["--inbox"], book="tide2")
    assert code == 0, out
    assert "notes.txt is not a contract file name" in out
    assert say(out) == "nothing: batch B001-002 is complete. Next: novel qa --chapters 1-2"
    for n in (1, 2):
        da = json.loads(S.scenes_path(ep(a), n).read_text(encoding="utf-8"))
        db = json.loads(S.scenes_path(ep(b), n).read_text(encoding="utf-8"))
        da["source"].pop("imported"), db["source"].pop("imported")
        assert da == db
    assert sorted(p.name for p in inbox.iterdir()) == ["_done", "notes.txt"]
    assert sorted(p.name for p in (inbox / "_done").iterdir()) == [f"bible-{BATCH}.jsonl", "c0001.images.jsonl",
                                                                  "c0002.images.jsonl"]
    assert B.load_locations(a / "bible") == B.load_locations(b / "bible")


def test_inbox_partial_file_waits(book, monkeypatch, capsys):
    c = counts(book)
    run(monkeypatch, capsys, bible_reply())
    (book / "inbox").mkdir(exist_ok=True)
    (book / "inbox" / "c0001.images.jsonl").write_text(jl(chapter_lines(c[1], 1, 1, 2)), encoding="utf-8")
    code, out = run(monkeypatch, capsys, args=["--inbox"])
    assert code == 0, out
    assert f"2 of {c[1]}" in out and (book / "inbox" / "c0001.images.jsonl").exists()


def test_file_option(book, monkeypatch, capsys, tmp_path):
    f = tmp_path / "reply.txt"
    f.write_text(bible_reply(), encoding="utf-8")
    code, out = run(monkeypatch, capsys, args=["--file", str(f)])
    assert code == 0 and f"file {f}" in out and say(out) == "next"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows clipboard")
def test_real_clipboard_round_trip_with_devanagari(book, monkeypatch, capsys):
    if not (prompt_cmd.set_clipboard("probe") and prompt_cmd.get_clipboard() == "probe"):
        pytest.skip("clipboard not available")
    reply = bible_reply()
    assert "घंटाघर" in reply
    assert prompt_cmd.set_clipboard(reply)
    from novel.cli import main
    code = main(["import", "--book", "tide"])                  # the real clipboard reader
    out = capsys.readouterr().out
    assert code == 0, out
    assert B.load_names(book / "bible")["Bell Tower"] == "घंटाघर"
    raw = next((book / "gemini" / BATCH / "raw").glob("*.txt"))
    assert raw.read_text(encoding="utf-8") == reply.replace("\n", "\r\n") or \
        raw.read_text(encoding="utf-8").replace("\r\n", "\n") == reply


# ---------------------------------------------------------------- contract unit checks and v1 compatibility

def test_ids_and_inbox_names():
    assert C.chapter_of("c21") == C.chapter_of("C0021") == C.chapter_of("c0021") == 21
    assert C.bible_id("b21-30") == ("B021-030", 21, 30, None)
    assert C.bible_id("B021-030-c21")[3] == 21
    assert C.parse_inbox_name("c0021.images.jsonl") == ("IMAGES", "c0021")
    assert C.parse_inbox_name("bible-B021-030-c0021.jsonl") == ("BIBLE", "B021-030-c0021")
    assert C.parse_inbox_name("random.txt") is None
    assert C.SHOTS_V2 == S.SHOTS_V2


def test_end_mismatch_and_unknown_kind():
    r = C.split_reply("===== BEGIN IMAGES c0021 =====\n{}\n===== END IMAGES c0022 =====\n"
                      "===== BEGIN SCENES c0021 =====\n===== END SCENES c0021 =====\n")
    assert "END line says IMAGES c0022" in r.blocks[0].errors[0].msg
    assert "unknown block kind 'SCENES'" in r.blocks[1].errors[0].msg


def test_v1_scene_file_still_passes(book):
    """A finished-episode style v1 file (episode bible v1, close-up allowed, 4 characters max)."""
    root = ep(book)
    (root / "bible").mkdir()
    roster = {"style": "x", "characters": {k: {"display": k.title(), "flow_name": k.title(), "appearance": "a"}
                                           for k in ("ann", "bo", "cy", "di", "ed")}}
    (root / "bible" / "characters.json").write_text(json.dumps(roster), encoding="utf-8")
    paras = S.chapter_paras(root, 1)
    wins = S.chapter_windows(paras, "7s")
    lock = json.loads(E.lock_path(root).read_text(encoding="utf-8"))
    scenes = [{"id": k, "para": w[0], "characters": ["ann"], "shot": "close-up" if k == 1 else "wide",
               "focus": [0.5, 0.5], "scene": "Ann walks along a grey stone quay at dawn while the gulls circle "
                                             "above the harbour."} for k, w in enumerate(wins, 1)]
    doc = {"version": 1, "chapter": 1, "pacing": "7s", "hindi_sha256": lock["00001.txt"], "note": "v1",
           "scenes": scenes}
    S.scenes_path(root, 1).parent.mkdir(exist_ok=True)
    S.scenes_path(root, 1).write_text(json.dumps(doc), encoding="utf-8")
    assert S.check_chapter(root, 1) == []
    scenes[1]["characters"] = ["ann", "bo", "cy", "di", "ed"]
    S.scenes_path(root, 1).write_text(json.dumps(doc), encoding="utf-8")
    assert S.check_chapter(root, 1) == ["scene 2: 5 characters, at most 4"]


def test_load_roster_falls_back_to_the_book_bible(book):
    roster = S.load_roster(ep(book))
    assert roster["version"] == 2 and "maren_vale" in roster["characters"]
