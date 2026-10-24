"""Tests for proxmox-lab. They import the package straight from ../src, so nothing needs installing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
