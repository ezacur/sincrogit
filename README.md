# SincroGit

Automatic, instant file synchronization, but with **robust Git versioning**.
It takes automatic *snapshots* of your repos every few minutes (auto-backup against
power cuts), mirrors the latest state to the remote every ~30 min (**autosnap**, for
disk-failure recovery) and "seals" commits with a clean history every 6 hours.

> Full design and decisions in **[DESIGN.md](DESIGN.md)**. New to Git or want the
> plain-language version? See **[GUIDE.md](GUIDE.md)** (Spanish: [GUIA.md](GUIA.md)).

## Status: Phases 1 and 2 complete

**Phase 1 (local core):**

- ✅ Filesystem watcher (`watchdog`) + *debounce*.
- ✅ **Snapshot** every 5 min: `git commit --amend` over a WIP commit (no commit pile-up).
- ✅ Initial snapshot on startup (captures pre-existing changes, e.g. after a reboot).
- ✅ **Sealing** every 6 h: turns the WIP into a permanent commit + creates a new WIP.
- ✅ **Filter**: only text < 1 MB is versioned automatically; binaries/large files by hand.
- ✅ **Opt-in binary versioning** (`extra_includes`, e.g. `**/*.docx`): version chosen
  binaries too — with **readable diffs for Word docs** via a pandoc textconv driver
  (no per-machine `git config` needed), so the AI seal/Smart Commit messages and the
  time-machine show *what changed in the document*.
- ✅ Deterministic *fallback* commit message when sealing.
- ✅ Clean shutdown with a final local snapshot.
- ✅ Logging to a rotating file + console.

**Phase 2 (AI + remote sync):**

- ✅ **AI commit messages** when sealing, hybrid mode: Ollama (local) → Gemini (cloud) →
  deterministic fallback. Never blocks the commit if the AI fails. Automatic seals are
  prefixed **`sincro:`** so machine commits are easy to tell apart from yours.
- ✅ **Manual "Smart Commit"**: commit your current work now with an **AI-proposed
  Conventional Commits** message (`feat:`/`fix:`/…) that you can edit. The proposal
  summarizes everything since your **last manual commit** (skipping the `sincro:` seals);
  it resets the 6 h seal timer. From the control panel (per-repo **"Commit…"**).
- ✅ Privacy: content is only sent to the cloud if `cloud_send_content: true`.
- ✅ **Push** of sealed commits (never the WIP) after sealing + retry on every sync.
- ✅ **Periodic pull** (every 10 min): `fetch` + rebase of the WIP only if the remote is ahead.
- ✅ **Conflicts**: the rebase is aborted, the repo is paused and you are notified. Never
  force, never data loss.
- ✅ **Autosnap** (live mirror, every 30 min, only if changed): force-pushes `HEAD`
  (incl. the WIP) to a per-machine side ref `refs/autosnap/<host>/<branch>` so a total
  disk failure loses at most ~30 min. Doesn't touch the clean branch; cross-machine
  recovery from the CLI (`--autosnaps`) and the control panel.
- ✅ **Branch guard**: if you `git checkout` another branch, SincroGit yields that repo
  (no snapshot/seal/push on the wrong branch) until you switch back.

**Phase 4 (system tray UI):**

- ✅ **System tray icon** with the SincroGit mark (a "G" with an hourglass). The
  **color reflects the state**: green=active, amber=paused, red=conflict, gray=stopped.
- ✅ Tray **menu**: open panel, pause/resume, sync now, seal now, quit.
- ✅ **Control panel** with tabs:
  - *Status*: repos table (branch, state, time since last seal, last action) with
    **per-repo buttons** (Pause/Resume, Seal+Push, Fetch+Pull) and an **"Add repo…"**
    button (optionally drops a `* text=auto` **`.gitattributes`** so line endings stay
    consistent across machines). Repos can be added live, without restarting.
  - *Log*: events **filterable by repo, action, level and text**.
  - *Configuration*: `config.yaml` editor (save / save and restart).
  - *About*.
- ✅ Desktop **notifications** (via Qt) on conflicts/errors.
- ✅ **File history / restore** ("time machine"): browse a file's past versions
  (sealed commits + reflog snapshots + fetched autosnap states), see a **colored diff**
  vs the current file, and restore **just that file or the whole repo** — from the CLI
  (`--history`, `--autosnaps`) and the control panel.

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
| `--autosnaps` | fetch & list autosnap recovery points (per machine) |
| `--commit REPO [-m MSG \| -y]` | manual commit of REPO: edit the AI-proposed message in `$EDITOR`, then seal+push |

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

### Versioning Word documents (.docx)

By default binaries aren't auto-versioned. To track `.docx` (synced + restorable) with
**readable diffs**:

1. Install [pandoc](https://pandoc.org); if it's not on PATH, set `pandoc_path` in the
   config (e.g. `pandoc_path: C:/tools/pandoc.exe`).
2. Add the pattern to your repo's includes:
   ```yaml
   defaults:
     extra_includes:
       - "**/*.docx"
   ```

SincroGit versions the `.docx` itself and maps it to a pandoc diff driver in
`.gitattributes` (committed, so it travels), passing the textconv command **inline per
call — no `git config` on any machine**. Then `git diff`, the AI seal messages, and the
time-machine show the document's changes as markdown. The `.docx` stays the source of
truth; the markdown view is lossy (no formatting/images), and without pandoc it degrades
to versioning the file as an opaque blob.

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

### Manual commit (Smart Commit)

Seal your current work now with a curated message instead of waiting for the 6 h
automatic seal. SincroGit proposes a Conventional Commits message (covering your work
since the last manual commit) and opens it in your editor:

```powershell
python -m sincrogit -c config.yaml --commit myrepo                  # edit the proposal in $EDITOR
python -m sincrogit -c config.yaml --commit myrepo -y               # accept the proposal as-is
python -m sincrogit -c config.yaml --commit myrepo -m "feat: add X" # use your own message
```

In the tray app, the per-repo **"Commit…"** button does the same.

### Test modes (one pass and exit)

```powershell
python -m sincrogit -c config.yaml --snapshot-once   # one snapshot and exit
python -m sincrogit -c config.yaml --seal-once       # force a seal (+push) and exit
python -m sincrogit -c config.yaml --sync-once       # one pull+push and exit
```

## How it works (summary)

```
... ── sealed_N ── WIP        ← HEAD, amended every 5 min (snapshot)
every 6h: the WIP is sealed (descriptive message) and a new WIP is created on top
result: ... ── sealed_N ── sealed_N+1 ── WIP(new)
```

- **Recover recent work** (power cut): the latest snapshot is in `HEAD`.
  Intermediate states of the window are in `git reflog`.
- **Go back to yesterday**: `git checkout`/`restore` from the matching sealed commit.

## Design notes & trade-offs

SincroGit is intentionally not "pure" Git. Git assumes each commit is a curated
logical change; SincroGit instead optimizes for a single developer who forgets to
commit and moves between machines. The deliberate trade-offs:

- **Time-boxed seals, not logical commits.** Automatic seals (`sincro:`) bundle
  whatever changed in a ~6 h window — a timeline, not atomic units. When you want a
  curated commit, use **Smart Commit** (AI-proposed Conventional Commits message).
  The `sincro:` prefix keeps machine commits and yours easy to tell apart.
- **The WIP is a continuous "save button".** One commit is amended every ~5 min, so a
  power cut loses nothing; intra-window states stay in the reflog.
- **Backup is decoupled from history.** `autosnap` force-pushes the live state to a
  per-machine side ref every ~30 min for disk-failure recovery, while `main` stays
  clean (only sealed commits) → the other machine's pull is always a clean fast-forward.

The cost we accept: history reads as time-buckets rather than perfectly atomic
commits, and a total disk failure can lose up to ~30 min — in exchange for effortless
versioned backup and sequential multi-machine sync.

## Limitations

SincroGit has a deliberately narrow scope. What it does **not** do:

- **Sequential, not concurrent.** It assumes one machine at a time. Simultaneous edits on
  two machines are not merged — the rebase is aborted and the repo paused for you to
  resolve by hand. It's a personal tool, not for team work on a shared branch.
- **Text only, < 1 MB.** Binaries and large files are never auto-committed; add those by
  hand. It is not a full backup of the folder.
- **Time-bucket history.** `sincro:` seals group ~6 h of unrelated changes, so a
  `git bisect`/`revert` of one logical change is harder than on curated history (use
  **Smart Commit** when you want a clean, logical commit).
- **Recovery windows aren't zero.** A power cut/crash can lose up to ~5 min (last
  snapshot); a total disk failure up to ~30 min (last autosnap).
- **Conflicts are yours to resolve.** On a rebase conflict it never forces — it pauses
  and notifies; you fix it in the terminal and resume.
- **Needs your Git credentials.** It runs in your user session and pushes with your
  existing SSH/credential setup; without push access it keeps work local and retries.
- **AI messages need a model and are approximate.** Without Ollama or a Gemini key it
  uses a deterministic fallback; a summary of a 6 h window is coarse by nature.
- **Windows-first.** Built for interactive use on Windows; on Linux/macOS you pull by hand.
- **Don't nest the repo inside another sync tool's folder** (Dropbox/OneDrive/Drive) — the
  external syncer can corrupt `.git`. Let SincroGit handle Git; let the other tool handle
  other files.

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
| `seal_interval_min` | 360 | How often a permanent commit is sealed (6 h) |
| `autosnap` | true | Live mirror of HEAD to `refs/autosnap/<host>/<branch>` (disk-failure recovery) |
| `autosnap_interval_min` | 30 | How often the mirror is force-pushed (only if it changed) |
| `max_file_bytes` | 1048576 | Maximum file size to version (1 MB) |
| `extra_excludes` | — | `.gitignore`-style patterns to exclude |
| `extra_includes` | — | patterns versioned even if binary (e.g. `**/*.docx`) |
| `max_include_bytes` | 26214400 | size cap (25 MB) for `extra_includes` |
| `pandoc_path` | `pandoc` | **(top-level)** path to pandoc for readable `.docx` diffs |
