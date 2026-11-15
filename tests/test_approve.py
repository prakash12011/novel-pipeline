"""novel approve and novel sheet (fixture book 'tide' imported with the real importer; temp NOVEL_HOME;
no browser, no network: the runner is exercised with tests/fake_gen.mjs)."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from novel import bible as B
from novel import prompts as P
from novel.cli import main as novel_main
from novel.stages import episode as E
from novel.stages import episode_scenes as S

from test_import import counts, ep, full_run, setup_book

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "https://flow.google.com/project/abc123"
SETTINGS = {"images": {"flow_project": PROJECT, "flow_model": "Nano Banana Pro"}}


@pytest.fixture
def tide(tmp_path, monkeypatch, capsys):
    bd = setup_book(tmp_path, monkeypatch, settings=SETTINGS)
    full_run(monkeypatch, capsys, bd)
    capsys.readouterr()
    return bd


def approve(capsys, *args, book="tide"):
    code = novel_main(["approve", "--book", book, "--ep", "1", *args])
    cap = capsys.readouterr()
    return code, cap.out + cap.err


def write_qa(bd, n, errors=(), verdict=None, version=2):
    q = ep(bd) / "qa"
    q.mkdir(exist_ok=True)
    doc = {"chapter": n, "errors": list(errors), "warnings": [], "forced": False, "checked_at": "2026-11-13T00:00:00"}
    if version:
        doc["version"] = version
    (q / f"{n:05d}.json").write_text(json.dumps(doc), encoding="utf-8")
    if verdict:
        (q / f"{n:05d}.review.json").write_text(json.dumps({"verdict": verdict}), encoding="utf-8")


def passing(bd, *chapters):
    for n in chapters:
        write_qa(bd, n)


def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
            for p in sorted(root.rglob("*"))}


def plan_of(bd) -> dict:
    return json.loads((ep(bd) / "flow" / "plan.json").read_text(encoding="utf-8"))


def lock_of(bd) -> dict:
    return json.loads((ep(bd) / "hindi.lock.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- QA gate

def test_refused_without_qa_and_nothing_written(tide, capsys):
    before = tree(ep(tide))
    code, out = approve(capsys, "--chapters", "1")
    assert code == 1
    assert "REFUSED" in out and "novel qa --chapters 1" in out
    assert tree(ep(tide)) == before
    assert not (ep(tide) / "flow").exists()


def test_force_approves_and_marks_the_qa_file(tide, capsys):
    code, out = approve(capsys, "--chapters", "1", "--force")
    assert code == 0, out
    qa = json.loads((ep(tide) / "qa" / "00001.json").read_text(encoding="utf-8"))
    assert qa["version"] == 2 and qa["forced"] is True and qa["forced_at"] and qa["chapter"] == 1
    assert plan_of(tide)["shots"]


def test_force_keeps_the_errors_of_an_existing_qa_file(tide, capsys):
    write_qa(tide, 1, errors=["scene 3: something"])
    code, out = approve(capsys, "--chapters", "1")
    assert code == 1 and "QA found 1 error" in out
    code, out = approve(capsys, "--chapters", "1", "--force")
    assert code == 0, out
    qa = json.loads((ep(tide) / "qa" / "00001.json").read_text(encoding="utf-8"))
    assert qa["forced"] is True and qa["errors"] == ["scene 3: something"]


def test_passing_qa_needs_no_force_and_is_not_marked(tide, capsys):
    passing(tide, 1)
    code, out = approve(capsys, "--chapters", "1")
    assert code == 0, out
    assert json.loads((ep(tide) / "qa" / "00001.json").read_text(encoding="utf-8"))["forced"] is False


def test_fix_verdict_refuses(tide, capsys):
    write_qa(tide, 1, verdict="FIX")
    code, out = approve(capsys, "--chapters", "1")
    assert code == 1 and "FIX" in out and "novel qa --chapters 1" in out
    assert not (ep(tide) / "flow").exists()


def test_old_format_qa_file_counts_as_not_run(tide, capsys):
    write_qa(tide, 1, version=None)
    code, out = approve(capsys, "--chapters", "1")
    assert code == 1 and "old-format" in out
    code, out = approve(capsys, "--chapters", "1", "--force")
    assert code == 0
    assert (ep(tide) / "qa" / "00001.old-format.json").exists()        # the leftover is kept, not destroyed
    assert json.loads((ep(tide) / "qa" / "00001.json").read_text(encoding="utf-8"))["version"] == 2


# ---------------------------------------------------------------- lock and scene file

def test_changed_hindi_after_lock_refuses_with_the_fix(tide, capsys):
    passing(tide, 1)
    f = ep(tide) / "hindi" / "00001.txt"
    f.write_text(f.read_text(encoding="utf-8") + "\nएक और पंक्ति।\n", encoding="utf-8")
    code, out = approve(capsys, "--chapters", "1")
    assert code == 1
    assert "changed after it was locked" in out and "hindi.lock.json" in out and "novel approve --chapters 1" in out
    assert not (ep(tide) / "flow").exists()


def test_unlocked_chapter_gets_locked(tide, capsys):
    passing(tide, 1)
    lock = lock_of(tide)
    del lock["00001.txt"]
    (ep(tide) / "hindi.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    before = tree(ep(tide))
    code, out = approve(capsys, "--chapters", "1", "--dry-run")
    assert code == 0 and "would be locked" in out and tree(ep(tide)) == before
    code, out = approve(capsys, "--chapters", "1")
    assert code == 0 and "locked now" in out
    assert lock_of(tide)["00001.txt"] == E.sha256(ep(tide) / "hindi" / "00001.txt")
    assert "00002.txt" in lock_of(tide)               # the other chapter's lock is untouched


def test_check_lock_message_names_the_new_command(tide):
    lock = lock_of(tide)
    del lock["00001.txt"]
    (ep(tide) / "hindi.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        E.check_lock(ep(tide), 1)
    assert "novel approve --chapters 1" in str(e.value) and "abpipe" not in str(e.value)


def test_v1_scene_file_is_refused(tide, capsys):
    passing(tide, 1)
    f = S.scenes_path(ep(tide), 1)
    doc = json.loads(f.read_text(encoding="utf-8"))
    doc["version"] = 1
    f.write_text(json.dumps(doc), encoding="utf-8")
    code, out = approve(capsys, "--chapters", "1")
    assert code == 1 and "v1 scene file" in out and "new flow" in out


def test_missing_scene_file_is_refused_other_chapter_proceeds(tide, capsys):
    passing(tide, 1, 2)
    S.scenes_path(ep(tide), 1).unlink()
    code, out = approve(capsys, "--chapters", "1-2")
    assert code == 1
    assert "chapter 1: REFUSED" in out and "chapter 2:" in out and "REFUSED" not in out.split("chapter 2:")[1]
    ids = {s["id"][:5] for s in plan_of(tide)["shots"]}
    assert ids == {"c0002"}


def test_scenes_for_a_different_text_are_refused(tide, capsys):
    passing(tide, 1)
    f = S.scenes_path(ep(tide), 1)
    doc = json.loads(f.read_text(encoding="utf-8"))
    doc["hindi_sha256"] = "0" * 64
    f.write_text(json.dumps(doc), encoding="utf-8")
    code, out = approve(capsys, "--chapters", "1")
    assert code == 1 and "different Hindi text" in out


# ---------------------------------------------------------------- prompts and the plan

def test_prompts_are_written_for_every_scene(tide, capsys):
    passing(tide, 1)
    code, out = approve(capsys, "--chapters", "1")
    assert code == 0, out
    doc = json.loads(S.scenes_path(ep(tide), 1).read_text(encoding="utf-8"))
    bible, locs = B.load_characters(tide / "bible"), B.load_locations(tide / "bible")
    pos = P.positions(doc)
    for s in doc["scenes"]:
        f = ep(tide) / "images" / "c0001" / f"s{s['id']:02d}.prompt.txt"
        assert f.read_text(encoding="utf-8") == P.flow_prompt(s, doc, 1, bible, locs, {}, pos[s["id"]])
        assert f.read_bytes().startswith(P.HEADER_MARK.encode()) and b"\r" not in f.read_bytes()
    assert f"chapter 1: {len(doc['scenes'])} scenes, {len(doc['scenes'])} prompts written" in out
    assert f"{len(doc['scenes'])} pictures still to generate" in out
    assert "novel images --chapters 1" in out


def test_scene_sentence_of_a_written_prompt_matches_adopt(tide, capsys):
    """flow/adopt.mjs's expression, run by node on a real v2 prompt file, gives the action sentence."""
    passing(tide, 1)
    approve(capsys, "--chapters", "1")
    f = ep(tide) / "images" / "c0001" / "s02.prompt.txt"
    js = ("const fs=require('fs');const norm=s=>(s||'').replace(/\\s+/g,' ').trim();"
          "console.log(norm(fs.readFileSync(process.argv[1],'utf8').split('SCENE:')[1]?.split('\\n')[0]))")
    got = subprocess.run(["node", "-e", js, str(f)], capture_output=True, text=True, check=True).stdout.strip()
    doc = json.loads(S.scenes_path(ep(tide), 1).read_text(encoding="utf-8"))
    assert got == doc["scenes"][1]["action"] == P.scene_sentence(f.read_text(encoding="utf-8"))
    assert len(got) >= 30


def test_plan_fields(tide, capsys):
    passing(tide, 1)
    approve(capsys, "--chapters", "1")
    plan = plan_of(tide)
    flow = (ep(tide) / "flow").resolve().as_posix()
    assert plan["project"] == PROJECT and plan["image_model"] == "Nano Banana Pro"
    assert plan["cdp"] == "http://127.0.0.1:9222" and plan["header_mark"] == P.HEADER_MARK
    # portable plan: every path is relative to the episode folder, no "base" (it would be absolute)
    assert plan["journal"] == "flow/journal.jsonl" and plan["stop_file"] == "flow/STOP" and "base" not in plan
    s = plan["shots"][0]
    assert s["id"] == "c0001-s01" and set(s) == {"id", "chars", "prompt", "out", "meta", "used"}
    for k in ("prompt", "out", "meta", "used"):
        assert not os.path.isabs(s[k]) and "\\" not in s[k]
    assert s["prompt"] == "images/c0001/s01.prompt.txt" and s["out"] == "images/c0001/s01-2k.jpg"
    assert flow and not str(ep(tide)) in json.dumps(plan)
    assert s["chars"] == ["Maren Vale"]
    assert not list((ep(tide) / "flow").glob("*.tmp"))


def test_old_absolute_plan_is_kept_working_and_made_portable_on_the_next_approval(tide, capsys):
    passing(tide, 1, 2)
    approve(capsys, "--chapters", "1")
    plan_file = ep(tide) / "flow" / "plan.json"
    plan = plan_of(tide)
    root = ep(tide).resolve()
    for sh in plan["shots"]:            # make it look like a plan written before this change
        for k in ("prompt", "out", "meta", "used"):
            sh[k] = (root / sh[k]).as_posix()
    plan["journal"], plan["stop_file"] = (root / "flow" / "journal.jsonl").as_posix(), (root / "flow" / "STOP").as_posix()
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    from novel import flowrun as F
    rows = F.classify(F.load_plan(plan_file), ep(tide))
    assert rows and all(r["out"].is_absolute() for r in rows)
    approve(capsys, "--chapters", "2")                       # chapter 1's kept shots are made relative
    new = plan_of(tide)
    assert all(not os.path.isabs(sh["out"]) for sh in new["shots"])
    assert new["journal"] == "flow/journal.jsonl"
    assert [r["out"] for r in F.classify(F.load_plan(plan_file), ep(tide)) if r["id"].startswith("c0001")] ==         [r["out"] for r in rows]


def _images_settings(bd, **images):
    """Override the book's images settings (the runner's pauses are set to 0 so tests do not sleep)."""
    import yaml
    doc = yaml.safe_load((bd / "book.yaml").read_text(encoding="utf-8")) or {}
    doc.setdefault("images", {}).update(images)
    (bd / "book.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_approved_plan_works_after_the_data_folder_moves(tide, capsys, tmp_path, monkeypatch):
    """Approve here, move the whole data folder somewhere else, run `novel images` there: no re-approving."""
    import yaml
    passing(tide, 1)
    approve(capsys, "--chapters", "1")
    _images_settings(tide, batch=1, batch_pause=0)
    n = counts(tide)[1]
    moved, code = _move_and_run_images(tide, capsys, tmp_path, monkeypatch)
    assert code == 0
    made = sorted((moved / "images" / "c0001").glob("s*-2k.jpg"))
    assert len(made) == n and all(p.stat().st_size > 50000 for p in made)
    assert (moved / "flow" / "journal.jsonl").exists()
    assert not (tmp_path / "data").exists()


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_batch_of_five_works_after_the_data_folder_moves(tide, capsys, tmp_path, monkeypatch):
    """The default batch (images.batch 5) with relative plan paths: pictures are sent 5 at a time, then saved."""
    passing(tide, 1)
    approve(capsys, "--chapters", "1")
    _images_settings(tide, batch=5, batch_pause=0)
    n = counts(tide)[1]
    moved, code = _move_and_run_images(tide, capsys, tmp_path, monkeypatch)
    assert code == 0
    made = sorted((moved / "images" / "c0001").glob("s*-2k.jpg"))
    assert len(made) == n and all(p.stat().st_size > 50000 for p in made)
    calls = (moved / "flow" / "calls.log").read_text(encoding="utf-8").splitlines()
    assert len(calls) == n and all(c.endswith("--batch") for c in calls)   # every picture went through batch mode


def _move_and_run_images(tide, capsys, tmp_path, monkeypatch):
    """The plan is approved in `tide`: move the data folder elsewhere, run `novel images` there. (moved, code)"""
    import yaml
    from novel.commands import images_cmd
    home = tmp_path / "home"
    new_data = tmp_path / "other pc" / "NovelData"
    new_data.parent.mkdir()
    shutil.move(str(tmp_path / "data"), str(new_data))
    (home / "settings.yaml").write_text(yaml.safe_dump({"data_dir": str(new_data)}), encoding="utf-8")
    monkeypatch.setenv("NOVEL_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("FLOW_GEN", str(ROOT / "tests" / "fake_gen.mjs"))
    monkeypatch.setenv("FAKE_SCRIPT", "{}")
    moved = new_data / "tide" / "episodes" / "E001"
    assert str(tmp_path / "data") not in (moved / "flow" / "plan.json").read_text(encoding="utf-8")
    code = images_cmd.main(["--book", "tide", "--ep", "1", "--chapters", "1"], inject={"answers": lambda u: "Chrome/130"},
                           ask=lambda p: pytest.fail(p), interactive=False)
    capsys.readouterr()
    return moved, code


def test_plan_merges_two_approvals_and_reapproval_replaces_only_its_chapter(tide, capsys):
    passing(tide, 1, 2)
    approve(capsys, "--chapters", "1")
    n1 = len(plan_of(tide)["shots"])
    assert {s["id"][:5] for s in plan_of(tide)["shots"]} == {"c0001"}
    approve(capsys, "--chapters", "2")
    shots = plan_of(tide)["shots"]
    assert {s["id"][:5] for s in shots} == {"c0001", "c0002"}
    assert len(shots) == n1 + counts(tide)[2]
    assert [s["id"] for s in shots] == sorted(s["id"] for s in shots)
    prev = json.loads((ep(tide) / "flow" / "plan.prev.json").read_text(encoding="utf-8"))
    assert {s["id"][:5] for s in prev["shots"]} == {"c0001"}
    # re-approve chapter 1 after a bible change: its shots change, chapter 2's stay as they were
    two_before = [s for s in shots if s["id"].startswith("c0002")]
    chars = B.load_characters(tide / "bible")
    chars["characters"]["maren_vale"]["variants"]["base"]["flow_name"] = "Maren V2"
    B.save_characters(tide / "bible", chars)
    approve(capsys, "--chapters", "1")
    shots2 = plan_of(tide)["shots"]
    assert [s for s in shots2 if s["id"].startswith("c0002")] == two_before
    assert any(s["chars"][0] == "Maren V2" for s in shots2 if s["id"].startswith("c0001"))
    assert len(shots2) == len(shots)


def test_kept_old_prompt_when_the_image_exists(tide, capsys):
    passing(tide, 1)
    approve(capsys, "--chapters", "1")
    d = ep(tide) / "images" / "c0001"
    old1 = (d / "s01.prompt.txt").read_text(encoding="utf-8")
    old2 = (d / "s02.prompt.txt").read_text(encoding="utf-8")
    (d / "s01-2k.jpg").write_bytes(b"\xff\xd8" + b"0" * 60000 + b"\xff\xd9")
    locs = B.load_locations(tide / "bible")
    locs["locations"]["harbour_quay"]["block"] += " A red buoy floats by the steps."
    B.save_locations(tide / "bible", locs)
    code, out = approve(capsys, "--chapters", "1")
    assert code == 0, out
    assert "kept: image exists, prompt changed" in out
    assert "c0001-s01" in out and "s01-2k.jpg" in out and "novel approve --chapters 1" in out
    assert (d / "s01.prompt.txt").read_text(encoding="utf-8") == old1                 # image exists: kept
    new2 = (d / "s02.prompt.txt").read_text(encoding="utf-8")
    assert new2 != old2 and "red buoy" in new2                                         # no image: rewritten
    # delete the picture and approve again: the new prompt is written
    (d / "s01-2k.jpg").unlink()
    approve(capsys, "--chapters", "1")
    assert "red buoy" in (d / "s01.prompt.txt").read_text(encoding="utf-8")


def test_dry_run_writes_nothing(tide, capsys, tmp_path):
    root = tmp_path
    before = tree(root)
    code, out = approve(capsys, "--chapters", "1-2", "--dry-run", "--force")
    assert code == 0, out
    assert "DRY RUN" in out and "prompts to write" in out and "FORCED past QA" in out
    assert tree(root) == before
    code, out = approve(capsys, "--chapters", "1-2", "--dry-run")
    assert code == 1 and "REFUSED" in out
    assert tree(root) == before


def test_missing_flow_project_is_a_clear_error(tmp_path, monkeypatch, capsys):
    bd = setup_book(tmp_path, monkeypatch)                        # no images.flow_project
    full_run(monkeypatch, capsys, bd)
    capsys.readouterr()
    passing(bd, 1)
    with pytest.raises(SystemExit) as e:
        novel_main(["approve", "--book", "tide", "--ep", "1", "--chapters", "1"])
    assert "images.flow_project" in str(e.value) and "book.yaml" in str(e.value)
    assert not (ep(bd) / "flow").exists()


def test_characters_to_create_and_to_recreate_are_listed(tide, capsys):
    passing(tide, 1)
    chars = B.load_characters(tide / "bible")
    chars["characters"]["tobias_reed"]["variants"]["base"]["flow_made"] = False
    chars["characters"]["maren_vale"]["needs_new_reference"] = True
    B.save_characters(tide / "bible", chars)
    code, out = approve(capsys, "--chapters", "1")
    assert code == 0, out
    assert "to create" in out and "novel characters make --keys tobias_reed" in out
    assert "recreate" in out and "novel characters make --keys maren_vale" in out
    assert "novel characters make first" in out


def test_unknown_variant_in_the_bible_refuses_the_chapter(tide, capsys):
    passing(tide, 1)
    chars = B.load_characters(tide / "bible")
    chars["characters"]["tobias_reed"]["variants"]["base"]["chapters"] = "5-"
    B.save_characters(tide / "bible", chars)
    code, out = approve(capsys, "--chapters", "1")
    assert code == 1 and "cannot build the prompts" in out and not (ep(tide) / "flow").exists()


# ---------------------------------------------------------------- the real runner can use the plan

@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_flow_runner_saves_one_image_per_shot_of_the_chapter(tide, capsys, tmp_path):
    passing(tide, 1, 2)
    approve(capsys, "--chapters", "1-2")
    plan = ep(tide) / "flow" / "plan.json"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = dict(os.environ, FLOW_GEN=str(ROOT / "tests" / "fake_gen.mjs"), FAKE_SCRIPT="{}")
    r = subprocess.run(["node", str(ROOT / "flow" / "run.mjs"), str(plan), "--only", "c0001", "--pause", "0",
                        "--chapter-pause", "0"], cwd=elsewhere, env=env, capture_output=True, text=True, timeout=120)
    shots = [s for s in plan_of(tide)["shots"] if s["id"].startswith("c0001")]
    assert len(shots) == counts(tide)[1]
    for s in shots:
        assert (ep(tide) / s["out"]).stat().st_size > 50000, r.stdout + r.stderr
    assert not any((ep(tide) / s["out"]).exists() for s in plan_of(tide)["shots"] if s["id"].startswith("c0002"))
    status = json.loads((ep(tide) / "flow" / "status.json").read_text(encoding="utf-8"))
    assert len(status["saved"]) == len(shots)


# ---------------------------------------------------------------- novel sheet

def sheet(capsys, *args):
    code = novel_main(["sheet", "--book", "tide", "--ep", "1", *args])
    cap = capsys.readouterr()
    return code, cap.out + cap.err


def test_sheet_for_a_v2_chapter_with_missing_pictures(tide, capsys):
    d = ep(tide) / "images" / "c0001"
    d.mkdir(parents=True)
    (d / "s01-2k.jpg").write_bytes(b"\xff\xd8" + b"0" * 100 + b"\xff\xd9")
    (d / "s01.flow.json").write_text(json.dumps({"flow_reported_ingredients": ["Maren Vale"],
                                                  "prompt_used_share": 1.0, "prompt_shortened": False}))
    code, out = sheet(capsys, "--chapter", "1")
    assert code == 0 and "contact.html" in out
    page = (d / "contact.html").read_text(encoding="utf-8")
    doc = json.loads(S.scenes_path(ep(tide), 1).read_text(encoding="utf-8"))
    assert page.count("<h2>") == 2 and "Group g01 · Harbour Quay · outdoor · dawn" in page and "Group g02" in page
    assert page.count("outdoor · dawn</h2>") == 2
    assert 'src="s01-2k.jpg"' in page and ":" not in page.split('src="')[1].split('"')[0]    # relative, not absolute
    assert page.count("not generated") == len(doc["scenes"]) - 1 and "s02 not generated" in page
    assert "Flow used: Maren Vale" in page and "prompt used 100%" in page
    assert "hands: empty, at the sides" in page and "Maren Vale" in page
    assert doc["scenes"][0]["action"].replace("'", "&#x27;") in page
    first_hindi = S.chapter_paras(ep(tide), 1)[doc["scenes"][0]["para"]][:20]
    assert first_hindi in page
    assert page.index("<h2>") < page.index("s01") < page.rindex("<h2>")
    # moving the folder keeps the picture: the path is relative to the html file
    moved = d.parent / "moved"
    shutil.copytree(d, moved)
    assert (moved / "s01-2k.jpg").exists()


def test_sheet_open_uses_the_default_browser(tide, capsys, monkeypatch):
    import webbrowser
    seen = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: seen.append(url) or True)
    code, _ = sheet(capsys, "--chapter", "2", "--open")
    assert code == 0 and len(seen) == 1 and seen[0].startswith("file:///") and seen[0].endswith("contact.html")
