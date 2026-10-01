"""
report.py — Render a TraceResult as the green/green/RED pipeline in the terminal.
"""

from __future__ import annotations
import sys
from .walker import TraceResult, HopStatus
from .correlator import Correlation
from .explainer import Explanation

_USE_COLOR = sys.stdout.isatty() or ("--force-color" in sys.argv)


def _c(code: str, s: str) -> str:
    if not _USE_COLOR:
        return s
    return f"\033[{code}m{s}\033[0m"


GREEN = "92"
RED = "91"
DIM = "90"
BOLD = "1"
YELLOW = "93"
CYAN = "96"

_DOT = {
    HopStatus.PRESENT: _c(GREEN, "●"),
    HopStatus.ABSENT: _c(RED, "●"),
    HopStatus.NOT_REACHED: _c(DIM, "◌"),
    HopStatus.NA: _c(DIM, "○"),
}
_TAG = {
    HopStatus.PRESENT: _c(GREEN, "✓ PRESENT"),
    HopStatus.ABSENT: _c(RED, "✗ ABSENT"),
    HopStatus.NOT_REACHED: _c(DIM, "◌ not reached"),
    HopStatus.NA: _c(DIM, "— n/a"),
}
# Distinct from plain ABSENT: this is specifically the hop trace.break_layer identifies as
# where propagation stopped. When nothing was ever PRESENT (break_layer is None), every hop
# is legitimately ABSENT but none of them is "the" break — conflating the two used to label
# every absent hop "BROKE HERE" even in that case.
_BREAK_TAG = _c(RED, "✗ BROKE HERE")

_MATCH_LABEL = {
    "key-field": "exact key field",
    "instance-chain": "exact OID reference chain",
    "acl-counter-link": "exact ACL counter link",
    "attributes": "attribute comparison",
    "attributes-ambiguous": "attribute comparison (ambiguous)",
    "key-substring": "key substring (weak)",
    "type-level": "SAI type only (not confirmed to be this object)",
}
_WEAK = {"attributes-ambiguous", "key-substring", "type-level"}


def render(trace: TraceResult, corr: Correlation, expl: Explanation) -> str:
    out: list[str] = []
    out.append("")
    out.append(_c(CYAN, f"⇢ PathTrace  ·  intent: {trace.intent.human}"))
    out.append(_c(DIM, "  SONiC datapath: CONFIG_DB → APPL_DB → STATE_DB → ASIC_DB → silicon"))
    out.append("")

    for i, h in enumerate(trace.hops):
        is_break = (h.db == trace.break_layer and h.status == HopStatus.ABSENT)
        was_present_upstream = any(hh.status == HopStatus.PRESENT for hh in trace.hops[:i])
        name = _c(BOLD, h.db) if h.status == HopStatus.PRESENT else \
               _c(RED, h.db) if is_break else _c(DIM, h.db)
        keytxt = ""
        if h.status == HopStatus.PRESENT and h.key:
            keytxt = _c(DIM, f"  {h.key}")
        elif is_break and was_present_upstream:
            keytxt = _c(RED, "  (present upstream, missing here)")
        elif h.status == HopStatus.ABSENT:
            # Also covers the anomalous case: this hop is flagged as the break, but nothing
            # was genuinely present before it (see TraceResult.anomalous) -- "present
            # upstream" would be false here, a downstream match doesn't change that.
            keytxt = _c(DIM, "  (never present)")
        tag = _BREAK_TAG if is_break else _TAG[h.status]
        line = f"  {_DOT[h.status]}  {name:<22} {tag}{keytxt}"
        out.append(line)
        if h.status == HopStatus.PRESENT and h.match:
            label = _MATCH_LABEL.get(h.match, h.match)
            note = f"      matched by: {label}"
            if h.detail:
                note += f" — {h.detail}"
            out.append(_c(YELLOW if h.match in _WEAK else DIM, note))
        elif h.status in (HopStatus.ABSENT, HopStatus.NOT_REACHED) and h.detail:
            out.append(_c(DIM, f"      {h.detail}"))
        if i < len(trace.hops) - 1:
            connector = "│"
            col = GREEN if h.status == HopStatus.PRESENT and not is_break else DIM
            out.append(f"  {_c(col, connector)}")

    out.append("")
    if trace.healthy:
        out.append(_c(GREEN, "  ✓ Intent fully propagated to silicon. Healthy."))
        out.append("")
        return "\n".join(out)

    # Broken: show root cause + evidence + next step
    if trace.break_layer is not None:
        out.append(_c(RED, f"  ⬤ ROOT CAUSE — intent stopped at {trace.break_layer}"))
    else:
        out.append(_c(RED, "  ⬤ ROOT CAUSE — intent never reached the datapath"))
    out.append(f"    {expl.cause}")
    out.append("")
    if corr.lines:
        out.append(_c(DIM, "  Correlated log evidence:"))
        for ln in corr.lines:
            hl = YELLOW if ("ERR" in ln or "SAI_STATUS" in ln) else DIM
            out.append("    " + _c(hl, ln))
        out.append("")
    elif corr.no_specific_match:
        out.append(_c(DIM, "  Correlated log evidence: none — orchagent/syncd/swss logged "
                            "activity, but none of it named this specific object (log lines "
                            "mentioning unrelated objects are not shown as if they were)."))
        out.append("")
    out.append(_c(CYAN, "  → Suggested next step:"))
    out.append(f"    {expl.next_step}")
    out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------- dependencies
def _hop_summary(tr) -> str:
    if tr.healthy:
        return _c(GREEN, "healthy")
    if tr.break_layer:
        return _c(RED, f"not healthy (stopped at {tr.break_layer})")
    return _c(RED, "not healthy (never reached the datapath)")


def render_dependencies(root, cause_node, cause_expl) -> str:
    """Dependency tree plus the likely root cause (see deps.py)."""
    from .deps import flatten
    out = ["", _c(BOLD, "  ── Dependencies ────────────────────────────────────")]
    for level, n in flatten(root):
        if level == 0:
            continue
        pad = "    " + "   " * (level - 1)
        if n.error:
            out.append(f"{pad}{_c(DIM, '?')} {n.intent}  {_c(DIM, '(cannot resolve: ' + n.error + ')')}")
        else:
            mark = _c(GREEN, "✓") if n.healthy else _c(RED, "✗")
            out.append(f"{pad}{mark} {n.intent}  {_hop_summary(n.trace)}")
    if len(out) == 2:
        out.append(_c(DIM, "    (this object type declares no dependencies)"))
    out.append("")
    if cause_node is None:
        out.append(_c(GREEN, "  ✓ Object and all of its dependencies are healthy."))
    elif cause_node is root:
        out.append(_c(YELLOW, "  No upstream dependency explains this failure — the fault is in "
                              "this object's own propagation (see above)."))
    else:
        out.append(_c(RED, f"  ⬤ LIKELY ROOT CAUSE — dependency '{cause_node.intent}' is not healthy"))
        if cause_node.trace.break_layer:
            out.append(f"    It stopped at {cause_node.trace.break_layer}. Fix it first; "
                       f"'{root.intent}' depends on it.")
        if cause_expl is not None:
            out.append(f"    {cause_expl.cause}")
            out.append(_c(CYAN, "  → Suggested next step:"))
            out.append(f"    {cause_expl.next_step}")
    out.append("")
    return "\n".join(out)


# ------------------------------------------------------------ summary (default)
# A compact, presentable table. The verbose pipeline view above is shown only with --detailed.
def _summary_cell(h, is_break, was_present_upstream):
    """(status_text, status_color, detail_text, detail_color) for one hop row.

    The DETAIL cell is kept concise: exact-key layers show their key; the ASIC layer shows
    just the matched oid (the SAI type + match method are in --detailed). This keeps the table
    narrow and presentable.
    """
    if h.status == HopStatus.PRESENT:
        key = h.key or ""
        if "oid:" in key:                 # ASIC object -> just the oid
            key = "oid:" + key.split("oid:", 1)[1]
        return "PRESENT", GREEN, key, DIM
    if h.status == HopStatus.NA:
        return "n/a", DIM, "-", DIM
    if h.status == HopStatus.NOT_REACHED:
        return "skipped", DIM, "(downstream of the break; not reached)", DIM
    # ABSENT
    detail = h.detail or ("(present upstream, missing here)" if was_present_upstream
                          else "(never present)")
    if is_break:
        return "BROKE", RED, detail, RED
    return "ABSENT", RED, detail, DIM


def _box_table(headers, rows, caps=None, show_header=True) -> list:
    """Render a standard ASCII grid table (psql / MySQL style: +---+ and |).

    rows = list of rows; each cell is (text, color_code|''). A cap on a column word-wraps
    cells wider than the cap across multiple physical rows (so long prose stays inside the
    table instead of overflowing the terminal). Column widths are measured on PLAIN text,
    then the padded plain cell is wrapped in color -- so ANSI codes never corrupt alignment.
    Set show_header=False for a borderless-header key/value grid.
    """
    import textwrap
    ncol = len(headers)
    caps = caps or [None] * ncol

    def wrap(s, w):
        s = str(s)
        if not w or len(s) <= w:
            return [s]
        return textwrap.wrap(s, width=w, break_long_words=True, break_on_hyphens=False) or [""]

    # each cell -> (list-of-wrapped-lines, color)
    wrows = [[(wrap(r[i][0], caps[i]), r[i][1]) for i in range(ncol)] for r in rows]
    widths = []
    for i in range(ncol):
        w = len(headers[i]) if show_header else 0
        for cells in wrows:
            for ln in cells[i][0]:
                w = max(w, len(ln))
        widths.append(w)

    rule = "  +" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [rule]
    if show_header:
        out.append("  | " + " | ".join(_c(DIM, headers[i].ljust(widths[i])) for i in range(ncol)) + " |")
        out.append(rule)
    for cells in wrows:
        for j in range(max(len(cells[i][0]) for i in range(ncol))):
            parts = []
            for i in range(ncol):
                lines, color = cells[i]
                padded = (lines[j] if j < len(lines) else "").ljust(widths[i])
                parts.append(_c(color, padded) if color else padded)
            out.append("  | " + " | ".join(parts) + " |")
    out.append(rule)
    return out


def render_summary(trace: TraceResult, corr: Correlation, expl: Explanation) -> str:
    out = ["", _c(CYAN, f"PathTrace — {trace.intent.human}"), ""]
    rows = []
    for i, h in enumerate(trace.hops):
        is_break = (h.db == trace.break_layer and h.status == HopStatus.ABSENT)
        was_present_upstream = any(hh.status == HopStatus.PRESENT for hh in trace.hops[:i])
        st, stc, detail, dc = _summary_cell(h, is_break, was_present_upstream)
        layer_color = BOLD if h.status == HopStatus.PRESENT else RED if is_break else DIM
        rows.append([(h.db, layer_color), (st, stc), (detail, dc)])
    out += _box_table(["LAYER", "STATUS", "DETAIL"], rows, caps=[None, None, 60])
    out.append("")
    # Verdict / Cause / Next as a headerless key/value grid (long prose wraps inside the cell).
    if trace.healthy:
        res = [[("Verdict", GREEN), ("✓ HEALTHY — intent fully propagated to silicon", GREEN)]]
    else:
        where = f"stopped at {trace.break_layer}" if trace.break_layer else "never reached the datapath"
        res = [[("Verdict", RED), (f"✗ BROKEN — {where}", RED)],
               [("Cause", DIM), (expl.cause, "")]]
        if corr.sai_status_codes:
            res.append([("SAI", DIM), (", ".join(corr.sai_status_codes), YELLOW)])
        res.append([("Next", CYAN), (expl.next_step, "")])
    out += _box_table(["", ""], res, caps=[None, 66], show_header=False)
    out.append("")
    return "\n".join(out)


def render_dependencies_summary(root, cause_node, cause_expl) -> str:
    """Compact dependency table + one-line root cause (the --deps summary)."""
    from .deps import flatten
    out = ["", _c(BOLD, "  Dependencies:")]
    nodes = [(lvl, n) for lvl, n in flatten(root) if lvl > 0]
    if not nodes:
        out.append(_c(DIM, "    (this object type declares no dependencies)"))
    else:
        rows = []
        for level, n in nodes:
            name = ("  " * (level - 1)) + n.intent
            if n.error:
                rows.append([(name, DIM), ("? cannot resolve", DIM)])
            elif n.healthy:
                rows.append([(name, ""), ("✓ healthy", GREEN)])
            elif n.trace.break_layer:
                rows.append([(name, ""), (f"✗ broken at {n.trace.break_layer}", RED)])
            else:
                rows.append([(name, ""), ("✗ never reached the datapath", RED)])
        out += _box_table(["DEPENDENCY", "STATUS"], rows)
    if cause_node is None:
        out.append("  " + _c(GREEN, "Root cause: none — object and all dependencies are healthy"))
    elif cause_node is root:
        out.append("  " + _c(YELLOW, "Root cause: this object's own propagation (dependencies healthy)"))
    else:
        layer = (f" (stopped at {cause_node.trace.break_layer})"
                 if cause_node.trace and cause_node.trace.break_layer else "")
        out.append("  " + _c(RED, f"Root cause: dependency '{cause_node.intent}'{layer} — fix it first"))
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------- audit
def render_audit(rep) -> str:
    out = ["", _c(CYAN, f"⇢ PathTrace audit  ·  {rep.object_type}"),
           _c(DIM, f"  {rep.enumerated} CONFIG_DB object(s) enumerated"
                   + (f", {rep.skipped} skipped (key shape doesn't fit this type)" if rep.skipped else "")),
           ""]
    n_ok, n_bad, n_an = rep.count("healthy"), rep.count("broken"), rep.count("anomalous")
    out.append("  " + _c(GREEN, f"✓ {n_ok} healthy") + "   " + _c(RED, f"✗ {n_bad} broken")
               + "   " + _c(YELLOW, f"⚠ {n_an} unverified"))
    for e in rep.entries:
        if e.status == "healthy":
            continue
        out.append("")
        if e.status == "broken":
            where = f"stopped at {e.break_layer}" if e.break_layer else "never reached the datapath"
            out.append("  " + _c(RED, "✗ ") + _c(BOLD, e.intent) + "  " + _c(RED, where))
        else:
            out.append("  " + _c(YELLOW, "⚠ ") + _c(BOLD, e.intent) + "  "
                       + _c(YELLOW, f"absent at {e.break_layer}, but a downstream match is unverified"))
        if e.sai_status_codes:
            out.append(_c(DIM, "      SAI: " + ", ".join(e.sai_status_codes)))
        out.append(_c(DIM, "      " + e.cause))
        # DEPTH: name the deepest cause for this object (only set when audited with --deps)
        if e.root_cause_is_dep and e.root_cause:
            layer = f" (stopped at {e.root_cause_break_layer})" if e.root_cause_break_layer else ""
            out.append(_c(RED, f"      └─ root cause: dependency '{e.root_cause}'{layer}"))
        elif rep.with_deps and e.root_cause:
            out.append(_c(DIM, "      └─ root cause: this object's own propagation (dependencies healthy)"))
    for key, msg in rep.errors:
        out.append(_c(DIM, f"  ! could not audit {key}: {msg}"))

    # DEPTH SUMMARY: collapse shared upstream failures into fleet-wide, fix-this-first findings.
    groups = rep.root_cause_groups() if rep.with_deps else []
    if groups:
        out.append("")
        out.append(_c(BOLD, "  ── Root-cause summary (shared upstream failures) ───────"))
        for g in groups:
            layer = f" — stopped at {g.break_layer}" if g.break_layer else ""
            out.append("  " + _c(RED, f"⬤ {g.intent}{layer}"))
            out.append(_c(DIM, f"     cascades to {len(g.affected)} object(s): "
                               + ", ".join(e.intent for e in g.affected)))
            out.append(_c(CYAN, f"     → Fix '{g.intent}' first."))
    out.append("")
    return "\n".join(out)
