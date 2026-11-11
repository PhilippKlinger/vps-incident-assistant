import re
from datetime import timedelta

from assistant.context import IncidentContext
from assistant.models import Analysis

_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+|www\.\S+", re.IGNORECASE)
_ESCAPE = re.compile(r"([`*_\[\]()!#|&\\])")
_ISSUE_REF = re.compile(r"#(\d)")
_CONFIDENCE = {"low": "niedrig", "medium": "mittel", "high": "hoch"}
_UNKNOWN = "unbekannt"


def sanitize(text: str, max_len: int) -> str:
    """Make untrusted text inert in GitHub Markdown: no links, mentions, images or HTML."""
    text = " ".join(text.split())
    # Brackets go first: removing them after the URL pass could reassemble "h<ttps://".
    text = text.replace("<", "").replace(">", "")
    text = _URL.sub("", text)
    text = _ESCAPE.sub(r"\\\1", text)
    text = text.replace("@", "(at)")
    # "#12" would turn into an issue reference even when the "#" is escaped.
    text = _ISSUE_REF.sub(r"# \1", text).strip()[:max_len]
    # A cut inside an escape pair leaves a lone backslash that would eat the next character.
    trailing = len(text) - len(text.rstrip("\\"))
    return text[:-1] if trailing % 2 else text


def _short(repo: str) -> str:
    return repo.split("/")[-1]


def _facts(ctx: IncidentContext) -> str:
    return "\n".join(f"- {fact.id}: {sanitize(fact.text, 300)}" for fact in ctx.facts)


def _rule(ctx: IncidentContext) -> str:
    rule = ctx.rule
    return f"Kategorie: {rule.category}\nSchwere: {rule.severity}\nGrund: {rule.reason}"


def _changes(ctx: IncidentContext) -> str:
    if not ctx.github_ok:
        return "nicht abrufbar"
    if not ctx.changes:
        return "keine"
    return "\n".join(
        f"- {c.id} · {_short(c.repo)} · {c.sha[:7]} · {sanitize(c.title, 120)}" for c in ctx.changes
    )


def issue_title(ctx: IncidentContext) -> str:
    display_name = ctx.project.display_name
    title = f"Störung: {display_name} – {ctx.rule.category}"
    if ctx.event.monitor_name == display_name:
        return title
    return f"{title} ({sanitize(ctx.event.monitor_name, 120)})"


def issue_body(ctx: IncidentContext, *, analysis_pending: bool) -> str:
    note = "Analyse folgt." if analysis_pending else "Hinweis: ohne LLM-Analyse."
    return (
        f"### Regel-Ergebnis\n\n{_rule(ctx)}\n\n"
        f"### Fakten\n\n{_facts(ctx)}\n\n"
        f"### Änderungen im Zeitfenster\n\n{_changes(ctx)}\n\n"
        f"{note}"
    )


def append_comment(ctx: IncidentContext) -> str:
    name = sanitize(ctx.event.monitor_name, 120)
    return f"### Weiterer Alarm\n\nMonitor: {name}\n{_rule(ctx)}\n\n{_facts(ctx)}"


def flap_comment(ctx: IncidentContext) -> str:
    return f"### Erneut gestört kurz nach der Erholung\n\n{_rule(ctx)}\n\n{_facts(ctx)}"


def resolved_comment(duration: timedelta) -> str:
    # Measured from the issue creation, so a flap in between counts towards it.
    minutes = max(1, int(duration.total_seconds() // 60))
    return f"Erholt, {minutes} min nach Beginn der Störung. Nachbetrachtung offen."


def unavailable_comment(reason: str) -> str:
    return f"Analyse nicht verfügbar: {reason}. Es gilt das Regel-Ergebnis oben."


def _change_line(analysis: Analysis, ctx: IncidentContext) -> str:
    if analysis.suspected_change == "none":
        return "keine bestimmte Änderung"
    for change in ctx.changes:
        if change.id == analysis.suspected_change:
            return f"{change.id} · {_short(change.repo)} · {change.sha[:7]}"
    return _UNKNOWN


def _runbook_line(analysis: Analysis, ctx: IncidentContext) -> str:
    if analysis.runbook == "none":
        return "kein passender Abschnitt"
    for anchor in ctx.anchors:
        if anchor.id == analysis.runbook:
            slug = anchor.id.split("#", 1)[1]
            return f"[{sanitize(anchor.title, 120)}](https://github.com/{anchor.repo}#{slug})"
    return _UNKNOWN


def analysis_comment(analysis: Analysis, ctx: IncidentContext) -> str:
    evidence = ", ".join(sanitize(item, 20) for item in analysis.evidence)
    return (
        "### Analyse (Vermutung)\n\n"
        f"{sanitize(analysis.summary, 300)}\n\n"
        f"- Vermutete Änderung: {_change_line(analysis, ctx)}\n"
        f"  {sanitize(analysis.change_reasoning, 400)}\n"
        f"- Runbook: {_runbook_line(analysis, ctx)}\n"
        f"  {sanitize(analysis.runbook_reasoning, 300)}\n"
        f"- Belege: {evidence}\n"
        f"- Zuversicht: {_CONFIDENCE[analysis.confidence]}"
    )


def _lookup(wanted: str, rows) -> str:
    if wanted == "none":
        return "keine"
    return next((label for key, label in rows if key == wanted), _UNKNOWN)


def telegram_text(ctx: IncidentContext, issue_url: str, analysis: Analysis | None) -> str:
    name = ctx.project.display_name
    if analysis is None:
        rule = ctx.rule
        return f"{name}: {rule.category} ({rule.severity}) · {rule.reason} · {issue_url}"
    change = _lookup(
        analysis.suspected_change,
        ((c.id, f"{_short(c.repo)} {c.sha[:7]}") for c in ctx.changes),
    )
    runbook = _lookup(analysis.runbook, ((a.id, a.title) for a in ctx.anchors))
    number = issue_url.rstrip("/").rsplit("/", 1)[-1]
    return (
        f"{name}: vermutete Ursache {change} · Runbook: {runbook} · Issue #{number} · {issue_url}"
    )


def monitor_up_comment(monitor_name: str) -> str:
    return f"{sanitize(monitor_name, 120)} wieder erreichbar, weitere Monitore noch gestört."
