"""The finding contract: what Trovis is allowed to say, and what must back it.

A FINDING is a published statement about this account's work. It is not a
rendering of a metric — the metrics already live in the Home snapshot, and
repeating one back is not a finding. A finding exists only when investigation
established something the reader would otherwise miss.

Three ideas the shape enforces, because each one is a way products lie:

1. **A claim names its kind.** `observation` (the record says this),
   `calculation` (the server computed this from the record), `hypothesis`
   (this is a proposed explanation). A hypothesis rendered as an observation is
   the single most common way an analytics product becomes untrustworthy.

2. **Every material claim points at evidence that can be re-opened.** Not a
   paraphrase — an evidence reference with a kind and an id, so a reader (or a
   later reviewer, or this module a week from now) can go and look. If the
   underlying record changes, the reference goes STALE rather than silently
   resolving to whatever is there now.

3. **Numbers come from the server.** A numeric claim must carry a
   `metric_ref` naming either a path into the authoritative snapshot or a
   server-side calculation performed during the investigation. The model
   selects and explains; it never arithmetic.

`validate_finding` is deterministic and fails CLOSED. It runs before anything
reaches a database, let alone a screen. A separate semantic assessment (see
investigator.py) asks whether the evidence actually supports the wording — it
SUPPLEMENTS these checks and can never substitute for them, because a second
model agreeing with the first is not evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulary — closed sets, all of them
# ---------------------------------------------------------------------------

# What a finding is FOR the reader. Deliberately three, and deliberately not
# "insight": a category has to change what the reader does.
CATEGORIES = ("attention", "opportunity", "positive_change")

# What KIND of statement the finding's headline claim is.
CLAIM_KINDS = ("observation", "calculation", "hypothesis")

# How far the evidence goes. `supported` means the deterministic checks and the
# evidence agree with the wording; `qualified` means something real was found
# but the explanation could not be established — which is publishable, and is
# NOT the same as a guess dressed up.
CONFIDENCES = ("supported", "qualified")

# Entities a finding may point at. Every one is an existing canonical id in
# this repo; there is no new taxonomy here and no invented relationship.
ENTITY_KINDS = ("run", "job", "agent", "person")

# Evidence a claim may lean on. Each is re-openable: `kind` says which table,
# `ref` says which row or which server calculation.
EVIDENCE_KINDS = (
    "run",            # one work item, by loops.id
    "run_event",      # one lifecycle event, by loop_events.id
    "failed_span",    # a failing operation inside a run
    "calculation",    # a server-side computation performed this analysis
    "snapshot",       # a field of the authoritative Home snapshot
    "agent_context",  # an agent's recorded description / identity
)

# What a finding may propose. This PR does NOT execute anything, and the
# allowlist is how that stays true: every one of these is something a PERSON
# does, or a place the product can send them. Nothing here is an action Trovis
# takes on its own.
NEXT_STEP_KINDS = (
    "review_runs",      # go look at these specific runs
    "review_agent",     # go look at this agent's setup
    "contact_person",   # someone is holding work and should be asked
    "investigate_cost", # the money is worth a closer look
    "no_action",        # worth knowing, nothing to do
)

# Graphics the reader's Home can draw. A finding may only ask for one of these
# and may only feed it data the server calculated.
GRAPHIC_KINDS = ("none", "run_outcome_split", "period_comparison", "wait_concentration")

MAX_TITLE_CHARS = 90
MAX_EXPLANATION_CHARS = 420
MAX_CLAIMS = 6
MAX_ENTITIES = 12
MAX_EVIDENCE = 24

# A finding that says nothing a metric did not already say is not a finding.
# These are the shapes that kept showing up in early drafts.
_EMPTY_PHRASES = re.compile(
    r"^\s*(there (is|are)|you have|the (team|org|account) (has|had))\b.{0,40}"
    r"\b(items?|tasks?|pieces? of work)\b\s*\.?\s*$",
    re.I,
)


# Wording that claims a WHOLE. Rejected on a chart label, and on a claim, when
# the search behind it did not finish.
_EXHAUSTIVE_RE = re.compile(
    r"\b(all|every|total|overall|entire|complete|none|no other|only)\b", re.I
)


def derive_coverage(
    snapshot: dict[str, Any], *, retrieval: dict[str, Any] | None = None
) -> dict[str, Any]:
    """What this analysis could actually establish. SERVER-DERIVED, always.

    Two independent ways a search comes up short, and both belong here:

      the SCOPE          the snapshot's own completeness — whether the counts
                         are exact, whether the scope's membership is whole,
                         whether an absence was established at all.
      the RETRIEVAL      whether the investigation's own budget ran out. A
                         capped scan saw a subset, and a claim about what is
                         NOT there cannot rest on a subset.

    Nothing a model says is merged in. An earlier version used `setdefault`,
    which let a draft assert `counts_exact: true` over an incomplete snapshot
    and publish "No other job is affected" as supported. Completeness is a fact
    about the search, and the model did not perform the search.
    """
    completeness = snapshot.get("completeness") or {}
    retrieval = retrieval or {}
    # FAIL CLOSED. A retrieval report that does not say it was complete is not
    # a complete retrieval. The earlier version defaulted the missing key to
    # True, so handing it the BUDGET's report — which knows about exhaustion
    # and nothing else — produced `complete: true` over a search whose rows had
    # been capped, trimmed, or lost to a failed tool. `complete` is now set
    # only by `InvestigationSession.retrieval_report`, which sees all of it.
    retrieval_complete = bool(retrieval.get("complete")) if retrieval else True
    return {
        "counts_exact": bool(completeness.get("counts_exact", True)),
        "scope_membership_complete": bool(
            completeness.get("scope_membership_complete", True)
        ),
        "absence_established": bool(completeness.get("absence_established", True)),
        "scope_state": completeness.get("scope_state"),
        # The investigation's own bound, carried onto the finding so a reader
        # (and a later analysis) can see the search was partial.
        "retrieval_complete": retrieval_complete,
        "retrieval_exhausted": sorted(retrieval.get("exhausted") or []),
        # WHY the search fell short, named rather than implied: a capped query,
        # an unresolved assignment, a trimmed result, a dropped result, a
        # failed tool. A reader (and the next analysis) can see which.
        "retrieval_limitations": sorted(retrieval.get("limitation_kinds") or []),
        "retrieval": {
            k: retrieval.get(k) for k in
            ("tool_calls", "rows_retrieved", "events_retrieved",
             "evidence_delivered")
            if k in retrieval
        },
        # The distinction that has to survive: we may say exactly what the
        # records we READ show, and we may not say what the whole scope
        # contains unless both halves are complete.
        "supports_exhaustive_claims": bool(
            completeness.get("counts_exact", True)
            and completeness.get("scope_membership_complete", True)
            and completeness.get("absence_established", True)
            and retrieval_complete
        ),
    }


class FindingRejected(Exception):
    """A finding failed a deterministic check. Carries every reason."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def scope_key(
    *,
    account_id: int | None,
    surfaces: list[str] | None,
    breadth: str | None,
    visible_user_ids: list[int] | None,
    whose: str,
    person_id: int | None,
    viewer_user_id: int | None,
    days: int,
    timezone_name: str,
) -> str:
    """The AUDIENCE's identity: everything that changes what may be shown.

    Findings are stored under this and served under a freshly recomputed one,
    so a permission change makes the old slice unreachable in the same request
    — there is no revocation lag to reason about, because the key simply stops
    matching.

    `viewer_user_id` is in the key because personal attention is in the
    analysis: two people on the same team with the same seat still have
    different desks.
    """
    payload = {
        "account": account_id,
        "surfaces": sorted(surfaces or []),
        "breadth": breadth,
        # None (company-wide, no filter) and [] (nobody) are different keys, as
        # they are different answers everywhere else.
        "visible": None if visible_user_ids is None else sorted(visible_user_ids),
        "whose": whose,
        "person": person_id,
        "viewer": viewer_user_id,
        "days": days,
        "tz": timezone_name,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def job_key(scope: str, evidence_version: str, prompt_version: str) -> str:
    """The unit of work. Same audience + same records + same prompts = the same
    question, so asking again while one is pending joins it instead of buying a
    second model call."""
    return hashlib.sha256(
        f"{scope}:{evidence_version}:{prompt_version}".encode()
    ).hexdigest()[:32]


def finding_key(category: str, entities: list[dict[str, Any]], topic: str) -> str:
    """The CONDITION's identity, stable across re-analysis.

    Built from what the finding is about, not from how it was worded, so
    tomorrow's run updates the same row rather than publishing a near-duplicate
    with a different sentence.
    """
    ents = sorted(
        f"{e.get('kind')}:{e.get('id')}" for e in (entities or [])
        if e.get("kind") and e.get("id") is not None
    )
    blob = json.dumps(
        {"category": category, "entities": ents, "topic": (topic or "").strip().lower()},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Deterministic validation
# ---------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _snapshot_value(snapshot: dict[str, Any], path: str) -> Any:
    """Resolve a dotted path into the authoritative snapshot, or None."""
    node: Any = snapshot
    for part in (path or "").split("."):
        if not part:
            return None
        if isinstance(node, list):
            try:
                node = node[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def validate_finding(
    finding: dict[str, Any],
    *,
    snapshot: dict[str, Any],
    evidence_index: dict[str, dict[str, Any]],
    calculations: dict[str, dict[str, Any]],
    financial_visible: bool,
    retrieval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check a model-proposed finding against the record. Raises on rejection.

    `evidence_index` holds every evidence item the investigation actually
    retrieved, keyed `"{kind}:{ref}"`. A reference outside it is a FABRICATION
    — the model naming a run id it never fetched — and is fatal, not a warning.

    `calculations` holds the server-side computations performed this analysis.
    Numeric claims must resolve to one of these or to a snapshot path.

    `retrieval` is the investigation's budget report. It is part of coverage
    for the same reason the snapshot's completeness is: a search that stopped
    at a cap did not see everything, and a claim about what is NOT there
    cannot rest on it.

    Returns the finding with normalized fields and a `validation` block. A
    finding that survives may still be narrowed: a `hypothesis` whose evidence
    is thin comes back `qualified` rather than `supported`.
    """
    reasons: list[str] = []
    out = dict(finding)

    # --- shape ---------------------------------------------------------
    category = str(out.get("category") or "").strip()
    if category not in CATEGORIES:
        reasons.append(f"category {category!r} is not one of {CATEGORIES}")
    claim_kind = str(out.get("claim_kind") or "").strip()
    if claim_kind not in CLAIM_KINDS:
        reasons.append(f"claim_kind {claim_kind!r} is not one of {CLAIM_KINDS}")

    title = str(out.get("title") or "").strip()
    explanation = str(out.get("explanation") or "").strip()
    if not title or len(title) > MAX_TITLE_CHARS:
        reasons.append("title must be present and under 90 characters")
    if not explanation or len(explanation) > MAX_EXPLANATION_CHARS:
        reasons.append("explanation must be present and under 420 characters")
    if _EMPTY_PHRASES.match(explanation):
        reasons.append("explanation restates a count without adding anything")

    # --- entities exist ------------------------------------------------
    entities = out.get("entities") or []
    if not isinstance(entities, list) or len(entities) > MAX_ENTITIES:
        reasons.append("entities must be a list of at most 12 references")
        entities = []
    for ent in entities:
        if not isinstance(ent, dict):
            reasons.append("each entity must be an object")
            continue
        kind = ent.get("kind")
        if kind not in ENTITY_KINDS:
            reasons.append(f"entity kind {kind!r} is not one of {ENTITY_KINDS}")
            continue
        key = f"{kind}:{ent.get('id')}"
        if key not in evidence_index and kind in ("run", "job", "agent"):
            reasons.append(f"entity {key} was never retrieved during this analysis")

    # --- evidence exists -----------------------------------------------
    evidence = out.get("evidence") or []
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
        reasons.append("evidence must be a list of at most 24 references")
        evidence = []
    resolved_evidence: list[dict[str, Any]] = []
    for ref in evidence:
        if not isinstance(ref, dict):
            reasons.append("each evidence reference must be an object")
            continue
        kind = ref.get("kind")
        if kind not in EVIDENCE_KINDS:
            reasons.append(f"evidence kind {kind!r} is not one of {EVIDENCE_KINDS}")
            continue
        key = f"{kind}:{ref.get('ref')}"
        if kind == "snapshot":
            if _snapshot_value(snapshot, str(ref.get("ref") or "")) is None:
                reasons.append(f"snapshot path {ref.get('ref')!r} does not exist")
                continue
        elif kind == "calculation":
            if str(ref.get("ref")) not in calculations:
                reasons.append(f"calculation {ref.get('ref')!r} was never performed")
                continue
        elif key not in evidence_index:
            reasons.append(f"evidence {key} was never retrieved during this analysis")
            continue
        item = dict(ref)
        # Pin what the evidence said AT ANALYSIS TIME. If the record moves, the
        # detail view can show the reference as stale instead of quietly
        # presenting a different row as the original basis.
        item["digest"] = _evidence_digest(kind, ref.get("ref"), evidence_index,
                                          calculations, snapshot)
        resolved_evidence.append(item)
    if not resolved_evidence:
        reasons.append("a finding with no evidence is not a finding")

    # --- claims --------------------------------------------------------
    claims = out.get("claims") or []
    if not isinstance(claims, list) or not claims or len(claims) > MAX_CLAIMS:
        reasons.append("claims must be a non-empty list of at most 6")
        claims = []
    evidence_keys = {f"{r.get('kind')}:{r.get('ref')}" for r in resolved_evidence}
    for claim in claims:
        if not isinstance(claim, dict):
            reasons.append("each claim must be an object")
            continue
        text = str(claim.get("text") or "").strip()
        kind = claim.get("kind")
        if not text:
            reasons.append("a claim needs text")
        if kind not in CLAIM_KINDS:
            reasons.append(f"claim kind {kind!r} is not one of {CLAIM_KINDS}")
        supports = claim.get("evidence") or []
        if not isinstance(supports, list) or not supports:
            reasons.append(f"claim {text[:40]!r} cites no evidence")
            supports = []
        for s in supports:
            if str(s) not in evidence_keys:
                reasons.append(f"claim cites {s!r}, which is not in this finding's evidence")

        # Numeric claims must resolve to the server's own number.
        value = _num(claim.get("value"))
        if value is not None:
            ref = str(claim.get("metric_ref") or "")
            resolved = None
            if ref.startswith("snapshot:"):
                resolved = _num(_snapshot_value(snapshot, ref[len("snapshot:"):]))
            elif ref.startswith("calc:"):
                calc = calculations.get(ref[len("calc:"):])
                resolved = _num((calc or {}).get("value"))
            if resolved is None:
                reasons.append(
                    f"numeric claim {text[:40]!r} has no resolvable metric_ref"
                )
            elif abs(resolved - value) > 1e-6:
                reasons.append(
                    f"numeric claim {text[:40]!r} says {value} but "
                    f"{ref} is {resolved}"
                )

    # --- the record's own outcome overrules the wording ----------------
    # Checked against the retrieved rows, not against the sentence. A finding
    # may describe work that was given up on; it may not describe it as work
    # that got done, and it may not build a WIN out of it. This is
    # deterministic on purpose: an abandonment reframed as a success is the
    # single worst thing this layer could publish, and a second model agreeing
    # it reads nicely is not a check.
    cited_runs = [
        evidence_index.get(f"run:{ref.get('ref')}") or {}
        for ref in resolved_evidence if ref.get("kind") == "run"
    ]
    outcomes = [str(r.get("outcome") or "") for r in cited_runs if r]
    if outcomes:
        any_completed = "completed" in outcomes
        if category == "positive_change" and not any_completed:
            reasons.append(
                "a positive finding cites no run the record says completed "
                f"(outcomes: {sorted(set(outcomes))})"
            )
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            text = str(claim.get("text") or "")
            cited = [
                evidence_index.get(f"run:{k.split(':', 1)[1]}") or {}
                for k in (claim.get("evidence") or [])
                if isinstance(k, str) and k.startswith("run:")
            ]
            cited_outcomes = [str(r.get("outcome") or "") for r in cited if r]
            if not cited_outcomes:
                continue
            # The record says it finished; the sentence says it did not, or
            # vice versa. Either way the wording loses.
            if _asserts(_COMPLETION_RE, text) and "completed" not in cited_outcomes:
                reasons.append(
                    f"claim {text[:50]!r} says work was completed, but the runs "
                    f"it cites were {sorted(set(cited_outcomes))}"
                )
            if _asserts(_FAILURE_RE, text) and set(cited_outcomes) == {"completed"}:
                # The recovered-error case: a failing step inside a run that
                # finished is FRICTION. Calling it failed work is the mistake
                # this catches, and it is a contradiction of the record rather
                # than a matter of tone.
                reasons.append(
                    f"claim {text[:50]!r} says work failed, but every run it "
                    "cites is recorded as completed — a failing step inside a "
                    "completed run is friction, not failed work"
                )

    # --- a verdict is not an observation -------------------------------
    # "Wasteful", "redundant", "inefficient" are judgements about whether work
    # SHOULD have happened. The record holds what happened, never whether it
    # was worth doing, so these can never be observations. Repetition is a
    # question; calling it waste is a conclusion that needs its own evidence.
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        text = str(claim.get("text") or "")
        if claim.get("kind") == "observation" and _asserts(_JUDGEMENT_RE, text):
            reasons.append(
                f"claim {text[:50]!r} states a judgement as an observation — "
                "the record shows what happened, not whether it was worthwhile"
            )

    # --- coverage and exactness ----------------------------------------
    # DERIVED, never merged. `setdefault` used to let a model-supplied
    # `coverage` stand, so a draft that simply asserted `counts_exact: true`
    # over an incomplete snapshot got "No other job is affected" published as
    # supported. Completeness is a fact about the SEARCH, and the only things
    # that know it are the snapshot and the retrieval budget.
    coverage = derive_coverage(snapshot, retrieval=retrieval)
    out["coverage"] = coverage
    if not coverage["counts_exact"] or not coverage["retrieval_complete"]:
        # The counts are a floor — either because the scope's membership is
        # incomplete, or because retrieval stopped before the end. A percentage
        # or a "no X at all" built on a floor is not a weaker statement, it is
        # a wrong one. Note the distinction this preserves: "these four runs
        # stopped at the same step" is an exact observation about records we
        # actually read, and stays publishable; "no other job is affected" is
        # an exhaustive claim about a scope we did not finish searching, and
        # does not.
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            text = str(claim.get("text") or "")
            if re.search(r"\d+(\.\d+)?\s*%", text) or re.search(
                r"\b(none|no other|never|only|all of)\b", text, re.I
            ):
                reasons.append(
                    "an incomplete search cannot support a percentage or an "
                    f"exhaustive claim: {text[:60]!r} "
                    f"(counts_exact={coverage['counts_exact']}, "
                    f"retrieval_complete={coverage['retrieval_complete']}, "
                    f"limitations={coverage['retrieval_limitations']})"
                )
            # A number computed over a partial result is a FLOOR, and says so
            # on the finding. The exact observation it came from stays
            # publishable; what it may not become is a whole.
            if claim.get("kind") == "calculation":
                claim["partial"] = True
                claim["qualifier"] = "at_least"

    # --- financial gate ------------------------------------------------
    mentions_money = _mentions_money(out)
    if mentions_money and not financial_visible:
        reasons.append("financial content in a finding for a reader without Cost")
    out["requires_financial"] = bool(mentions_money)

    # --- next step -----------------------------------------------------
    step = out.get("next_step")
    if step is not None:
        if not isinstance(step, dict) or step.get("kind") not in NEXT_STEP_KINDS:
            reasons.append(f"next_step.kind must be one of {NEXT_STEP_KINDS}")
        else:
            step["text"] = str(step.get("text") or "").strip()[:200]
            out["next_step"] = step

    # --- graphic -------------------------------------------------------
    graphic = out.get("graphic")
    if graphic is not None:
        if not isinstance(graphic, dict) or graphic.get("kind") not in GRAPHIC_KINDS:
            reasons.append(f"graphic.kind must be one of {GRAPHIC_KINDS}")
        elif graphic.get("kind") != "none":
            series = graphic.get("series")
            if not isinstance(series, list) or not series:
                reasons.append("a graphic needs a series")
            else:
                for point in series:
                    if not isinstance(point, dict):
                        reasons.append("each graphic point must be an object")
                        continue
                    ref = str(point.get("metric_ref") or "")
                    resolved = None
                    if ref.startswith("snapshot:"):
                        resolved = _num(_snapshot_value(snapshot, ref[len("snapshot:"):]))
                    elif ref.startswith("calc:"):
                        resolved = _num((calculations.get(ref[len("calc:"):]) or {}).get("value"))
                    if resolved is None:
                        reasons.append(
                            f"graphic point cites {ref!r}, which resolves to nothing"
                        )
                    else:
                        # The server's number wins. The model labels; it does
                        # not supply chart values.
                        point["value"] = resolved
                    # A label that says "all" or "total" over an incomplete
                    # search reads as a whole where the data is a floor.
                    if not coverage["counts_exact"] or not coverage["retrieval_complete"]:
                        label = str(point.get("label") or "")
                        if _EXHAUSTIVE_RE.search(label):
                            reasons.append(
                                f"chart label {label[:40]!r} implies a complete "
                                "picture the evidence does not cover"
                            )
                        point["partial"] = True

    if reasons:
        raise FindingRejected(reasons)

    out["category"] = category
    out["claim_kind"] = claim_kind
    out["title"] = title
    out["explanation"] = explanation
    out["evidence"] = resolved_evidence
    out["claims"] = claims
    out["entities"] = entities

    # A hypothesis is publishable but never `supported`: the label has to carry
    # the difference between "this happened" and "this may be why".
    confidence = str(out.get("confidence") or "").strip()
    if confidence not in CONFIDENCES:
        confidence = "qualified"
    if claim_kind == "hypothesis":
        confidence = "qualified"
    if not (
        coverage["counts_exact"]
        and coverage["scope_membership_complete"]
        and coverage["retrieval_complete"]
    ):
        confidence = "qualified"
    out["confidence"] = confidence
    out["validation"] = {
        "deterministic": "passed",
        "evidence_items": len(resolved_evidence),
        "numeric_claims_checked": sum(
            1 for c in claims if isinstance(c, dict) and _num(c.get("value")) is not None
        ),
    }
    return out


# Wording that asserts work got done. Matched against a claim whose cited runs
# the record says were abandoned — the mismatch is the finding, not the prose.
_COMPLETION_RE = re.compile(
    r"\b(completed?|completion|finished|delivered|succeeded|successful|"
    r"resolved|shipped|got done|went through)\b",
    re.I,
)


# Wording that asserts work did NOT get done. Matched against claims whose
# cited runs the record says completed.
_FAILURE_RE = re.compile(
    r"\b(failed|failing|failure|did ?n.t (complete|finish)|never (completed|finished)|"
    r"was lost|broke|broken|errored out|gave up)\b",
    re.I,
)

# A negated match is not an assertion. "Completed with no failing step" says
# the opposite of "failed", and "did not complete" says the opposite of
# "completed" — so both outcome checks below look behind each match before
# treating it as a claim about what happened.
_NEGATOR_RE = re.compile(r"\b(no|not|never|without|n.t|zero)\s+(\w+\s+){0,2}$", re.I)


def _asserts(pattern: "re.Pattern[str]", text: str) -> bool:
    """True when `text` actually ASSERTS what the pattern describes."""
    for m in pattern.finditer(text or ""):
        window = text[max(0, m.start() - 24):m.start()]
        if not _NEGATOR_RE.search(window):
            return True
    return False


# Verdicts about whether work was WORTH doing. The record cannot hold these.
_JUDGEMENT_RE = re.compile(
    r"\b(wasteful|waste of|redundant|unnecessary|pointless|inefficient|"
    r"inefficiency|sloppy|badly|poorly)\b",
    re.I,
)


_MONEY_RE = re.compile(
    r"[$£€]|\b(cost|costs|costing|spend|spending|spent|dollar|usd|price|"
    r"pricing|budget|bill|billed|invoice|savings?|cheaper|expensive)\b",
    re.I,
)


def _mentions_money(finding: dict[str, Any]) -> bool:
    """Does anything a reader would SEE talk about money?

    Checked over the rendered surface, not over a `requires_financial` flag the
    model could set to False while writing about spend. The financial gate is
    a property of the text, not a promise about it.
    """
    parts = [
        str(finding.get("title") or ""),
        str(finding.get("explanation") or ""),
        str(finding.get("consequence") or ""),
        json.dumps(finding.get("claims") or []),
        json.dumps(finding.get("next_step") or {}),
    ]
    if any(_MONEY_RE.search(p) for p in parts):
        return True
    for ref in finding.get("evidence") or []:
        if isinstance(ref, dict) and str(ref.get("ref") or "").startswith("financial"):
            return True
    return False


def _evidence_digest(
    kind: str,
    ref: Any,
    evidence_index: dict[str, dict[str, Any]],
    calculations: dict[str, dict[str, Any]],
    snapshot: dict[str, Any],
) -> str:
    """A hash of what this evidence said when the finding was written.

    Staleness detection, not integrity protection: when the detail view
    re-reads a run and the digest no longer matches, it can say "the record
    changed since this was written" instead of presenting today's row as the
    basis for yesterday's sentence.
    """
    if kind == "calculation":
        payload = calculations.get(str(ref)) or {}
    elif kind == "snapshot":
        payload = {"value": _snapshot_value(snapshot, str(ref))}
    else:
        payload = evidence_index.get(f"{kind}:{ref}") or {}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def evidence_digest_for(payload: Any) -> str:
    """The same digest, for a freshly re-read record."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Serving: re-check, then shape
# ---------------------------------------------------------------------------
# Storage is keyed by the audience slice, so a reader whose permissions changed
# simply computes a different scope_key and cannot reach the old rows. These
# helpers add the second belt: a per-finding re-check at serve time, so a
# finding that mentions money is withheld the moment the Cost surface goes
# away, even if it somehow shares a slice.


def serve_finding(
    finding: dict[str, Any],
    *,
    scope_key_now: str,
    financial_visible: bool,
    detail: bool = False,
) -> dict[str, Any] | None:
    """One finding as this reader may see it right now, or None to withhold.

    Withheld, never redacted: a finding whose whole point is a cost pattern
    becomes an empty shell if you strip the money out of it, and an empty shell
    still tells the reader there is a cost pattern they cannot see.
    """
    if finding.get("scope_key") != scope_key_now:
        return None
    if finding.get("requires_financial") and not financial_visible:
        return None

    out = {
        "id": finding["id"],
        "category": finding["category"],
        "claim_kind": finding["claim_kind"],
        "confidence": finding["confidence"],
        "title": finding["title"],
        "explanation": finding["explanation"],
        "consequence": finding.get("consequence"),
        "entities": finding.get("entities") or [],
        "next_step": finding.get("next_step"),
        "graphic": finding.get("graphic"),
        "coverage": finding.get("coverage") or {},
        "state": finding.get("state"),
        "analyzed_at": finding.get("analyzed_at"),
        "evidence_cutoff": finding.get("evidence_cutoff"),
        "period_start_utc": finding.get("period_start_utc"),
        "period_end_utc": finding.get("period_end_utc"),
        "timezone": finding.get("timezone"),
        "evidence_count": len(finding.get("evidence") or []),
        "requires_financial": bool(finding.get("requires_financial")),
    }
    if detail:
        out["claims"] = finding.get("claims") or []
        out["evidence"] = finding.get("evidence") or []
        out["uncertainty"] = finding.get("uncertainty") or []
        out["prompt_version"] = finding.get("prompt_version")
        out["model"] = finding.get("model")
        out["evidence_version"] = finding.get("evidence_version")
    return out


def navigation_for(finding: dict[str, Any], carry_query: dict[str, Any]) -> dict[str, Any]:
    """Where a reader can actually go from here.

    Typed targets for entities that exist, and an HONEST label for the one that
    does not: `/work/items` still has no period filter (HOME_SNAPSHOT.md, gap
    1), so a link to "the work behind this finding" cannot promise the exact
    window. It says so rather than claiming a filtered destination that would
    come back wider than the finding.
    """
    targets = []
    for ent in finding.get("entities") or []:
        kind, ident = ent.get("kind"), ent.get("id")
        if kind == "run" and ident is not None:
            targets.append({
                "kind": "run", "id": ident, "path": f"/work/items/{ident}",
                "exact": True,
            })
        elif kind == "job" and ident is not None:
            targets.append({
                "kind": "job", "id": ident,
                "path": "/work/items", "query": {"workflow_id": ident},
                "exact": False,
                "note": (
                    "Filters to this job, but not to the finding's period — "
                    "the work list has no period filter yet."
                ),
            })
        elif kind == "agent" and ident is not None:
            targets.append({
                "kind": "agent", "id": ident,
                "path": f"/agents/{ident}/summary", "exact": True,
            })
    return {"targets": targets, "carry_query": dict(carry_query or {})}


def stale_evidence(
    finding: dict[str, Any], reread: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Which evidence references no longer match what they pointed at.

    Called when a detail view re-reads the records. A changed digest does not
    invalidate the finding — records legitimately move on — but the reader is
    told which basis shifted rather than being shown today's row as the
    original reason.
    """
    out = []
    for ref in finding.get("evidence") or []:
        key = f"{ref.get('kind')}:{ref.get('ref')}"
        current = reread.get(key)
        if current is None:
            out.append({**ref, "status": "missing"})
        elif evidence_digest_for(current) != ref.get("digest"):
            out.append({**ref, "status": "changed"})
    return out
