"""
walker.py — Walk a resolved intent across the SONiC datapath and locate the break.

For each hop (CONFIG_DB -> APPL_DB -> STATE_DB -> ASIC_DB) we determine one of:
  PRESENT   object found at this layer
  ABSENT    object not found at this layer
  N/A       this layer legitimately doesn't hold this object (e.g. CONFIG_DB for a
            learned route) — not a break, just skipped

The break is the FIRST layer that is ABSENT while a preceding layer was PRESENT.
That transition (present -> absent) is exactly where intent stopped propagating.
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from .resolver import ResolvedIntent, HopSpec
from .dbclient import DBBackend


class HopStatus(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"          # the break: present upstream, absent here
    NOT_REACHED = "NOT_REACHED"  # downstream of the break; never had a chance
    NA = "N/A"


@dataclass
class HopResult:
    db: str
    status: HopStatus
    key: str | None            # the key we found (or looked for)
    fields: dict | None        # the object's field-map if present
    note: str
    match: str = ""            # how an ASIC_DB match was made (asicmatch.*), "" for exact keys
    detail: str = ""           # why a scan-based hop is ABSENT (which link of the chain broke)


@dataclass
class TraceResult:
    intent: ResolvedIntent
    hops: list[HopResult]
    break_layer: str | None    # db name where propagation broke, or None if healthy
    healthy: bool
    anomalous: bool = False    # a later hop is PRESENT despite an earlier one being ABSENT --
                               # real propagation can never skip a layer, so this means the
                               # later match isn't verified to be this specific object (see
                               # trace()'s docstring note below)

    @property
    def last_present_layer(self) -> str | None:
        last = None
        for h in self.hops:
            if h.status == HopStatus.PRESENT:
                last = h.db
        return last


def _check_hop(backend: DBBackend, hop: HopSpec) -> HopResult:
    # Layers that don't hold this object type at all
    if hop.key is None and hop.asic_type is None:
        return HopResult(hop.db, HopStatus.NA, None, None, hop.note)

    # ASIC_DB hop with an instance-level matcher (see asicmatch.py)
    if hop.asic_finder is not None:
        out = hop.asic_finder(backend)
        if out.found:
            return HopResult(hop.db, HopStatus.PRESENT, out.key, out.fields, hop.note,
                             out.match, out.detail)
        return HopResult(hop.db, HopStatus.ABSENT, f"[scan {hop.asic_type}]", None, hop.note,
                         "", out.detail)

    # Legacy scan-based hop: scan by SAI type + predicate
    if hop.asic_type is not None:
        matches = backend.scan_type(hop.db, hop.asic_type)
        for key, fields in matches:
            if hop.asic_predicate is None or hop.asic_predicate(fields):
                return HopResult(hop.db, HopStatus.PRESENT, key, fields, hop.note)
        return HopResult(hop.db, HopStatus.ABSENT, f"[scan {hop.asic_type}]", None, hop.note)

    # Exact-key hop
    fields = backend.get_hash(hop.db, hop.key)
    if fields is not None:
        return HopResult(hop.db, HopStatus.PRESENT, hop.key, fields, hop.note)
    return HopResult(hop.db, HopStatus.ABSENT, hop.key, None, hop.note)


def trace(backend: DBBackend, intent: ResolvedIntent) -> TraceResult:
    hops: list[HopResult] = []
    for spec in intent.hops:
        hops.append(_check_hop(backend, spec))

    # Find the break: first ABSENT that follows at least one PRESENT.
    break_layer = None
    seen_present = False
    break_idx = None
    for i, h in enumerate(hops):
        if h.status == HopStatus.PRESENT:
            seen_present = True
        elif h.status == HopStatus.ABSENT and seen_present:
            break_layer = h.db
            break_idx = i
            break

    # Everything strictly after the break that is ABSENT is really NOT_REACHED.
    if break_idx is not None:
        for j in range(break_idx + 1, len(hops)):
            if hops[j].status == HopStatus.ABSENT:
                hops[j] = HopResult(hops[j].db, HopStatus.NOT_REACHED,
                                    hops[j].key, hops[j].fields, hops[j].note,
                                    hops[j].match, hops[j].detail)

    # Anomaly guard: a hop can be ABSENT while a LATER hop is still PRESENT when that later
    # layer's match can't guarantee it found THIS specific object rather than some other
    # object of the same type (e.g. an ASIC_DB match by SAI type + generic field presence,
    # which has no way to tell one ACL rule or VLAN member apart from another of the same
    # kind -- see object_types.yaml's field_present_prefix strategy). Real propagation can
    # never skip a layer, so "present downstream of a genuine absence" is never legitimate
    # signal -- it means the downstream match is unverified, not that the object reached
    # hardware. Never let that produce a healthy verdict.
    first_absent_idx = next((i for i, h in enumerate(hops) if h.status == HopStatus.ABSENT), None)
    anomalous = False
    if first_absent_idx is not None:
        anomalous = any(h.status == HopStatus.PRESENT for h in hops[first_absent_idx + 1:])
        if anomalous and break_layer is None:
            break_layer = hops[first_absent_idx].db

    # Healthy = the last layer this object type can actually be checked at (skipping any
    # trailing N/A layers -- e.g. a control-plane-only object type with no discrete ASIC
    # representation, like a BGP session) is PRESENT, and nothing along the way was
    # anomalous (an unverifiable match masking a genuine upstream absence).
    terminal = next((h for h in reversed(hops) if h.status != HopStatus.NA), hops[-1])
    healthy = terminal.status == HopStatus.PRESENT and not anomalous

    return TraceResult(intent=intent, hops=hops, break_layer=break_layer, healthy=healthy,
                       anomalous=anomalous)
