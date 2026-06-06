"""PyInstaller entry point for the standalone SincroGit executable.

Builds one .exe that is both the GUI/daemon (no arguments) and the CLI (any
argument). See build.ps1 / the README "Building a standalone .exe" section.
"""

import sys

from sincrogit.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
