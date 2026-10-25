# Changelog

## 0.4.1

- Fixed: on Windows, right after `lab connect` the background approval helper made a terminal window pop up every few
  seconds (it was started with no console, so each of its ssh checks opened one). It now has a hidden console. After a
  restart it was already fine.

## 0.4.0

- New setting `[mcp] root_without_approval = true` (default `false`): the AI can run commands and read/write files as root
  inside a project VM without a popup. Delete, archive, restore, other people's projects and protected projects still ask.
  Each root use is logged and appears in `lab approvals` as granted automatically.

## 0.3.0

- New look: the installers and the wizards open with a `proxmox-lab` title box and show a tree of steps.
- `lab init`, `lab setup` and `lab connect` are driven with the keyboard on a real terminal: arrow keys or Tab to
  move, the selected choice is underlined, Enter to pick; Yes/No questions take left/right, Tab, `y` or `n`.
  Piped or scripted runs keep the plain numbered questions.

## 0.2.0

- The client now exists for **macOS and Linux** (x64 and ARM64) as well as Windows, with `install-client.sh`.
  Approval popups use the native dialog on macOS and zenity/kdialog on Linux; the blurred card stays Windows-only.
  The login launcher is a LaunchAgent on macOS and an XDG autostart entry on Linux.

## 0.1.1 (first release)

The `v0.1.0` tag was never released: its Windows build failed a smoke test, which is fixed here.
Also new: `lab check` (is this PC ready?), and `lab exec ... -- COMMAND` now works on Python 3.11.

- Host: guided `lab setup`, `curl | bash` installer, `lab doctor`, base-image builder (Ubuntu 24.04 tested).
- Projects: create, start, stop, restart, delete, protect; per-user quotas; DHCP or lab-assigned static addresses; VLAN tags.
- Verified archives: backup, integrity check, trial restore with the network unplugged, boot test, then (only then) removal
  of the original; automatic rollback on any failure; crash recovery from a journal (`lab recover`).
- MCP server for Claude with approvals, session binding, protected projects and an audit log.
- Windows client: `lab.exe` (x64 and ARM64), `lab connect`, SSH shortcuts, Claude Desktop/Code setup.
- Approval popup with blurred background, More info dropdown and Return-to-approve.
- Live progress display with real percentages.
