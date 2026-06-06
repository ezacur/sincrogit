# SincroGit

Automatic Dropbox-style synchronization, but with **robust Git versioning**.
It takes automatic *snapshots* of your repos every few minutes (auto-backup against
power cuts) and "seals" commits with a clean history every 2 hours.

> Full design and decisions in **[DESIGN.md](DESIGN.md)**.

## Status: Phases 1 and 2 complete

**Phase 1 (local core):**

- ✅ Filesystem watcher (`watchdog`) + *debounce*.
- ✅ **Snapshot** every 5 min: `git commit --amend` over a WIP commit (no commit pile-up).
- ✅ Initial snapshot on startup (captures pre-existing changes, e.g. after a reboot).
- ✅ **Sealing** every 2 h: turns the WIP into a permanent commit + creates a new WIP.
- ✅ **Filter**: only text < 1 MB is versioned automatically; binaries/large files by hand.
- ✅ Deterministic *fallback* commit message when sealing.
- ✅ Clean shutdown with a final local snapshot.
- ✅ Logging to a rotating file + console.

**Phase 2 (AI + remote sync):**

- ✅ **AI commit messages** when sealing, hybrid mode: Ollama (local) → Gemini (cloud) →
  deterministic fallback. Never blocks the commit if the AI fails.
- ✅ Privacy: content is only sent to the cloud if `cloud_send_content: true`.
- ✅ **Push** of sealed commits (never the WIP) after sealing + retry on every sync.
- ✅ **Periodic pull** (every 10 min): `fetch` + rebase of the WIP only if the remote is ahead.
- ✅ **Conflicts**: the rebase is aborted, the repo is paused and you are notified. Never
  force, never data loss.

**Phase 4 (system tray UI):**

- ✅ **System tray icon** with the SincroGit mark (a "G" with an hourglass). The
  **color reflects the state**: green=active, amber=paused, red=conflict, gray=stopped.
- ✅ Tray **menu**: open panel, pause/resume, sync now, seal now, quit.
- ✅ **Control panel** with tabs:
  - *Status*: repos table (branch, state, time since last seal, last action) with
    **per-repo buttons** (Pause/Resume, Seal+Push, Fetch+Pull) and an **"Add repo…"**
    button. Repos can be added live, without restarting.
  - *Log*: events **filterable by repo, action, level and text**.
  - *Configuration*: `config.yaml` editor (save / save and restart).
  - *About*.
- ✅ Desktop **notifications** (via Qt) on conflicts/errors.
- ✅ **File history / restore** ("time machine"): browse a file's past versions
  (sealed commits + reflog snapshots), preview and restore — from the CLI
  (`--history`) and the control panel.

Pending (Phase 3): deployment as a Windows scheduled task (`pythonw.exe`) to launch
`--tray` at log-on, and a `sincrogit status` command.

## Installation

```powershell
pip install -r requirements.txt
# or, as a package:  pip install -e .
```

## Usage

1. Configuration: on first launch SincroGit creates `sincrogit.config.yaml` next to
   the executable (with an empty repo list) and opens the Configuration tab. You then
   **add repos from the GUI** (Status → "Add repo…"). To start from a template by hand:
   ```powershell
   copy config.example.yaml sincrogit.config.yaml
   ```
2. Start SincroGit:
   ```powershell
   # GUI tray app + daemon (no arguments):
   python -m sincrogit

   # …or point at a specific config:
   python -m sincrogit --tray --config config.yaml

   # Headless daemon (no GUI), for servers or automated tasks:
   python -m sincrogit --headless --config config.yaml
   ```

**Launch model** (same for the script and the standalone `.exe`):

| Invocation | Behavior |
|------------|----------|
| *(no arguments)* | GUI tray app + daemon (**single instance**; a second launch just shows the running panel) |
| `--tray [--config X]` | GUI tray app + daemon |
| `--headless [--config X]` | daemon without GUI |
| `--snapshot-once` / `--seal-once` / `--sync-once` | CLI one-shot and exit |
| `--history FILE [--pick N]` | browse/restore a file's versions |

### AI messages (optional)

- **Ollama (local, recommended):** install [Ollama](https://ollama.com), pull a model
  (`ollama pull llama3.2`) and SincroGit will use it automatically. Your code never
  leaves your machine.
- **Gemini (cloud):** get an API key from Google AI Studio and export it:
  ```powershell
  setx SINCROGIT_GEMINI_KEY "your_api_key"
  ```
  With `cloud_send_content: false` (the default), Gemini only receives file names
  and `--stat`, not the content.
- If you configure neither, a deterministic **fallback message** is used.

### Restore a past version (time machine)

Browse and restore previous versions of a file, combining sealed commits
(permanent) and intra-window snapshots (from the reflog, ~30 days):

```powershell
# Interactive: lists versions and asks which one to restore
python -m sincrogit -c config.yaml --history path\to\file.py

# Non-interactive: restore version N directly
python -m sincrogit -c config.yaml --history path\to\file.py --pick 3
```

In the tray app, the same is available from the control panel:
**Status → "File history…"** (browse, preview any version, and restore).

### Test modes (one pass and exit)

```powershell
python -m sincrogit -c config.yaml --snapshot-once   # one snapshot and exit
python -m sincrogit -c config.yaml --seal-once       # force a seal (+push) and exit
python -m sincrogit -c config.yaml --sync-once       # one pull+push and exit
```

## How it works (summary)

```
... ── sealed_N ── WIP        ← HEAD, amended every 5 min (snapshot)
every 2h: the WIP is sealed (descriptive message) and a new WIP is created on top
result: ... ── sealed_N ── sealed_N+1 ── WIP(new)
```

- **Recover recent work** (power cut): the latest snapshot is in `HEAD`.
  Intermediate states of the window are in `git reflog`.
- **Go back to yesterday**: `git checkout`/`restore` from the matching sealed commit.

## Building a standalone .exe

A single self-contained `SincroGit.exe` (GUI + CLI, no Python needed) is built
with PyInstaller:

```powershell
python -m pip install pyinstaller pillow
.\build.ps1
```

This generates `app.ico` from the vector icon and produces `dist\SincroGit.exe`.

- **Double-click** (no arguments) → tray app + daemon, **single instance** (a second
  launch just brings up the running panel).
- **From a terminal with arguments** → CLI (output attaches to that terminal):
  `SincroGit.exe --history file.py`, `SincroGit.exe --headless`, etc.
- **Config location:** the exe looks for `sincrogit.config.yaml` next to itself, then
  in `%APPDATA%\SincroGit\`. On first run with none found, it creates a default next to
  the exe (falling back to `%APPDATA%\SincroGit\` if that folder isn't writable) and
  opens the Configuration tab. `sincrogit.log` is written next to the config.

Notes: it's a `--onefile --noconsole` build (~55 MB); first launch unpacks to a temp
dir (~1–2 s). For output redirection/scripting, prefer `python -m sincrogit`.

## Configuration

See [config.example.yaml](config.example.yaml). Main keys (`defaults`,
overridable per repo):

| Key | Default | Meaning |
|-----|---------|---------|
| `snapshot_interval_sec` | 300 | How often the WIP is amended (5 min) |
| `debounce_sec` | 25 | Wait after the last change before snapshotting |
| `seal_interval_min` | 120 | How often a permanent commit is sealed (2 h) |
| `max_file_bytes` | 1048576 | Maximum file size to version (1 MB) |
| `extra_excludes` | — | `.gitignore`-style patterns to exclude |
```
