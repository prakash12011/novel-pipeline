"""`novel chrome`: all start logic runs with injected "port answers" and "launch"; no real browser."""

import pytest
import yaml

from novel import cli
from novel.commands import chrome_cmd

EXE = r"C:\fake\chrome.exe"
PROFILE = r"D:\fake profile"
CDP = "http://127.0.0.1:9444"
URL = "https://labs.google/fx/tools/flow"


class Fake:
    def __init__(self, answers):
        self.answers = list(answers)
        self.launched = []
        self.said = []
        self.now = 0.0

    def probe(self, url, timeout=2.0):
        assert url == CDP
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def launch(self, argv):
        self.launched.append(argv)

    def sleep(self, s):
        self.now += s

    def clock(self):
        return self.now

    def run(self, **kw):
        return chrome_cmd.start(EXE, PROFILE, CDP, URL, answers=self.probe, do_launch=self.launch,
                                sleep=self.sleep, clock=self.clock, say=self.said.append, **kw)


def test_already_running_launches_nothing():
    f = Fake(["Chrome/130.0"])
    assert f.run() == 0
    assert f.launched == []
    assert f.said == ["Chrome is already running on port 9444 (Chrome/130.0)"]


def test_not_running_launches_once_with_right_arguments():
    f = Fake([None, None, None, "Chrome/130.0"])        # answers only after a second or two
    assert f.run() == 0
    assert f.launched == [[EXE, "--remote-debugging-port=9444", f"--user-data-dir={PROFILE}", URL]]
    assert "sign in" in f.said[-1].lower() or "Sign in" in f.said[-1]
    assert "16:9" in f.said[-1]


def test_timeout_when_it_never_answers():
    f = Fake([None])
    assert f.run(wait_seconds=3) == 1
    assert len(f.launched) == 1
    assert "did not answer on port 9444 within 3 s" in f.said[-1]
    assert f.now >= 3


def test_dry_run_launches_nothing():
    f = Fake([None])
    assert f.run(dry_run=True) == 0
    assert f.launched == []
    assert "--remote-debugging-port=9444" in f.said[0] and EXE in f.said[0]
    f2 = Fake(["Chrome/1"])
    assert f2.run(dry_run=True) == 0 and f2.launched == []


def test_command_uses_settings_and_flow_project(tmp_path, monkeypatch, capsys):
    home, data = tmp_path / "home", tmp_path / "data"
    (data / "tb").mkdir(parents=True)
    home.mkdir()
    (home / "settings.yaml").write_text(yaml.safe_dump({
        "data_dir": str(data),
        "chrome": {"exe": EXE, "profile": PROFILE, "cdp_url": CDP}}), encoding="utf-8")
    (data / "tb" / "book.yaml").write_text(yaml.safe_dump(
        {"images": {"flow_project": "https://labs.google/fx/tools/flow/project/abc"}}), encoding="utf-8")
    monkeypatch.setenv("NOVEL_HOME", str(home))
    monkeypatch.setattr(chrome_cmd, "probe", lambda url, timeout=2.0: None)
    launched = []
    monkeypatch.setattr(chrome_cmd, "launch", lambda argv: launched.append(argv))
    assert cli.main(["chrome", "--dry-run", "--book", "tb"]) == 0
    out = capsys.readouterr().out
    assert "--remote-debugging-port=9444" in out and "project/abc" in out
    assert "dry run" in out and launched == []
    assert cli.main(["chrome", "--dry-run", "--book", "tb", "--url", "https://example.test/x"]) == 0
    assert "https://example.test/x" in capsys.readouterr().out
