import json
import shutil

import pytest
import yaml
from PIL import Image

from novel.commands import thumb_cmd
from novel.core import paths
from novel.publish import thumb

FONT = paths.repo_root() / "assets" / "fonts" / "Anton-Regular.ttf"
HOOKS = ["THIS VILLAGE|IS *NOT NORMAL*", "WHO IS|*HERO?*", "THE LAST|*CHANCE*"]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    (home / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    book = data / "demo"
    flow = book / "bible" / "flow"
    flow.mkdir(parents=True)
    Image.new("RGB", (800, 1000), (90, 120, 160)).save(flow / "hero-portrait.jpg", quality=90)
    ep = book / "episodes" / "E002"
    ep.mkdir(parents=True)
    (ep / "episode.json").write_text(json.dumps({"episode": 2, "start": 11, "end": 20}), encoding="utf-8")
    return tmp_path, book, ep


def run(*args):
    return thumb_cmd.main(["--book", "demo", "--ep", "2", *args])


def hook_args(hooks):
    out = []
    for h in hooks:
        out += ["--hook", h]
    return out


def test_three_hooks_give_three_jpgs_and_sheet(setup, capsys):
    tmp, _, _ = setup
    out = tmp / "out"
    rc = run("--image", "hero", "--out", str(out), *hook_args(HOOKS))
    assert rc == 0, capsys.readouterr().err
    for letter in "abc":
        p = out / f"thumb-{letter}.jpg"
        assert p.is_file()
        with Image.open(p) as im:
            assert im.format == "JPEG"
            assert im.size == (1280, 720)
        assert p.stat().st_size < 2_000_000
    assert (out / "sheet.jpg").is_file()
    assert not (out / "thumb-d.jpg").exists()


def test_one_hook_writes_only_one_jpg(setup):
    tmp, _, _ = setup
    out = tmp / "one"
    assert run("--image", "hero", "--out", str(out), *hook_args(HOOKS[:1])) == 0
    assert (out / "thumb-a.jpg").is_file()
    assert not (out / "thumb-b.jpg").exists()
    assert (out / "sheet.jpg").is_file()


def test_default_output_is_episode_publish_folder(setup):
    _, _, ep = setup
    assert run("--image", "hero", *hook_args(HOOKS[:1])) == 0
    assert (ep / "publish" / "thumb-a.jpg").is_file()
    assert (ep / "publish" / "sheet.jpg").is_file()


def test_image_found_by_key_and_by_path(setup):
    tmp, book, _ = setup
    key_path = thumb_cmd.find_art("hero", "demo")
    assert key_path == book / "bible" / "flow" / "hero-portrait.jpg"
    copy = tmp / "elsewhere.jpg"
    shutil.copy(key_path, copy)
    assert thumb_cmd.find_art(str(copy), "demo") == copy

    assert run("--image", "hero", "--out", str(tmp / "k"), *hook_args(HOOKS[:1])) == 0
    assert run("--image", str(copy), "--out", str(tmp / "p"), *hook_args(HOOKS[:1])) == 0
    assert (tmp / "p" / "thumb-a.jpg").is_file()


def test_missing_image_returns_1_and_names_the_key(setup, capsys):
    tmp, _, _ = setup
    rc = run("--image", "nobody", "--out", str(tmp / "x"), *hook_args(HOOKS[:1]))
    assert rc == 1
    err = capsys.readouterr().err
    assert "nobody" in err
    assert "hero" in err
    assert not (tmp / "x" / "thumb-a.jpg").exists()


def test_long_hook_shrinks_font_and_still_fits(setup, capsys):
    tmp, _, _ = setup
    hook = "THIS VILLAGE IS NOT|*NORMAL AT ALL*"
    lines = thumb.parse(hook)
    f, size, widths = thumb.fit(lines, thumb.BOX_W, thumb.BOX_BOT - thumb.BOX_TOP, FONT)
    assert size < 190
    assert max(widths) <= thumb.BOX_W

    out = tmp / "long"
    rc = run("--image", "hero", "--out", str(out), "--hook", hook)
    assert rc == 0, capsys.readouterr().err
    with Image.open(out / "thumb-a.jpg") as im:
        assert im.size == (1280, 720)


def test_hook_too_long_even_at_minimum_size_returns_1(setup, capsys):
    tmp, _, _ = setup
    rc = run("--image", "hero", "--out", str(tmp / "huge"),
             "--hook", "ONE VERY LONG HOOK LINE THAT KEEPS GOING AND GOING|AND A SECOND LINE")
    assert rc == 1
    assert "hook too long" in capsys.readouterr().err


def test_hooks_read_from_publish_json(setup):
    tmp, _, ep = setup
    (ep / "publish").mkdir()
    (ep / "publish" / "publish.json").write_text(
        json.dumps({"thumbnail_hooks": ["A HOOK|*HERE*", "ANOTHER|*ONE*"]}), encoding="utf-8")
    out = tmp / "from_file"
    assert run("--image", "hero", "--out", str(out)) == 0
    assert (out / "thumb-a.jpg").is_file()
    assert (out / "thumb-b.jpg").is_file()
    assert not (out / "thumb-c.jpg").exists()


def test_no_hooks_returns_1(setup, capsys):
    tmp, _, _ = setup
    rc = run("--image", "hero", "--out", str(tmp / "none"))
    assert rc == 1
    assert "No hooks. Pass --hook" in capsys.readouterr().err


def test_publish_json_without_hooks_says_so(setup, capsys):
    tmp, _, ep = setup
    (ep / "publish").mkdir()
    (ep / "publish" / "publish.json").write_text(json.dumps({"valid": True, "thumbnail_hooks": []}), encoding="utf-8")
    assert run("--image", "hero", "--out", str(tmp / "o")) == 1
    err = capsys.readouterr().err
    assert "publish.json has no thumbnail_hooks" in err and "--hook" in err and "--ai none" in err


def test_no_art_lists_the_portraits_there_are(setup, capsys):
    tmp, _, _ = setup
    assert run("--out", str(tmp / "o"), *hook_args(HOOKS[:1])) == 1
    err = capsys.readouterr().err
    assert "No art." in err and "Characters with a portrait: hero." in err


def put_publish(ep, **kw):
    (ep / "publish").mkdir(exist_ok=True)
    (ep / "publish" / "publish.json").write_text(json.dumps({"thumbnail_hooks": HOOKS[:1], **kw}), encoding="utf-8")


def spy(monkeypatch):
    seen = {}
    real = thumb.make_thumbs

    def fake(art, hooks, out, **kw):
        seen.update(kw, art_size=art.size)
        return real(art, hooks, out, **kw)
    monkeypatch.setattr(thumb, "make_thumbs", fake)
    return seen


def test_art_defaults_to_the_character_novel_meta_chose(setup, capsys):
    tmp, book, ep = setup
    put_publish(ep, thumbnail_character="Hero")           # a display name; the portrait file is hero-portrait.jpg
    assert run("--out", str(tmp / "o")) == 0
    out = capsys.readouterr().out
    assert "Hero's portrait" in out and "hero-portrait.jpg" in out and (tmp / "o" / "thumb-a.jpg").is_file()


def test_meta_character_found_through_the_bible_display_name(setup):
    tmp, book, ep = setup
    flow = book / "bible" / "flow"
    Image.new("RGB", (400, 500), (200, 50, 50)).save(flow / "maren_vale-portrait.jpg")
    (book / "bible" / "characters.json").write_text(json.dumps({"version": 2, "style": "s", "characters": {
        "maren_vale": {"display": "Maren Vale", "role": "r", "look": "l", "variants": {
            "base": {"flow_name": "Maren V2", "chapters": "1-", "look": None, "flow_made": True}}}}}), encoding="utf-8")
    put_publish(ep, thumbnail_character="Maren Vale")
    got = thumb_cmd.art_from_meta("demo", ep)
    assert got and got[0].name == "maren_vale-portrait.jpg"


def test_explicit_image_and_setting_beat_the_meta_character(setup, monkeypatch, capsys):
    tmp, book, ep = setup
    Image.new("RGB", (300, 300), (1, 2, 3)).save(book / "bible" / "flow" / "other-portrait.jpg")
    put_publish(ep, thumbnail_character="Hero")
    seen = spy(monkeypatch)
    assert run("--image", "other", "--out", str(tmp / "o1")) == 0
    assert seen["art_size"] == (300, 300)
    (book / "book.yaml").write_text(yaml.safe_dump({"thumbnail": {"art": "other"}}), encoding="utf-8")
    seen.clear()
    spy(monkeypatch)
    assert run("--out", str(tmp / "o2")) == 0
    assert "thumbnail_character that novel meta chose" not in capsys.readouterr().out


def test_no_portrait_for_the_meta_character_is_still_the_old_error(setup, capsys):
    tmp, book, ep = setup
    put_publish(ep, thumbnail_character="Nobody Known")
    assert run() == 1
    assert "No art." in capsys.readouterr().err
    put_publish(ep, thumbnail_character=None)
    assert run() == 1


def test_focus_comes_from_the_setting_unless_given(setup, monkeypatch):
    tmp, book, ep = setup
    (book / "book.yaml").write_text(yaml.safe_dump({"thumbnail": {"focus": "0.3,0.2"}}), encoding="utf-8")
    seen = spy(monkeypatch)
    assert run("--image", "hero", "--out", str(tmp / "a"), *hook_args(HOOKS[:1])) == 0
    assert seen["focus"] == (0.3, 0.2)
    assert run("--image", "hero", "--focus", "0.6,0.5", "--out", str(tmp / "b"), *hook_args(HOOKS[:1])) == 0
    assert seen["focus"] == (0.6, 0.5)
    (book / "book.yaml").write_text("{}\n", encoding="utf-8")
    assert run("--image", "hero", "--out", str(tmp / "c"), *hook_args(HOOKS[:1])) == 0
    assert seen["focus"] == (0.53, 0.40)               # nothing set: the old default


def test_bad_focus_setting_says_where_it_came_from(setup, capsys):
    tmp, book, ep = setup
    (book / "book.yaml").write_text(yaml.safe_dump({"thumbnail": {"focus": "left"}}), encoding="utf-8")
    assert run("--image", "hero", "--out", str(tmp / "a"), *hook_args(HOOKS[:1])) == 1
    assert "thumbnail.focus" in capsys.readouterr().err
