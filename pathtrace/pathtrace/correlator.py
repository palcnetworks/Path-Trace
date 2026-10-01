"""
correlator.py — Pull the syslog lines that explain a break.

When the walker finds a break at layer N, the interesting evidence is the
orchagent/syncd log activity about *this object* around the break. We score each log
line by relevance to the traced object (discriminator, object type, SAI status codes)
and return the top lines plus any SAI_STATUS_* codes found — the codes are the single
most useful signal for the explanation step.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from .walker import TraceResult

# The daemons that actually program the datapath.
_RELEVANT_DAEMONS = ("orchagent", "syncd", "swss")
_SAI_STATUS_RE = re.compile(r"SAI_STATUS_[A-Z_]+")
_ERR_LEVEL_RE = re.compile(r"\b(ERR|ERROR|WARNING|CRIT)\b")


@dataclass
class Correlation:
    lines: list[str] = field(default_factory=list)
    sai_status_codes: list[str] = field(default_factory=list)
    crm_hit: bool = False
    # True when daemon log lines existed to search but NONE genuinely named this object --
    # distinct from simply having no logs at all. Lets the report say "we looked and found
    # nothing tied to this object" instead of silently showing nothing.
    no_specific_match: bool = False


def _tokens(trace: TraceResult) -> tuple[list[str], list[str]]:
    """Derive correlation tokens generically from the traced object — no per-type code.

    Returns (specific, generic):
      specific — tokens that actually name THIS object (discriminator parts, object-type
                 name words). A line needs at least one of these to be considered evidence
                 about this object at all.
      generic  — always-present hardware/programming signals (SAI_STATUS, CrmResource,
                 THRESHOLD_EXCEEDED). These boost relevance among lines that already have a
                 specific match; they must never be sufficient on their own, since e.g. a
                 SAI_STATUS_* code from a totally unrelated SAI API (queues, ports, ...) is
                 not evidence about a completely different object just because both happen
                 to be ERR-level SAI failures.
    """
    disc = trace.intent.discriminator
    toks = [disc]
    toks += [p for p in re.split(r"[/\s:|]", disc) if p]
    # object-type name words, e.g. "vlan_member" -> vlan, member, VLAN, MEMBER
    for w in trace.intent.object_type.split("_"):
        if w:
            toks += [w, w.upper(), w.capitalize()]
    # de-dup preserving order
    seen, specific = set(), []
    for t in toks:
        if t and t not in seen:
            seen.add(t)
            specific.append(t)
    generic = ["CrmResource", "THRESHOLD_EXCEEDED", "SAI_STATUS"]
    return specific, generic


def correlate(trace: TraceResult, logs: list[str], top_k: int = 6) -> Correlation:
    specific_toks, generic_toks = _tokens(trace)
    scored: list[tuple[int, str]] = []
    daemon_lines_seen = False
    for line in logs:
        if not any(d in line for d in _RELEVANT_DAEMONS):
            continue
        daemon_lines_seen = True
        if not any(t in line for t in specific_toks):
            # No genuine link to THIS object -- a generic SAI_STATUS/CRM hit alone is not
            # evidence about this specific trace, just noise that happens to look important
            # (e.g. an unrelated SAI_API_QUEUE error scoring high on every trace because it
            # contains *a* SAI_STATUS_* code).
            continue
        score = sum(2 for t in specific_toks if t in line)
        score += sum(2 for t in generic_toks if t in line)
        if _SAI_STATUS_RE.search(line):
            score += 3
        if _ERR_LEVEL_RE.search(line):
            score += 2
        scored.append((score, line.rstrip("\n")))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [ln for _, ln in scored[:top_k]]

    # Preserve chronological order among the selected lines for readability.
    chrono = [ln for ln in (l.rstrip("\n") for l in logs) if ln in top]

    # Extract codes/CRM hit from the FULL lines first -- truncating for display must not
    # risk losing a status code that happened to fall after the cut point.
    codes: list[str] = []
    for ln in chrono:
        for m in _SAI_STATUS_RE.findall(ln):
            if m not in codes:
                codes.append(m)
    crm = any("CrmResource" in ln or "THRESHOLD_EXCEEDED" in ln for ln in chrono)

    # Some SONiC log lines are multi-KB (e.g. a full ConfigMgmt JSON dump) -- cap length so
    # one such line can't flood the terminal/JSON output regardless of why it matched.
    _MAX_LINE = 300
    chrono = [ln if len(ln) <= _MAX_LINE else ln[:_MAX_LINE] + " ...[truncated]" for ln in chrono]

    no_specific_match = daemon_lines_seen and not chrono
    return Correlation(lines=chrono, sai_status_codes=codes, crm_hit=crm,
                       no_specific_match=no_specific_match)
