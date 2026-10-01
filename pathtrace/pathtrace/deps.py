"""
deps.py — trace an object AND the objects it depends on, then name the real root cause.

A VLAN member that never reached silicon is often not the problem: the port it names may not
exist in SAI, or the VLAN itself may have failed. The registry's `depends_on` lists those
upstream objects; this module traces each of them (recursively) with the same walker, and
finds the deepest unhealthy dependency — the first thing worth fixing.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from .resolver import resolve, IntentParseError
from .walker import trace, TraceResult
from .dbclient import DBBackend, CachedBackend

MAX_DEPTH = 4


@dataclass
class DepNode:
    intent: str
    trace: Optional[TraceResult] = None
    children: list = field(default_factory=list)
    error: Optional[str] = None          # dependency intent could not be resolved

    @property
    def healthy(self) -> bool:
        return self.trace is not None and self.trace.healthy

    @property
    def object_type(self) -> str:
        return self.trace.intent.object_type if self.trace else "?"


def build_tree(backend: DBBackend, intent: str, _seen: Optional[set] = None,
               depth: int = 0) -> DepNode:
    if depth == 0 and not isinstance(backend, CachedBackend):
        backend = CachedBackend(backend)
    seen = _seen if _seen is not None else set()
    seen.add(intent.strip().lower())
    try:
        resolved = resolve(intent)
    except IntentParseError as e:
        return DepNode(intent=intent, error=str(e))
    node = DepNode(intent=resolved.human, trace=trace(backend, resolved))
    if depth < MAX_DEPTH:
        for dep in resolved.dependencies:
            if dep.strip().lower() in seen:      # cycle / diamond: already covered
                continue
            node.children.append(build_tree(backend, dep, seen, depth + 1))
    return node


def likely_root_cause(root: DepNode) -> Optional[DepNode]:
    """Deepest unhealthy node whose own dependencies are all healthy.

    Returns None when everything is healthy. Returns `root` itself when the root is unhealthy
    but nothing upstream explains it (the fault is in the object's own propagation).
    """
    def find(n: DepNode) -> Optional[DepNode]:
        for c in n.children:
            if c.error is None and not c.healthy:
                return find(c)
        return n if (n.error is None and not n.healthy) else None
    return find(root)


def flatten(node: DepNode, level: int = 0):
    yield level, node
    for c in node.children:
        yield from flatten(c, level + 1)
