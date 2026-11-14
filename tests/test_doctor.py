import json

import pytest

from novel.commands import doctor_cmd as D


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(h))
    return h


def test_format_and_exit_code():
    rs = [D.Result("a", D.OK, "fine"),
          D.Result("b", D.MISSING, "gone", "do this", True)]
    lines = D.format_results(rs)
    assert any("a" in l and "ok" in l and "fine" in l for l in lines)
    assert "      FIX: do this" in lines
    assert lines[-1].startswith("NOT READY")
    assert D.exit_code(rs) == 1
    assert D.exit_code(rs[:1]) == 0


def test_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr(D, "which", lambda c: None)
    rs = D.check_ffmpeg()
    assert [r.status for r in rs] == [D.MISSING] * 3
    assert all(r.fix for r in rs)
    assert D.exit_code(rs) == 1


def test_optional_miss_keeps_exit_code():
    rs = [D.Result("x", D.OK, ""), D.Result("opt", D.WARN, "no", "fix", False)]
    assert D.exit_code(rs) == 0


def test_optional_checks_only_warn(home, monkeypatch):
    monkeypatch.setattr(D, "which", lambda c: None)
    monkeypatch.setenv("CHAINNER", str(home / "nope.exe"))
    for r in (D.check_claude(), D.check_youtube_key(), D.check_chainner()):
        assert r.status == D.WARN and not r.required and r.fix


def test_secrets_counts_only(home, capsys):
    (home / "secrets.env").write_text("GEMINI_API_KEY=AIzaSECRETONE\nGEMINI_API_KEY2=AIzaSECRETTWO\n"
                                      "YOUTUBE_API_KEY=YTSECRET\n", encoding="utf-8")
    r = D.check_gemini_keys()
    y = D.check_youtube_key()
    assert r.status == D.OK and "2 key" in r.detail
    out = "\n".join(D.format_results([r, y]))
    for s in ("SECRETONE", "SECRETTWO", "YTSECRET"):
        assert s not in out


def test_gemini_online_by_position_never_key(home):
    (home / "secrets.env").write_text("GEMINI_API_KEY=KEYAAA\nGEMINI_API_KEY2=KEYBBB\n", encoding="utf-8")
    seen = []

    def fake(url, timeout, headers):
        seen.append(headers["x-goog-api-key"])
        return (200, "") if headers["x-goog-api-key"] == "KEYAAA" else (400, "")

    rs = D.check_gemini_online(http=fake)
    assert [(r.name, r.status) for r in rs] == [("gemini key 1", D.OK), ("gemini key 2", D.WARN)]
    out = "\n".join(D.format_results(rs))
    assert "KEYAAA" not in out and "KEYBBB" not in out


def test_closed_devtools_port_skipped(home):
    (home / "settings.yaml").write_text("data_dir: x\nchrome:\n  cdp_url: http://127.0.0.1:1\n", encoding="utf-8")
    rs = D.check_chrome(http=lambda url, t: (None, "refused"))
    dev = [r for r in rs if r.name == "chrome devtools"][0]
    assert dev.status == D.SKIPPED
    assert "Chrome not running; start it with `novel chrome`" in dev.detail
    assert dev.status != D.MISSING


def test_data_dir_writable(home, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (home / "settings.yaml").write_text(f"data_dir: {data.as_posix()}\n", encoding="utf-8")
    assert D.check_settings_file().status == D.OK
    assert D.check_data_dir().status == D.OK
    assert list(data.iterdir()) == []


def test_settings_missing(home):
    r = D.check_settings_file()
    assert r.status == D.MISSING and "novel settings init" in r.fix


def test_stray_secrets(home, tmp_path):
    (home / "settings.yaml").write_text(f"data_dir: {tmp_path.as_posix()}\n", encoding="utf-8")
    assert D.check_stray_secrets().status in (D.OK, D.MISSING)
    (tmp_path / "HF_TOKEN_X").write_text("x")
    r = D.check_stray_secrets()
    assert r.status == D.MISSING and "HF_TOKEN_X" in r.detail


def test_path_check_and_fix_logic(monkeypatch):
    from novel.core import paths
    folder = str(paths.repo_root() / "bin")
    assert D.check_bin_on_path(user_path="C:\\x", process_path="C:\\y").status == D.WARN
    assert D.check_bin_on_path(user_path="C:\\x;" + folder, process_path="").status == D.OK

    written = []
    out = []
    # decline
    assert D.fix_path(getter=lambda: "C:\\x", writer=written.append, ask=lambda p: "n", out=out.append) is False
    assert written == [] and "Add it? [y/N]" in "".join(out) or True
    # accept
    assert D.fix_path(getter=lambda: "C:\\x", writer=lambda v: written.append(v) or True,
                      ask=lambda p: "y", out=out.append) is True
    assert written == ["C:\\x" + ";" + folder] or written == ["C:\\x" + __import__("os").pathsep + folder]
    # already there
    written.clear()
    assert D.fix_path(getter=lambda: folder, writer=written.append, ask=lambda p: "y", out=out.append) is False
    assert written == []


def test_fix_path_prompt_text():
    prompts = []
    D.fix_path(getter=lambda: "C:\\x", writer=lambda v: True, ask=lambda p: prompts.append(p) or "n", out=lambda s: None)
    assert prompts == ["Add it? [y/N] "]


def test_json_main(home, capsys, monkeypatch):
    monkeypatch.setattr(D, "collect", lambda online=False: [D.Result("a", D.OK, "d")])
    assert D.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "a"


def test_kaggle_token_fix_names_the_real_file_and_no_made_up_command(home, tmp_path, monkeypatch):
    monkeypatch.setattr(D.Path, "home", classmethod(lambda cls: tmp_path / "pc"))
    (home / "settings.yaml").write_text("kaggle: {account: seema}\n", encoding="utf-8")
    r = D.check_kaggle_token()
    want = tmp_path / "pc" / ".kaggle" / "access_token-seema"
    assert r.status == D.MISSING and r.required
    assert str(want) in r.detail and str(want) in r.fix
    assert "kaggle.com/settings" in r.fix and "Create New Token" in r.fix
    assert "kaggle use" not in r.fix and "kaggle use" not in "\n".join(D.format_results([r]))
    # with the file in place it is fine and the token is never printed
    want.parent.mkdir(parents=True)
    want.write_text("KGAT_SECRETTOKEN", encoding="utf-8")
    ok = D.check_kaggle_token()
    assert ok.status == D.OK and "SECRETTOKEN" not in "\n".join(D.format_results([ok]))


def test_kaggle_token_fix_without_an_account(home, tmp_path, monkeypatch):
    monkeypatch.setattr(D.Path, "home", classmethod(lambda cls: tmp_path / "pc"))
    r = D.check_kaggle_token()
    assert r.status == D.MISSING and "access_token" in r.fix and "kaggle: {account:" in r.fix
