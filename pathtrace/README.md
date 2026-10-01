# PathTrace — Datapath Intent Tracing for SONiC

> **SONiC Hackathon 2026 submission** · vendor-neutral · Apache-2.0

| | |
|---|---|
| **Project title** | PathTrace: Datapath Intent Tracing for SONiC |
| **Team name** | `PathTrace` |
| **Team members** | `Thovi Keerthi Kumar`, `Sandeep Kulambi` |
| **Company / affiliation** | `PalC Networks` |
| **Repository** | `[https://github.com/palcnetworks/Path-Trace/]` |
| **Topic** | Debugging and root-cause analysis |
| **Status** | **Completed** for the committed scope (tracer + registry + SAE packaging) — see [Status](#status) |

## What it is

A SONiC config change flows `CONFIG_DB → APPL_DB → STATE_DB → ASIC_DB → silicon`. When an
object is *accepted but never programmed* (for example syncd returns a SAI error), the failure
is silent: the CLI and CONFIG_DB say "present", the datapath says otherwise. Finding where it
stopped means hand-walking four Redis databases and grepping orchagent/syncd logs.

PathTrace does it with one read-only command:

```bash
pathtrace --intent "acl rule BLOCK_LIST RULE_11"
```

```
PathTrace — acl rule BLOCK_LIST RULE_11

  +-----------+---------+----------------------------------------+
  | LAYER     | STATUS  | DETAIL                                 |
  +-----------+---------+----------------------------------------+
  | CONFIG_DB | PRESENT | ACL_RULE|BLOCK_LIST|RULE_11            |
  | APPL_DB   | n/a     | -                                      |
  | STATE_DB  | BROKE   | (present upstream, missing here)       |
  | ASIC_DB   | skipped | (downstream of the break; not reached) |
  +-----------+---------+----------------------------------------+

  +---------+------------------------------------------------------------------+
  | Verdict | ✗ BROKEN — stopped at STATE_DB                                   |
  | Cause   | Present through CONFIG_DB, but propagation stopped at STATE_DB.  |
  |         | SAI_STATUS_NOT_IMPLEMENTED was reported: the object was accepted |
  |         | into the databases but the silicon cannot program it.            |
  | SAI     | SAI_STATUS_NOT_IMPLEMENTED                                       |
  | Next    | Remove or replace the unsupported field/feature, or deploy on a  |
  |         | platform whose SAI implements it.                                |
  +---------+------------------------------------------------------------------+
```
*(default output is this compact ASCII-grid summary; `--detailed` adds the full per-hop pipeline
view with correlated log evidence, and `--json` emits the machine-readable payload)*

`config validate` / YANG check whether configuration is *well-formed*. PathTrace checks whether a
specific object actually *propagated* to ASIC_DB and, when it did not, where and why.

## What's new for SONiC

- **Intent tracer** (`pathtrace`) — checks one object at every pipeline layer, finds the first
  present→absent break, correlates the matching orchagent/syncd syslog lines, and extracts the
  SAI status code to state a plain-language root cause and next step. Strictly read-only; reads
  existing Redis state and syslog only; introduces no new SAI objects. Exit status is `0`
  healthy / `1` broken / `2` usage error; `--json` output.
- **Declarative object registry** (`pathtrace/object_types.yaml`) — 12 object types across the
  VLAN, QoS and ACL subsystems: ACL rule, ACL table, VLAN, VLAN member, VLAN interface (SVI),
  QoS map, scheduler, WRED profile, buffer profile, port QoS, plus the port and port-channel
  types they depend on. A new object type is a YAML edit, not code.
- **QoS datapath tracing** — QoS maps, schedulers, WRED profiles, buffer profiles and per-port
  QoS bindings are confirmed in ASIC_DB by **attribute comparison**: every configured key/value
  (or SAI attribute) is checked equal in silicon, since these SAI objects carry no name.
- **Instance-level ASIC_DB matching** (`pathtrace/asicmatch.py`) — SAI objects are addressed by
  opaque OIDs, so "an object of this type exists" is not evidence that *this* object does.
  Matching follows real references instead: host-interface name → port OID → bridge port →
  VLAN → VLAN member; ACL rule → COUNTERS_DB `ACL_COUNTER_RULE_MAP` → counter OID → ACL entry
  (falling back to comparing rule fields against SAI attributes). Every ASIC hop reports how it
  matched (`matched by:`) and, when absent, which link in the chain is missing.
- **Dependency tracing** (`--deps` / `--trace-dependencies`) — the registry declares what each
  object depends on (a VLAN member needs its VLAN and its port). The tracer walks those too and
  names the deepest unhealthy dependency as the likely root cause.
- **Audit mode** (`--audit <type|all>`) — sweeps every CONFIG_DB object of a type and lists the
  ones that never reached ASIC_DB, with the SAI status codes found in syslog. It answers
  "what else on this switch is silently broken?". With `--deps` it also traces each broken
  object's dependencies and **collapses objects sharing one broken upstream cause into a single,
  ranked "fix this first" finding** (fleet-wide root-cause aggregation).
- **Presentable output** — by default `show pathtrace datapath` prints a compact ASCII-grid
  summary (pipeline table + verdict/cause/next + dependency table); `--detailed` adds the full
  per-hop view with correlated logs, and `--json` emits the complete machine-readable payload.
- **SONiC Application Extension (SAE) package** — `sonic-package/` (manifest, Dockerfile,
  build script) and a native `show pathtrace datapath` / `show pathtrace types` CLI plugin,
  installable with `sonic-package-manager`.

## Before the hackathon vs. built during the hackathon

| | |
|---|---|
| **Existed before** | Design documents only: the high-level design (`docs/architecture/pathtrace-hld.md`). No implementation. |
| **Built during the hackathon** | Everything else in this repository: the tracer engine (resolver, walker, correlator, explainer, report, Redis backend), the declarative object registry (12 types), instance-level ASIC_DB matching, dependency tracing, audit mode, the `pathtrace` CLI, the SAE package and `show pathtrace` plugin, the registry-authoring tools, unit tests, and validation on SONiC VS and a physical switch. |

## Usage

Build the SAE package and install it on a SONiC device:

```bash
sonic-package/build_package.sh
sudo sonic-package-manager install --from-tarball dist/pathtrace.gz --enable
```

Then, on the device:

```bash
show pathtrace types
show pathtrace datapath --intent "vlan member 100 Ethernet0"
show pathtrace datapath --intent "vlan member 100 Ethernet0" --deps      # + upstream dependencies
show pathtrace datapath --intent "vlan member 100 Ethernet0" --detailed  # full pipeline view + logs
show pathtrace audit vlan_member                                        # sweep all configured members
show pathtrace audit vlan_member --deps                                 # + fleet-wide root cause
show pathtrace datapath --intent "qos map DSCP_TO_TC_MAP AZURE" --json
```

Supported intents (`pathtrace --list-types`): `acl rule <TABLE> <RULE>` · `acl table <TABLE>` ·
`vlan <VID>` · `vlan member <VID> <PORT>` · `vlan interface <VID>` · `port <ETH>` ·
`portchannel <LAG>` · `qos map <TABLE> <NAME>` · `scheduler <NAME>` · `wred profile <NAME>` ·
`buffer profile <NAME>` · `port qos <ETH>`. Standalone (source checkout, against any reachable
SONiC Redis): `pip install -e . && pathtrace --intent "..." --host <redis-host>`. Run the unit
tests with `python3 -m pytest` (tracer and tools; no switch needed).

## How it works

| Component | Role |
|-----------|------|
| `resolver.py`   | Maps an intent to the expected key at each DB layer using the registry. |
| `asicmatch.py`  | Instance-level ASIC_DB matchers (key field, OID chain, ACL counter link, attribute compare), selected per type in the registry. |
| `deps.py`       | Traces an object's `depends_on` graph and picks the likely root cause. |
| `audit.py`      | Enumerates CONFIG_DB objects of a type and traces each one. |
| `dbclient.py`   | Reads the four Redis DBs (`RedisBackend`); one small interface so tests can substitute an in-memory backend. |
| `walker.py`     | Marks each hop PRESENT / ABSENT / N/A / NOT_REACHED and finds the first present→absent break. |
| `correlator.py` | Pulls the orchagent/syncd syslog lines relevant to the broken object; extracts SAI status codes. |
| `explainer.py`  | Deterministic, rule-based root cause and next step from the SAI status and break layer. |
| `report.py`     | Renders output: a compact ASCII-grid summary by default, the full per-hop pipeline view with `--detailed`, and JSON with `--json`. |

The per-hop trace is deterministic database reads — ground truth, not inference.


## Future extensions (not implemented)

- **Feature validators** — feature-level consistency checks built on the same read-only engine.
- **AI-assisted explanation** — optional model phrasing of the root cause over the deterministic
  facts; it would rephrase, never re-derive the break location.

## Status

**Completed** for the committed scope: the intent tracer, declarative registry, instance-level
matching, dependency tracing, audit mode (breadth + `--deps` root-cause aggregation) and SAE
packaging are implemented. ACL-rule, VLAN-member and QoS traces and `audit --deps`, were 
validated on SONiC VS and a physical Broadcom switch (installed via `sonic-package-manager`); 

## License
Apache-2.0 — same as SONiC.
