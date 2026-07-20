"""Thin wrapper over the git CLI (via subprocess).

The CLI is used instead of GitPython because the fine-grained operations
(plumbing snapshots, sealing, ref surgery) are clearer and more predictable.
See §7 of DESIGN.md.

Two-tier SHADOW model (see §2 of DESIGN.md):
  - Snapshot: a commit built through a PRIVATE index and appended every few
    minutes to the side ref refs/sincro/wip/<branch> — the user's HEAD, index
    and `git status` are never touched.
  - Sealed: periodically (seal_interval_min, default 6 h) the accumulated
    snapshot tree becomes ONE real commit on the branch, and the shadow chain
    re-anchors there (the old chain stays in the side ref's reflog ~30 days).
"""

import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from .convert import pptx_available, pptx_bytes_to_md

log = logging.getLogger("sincrogit.git")


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a child process AND its descendants. `proc.kill()` alone kills only the
    direct child (git.exe); on Windows its children (ssh.exe, git-remote-https.exe)
    would survive as orphans, so use `taskkill /T` to take down the whole tree.
    Best-effort — never raises."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            proc.kill()
    except Exception:  # noqa: BLE001 — cleanup must never raise
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def autosnap_host() -> str:
    """This machine's name, sanitized for use inside a git ref path.

    The autosnap ref is per-user AND per-machine
    (refs/autosnap/<user>/<host>/<branch>) so two machines mirroring the same
    repo never clobber each other, and a machine can tell its OWN other machines'
    mirrors apart from a teammate's (see autosnap_ref / sincro_user).
    """
    name = socket.gethostname() or "host"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._-")
    return name or "host"


def resolve_pandoc(pandoc_path: str | None) -> str | None:
    """Return a working pandoc command (forward-slashed) or None if unavailable.

    Tries the configured path/command, then PATH. Used to render readable diffs of
    binary documents (.docx, ...) via a git textconv driver. Forward slashes so the
    path survives git's internal shell on Windows.
    """
    if not pandoc_path:
        return None
    for cand in (pandoc_path, shutil.which(pandoc_path)):
        if not cand:
            continue
        try:
            res = subprocess.run(
                [cand, "--version"], capture_output=True, text=True,
                timeout=10, creationflags=_NO_WINDOW,
            )
            if res.returncode == 0:
                return cand.replace("\\", "/")
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None

# On Windows, a windowed (--noconsole) app spawns a console window for every
# child process. CREATE_NO_WINDOW suppresses that flash for each git call.
# (0 on other platforms, where it has no effect.)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class GitError(RuntimeError):
    pass


class GitRepo:
    # Message new (transient) WIP commits are created with: the `sincro:` prefix
    # marks it as SincroGit's, same as the seals, so every machine commit is
    # recognizable at a glance in `git log`.
    WIP_MESSAGE = "sincro: WIP autosnapshot"
    # DETECTION accepts the legacy message too: repos that were running before the
    # rename still have a live WIP with the old text at HEAD — failing to recognize
    # it would bake it into history as an "external commit". Never trim the legacy
    # entry while old WIPs can exist in reflogs/autosnap refs.
    WIP_PREFIXES = (WIP_MESSAGE, "WIP: autosnapshot")

    def __init__(self, path: str, pandoc: str | None = None, pandoc_provider=None):
        self.path = path
        # Resolved pandoc command (or None). If set, git diffs get a textconv
        # driver named 'pandoc' so .docx (mapped via .gitattributes) diff readably.
        self._pandoc = pandoc
        # Lazy resolver: a callable () -> (path | None), invoked the FIRST time this
        # repo actually handles a .docx. So a repo never runs `pandoc --version`
        # unless/until a .docx shows up; after that the result is fixed.
        self._pandoc_provider = pandoc_provider
        self._pandoc_resolved = pandoc is not None  # an explicit value is "resolved"
        # The .git dir is stable for the repo's lifetime: cache it so is_busy()
        # is pure os.path.exists after the first call (the idle-wait computation
        # consults it every tick — it must not spawn a subprocess).
        self._git_dir_cache = None
        self._empty_tree_cache = None  # the empty-tree oid (repos with no commits)

    def _ensure_pandoc(self):
        """Resolve pandoc on first actual .docx use (lazy, once). After this call
        self._pandoc is fixed — the command, or None if pandoc isn't available —
        and is never re-resolved (so we don't re-probe on every snapshot)."""
        if self._pandoc_resolved:
            return
        self._pandoc_resolved = True
        if self._pandoc_provider is not None:
            self._pandoc = self._pandoc_provider()

    # ----------------------------------------------------------------- core
    def _run(
        self,
        args: list,
        check: bool = True,
        stdin_data: str | None = None,
        timeout: float | None = None,
        extra_env: dict | None = None,
    ):
        cmd = [
            "git",
            "-c", "core.quotepath=false",        # don't octal-escape non-ASCII paths
            "-c", "i18n.logOutputEncoding=utf-8",  # read log/diff output as UTF-8...
            "-c", "i18n.commitEncoding=utf-8",     # ...regardless of repo locale
        ]
        if self._pandoc:
            # Provide the textconv command inline (per-call) so no persistent
            # `git config` is needed on any machine. Only takes effect for paths
            # mapped to `diff=pandoc` in .gitattributes (e.g. *.docx). The path is
            # quoted in case it contains spaces (git runs textconv via its shell).
            cmd += ["-c", f'diff.pandoc.textconv="{self._pandoc}" '
                          f'--from=docx --to=markdown --wrap=none']
        cmd += list(args)
        env = {
            **os.environ,
            # If credentials are missing, git fails fast instead of waiting for
            # interactive input that will never arrive.
            "GIT_TERMINAL_PROMPT": "0",
            # Git messages always in English -> consistent logs regardless of the
            # system language. (Doesn't affect our parsing, which uses
            # 'porcelain'/'plumbing' commands that are locale-independent.)
            "LC_ALL": "C",
            "LANG": "C",
            # The shadow snapshots run against a private index (GIT_INDEX_FILE),
            # never the user's — see the shadow section.
            **(extra_env or {}),
        }
        # Popen (not subprocess.run) so a timeout can kill the WHOLE process tree:
        # subprocess.run only kills the direct child (git.exe), and on Windows its
        # children (ssh.exe, git-remote-https.exe) would linger as orphans holding
        # the network connection (and potentially a lock). See §11 of DESIGN.md.
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self.path,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=_NO_WINDOW,  # no console flash on Windows
            )
        except OSError as e:
            # The repo folder vanished (unplugged drive, moved cloud folder) or git
            # itself isn't runnable. Surface it as GitError — the engine's per-repo
            # error handling catches GitError, while a raw OSError would kill the
            # whole engine thread (silently, under the windowed GUI).
            raise GitError(f"`git {' '.join(args)}` could not start: {e}") from e
        try:
            out, err = proc.communicate(input=stdin_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            try:
                proc.communicate(timeout=10)  # reap once the tree is gone
            except subprocess.TimeoutExpired:
                pass
            raise GitError(f"`git {' '.join(args)}` timed out ({timeout}s)")
        res = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        if check and res.returncode != 0:
            # Some git commands report the failure on stdout (e.g. a few hook and
            # remote errors); without the fallback the message would be empty.
            detail = res.stderr.strip() or res.stdout.strip()
            raise GitError(
                f"`git {' '.join(args)}` failed (code {res.returncode}): {detail}"
            )
        return res

    # ------------------------------------------------------------- queries
    def is_git_repo(self) -> bool:
        res = self._run(["rev-parse", "--is-inside-work-tree"], check=False)
        return res.returncode == 0 and res.stdout.strip() == "true"

    def current_branch(self) -> str | None:
        res = self._run(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def has_any_commit(self) -> bool:
        return self._run(["rev-parse", "--verify", "HEAD"], check=False).returncode == 0

    def head_has_parent(self) -> bool:
        return self._run(["rev-parse", "--verify", "HEAD~1"], check=False).returncode == 0

    def head_message(self) -> str | None:
        res = self._run(["log", "-1", "--format=%s"], check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def head_is_wip(self) -> bool:
        msg = self.head_message()
        return msg is not None and msg.startswith(self.WIP_PREFIXES)

    def commit_time(self, ref: str) -> int | None:
        """UNIX timestamp (committer date) of the commit, or None."""
        res = self._run(["log", "-1", "--format=%ct", ref], check=False)
        if res.returncode != 0 or not res.stdout.strip():
            return None
        try:
            return int(res.stdout.strip())
        except ValueError:
            return None

    def last_sealed_time(self) -> int | None:
        """Timestamp of the last SEALED (non-WIP) commit, for the sealing clock."""
        if self.head_is_wip():
            return self.commit_time("HEAD~1") if self.head_has_parent() else None
        if self.has_any_commit():
            return self.commit_time("HEAD")
        return None

    def _git_dir(self) -> str:
        if self._git_dir_cache is None:
            res = self._run(["rev-parse", "--git-dir"], check=False)
            gd = res.stdout.strip() or ".git"
            self._git_dir_cache = gd if os.path.isabs(gd) else os.path.join(self.path, gd)
        return self._git_dir_cache

    def is_busy(self) -> bool:
        """Is there a manual git operation in progress? (merge/rebase/cherry-pick/lock)

        In that case the daemon yields and skips the cycle. See §11 of DESIGN.md.
        """
        gd = self._git_dir()
        markers = (
            "MERGE_HEAD",
            "rebase-merge",
            "rebase-apply",
            "CHERRY_PICK_HEAD",
            "BISECT_LOG",
            "index.lock",
        )
        return any(os.path.exists(os.path.join(gd, m)) for m in markers)

    # A real git command holds index.lock for seconds; one this old is a
    # leftover from a crash (of git, the machine, or a force-killed daemon).
    STALE_LOCK_SEC = 3600

    def stale_lock(self, max_age_sec: float = STALE_LOCK_SEC) -> str | None:
        """Path of a `.git/index.lock` untouched for `max_age_sec`, else None.

        A crash strands the lock, is_busy() then reports busy FOREVER and the
        daemon never syncs again — with nothing telling the user why. Detection
        only: deleting a lock is the user's call (a git command could still be
        legitimately running), so --doctor and the long-busy warning surface it
        with instructions instead."""
        lock = os.path.join(self._git_dir(), "index.lock")
        try:
            age = time.time() - os.path.getmtime(lock)
        except OSError:
            return None  # no lock (or unreadable): nothing to report
        return lock if age >= max_age_sec else None

    # --------------------------------------------------- crash self-healing
    _SHA_RE = re.compile(r"[0-9a-f]{40}")

    def repair_corrupt_refs(self, branch: str) -> list:
        """Self-heal the tiny ref files a power cut can zero out.

        NTFS metadata often survives a crash while the last small write is lost:
        `.git/HEAD` or `.git/refs/heads/<branch>` ends up the right SIZE but full
        of NUL bytes, and git fails with "your current branch appears to be
        broken". The reflog is append-only and survives, so its newest entry
        whose commit still exists IS the pre-crash state — restore the ref from
        it (exactly the manual recovery, automated).

        Conservative on purpose: only touches a ref that does NOT resolve (a
        broken ref is unusable, so repairing can only improve things); restores
        only from the ref's OWN reflog (never guesses from another branch's
        history); leaves everything alone when there is nothing trustworthy to
        restore from. Returns human-readable descriptions of the repairs made
        ([] = nothing was wrong). Best-effort: never raises.
        """
        repairs = []
        try:
            gd = self._git_dir()

            # --- HEAD itself (a symbolic ref in a tiny file) ---
            head_path = os.path.join(gd, "HEAD")
            try:
                with open(head_path, "rb") as fh:
                    head_txt = fh.read(256).decode("ascii", errors="replace").strip()
            except OSError:
                head_txt = ""
            head_ok = head_txt.startswith("ref: ") or bool(self._SHA_RE.fullmatch(head_txt))
            if not head_ok:
                # Re-point at the configured branch (the only branch the engine
                # operates on; with a zeroed HEAD the previous branch is unknowable).
                with open(head_path, "w", encoding="ascii", newline="\n") as fh:
                    fh.write(f"ref: refs/heads/{branch}\n")
                repairs.append(f"HEAD was corrupt; re-pointed at refs/heads/{branch}")
                head_txt = f"ref: refs/heads/{branch}"

            # --- the branch HEAD references, and its shadow ref (both are small
            # loose files a power cut can zero; each has its own reflog) ---
            target = branch
            if head_txt.startswith("ref: refs/heads/"):
                target = head_txt[len("ref: refs/heads/"):].strip() or branch
            for ref in (f"refs/heads/{target}", self.shadow_ref(target)):
                self._repair_one_ref(gd, ref, repairs)
        except Exception as e:  # noqa: BLE001 — healing must never block startup
            log.warning("ref auto-repair skipped: %s", e)
        return repairs

    def _repair_one_ref(self, gd: str, ref: str, repairs: list) -> None:
        """Restore ONE zeroed/corrupt ref from its own reflog (see above)."""
        loose = os.path.join(gd, *ref.split("/"))
        if self._run(["rev-parse", "--verify", "--quiet", ref], check=False).returncode == 0:
            return  # resolves fine — nothing to do
        if not os.path.exists(loose) and not os.path.exists(
                os.path.join(gd, "logs", *ref.split("/"))):
            return  # the ref never existed here (e.g. shadow not created yet)
        sha = self._last_good_reflog_sha(gd, ref)
        if not sha:
            return  # no trustworthy source: leave it for a human
        try:
            os.remove(loose)  # the zeroed file blocks update-ref's lock
        except OSError:
            pass
        self._run(["update-ref", ref, sha], check=False)
        if self._run(["rev-parse", "--verify", "--quiet", ref], check=False).returncode == 0:
            repairs.append(
                f"{ref} was corrupt (power cut?); restored from its reflog to {sha[:8]}")

    def _last_good_reflog_sha(self, gitdir: str, ref: str):
        """Newest entry in the ref's OWN reflog whose commit object still exists
        (the crash may also have truncated the newest loose object). None if the
        log is missing or nothing verifies — then we refuse to guess."""
        path = os.path.join(gitdir, "logs", *ref.split("/"))
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            parts = line.split()
            if len(parts) < 2:
                continue
            new = parts[1]
            if self._SHA_RE.fullmatch(new) and new != "0" * 40:
                if self._run(["cat-file", "-e", f"{new}^{{commit}}"], check=False).returncode == 0:
                    return new
        return None

    # ------------------------------------------------------------ mutations
    def ensure_gitattributes(self, lines=("* text=auto",)) -> list:
        """Ensure each given line is present in .gitattributes (append the missing
        ones, creating the file if needed). Existing content/comments are kept.
        Returns the lines actually added (empty list if all were already there).

        Used for: '* text=auto' (normalize line endings so a CRLF/LF-only change
        isn't seen as an edit across machines) and '*.docx -text diff=pandoc' (map
        Word docs to the pandoc textconv diff driver and keep them out of EOL
        normalization). Written with LF.
        """
        path = os.path.join(self.path, ".gitattributes")
        raw = ""
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError:
                return []
        present = {ln.strip() for ln in raw.splitlines()}
        to_add = [ln for ln in lines if ln.strip() and ln.strip() not in present]
        if not to_add:
            return []
        try:
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                if not raw:
                    fh.write("# Added by SincroGit.\n")
                elif not raw.endswith("\n"):
                    fh.write("\n")
                for ln in to_add:
                    fh.write(ln + "\n")
            return to_add
        except OSError:
            return []

    def list_tracked(self, paths: list) -> set:
        """Subset of `paths` present in the SHADOW index (i.e. snapshotted).

        Used to tell apart "a snapshotted file dropped out of auto-snapshot"
        from "a brand-new file we never versioned". `paths` is small here (only
        rejected files), so passing them as arguments is fine.
        """
        if not paths:
            return set()
        res = self._run(["ls-files", "-z", "--", *paths], check=False,
                        extra_env=self._shadow_env())
        return {p for p in res.stdout.split("\0") if p}

    def has_staged_changes(self) -> bool:
        # `diff --cached --quiet` => code 1 if something is staged, 0 otherwise.
        return self._run(["diff", "--cached", "--quiet"], check=False).returncode != 0

    # ------------------------------------------------------------ shadow model
    # Snapshots live OUTSIDE the user's view of the repo: each one is a commit
    # built through a PRIVATE index (GIT_INDEX_FILE = .git/sincro-index) and
    # recorded on refs/sincro/wip/<branch>. HEAD, the user's index and their
    # `git status` are never touched — `git log` stays clean and every git tool
    # sees a completely normal repository. At each seal the ref re-anchors to
    # the sealed commit; the pre-seal chain stays reachable through the ref's
    # reflog (~30 days), which is the time machine's intra-window memory.
    SNAPSHOT_MESSAGE = "sincro: snapshot"

    @staticmethod
    def shadow_ref(branch: str) -> str:
        return f"refs/sincro/wip/{branch}"

    def _shadow_env(self) -> dict:
        return {"GIT_INDEX_FILE": os.path.join(self._git_dir(), "sincro-index")}

    def _empty_tree(self) -> str:
        if self._empty_tree_cache is None:
            self._empty_tree_cache = self._run(["mktree"], stdin_data="").stdout.strip()
        return self._empty_tree_cache

    def head_sha(self) -> str | None:
        res = self._run(["rev-parse", "--verify", "--quiet", "HEAD"], check=False)
        return res.stdout.strip() or None

    def shadow_tip(self, branch: str) -> str | None:
        res = self._run(["rev-parse", "--verify", "--quiet", self.shadow_ref(branch)],
                        check=False)
        return res.stdout.strip() or None

    def tree_of(self, ref: str) -> str | None:
        res = self._run(["rev-parse", f"{ref}^{{tree}}"], check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    # Read-only introspection for `sincrogit status` — safe alongside a
    # running daemon (nothing here takes locks or writes the index).
    def ref_time(self, ref: str) -> float | None:
        """Committer epoch of `ref`'s tip commit, or None if it doesn't resolve."""
        res = self._run(["log", "-1", "--format=%ct", ref], check=False)
        out = res.stdout.strip().splitlines()
        try:
            return float(out[0]) if res.returncode == 0 and out else None
        except ValueError:
            return None

    def commits_ahead(self, base: str, tip: str) -> int | None:
        """How many commits `tip` holds beyond `base` (rev-list --count), or
        None when either ref doesn't resolve."""
        res = self._run(["rev-list", "--count", f"{base}..{tip}"], check=False)
        out = res.stdout.strip()
        return int(out) if res.returncode == 0 and out.isdigit() else None

    def worktree_differs_from(self, ref: str) -> bool:
        """Does the worktree differ from `ref`'s tree? GIT_OPTIONAL_LOCKS=0
        keeps git from opportunistically rewriting the index stat-cache, so
        this never contends with the daemon's own git work."""
        res = self._run(["diff", "--quiet", ref], check=False,
                        extra_env={"GIT_OPTIONAL_LOCKS": "0"})
        return res.returncode == 1

    def ensure_shadow(self, branch: str) -> bool:
        """Make sure the shadow ref exists (anchored at HEAD — or at an empty
        root snapshot in a repo with no commits) and that git RECORDS its
        reflog: side refs get none by default, and that reflog is both the
        time machine's memory and the crash-repair source. True if created."""
        # Local to this clone, invisible, idempotent. Without it update-ref
        # writes no reflog for refs outside refs/heads/.
        self._run(["config", "core.logAllRefUpdates", "always"], check=False)
        if self.shadow_tip(branch):
            return False
        head = self.head_sha()
        anchor = head or self._run(
            ["commit-tree", self._empty_tree(), "-m", self.SNAPSHOT_MESSAGE]
        ).stdout.strip()
        self._run(["update-ref", "-m", "sincro: anchor",
                   self.shadow_ref(branch), anchor])
        return True

    def sync_shadow_index(self, branch: str) -> str:
        """Point the private index at the shadow tip's tree; returns that tree.
        Cheap in steady state (the persistent index file is already there, with
        a warm stat cache); the full read-tree only happens after a seal
        re-anchor or an external surprise."""
        tip = self.shadow_tip(branch)
        tip_tree = self.tree_of(tip) if tip else self._empty_tree()
        env = self._shadow_env()
        if os.path.exists(env["GIT_INDEX_FILE"]):
            res = self._run(["write-tree"], check=False, extra_env=env)
            if res.returncode == 0 and res.stdout.strip() == tip_tree:
                return tip_tree
        self._run(["read-tree", tip_tree], extra_env=env)
        return tip_tree

    def shadow_changed_paths(self) -> list:
        """Paths whose WORKTREE content differs from the private index (i.e.
        from the last snapshot): tracked edits/deletions plus untracked files.
        The precise 'what changed since the last snapshot' — and, right AFTER a
        snapshot pass, the leftovers are exactly what the filter refused (the
        uncapturable content the restore guards must protect)."""
        env = self._shadow_env()
        out = self._run(["diff", "--name-only", "-z"], check=False,
                        extra_env=env).stdout
        paths = [p for p in out.split("\0") if p]
        out = self._run(["ls-files", "--others", "--exclude-standard", "-z"],
                        check=False, extra_env=env).stdout
        paths += [p for p in out.split("\0") if p]
        return paths

    def shadow_stage(self, paths: list) -> None:
        """`git add -A` of these paths into the PRIVATE index (adds, updates
        and deletions). Paths via stdin to dodge command-line length limits."""
        if not paths:
            return
        if any(p.lower().endswith(".docx") for p in paths):
            self._ensure_pandoc()  # the gating diff below needs the textconv
        data = "\0".join(paths) + "\0"
        self._run(["add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul"],
                  stdin_data=data, extra_env=self._shadow_env())

    def shadow_write_tree(self) -> str:
        return self._run(["write-tree"], extra_env=self._shadow_env()).stdout.strip()

    def trees_match(self, a: str, b: str) -> bool:
        """Tree-vs-tree comparison THROUGH the textconv drivers (`--quiet`
        honors them — verified in the phase-0 spike), so a .docx whose only
        change is visual styling still counts as unchanged."""
        if a == b:
            return True
        return self._run(["diff", "--quiet", a, b], check=False).returncode == 0

    def commit_shadow(self, branch: str, tree: str, parent: str | None) -> str:
        """One snapshot: a commit of `tree` appended to the shadow chain. The
        update-ref passes the expected old value, so a concurrent mover fails
        loudly instead of being silently overwritten."""
        args = ["commit-tree", tree, "-m", self.SNAPSHOT_MESSAGE]
        if parent:
            args += ["-p", parent]
        new = self._run(args).stdout.strip()
        upd = ["update-ref", "-m", "sincro: snapshot", self.shadow_ref(branch), new]
        if parent:
            upd.append(parent)
        self._run(upd)
        return new

    def migrate_wip_tip(self, branch: str) -> bool:
        """One-time migration from the old model: if HEAD is a legacy WIP
        commit, move it to the shadow chain and give the branch back to the
        WIP's parent (the last sealed commit). The worktree is untouched, and
        the user's index is refreshed — so the unsealed edits reappear as
        ordinary uncommitted changes, which is exactly what they are. A ROOT
        WIP (repo whose only commit is the WIP) is left in place: a branch
        must point somewhere, and one initial commit is harmless history.
        Returns True if a migration happened."""
        if not self.head_is_wip():
            return False
        wip = self.head_sha()
        self._run(["config", "core.logAllRefUpdates", "always"], check=False)
        self._run(["update-ref", "-m", "sincro: migrate",
                   self.shadow_ref(branch), wip])
        if self.head_has_parent():
            parent = self._run(["rev-parse", "HEAD~1"]).stdout.strip()
            self._run(["update-ref", "-m", "sincro: migrate",
                       f"refs/heads/{branch}", parent, wip])
            self._refresh_user_index()
        return True

    def reanchor_shadow(self, branch: str, sha: str) -> None:
        """Point the shadow ref at `sha` (a fresh seal, or a migrated WIP): the
        new chain starts there; the previous chain stays reachable through the
        ref's reflog for the usual ~30-day window."""
        old = self.shadow_tip(branch)
        args = ["update-ref", "-m", "sincro: reanchor", self.shadow_ref(branch), sha]
        if old:
            args.append(old)
        self._run(args)

    def graft_uncaptured(self, base_tree: str, tree: str) -> str:
        """Return `tree` (the shadow tree about to be sealed) with every entry
        of `base_tree` (HEAD's tree) that is MISSING from it grafted back in —
        provided the file still exists in the worktree.

        The shadow chain is filtered on purpose (no binaries, no oversize
        files), so content the user committed BY HAND lives in HEAD's tree but
        never in the shadow's. Sealing the raw shadow tree over HEAD would
        record every such file as *deleted* — the exact opposite of the
        'a manual `git add photo.jpg` and done' promise. The worktree check
        keeps real deletions honest: a file the user removed from disk (whether
        we captured it or not) still drops out of the seal."""
        if not base_tree or base_tree == tree:
            return tree
        out = self._run(["diff-tree", "-r", "-z", "--no-renames",
                         "--diff-filter=D", base_tree, tree]).stdout
        toks = out.split("\0")
        lines = []
        for meta, path in zip(toks[::2], toks[1::2]):
            if not meta.startswith(":") or not path:
                continue
            mode, _new_mode, sha = meta[1:].split(" ")[:3]
            if os.path.lexists(os.path.join(self.path, path)):
                lines.append(f"{mode} {sha}\t{path}")
        if not lines:
            return tree
        # Throwaway index: never the user's, never the shadow's.
        env = {"GIT_INDEX_FILE": os.path.join(self._git_dir(), "sincro-graft-index")}
        try:
            self._run(["read-tree", tree], extra_env=env)
            self._run(["update-index", "-z", "--index-info"],
                      stdin_data="\0".join(lines) + "\0", extra_env=env)
            return self._run(["write-tree"], extra_env=env).stdout.strip()
        finally:
            try:
                os.remove(env["GIT_INDEX_FILE"])
            except OSError:
                pass

    def seal_from_shadow(self, branch: str, tree: str, *messages: str) -> str:
        """The REAL commit: `tree` (the latest snapshot) committed on top of
        HEAD with the seal message; the branch advances and the user's index is
        refreshed (mixed reset — their worktree, which IS `tree`, is untouched).
        Returns the new sha."""
        head = self.head_sha()
        args = ["commit-tree", tree]
        if head:
            args += ["-p", head]
        for m in messages:
            if m:
                args += ["-m", m]
        new = self._run(args).stdout.strip()
        upd = ["update-ref", "-m", "sincro: seal", f"refs/heads/{branch}", new]
        if head:
            upd.append(head)
        self._run(upd)
        self._refresh_user_index()  # user's index -> the new HEAD
        return new

    def _refresh_user_index(self) -> None:
        """Mixed `git reset` so the user's index tracks the just-moved HEAD.

        Must not fail the seal (the commit already exists), so no `check` — but
        it must not fail SILENTLY either: a stale index makes `git status` show
        phantom staged changes, and has_staged_changes() then postpones every
        future auto-seal "because of a manual commit in progress". Retry once
        (the realistic cause is a transient index.lock from an AV scan), then
        say what state that leaves the repo in and how to get out."""
        res = self._run(["reset", "-q"], check=False)
        if res.returncode != 0:
            time.sleep(0.5)
            res = self._run(["reset", "-q"], check=False)
        if res.returncode != 0:
            log.warning(
                "[%s] could not refresh the index after sealing (%s); `git "
                "status` will show stale staged changes and auto-seals will be "
                "postponed until a plain `git reset` succeeds",
                os.path.basename(self.path), res.stderr.strip() or "unknown error")

    def unmerged_paths(self) -> list:
        """Paths with unmerged index entries (conflict markers pending) — e.g.
        after an autostash pop that conflicted (the rebase itself SUCCEEDS in
        that case, so is_busy sees nothing; verified in the phase-0 spike)."""
        out = self._run(["ls-files", "-u", "-z"], check=False).stdout
        return sorted({ln.split("\t", 1)[1] for ln in out.split("\0")
                       if ln and "\t" in ln})

    def restore_paths_worktree(self, sha: str, paths: list) -> None:
        """Write these paths' content at `sha` into the WORKTREE only: the
        user's index is not touched, so `git status` shows the restore as
        ordinary modifications and the next snapshot captures it."""
        paths = [p.replace("\\", "/") for p in paths]
        for chunk in self._path_chunks(paths):
            self._run(["restore", "--source", sha, "--worktree", "--", *chunk])

    def delete_paths_worktree(self, paths: list) -> list:
        """Remove these files from the WORKTREE only (plain deletes, no git rm:
        the user's index stays theirs; their status shows the deletions).
        Returns the relpaths that could NOT be removed — on Windows a file open
        in another program can't be deleted — so the caller can SAY so instead
        of leaving a silently half-applied restore. Already-missing files are
        not failures: absent is the goal state."""
        failed = []
        for rel in paths:
            try:
                os.remove(os.path.join(self.path, rel.replace("/", os.sep)))
            except FileNotFoundError:
                pass  # already gone: that's the goal state
            except OSError:
                failed.append(rel)
        return failed

    def diff_trees_name_status(self, target: str, current: str) -> list:
        """[(status, path)] of a pure tree-vs-tree `git diff` (target ->
        current): 'A' = in current only, 'D' = in target only, 'M'/'T' =
        differs. Unlike a worktree diff it also covers files the user's HEAD
        doesn't track yet (snapshots know them). Raises GitError.

        --no-renames is DELIBERATE: this feeds the restore/handoff logic, which
        drives `git restore --source <target>` per path. Rename detection would
        collapse a move old->new into a single 'R old new' whose recorded path
        (new) does NOT exist in `target` — restoring it would delete new and
        never bring old back (silent wrong restore). Disabling renames yields
        'D old' + 'A new', both of which the callers handle correctly."""
        res = self._run(["diff", "--no-renames", "--name-status", target, current])
        out = []
        for line in res.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                out.append((parts[0][:1], parts[-1]))
        return out

    def name_status_for_seal(self, base: str, target: str) -> list:
        """[(status, path)] of the window being sealed: base (HEAD's tree, or
        the empty tree in a fresh repo) -> target (the latest snapshot tree).
        --no-renames so the file list matches what actually changed on disk (a
        move reads as a delete + an add, consistent with diff_trees_name_status
        and the restore logic) rather than a single 'R' the callers don't map."""
        res = self._run(["diff", "--no-renames", "--name-status", base, target])
        items = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            status = parts[0][0]  # first character: A/M/D/R...
            path = parts[-1]
            items.append((status, path))
        return items

    def gc_auto(self):
        """`git gc --auto`: packs loose objects only if the thresholds are exceeded.
        Important because each amend leaves orphan objects; without this, over
        months the repo would accumulate thousands of loose objects. With
        gc.autoDetach (the default), packing happens in the background and
        doesn't block.
        """
        try:
            self._run(["gc", "--auto"], check=False, timeout=120)
        except GitError:
            pass  # maintenance must never take down the cycle

    def last_manual_sha(self) -> str | None:
        """SHA of the most recent commit made by a HUMAN — i.e. not a WIP and not
        an automatic seal (prefix 'sincro:' or the legacy 'auto:'). Used to scope a
        manual commit's AI message to "everything since my last manual commit".
        Returns None if there's only machine commits / WIPs so far.
        """
        res = self._run(
            ["log", "--first-parent", "--format=%H%x09%s", "-n", "200"], check=False
        )
        for line in res.stdout.splitlines():
            sha, subj, _ = self._split3(line)
            if not sha:
                continue
            if subj.startswith(self.WIP_PREFIXES):
                continue
            if subj.startswith("sincro:") or subj.startswith("auto:"):
                continue
            return sha
        return None

    # ------------------------------------------------------------- diffs (AI)
    def diff_stat_for_seal(self, max_chars: int = 4000, base: str = "HEAD",
                           target: str = "HEAD") -> str:
        out = self._run(["diff", "--stat", base, target]).stdout
        if max_chars and len(out) > max_chars:
            out = out[:max_chars] + "\n... [stat truncated]"
        return out

    def diff_text_for_seal(self, max_chars: int, base: str = "HEAD",
                           target: str = "HEAD") -> str:
        out = self._run(["diff", base, target]).stdout
        if max_chars and len(out) > max_chars:
            out = out[:max_chars] + "\n... [diff truncated]"
        return out

    # -------------------------------------------------------------- network
    def has_remote(self, remote: str) -> bool:
        return self._run(["remote", "get-url", remote], check=False).returncode == 0

    def remote_url(self, remote: str) -> str | None:
        """The configured URL of `remote`, or None if it has none."""
        res = self._run(["remote", "get-url", remote], check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def set_remote(self, remote: str, url: str) -> None:
        """Point `remote` at `url`: add it, or update it if it already exists
        (idempotent, so re-running onboarding with a corrected URL just works).
        Git stores any string without validating it — whether the URL actually
        works is what configure_remote's ls-remote/push checks are for. Raises
        GitError only if git rejects the remote NAME."""
        if self.has_remote(remote):
            self._run(["remote", "set-url", remote, url])
        else:
            self._run(["remote", "add", remote, url])

    def fetch(self, remote: str, timeout: float | None = None) -> bool:
        res = self._run(["fetch", "--quiet", remote], check=False, timeout=timeout)
        if res.returncode != 0:
            log.warning("fetch of '%s' failed: %s", remote, res.stderr.strip())
        return res.returncode == 0

    def remote_branch_exists(self, remote: str, branch: str) -> bool:
        ref = f"refs/remotes/{remote}/{branch}"
        return self._run(["rev-parse", "--verify", "--quiet", ref], check=False).returncode == 0

    def commits_behind(self, remote: str, branch: str) -> int:
        """How many commits the remote has that we don't have locally."""
        res = self._run(
            ["rev-list", "--count", f"HEAD..{remote}/{branch}"], check=False
        )
        try:
            return int(res.stdout.strip())
        except (ValueError, AttributeError):
            return 0

    def has_unpushed_sealed(self, remote: str, branch: str) -> bool:
        """Are there local commits not already on the remote? (In the shadow
        model HEAD only holds sealed/user commits, so HEAD is the yardstick.)"""
        if not self.head_sha():
            return False
        res = self._run(
            ["rev-list", "--count", f"{remote}/{branch}..HEAD"], check=False
        )
        try:
            return int(res.stdout.strip()) > 0
        except (ValueError, AttributeError):
            return True  # when in doubt, attempt the push

    def rebase_onto_remote(self, remote: str, branch: str) -> tuple:
        """Rebase the local branch onto the remote, autostashing the dirty
        worktree (in the shadow model the user's edits live there, uncommitted).

        Returns (ok, dirty_conflict):
          (True, False)  — clean: rebased, autostash re-applied.
          (True, True)   — the REBASE succeeded but re-applying the dirty edits
                           conflicted: git leaves conflict markers in the tree
                           and a stash entry, and the repo is NOT mid-rebase
                           (verified in the phase-0 spike) — the caller must
                           pause + explain, or an auto-seal would seal markers.
          (False, False) — the rebase itself conflicted; it was aborted so the
                           repo is left intact. See §3.4 / §11 of DESIGN.md.
        """
        res = self._run(
            ["rebase", "--autostash", f"{remote}/{branch}"], check=False
        )
        if res.returncode == 0:
            return True, bool(self.unmerged_paths())
        # Conflict or other failure: abort so the repo isn't left half-done.
        self._run(["rebase", "--abort"], check=False)
        log.warning("rebase onto %s/%s failed: %s", remote, branch, res.stderr.strip())
        return False, False

    def push_sealed(self, remote: str, branch: str, timeout: float | None = None):
        """Push the branch. In the shadow model HEAD only ever holds sealed and
        user commits (snapshots live on the side ref), so pushing HEAD is safe
        by construction. Returns (ok: bool, message: str); an unpushed backlog
        rides along implicitly (retry on the next sync).
        """
        head = self.head_sha()
        if not head:
            return True, "nothing to push (no commits yet)"
        res = self._run(
            ["push", remote, f"{head}:refs/heads/{branch}"],
            check=False,
            timeout=timeout,
        )
        msg = (res.stderr.strip() or res.stdout.strip())
        return res.returncode == 0, msg

    def ls_remote_heads(self, remote: str, timeout: float | None = None) -> tuple:
        """Probe READ reachability of a remote (no worktree touch): `git ls-remote
        --heads`. Returns (ok, detail); detail is the first error line on failure.
        A public entry point for --doctor (it must not reach into _run)."""
        res = self._run(["ls-remote", "--heads", remote], check=False, timeout=timeout)
        if res.returncode == 0:
            return True, ""
        err = (res.stderr or res.stdout).strip().splitlines()
        return False, (err[0] if err else "unreachable")

    def push_dry_run(self, remote: str, branch: str, timeout: float | None = None) -> tuple:
        """Probe WRITE (push) auth without transferring anything: `git push
        --dry-run`. Returns (ok, detail); detail is the last error line on failure.
        For --doctor, to catch credentials that would fail the real push/autosnap."""
        res = self._run(["push", "--dry-run", remote, f"{branch}:{branch}"],
                        check=False, timeout=timeout)
        if res.returncode == 0:
            return True, ""
        detail = (res.stderr or res.stdout).strip().splitlines()
        return False, (detail[-1] if detail else "?")

    # --------------------------------------------------------- autosnap (mirror)
    def sincro_user(self) -> str:
        """A stable identity for "the same person across machines", sanitized for a
        git ref path. Uses this repo's `git config user.email` (the same on all your
        machines) and falls back to the OS user. Lets a machine recognize its OWN
        other machines' live mirrors (vs. a teammate's) for cross-machine handoff."""
        res = self._run(["config", "user.email"], check=False)
        raw = res.stdout.strip()
        if not raw:
            try:
                import getpass
                raw = getpass.getuser()
            except Exception:  # noqa: BLE001
                raw = "user"
        return re.sub(r"[^A-Za-z0-9._-]", "_", raw).strip("._-") or "user"

    @staticmethod
    def autosnap_ref(user: str, host: str, branch: str) -> str:
        # Namespaced by user AND host: the user component lets your machines find
        # each other's mirrors for handoff; the host component keeps two of your
        # machines from clobbering the same ref (each is the sole writer of its own).
        return f"refs/autosnap/{user}/{host}/{branch}"

    def push_autosnap(self, remote: str, branch: str, user: str, host: str,
                      timeout: float | None = None):
        """Force-push the SHADOW TIP (sealed history + the live snapshots) to
        this machine's autosnap side ref, so the latest local state survives a
        total disk failure AND your other machines can pick it up (handoff).
        The ref is single-writer (only this host writes it), so a plain --force
        is safe and it never touches the clean `branch`. Returns (ok, msg).
        """
        tip = self.shadow_tip(branch)
        if not tip:
            return True, "nothing to mirror (no snapshots yet)"
        ref = self.autosnap_ref(user, host, branch)
        res = self._run(
            ["push", "--force", remote, f"{tip}:{ref}"], check=False, timeout=timeout
        )
        msg = (res.stderr.strip() or res.stdout.strip())
        return res.returncode == 0, msg

    def fetch_autosnaps(self, remote: str, user: str | None = None,
                        timeout: float | None = None) -> bool:
        """Fetch autosnap refs into local refs/autosnap/*. With `user`, fetch only
        that user's machines (for handoff: cheap, and ignores teammates' mirrors);
        without it, fetch everyone's (for cross-machine disaster recovery). True on
        success."""
        spec = (f"+refs/autosnap/{user}/*:refs/autosnap/{user}/*" if user
                else "+refs/autosnap/*:refs/autosnap/*")
        res = self._run(
            ["fetch", "--quiet", remote, spec], check=False, timeout=timeout,
        )
        if res.returncode != 0:
            log.warning("fetch of autosnap refs from '%s' failed: %s", remote, res.stderr.strip())
        return res.returncode == 0

    def list_autosnap_refs(self) -> list:
        """Local refs/autosnap/* as dicts: ref, user, host, branch, sha, epoch,
        subject. Newest first. Present only after fetch_autosnaps (or this host's
        own)."""
        # NOTE: for-each-ref does NOT understand %x09 (that's git-log syntax); use a
        # literal tab as the field separator.
        fmt = "--format=%(refname)\t%(objectname)\t%(committerdate:unix)\t%(contents:subject)"
        res = self._run(["for-each-ref", fmt, "refs/autosnap/"], check=False)
        out = []
        for line in res.stdout.splitlines():
            ref, sha, ct, subj = (line.split("\t", 3) + ["", "", "", ""])[:4]
            if not ref or not sha:
                continue
            # New layout: refs/autosnap/<user>/<host>/<branch> (branch may contain
            # '/'). Legacy layout (no user): refs/autosnap/<host>/<branch>.
            parts = ref[len("refs/autosnap/"):].split("/")
            if len(parts) >= 3:
                user, host, br = parts[0], parts[1], "/".join(parts[2:])
            elif len(parts) == 2:
                user, host, br = "", parts[0], parts[1]
            else:
                continue
            out.append({
                "ref": ref, "user": user, "host": host, "branch": br, "sha": sha,
                "epoch": int(ct) if ct.isdigit() else 0, "subject": subj,
            })
        out.sort(key=lambda e: e["epoch"], reverse=True)
        return out

    def local_branches(self) -> set:
        """Names of the local branches (refs/heads/*)."""
        res = self._run(["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
                        check=False)
        return {b.strip() for b in res.stdout.splitlines() if b.strip()}

    # Minimum mirror age before a stale autosnap ref may be pruned. Generous so a
    # freshly re-cloned repo (disaster recovery) never prunes states for branches
    # it simply hasn't recreated yet.
    AUTOSNAP_PRUNE_AGE_SEC = 7 * 86400

    def prune_autosnap_refs(self, remote: str, user: str, host: str,
                            min_age_sec: int = AUTOSNAP_PRUNE_AGE_SEC,
                            timeout: float | None = None) -> list:
        """Delete THIS machine's remote autosnap refs whose branch no longer exists
        locally (e.g. a deleted feature branch), so the remote doesn't accumulate
        dead mirrors forever. Conservative on purpose:
          - only refs/autosnap/<user>/<host>/* — this host is their sole writer,
            so the delete can't race anyone, and other machines' recovery states
            are never touched;
          - only mirrors at least `min_age_sec` old (see AUTOSNAP_PRUNE_AGE_SEC).
        Returns the pruned branch names. Best-effort (network)."""
        if not self.fetch_autosnaps(remote, user=user, timeout=timeout):
            return []
        branches = self.local_branches()
        now = time.time()
        removed = []
        for r in self.list_autosnap_refs():
            if r["user"] != user or r["host"] != host:
                continue  # never touch other machines' (or teammates') states
            if r["branch"] in branches:
                continue  # the branch is alive here: the mirror is current
            if not r["epoch"] or now - r["epoch"] < min_age_sec:
                continue  # too recent — or age UNKNOWN: never prune blind
            res = self._run(["push", remote, "--delete", r["ref"]],
                            check=False, timeout=timeout)
            if res.returncode == 0:
                self._run(["update-ref", "-d", r["ref"]], check=False)  # local copy
                removed.append(r["branch"])
        return removed

    # ------------------------------------------------------ cross-machine handoff
    def _diff_names(self, a: str, b: str) -> list:
        """Paths that differ between two commits (--no-renames, see
        work_relationship). Raises GitError on failure: a failed diff must
        surface as an error, never read as 'no differences' — the containment
        test below would take that as safe-to-apply."""
        res = self._run(["diff", "--no-renames", "--name-only", "-z", a, b])
        return [p for p in res.stdout.split("\0") if p]

    def _differs_on(self, a: str, b: str, paths: list) -> bool:
        """Do `a` and `b` differ on ANY of these paths? Chunked (_path_chunks):
        a snapshot can touch thousands of files — the agent-churn scenario —
        and Windows caps a command line at ~32k chars, so the paths can't ride
        as arguments in one call. Short-circuits on the first differing chunk.
        Raises GitError on failure (same fail-safe as _diff_names)."""
        for chunk in self._path_chunks(paths):
            res = self._run(["diff", "--no-renames", "--name-only", "-z", a, b,
                             "--", *chunk])
            if any(p for p in res.stdout.split("\0") if p):
                return True
        return False

    def work_relationship(self, mine: str, theirs: str) -> str:
        """Classify two commits by WORK CONTENT, not commit ancestry. Ancestry is
        useless across machines here: the WIP is continuously *amended*, so once a
        machine adopts a peer's WIP and edits, its new WIP is a SIBLING of the peer's
        (same parent = the shared seal), never a descendant. So instead we compare
        the paths each side changed since their merge base:

          'equal'           - same content
          'theirs_contains' - theirs matches mine on every path I changed (+ maybe
                              more) -> safe to adopt theirs, I lose nothing
          'mine_contains'   - I have all of theirs (+ maybe more) -> I'm ahead
          'diverged'        - each side changed a path the other doesn't match -> the
                              user must resolve by hand (no auto-merge)
        """
        if mine == theirs:
            return "equal"
        mb = self._run(["merge-base", mine, theirs], check=False).stdout.strip()
        if not mb:
            return "diverged"   # unrelated histories: never auto-apply
        # --no-renames throughout: a rename must read as delete-old + add-new on
        # BOTH sides so the per-path containment test below stays consistent with
        # how the handoff apply (diff_trees_name_status) later rewrites the tree.
        try:
            mine_changed = self._diff_names(mb, mine)
            theirs_changed = self._diff_names(mb, theirs)
            # "theirs has all my work" iff theirs == mine on every path I changed.
            theirs_has_mine = not (mine_changed
                                   and self._differs_on(mine, theirs, mine_changed))
            mine_has_theirs = not (theirs_changed
                                   and self._differs_on(mine, theirs, theirs_changed))
        except GitError as e:
            # Unknown must NEVER pass for equal: a failed diff classified as
            # "contained" would greenlight a handoff apply. Diverged only warns.
            log.warning("work_relationship: diff failed (%s); reporting 'diverged'", e)
            return "diverged"
        if theirs_has_mine and mine_has_theirs:
            return "equal"
        if theirs_has_mine:
            return "theirs_contains"
        if mine_has_theirs:
            return "mine_contains"
        return "diverged"

    def peer_wip(self, user: str, host: str, branch: str):
        """The newest live mirror that is MINE (same user) on ANOTHER machine
        (host != this one) for `branch`, from the locally-fetched refs. Returns the
        ref dict (see list_autosnap_refs) or None. Call fetch_autosnaps(user=...)
        first to refresh."""
        for r in self.list_autosnap_refs():   # already newest-first
            if r["user"] == user and r["branch"] == branch and r["host"] != host:
                return r
        return None

    def untracked_collisions(self, sha: str) -> list:
        """On-disk files the SNAPSHOTS don't hold (untracked relative to the
        shadow index — right after a snapshot pass that's exactly the filtered
        ones) that ARE tracked in `sha`'s tree. Used to refuse a handoff apply
        or a restore that would silently destroy unversioned content."""
        untracked = self._run(
            ["ls-files", "--others", "--exclude-standard", "-z"], check=False,
            extra_env=self._shadow_env(),
        ).stdout.split("\0")
        untracked = {p for p in untracked if p}
        if not untracked:
            return []
        in_tree = self._run(
            ["ls-tree", "-r", "--name-only", "-z", sha], check=False
        ).stdout.split("\0")
        return sorted(untracked.intersection(p for p in in_tree if p))

    # ------------------------------------------------------- history / restore
    @staticmethod
    def _split3(line: str):
        parts = (line.split("\t", 2) + ["", "", ""])[:3]
        return parts[0], parts[1], parts[2]

    def _blobs_at(self, shas: list, relpath: str) -> dict:
        """{sha: blob_oid} of `relpath` at each commit, resolved in ONE
        `cat-file --batch-check` call — one subprocess per commit (the old
        rev-parse loop) made opening a file's history take many seconds once
        the reflog filled up. Commits where the path is absent (or is a
        directory) are simply left out."""
        if not shas:
            return {}
        data = "".join(f"{sha}:{relpath}\n" for sha in shas)
        res = self._run(
            ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
            check=False, stdin_data=data,
        )
        out = {}
        # One output line per input line, in order: "<oid> blob" on success,
        # "<input> missing" (or an error tag) otherwise.
        for sha, line in zip(shas, res.stdout.splitlines()):
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "blob":
                out[sha] = parts[0]
        return out

    # Messages that mark a MACHINE state (a snapshot), old model or new. Used to
    # label history entries; head_is_wip (migration) keeps using WIP_PREFIXES.
    _SNAPSHOTISH = ("sincro: snapshot", "sincro: WIP autosnapshot", "WIP: autosnapshot")

    def file_history(self, relpath: str, limit: int = 50, branch: str = "main") -> list:
        """Distinct versions of a file, newest first.

        Combines three sources: the reachable history (sealed commits,
        permanent), the shadow chain + its reflog (intra-window snapshots,
        ~30 days) and any fetched autosnap refs (other machines' live mirrors).
        Versions with identical content are collapsed. Each item is a dict with:
        sha, blob, epoch, subject, source ('sealed' | 'snapshot' | 'autosnap').
        """
        relpath = relpath.replace("\\", "/")
        info = {}  # sha -> (epoch, commit subject)
        autosnap_label = {}  # sha -> host (these shas are shown as 'autosnap')

        # 1) Reachable history that touched the file (sealed + user commits).
        res = self._run(
            ["log", "--format=%H%x09%ct%x09%s", "--", relpath], check=False
        )
        for line in res.stdout.splitlines():
            sha, ct, subj = self._split3(line)
            if sha and ct.isdigit():
                info.setdefault(sha, (int(ct), subj))

        # 2) The shadow chain (current window's snapshots) and its reflog
        #    (pre-seal chains, ~30 days), bounded for performance.
        #    %s = the commit's own subject (not the reflog message).
        sref = self.shadow_ref(branch)
        for source_args in (["log", "-n", "500", sref],
                            ["log", "-g", "-n", "500", sref]):
            res = self._run(source_args + ["--format=%H%x09%ct%x09%s"], check=False)
            for line in res.stdout.splitlines():
                sha, ct, subj = self._split3(line)
                if sha and ct.isdigit() and sha not in info:
                    info[sha] = (int(ct), subj)

        # 3) Autosnap refs (other machines' live mirrors), only present after a
        #    recovery fetch. Each ref's tip is one extra recoverable state.
        for r in self.list_autosnap_refs():
            sha = r["sha"]
            info.setdefault(sha, (r["epoch"], f"autosnap: {r['host']}"))
            autosnap_label[sha] = r["host"]

        # Resolve the file blob at each commit (skip commits where it's absent),
        # all in a single git call (see _blobs_at). The kind is derived from the
        # source: autosnap refs first, then WIP commits are snapshots, the rest
        # are sealed (permanent) commits.
        blob_at = self._blobs_at(list(info.keys()), relpath)
        entries = []
        for sha, (epoch, subj) in info.items():
            blob = blob_at.get(sha)
            if not blob:
                continue
            if sha in autosnap_label:
                source, subject = "autosnap", f"(autosnap: {autosnap_label[sha]})"
            elif subj.startswith(self._SNAPSHOTISH):
                source, subject = "snapshot", "(auto-snapshot)"
            else:
                source, subject = "sealed", subj
            entries.append({
                "sha": sha,
                "blob": blob,
                "epoch": epoch,
                "subject": subject,
                "source": source,
            })

        entries.sort(key=lambda e: e["epoch"], reverse=True)

        # Collapse consecutive identical contents into one version.
        out = []
        last_blob = None
        for e in entries:
            if e["blob"] != last_blob:
                out.append(e)
                last_blob = e["blob"]
            if len(out) >= limit:
                break
        return out

    def repo_history(self, limit: int = 200, branch: str = "main") -> list:
        """Distinct WHOLE-REPO states, newest first — the version timeline of the
        Time Machine explorer. Same three sources as file_history (sealed
        history, shadow snapshots, fetched autosnap refs) but repo-wide: states
        with an identical tree are collapsed into one. Each item is a dict with:
        sha, tree, epoch, subject, source ('sealed' | 'snapshot' | 'autosnap').
        """
        info = {}  # sha -> (epoch, tree, subject)
        autosnap_label = {}  # sha -> host

        def _parse4(line):
            parts = (line.split("\t", 3) + ["", "", "", ""])[:4]
            return parts[0], parts[1], parts[2], parts[3]

        # 1) Reachable history (sealed + user commits), with tree oids.
        res = self._run(["log", "--format=%H%x09%ct%x09%T%x09%s"], check=False)
        for line in res.stdout.splitlines():
            sha, ct, tree, subj = _parse4(line)
            if sha and ct.isdigit():
                info.setdefault(sha, (int(ct), tree, subj))

        # 2) The shadow chain and its reflog (intra-window snapshots), bounded.
        sref = self.shadow_ref(branch)
        for source_args in (["log", "-n", "500", sref],
                            ["log", "-g", "-n", "500", sref]):
            res = self._run(source_args + ["--format=%H%x09%ct%x09%T%x09%s"],
                            check=False)
            for line in res.stdout.splitlines():
                sha, ct, tree, subj = _parse4(line)
                if sha and ct.isdigit() and sha not in info:
                    info[sha] = (int(ct), tree, subj)

        # 3) Autosnap refs (other machines' live mirrors; present after a fetch).
        #    Their trees aren't in the two logs above — one rev-parse per ref is
        #    fine (a handful of machines x branches at most).
        for r in self.list_autosnap_refs():
            sha = r["sha"]
            autosnap_label[sha] = r["host"]
            if sha in info:
                continue
            res = self._run(["rev-parse", f"{sha}^{{tree}}"], check=False)
            tree = res.stdout.strip() if res.returncode == 0 else ""
            if tree:
                info[sha] = (r["epoch"], tree, f"autosnap: {r['host']}")

        entries = []
        for sha, (epoch, tree, subj) in info.items():
            if sha in autosnap_label:
                source, subject = "autosnap", f"(autosnap: {autosnap_label[sha]})"
            elif subj.startswith(self._SNAPSHOTISH):
                source, subject = "snapshot", "(auto-snapshot)"
            else:
                source, subject = "sealed", subj
            entries.append({"sha": sha, "tree": tree, "epoch": epoch,
                            "subject": subject, "source": source})
        entries.sort(key=lambda e: e["epoch"], reverse=True)

        # Collapse consecutive identical repo states into one.
        out = []
        last_tree = None
        for e in entries:
            if e["tree"] != last_tree:
                out.append(e)
                last_tree = e["tree"]
            if len(out) >= limit:
                break
        return out

    def snapshot_timeline(self, branch: str = "main", limit: int = 200) -> list:
        """Per-state change lists for the Time machine tab, newest first. Each
        item: {sha, parent, epoch, subject, kind ('snapshot' | 'seal' |
        'autosnap'), files: [(status, path, adds, dels)]} — adds/dels are ints,
        or None for binary. Autosnap entries (other machines' fetched mirrors,
        this branch only) also carry "host".

        Same two walks as file_history (the live shadow chain + its reflog,
        both bounded), each run twice: --name-status for the A/M/D letter and
        --numstat for the +/− line counts (git has no single porcelain output
        carrying both). A commit's file list is its diff vs its FIRST parent:
        for a snapshot that's the previous snapshot — exactly "what this
        snapshot captured" — and for a seal, the previous branch tip (the
        whole sealed window). --no-renames matches the rest of the tooling
        (a move reads as D old + A new).
        """
        sref = self.shadow_ref(branch)
        fmt = "--format=%x01%H%x09%P%x09%ct%x09%s"
        # Walk only a little past what we'll show (states collapse by identical
        # tree, so allow headroom). Walking a flat 500 and slicing to `limit`
        # meant computing --name-status/--numstat for hundreds of commits that
        # never reach the rail — the biggest slice of the load time.
        depth = str(min(500, max(limit + 100, 120)))
        walks = (["log", "-n", depth], ["log", "-g", "-n", depth])

        def records(extra: str):
            """Yield (sha, parent, epoch, subject, [file lines]) per commit,
            deduped by sha across the two walks (first wins)."""
            seen = set()
            for walk in walks:
                res = self._run([*walk, fmt, "--no-renames", extra, sref],
                                check=False)
                for rec in res.stdout.split("\x01"):
                    lines = [ln for ln in rec.splitlines() if ln.strip()]
                    if not lines:
                        continue
                    head = (lines[0].split("\t", 3) + ["", "", "", ""])[:4]
                    sha, parents, ct, subj = head
                    if not sha or not ct.isdigit() or sha in seen:
                        continue
                    seen.add(sha)
                    yield sha, parents.split(" ")[0], int(ct), subj, lines[1:]

        entries = {}
        for sha, parent, epoch, subj, lines in records("--name-status"):
            files = []
            for ln in lines:
                parts = ln.split("\t")
                if len(parts) >= 2:
                    files.append([parts[0][:1], parts[-1], None, None])
            entries[sha] = {
                "sha": sha, "parent": parent, "epoch": epoch, "subject": subj,
                # EXACT match: snapshot commits carry the literal message; a
                # seal whose AI title merely STARTS with it must stay a seal.
                "kind": "snapshot" if subj in self._SNAPSHOTISH else "seal",
                "files": files,
            }
        for sha, _parent, _epoch, _subj, lines in records("--numstat"):
            e = entries.get(sha)
            if not e:
                continue
            counts = {}
            for ln in lines:
                parts = ln.split("\t")
                if len(parts) >= 3:
                    a, d = parts[0], parts[1]
                    counts[parts[-1]] = (int(a) if a.isdigit() else None,
                                         int(d) if d.isdigit() else None)
            for f in e["files"]:
                f[2], f[3] = counts.get(f[1], (None, None))

        # Other machines' fetched mirrors (recovery points) belong on the same
        # axis. Each is one commit; its file list is its diff vs its parent —
        # the same meaning as every other entry. A handful of refs at most, so
        # the two extra git calls per ref are cheap.
        for r in self.list_autosnap_refs():
            sha = r["sha"]
            if r["branch"] != branch or sha in entries:
                continue
            files = {}
            res = self._run(["log", "-1", "--no-renames", "--name-status",
                             "--format=", sha], check=False)
            for ln in res.stdout.splitlines():
                parts = ln.split("\t")
                if len(parts) >= 2 and parts[0].strip():
                    files[parts[-1]] = [parts[0][:1], parts[-1], None, None]
            res = self._run(["log", "-1", "--no-renames", "--numstat",
                             "--format=", sha], check=False)
            for ln in res.stdout.splitlines():
                parts = ln.split("\t")
                if len(parts) >= 3 and parts[-1] in files:
                    a, d = parts[0], parts[1]
                    files[parts[-1]][2] = int(a) if a.isdigit() else None
                    files[parts[-1]][3] = int(d) if d.isdigit() else None
            parent = self._run(["rev-parse", "--verify", "--quiet", f"{sha}^"],
                               check=False).stdout.strip()
            entries[sha] = {
                "sha": sha, "parent": parent, "epoch": r["epoch"],
                "subject": f"autosnap: {r['host']}", "kind": "autosnap",
                "host": r["host"], "files": list(files.values()),
            }

        out = sorted(entries.values(), key=lambda e: e["epoch"], reverse=True)
        for e in out:
            e["files"] = [tuple(f) for f in e["files"]]
        return out[:limit]

    def file_content_at(self, relpath: str, sha: str, max_bytes: int = 400_000) -> str | None:
        relpath = relpath.replace("\\", "/")
        res = self._run(["show", f"{sha}:{relpath}"], check=False)
        if res.returncode != 0:
            return None
        return res.stdout[:max_bytes]

    def file_bytes_at(self, relpath: str, sha: str) -> bytes | None:
        """RAW bytes of the file's version at `sha` — no text decoding, no size
        cap — for "Save a copy as…" exports (the recover-WITHOUT-overwriting
        path). None if the file doesn't exist in that version."""
        rel = relpath.replace("\\", "/")
        try:
            res = subprocess.run(
                ["git", "-C", self.path, "show", f"{sha}:{rel}"],
                capture_output=True, creationflags=_NO_WINDOW, timeout=60,
            )  # binary-safe: no text= decoding
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GitError(f"`git show {sha}:{rel}` failed: {e}") from e
        return res.stdout if res.returncode == 0 else None

    # ---------------------------------------------- readable text (docx, pptx, ...)
    def _text_converter(self, rel: str):
        """The bytes->markdown converter for this path, or None (plain text).
        .docx goes through pandoc (external, lazily resolved); .pptx through
        python-pptx (in-process, optional import). When a converter is
        unavailable the caller falls back to raw content — same degradation as
        versioning the file as an opaque blob."""
        low = rel.lower()
        if low.endswith(".docx"):
            self._ensure_pandoc()  # GUI may preview a .docx without one being staged
            return self._docx_bytes_to_md if self._pandoc else None
        if low.endswith(".pptx") and pptx_available():
            return pptx_bytes_to_md
        return None

    def file_text_at(self, relpath: str, sha: str, max_bytes: int = 400_000) -> str | None:
        """Readable text of a file version: markdown for .docx (pandoc) and
        .pptx (python-pptx); otherwise the raw content (file_content_at)."""
        rel = relpath.replace("\\", "/")
        converter = self._text_converter(rel)
        if converter is not None:
            try:
                res = subprocess.run(
                    ["git", "-C", self.path, "show", f"{sha}:{rel}"],
                    capture_output=True, creationflags=_NO_WINDOW, timeout=30,
                )  # binary blob (no text decode)
                if res.returncode == 0:
                    md = converter(res.stdout)
                    if md is not None:
                        return md[:max_bytes]
            except (OSError, subprocess.TimeoutExpired):
                pass  # fall back to the raw content below
        return self.file_content_at(rel, sha, max_bytes)

    def worktree_text(self, relpath: str, max_bytes: int = 400_000) -> str:
        """The working-tree file as readable text (markdown for .docx/.pptx).
        '' if missing."""
        rel = relpath.replace("\\", "/")
        full = os.path.join(self.path, rel.replace("/", os.sep))
        converter = self._text_converter(rel)
        if converter is not None:
            try:
                with open(full, "rb") as fh:
                    data = fh.read()
            except OSError:
                return ""
            md = converter(data)
            if md is not None:
                return md[:max_bytes]
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read(max_bytes)
        except OSError:
            return ""

    def _docx_bytes_to_md(self, data: bytes) -> str | None:
        """Convert .docx bytes to markdown via pandoc (through a temp file, the
        most reliable way for binary formats). None on failure."""
        if not data:
            return ""
        fd, tmp = tempfile.mkstemp(suffix=".docx")
        try:
            os.write(fd, data)
            os.close(fd)
            res = subprocess.run(
                [self._pandoc, "--from=docx", "--to=markdown", "--wrap=none", tmp],
                capture_output=True, timeout=30, creationflags=_NO_WINDOW,
            )
            if res.returncode != 0:
                return None
            return res.stdout.decode("utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            return None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # Batched path operations (restore_paths_worktree, _differs_on) run in
    # chunks: a selective restore can name many files, and Windows caps a
    # command line at ~32k characters. Both limits matter: 100 paths is the
    # comfortable count, but 100 DEEP paths can still blow past 32k, so the
    # chunker also enforces a byte budget (with headroom for the git call
    # itself and the repo path the harness prepends).
    _PATH_CHUNK = 100
    _CHUNK_BYTES = 24_000

    @classmethod
    def _path_chunks(cls, paths: list):
        """Yield slices of `paths` capped at _PATH_CHUNK entries AND
        _CHUNK_BYTES total characters (each path plus quoting/separator
        overhead). A single oversized path still travels alone — git itself
        is the one to reject it, with a real error."""
        chunk, size = [], 0
        for p in paths:
            cost = len(p) + 3  # separator + the quotes Windows may need
            if chunk and (len(chunk) >= cls._PATH_CHUNK
                          or size + cost > cls._CHUNK_BYTES):
                yield chunk
                chunk, size = [], 0
            chunk.append(p)
            size += cost
        if chunk:
            yield chunk
