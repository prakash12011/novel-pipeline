import json
import sys
from pathlib import Path

import pytest
import yaml

from novel.commands import meta_cmd, published_cmd, thumb_cmd
from novel.publish import meta, research, validate

FAKE = Path(__file__).parent / "fakes" / "fake_claude.py"

# The real COI templates from book.yaml (the channel's own text, not novel text).
TITLE_TPL = "Circle of Inevitability Explained in Hindi | Episode [N] (Ch [X]-[Y]) | Lord of the Mysteries 2"
DESC_TPL = """\
Lord of the Mysteries 2 – Circle of Inevitability ki kahani Hindi mein! 🎧
Episode [N] mein Chapter [X] se [Y] tak sab kuch explain kiya gaya hai: [one-line spoiler-light teaser in our own words].

⏱️ Chapters
00:00 Intro
[mm:ss or h:mm:ss] Chapter [X]
... one line per chapter up to Chapter [Y]

📚 Puri series (playlist): [playlist link]
◀️ Previous: Episode [N-1] (Chapter [X-10]-[Y-10])      <- leave this line out for Episode 1
▶️ Next: Episode [N+1] (Chapter [Y+1]-[Y+10])

🔔 Har episode ke liye channel ko SUBSCRIBE karo aur bell dabao.
[one short comment question about this episode, e.g. "Comment mein batao: aapko kya lagta hai, aage kya hoga?"]

Novel: Lord of the Mysteries 2: Circle of Inevitability, by Cuttlefish That Loves Diving. Yeh video novel ka Hindi explanation hai. Original novel padh ke author ko support karo.

#CircleOfInevitability #LOTM2 #LordOfTheMysteriesHindi"""
TAGS = ["circle of inevitability explained in hindi", "circle of inevitability hindi",
        "lotm circle of inevitability explained in hindi", "lord of the mysteries 2 explained in hindi",
        "circle of inevitability audiobook in hindi", "circle of inevitability volume [V] explained in hindi",
        "lotm explained in hindi season 2", "lord of the mysteries hindi", "lotm 2 hindi", "lumian lee"]
HASHTAGS = ["#CircleOfInevitability", "#LOTM2", "#LordOfTheMysteriesHindi"]
PLAYLIST = "https://www.youtube.com/playlist?list=PLinvented"

TITLES = ["Quiet Harbor", "Lantern Debt", "Salt Road", "Broken Tide", "Open Gate",
          "Iron Orchard", "Hollow Bell", "Pale Winter", "Last Ferry", "Third Door"]


def chapter_text(n):
    return (f"Chapter {n}: {TITLES[n - 11]}\n\n"
            + " ".join(f"Marin{n} carried a lamp{n} past stall{i} while the gulls{i} wheeled over harbour{i}."
                       for i in range(1, 60)) + "\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.setenv("NOVEL_CLAUDE_CMD", f'"{sys.executable}" "{FAKE}"')
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    monkeypatch.delenv("FAKE_CLAUDE_REPLIES", raising=False)
    (home / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    book = data / "coi"
    ep = book / "episodes" / "E002"
    (ep / "chapters").mkdir(parents=True)
    book_yaml = {
        "book": {"title": "Circle of Inevitability", "series_label": "Circle of Inevitability",
                 "volumes": [[1, 500, 1], [501, 1000, 2], [1001, 1180, 3]]},
        "publish": {"title_template": TITLE_TPL, "description_template": DESC_TPL, "tags": TAGS,
                    "hashtags": HASHTAGS, "playlist_url": PLAYLIST},
    }
    (book / "book.yaml").write_text(yaml.safe_dump(book_yaml, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (ep / "episode.json").write_text(json.dumps({"episode": 2, "start": 11, "end": 20}), encoding="utf-8")
    (ep / "episode.yaml").write_text("published: false\n", encoding="utf-8")
    for n in range(11, 21):
        (ep / "chapters" / f"{n:04d}_{TITLES[n - 11].lower().replace(' ', '-')}.txt").write_text(
            chapter_text(n), encoding="utf-8")
    # E001 published (url known), E003 not
    e1 = book / "episodes" / "E001"
    e1.mkdir(parents=True)
    (e1 / "episode.yaml").write_text("published: true\npublished_url: https://youtu.be/AAAAAAAAAAA\n", encoding="utf-8")
    (book / "episodes" / "E003").mkdir()
    (book / "episodes" / "E003" / "episode.yaml").write_text("published: false\n", encoding="utf-8")
    write_json_stamps(ep)
    return type("Env", (), {"tmp": tmp_path, "book": book, "ep": ep, "book_yaml": book_yaml,
                            "calls": tmp_path / "claude.log"})


def write_json_stamps(ep, first=22.0, duration=2700.0, n=10, step=270.0):
    rows = [{"chapter": 11 + i, "title": TITLES[i], "start": first + i * step} for i in range(n)]
    (ep / "E002.timestamps.json").write_text(
        json.dumps({"episode": 2, "duration": duration, "chapters": rows, "cards": []}), encoding="utf-8")


def set_book(env, **publish):
    env.book_yaml["publish"].update(publish)
    (env.book / "book.yaml").write_text(yaml.safe_dump(env.book_yaml, allow_unicode=True, sort_keys=False),
                                        encoding="utf-8")


def run(*args):
    return meta_cmd.main(["--book", "coi", "--ep", "2", *args])


def load(env):
    return json.loads((env.ep / "publish" / "publish.json").read_text(encoding="utf-8"))


def replies(env, monkeypatch, *specs):
    f = env.tmp / "replies.json"
    f.write_text(json.dumps(list(specs)), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_REPLIES", str(f))


def ncalls(env):
    return len(env.calls.read_text(encoding="utf-8").splitlines()) if env.calls.exists() else 0


GOOD = {"teaser": "Ek chhote bandargah ki raat mein kuch badal raha hai. Hero ko ek naya sawaal milta hai.",
        "comment_question": "Comment mein batao: aapko kya lagta hai?",
        "thumbnail_hooks": ["NIGHT AT|THE *HARBOR*", "WHO IS|*MARIN?*", "ONE LAST|*WARNING*"],
        "thumbnail_character": "Marin"}


# ------------------------------------------------------------------ templates

def test_fills_every_placeholder_and_note(env, capsys):
    assert run() == 0
    p = load(env)
    assert p["title"] == "Circle of Inevitability Explained in Hindi | Episode 2 (Ch 11-20) | Lord of the Mysteries 2"
    d = p["description"]
    assert validate.find_placeholders(d) == [] and validate.find_placeholders(p["title"]) == []
    assert "Episode 2 mein Chapter 11 se 20 tak" in d                       # [N] [X] [Y]
    assert "<-" not in d and "leave this line out" not in d                 # note removed
    assert "one line per chapter" not in d and "mm:ss" not in d             # stamp-block notes removed
    assert f"Puri series (playlist): {PLAYLIST}" in d                        # [playlist link]
    assert "◀️ Previous: Episode 1 (Chapter 1-10): https://youtu.be/AAAAAAAAAAA" in d   # [N-1] [X-10] [Y-10]
    assert "Next:" not in d                                                  # E003 url unknown
    assert p["teaser"] in d and p["comment_question"] in d                   # teaser, comment question
    assert p["tags"][5] == "circle of inevitability volume 1 explained in hindi"   # [V]
    assert d.rstrip().endswith("#LordOfTheMysteriesHindi")
    assert p["valid"] is True and p["errors"] == []


def test_every_token_in_the_real_templates_is_handled():
    tokens = set(validate.find_placeholders(DESC_TPL + TITLE_TPL + " ".join(TAGS)))
    # every bracket/angle token of the real templates, so a new one in book.yaml fails here
    assert "[N]" in tokens and "[V]" in tokens and "[playlist link]" in tokens and "[N+1]" in tokens
    vals = {"N": 2, "X": 11, "Y": 20, "V": 1}
    out = meta.fill_description(DESC_TPL, vals, teaser="T", question="Q", stamp_block=["0:00 A", "1:00 B", "2:00 C"],
                                playlist_url="https://p", prev_url="https://a", next_url="https://b", ep_number=2)
    assert validate.find_placeholders(out) == []
    assert "Next: Episode 3 (Chapter 21-30): https://b" in out
    assert "gaya hai: T.\n" in out                # the template's own full stop is kept once
    out2 = meta.fill_description(DESC_TPL, vals, teaser="Wow!", question="Q", stamp_block=["0:00 A"],
                                 playlist_url=None, prev_url=None, next_url=None, ep_number=2)
    assert "gaya hai: Wow!\n" in out2 and ".." not in out2
    assert "Previous: Episode 1 (Chapter 1-10): https://a" in out


def test_episode_one_has_no_previous_line(env):
    e1 = env.book / "episodes" / "E001"
    (e1 / "episode.json").write_text(json.dumps({"episode": 1, "start": 1, "end": 10}), encoding="utf-8")
    d = meta.fill_description(DESC_TPL, {"N": 1, "X": 1, "Y": 10, "V": 1}, teaser="T", question="Q",
                              stamp_block=["0:00 A", "1:00 B", "2:00 C"], playlist_url=None,
                              prev_url="https://x", next_url=None, ep_number=1)
    assert "Previous" not in d and "<-" not in d and "[" not in d


def test_playlist_line_dropped_when_unset(env):
    set_book(env, playlist_url=None)
    assert run() == 0
    p = load(env)
    assert "playlist" not in p["description"].lower() and "Puri series" not in p["description"]
    assert any("playlist_url" in w for w in p["warnings"])
    assert p["valid"]


def test_next_link_present_when_known(env):
    (env.book / "episodes" / "E003" / "episode.yaml").write_text(
        "published: true\npublished_url: https://youtu.be/CCCCCCCCCCC\n", encoding="utf-8")
    assert run() == 0
    assert "▶️ Next: Episode 3 (Chapter 21-30): https://youtu.be/CCCCCCCCCCC" in load(env)["description"]


def test_previous_omitted_when_unknown(env):
    (env.book / "episodes" / "E001" / "episode.yaml").write_text("published: false\n", encoding="utf-8")
    assert run() == 0
    assert "Previous" not in load(env)["description"]


def test_no_timestamps_drops_header_and_fails_validation(env):
    (env.ep / "E002.timestamps.json").unlink()
    code = run()
    p = load(env)
    assert code == 1 and p["valid"] is False
    assert "Chapters\n" not in p["description"] and "⏱" not in p["description"]
    assert any("timestamps" in e for e in p["errors"])
    assert any("no E002.timestamps" in w for w in p["warnings"])


# ------------------------------------------------------------------ timestamps and volume

def test_stamps_from_json_with_intro(env):
    assert run() == 0
    d = load(env)["description"]
    lines = [ln for ln in d.splitlines() if ln[:1].isdigit()]
    assert lines[0] == "0:00 Intro" and lines[1] == "0:22 Chapter 11" and lines[2] == "4:52 Chapter 12"
    assert len(lines) == 11 and lines[-1].endswith("Chapter 20")
    assert load(env)["timestamp_source"] == "E002.timestamps.json"


def test_first_chapter_at_zero_when_no_cards():
    stamps, warns = meta.build_stamps([(11, 4.0), (12, 300.0), (13, 600.0)], 900, "json")
    assert stamps == [(0, "Chapter 11"), (300, "Chapter 12"), (600, "Chapter 13")] and warns == []


def test_intro_threshold_is_ten_seconds():
    assert meta.build_stamps([(1, 10.0), (2, 100.0)], 200, "json")[0][0] == (0, "Intro")
    assert meta.build_stamps([(1, 9.9), (2, 100.0)], 200, "json")[0][0] == (0, "Chapter 1")


def test_ten_second_rule_drops_with_warning():
    stamps, warns = meta.build_stamps([(1, 0.0), (2, 6.0), (3, 100.0)], 200, "json")
    assert stamps == [(0, "Chapter 1"), (100, "Chapter 3")]
    assert len(warns) == 1 and "Chapter 2" in warns[0]


def test_hour_format_for_every_stamp():
    lines = meta.stamp_lines([(0, "Intro"), (20, "Chapter 1"), (3700, "Chapter 2")], 4000)
    assert lines == ["0:00:00 Intro", "0:00:20 Chapter 1", "1:01:40 Chapter 2"]
    assert meta.stamp_lines([(0, "A"), (125, "B")], 600) == ["0:00 A", "2:05 B"]
    assert meta.stamp_lines([(0, "A"), (3601, "B")], None)[1] == "1:00:01 B"   # no duration: last stamp decides


def test_hour_format_in_description(env):
    write_json_stamps(env.ep, first=22.0, duration=4000.0, step=380.0)
    assert run() == 0
    d = load(env)["description"]
    assert "0:00:00 Intro" in d and "0:00:22 Chapter 11" in d


def test_stamps_from_text_fallback(env):
    (env.ep / "E002.timestamps.json").unlink()
    (env.ep / "E002.timestamps.txt").write_text(
        "\n".join(f"{(i * 270) // 60}:{(i * 270) % 60:02d} Chapter {11 + i}: {TITLES[i]}" for i in range(10)) + "\n",
        encoding="utf-8")
    assert run() == 0
    p = load(env)
    labels = [s["label"] for s in p["timestamps"]]
    assert labels[0] == "Chapter 11" and "Intro" not in labels            # no Intro from a txt file
    assert p["description"].count("Chapter 1") >= 10 and "Quiet Harbor" not in p["description"]
    assert any("older text file" in w for w in p["warnings"])
    assert p["timestamp_source"] == "E002.timestamps.txt"


def test_variant_file_name_found(env):
    (env.ep / "E002.timestamps.json").rename(env.ep / "E002-c11-20.timestamps.json")
    assert run() == 0 and load(env)["timestamp_source"] == "E002-c11-20.timestamps.json"


def test_volume_lookup():
    vols = [[1, 500, 1], [501, 1000, 2], [1001, 1180, 3]]
    assert meta.volume_for(vols, 11) == (1, True)
    assert meta.volume_for(vols, 501) == (2, True)
    assert meta.volume_for(vols, 1180) == (3, True)
    assert meta.volume_for(vols, 5000) == (1, False)
    assert meta.volume_for([], 3) == (1, False)


def test_volume_in_tags_and_crossing_note(env):
    env.book_yaml["book"]["volumes"] = [[1, 15, 1], [16, 40, 2]]
    set_book(env)
    assert run() == 0
    p = load(env)
    assert "circle of inevitability volume 1 explained in hindi" in p["tags"]
    assert any("cross volumes" in w for w in p["warnings"])


# ------------------------------------------------------------------ AI

def test_ai_happy_path_with_fake_cli(env):
    assert run() == 0
    p = load(env)
    assert p["ai"] == "claude" and p["ai_note"] is None
    assert len(p["thumbnail_hooks"]) == 3 and p["valid"]
    assert ncalls(env) == 1
    assert p["ai_context"]["chapters"] == 10 and p["ai_context"]["chars_sent"] < 10 * 2100


def test_prompt_has_rules_chapters_and_research(env, monkeypatch):
    seen = {}

    def fake_call(prompt, timeout=180):
        seen["p"] = prompt
        return json.dumps(GOOD)
    monkeypatch.setattr(meta, "call_claude", fake_call)
    rdir = env.book / "research"
    research.save(rdir, {"fetched_at": meta.now_utc().strftime(research.TIME_FMT),
                         "phrases": [{"text": "invented phrase one", "count": 9, "seeds": ["x"]}]}, meta.now_utc())
    assert run() == 0
    p, snap = seen["p"], load(env)["research_snapshot"]
    assert "NEVER quote or paraphrase" in p and "invented phrase one" in p and "Chapter 15" in p
    assert "Quiet Harbor" not in p                                  # novel chapter titles are not sent
    assert "characters in total" in p
    assert snap["top_phrases"][0]["text"] == "invented phrase one"


def test_default_command_line(monkeypatch):
    monkeypatch.delenv("NOVEL_CLAUDE_CMD", raising=False)
    monkeypatch.setattr(meta.shutil, "which", lambda n: "C:/bin/claude.exe")
    cmd = meta.claude_command()
    assert cmd[0] == "C:/bin/claude.exe"
    assert cmd[1:7] == ["-p", "--model", "sonnet", "--output-format", "json", "--tools"] and cmd[7] == ""
    monkeypatch.setattr(meta.shutil, "which", lambda n: None)
    with pytest.raises(meta.ClaudeUnavailable):
        meta.claude_command()


def test_fenced_json_is_accepted(env, monkeypatch):
    replies(env, monkeypatch, {"kind": "fence", "payload": GOOD})
    assert run() == 0 and load(env)["ai"] == "claude"


def test_envelope_parsing():
    assert meta.parse_envelope(json.dumps({"result": "hi"})) == "hi"
    assert meta.parse_envelope(json.dumps([{"type": "system"}, {"type": "result", "result": "yo"}])) == "yo"
    for bad in ("not json", json.dumps({"is_error": True, "result": "x"}), json.dumps({"result": ""})):
        with pytest.raises(meta.AIFailure):
            meta.parse_envelope(bad)


def stub_gemini(monkeypatch, payload=GOOD, calls=None):
    def fake(system, prompt, *, model, log):
        if calls is not None:
            calls.append(prompt)
        return json.dumps(payload)
    monkeypatch.setattr(meta, "call_gemini", fake)


def test_cli_missing_falls_back_to_gemini(env, monkeypatch):
    monkeypatch.setenv("NOVEL_CLAUDE_CMD", str(env.tmp / "no-such-claude.exe"))
    calls = []
    stub_gemini(monkeypatch, calls=calls)
    assert run() == 0
    p = load(env)
    assert p["ai"] == "gemini" and "claude" in p["ai_note"] and len(calls) == 1 and p["valid"]


def test_unparsable_cli_output_retried_then_gemini(env, monkeypatch):
    replies(env, monkeypatch, {"kind": "garbage"}, {"kind": "model_garbage"})
    calls = []
    stub_gemini(monkeypatch, calls=calls)
    assert run() == 0
    p = load(env)
    assert ncalls(env) == 2 and p["ai"] == "gemini" and len(calls) == 1 and p["ai_note"]


def test_cli_failure_then_success_stays_claude(env, monkeypatch):
    replies(env, monkeypatch, {"kind": "exit1"}, {"kind": "ok", "payload": GOOD})
    assert run() == 0 and load(env)["ai"] == "claude" and ncalls(env) == 2


def test_ai_gemini_goes_straight_there(env, monkeypatch):
    stub_gemini(monkeypatch)
    assert run("--ai", "gemini") == 0
    assert load(env)["ai"] == "gemini" and ncalls(env) == 0


def test_all_ai_failing_gives_clear_error(env, monkeypatch, capsys):
    monkeypatch.setenv("NOVEL_CLAUDE_CMD", str(env.tmp / "nope"))

    def boom(*a, **k):
        raise meta.AIFailure("Gemini: no key")
    monkeypatch.setattr(meta, "call_gemini", boom)
    assert run() == 1
    assert "--ai none" in capsys.readouterr().err


def quote_six_words(env):
    words = chapter_text(13).split("\n\n", 1)[1].split()
    return " ".join(words[3:9])


def test_quoted_teaser_retries_once_then_invalid(env, monkeypatch, capsys):
    bad = dict(GOOD, teaser=f"Isme aata hai: {quote_six_words(env)} aur phir kuch.")
    replies(env, monkeypatch, {"kind": "ok", "payload": bad}, {"kind": "ok", "payload": bad})
    assert run() == 1
    p = load(env)
    assert p["valid"] is False and any(e.startswith("teaser") and "consecutive" in e for e in p["errors"])
    assert ncalls(env) == 2
    log = [json.loads(ln) for ln in env.calls.read_text().splitlines()]
    assert log[1]["retry"] is True and log[0]["retry"] is False
    assert "NOT VALID" in (env.ep / "publish" / "publish.txt").read_text(encoding="utf-8")
    assert "NOT VALID" in capsys.readouterr().err


def test_quoted_teaser_fixed_on_retry(env, monkeypatch):
    bad = dict(GOOD, teaser=f"Isme aata hai: {quote_six_words(env)} aur phir kuch.")
    replies(env, monkeypatch, {"kind": "ok", "payload": bad}, {"kind": "ok", "payload": GOOD})
    assert run() == 0 and load(env)["valid"] and ncalls(env) == 2


def test_bad_hooks_trigger_retry(env, monkeypatch):
    bad = dict(GOOD, thumbnail_hooks=["lower|*x*", "A B|*C*", "D E|*F*"])
    replies(env, monkeypatch, {"kind": "ok", "payload": bad}, {"kind": "ok", "payload": GOOD})
    assert run() == 0 and ncalls(env) == 2


def test_ai_none(env, capsys):
    assert run("--ai", "none") == 1                     # --teaser is required
    assert "--teaser" in capsys.readouterr().err
    assert run("--ai", "none", "--teaser", "Ek chhoti si kahani. Aage kya hoga.") == 0
    p = load(env)
    assert p["ai"] == "none" and p["thumbnail_hooks"] == [] and p["valid"]
    assert p["teaser"].startswith("Ek chhoti") and p["comment_question"] == meta.GENERIC_QUESTION
    assert ncalls(env) == 0 and any("--ai none" in w for w in p["warnings"])


def test_settings_default_ai_key():
    from novel.core import settings
    assert settings.get("publish.ai", default=None) == "claude"


# ------------------------------------------------------------------ outputs

def test_outputs_written_and_previous_pair_archived(env):
    assert run() == 0
    first = (env.ep / "publish" / "publish.json").read_text(encoding="utf-8")
    assert run() == 0
    pub = env.ep / "publish"
    old = list((pub / "old").iterdir())
    assert len(old) == 1 and (old[0] / "publish.json").read_text(encoding="utf-8") == first
    assert (old[0] / "publish.txt").is_file() and (pub / "publish.txt").is_file()
    assert run() == 0
    assert len(list((pub / "old").iterdir())) == 2


def test_publish_json_keys_and_txt_blocks(env):
    assert run() == 0
    p = load(env)
    for k in ("title", "description", "tags", "hashtags", "timestamps", "teaser", "comment_question",
              "thumbnail_hooks", "ai", "research_snapshot", "valid", "errors", "warnings", "generated_at"):
        assert k in p
    t = (env.ep / "publish" / "publish.txt").read_text(encoding="utf-8")
    order = [t.index(x) for x in ("1. TITLE", "2. DESCRIPTION", "3. TAGS", "4. STUDIO SETTINGS", "5. THUMBNAIL HOOKS")]
    assert order == sorted(order)
    assert ", ".join(p["tags"]) in t and "Category: Entertainment" in t and "Next: novel thumb" in t
    assert "Paid promotion / altered content: No / No" in t


def test_hooks_readable_by_thumb_loader(env):
    assert run() == 0
    hooks = thumb_cmd._hooks_from_file(env.ep)
    assert hooks == load(env)["thumbnail_hooks"] and len(hooks) == 3
    assert all(isinstance(h, str) and "|" in h and h.count("*") == 2 for h in hooks)


def test_research_runs_when_no_cache_and_offline_is_tolerated(env, monkeypatch):
    set_book(env, research_seeds=["circle of inevitability"])

    def offline(url):
        raise OSError("offline")
    assert meta_cmd.main(["--book", "coi", "--ep", "2"], opener=offline, pause=0) == 0
    p = load(env)
    assert p["research_snapshot"] is None and any("research" in w for w in p["warnings"])


def test_research_fetched_through_opener(env):
    set_book(env, research_seeds=["circle of inevitability"])

    def opener(url):
        return json.dumps(["x", ["circle of inevitability explained in hindi"]]).encode()
    assert meta_cmd.main(["--book", "coi", "--ep", "2"], opener=opener, pause=0) == 0
    snap = load(env)["research_snapshot"]
    assert snap["top_phrases"][0]["text"] == "circle of inevitability explained in hindi"
    assert list((env.book / "research").glob("autocomplete-*.json"))


# ------------------------------------------------------------------ novel published

YAML_BEFORE = "# Settings for THIS episode only.\npublished: false      # set to true after upload\n\ntts:\n  voice: Anirban   # keep\n"


def pub(env, url, ep=2):
    return published_cmd.main(["--book", "coi", "--ep", str(ep), "--url", url])


def test_published_keeps_comments_and_keys(env):
    f = env.ep / "episode.yaml"
    f.write_text(YAML_BEFORE, encoding="utf-8")
    assert pub(env, "https://youtu.be/dQw4w9WgXcQ") == 0
    new = f.read_text(encoding="utf-8")
    assert new.startswith("# Settings for THIS episode only.\npublished: true      # set to true after upload\n")
    assert "tts:\n  voice: Anirban   # keep\n" in new
    data = yaml.safe_load(new)
    assert data["published"] is True and data["published_url"] == "https://youtu.be/dQw4w9WgXcQ"
    # again: replaces the url, does not duplicate
    assert pub(env, "https://www.youtube.com/watch?v=abcdefghijk&t=5") == 0
    new = f.read_text(encoding="utf-8")
    assert new.count("published_url") == 1 and yaml.safe_load(new)["published_url"].endswith("abcdefghijk&t=5")


def test_published_adds_keys_when_absent_and_creates_file(env):
    f = env.ep / "episode.yaml"
    f.write_text("tts: {voice: X}", encoding="utf-8")        # no trailing newline, no keys
    assert pub(env, "https://youtu.be/dQw4w9WgXcQ") == 0
    assert yaml.safe_load(f.read_text(encoding="utf-8")) == {
        "tts": {"voice": "X"}, "published": True, "published_url": "https://youtu.be/dQw4w9WgXcQ"}
    f.unlink()
    assert pub(env, "https://youtu.be/dQw4w9WgXcQ") == 0 and f.is_file()


def test_published_keeps_crlf(env):
    f = env.ep / "episode.yaml"
    f.write_bytes(b"published: false\r\nx: 1\r\n")
    assert pub(env, "https://youtu.be/dQw4w9WgXcQ") == 0
    raw = f.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


@pytest.mark.parametrize("url", ["https://example.com/watch?v=dQw4w9WgXcQ", "not a url", "https://youtu.be/short",
                                 "https://youtube.com/", "http://notyoutube.com/youtu.be/dQw4w9WgXcQ"])
def test_published_rejects_non_youtube(env, url, capsys):
    before = (env.ep / "episode.yaml").read_text(encoding="utf-8")
    assert pub(env, url) == 1
    assert (env.ep / "episode.yaml").read_text(encoding="utf-8") == before
    assert "YouTube" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["https://youtu.be/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                                 "https://youtube.com/shorts/dQw4w9WgXcQ", "https://m.youtube.com/watch?feature=x&v=dQw4w9WgXcQ"])
def test_published_accepts_youtube(url):
    assert published_cmd.is_youtube_url(url)


def test_published_prints_next_step(env, capsys):
    assert pub(env, "https://youtu.be/dQw4w9WgXcQ") == 0
    assert "novel episode add" in capsys.readouterr().out
