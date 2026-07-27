"""SincroGit — automatic synchronization with robust Git versioning.

Phase 1: watcher + snapshots + periodic sealing with a fallback message.
Phase 2: AI messages, push and pull. Phase 4: system tray UI.
v0.2: the SHADOW model — snapshots live on a private side ref instead of a
WIP commit at the user's tip (invisible to git log/status). See DESIGN.md §2.
"""

__version__ = "0.2.2"
