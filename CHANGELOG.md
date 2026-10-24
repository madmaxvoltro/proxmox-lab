# Changelog

## 0.1.0 (first release)

- Host: guided `lab setup`, `curl | bash` installer, `lab doctor`, base-image builder (Ubuntu 24.04 tested).
- Projects: create, start, stop, restart, delete, protect; per-user quotas; DHCP or lab-assigned static addresses; VLAN tags.
- Verified archives: backup, integrity check, trial restore with the network unplugged, boot test, then (only then) removal
  of the original; automatic rollback on any failure; crash recovery from a journal (`lab recover`).
- MCP server for Claude with approvals, session binding, protected projects and an audit log.
- Windows client: `lab.exe` (x64 and ARM64), `lab connect`, SSH shortcuts, Claude Desktop/Code setup.
- Approval popup with blurred background, More info dropdown and Return-to-approve.
- Live progress display with real percentages.
