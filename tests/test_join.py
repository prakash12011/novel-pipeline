"""Joining an episode: disclaimer, subscribe cards, series label, timestamps, logo decision.

Real ffmpeg on tiny inputs (3 s chapters). A module-scoped fixture builds a fixture book "demo" with two
episodes of 6 chapters each (E001 = chapters 1-6, E002 = chapters 11-16) and a narrated subscribe
message (E900); each test rewrites book.yaml / episode.yaml and joins again.
"""

import json
import re
import shutil
import subprocess

import pytest
import yaml

from novel.core import context, paths
from novel.stages import episode as E
from novel.stages import episode_build, episode_extras, episode_join
from novel.stages.manifest import Chapter, Manifest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")

BOOK = "demo"
FAST = {"video": {"style": "kenburns", "disclaimer": {"seconds": 1.0}, "subscribe": {"silent": {"seconds": 1}}}}   # these tests are not about the style


def ff(*args):
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True).stdout
    return float(out.strip())


def write_yaml(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(obj, allow_unicode=True), encoding="utf-8")


def make_episode(n, first, last, clip):
    root = E.root(n)
    (root / "video").mkdir(parents=True, exist_ok=True)
    chapters = []
    for c in range(first, last + 1):
        shutil.copy(clip, root / "video" / f"c{c:04d}.mp4")
        chapters.append(Chapter(idx=c, title=f"Chapter {c} title", source=f"{c:04d}_x.txt"))
    Manifest(path=root / "manifest.json", chapters=chapters).save()
    (root / "episode.json").write_text(json.dumps({"episode": n, "start": first, "end": last}), encoding="utf-8")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    base = tmp_path_factory.mktemp("world")
    home, data = base / "home", base / "data"
    home.mkdir()
    data.mkdir()
    mp.setenv("NOVEL_HOME", str(home))
    write_yaml(home / "settings.yaml", {"data_dir": str(data)})
    old = (E.EPISODES, context._ACTIVE_BOOK)
    context.activate(BOOK)
    write_yaml(data / BOOK / "book.yaml", {})
    # the same encoder parameters as the chapter render: H.264 yuv420p bt709 tv range, 24 fps, AAC mono 24 kHz
    clip = base / "clip.mp4"
    ff("-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000",
       "-t", "3", "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
       "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-r", "24",
       "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
       "-c:a", "aac", "-b:a", "160k", "-ac", "1", "-ar", "24000", clip)
    make_episode(1, 1, 6, clip)
    make_episode(2, 11, 16, clip)
    ex = E.root(900)
    (ex / "audio").mkdir(parents=True)
    ff("-f", "lavfi", "-i", "sine=frequency=330:sample_rate=24000", "-t", "1", "-ac", "1", ex / "audio" / "09001.mp3")
    yield {"data": data, "home": home}
    E.EPISODES, context._ACTIVE_BOOK = old
    mp.undo()


def set_book(world, extra=None, episode_yaml=None):
    """Replace book.yaml (fast cards + extra) and every episode.yaml."""
    from novel.core.settings import deep_merge
    write_yaml(world["data"] / BOOK / "book.yaml", deep_merge(FAST, extra or {}))
    for n in (1, 2):
        f = paths.ep_root(BOOK, n) / "episode.yaml"
        f.unlink(missing_ok=True)
        if episode_yaml and n in episode_yaml:
            write_yaml(f, episode_yaml[n])


def order(n):
    """Segment order of the last join, as D (disclaimer), V (voiced), S (silent), cNN (chapter)."""
    names = re.findall(r"/([^/']+)'", (E.root(n) / "video" / "concat.txt").read_text(encoding="utf-8"))
    short = {"disclaimer.mp4": "D", "subscribe-voiced.mp4": "V", "subscribe-silent.mp4": "S"}
    return [short.get(x) or f"c{int(x[1:5])}" for x in names]


def run_join(n, **kw):
    return episode_join.join(n, None, **kw)


@pytest.mark.parametrize("n,expected", [
    (1, ["D", "c1", "c2", "c3", "c4", "c5", "S", "c6", "V"]),
    (2, ["D", "V", "c11", "c12", "c13", "c14", "c15", "S", "c16"]),
])
def test_default_placement(world, n, expected):
    set_book(world)
    run_join(n)
    assert order(n) == expected


@pytest.mark.parametrize("placement,ep1,ep2", [
    ("start-except-first-episode", ["D", "c1", "c2", "c3", "c4", "c5", "S", "c6", "V"],
     ["D", "V", "c11", "c12", "c13", "c14", "c15", "S", "c16"]),
    ("start", ["D", "V", "c1", "c2", "c3", "c4", "c5", "S", "c6"],
     ["D", "V", "c11", "c12", "c13", "c14", "c15", "S", "c16"]),
    ("end", ["D", "c1", "c2", "c3", "c4", "c5", "S", "c6", "V"],
     ["D", "c11", "c12", "c13", "c14", "c15", "S", "c16", "V"]),
    ("none", ["D", "c1", "c2", "c3", "c4", "c5", "S", "c6"],
     ["D", "c11", "c12", "c13", "c14", "c15", "S", "c16"]),
])
def test_voiced_placement(world, placement, ep1, ep2):
    set_book(world, {"video": {"subscribe": {"voiced": {"placement": placement}}}})
    run_join(1)
    run_join(2)
    assert order(1) == ep1
    assert order(2) == ep2


def test_bad_placement_is_a_clear_error(world):
    set_book(world, {"video": {"subscribe": {"voiced": {"placement": "middle"}}}})
    with pytest.raises(SystemExit, match="placement 'middle'"):
        run_join(1)


@pytest.mark.parametrize("every,expected", [
    (5, ["D", "c1", "c2", "c3", "c4", "c5", "S", "c6", "V"]),
    (0, ["D", "c1", "c2", "c3", "c4", "c5", "c6", "V"]),
    (2, ["D", "c1", "c2", "S", "c3", "c4", "S", "c5", "c6", "V"]),
])
def test_silent_every_chapters(world, every, expected):
    set_book(world, {"video": {"subscribe": {"silent": {"every_chapters": every}}}})
    run_join(1)
    assert order(1) == expected


def test_silent_after_override_per_chapter(world):
    ep = {1: {"chapters": {2: {"video": {"subscribe": {"silent_after": True}}},
                           5: {"video": {"subscribe": {"silent_after": False}}}}}}
    set_book(world, episode_yaml=ep)
    run_join(1)
    # the every-5 rule would add a card after chapter 5: suppressed; chapter 2 forced one
    assert order(1) == ["D", "c1", "c2", "S", "c3", "c4", "c5", "c6", "V"]


def test_silent_after_is_chapter_scope_only_key(world):
    from novel.core import settings
    assert "video.subscribe.silent_after" in settings.CHAPTER_SCOPE
    assert settings.get("video.subscribe.silent_after") is None
    ep = {1: {"chapters": {3: {"video": {"subscribe": {"silent_after": True}}}}}}
    set_book(world, episode_yaml=ep)
    assert settings.get("video.subscribe.silent_after", book=BOOK, ep=1, ch=3) is True
    assert settings.get("video.subscribe.silent_after", book=BOOK, ep=1, ch=4) is None


def test_disclaimer_disabled_and_enabled(world):
    set_book(world, {"video": {"disclaimer": {"enabled": False}}})
    run_join(1)
    assert "D" not in order(1)
    stamps = (E.root(1) / "E001.timestamps.txt").read_text(encoding="utf-8").splitlines()
    data = json.loads((E.root(1) / "E001.timestamps.json").read_text(encoding="utf-8"))
    assert data["chapters"][0]["start"] == 0.0           # no card in front: true start is 0:00 too
    assert stamps[0] == "0:00 Chapter 1 title"
    assert not [c for c in data["cards"] if c["kind"] == "disclaimer"]
    set_book(world)
    run_join(1)
    assert order(1)[0] == "D"


def test_changed_disclaimer_text_rebuilds_the_card(world):
    set_book(world, {"video": {"disclaimer": {"text": "First wording.\n\nSecond paragraph."}}})
    run_join(1)
    card = E.root(1) / "video" / "disclaimer.mp4"
    png1, mp1 = card.with_suffix(".png").read_bytes(), card.stat().st_mtime_ns
    run_join(1)                                           # nothing changed: the card is reused
    assert card.stat().st_mtime_ns == mp1
    set_book(world, {"video": {"disclaimer": {"text": "Changed wording."}}})
    run_join(1)
    assert card.with_suffix(".png").read_bytes() != png1
    assert card.stat().st_mtime_ns != mp1
    png2 = card.with_suffix(".png").read_bytes()
    set_book(world, {"video": {"disclaimer": {"text": "Changed wording.", "heading": "OTHER HEADING"}}})
    run_join(1)
    assert card.with_suffix(".png").read_bytes() != png2
    set_book(world, {"video": {"disclaimer": {"text": "Changed wording.", "heading": "OTHER HEADING",
                                              "seconds": 1.5}}})
    run_join(1)
    assert 1.4 < duration(card) < 1.7


def test_generic_disclaimer_text_names_title_and_author(world):
    set_book(world, {"book": {"title": "Test Novel", "author": "A. Writer"}})
    head, paras = episode_join.disclaimer_text(E.root(1))
    text = " ".join(paras)
    assert head == "COPYRIGHT & AI DISCLAIMER"
    assert "Test Novel" in text and "A. Writer" in text
    set_book(world)
    text = " ".join(episode_join.disclaimer_text(E.root(1))[1])
    assert "the original novel" in text and "the original author" in text


def test_timestamps_text_format_and_json(world):
    set_book(world)
    out = run_join(1)
    stamps = out.with_suffix(".timestamps.txt").read_text(encoding="utf-8").splitlines()
    data = json.loads(out.with_suffix(".timestamps.json").read_text(encoding="utf-8"))
    assert len(stamps) == 6 and all(re.fullmatch(r"\d+:\d{2}(:\d{2})? Chapter \d+ title", s) for s in stamps)
    assert stamps[0] == "0:00 Chapter 1 title"           # the first chapter is 0:00 whatever precedes it
    assert data["episode"] == 1 and abs(data["duration"] - duration(out)) < 0.2
    rows = data["chapters"]
    assert [r["chapter"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert rows[0]["start"] == pytest.approx(1.0, abs=0.2)   # the 1 s disclaimer comes first (true position)
    for line, row in zip(stamps[1:], rows[1:]):           # later chapters: the text is the true time
        mm, ss = line.split(" ", 1)[0].split(":")
        assert int(mm) * 60 + int(ss) == int(row["start"])
    assert [c["kind"] for c in data["cards"]] == ["disclaimer", "subscribe-silent", "subscribe-voiced"]
    assert data["cards"][0]["start"] == 0.0 and data["cards"][0]["seconds"] == pytest.approx(1.0, abs=0.1)
    ends = [(r["start"], 3.0) for r in rows] + [(c["start"], c["seconds"]) for c in data["cards"]]
    assert max(s + d for s, d in ends) == pytest.approx(data["duration"], abs=0.2)
    assert "0:00" in data["offset_note"] and "timestamps.txt" in data["offset_note"]


def test_series_label_used_in_metadata(world):
    set_book(world, {"book": {"title": "Test Novel", "series_label": "Test Series"}})
    run_join(1)
    assert "title=Test Series - Episode 1" in (E.root(1) / "video" / "chapters.txt").read_text(encoding="utf-8")
    assert E.series_label() == "Test Series"
    set_book(world, {"book": {"title": "Test Novel"}})
    run_join(1)
    assert "title=Test Novel - Episode 1" in (E.root(1) / "video" / "chapters.txt").read_text(encoding="utf-8")


def test_extras_root_is_under_extras(world):
    assert E.root(900) == paths.book_dir(BOOK) / "extras" / "E900"
    assert E.root(1) == paths.book_dir(BOOK) / "episodes" / "E001"


# --- logo: the "chapter needs re-render" decision ---------------------------------------------------


def make_report(n, logo, **extra):
    root = E.root(1)
    mp4 = root / "video" / f"c{n:04d}.mp4"
    mp4.with_suffix(".render.json").write_text(
        json.dumps({"captions": True, "pass": True, "logo": logo, **extra}), encoding="utf-8")


def test_logo_enabled_with_missing_file_is_a_clear_error(world):
    set_book(world, {"video": {"logo": {"enabled": True, "file": "missing/logo.png"}}})
    make_report(1, None)
    with pytest.raises(SystemExit, match=r"logo file does not exist: .*logo\.png"):
        episode_build.why_render(E.root(1), 1)


def test_logo_setting_change_marks_chapter_for_rerender(world, tmp_path):
    from PIL import Image
    png = tmp_path / "logo.png"
    Image.new("RGBA", (60, 30), (255, 0, 0, 200)).save(png)
    set_book(world)
    make_report(1, None)
    assert episode_build.why_render(E.root(1), 1) is None          # logo off, report says off: current
    logo = {"enabled": True, "file": str(png)}
    set_book(world, {"video": {"logo": logo}})
    assert episode_build.why_render(E.root(1), 1) == "logo settings changed"
    from novel.stages.episode_video import logo_signature
    make_report(1, logo_signature({**logo, "position": "top-left", "margin": 26, "opacity": 1.0, "width": None}))
    assert episode_build.why_render(E.root(1), 1) is None          # rendered with these settings
    set_book(world, {"video": {"logo": {**logo, "opacity": 0.5}}})
    assert episode_build.why_render(E.root(1), 1) == "logo settings changed"
    set_book(world, {"video": {"logo": {**logo, "width": 40}}})
    assert episode_build.why_render(E.root(1), 1) == "logo settings changed"
    # a chapter can switch it off: chapter 2 was rendered without a logo and stays current
    make_report(1, None)
    make_report(2, None)
    set_book(world, {"video": {"logo": logo}},
             {1: {"chapters": {2: {"video": {"logo": {"enabled": False}}}}}})
    assert episode_build.why_render(E.root(1), 2) is None
    assert episode_build.why_render(E.root(1), 1) == "logo settings changed"


def test_logo_layer_position_width_opacity(world, tmp_path):
    from PIL import Image
    from novel.stages.episode_video import logo_layer
    png = tmp_path / "l.png"
    Image.new("RGBA", (100, 50), (255, 255, 255, 255)).save(png)
    base = {"enabled": True, "file": str(png), "margin": 10, "opacity": 0.5, "width": 50}
    rgb, alpha, xy = logo_layer({**base, "position": "bottom-right"}, (1920, 1080))
    assert rgb.size == (50, 25) and xy == (1920 - 50 - 10, 1080 - 25 - 10) and alpha.getpixel((1, 1)) == 128
    assert logo_layer({**base, "position": "top-left"}, (1920, 1080))[2] == (10, 10)
    assert logo_layer({"enabled": False}, (1920, 1080)) is None
    with pytest.raises(SystemExit, match="position"):
        logo_layer({**base, "position": "middle"}, (1920, 1080))


# --- video.title_font: read by the title card, and a change makes the chapter video stale -------------


def test_title_font_setting_marks_chapter_for_rerender_only_when_it_differs(world):
    set_book(world)
    make_report(1, None)                                           # an old report: no "title_font" key
    assert episode_build.why_render(E.root(1), 1) is None          # the default font: still current
    set_book(world, {"video": {"title_font": "assets/fonts/Anton-Regular.ttf"}})
    assert episode_build.why_render(E.root(1), 1) == "title font setting changed"
    root = E.root(1)
    (root / "video" / "c0001.render.json").write_text(json.dumps(
        {"captions": True, "pass": True, "logo": None, "title_font": "assets/fonts/Anton-Regular.ttf"}), encoding="utf-8")
    assert episode_build.why_render(root, 1) is None
    set_book(world, {"video": {"title_font": "assets/fonts/PlayfairDisplay.ttf"}})
    assert episode_build.why_render(root, 1) == "title font setting changed"


# --- video.style: a change makes the chapter video stale ---------------------------------------------


def test_video_style_setting_marks_chapter_for_rerender_only_when_it_differs(world):
    set_book(world)
    make_report(1, None)                                           # an old report: no "style" key
    assert episode_build.why_render(E.root(1), 1) is None          # the default style (kenburns): still current
    set_book(world, {"video": {"style": "panels"}})
    assert episode_build.why_render(E.root(1), 1) == "video style changed"
    make_report(1, None, style="panels")
    assert episode_build.why_render(E.root(1), 1) is None          # rendered in this style
    set_book(world)
    assert episode_build.why_render(E.root(1), 1) == "video style changed"


def test_unknown_video_style_is_a_clear_error(world):
    from novel.stages.episode import video_style
    set_book(world, {"video": {"style": "collage"}})
    make_report(1, None)
    with pytest.raises(SystemExit, match="video.style 'collage': use kenburns or panels"):
        episode_build.why_render(E.root(1), 1)
    assert video_style({}) == "kenburns" and video_style({"video": {"style": "panels"}}) == "panels"


# --- video.captions.enabled: a report is current when it matches the setting, either way --------------


def test_subtitles_setting_marks_chapter_for_rerender_only_when_it_differs(world):
    set_book(world, {"video": {"captions": {"enabled": False}}})
    make_report(1, None, captions=False)                           # rendered without subtitles, as asked
    assert episode_build.why_render(E.root(1), 1) is None
    make_report(1, None, captions=True)
    assert episode_build.why_render(E.root(1), 1) == "subtitles setting changed"
    set_book(world)                                                # subtitles on (the default)
    make_report(1, None, captions=False)
    assert episode_build.why_render(E.root(1), 1) == "subtitles setting changed"
    make_report(1, None, captions=True)
    assert episode_build.why_render(E.root(1), 1) is None


def test_title_card_uses_the_configured_font_and_a_plain_font_does_not_crash(world):
    import numpy as np
    from novel.stages import episode_render as R
    from novel.stages.episode_video import title_font_file
    title = {"name": "The Long Road Home", "series": "Demo", "chapter": 1}
    base = R._title_layers({**title, "font": str(R.FONT)})
    anton = R._title_layers({**title, "font": str(title_font_file({"video": {"title_font": "assets/fonts/Anton-Regular.ttf"}}))})
    assert not np.array_equal(np.asarray(base[1]), np.asarray(anton[1]))      # another typeface: another picture
    assert np.asarray(anton[1]).any()                                         # and text was drawn (Anton has no variations)
    with pytest.raises(SystemExit, match="video.title_font"):
        title_font_file({"video": {"title_font": "assets/fonts/nope.ttf"}})
    assert R._title_layers(title)[1] is not None                              # no "font" key: the old constant


def test_default_title_font_is_the_old_constant(world):
    from novel.stages import episode_render as R
    from novel.stages.episode_video import title_font_file, DEFAULT_TITLE_FONT
    assert title_font_file({"video": {}}) == R.FONT and DEFAULT_TITLE_FONT == "assets/fonts/PlayfairDisplay.ttf"
