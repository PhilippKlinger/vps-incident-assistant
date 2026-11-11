import re
from dataclasses import dataclass

from evals.cases import Case

_STOPWORDS = {"the", "and", "for", "with", "from", "request", "failed", "status", "code"}
_STOPWORDS |= {"http", "kuma", "down"}
_WORDS = re.compile(r"[a-z0-9]+")
_MIN_LENGTH = 3


@dataclass(frozen=True)
class Answer:
    suspected_change: str  # change id or "none"
    runbook: str  # anchor id or "none"


def latest_change(case: Case) -> Answer:
    changes = case.context.changes
    return Answer(changes[0].id if changes else "none", "none")


def _symptom_tokens(case: Case) -> set[str]:
    context = case.context
    texts = [context.event.msg, context.rule.category.replace("_", " ")]
    texts += [fact.text for fact in context.facts if fact.source == "probe"]
    words = _WORDS.findall(" ".join(texts).lower())
    return {w for w in words if len(w) >= _MIN_LENGTH and w not in _STOPWORDS}


def _tokens(text: str) -> set[str]:
    return set(_WORDS.findall(text.lower()))


def keyword_match(case: Case) -> Answer:
    symptoms = _symptom_tokens(case)
    best_change, best = "none", 0
    # Changes are newest first, so a strict comparison keeps the newer one on ties.
    for change in case.context.changes:
        overlap = len(symptoms & _tokens(" ".join(change.files)))
        if overlap > best:
            best_change, best = change.id, overlap
    best_anchor, best = "none", 0
    for anchor in case.context.anchors:
        overlap = len(symptoms & _tokens(anchor.title))
        if overlap > best:
            best_anchor, best = anchor.id, overlap
    return Answer(best_change, best_anchor)
