# Security

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private reporting:
**Security → Report a vulnerability** on this repository. You will get an answer within a few days.

## What this project is trying to guarantee

- An AI session using the MCP server never gets a shell on the Proxmox host and cannot approve its own requests.
- Risky actions need a human decision, bound to the exact action and arguments, single-use and time-limited.
- The Tailscale auth key for a new VM is minted per VM, single-use and ephemeral, and is delivered over the
  QEMU guest agent's stdin. It is never written to disk, a log, cloud-init or an archive.
- Nothing that arrives with a project (a repo file, a client, an MCP argument) can choose disks, the bridge or paths.
- An archive is only trusted after a trial restore and boot test; a failure never costs you the original.

If you find a way to break one of these, that is a vulnerability.

## Known limits (not vulnerabilities)

- The approval helper approves **as the logged-in user**. An AI session that also has a shell on the same PC can run
  the same commands. Keep `lab` and `ssh` out of auto-approved shell commands there.
- Project VMs are isolated on the tailnet by their tag, not on your LAN. Isolating LAN VLANs is your router's/firewall's job.
- `lab.exe` is not code-signed.
- A user who can SSH to the host as `root` is an administrator. A restricted account for non-admin users is not built yet.
