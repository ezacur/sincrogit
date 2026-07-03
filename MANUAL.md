# ⏳g SincroGit — User Manual

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
same repos' WIPs would race each other's git work. A second tray launch just brings the
running panel to the front and exits 0; a second `--headless` refuses to start (exit
code 2).

---

## 3. CLI command reference

Anything other than `--tray`/no-args is a **one-shot**: it runs, prints to the terminal, and
exits. Every one-shot needs a config (auto-discovered, or `--config PATH`).

> If the daemon (tray or headless) is running, one-shots **refuse to start** — a second
> process would race the daemon's git work on the same repos. Use the panel/tray actions
> instead, quit or pause the daemon, or pass `--force` if you know it's safe.

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
| `--doctor` | Health check: git, config, each repo's branch/remote/credentials (dry-run push), pandoc, AI backends, daemon. Exit 0 = healthy. |
| `--force` | Run a one-shot even while the daemon is running (skips the safety refusal). |
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

Pending edits are snapshotted into the WIP *before* the restore touches anything, and
the restore is itself captured — so a restore is always reversible, back to the moment
right before it.

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
  buttons. Hovering the **State** cell explains it (why a conflict paused the repo, what a
  pending handoff is, why a merge/rebase shows *Busy*). Buttons:
  - **Pause / Resume** — stop/resume autosync for that repo.
  - **Properties…** — this repo's settings as a form (branch, remote, rhythms, sync,
    handoff mode, file filters) instead of YAML. Only the fields you change are written;
    the rest keep inheriting the defaults. Also has **Remove repo…** (config only — the
    git repo on disk is untouched). Applies on restart.
  - **Commit…** — Smart Commit (AI-proposed message dialog).
  - **Seal+Push** — seal the current WIP now and push.
  - **Fetch+Pull** — fetch and rebase from the remote now.
  - While one of these (or an engine sync) is running, the bar says *working…* and the
    buttons disable — the outcome (including "nothing to seal" or a refusal) lands in the Log.
  - **Apply handoff** — appears (blue) only when your other machine has work waiting (in
    `live_handoff: ask` mode). Shows what it will do (which machine, how old) before applying.
  - **How to fix…** — appears when a repo is paused on a conflict: explains what happened
    (the rebase was aborted; your files are intact) and what to do, with an *Open folder* button.
  - Top bar: **File history…** (pick a FILE, browse its versions), **Time machine…**
    (pick a VERSION, see every file that differs, restore a selected set),
    **Machines…** (each machine's last autosnap mirror, freshness color-coded — spot a
    machine that stopped backing itself up, and *Fetch latest* to refresh) and **Add
    repo…** (optionally drops a `* text=auto` `.gitattributes`).
  - Right-click a repo row for **Open folder / File history / Time machine / Properties**.
  - A one-line **activity digest** under the action bar: today's snapshot / seal /
    push / pull counts (the Log has the detail; this is the glance).
- **Log** — events, newest first and updating live (no refresh needed); filterable by
  repo / action / level / text, including the DEBUG detail the file log gets.
- **Settings** — the friendly form: rhythms (snapshot/seal with a *Purist mode* checkbox),
  backup & sync (autosnap, handoff mode, follow-branch), AI messages, theme (light/dark/auto),
  pandoc path, log level. Edits the global defaults; *Save and restart* to apply.
- **Advanced (YAML)** — the raw `config.yaml` editor, for per-repo overrides and comments.

The **File history** dialog shows the repo's **file tree** on the left (`.git` hidden) —
click any file to see its versions (with relative times and color-coded types:
sealed / snapshot / autosnap — hover for what each means) and a themed diff against the
current file, with the changed spans **highlighted inside each line**. The search box
counts a text across every version and highlights where it appeared, changed or
vanished ("when did this function change?"). **Save a copy…** writes the selected
version to a NEW file (suggested `name (stamp).ext`) — recover an old version under
another name, overwriting nothing. **Restore ENTIRE repo…** first computes a
**preview** of exactly what would change (how many files revert / disappear / come
back, the full list under Details, and any at-risk files flagged) so you confirm on
facts, not on faith.

The **Time machine** dialog is the same history navigated the other way around: the
repo's **version timeline** on the left (seals, snapshots, fetched autosnaps); pick a
point and the right side lists **every file that differs from the present**, each with a
checkbox and its action (*revert* / *delete* / *recreate*), plus a diff of the clicked
file (**unified or side-by-side**). **Restore selected (N)** brings the checked set back
in ONE atomic step, captured as a single snapshot (reversible, as always). Files whose
current content snapshots can't capture show ⚠ and can't be selected.

The **tray icon colour** reflects state: green = active, amber = paused, red = conflict
(needs you), gray = stopped. The tray menu also has Pause/Resume, Sync now, Seal now, Quit.

---

## 5. Common tasks (recipes)

| I want to… | Do this |
|------------|---------|
| **Add a repo** | Panel → Status → *Add repo…* (or edit `repos:` in the config and *Save and restart*). |
| **Change ONE repo's settings** | Select it → *Properties…* (branch, rhythms, sync, filters as a form). Or edit its entry in Advanced (YAML). |
| **Remove a repo from SincroGit** | *Properties…* → *Remove repo…* (config only; the git repo on disk is untouched). |
| **Get back an earlier version of a file** | Panel → *File history…* → pick the file → pick a version → *Restore*. Or `--history FILE`. |
| **Recover an old version WITHOUT overwriting** | *File history…* (or *Time machine…*) → pick the version → *Save a copy…* → give it another name. |
| **Find when a text appeared/vanished** | *File history…* → pick the file → type the text → *Find* (transitions highlighted in blue). |
| **Get back SEVERAL files at once** | Panel → *Time machine…* → pick a version → check the files → *Restore selected*. |
| **Check the whole setup is healthy** | `python -m sincrogit --doctor` (git, remotes, credentials, pandoc, AI, daemon). |
| **See if my other machines are backing up** | Panel → *Machines…* (stale mirrors show in red; *Fetch latest* refreshes). |
| **Roll the whole repo back** | Panel → *File history…* → *Restore whole repo* to a chosen point (with a preview of what changes). |
| **Make a clean, documented commit now** | Per-repo **Commit…** button, or `--commit REPO`. |
| **Move my work to another machine** | Just **lock the screen / close the lid** — SincroGit flushes; on the other machine, unlock and it syncs. Or **Smart Commit** before leaving for an instant handoff. |
| **Recover after a dead disk** | On another machine: `--autosnaps` (or panel → *Fetch autosnaps*), then *File history* / *Restore*. Loses at most ~30 min. |
| **Power cut left git saying "branch broken"** | Nothing — just start SincroGit. It detects the zeroed ref and restores it from the reflog at startup (a "repair" warning shows in the Log). |
| **Stop writing automatic commits (purist)** | Set `seal_interval_min: inf`; commit by hand with Smart Commit. |
| **Work on a feature branch (team)** | Set `track_current_branch: true`, work on your own branch, Smart Commit → Pull Request. See [README → Using it in a team](README.md#using-it-in-a-team-shared-repos). |
| **Sync one repo more aggressively** | Override its intervals per repo — see [README → Tuning a "hot" repo](README.md#tuning-a-hot-repo). |
| **Pause everything briefly** | Tray → *Pause* (or per-repo *Pause*). |

> A restore never destroys unsaved work: pending edits are snapshotted first, and if some
> current content is something snapshots *can't* capture (excluded, over the size limit,
> binary) the restore **refuses** and names the files to copy somewhere safe first.

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
| `extra_excludes` / `extra_includes` | — | Paths to skip / binaries to version anyway (e.g. `**/*.docx`, `**/*.pptx`). |
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
- **Log:** `sincrogit.log` (rotating) next to the config; structured events in `events.jsonl`
  (also rotating, one `.1` backup).
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

---

## 9. Sharing the repo with other git tools

SincroGit is designed to coexist with your other git tooling — it yields while *you*
operate (a merge/rebase in progress, a locked index, another branch checked out) and
resumes afterwards. While it yields, your edits are not being snapshotted; if the
manual operation runs long (10+ min) the Log and a toast warn you once that snapshots
are postponed, and the panel shows the repo as *Busy (merge/rebase)*. The rules of
the road:

- **The `sincro: WIP autosnapshot` commit at the tip is SincroGit's; everything below it is
  yours.** Commit, branch, tag or rebase *under* it freely — the daemon detects external
  commits and respects them as manual seals. But don't amend or reword the WIP itself
  from another tool: rewording strips the `WIP:` prefix, so the daemon treats it as a
  manual commit — and pushes it.
- **Git clients (lazygit, Fork, GitKraken, VS Code, …):** fine alongside. They'll show
  the WIP at the tip — leave that one commit alone and work as usual.
- **GitButler (`but`):** it *takes over* a repo (checks out its own
  `gitbutler/workspace` branch and blocks direct commits with a hook). With SincroGit's
  default branch guard this is handled: SincroGit **yields** while GitButler is active
  and resumes when you leave workspace mode (`but teardown` / checkout your branch).
  Do **not** combine it with `track_current_branch: true` on that repo — SincroGit would
  follow (and push) GitButler's workspace branch. In general: one tool *managing* a
  given repo at a time.
- **Dropbox / OneDrive / Drive:** never keep the repo inside another sync tool's folder —
  it can corrupt `.git` (see [README → Limitations](README.md#limitations)).
