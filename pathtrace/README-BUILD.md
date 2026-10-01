# PathTrace — buildable source tree (with `audit --deps` enhancement)

Self-contained, build-ready PathTrace source. **Generate the SAE package from here.**

Package version: **0.1.0** — includes the `audit --deps` enhancement (fleet-wide root-cause
aggregation). This tree is based on the source currently deployed on the DUTs (so it carries
the full object-type registry, including the QoS types) plus the audit enhancement.

## Layout

```
pathtrace/                       # the Python module (traced logic + registry)
  audit.py                       #   ← depth analysis + RootCauseGroup aggregation
  cli.py                         #   ← --deps threaded into audit + JSON fields
  report.py                      #   ← per-object root cause + "Root-cause summary"
  object_types.yaml              #   full registry (ACL, L2, L3, BGP, QoS, buffers)
  (asicmatch, walker, deps, resolver, correlator, explainer, dbclient, __init__)
sonic-package/
  Dockerfile                     # SAE image definition
  manifest.json                  # package metadata (version 0.3.0)
  cli/show/plugins/pathtrace.py  # host `show pathtrace` plugin (audit has --deps)
  build_package.sh               # build the image + offline tarball
  deploy_to_dut.sh               # install/verify on a DUT over SSH
pyproject.toml, README.md
tests/                           # unit tests (incl. test_audit_deps.py)
dist/pathtrace.gz                # built artifact (produced by build_package.sh)
```

## Build the package

```bash
cd /path/to/pathtrace
bash sonic-package/build_package.sh          # -> dist/pathtrace.gz (single-platform linux/amd64)
```

`build_package.sh` builds with `--provenance=false --sbom=false` so the tarball is a clean
single-image archive that `sonic-package-manager install --from-tarball` can read. Match the
DUT CPU architecture (the current HW DUT is x86_64; build on an x86_64 host).

## Install on a DUT

```bash
bash sonic-package/deploy_to_dut.sh --host <DUT_IP> --user admin --password '<pass>' \
     --tarball dist/pathtrace.gz
```
Reinstall-safe: it disables + uninstalls any existing pathtrace, installs fresh, enables the
feature, waits for the container, and runs post-install checks.
