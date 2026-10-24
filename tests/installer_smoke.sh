#!/usr/bin/env bash
# Smoke test for install.sh: installs from a local fake "release" served over HTTP.
# Checks the checksum gate, the refusal to overwrite a foreign `lab`, upgrade and uninstall.
# Needs: bash, curl, tar, sha256sum, python3. Does not need Proxmox or root. Run from anywhere.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
PORT="${PORT:-18765}"
SERVER_PID=""
cleanup() { [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true; rm -rf "$WORK"; }
trap cleanup EXIT

pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1" >&2; exit 1; }

make_release() {  # <tag>: a release directory with the tarball, SHA256SUMS and the "latest" answer
  local tag="$1" name="project-lab-$1.tar.gz"
  mkdir -p "$WORK/srv/dl/$tag" "$WORK/srv/api/releases"
  tar -czf "$WORK/srv/dl/$tag/$name" -C "$HERE" --transform "s,^,project-lab-$tag/," src
  (cd "$WORK/srv/dl/$tag" && sha256sum "$name" > SHA256SUMS)
  printf '{"tag_name": "%s"}' "$tag" > "$WORK/srv/api/releases/latest"
}

make_release v9.9.1
(cd "$WORK/srv" && exec python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1) &
SERVER_PID=$!
sleep 1

mkdir -p "$WORK/bin"
export LAB_NO_ROOT_CHECK=1 LAB_PREFIX="$WORK/opt" LAB_BIN="$WORK/bin/lab"
export LAB_API_URL="http://127.0.0.1:$PORT/api" LAB_DOWNLOAD_URL="http://127.0.0.1:$PORT/dl"

# 1. a normal install
bash "$HERE/install.sh" --no-setup >"$WORK/out1.txt" 2>&1 || { cat "$WORK/out1.txt"; fail "install"; }
grep -q "checksum verified" "$WORK/out1.txt" || fail "checksum not verified"
[ -L "$WORK/opt/current" ] || [ -d "$WORK/opt/current" ] || fail "current link missing"
"$WORK/bin/lab" version | grep -q "lab " || fail "installed command does not run"
pass "install, checksum, command runs"

# 2. an upgrade keeps the older release for rollback and moves the link
make_release v9.9.2
bash "$HERE/install.sh" --no-setup >/dev/null 2>&1 || fail "upgrade"
[ -d "$WORK/opt/releases/v9.9.1" ] && [ -d "$WORK/opt/releases/v9.9.2" ] || fail "releases not kept"
pass "upgrade keeps the previous release"

# 3. a corrupted download is refused and nothing changes
python3 - "$WORK/srv/dl/v9.9.2/project-lab-v9.9.2.tar.gz" <<'PY'
import sys
p = sys.argv[1]
b = bytearray(open(p, "rb").read())
b[100] ^= 0xFF
open(p, "wb").write(b)
PY
if bash "$HERE/install.sh" --no-setup >"$WORK/out3.txt" 2>&1; then fail "a corrupted download was accepted"; fi
grep -q "checksum mismatch" "$WORK/out3.txt" || fail "no checksum message"
pass "corrupted download refused"

# 4. a `lab` that is not ours is never overwritten
make_release v9.9.2
rm -f "$WORK/bin/lab"
printf '#!/bin/sh\necho mine\n' > "$WORK/bin/lab"
if bash "$HERE/install.sh" --no-setup >"$WORK/out4.txt" 2>&1; then fail "overwrote a foreign lab"; fi
grep -q "not created by this installer" "$WORK/out4.txt" || fail "no message about the foreign lab"
grep -q mine "$WORK/bin/lab" || fail "the foreign lab was modified"
pass "foreign lab left alone"

# 5. uninstall removes only the program
rm -f "$WORK/bin/lab"
bash "$HERE/install.sh" --no-setup >/dev/null 2>&1 || fail "reinstall"
bash "$HERE/install.sh" --uninstall >/dev/null 2>&1 || fail "uninstall"
[ ! -e "$WORK/opt" ] && [ ! -e "$WORK/bin/lab" ] || fail "uninstall left files behind"
pass "uninstall"

# 6. outside Proxmox (and without the test override) it refuses with a clear message
if env -u LAB_NO_ROOT_CHECK bash "$HERE/install.sh" >"$WORK/out6.txt" 2>&1; then fail "ran outside root/Proxmox"; fi
pass "refuses without root"

echo "all installer checks passed"
