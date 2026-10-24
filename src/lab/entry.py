"""Console entry point: the same `lab` command is the host CLI on the Proxmox host and the thin
client everywhere else. Set LAB_CLIENT=1 to force client mode, LAB_HOST=1 to force host mode."""

import os
import shutil
import sys


def is_host() -> bool:
    if os.environ.get("LAB_HOST") == "1":
        return True
    if os.environ.get("LAB_CLIENT") == "1":
        return False
    return shutil.which("pvesh") is not None


def main(argv=None) -> int:
    if is_host():
        from . import cli
        return cli.main(argv)
    from . import client
    return client.main(argv)


if __name__ == "__main__":
    sys.exit(main())
