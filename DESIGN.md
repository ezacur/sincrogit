# SincroGit — Design document

> Automatic Dropbox-style synchronization, but with robust Git versioning.
> Target platform: **Windows** (interactive use, one machine at a time).

---

## 1. Goals and scope

**I want two things at once:**

1. **Versioning + local auto-backup.** Go back to yesterday's version; and don't lose work on a power cut or crash (recover up to the last minute).
2. **Sync between machines (sequential).** I work almost always on the desktop and occasionally on the laptop, **never both at once**. When switching machines, I want the sources to update quickly and automatically.

**Out of scope (for now):**

- Simultaneous editing on two machines / complex conflict resolution.
- Versioning binaries or large files (those I commit/push **by hand**).
- Any OS other than Windows. On Linux/others I'll `pull` by hand and won't edit.

---

## 2. Conceptual model: two tiers

The trick to reconcile *"almost instant snapshot"* with *"I don't want thousands of commits"* is to separate two tiers:

| Tier | What it is | Frequency | Visible in history |
|------|-----------|-----------|--------------------|
| **WIP (snapshot)** | A **single** commit at the tip (`HEAD`) that is **amend**ed with the current state | Every ~1 min (with debounce) | No (it's transient, it gets sealed or rewritten) |
| **Sealed (history)** | The WIP is "frozen" with a descriptive AI message and a new WIP is created on top | Every ~2 h (or on idle / shutdown) | Yes (permanent commit) |

```
... ── sealed_N ── WIP        ← HEAD (amended every minute)
                    │
       every 2h ────┘ it gets sealed (reword with AI message) and a new WIP is born on top

result: ... ── sealed_N ── sealed_N+1 ── WIP(new) ← HEAD
```

**Why it works:**

- The current state is saved to disk every minute → **recovery on a power cut** (on reboot, `HEAD` = last snapshot).
- Because we `amend`, hundreds of commits don't pile up: only **~12 commits/day** (one every 2 h).
- The "clean" history (sealed) is the only thing that travels to the remote → **pull always clean, no force-push** (see §4).

> **Fine-grained safety net:** each `amend` leaves the previous snapshot as an *unreachable* commit in the **reflog** (≈30 days by default). That is, even though the visible history only has 1 commit per window, internally you can recover intermediate states with `git reflog`. *(Optional, see §12: an `autosnap` branch with real commits every minute if you want browsable intra-window history.)*

---

## 3. Detailed flow

### 3.1 Service startup (per repo)
1. Validate: it's a git repo, the configured remote and branch exist.
2. **`git pull --rebase --autostash`** to bring in what the other machine left.
   - If there is an unpushed local WIP (typical case after a crash) → it is **rebased** on top of the remote.
   - **If there is a conflict** → `git rebase --abort`, the **autosync for that repo is paused**, the user is notified and it is logged. It is **never** resolved destructively, nor is force used. (This is rare in sequential use, but the policy is: when in doubt, don't lose data.)
3. Ensure a WIP exists at the tip (if not, create an empty one).
4. Start the watcher.

### 3.2 Snapshot cycle (every minute, with debounce)
- The **watcher** (filesystem events) marks the repo as *dirty* and resets a debounce (e.g. 20-30 s without changes).
- When the debounce settles **and** ≥1 min has passed since the last snapshot:
  1. Compute candidate files and apply the **filter** (§5).
  2. `git add <only the candidates>`.
  3. If something is staged: `git commit --amend --no-edit` (static WIP message like `WIP: autosnapshot`).
- No changes → nothing happens.

### 3.3 Sealing (every 2 h)
**Only automatic trigger:** a timer of **2 h since the last seal**.

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
Desktop: works → every 2h (or manual `sincrogit sync`) seal + push  ──►  remote up to date
Laptop:  starts → pull --rebase (clean) → works → seal + push ──► remote up to date
Desktop: starts → pull --rebase (clean) → continues...
```

**Important (consequence of sealing only every 2h):** between seals, the latest thing you worked on might **not be on the remote yet** (up to 2 h). So, if I'm going to switch machines mid-window, it's worth running **`sincrogit sync`** before getting up, so the laptop starts with everything. If I don't, the laptop will have the state of the last seal (up to 2 h ago).

**Accepted cost:** on a **total disk failure** (not a simple power cut), you could lose up to whatever is unsealed (max ~2 h). The power cut / OS crash is covered by the local snapshot every 5 min.

> *("Live mirror" variant discarded for now: it did force-with-lease of the WIP every minute to keep the remote <1 min behind. More traffic and complexity; re-evaluable if real-time remote backup ever becomes critical.)*

---

## 5. File filter: code only

**Criterion: only TEXT and < 1 MB is versioned automatically.** Everything else (binaries, large files) I manage **by hand**.

- **"Text" detection** by content, not by extension: read the first ~8 KB and discard if there are NUL bytes / it isn't decodable (standard "binary" heuristic). It's more reliable than an extension list.
- **Size:** discard if > 1 MB.
- **Key implementation:** the filter lives in the **tool's `git add` logic** (it runs `git add` *only* on files that pass the filter; **never** `git add -A`).
  - Advantage: since I **don't** use `.gitignore` for this, if one day I want to add a binary or a large file, I just `git add <file>` by hand and commit — the tool doesn't stop me, it simply doesn't touch it on its own.
- Configurable: maximum size and extra exclusion patterns (e.g. `node_modules/`, `.venv/`, `dist/`).

---

## 6. AI commit messages (hybrid mode)

They are generated **only when sealing** (~12 times/day at most → fits comfortably in any free tier or locally).

**Hybrid strategy (chosen):**
1. If **Ollama** is available locally → use it (free, no quota, **the code doesn't leave the machine**).
2. Otherwise → fall back to a **cloud** provider (Gemini / Groq) with an API key.
3. **Privacy mode:** option to send to the cloud **only `git diff --stat` + file names** (not the content), so as not to expose sensitive code.
4. **Fallback always available:** if the AI fails (no network, no quota, timeout) → automatic deterministic message, e.g.:
   `auto: 4 modified, 1 new, 1 deleted (src/foo.py, ...)`.
   **The commit/seal is never blocked because of the AI.**

**Model input:** `git diff sealed_N..WIP --stat` + a summarized/truncated diff (token limit) → prompt asking for a concise *conventional commits*-style message on a single line + optional body.

---

## 7. Software architecture (Python)

```
sincrogit/
├─ sincrogit/
│  ├─ __main__.py        # daemon / CLI entrypoint
│  ├─ config.py          # loads/validates YAML config
│  ├─ gitrepo.py         # git wrapper (via subprocess)
│  ├─ watcher.py         # watchdog + per-repo debounce
│  ├─ engine.py          # timers: snapshot / seal / sync orchestration
│  ├─ filefilter.py      # text + size detection
│  ├─ messages.py        # deterministic fallback commit message
│  ├─ ai.py              # providers (ollama/gemini) + fallback
│  ├─ events.py          # structured event log
│  ├─ notify.py          # Windows notifications (toasts) + logging
│  └─ gui/               # tray icon + control panel (PyQt5)
├─ config.yaml
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
  seal_interval_min: 120         # "real" commit + push every 2h (only automatic trigger)
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
  - path: "C:/Dropbox/mTools/sincrogit"
    remote: origin
    branch: main
  - path: "C:/Dropbox/projects/foo"
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
| **Power cut / OS crash** | The last snapshot (≤1 min) is committed in `HEAD` (WIP) | On reboot, the work is there. `git reflog` for intermediate states of the window. |
| **"I want yesterday's version"** | It's in the sealed commits | `git checkout`/`git restore` from the matching sealed commit. |
| **I deleted something 20 min ago (within the window)** | The previous snapshot became *unreachable* in the reflog | `git reflog` + `git checkout`. *(More convenient with the optional `autosnap` branch, §12.)* |
| **Total disk failure** | What's sealed+pushed is on the remote | `git clone` on a new machine. Max loss ≈ whatever is unsealed (up to 2 h; less if I ran `sincrogit sync`). |
| **Conflict when switching machines** | Rebase fails on startup | Autosync is **paused** for that repo + notification; I resolve by hand. Nothing is ever lost. |

---

## 11. Edge cases and safety

- **Repo with no commits / no remote:** validate on startup and warn; don't break.
- **Multiple repos:** each with its own independent watcher/timers.
- **Code privacy in the cloud:** by default, hybrid mode prioritizes Ollama (local); if it falls back to the cloud, `cloud_send_content: false` sends only statistics. The API key lives in an environment variable.
- **My manual git operations** while the daemon runs (rebase, branch checkout, etc.): the tool must detect a changed `HEAD`/`rebase in progress`/busy index and **yield** (skip that cycle) instead of fighting. It detects `.git/MERGE_HEAD`, `.git/rebase-*`, the index lock.
- **The push targets the last non-WIP commit** (resolved by message, not the
  positional `HEAD~1`): if the user commits manually on top of the WIP, their
  commit is what gets pushed — never the transient WIP. The seal clock is also
  reset when an external commit is detected (it's respected as a manual seal).
- **Never `--force`** in the automatic flow.
- **Git output language** forced to English (`LC_ALL=C`) for consistent logs (our parsing uses locale-independent porcelain/plumbing commands).
- **Maintenance:** `git gc --auto` after each seal, to pack the orphan objects left by the amends.

---

## 12. Roadmap by phases

**✅ Phase 1 — MVP (automatic local historian) — COMPLETE:**
- Config + repo validation.
- Watcher + debounce + snapshot (amend) every 5 min (+ initial snapshot on startup).
- Text/size filter.
- Sealing every 2 h with a **fallback message**.
- Logging.
> With this I already have auto-backup + versioning, which is 80% of the value.

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
- `autosnap` branch with real commits every 5 min (browsable intra-window history) instead of relying on the reflog.
- "Live mirror" variant (force-with-lease of the WIP) if real-time remote backup becomes necessary.

---

## 13. Decisions made

- ✅ Model **WIP+amend → seal every 2h**.
- ✅ **Intervals: snapshot every 5 min, seal every 2 h.**
- ✅ Push **only of sealed commits** (WIP local; pull always clean; no force-push).
- ✅ **Hybrid** AI (Ollama local → cloud fallback; option to send only stats).
- ✅ **Cloud provider: Gemini** (`gemini-2.5-flash-lite`), API key in an environment variable.
- ✅ Filter: **text < 1 MB only**; binaries/large files by hand.
- ✅ **Python**; git via subprocess; `watchdog`; **PyQt5** for the tray UI.
- ✅ Background: **scheduled task at log on** with `pythonw.exe`.
- ✅ Working branch: **`main`** (confirm per repo).
- ✅ **Seal only every 2 h** (no idle or shutdown sealing); manual `sincrogit sync` for handoff.
- ✅ **Periodic pull every 10 min** (`fetch` + pull only if the remote has new commits), besides the startup pull.
- ⏳ `autosnap` branch (fine browsable history): **deferred to the future**; for now `reflog`.

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
