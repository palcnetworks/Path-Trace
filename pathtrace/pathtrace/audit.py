"""
audit.py — sweep every CONFIG_DB object of a type and report which ones never reached ASIC_DB.

`pathtrace --intent ...` answers "why did THIS object fail?". Audit answers the question an
operator asks first: "what else on this switch is silently broken?". It enumerates the
objects through the registry's own CONFIG_DB key template (no per-type code), builds an
intent for each, traces it with the same walker, and correlates the syslog for the broken
ones.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional

from .resolver import Registry, SEP, resolve, IntentParseError, RegistryError
from .walker import trace, TraceResult
from .correlator import correlate
from .explainer import explain
from .dbclient import DBBackend, CachedBackend

_TOKEN = re.compile(r"\{(\w+)\}")


class NotAuditable(RegistryError):
    pass


@dataclass
class AuditEntry:
    intent: str
    status: str                    # healthy | broken | anomalous
    break_layer: Optional[str] = None
    sai_status_codes: list = field(default_factory=list)
    cause: str = ""
    trace: Optional[TraceResult] = None
    # --- depth analysis (populated only when audit is run with_deps=True) ---
    root_cause: Optional[str] = None            # intent of the deepest unhealthy node
    root_cause_is_dep: bool = False             # True if that node is an upstream dependency
    root_cause_break_layer: Optional[str] = None


@dataclass
class RootCauseGroup:
    """One upstream dependency that explains a set of broken objects (fleet-wide root cause)."""
    intent: str                    # the shared dependency intent, e.g. "vlan 100"
    break_layer: Optional[str]
    affected: list = field(default_factory=list)   # the AuditEntry objects it cascades to


@dataclass
class AuditReport:
    object_type: str
    enumerated: int = 0            # CONFIG_DB keys found under the type's table
    skipped: int = 0               # keys that don't fit the type's key template
    entries: list = field(default_factory=list)
    errors: list = field(default_factory=list)     # (key, message)
    with_deps: bool = False        # whether depth (dependency) analysis was performed

    def count(self, status: str) -> int:
        return sum(1 for e in self.entries if e.status == status)

    @property
    def clean(self) -> bool:
        return not any(e.status != "healthy" for e in self.entries)

    def root_cause_groups(self) -> list:
        """Collapse broken objects that share one upstream dependency into single findings.

        A single broken VLAN that strands 48 members should be reported ONCE, with its blast
        radius, not as 48 look-alike failures. Groups are ordered by blast radius (largest
        first) so the operator sees "fix this first" at the top.
        """
        groups: dict = {}
        for e in self.entries:
            if e.status != "healthy" and e.root_cause_is_dep and e.root_cause:
                key = (e.root_cause, e.root_cause_break_layer)
                groups.setdefault(key, RootCauseGroup(e.root_cause, e.root_cause_break_layer)).affected.append(e)
        return sorted(groups.values(), key=lambda g: -len(g.affected))


def _config_hop(spec: dict) -> Optional[dict]:
    for h in spec["hops"]:
        if h["db"] == "CONFIG_DB" and not h.get("na") and "key" in h:
            return h
    return None


def is_auditable(spec: dict) -> bool:
    return _config_hop(spec) is not None


def key_regex(template: str) -> tuple[re.Pattern, str]:
    """Invert a CONFIG_DB key template into (regex, scan_prefix).

    'ACL_RULE{SEP}{table}{SEP}{rule}' -> ^ACL_RULE\\|(?P<table>[^|]+)\\|(?P<rule>[^|]+)$
    """
    sep = SEP["CONFIG_DB"]
    t = template.replace("{SEP}", sep)
    parts, last, prefix_end = [], 0, None
    for m in _TOKEN.finditer(t):
        parts.append(re.escape(t[last:m.start()]))
        if prefix_end is None:
            prefix_end = m.start()
        parts.append(f"(?P<{m.group(1)}>[^{re.escape(sep)}]+?)")
        last = m.end()
    parts.append(re.escape(t[last:]))
    scan_prefix = t[:prefix_end] if prefix_end is not None else t
    return re.compile("^" + "".join(parts) + "$"), scan_prefix


def _intent_for(spec: dict, groups: dict) -> str:
    audit = spec.get("audit") or {}
    if audit.get("intent"):
        return audit["intent"].format(**groups)
    order = sorted(re.compile(spec["match"]).groupindex.items(), key=lambda kv: kv[1])
    return spec["intent_prefix"] + " " + " ".join(groups[name] for name, _ in order)


def audit_type(backend: DBBackend, registry: Registry, object_type: str,
               with_deps: bool = False) -> AuditReport:
    spec = registry.spec(object_type)
    hop = _config_hop(spec)
    if hop is None:
        raise NotAuditable(
            f"{object_type} has no CONFIG_DB representation, so its objects cannot be "
            f"enumerated from configuration")
    if not isinstance(backend, CachedBackend):
        backend = CachedBackend(backend)
    rx, prefix = key_regex(hop["key"])
    report = AuditReport(object_type=object_type, with_deps=with_deps)
    logs = backend.logs()

    prefixes = (spec.get("audit") or {}).get("scan_prefixes") or [prefix]
    scanned = sorted({kv[0]: kv for pf in prefixes
                      for kv in backend.scan_prefix("CONFIG_DB", pf)}.values())
    for key, _fields in scanned:
        report.enumerated += 1
        m = rx.match(key)
        if not m:
            report.skipped += 1
            continue
        try:
            intent_str = _intent_for(spec, m.groupdict())
            resolved = resolve(intent_str)
        except (IntentParseError, KeyError) as e:
            report.errors.append((key, str(e)))
            continue
        if resolved.object_type != object_type:
            report.errors.append((key, f"intent {intent_str!r} resolved to {resolved.object_type}"))
            continue
        tr = trace(backend, resolved)
        if tr.healthy:
            report.entries.append(AuditEntry(resolved.human, "healthy", trace=tr))
            continue
        corr = correlate(tr, logs)
        expl = explain(tr, corr)
        entry = AuditEntry(
            resolved.human, "anomalous" if tr.anomalous else "broken", tr.break_layer,
            list(corr.sai_status_codes), expl.cause, tr)
        # DEPTH: for a broken object, walk its upstream dependencies and name the deepest
        # unhealthy one. Only done for broken objects (healthy ones have, by construction,
        # a working upstream for OID-chain-matched types) and only when explicitly requested
        # -- breadth stays cheap; depth runs over the small broken set, sharing the cached
        # backend so dependencies common to many objects are read once.
        if with_deps and resolved.dependencies:
            from .deps import build_tree, likely_root_cause
            tree = build_tree(backend, intent_str)
            cause = likely_root_cause(tree)
            if cause is not None:
                entry.root_cause = cause.intent
                entry.root_cause_is_dep = cause is not tree
                entry.root_cause_break_layer = cause.trace.break_layer if cause.trace else None
        report.entries.append(entry)
    return report
