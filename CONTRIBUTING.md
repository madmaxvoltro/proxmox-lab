# Contributing

Thanks for looking. Bug reports, ideas and pull requests are welcome.

## Run the tests

```bash
git clone https://github.com/madmaxvoltro/proxmox-lab && cd proxmox-lab
python -m unittest discover -s tests -t .
```

Python 3.11+ and nothing else: the project has no runtime dependencies on purpose (the host side is
installed with `curl | bash` and should not need `pip`). The tests do not need Proxmox, Tailscale or a
network: they run against in-memory fakes.

## How the code is laid out

| Path | What lives there |
|---|---|
| `src/lab/runner.py`, `hostio.py` | The only two doors to the outside world (commands, files, HTTP). Dry-run and the test fakes replace them |
| `src/lab/proxmox.py` | Thin wrapper over `qm`, `pvesh`, `pvesm`, `vzdump`, `qmrestore` |
| `src/lab/lifecycle.py`, `archive.py`, `guest.py` | Create/start/stop/delete, verified archive & restore, exec/files via the guest agent |
| `src/lab/mcp.py`, `approvals.py`, `audit.py` | The MCP server, human approvals, the audit log |
| `src/lab/setup.py`, `init.py`, `doctor.py` | The setup wizard and health checks |
| `src/lab/client.py`, `connect.py`, `watch.py` | The Windows client, `lab connect`, the approval popup (`data/approve_popup.ps1`) |
| `tests/fake_proxmox.py` | An in-memory Proxmox with failure injection |
| `docs/SPEC.md` | The design and the reasons behind it |

## Ground rules

- **Anything that deletes or overwrites needs a test that proves the failure path leaves the original intact.** Archive, restore and create are tested by injecting a failure at each step.
- **Secrets never go in argv, logs or files.** The Tailscale key travels on stdin only; there is a test for it.
- **The host's config alone decides disks and network.** Project files, clients and MCP arguments must not be able to.
- Keep output friendly: progress goes through the reporter in `progress.py`, never `print`.
- If you change behaviour, update `docs/SPEC.md` in the same pull request.

## Releases

Maintainers: bump `version` in `pyproject.toml` and `src/lab/__init__.py`, write the version's entry in `CHANGELOG.md` (it becomes the release notes, and the release build fails without it), then
`git tag vX.Y.Z && git push --tags`. The release workflow tests, builds `lab.exe` for x64 and ARM64,
and publishes the files with a `SHA256SUMS`.
