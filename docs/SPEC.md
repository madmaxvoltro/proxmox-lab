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
snippet_disk = "<storage with 'snippets' content, for cloud-init user-data>"
[network]
bridge       = "<discovered>"
vlan         = 0             # VLAN tag on a VLAN-aware bridge (0 = untagged). `lab init` suggests it from a host interface like vmbr1.60
mode         = "dhcp"        # dhcp | static. `lab init` probes the bridge for a DHCP server and picks the default
# static mode only (the network has no DHCP; lab hands out addresses itself):
subnet       = "192.168.10.0/24"
gateway      = "192.168.10.1"
dns          = "192.168.10.1 1.1.1.1"
pool_start   = "192.168.10.200"    # lab only ever uses addresses in [pool_start, pool_end]
pool_end     = "192.168.10.220"
[vm]
vmid_range   = [1000, 1999]
template_range = [9000, 9099]   # one golden template per distro; must not overlap vmid_range
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
base_tag     = "tag:server"
owner_tag_prefix = "tag:user-"
oauth_file = "/etc/project-lab/ts-oauth"   # 0600 root; OAuth client mints per-VM keys (Q10)
[idle]
enabled = false
```

### 4.1 Who decides storage (host-authoritative)

The host config above is the **only** source for `fast_disk`, `archive_disk`, `snippet_disk`, the bridge, VMID/template ranges, paths and Tailscale settings. Nothing that travels with a project can set or override them: not a `lab.toml`/`setup.sh` in the project's repo (`--repo`, `--from`), not a profile, not client-side config, not CLI flags from the client, not MCP tool arguments. A project-level file may only request: `distro`, `cpu`, `ram`, `disk`, `clone_mode`, `profile`. Any other key (including anything named like a storage, bridge or path) is rejected with an error, not ignored. The host config is also root-owned and not writable by lab users; only `lab init` (admin, on the host) changes it. Rationale: users and AI sessions must never be able to steer VMs or archives onto a disk the admin did not choose.

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

`lab doctor` checks: storages exist and have free space, archive disk reachable and writable, template present, DB/Proxmox drift, Tailscale key valid/expiring, orphaned VMs/backups/tailnet devices, leftover `lab-verify-*` temp VMs. v1 `doctor` is strictly read-only and exits 5 if any check fails; `--fix` (safe repairs, asks before anything destructive) comes later.

Template build (implemented): verified image -> VM with a build-time cloud-init user-data snippet on the snippet storage -> wait for the guest agent -> run the toolchain check inside the guest -> generalise (cloud-init clean, host keys, machine-id, tailscale state) -> graceful shutdown -> remove the snippet drive -> `qm template`. A failed build destroys only the build VM it created.

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

### 7.1 Lifecycle behaviour (implemented in M2)

- **create**: validates the name, then under the global lock: name free (one generic "unavailable" error whether taken by you, someone else or a foreign VM), quota check, template lookup (built lazily if missing), VMID from the range, journal entry, DB row (`creating`), clone, per-VM cloud-init user-data snippet (hostname, the owner as sole login user with passwordless sudo and a locked password, no keys), CPU/RAM/tags/description, optional disk grow, boot, wait for guest agent, wait for first-boot, Tailscale enrolment, then `running`. **Any failure rolls back** the VM it created (only if it still carries the name lab gave it), the snippet and the DB row, and records `rolled_back`.
- **stop**: logs the guest out of Tailscale, graceful shutdown, and if the guest has not stopped within 120 s it stops *without* forcing and tells you to retry with `--force`. Clears the recorded tailnet IP, keeps the name.
- **start**: quota check, boot, wait for agent, enrol with a freshly minted key (the IP may differ). Idempotent: a running, enrolled project is left alone; a running project that lost its enrolment is re-enrolled.
- **delete**: typed project name (or `--yes`), refuses protected and archived projects, re-verifies the VM (name `lab-<name>`, tag `lab`, inside the VMID range) before destroying, same shutdown rule as stop, removes the snippet and index row.
- **list/status**: non-admins see only their own projects; `--all` is admin only. Someone else's project is reported as "no such project". `list` merges live Proxmox state (`missing` if the VM has vanished).
- **identity**: over SSH, the tailnet login must map to a lab user. On the host console as root with no SSH session, the caller is the first admin. `--as NAME` lets an admin act with that user's (lesser) permissions.
- **quotas**: vCPU, RAM and VM count are counted for running projects; disk for all non-archived projects. Defaults in `[quota]`, per-user overrides in the user record, admins exempt.
- **users**: `lab user add|list|remove` (admin); cannot remove a user who owns projects or the last admin.

### 7.2 Archive and restore (implemented in M3)

- **archive**: under the global lock and a journal entry: space check on the archive disk (disk size x1.2), graceful stop (never forced), `vzdump` to the archive disk, then verify: backup listed with a plausible size, `zstd -t`, SHA-256 recorded, **trial restore** to a temporary `lab-verify-<name>` VM with `link_down` on its NIC, **boot test** (agent up, root mounted, owner exists, hostname correct), temp VM destroyed. Only then the **commit point**: archive recorded as verified, and only then the original VM is destroyed. Old archives beyond `archive.keep` are pruned only after the new one verified.
- **failure before commit**: the new backup and temp VM are deleted, the original is restarted exactly as it was (re-enrolled in Tailscale if it was running), the project state is restored, and the error says the archive FAILED, the backup was deleted and the project is untouched. Ctrl-C and crashes get the same treatment.
- **recover** (`lab recover NAME`): resumes from the journal. Before the commit point it rolls back; after it, it rolls forward (and refuses to destroy the VM if the verified backup has vanished).
- **restore**: needs a verified archive present on the archive disk, passes the quota check, restores to the fast disk under a new VMID, regenerates the per-VM cloud-init snippet, drops the old Tailscale identity inside the guest and enrols fresh. A failed restore is rolled back and the archive stays. The archive is kept after restore.
- **keep-active**: `--keep-active` verifies and records an archive but keeps the VM.
- **delete** refuses a project that has archives unless `--archives` is passed; works on archived projects.

### 7.3 Addressing (added after the first host trial)

The first host's bridge had no DHCP server, so the template build VM never got a network. Addressing is now explicit. `lab init` sends a DHCPDISCOVER on the chosen bridge (asks for an offer, never takes a lease; run as root) and defaults to `dhcp` if a server answers, `static` if none does, or the current setting if it cannot probe. In `static` mode it suggests subnet and gateway from the host's own address and default route on that bridge, then asks for DNS and the pool. `lab` assigns each project the first pool address that is not recorded for another project and does not answer a ping, stores it (`projects.static_ip`, unique), and sets `ipconfig0=ip=ADDR/PREFIX,gw=GW` plus `nameserver`. The address stays reserved while the project is archived, is reused on restore, and is freed by delete or a rolled-back create. The template build VM takes a temporary pool address and the finished template keeps no address or user-data. `doctor` warns when a bridge in `dhcp` mode gets no answer and shows static pool usage (warn at 90%, fail when full). All of this is host config only (§4.1).

### 7.4 Progress display

Slow commands (`template add`, `create`, `start`, `stop`, `archive`, `restore`) report through a reporter (`progress.py`), never by printing. On an interactive terminal the display is docker-build style: finished steps collapse to `✔ [3/6] Backing up api (1:12)`, and the running step shows a header with a bar, percentage and elapsed time plus a window of its last four detail lines, redrawn in place. Percentages come from the real sources: image download (Content-Length), hashing, and the `%` lines that `qm clone`, `qm set ... import-from`, `vzdump` and `qmrestore` print (the runner streams their output live when asked). Waits (guest agent, cloud-init) show elapsed time against the timeout, not a percentage, and while cloud-init runs the last line of the guest's `cloud-init-output.log` is shown, so a long package install is visible. When output is not a terminal (pipes, logs, `LAB_PLAIN=1`) the same events become plain lines, percentages only per 10%. LVM chatter (`Logical volume ... created`) is dropped and `WARNING` lines are collected and printed once as `note:` lines at the end. From the Windows client, `lab` allocates an SSH terminal (`ssh -t`) whenever your own stdin and stdout are terminals and the command is not `--json`, `put`, `get`, `exec` or `mcp`, so the host draws the same live display. It all goes to stderr, so `--json` output and the MCP protocol stay clean. On failure the failing step is marked `✘` and its last output stays on screen. Set `NO_COLOR` to drop colours.

## 8. Auto-setup on create

If a project source is provided (`--repo URL`, `--from PATH`) or already present in the VM, lab detects, in order, and runs inside the VM after first boot:

1. `setup.sh` → executed as the project user (shown, logged to `~/.lab/setup.log`).
2. `Dockerfile` / `compose.yaml` → ensure Docker module present, `docker compose up -d` or `docker build`.
3. Nothing found → skip.

Always overridable with `--no-setup`. Runs only inside the VM (isolation is the point). Also on first boot: git identity, GitHub deploy key or `gh auth` (per config), dotfiles repo applied, profile applied.

## 9. Tailscale lifecycle

Decisions: ephemeral, tagged (`tag:server` + owner tag `tag:user-<owner>`), pre-approved auth key (shared reusable, or minted per VM — see Q10) delivered at runtime through the QEMU guest agent (`qm guest exec --pass-stdin`), not cloud-init (decision Q1); join with `tailscale up --ssh --hostname lab-NAME`. No SSH keys to manage; access governed by tailnet ACL `ssh` rules.

| Event | Action |
|---|---|
| create | enrol, record tailnet IP and MagicDNS name |
| stop | see note below |
| start | ensure enrolled; re-enrol if the node was removed; update recorded IP |
| archive | `tailscale logout` **before** shutdown/backup so the backup holds no live identity; confirm device gone |
| restore | boot with clean state, enrol as a fresh device, new IP possible |
| delete | logout, confirm removal, then destroy VM |

**Important caveat** — ephemeral nodes are removed by Tailscale after being offline ~30–60 min, so a stopped VM loses its tailnet identity anyway. Plan: treat `stop` as `logout` too (clean and deterministic), and re-enrol on `start`. Hostname collisions (`lab-x-1`) are avoided by logging out cleanly first. Consequence: the tailnet IP may change across stop/start; always use the MagicDNS name.

**Key handling**: the OAuth client credentials live only in `/etc/project-lab/ts-oauth` (0600 root) on the host; a fresh single-use key is minted per enrolment (Q10). Enrolment is: mint key → wait for guest agent → `qm guest exec <vmid> --pass-stdin -- tailscale up --auth-key=file:/dev/stdin --ssh --hostname lab-NAME`, with the key piped on stdin. It never goes in argv, cloud-init user-data, the seed image, the template, the VM filesystem, logs or backups. Cloud-init still handles hostname, user, resize, profile packages and setup. The same path is used for create, start and restore. Requires the guest agent in every template (it is).

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
8. Storage placement is decided only by the host config (§4.1); project files, profiles, clients and MCP callers cannot choose disks.
7. All operations are journaled and idempotent where possible; logs in `/var/log/project-lab/`.

## 15. Open questions

- ~~Q1 Key delivery~~ — **decided**: guest-agent stdin (§9).
- ~~Q2~~ **decided**: VMs get `tag:server` + an owner tag (`tag:user-<name>`), never an AI tag; existing tags are `server` (admins can SSH) and `userapp` (apps open to other users). `lab init` prints the missing `tagOwners` / `ssh` ACL snippet. See Q10 for the key-minting consequence.
- ~~Q3~~ **decided**: login user = the owner's lab username (same in every distro), created by cloud-init, passwordless sudo, password locked (access only via Tailscale SSH / guest agent).
- ~~Q4~~ **decided**: per-project GitHub deploy key generated in the VM (public key printed for the user to add); dotfiles repo URL is a config setting, applied on first boot if set.
- ~~Q5~~ **decided**: idle = 7 days, no SSH session, CPU < 5%; stop only; disabled until enabled.
- ~~Q6~~ **decided**: Windows-only client for v1 (portable paths).
- ~~Q7~~ **decided**: MCP elicitation first, `lab approve ID` terminal fallback.
- ~~Q8~~ **decided**: guest agent for one-shot commands and file transfer, Tailscale SSH for interactive/long-running.
- ~~Q9~~ **decided** (my call): per-family package maps cover only base packages and the docker/go/rust/uv modules in v1.
- ~~Q10~~ **decided**: a Tailscale OAuth client (scope `auth_keys` write, restricted to `tag:server` and `tag:user-*`) mints a single-use, short-expiry, ephemeral, pre-approved key per VM with `tag:server` + `tag:user-<owner>`. Credentials in `/etc/project-lab/ts-oauth` (0600 root); the minted key is piped via guest-agent stdin and never stored. This replaces the static `ts-authkey` file in §4/§9. Enrolment fails closed if minting fails.
- ~~Q11~~ **decided**: `lab init` makes the person running it the first admin (tailnet login via `tailscale whois`); `lab user add <tailnet-login> <name>` (admin) adds others.
- ~~Q12~~ **decided**: users see only their own projects and archives; admins see all. A name clash with another user's project returns a generic "name unavailable" without revealing the owner.

## 16. MCP server (`lab mcp`)

**Transport** (as built in M4): stdio. The MCP client on the PC launches `ssh -T -o BatchMode=yes <host> lab mcp`, so the server process runs **on the host** over the client's SSH-over-tailnet transport; nothing new listens anywhere. This replaces the earlier idea of running the server on the PC and calling `lab --json`: the audit log, approvals (`lab approve`) and caller identity all live on the host, so the PC holds no policy and no secrets. `lab mcp-config` prints the client config.

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

**Identity**: `lab mcp` runs as the calling human's lab identity. The AI acts as that user, is bound by their permissions and quotas, and is never given its own owner tag; VMs it creates are owned by the human. Approvals are always answered by that human (or an admin), never by the AI session.

### 16.1 As built (M4)

- **Tools**: `lab_list`, `lab_status`, `lab_create`, `lab_attach`, `lab_start/stop/restart`, `lab_archive`, `lab_restore`, `lab_delete`, `lab_exec` (+ `background`, `lab_exec_poll`, `lab_exec_cancel`), `lab_read_file`, `lab_write_file`, `lab_upload`, `lab_download` (base64), `lab_logs`, `lab_approvals`. Not yet: `lab_snapshot/rollback/clone/resize` (M5 features). There is no tool that grants an approval, protects/unprotects, or touches the host. Unknown arguments are rejected (`additionalProperties: false`), so a tool call can never set owner, disk, bridge or other host-decided settings.
- **Where commands run**: the QEMU guest agent, as the project owner via `runuser -l`; root only with approval. File content goes over stdin as base64, never in argv. Files are limited to `mcp.max_file_bytes` (4 MiB); larger transfers use scp over Tailscale. Output is capped per stream (`mcp.max_output_bytes`).
- **Session binding**: a session may act inside projects it created or that a human let it attach to (per project, per session). `lab_start/stop/restart`, exec, files and logs all count as working inside. `lab_list`/`lab_status` do not need attachment.
- **Approvals**: needed for delete, archive, restore, root exec/read/write, create above `[mcp]` caps, and attaching. Delivered by MCP elicitation when the client supports it (accept = approve exactly this action; decline/cancel = refused, nothing is stored); otherwise the call fails with a six-character ID and the human runs `lab approve ID` (or `lab approve ID --deny`) in a host terminal. An approval is bound to the exact tool, project and arguments, is single-use, expires after `mcp.approval_ttl_s` (600 s), and only its owner or an admin can decide it. Asking again reuses the pending ID.
- **Desktop popup (`lab watch`)**: clients that cannot show MCP prompts (the first host trial showed this for the Claude Code desktop session) get a helper on the PC instead. `lab watch` polls `lab approvals --pending --heartbeat` every 3 s and shows an Approve / Deny window (Deny has the focus; closing the window denies). While a heartbeat from that user is fresher than 20 s, a gated MCP call is held open up to `mcp.approval_wait_s` (50 s) and continues the moment the user clicks, so one tool call completes. With no helper running, or no click in time, the call fails with the approval ID as before and the click or `lab approve ID` still works for a retry. `lab watch --install` adds a hidden launcher to the Startup folder. The server writes a `session` audit record per connection with the client name and whether it supports elicitation. **Limit**: the helper approves as the user over their SSH identity, so an AI session that also has a shell on the PC can issue the same commands; keep `lab` and `ssh` out of auto-approved shell commands there.
- **Protected projects**: `lab_list`/`lab_status` show only name and `protected`. Stop, restart, archive, restore and delete are refused outright. Start, exec, files and logs need a fresh single-use approval every time, even when attached.
- **Audit** (`/var/log/project-lab/mcp-audit.log`, JSON lines): a `start` record for every state-changing call and an `end` record for every call, with session, user, tool, project, redacted arguments, result, approver and duration. Tailscale keys, `password/token/secret=` text, bearer tokens and secret-named fields are redacted; file contents are stored only as length and hash. **Fail closed**: if the log cannot be written the call is not executed.
- **Client** (`lab` on the PC, which dispatches to client mode when it is not on a Proxmox host; `LAB_CLIENT=1`/`LAB_HOST=1` override): `lab setup HOST [--user U]`, `lab ssh NAME [--via-host]`, `lab ssh-config` (managed block in `~/.ssh/config` with `lab-host` and `lab-NAME` entries; everything outside the markers is untouched, original backed up once), `lab mcp-config`, `lab put/get` of local files, a local typed confirmation for `delete`, and every other command forwarded to the host over SSH with arguments quoted for the remote shell.
- **Host commands added**: `lab exec NAME [--root] [--timeout N] [--cwd D] -- CMD...`, `lab put NAME DST` (stdin), `lab get NAME SRC` (stdout), `lab approve ID [--deny] [--yes]`, `lab approvals [--all]`, `lab mcp`.

## 17. Multi-user

Multi-user is in v1 (decision).

- **Identity**: users reach the host over Tailscale SSH; the host resolves the caller's tailnet login (`tailscale whois` on the connecting address) and maps it to a lab user stored in the lab database (`users` table: login, name, role, quota overrides; managed with `lab user ...`, first admin created by `lab init`). Unknown logins are refused. No new passwords or tokens.
- **Roles**: `admin` (sees and manages everything, exempt from quotas) and `user` (own projects only; cannot see other users' projects or archives).
- **Ownership**: every project records `owner`. Stored in the VM description/tag and DB. Owner is the login user inside the VM (§15 Q3) and drives the tailnet owner tag.
- **Tags**: each VM joins the tailnet with `tag:server` + `tag:user-<owner>` (ACL snippet printed by `lab init`; key minting per Q10). Admins reach every VM through the `server` tag rule; an owner reaches their own through the owner-tag rule.
- **Quotas** (per user, configurable, admins exempt): max running VMs, total vCPU, total RAM, total fast-disk GB, optional max archive GB. Checked on create, resize, start, restore. Refusals state which limit and the current usage.
- **Visibility**: `lab list` filters by owner for users; `--all` for admins. Protect and attach rules in §16 apply per owner.
- **Locks/IDs**: VMID range is shared; names are globally unique (`lab-<name>`), with an optional `<owner>/` display prefix considered later.
- **Audit**: every state-changing command records the caller identity, in addition to MCP audit.
