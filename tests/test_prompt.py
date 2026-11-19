import json
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from novel import bible as B
from novel.commands import prompt_cmd
from novel.core import paths
from novel.gemini import prompt as P
from novel.stages import episode as E
from novel.stages import episode_scenes as S
from novel.stages import episode_translate as T

FIX = Path(__file__).parent / "fixtures" / "prompt"
# A template placeholder: a brace, a letter or underscore, word characters, a brace. JSON schema lines
# ({"type"...) and picture markers ({1}, {2}) never match it.
LEFTOVER = re.compile(r"\{[A-Za-z_]\w*\}")


@pytest.fixture
def book(tmp_path, monkeypatch):
    """Book 'demo': chapters 1-5 (English), bible v2, episode E001 = chapters 4-5 with locked Hindi."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    (home / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    bd = data / "demo"
    shutil.copytree(FIX / "chapters", bd / "chapters")
    shutil.copytree(FIX / "bible", bd / "bible")
    (bd / "book.yaml").write_text(yaml.safe_dump({"book": {"title": "The Harbour Tales"}}), encoding="utf-8")
    ep = bd / "episodes" / "E001"
    shutil.copytree(FIX / "hindi", ep / "hindi")
    (ep / "episode.json").write_text(json.dumps({"episode": 1, "start": 4, "end": 5}), encoding="utf-8")
    monkeypatch.setattr(E, "EPISODES", paths.episodes_dir("demo"))
    T.cmd_lock(SimpleNamespace(episode=1, chapters="4-5"))          # the real lock code
    return bd


def run(args):
    from novel.cli import main
    return main(["prompt", "--book", "demo", *args])


def prompt_of(bd, batch):
    return (bd / "gemini" / batch / "prompt.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------- batch id, layout, modes

def test_batch_id_padding():
    assert P.batch_id(61, 80) == "B061-080"
    assert P.batch_id(5, 9) == "B005-009"
    assert P.batch_id(995, 1004) == "B0995-1004"


def test_full_mode_layout(book, capsys):
    assert run(["--chapters", "1-3"]) == 0
    folder = book / "gemini" / "B001-003"
    assert (folder / "prompt.txt").is_file()
    assert (folder / "sources" / "chapters-1-3.txt").is_file()
    assert (folder / "sources" / "bible.txt").is_file()
    assert not list((folder / "sources").glob("windows-*"))
    meta = json.loads((folder / "batch.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "full" and meta["chapters"] == [1, 2, 3] and meta["windows"] == {}
    out = capsys.readouterr().out
    assert str(folder) in out and "Upload these files to Gemini" in out
    assert str(folder / "sources" / "chapters-1-3.txt") in out and str(folder / "sources" / "bible.txt") in out
    assert f"{len(prompt_of(book, 'B001-003'))} characters" in out
    assert "What happens next" in out and "novel import" in out and ">>> MORE" in out
    text = (folder / "sources" / "chapters-1-3.txt").read_text(encoding="utf-8")
    assert "##### CHAPTER 1: The Harbour #####" in text and "##### CHAPTER 3: Night Ferry #####" in text
    assert "Tobias was already waiting by the fruit stall" in text


def test_auto_picks_scenes_when_all_locked(book, capsys):
    assert run(["--chapters", "4-5"]) == 0
    folder = book / "gemini" / "B004-005"
    meta = json.loads((folder / "batch.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "scenes"
    assert (folder / "sources" / "windows-4-5.txt").is_file()
    out = capsys.readouterr().out
    assert "total pictures" in out and "chapter 4:" in out


def test_mixed_range_is_rejected_naming_chapters(book, capsys):
    assert run(["--chapters", "3-4"]) == 1
    err = capsys.readouterr().err
    assert "mixed" in err and "[4]" in err and "[3]" in err
    assert not (book / "gemini").exists()


def test_forced_scenes_needs_locked_hindi(book, capsys):
    assert run(["--chapters", "1-3", "--mode", "scenes"]) == 1
    assert "[1, 2, 3]" in capsys.readouterr().err


def test_missing_chapter_file_names_it_and_the_fix(book, capsys):
    assert run(["--chapters", "5-6", "--mode", "full"]) == 1
    err = capsys.readouterr().err
    assert "chapter 6" in err and "novel extract" in err


def test_changed_hindi_after_lock_is_an_error(book, capsys):
    f = book / "episodes" / "E001" / "hindi" / "00004.txt"
    f.write_text(f.read_text(encoding="utf-8") + "\nएक लाइन और।\n", encoding="utf-8")
    assert run(["--chapters", "4-5"]) == 1
    assert "changed after it was locked" in capsys.readouterr().err


# ---------------------------------------------------------------- the prompt text

@pytest.mark.parametrize("chapters", ["1-3", "4-5"])
def test_every_placeholder_is_filled(book, chapters):
    assert run(["--chapters", chapters]) == 0
    batch = P.batch_id(*P.parse_chapters(chapters))
    text = prompt_of(book, batch)
    assert LEFTOVER.findall(text) == []
    assert "{1}" in text or chapters == "4-5"                 # picture markers stay literal in full mode
    assert '"The Harbour Tales"' in text and '"Manga and Novel"' in text
    first = int(chapters.split("-")[0])
    assert f"chapters {first} to {chapters.split('-')[1]}" in text
    assert f"c{first:04d}" in text and f"bible-{batch}.jsonl" in text
    assert "about 7 seconds, ~107 letters" in text or chapters == "4-5"
    assert "at most 4" in text


def test_template_fails_loudly_on_unknown_placeholder():
    assert P.fill("{a} {1} {\"type\":1}", {"a": "x"}) == "x {1} {\"type\":1}"
    with pytest.raises(P.PromptError):
        P.fill("{nope}", {})


def test_language_rules_and_banned_words_verbatim(book):
    run(["--chapters", "1-3"])
    text = prompt_of(book, "B001-003")
    assert T.HINGLISH in text
    for w in P.banned_words():
        assert w in text
    assert set(P.banned_words()) >= {"nude", "naked", "gore", "disembowel", "dismember", "breasts"}
    assert "never the words " + ", ".join(P.banned_words()) in " ".join(text.split())
    for w in P.banned_words():                                 # each listed word really is banned
        assert S.BANNED.search(f"a {w} b")


def test_scenes_prompt_differences(book):
    run(["--chapters", "1-3"])
    run(["--chapters", "4-5"])
    full, scenes = prompt_of(book, "B001-003"), prompt_of(book, "B004-005")
    assert "HINDI c0001" in full and "its HINDI block, then its IMAGES block" in full
    assert "HINDI c0004" not in scenes and "plain text, not in a code fence" not in scenes
    assert "only the IMAGES block of the next chapter" in scenes
    assert 'windows-4-5.txt' in scenes and 'windows-4-5.txt' not in full
    assert "The Hindi narration is final; do not rewrite it." in scenes
    assert "PICTURE MARKERS" not in scenes and "PICTURE MARKERS" in full
    assert "Exactly one image line for every window, same numbers." in scenes
    assert "Exactly one image line for every marker ({1}, {2}, ...)" in full
    meta = json.loads((book / "gemini" / "B004-005" / "batch.json").read_text(encoding="utf-8"))
    for n, c in meta["windows"].items():
        assert f"- chapter {n}: {c} windows" in scenes
    # nothing specific to one novel
    for text in (full, scenes):
        assert "Circle of Inevitability" not in text and "Lumian" not in text


def test_example_lines_are_valid_json(book):
    run(["--chapters", "1-3"])
    text = prompt_of(book, "B001-003")
    example = text.split("EXAMPLE (format only")[1]
    rows = [json.loads(l) for l in example.splitlines() if l.startswith("{")]
    assert [r["type"] for r in rows] == ["group", "image"]
    group, image = rows
    assert {"group", "loc", "time", "light", "weather", "present", "staging"} <= set(group)
    assert group["time"] in ("dawn", "day", "dusk", "night")
    assert {"n", "group", "shot", "cast", "others", "action", "focus"} <= set(image)
    assert 150 <= len(image["action"]) <= 450
    assert image["group"] == group["group"]
    assert all(c["key"] in group["present"] for c in image["cast"])
    assert all({"key", "var", "pose", "hands", "mood", "wearing"} <= set(c) for c in image["cast"])
    assert image["shot"] in ("wide", "establishing", "medium", "over-the-shoulder", "low-angle",
                             "high-angle", "detail")
    assert len(image["focus"]) == 2 and all(0 <= f <= 1 for f in image["focus"])


# ---------------------------------------------------------------- bible.txt and the review task

def test_bible_txt_has_every_key(book):
    run(["--chapters", "1-3"])
    text = (book / "gemini" / "B001-003" / "sources" / "bible.txt").read_text(encoding="utf-8")
    for needle in ("NAMES", "CHARACTER BIBLE", "LOCATION REGISTRY", "mara_quill", "joss_hale", "old_joss",
                   "harbour_market", "Harbour Market", "मारा क्विल", "Maren = मैरेन", "Tobias = टोबायस",
                   "twenty years later", "reason: age", "apple crate", "A cobbled quay",
                   "default light: low orange sun", "chapters: 5"):
        assert needle in text, needle


def test_to_review_only_when_needed(book):
    run(["--chapters", "1-3"])
    text = (book / "gemini" / "B001-003" / "sources" / "bible.txt").read_text(encoding="utf-8")
    assert "TO REVIEW" in text
    review = text.split("TO REVIEW")[1]
    assert "mara_quill" in review and "holding a deck of cards" in review and "dark oilskin coat" in review
    assert "joss_hale" not in review
    prompt = prompt_of(book, "B001-003")
    assert "REVIEW TASK" in prompt and '"type":"revise"' in prompt
    assert LEFTOVER.findall(prompt) == []

    chars = B.load_characters(book / "bible")
    chars["characters"]["mara_quill"].pop("legacy")
    chars["characters"]["mara_quill"]["look"] = "slim woman with silver hair and grey eyes"
    chars["characters"]["mara_quill"]["variants"]["base"]["ref_version"] = 2
    B.save_characters(book / "bible", chars)
    run(["--chapters", "1-3"])
    text = (book / "gemini" / "B001-003" / "sources" / "bible.txt").read_text(encoding="utf-8")
    assert "TO REVIEW" not in text
    assert "REVIEW TASK" not in prompt_of(book, "B001-003")


def test_revised_character_leaves_the_review_list(book):
    chars = B.load_characters(book / "bible")
    chars["characters"]["mara_quill"]["legacy"]["revisions"] = [{"look": "old", "props": []}]
    B.save_characters(book / "bible", chars)
    run(["--chapters", "1-3"])
    text = (book / "gemini" / "B001-003" / "sources" / "bible.txt").read_text(encoding="utf-8")
    assert "TO REVIEW" not in text


def test_empty_bible_says_none_yet(book):
    shutil.rmtree(book / "bible")
    run(["--chapters", "1-3"])
    text = (book / "gemini" / "B001-003" / "sources" / "bible.txt").read_text(encoding="utf-8")
    assert text.count("(none yet)") == 3 and "TO REVIEW" not in text


# ---------------------------------------------------------------- windows

def test_windows_match_the_existing_window_code(book):
    run(["--chapters", "4-5"])
    folder = book / "gemini" / "B004-005"
    meta = json.loads((folder / "batch.json").read_text(encoding="utf-8"))
    wtxt = (folder / "sources" / "windows-4-5.txt").read_text(encoding="utf-8")
    ep = book / "episodes" / "E001"
    assert meta["params"] == {"paced_seconds": S.PACED_SECONDS, "max_seconds": S.MAX_SECONDS,
                              "chars_per_second": S.CHARS_PER_SECOND, "pacing": S.PACING}
    total = 0
    for n in (4, 5):
        paras = S.chapter_paras(ep, n)
        wins = S.chapter_windows(paras, "7s")           # what check_chapter calls for pacing "7s"
        assert wins == S.paced_windows(paras)
        assert meta["windows"][str(n)] == len(wins) > 0
        assert meta["hindi_sha256"][str(n)] == E.sha256(ep / "hindi" / f"{n:05d}.txt")
        section = wtxt.split(f"##### CHAPTER {n}: {len(wins)} WINDOWS #####")[1].split("##### CHAPTER")[0]
        found = re.findall(r"^\[w (\d+)\] (.*)$", section, re.M)
        assert [int(k) for k, _ in found] == list(range(1, len(wins) + 1))
        # joined window texts == the narrated text (title paragraph excluded), whitespace-normalised
        joined = " ".join(t for _, t in found)
        assert " ".join(joined.split()) == " ".join(" ".join(paras[1:]).split())
        total += len(wins)
    assert total >= 4
    assert f"[w 1] " in wtxt and "\n\n[w 2] " in wtxt     # one paragraph per window
    # chapter 4 has a long paragraph: it must be cut into several windows
    assert meta["windows"]["4"] > 2


# ---------------------------------------------------------------- re-run

def test_no_out_warning_without_out(book, capsys):
    run(["--chapters", "1-2"])
    assert "WARNING" not in capsys.readouterr().out


def test_rerun_keeps_raw_and_progress_and_overwrites_derived(book):
    run(["--chapters", "1-3"])
    folder = book / "gemini" / "B001-003"
    (folder / "raw").mkdir()
    (folder / "raw" / "reply1.txt").write_text("keep me", encoding="utf-8")
    (folder / "progress.json").write_text('{"done": [1]}', encoding="utf-8")
    (folder / "prompt.txt").write_text("stale", encoding="utf-8")
    assert run(["--chapters", "1-3"]) == 0
    assert (folder / "raw" / "reply1.txt").read_text(encoding="utf-8") == "keep me"
    assert (folder / "progress.json").read_text(encoding="utf-8") == '{"done": [1]}'
    assert (folder / "prompt.txt").read_text(encoding="utf-8") != "stale"


def test_out_dir_replaces_the_batch_folder(book, tmp_path, capsys):
    out = tmp_path / "elsewhere"
    assert run(["--chapters", "1-2", "--out", str(out)]) == 0
    said = capsys.readouterr().out
    assert "WARNING: --out" in said and "novel import" in said and "will not find" in said
    assert (out / "prompt.txt").is_file() and (out / "sources" / "chapters-1-2.txt").is_file()
    assert not (book / "gemini").exists()


# ---------------------------------------------------------------- clipboard

def _clipboard_ok():
    return sys.platform == "win32" and prompt_cmd.set_clipboard("probe") and prompt_cmd.get_clipboard() == "probe"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows clipboard")
def test_copy_round_trip_with_devanagari(book, capsys):
    if not _clipboard_ok():
        pytest.skip("clipboard not available")
    assert run(["--chapters", "1-3", "--copy"]) == 0
    text = (book / "gemini" / "B001-003" / "prompt.txt").read_text(encoding="utf-8")
    assert "तो" in text
    assert prompt_cmd.get_clipboard() == text
    out = capsys.readouterr().out
    assert "on the clipboard" in out and "replaced" in out


def test_copy_without_clipboard_still_exits_zero(book, monkeypatch, capsys):
    monkeypatch.setattr(prompt_cmd, "set_clipboard", lambda text: False)
    assert run(["--chapters", "1-3", "--copy"]) == 0
    out = capsys.readouterr().out
    assert "not available" in out and str(book / "gemini" / "B001-003" / "prompt.txt") in out


# ---------------------------------------------------------------- build_rules framing (wide / tight)

RULES = dict(channel="Manga and Novel", title="The Harbour Tales", first=4, last=5, paced=7, chars_per_second=15,
             max_characters=4, chapter=4, window_count=9)

TIGHT_TEXTS = (
    'The camera is CLOSE in this series: one person per picture is best, two only when the moment is between those two',
    'where that person is in the FRAME only, in plain words: "left", "centre" or "right"',
    '"medium" is one or two people from the waist up and is the normal shot.',
    'Name the spot inside the place where this picture happens (by the oven, at the doorway, beside the fallen pillar)',
    '"detail" is an OBJECT ALONE: its cast is empty [], and no person and no hand is in the picture; when a person '
    'holds or uses the object, use "medium" instead.',
)


def flat(text):
    return " ".join(text.split())


def test_wide_framing_is_the_default_and_unchanged(book):
    default = P.build_rules("images", **RULES)
    assert P.build_rules("images", framing="wide", **RULES) == default
    # the wide rules are PART_B as it stands, with its example
    assert "Fewer people is better" in default and '"medium" for at most a third' in default
    assert '"shot":"wide"' in default and "beside the scale" not in default
    for text in TIGHT_TEXTS:
        assert flat(text) not in flat(default)
    assert LEFTOVER.findall(default) == []


def test_tight_framing_changes_only_the_four_bullets(book):
    wide, tight = P.build_rules("images", **RULES), P.build_rules("images", framing="tight", **RULES)
    for text in TIGHT_TEXTS:
        assert flat(text) in flat(tight), text
    assert '"medium" for at most a third' not in tight and "Fewer people is better" not in tight
    assert 'Never a crowd: anyone else goes in "others"' in flat(tight)
    assert "ONLY for a window with an empty cast, or for the first image of a group" in flat(tight)
    assert '"detail" is a hand, an object or a face' not in flat(tight)
    # the schema lines and the placeholders are the same as in the wide rules
    schema = [l for l in P.PART_B.splitlines() if l.lstrip().startswith(("{", '"cast"', '"others"'))]
    assert schema and all(l in P.PART_B_TIGHT.splitlines() for l in schema)
    assert LEFTOVER.findall(tight) == []
    assert "at most 4" in tight and "never the words nude" in flat(tight)
    assert all(len(l) <= 120 for l in P.PART_B_TIGHT.splitlines())
    assert wide != tight and len(P.PART_B_TIGHT.splitlines()) >= len(P.PART_B.splitlines())


def test_tight_example_is_valid_and_close(book):
    tight = P.build_rules("images", framing="tight", **RULES)
    assert '"shot":"medium"' in tight and '"shot":"wide"' not in tight
    rows = [json.loads(l) for l in tight.split("EXAMPLE (format only")[1].splitlines() if l.startswith("{")]
    group, image = rows
    assert image["shot"] == "medium" and [c["position"] for c in image["cast"]] == ["left", "right"]
    assert 150 <= len(image["action"]) <= 450 and len(image["cast"]) == 2
    assert all(c["key"] in group["present"] for c in image["cast"])
    assert group == P.EXAMPLE_GROUP


@pytest.mark.parametrize("part", ["translate", "bible"])
def test_framing_only_affects_the_images_part(book, part):
    assert P.build_rules(part, framing="tight", **RULES) == P.build_rules(part, **RULES)


def test_unknown_framing_is_an_error(book):
    with pytest.raises(P.PromptError, match="framing"):
        P.build_rules("images", framing="medium", **RULES)
    with pytest.raises(P.PromptError):
        P.build_rules("translate", framing="", **RULES)
