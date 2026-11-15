"""Panel captions (panel_captions): what text is shown in which panel when.

Made-up data only: a fake text measure (22 px per character) and hand-built panel plans.
"""

import random

from novel.stages import panel_captions as C

PX = 22
EPS = 6e-4          # times are rounded to 3 decimals


def measure(text):
    return PX * len(text)


def panel(k, w=1200, shape="wide", h=1012):
    return {"k": k, "w": w, "h": h, "shape": shape}


def plan(cuts, shapes=None, widths=None):
    n = len(cuts) - 1
    shapes = shapes or ["wide"] * n
    widths = widths or [1200] * n
    return {"cuts": cuts, "panels": [panel(k, widths[k], shapes[k]) for k in range(n)]}


def line(para, en, start, end):
    return {"para": para, "en": en, "start": start, "end": end}


def words_of(n, prefix="w"):
    return [f"{prefix}{i}" for i in range(n)]


# ------------------------------------------------------------------ wrap


def test_wrap_respects_width():
    ws = "one two three four five six seven".split()
    width = 12 * PX
    lines = C.wrap(ws, width, measure)
    assert [w for ln in lines for w in ln.split()] == ws
    assert all(measure(ln) <= width for ln in lines)
    assert len(lines) > 1


def test_wrap_single_line_and_empty():
    assert C.wrap(["a", "b"], 1000, measure) == ["a b"]
    assert C.wrap([], 1000, measure) == []


def test_wrap_long_word_gets_own_line_never_split():
    lines = C.wrap(["a", "extraordinarily", "b"], 5 * PX, measure)
    assert lines == ["a", "extraordinarily", "b"]


# ------------------------------------------------------------------ split_chunks


def test_split_chunks_respects_limits():
    ws = words_of(40)
    width = 20 * PX
    chunks = C.split_chunks(ws, width, 2, 6, measure)
    assert [w for c in chunks for w in c] == ws
    for c in chunks:
        assert len(c) <= 6
        assert len(C.wrap(c, width, measure)) <= 2


def test_split_chunks_max_lines_binds_before_max_words():
    ws = words_of(12, "wwww")                         # 5-char words: 4 per line of 20 chars
    chunks = C.split_chunks(ws, 20 * PX, 1, 9, measure)
    assert all(len(c) <= 3 for c in chunks)           # "wwww0 wwww1 wwww2" = 17 chars; four would be 23
    assert all(len(C.wrap(c, 20 * PX, measure)) == 1 for c in chunks)


def test_split_chunks_prefers_punctuation():
    ws = "He walked, slowly down the lane and then turned back home again".split()
    chunks = C.split_chunks(ws, 1000 * PX, 2, 6, measure)
    # Greedy would take six words ("... the"); the comma after word 2 is too early (< half), so it holds.
    assert chunks[0] == ws[:6]
    ws = "He walked on and on, slowly down the lane and then turned back".split()
    chunks = C.split_chunks(ws, 1000 * PX, 2, 8, measure)
    assert chunks[0][-1] == "on,"                     # 5 words >= half of 8: end right after the comma


def test_split_chunks_punctuation_too_early_is_ignored():
    ws = "Yes, he walked down the lane and then turned back".split()
    chunks = C.split_chunks(ws, 1000 * PX, 2, 8, measure)
    assert len(chunks[0]) == 8                        # a 1-word chunk ending in a comma is not preferred


def test_split_chunks_no_orphan_last_word():
    ws = words_of(7)
    chunks = C.split_chunks(ws, 1000 * PX, 2, 6, measure)
    assert [len(c) for c in chunks] == [5, 2]
    assert [w for c in chunks for w in c] == ws


def test_split_chunks_orphan_stays_when_move_does_not_fit():
    ws = words_of(7)
    chunks = C.split_chunks(ws, 1000 * PX, 2, 6, measure)
    assert len(chunks[-1]) == 2
    # previous chunk has only two words: nothing to move
    ws = ["aaa", "bbb", "ccc"]
    assert C.split_chunks(ws, 1000 * PX, 2, 2, measure) == [["aaa", "bbb"], ["ccc"]]


def test_split_chunks_deterministic_and_edge_cases():
    ws = words_of(25)
    assert C.split_chunks(ws, 15 * PX, 2, 5, measure) == C.split_chunks(ws, 15 * PX, 2, 5, measure)
    assert C.split_chunks([], 100, 2, 5, measure) == []
    assert C.split_chunks(["supercalifragilistic"], 3 * PX, 2, 5, measure) == [["supercalifragilistic"]]


# ------------------------------------------------------------------ cues


def test_portrait_chunks_are_short_wide_are_longer():
    text = " ".join(words_of(20))
    ln = [line(1, text, 0.0, 20.0)]
    wide = C.cues(ln, plan([0.0, 30.0], ["wide"]), measure, lead=0.0, title_end=0.0)
    port = C.cues(ln, plan([0.0, 30.0], ["portrait"], [800]), measure, lead=0.0, title_end=0.0)
    assert max(len(c["text"].split()) for c in port) <= 4
    assert max(len(c["text"].split()) for c in wide) > 4
    assert max(len(c["text"].split()) for c in wide) <= 9
    assert len(port) > len(wide)


def test_sentence_spanning_a_cut_is_split_across_panels():
    pl = plan([0.0, 10.0, 20.0])
    en = "aaaa bbbb cccc dddd eeee ffff gggg hhhh"
    got = C.cues([line(1, en, 6.0, 14.0)], pl, measure, lead=0.0, title_end=0.0)
    assert {c["k"] for c in got} == {0, 1}
    assert " ".join(c["text"] for c in got) == en
    for c in got:
        assert pl["cuts"][c["k"]] - EPS <= c["t0"] < c["t1"] <= pl["cuts"][c["k"] + 1] + EPS
    first = [c for c in got if c["k"] == 0]
    assert first[0]["text"].startswith("aaaa")
    assert max(c["t1"] for c in first) <= 10.0 + EPS
    assert min(c["t0"] for c in got if c["k"] == 1) >= 10.0 - EPS
    # half the characters on each side of the cut: words split evenly
    assert "dddd" in first[-1]["text"] or "dddd" in got[len(first)]["text"]


def test_lead_offsets_narration_time():
    pl = plan([0.0, 10.0, 20.0])
    got = C.cues([line(1, "hello there friend", 1.0, 4.0)], pl, measure, lead=5.0, title_end=0.0)
    assert len(got) == 1 and got[0]["k"] == 0
    assert abs(got[0]["t0"] - 6.0) < EPS and abs(got[0]["t1"] - 9.0) < EPS
    assert got[0]["text"] == "hello there friend"
    assert got[0]["lines"] == ["hello there friend"]


def test_para_zero_and_empty_text_skipped():
    pl = plan([0.0, 20.0])
    ln = [line(0, "Chapter One", 0.0, 2.0), line(1, "", 2.0, 4.0), line(1, "   ", 4.0, 6.0),
          line(1, "Real text here", 6.0, 9.0)]
    got = C.cues(ln, pl, measure, lead=0.0, title_end=0.0)
    assert [c["text"] for c in got] == ["Real text here"]


def test_title_end_clips_and_min_seconds_drops():
    pl = plan([0.0, 30.0])
    ln = [line(1, "one two three four", 1.0, 5.0),        # straddles title_end = 3
          line(1, "too late", 5.0, 5.2),                   # 0.2 s: under MIN_SECONDS
          line(1, "hidden", 0.0, 2.0)]                     # wholly under the title card
    got = C.cues(ln, pl, measure, lead=0.0, title_end=3.0)
    assert all(c["t0"] >= 3.0 for c in got)
    assert [c["text"] for c in got] == ["one two three four"]
    assert abs(got[0]["t0"] - 3.0) < EPS and abs(got[0]["t1"] - 5.0) < EPS


def test_chunks_shorter_than_min_seconds_are_dropped_not_stretched():
    pl = plan([0.0, 30.0])
    # A portrait panel (4-word chunks): 20 words in 1.0 s makes 0.2 s chunks, all under MIN_SECONDS.
    pl = plan([0.0, 30.0], ["portrait"], [800])
    ln = [line(1, " ".join(words_of(20)), 2.0, 3.0)]
    assert C.cues(ln, pl, measure, lead=0.0, title_end=0.0) == []
    # Mixed: a long sentence squeezed into 1.6 s keeps only the chunks that got 0.35 s or more.
    mixed = C.cues([line(1, " ".join(words_of(30)), 2.0, 3.6)], plan([0.0, 30.0]), measure,
                   lead=0.0, title_end=0.0)
    assert all(c["t1"] - c["t0"] >= C.MIN_SECONDS - EPS for c in mixed)


def test_overlapping_sentences_never_overlap_cues():
    pl = plan([0.0, 30.0])
    ln = [line(1, "first sentence of text", 1.0, 5.0), line(2, "second sentence of text", 4.0, 8.0)]
    got = C.cues(ln, pl, measure, lead=0.0, title_end=0.0)
    assert len(got) == 2
    assert got[0]["t1"] <= got[1]["t0"]


def test_cue_fields_and_lines_fit_panel():
    pl = plan([0.0, 40.0], ["square"], [500])
    text = " ".join(f"word{i}" for i in range(12))
    got = C.cues([line(1, text, 1.0, 30.0)], pl, measure, lead=0.0, title_end=0.0)
    assert got
    for c in got:
        assert set(c) == {"k", "t0", "t1", "lines", "text"}
        assert " ".join(c["lines"]) == c["text"]
        assert len(c["lines"]) <= 2
        assert all(measure(ln) <= C.BOX_SHARE * 500 for ln in c["lines"])
        assert c["t0"] == round(c["t0"], 3) and c["t1"] == round(c["t1"], 3)


def test_randomized_cues_sorted_nonoverlapping_inside_panels():
    rng = random.Random(1234)
    shapes = list(C.LIMITS)
    for _ in range(25):
        n = rng.randint(2, 6)
        cuts = [0.0]
        for _ in range(n):
            cuts.append(cuts[-1] + rng.uniform(3, 15))
        pl = plan(cuts, [rng.choice(shapes) for _ in range(n)], [rng.randint(500, 1800) for _ in range(n)])
        lead, t, ln = 2.0, 0.0, [line(0, "Heading", 0.0, 1.5)]
        while lead + t < cuts[-1] - 6:
            dur = rng.uniform(0.8, 7.0)
            en = " ".join("x" * rng.randint(1, 9) + rng.choice(["", "", ",", "."]) for _ in range(rng.randint(1, 25)))
            ln.append(line(rng.randint(1, 9), en, t, t + dur))
            t += dur + rng.uniform(-0.5, 0.8)             # sometimes overlapping, sometimes gaps
        title_end = rng.uniform(0, 4)
        got = C.cues(ln, pl, measure, lead=lead, title_end=title_end)
        for a, b in zip(got, got[1:]):
            assert a["t0"] <= b["t0"]
            assert a["t1"] <= b["t0"]
        for c in got:
            k = c["k"]
            assert c["t1"] - c["t0"] >= C.MIN_SECONDS - EPS
            assert c["t0"] >= title_end - EPS
            assert cuts[k] - EPS <= c["t0"] and c["t1"] <= cuts[k + 1] + EPS
            max_lines, max_words = C.LIMITS[pl["panels"][k]["shape"]]
            assert len(c["lines"]) <= max_lines or len(c["text"].split()) == 1
            assert len(c["text"].split()) <= max_words


# ------------------------------------------------------------------ at


def test_at_finds_cue_and_none_in_gaps():
    cs = [{"k": 0, "t0": 1.0, "t1": 2.0, "lines": ["a"], "text": "a"},
          {"k": 0, "t0": 2.0, "t1": 3.0, "lines": ["b"], "text": "b"},
          {"k": 1, "t0": 5.0, "t1": 6.5, "lines": ["c"], "text": "c"}]
    assert C.at(cs, 0.5) is None
    assert C.at(cs, 1.0)["text"] == "a"
    assert C.at(cs, 1.999)["text"] == "a"
    assert C.at(cs, 2.0)["text"] == "b"          # t1 is exclusive, the next cue starts there
    assert C.at(cs, 3.0) is None
    assert C.at(cs, 4.9) is None
    assert C.at(cs, 5.0)["text"] == "c"
    assert C.at(cs, 6.5) is None
    assert C.at(cs, 99.0) is None
    assert C.at([], 1.0) is None


def test_at_agrees_with_linear_scan_on_real_cues():
    pl = plan([0.0, 12.0, 25.0])
    ln = [line(i, f"sentence number {i} goes on", i * 2.5, i * 2.5 + 2.0) for i in range(1, 9)]
    got = C.cues(ln, pl, measure, lead=1.0, title_end=0.0)
    for i in range(0, 300):
        t = i / 10
        want = next((c for c in got if c["t0"] <= t < c["t1"]), None)
        assert C.at(got, t) == want


# ------------------------------------------------------------------ wrap: balanced two lines


def test_wrap_two_lines_are_balanced_not_one_stray_word():
    ws = ["aa", "bb", "cc", "dd", "ee", "ff"]
    width = 14 * PX                                    # greedy: "aa bb cc dd ee" / "ff"
    assert C.wrap(ws, width, measure) == ["aa bb cc", "dd ee ff"]
    ws = ["aaaaaaaa", "bb", "cc"]
    assert C.wrap(ws, 11 * PX, measure) == ["aaaaaaaa", "bb cc"]     # greedy: "aaaaaaaa bb" / "cc"


def test_wrap_balanced_split_is_the_narrowest_possible():
    rng = random.Random(7)
    checked = 0
    for _ in range(300):
        ws = ["x" * rng.randint(1, 9) for _ in range(rng.randint(3, 10))]
        width = rng.randint(8, 40) * PX
        got = C.wrap(ws, width, measure)
        assert [w for ln in got for w in ln.split()] == ws          # nothing lost, order kept
        if len(got) != 2 or any(measure(w) > width for w in ws):
            continue
        checked += 1
        assert all(measure(ln) <= width for ln in got)
        best = min(max(measure(" ".join(ws[:i])), measure(" ".join(ws[i:]))) for i in range(1, len(ws)))
        assert max(measure(ln) for ln in got) == best
    assert checked > 30


def test_wrap_two_words_and_three_lines_are_left_greedy():
    assert C.wrap(["aaaa", "bbbbbbbb"], 9 * PX, measure) == ["aaaa", "bbbbbbbb"]     # two words: nothing to balance
    ws = ["aaaa"] * 5
    assert C.wrap(ws, 10 * PX, measure) == ["aaaa aaaa", "aaaa aaaa", "aaaa"]       # three lines: plain greedy


# ------------------------------------------------------------------ split_chunks: soft words


def test_split_chunks_does_not_end_on_a_soft_word():
    ws = "one two three the four five six seven eight".split()
    chunks = C.split_chunks(ws, 1000 * PX, 2, 4, measure)
    assert chunks == [["one", "two", "three"], ["the", "four", "five", "six"], ["seven", "eight"]]
    assert "the" in C.SOFT and "of" in C.SOFT


def test_split_chunks_soft_word_is_case_insensitive():
    ws = "one two three The four five six seven eight".split()
    chunks = C.split_chunks(ws, 1000 * PX, 2, 4, measure)
    assert chunks[0] == ["one", "two", "three"] and chunks[1][0] == "The"


def test_split_chunks_soft_word_with_punctuation_may_end_a_chunk():
    ws = "one two three the, four five six seven".split()
    chunks = C.split_chunks(ws, 1000 * PX, 2, 4, measure)
    assert chunks[0] == ["one", "two", "three", "the,"]


def test_split_chunks_soft_word_may_end_the_last_chunk_or_stand_alone():
    assert C.split_chunks(["one", "two", "three", "of"], 1000 * PX, 2, 9, measure) == [["one", "two", "three", "of"]]
    assert C.split_chunks(["a", "b", "c"], 1000 * PX, 2, 1, measure) == [["a"], ["b"], ["c"]]


def test_split_chunks_soft_rule_survives_the_orphan_top_up():
    # [a of b] / [z]: z is an orphan, so b moves to it, which would leave the first chunk ending on "of".
    chunks = C.split_chunks(["a", "of", "b", "z"], 1000 * PX, 2, 3, measure)
    assert [w for c in chunks for w in c] == ["a", "of", "b", "z"]
    for c in chunks[:-1]:
        assert not (len(c) >= 2 and c[-1][-1] not in C.BREAK_CHARS and c[-1].lower() in C.SOFT), chunks


def test_split_chunks_randomized_no_soft_ending_before_the_last_two_chunks():
    # (the second-to-last chunk is excluded: the orphan top-up can leave it soft, see the test above)
    rng = random.Random(99)
    pool = ["the", "of", "and", "his", "walked", "river", "stone", "dark", "to", "light", "winter", "road"]
    for _ in range(300):
        ws = [rng.choice(pool) + rng.choice(["", "", "", ","]) for _ in range(rng.randint(3, 25))]
        chunks = C.split_chunks(ws, rng.randint(12, 60) * PX, 2, rng.randint(2, 9), measure)
        assert [w for c in chunks for w in c] == ws
        for c in chunks[:-2]:
            assert not (len(c) >= 2 and c[-1][-1] not in C.BREAK_CHARS and c[-1].lower() in C.SOFT), (ws, chunks)


# ------------------------------------------------------------------ mark_fades


def cue_(k, t0, t1):
    return {"k": k, "t0": t0, "t1": t1, "lines": ["x"], "text": "x"}


def test_mark_fades_swap_in_same_panel_does_not_fade():
    cs = C.mark_fades([cue_(0, 1.0, 2.0), cue_(0, 2.0, 3.0), cue_(0, 3.04, 4.0), cue_(0, 4.5, 5.0)])
    assert [c["fade_out"] for c in cs] == [False, False, True, True]      # 3.0 -> 3.04 swaps; 4.0 -> 4.5 is a gap


def test_mark_fades_gap_over_fade_gap_fades():
    cs = C.mark_fades([cue_(0, 1.0, 2.0), cue_(0, 2.0 + C.FADE_GAP + 0.01, 3.0)])
    assert cs[0]["fade_out"] is True
    cs = C.mark_fades([cue_(0, 1.0, 2.0), cue_(0, 2.0 + C.FADE_GAP - 0.01, 3.0)])
    assert cs[0]["fade_out"] is False


def test_mark_fades_other_panel_and_last_cue_fade():
    cs = C.mark_fades([cue_(0, 1.0, 2.0), cue_(1, 2.0, 3.0), cue_(1, 3.0, 4.0)])
    assert [c["fade_out"] for c in cs] == [True, False, True]       # the camera moved on; the last one fades out


def test_mark_fades_returns_same_list_and_handles_empty_and_single():
    cs = [cue_(0, 1.0, 2.0)]
    assert C.mark_fades(cs) is cs and cs[0]["fade_out"] is True
    assert C.mark_fades([]) == []


def test_mark_fades_on_real_cues_chunks_of_one_sentence_swap():
    pl = plan([0.0, 40.0])
    got = C.mark_fades(C.cues([line(1, " ".join(words_of(30)), 1.0, 25.0)], pl, measure, lead=0.0, title_end=0.0))
    assert len(got) >= 3
    assert [c["fade_out"] for c in got] == [False] * (len(got) - 1) + [True]
