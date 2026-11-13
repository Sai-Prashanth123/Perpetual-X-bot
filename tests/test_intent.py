"""Offline tests: understanding a message before answering (fake model client, no network).

    uv run --no-project tests/test_intent.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from perpetual_core import intent  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


class Fake:
    def __init__(self, data=None, boom=False):
        self.seen = {}
        outer = self

        class M:
            def create(self, **kw):
                outer.seen = kw
                if boom:
                    raise RuntimeError("down")
                return type("R", (), {"stop_reason": "end_turn", "content": [
                    type("B", (), {"type": "text", "text": json.dumps(data)})()]})()
        self.messages = M()


def main():
    checkin = "👋 Quick check-in, Sai: how are these going?\n• TASK-32 Create GTM brain for Alex"
    f = Fake({"means": "Their tasks are progressing fine", "replying_to": "the task check-in",
              "wants": "acknowledge; mark TASK-32 as doing", "clear": True, "question": ""})
    u = intent.understand("all are going good", [], [checkin], "Sai", client=f)
    prompt = f.seen["messages"][0]["content"]
    check("Quick check-in" in prompt and "all are going good" in prompt, "the check-in is given to the model")
    check(f.seen["output_config"]["format"]["type"] == "json_schema", "structured output")
    lines = intent.note(u)
    check(any("the task check-in" in x for x in lines) and any("acknowledge" in x for x in lines),
          "note says what it answers and what to do")

    vague = Fake({"means": "Wants something changed on 'it'", "replying_to": "", "wants": "", "clear": False,
                  "question": "Which one do you mean: the Alex article or the website copy?"})
    lines = intent.note(intent.understand("change it to the other one", [], [], "Naga", client=vague))
    check(any("ask exactly one short question" in x and "Alex article" in x for x in lines),
          "unclear → ask one clarifying question")

    check(intent.understand("hi", [], [], client=Fake({})) is None, "small talk with no context: skipped")
    check(intent.understand("do the thing", [], [], client=Fake(boom=True)) is None, "model down: no note, no error")
    check(intent.note(None) == [], "no understanding → nothing injected")
    print("all good")


if __name__ == "__main__":
    main()
