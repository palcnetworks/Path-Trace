"""
asicmatch.py — decide whether a SPECIFIC object is programmed in ASIC_DB.

ASIC_DB keys are SAI OIDs allocated at runtime; they are not predictable from the intent and
most SAI objects carry no human-readable name. A match by "an object of this SAI type exists"
(type-level) cannot tell one ACL entry or VLAN member from another, which is how a real
false-Healthy verdict happened on hardware. This module provides progressively stronger,
instance-level strategies, each reporting HOW the match was made so the report can say so:

  key-field         the SAI key is JSON (route entries, neighbor entries...) and a named field
                    equals the expected value                                   [exact]
  instance-chain    follow OID references from something that DOES carry a name
                    (a host interface name, a VLAN id) down to the object       [exact]
  acl-counter-link  ACL rule -> its per-rule counter (COUNTERS_DB ACL_COUNTER_RULE_MAP)
                    -> the ACL entry that references that counter               [exact]
  attributes        compare the object's CONFIG_DB fields against the SAI object's
                    attributes (ACL priority/addresses/action; QoS scheduler,
                    WRED, buffer profile; QoS map contents)                     [strong]
  key-substring     substring of the SAI key (legacy)                           [weak]
  type-level        an object of the right SAI type exists                      [weakest]

Every strategy returns a MatchOutcome; when nothing matches, `detail` says WHICH link in the
chain was missing, which turns "absent from ASIC_DB" into an actionable statement.
"""

from __future__ import annotations
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Callable, Optional

ASIC = "ASIC_DB"

# Match-quality labels (also shown in reports).
KEY_FIELD = "key-field"
INSTANCE_CHAIN = "instance-chain"
ACL_COUNTER_LINK = "acl-counter-link"
ATTRIBUTES = "attributes"
ATTRIBUTES_AMBIGUOUS = "attributes-ambiguous"
KEY_SUBSTRING = "key-substring"
TYPE_LEVEL = "type-level"

EXACT_MATCHES = {KEY_FIELD, INSTANCE_CHAIN, ACL_COUNTER_LINK}


@dataclass
class MatchOutcome:
    key: Optional[str] = None
    fields: Optional[dict] = None
    match: str = ""
    detail: str = ""

    @property
    def found(self) -> bool:
        return self.key is not None


Finder = Callable[[object], MatchOutcome]


# ------------------------------------------------------------------ helpers
def oid_of(key: str) -> str:
    """'ASIC_STATE:SAI_OBJECT_TYPE_X:oid:0x1' -> 'oid:0x1'."""
    parts = key.split(":", 2)
    return parts[2] if len(parts) == 3 else key


def key_json(key: str) -> Optional[dict]:
    """Parse the JSON body of structured SAI keys (route/neighbor/fdb entries)."""
    parts = key.split(":", 2)
    if len(parts) != 3:
        return None
    try:
        body = json.loads(parts[2])
    except (ValueError, TypeError):
        return None
    return body if isinstance(body, dict) else None


def _subst(value, groups: dict) -> str:
    return str(value).format(**groups)


# ------------------------------------------------------------ simple strategies
def _key_contains(asic_type: str, needle: str) -> Finder:
    def find(backend) -> MatchOutcome:
        for key, fields in backend.scan_type(ASIC, asic_type):
            if needle in fields.get("__key__", key):
                return MatchOutcome(key, fields, KEY_SUBSTRING)
        return MatchOutcome(detail=f"no {asic_type} whose key contains {needle!r}")
    return find


def _key_json(asic_type: str, field: str, value: str) -> Finder:
    def find(backend) -> MatchOutcome:
        seen = 0
        for key, fields in backend.scan_type(ASIC, asic_type):
            body = key_json(fields.get("__key__", key))
            if body is None:
                continue
            seen += 1
            if str(body.get(field)) == value:
                return MatchOutcome(key, fields, KEY_FIELD)
        return MatchOutcome(detail=f"no {asic_type} with {field}={value!r} "
                                   f"({seen} entries of that type inspected)")
    return find


def _field_present_prefix(asic_type: str, prefix: str) -> Finder:
    def find(backend) -> MatchOutcome:
        for key, fields in backend.scan_type(ASIC, asic_type):
            if any(k.startswith(prefix) for k in fields):
                return MatchOutcome(key, fields, TYPE_LEVEL,
                                    "matched by SAI type only; not confirmed to be this instance")
        return MatchOutcome(detail=f"no {asic_type} object in ASIC_DB")
    return find


# ------------------------------------------------------------------ OID chain
def _oid_chain(steps: list, groups: dict) -> Finder:
    """Follow references step by step.

    Each step: {type, where: {attr: value}, [name], [yield], [missing]}.
      where values: a literal/format string ("{port}"), or "@name" = the value bound by an
      earlier step. The pseudo-attribute __oid__ compares the object's own OID.
      A step binds `name` to `yield`'s attribute value if given, else to the object's OID.
    The last step's object is the result.
    """
    def find(backend) -> MatchOutcome:
        bound: dict = {}
        last = None
        for i, step in enumerate(steps):
            typ = step["type"]
            want = {}
            for attr, raw in (step.get("where") or {}).items():
                raw = str(raw)
                want[attr] = bound[raw[1:]] if raw.startswith("@") else _subst(raw, groups)
            hit = None
            for key, fields in backend.scan_type(ASIC, typ):
                real_key = fields.get("__key__", key)
                ok = True
                for attr, val in want.items():
                    got = oid_of(real_key) if attr == "__oid__" else fields.get(attr)
                    if got != val:
                        ok = False
                        break
                if ok:
                    hit = (key, fields)
                    break
            if hit is None:
                missing = step.get("missing")
                msg = _subst(missing, groups) if missing else \
                    f"no {typ} with " + ", ".join(f"{a}={v}" for a, v in want.items())
                return MatchOutcome(detail=f"chain broken at step {i + 1}/{len(steps)}: {msg}")
            key, fields = hit
            name = step.get("name")
            if name:
                bound[name] = fields.get(step["yield"]) if step.get("yield") \
                    else oid_of(fields.get("__key__", key))
                if bound[name] is None:
                    return MatchOutcome(
                        detail=f"chain broken at step {i + 1}/{len(steps)}: {typ} has no "
                               f"{step['yield']} attribute")
            last = hit
        return MatchOutcome(last[0], last[1], INSTANCE_CHAIN)
    return find


# ------------------------------------------------------------------ ACL entries
_ACL_PREFIX = "SAI_ACL_ENTRY_ATTR_"


def _ip_from_config(v: str):
    try:
        n = ipaddress.ip_network(v if "/" in v else v, strict=False)
        return int(n.network_address), n.prefixlen
    except ValueError:
        return None


def _ip_from_sai(v: str):
    """'10.44.0.0&mask:255.255.0.0' -> (network_int, prefixlen)."""
    m = re.match(r"^([^&]+)&mask:(.+)$", v)
    if not m:
        return None
    try:
        addr = ipaddress.ip_address(m.group(1))
        mask = int(ipaddress.ip_address(m.group(2)))
    except ValueError:
        return None
    plen = bin(mask).count("1")
    return int(addr) & mask, plen


def _cmp_ip(cfg: str, sai: str) -> bool:
    a, b = _ip_from_config(cfg), _ip_from_sai(sai)
    return a is not None and b is not None and a == b


def _cmp_int(cfg: str, sai: str) -> bool:
    try:
        return int(cfg, 0) == int(sai.split("&", 1)[0], 0)
    except ValueError:
        return False


def _cmp_action(cfg: str, sai: str) -> bool:
    return sai == "SAI_PACKET_ACTION_" + cfg.upper()


# CONFIG_DB field -> (SAI attribute, comparator)
_ACL_FIELD_MAP = {
    "PRIORITY": (_ACL_PREFIX + "PRIORITY", _cmp_int),
    "SRC_IP": (_ACL_PREFIX + "FIELD_SRC_IP", _cmp_ip),
    "DST_IP": (_ACL_PREFIX + "FIELD_DST_IP", _cmp_ip),
    "SRC_IPV6": (_ACL_PREFIX + "FIELD_SRC_IPV6", _cmp_ip),
    "DST_IPV6": (_ACL_PREFIX + "FIELD_DST_IPV6", _cmp_ip),
    "L4_SRC_PORT": (_ACL_PREFIX + "FIELD_L4_SRC_PORT", _cmp_int),
    "L4_DST_PORT": (_ACL_PREFIX + "FIELD_L4_DST_PORT", _cmp_int),
    "IP_PROTOCOL": (_ACL_PREFIX + "FIELD_IP_PROTOCOL", _cmp_int),
    "PACKET_ACTION": (_ACL_PREFIX + "ACTION_PACKET_ACTION", _cmp_action),
}

_ACL_TYPE = "SAI_OBJECT_TYPE_ACL_ENTRY"


def _acl_entry(table: str, rule: str) -> Finder:
    def find(backend) -> MatchOutcome:
        # 1) exact: the rule's own counter, registered by orchagent per rule.
        ctr_map = backend.get_hash("COUNTERS_DB", "ACL_COUNTER_RULE_MAP") or {}
        ctr_oid = ctr_map.get(f"{table}:{rule}")
        entries = backend.scan_type(ASIC, _ACL_TYPE)
        if ctr_oid:
            for key, fields in entries:
                if fields.get(_ACL_PREFIX + "ACTION_COUNTER") == ctr_oid:
                    return MatchOutcome(key, fields, ACL_COUNTER_LINK)
            return MatchOutcome(
                detail=f"orchagent registered counter {ctr_oid} for {table}:{rule} but no "
                       f"ACL entry in ASIC_DB references it")

        # 2) strong: compare the rule's CONFIG_DB fields with each entry's SAI attributes.
        cfg = backend.get_hash("CONFIG_DB", f"ACL_RULE|{table}|{rule}")
        if cfg is None:
            return MatchOutcome(
                detail="rule is not in CONFIG_DB, so its ASIC entry cannot be identified")
        checks = [(f, v, *_ACL_FIELD_MAP[f]) for f, v in cfg.items() if f in _ACL_FIELD_MAP]
        unchecked = sorted(f for f in cfg if f not in _ACL_FIELD_MAP)
        if not checks:
            for key, fields in entries:
                return MatchOutcome(key, fields, TYPE_LEVEL,
                                    "rule has no comparable attributes; matched by SAI type only")
            return MatchOutcome(detail=f"no {_ACL_TYPE} object in ASIC_DB")
        hits = []
        for key, fields in entries:
            if all(attr in fields and cmp(v, fields[attr]) for _, v, attr, cmp in checks):
                hits.append((key, fields))
        note = f"compared {', '.join(c[0] for c in checks)}"
        if unchecked:
            note += f"; not comparable: {', '.join(unchecked)}"
        if not hits:
            return MatchOutcome(detail=f"no ACL entry in ASIC_DB has the attributes of "
                                       f"{table}|{rule} ({note})")
        if len(hits) > 1:
            return MatchOutcome(hits[0][0], hits[0][1], ATTRIBUTES_AMBIGUOUS,
                                f"{len(hits)} ACL entries share these attributes ({note})")
        return MatchOutcome(hits[0][0], hits[0][1], ATTRIBUTES, note)
    return find



# ------------------------------------------------------------------ generic config->SAI attributes
def _cmp_str_map(mapping: dict):
    def cmp(cfg: str, sai: str) -> bool:
        want = mapping.get(cfg) or mapping.get(cfg.upper()) or mapping.get(cfg.lower())
        return want is not None and sai == want
    return cmp


def _cmp_bool(cfg: str, sai: str) -> bool:
    return cfg.strip().lower() == sai.strip().lower()


def _comparable(kind: str, mapping: Optional[dict], cfg_val: str) -> bool:
    if kind == "map":
        return bool(mapping) and (cfg_val in mapping or cfg_val.upper() in mapping
                                  or cfg_val.lower() in mapping)
    return True


def _config_attrs(asic_type: str, config_key: str, fields: list) -> Finder:
    """Generic 'this CONFIG_DB row vs the SAI object' matcher (scheduler, WRED, buffer profile).

    fields: [{cfg, attr, kind: int|map|bool, map: {...}}]. Only fields present in the row are
    compared. Values that cannot be translated are listed as 'not comparable', never treated as
    mismatches.
    """
    def find(backend) -> MatchOutcome:
        cfg = backend.get_hash("CONFIG_DB", config_key)
        if cfg is None:
            return MatchOutcome(
                detail=f"{config_key} is not in CONFIG_DB, so its ASIC object cannot be identified")
        checks, skipped = [], []
        for f in fields:
            if f["cfg"] not in cfg:
                continue
            kind = f.get("kind", "int")
            val = cfg[f["cfg"]]
            if not _comparable(kind, f.get("map"), val):
                skipped.append(f["cfg"])
                continue
            cmp = _cmp_int if kind == "int" else _cmp_bool if kind == "bool" \
                else _cmp_str_map(f["map"])
            checks.append((f["cfg"], val, f["attr"], cmp))
        candidates = backend.scan_type(ASIC, asic_type)
        if not checks:
            for key, flds in candidates:
                return MatchOutcome(key, flds, TYPE_LEVEL,
                                    "no comparable fields in the row; matched by SAI type only")
            return MatchOutcome(detail=f"no {asic_type} object in ASIC_DB")
        hits, best = [], (-1, None, [])
        for key, flds in candidates:
            bad = [(c, v, a) for c, v, a, cmp in checks if a not in flds or not cmp(v, flds[a])]
            if not bad:
                hits.append((key, flds))
            elif len(checks) - len(bad) > best[0]:
                best = (len(checks) - len(bad), key, [(c, v, a, flds.get(a)) for c, v, a in bad])
        note = f"compared {', '.join(c[0] for c in checks)}"
        if skipped:
            note += f"; not comparable: {', '.join(skipped)}"
        if not hits:
            if best[1] is None:
                return MatchOutcome(detail=f"no {asic_type} object in ASIC_DB")
            diffs = "; ".join(f"{c}={v} but SAI {a.split('_ATTR_')[-1]}={g}"
                              for c, v, a, g in best[2][:3])
            return MatchOutcome(detail=f"no {asic_type} in ASIC_DB matches {config_key} "
                                       f"(closest differs: {diffs})")
        if len(hits) > 1:
            return MatchOutcome(hits[0][0], hits[0][1], ATTRIBUTES_AMBIGUOUS,
                                f"{len(hits)} objects share these attributes ({note})")
        return MatchOutcome(hits[0][0], hits[0][1], ATTRIBUTES, note)
    return find


# ------------------------------------------------------------------ QoS maps
# CONFIG_DB table -> (SAI_QOS_MAP_TYPE, key field, value field) in the serialized entry list.
_QOS_MAPS = {
    "DSCP_TO_TC_MAP": ("SAI_QOS_MAP_TYPE_DSCP_TO_TC", "dscp", "tc"),
    "TC_TO_QUEUE_MAP": ("SAI_QOS_MAP_TYPE_TC_TO_QUEUE", "tc", "qidx"),
    "TC_TO_PRIORITY_GROUP_MAP": ("SAI_QOS_MAP_TYPE_TC_TO_PRIORITY_GROUP", "tc", "pg"),
    "PFC_PRIORITY_TO_QUEUE_MAP": ("SAI_QOS_MAP_TYPE_PFC_PRIORITY_TO_QUEUE", "prio", "qidx"),
}
_QOS_MAP_TYPE = "SAI_OBJECT_TYPE_QOS_MAP"
_QOS_LIST_ATTR = "SAI_QOS_MAP_ATTR_MAP_TO_VALUE_LIST"


def _sai_qos_entries(fields: dict, kf: str, vf: str) -> Optional[dict]:
    try:
        body = json.loads(fields.get(_QOS_LIST_ATTR, ""))
        return {str(e["key"][kf]): str(e["value"][vf]) for e in body["list"]}
    except (ValueError, TypeError, KeyError):
        return None


def _qos_map_hits(backend, table: str, name: str):
    """Return (hits, detail): every QOS_MAP object whose content equals CONFIG_DB table|name."""
    if table not in _QOS_MAPS:
        return [], f"unknown QoS map table {table!r}"
    sai_type, kf, vf = _QOS_MAPS[table]
    cfg = backend.get_hash("CONFIG_DB", f"{table}|{name}")
    if cfg is None:
        return [], f"{table}|{name} is not in CONFIG_DB, so its ASIC object cannot be identified"
    want = {str(k): str(v) for k, v in cfg.items()}
    of_type = [(k, f) for k, f in backend.scan_type(ASIC, _QOS_MAP_TYPE)
               if f.get("SAI_QOS_MAP_ATTR_TYPE") == sai_type]
    if not of_type:
        return [], f"no {sai_type} QoS map in ASIC_DB"
    hits, best = [], (None, None)
    for key, flds in of_type:
        got = _sai_qos_entries(flds, kf, vf)
        if got is None:
            continue
        diff = [k for k in want if got.get(k) != want[k]]
        if not diff and len(got) == len(want):
            hits.append((key, flds))
        elif best[0] is None or len(diff) < len(best[0]):
            best = (diff, got)
    if hits:
        return hits, ""
    if best[0] is None:
        return [], f"{len(of_type)} {sai_type} map(s) in ASIC_DB but none could be read"
    k = best[0][0] if best[0] else None
    ex = f"; e.g. {kf} {k}: config {want.get(k)}, SAI {best[1].get(k)}" if k else \
        f"; SAI has {len(best[1])} entries, config has {len(want)}"
    return [], (f"no {sai_type} map in ASIC_DB has the contents of {table}|{name} "
                f"(closest differs on {len(best[0])} entr{'y' if len(best[0]) == 1 else 'ies'}{ex})")


def _qos_map(table: str, name: str) -> Finder:
    def find(backend) -> MatchOutcome:
        hits, detail = _qos_map_hits(backend, table, name)
        if not hits:
            return MatchOutcome(detail=detail)
        note = f"compared all {len(_QOS_MAPS[table][1:])} key/value fields of every entry"
        if len(hits) > 1:
            return MatchOutcome(hits[0][0], hits[0][1], ATTRIBUTES_AMBIGUOUS,
                                f"{len(hits)} QoS maps in ASIC_DB have identical contents")
        return MatchOutcome(hits[0][0], hits[0][1], ATTRIBUTES, "map contents equal CONFIG_DB")
    return find


# PORT_QOS_MAP field -> (SAI port attribute, CONFIG_DB table of the referenced map)
_PORT_QOS_FIELDS = {
    "dscp_to_tc_map": ("SAI_PORT_ATTR_QOS_DSCP_TO_TC_MAP", "DSCP_TO_TC_MAP"),
    "tc_to_queue_map": ("SAI_PORT_ATTR_QOS_TC_TO_QUEUE_MAP", "TC_TO_QUEUE_MAP"),
    "tc_to_pg_map": ("SAI_PORT_ATTR_QOS_TC_TO_PRIORITY_GROUP_MAP", "TC_TO_PRIORITY_GROUP_MAP"),
    "pfc_to_queue_map": ("SAI_PORT_ATTR_QOS_PFC_PRIORITY_TO_QUEUE_MAP", "PFC_PRIORITY_TO_QUEUE_MAP"),
}
_NULL_OID = "oid:0x0"


def _ref(value: str, implicit_table: str):
    """'[DSCP_TO_TC_MAP|AZURE]' or 'AZURE' -> (table, name)."""
    v = value.strip().strip("[]")
    if "|" in v:
        t, n = v.split("|", 1)
        return t, n
    return implicit_table, v


def _port_qos(port: str) -> Finder:
    def find(backend) -> MatchOutcome:
        cfg = backend.get_hash("CONFIG_DB", f"PORT_QOS_MAP|{port}")
        if cfg is None:
            return MatchOutcome(detail=f"PORT_QOS_MAP|{port} is not in CONFIG_DB")
        wanted = [(f, *_PORT_QOS_FIELDS[f]) for f in cfg if f in _PORT_QOS_FIELDS]
        if not wanted:
            return MatchOutcome(detail=f"PORT_QOS_MAP|{port} binds no maps")
        port_oid = None
        for key, flds in backend.scan_type(ASIC, "SAI_OBJECT_TYPE_HOSTIF"):
            if flds.get("SAI_HOSTIF_ATTR_NAME") == port:
                port_oid = flds.get("SAI_HOSTIF_ATTR_OBJ_ID")
                break
        if port_oid is None:
            return MatchOutcome(detail=f"chain broken at step 1/3: {port} has no host interface "
                                       f"in ASIC_DB (port unknown to SAI)")
        pkey = f"ASIC_STATE:SAI_OBJECT_TYPE_PORT:{port_oid}"
        pflds = backend.get_hash(ASIC, pkey)
        if pflds is None:
            return MatchOutcome(detail=f"chain broken at step 2/3: host interface of {port} "
                                       f"points at {port_oid}, which is not a SAI port")
        for fname, attr, implicit in wanted:
            tbl, nm = _ref(cfg[fname], implicit)
            bound = pflds.get(attr)
            if bound is None or bound == _NULL_OID:
                return MatchOutcome(detail=f"chain broken at step 3/3: {port} has no {attr} "
                                           f"(config binds {fname} to {tbl}|{nm})")
            mflds = backend.get_hash(ASIC, f"ASIC_STATE:{_QOS_MAP_TYPE}:{bound}")
            if mflds is None:
                return MatchOutcome(detail=f"chain broken at step 3/3: {attr} on {port} points at "
                                           f"{bound}, which is not a QoS map in ASIC_DB")
            want_type = _QOS_MAPS.get(tbl, (None,))[0]
            if want_type and mflds.get("SAI_QOS_MAP_ATTR_TYPE") not in (None, want_type):
                return MatchOutcome(detail=f"chain broken at step 3/3: {attr} on {port} points at "
                                           f"a {mflds.get('SAI_QOS_MAP_ATTR_TYPE')} map, "
                                           f"expected {want_type}")
            hits, why = _qos_map_hits(backend, tbl, nm)
            if not hits and backend.get_hash("CONFIG_DB", f"{tbl}|{nm}") is not None \
                    and "could not be read" not in why:
                return MatchOutcome(detail=f"chain broken at step 3/3: {port} is bound to a QoS map "
                                           f"({bound}) whose contents differ from {tbl}|{nm}; {why}")
            if hits and bound not in {oid_of(k) for k, _ in hits}:
                return MatchOutcome(detail=f"chain broken at step 3/3: {port} is bound to a QoS map "
                                           f"({bound}) whose contents differ from {tbl}|{nm}")
        return MatchOutcome(pkey, pflds, INSTANCE_CHAIN)
    return find

# ------------------------------------------------------------------ factory
KNOWN_STRATEGIES = ("key_contains", "key_json", "field_present_prefix", "oid_chain", "acl_entry",
                    "config_attrs", "qos_map", "port_qos")


def build_finder(asic_type: str, asic_match: Optional[dict], groups: dict) -> Optional[Finder]:
    """Build the finder for one ASIC_DB hop. Raises ValueError on an unknown strategy."""
    if asic_match is None:
        return _field_present_prefix(asic_type, "")
    by = asic_match.get("by")
    if by == "key_contains":
        return _key_contains(asic_type, _subst(asic_match.get("value", ""), groups))
    if by == "key_json":
        return _key_json(asic_type, asic_match["field"], _subst(asic_match["value"], groups))
    if by == "field_present_prefix":
        return _field_present_prefix(asic_type, asic_match.get("value", ""))
    if by == "oid_chain":
        if not asic_match.get("steps"):
            raise ValueError("oid_chain requires a non-empty 'steps' list")
        return _oid_chain(asic_match["steps"], groups)
    if by == "acl_entry":
        return _acl_entry(_subst(asic_match["table"], groups), _subst(asic_match["rule"], groups))
    if by == "config_attrs":
        if not asic_match.get("fields"):
            raise ValueError("config_attrs requires a non-empty 'fields' list")
        return _config_attrs(asic_type, _subst(asic_match["config_key"], groups), asic_match["fields"])
    if by == "qos_map":
        return _qos_map(_subst(asic_match["table"], groups), _subst(asic_match["name"], groups))
    if by == "port_qos":
        return _port_qos(_subst(asic_match["port"], groups))
    raise ValueError(f"Unknown asic_match.by: {by!r} (known: {', '.join(KNOWN_STRATEGIES)})")
