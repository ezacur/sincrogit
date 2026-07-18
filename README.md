# ⏳g — SincroGit

SincroGit gives any repo an automatic, versioned **time machine** — without you ever
running `git`. Every few minutes it snapshots your **saved** files, every ~6 h it "seals"
a clean permanent commit, and it mirrors your latest state to the remote so your work
follows you between machines with near-zero effort.

**What it's actually good for** (no overselling):

- **A time machine for people/projects that won't commit by hand.** You broke or deleted
  something hours ago and only just noticed? Roll back to any earlier *saved* state — no
  discipline, no `git add` ever. Ideal for the busy/forgetful/learning developer.
- **Scratch & experiment repos.** Code that doesn't deserve a curated history but whose
  *trail* you'd hate to lose — spikes, tests, throwaways. Full recoverable history, zero
  ceremony. (Arguably its sweet spot.)
- **A safety net for AI coding agents.** An agent editing your repo is the ultimate
  "won't commit by hand" user: many files, fast, review after the fact. SincroGit gives
  you the trail and the rollback of everything it did — with **zero integration**, the
  agent doesn't change how it works (see
  [A safety net for AI coding agents](#a-safety-net-for-ai-coding-agents)).
- **Low-effort multi-machine continuity.** Move between your office and home machines and
  your work follows you — *delayed by minutes, not instant* (see
  [handoff](#cross-machine-handoff-live-wip)); a **Smart Commit** before you leave makes
  the handoff prompt.
- **(Rare bonus) survives a dead disk.** The remote mirror recovers your latest state
  (≤~30 min old) if the whole machine dies. It does **not** rescue unsaved editor buffers,
  and a power cut with an intact disk loses nothing either way — your saved files are
  already on disk; SincroGit's value there is the *rollback*, not the survival.

> **How to operate it** (CLI commands, panel actions, recipes): the
> **[User Manual](MANUAL.md)** (Spanish: [MANUAL_ES.md](MANUAL_ES.md)). New to Git or want
> the plain-language *when/why*? See **[GUIDE.md](GUIDE.md)** (Spanish: [GUIA.md](GUIA.md)).
> Design and decisions: **[DESIGN.md](DESIGN.md)**.

## Already fluent in Git? The skeptic's minute

A daemon that snapshots your repo and force-pushes refs *sounds* like something to keep
away from your repos — so here is, up front, exactly what it touches and what it never
touches:

- **It never occupies your tip.** Snapshots are commits built through a PRIVATE index
  and recorded on a side ref (`refs/sincro/wip/<branch>`) — your `git log` shows only
  your commits and the seals, your staging area is never touched, and `git status`
  keeps telling the truth about your worktree. Every git tool sees a normal repo.
- **Your commits are never rewritten.** Nothing of yours is amended, rebased away or
  dropped, and every snapshot stays recoverable through the side ref's reflog (≈30 days).
- **Your branch is never force-pushed.** It only ever receives sealed commits, always as
  a fast-forward. `--force` is used solely on SincroGit's own per-machine side refs
  (`refs/autosnap/<user>/<host>/<branch>`), where each machine is the sole writer.
- **It never merges or resolves anything on its own.** Divergent work or a rebase
  conflict → it stops, pauses that repo and notifies you; both states stay intact (see
  [Cross-machine handoff](#cross-machine-handoff-live-wip)).
- **Machine commits are labeled.** Every automatic seal carries the `sincro:` prefix —
  trivial to spot, squash or drop before a PR. And an auto-seal **yields while you have
  something staged** (it never absorbs a commit you're crafting by hand) and **restarts
  its clock from any commit you make yourself** — a manual `git commit` counts as the
  seal, so no `sincro:` checkpoint ever lands right on its heels.
- **You can have zero machine commits at all.** Purist mode (`seal_interval_min: inf`)
  keeps the branch 100 % yours — only your Smart Commits land on it — while the
  snapshots, the autosnap mirror and the cross-machine handoff keep running on the side
  ref (see [Pragmatic vs purist](#pragmatic-vs-purist-you-decide-what-a-commit-means)).

How each guarantee is implemented is documented in [DESIGN.md](DESIGN.md) §11. For how
SincroGit relates to jj, GitButler, dura and friends, see
[How it compares](#how-it-compares-with-neighboring-tools).

## Status: Phases 1, 2 and 4 complete (Phase 3, deployment: partial)

**Phase 1 (local core):**

- ✅ Filesystem watcher (`watchdog`) + *debounce*.
- ✅ **Snapshot** every 5 min: a commit on a private side ref (`refs/sincro/wip/…`),
  built through a private index — **invisible to `git log`/`git status`**, no commit
  pile-up on your branch, and your staging area is never touched.
- ✅ Initial snapshot on startup (captures pre-existing changes, e.g. after a reboot).
- ✅ **Sealing** every 6 h: commits the accumulated snapshot tree as ONE real commit on
  the branch (and re-anchors the snapshot chain there).
- ✅ **Filter**: only text < 1 MB is versioned automatically; binaries/large files by hand.
- ✅ **Opt-in binary versioning** (`extra_includes`, e.g. `**/*.docx`, `**/*.pptx`):
  version chosen binaries too — with **readable diffs for Word docs** (pandoc textconv
  driver) **and PowerPoint** (in-process `python-pptx` extractor)
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
- ✅ **Autosnap** (live mirror, every 30 min, only if changed): does a `push --force` of
  the shadow tip (the latest snapshot: sealed history + the live WIP) to a per-user/
  per-machine side ref `refs/autosnap/<user>/<host>/<branch>`
  so a total disk failure loses at most ~30 min. Doesn't touch the clean branch;
  cross-machine recovery from the CLI (`--autosnaps`) and the control panel.
- ✅ **Cross-machine handoff** (live WIP, decoupled from sealing): each sync picks up your
  *other* machine's live work and fast-forwards to it when that's loss-free; on divergence
  it never auto-merges — it notifies and leaves both intact for you to resolve. See
  [Cross-machine handoff](#cross-machine-handoff-live-wip). Toggle: `live_handoff`.
- ✅ **OS-event handoff** (Windows): locking/suspending **flushes** your latest state to the
  remote, unlocking/resuming **syncs** it — so "lock here → unlock there" hands off in
  seconds instead of waiting out the ~30 min mirror interval (+ a wall-clock-gap resume
  detector that also works headless). A **shutdown / restart / logoff** also flushes
  first — synchronously, holding the shutdown with a visible "backing up your work"
  reason while it runs. Every one of these leaves a `flush`/`resume` line in the Log.
- ✅ **Branch guard**: if you `git checkout` another branch, SincroGit yields that repo
  (no snapshot/seal/push on the wrong branch) until you switch back. Or set
  `track_current_branch: true` to **follow** the current branch instead (feature-branch
  workflow; snapshots/autosnap/handoff/push on whatever branch you're on).
- ✅ **Smart Ignore**: if one folder keeps churning out filtered files (build output,
  caches), SincroGit suggests once — a notification — adding it to `extra_excludes`. Never
  auto-edits. Toggle `suggest_excludes`.

**Phase 4 (system tray UI):**

- ✅ **System tray icon** with the hourglass+g brand mark. The
  **color reflects the state**: green=active, amber=paused, red=conflict, gray=stopped.
- ✅ Tray **menu**: open panel, pause/resume, sync now, seal now, quit.
- ✅ **Control panel** with tabs:
  - *Status*: repos table (branch, state, time since last seal, last action) with an
    **action bar for the selected repo** (Pause/Resume, Properties…, Commit…, Seal+Push,
    Fetch+Pull, Apply handoff — with a "working…" indicator while one runs — and a
    "How to fix…" helper when a conflict pauses the repo) and an **"Add repo…"** button
    (optionally drops a `* text=auto` **`.gitattributes`** so line endings stay consistent
    across machines). Repos can be added live, without restarting; **Properties…** edits
    one repo's settings as a form (only the changed fields are written — the rest keep
    inheriting the defaults) and can remove the repo from the config.
  - *Time machine*: per repo, a day-grouped rail of **every snapshot, seal and
    fetched mirror**, with a compare switch — *what changed then* (each state's files
    and diffs vs its parent) or *vs today* (what a restore would change, with selective
    and whole-repo restore) — and a **file pin** that turns the rail into that file's
    version history (search across versions, per-file restore, hunk restore).
  - *Log*: events **filterable by repo, action, level and text** (newest first, live).
  - *Settings*: friendly form over the global defaults (purist mode, handoff, AI, theme…).
  - *Advanced (YAML)*: raw `config.yaml` editor (save / save and restart).
- ✅ Desktop **notifications** (Windows toast via `winotify`; the tray app also surfaces
  them through Qt tray balloons) on conflicts, handoffs and errors.
- ✅ **File history / restore** ("time machine"): browse a file's past versions
  (sealed commits + reflog snapshots + fetched autosnap states), see a **colored diff**
  vs the current file, and restore **just that file or the whole repo** — from the CLI
  (`--history`, `--autosnaps`) and the control panel. A whole-repo restore first shows a
  **preview** of exactly what would change (and flags any file whose content snapshots
  can't capture) before asking for confirmation. A text file can also be restored
  **hunk by hunk** — tick only the changed blocks you want back and keep the rest of your
  current edits. Versions are **searchable** ("when did this function change?") and any
  version can be **saved as a copy** under another name — recovery without overwriting
  anything.
- ✅ **Time machine explorer**: the same history navigated **by version** — pick a point
  in the repo's timeline, see every file that differs from the present (with its diff,
  **unified or side-by-side, with intra-line highlighting**), check the ones you want and
  **restore the selected set** in one atomic, snapshot-captured step.
- ✅ **My machines view**: each machine's last autosnap mirror per repo, freshness
  color-coded — at a glance, whether your other machine is still backing itself up.

Pending (Phase 3): see the [TODO](#todo) below.

## TODO

In priority order:

1. **Frictionless onboarding for Git newcomers.** The audience that needs SincroGit most
   is the least equipped to create a remote and wire up credentials — today that setup is
   the real entry barrier, not the daemon. **In place now:** the "Add repo…" dialog takes
   a remote URL and **verifies it end to end** (reachability + a dry-run push for write
   access) before adding the repo, and pre-fills an already-configured remote. Its
   companion piece, the **`--doctor` health check**, does the same checks for existing
   repos (git present, remote reachable, credentials verified, pandoc, AI backends,
   daemon). Still planned: creating a brand-new private remote (GitHub/GitLab) from inside
   the dialog, for users who don't have one yet.
2. **Start at log-on, automatically** (the missing Phase-3 piece). The "zero discipline"
   promise breaks if you have to remember to launch the safety net: a first-run prompt
   (or installer step) should register the Windows scheduled task
   (`SincroGit.exe --tray` / `pythonw.exe -m sincrogit --tray` at log-on — see
   [DESIGN.md §9](DESIGN.md)).
3. **`sincrogit status` command** (the tray menu already covers the common actions).

### TODO — AI messages (the aicommit2-inspired batch)

Adopted after surveying [aicommit2](https://github.com/tak-bro/aicommit2), keeping
SincroGit's three contracts intact: the commit/seal is never blocked by an AI failure,
privacy by default (`cloud_send_content`), and no new dependencies (`ai.py` stays
stdlib-`urllib`).

- **Generic OpenAI-compatible endpoint** (`ai.cloud_provider: compatible` +
  `ai.cloud_url`): one extra HTTP client covers OpenRouter, DeepSeek, Together,
  LM Studio, llama.cpp, Anthropic… instead of maintaining a bespoke client per
  provider. API keys stay in environment variables (never in the YAML).
- ✅ **Messages in the user's language** — *done*, as **`ai.language`** (`en` | `es`): a
  prompt-level parameter, so seal and Smart Commit messages come out in Spanish with
  `ai.language: es`. (Extending it to arbitrary locales is the only part left.)
- **Per-repo `ai:` overrides**: e.g. a sensitive repo pinned to `mode: local`
  (Ollama-only) while the rest may use the cloud — consistent with the existing
  per-repo defaults.

### TODO — the lazygit-inspired batch

From surveying [lazygit](https://github.com/jesseduffield/lazygit) — the natural cockpit
to use *alongside* SincroGit (complement, not donor: we deliberately don't rebuild a git
client in the panel):

- **Partial Smart Commit.** A file checkbox list in the Smart Commit dialog (the
  file-level cousin of lazygit's interactive staging): commit the selected files as the
  curated commit and return the rest to the recreated WIP — so one work window can be
  split into logical commits. Hunk/line granularity stays lazygit's job. Bonus while in
  there: an optional `commit_prefix` (regex on the branch name → message prefix, e.g.
  `feature/AB-123` → `[AB-123]`) applied to Smart Commit proposals.
- **lazygit `customCommands` snippets (docs-only).** A MANUAL recipe to drive SincroGit
  from inside lazygit (`--commit REPO -y`, `--apply-handoff REPO`, inspecting
  `refs/autosnap/...`). *(The "sharing the repo with other git tools" note — don't
  reword the WIP, GitButler hand-off rules — already exists:
  [MANUAL §9](MANUAL.md#9-sharing-the-repo-with-other-git-tools).)*

### TODO — technical (for developers)

- **Automated test suite — EXISTS** (`tests/`, pytest, ~90 tests, ~1 min): the restore
  refusals and the rename-safe restore, the selective restore / timeline / export /
  history search, config-file surgery, `--doctor`, the long-busy warning and state
  precedence, diff rendering, the GUI dialogs offscreen — **and the multi-machine paths
  against throwaway bare remotes**: `work_relationship` classification (all four
  verdicts), handoff fast-forward (auto + ask + re-validation), the uncaptured-content
  refusal, handoff across a rename, both rebase-conflict shapes (abort + pause), the
  rejected-push reconcile loop, seal/push idempotence, and autosnap-ref pruning. Run it
  with `pip install -e .[dev]` then `pytest`. **Still missing**: CI to run it all on
  every push.

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
| `--apply-handoff REPO` | apply your other machine's pending live work to REPO (cross-machine handoff) |
| `--doctor` | health check: git, config, each repo's branch/remote/credentials (dry-run push), pandoc, AI backends, daemon. Exit 0 = healthy |
| `--force` | run a one-shot even while the daemon is running (by default they refuse, to avoid racing its git work — see the [Manual](MANUAL.md)) |

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

### Versioning Word documents (.docx) and PowerPoint (.pptx)

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

**What counts as a change.** Because the change-detection runs through pandoc, a `.docx`
is versioned/synced **only when its markdown content changes** — text edits and structural
formatting (bold, italics, headings, lists, tables, links) count; purely visual styling
(font, color, size, alignment, layout) and Word's internal resave churn (timestamps,
revision IDs) do **not**, so they aren't versioned or backed up until a content edit
includes them. After a styling-only session, force a version with a manual **Smart
Commit**. (Without pandoc, detection falls back to bytes, so every save is a version.)

**PowerPoint (.pptx).** Same opt-in (`extra_includes: ["**/*.pptx"]`), different
converter: an **in-process extractor** over `python-pptx` (`pip install python-pptx`;
bundled in the exe) renders each version as markdown — slide titles, bullets with
their indent levels, tables and speaker notes — so the **preview, intra-line diff,
history search and "Save a copy"** all work on presentations. Honest differences vs
`.docx`: there is no git textconv driver (that needs an external executable), so
(a) `git diff` and the AI seal messages still see the `.pptx` as binary (`--stat`), and
(b) change detection is **by bytes, not by content** — every PowerPoint resave counts
as a version. Without `python-pptx` it degrades to an opaque blob, like `.docx`
without pandoc; `--doctor` checks for it when a repo versions `.pptx`.

> 📌 **Possible, not yet implemented:** the same mechanisms could cover more formats —
> Jupyter notebooks (`.ipynb` via `jupytext`/`nbconvert`), spreadsheets (`.xlsx` via
> `in2csv` or an openpyxl extractor like the `.pptx` one) — driven by a configurable
> `pattern → converter` map. Caveat to keep in mind: a readable diff doesn't fix the
> *repo size* — a `.ipynb` would still store the full JSON (outputs); real notebook
> hygiene also needs a clean filter (e.g. `nbstripout`).

### Restore a past version (time machine)

Browse and restore previous versions of a file, combining sealed commits
(permanent), intra-window snapshots (from the reflog, ~30 days) and other machines'
autosnap states (after "Fetch autosnaps"):

```powershell
# Interactive: lists versions and asks which one to restore
python -m sincrogit -c config.yaml --history path\to\file.py

# Non-interactive: restore version N directly
python -m sincrogit -c config.yaml --history path\to\file.py --pick 3

# Disaster recovery: fetch & list every machine's autosnap states
python -m sincrogit -c config.yaml --autosnaps
```

In the tray app, the same is available from the control panel:
the **Time machine tab** (pin the file: browse, preview any version, and restore a file or the
whole repo).

Restoring is itself protected: pending edits are first snapshotted into the WIP (so
nothing saved since the last snapshot can be lost), and the restored state is captured
as a new snapshot — a restore is always reversible, back to the moment right before it.
And if a file's *current* content is something snapshots can't capture (excluded, over
the size limit, binary — it exists nowhere in git), the restore **refuses** instead of
destroying it, telling you which files to copy somewhere safe first — the same policy
the cross-machine handoff applies.

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
branch:  ... ── sealed_N ──────────────────── sealed_N+1     ← HEAD (yours + seals only)
                    │                              ▲
shadow:             └── s1 ── s2 ── … ── s42 ──────┘   refs/sincro/wip/<branch>
                       (one snapshot commit every ~5 min, built through a
                        PRIVATE index — invisible to git log/status)
every 6h: the accumulated snapshot tree becomes ONE sealed commit on the branch
```

- **Undo a recent mistake**: the snapshots are real commits on the side ref
  (≈5 min resolution) — browse/restore them from the Time machine tab.
- **Go back to yesterday**: `git checkout`/`restore` from the matching sealed commit.
- **Total disk failure**: recover on another machine from the `autosnap` ref (≤30 min).

## Design notes & trade-offs

SincroGit is intentionally not "pure" Git. Git assumes each commit is a curated
logical change; SincroGit instead optimizes for a single developer who forgets to
commit and moves between machines. The deliberate trade-offs:

- **Time-boxed seals, not logical commits.** Automatic seals (`sincro:`) bundle
  whatever changed in a ~6 h window — a timeline, not atomic units. When you want a
  curated commit, use **Smart Commit** (AI-proposed Conventional Commits message).
  The `sincro:` prefix keeps machine commits and yours easy to tell apart.
- **The snapshot chain is a continuous "save button" — for *saved* files.** A commit
  lands on the side ref every ~5 min, so any earlier saved state is recoverable (≈5 min
  resolution). It snapshots what's on disk, **not** your editor's unsaved buffer — so its
  value is the *rollback*, not surviving a crash (a power cut with an intact disk loses
  nothing regardless; saved files are already on disk).
- **Backup is decoupled from history.** `autosnap` force-pushes the live state to a
  per-machine side ref every ~30 min, while `main` stays clean (only sealed commits) → the
  other machine's pull is always a clean fast-forward. This serves both the rare
  disk-failure recovery and the cross-machine handoff.

The cost we accept: history reads as time-buckets rather than perfectly atomic commits;
rollback resolution is ~5 min (the snapshot cadence); and a total disk failure can lose up
to ~30 min (the autosnap cadence) — in exchange for an effortless versioned time machine
and low-effort multi-machine continuity.

### Pragmatic vs purist: you decide what a commit means

Purist Git says a commit should narrate a **logical unit of work** (a feature, a fix),
**not the passage of time**. The permanent history is for a human to read later and
understand *why* the code changed. SincroGit ships *pragmatic* by default — it seals on
a clock so the forgetful get a clean-ish history for free — but you can flip it to
*purist* and keep the exact same safety net underneath:

- **Pragmatic (default).** Auto-seal every 6 h: the machine writes your timeline, you do
  nothing. Best for solo work where you don't care about perfectly atomic commits.
- **Purist.** Set `seal_interval_min: inf` (see *[Disabling an interval or limit](#disabling-an-interval-or-limit)*).
  The automatic seal never fires, so the branch stays **immaculate** — every permanent
  commit is one *you* made, when a task is actually done, via **Smart Commit**
  (AI-proposed Conventional Commits). The WIP and `autosnap` still run underneath, so you
  keep the time machine and disk-failure recovery. This is "almost pure Git" with an
  invisible safety net — a history presentable even alongside a team.
  Purist mode's one catch — a branch that silently stagnates if you forget to Smart
  Commit — is covered by an opt-out reminder (`suggest_commit`, on by default): when
  un-sealed work has piled up and the repo goes **quiet** (you seem to have finished
  something) and it's been a while since your last permanent commit, SincroGit nudges you
  **once** (at most daily) to Smart Commit. Your work is backed up regardless — the nudge
  is only about keeping your *branch history* current.

  > **Note:** even in purist mode you still get automatic machine-to-machine continuity,
  > because the **[live handoff](#cross-machine-handoff-live-wip)** works off the WIP, not
  > the seal. So the branch stays immaculate *and* your laptop still picks up your
  > desktop's latest work on its own.

### Cross-machine handoff (live WIP)

Your machines hand work off **automatically**, decoupled from sealing: each pushes its
live WIP to a personal side ref `refs/autosnap/<you>/<host>/<branch>` (the `<you>` comes
from your `git config user.email`, so a machine recognizes its *own* other machines vs. a
teammate's). On every sync, SincroGit fetches your other machines' mirrors and:

- **Safe fast-forward (the common case).** If the other machine's work *contains all of
  yours* (typically: you did nothing here since you left), it's a loss-free fast-forward (it
  only ever discards an empty WIP, reversible via the reflog) — and it's **skipped** (you're
  notified instead) if it would overwrite an untracked file **or** if you have local edits
  auto-snapshot can't capture (a tracked file now too large / binary / excluded): those
  exist nowhere in git, so applying would destroy them — commit them by hand first. What
  happens otherwise depends on `live_handoff`:
  - **`auto`** (default): applied automatically — you just sit down and continue. It is
    **never silent**: you get a tray notification ("caught up to *Desktop-PC*"), so you
    always know your files moved.
  - **`ask`**: nothing is touched. You get a notification and the panel shows an **Apply
    handoff** button (or run `--apply-handoff REPO`); it re-checks it's still safe, then
    fast-forwards. Good if you'd rather confirm before your working tree changes.
  - **`off`**/`false`: handoff is disabled (manual only, as below).
- **Divergence → you decide (no auto-merge, any mode).** If *both* machines changed work the
  other doesn't have, SincroGit **does not merge** them. This is deliberate: silently 3-way
  merging two piles of unreviewed in-progress work is exactly how you'd get a subtly broken
  tree. It notifies you (once) and leaves **both** states intact.

> ⚠️ **What's intentionally missing: automatic merging.** On divergence you resolve it,
> your way:
>
> **Easiest (recommended) — seal one side, let the other rebase:**
> 1. On the machine whose work you want as the base, do a **Smart Commit** (turns its WIP
>    into a real commit and pushes it).
> 2. The other machine's normal pull rebases your WIP onto it. Non-overlapping → clean and
>    automatic; overlapping → a normal rebase conflict (SincroGit pauses; you resolve in
>    your editor and hit **Resume**).
>
> **Full control — inspect/merge by hand** (the peer's live state is at the side ref; find
> exact names in the panel's *Fetch autosnaps* or `--autosnaps`):
> ```bash
> git log  --oneline  refs/autosnap/<you>/<other-host>/<branch>   # what they have
> git diff HEAD       refs/autosnap/<you>/<other-host>/<branch>   # compare
> git reset --hard    refs/autosnap/<you>/<other-host>/<branch>   # take theirs (yours -> reflog)
> git merge           refs/autosnap/<you>/<other-host>/<branch>   # or merge both, resolve conflicts
> ```

Set `live_handoff` per repo to `auto` (default), `ask`, or `off`. It needs `autosnap` on
(that's what publishes the mirror your other machine reads).

**Made prompt by OS events (Windows).** Rather than wait out the intervals, SincroGit hooks
the session: **locking the screen or suspending** (you're leaving) **flushes** your latest
state to the remote immediately, and **unlocking or resuming** (you've arrived) **syncs** it
immediately. So "lock here → unlock there" hands off in seconds. (A long suspend that cuts
the network mid-flush falls back to the next autosnap; a wall-clock-gap detector also forces
a sync after any resume, so it works headless too for the wake side.) **Shutting down,
restarting or logging off** flushes too: the tray app catches the session-end message and
pushes your latest state synchronously before letting Windows proceed — with a visible
"SincroGit: backing up your latest work" reason on the shutdown screen while it runs
(bounded to ~20 s, so it can never hold your shutdown hostage).

### Using it in a team (shared repos)

SincroGit defaults to a **personal, single-branch** tool. Pointed straight at a **shared**
branch (everyone pushing to `main`/`develop`), the periodic auto-rebase hits a conflict the
moment a teammate's push touches your files — and SincroGit, never destructive, **pauses and
asks you to resolve it in the terminal**. That recurring interruption defeats the "forget the
terminal" promise, so **don't run it on a branch other people push to.** The team-friendly
setup is one flag:

1. **`track_current_branch: true`** — SincroGit follows whatever branch you're on instead of
   pausing off the configured one.
2. Work on **your own branch** (`feature/login-pepe`). SincroGit backs it up and hands it off
   between *your* machines invisibly: the live-WIP mirrors are namespaced **per user**
   (`refs/autosnap/<you>/<host>/<branch>`), so they never touch your teammates' branches or
   their drafts, and theirs never touch yours (**team-safe**).
3. When a unit of work is done, hit **Smart Commit** (AI-proposed Conventional Commits
   message) and open a **Pull Request** to the shared branch — a normal, reviewed merge.

> **Cleanest combo:** add **`seal_interval_min: inf`** (purist mode). Then SincroGit makes
> **no** automatic commits on your branch — every permanent commit is one *you* made via Smart
> Commit — while the WIP + autosnap keep protecting and syncing you underneath. The branch
> history looks hand-crafted; nobody can tell a safety net was running.

**You never lose the ability to commit by logical units.** The "time bucket" is only the
*automatic floor*, not a ceiling: at any moment you can seal a finished task yourself with
**Smart Commit** (the tool drafts the message for you). And automatic commits always carry the
**`sincro:`** prefix, so machine snapshots and your real commits stay trivially distinguishable
— squash or drop the `sincro:` ones before a PR, or just run purist mode so there are none.

It's still sequential **per branch** (one machine at a time on a given branch); it doesn't
merge two people editing the *same* branch at once.

### A safety net for AI coding agents

An AI coding agent (Claude Code, Cursor, Codex, …) is the ultimate "won't commit by
hand" user: it changes many files, fast, and review happens after the fact. Run
SincroGit on the repos your agents work in and you get, with **zero integration**:

- **A full trail — turn the resolution up.** For an agent, the default 5 min bucket is
  coarse: it can rewrite half the repo in that window. Turn **both** knobs down on that
  repo. With continuous writes, snapshots land every `snapshot_interval_sec` to 2× that
  (the anti-starvation cap), and `debounce_sec` aligns each snapshot to the end of an
  agent's write burst:

  ```yaml
  repos:
    - path: "C:/work/agent-playground"
      snapshot_interval_sec: 30   # a rollback point every ~30-60 s of agent churn
      debounce_sec: 5             # agents write in bursts; settle fast
  ```

  Snapshots are local side-ref commits (no network), so the finer cadence costs nothing
  remotely — the daily `git gc` packs the extra loose objects. You can push it to near
  per-burst (`snapshot_interval_sec: 5`; the floor is really the debounce) at the cost
  of a longer snapshot chain. See [Tuning a "hot" repo](#tuning-a-hot-repo).
- **Rollback of anything it did.** A bad agent edit from an hour ago? *Time machine* →
  restore the file (or the whole repo) to right before it happened.
- **A clean separation of your work.** Run **purist mode** (`seal_interval_min: inf`):
  the permanent history stays 100 % yours (your Smart Commits), while the WIP + autosnap
  silently record everything the agent does in between. Or stay pragmatic and let the
  `sincro:` seals timestamp the agent's progress every few hours.

The emerging agent-VC tools (e.g. GitButler's agent skills) work by teaching the agent
their own commands instead of git. SincroGit takes the opposite approach: it is
**invisible** — nothing to install in the agent, nothing the agent must do differently —
so it works with any agent, today's or next year's. Honest caveat: it records a
*timeline*, not an audit log. It doesn't attribute changes to "agent vs. you" beyond
what your own Smart Commits separate, and states *between* snapshots aren't captured —
the window is whatever you tune (~5 min by default, ~30-60 s with the profile above).

## How it compares with neighboring tools

Plenty of tools auto-commit a repo; none combines SincroGit's pieces. Survey **as of
June 2026** (activity changes — treat the notes as a snapshot). Legend: ✅ yes ·
➖ partial · ❌ no.

| Tool | Snapshots without commit pile-up | Set-and-forget daemon | AI commit messages | Cross-machine WIP handoff | Never auto-merges / force-pushes your branch | Time-machine GUI |
|------|------|------|------|------|------|------|
| **SincroGit** | ✅ shadow side ref (invisible to log/status) | ✅ tray daemon | ✅ auto-seal + Smart Commit (Ollama → Gemini → fallback) | ✅ per-machine refs + OS lock/unlock triggers | ✅ | ✅ per-file, tray app |
| [jujutsu (jj)](https://github.com/jj-vcs/jj) | ✅ same model (working copy = one amended commit) | ➖ on-save, via watchman trigger | ❌ (external tools) | ❌ local-only | ✅ | ❌ CLI (`jj op restore`) |
| [GitButler](https://github.com/gitbutlerapp/gitbutler) | ➖ oplog snapshots around operations | ➖ desktop app | ✅ interactive (Ollama/OpenAI/Anthropic) | ❌ | ➖ force-pushes its virtual branches | ➖ project-level restore, desktop GUI |
| [dura](https://github.com/tkellogg/dura) | ❌ commit per change (shadow branches) | ✅ daemon | ❌ | ❌ (no remote) | ✅ (never pushes) | ❌ |
| [gitwatch](https://github.com/gitwatch/gitwatch) | ❌ commit per change, on your branch | ✅ daemon | ❌ | ➖ pushes your branch | ❌ | ❌ |
| [git-wip](https://github.com/bartman/git-wip) | ❌ stacked commits on `refs/wip/*` | ❌ editor save hooks | ❌ | ❌ | ✅ | ❌ |
| [GitDoc](https://github.com/lostintangent/gitdoc) | ➖ interval commits (optional squash), on your branch | ➖ VS Code-bound | ➖ Copilot only | ➖ same-branch auto-push/pull | ❌ (auto-pulls) | ❌ |
| [aicommit2](https://github.com/tak-bro/aicommit2) | — | ❌ (interactive CLI) | ✅ multi-provider incl. Ollama | ❌ | — | ❌ |
| [git-annex assistant](https://git-annex.branchable.com/) | ❌ commit per change | ✅ daemon | ❌ | ➖ shared `synced/*` refs, auto-merge | ❌ (auto-merges) | ➖ webapp |
| [SparkleShare](https://github.com/hbons/SparkleShare) | ❌ commit per change | ✅ tray daemon | ❌ | ➖ shared branch, auto-merge | ❌ | ➖ tray + restore (Windows build long abandoned) |
| [Obsidian Git](https://github.com/Vinzent03/obsidian-git) | ❌ interval commits | ➖ Obsidian vaults only | ❌ (templates) | ➖ shared branch, auto-pull/merge | ❌ | ➖ in-app file history |
| [git-sync (simonthum)](https://github.com/simonthum/git-sync) | ❌ commit per run | ❌ script/hook | ❌ | ➖ shared branch, auto-rebase | ❌ | ❌ |

What the survey says: every column has at least a partial precedent somewhere, but no
tool combines them — and two pieces had **no equivalent anywhere we looked**:
per-user/per-machine handoff refs with never-auto-merge semantics, and sync triggered by
OS lock/unlock/suspend events. The crowded spaces are interval auto-commit (many tools,
mostly stagnant) and interactive AI commit messages (many, very active); the empty space
is the handoff mechanics.

### jj (jujutsu): the closest relative

[jj](https://github.com/jj-vcs/jj) deserves its own note: it is the closest relative in
spirit — the working copy is continuously captured as commits, no manual staging — and
its `jj op log` / `jj op restore` is a true local time machine. (SincroGit keeps those
captures on an invisible side ref, so unlike jj your repo's git surface is untouched.)
The difference is scope and direction:

- **jj is a VCS you adopt.** A new CLI and a new mental model (it coexists with git
  remotes, but *you* stop typing `git`). Its safety net is local-only: no remote mirror,
  no machine-to-machine handoff, no AI messages, no GUI; snapshots fire on jj commands /
  watchman events, not on a wall clock or session events.
- **SincroGit is an overlay you don't have to learn.** Your repo stays plain git and your
  habits stay untouched; what it adds is exactly what jj doesn't carry — the remote
  autosnap mirror, the cross-machine handoff (incl. lock/unlock triggers), AI seal
  messages and the tray/time-machine UI.

If you're happy to switch tools, jj is excellent and more deeply integrated. If you want
to keep plain git — or you need the multi-machine continuity — that's SincroGit's lane.

## Limitations

SincroGit has a deliberately narrow scope. What it does **not** do:

- **It versions *saved* files, not unsaved buffers.** A power cut/crash with an intact
  disk loses nothing either way (your saved files are on disk); SincroGit's value there is
  the *rollback* to an earlier saved state, not crash survival. It does **not** rescue
  work you never saved — that's your editor's autosave.
- **Multi-machine sync isn't real-time, but it's usually seconds.** On Windows, locking
  the screen / closing the lid **flushes** your latest state to the remote at once, and
  unlocking / waking the other machine **syncs** it at once — so the normal "lock here,
  unlock there" flow hands off in **seconds** (see [Cross-machine handoff](#cross-machine-handoff-live-wip)).
  If you just walk away **without** locking, it falls back to the periodic mirror
  (`autosnap_interval_min` ~30 min) + pull (`pull_interval_min` ~10 min) — up to ~40 min.
  A **Smart Commit** before you switch is always instant (it goes via the branch). It is
  never a real-time, keystroke-level sync.
- **Sequential, not concurrent.** It assumes one machine at a time. Simultaneous edits on
  two machines are not merged — the rebase is aborted and the repo paused for you to
  resolve by hand. It's a personal tool; for team repos use your own branch (see
  [Using it in a team](#using-it-in-a-team-shared-repos)), not a shared one.
- **Text only, < 1 MB.** Binaries and large files are never auto-committed; add those by
  hand. It is not a full backup of the folder.
- **Time-bucket history.** `sincro:` seals group ~6 h of unrelated changes, so a
  `git bisect`/`revert` of one logical change is harder than on curated history (use
  **Smart Commit** when you want a clean, logical commit).
- **Rollback resolution / disk-failure window aren't zero.** You can roll back to ~5 min
  resolution (the snapshot cadence); a **total disk failure** loses up to ~30 min (the last
  autosnap on the remote) — a rare event, and the only case where "files on disk" doesn't
  already cover you.
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

**Rebuilding while the daemon runs is handled for you:** if the exe being built is the
one currently running, `build.ps1` asks it (via the localhost control port) to **flush
every repo** (snapshot + autosnap push) and exit cleanly, waits, builds, and **relaunches
it** — so a rebuild never loses work and never leaves the daemon dead. An older daemon
that doesn't know the command gets a forced kill (noted in the output); a SincroGit
running from a *different* path is left alone.

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
| `snapshot_interval_sec` | 300 | How often a snapshot lands on the side ref (5 min) |
| `debounce_sec` | 25 | Wait after the last change before snapshotting |
| `seal_interval_min` | 360 | How often a permanent commit is sealed (6 h). `inf`/`off` = purist mode (commit by hand) |
| `pull_interval_min` | 10 | How often to fetch; pull (rebase) only if the remote is ahead (10 min) |
| `autosnap` | true | Live mirror of the latest snapshot to `refs/autosnap/<user>/<host>/<branch>` (disk-failure recovery + handoff) |
| `autosnap_interval_min` | 30 | How often the mirror is force-pushed (only if it changed) |
| `live_handoff` | auto | Pick up your other machine's live WIP: `auto` (fast-forward + notify), `ask` (one-click apply), `off`. See [Cross-machine handoff](#cross-machine-handoff-live-wip) |
| `track_current_branch` | false | Follow the **current** branch instead of pausing off `branch` (feature-branch workflow; pairs with purist mode). Opt-in |
| `suggest_excludes` | true | Suggest (once, a notification) adding a high-churn folder to `extra_excludes` — never auto-edits |
| `suggest_commit` | true | **Purist mode only:** nudge (once/day, at a quiet moment) to Smart Commit when un-sealed work piles up on a stagnant branch. Ignored when auto-seal is on |
| `max_file_bytes` | 1048576 | Maximum file size to version (1 MB) |
| `extra_excludes` | — | `.gitignore`-style patterns to exclude |
| `extra_includes` | — | patterns versioned even if binary (e.g. `**/*.docx`, `**/*.pptx`) |
| `max_include_bytes` | 26214400 | size cap (25 MB) for `extra_includes` |
| `push` | true | Push sealed commits to the remote |
| `pull` | true | Periodic pull (rebase) from the remote |
| `git_timeout_sec` | 60 | Timeout for network git ops (fetch/push) |
| `pandoc_path` | `pandoc` | **(top-level)** path to pandoc for readable `.docx` diffs |
| `theme` | `auto` | **(top-level)** GUI theme: `auto` (follow Windows), `light`, `dark` |

The `ai:` section (message generation — top-level, not per-repo; the API key stays
in an env var, never in the file):

| Key | Default | Meaning |
|-----|---------|---------|
| `ai.mode` | `hybrid` | `hybrid` (Ollama → cloud → deterministic), `local`, `cloud`, `none` |
| `ai.cloud_provider` | `gemini` | Cloud provider |
| `ai.cloud_model` | `gemini-2.5-flash-lite` | Cloud model |
| `ai.cloud_send_content` | false | If false, only `--stat` + filenames go to the cloud (privacy) |
| `ai.language` | `en` | Commit-message language (`en` / `es`) |
| `ai.ollama_url` | `http://localhost:11434` | Local Ollama endpoint |
| `ai.ollama_model` | `llama3.2` | Local Ollama model |
| `ai.timeout_sec` | 30 | Per-provider AI request timeout |
| `ai.max_diff_chars` | 6000 | Max diff characters sent to the model |
| `ai.api_key_env` | `SINCROGIT_GEMINI_KEY` | Env var holding the cloud API key |

Values are **validated at load**: numeric fields accept numbers or numeric strings
(`"300"`), and booleans, negatives or garbage fail at startup with a clear per-field
error — never as a crash inside the engine hours later.

### Disabling an interval or limit

Any interval or size threshold can be **turned off** by setting it to `inf` (or `off`,
`none`, `never`): the action then **never fires** and the limit becomes **unlimited**.
It works for `snapshot_interval_sec`, `seal_interval_min`, `pull_interval_min`,
`autosnap_interval_min`, `debounce_sec`, `max_file_bytes` and `max_include_bytes`. The
headline use is **purist mode**: `seal_interval_min: inf` (no automatic seal — you commit
by hand). For example:

```yaml
defaults:
  seal_interval_min: inf     # purist mode: never auto-seal (commit manually)
  pull_interval_min: off     # don't pull automatically
```

> ⚠️ **Disabling a *size* limit is dangerous.** `max_file_bytes: inf` (or
> `max_include_bytes: inf`) removes the size guard entirely, so SincroGit may
> auto-commit **huge files** — multi-GB binaries, build outputs, datasets — and Git
> keeps **every version forever**, bloating the repo irreversibly. Prefer a high
> explicit number (e.g. `max_file_bytes: 10485760` for 10 MB) over `inf` unless you
> truly mean "no limit at all".

### Tuning a "hot" repo

Every key under `defaults:` can be **overridden per repo**, so you can keep relaxed
defaults everywhere and make just one repo "hot" — versioned more finely and mirrored more
often — without hammering the network for all of them:

```yaml
defaults:
  snapshot_interval_sec: 300      # 5 min — relaxed for most repos
  autosnap_interval_min: 30       # 30 min mirror

repos:
  - path: "C:/work/the-big-deadline"   # the hot one
    snapshot_interval_sec: 120         # finer time machine (2 min)
    autosnap_interval_min: 10          # smaller disk-failure window (10 min)
  - path: "C:/work/side-project"       # stays on the relaxed defaults
```

What "hot" buys you: a **finer time machine** (smaller `snapshot_interval_sec`, local and
cheap) and a **smaller disk-failure window** (smaller `autosnap_interval_min`). What it
costs: more force-pushes (and remote orphan objects) for *that* repo while you're actively
editing it — the loop stays idle when nothing changes, so a hot repo costs nothing when
you're not touching it. Note you **don't** need this for machine-to-machine handoff: the
[OS-event handoff](#cross-machine-handoff-live-wip) already makes that prompt regardless of
the interval — "hot" is for finer undo and a tighter disk-failure RPO.
