"""
resolver.py — Map a human intent to per-DB lookups, driven by a DECLARATIVE REGISTRY.

The set of traceable object types is defined as data in object_types.yaml, not in code.
This module loads that registry and, for a given intent, produces a ResolvedIntent whose
HopSpecs the walker consumes. Adding a new object type is a YAML edit — no code change.

Design note: ASIC_DB keys are SAI OIDs allocated at runtime and are NOT predictable from
the intent, so ASIC hops are matched by scanning a SAI object type with a declarative
strategy (see asicmatch.py: key_json, oid_chain, acl_entry, key_contains,
field_present_prefix) rather than by exact key.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Optional
import os
import re

from . import asicmatch

# SONiC Redis logical DB indices (stable across releases)
DB_INDEX = {
    "CONFIG_DB": 4,
    "APPL_DB": 0,
    "STATE_DB": 6,
    "ASIC_DB": 1,
    "COUNTERS_DB": 2,
}

# Separators differ per DB in SONiC: CONFIG_DB/STATE_DB use '|', APPL_DB/ASIC_DB use ':'.
SEP = {
    "CONFIG_DB": "|",
    "APPL_DB": ":",
    "STATE_DB": "|",
    "ASIC_DB": ":",
}

_REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "object_types.yaml")


@dataclass
class HopSpec:
    """How to find the object at one DB layer (unchanged interface for the walker)."""
    db: str
    key: Optional[str]
    asic_type: Optional[str] = None
    asic_predicate: Optional[Callable[[dict], bool]] = None   # legacy, type/field level only
    note: str = ""
    asic_finder: Optional[Callable] = None   # instance-level matcher (asicmatch.Finder)


@dataclass
class ResolvedIntent:
    object_type: str
    human: str
    discriminator: str
    hops: list = field(default_factory=list)
    dependencies: list = field(default_factory=list)   # intent strings this object depends on


class IntentParseError(ValueError):
    pass


class RegistryError(ValueError):
    pass


def _load_yaml(path: str) -> dict:
    try:
        import yaml
    except ImportError as e:
        raise RegistryError(
            "PyYAML is required to load the object-type registry (pip install pyyaml)."
        ) from e
    with open(path, "r") as f:
        return yaml.safe_load(f)


class Registry:
    """Loaded, indexed view of object_types.yaml."""

    def __init__(self, path: str = _REGISTRY_PATH):
        data = _load_yaml(path)
        specs = (data or {}).get("object_types", [])
        if not specs:
            raise RegistryError(f"No object_types found in registry: {path}")
        # Longest prefix first so 'bgp neighbor' beats a hypothetical 'bgp'.
        self._specs = sorted(specs, key=lambda s: -len(s["intent_prefix"]))

    def types(self) -> list:
        return [s["name"] for s in self._specs]

    def specs(self) -> list:
        return list(self._specs)

    def spec(self, name: str) -> dict:
        for s in self._specs:
            if s["name"] == name:
                return s
        raise RegistryError(f"Unknown object type: {name!r}. Known: {', '.join(self.types())}")

    def prefixes(self) -> list:
        return [s["intent_prefix"] for s in self._specs]

    def match_intent(self, intent: str):
        s = intent.strip()
        for spec in self._specs:
            pfx = spec["intent_prefix"]
            if re.match(re.escape(pfx) + r"(\s+|$)", s, re.IGNORECASE):
                arg = s[len(pfx):].strip()
                m = re.match(spec["match"], arg)
                if not m:
                    raise IntentParseError(
                        f"'{pfx}' intent malformed. Expected pattern: {spec['match']}")
                return spec, m.groupdict()
        raise IntentParseError(
            f"Unrecognized intent: {intent!r}. Supported prefixes: "
            f"{', '.join(repr(p) for p in self.prefixes())}.")


def _fmt(template: str, groups: dict, db: str) -> str:
    return template.replace("{SEP}", SEP[db]).format(**groups)


def _build_hops(spec: dict, groups: dict) -> list:
    hops = []
    for h in spec["hops"]:
        db = h["db"]
        note = h.get("note", "")
        if h.get("na"):
            hops.append(HopSpec(db, None, None, None, note))
        elif "asic_type" in h:
            try:
                finder = asicmatch.build_finder(h["asic_type"], h.get("asic_match"), groups)
            except (ValueError, KeyError) as e:
                raise RegistryError(f"{spec['name']}: bad asic_match: {e}") from e
            hops.append(HopSpec(db, None, h["asic_type"], None, note, finder))
        else:
            hops.append(HopSpec(db, _fmt(h["key"], groups, db), None, None, note))
    return hops


def _build_dependencies(spec: dict, groups: dict) -> list:
    """Intent strings for the objects this one depends on (registry `depends_on`)."""
    out = []
    for d in spec.get("depends_on", []) or []:
        g = dict(groups)
        when = d.get("when")
        if when:
            m = re.match(when["regex"], groups.get(when["var"], ""))
            if not m:
                continue
            g.update(m.groupdict())
        out.append(d["intent"].format(**g))
    return out


_registry = None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry()
    return _registry


def resolve(intent: str) -> ResolvedIntent:
    """Parse an intent using the declarative registry into a ResolvedIntent."""
    reg = get_registry()
    spec, groups = reg.match_intent(intent)
    disc = spec["discriminator"].format(**groups)
    return ResolvedIntent(
        object_type=spec["name"],
        human=intent.strip(),
        discriminator=disc,
        hops=_build_hops(spec, groups),
        dependencies=_build_dependencies(spec, groups),
    )


if __name__ == "__main__":
    import sys
    reg = get_registry()
    if len(sys.argv) == 1:
        print("Registered object types:")
        for t, p in zip(reg.types(), reg.prefixes()):
            print(f"  {t:<16} intent starts with: '{p}'")
    else:
        r = resolve(" ".join(sys.argv[1:]))
        print(f"object_type = {r.object_type}")
        print(f"discriminator = {r.discriminator}")
        for h in r.hops:
            tgt = h.key if h.key else (f"[scan {h.asic_type}]" if h.asic_type else "[n/a]")
            print(f"  {h.db:<10} -> {tgt}   # {h.note}")
