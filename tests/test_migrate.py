import hashlib
import json
import os
import zipfile

import pytest
import yaml

from novel import migrate
from novel.commands import migrate_cmd
from novel.stages import episode as episode_stage
from novel.stages import extract as ex

WORDS = "alpha beta gamma delta epsilon zeta eta theta iota kappa " * 8


def make_epub(path, nums):
    docs = []
    for n in nums:
        docs.append((f"c{n}.xhtml",
                     f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head>'
                     f"<title>Chapter {n}: Story {n}</title></head><body><div>"
                     f"<p>{WORDS} number {n}</p><p>Second paragraph of {n}.</p></div></body></html>"))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/book.opf"/></rootfiles></container>')
        items = "".join(f'<item id="i{i}" href="{n}" media-type="application/xhtml+xml"/>'
                        for i, (n, _) in enumerate(docs))
        refs = "".join(f'<itemref idref="i{i}"/>' for i in range(len(docs)))
        z.writestr("OEBPS/book.opf", f'<package xmlns="http://www.idpf.org/2007/opf"><manifest>{items}'
                                      f"</manifest><spine>{refs}</spine></package>")
        for n, content in docs:
            z.writestr(f"OEBPS/{n}", content)


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)


SKILL = """---
name: fake
---
**Title**
```
Fake Show Episode [N] (Ch [X]-[Y])
```

**Description**
```
Intro line for [N].
Novel by Pen Name. Support the author.

#FakeShow #Fake2
```

**Tags** (comma-separated)
```
fake show, fake show hindi, tag [V]
```
"""

DISCLAIMER_PY = '''
def disclaimer_text(ep_root):
    return ("FAKE HEADING", [
        "An invented notice. All rights belong to the original author, "
        "Pen Name, and the respective publishers.",
    ])
'''

EXTRAS_PY = '''
CHANNEL = "Manga Aur Novel"
CARD_TEXT = "narrator text, not the card"
SPOKEN = ["pehli line", "doosri line"]


def card_image(out):
    d.text((1, 2), "Enjoying Manga Aur Novel?", font=f)
    d.text((1, 2), "SUBSCRIBE", font=f)
    d.text((1, 2), CHANNEL, font=f)
    d.text((1, 2), "Like  \u00b7  Comment", font=f)
    d.text((1, 2), "Help other fans find this channel", font=f)
'''

OLD_BIBLE = {
    "_README": "old readme",
    "style": "ink style",
    "characters": {
        "hero": {"display": "Hero", "hi": "हीरो", "tier": "main", "appearance": "a tall man",
                 "flow_name": "hero", "chapters": "1-", "flow_made": True},
        "sefirah_castle": {"display": "Castle", "appearance": "grey stone towers", "flow_name": "castle"},
    },
    "content_flags": {},
}


def build_old(root):
    """A small fake of the old project. Returns the old root."""
    put(root / "config.yaml", "tts:\n  maya2:\n    voice: Fakevoice\n")
    put(root / "abpipe" / "episode_join.py", DISCLAIMER_PY)
    put(root / "abpipe" / "episode_extras.py", EXTRAS_PY)
    put(root / ".claude" / "skills" / "coi-episode-publish" / "SKILL.md", SKILL)
    (root / "ebook").mkdir(parents=True)
    make_epub(root / "ebook" / "vol1.epub", [1, 2, 3])
    make_epub(root / "ebook" / "vol2.epub", [4, 5, 6])
    sc = ex.scan(root / "ebook", None, [])
    eps = root / "workspace" / "episodes"
    for n in range(1, 7):
        e = eps / f"E{n:03d}"
        ex.write_chapters([c for c in sc.chapters if c.num == n], e / "chapters")
        hindi = f"नमस्ते अध्याय {n}\n"
        put(e / "hindi" / f"{n:05d}.txt", hindi)
        lock = {f"{n:05d}.txt": hashlib.sha256(hindi.encode("utf-8")).hexdigest()}
        put(e / "hindi.lock.json", json.dumps(lock))
        put(e / "audio" / f"c{n:04d}.mp3", b"mp3" * 10)
        put(e / "timing" / f"{n:05d}.json", "{}")
        put(e / "manifest.json", "{}")
        put(e / "episode.json", json.dumps({"episode": n, "start": n, "end": n,
                                            "flow_project": f"https://flow.example/p/{n}"}))
        put(e / "names.json", json.dumps({"Hero": "हीरो"}))
        put(e / "bible" / "characters.json", json.dumps(OLD_BIBLE))
        put(e / "bible" / "flow" / "hero-portrait.jpg", b"jpg-portrait")
        put(e / "bible" / "flow" / "journal.jsonl", "{}\n")
        put(e / "scenes" / f"c{n:04d}.json", "{}")
        put(e / "images" / f"c{n:04d}" / "s01-2k.jpg", b"img" * 20)
        put(e / "flow" / "plan.json", "{}")
        put(e / "video" / "disclaimer.mp4", b"vid")
        put(e / "cast-sheet.md", "cast")
        put(e / "gemini-prompt.txt", "prompt")
    for n in (1, 2):
        e = eps / f"E{n:03d}"
        put(e / f"E{n:03d}.mp4", b"x" * 500)
        put(e / f"E{n:03d}.timestamps.txt", "00:00")
        put(e / "thumbnail" / "thumb-a.jpg", b"thumb")
        put(e / "video" / "c0001.mp4", b"y" * 300)
        put(e / "video" / "c0001.render.json", "{}")
    e900 = eps / "E900"
    put(e900 / "audio" / "x.mp3", b"mp3")
    put(e900 / "video" / "subscribe-voiced.mp4", b"vid")
    (e900 / "qa").mkdir()
    put(eps / "_split-log.json", "{}")
    put(eps / "_archive-20ch" / "E001" / "old.txt", "old")
    return root


def listing(root):
    """{relative path: sha256} of every file under root."""
    out = {}
    for r, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(r, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def stat_listing(root):
    out = {}
    for r, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(r, f)
            st = os.stat(p)
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    (home / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    old = build_old(tmp_path / "old")
    return old, data / "coi", data


def run(old, *flags):
    return migrate_cmd.main(["coi", "--from", str(old), *flags])


def test_dry_run_writes_nothing(env, capsys):
    old, book, data = env
    assert run(old, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "would copy" in out and "free space on" in out and "Nothing was written" in out
    assert not book.exists()
    assert list(data.iterdir()) == []


def test_real_run_layout(env):
    old, book, _ = env
    assert run(old) == 0
    for d in ("source", "chapters", "bible", "bible/flow", "inbox", "gemini", "research", "extras", "episodes"):
        assert (book / d).is_dir(), d
    cfg = yaml.safe_load((book / "book.yaml").read_text(encoding="utf-8"))
    assert cfg["book"]["author"] == "Pen Name"
    assert cfg["book"]["slug"] == "coi"
    assert cfg["book"]["volumes"] == [[1, 500, 1], [501, 1000, 2], [1001, 1180, 3]]
    assert cfg["tts"] == {"voice": "Fakevoice", "kaggle_profile": "prakash"}
    assert cfg["images"] == {"flow_project": "https://flow.example/p/3", "flow_model": "Nano Banana Pro"}
    assert cfg["video"]["disclaimer"]["text"].startswith("An invented notice")
    assert cfg["video"]["subscribe"]["card_text"].splitlines() == [
        "Enjoying Manga and Novel?", "SUBSCRIBE", "Like  ·  Comment", "Help other fans find this channel"]
    assert "Manga Aur Novel" not in (book / "book.yaml").read_text(encoding="utf-8")
    assert cfg["video"]["subscribe"]["spoken_lines"] == ["pehli line", "doosri line"]
    assert cfg["publish"]["hashtags"] == ["#FakeShow", "#Fake2"]
    assert cfg["publish"]["tags"] == ["fake show", "fake show hindi", "tag [V]"]
    assert cfg["publish"]["title_template"] == "Fake Show Episode [N] (Ch [X]-[Y])"
    assert "playlist_url" not in cfg["publish"]

    assert sorted(p.name for p in (book / "source").iterdir()) == ["vol1.epub", "vol2.epub"]
    assert len(list((book / "chapters").glob("*.txt"))) == 6
    # chapters are byte-identical to the old per-episode files
    for n in range(1, 7):
        for f in (old / "workspace" / "episodes" / f"E{n:03d}" / "chapters").glob("*.txt"):
            assert (book / "chapters" / f.name).read_bytes() == f.read_bytes()

    chars = json.loads((book / "bible" / "characters.json").read_text(encoding="utf-8"))
    locs = json.loads((book / "bible" / "locations.json").read_text(encoding="utf-8"))
    assert chars["version"] == 2 and list(chars["characters"]) == ["hero"]
    assert "sefirah_castle" in locs["locations"] and "sefirah_castle" not in chars["characters"]
    assert (book / "bible" / "names.json").exists()
    assert (book / "bible" / "flow" / "hero-portrait.jpg").read_bytes() == b"jpg-portrait"

    eps = book / "episodes"
    for n in (1, 2):
        e = eps / f"E{n:03d}"
        assert yaml.safe_load((e / "episode.yaml").read_text(encoding="utf-8")) == {"published": True}
        assert (e / "images" / f"c{n:04d}" / "s01-2k.jpg").exists()
        assert (e / f"E{n:03d}.timestamps.txt").exists()
        assert not (e / f"E{n:03d}.mp4").exists()
        assert not (e / "thumbnail").exists()
        assert (e / "video" / "c0001.render.json").exists()
        assert not (e / "video" / "c0001.mp4").exists()
    for n in (3, 4, 5, 6):
        e = eps / f"E{n:03d}"
        assert yaml.safe_load((e / "episode.yaml").read_text(encoding="utf-8")) == {"published": False}
        for kept in ("hindi", "audio", "timing", "chapters", "hindi.lock.json", "manifest.json"):
            assert (e / kept).exists(), (n, kept)
        arch = e / "_archive" / "v1-old-flow"
        for old_part in ("scenes", "images", "flow", "video", "bible", "cast-sheet.md", "gemini-prompt.txt"):
            assert (arch / old_part).exists(), (n, old_part)
        assert not (e / "images").exists() and not (e / "scenes").exists()
    assert (book / "extras" / "E900" / "audio" / "x.mp3").exists()
    assert (book / "extras" / "E900" / "qa").is_dir()
    assert not (book / "_archive-20ch").exists()


def test_with_media_adds_final_video_and_thumbnails(env):
    old, book, _ = env
    assert run(old) == 0
    assert not (book / "episodes" / "E001" / "E001.mp4").exists()
    assert run(old, "--with-media") == 0
    assert (book / "episodes" / "E001" / "E001.mp4").exists()
    assert (book / "episodes" / "E002" / "thumbnail" / "thumb-a.jpg").exists()
    # intermediates are never copied
    assert not (book / "episodes" / "E001" / "video" / "c0001.mp4").exists()


def test_second_run_copies_nothing(env, capsys):
    old, book, _ = env
    assert run(old) == 0
    capsys.readouterr()
    before = stat_listing(book)
    assert run(old) == 0
    out = capsys.readouterr().out
    assert "nothing to copy" in out
    assert "already migrated" in out
    assert stat_listing(book) == before


def test_differing_target_stops_the_run(env, capsys):
    old, book, _ = env
    assert run(old) == 0
    capsys.readouterr()
    victim = book / "episodes" / "E003" / "audio" / "c0003.mp3"
    victim.write_bytes(b"edited by hand")
    (book / "extras" / "E900" / "audio" / "x.mp3").unlink()      # something a rerun would copy back
    before = listing(book)
    assert run(old) == 1
    captured = capsys.readouterr()
    assert "different content" in captured.err and "c0003.mp3" in captured.err
    assert victim.read_bytes() == b"edited by hand"
    assert listing(book) == before        # nothing written, not even the missing file


def test_migration_json_hashes_match(env):
    old, book, _ = env
    assert run(old) == 0
    rec = json.loads((book / "migration.json").read_text(encoding="utf-8"))
    assert rec["files"]
    for e in rec["files"]:
        target = book / e["target"]
        assert target.is_file(), e["target"]
        assert target.stat().st_size == e["size"]
        assert hashlib.sha256(target.read_bytes()).hexdigest() == e["sha256"]
        if not e["source"].startswith("generated:"):
            src = migrate.Path(e["source"])
            assert hashlib.sha256(src.read_bytes()).hexdigest() == e["sha256"]
    targets = [e["target"] for e in rec["files"]]
    assert len(targets) == len(set(targets))
    assert "book.yaml" in targets and "episodes/E003/_archive/v1-old-flow/images/c0003/s01-2k.jpg" in targets


def test_lock_hashes_match_copied_hindi(env):
    old, book, _ = env
    assert run(old) == 0
    for n in range(1, 7):
        episode_stage.check_lock(book / "episodes" / f"E{n:03d}", n)     # raises SystemExit on a mismatch


def test_old_project_untouched(env):
    old, book, _ = env
    before = listing(old)
    assert run(old, "--dry-run") == 0
    assert run(old) == 0
    assert run(old, "--with-media") == 0
    assert run(old) == 0
    assert listing(old) == before


def test_not_enough_free_space_stops(env, capsys, monkeypatch):
    old, book, data = env
    monkeypatch.setattr(migrate, "free_space", lambda b: (data, 1024))
    assert run(old) == 1
    assert "not enough free space" in capsys.readouterr().err
    assert not book.exists()


def test_other_book_refused(env, capsys):
    old, _, _ = env
    assert migrate_cmd.main(["lotm", "--from", str(old)]) == 2
