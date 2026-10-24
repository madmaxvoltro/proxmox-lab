#!/usr/bin/env bash
# Smoke test for install-client.sh against a local fake release served over HTTP.
# Checks asset selection for all four Linux/macOS builds, the checksum gate, replacing a running install,
# and uninstall. Needs bash, curl, python3. Does not touch your real ~/.local/bin.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
PORT="${PORT:-18766}"
SERVER_PID=""
cleanup() { if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; fi; rm -rf "$WORK"; }
trap cleanup EXIT

pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1" >&2; exit 1; }
sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

make_release() {  # <tag>: four fake builds (tiny scripts that answer `version` and `uninstall`) plus SHA256SUMS
  local tag="$1" dir="$WORK/srv/dl/$1" asset
  mkdir -p "$dir" "$WORK/srv/api/releases"
  : > "$dir/SHA256SUMS"
  for asset in lab-linux-x64 lab-linux-arm64 lab-macos-x64 lab-macos-arm64; do
    # shellcheck disable=SC2016
    printf '#!/bin/sh\ncase "$1" in version) echo "lab %s %s";; uninstall) echo "fake uninstall";; *) echo "args: $*";; esac\n' "$tag" "$asset" > "$dir/$asset"
    printf '%s  %s\n' "$(sha "$dir/$asset")" "$asset" >> "$dir/SHA256SUMS"
  done
  printf '{"tag_name": "%s"}' "$tag" > "$WORK/srv/api/releases/latest"
}

make_release v9.8.1
(cd "$WORK/srv" && exec python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1) &
SERVER_PID=$!
sleep 1

export LAB_BIN_DIR="$WORK/bin" LAB_API_URL="http://127.0.0.1:$PORT/api" LAB_DOWNLOAD_URL="http://127.0.0.1:$PORT/dl"

# 1. each supported system gets its own build
for combo in "linux x86_64 lab-linux-x64" "linux aarch64 lab-linux-arm64" "macos x86_64 lab-macos-x64" "macos arm64 lab-macos-arm64"; do
  read -r os arch asset <<<"$combo"
  rm -rf "${WORK:?}/bin"
  LAB_OS="$os" LAB_ARCH="$arch" bash "$HERE/install-client.sh" --no-connect >"$WORK/out.txt" 2>&1 || { cat "$WORK/out.txt"; fail "install for $os/$arch"; }
  "$WORK/bin/lab" version | grep -q "$asset" || fail "$os/$arch did not get $asset"
  grep -q "checksum verified" "$WORK/out.txt" || fail "checksum not verified for $os/$arch"
done
pass "linux x64/arm64 and macOS x64/arm64 each get their own build, checksum verified"

# 2. a newer release replaces the installed program
make_release v9.8.2
LAB_OS=linux LAB_ARCH=x86_64 bash "$HERE/install-client.sh" --no-connect >/dev/null 2>&1 || fail "upgrade"
"$WORK/bin/lab" version | grep -q v9.8.2 || fail "upgrade did not replace the program"
pass "upgrade"

# 3. a corrupted download is refused and the installed program stays as it was
printf 'tampered' >> "$WORK/srv/dl/v9.8.2/lab-linux-x64"
if LAB_OS=linux LAB_ARCH=x86_64 bash "$HERE/install-client.sh" --no-connect >"$WORK/out3.txt" 2>&1; then fail "accepted a corrupted download"; fi
grep -q "checksum mismatch" "$WORK/out3.txt" || fail "no checksum message"
"$WORK/bin/lab" version | grep -q v9.8.2 || fail "the installed program changed"
pass "corrupted download refused, nothing changed"

# 4. unsupported CPUs and systems say so
if LAB_OS=linux LAB_ARCH=riscv64 bash "$HERE/install-client.sh" --no-connect >"$WORK/out4.txt" 2>&1; then fail "accepted riscv64"; fi
grep -q "unsupported CPU" "$WORK/out4.txt" || fail "no CPU message"
if LAB_OS=plan9 bash "$HERE/install-client.sh" --no-connect >"$WORK/out5.txt" 2>&1; then fail "accepted plan9"; fi
grep -q "install.ps1" "$WORK/out5.txt" || fail "does not point Windows users to install.ps1"
pass "unsupported systems get a clear message"

# 5. uninstall hands over to the program itself
LAB_OS=linux LAB_ARCH=x86_64 bash "$HERE/install-client.sh" --uninstall 2>&1 | grep -q "fake uninstall" || fail "uninstall did not run the program's own uninstall"
pass "uninstall"

echo "all client installer checks passed"
