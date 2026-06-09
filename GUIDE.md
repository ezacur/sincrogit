# SincroGit for the lazy and forgetful 🦥

Let's be honest: Git is great, but it demands discipline. And sometimes we just **don't
feel like** running `git add`, crafting the perfect message, and `git push`ing every time
we get up for a coffee. If you've ever overwritten a good version with a bad one and wished
for an *undo*, or your history is full of messages like `asdffdsa` and `now it really
works`, you're in the right place.

SincroGit has **one rule**: *you focus on coding; it keeps a quiet, versioned **time
machine** of your saved files, so you can always go back.*

> It's also perfect for **scratch and experiment repos** — code that doesn't deserve a
> hand-crafted history, but whose trail you'd hate to lose. Let it run; never lose a spike.

> Want the technical detail? It's in [DESIGN.md](DESIGN.md). (Versión en español:
> [GUIA.md](GUIA.md).) Here we keep it practical.

## 🪄 The magic underneath: three rhythms

Forget the terminal. SincroGit has your back with three automatic rhythms:

- **🖊️ The draft — every ~5 min.** While you code (or watch memes while it compiles), it
  takes an invisible "snapshot" of your **saved** files. So if you delete a function, break
  something, or just want how it looked an hour ago, you can roll back — even though you
  never committed. *If you didn't touch anything, it does nothing.* (It snapshots what you
  saved to disk — not your editor's unsaved buffer; that's your editor's autosave.)
- **☁️ The cloud copy — every ~30 min.** It pushes your latest state to a private corner
  of the remote. It's your safety net for a **disk disaster** (not the day-to-day one).
- **📦 The seal — every ~6 h.** It bundles all those invisible drafts into a "real"
  commit, the **AI writes a decent summary**, and it pushes it to your branch.

The result: a clean history, made for you. You get the reputation of a disciplined
developer… while being the laziest one. 😎

## 💻↔💻 A normal day: from desktop to laptop

SincroGit shines if you use more than one computer (and you're the type who closes the
laptop lid without `push`ing).

**In the morning, on the desktop:**
1. You sit down. SincroGit starts on its own and **quietly pulls** whatever you last synced.
2. You code for three hours. No sign of the console.
3. Lunch break. You get up and leave **without touching anything**.

**Before switching machines:** nothing you *must* do — your work mirrors itself. Best part:
when you **lock the screen or close the lid**, SincroGit pushes your latest state to the
remote right then. So if you leave the way you normally do, the handoff is **seconds**, not
minutes. (If you just walk away without locking, it still catches up on its own within ~30
min. And a **Smart Commit** before you go is always instant.)

**In the afternoon, on the laptop:**
- You open it (unlock / wake it) and SincroGit **immediately** spots your desktop's newer
  work and **fast-forwards you to it** — you carry on where you left off. No commit, no pull,
  nothing. (You get a small heads-up notification, so it's never silent. Rather press a
  button yourself before your files change? Set `live_handoff: ask`.)

> 🤝 **"Your machines diverged"?** That only happens if you changed things on **both**
> machines without syncing in between. SincroGit won't guess how to blend two piles of
> half-finished work, so it leaves **both** intact and asks you. Easiest fix: **Smart
> Commit** on one machine, then the other syncs normally (full recipe in the
> [README](README.md#cross-machine-handoff-live-wip)).

> 🔥 **What if the desktop actually dies?** That's what the cloud copy is for: on the
> laptop, *File History → "Fetch autosnaps"* recovers your latest state (up to ~30 min old).

## ✨ Taking control: the manual commit (Smart Commit)

Being lazy doesn't mean you don't do important things. Say you just finished something big
(e.g. *the payment gateway*) and want it **closed and documented now**, without waiting 6 h.

1. In the panel, hit **"Commit…"** on that repo.
2. SincroGit looks at everything you've touched **since your last manual commit** and the
   AI proposes a **clean title + a bulleted list** of the changes.
3. You read it, nod (the AI writes better than you do at 6 PM on a Friday) and, if you
   like, edit it. You accept.
4. That bundle gets pushed and the **6 h timer resets**. Back to procrastinating.

> No mouse? From the terminal: `python -m sincrogit -c config.yaml --commit myrepo`
> (it opens the proposed message in your editor so you can tweak it).

## ⏪ Help, I broke something! The time machine

You deleted a vital function, saved out of reflex (`Ctrl+S`)… and then realize the
disaster. Don't panic.

1. Open the panel → **"File History"**.
2. Pick the file you messed up.
3. You'll see **all** its versions (including the secret drafts from 15 min ago), with a
   **red/green diff** against how it is now.
4. Pick the one that worked, **Restore**, and SincroGit brings the file back to life. (If
   you really made a mess, you can also restore the **whole repo**.)

## ⚠️ Golden rules for peace of mind

Just four things to remember:

1. **Huge files and photos:** SincroGit ignores heavy images and binaries (it only
   versions text under 1 MB). Want to add an image? A manual `git add photo.jpg` and done;
   it'll say "sure, fine" and include it in the next bundle.
   *(Word docs? Those you CAN version with a readable diff: add `**/*.docx` to
   `extra_includes` in the config — needs [pandoc](https://pandoc.org). See the
   [README](README.md). Versioned when you change text or structure — purely visual
   styling like font or color doesn't count.)*
2. **Conflicts ("I stepped on myself"):** if you worked on both machines without syncing,
   SincroGit won't guess which version wins. Since it's **never** destructive, it pauses
   (red icon) and asks for help. Fix it in your editor and hit **"Resume"**.
3. **Branch switch:** if you jump to another branch from the terminal (`git checkout
   tests`), SincroGit is smart enough to **pause** and not pollute your experiments. When
   you go back to your usual branch, it resumes. *(Power user on feature branches? Set
   `track_current_branch: true` and it will **follow** each branch instead of pausing.)*
4. **Don't keep the repo inside Dropbox / OneDrive / Drive.** Those tools can **corrupt**
   the `.git` by syncing at the same time. Let SincroGit handle Git, and let the other tool
   handle *other* files.

## 🚫 What it does NOT do (so there are no surprises)

- It doesn't merge work from **two machines at once** (it's turn-based).
- It doesn't sync **instantly** between machines — it's a few minutes' relay (see above).
- It doesn't rescue **unsaved** work — it versions what you've **saved** to disk (your
  editor's autosave handles the rest). A power cut with an intact disk loses nothing anyway.
- It doesn't version **binaries or files > 1 MB** automatically (those, by hand).
- It's not a **full backup**: it keeps your text code, not the whole folder.
- It doesn't resolve conflicts for you: it warns you and you resolve them.
- On a **total** disk failure (rare) you can lose **up to ~30 min** (not zero).

---

That's it. Close this guide, open your editor, and relax: the dirty work —saving, syncing,
labeling— is on it. 🦥
