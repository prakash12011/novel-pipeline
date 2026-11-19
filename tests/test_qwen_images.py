"""The Qwen production path: copying finished pictures into the episode, the free-account picker, the sheet prompt."""

import os
from pathlib import Path

import pytest

from novel import kcomfy
from novel.stages import account, qwen_images


def test_adopt_copies_finished_pictures_as_2k_and_skips_side_files(tmp_path):
    src = tmp_path / "trial" / kcomfy.QW_DIR / "images"
    src.mkdir(parents=True)
    for name in ("s01.jpg", "s12.jpg", "s12_before_guard.jpg", "s100.jpg"):
        (src / name).write_bytes(b"jpg " + name.encode())
    (src / "s01.prompt.txt").write_text("the prompt", encoding="utf-8")
    img = tmp_path / "ep" / "images" / "c0001"
    assert qwen_images.adopt(tmp_path / "trial", img) == 3
    assert sorted(p.name for p in img.glob("*.jpg")) == ["s01-2k.jpg", "s100-2k.jpg", "s12-2k.jpg"]
    assert (img / "s01.prompt.txt").read_text(encoding="utf-8") == "the prompt"
    assert qwen_images.adopt(tmp_path / "trial", img) == 0             # nothing newer: nothing copied again


def test_chapter_range():
    assert qwen_images.chapter_range(None, 1, 3) == [1, 2, 3]
    assert qwen_images.chapter_range("2-3", 1, 10) == [2, 3] and qwen_images.chapter_range("4", 1, 10) == [4]


@pytest.fixture
def three_accounts(monkeypatch):
    from novel.core import settings
    monkeypatch.setattr(account, "load_profiles", lambda: {"active": None, "profiles": {
        "prakash": {}, "seema": {}, "shashi": {}}})
    pick = {"value": True}
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda key, *a, **kw: pick["value"] if key == "kaggle.pick_free"
                        else real(key, *a, **kw))
    return pick


def test_free_profile_takes_the_preferred_account_when_free_else_the_next_free(three_accounts, monkeypatch):
    busy = {"prakash": ["prakashverma120011/hindi-audiobook-daily"], "seema": [], "shashi": []}
    monkeypatch.setattr(account, "running_kernels", lambda name: busy[name])
    said = []
    assert account.free_profile("seema", say=said.append) == "seema"
    assert account.free_profile("prakash", say=said.append) == "seema"           # prakash busy: the next free one
    assert said[-2] == "Kaggle account prakash: busy (prakashverma120011/hindi-audiobook-daily)"
    busy["seema"] = ["seema12011/coi-ktrial-comfy"]
    assert account.free_profile(None, say=said.append) == "shashi"


def test_free_profile_stops_when_every_account_is_busy(three_accounts, monkeypatch):
    monkeypatch.setattr(account, "running_kernels", lambda name: [f"{name}/x"])
    with pytest.raises(SystemExit, match="every Kaggle account is busy"):
        account.free_profile(None, say=lambda t: None)


def test_free_profile_off_is_the_old_choice_without_any_kaggle_call(three_accounts, monkeypatch):
    three_accounts["value"] = False
    monkeypatch.setattr(account, "running_kernels", lambda name: pytest.fail("no Kaggle call when pick_free is off"))
    monkeypatch.setattr(account, "resolve_profile", lambda prefer=None: prefer or "prakash")
    assert account.free_profile(None) == "prakash" and account.free_profile("seema") == "seema"


def test_sheet_prompt_is_the_book_style_on_a_plain_sheet_with_the_bible_look():
    from novel.stages import ktrial_run as KR
    bible = {"style": "Anime art style, warm amber palette.",
             "characters": {"maren_vale": {"display": "Maren Vale", "look": "tall woman, short dark hair."}}}
    text = KR.sheet_prompt(bible, "maren_vale")
    assert text.startswith("16:9 character reference sheet on a plain, flat, light neutral gray background.")
    assert "CHARACTER: Maren Vale, tall woman, short dark hair.\n" in text
    assert KR.SHEET_LAYOUT in text and text.rstrip().endswith(KR.SHEET_END)


def test_live_servers_takes_the_newest_answering_record_of_each_notebook(tmp_path, monkeypatch):
    from novel.stages import ktrial_run as KR
    monkeypatch.setattr(qwen_images.paths, "book_dir", lambda book: tmp_path)
    for chapter, n, url, model in ((1, 1, "https://old-1", kcomfy.QWEN), (2, 1, "https://new-1", kcomfy.QWEN),
                                   (2, 2, "https://new-2", kcomfy.QWEN), (1, 2, "https://flux-2", kcomfy.FLUX)):
        trial = tmp_path / "_work" / "ktrial" / f"c{chapter:04d}"
        KR.save_server(trial, {"url": url, "model": model}, n)
        os.utime(KR.server_path(trial, n), (chapter * 10 + n, chapter * 10 + n))
    monkeypatch.setattr(KR, "answering", lambda s, retries=1: None if "dead" in s["url"] else dict(s))
    got = qwen_images.live_servers("b")
    assert [(s["n"], s["url"]) for s in got] == [(1, "https://new-1"), (2, "https://new-2")]
    monkeypatch.setattr(KR, "answering", lambda s, retries=1: None if s["url"] == "https://new-1" else dict(s))
    assert [(s["n"], s["url"]) for s in qwen_images.live_servers("b")] == [(1, "https://old-1"), (2, "https://new-2")]


def test_a_booting_notebook_2_is_kept_to_join_but_an_old_one_is_not(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from novel.stages import ktrial_run as KR
    monkeypatch.setattr(qwen_images.paths, "book_dir", lambda book: tmp_path)
    trial = tmp_path / "_work" / "ktrial" / "c0001"
    stamp = lambda h: (datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y%m%dT%H%M%S")   # noqa: E731
    KR.save_server(trial, {"url": "https://one", "model": kcomfy.QWEN, "pushed": stamp(0.1)}, 1)
    KR.save_server(trial, {"url": "https://two", "model": kcomfy.QWEN, "pushed": stamp(0.1)}, 2)
    monkeypatch.setattr(KR, "answering", lambda s, retries=1: dict(s) if s["url"] == "https://one" else None)
    assert [s["n"] for s in qwen_images.live_servers("b")] == [1, 2]          # 2 still booting: it joins later
    KR.save_server(trial, {"url": "https://two", "model": kcomfy.QWEN, "pushed": stamp(9)}, 2)
    assert [s["n"] for s in qwen_images.live_servers("b")] == [1]             # yesterday's notebook 2: gone
    monkeypatch.setattr(KR, "answering", lambda s, retries=1: None)
    KR.save_server(trial, {"url": "https://two", "model": kcomfy.QWEN, "pushed": stamp(0.1)}, 2)
    assert qwen_images.live_servers("b") == []                                # no server 1: start both again
