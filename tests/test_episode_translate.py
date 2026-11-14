"""Offline checks for the episode translator: no network, no API quota.

    py -m pytest tests
"""

import re

import pytest

from novel.stages import episode_translate as T
from novel.stages.spoken_numbers import english_number_words, hindi_number_words


def test_rejoins_split_sentences_but_keeps_dialogue_lead_ins():
    text = ("Lumian ran as fast as his fear allowed him to push himself\n\n"
            "to run even faster.\n\n"
            "In the bustling townsquare of Cordu,\n\n"
            "Reimund Greg caught sight of him.\n\n"
            "He continued,\n\n"
            '"Folks who have crossed paths..."')
    assert T.clean_paragraphs(text) == [
        "Lumian ran as fast as his fear allowed him to push himself to run even faster.",
        "In the bustling townsquare of Cordu, Reimund Greg caught sight of him.",
        "He continued,",
        '"Folks who have crossed paths..."',
    ]


def test_parse_reads_markers_in_order_and_recognises_skip():
    raw = "[1] चैप्टर 1: फॉरेनर्स\n[2] <skip>\n[3] वह [2] बार हंसा।\n[4] **<skip>**"
    got = T.parse(raw, {1, 2, 3, 4})
    assert got == {1: "चैप्टर 1: फॉरेनर्स", 2: T.SKIP, 3: "वह [2] बार हंसा।", 4: T.SKIP}


@pytest.mark.parametrize("body", [
    "(यह पैराग्राफ ऊपर वाले में शामिल कर दिया गया है)",
    "(खाली लाइन)",
    "[पिछले पैराग्राफ में जोड़ा गया]।",
])
def test_placeholder_notes_are_rejected(body):
    assert T.paragraph_problem(body) == "note"


@pytest.mark.parametrize("body", [
    "नोट: चौथी एपोक के बाद सब कुछ बदल गया।",       # real story text in chapter 1074
    "उसने कहा (धीरे से) कि वह लौटेगा।",
    T.SKIP,
])
def test_story_text_is_not_mistaken_for_a_note(body):
    assert T.paragraph_problem(body) is None


def test_latin_and_english_paragraphs_are_rejected():
    assert T.paragraph_problem("उसने Lumian को देखा।") == "latin"
    assert T.paragraph_problem("He walked into the room and sat down quietly there.") == "english"


def test_finish_keeps_hindi_numbers_and_fixes_spelling_drift():
    out = T.finish("ल्यूमियन ने तीन दिन में सिर्फ दो ब्रेड खाई, पाँच सौ लोग थे।")
    assert out == "लुमियान ने तीन दिन में सिर्फ दो ब्रेड खाई, पाँच सौ लोग थे।"


def test_digits_are_sent_back_to_be_written_in_words():
    assert T.paragraph_problem("उसने 3 दिन में 2 ब्रेड खाई।") == "digits"
    assert T.paragraph_problem("उसने ३ दिन इंतज़ार किया।") == "digits"


def test_english_numbers_are_reported_except_inside_names():
    gloss = {"Five Seas": "फाइव सीज़"}
    assert T.english_numbers("फाइव सीज़ में थ्री दिन", gloss) == ["थ्री"]


def test_ambiguous_words_are_reported_not_replaced():
    text = "चारों ओर अंधेरा था। दो लोग आए। मुझे जाने दो। एक आदमी।"
    assert english_number_words(text) == text
    assert hindi_number_words(text) == ["दो"]


def test_idiom_survives():
    text = "वह नौ दो ग्यारह हो गया।"
    assert english_number_words(text) == text
    assert hindi_number_words(text) == []


class FakeGemini:
    """Replays canned replies and records what was asked."""

    def __init__(self, replies):
        self.replies, self.asked, self.model, self.calls = list(replies), [], "fake", 0

    def ask(self, system, text, **kw):
        self.asked.append(text)
        self.calls += 1
        return self.replies.pop(0)

    def note(self, **kw):
        pass


def test_retell_redoes_notes_and_digits_only():
    paras = ["Chapter 1: Foreigners", "He saw seven doors.", "Nothing happened.", "Two men came."]
    g = FakeGemini([
        "[1] चैप्टर एक: फॉरेनर्स\n[2] उसने सात दरवाज़े देखे।\n[3] (खाली लाइन)\n[4] 2 लोग आए।",
        "[3] <skip>\n[4] दो लोग आए।",
    ])
    done, reasons = T.retell(g, paras, {})
    assert done == {1: "चैप्टर एक: फॉरेनर्स", 2: "उसने सात दरवाज़े देखे।",
                    3: T.SKIP, 4: "दो लोग आए।"}
    assert reasons == {"note": 1, "digits": 1}
    assert g.asked[1] == "[3] Nothing happened.\n\n[4] Two men came."


def test_retell_never_accepts_a_skipped_title():
    g = FakeGemini(["[1] <skip>\n[2] वह गया।", "[1] चैप्टर एक: फॉरेनर्स"])
    done, reasons = T.retell(g, ["Chapter 1: Foreigners", "He left."], {})
    assert done[1] == "चैप्टर एक: फॉरेनर्स"
    assert reasons == {"title skipped": 1}


def test_retell_refuses_too_many_skips():
    paras = ["Chapter 1: X"] + [f"Line {i}." for i in range(2, 21)]
    first = "[1] चैप्टर एक: एक्स\n" + "\n".join(f"[{i}] <skip>" for i in range(2, 21))
    second = "\n".join(f"[{i}] लाइन।" for i in range(2, 21))
    done, reasons = T.retell(FakeGemini([first, second]), paras, {})
    assert reasons["too many skips"] == 19
    assert all(done[i] == "लाइन।" for i in range(2, 21))


def test_retell_gives_up_loudly():
    replies = ["[1] (खाली लाइन)"] * (1 + T.REPAIR_ROUNDS)
    with pytest.raises(ValueError, match="still unusable"):
        T.retell(FakeGemini(replies), ["Chapter 1: X"], {})


def test_extract_repairs_double_encoded_accents_only():
    from novel.stages.extract import _fix_mojibake
    assert _fix_mojibake('DariÃ¨ge, CÅ“ur Ã‰picé, "Ã‡a va') == 'Dariège, Cœur Épicé, "Ça va'
    clean = "Dariège café — naïve, mother’s “quote”"
    assert _fix_mojibake(clean) == clean


def test_condense_only_accepts_shorter_clean_paragraphs():
    done = {1: "चैप्टर वन: फॉरेनर्स", 2: "वह बहुत ही धीरे धीरे चलता हुआ कमरे में आया।",
            3: "उसने देखा।", 4: "बाहर बहुत ज़ोर से बारिश हो रही थी और ठंड थी।"}
    g = FakeGemini(["[1] चैप्टर वन: फॉरेनर्स\n[2] वह धीरे से कमरे में आया।\n"
                    "[3] उसने बहुत ध्यान से चारों तरफ देखा।\n[4] (खाली लाइन)"])
    out, shortened = T.condense(g, done, cut=20)
    assert out[2] == "वह धीरे से कमरे में आया।"          # shorter: taken
    assert out[3] == "उसने देखा।"                       # longer: refused
    assert out[4] == done[4]                             # a note: refused
    assert shortened == 1


def test_a_single_latin_letter_is_caught():
    assert T.paragraph_problem("थर्टी s के मिडिल-एज आदमी।") == "latin"


# ---- scene pacing (episode_scenes.paced_windows): no image past ~7.5 s of narration

def test_paced_windows_cover_text_and_cap_length():
    from novel.stages.episode_scenes import paced_windows, window_text, CHARS_PER_SECOND, MAX_SECONDS
    long_para = " ".join(f"यह वाक्य नंबर {i} है और कहानी आगे बढ़ती है।" for i in range(12))
    paras = ["Title", "छोटा पैरा।", "दूसरा छोटा पैरा।", long_para, "आखिरी।"]
    wins = paced_windows(paras)
    joined = "".join(t for w in wins for _, t in window_text(paras, w)).replace(" ", "")
    assert joined == "".join(paras[1:]).replace(" ", "")
    for w in wins:
        assert sum(len(t) for _, t in window_text(paras, w)) <= MAX_SECONDS * CHARS_PER_SECOND + 1
    assert wins[0][:3] == (1, 0, 2)                    # two short paragraphs share one image
    assert sum(1 for w in wins if w[0] == 3) >= 2      # the long one gets several


def test_legacy_scene_files_keep_22_windows():
    from novel.stages.episode_scenes import chapter_windows, windows
    paras = ["Title"] + [f"para {i} " * 20 for i in range(30)]
    assert [(a, b) for a, _, b, _ in chapter_windows(paras, None)] == windows(paras)
