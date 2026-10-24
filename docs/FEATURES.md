# lab — Feature sheet

Legend: **P0** must for v1 · **P1** v1 if time allows · **P2** later · ⚠ has an open question in SPEC §15

## Core lifecycle
| ID | Feature | Pri |
|---|---|---|
| F01 | `lab create` from golden template (full or linked clone, per project) | P0 |
| F02 | Auto VMID allocation in reserved range, locked | P0 |
| F03 | Per-project cores / RAM / disk overrides | P0 |
| F04 | `list` / `status` with `--json` | P0 |
| F05 | `start` / `stop` / `restart` graceful | P0 |
| F06 | `delete` with typed confirmation, removes tailnet device | P0 |
| F07 | `clone` project → project | P1 |
| F08 | `snap` / `rollback` / `snaps` | P1 |
| F09 | Defaults 1 vCPU / 2 GB / 32 GB; `--cpu --ram --disk` and `--size small\|medium\|large` presets | P0 |
| F10 | `--distro` (ubuntu 22.04/24.04/26.04, debian 12/13, fedora, rocky/alma), data-driven `distros.toml` | P0 |
| F11 | Lazy per-distro template build on first use; `template list\|add\|update\|rm` | P0 |
| F12 | Disk grow at create and `lab resize` (grow only) | P1 |
| F13 | `lab exec` / `put` / `get` into project VMs | P0 |

## MCP server
| ID | Feature | Pri |
|---|---|---|
| M01 | `lab mcp` stdio server over the client's SSH transport, no listening port | P0 |
| M02 | Lifecycle tools (list/status/create/start/stop/resize/snap/clone) | P0 |
| M03 | `lab_exec` in VMs as project user, timeouts, background jobs + poll/cancel | P0 |
| M04 | File tools: read/write/upload/download with size limits | P0 |
| M05 | Approval gates: delete, archive/restore, sudo/root, create over caps | P0 |
| M06 | Project-bound sessions; entering another project needs approval (`lab_attach`) ⚠Q7 | P0 |
| M07 | `protect` / `unprotect`; protected projects hidden from destructive/exec tools | P0 |
| M08 | Audit log of every MCP call, secrets redacted | P0 |
| M09 | No host shell, no raw `qm`; Tailscale key never exposed | P0 |
| M10 | Guest-agent vs SSH execution split ⚠Q8 | P1 |
| M11 | Streamable HTTP transport for remote clients | P2 |

## Archive & restore
| ID | Feature | Pri |
|---|---|---|
| A01 | One-command archive to archive disk | P0 |
| A02 | Verify: vzdump OK + zstd -t + sha256 | P0 |
| A03 | Verify: trial restore (network disconnected) + boot test | P0 |
| A04 | Auto-rollback on failed verify: delete bad archive, keep VM, warn | P0 |
| A05 | Remove fast-disk VM only after verified archive | P0 |
| A06 | Keep latest 1; supersede only after new one verifies | P0 |
| A07 | `restore` to fast disk with fresh identity | P0 |
| A08 | Journaled/resumable archive after interruption | P1 |
| A09 | Archive free-space precheck | P0 |

## Tailscale
| ID | Feature | Pri |
|---|---|---|
| T01 | Ephemeral tagged auth key piped via guest-agent stdin, `tailscale up --ssh --hostname lab-NAME` | P0 |
| T02 | Record tailnet IP + MagicDNS name | P0 |
| T03 | Logout before archive/delete/stop; re-enrol on start/restore | P0 |
| T04 | ACL/tag prerequisite check with fix-it snippet ⚠Q2 | P1 |
| T05 | Key expiry warning in `doctor` | P1 |

## Setup & health
| ID | Feature | Pri |
|---|---|---|
| S01 | `lab init` discovery: storages, disks, bridge, free VMIDs (read-only) | P0 |
| S02 | Choose **fast disk** and **archive disk** interactively | P0 |
| S03 | Add dir storage on a new dedicated subfolder if needed (shows command, confirms) | P0 |
| S04 | Build golden template (pinned image, checksum, generalise) | P0 |
| S05 | `lab doctor` drift/orphan/space checks | P0 |
| S06 | `doctor --rebuild-index` from Proxmox tags/description | P1 |
| S07 | `lab template update` (dated, tested swap) | P1 |

## Developer experience
| ID | Feature | Pri |
|---|---|---|
| D01 | Profiles (TOML) applied at first boot | P0 |
| D02 | Auto-run `setup.sh` / Dockerfile / compose from repo or folder | P1 |
| D03 | `--repo URL` clone on first boot | P1 |
| D04 | Git identity + deploy key / `gh auth` ⚠Q4 | P1 |
| D05 | Dotfiles repo on boot ⚠Q4 | P1 |
| D06 | Docker / Go / Rust / uv modules in profiles | P1 |
| D07 | Windows client writes `~/.ssh/config` managed block (VS Code Remote) | P0 |
| D08 | `lab ssh NAME` | P0 |

## Resource management
| ID | Feature | Pri |
|---|---|---|
| R01 | Idle auto-stop timer, `pin` to exempt ⚠Q5 | P1 |
| R02 | RAM overcommit / fast-disk free-space warnings | P1 |
| R03 | Notification hook (command/webhook) | P2 |

## Later / out of scope for v1
LXC backend (`--type lxc`), web dashboard / HTTP API, Arch/Alpine distros, multi-node, template per profile, scheduled archive suggestions ("untouched 30 days"), off-box copy of archives (a single archive disk is not disaster recovery).

## Milestones
1. **M0 Skeleton** — repo, config, DB, Proxmox wrapper with dry-run mode, test harness with fake `qm`.
2. **M1 Setup** — init/doctor, golden template.
3. **M2 Lifecycle** — create/list/start/stop/delete + Tailscale enrol.
4. **M3 Archive/restore** — full verified flow with failure injection tests.
5. **M4 Client** — thin client, SSH config, `lab ssh`.
6. **M5 DX** — profiles, auto-setup, dotfiles, snapshots, clone, multi-distro.
7. **M6 MCP** — read-only tools first, then exec/files, then approvals and audit.
8. **M7 Idle + hardening.**
