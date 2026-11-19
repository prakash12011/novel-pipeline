import pytest

from novel import kqa


def test_opencode_chat_is_one_stateless_request_and_counts_the_allowance():
    sent = []

    def post(base, key, body):
        sent.append(body)
        return {"choices": [{"message": {"content": "lines"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "opencode": {"allowance": 0.012}}}
    c = kqa.OpenCodeChat("claude-haiku-5-5", key="k", post=post)
    r = c.generate("the rules", "the chapter")
    assert r.text == "lines" and r.finish == "STOP" and r.model == "claude-haiku-5-5"
    assert [m["role"] for m in sent[0]["messages"]] == ["system", "user"] and sent[0]["model"] == "claude-haiku-5-5"
    c.generate("s", "t")
    assert c.allowance == pytest.approx(0.024)


def test_a_cut_off_answer_says_max_tokens():
    c = kqa.OpenCodeChat("m", key="k", post=lambda b, k, body: {"choices": [{"message": {"content": "half"},
                                                                             "finish_reason": "length"}]})
    assert c.generate("s", "t").finish == "MAX_TOKENS"
