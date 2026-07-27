# ⏳g SincroGit — for developers who know Git and still don't commit

You know how to clone, branch and commit. That was never the problem. The problem
is that you stand up at the end of the day, close the laptop, and the commit you
meant to make doesn't happen — and then it doesn't happen tomorrow either.

Nothing bad comes of that. Until something does: the machine dies, or you simply
want the version of a file from last Tuesday, and there is nothing to go back to
because the last three weeks of work are one undifferentiated pile on disk.

SincroGit is the answer to *"I'll commit later"*. It runs in the tray and keeps a
recoverable trail of your work whether or not you ever type `git`.

> Want the **commands and every option**? [User Manual](MANUAL.md). The
> engineering? [DESIGN.md](DESIGN.md). Spanish: [GUIA.md](GUIA.md).

## Does this sound familiar?

- You've found yourself with **weeks or months of work uncommitted** — not because
  it's hard, but because there's never a moment that feels like the right one.
- You work **alone**, on `main`, and branches are ceremony you don't need.
- You'd like the version from **an hour ago**, and `Ctrl+Z` is long gone.
- You used a file syncer (Dropbox, OneDrive) and liked that it just *happened* —
  but "the copy from 30 days ago, one file at a time" isn't version history.
- You have **two machines** and the handoff is always a manual push you forget.

If three of those are true, this was built for you. Literally — it was built by
someone the list describes.

## What it does, in one line

**Every few minutes it records the state of your saved files, so any moment of the
last month is somewhere you can get back to — without you ever running `git`.**

It does that *beside* your history, not in it. Your `git log` stays yours, your
staging area is never touched, and `git status` keeps telling the truth. If you
uninstall it tomorrow, your repo is a completely ordinary Git repo.

## The three rhythms

Three clocks run per repo. You can change or switch off any of them.

| | Every | What happens | Where it lands |
|---|---|---|---|
| 🖊️ **Snapshot** | ~5 min | Your saved files are recorded, invisibly | A side ref, local only |
| ☁️ **Mirror** | ~30 min | That state is copied off the machine | `refs/autosnap/…` on your remote |
| 📦 **Seal** | ~6 h | The accumulated work becomes ONE real commit, with an AI-written message | Your branch, pushed |

Nothing happens when you haven't touched anything — an idle repo costs nothing.

The seal is the only one that writes to your branch, and it's the one people
argue about. Two honest answers: leave it on and your history gains a tidy
`sincro:` checkpoint every few hours (trivial to squash before a PR), or set
`seal_interval_min: inf` and your branch stays **100 % yours** while the snapshots
and the mirror keep running underneath. Both are supported on purpose.

## Getting your code back

This is the part you actually installed it for.

1. Open the panel → the **Time machine** tab.
2. Pick the day on the left, then the moment. Every snapshot is there, including
   the ones from 20 minutes ago that you never committed.
3. Double-click a file to pin it: you get every version of it, a red/green diff
   against how it looks *right now*, and a search box across all of them.
4. Restore the file, a selection of files, only some **blocks** of a file, or the
   whole repo. Or **"Save a copy…"** if you'd rather not overwrite anything.

Two things worth knowing. First, **the restore is itself versioned** — it becomes
a new snapshot, so an undo of an undo is always available; nothing you do here is
one-way. Second, it **refuses** to overwrite content it couldn't capture (an
excluded file, something too big) rather than destroying it silently, and tells
you which files to move aside first.

## Why this got worse when the AI started writing

The old failure mode was *"I lost an afternoon"*. It has changed shape:

- **The volume went up.** An agent rewrites twelve files in ninety seconds. What
  broke is somewhere in there, and it wasn't in a commit.
- **Review happens after the fact.** You read the result, not the keystrokes — so
  "go back to before that" is now a normal daily need, not an emergency.
- **Your agent's undo is narrower than you think.** The checkpoints in today's
  coding agents cover the edits made through their own file tools, within one
  session. Anything the agent did through the shell, anything a second agent did,
  anything you changed by hand in the editor meanwhile — outside the net.

SincroGit doesn't care who typed it. It photographs the working tree on a clock,
so the trail covers the agent, the shell, the other agent, and you. Nothing to
install in the agent, nothing it has to do differently.

For a repo an agent works in, turn the resolution up — a rollback point per burst
instead of per five minutes:

```yaml
repos:
  - path: "C:/work/agent-playground"
    snapshot_interval_sec: 30   # a recoverable point every ~30-60 s
    debounce_sec: 5             # agents write in bursts; settle fast
```

## A normal day

**Morning, desktop.** SincroGit started with your Windows session. You code for
three hours. Nothing asks you anything.

**You get up for lunch** and lock the screen. That lock is a signal: your latest
state goes to the remote right then. Stay away past ~20 minutes and it decides
you actually left, turns the pending work into a real commit and pushes it.

**Afternoon, laptop.** You unlock it and it *already* has this morning's work —
it noticed the desktop was ahead and fast-forwarded you to it. No pull, no
commit, no thinking. You get a notification so it's never silent.

**Finished something real?** Don't wait for the 6 h clock. Hit **"Commit…"** on
that repo: the AI proposes a Conventional Commits message from everything since
your last manual commit, you edit it if you like, and it's pushed.

## Four rules and you're done

1. **Big files and binaries stay manual.** Only text under 1 MB is versioned
   automatically. Want a `.jpg` or a `.dll` in there? `git add` it by hand once
   and SincroGit will carry it from then on — it will never revert or drop a file
   you committed yourself. (Word and PowerPoint files *can* be versioned with
   readable diffs — see `extra_includes` in the [Manual](MANUAL.md).)
2. **It never resolves a conflict for you.** Edited on both machines without
   syncing? It stops, leaves both states intact, turns the icon red and asks. You
   fix it in your editor and press Resume.
3. **Switching branch pauses it.** `git checkout experiment` and it steps aside
   rather than polluting your experiment; back on `main`, it resumes. If you do
   live on feature branches, `track_current_branch: true` makes it follow you.
4. **Don't keep the repo inside Dropbox / OneDrive / Drive.** Those tools corrupt
   `.git` when two things write at once. Let SincroGit handle Git and let them
   handle everything else.

## What it does NOT do

- It doesn't rescue what you **never saved** — it versions files on disk. Your
  editor's autosave owns that gap.
- It isn't **real-time** between machines. Lock the screen and the handoff takes
  seconds; walk away without locking and it's up to ~40 minutes.
- It doesn't **merge two machines at once**. It's turn-based, by design.
- It isn't a **full backup**: it keeps your text, not your build folder.
- It won't make your history pretty. Time-bucketed `sincro:` commits are a trail,
  not curated history — use **"Commit…"** when you want a real one.
- It's **Windows-first**. Elsewhere you pull by hand.

## What it costs to leave running

Measured on a real install with five repos, after seven weeks:
about 90 MB of RAM, a few seconds of CPU per day, and roughly 7 MB of extra
`.git` for a medium-sized code repo. Snapshots are local and cost no network;
the daily `git gc` keeps the object store packed. You will not notice it.

## Start in five minutes

1. Get `SincroGit.exe` (or `pip install -e .` from source — see the
   [README](README.md#installation)).
2. Run it. The tray icon appears; the folder holding the exe becomes the install,
   and a config file is created there with every option commented.
3. **"Add repo…"** → pick the folder. If it has no remote yet, paste a URL and hit
   Verify: it checks reachability *and* write access before adding anything.
4. Tick **"Start SincroGit when I sign in to Windows"** in Settings. Once.
5. Go back to work and forget it's there. That's the whole point.

Worried something isn't right? `sincrogit --doctor` checks git, your remotes,
credentials, the AI backends and the daemon, and tells you what to fix.
`sincrogit status` shows every repo at a glance.
