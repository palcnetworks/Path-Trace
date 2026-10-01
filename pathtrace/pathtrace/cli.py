"""
cli.py — PathTrace command line entrypoint.

Always talks to a real Redis (a real switch or SONiC VS); it has no dependency on test code.

Usage:
  pathtrace --intent "acl rule BLOCK_LIST RULE_10"
  pathtrace --intent "vlan member 100 Ethernet0" --deps      # also trace what it depends on
  pathtrace --audit acl_rule                                 # sweep every configured ACL rule
  pathtrace --audit all --json
  pathtrace --list-types

Exit status: 0 healthy, 1 broken (or, for --audit, anything broken/unverified), 2 usage error.
"""

from __future__ import annotations
import argparse
import json
import sys

from .resolver import resolve, get_registry, IntentParseError, RegistryError
from .walker import trace as run_trace
from .correlator import correlate
from .explainer import explain
from .report import (render, render_dependencies, render_audit,
                     render_summary, render_dependencies_summary)


def _hops_json(tr):
    return [{"db": h.db, "status": h.status.value, "key": h.key,
             "match": h.match or None, "detail": h.detail or None} for h in tr.hops]


def _trace_json(intent, tr, corr, expl):
    return {
        "intent": intent.human,
        "object_type": intent.object_type,
        "healthy": tr.healthy,
        "anomalous": tr.anomalous,
        "break_layer": tr.break_layer,
        "hops": _hops_json(tr),
        "sai_status_codes": corr.sai_status_codes,
        "log_evidence_no_specific_match": corr.no_specific_match,
        "cause": expl.cause,
        "next_step": expl.next_step,
    }


def _node_json(node):
    d = {"intent": node.intent, "object_type": node.object_type}
    if node.error:
        d["error"] = node.error
    else:
        d.update({"healthy": node.healthy, "break_layer": node.trace.break_layer,
                  "hops": _hops_json(node.trace)})
    d["dependencies"] = [_node_json(c) for c in node.children]
    return d


def _list_types():
    reg = get_registry()
    print("Traceable object types (from object_types.yaml):")
    # `audit` and `hw-verified` are intentionally not displayed here (the fields still exist
    # in the registry and drive behavior; only the columns are hidden from this listing).
    print(f"  {'type':<20} {'intent':<26} {'depends on'}")
    for s in sorted(reg.specs(), key=lambda s: s["name"]):
        deps = "yes" if s.get("depends_on") else "-"
        print(f"  {s['name']:<20} {(s['intent_prefix'] + ' ...'):<26} {deps}")


def _run_audit(backend, which: str, as_json: bool, with_deps: bool = False) -> int:
    from .audit import audit_type, is_auditable, NotAuditable
    from .dbclient import CachedBackend
    reg = get_registry()
    if which == "all":
        names = [s["name"] for s in reg.specs() if is_auditable(s)]
    else:
        names = [which]
    backend = CachedBackend(backend)
    reports = []
    try:
        for n in sorted(names):
            reports.append(audit_type(backend, reg, n, with_deps=with_deps))
    except NotAuditable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except RegistryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps([{
            "object_type": r.object_type,
            "enumerated": r.enumerated,
            "skipped": r.skipped,
            "healthy": r.count("healthy"),
            "broken": r.count("broken"),
            "unverified": r.count("anomalous"),
            "with_deps": r.with_deps,
            "problems": [{"intent": e.intent, "status": e.status, "break_layer": e.break_layer,
                          "sai_status_codes": e.sai_status_codes, "cause": e.cause,
                          "root_cause": e.root_cause,
                          "root_cause_is_dependency": e.root_cause_is_dep,
                          "root_cause_break_layer": e.root_cause_break_layer}
                         for e in r.entries if e.status != "healthy"],
            "root_cause_groups": [{"intent": g.intent, "break_layer": g.break_layer,
                                   "affected": [e.intent for e in g.affected]}
                                  for g in r.root_cause_groups()],
            "errors": [{"key": k, "message": m} for k, m in r.errors],
        } for r in reports], indent=2))
    else:
        for r in reports:
            print(render_audit(r))
    return 0 if all(r.clean for r in reports) else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="pathtrace",
                                description="Trace an intent across the SONiC datapath.")
    p.add_argument("--intent", help='e.g. "acl rule BLOCK_LIST RULE_10", "route 10.0.0.0/24"')
    p.add_argument("--deps", "--trace-dependencies", dest="deps", action="store_true",
                   help="also trace upstream dependencies and name the root cause. With "
                        "--intent: for the one object. With --audit: for every broken object, "
                        "then collapse shared upstream causes into fleet-wide findings.")
    p.add_argument("--audit", metavar="TYPE",
                   help="sweep every CONFIG_DB object of TYPE (or 'all') and list those that "
                        "did not reach ASIC_DB")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p.add_argument("--detailed", action="store_true",
                   help="show the full per-hop pipeline view, correlated log evidence, and "
                        "suggested next steps (default is a compact summary table)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6379)
    p.add_argument("--syslog", default="/var/log/syslog")
    p.add_argument("--list-types", action="store_true",
                   help="list all traceable object types from the registry")
    p.add_argument("--force-color", action="store_true", help="force ANSI color output")
    args = p.parse_args(argv)

    if args.list_types:
        _list_types()
        return 0

    if not args.intent and not args.audit:
        p.error("--intent or --audit is required (or use --list-types)")
    if args.intent and args.audit:
        p.error("use either --intent or --audit, not both")

    from .dbclient import RedisBackend
    backend = RedisBackend(host=args.host, port=args.port, syslog_path=args.syslog)

    if args.audit:
        return _run_audit(backend, args.audit, args.json, with_deps=args.deps)

    try:
        intent = resolve(args.intent)
    except IntentParseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    tree = cause = cause_expl = None
    if args.deps:
        from .deps import build_tree, likely_root_cause
        tree = build_tree(backend, args.intent)
        tr = tree.trace
        cause = likely_root_cause(tree)
    else:
        tr = run_trace(backend, intent)
    logs = backend.logs()
    corr = correlate(tr, logs)
    expl = explain(tr, corr)
    if cause is not None and cause is not tree:
        cause_expl = explain(cause.trace, correlate(cause.trace, logs))

    if args.json:
        payload = _trace_json(intent, tr, corr, expl)
        if tree is not None:
            payload["dependencies"] = [_node_json(c) for c in tree.children]
            payload["likely_root_cause"] = (
                None if cause is None else
                {"intent": cause.intent, "is_dependency": cause is not tree,
                 "break_layer": cause.trace.break_layer,
                 "cause": (cause_expl.cause if cause_expl else expl.cause),
                 "next_step": (cause_expl.next_step if cause_expl else expl.next_step)})
        print(json.dumps(payload, indent=2))
    elif args.detailed:
        print(render(tr, corr, expl))
        if tree is not None:
            print(render_dependencies(tree, cause, cause_expl))
    else:
        print(render_summary(tr, corr, expl))
        if tree is not None:
            print(render_dependencies_summary(tree, cause, cause_expl))

    # Exit status reflects the traced object itself: 0 = healthy, 1 = broken.
    return 0 if tr.healthy else 1


if __name__ == "__main__":
    sys.exit(main())
