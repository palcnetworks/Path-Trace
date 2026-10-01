# PathTrace — Datapath Intent Tracing for SONiC

## High Level Design Document

### Rev 0.1

---

## Table of Contents

- [Revision](#revision)
- [Scope](#scope)
- [Definitions / Abbreviations](#definitions--abbreviations)
- [1 Overview](#1-overview)
  - [1.1 Problem Statement](#11-problem-statement)
  - [1.2 Goals](#12-goals)
  - [1.3 Non-Goals](#13-non-goals)
- [2 Requirements](#2-requirements)
  - [2.1 Functional Requirements](#21-functional-requirements)
  - [2.2 Configuration and Management Requirements](#22-configuration-and-management-requirements)
  - [2.3 Scalability Requirements](#23-scalability-requirements)
  - [2.4 Warm Boot Requirements](#24-warm-boot-requirements)
- [3 Architecture Design](#3-architecture-design)
  - [3.1 Deployment Model (SONiC Application Extension)](#31-deployment-model-sonic-application-extension)
  - [3.2 Component Pipeline](#32-component-pipeline)
- [4 High-Level Design](#4-high-level-design)
  - [4.1 Module Breakdown](#41-module-breakdown)
  - [4.2 The Declarative Object Registry](#42-the-declarative-object-registry)
  - [4.3 Intent Resolution](#43-intent-resolution)
  - [4.4 Datapath Walk and Break Detection](#44-datapath-walk-and-break-detection)
  - [4.5 Instance-Level ASIC_DB Matching](#45-instance-level-asic_db-matching)
  - [4.6 The Anomaly Guard (Correctness Guard)](#46-the-anomaly-guard-correctness-guard)
  - [4.7 Log Correlation](#47-log-correlation)
  - [4.8 Root-Cause Explanation](#48-root-cause-explanation)
  - [4.9 Dependency Tracing](#49-dependency-tracing)
  - [4.10 Audit Mode and Fleet-Wide Root-Cause Aggregation](#410-audit-mode-and-fleet-wide-root-cause-aggregation)
  - [4.11 Database Access Layer](#411-database-access-layer)
- [5 Database Interaction](#5-database-interaction)
- [6 SAI API](#6-sai-api)
- [7 Configuration and Management](#7-configuration-and-management)
  - [7.1 CLI (show commands)](#71-cli-show-commands)
  - [7.2 Native pathtrace CLI](#72-native-pathtrace-cli)
  - [7.3 JSON Output Schema](#73-json-output-schema)
  - [7.4 Exit Codes](#74-exit-codes)
  - [7.5 YANG Model](#75-yang-model)
- [8 Warmboot and Fastboot Design Impact](#8-warmboot-and-fastboot-design-impact)
- [9 Memory, Performance and Scalability](#9-memory-performance-and-scalability)
- [10 Restrictions / Limitations](#10-restrictions--limitations)
- [11 Testing Requirements / Design](#11-testing-requirements--design)
- [12 Future Work](#12-future-work)
- [13 Open / Action Items](#13-open--action-items)

---

## Revision

| Rev | Date       | Author             | Change Description                     |
|-----|------------|--------------------|----------------------------------------|
| 0.1 | 2026-10-01 | PathTrace Team (PalC Networks) | Initial high-level design |

---

## Scope

This document describes the high-level design of **PathTrace**, a read-only diagnostic
utility for SONiC that traces a single configured *intent* (for example "ACL rule
`BLOCK_LIST/RULE_11`" or "VLAN member `Vlan100/Ethernet0`") across the SONiC datapath —
`CONFIG_DB → APPL_DB → STATE_DB → ASIC_DB` — locates the first layer where propagation
stopped, correlates the relevant `orchagent`/`syncd`/`swss` syslog activity, and produces a
plain-language root cause and a suggested next step.

The document covers the tracing engine, the declarative object-type registry, instance-level
ASIC_DB matching, dependency tracing, audit (fleet-wide sweep) mode, the SONiC CLI
integration, and the SONiC Application Extension (SAE) packaging. It does **not** cover any
control-plane or dataplane modification: PathTrace never writes to any database and introduces
no new SAI objects.

---

## Definitions / Abbreviations

| Term | Definition |
|------|------------|
| **Intent** | A human-readable identifier for one configured object, e.g. `acl rule BLOCK_LIST RULE_11`. |
| **Datapath** | The chain of SONiC databases an intent flows through before it is programmed in silicon: `CONFIG_DB → APPL_DB → STATE_DB → ASIC_DB`. |
| **Hop** | A single DB layer in the trace of one object. |
| **Break** | The first layer that is `ABSENT` while a preceding layer was `PRESENT` — where propagation stopped. |
| **Registry** | The declarative YAML file (`object_types.yaml`) that defines, as data, how each object type propagates across the datapath. |
| **OID** | SAI Object Identifier, a runtime-allocated opaque handle used as the ASIC_DB key. |
| **SAE** | SONiC Application Extension — the packaging/installation mechanism driven by `sonic-package-manager`. |
| **SAI** | Switch Abstraction Interface. |
| **CRM** | Critical Resource Monitoring (SONiC hardware-table usage thresholds). |
| **DUT** | Device Under Test. |
| **VS** | SONiC Virtual Switch. |

---

## 1 Overview

### 1.1 Problem Statement

A SONiC configuration change flows `CONFIG_DB → APPL_DB → STATE_DB → ASIC_DB → silicon`. When
an object is *accepted into the databases but never programmed into hardware* (for example
`syncd` returns a SAI error), the failure is **silent**: the CLI and `CONFIG_DB` report the
object as "present", while the datapath behaves as though it does not exist.

Diagnosing such a failure today means hand-walking four Redis databases with `redis-cli` /
`sonic-db-cli` and grepping `orchagent`/`syncd` logs — and, crucially, ASIC_DB keys are opaque
SAI OIDs with no human-readable name, so even confirming "is *this specific* object in
hardware?" is non-trivial.

PathTrace reduces this to one read-only command:

```bash
pathtrace --intent "acl rule BLOCK_LIST RULE_11"
```

which prints a per-layer pipeline table, a verdict (`HEALTHY` / `BROKEN`), the break layer,
the correlated SAI status code, and a next step.

PathTrace is complementary to `config validate` / YANG validation: those check whether a
configuration is *well-formed*; PathTrace checks whether a specific object actually
*propagated to silicon* and, if not, **where and why** it stopped.

### 1.2 Goals

- Trace one intent across all four datapath layers and deterministically locate the break.
- Confirm ASIC_DB programming at the **instance** level (this exact object), not merely
  "an object of this type exists".
- Correlate `orchagent`/`syncd`/`swss` syslog lines that genuinely name the traced object and
  surface the SAI status code.
- Produce a deterministic, rule-based root cause and next step (no inference, no network).
- Allow new object types to be added as **data** (a YAML edit), not code.
- Trace upstream **dependencies** and name the deepest unhealthy one as the likely root cause.
- Provide an **audit** mode that sweeps every configured object of a type (or all types) and
  collapses objects sharing a broken upstream cause into a single, ranked "fix first" finding.
- Be strictly **read-only** and ship as an installable SONiC Application Extension with a
  native `show pathtrace ...` CLI.

### 1.3 Non-Goals

- PathTrace does not modify configuration, databases, or SAI state.
- It does not replace YANG / `config validate` well-formedness checking.
- It does not inject or test traffic; it reasons purely over existing Redis state and syslog.
- It is not a daemon; it performs no background work and acts only when invoked.

---

## 2 Requirements

### 2.1 Functional Requirements

1. Parse a human intent string and map it to the expected key at each DB layer.
2. For each layer, classify the object as `PRESENT`, `ABSENT`, `NOT_REACHED`, or `N/A`.
3. Identify the **break** = first `ABSENT` following at least one `PRESENT`.
4. For ASIC_DB, confirm the *specific* instance is programmed via one of several declarative
   match strategies (key-field, OID chain, ACL-counter link, attribute comparison), reporting
   **how** the match was made.
5. Never report a healthy verdict when a downstream match masks a genuine upstream absence
   (the *anomaly guard*).
6. Correlate syslog lines that name the traced object and extract `SAI_STATUS_*` codes.
7. Emit a deterministic root cause and next step keyed off the break layer and SAI status.
8. Support dependency tracing (`--deps`) and fleet-wide audit (`--audit <type|all>`).
9. Provide compact table output (default), a detailed per-hop view (`--detailed`), and
   machine-readable JSON (`--json`).
10. Return an exit status reflecting health (`0` healthy, `1` broken, `2` usage error).

### 2.2 Configuration and Management Requirements

- No configuration is required. PathTrace is entirely driven by CLI arguments.
- Integrates with the SONiC `show` CLI as `show pathtrace datapath | audit | types`.
- Installable and removable via `sonic-package-manager` (SAE package).

### 2.3 Scalability Requirements

- Single-object traces are bounded by the number of SAI objects of the relevant type scanned
  in ASIC_DB.
- Audit and dependency traces use a per-run read-through cache (`CachedBackend`) so repeated
  scans (e.g. "every host interface in ASIC_DB") are read from Redis only once.
- Dependency recursion is bounded to depth 4 with cycle/diamond detection.

### 2.4 Warm Boot Requirements

- PathTrace is a read-only, on-demand tool with no persistent state; it has **no** warm-boot
  or fast-boot interaction (see [Section 8](#8-warmboot-and-fastboot-design-impact)).

---

## 3 Architecture Design

### 3.1 Deployment Model (SONiC Application Extension)

PathTrace ships as a SONiC Application Extension (SAE) image built from
`sonic-package/Dockerfile` and installed with:

```bash
sudo sonic-package-manager install --from-tarball dist/pathtrace.gz --enable
```

Key packaging facts (from `sonic-package/manifest.json` and `Dockerfile`):

- **Non-privileged** host-service container; mounts `/var/log/syslog` read-only.
- Ordered `after: [database, swss, syncd]` so the databases exist when it runs.
- Runs an idle service (`tail -f /dev/null`) — PathTrace does no background work; the host
  CLI plugin `docker exec`s into the container on demand.
- The CLI plugin (`cli/show/plugins/pathtrace.py`) is installed to the location declared in
  the manifest (`"cli": { "show": "/cli/show/plugins/pathtrace.py" }`) and auto-discovered by
  SONiC's click-based CLI.

```
┌─────────────────────────── SONiC host ───────────────────────────┐
│                                                                   │
│  show pathtrace datapath --intent "..."                           │
│         │  (click plugin: cli/show/plugins/pathtrace.py)          │
│         ▼                                                          │
│  docker exec pathtrace  pathtrace --intent "..." --force-color    │
│         │                                                          │
│  ┌──────▼────────────── pathtrace container ────────────────┐     │
│  │  pathtrace CLI  ──►  RedisBackend  ──►  Redis (CONFIG_DB, │     │
│  │                                        APPL_DB, STATE_DB, │     │
│  │                                        ASIC_DB, COUNTERS) │     │
│  │                 ──►  /var/log/syslog (ro mount)           │     │
│  └──────────────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────────┘
```

### 3.2 Component Pipeline

A single trace flows through a deterministic pipeline of pure components:

```
intent string
     │
     ▼
 resolver.resolve()  ──(reads)──►  object_types.yaml (Registry)
     │  ResolvedIntent (object_type, discriminator, hops[], dependencies[])
     ▼
 walker.trace(backend, intent)  ──(reads)──►  DBBackend (Redis)
     │                           ──(uses)───►  asicmatch.Finder (ASIC hops)
     │  TraceResult (hops[], break_layer, healthy, anomalous)
     ▼
 correlator.correlate(trace, logs)
     │  Correlation (lines[], sai_status_codes[], no_specific_match)
     ▼
 explainer.explain(trace, corr)
     │  Explanation (cause, next_step)
     ▼
 report.render* / cli JSON  ──►  terminal / JSON / exit status
```

Every component is deterministic and side-effect-free with respect to switch state: the trace
is a sequence of database reads, so the verdict is ground truth, not inference.

---

## 4 High-Level Design

### 4.1 Module Breakdown

| Module | Role |
|--------|------|
| `cli.py` | Command-line entrypoint; argument parsing, orchestration, JSON assembly, exit status. |
| `resolver.py` | Loads the registry; maps an intent to a `ResolvedIntent` (per-DB `HopSpec`s + dependency intents). |
| `object_types.yaml` | Declarative registry of all traceable object types (data, not code). |
| `asicmatch.py` | Instance-level ASIC_DB match strategies (key-field, OID chain, ACL-counter link, attribute compare, QoS map, port-QoS). |
| `walker.py` | Walks the hops, classifies each layer, finds the break, applies the anomaly guard, computes health. |
| `dbclient.py` | DB access abstraction: `DBBackend` interface, `RedisBackend`, `CachedBackend`. |
| `correlator.py` | Scores syslog lines by relevance to the traced object; extracts SAI status codes. |
| `explainer.py` | Deterministic rule-based root cause + next step, keyed off SAI status and break layer. |
| `deps.py` | Builds a dependency tree and finds the likely root cause. |
| `audit.py` | Sweeps every CONFIG_DB object of a type; collapses shared upstream failures. |
| `report.py` | Renders compact summary table, detailed per-hop view, dependency / audit views. |

### 4.2 The Declarative Object Registry

The set of traceable object types is **data**, held in `object_types.yaml`. Adding a new
object type is a YAML edit — the engine (resolver → walker → correlator → explainer) is
unchanged. Each entry defines:

| Field | Meaning |
|-------|---------|
| `name` | Object type id (snake_case). |
| `intent_prefix` | Leading words that select this type (e.g. `acl rule`). |
| `match` | Regex with named groups parsing the intent argument; group order is the audit argument order. |
| `discriminator` | Format string over groups giving the object's identity (e.g. `{table}/{rule}`). |
| `verified_on_hw` | Whether every hop has been confirmed on real hardware. |
| `depends_on` | Optional upstream intents (with optional conditional `when`), used by `--deps`. |
| `audit` | Optional scan prefixes / intent template for `--audit` enumeration. |
| `hops` | Ordered per-DB specs: exact `key`, `na: true`, or ASIC `asic_type` + `asic_match`. |

Prefixes are matched **longest-first** (`Registry.__init__` sorts by `-len(intent_prefix)`),
so a more specific prefix (e.g. `vlan member`) wins over a shorter one (`vlan`).

The registry currently defines **12 object types** across L2 / QoS / ACL subsystems:
`acl_rule`, `acl_table`, `vlan`, `vlan_member`, `vlan_interface`, `port`, `portchannel`,
`qos_map`, `scheduler`, `wred_profile`, `buffer_profile`, `port_qos`.

### 4.3 Intent Resolution

`resolver.resolve(intent)`:

1. `Registry.match_intent()` finds the spec whose `intent_prefix` matches and parses the
   remaining argument with the spec's `match` regex into named groups (raising
   `IntentParseError` on a malformed intent).
2. `_build_hops()` turns each hop entry into a `HopSpec`:
   - `na: true` → a layer that legitimately doesn't hold this type.
   - `asic_type` present → an ASIC hop; `asicmatch.build_finder()` produces an instance-level
     `Finder` closure from the `asic_match` strategy.
   - otherwise → an exact-key hop; the `key` template is formatted with the groups and the
     per-DB separator (`{SEP}` → `|` for CONFIG_DB/STATE_DB, `:` for APPL_DB/ASIC_DB).
3. `_build_dependencies()` expands `depends_on` into concrete intent strings, honoring
   conditional `when` clauses (e.g. a VLAN member depends on `port {port}` only if the port
   name matches `^Ethernet\d+$`, else `portchannel {port}`).

The DB indices are fixed and stable across releases
(`CONFIG_DB=4, APPL_DB=0, STATE_DB=6, ASIC_DB=1, COUNTERS_DB=2`).

### 4.4 Datapath Walk and Break Detection

`walker.trace()` evaluates each `HopSpec` via `_check_hop()` into a `HopResult` with one of:

- `PRESENT` — object found at this layer.
- `ABSENT` — not found at this layer.
- `NOT_REACHED` — absent *and* downstream of a break (never had a chance).
- `N/A` — this layer legitimately doesn't hold this object type.

Break detection:

1. The **break** is the first `ABSENT` hop that follows at least one `PRESENT` hop. That
   `PRESENT → ABSENT` transition is exactly where the intent stopped propagating.
2. Every `ABSENT` hop strictly after the break is relabeled `NOT_REACHED`.
3. **Health**: the object is healthy iff the last non-`N/A` layer is `PRESENT` **and** the
   trace is not anomalous. Trailing `N/A` layers are skipped so a control-plane-only type with
   no discrete ASIC representation can still be healthy.

### 4.5 Instance-Level ASIC_DB Matching

ASIC_DB keys are runtime-allocated SAI OIDs and most SAI objects carry no human-readable name,
so "an object of this SAI type exists" is **not** evidence that *this* object is programmed.
`asicmatch.py` provides progressively stronger strategies, each reporting **how** the match
was made and, on failure, **which link of the chain was missing**:

| Strategy (`asic_match.by`) | Technique | Strength |
|----------------------------|-----------|----------|
| `key_json` | SAI key is JSON (route/neighbor/FDB entries); a named field equals the expected value. | exact |
| `oid_chain` | Follow OID references step-by-step from a named object (host-interface name, VLAN id) down to the target object. | exact |
| `acl_entry` | ACL rule → its per-rule counter via `COUNTERS_DB ACL_COUNTER_RULE_MAP` → the ACL entry that references that counter; else fall back to CONFIG_DB-vs-SAI attribute comparison. | exact / strong |
| `config_attrs` | Compare a CONFIG_DB row's fields against a SAI object's attributes (scheduler, WRED, buffer profile). | strong |
| `qos_map` | Compare every key/value entry of a CONFIG_DB QoS map against the SAI `QOS_MAP` entry list. | strong |
| `port_qos` | Host-interface → port OID → bound QoS-map OIDs, confirming contents equal CONFIG_DB. | exact chain |
| `key_contains` | Substring of the SAI key. | weak (legacy) |
| `field_present_prefix` | Any object of the right SAI type exists. | weakest |

Example — `vlan_member` ASIC match is an OID chain:
`SAI_HOSTIF (by name) → SAI_BRIDGE_PORT (by port OID) → SAI_VLAN (by VLAN id) →
SAI_VLAN_MEMBER (by VLAN OID + bridge-port OID)`. If the chain breaks, the failure message
names the exact step, e.g. *"{port} has no bridge port (not a switchport in the default
bridge)"*.

Attribute comparators normalize representation differences: IP prefixes
(`10.44.0.0&mask:255.255.0.0` ↔ `10.44.0.0/16`), integers (`0x…` and `&mask` forms), packet
actions (`forward` ↔ `SAI_PACKET_ACTION_FORWARD`), and enumerated maps. Values that cannot be
translated are reported as "not comparable" rather than treated as mismatches, and an
ambiguous match (multiple SAI objects share the compared attributes) is reported distinctly
(`attributes-ambiguous`).

### 4.6 The Anomaly Guard (Correctness Guard)

Real propagation can never skip a layer. Therefore a hop that is `ABSENT` while a **later**
hop is `PRESENT` is not legitimate signal — it means the downstream match could not guarantee
it found *this specific* object (e.g. a weak `field_present_prefix` / type-level ASIC match).

`walker.trace()` sets `anomalous = True` whenever any hop after the first `ABSENT` is still
`PRESENT`, forces the verdict to **not healthy**, and the explainer emits a dedicated message
telling the operator to verify the object directly rather than trust the downstream match.

This guard was added in response to a **real false-`HEALTHY` verdict observed on hardware**,
where a type-level ASIC scan matched a *different* object of the same SAI type.

### 4.7 Log Correlation

`correlator.correlate()` pulls the syslog evidence that explains a break:

- Only lines from the datapath daemons (`orchagent`, `syncd`, `swss`) are considered.
- A line must contain at least one **specific** token that names *this* object (discriminator
  parts and object-type name words) to be counted as evidence. **Generic** signals
  (`SAI_STATUS`, `CrmResource`, `THRESHOLD_EXCEEDED`) only boost ranking among already-matched
  lines — they are never sufficient alone, so an unrelated `SAI_STATUS_*` from a different SAI
  API is not mis-attributed to this object.
- The top lines (by score) are returned in chronological order; `SAI_STATUS_*` codes are
  extracted from the **full** lines before display truncation (≤ 300 chars/line).
- `no_specific_match` is set when daemon lines existed but none named this object — so the
  report can say "we looked and found nothing tied to this object" rather than show nothing.

### 4.8 Root-Cause Explanation

`explainer.explain()` is deterministic and rule-based (no network, no model inference). It
keys off the break layer and the SAI status codes found by the correlator, against a small
knowledge base (`_SAI_MEANING`) covering, for example:

- `SAI_STATUS_NOT_IMPLEMENTED` — object accepted into DBs but the ASIC's SAI library cannot
  program it → remove/replace the unsupported field or deploy on a capable platform.
- `SAI_STATUS_TABLE_FULL` — hardware table full → free capacity / move to higher-scale
  platform / monitor CRM.
- `SAI_STATUS_OBJECT_IN_USE`, `SAI_STATUS_INSUFFICIENT_RESOURCES` — dependency / capacity.

It also handles the distinct cases of an **anomalous** trace, an object **never present at any
layer** (e.g. a management-interface route that never enters the dataplane by design), and a
generic break with a CRM resource-threshold hit.

### 4.9 Dependency Tracing

With `--deps`, `deps.build_tree()` recursively traces the object and every intent in its
registry `depends_on` list, using the same walker and a shared `CachedBackend`:

- Recursion bounded to `MAX_DEPTH = 4`; a `seen` set prevents cycles/diamonds.
- `likely_root_cause()` returns the **deepest unhealthy node whose own dependencies are all
  healthy** — i.e. the first thing worth fixing. It returns the root itself when the root is
  unhealthy but nothing upstream explains it, and `None` when everything is healthy.

Example: a broken `vlan member 100 Ethernet0` may be healthy in itself, but its dependency
`vlan 100` failed to program — PathTrace names `vlan 100` as the likely root cause.

### 4.10 Audit Mode and Fleet-Wide Root-Cause Aggregation

`audit.audit_type()` answers "what else on this switch is silently broken?":

1. It **inverts** the registry's CONFIG_DB key template into a regex + scan prefix
   (`key_regex()`), enumerates every matching CONFIG_DB key, rebuilds an intent for each, and
   traces it with the same walker — no per-type code.
2. For each broken object the syslog is correlated and explained.
3. With `--deps`, each broken object's upstream dependencies are traced and the deepest
   unhealthy one recorded.
4. `root_cause_groups()` **collapses broken objects that share one upstream dependency** into
   a single finding, ordered by blast radius (largest first). A single broken VLAN that
   strands 48 members is reported **once** with its cascade, not as 48 look-alike failures.

`--audit all` sweeps every auditable type (those with a CONFIG_DB key hop). A read-through
`CachedBackend` makes repeated scans cheap across the whole sweep.

### 4.11 Database Access Layer

`dbclient.py` defines a narrow `DBBackend` interface:

- `get_hash(db, key)` → `HGETALL` (None if absent)
- `scan_type(db, type_prefix)` → all `ASIC_STATE:<type>:*` objects
- `scan_prefix(db, key_prefix)` → all keys under a prefix
- `logs()` → syslog lines for the correlator

Two implementations: `RedisBackend` (the only production backend; connects per-DB-index with
`decode_responses=True`, tails `/var/log/syslog`, with a SONiC-VS fallback that reads a seeded
syslog list from STATE_DB) and `CachedBackend` (per-run read-through cache for audit/deps).
Because the engine depends only on the interface, test doubles (`tests/mock_backend.py`) work
without the engine knowing they exist.

---

## 5 Database Interaction

PathTrace is **read-only** against the following databases. No keys are created, modified, or
deleted.

| Database | Index | Access | Purpose |
|----------|-------|--------|---------|
| CONFIG_DB | 4 | read (`get_hash`, `scan_prefix`) | Desired intent; audit enumeration source. |
| APPL_DB | 0 | read (`get_hash`) | Object handed to orchagent. |
| STATE_DB | 6 | read (`get_hash`) | Operational state; VS syslog-seed fallback. |
| ASIC_DB | 1 | read (`scan_type`, `get_hash`) | SAI programming; instance-level match. |
| COUNTERS_DB | 2 | read (`get_hash`) | `ACL_COUNTER_RULE_MAP` for ACL-rule counter linking. |

Separators per DB: `|` for CONFIG_DB/STATE_DB, `:` for APPL_DB/ASIC_DB. ASIC_DB keys follow
`ASIC_STATE:SAI_OBJECT_TYPE_*:oid:0x...`.

No schema changes are introduced in any database.

---

## 6 SAI API

PathTrace makes **no** SAI calls and introduces **no** SAI objects. It only *reads* the
serialized SAI state that `syncd`/`sairedis` already publish into ASIC_DB (object types,
attributes, and OID references) and the serialized SAI status codes that appear in syslog.
It interprets, but never invokes, the SAI layer.

---

## 7 Configuration and Management

### 7.1 CLI (show commands)

Installed by the SAE package and auto-discovered by the SONiC click CLI
(`cli/show/plugins/pathtrace.py`), which `docker exec`s into the container:

```bash
show pathtrace types                                            # list traceable object types
show pathtrace datapath --intent "vlan member 100 Ethernet0"    # trace one object
show pathtrace datapath --intent "vlan member 100 Ethernet0" --deps      # + dependencies
show pathtrace datapath --intent "vlan member 100 Ethernet0" --detailed  # full per-hop view
show pathtrace datapath --intent "qos map DSCP_TO_TC_MAP AZURE" --json   # machine-readable
show pathtrace audit vlan_member                               # sweep all members
show pathtrace audit vlan_member --deps                        # + fleet-wide root cause
show pathtrace audit all
```

### 7.2 Native pathtrace CLI

The container entrypoint (`pathtrace.cli:main`) is also usable directly (e.g. on SONiC VS):

```
pathtrace --intent "<intent>" [--deps] [--detailed] [--json]
pathtrace --audit <type|all> [--deps] [--json]
pathtrace --list-types
pathtrace --host <redis-host> --port <port> --syslog <path>
```

Supported intents (from the registry):
`acl rule <TABLE> <RULE>` · `acl table <TABLE>` · `vlan <VID>` ·
`vlan member <VID> <PORT>` · `vlan interface <VID>` · `port <ETH>` ·
`portchannel <LAG>` · `qos map <TABLE> <NAME>` · `scheduler <NAME>` ·
`wred profile <NAME>` · `buffer profile <NAME>` · `port qos <ETH>`.

### 7.3 JSON Output Schema

`--json` emits a machine-readable payload. For a single trace (from `cli._trace_json`):

```json
{
  "intent": "acl rule BLOCK_LIST RULE_11",
  "object_type": "acl_rule",
  "healthy": false,
  "anomalous": false,
  "break_layer": "STATE_DB",
  "hops": [
    {"db": "CONFIG_DB", "status": "PRESENT", "key": "...", "match": null, "detail": null},
    ...
  ],
  "sai_status_codes": ["SAI_STATUS_NOT_IMPLEMENTED"],
  "log_evidence_no_specific_match": false,
  "cause": "...",
  "next_step": "..."
}
```

With `--deps`, a `dependencies` tree and a `likely_root_cause` object are added. The `--audit`
JSON reports per-type counts (`healthy`/`broken`/`unverified`), `problems`, and
`root_cause_groups` (shared upstream causes with their affected objects).

### 7.4 Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Traced object healthy (for `--audit`, all objects healthy). |
| `1` | Traced object broken / anomalous (for `--audit`, anything broken or unverified). |
| `2` | Usage error (bad/missing arguments, unknown type, registry error). |

### 7.5 YANG Model

PathTrace adds **no** CONFIG_DB tables and therefore requires **no** YANG model. It is a
diagnostic/read tool, not a configurable feature.

---

## 8 Warmboot and Fastboot Design Impact

None. PathTrace holds no persistent state, runs no background/daemon logic, and performs only
on-demand reads. Its idle service (`tail -f /dev/null`) does not participate in the warm/fast
shutdown sequence (`warm-shutdown`/`fast-shutdown` are empty in the manifest). It can be run
before or after a warm/fast boot to confirm whether objects re-programmed correctly.

---

## 9 Memory, Performance and Scalability

- A single-object trace is a handful of `HGETALL`s plus, for each ASIC hop, one `SCAN` of the
  relevant SAI object type. Cost scales with the count of SAI objects of that type.
- Audit and dependency traces use `CachedBackend` so that repeated scans (common dependency,
  "every host interface") hit Redis once per run; against a live switch the state is
  effectively a snapshot for the few seconds a run takes.
- Dependency recursion is bounded (`MAX_DEPTH = 4`) with cycle detection.
- Syslog correlation reads only the last ~2000 lines — sufficient for a single-object
  correlation window — and caps any single displayed line at 300 characters so a multi-KB log
  line cannot flood output.
- The container is non-privileged and runs an idle process, so steady-state resource use is
  negligible.

---

## 10 Restrictions / Limitations

- **Per-type verification status.** Each object type carries a `verified_on_hw` flag. The
  OID-chain / counter-link / attribute-comparison matchers are covered by unit tests against
  modelled SAI keyspaces; VLAN-member and QoS traces plus `audit --deps` have additionally
  been exercised on a physical Broadcom switch. Remaining types await the manual hardware
  checklist.
- **`acl_table`** is matched at SAI-type level only (SAI ACL tables carry no name); the
  anomaly guard prevents this weak match from making a missing table look healthy.
- **ACL-rule counter linking** depends on `orchagent` populating `ACL_COUNTER_RULE_MAP`;
  without it, PathTrace falls back to attribute comparison and says so.
- **Dependency tracing** is bounded (depth 4) and follows only edges declared in the registry.
- **Single Redis connection** — multi-ASIC / chassis (per-namespace) is not yet supported.
- Platform-specific DB layout differences are handled as registry data (e.g. ACL rules are not
  persisted in APPL_DB on the tested build, so that hop is `na`).

---

## 11 Testing Requirements / Design

The test suite (`pathtrace/tests/`) runs with no switch, SSH, or Redis — it uses an in-memory
`MockBackend` implementing the `DBBackend` interface:

| Test file | Coverage |
|-----------|----------|
| `test_pathtrace.py` | End-to-end trace scenarios (healthy / broken / break-layer). |
| `test_instance_matching.py` | ASIC instance matchers (OID chain, ACL counter link, attributes). |
| `test_vlan_interface.py` | SVI router-interface OID chain. |
| `test_deps.py` | Dependency tree construction and likely-root-cause selection. |
| `test_audit.py`, `test_audit_deps.py` | Audit enumeration and fleet-wide root-cause grouping. |
| `test_registry.py` | Registry loading, prefix ordering, intent parsing. |
| `fixtures.py`, `mock_backend.py`, `switch.py` | Modelled SAI keyspaces and test backends. |

System / hardware testing: traces are validated against a real SONiC DUT (installed as an SAE
package) using known-good and deliberately-broken objects. The hardware verification history
(including the false-`HEALTHY` anomaly that motivated the anomaly guard, a log-correlation
precision fix, and registry/hop corrections for platform DB-layout differences) is the basis
for the correctness guards described in this document.

**Run:**

```bash
cd pathtrace && pytest -q
```

---

## 12 Future Work

- **Feature validators** — feature-level consistency checks built on the same read-only
  engine.
- **AI-assisted explanation** — optional model phrasing of the root cause *over the
  deterministic facts*; it would rephrase, never re-derive, the break location.
- **Multi-ASIC / chassis** support via per-namespace Redis connections.
- Expanding `verified_on_hw` coverage across all registry types.

---

## 13 Open / Action Items

| Item | Owner | Status |
|------|-------|--------|
| Complete the manual hardware checklist for all 12 registry types (`verified_on_hw`). | PathTrace Team | Open |
| Multi-ASIC / chassis (per-namespace) support. | PathTrace Team | Open |
| Replace type-level `acl_table` match with an instance-level strategy. | PathTrace Team | Open |

---

*License: Apache-2.0.*
