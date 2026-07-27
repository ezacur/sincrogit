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
same repos' snapshots would race each other's git work. A second tray launch just brings the
running panel to the front and exits 0; a second `--headless` refuses to start (exit
code 2).

**Start at login (Windows):** tick *Start SincroGit when I sign in to Windows* in the
Settings tab (applied the moment you Save; no restart), or run `--autostart on`. It
registers the current program + config in the per-user Run key — no admin needed, and
Windows also lists it under Task Manager → Startup apps, where you can toggle it like any
other app. It is a per-machine setting (not stored in `config.yaml`), `--doctor` reports
its state, and if the entry is ever left pointing at a program that no longer exists (a
moved install), the next tray launch re-registers itself.

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
| `--doctor` | Health check: git, config, each repo's branch/remote/credentials (dry-run push), pandoc, AI backends, daemon, auto-start. Exit 0 = healthy. |
| `--autostart on\|off` | Register / unregister start-at-login for the current user (Windows Run key), then exit. Safe alongside a running daemon. |
| `status` / `--status` | One glance at every repo: branch, state, snapshot/commit ages, unsealed snapshots, pending edits. Read-only — safe alongside the daemon. `--repo NAME` limits it to one repo. |
| `log` / `--log` | Print the structured event log (the panel's Log tab, in the terminal), oldest to newest. Read-only — safe alongside the daemon. |
| `--repo NAME` | With `status`/`log`: only this repo (`log` keeps global events too, like the panel's filter). |
| `--action A[,B,...]` | With `log`: only these action types (e.g. `seal,leave-seal,push`). |
| `--level LVL` | With `log`: minimum severity (`DEBUG`/`INFO`/`WARNING`/`ERROR`). |
| `--tail N` | With `log`: last N events (0 = all; default 50). |
| `--version` | Print this build's identity — version, build time, and the **exe's own SHA-256** — then exit. Needs no config, so it works on an exe you just copied onto a machine; the hash is what tells two builds of the same version apart. |
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

Pending edits are snapshotted (into the shadow chain) *before* the restore touches anything, and
the restore is itself captured — so a restore is always reversible, back to the moment
right before it.

### Cross-machine recovery — `--autosnaps` and `--apply-handoff REPO`

```powershell
python -m sincrogit -c config.yaml --autosnaps            # fetch + list each machine's latest mirror
python -m sincrogit -c config.yaml --apply-handoff myrepo # pull your other machine's live work into myrepo
```

`--autosnaps` is the disaster-recovery list (use it on another machine after a dead disk; the
fetched states then show up in `--history` too). `--apply-handoff` is the manual trigger for
the cross-machine handoff (useful with `live_handoff: ask`, or to force it now).

---

## 4. The control panel (GUI)

Open it from the tray icon (double-click) or *Open control panel*.

- **Status** — the repos table (branch, state, snapshot age, time since last seal,
  unsealed snapshots, last action) with per-repo buttons. Hovering the **State** cell
  explains it (why a conflict paused the repo, what a pending handoff is, why a
  merge/rebase shows *Busy*); **Unsealed** counts the snapshots the next permanent
  commit will publish, and marks with ✎ any edits newer than the last snapshot (the
  CLI `status` shows the same numbers). Buttons:
  - **Pause / Resume** — stop/resume autosync for that repo.
  - **Properties…** — jumps to this repo's section in the **Settings tab** (no
    window: everything edits inline there — see Settings below).
  - **Commit…** — Smart Commit (AI-proposed message dialog).
  - **Seal+Push** — seal the pending snapshots into a real commit now and push.
  - **Fetch+Pull** — fetch and rebase from the remote now.
  - While one of these (or an engine sync) is running, the bar says *working…* and the
    buttons disable — the outcome (including "nothing to seal" or a refusal) lands in the Log.
  - **Apply handoff** — appears (blue) only when your other machine has work waiting (in
    `live_handoff: ask` mode). Shows what it will do (which machine, how old) before applying.
  - **How to fix…** — appears when a repo is paused on a conflict: explains what happened
    (the rebase was aborted; your files are intact) and what to do, with an *Open folder* button.
  - Top bar: **Time machine…** (jumps to the Time machine tab focused on this repo —
    the repo's whole past lives there), **Machines…** (each machine's last autosnap
    mirror, freshness color-coded — spot a machine that stopped backing itself up, and
    *Fetch latest* to refresh) and **Add repo…** (optionally drops a `* text=auto`
    `.gitattributes`; you can also paste a **remote URL** and **Verify** it —
    reachability plus a dry-run push for write access — before adding, so
    push/pull/sync work from the start. If you already set this repo's options on
    **another of your machines**, the dialog offers a checkbox to **inherit those
    saved settings** — see *Cross-machine settings* below).
  - Right-click a repo row for **Open folder / Time machine / Properties**.
  - A one-line **activity digest** under the action bar: today's snapshot / seal /
    push / pull counts (the Log has the detail; this is the glance).
- **Time machine** — every view of the repo's past, in one grid. The left rail lists
  the states day by day — every ~5-min snapshot, every seal and (after *Fetch
  autosnaps*) your other machines' mirrors, color-coded — and refreshes itself as new
  snapshots land (*Seals only* trims the rail to the permanent commits). The
  **Compare** switch decides the question the right side answers:
  - *what changed then* (default): the files that state captured (status and +/− line
    counts) and, per file, the **colored diff** of exactly what that snapshot saved.
  - *vs today*: **every file that differs from the present** at that state, each with
    a checkbox and its action (*revert* / *delete* / *recreate*), plus the clicked
    file's diff (**unified or side-by-side**, with intra-line highlights). **Restore
    selected (N)** brings the checked set back in ONE atomic step, captured as a
    single snapshot (reversible, as always); files whose current content snapshots
    can't capture show ⚠ and can't be selected. **Restore ENTIRE repo…** first
    computes a **preview** of exactly what would change (how many files revert /
    disappear / come back, the full list under Details, any at-risk files flagged) so
    you confirm on facts, not on faith.

  **Pin a file** (double-click it in the list, or *Pin a file…*) to follow ONE file
  through time: the rail becomes that file's versions (relative times, color-coded
  types: sealed / snapshot / autosnap — hover for what each means). The search box
  counts a text across every version and highlights where it appeared, changed or
  vanished ("when did this function change?"). **Save a copy…** writes the selected
  version to a NEW file (suggested `name (stamp).ext`) — recover an old version under
  another name, overwriting nothing. **Restore file** rolls the whole file back;
  **Restore hunks…** opens a picker where you tick only the changed blocks you want
  back (text files only), keeping the rest of your current edits — the partial
  restore is captured as a snapshot too.
- **Log** — events, newest first and updating live (no refresh needed); filterable by
  repo / action / level / text, including the DEBUG detail the file log gets.
- **Settings** — master-detail, all in one screen: the list on the left holds
  **Global defaults** plus every repo. Picking a repo shows EVERY per-repo setting
  inline (branch, remote, rhythms — including the leave seal and the debounce —,
  sync, handoff mode, network timeout, file filters and size caps, notices); next
  to each field a hint says where the value comes from — *default (X)* when
  inherited, or *override — default: X* when the repo pins it. Only the fields you
  change are written; **Use defaults…** drops every override at once, and
  **Remove repo…** removes the entry (config only). The **Global defaults** page is
  the friendly form: rhythms (snapshot cadence, a **leave seal** toggle —
  lock the machine and stay away `seal_on_leave_min` minutes (20 by default) and the
  pending work is sealed as `sincro: [leave]` and pushed, so your other machine pulls a
  fresh branch; coming back earlier cancels it — plus a **Permanent history**
  selector: *Automatic checkpoints* — the recommended auto-seal — or *Only my own commits*,
  i.e. purist mode, with an optional once-a-day **commit reminder** when work piles up),
  backup & sync (autosnap, handoff mode, follow-branch), AI messages, theme (light/dark/auto),
  pandoc path, log level. Edits the global defaults; *Save and restart* to apply.
- **Advanced (YAML)** — the raw `config.yaml` editor, for per-repo overrides and comments.

**Cross-machine settings.** A repo's per-repo options travel WITH the repo. Whenever a
repo mirrors to the remote (autosnap), SincroGit also publishes its options to a tiny
per-user side ref (`refs/sincro/config/<you>`) — no secrets, just the intervals, filters
and toggles you set. When you later add that same repo on **another of your machines**,
the *Add repo…* dialog fetches those options and offers to inherit them (a checkbox,
listing them). It's a **one-time** copy at add: changing the options later on one machine
does NOT re-sync to the others (adjust them there, or re-inherit by removing and
re-adding). The ref is namespaced by your git identity, so a teammate's preferences and
yours never collide.

The **tray icon colour** reflects state: green = active, amber = paused, orange-red = your
work is not reaching the remote, red = conflict (needs you), gray = stopped. The tray menu
opens with a greyed-out identity line (`SincroGit <version>`, whose tooltip adds the build
date — hover it to answer "which build is this machine running?" without a terminal), then
Pause/Resume, Sync now, Seal now, **"Update and relaunch…"**, Quit.

**"Update and relaunch…"** upgrades this machine in place. It asks GitHub for the latest
release, compares its published SHA-256 against the exe you are running (the version
*string* is identical across builds, so the digest is what decides), and if they differ it
offers the download. Nothing is installed until the transfer matches that digest — a
truncated or tampered download fails the update instead. Then it flushes and pushes every
repo, parks the running exe as `SincroGit.exe.old` (Windows won't overwrite a running
binary, but it will rename one), puts the new one at the same path and restarts into it.
The path never changes, so your start-at-login entry stays valid; the leftover `.old` is
deleted on the next start. A failed swap restores the working binary and restarts anyway —
you are never left without a daemon. Running from source there is no exe to replace and it
says so: use `git pull` + `build.ps1`.

---

## 5. Common tasks (recipes)

| I want to… | Do this |
|------------|---------|
| **Add a repo** | Panel → Status → *Add repo…* (or edit `repos:` in the config and *Save and restart*). |
| **Reuse a repo's settings on a second machine** | Add it via *Add repo…* — if you configured it elsewhere, tick *Use the settings saved from your other machine*. |
| **Change ONE repo's settings** | Select it → *Properties…* (branch, rhythms, sync, filters as a form). Or edit its entry in Advanced (YAML). |
| **Remove a repo from SincroGit** | *Properties…* → *Remove repo…* (config only; the git repo on disk is untouched). |
| **Get back an earlier version of a file** | Panel → *Time machine* → pin the file (double-click it) → pick a version → *Restore file*. Or `--history FILE`. |
| **Recover an old version WITHOUT overwriting** | *Time machine* → pick the version → *Save a copy…* → give it another name. |
| **Find when a text appeared/vanished** | *Time machine* → pin the file → type the text → *Find* (transitions highlighted). |
| **Get back SEVERAL files at once** | Panel → *Time machine* → *vs today* → pick a state → check the files → *Restore selected*. |
| **Check the whole setup is healthy** | `python -m sincrogit --doctor` (git, remotes, credentials, pandoc, AI, daemon). |
| **See if my other machines are backing up** | Panel → *Machines…* (stale mirrors show in red; *Fetch latest* refreshes). |
| **Roll the whole repo back** | Panel → *Time machine* → pick a state → *Restore ENTIRE repo…* (with a preview of what changes). |
| **Make a clean, documented commit now** | Per-repo **Commit…** button, or `--commit REPO`. |
| **Move my work to another machine** | Just **lock the screen / close the lid** — SincroGit flushes; on the other machine, unlock and it syncs. Or **Smart Commit** before leaving for an instant handoff. |
| **Leave knowing home will be fresh** | Nothing: lock (Win+L) and go. After ~20 min away the pending work is sealed (`sincro: [leave]`) and pushed; back sooner = no commit. Tune/disable with `seal_on_leave_min` (off in purist mode). |
| **Recover after a dead disk** | On another machine: `--autosnaps` (or panel → *Time machine* → *Fetch autosnaps*), then restore. Loses at most ~30 min. |
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
| `snapshot_interval_sec` | 300 | How often a snapshot lands on the side ref (the time-machine granularity). |
| `seal_interval_min` | 360 | How often a permanent commit is sealed (`inf` = purist: never auto-seal). |
| `autosnap` / `autosnap_interval_min` | true / 30 | Live mirror to the remote (disk-failure recovery + handoff). |
| `live_handoff` | auto | Pick up your other machine's live work: `auto` / `ask` / `off`. |
| `track_current_branch` | false | Follow the current branch instead of pausing off `branch`. |
| `push` / `pull` | true / true | Push sealed commits / periodic pull. |
| `extra_excludes` / `extra_includes` | — | Paths to skip / binaries to version anyway (e.g. `**/*.docx`, `**/*.pptx`). |
| `max_file_bytes` | 1048576 | Largest file auto-versioned (1 MB). |
| `suggest_excludes` | true | Suggest excluding a high-churn folder (Smart Ignore). |
| `suggest_commit` | true | Purist mode only: remind (once/day, at a quiet moment) to Smart Commit when un-sealed work piles up. |
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
are postponed, and the panel shows the repo as *Busy (merge/rebase)*. If *Busy* never
clears and no git command is actually running, a crash probably left a stale
`.git/index.lock` behind: the warning says so, and `--doctor` names the exact file —
delete it and syncing resumes. The rules of the road:

- **SincroGit never occupies your tip.** Snapshots live on a private side ref
  (`refs/sincro/wip/<branch>`) built through a private index: your `git log` shows only
  your commits and the seals, your staging area is yours, and `git status` tells the
  truth. Commit, branch, tag or rebase freely — a manual commit isn't even a special
  case anymore. (Repos coming from older SincroGit versions are migrated automatically
  on startup: the legacy WIP tip moves to the side ref and your unsealed edits reappear
  as ordinary uncommitted changes.)
- **Git clients (lazygit, Fork, GitKraken, VS Code, …):** fine alongside — they see a
  completely normal repository.
- **GitButler (`but`):** it *takes over* a repo (checks out its own
  `gitbutler/workspace` branch and blocks direct commits with a hook). With SincroGit's
  default branch guard this is handled: SincroGit **yields** while GitButler is active
  and resumes when you leave workspace mode (`but teardown` / checkout your branch).
  Do **not** combine it with `track_current_branch: true` on that repo — SincroGit would
  follow (and push) GitButler's workspace branch. In general: one tool *managing* a
  given repo at a time.
- **Dropbox / OneDrive / Drive:** never keep the repo inside another sync tool's folder —
  it can corrupt `.git` (see [README → Limitations](README.md#limitations)).
