# lab

Personal project lab on Proxmox: `lab create foo` gives you an Ubuntu 24.04 VM on the fast disk, joined to your Tailscale network with Tailscale SSH. `lab archive foo` moves it to the archive disk (verified, auto-rollback on failure) and removes it from the tailnet; `lab restore foo` brings it back.

Status: spec phase. See [docs/SPEC.md](docs/SPEC.md) and [docs/FEATURES.md](docs/FEATURES.md).
