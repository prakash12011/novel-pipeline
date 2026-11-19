import json
import subprocess

import pytest

from novel import kclaude


def fake_run(answer: dict, seen: list):
    def run(cmd, **kw):
        seen.append((cmd, kw))
        assert (kw["cwd"] and open(cmd[cmd.index("--system-prompt-file") + 1], encoding="utf-8").read() == "rules")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(answer), "")
    return run


def test_a_headless_call_is_fresh_unsaved_toolless_and_never_on_the_api(monkeypatch):
    monkeypatch.setattr(kclaude.shutil, "which", lambda name: "claude.exe")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")
    monkeypatch.setenv(kclaude.TOKEN_VAR, "tok-b")
    seen = []
    c = kclaude.ClaudeHeadless(run=fake_run({"type": "result", "subtype": "success", "is_error": False,
                                             "result": "the lines", "total_cost_usd": 0.4,
                                             "usage": {"input_tokens": 100, "cache_read_input_tokens": 50,
                                                       "output_tokens": 30},
                                             "modelUsage": {"claude-sonnet-5-5": {}}}, seen))
    r = c.generate("rules", "the chapter")
    cmd, kw = seen[0]
    for flag in ("-p", "--no-session-persistence", "--strict-mcp-config"):
        assert flag in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5" and cmd[cmd.index("--effort") + 1] == "medium"
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--setting-sources") + 1] == ""
    assert kw["input"] == "the chapter" and "ANTHROPIC_API_KEY" not in kw["env"]
    assert kw["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-b"                     # the other account's token
    assert (r.text, r.finish, r.model) == ("the lines", "STOP", "claude-code:claude-sonnet-5-5")
    assert r.usage["promptTokenCount"] == 150 and c.cost == pytest.approx(0.4)


def test_an_error_result_is_an_error(monkeypatch):
    monkeypatch.setattr(kclaude.shutil, "which", lambda name: "claude.exe")
    c = kclaude.ClaudeHeadless(run=fake_run({"subtype": "error_during_execution", "is_error": True,
                                             "result": "usage limit reached"}, []))
    with pytest.raises(kclaude.ClaudeError, match="usage limit"):
        c.generate("rules", "t")


def test_a_picture_is_read_in_place_with_only_the_read_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(kclaude.shutil, "which", lambda name: "claude.exe")
    from types import SimpleNamespace
    pic = tmp_path / "s01-2k.jpg"
    pic.write_bytes(b"jpeg")
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, text=kw["input"])
        return SimpleNamespace(returncode=0, stderr="", stdout='{"result": "{}", "subtype": "success"}')

    kclaude.ClaudeHeadless("claude-haiku-5-5", run=run).generate("sys", "look", image=pic)
    cmd = seen["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "Read" and cmd[cmd.index("--add-dir") + 1] == str(tmp_path.resolve())
    assert str(pic.resolve()) in seen["text"] and list(tmp_path.iterdir()) == [pic]     # no copy anywhere
    kclaude.ClaudeHeadless("claude-haiku-5-5", run=run).generate("sys", "text only")
    assert seen["cmd"][seen["cmd"].index("--tools") + 1] == "" and "--add-dir" not in seen["cmd"]
