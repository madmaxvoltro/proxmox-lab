#!/usr/bin/env bash
# proxmox-lab installer for the Proxmox host. Run it as root on the host:
#
#   curl -fsSL https://raw.githubusercontent.com/madmaxvoltro/proxmox-lab/main/install.sh | bash
#
# Options (put them after `bash -s --`):
#   --version vX.Y.Z   install a specific release (default: the latest)
#   --no-setup         install only; do not start the setup wizard
#   --uninstall        remove the program (your VMs, config and data are not touched)
# Environment: LAB_REPO=owner/name, LAB_VERSION=vX.Y.Z|main
# (packagers/tests: LAB_PREFIX, LAB_BIN, LAB_API_URL, LAB_DOWNLOAD_URL, LAB_NO_ROOT_CHECK=1)
set -euo pipefail

REPO="${LAB_REPO:-madmaxvoltro/proxmox-lab}"
VERSION="${LAB_VERSION:-latest}"
PREFIX="${LAB_PREFIX:-/opt/project-lab}"
BIN="${LAB_BIN:-/usr/local/bin/lab}"
API_URL="${LAB_API_URL:-https://api.github.com/repos/${REPO}}"
DOWNLOAD_URL="${LAB_DOWNLOAD_URL:-https://github.com/${REPO}/releases/download}"
MARKER="# managed by the proxmox-lab installer"
RUN_SETUP=1
UNINSTALL=0

if [ -t 1 ]; then
  B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; D=$'\033[2m'; N=$'\033[0m'
else
  B=''; G=''; R=''; Y=''; D=''; N=''
fi
step() { printf '\n%s%s%s\n' "$B" "$*" "$N"; }
ok()   { printf '  %s✔%s %s\n' "$G" "$N" "$*"; }
note() { printf '  %s%s%s\n' "$D" "$*" "$N"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%s✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --version)  VERSION="${2:?--version needs a value}"; shift 2 ;;
    --no-setup) RUN_SETUP=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)  sed -n '2,12p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

printf '%s\n' "${B}proxmox-lab${N}  disposable project VMs on Proxmox, safe for AI"

[ "$(id -u)" -eq 0 ] || [ -n "${LAB_NO_ROOT_CHECK:-}" ] || die "please run as root (for example: curl ... | sudo bash)"

if [ "$UNINSTALL" -eq 1 ]; then
  step "Uninstalling"
  if [ -f "$BIN" ] && grep -q "$MARKER" "$BIN"; then rm -f "$BIN"; ok "removed $BIN"; fi
  if [ -d "$PREFIX" ]; then rm -rf "$PREFIX"; ok "removed $PREFIX"; fi
  note "left in place on purpose: /etc/project-lab (config), /var/lib/project-lab (database, images),"
  note "/var/log/project-lab, your VMs and archives. Delete them by hand if you really want them gone."
  exit 0
fi

step "Checking this machine"
{ command -v pveversion >/dev/null 2>&1 || [ -n "${LAB_NO_ROOT_CHECK:-}" ]; } || die "this is not a Proxmox host (no pveversion). Run this on the Proxmox server; the Windows side has its own installer."
if command -v pveversion >/dev/null 2>&1; then ok "Proxmox VE $(pveversion | sed -n 's|^pve-manager/\([0-9.]*\).*|\1|p')"; fi
for tool in curl tar sha256sum python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required but not installed (apt install $tool)"
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11 or newer is required (found $(python3 -V 2>&1)); Proxmox VE 9 ships 3.13"
ok "$(python3 -V 2>&1)"

step "Finding the release"
if [ "$VERSION" = "latest" ]; then
  VERSION="$(curl -fsSL "${API_URL}/releases/latest" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')" \
    || die "could not read the latest release of ${REPO} (no release yet? try LAB_VERSION=main)"
fi
ok "$VERSION"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

step "Downloading"
if [ "$VERSION" = "main" ]; then
  warn "installing the development branch: no checksum is published for it"
  curl -fsSL "https://codeload.github.com/${REPO}/tar.gz/refs/heads/main" -o "$TMP/lab.tar.gz" || die "download failed"
else
  base="${DOWNLOAD_URL}/${VERSION}"
  asset="project-lab-${VERSION}.tar.gz"
  curl -fsSL "${base}/${asset}" -o "$TMP/lab.tar.gz" || die "download failed: ${base}/${asset}"
  curl -fsSL "${base}/SHA256SUMS" -o "$TMP/SHA256SUMS" || die "could not download SHA256SUMS"
  expected="$(grep " ${asset}\$" "$TMP/SHA256SUMS" | cut -d' ' -f1 || true)"
  [ -n "$expected" ] || die "no checksum for ${asset} in SHA256SUMS"
  actual="$(sha256sum "$TMP/lab.tar.gz" | cut -d' ' -f1)"
  [ "$expected" = "$actual" ] || die "checksum mismatch (expected $expected, got $actual); not installing"
  ok "checksum verified"
fi

step "Installing"
DEST="$PREFIX/releases/$VERSION"
rm -rf "$DEST"
mkdir -p "$DEST"
tar -xzf "$TMP/lab.tar.gz" -C "$DEST" --strip-components=1
[ -f "$DEST/src/lab/entry.py" ] || die "the download does not look like proxmox-lab (src/lab/entry.py missing)"
ln -sfn "$DEST" "$PREFIX/current"
if [ -e "$BIN" ] && ! grep -q "$MARKER" "$BIN"; then
  die "$BIN already exists and was not created by this installer; move it away and run again"
fi
cat > "$BIN" <<EOF
#!/bin/sh
$MARKER
exec env PYTHONPATH=$PREFIX/current/src python3 -m lab.entry "\$@"
EOF
chmod 755 "$BIN"
# keep the three newest releases for quick rollback
# shellcheck disable=SC2012
ls -1t "$PREFIX/releases" | tail -n +4 | while read -r old; do rm -rf "${PREFIX:?}/releases/${old}"; done
ok "installed $VERSION to $PREFIX (command: lab)"
"$BIN" version >/dev/null || die "the installed lab does not start; see the error above"

if [ -f /etc/project-lab/config.toml ]; then
  step "Updating an existing setup"
  ok "your configuration was kept"
  "$BIN" doctor || warn "some checks need attention (see above)"
  exit 0
fi

if [ "$RUN_SETUP" -eq 0 ]; then
  printf '\nInstalled. Next, run:  %slab setup%s\n' "$B" "$N"
  exit 0
fi

if [ -r /dev/tty ] && : </dev/tty 2>/dev/null; then
  printf '\nStarting the setup wizard...\n'
  exec "$BIN" setup </dev/tty
fi
printf '\nInstalled. There is no terminal for questions here, so run:  %slab setup%s\n' "$B" "$N"
