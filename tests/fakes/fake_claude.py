"""A stand-in for `claude -p --output-format json`: reads the prompt on stdin, answers in the
CLI's JSON envelope. Used through NOVEL_CLAUDE_CMD="<python> <this file>".

    FAKE_CLAUDE_REPLIES=<file>  JSON list of replies, one per call (the last one repeats). Each is
                                {"kind": "ok" | "fence" | "garbage" | "envelope_error" | "exit1" | "sleep",
                                 "payload": {...}}   (payload = the model's JSON object)
                                Without the file: one good canned reply.
    FAKE_CLAUDE_LOG=<file>      one JSON line per call: {"n", "prompt_chars", "retry", "argv"}
"""

import json
import os
import sys
import time

CANNED = {
    "teaser": "Is episode mein ek chhote gaon ki ajeeb baatein samne aati hain. Dekhte hain hero kis raaste par chalta hai.",
    "comment_question": "Comment mein batao: aapko kya lagta hai, gaon ka raaz kya hoga?",
    "thumbnail_hooks": ["THIS VILLAGE|IS *NOT NORMAL*", "WHO IS|*THE HERO?*", "THE LAST|*WARNING*"],
    "thumbnail_character": "Hero",
}


def main() -> int:
    prompt = sys.stdin.read()
    n = 1
    counter = os.environ.get("FAKE_CLAUDE_LOG")
    if counter and os.path.exists(counter):
        with open(counter, encoding="utf-8") as fh:
            n = sum(1 for _ in fh) + 1
    if counter:
        with open(counter, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"n": n, "prompt_chars": len(prompt),
                                 "retry": "PREVIOUS ANSWER FAILED" in prompt,
                                 "argv": sys.argv[1:]}) + "\n")
    spec = {"kind": "ok", "payload": CANNED}
    rf = os.environ.get("FAKE_CLAUDE_REPLIES")
    if rf:
        with open(rf, encoding="utf-8") as fh:
            replies = json.load(fh)
        spec = replies[min(n, len(replies)) - 1]
    kind = spec.get("kind", "ok")
    text = json.dumps(spec.get("payload", CANNED), ensure_ascii=False)
    if kind == "exit1":
        print("fake claude: boom", file=sys.stderr)
        return 1
    if kind == "sleep":
        time.sleep(30)
    if kind == "garbage":
        print("this is not json at all")
        return 0
    if kind == "envelope_error":
        print(json.dumps({"type": "result", "is_error": True, "result": "API Error"}))
        return 0
    if kind == "fence":
        text = "Here you go:\n```json\n" + text + "\n```"
    if kind == "model_garbage":
        text = "I cannot do that."
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                      "session_id": "fake"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
