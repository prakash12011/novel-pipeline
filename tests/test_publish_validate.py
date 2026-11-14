import pytest

from novel.publish import validate as v

SERIES = "Circle of Inevitability Explained in Hindi"


# ---- title
def test_title_ok():
    assert v.check_title(SERIES + " | Episode 2 (Ch 11-20) | Lord of the Mysteries 2", SERIES) == []


def test_title_too_long():
    assert any("101" in e for e in v.check_title(SERIES + " " + "x" * (101 - len(SERIES) - 1), SERIES))
    assert v.check_title((SERIES + " " + "x" * 100)[:100], SERIES) == []


def test_title_must_start_with_series():
    assert v.check_title("Episode 2 | " + SERIES, SERIES)


def test_title_devanagari_and_brackets():
    assert any("Devanagari" in e for e in v.check_title(SERIES + " कहानी", SERIES))
    assert any("<" in e for e in v.check_title(SERIES + " <b>", SERIES))


# ---- description
def test_description_length():
    assert v.check_description("a" * 5000) == []
    assert v.check_description("a" * 5001)
    assert v.check_description("  ")


def test_description_angle_brackets():
    assert v.check_description("hello <world>")


# ---- timestamps
GOOD = "0:00 Intro\n0:30 Chapter 1\n5:10 Chapter 2\n"


def test_stamps_good():
    assert v.check_timestamps(GOOD) == []


def test_stamps_need_three():
    assert any("at least 3" in e for e in v.check_timestamps("0:00 A\n1:00 B\n"))


def test_stamps_first_zero():
    assert any("0:00" in e for e in v.check_timestamps("0:05 A\n1:00 B\n2:00 C\n"))


def test_stamps_ascending():
    assert any("ascend" in e for e in v.check_timestamps("0:00 A\n2:00 B\n1:00 C\n"))


def test_stamps_ten_seconds():
    errs = v.check_timestamps("0:00 A\n0:09 B\n1:00 C\n")
    assert any("9 s" in e for e in errs)
    assert v.check_timestamps("0:00 A\n0:10 B\n1:00 C\n") == []


def test_stamps_hour_form_mixing():
    assert any("mixes" in e for e in v.check_timestamps("0:00:00 A\n5:00 B\n1:02:00 C\n"))
    assert v.check_timestamps("0:00:00 A\n0:05:00 B\n1:02:00 C\n") == []


def test_parse_stamp_lines():
    assert v.parse_stamp_lines("x\n1:02:03 Foo\n0:07 Bar") == [(3723, "Foo", True), (7, "Bar", False)]


# ---- tags
def test_tags_length_counting():
    assert v.tags_length(["ab", "cd"]) == 5                     # 2 + 2 + 1 comma
    assert v.tags_length(["a b", "cd"]) == 3 + 2 + 2 + 1        # quotes on a tag with a space


def test_tags_count_and_limit():
    tags = [f"tag number {i}" for i in range(10)]
    assert v.check_tags(tags, 10) == []
    assert v.check_tags(tags, 9)
    long = ["x" * 60] * 9
    assert v.tags_length(long) == 540 + 8
    assert any("limit 500" in e for e in v.check_tags(long, 9))


def test_tags_devanagari():
    assert v.check_tags(["हिंदी"], 1)


# ---- hashtags
def test_hashtags():
    assert v.check_hashtags(["#One", "#Two2", "#Three_"]) == []
    assert v.check_hashtags(["#One", "#Two"])
    assert v.check_hashtags(["#One", "#Two words", "#Three"])
    assert v.check_hashtags(["One", "#Two", "#Three"])
    assert v.check_hashtags(["#A", "#B", "#C"], "text #A #B") != []
    assert v.check_hashtags(["#A", "#B", "#C"], "text #A #B #C") == []


# ---- teaser
SRC = ["The old shepherd walked slowly across the empty field toward the river and sat down beneath a tree."]


def test_teaser_clean():
    assert v.check_teaser("Gaon mein ek ajeeb raaz hai jo dheere dheere khulta hai.", SRC) == []


def test_teaser_shared_run():
    errs = v.check_teaser("Wo kehta hai walked slowly across the empty field toward kuch", SRC)
    assert any("6 consecutive" in e for e in errs)
    # five shared words are allowed
    assert v.check_teaser("slowly across the empty field toward", SRC)   # exactly 6 -> flagged
    assert v.check_teaser("across the empty field toward", SRC) == []   # 5 words


def test_teaser_chapter_title():
    errs = v.check_teaser("Isme Madame Pualis ka zikr hai.", SRC, ["Madame Pualis", "Letter"])
    assert any("chapter title" in e for e in errs)
    # one-word titles are ordinary words and are ignored
    assert v.check_teaser("Ek letter ka raaz.", SRC, ["Letter"]) == []


def test_teaser_devanagari_and_empty():
    assert v.check_teaser("कहानी", SRC)
    assert v.check_teaser("  ", SRC)


# ---- comment question and hooks
def test_comment_question():
    assert v.check_comment_question("Aap kya sochte ho?") == []
    assert v.check_comment_question("")
    assert v.check_comment_question("a\nb")


@pytest.mark.parametrize("hook", ["THIS VILLAGE|IS *NOT NORMAL*", "WHO IS|*HERO?*", "LOTM 2|*BEGINS!*"])
def test_hook_ok(hook):
    assert v.check_hook(hook) == []


@pytest.mark.parametrize("hook,frag", [
    ("NO PIPE *HERE*", "|"),
    ("TWO LINES|NO ACCENT", "KEYWORD"),
    ("lower case|*HERE*", "CAPS"),
    ("ONE|*WORD*|THREE", "|"),
    ("A VERY LONG HOOK|WITH *TOO MANY* WORDS", "words"),
    ("कह|*कह*", "English"),
])
def test_hook_bad(hook, frag):
    assert any(frag in e for e in v.check_hook(hook))


def test_hooks_count_and_duplicates():
    h = ["A B|*C*", "D E|*F*", "G H|*I*"]
    assert v.check_hooks(h) == []
    assert v.check_hooks(h[:2])
    assert v.check_hooks([h[0], h[0], h[1]])
    assert v.check_hooks("nope")


# ---- placeholders
def test_find_placeholders():
    assert v.find_placeholders("Episode [N] <playlist link>") == ["[N]", "<playlist link>"]
    assert v.find_placeholders("clean text (Ch 1-10)") == []


# ---- whole pack
def pack(**kw):
    base = {"title": SERIES + " | Episode 2", "description": "x\n0:00 Intro\n0:30 A\n5:00 B\n#A #B #C",
            "tags": ["one tag", "two tag"], "hashtags": ["#A", "#B", "#C"], "teaser": "Ek ajeeb raaz.",
            "comment_question": "Kya lagta hai?", "thumbnail_hooks": ["A B|*C*", "D E|*F*", "G H|*I*"]}
    base.update(kw)
    return base


def run_pack(p):
    return v.validate_pack(p, series_phrase=SERIES, expected_tags=2, english_sources=SRC, chapter_titles=[])


def test_pack_ok():
    assert run_pack(pack()) == []


def test_pack_leftover_placeholder_everywhere():
    assert any("title: leftover" in e for e in run_pack(pack(title=SERIES + " [N]")))
    assert any("description: leftover" in e for e in run_pack(pack(description="0:00 A\n0:30 B\n5:00 C\n#A #B #C [X]")))
    assert any("tags: leftover" in e for e in run_pack(pack(tags=["a [V]", "b"])))


def test_ai_errors_split():
    errs = run_pack(pack(teaser="", title="Wrong"))
    assert any(e.startswith("title") for e in errs)
    assert v.ai_errors(errs) == ["teaser: empty"]
