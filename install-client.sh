#!/usr/bin/env bash
# proxmox-lab client installer for macOS and Linux (Intel/AMD and ARM). No sudo needed.
#
#   curl -fsSL https://raw.githubusercontent.com/madmaxvoltro/proxmox-lab/main/install-client.sh | bash
#
# It downloads the `lab` program for your system, verifies its checksum, puts it in ~/.local/bin and
# starts `lab connect` (SSH shortcuts, Claude Desktop/Code, approval popups).
# Options (after `bash -s --`):  --version vX.Y.Z   --no-connect   --uninstall
# Environment: LAB_HOST (skip the question), LAB_VERSION, LAB_BIN_DIR (default ~/.local/bin), LAB_REPO
# (tests: LAB_API_URL, LAB_DOWNLOAD_URL, LAB_OS, LAB_ARCH)
set -euo pipefail

REPO="${LAB_REPO:-madmaxvoltro/proxmox-lab}"
VERSION="${LAB_VERSION:-latest}"
BIN_DIR="${LAB_BIN_DIR:-$HOME/.local/bin}"
API_URL="${LAB_API_URL:-https://api.github.com/repos/${REPO}}"
DOWNLOAD_URL="${LAB_DOWNLOAD_URL:-https://github.com/${REPO}/releases/download}"
CONNECT=1
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
    --version)    VERSION="${2:?--version needs a value}"; shift 2 ;;
    --no-connect) CONNECT=0; shift ;;
    --uninstall)  UNINSTALL=1; shift ;;
    -h|--help)    sed -n '2,11p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

DEST="$BIN_DIR/lab"
printf '%s\n' "${B}proxmox-lab${N}  client for macOS and Linux"

# a terminal for questions, even when this script itself is piped into bash
ask_tty() { if [ -r /dev/tty ] && : </dev/tty 2>/dev/null; then "$@" </dev/tty; else "$@"; fi; }

if [ "$UNINSTALL" -eq 1 ]; then
  step "Uninstalling"
  if [ -x "$DEST" ]; then ask_tty "$DEST" uninstall || true; else note "nothing installed at $DEST"; fi
  exit 0
fi

step "Checking this computer"
case "${LAB_OS:-$(uname -s)}" in
  Linux|linux)  OS=linux ;;
  Darwin|macos) OS=macos ;;
  *) die "unsupported system: $(uname -s). On Windows use:  irm https://raw.githubusercontent.com/${REPO}/main/install.ps1 | iex" ;;
esac
MACHINE="${LAB_ARCH:-$(uname -m)}"
case "$MACHINE" in
  x86_64|amd64|x64)   ARCH=x64 ;;
  aarch64|arm64)      ARCH=arm64 ;;
  *) die "unsupported CPU: $MACHINE (builds exist for x86-64 and ARM64)" ;;
esac
# a Terminal running under Rosetta reports x86_64 on an Apple Silicon Mac: use the native build
if [ "$OS" = macos ] && [ -z "${LAB_ARCH:-}" ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = 1 ]; then ARCH=arm64; fi
ASSET="lab-${OS}-${ARCH}"
ok "$OS, $ARCH  (build: $ASSET)"
command -v curl >/dev/null 2>&1 || die "curl is required"
if command -v sha256sum >/dev/null 2>&1; then
  sha() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
  sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
  die "sha256sum or shasum is required to verify the download"
fi
command -v ssh >/dev/null 2>&1 || warn "the OpenSSH client (ssh) was not found; lab needs it to reach your Proxmox host"
command -v tailscale >/dev/null 2>&1 || warn "Tailscale was not found; install it from https://tailscale.com/download and sign in"

step "Finding the release"
if [ "$VERSION" = "latest" ]; then
  VERSION="$(curl -fsSL "${API_URL}/releases/latest" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)"
  [ -n "$VERSION" ] || die "could not read the latest release of ${REPO}"
fi
ok "$VERSION"

step "Downloading"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
base="${DOWNLOAD_URL}/${VERSION}"
curl -fsSL "${base}/${ASSET}" -o "$TMP/$ASSET" || die "download failed: ${base}/${ASSET} (is there a build for your system in this release?)"
curl -fsSL "${base}/SHA256SUMS" -o "$TMP/SHA256SUMS" || die "could not download SHA256SUMS"
expected="$(grep " ${ASSET}\$" "$TMP/SHA256SUMS" | cut -d' ' -f1 || true)"
[ -n "$expected" ] || die "no checksum for ${ASSET} in SHA256SUMS; not installing"
actual="$(sha "$TMP/$ASSET")"
[ "$expected" = "$actual" ] || die "checksum mismatch (expected $expected, got $actual); not installing"
ok "checksum verified"

step "Installing"
mkdir -p "$BIN_DIR"
cp "$TMP/$ASSET" "$DEST.new"
chmod 755 "$DEST.new"
if [ "$OS" = macos ]; then xattr -d com.apple.quarantine "$DEST.new" 2>/dev/null || true; fi
mv -f "$DEST.new" "$DEST"
ok "installed $VERSION to $DEST"
"$DEST" version || die "the installed program does not start"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. Add it with:"
     note "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.profile   # then open a new terminal" ;;
esac

if [ "$CONNECT" -eq 0 ]; then
  printf '\nInstalled. Next, run:  %slab connect%s\n' "$B" "$N"
  exit 0
fi
if [ -r /dev/tty ] && : </dev/tty 2>/dev/null; then
  printf '\nStarting the connect wizard...\n'
  if [ -n "${LAB_HOST:-}" ]; then exec "$DEST" connect "$LAB_HOST" </dev/tty; fi
  exec "$DEST" connect </dev/tty
fi
printf '\nInstalled. There is no terminal for questions here, so run:  %slab connect%s\n' "$B" "$N"
