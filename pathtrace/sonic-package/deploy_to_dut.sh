#!/usr/bin/env bash
# deploy_to_dut.sh — deploy the PathTrace SAE tarball to a SONiC DUT over SSH and verify the
# install/reinstall actually succeeded end to end: package registered, feature enabled,
# container running, and the CLI plugins genuinely work (not just "container exists").
#
# Safe to re-run: if pathtrace is already installed, it is disabled and uninstalled first
# (sonic-package-manager refuses to uninstall an enabled feature), then freshly installed.
#
# Usage:
#   ./deploy_to_dut.sh --host <ip> --user <user> --password <password> [options]
#   ./deploy_to_dut.sh --host <ip> --user <user> --identity-file ~/.ssh/id_rsa [options]
#
# Required:
#   --host HOST              DUT management IP/hostname
#   --user USER              SSH username
#   --password PASS          SSH password
#     -- or --
#   --identity-file PATH     SSH private key (alternative to --password)
#
# Optional:
#   --password-env VARNAME   Read the password from environment variable VARNAME instead of
#                            putting it directly on the command line (avoids shell-history /
#                            `ps aux` exposure -- prefer this or --identity-file over
#                            --password when practical).
#   --port PORT              SSH port (default: 22)
#   --tarball PATH           Local path to the built tarball (default: dist/pathtrace.gz)
#   --remote-path PATH       Remote scratch path for the tarball (default: /tmp/pathtrace.gz)
#   --no-enable              Install without --enable (skips container-up checks too)
#   --timeout SECONDS        Seconds to wait for the container to reach "Up" (default: 60)
#
# Exit status: 0 only if install AND every post-install verification check passes.
set -uo pipefail
cd "$(dirname "$0")/.."   # repo root

HOST=""; SSH_USER=""; SSH_PASS=""; PASS_ENV=""; IDENTITY_FILE=""; PORT=22
TARBALL="dist/pathtrace.gz"; REMOTE_PATH="/tmp/pathtrace.gz"
ENABLE=1; WAIT_TIMEOUT=60
PKG_NAME="pathtrace"

usage() { grep '^#' "$0" | sed 's/^#!\?\s\?//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --password) SSH_PASS="$2"; shift 2 ;;
    --password-env) PASS_ENV="$2"; shift 2 ;;
    --identity-file) IDENTITY_FILE="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --tarball) TARBALL="$2"; shift 2 ;;
    --remote-path) REMOTE_PATH="$2"; shift 2 ;;
    --no-enable) ENABLE=0; shift ;;
    --timeout) WAIT_TIMEOUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -n "$HOST" ]]     || fail "--host is required"
[[ -n "$SSH_USER" ]] || fail "--user is required"
[[ -f "$TARBALL" ]]  || fail "tarball not found: $TARBALL (run build_package.sh first, or pass --tarball)"
[[ -n "$PASS_ENV" ]] && SSH_PASS="${!PASS_ENV:-}"
command -v ssh >/dev/null 2>&1 || fail "ssh not found on PATH"
command -v scp >/dev/null 2>&1 || fail "scp not found on PATH"

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10 -p "$PORT")
SCP_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10 -P "$PORT")

if [[ -n "$IDENTITY_FILE" ]]; then
  SSH_OPTS+=(-i "$IDENTITY_FILE"); SCP_OPTS+=(-i "$IDENTITY_FILE")
  ssh_run() { ssh "${SSH_OPTS[@]}" "${SSH_USER}@${HOST}" "$@"; }
  scp_put() { scp "${SCP_OPTS[@]}" "$1" "${SSH_USER}@${HOST}:$2"; }
elif [[ -n "$SSH_PASS" ]]; then
  command -v sshpass >/dev/null 2>&1 || fail "sshpass not found (needed for password auth); install it or use --identity-file"
  ssh_run() { sshpass -p "$SSH_PASS" ssh "${SSH_OPTS[@]}" "${SSH_USER}@${HOST}" "$@"; }
  scp_put() { sshpass -p "$SSH_PASS" scp "${SCP_OPTS[@]}" "$1" "${SSH_USER}@${HOST}:$2"; }
else
  fail "one of --password, --password-env, or --identity-file is required"
fi

echo ">> [1/7] checking SSH connectivity to ${SSH_USER}@${HOST}:${PORT}"
ssh_run "echo connected" >/dev/null || fail "cannot SSH to ${HOST}"
echo "   OK"

echo ">> [2/7] copying $(basename "$TARBALL") to ${HOST}:${REMOTE_PATH}"
scp_put "$TARBALL" "$REMOTE_PATH" || fail "scp failed"
echo "   OK"

echo ">> [3/7] checking for an existing install (reinstall-safe)"
if ssh_run "sonic-package-manager list 2>/dev/null | awk '{print \$1}' | grep -qx '${PKG_NAME}'"; then
  echo "   ${PKG_NAME} already installed -- disabling + uninstalling first"
  ssh_run "sudo config feature state ${PKG_NAME} disabled" \
    || echo "   (warning: disable step failed or was already disabled, continuing)"
  ssh_run "sudo sonic-package-manager uninstall ${PKG_NAME} -y" \
    || fail "uninstall of existing ${PKG_NAME} failed"
else
  echo "   not currently installed -- fresh install"
fi

echo ">> [4/7] installing from tarball"
ENABLE_FLAG=""; [[ $ENABLE -eq 1 ]] && ENABLE_FLAG="--enable"
ssh_run "sudo sonic-package-manager install --from-tarball ${REMOTE_PATH} ${ENABLE_FLAG} -y -v INFO" \
  || fail "sonic-package-manager install failed"

if [[ $ENABLE -eq 1 ]]; then
  echo ">> [5/7] ensuring feature is enabled"
  # --enable is documented to enable the feature at install time, but was observed on a real
  # DUT to leave it disabled after a reinstall -- make this step explicit/idempotent rather
  # than trusting a single code path.
  ssh_run "sudo config feature state ${PKG_NAME} enabled" || fail "could not enable feature ${PKG_NAME}"

  echo ">> [6/7] waiting for container to come up (timeout ${WAIT_TIMEOUT}s)"
  elapsed=0
  until ssh_run "sudo docker ps --format '{{.Names}} {{.Status}}'" 2>/dev/null | grep -q "^${PKG_NAME} Up"; do
    sleep 3; elapsed=$((elapsed + 3))
    if [[ $elapsed -ge $WAIT_TIMEOUT ]]; then
      fail "container did not reach 'Up' within ${WAIT_TIMEOUT}s -- check: ssh ${SSH_USER}@${HOST} sudo systemctl status ${PKG_NAME}"
    fi
  done
  echo "   OK (up after ${elapsed}s)"
else
  echo ">> [5/7] --no-enable requested, leaving feature disabled"
  echo ">> [6/7] skipping container-up wait (--no-enable)"
fi

echo ">> [7/7] post-install verification"
CHECKS_FAILED=0
check() {
  local desc="$1"; shift
  if ssh_run "$@" >/dev/null 2>&1; then
    echo "   PASS: $desc"
  else
    echo "   FAIL: $desc"
    CHECKS_FAILED=$((CHECKS_FAILED + 1))
  fi
}

check "package registered as Installed" \
  "sonic-package-manager list 2>/dev/null | grep -E '^${PKG_NAME}[[:space:]].*Installed'"

if [[ $ENABLE -eq 1 ]]; then
  check "feature state is enabled"          "show feature status ${PKG_NAME} 2>/dev/null | grep -qw enabled"
  check "container is Up"                   "sudo docker ps --format '{{.Names}} {{.Status}}' | grep -q '^${PKG_NAME} Up'"
  check "show pathtrace types works"        "show pathtrace types 2>/dev/null | grep -q acl_rule"
  # No mock scenarios in production (see pathtrace/cli.py) -- this check runs the real
  # binary against this DUT's actual live state. It verifies the binary/container/Redis
  # connectivity chain works end to end; it does not assert a specific PASS/FAIL verdict,
  # since that legitimately depends on what's really configured on this switch.
  check "pathtrace binary runs end-to-end (registry loads, no crash)" \
    "sudo docker exec ${PKG_NAME} pathtrace --list-types 2>&1 | grep -q acl_rule"
fi

ssh_run "rm -f ${REMOTE_PATH}" >/dev/null 2>&1 || true  # best-effort cleanup of the scratch copy

echo
if [[ $CHECKS_FAILED -eq 0 ]]; then
  echo "SUCCESS: pathtrace installed and verified on ${HOST}"
  exit 0
else
  echo "FAILED: ${CHECKS_FAILED} verification check(s) did not pass on ${HOST}"
  exit 1
fi
