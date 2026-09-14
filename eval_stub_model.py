"""A scripted model, for checking the evaluation HARNESS — never its subject.

`run_home_eval.py --mode stub` uses this to drive the whole pipeline without a
network call, so that the day a key exists the live run is one command rather
than a debugging session. It answers each step from a fixed script keyed on
which prompt it was shown, exactly as `test_home_findings.py` does.

**Nothing this file produces is evidence about model quality.** It is a
harness, and its answers were written by hand. Every report that includes a
stub run says so in the same breath. If you find yourself reading a stub result
as a finding about Trovis's AI, that is the mistake this paragraph exists to
prevent.

It is deliberately naive: it opens with one tool call, then returns a
minimally-supported observation drawn from whatever the retrieval actually
returned. That exercises retrieval, the evidence ledger, validation,
publication and the read path. It does not exercise judgement, because it has
none.
"""
from __future__ import annotations

import json
import re
from typing import Any


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Block(input_tokens=0, output_tokens=0)


def _text(payload: Any) -> _Resp:
    return _Resp([_Block(type="text", text=json.dumps(payload))])


def _tool(name: str, inp: dict, call_id: str = "s1") -> _Resp:
    return _Resp([_Block(type="tool_use", id=call_id, name=name, input=inp)],
                 stop_reason="tool_use")


def _first_run_ref(messages) -> str | None:
    """Pull one run id out of whatever retrieval put in front of us.

    The stub cites only evidence it was actually shown, because the point of
    running it is to prove the ledger and the validator are wired up — and a
    stub that fabricated ids would prove the opposite of what it is for.
    """
    blob = json.dumps(messages, default=str)
    # A tool result arrives as a STRING of JSON inside the message, so the key
    # shows up escaped (\"run_id\") rather than plain. Matching the literal
    # `"run_id":` found nothing and the stub withdrew every finding — which
    # looked exactly like an abstention and was not one.
    m = re.search(r'run_id\\?"?\s*:\s*(\d+)', blob)
    return m.group(1) if m else None


class _Messages:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kw):
        system = kw.get("system") or ""
        messages = kw.get("messages") or []
        # Remember the first real run id retrieval has shown us. Composition
        # runs as a separate call with its own message list, and a finding may
        # only cite evidence that was actually delivered.
        seen = _first_run_ref(messages)
        if seen and not self.owner.run_ref:
            self.owner.run_ref = seen
        if system.startswith("You are Trovis, examining"):
            return _text({"candidates": [{
                "topic": "recorded-outcomes",
                "question": "What do the recorded outcomes of this job show?",
                "hypothesis": "Some items did not finish.",
                "category": "attention",
                "why_this_reader": "They are responsible for this work.",
                "evidence_needed": ["the job's runs and their outcomes"],
            }]})

        if system.startswith("You are Trovis, investigating"):
            self.owner.turns += 1
            if self.owner.turns == 1:
                return _tool("list_comparable_runs", {"limit": 25})
            ref = self.owner.run_ref
            return _text({
                "verdict": "qualified",
                "summary": "The retrieved runs carry the outcomes shown.",
                "for": [f"run:{ref}"] if ref else [],
                "against": [],
                "alternatives_considered": ["a different population between windows"],
                "unknown": ["why any individual item ended as it did"],
                "sample": {"observed": 1, "comparable": 1},
            })

        if system.startswith("You are Trovis, checking"):
            return _text({"decision": "publish", "reason": "observation only",
                          "claim_kind": "observation", "confidence": "qualified",
                          "overstated_phrases": []})

        if system.startswith("You are Trovis, ordering"):
            return _text({"order": [{"index": 0, "score": 0.5, "reason": "only one"}],
                          "merge": [], "drop": []})

        # composition / revision / optimization
        ref = self.owner.run_ref
        if not ref:
            return _text({"withdraw": True})
        return _text({
            "title": "One work item's recorded outcome",
            "explanation": "The record shows this item's outcome as retrieved.",
            "consequence": None,
            "claims": [{"text": "This item has the outcome the record shows.",
                        "kind": "observation", "evidence": [f"run:{ref}"]}],
            "entities": [{"kind": "run", "id": int(ref)}],
            "evidence": [{"kind": "run", "ref": ref, "note": "the retrieved row"}],
            "uncertainty": ["this is one item, not a pattern"],
            "next_step": {"kind": "review_runs", "text": "Open the item."},
            "graphic": {"kind": "none"},
        })


class StubClient:
    def __init__(self):
        self.messages = _Messages(self)
        self.turns = 0
        self.run_ref: str | None = None


# One instance for the process. `investigator._client()` is called per step,
# and a fresh object each time would reset the turn counter — the investigation
# loop would call a tool for ever and never return a verdict.
_SHARED = StubClient()


def client() -> StubClient:
    return _SHARED


def reset() -> None:
    """Between scenarios: forget the previous account's evidence."""
    _SHARED.turns = 0
    _SHARED.run_ref = None
