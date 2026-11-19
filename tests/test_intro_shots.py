"""novel intro shots: the writer's answer is checked and filed as shots.json (the writer itself is a fake)."""

import json
from types import SimpleNamespace

import pytest

from novel.core import paths
from novel.stages import episode as E
from novel.stages import episode_intro as I
from test_tts import env  # noqa: F401  (a temp home, a book "coi" with episode 1)

PARAS = "एक छोटी सी बात हुई थी।\n\nफिर एक और बात हुई।\n\nअब आगे क्या होगा?"


class Writer:
    """Answers from a list, one per call; keeps what it was asked."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def generate(self, system, text, **kw):
        self.asked.append((system, text, kw))
        return SimpleNamespace(text=self.answers.pop(0), finish="STOP")


def shot(i, cast=("lumian_lee",)):
    return {"id": i, "cast": list(cast), "shot": "medium shot", "scene": f"Scene {i}.",
            "people": {k: {"where": "left", "pose": "walking", "face": "mouth tight, eyes wide, brows low"}
                       for k in cast}}


@pytest.fixture
def intro(env):  # noqa: F811
    book = paths.book_dir("coi")
    (book / "bible" / "qwen" / "front").mkdir(parents=True)
    (book / "bible" / "qwen" / "front" / "lumian_lee_front.jpg").write_bytes(b"x")
    (book / "bible" / "characters.json").write_text(json.dumps({"characters": {
        "lumian_lee": {"display": "Lumian Lee", "look": "an eighteen-year-old lad"},
        "no_front": {"display": "No Front", "look": "nobody"}}}), encoding="utf-8")
    I.init(1, PARAS)
    return E.root(9001)


def answer(*shots):
    return json.dumps({"shots": list(shots)})


def test_a_good_answer_is_filed_as_shots_json(intro):
    w = Writer(answer(shot(1), shot(2), shot(3, ())))
    out = I.write_shots(1, w)
    got = json.loads(out.read_text(encoding="utf-8"))
    assert [s["id"] for s in got] == [1, 2, 3] and got[0]["people"]["lumian_lee"]["face"]
    system, text, kw = w.asked[0]
    assert "exactly 3 shots" in system and "lumian_lee | Lumian Lee" in text
    assert "no_front" not in text and kw["scenes"] is True             # only people with a front view are offered


def test_a_bad_answer_is_asked_again_with_its_problems_then_refused(intro):
    bad = answer(shot(1, ("stranger",)), shot(2), shot(3))
    w = Writer(bad, answer(shot(1), shot(2)))                          # the retry has the wrong count too
    with pytest.raises(SystemExit) as e:
        I.write_shots(1, w)
    assert "cast must be a list of keys" in w.asked[1][1] and "exactly 3 shots" in str(e.value)
    assert (intro / "shots.reply.txt").exists() and not (intro / "shots.json").exists()


def test_the_retry_can_fix_it_and_an_existing_file_needs_redo(intro):
    w = Writer(answer(shot(1), shot(2)), answer(shot(1), shot(2), shot(3)))
    I.write_shots(1, w)
    assert len(w.asked) == 2 and (intro / "shots.json").exists()
    with pytest.raises(SystemExit):
        I.write_shots(1, Writer())                                     # no --redo: nothing is asked
    I.write_shots(1, Writer(answer(shot(1), shot(2), shot(3))), redo=True)
    assert len(list(intro.glob("shots.before-*.json"))) == 1
