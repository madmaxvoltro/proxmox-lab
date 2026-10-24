"""Entry point for the PyInstaller build of lab.exe (a package module cannot be the script itself)."""
import sys

from lab.entry import main

if __name__ == "__main__":
    sys.exit(main())
