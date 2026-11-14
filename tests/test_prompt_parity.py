"""The Python and JavaScript reference prompts must stay identical."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from novel import bible as B

FLOW = Path(__file__).resolve().parents[1] / "flow"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_portrait_and_body_prompts_match_js():
    style = "Flat ink style, bold lines"
    look = "young woman with long black hair, green eyes and a grey coat"
    js = ("import {portraitPrompt, bodyPrompt} from './bible.mjs';"
          f"const s={json.dumps(style)}, l={json.dumps(look)};"
          "process.stdout.write(JSON.stringify({portrait: portraitPrompt(s, l), body: bodyPrompt(s)}));")
    out = subprocess.run(["node", "--input-type=module", "-e", js], cwd=FLOW, capture_output=True,
                         text=True, encoding="utf-8", check=True).stdout
    got = json.loads(out)
    assert got["portrait"] == B.portrait_prompt(style, look)
    assert got["body"] == B.body_prompt(style)
