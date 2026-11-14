import json

import pytest
import yaml

from novel.commands import episode_cmd, new_cmd
from novel.core import context

CHAPTER_KEYS = ["idx", "title", "source", "status", "hindi", "audio", "timing", "seconds", "error",
                "attempts", "translate_seconds", "tts_seconds", "rtf"]   # as in the real COI E003
EPISODE_KEYS = ["episode", "start", "end", "created", "flow_project", "voice", "translate_model"]


@pytest.fixture
def book(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    (home / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    monkeypatch.setattr(context, "_ACTIVE_BOOK", None)
    assert new_cmd.main(["--yes", "--title", "Fixture Book", "--slug", "fx", "--chapters-per-episode", "10"]) == 0
    chap = data / "fx" / "chapters"
    for n in range(1, 26):
        (chap / f"{n:04d}_title-{n}.txt").write_text(f"Chapter {n}: Title {n}\n\nInvented body {n}.\n",
                                                     encoding="utf-8")
    return data / "fx"


def add(*args):
    return episode_cmd.main(["add", "--book", "fx", *args])


def folders(book):
    return sorted(p.name for p in (book / "episodes").iterdir())


def test_three_adds_then_short_episode(book, capsys):
    assert add() == 0
    assert add() == 0
    out = capsys.readouterr().out
    assert "chapters 1-10" in out and "chapters 11-20" in out
    assert "novel prompt --chapters 11-20 --copy" in out
    assert add() == 1
    err = capsys.readouterr().err
    assert "only 5 chapter(s) are left (21-25)" in err and "--chapters 21-25" in err
    assert folders(book) == ["E001", "E002"]
    assert add("--chapters", "21-25") == 0
    assert folders(book) == ["E001", "E002", "E003"]
    assert len(list((book / "episodes" / "E003" / "chapters").glob("*.txt"))) == 5
    assert add() == 1
    assert "no chapters are left" in capsys.readouterr().err
    assert context.read_state() == {"book": "fx", "episode": 3}


def test_nothing_extracted(book, capsys):
    for f in (book / "chapters").glob("*.txt"):
        f.unlink()
    assert add() == 1
    assert "novel extract" in capsys.readouterr().err


def test_overlap_is_error_gap_is_warning(book, capsys):
    assert add() == 0
    capsys.readouterr()
    assert add("--chapters", "8-14") == 1
    assert "overlap episode 1" in capsys.readouterr().err
    assert add("--chapters", "1-10", "--ep", "5") == 1
    assert "already episode 1" in capsys.readouterr().err
    assert folders(book) == ["E001"]
    assert add("--chapters", "15-19") == 0
    cap = capsys.readouterr()
    assert "warning: chapters 11-14 are not in any episode" in cap.out
    assert folders(book) == ["E001", "E002"]


def test_ep_override_and_never_overwrite(book, capsys):
    assert add("--ep", "4") == 0
    assert folders(book) == ["E004"]
    marker = book / "episodes" / "E004" / "episode.yaml"
    marker.write_text("published: true\n", encoding="utf-8")
    assert add("--ep", "4", "--chapters", "11-20") == 1
    assert "already exists" in capsys.readouterr().err
    assert marker.read_text(encoding="utf-8") == "published: true\n"
    assert add() == 0                      # next number is highest + 1
    assert folders(book) == ["E004", "E005"]
    assert add("--ep", "900") == 1         # extras numbers are refused


def test_extras_ignored_for_numbering(book):
    extra = book / "episodes" / "E900"
    extra.mkdir(parents=True)
    (extra / "episode.json").write_text(json.dumps({"episode": 900, "start": 1, "end": 999}), encoding="utf-8")
    assert add() == 0
    assert (book / "episodes" / "E001" / "manifest.json").exists()
    info = json.loads((book / "episodes" / "E001" / "episode.json").read_text(encoding="utf-8"))
    assert (info["start"], info["end"]) == (1, 10)


def test_missing_chapter_files_in_range(book, capsys):
    assert add("--chapters", "24-30") == 1
    assert "26-30 are not in" in capsys.readouterr().err
    assert add("--chapters", "abc") == 2


def test_shapes_and_files(book):
    assert add() == 0
    ep = book / "episodes" / "E001"
    man = json.loads((ep / "manifest.json").read_text(encoding="utf-8"))
    assert list(man) == ["version", "chapters"] and man["version"] == 1
    assert len(man["chapters"]) == 10
    first = man["chapters"][0]
    assert list(first) == CHAPTER_KEYS
    assert first["idx"] == 1 and first["source"] == "0001_title-1.txt"
    assert first["title"] == "Chapter 1: Title 1" and first["status"] == "pending"
    info = json.loads((ep / "episode.json").read_text(encoding="utf-8"))
    assert list(info) == EPISODE_KEYS
    assert (info["episode"], info["start"], info["end"]) == (1, 1, 10)
    assert info["voice"] == "Anirban" and info["translate_model"] == "gemini-3.5-flash-lite"
    y = yaml.safe_load((ep / "episode.yaml").read_text(encoding="utf-8"))
    assert y == {"published": False}
    assert "chapters:" in (ep / "episode.yaml").read_text(encoding="utf-8")
    assert (ep / "chapters" / "0010_title-10.txt").read_text(encoding="utf-8").startswith("Chapter 10")


def test_settings_flow_into_episode_json(book):
    with (book / "book.yaml").open("a", encoding="utf-8") as f:
        f.write("\nimages:\n  flow_project: https://flow.google.com/project/zz\ntts:\n  voice: Kavya\n")
    assert add() == 0
    info = json.loads((book / "episodes" / "E001" / "episode.json").read_text(encoding="utf-8"))
    assert info["flow_project"] == "https://flow.google.com/project/zz" and info["voice"] == "Kavya"


def test_list(book, capsys):
    assert episode_cmd.main(["list", "--book", "fx"]) == 0
    assert "no episodes yet" in capsys.readouterr().out
    assert add() == 0
    assert add() == 0
    ep1 = book / "episodes" / "E001"
    (ep1 / "hindi").mkdir()
    for n in (1, 2, 3):
        (ep1 / "hindi" / f"{n:05d}.txt").write_text("x", encoding="utf-8")
    (ep1 / "hindi.lock.json").write_text(json.dumps({"00001.txt": "a", "00002.txt": "b"}), encoding="utf-8")
    (ep1 / "audio").mkdir()
    (ep1 / "audio" / "00001.mp3").write_bytes(b"x")
    (ep1 / "episode.yaml").write_text("published: true\n", encoding="utf-8")
    (book / "episodes" / "E900").mkdir()
    capsys.readouterr()
    assert episode_cmd.main(["list", "--book", "fx"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "[fx]"
    assert lines[1].startswith("E001  ch 1-10") and "10 chapter files" in lines[1]
    assert "hindi 3  locked 2  audio 1  published yes" in lines[1]
    assert lines[2].startswith("E002  ch 11-20") and "hindi 0  locked 0  audio 0  published no" in lines[2]
    assert len(lines) == 3


def test_unknown_action():
    assert episode_cmd.main(["frob"]) == 2


def test_episode_yaml_comments_are_real_examples(book):
    """Take the # off every example in the file: it must parse, use only real keys, and the per-chapter part
    must be accepted by the settings code (so the examples do what they say)."""
    from novel.core import settings
    assert add() == 0
    ep = book / "episodes" / "E001"
    text = (ep / "episode.yaml").read_text(encoding="utf-8")
    assert "for ONE chapter only" in text and "whole episode" in text
    lines = []
    for line in text.splitlines():
        if line.startswith("# ") and (line[2:].startswith(" ") or line[2:].split(":")[0] in
                                      ("video", "images", "tts", "chapters")):
            lines.append(line[2:])
        elif not line.startswith("#"):
            lines.append(line)
    doc = yaml.safe_load("\n".join(lines))
    assert doc["published"] is False
    chapters = doc.pop("chapters")
    assert list(chapters) == [1]                                    # the first chapter of the episode
    defaults = settings.flatten(yaml.safe_load(settings.defaults_file().read_text(encoding="utf-8")))
    for key in settings.flatten({k: v for k, v in doc.items() if k != "published"}):
        assert key in defaults, key
    for key in settings.flatten(chapters[1]):
        assert key in defaults, key
    settings.check_chapter_block(chapters[1], "episode.yaml chapters.1")        # does not raise
    assert set(settings.flatten(chapters[1])) == {"video.logo.enabled", "video.captions.enabled",
                                                  "video.subscribe.silent_after"}
    # and the file as written (all commented) is still just `published: false`
    assert yaml.safe_load(text) == {"published": False}
