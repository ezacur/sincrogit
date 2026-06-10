# SincroGit — User Manual

A practical, reference-style guide to **operating** SincroGit: how to launch it, every CLI
command, the control-panel actions, common task recipes, and where its files live.

> This manual is the **how**. For the **when/why** (which scenarios SincroGit is good for, in
> plain language) read **[GUIDE.md](GUIDE.md)**; for the internals and design rationale,
> **[DESIGN.md](DESIGN.md)**; for the full configuration reference, the
> **[README](README.md#configuration)**. (Versión en español: **[MANUAL_ES.md](MANUAL_ES.md)**.)

In the examples below, `python -m sincrogit …` and the standalone `SincroGit.exe …` are
interchangeable.

---

## 1. Install & first run

```powershell
pip install -r requirements.txt        # or, as a package:  pip install -e .
```

On the **first launch with no arguments**, SincroGit creates `sincrogit.config.yaml` next to
the executable and opens the Configuration tab — you then add repos from the GUI (Status →
*Add repo…*). To start from a template by hand instead:

```powershell
copy config.example.yaml sincrogit.config.yaml
```

---

## 2. Launching SincroGit

There are four ways to start it; the binary and `python -m sincrogit` behave identically.

| Invocation | What it does |
|------------|--------------|
| *(no arguments)* | GUI tray app **+** background daemon. **Single instance**: a second launch just brings the running panel to the front and exits. |
| `--tray [--config X]` | Same as above, explicitly (and lets you point at a specific config). |
| `--headless [--config X]` | Background daemon **without** the GUI — for servers / automation. |
| *(a one-shot flag)* | Runs a single CLI command and exits (see §3). Output goes to the launching terminal. |

Single-instance is enforced by a named mutex on Windows (plus a localhost-port handshake
elsewhere), and applies to the tray **and** `--headless` alike — two daemons amending the
same repos' WIPs would race each other's git work. A second instance refuses to start
(exit code 2).

---

## 3. CLI command reference

Anything other than `--tray`/no-args is a **one-shot**: it runs, prints to the terminal, and
exits. Every one-shot needs a config (auto-discovered, or `--config PATH`).

| Command | Purpose |
|---------|---------|
| `--config`, `-c PATH` | Use a specific `config.yaml` (otherwise auto-discovered, see §7). |
| `--headless` | Run the daemon without the GUI. |
| `--tray` | Launch the GUI tray app. |
| `--snapshot-once` | One snapshot pass over all repos, then exit. |
| `--seal-once` | Force a seal (+push) on all repos, then exit. |
| `--sync-once` | One sync pass (fetch + pull + push) on all repos, then exit. |
| `--commit REPO` | Manual "Smart Commit" of REPO (see below). |
| `--message`, `-m MSG` | With `--commit`: use MSG directly (skip the AI proposal/editor). |
| `--yes`, `-y` | With `--commit`: accept the AI-proposed message without editing. |
| `--history FILE` | Show FILE's version history and restore a chosen version. |
| `--pick N` | With `--history`: restore version N non-interactively. |
| `--autosnaps` | Fetch + list the per-machine autosnap recovery points for every repo. |
| `--apply-handoff REPO` | Apply your other machine's pending live work to REPO. |
| `--help`, `-h` | Show usage and exit. |

### Manual commit — `--commit REPO`

Seals your current work now with a curated message instead of waiting for the automatic seal.
SincroGit proposes a **Conventional Commits** message (covering everything since your last
manual commit) and opens it in your editor; on save it seals and pushes.

```powershell
python -m sincrogit -c config.yaml --commit myrepo                  # edit the proposal in $EDITOR
python -m sincrogit -c config.yaml --commit myrepo -y               # accept the proposal as-is
python -m sincrogit -c config.yaml --commit myrepo -m "feat: add X" # use your own message
```

The editor is resolved git-style: `GIT_EDITOR` → `VISUAL` → `EDITOR` → git's `core.editor` →
Notepad. The proposal needs an AI backend (Ollama or a Gemini key); without one it falls back
to a deterministic message.

### Time machine — `--history FILE [--pick N]`

Lists a file's past versions (sealed commits + ~5-min snapshots from the reflog + any fetched
autosnap states) and restores the one you pick.

```powershell
python -m sincrogit -c config.yaml --history src\app.py            # interactive: lists, asks which
python -m sincrogit -c config.yaml --history src\app.py --pick 3   # restore version 3 directly
```

A restore is itself captured by the next snapshot, so it stays reversible.

### Cross-machine recovery — `--autosnaps` and `--apply-handoff REPO`

```powershell
python -m sincrogit -c config.yaml --autosnaps            # fetch + list each machine's latest mirror
python -m sincrogit -c config.yaml --apply-handoff myrepo # pull your other machine's live WIP into myrepo
```

`--autosnaps` is the disaster-recovery list (use it on another machine after a dead disk; the
fetched states then show up in `--history` too). `--apply-handoff` is the manual trigger for
the cross-machine handoff (useful with `live_handoff: ask`, or to force it now).

---

## 4. The control panel (GUI)

Open it from the tray icon (double-click) or *Open control panel*.

- **Status** — the repos table (branch, state, time since last seal, last action) with per-repo
  buttons:
  - **Pause / Resume** — stop/resume autosync for that repo.
  - **Commit…** — Smart Commit (AI-proposed message dialog).
  - **Seal+Push** — seal the current WIP now and push.
  - **Fetch+Pull** — fetch and rebase from the remote now.
  - **Apply handoff** — appears (blue) only when your other machine has work waiting (in
    `live_handoff: ask` mode).
  - Top bar: **File history…** (browse/preview/restore a file or the whole repo) and **Add
    repo…** (optionally drops a `* text=auto` `.gitattributes`).
- **Log** — events, filterable by repo / action / level / text.
- **Configuration** — edit `config.yaml`; *Save* or *Save and restart* to apply.
- **About**.

The **tray icon colour** reflects state: green = active, amber = paused, red = conflict
(needs you), gray = stopped. The tray menu also has Pause/Resume, Sync now, Seal now, Quit.

---

## 5. Common tasks (recipes)

| I want to… | Do this |
|------------|---------|
| **Add a repo** | Panel → Status → *Add repo…* (or edit `repos:` in the config and *Save and restart*). |
| **Get back an earlier version of a file** | Panel → *File history…* → pick the file → pick a version → *Restore*. Or `--history FILE`. |
| **Roll the whole repo back** | Panel → *File history…* → *Restore whole repo* to a chosen point. |
| **Make a clean, documented commit now** | Per-repo **Commit…** button, or `--commit REPO`. |
| **Move my work to another machine** | Just **lock the screen / close the lid** — SincroGit flushes; on the other machine, unlock and it syncs. Or **Smart Commit** before leaving for an instant handoff. |
| **Recover after a dead disk** | On another machine: `--autosnaps` (or panel → *Fetch autosnaps*), then *File history* / *Restore*. Loses at most ~30 min. |
| **Stop writing automatic commits (purist)** | Set `seal_interval_min: inf`; commit by hand with Smart Commit. |
| **Work on a feature branch (team)** | Set `track_current_branch: true`, work on your own branch, Smart Commit → Pull Request. See [README → Using it in a team](README.md#using-it-in-a-team-shared-repos). |
| **Sync one repo more aggressively** | Override its intervals per repo — see [README → Tuning a "hot" repo](README.md#tuning-a-hot-repo). |
| **Pause everything briefly** | Tray → *Pause* (or per-repo *Pause*). |

---

## 6. Configuration (essentials)

Configuration is a YAML file: a `defaults:` block + a `repos:` list, where **any default can
be overridden per repo**. The most-used keys:

| Key | Default | Meaning |
|-----|---------|---------|
| `snapshot_interval_sec` | 300 | How often the WIP is amended (the time-machine granularity). |
| `seal_interval_min` | 360 | How often a permanent commit is sealed (`inf` = purist: never auto-seal). |
| `autosnap` / `autosnap_interval_min` | true / 30 | Live mirror to the remote (disk-failure recovery + handoff). |
| `live_handoff` | auto | Pick up your other machine's WIP: `auto` / `ask` / `off`. |
| `track_current_branch` | false | Follow the current branch instead of pausing off `branch`. |
| `push` / `pull` | true / true | Push sealed commits / periodic pull. |
| `extra_excludes` / `extra_includes` | — | Paths to skip / binaries to version anyway (e.g. `**/*.docx`). |
| `max_file_bytes` | 1048576 | Largest file auto-versioned (1 MB). |
| `suggest_excludes` | true | Suggest excluding a high-churn folder (Smart Ignore). |
| `pandoc_path` | `pandoc` | (top-level) pandoc for readable `.docx` diffs. |
| `ai.*` | — | AI backend for commit messages (Ollama / Gemini / none). |

Two idioms: any **interval/threshold** can be disabled with `inf` (or `off`/`none`/`never`);
and a **"hot" repo** tightens its own intervals while the others stay relaxed. See the full
table and both idioms in the **[README → Configuration](README.md#configuration)**.

---

## 7. Files & locations

- **Config:** `sincrogit.config.yaml`, looked up next to the executable, then in
  `%APPDATA%\SincroGit\`, then the working directory; override with `--config PATH`.
- **Log:** `sincrogit.log` (rotating) next to the config; structured events in `events.jsonl`.
- **`.gitattributes`:** SincroGit may add `* text=auto` (consistent line endings across
  machines) and, for `.docx` repos, `*.docx -text diff=pandoc` — both committed, so they
  travel with the repo.
- **Autosnap refs (on the remote):** `refs/autosnap/<user>/<host>/<branch>` — per-user,
  per-machine live mirrors; never your working branch.

---

## 8. Exit codes (for scripting)

The one-shot commands are scriptable:

- **0** — success.
- **1** — the command ran but couldn't complete (e.g. nothing to restore, handoff no longer
  safe; also a `--headless` daemon that stopped on an unexpected engine error).
- **2** — startup problem (no config found, invalid config, the GUI was requested without
  PyQt5 installed, or a second instance when SincroGit is already running).

Example (a scheduled nightly seal):

```powershell
python -m sincrogit -c C:\repos\config.yaml --seal-once
```
