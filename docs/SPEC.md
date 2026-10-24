# lab — Project Lab for Proxmox: Specification

Status: **draft v0.2** — decisions below come from the scoping Q&A; open items are in §15.

## 1. Goal

Spin up an isolated, ready-to-code Ubuntu environment in seconds, reach it from anywhere over Tailscale with no SSH keys to manage, and when a project goes cold push it to the **archive disk** (verified), freeing the **fast disk** and removing it from the tailnet. Restoring brings it back and re-joins the tailnet.

Non-goals (v1): LXC, multi-node clusters, a web UI, public exposure of anything, being a general VM manager (lab only manages what it created).

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **Host** | The Proxmox VE 9.2.x server (i5-13500, 64 GB RAM). |
| **Fast disk** | Proxmox storage ID (NVMe) where active project VMs live. Chosen at `lab init`. |
| **Archive disk** | Proxmox storage ID (large HDD) holding verified backups of archived projects. Chosen at `lab init`. Never called "the HDD" in the tool. |
| **Golden template** | Ubuntu 24.04 cloud-init template VM. Never authenticated, never developed in. |
| **Profile** | A TOML file describing extra software/config applied at first boot. |
| **Project** | One VM created by lab. Name = Proxmox name = hostname = tailnet name (`lab-<name>`). |
| **Client** | The thin `lab` on the Windows PC. It runs the host's `lab` over SSH on the tailnet. |

Storage IDs, bridge names, IPs and VMIDs are **never hard-coded**; `lab init` discovers them and writes them to config.

## 3. Architecture

```
Windows PC                         Proxmox host
┌────────────┐  ssh over tailnet  ┌──────────────────────────────┐
│ lab (thin) │ ─────────────────▶ │ lab (core, Python 3, root)   │
│ ssh config │                    │  ├─ qm / vzdump / pvesm / pvesh
│ writer     │ ◀── --json ─────── │  ├─ tailscale API (enrol/cleanup)
└────────────┘                    │  ├─ SQLite index + lock
                                  │  └─ systemd timer (idle watch)
                                  └───────────┬──────────────────┘
                                              │ clone / cloud-init
                       ┌──────────────────────▼───────────┐
                       │ Project VMs on fast disk (lab-*)  │──▶ tailnet (tag:project-lab, ssh)
                       └──────────────────────────────────┘
                       Verified backups ──▶ archive disk
```

- **Host core**: Python 3, stdlib-first, calls `qm`, `vzdump`, `pvesm`, `pvesh` via `subprocess` (no Proxmox API token needed). Every command supports `--json`.
- **Thin client**: same package, `client` mode. `lab <cmd>` → `ssh <host> lab <cmd>`; adds local-only behaviour (SSH config writing, `lab ssh`). A future HTTP/API layer can sit on `--json`; out of scope for v1.
- **State**: SQLite at `/var/lib/project-lab/lab.db` is an **index only**. Source of truth is Proxmox: each project VM carries the tag `lab` and a machine-readable block in its description, so the DB can be rebuilt (`lab doctor --rebuild-index`). Before any destructive action the core cross-checks real Proxmox state.
- **Locking**: one global flock for create/archive/restore/delete; per-project lock for the rest. VMIDs allocated from a configured range via `pvesh get /cluster/nextid` plus range check.

## 4. Config

`/etc/project-lab/config.toml` (written by `lab init`, editable):

```toml
[storage]
fast_disk    = "<discovered>"
archive_disk = "<chosen at init>"
[network]
bridge       = "<discovered>"
[vm]
vmid_range   = [1000, 1999]
template_id  = 9000
default_distro = "ubuntu-24.04"
default_cores  = 1
default_memory = 2048
default_disk   = "32G"
clone_mode   = "full"        # full | linked, overridable per project
[presets]                    # --size NAME
small  = { cpu = 1, ram = "2G",  disk = "32G" }
medium = { cpu = 4, ram = "8G",  disk = "48G" }
large  = { cpu = 8, ram = "16G", disk = "64G" }
[mcp]
max_cpu = 8
max_ram = "16G"
max_running_vms = 6          # creates beyond these need human approval
[archive]
keep         = 1
[tailscale]
tag          = "tag:project-lab"
auth_key_file = "/etc/project-lab/ts-authkey"   # 0600 root
[idle]
enabled = false
```

## 5. Setup: `lab init` and `lab doctor`

`lab init` is interactive, read-only until confirmed, and **never formats, partitions, or wipes**.

1. Version check (`pveversion`), root check.
2. Discover: `pvesm status`, `/etc/pve/storage.cfg`, `lsblk`, `qm list`, `ip -br addr`, bridges.
3. Pick **fast disk** (must support `images`) and **archive disk**:
   - list candidate Proxmox storages that support `backup`;
   - or, if the large disk is mounted but not a Proxmox storage, offer to add a *directory* storage pointing at a **new dedicated subfolder** (e.g. `<mount>/project-lab-archive`), showing the exact `pvesm add dir ...` command before running it;
   - warn if the candidate mount already contains other data (Jellyfin etc.) and that only the new subfolder is touched.
4. Pick bridge, VMID range (checked against `qm list`).
5. Tailscale: ask for the ephemeral, tagged, reusable auth key → stored 0600 root outside any template/backup. Verify tailscale tag ACL prerequisites (tagOwners, SSH rule) and print the snippet to add if missing.
6. Build the golden template (§6).
7. Write config; run `lab doctor`.

`lab doctor` checks: storages exist and have free space, archive disk reachable and writable, template present, DB/Proxmox drift, Tailscale key valid/expiring, orphaned VMs/backups/tailnet devices, leftover `lab-verify-*` temp VMs. `--fix` only repairs safe things and asks before anything destructive.

## 6. Golden template and profiles

- **Distros** (`--distro`, default `ubuntu-24.04`): `ubuntu-22.04`, `ubuntu-24.04`, `ubuntu-26.04`, `debian-12`, `debian-13`, `fedora-<current>`, `rocky-9`/`almalinux-9`. Official cloud images, pinned dated release, checksum verified. Defined in `distros.toml` (image URL, checksum URL, package manager family, default login user, template ID), so adding one is a data change.
- **Lazy templates**: the first `lab create --distro X` builds template `lab-tpl-X` (1–2 minutes) if missing, then reuses it. `lab template list|add|update|rm` manage them. Template IDs come from a reserved range, never hard-coded.
- Preinstalled in every template: openssh-server, qemu-guest-agent, tailscale (installed, **not** authenticated), git, curl, wget, ca-certificates, build tools (build-essential / "Development Tools"), python3/pip/venv, nodejs/npm. Package names are mapped per family (apt / dnf).
- Template disk is 32 GB; larger `--disk` is applied by `qm resize` plus cloud-init growpart at create. Shrinking is rejected. `lab resize NAME --disk/--cpu/--ram` handles live growth later.
- Template hardware: virtio, discard/ssd flags, serial console. Per-project defaults are 1 vCPU / 2 GB (see config).
- Generalised before templating (cloud-init clean, machine-id, SSH host keys, tailscale state removed).
- `lab template update`: builds a new dated template from a temp clone, tests it, swaps `template_id`; existing projects untouched (full clones) or flagged as dependent (linked clones).

**Profiles** (`/etc/project-lab/profiles/*.toml`, applied via cloud-init at first boot):

```toml
name = "python-api"
packages = ["postgresql-client"]
tools    = ["docker"]          # known modules: docker, go, rust, ...
run      = ["pip install --user pipx"]
cores = 4
memory = 4096
disk = "32G"
```

Modules are pinned in the tool (docker, go, rust, uv, …), not arbitrary downloads, unless the profile opts in.

## 7. Commands

| Command | Behaviour |
|---|---|
| `lab init` / `lab doctor` | §5 |
| `lab create NAME [--distro D] [--size small\|medium\|large] [--cpu N --ram 8G --disk 64G] [--profile P] [--full\|--linked] [--repo URL] [--from PATH] [--no-setup]` (defaults 1 vCPU / 2G / 32G, ubuntu-24.04) | Validate name, allocate VMID, clone, configure cloud-init, start, enrol on Tailscale, run auto-setup (§8), register in DB, write client SSH config, print `ssh` line. |
| `lab list [--json]` | Name, VMID, state (running/stopped/archived), tailnet IP/name, fast/archive, size, last backup, last active. |
| `lab status NAME` | Resources, disk use, uptime, tailnet status, archive info. |
| `lab ssh NAME` | Client: connects via tailnet name (Tailscale SSH). Falls back to host jump if needed. |
| `lab start / stop / restart NAME` | Graceful; Tailscale handled per §9. |
| `lab clone SRC DST` | Full copy of a project into a new identity. |
| `lab snap NAME [LABEL]`, `lab rollback NAME [LABEL]`, `lab snaps NAME` | Proxmox snapshots (fast disk only). |
| `lab archive NAME` | §10. |
| `lab restore NAME [--start]` | §11. |
| `lab delete NAME` | Typed-name confirmation; removes tailnet device, VM, and (with `--archives`) its archive. |
| `lab resize NAME [--cpu --ram --disk]` | Grow resources (disk grow only). |
| `lab exec NAME -- CMD`, `lab put/get NAME SRC DST` | Run a command / copy files in a project VM (also the basis of the MCP tools). |
| `lab template list\|add\|update\|rm` | §6 |
| `lab protect NAME` / `lab unprotect NAME` | Mark a project protected (§16). |
| `lab mcp` | Start the stdio MCP server (§16). |
| `lab idle check` | Run by timer; stops idle projects (§12). |
| `lab config`, `lab profiles` | Show config / list profiles. |

Exit codes are stable (0 ok, 2 usage, 3 precondition failed, 4 verification failed, 5 partial/needs attention).

## 8. Auto-setup on create

If a project source is provided (`--repo URL`, `--from PATH`) or already present in the VM, lab detects, in order, and runs inside the VM after first boot:

1. `setup.sh` → executed as the project user (shown, logged to `~/.lab/setup.log`).
2. `Dockerfile` / `compose.yaml` → ensure Docker module present, `docker compose up -d` or `docker build`.
3. Nothing found → skip.

Always overridable with `--no-setup`. Runs only inside the VM (isolation is the point). Also on first boot: git identity, GitHub deploy key or `gh auth` (per config), dotfiles repo applied, profile applied.

## 9. Tailscale lifecycle

Decisions: ephemeral, tagged (`tag:project-lab`), reusable pre-approved auth key delivered at runtime through the QEMU guest agent (`qm guest exec --pass-stdin`), not cloud-init (decision Q1); join with `tailscale up --ssh --hostname lab-NAME`. No SSH keys to manage; access governed by tailnet ACL `ssh` rules.

| Event | Action |
|---|---|
| create | enrol, record tailnet IP and MagicDNS name |
| stop | see note below |
| start | ensure enrolled; re-enrol if the node was removed; update recorded IP |
| archive | `tailscale logout` **before** shutdown/backup so the backup holds no live identity; confirm device gone |
| restore | boot with clean state, enrol as a fresh device, new IP possible |
| delete | logout, confirm removal, then destroy VM |

**Important caveat** — ephemeral nodes are removed by Tailscale after being offline ~30–60 min, so a stopped VM loses its tailnet identity anyway. Plan: treat `stop` as `logout` too (clean and deterministic), and re-enrol on `start`. Hostname collisions (`lab-x-1`) are avoided by logging out cleanly first. Consequence: the tailnet IP may change across stop/start; always use the MagicDNS name.

**Key handling**: the key lives only in `/etc/project-lab/ts-authkey` (0600 root) on the host. Enrolment is: wait for guest agent → `qm guest exec <vmid> --pass-stdin -- tailscale up --auth-key=file:/dev/stdin --ssh --hostname lab-NAME`, with the key piped on stdin. It never goes in argv, cloud-init user-data, the seed image, the template, the VM filesystem, logs or backups. Cloud-init still handles hostname, user, resize, profile packages and setup. The same path is used for create, start and restore. Requires the guest agent in every template (it is).

## 10. Archive (one command, verified, auto-rollback)

`lab archive NAME` — the whole thing, no second step.

1. Preconditions: project exists in Proxmox, archive disk has free space ≥ VM used size × 1.2, no snapshot-dependent linked clones from it.
2. `tailscale logout` in guest (via guest agent); confirm device gone.
3. Graceful shutdown (timeout, then fail — no force-kill without confirmation).
4. `vzdump --mode stop --compress zstd --storage <archive_disk>` (to a staging name).
5. **Verify** (all must pass):
   1. vzdump exit/status OK, size sanity;
   2. `zstd -t` integrity of the archive file;
   3. **trial restore** to temp VMID `lab-verify-NAME` on fast disk, network **disconnected** (`link_down`);
   4. **boot test**: start it, guest agent responds, root filesystem mounts, `cloud-init status` sane; then shut down and destroy the temp VM.
6. On success: record archive (path, size, sha256, timestamps), mark `archived`, **destroy the fast-disk VM**, prune older archives per `keep` (new one verified first).
7. On **any verification failure**: delete the bad archive from the archive disk, destroy the temp verify VM, **leave the original VM untouched on the fast disk**, start it again and re-enrol on Tailscale if it had been running, print a loud warning with the reason, exit code 4.
8. Interrupted run (power loss, ^C): journal in DB; `lab doctor` finds and resumes/rolls back; the original VM is never destroyed before step 6.

Flags: `--keep-active` (verify only, don't remove), `--no-boot-test` (discouraged), `--yes`.

## 11. Restore

`lab restore NAME`: locate the project's archive, allocate a fresh VMID, `qmrestore` to fast disk, re-apply cloud-init (hostname, user, fresh tailnet enrolment), start (unless `--no-start`), wait for Tailscale, update DB and client SSH config, report new tailnet IP. Archive is kept (not deleted) until `lab delete --archives` or re-archive supersedes it.

## 12. Idle auto-stop and limits

- systemd timer on host runs `lab idle check` (default hourly, disabled by default).
- A project is idle when: no SSH/Tailscale sessions, CPU below threshold, no non-loopback traffic, for N days (default 7); protected projects can opt out (`lab pin NAME`).
- Idle action: **stop** (never archive automatically). Notification via log and optional command hook.
- Limits: warn on create if total allocated RAM of running lab VMs would exceed a configurable share of host RAM (64 GB host), and if fast disk free space is below a threshold.

## 13. Client conveniences (Windows)

- On `create`/`restore`/`start`, write/refresh a managed block in `~/.ssh/config` (`Host lab-NAME` → tailnet MagicDNS name), so VS Code Remote-SSH works with no extra setup; remove it on archive/delete.
- `lab ssh NAME` is a shortcut over that config.
- Client config: host address (tailnet name of the Proxmox host), SSH user.

## 14. Safety invariants

1. Never format, partition or wipe a disk. Never modify a storage definition without showing the command and getting confirmation.
2. Never delete the only copy: the fast-disk VM is destroyed only after a verified archive exists.
3. Destructive commands require typed confirmation of the project name (unless `--yes`).
4. lab only touches VMs tagged `lab` within its VMID range; it ignores everything else on the host (Jellyfin etc.).
5. The Tailscale key never appears in argv, shell history, templates, repos or archives.
6. Template is immutable; updates go through `lab template update`.
7. All operations are journaled and idempotent where possible; logs in `/var/log/project-lab/`.

## 15. Open questions

- ~~Q1 Key delivery~~ — **decided**: guest-agent stdin (§9).
- **Q2** Tailscale ACL: do you already have `tagOwners` for `tag:project-lab` and an `ssh` rule allowing your user → that tag?
- **Q3** Default VM login user name (`ubuntu`, or your name) and where your SSH/Tailscale SSH identity comes from.
- **Q4** Dotfiles repo URL and GitHub auth method (deploy key per project vs. `gh auth` token).
- **Q5** Idle thresholds (days, CPU %).
- **Q6** Whether the thin client should also support macOS/Linux (spec assumes Windows only).
- **Q7** MCP "enter another project" approval: delivered how? (MCP elicitation prompt if the client supports it, else a pending-approval queue answered with `lab approve ID` from a terminal.) Proposed: both, elicitation first.
- **Q8** How the AI executes in VMs: guest agent (works before Tailscale is up, no network needed) vs. Tailscale SSH (real shell/PTY, streaming). Proposed: guest agent for one-shot commands and file transfer, SSH for interactive/long-running.
- **Q9** Per-family package maps for profiles (apt vs dnf) — how far to go in v1 (proposed: only the base packages and the docker/go/rust/uv modules).

## 16. MCP server (`lab mcp`)

**Transport**: stdio, launched by the MCP client (Claude Desktop / Claude Code) on the Windows PC. It uses the thin client's SSH-over-tailnet transport to the host; nothing new listens anywhere. All host calls use `lab ... --json`.

**Trust model**: the AI gets a full shell **inside project VMs** (they are the sandbox) and lifecycle control, but **never a shell on the Proxmox host**. There is no host-exec, no raw `qm`, no arbitrary file access outside a VM. The MCP layer calls the same code paths as the CLI; it adds no extra Proxmox privileges.

**Tools**

| Tool | Notes |
|---|---|
| `lab_list`, `lab_status` | read-only; protected projects appear as `protected` with no detail |
| `lab_create` | distro, size/cpu/ram/disk, profile, repo; returns tailnet name |
| `lab_start`, `lab_stop`, `lab_restart`, `lab_resize` | |
| `lab_snapshot`, `lab_rollback`, `lab_clone` | |
| `lab_archive`, `lab_restore`, `lab_delete` | approval required (below) |
| `lab_exec` | run a command in a VM as the project user; stdout/stderr/exit code; timeout; optional background job id + `lab_exec_poll` / `lab_exec_cancel` |
| `lab_exec` with `sudo`/root | approval required |
| `lab_read_file`, `lab_write_file`, `lab_upload`, `lab_download` | limits on size |
| `lab_logs` | host-side operation log and VM setup log |
| `lab_approvals` | list/inspect pending approvals |

**Approvals** — a tool call that needs one returns/prompts for confirmation with a plain-language plan (what will happen, what is destroyed) and runs only after a human approves. Required for:
1. `delete` (VM and archives)
2. `archive` and `restore`
3. any `sudo`/root command in a VM
4. `create` beyond the caps in `[mcp]` (cpu, ram, running VM count)
5. **entering another project**: a session is bound to the project(s) it created or was explicitly attached to (`lab_attach`); first exec/read/write in any other project asks for permission. Approval is per project per session.

**Protected projects** (`lab protect`): invisible to destructive tools and exec; any access needs explicit human approval regardless of session binding.

Approval delivery: MCP elicitation where the client supports it, otherwise the call fails with an approval ID and the human runs `lab approve ID` in a terminal (Q7). Approvals expire (default 10 min), are single-use, and are bound to the exact action and arguments.

**Audit**: every MCP call (tool, args with secrets redacted, project, result, approver) is appended to `/var/log/project-lab/mcp-audit.log` on the host. Output returned to the model is size-capped; the Tailscale key and config secrets are never exposed by any tool.
