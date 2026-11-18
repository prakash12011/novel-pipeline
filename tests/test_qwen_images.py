"""The Qwen production path: copying finished pictures into the episode, the free-account picker, the sheet prompt."""

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


def test_two_cards_split_the_pictures_alternately_and_each_makes_its_half(monkeypatch):
    from types import SimpleNamespace
    from novel.stages import ktrial_run as KR
    plan = {"plates": [], "shots": [{"out": f"images/s{i:02d}.jpg", "id": f"c0001-s{i:02d}", "group": "g01"}
                                    for i in range(1, 6)]}
    seen = {}
    monkeypatch.setattr(KR, "_execute", lambda run, args, plan: seen.setdefault(run, args.only) and 0)
    args = SimpleNamespace(only=None, limit=None, reroll=False, redo=False)
    assert KR._execute_two(["card1", "card2"], args, plan) == 0
    assert seen == {"card1": "s01,s03,s05", "card2": "s02,s04"}
    seen.clear()
    assert KR._execute_two(["card1", "card2"], SimpleNamespace(only="s04", limit=None), plan) == 0
    assert seen == {"card1": "s04"}                                              # one picture: one card
