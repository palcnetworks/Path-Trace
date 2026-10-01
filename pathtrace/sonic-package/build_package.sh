#!/usr/bin/env bash
# build_package.sh — build/rebuild the PathTrace SONiC Application Extension (SAE) artifacts
# from source: the Docker image (with the SAE manifest embedded as the required
# com.azure.sonic.manifest label) and the installable offline tarball consumed by
# `sonic-package-manager install --from-tarball`.
#
# Usage:
#   ./build_package.sh [--buildimage-path PATH] [--tag VERSION] [--out DIR]
#
# Options:
#   --buildimage-path PATH   Path to a sonic-buildimage checkout (with src/sonic-utilities
#                            populated). When given, the manifest is additionally validated
#                            against the REAL production sonic_package_manager.manifest
#                            schema, not just checked for well-formed JSON. Optional, but
#                            this is exactly the check that would have caught a real bug
#                            found in live DUT testing: an empty-string "clear" CLI field
#                            silently broke `sonic-package-manager install`.
#                            Also settable via the SONIC_BUILDIMAGE_PATH env var.
#   --tag VERSION            Override the image tag (default: package.version in manifest.json)
#   --out DIR                Output directory for the tarball (default: dist/)
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root (pathtrace_pkg/)

PKG_DIR="sonic-package"
MANIFEST_JSON="${PKG_DIR}/manifest.json"
OUT_DIR="dist"
BUILDIMAGE_PATH="${SONIC_BUILDIMAGE_PATH:-}"
TAG_OVERRIDE=""

usage() { grep '^#' "$0" | sed 's/^#!\?\s\?//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --buildimage-path) BUILDIMAGE_PATH="$2"; shift 2 ;;
    --tag) TAG_OVERRIDE="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

command -v docker >/dev/null 2>&1 || { echo "error: docker not found on PATH" >&2; exit 1; }
[[ -f "$MANIFEST_JSON" ]] || { echo "error: $MANIFEST_JSON not found (run from the pathtrace_pkg source tree)" >&2; exit 1; }

VERSION="$(python3 -c "import json; print(json.load(open('${MANIFEST_JSON}'))['package']['version'])")"
IMAGE="pathtrace:${TAG_OVERRIDE:-$VERSION}"

echo ">> [1/5] lightweight manifest sanity check"
python3 - "$MANIFEST_JSON" <<'PYEOF'
import json, sys
path = sys.argv[1]
m = json.load(open(path))
# Guards against the exact bug class found in live testing: an empty-string CLI field
# marshals (via the real ListMarshaller) into a one-element list containing "", and the
# installer then tries to extract a file from an empty path inside the image, failing with
# a raw Docker API error deep in the install.
bad = [k for k, v in m.get("cli", {}).items() if isinstance(v, str) and v == ""]
if bad:
    print(f"error: cli.{bad[0]} is an empty string -- must be a real path, omitted, or [].",
          file=sys.stderr)
    sys.exit(1)
for required in ("version", "package", "service", "container", "cli"):
    if required not in m:
        print(f"error: manifest missing required top-level key '{required}'", file=sys.stderr)
        sys.exit(1)
print("   OK: no empty-string cli.* fields, required top-level keys present")
PYEOF

if [[ -n "$BUILDIMAGE_PATH" ]]; then
  SPM_SRC="${BUILDIMAGE_PATH}/src/sonic-utilities/sonic_package_manager"
  echo ">> [2/5] validating manifest against the REAL sonic_package_manager schema"
  echo "   (buildimage: ${BUILDIMAGE_PATH})"
  if [[ ! -f "${SPM_SRC}/manifest.py" ]]; then
    echo "   WARNING: ${SPM_SRC}/manifest.py not found -- is the src/sonic-utilities submodule populated? Skipping deep validation." >&2
  else
    set +e
    python3 - "$SPM_SRC" "$MANIFEST_JSON" <<'PYEOF'
import sys, json, importlib.util, types

base, manifest_path = sys.argv[1], sys.argv[2]

# Load only the manifest-schema dependency chain directly, NOT the full sonic_package_manager
# package -- its __init__.py eagerly imports config.config_mgmt -> sonic_yang, a native
# module that only exists after a full SONiC image build, and isn't needed just to validate
# the manifest schema.
pkg = types.ModuleType("sonic_package_manager")
pkg.__path__ = [base]
sys.modules["sonic_package_manager"] = pkg

def load(name, filename):
    spec = importlib.util.spec_from_file_location(f"sonic_package_manager.{name}", f"{base}/{filename}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"sonic_package_manager.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod

try:
    load("version", "version.py")
    load("constraint", "constraint.py")
    load("errors", "errors.py")
    load("database", "database.py")
    Manifest = load("manifest", "manifest.py").Manifest
except ModuleNotFoundError as e:
    # A missing pip dependency (e.g. semantic_version) is an environment gap, not a manifest
    # problem -- degrade to a warning rather than failing the whole build over an optional
    # extra check. Exit code 2 signals "skipped", distinct from 1 ("genuinely invalid").
    print(f"   WARNING: optional dependency missing ({e}); skipping deep validation. "
          f"Install with: pip install semantic_version", file=sys.stderr)
    sys.exit(2)

raw = json.load(open(manifest_path))
try:
    Manifest.marshal(raw)
    print("   OK: manifest conforms to the real production schema")
except Exception as e:
    print(f"   error: manifest failed real schema validation: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
    rc=$?
    set -e
    [[ $rc -eq 1 ]] && { echo "   deep validation failed -- aborting build" >&2; exit 1; }
  fi
else
  echo ">> [2/5] skipping deep schema validation (no --buildimage-path / SONIC_BUILDIMAGE_PATH given)"
fi

echo ">> [3/5] building image ${IMAGE}"
MANIFEST="$(cat "$MANIFEST_JSON")"
# --provenance=false --sbom=false force a SINGLE-platform image. Without them, modern BuildKit
# can emit an OCI manifest-list plus an attestation manifest; `docker save` of that produces a
# multi-entry tarball that `sonic-package-manager install --from-tarball` cannot read.
docker build \
    --provenance=false --sbom=false \
    -f "${PKG_DIR}/Dockerfile" \
    --build-arg manifest="${MANIFEST}" \
    -t "${IMAGE}" \
    .

echo ">> [4/5] verifying manifest label is present and well-formed on the built image"
docker inspect -f '{{ index .Config.Labels "com.azure.sonic.manifest" }}' "${IMAGE}" \
    | python3 -m json.tool >/dev/null
echo "   OK"

echo ">> [5/5] saving offline tarball"
mkdir -p "$OUT_DIR"
TARBALL="${OUT_DIR}/pathtrace.gz"
docker save "${IMAGE}" | gzip > "$TARBALL"
echo "   wrote ${TARBALL} ($(du -h "$TARBALL" | cut -f1))"

cat <<EOF

Build complete: ${TARBALL}

Install it on a DUT with:
  sudo sonic-package-manager install --from-tarball ${TARBALL} --enable
EOF
