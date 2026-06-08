# SincroGit — Design document

> An automatic, versioned time machine for your repos (and low-effort multi-machine
> continuity), with **zero** Git discipline required.
> Target platform: **Windows** (interactive use, one machine at a time).

---

## 1. Goals and scope

**I want two things at once:**

1. **Versioning + a time machine, with zero discipline.** Roll back any *saved* file to an earlier state (you broke/deleted/overwrote something) or to yesterday — without ever running `git`. (It snapshots what's on disk, not unsaved editor buffers; a power cut with an intact disk loses nothing regardless — the value is the rollback, not crash survival. A *total disk failure* is the one case the remote mirror covers, and it's rare.)
2. **Sync between machines (sequential).** I work almost always on the desktop and occasionally on the laptop, **never both at once**. When switching machines, I want the sources to update automatically (within minutes — see §4.2; not instant).

**Out of scope (for now):**

- Simultaneous editing on two machines / complex conflict resolution.
- Versioning binaries or large files (those I commit/push **by hand**).
- Any OS other than Windows. On Linux/others I'll `pull` by hand and won't edit.

---

## 2. Conceptual model: two tiers

The trick to reconcile *"almost instant snapshot"* with *"I don't want thousands of commits"* is to separate two tiers:

| Tier | What it is | Frequency | Visible in history |
|------|-----------|-----------|--------------------|
| **WIP (snapshot)** | A **single** commit at the tip (`HEAD`) that is **amend**ed with the current state | Every ~5 min (with debounce) | No (it's transient, it gets sealed or rewritten) |
| **Sealed (history)** | The WIP is "frozen" with a descriptive AI message and a new WIP is created on top | Every ~6 h | Yes (permanent commit) |

```
... ── sealed_N ── WIP        ← HEAD (amended every ~5 min)
                    │
       every 6h ────┘ it gets sealed (reword with AI message) and a new WIP is born on top

result: ... ── sealed_N ── sealed_N+1 ── WIP(new) ← HEAD
```

**Why it works:**

- The current saved state is committed every ~5 min → a **rollback point** at ~5 min resolution (`HEAD` = last snapshot, earlier ones in the reflog). NB: this is *not* power-cut protection — saved files survive a power cut on the disk anyway, and unsaved buffers are never captured; the value is the time machine.
- Because we `amend`, hundreds of commits don't pile up: only **~4 commits/day** (one every 6 h).
- The "clean" history (sealed) is the only thing that travels to the remote → **pull always clean, no force-push** (see §4).

> **Fine-grained safety net:** each `amend` leaves the previous snapshot as an *unreachable* commit in the **reflog** (≈30 days by default). That is, even though the visible history only has 1 commit per window, internally you can recover intermediate states with `git reflog`. *(Optional, see §12: an `autosnap` branch with real commits every ~5 min if you want browsable intra-window history.)*

---

## 3. Detailed flow

### 3.1 Service startup (per repo)
1. Validate: it's a git repo, the configured remote and branch exist.
2. **`git pull --rebase --autostash`** to bring in what the other machine left.
   - If there is an unpushed local WIP (typical case after a crash) → it is **rebased** on top of the remote.
   - **If there is a conflict** → `git rebase --abort`, the **autosync for that repo is paused**, the user is notified and it is logged. It is **never** resolved destructively, nor is force used. (This is rare in sequential use, but the policy is: when in doubt, don't lose data.)
3. Ensure a WIP exists at the tip (if not, create an empty one).
4. Start the watcher.

### 3.2 Snapshot cycle (every ~5 min, with debounce)
- The **watcher** (filesystem events) marks the repo as *dirty* and resets a debounce (e.g. 20-30 s without changes).
- When the debounce settles **and** ≥5 min has passed since the last snapshot:
  1. Compute candidate files and apply the **filter** (§5).
  2. `git add <only the candidates>`.
  3. If something is staged: `git commit --amend --no-edit` (static WIP message like `WIP: autosnapshot`).
- No changes → nothing happens.

### 3.3 Sealing (every 6 h)
**Only automatic trigger:** a timer of **6 h since the last seal**.

1. If the WIP has no changes vs `sealed_N` → **don't seal** (don't pollute the history).
2. Generate a message with AI from `git diff sealed_N..WIP` (§6).
3. `git commit --amend -m "<AI message>"` → the WIP becomes `sealed_N+1`.
4. Create a new empty WIP on top: `git commit --allow-empty -m "WIP: autosnapshot"`.
5. **Push** (§4).

> There is no sealing on idle or on shutdown. To force a one-off seal+push (e.g. right before I head to the laptop) there will be a manual `sincrogit sync` command (§12).

### 3.4 Periodic pull (every 10 min)
Besides the startup pull (§3.1), the daemon checks the remote every **10 min** to bring in what the other machine left, **without** having to log back in or pull by hand.

1. **`git fetch`** (cheap; doesn't touch the working tree).
2. Check whether the remote has new commits:
   `git rev-list --count HEAD..<remote>/<branch>`.
   - If it's **0** → nothing to bring → **do nothing** (the usual case while I work on this machine).
   - If it's **> 0** → **`git pull --rebase --autostash`** (rebase my local WIP on top of the new stuff).
3. **Rebase conflict** → `git rebase --abort`, **pause autosync for that repo + notify**; resolve by hand. Never force, never data loss.

> Since usage is **sequential** (never both machines at once), while I work on one, the other isn't pushing → step 2 returns 0 and the pull doesn't fire. When I sit at the other machine, within ≤10 min it picks up on its own what I sealed on the first.

---

## 4. Push and multi-machine

**Golden rule: only sealed commits are pushed; the WIP never leaves the machine.**

- Push: push **`HEAD~1`** (the last sealed), never the live WIP:
  `git push origin HEAD~1:<branch>` → this way the remote receives immutable history and the local WIP stays 1 commit ahead.
- Because sealed commits are immutable and never rewritten, **the push is always fast-forward** and the **other machine's pull is always clean**. No force-push is needed in any normal-flow case.

**Handoff between machines (sequential use):**

```
Desktop: works → every 6h (or manual `sincrogit sync`) seal + push  ──►  remote up to date
Laptop:  starts → pull --rebase (clean) → works → seal + push ──► remote up to date
Desktop: starts → pull --rebase (clean) → continues...
```

**Normal handoff (clean branch):** the laptop does `pull --rebase` and starts from the **sealed** state (up to 6 h old). For a mid-window handoff, run **`sincrogit sync`** before getting up (seal + push) so the laptop starts with everything via the clean path.

### 4.1 Autosnap (live mirror) — disaster recovery

Since sealing every 6 h would leave up to 6 h of work off the remote, **autosnap** decouples the *remote backup* from the *history*: every **30 min** (and only if something changed) it `push --force`es `HEAD` (sealed history **+ the live WIP**) to a **per-machine** side ref `refs/autosnap/<host>/<branch>`.

- **Keeps the branch clean:** nobody pulls that ref for work; `main` still receives only sealed commits → the pull is always clean. It's the deliberate exception to "the WIP never leaves the machine", scoped to a backup ref.
- **Disk-failure RPO ≈ 30 min** (instead of 6 h). On the other machine: *Fetch autosnaps* → browse/restore the latest state (single file or whole repo).
- **Cost:** up to ~48 pushes/day/repo during active work (cheap force-push; **nothing** on idle repos, since it only pushes when HEAD changed). Orphan objects on the remote until its GC.
- **Power cut / OS crash** needs nothing special from autosnap: saved files survive on the local disk, and the 5-min snapshot/`reflog` give the rollback points. autosnap is for the *machine-is-gone* case (and the handoff, §4.2).

> *("Live mirror" variant discarded for now: it did force-with-lease of the WIP every minute to keep the remote <1 min behind. More traffic and complexity; re-evaluable if real-time remote backup ever becomes critical.)*

### 4.2 Cross-machine handoff (live WIP)

The autosnap mirror is also the substrate for **automatic machine-to-machine handoff**,
decoupled from sealing (so it works in purist mode too, where the seal never fires). Two
design points:

- **Ref namespace `refs/autosnap/<user>/<host>/<branch>`.** The `<host>` keeps each machine
  the *sole writer* of its own ref (a plain `--force` is safe, no clobber). The `<user>`
  (sanitized `git config user.email`) lets a machine recognize its *own* other machines vs.
  a teammate's, so handoff fetches only `refs/autosnap/<user>/*` — cheap, and team-safe (it
  never touches `main`/feature branches, only personal side refs).
- **Compare by WORK CONTENT, not ancestry.** Critical subtlety: the WIP is continuously
  *amended*, so once a machine adopts a peer's WIP and edits, its new WIP is a *sibling* of
  the peer's (same parent = the shared seal), never a descendant — ancestry checks would
  report divergence constantly. Instead `GitRepo.work_relationship(mine, theirs)` compares,
  relative to the merge base, the *paths each side changed*: if `theirs` matches `mine` on
  every path I changed (and has more) it's `theirs_contains` → safe to adopt; the mirror is
  classified `equal` / `mine_contains` / `diverged` otherwise.

Behavior — `live_handoff` is a 3-state knob (`auto` default | `ask` | `off`):
- **`theirs_contains` → safe fast-forward** (`git reset --hard` to the peer): provably
  loss-free (the peer holds all my changed-path content; only an empty WIP is dropped),
  reversible via the reflog, and **refused** if it would clobber an untracked file
  (`untracked_collisions`). In `auto` it's applied immediately **and a tray notification is
  fired** (level b is never *silent* — the working tree changing under you is a surprise even
  when nothing is lost). In `ask` it is NOT applied: the candidate is recorded
  (`pending_handoff`, surfaced in `status()` and the panel), the user is notified, and a
  one-click **Apply** (`Engine.apply_handoff` / `--apply-handoff`) re-validates from scratch
  (re-fetch + re-classify + re-check collisions, since the peer may have moved) before the
  fast-forward (level a / consent).
- **`diverged` → notify, never auto-merge.** Deliberately no 3-way auto-merge of two piles of
  unreviewed in-progress work (a quiet, subtly-broken tree is the worst outcome). SincroGit
  warns **once** per distinct peer state and leaves both intact; the user resolves by
  **Smart-Committing one side then syncing** (normal rebase, with the usual conflict-pause),
  or by inspecting/merging the side ref by hand. See the README.

Runs at the end of the sync cycle (so it needs `pull` or `push` on to fire) and inside the
repo's `op_lock`. Phase 2 levels (a)+(b); a true auto-merge mode is intentionally out of
scope.

---

## 5. File filter: code only

**Criterion: only TEXT and < 1 MB is versioned automatically.** Everything else (binaries, large files) I manage **by hand**.

- **"Text" detection** by content, not by extension (more reliable than an extension list). Since the size filter already guarantees the file is ≤ 1 MB, a large prefix is inspected (up to ~1 MB, not just a few KB) and classified in layers: empty → text; a Unicode **BOM** (UTF-8/16/32) → text; a **NUL** byte → binary; otherwise decide by the **proportion of control bytes** (those < 0x20 that aren't whitespace/tab/newlines, + DEL): very few → readable text (incl. UTF-8 with accents/emoji/CJK and Latin-1); many → binary. *(Limitation: UTF-16/32 **without** a BOM contains NUL bytes → treated as binary, same as git.)*
- **Size:** discard if > 1 MB.
- **Key implementation:** the filter lives in the **tool's `git add` logic** (it runs `git add` *only* on files that pass the filter; **never** `git add -A`).
  - Advantage: since I **don't** use `.gitignore` for this, if one day I want to add a binary or a large file, I just `git add <file>` by hand and commit — the tool doesn't stop me, it simply doesn't touch it on its own.
- Configurable: maximum size and extra exclusion patterns (e.g. `node_modules/`, `.venv/`, `dist/`).
- **Smart Ignore (`suggest_excludes`, default on):** the filter reports each rejected file (binary/too-large, not a user exclude) to the engine, which buckets them by top-level folder. When one folder accumulates **≥ `NOISE_SUGGEST_THRESHOLD` (50)** distinct filtered-out files — almost always build output or a cache — it **suggests once** (a notification + log) adding `**/<folder>/**` to `extra_excludes`. It never auto-edits the config, fires at most once per folder per session, and counts only *rejected* files (a big text refactor passes the filter, so it never trips). Catches noise the default excludes miss without nagging.
- **Include list (`extra_includes`)**: patterns versioned **even if binary** (e.g. `**/*.docx`), under a separate size cap (`max_include_bytes`, 25 MB). For `.docx` and similar, SincroGit maps the file to a **pandoc `textconv` diff driver** in `.gitattributes` (committed, travels) and injects the textconv command **inline** (`git -c diff.pandoc.textconv=…`) on every diff → readable (markdown) diffs with no per-machine `git config`; this feeds the AI seal messages and the time-machine. The `.docx` stays the source of truth; the markdown is a *lossy* view. Pandoc's path is configurable (`pandoc_path`, per machine); without pandoc it degrades to versioning the opaque blob. **Consequence:** since change-detection uses that diff, a `.docx` is versioned/synced **only when its markdown changes** (text and structural formatting: bold, headings, lists, tables); purely visual styling (font/color/layout) and Word's resave churn don't trigger a version until a content change carries them in.

---

## 6. AI commit messages (hybrid mode)

They are generated when sealing (automatic) and on a **manual commit** (Smart Commit).

**Hybrid strategy (chosen):**
1. If **Ollama** is available locally → use it (free, no quota, **the code doesn't leave the machine**).
2. Otherwise → fall back to a **cloud** provider (Gemini) with an API key.
3. **Privacy mode:** option to send to the cloud **only `git diff --stat` + file names** (not the content), so as not to expose sensitive code.
4. **Fallback always available:** if the AI fails (no network, no quota, timeout) → automatic deterministic message, e.g.:
   `sincro: 4 file(s) (1 modified, 1 new, 1 deleted)`.
   **The commit/seal is never blocked because of the AI.**

**Two prefix conventions (key to tell machine from human apart):**
- **Automatic seal → `sincro:`** (a time-bucket; we don't pretend to classify it as `feat`/`fix`). The prefix marks it as a machine commit.
- **Manual commit (Smart Commit) → Conventional Commits** (`feat:`/`fix:`/`docs:`/`refactor:`/…). The user triggers it from the GUI, the AI **proposes** an editable message, and on confirm the current WIP is sealed with it and the **6 h seal timer is reset**.

**Model input:**
- *Automatic seal:* `git diff sealed_N..WIP --stat` + truncated diff → a concise `sincro:` message.
- *Manual commit:* diff **since the last manual commit** (skipping the `sincro:` seals) up to the WIP → the AI summarizes the whole unit of work. The commit only contains the WIP delta, so the body honestly notes it's a **cumulative summary** (some of the code is in earlier `sincro:` seals).

---

## 7. Software architecture (Python)

```
sincrogit/
├─ sincrogit/
│  ├─ __main__.py        # entrypoint / CLI (tray, headless, --history, --autosnaps, ...)
│  ├─ config.py          # loads/validates YAML config
│  ├─ runtime.py         # exe config, single instance (+ handshake), console
│  ├─ gitrepo.py         # git wrapper (subprocess): snapshot/seal/autosnap/restore
│  ├─ watcher.py         # watchdog + per-repo debounce (only marks "dirty")
│  ├─ engine.py          # orchestration: tick snapshot 5min / seal 6h / autosnap 30min / sync
│  ├─ filefilter.py      # text + size detection
│  ├─ messages.py        # deterministic fallback commit message
│  ├─ ai.py              # AI providers (ollama/gemini) + fallback
│  ├─ events.py          # structured event log (JSONL) for the GUI
│  ├─ log.py             # logging to a rotating file
│  ├─ notify.py          # Windows notifications (toasts)
│  └─ gui/               # tray icon + control panel + dialogs (add-repo, history) (PyQt5)
├─ config.example.yaml
├─ pyproject.toml
└─ DESIGN.md
```

**Libraries:**
- **`watchdog`** — filesystem events.
- **git via `subprocess`** (not GitPython) — exact control of `amend`/`push HEAD~1`/`rebase`, transparent and predictable behavior.
- **standard `urllib`** — cloud AI calls; local **Ollama** client over HTTP (no extra dependency).
- **`pyyaml`** — config.
- **`PyQt5`** — tray icon + control panel.
- **`logging`** (to a rotating file) + Qt notifications — alerts (e.g. "autosync paused due to conflict").
- Scheduling: own loop with timers.

**Decision:** wrap the `git` CLI with `subprocess` instead of GitPython, because the fine-grained operations (continuous amend, push of `HEAD~1`, rebase with a conflict policy) are clearer and more robust with the CLI.

---

## 8. Configuration (example)

```yaml
# config.yaml
defaults:
  snapshot_interval_sec: 300     # how often the WIP is amended (5 min)
  debounce_sec: 25               # wait after the last change before snapshotting
  seal_interval_min: 360         # "real" commit + push every 6h (permanent timeline)
  autosnap: true                 # live mirror of HEAD to refs/autosnap/<host>/<branch>
  autosnap_interval_min: 30      # force-push the mirror every 30 min (only if it changed)
  pull_interval_min: 10          # fetch every 10 min; pull only if there's something new
  max_file_bytes: 1048576        # 1 MB
  extra_excludes:                # in addition to the text/size filter
    - "**/node_modules/**"
    - "**/.venv/**"
    - "**/dist/**"

ai:
  mode: hybrid                   # hybrid | local | cloud | none
  cloud_provider: gemini         # chosen: Gemini
  cloud_model: gemini-2.5-flash-lite  # fast and within the free tier
  cloud_send_content: false      # false => only --stat + names (privacy)
  # api_key via env var SINCROGIT_GEMINI_KEY, NOT in the file

repos:
  - path: "C:/repos/sincrogit"
    remote: origin
    branch: main
  - path: "C:/repos/foo"
    remote: origin
    branch: main
    seal_interval_min: 60        # per-repo override
```

> The **API key never goes in the YAML** → environment variable (`SINCROGIT_GEMINI_KEY`, etc.).

---

## 9. Running in the background on Windows

**Yes, it can run in the background.** Options, from simplest to most "service-like":

| Option | How | Pros | Cons |
|--------|-----|------|------|
| **Scheduled task "at log on"** ⭐ | Task Scheduler → trigger *At log on*, action `pythonw.exe -m sincrogit --tray`, hidden window, automatic restart | Runs **in your user session** → has access to your **SSH keys / Credential Manager** for the push. Resilient. | Only runs while logged in (enough: you only edit while logged in). |
| **`pythonw.exe` in the Startup folder** | Shortcut in `shell:startup` | Simplest | Less control over restart/logs. |
| **Real Windows service** (NSSM or `pywin32`) | NSSM wraps the script as a service | Starts without login | ⚠️ Runs as *LocalSystem* → **doesn't see your SSH keys / user credentials** → the **push fails**. You'd have to configure machine-level credentials. |

**Recommendation: scheduled task "at log on" with `pythonw.exe`** (no console). It fits best because:
- You only need it to run while you work (logged in).
- It inherits your git credentials → the push works without configuring anything weird.
- Task Scheduler provides automatic restart and delayed start.

`pythonw.exe` (instead of `python.exe`) avoids a console window appearing.

---

## 10. Failure recovery

| Scenario | What happens | How I recover |
|----------|--------------|---------------|
| **Power cut / OS crash (disk intact)** | Saved files are on the disk; the last snapshot (≤5 min) is in `HEAD` (WIP) | Nothing to recover for saved files (the disk has them). For *rolling back* a bad saved state: `git reflog` (≈5 min resolution). Unsaved buffers are your editor's job. |
| **"I want yesterday's version"** | It's in the sealed commits | `git checkout`/`git restore` from the matching sealed commit. |
| **I deleted something 20 min ago (within the window)** | The previous snapshot became *unreachable* in the reflog | `git reflog` + `git checkout`. *(More convenient with the optional `autosnap` branch, §12.)* |
| **Total disk failure** | Sealed state is on the remote; the latest state (≤30 min) is in the `autosnap` ref (§4.1) | On another machine: *Fetch autosnaps* → restore (file or whole repo). Max loss ≈ 30 min. Without autosnap: down to the last seal (6 h). |
| **Conflict when switching machines** | Rebase fails on startup | Autosync is **paused** for that repo + notification; I resolve by hand. Nothing is ever lost. |

---

## 11. Edge cases and safety

- **Repo with no commits / no remote:** validate on startup and warn; don't break.
- **Multiple repos:** each with its own independent watcher/timers.
- **Code privacy in the cloud:** by default, hybrid mode prioritizes Ollama (local); if it falls back to the cloud, `cloud_send_content: false` sends only statistics. The API key lives in an environment variable.
- **My manual git operations** while the daemon runs (rebase, branch checkout, etc.): the tool must detect a changed `HEAD`/`rebase in progress`/busy index and **yield** (skip that cycle) instead of fighting. It detects `.git/MERGE_HEAD`, `.git/rebase-*`, the index lock.
- **Branch guard / branch following.** By default, when HEAD isn't on the configured `branch`, the repo **yields** (no snapshot/seal/autosnap/push on the wrong branch) — `_ensure_on_branch`, rate-limited. With **`track_current_branch: true`** it instead **follows** the current branch: every branch-scoped op uses `st.active_branch` (the live HEAD branch) rather than `cfg.branch`, so snapshot/autosnap/handoff/push all happen on whatever branch you're on (each branch gets its own `refs/autosnap/<user>/<host>/<branch>`, and handoff only matches the same branch). Detached HEAD still yields. Pairs naturally with purist mode (no auto-seal → nothing auto-pushed to the wrong place). Opt-in; default keeps the safe guard.
- **The push targets the last non-WIP commit** (resolved by message, not the
  positional `HEAD~1`): if the user commits manually on top of the WIP, their
  commit is what gets pushed — never the transient WIP. The seal clock is also
  reset when an external commit is detected (it's respected as a manual seal).
- **Single instance (no two daemons racing git).** Authoritative guard is a Windows
  named mutex (`acquire_instance_mutex`; no stale-lock problem — the OS releases it on
  process death — and it can't be stolen by an app squatting on the lock port). The
  localhost port (49677) is kept only as the "show the running panel" activation channel;
  if a foreign app holds it, single-instance is still enforced by the mutex (we just lose
  that IPC channel). The mutex is tray-only (headless can intentionally run several with
  different configs).
- **Watcher load.** The watchdog handler drops events for `.git` internals **and** for
  paths matching the repo's excludes (`FileFilter.is_excluded`, a cheap pathspec check, no
  disk I/O) — so a burst like `npm install` under `node_modules/` never wakes the engine.
  Complements *Smart Ignore* (which suggests adding such folders to `extra_excludes`).
- **Never `--force`** in the automatic flow.
- **Git output language** forced to English (`LC_ALL=C`) for consistent logs (our parsing uses locale-independent porcelain/plumbing commands).
- **Maintenance:** `git gc --auto` after each seal **and at least once a day**
  (`Engine.GC_INTERVAL_SEC`, on a background worker), to pack the orphan objects left by
  the amends. The daily trigger is **decoupled from sealing on purpose**: in purist mode
  (`seal_interval_min: inf`) the seal never fires, so without it a long-lived WIP would
  accumulate loose objects unbounded.
- **Disabling intervals/limits:** any interval (`*_interval_*`, `debounce_sec`) or size
  threshold (`max_file_bytes`, `max_include_bytes`) accepts a *disable sentinel*
  (`inf`/`off`/`none`/`never`, also bare `None`/`False`), normalized to `math.inf` in
  `RepoConfig.__post_init__`. `inf` flows through the deadline arithmetic untouched (a
  due-time of `inf` is never reached; `min(x, inf) == x`), so the engine needs **no**
  special-casing. The headline use is **purist mode** (`seal_interval_min: inf`): no
  automatic seal — the permanent history is built only by manual **Smart Commit**, while
  the WIP + `autosnap` keep providing the safety net. (YAML only parses `.inf` as a
  float; a bare `inf`/`off` arrives as a string/bool, which is why the normalization
  exists.)

---

## 12. Roadmap by phases

**✅ Phase 1 — MVP (automatic local historian) — COMPLETE:**
- Config + repo validation.
- Watcher + debounce + snapshot (amend) every 5 min (+ initial snapshot on startup).
- Text/size filter.
- Sealing every 6 h with a **fallback message**.
- Logging.
> With this I already have a versioned time machine, which is 80% of the value.

**✅ Phase 2 — AI + remote sync — COMPLETE:**
- Hybrid AI message generator (Ollama → Gemini → fallback). Never blocks the seal.
- Push of sealed commits (refspec with SHA → `refs/heads/<branch>`) + retry on every sync.
- `fetch` + pull with WIP rebase, only if the remote is ahead; initial sync on startup.
- Conflict policy: abort rebase + pause repo + notify (verified in tests).

**✅ Phase 4 — Tray UI (PyQt5) — COMPLETE:**
- System tray icon (a "G" with an hourglass, drawn vectorially) whose **color reflects
  the state** (active/paused/conflict/stopped).
- Menu: open panel, pause/resume, sync now, seal now, quit.
- Control panel with tabs Status / Log (filterable by repo, action, level, text) /
  Configuration (YAML editor) / About.
- Structured event log (`events.jsonl`) + desktop notifications.
- Architecture: engine in a background thread, GUI on the main thread, communication via
  Qt signals; manual actions serialized with a lock in the engine.
- Startup: `python -m sincrogit --tray` (or `pythonw` without a console).

**✅ File history / restore ("time machine") — COMPLETE:**
- Browse a file's past versions, merging the reachable history (sealed commits,
  permanent) and the reflog (intra-window snapshots, ~30 days), collapsing
  identical contents.
- Preview any version and restore it (`git checkout <sha> -- file`); the restore
  becomes a new snapshot, so it is itself versioned.
- CLI: `--history FILE` (interactive) / `--history FILE --pick N` (non-interactive).
- GUI: control panel → Status → "File history…" dialog.

**Phase 3 — Deployment (partial):**
- ✅ Standalone single-file `SincroGit.exe` (GUI + CLI) via PyInstaller
  (`--onefile --noconsole`); CLI output attaches to the launching terminal.
- ✅ Single-instance lock (localhost socket, no stale-lock problem): a second
  launch asks the running one to show its panel and exits.
- ✅ Config resolution: next to the .exe → `%APPDATA%\SincroGit\` → cwd; a default
  is created on first run.
- ⏳ Pending: scheduled task at log on to auto-start `SincroGit.exe`.
- ⏳ Pending: `status` command/tab (the "seal+push now" shortcut is already in the menu).

**Optional / future:**
- `autosnap` branch with real commits every 5 min (browsable intra-window history *on the remote*) instead of the force-push mirror of the latest state.
- "Live mirror" variant (force-with-lease of the WIP) if real-time remote backup becomes necessary.

---

## 13. Decisions made

- ✅ Model **WIP+amend → seal every 6h** + **autosnap** (live mirror) every 30 min.
- ✅ **Intervals: snapshot every 5 min, seal every 6 h, autosnap every 30 min.**
- ✅ Push **only of sealed commits** (WIP local; pull always clean; no force-push).
- ✅ **Hybrid** AI (Ollama local → cloud fallback; option to send only stats).
- ✅ **Prefixes:** automatic seal `sincro:`; **manual commit (Smart Commit)** with an AI-proposed Conventional Commits message (cumulative summary since the last manual commit) + timer reset.
- ✅ **Cloud provider: Gemini** (`gemini-2.5-flash-lite`), API key in an environment variable.
- ✅ Filter: **text < 1 MB only**; binaries/large files by hand.
- ✅ **Python**; git via subprocess; `watchdog`; **PyQt5** for the tray UI.
- ✅ Background: **scheduled task at log on** with `pythonw.exe`.
- ✅ Working branch: **`main`** (confirm per repo).
- ✅ **Seal every 6 h** (coarse permanent timeline); manual `sincrogit sync` for handoff via the clean path.
- ✅ **Periodic pull every 10 min** (`fetch` + pull only if the remote has new commits), besides the startup pull.
- ✅ **Autosnap** (live mirror of `HEAD` to `refs/autosnap/<host>/<branch>`, force-pushed every 30 min, only if it changed): disk-failure RPO ≈ 30 min, cross-machine recovery per file or whole repo (CLI `--autosnaps` + GUI). The "fine browsable history on the remote" variant (one commit per snapshot) is still deferred.

## 14. How to configure the Gemini API key

1. Get a free API key from **Google AI Studio** (`aistudio.google.com`) → *Get API key*.
2. Save it as a **user environment variable** on Windows (not in the YAML, not in the repo):
   ```powershell
   setx SINCROGIT_GEMINI_KEY "your_api_key_here"
   ```
   (Close and reopen the terminal/session for the variable to be available.)
3. The tool reads it from `os.environ["SINCROGIT_GEMINI_KEY"]`.
4. Remember: in hybrid mode it tries **local Ollama first**; Gemini only kicks in if Ollama isn't there. And with `cloud_send_content: false` Gemini only receives file names + `--stat`, not the content.

## 15. Open questions

*(None pending — design closed.)*
