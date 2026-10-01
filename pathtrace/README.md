# PathTrace — Datapath Intent Tracing for SONiC

> **SONiC Hackathon 2026 submission** · vendor-neutral · Apache-2.0

| | |
|---|---|
| **Project title** | PathTrace: Datapath Intent Tracing for SONiC |
| **Team name** | `PathTrace` |
| **Team members** | `Thovi Keerthi Kumar`, `Sandeep Kulambi`, … (up to 5) |
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
- **Registry-authoring tools** (`tools/`) — a dev-only workflow that learns the real key layout of
  a new object type from a live switch and drafts the registry entry (see below).
- **Correctness guard** — a downstream ASIC_DB match after a genuine upstream absence is never
  reported as healthy (found on real hardware; see below).
- **SONiC Application Extension (SAE) package** — `sonic-package/` (manifest, Dockerfile,
  build script) and a native `show pathtrace datapath` / `show pathtrace types` CLI plugin,
  installable with `sonic-package-manager`.

## Before the hackathon vs. built during the hackathon

| | |
|---|---|
| **Existed before** | Design documents only: the high-level design (`docs/architecture/pathtrace-hld.md`). No implementation. |
| **Built during the hackathon** | Everything else in this repository: the tracer engine (resolver, walker, correlator, explainer, report, Redis backend), the declarative object registry (12 types), instance-level ASIC_DB matching, dependency tracing, audit mode, the `pathtrace` CLI, the SAE package and `show pathtrace` plugin, the registry-authoring tools, unit tests, and validation on SONiC VS and a physical switch. |

### Real-hardware findings (evidence of validation)

Installing and tracing on a physical switch found real defects that unit tests could not:

1. A `manifest.json` field that silently broke `sonic-package-manager install`.
2. A container mount conflict that blocked service startup.
3. A false **Healthy** verdict — an ASIC-layer scan matched a *different* object of the same
   type. Fixed with the anomaly guard described above.
4. A log-correlation precision gap (unrelated syslog lines attached to a broken object).
5. Registry hops that did not match the platform: e.g. ACL-rule rows are not persisted in this
   build's APPL_DB. Fixed as registry data edits plus a walker fix so the verdict uses the last
   *applicable* hop.

Write-up: [`docs/verification/live-hw-verification-report.md`](docs/verification/live-hw-verification-report.md);
console logs: [`final-hw-test-logs/`](final-hw-test-logs/).

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

## Registry-authoring tools (`tools/`)

Adding an object type means knowing its *real* key at each layer, and the only trustworthy way
to learn that is to configure the feature on a real switch and watch what appears in each DB
(hand-checking against a real switch is what exposed wrong registry hops during validation).
`tools/` automates that investigation. It is
**dev-only and never shipped** in the SAE image.

| Tool | Role |
|---|---|
| `discover_type.py` | Over SSH: snapshots all four DBs, runs your config command, snapshots again, diffs, and writes a **draft** YAML entry. Every hop is tagged `CONFIDENCE: HIGH/LOW` (ASIC matches by field prefix are always LOW). It then syntax-checks the draft against the real registry loader, re-traces the object live on the switch with the real walker, and emits a unit-test skeleton. Cleans up after itself. |
| `merge_draft.py` | After human review, splices a draft into `object_types.yaml`, re-validates the merged file, runs the test suite, and rolls back automatically on failure. Supports `--dry-run`. |
| `db_snapshot.py`, `dut_ssh.py`, `verify.py`, `yaml_draft.py` | Building blocks: `sonic-db-cli`-over-SSH snapshots/diffs, SSH/SCP connection, syntax check + live re-trace, and YAML rendering. |

Safety properties: it never writes to `pathtrace/object_types.yaml` on its own (a human merges),
asks before running the one command that mutates switch config, redacts passwords in logs,
and runs unconfigure/cleanup in a `finally` block. Its tests need no switch, SSH or Redis
(`pytest tools/`). Details: [`tools/README.md`](tools/README.md) and
[`docs/guides/discover-type-guide.md`](docs/guides/discover-type-guide.md).

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

## Known limitations

- **Verification status is tracked per type.** Each object type carries a `verified_on_hw` flag
  in the registry. The OID-chain / counter-link / attribute-comparison matchers are covered by
  unit tests against modelled SAI keyspaces; VLAN-member and QoS traces plus `audit --deps` were
  additionally exercised on a physical Broadcom switch (installed as a sonic-package SAE), while
  the remaining types await the manual
  [`docs/verification/hw-verification-checklist.md`](docs/verification/hw-verification-checklist.md).
- `acl table` can only be matched by SAI type (SAI ACL tables carry no name); the anomaly guard
  keeps that weak match from making a missing table look healthy.
- ACL-rule counter linking depends on orchagent populating `ACL_COUNTER_RULE_MAP`; without it
  the tracer compares rule fields with SAI attributes and says so.
- Dependency tracing is bounded (depth 4) and follows only edges declared in the registry.
- Single Redis connection: multi-ASIC / chassis (per-namespace) is not yet supported.

## Future extensions (not implemented)

- **Feature validators** — feature-level consistency checks built on the same read-only engine.
- **AI-assisted explanation** — optional model phrasing of the root cause over the deterministic
  facts; it would rephrase, never re-derive the break location.

## Status

**Completed** for the committed scope: the intent tracer, declarative registry, instance-level
matching, dependency tracing, audit mode (breadth + `--deps` root-cause aggregation) and SAE
packaging are implemented and covered by 102 unit tests. ACL-rule, VLAN-member and QoS traces,
and `audit --deps`, were validated on SONiC VS and a physical Broadcom switch (installed via
`sonic-package-manager`); the remaining types are pending the hardware checklist above (see Known
limitations). The future extensions are not part of this submission.

## Documentation

- [`docs/architecture/pathtrace-hld.md`](docs/architecture/pathtrace-hld.md) — high-level design
- [`docs/architecture/architecture-note.md`](docs/architecture/architecture-note.md) — architecture, safety model, what real hardware taught us
- [`docs/guides/usage-and-operations-guide.md`](docs/guides/usage-and-operations-guide.md) — every option (incl. `--deps`, `--audit`) with workflow and troubleshooting
- [`docs/guides/user-guide.md`](docs/guides/user-guide.md) — usage · [`operations-guide.md`](docs/guides/operations-guide.md) — build & install · [`discover-type-guide.md`](docs/guides/discover-type-guide.md) — registry-authoring tools

## License
Apache-2.0 — same as SONiC.
