"""novel qa: code checks, AI review (fake Claude CLI, stubbed Gemini), report, --fix-prompt (fixture book 'tide')."""

import json
import sys
from pathlib import Path

import pytest

from novel import qa as Q
from novel.commands import prompt_cmd
from novel.gemini import contract as C
from novel.gemini import importer as I
from novel.publish import meta as M
from novel.stages import episode_scenes as S
from novel.stages import episode_translate as T

from test_import import bible_reply, block, chapter_lines, counts, ep, run, setup_book

FAKE = Path(__file__).parent / "fakes" / "fake_claude.py"
CODE_KEYS = {"version", "chapter", "mode", "errors", "warnings", "forced", "checked_at", "hindi_sha256",
             "scenes_sha256"}
REVIEW_KEYS = {"version", "chapter", "verdict", "reviewer", "model", "findings", "reviewed_at", "hindi_sha256",
               "scenes_sha256"}
FINDING_KEYS = {"severity", "kind", "ref", "text", "suggest"}


# ---------------------------------------------------------------- fixture book with clean chapters

def clean_action(n):
    return (f"Picture {n}: Maren Vale stands in the lamp-lit room while the rain taps on the roof and the light "
            f"moves slowly over the beams and the floor.")


def clean_lines(count, chapter):
    lines = chapter_lines(count, chapter)
    for line in lines:
        if line["type"] == "image":
            line["action"] = clean_action(line["n"])
    return lines


@pytest.fixture
def qa_book(tmp_path, monkeypatch, capsys):
    bd = setup_book(tmp_path, monkeypatch)
    c = counts(bd)
    code, out = run(monkeypatch, capsys, bible_reply())
    assert code == 0, out
    for ch in (1, 2):
        code, out = run(monkeypatch, capsys, block("IMAGES", f"c{ch:04d}", clean_lines(c[ch], ch)))
        assert code == 0, out
    monkeypatch.setenv("NOVEL_CLAUDE_CMD", f'"{sys.executable}" "{FAKE}"')
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    monkeypatch.delenv("FAKE_CLAUDE_REPLIES", raising=False)
    return bd


def qa(capsys, *args, book="tide"):
    from novel.cli import main
    code = main(["qa", "--book", book, "--ep", "1", *args])
    cap = capsys.readouterr()
    return code, cap.out + cap.err


def code_doc(bd, n):
    return json.loads((ep(bd) / "qa" / f"{n:05d}.json").read_text(encoding="utf-8"))


def review_doc(bd, n):
    return json.loads((ep(bd) / "qa" / f"{n:05d}.review.json").read_text(encoding="utf-8"))


def kinds(doc, field):
    return [(x["kind"], x["ref"]) for x in doc[field]]


def edit_scenes(bd, n, fn):
    p = S.scenes_path(ep(bd), n)
    doc = json.loads(p.read_text(encoding="utf-8"))
    fn(doc)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def edit_bible(bd, name, fn):
    p = bd / "bible" / name
    doc = json.loads(p.read_text(encoding="utf-8"))
    fn(doc)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def relock(n):
    from types import SimpleNamespace
    T.cmd_lock(SimpleNamespace(episode=1, chapters=str(n)))


def finding(sev="error", kind="image_mismatch", ref="image 2", text="The picture shows a different moment.",
            suggest="Show the moment the window tells."):
    return {"severity": sev, "kind": kind, "ref": ref, "text": text, "suggest": suggest}


def claude_says(monkeypatch, tmp_path, *replies):
    f = tmp_path / "replies.json"
    f.write_text(json.dumps(list(replies)), encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_REPLIES", str(f))


def ok(findings, verdict=None):
    verdict = verdict or ("FIX" if any(isinstance(f, dict) and f.get("severity") == "error" for f in findings)
                          else "PASS")
    return {"kind": "ok", "payload": {"verdict": verdict, "findings": findings}}


def claude_calls(tmp_path):
    f = tmp_path / "claude.log"
    return len(f.read_text(encoding="utf-8").splitlines()) if f.exists() else 0


def gemini_says(monkeypatch, *texts):
    calls = []

    def fake(system, prompt, *, model, log):
        calls.append(prompt)
        t = texts[min(len(calls), len(texts)) - 1]
        if isinstance(t, Exception):
            raise t
        return t if isinstance(t, str) else json.dumps(t)

    monkeypatch.setattr(M, "call_gemini", fake)
    return calls


# ---------------------------------------------------------------- baseline and file formats

def test_clean_chapters_pass_with_exact_v2_keys(qa_book, capsys):
    code, out = qa(capsys, "--only-code")
    assert code == 0, out
    for n in (1, 2):
        doc = code_doc(qa_book, n)
        assert set(doc) == CODE_KEYS and doc["version"] == 2 and doc["chapter"] == n and doc["mode"] == "scenes"
        assert doc["errors"] == [] and doc["warnings"] == [], doc["warnings"]
        assert doc["forced"] is False and doc["hindi_sha256"] and doc["scenes_sha256"]
    assert "| 1 |" in out and "novel approve --chapters 1-2" in out
    assert not (ep(qa_book) / "qa" / "00001.review.json").exists()


def test_default_chapters_are_the_ones_with_a_scene_file(qa_book, capsys):
    S.scenes_path(ep(qa_book), 2).unlink()
    code, out = qa(capsys, "--only-code")
    assert code == 0, out
    assert "no scene file yet for chapters 2" in out
    assert (ep(qa_book) / "qa" / "00001.json").exists() and not (ep(qa_book) / "qa" / "00002.json").exists()


# ---------------------------------------------------------------- Hindi checks

def test_paragraph_with_digits_is_an_error_and_clean_text_is_not(qa_book, capsys):
    hf = ep(qa_book) / "hindi" / "00001.txt"
    text = hf.read_text(encoding="utf-8")
    assert "मैरेन बस सिर हिलाती है।" in text
    hf.write_text(text.replace("मैरेन बस सिर हिलाती है।", "मैरेन 5 बार सिर हिलाती है।"), encoding="utf-8")
    relock(1)
    code, out = qa(capsys, "--only-code", "--chapters", "1-2")
    assert code == 1
    d1, d2 = code_doc(qa_book, 1), code_doc(qa_book, 2)
    assert ("hindi_paragraph", "para 3") in kinds(d1, "errors")
    assert "digits" in next(e["text"] for e in d1["errors"] if e["kind"] == "hindi_paragraph")
    assert not [e for e in d2["errors"] if e["kind"] == "hindi_paragraph"]


def test_lock_that_no_longer_matches_is_an_error(qa_book, capsys):
    hf = ep(qa_book) / "hindi" / "00001.txt"
    hf.write_text(hf.read_text(encoding="utf-8") + "\nऔर एक पंक्ति।\n", encoding="utf-8")
    code, out = qa(capsys, "--only-code", "--chapters", "1-2")
    assert code == 1
    assert [e for e in code_doc(qa_book, 1)["errors"] if e["kind"] == "lock" and "changed after" in e["text"]]
    assert not [e for e in code_doc(qa_book, 2)["errors"] if e["kind"] == "lock"]


def test_unlocked_is_an_error_in_scenes_mode_and_a_warning_in_full_mode(qa_book, capsys, monkeypatch):
    lock = ep(qa_book) / "hindi.lock.json"
    data = json.loads(lock.read_text(encoding="utf-8"))
    data.pop("00001.txt")
    lock.write_text(json.dumps(data), encoding="utf-8")
    qa(capsys, "--only-code", "--chapters", "1")
    assert ("lock", "chapter") in kinds(code_doc(qa_book, 1), "errors")
    monkeypatch.setattr(I.Importer, "batch_for_chapter", lambda self, n: None)       # no batch, no lock: full mode
    qa(capsys, "--only-code", "--chapters", "1")
    d = code_doc(qa_book, 1)
    assert d["mode"] == "full" and ("lock", "chapter") in kinds(d, "warnings")
    assert d["errors"] == [], d["errors"]               # the scene checks still pass on the file's own hash


def test_known_name_missing_from_the_hindi_is_a_warning(qa_book, capsys):
    edit_bible(qa_book, "names.json", lambda d: d.update({"Harl": "ज़ज़ज़"}))
    qa(capsys, "--only-code", "--chapters", "1-2")
    w1 = code_doc(qa_book, 1)["warnings"]
    assert any(w["kind"] == "name_missing" and "Harl" in w["text"] for w in w1)
    english2 = next((qa_book / "chapters").glob("0002_*.txt")).read_text(encoding="utf-8")
    if "Harl" not in english2:
        assert not [w for w in code_doc(qa_book, 2)["warnings"] if w["kind"] == "name_missing"]


def test_short_hindi_is_a_possible_omission(qa_book, capsys):
    f = next((qa_book / "chapters").glob("0001_*.txt"))
    f.write_text(f.read_text(encoding="utf-8") + "\n\n" + ("A long extra passage about the harbour. " * 40),
                 encoding="utf-8")
    qa(capsys, "--only-code", "--chapters", "1-2")
    assert any(w["kind"] == "omission" and "possible omission" in w["text"] for w in code_doc(qa_book, 1)["warnings"])
    assert not [w for w in code_doc(qa_book, 2)["warnings"] if w["kind"] == "omission"]


# ---------------------------------------------------------------- scene file checks

def test_missing_old_and_broken_scene_files(qa_book, capsys):
    S.scenes_path(ep(qa_book), 2).unlink()
    edit_scenes(qa_book, 1, lambda d: d.update({"version": 1}))
    code, out = qa(capsys, "--only-code", "--chapters", "1-2")
    assert code == 1
    assert ("scenes_missing", "chapter") in kinds(code_doc(qa_book, 2), "errors")
    assert code_doc(qa_book, 2)["scenes_sha256"] is None
    assert ("scenes_version", "chapter") in kinds(code_doc(qa_book, 1), "errors")


def test_check_chapter_problem_and_hash_mismatch(qa_book, capsys):
    def plant(d):
        d["scenes"][1]["shot"] = "bogus"
        d["hindi_sha256"] = "0" * 64
    edit_scenes(qa_book, 1, plant)
    qa(capsys, "--only-code", "--chapters", "1-2")
    errs = kinds(code_doc(qa_book, 1), "errors")
    assert ("scene_check", "image 2") in errs and ("hash", "chapter") in errs
    assert code_doc(qa_book, 2)["errors"] == []


def test_cast_variant_and_location_are_checked_against_the_current_bible(qa_book, capsys):
    # the bible changes after the import: a variant's chapters move, a location disappears
    edit_bible(qa_book, "characters.json", lambda d: d["characters"]["maren_vale"]["variants"]["base"].update(
        {"chapters": "2-"}))
    edit_bible(qa_book, "locations.json", lambda d: d["locations"].pop("net_loft"))
    qa(capsys, "--only-code", "--chapters", "1-2")
    d1, d2 = code_doc(qa_book, 1), code_doc(qa_book, 2)
    assert any(e["kind"] == "bible_variant" and "covers chapters 2-" in e["text"] for e in d1["errors"])
    assert [e for e in d2["errors"] if e["kind"] == "bible_location"]
    assert not [e for e in d2["errors"] if e["kind"] == "bible_variant"]
    edit_bible(qa_book, "characters.json", lambda d: d["characters"].pop("tobias_reed"))
    qa(capsys, "--only-code", "--chapters", "2")
    assert any(e["kind"] == "bible_variant" and "tobias_reed" in e["text"] for e in code_doc(qa_book, 2)["errors"])


# ---------------------------------------------------------------- continuity, props, people, sizes

def test_same_place_and_time_but_different_light(qa_book, capsys):
    edit_scenes(qa_book, 1, lambda d: d["groups"]["g02"].update({"light": "harsh white noon glare"}))
    qa(capsys, "--only-code", "--chapters", "1-2")
    assert any(w["kind"] == "continuity" and w["ref"] == "group g02" and "different light" in w["text"]
               for w in code_doc(qa_book, 1)["warnings"])
    assert not [w for w in code_doc(qa_book, 2)["warnings"] if w["kind"] == "continuity"]


def test_group_interrupted_by_one_picture_and_resumed(qa_book, capsys):
    edit_scenes(qa_book, 1, lambda d: d["scenes"][1].update({"group": "g02"}))
    qa(capsys, "--only-code", "--chapters", "1-2")
    ws = code_doc(qa_book, 1)["warnings"]
    assert any(w["kind"] == "continuity" and w["ref"] == "image 2" and "resumes at image 3" in w["text"]
               and "check that the place really changes" in w["text"] for w in ws), ws
    assert not [w for w in code_doc(qa_book, 2)["warnings"] if w["kind"] == "continuity"]


def test_action_that_names_another_place(qa_book, capsys):
    edit_scenes(qa_book, 2, lambda d: d["scenes"][0].update(
        {"action": clean_action(1) + " Far below, the quay lights flicker.", "scene": clean_action(1)}))
    qa(capsys, "--only-code", "--chapters", "1-2")
    w = [w for w in code_doc(qa_book, 2)["warnings"] if w["kind"] == "continuity"]
    assert w and w[0]["ref"] == "image 1" and "quay" in w[0]["text"] and "Net Loft" in w[0]["text"]
    assert not [w for w in code_doc(qa_book, 1)["warnings"] if w["kind"] == "continuity"]


def test_signature_prop_in_hands_on_most_pictures(qa_book, capsys):
    def plant(d):
        for s in d["scenes"]:
            for c in s["cast"]:
                if c["key"] == "maren_vale":
                    c["hands"] = "holding her lantern up"
    edit_scenes(qa_book, 1, plant)
    qa(capsys, "--only-code", "--chapters", "1-2")
    w = [w for w in code_doc(qa_book, 1)["warnings"] if w["kind"] == "props"]
    assert w and "maren_vale" in w[0]["text"] and "lantern" in w[0]["text"] and " of " in w[0]["text"]
    assert not [w for w in code_doc(qa_book, 2)["warnings"] if w["kind"] == "props"]


def test_person_named_in_the_action_but_not_in_the_cast(qa_book, capsys):
    edit_scenes(qa_book, 1, lambda d: d["scenes"][0].update(
        {"action": clean_action(1) + " Old Harl watches from the boat.", "scene": clean_action(1)}))
    qa(capsys, "--only-code", "--chapters", "1-2")
    w = [w for w in code_doc(qa_book, 1)["warnings"] if w["kind"] == "people"]
    assert len(w) == 1 and w[0]["ref"] == "image 1" and "Old Harl" in w[0]["text"] \
        and "not in the cast" in w[0]["text"]
    assert not [w for w in code_doc(qa_book, 2)["warnings"] if w["kind"] == "people"]


def test_cast_member_never_named_is_not_a_finding(qa_book, capsys):
    # Tobias is in the cast of every even picture and never named in any action: no warning.
    qa(capsys, "--only-code", "--chapters", "1")
    assert code_doc(qa_book, 1)["warnings"] == []


def test_big_group_and_shot_variety_warnings(qa_book, capsys, monkeypatch):
    def plant(d):
        for s in d["scenes"][:3]:
            s["shot"] = "wide"
    edit_scenes(qa_book, 1, plant)
    qa(capsys, "--only-code", "--chapters", "1-2")
    assert any(w["kind"] == "shots" and "three times in a row" in w["text"] for w in code_doc(qa_book, 1)["warnings"])
    assert not [w for w in code_doc(qa_book, 2)["warnings"] if w["kind"] == "shots"]
    assert not [w for w in code_doc(qa_book, 2)["warnings"] if w["kind"] == "group_size"]
    monkeypatch.setattr(Q, "GROUP_WARN_AT", 3)
    qa(capsys, "--only-code", "--chapters", "1-2")
    assert [w for w in code_doc(qa_book, 2)["warnings"] if w["kind"] == "group_size"]
    r = Q.code_check(Q.Run("tide", 1), 2)
    assert r.info["pictures"] == counts(qa_book)[2] and r.info["groups"] == 2


# ---------------------------------------------------------------- saved files

def test_old_format_qa_files_move_to_legacy(qa_book, capsys):
    q = ep(qa_book) / "qa"
    q.mkdir(parents=True, exist_ok=True)
    (q / "00001.json").write_text(json.dumps({"chapter": 1, "ratio": 0.5}), encoding="utf-8")
    (q / "00001.attach.json").write_text("[]", encoding="utf-8")
    (q / "00002.json").write_text(json.dumps({"chapter": 2, "ratio": 0.6}), encoding="utf-8")
    qa(capsys, "--only-code", "--chapters", "1")
    assert json.loads((q / "legacy" / "00001.json").read_text(encoding="utf-8")) == {"chapter": 1, "ratio": 0.5}
    assert (q / "legacy" / "00001.attach.json").exists() and not (q / "00001.attach.json").exists()
    assert code_doc(qa_book, 1)["version"] == 2
    assert json.loads((q / "00002.json").read_text(encoding="utf-8"))["ratio"] == 0.6      # chapter 2 not run: untouched
    qa(capsys, "--only-code", "--chapters", "1")                       # a v2 file is never moved
    assert sorted(p.name for p in (q / "legacy").iterdir()) == ["00001.attach.json", "00001.json"]
    qa(capsys, "--only-code", "--chapters", "2")
    assert json.loads((q / "legacy" / "00002.json").read_text(encoding="utf-8"))["ratio"] == 0.6


def test_old_format_review_file_moves_to_legacy_too(qa_book, capsys, monkeypatch, tmp_path):
    q = ep(qa_book) / "qa"
    q.mkdir(parents=True, exist_ok=True)
    (q / "00001.review.json").write_text(json.dumps({"verdict": "PASS"}), encoding="utf-8")
    claude_says(monkeypatch, tmp_path, ok([]))
    qa(capsys, "--chapters", "1")
    assert (q / "legacy" / "00001.review.json").exists()
    assert review_doc(qa_book, 1)["version"] == 2


def test_forced_survives_an_unchanged_rerun_and_clears_after_a_change(qa_book, capsys):
    qa(capsys, "--only-code", "--chapters", "1")
    f = ep(qa_book) / "qa" / "00001.json"
    doc = code_doc(qa_book, 1)
    doc["forced"] = True
    f.write_text(json.dumps(doc), encoding="utf-8")
    qa(capsys, "--only-code", "--chapters", "1")
    assert code_doc(qa_book, 1)["forced"] is True
    edit_scenes(qa_book, 1, lambda d: d["scenes"][0].update({"action": clean_action(1) + " More.",
                                                              "scene": clean_action(1) + " More."}))
    qa(capsys, "--only-code", "--chapters", "1")
    assert code_doc(qa_book, 1)["forced"] is False


# ---------------------------------------------------------------- AI review

def test_review_happy_path_prompt_files_and_report(qa_book, capsys, monkeypatch, tmp_path):
    seen = []
    real = M.call_claude

    def spy(prompt, timeout=0):
        seen.append(prompt)
        return real(prompt, timeout=timeout)

    monkeypatch.setattr(M, "call_claude", spy)
    claude_says(monkeypatch, tmp_path, ok([
        finding("warn", "tone", "para 2", "Stiff phrasing.", "Say it the way people talk."),
        finding("error", "image_mismatch", "image 2", "Picture 2 shows Tobias, who is not in this window.",
                "Show Maren alone."),
        finding("error", "missing_plot", "para 3", "The bell is not told.", "Add the bell."),
    ]))
    code, out = qa(capsys, "--chapters", "1")
    assert code == 1, out
    rv = review_doc(qa_book, 1)
    assert set(rv) == REVIEW_KEYS and rv["verdict"] == "FIX" and rv["reviewer"] == "claude" and rv["model"]
    assert all(set(f) == FINDING_KEYS for f in rv["findings"]) and len(rv["findings"]) == 3
    assert rv["hindi_sha256"] == code_doc(qa_book, 1)["hindi_sha256"]
    assert rv["scenes_sha256"] == code_doc(qa_book, 1)["scenes_sha256"]
    mp = next(f for f in rv["findings"] if f["kind"] == "missing_plot")
    assert mp["severity"] == "warn"                    # the Hindi is locked in scenes mode
    assert rv["findings"][0]["severity"] == "error"    # errors first
    # what the reviewer was given
    p = seen[0]
    assert "[img 1]" in p and "[para 1]" in p and "Harbour Quay" in p and "Maren Vale" in p
    assert "Chapter 1: Harbour Morning" in p and "signature props" in p and "storm coat" in p
    assert "MODE: the Hindi is already final and locked" in p
    report = (ep(qa_book) / "qa" / "report.md").read_text(encoding="utf-8")
    assert "| Chapter | Pictures | Code checks | AI review | Overall |" in report
    assert "FIX (1 error, 2 warnings; claude)" in report
    assert "[AI error] image 2: Picture 2 shows Tobias" in report and "[AI warn] para 3:" in report
    assert "Fix: novel qa --fix-prompt 1" in report
    assert "Chapters to fix: 1" in report.split("## Next")[1]
    assert "Report:" in out and "report.md" in out


def test_all_good_review_is_pass_and_exit_zero(qa_book, capsys, monkeypatch, tmp_path):
    claude_says(monkeypatch, tmp_path, ok([]))
    code, out = qa(capsys, "--chapters", "1-2")
    assert code == 0, out
    assert review_doc(qa_book, 1)["verdict"] == "PASS" and review_doc(qa_book, 1)["findings"] == []
    assert "PASS (0 errors, 0 warnings; claude)" in out and "novel approve --chapters 1-2" in out
    assert " | PASS |" in out


def test_fallback_when_claude_fails_is_recorded(qa_book, capsys, monkeypatch, tmp_path):
    claude_says(monkeypatch, tmp_path, {"kind": "exit1"})
    calls = gemini_says(monkeypatch, {"verdict": "FIX", "findings": [finding("error", "continuity", "group g01",
                                                                                "The room changes.")]})
    code, out = qa(capsys, "--chapters", "1")
    assert code == 1, out
    rv = review_doc(qa_book, 1)
    assert rv["reviewer"] == "gemini" and rv["model"] == "gemini-3.5-flash-lite" and len(calls) == 1
    assert claude_calls(tmp_path) == 2                              # one try and one retry, then the fallback
    report = (ep(qa_book) / "qa" / "report.md").read_text(encoding="utf-8")
    assert "fallback: claude failed" in report and "answer is from gemini" in report


def test_gemini_as_reviewer_falls_back_to_claude(qa_book, capsys, monkeypatch, tmp_path):
    gemini_says(monkeypatch, M.AIFailure("Gemini: down"))
    claude_says(monkeypatch, tmp_path, ok([]))
    code, out = qa(capsys, "--chapters", "1", "--reviewer", "gemini")
    assert code == 0, out
    assert review_doc(qa_book, 1)["reviewer"] == "claude"


def test_malformed_findings_are_dropped_with_a_note(qa_book, capsys, monkeypatch, tmp_path):
    claude_says(monkeypatch, tmp_path, ok([
        finding("warn", "tone", "para 2", "Stiff."),
        {"severity": "huge", "kind": "tone", "ref": "para 1", "text": "x"},
        {"severity": "warn", "kind": "poetry", "ref": "para 1", "text": "x"},
        {"severity": "warn", "kind": "tone", "ref": "", "text": "x"},
        "just a string",
    ]))
    code, out = qa(capsys, "--chapters", "1")
    assert code == 0, out
    rv = review_doc(qa_book, 1)
    assert len(rv["findings"]) == 1 and rv["verdict"] == "PASS"
    report = (ep(qa_book) / "qa" / "report.md").read_text(encoding="utf-8")
    assert report.count("dropped finding") == 4


def test_unusable_reply_is_review_failed_never_pass(qa_book, capsys, monkeypatch, tmp_path):
    claude_says(monkeypatch, tmp_path, {"kind": "garbage"})
    gemini_says(monkeypatch, "I could not read that chapter.")
    code, out = qa(capsys, "--chapters", "1")
    assert code == 1, out
    assert not (ep(qa_book) / "qa" / "00001.review.json").exists()
    row = next(l for l in out.splitlines() if l.startswith("| 1 |"))
    assert "review failed" in row and "PASS (" not in row.split("|")[4]
    report = (ep(qa_book) / "qa" / "report.md").read_text(encoding="utf-8")
    assert "| review failed |" in report and "review failed: claude failed" in report


def test_reply_whose_findings_are_all_malformed_is_a_failed_review(qa_book, capsys, monkeypatch, tmp_path):
    claude_says(monkeypatch, tmp_path, ok([{"severity": "x", "kind": "y"}]))
    gemini_says(monkeypatch, {"findings": [{"nope": 1}]})
    code, out = qa(capsys, "--chapters", "1")
    assert code == 1 and "review failed" in out


def test_retry_after_one_bad_answer(qa_book, capsys, monkeypatch, tmp_path):
    claude_says(monkeypatch, tmp_path, {"kind": "model_garbage"}, ok([]))
    code, out = qa(capsys, "--chapters", "1")
    assert code == 0, out
    assert claude_calls(tmp_path) == 2 and review_doc(qa_book, 1)["reviewer"] == "claude"


def test_review_skipped_when_unchanged_and_redone_when_forced_or_patched(qa_book, capsys, monkeypatch, tmp_path):
    claude_says(monkeypatch, tmp_path, ok([]))
    qa(capsys, "--chapters", "1")
    assert claude_calls(tmp_path) == 1
    qa(capsys, "--chapters", "1")
    assert claude_calls(tmp_path) == 1
    assert "review kept" in (ep(qa_book) / "qa" / "report.md").read_text(encoding="utf-8")
    qa(capsys, "--chapters", "1", "--force-review")
    assert claude_calls(tmp_path) == 2
    # a PATCH through the real importer changes the scene file: the review is redone
    doc = json.loads(S.scenes_path(ep(qa_book), 1).read_text(encoding="utf-8"))
    _, il = I.Importer.doc_lines(doc)
    line = dict(il[3])
    line["action"] = clean_action(3) + " A gull lands on the rail."
    code, out = run(monkeypatch, capsys, block("PATCH", "c0001", [line]))
    assert code == 0, out
    qa(capsys, "--chapters", "1")
    assert claude_calls(tmp_path) == 3


def test_reviewer_none_and_only_code_make_no_call(qa_book, capsys, tmp_path):
    code, out = qa(capsys, "--chapters", "1-2", "--reviewer", "none")
    assert code == 0 and claude_calls(tmp_path) == 0
    assert "not reviewed" in out and "novel approve --chapters 1-2" in out and "no AI review yet" in out
    code, out = qa(capsys, "--chapters", "1", "--only-code")
    assert code == 0 and claude_calls(tmp_path) == 0
    assert not (ep(qa_book) / "qa" / "00001.review.json").exists()


def test_chapter_with_code_errors_is_fix_even_when_the_reviewer_passes(qa_book, capsys, monkeypatch, tmp_path):
    edit_scenes(qa_book, 1, lambda d: d["scenes"][1].update({"shot": "bogus"}))
    claude_says(monkeypatch, tmp_path, ok([]))
    code, out = qa(capsys, "--chapters", "1")
    assert code == 1
    assert "FIX (1 error" in out and "PASS (0 errors, 0 warnings; claude)" in out and "| FIX |" in out


def test_report_keeps_the_previous_one(qa_book, capsys):
    qa(capsys, "--only-code", "--chapters", "1")
    first = (ep(qa_book) / "qa" / "report.md").read_text(encoding="utf-8")
    qa(capsys, "--only-code", "--chapters", "1-2")
    assert (ep(qa_book) / "qa" / "report.prev.md").read_text(encoding="utf-8") == first
    assert "| 2 |" in (ep(qa_book) / "qa" / "report.md").read_text(encoding="utf-8")


def test_review_input_is_built_from_the_current_bible(qa_book):
    run_ = Q.Run("tide", 1)
    r = Q.code_check(run_, 2)
    system, user = Q.build_review_prompt(run_, r, Q.english_text(run_, r.ep_root, 2))
    assert "[img 1]" in user and "Net Loft" in user and "storm coat (chapters 2)" in user
    assert "Harbour Quay" not in user                       # chapter 2 never goes to the quay
    assert "at most 20 findings" in system


# ---------------------------------------------------------------- --fix-prompt

def planted_fix(qa_book, capsys):
    """A cast variant that does not cover chapter 1 (as if the bible changed): a code error on image 3."""
    edit_scenes(qa_book, 1, lambda d: d["scenes"][2]["cast"][0].update({"var": "storm_coat"}))
    code, out = qa(capsys, "--only-code", "--chapters", "1")
    assert code == 1
    return out


def test_fix_prompt_asks_for_a_patch_block_the_importer_can_read(qa_book, capsys, monkeypatch):
    planted_fix(qa_book, capsys)
    clip = []
    monkeypatch.setattr(prompt_cmd, "set_clipboard", lambda t: clip.append(t) or True)
    code, out = qa(capsys, "--fix-prompt", "1", "--copy")
    assert code == 0, out
    assert "BEGIN PATCH c0001 -> inbox/c0001.patch.jsonl" in out and "END PATCH c0001" in out
    assert "storm_coat" in out and "[image 3]" in out
    assert "novel import" in out and "novel qa --chapters 1" in out
    assert "on the clipboard" in out and clip and "BEGIN PATCH c0001" in clip[0]
    rep = C.split_reply(clip[0])
    assert [b.kind for b in rep.blocks] == ["PATCH"] and rep.blocks[0].chapter == 1 and rep.blocks[0].complete
    items, errs, _ = C.parse_jsonl(rep.blocks[0].lines)
    assert not errs
    assert [i.obj["n"] for i in items if i.obj["type"] == "image"] == [3]
    assert not [i for i in items if i.obj["type"] == "group"]              # the group did not change


def test_fix_prompt_for_a_flagged_group_includes_its_group_line_and_pictures(qa_book, capsys):
    qa(capsys, "--only-code", "--chapters", "1")
    f = ep(qa_book) / "qa" / "00001.json"
    doc = code_doc(qa_book, 1)
    doc["warnings"].append({"kind": "continuity", "ref": "group g02", "text": "the light changes"})
    f.write_text(json.dumps(doc), encoding="utf-8")
    code, out = qa(capsys, "--fix-prompt", "1", "--with-warnings")
    assert code == 0, out
    rep = C.split_reply(out)
    items, _, _ = C.parse_jsonl(rep.blocks[0].lines)
    n = counts(qa_book)[1]
    assert [i.obj["group"] for i in items if i.obj["type"] == "group"] == ["g02"]
    assert sorted(i.obj["n"] for i in items if i.obj["type"] == "image") == list(range(n // 2 + 1, n + 1))


def test_fix_prompt_needs_fresh_qa_and_something_to_fix(qa_book, capsys):
    code, out = qa(capsys, "--fix-prompt", "1")
    assert code == 1 and "no QA result yet" in out and "novel qa --chapters 1" in out
    qa(capsys, "--only-code", "--chapters", "1")
    code, out = qa(capsys, "--fix-prompt", "1")
    assert code == 1 and "nothing to fix" in out
    planted_fix(qa_book, capsys)
    edit_scenes(qa_book, 1, lambda d: d["scenes"][0].update({"action": clean_action(1) + " Changed.",
                                                              "scene": clean_action(1) + " Changed."}))
    code, out = qa(capsys, "--fix-prompt", "1")
    assert code == 1 and "out of date" in out


def test_fix_prompt_in_full_mode_also_asks_for_the_hindi(qa_book, capsys):
    planted_fix(qa_book, capsys)
    f = ep(qa_book) / "qa" / "00001.json"
    doc = code_doc(qa_book, 1)
    doc["mode"] = "full"
    doc["errors"].append({"kind": "hindi_paragraph", "ref": "para 3", "text": "contains digits"})
    f.write_text(json.dumps(doc), encoding="utf-8")
    code, out = qa(capsys, "--fix-prompt", "1")
    assert code == 0, out
    rep = C.split_reply(out)
    assert [b.kind for b in rep.blocks] == ["PATCH", "HINDI"] and all(b.complete for b in rep.blocks)
    assert "HINDI import is not built yet" in out


def test_fix_prompt_in_scenes_mode_never_asks_for_the_hindi(qa_book, capsys, monkeypatch, tmp_path):
    planted_fix(qa_book, capsys)
    claude_says(monkeypatch, tmp_path, ok([finding("warn", "missing_plot", "para 3", "The bell is not told.")]))
    qa(capsys, "--chapters", "1")
    code, out = qa(capsys, "--fix-prompt", "1", "--with-warnings")
    assert code == 0, out
    assert [b.kind for b in C.split_reply(out).blocks] == ["PATCH"]
    assert "not sent to Gemini (the Hindi is locked" in out


# ---------------------------------------------------------------- the full round trip

def test_round_trip_fix_patch_import_and_pass(qa_book, capsys, monkeypatch, tmp_path):
    edit_scenes(qa_book, 1, lambda d: d["scenes"][2]["cast"][0].update({"var": "storm_coat"}))
    claude_says(monkeypatch, tmp_path, ok([]))
    code, out = qa(capsys, "--chapters", "1")
    assert code == 1 and "FIX" in out
    code, out = qa(capsys, "--fix-prompt", "1")
    assert code == 0
    # Gemini's answer: the block of the message with the variant corrected
    rep = C.split_reply(out)
    lines = rep.blocks[0].lines
    fixed = [l.replace('"var":"storm_coat"', '"var":"base"') for l in lines]
    assert fixed != lines
    reply = "Here is the patch.\n\n" + block("PATCH", "c0001", "\n".join(fixed))
    code, out = run(monkeypatch, capsys, reply)
    assert code == 0, out
    assert "pictures to make again: 3" in out
    code, out = qa(capsys, "--chapters", "1")
    assert code == 0, out
    assert code_doc(qa_book, 1)["errors"] == [] and review_doc(qa_book, 1)["verdict"] == "PASS"
    assert claude_calls(tmp_path) == 2                                # the scene file changed: reviewed again
    assert " | PASS |" in out and "novel approve --chapters 1" in out


# ---------------------------------------------------------------- settings: omission ratio and parallel reviews

def set_qa_settings(bd, **values):
    import yaml
    f = bd / "book.yaml"
    doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    doc.setdefault("qa", {}).update(values)
    f.write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_omission_ratio_is_a_setting_default_045(qa_book, capsys):
    from novel.core import settings
    assert settings.get("qa.omission_ratio", book="tide") == 0.45 and settings.get("qa.parallel", book="tide") == 3
    assert Q.omission_ratio("tide") == 0.45
    hindi = (ep(qa_book) / "hindi" / "00001.txt").read_text(encoding="utf-8")
    english = next((qa_book / "chapters").glob("0001_*.txt")).read_text(encoding="utf-8")
    ratio = len("".join(hindi.split())) / len("".join(english.split()))
    set_qa_settings(qa_book, omission_ratio=round(ratio + 0.05, 2))       # just above this chapter's ratio
    qa(capsys, "--only-code", "--chapters", "1")
    assert any(w["kind"] == "omission" for w in code_doc(qa_book, 1)["warnings"])
    set_qa_settings(qa_book, omission_ratio=0.01)
    qa(capsys, "--only-code", "--chapters", "1")
    assert not [w for w in code_doc(qa_book, 1)["warnings"] if w["kind"] == "omission"]


def slow_reviewer(monkeypatch, delay=0.4, fail_chapter=None):
    import threading
    import time
    state = {"now": 0, "max": 0, "order": []}
    lock = threading.Lock()

    def fake(prompt, timeout=0):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        try:
            time.sleep(delay)
            n = 2 if "Chapter 2: The Net Loft" in prompt else 1
            with lock:
                state["order"].append(n)
            if n == fail_chapter:
                raise M.AIFailure("the reviewer broke")
            return json.dumps({"verdict": "PASS", "findings": []})
        finally:
            with lock:
                state["now"] -= 1

    monkeypatch.setattr(M, "call_claude", fake)
    monkeypatch.setattr(M, "call_gemini", lambda *a, **k: (_ for _ in ()).throw(M.AIFailure("no gemini")))
    return state


def test_reviews_run_in_parallel_and_report_keeps_chapter_order(qa_book, capsys, monkeypatch):
    state = slow_reviewer(monkeypatch)
    code, out = qa(capsys, "--chapters", "1-2")
    assert code == 0, out
    assert state["max"] == 2                        # both chapters at once (default 3 workers)
    assert review_doc(qa_book, 1)["verdict"] == "PASS" and review_doc(qa_book, 2)["verdict"] == "PASS"
    lines = out.splitlines()
    starts = [i for i, l in enumerate(lines) if "review of chapter" in l and "started" in l]
    ends = [i for i, l in enumerate(lines) if "review of chapter" in l and "ended" in l]
    assert len(starts) == 2 and len(ends) == 2 and max(starts) < min(ends)    # both began before one ended
    report = (ep(qa_book) / "qa" / "report.md").read_text(encoding="utf-8")
    assert report.index("| 1 |") < report.index("| 2 |")


def test_parallel_option_one_runs_one_at_a_time(qa_book, capsys, monkeypatch):
    state = slow_reviewer(monkeypatch, delay=0.05)
    code, out = qa(capsys, "--chapters", "1-2", "--parallel", "1")
    assert code == 0, out
    assert state["max"] == 1 and state["order"] == [1, 2]
    code, out = qa(capsys, "--chapters", "1-2", "--parallel", "0")
    assert code == 2 and "--parallel" in out


def test_parallel_setting_is_used_and_one_failure_cancels_nothing(qa_book, capsys, monkeypatch):
    set_qa_settings(qa_book, parallel=1)
    state = slow_reviewer(monkeypatch, delay=0.05, fail_chapter=1)
    code, out = qa(capsys, "--chapters", "1-2")
    assert state["max"] == 1                        # settings qa.parallel = 1
    assert code == 1 and "review failed" in out
    assert not (ep(qa_book) / "qa" / "00001.review.json").exists()
    assert review_doc(qa_book, 2)["verdict"] == "PASS"       # chapter 2 still reviewed and written
    set_qa_settings(qa_book, parallel=3)
    state = slow_reviewer(monkeypatch, delay=0.2, fail_chapter=1)
    code, out = qa(capsys, "--chapters", "1-2", "--force-review", "--parallel", "2")
    assert state["max"] == 2 and code == 1
