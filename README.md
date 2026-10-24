# lab

Personal project lab on Proxmox: `lab create foo` gives you an Ubuntu 24.04 VM on the fast disk, joined to your Tailscale network with Tailscale SSH. `lab archive foo` moves it to the archive disk (verified, auto-rollback on failure) and removes it from the tailnet; `lab restore foo` brings it back.

Status: **M0 done** (scaffold, config, SQLite, Proxmox wrapper, dry-run, fake-Proxmox tests). See [docs/SPEC.md](docs/SPEC.md) and [docs/FEATURES.md](docs/FEATURES.md).

## Layout

```
src/lab/        config.py db.py runner.py proxmox.py locking.py units.py cli.py
tests/          fake_proxmox.py (in-memory qm/pvesh/pvesm/vzdump) + unit tests
```

Standard library only; Python >= 3.11 (Proxmox VE 9 ships 3.13).

## Develop

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
PYTHONPATH=src python -m lab.cli --dry-run discover   # on a Proxmox host
```

`--dry-run` runs read-only commands for real and prints (skips) everything that would change the host.
