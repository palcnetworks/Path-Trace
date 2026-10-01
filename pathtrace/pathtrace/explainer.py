"""
explainer.py — Turn a trace + correlated logs into a plain-English root cause.

Deterministic and rule-based: the explanation is keyed off the SAI status codes found in the
correlated logs and the break layer. It needs no network and never hallucinates.
(Model-assisted phrasing of this last-mile explanation is a possible future enhancement.)
"""


from __future__ import annotations
from dataclasses import dataclass
from .walker import TraceResult
from .correlator import Correlation


@dataclass
class Explanation:
    cause: str
    next_step: str


# ----------------------------------------------------------------------------
# Deterministic knowledge base (ground truth for the explanation)
# ----------------------------------------------------------------------------
_SAI_MEANING = {
    "SAI_STATUS_NOT_IMPLEMENTED":
        ("the operation is not implemented by this ASIC's SAI library — the object was "
         "accepted into the databases but the silicon cannot program it",
         "Remove or replace the unsupported field/feature, or deploy on a platform whose "
         "SAI implements it. Check the SAI capability/attribute support for this ASIC."),
    "SAI_STATUS_TABLE_FULL":
        ("the target hardware table is full — there is no room left in the ASIC to program "
         "this object, so it silently never reaches the forwarding plane",
         "Free capacity (route summarization, remove stale entries) or move to a higher-scale "
         "platform. Monitor CRM thresholds to catch this before it blackholes traffic."),
    "SAI_STATUS_OBJECT_IN_USE":
        ("the object cannot be modified/removed because the ASIC still references it, leaving "
         "the datapath in an inconsistent state",
         "Resolve the dependency first, then retry; inspect what still references this OID."),
    "SAI_STATUS_INSUFFICIENT_RESOURCES":
        ("the ASIC lacks the resources to create this object",
         "Reduce resource pressure or choose a platform with more capacity."),
}


def _rule_explain(trace: TraceResult, corr: Correlation) -> Explanation:
    if trace.healthy:
        return Explanation(
            cause="The object is present and consistent across all four databases — intent "
                  "fully propagated to silicon.",
            next_step="No action needed.",
        )

    if trace.anomalous:
        layer = trace.break_layer or trace.hops[0].db
        return Explanation(
            cause=(f"The object is absent from {layer} — it was never configured or learned "
                  f"there — yet a downstream layer still reports a match. Real propagation "
                  f"can never skip a layer, so that downstream match is NOT confirmed to be "
                  f"THIS specific object: the check at that layer can only confirm that an "
                  f"object of the right general type exists somewhere, not that it is this "
                  f"exact one (a known limitation for object types without a fully "
                  f"instance-specific ASIC_DB match). Treat this object as NOT reliably "
                  f"present in hardware."),
            next_step=(f"Verify directly whether this object is actually configured, starting "
                      f"at {layer}. Do not rely on the downstream match alone."),
        )

    layer = trace.break_layer or "the datapath"
    last = trace.last_present_layer or "no layer"

    # Prefer an explanation anchored on a known SAI status code.
    for code in corr.sai_status_codes:
        if code in _SAI_MEANING:
            meaning, fix = _SAI_MEANING[code]
            cause = (f"Present through {last}, but propagation stopped at {layer}. "
                     f"{code} was reported: {meaning}.")
            if corr.crm_hit:
                cause += " CRM also flagged a resource threshold exceeded."
            return Explanation(cause=cause, next_step=fix)

    # Never present at any layer at all — distinct from "broke partway through": there is no
    # single break hop to point at, so don't claim one (this is what previously rendered as
    # "Present through no layer" / "stopped at None"). Real example: a management-interface
    # route that never enters the swss/ASIC dataplane pipeline by design.
    if trace.last_present_layer is None:
        return Explanation(
            cause="The object was not found at any layer of the datapath — it never entered "
                  "the SONiC software pipeline at all.",
            next_step="Confirm the intent matches something actually configured/learned on this "
                      "switch. If it's a route, check whether it's a management-interface route, "
                      "which never enters the ASIC dataplane by design.",
        )

    # Generic break with no recognized code.
    generic = (f"Present through {last}, but the object is absent from {layer}. "
               f"Intent stopped propagating at this hop.")
    if corr.crm_hit:
        return Explanation(
            cause=generic + " CRM flagged a resource threshold exceeded, indicating a full "
                            "hardware table.",
            next_step="Free hardware-table capacity or move to a higher-scale platform.",
        )
    return Explanation(
        cause=generic,
        next_step=f"Inspect the {layer} producer's logs (orchagent/syncd) around this object "
                  f"to determine why it was not programmed.",
    )


def explain(trace: TraceResult, corr: Correlation) -> Explanation:
    return _rule_explain(trace, corr)
