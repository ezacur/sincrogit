"""Thin wrapper over the git CLI (via subprocess).

The CLI is used instead of GitPython because the fine-grained operations
(continuous amend, sealing, WIP detection) are clearer and more predictable.
See §7 of DESIGN.md.

Two-tier model (see §2 of DESIGN.md):
  - WIP (snapshot): a single commit at HEAD that is amended every few minutes.
  - Sealed: every 2h the WIP is "frozen" with a descriptive message and a new WIP is born.
"""

import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile

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

    The autosnap ref is per-machine (refs/autosnap/<host>/<branch>) so two
    machines mirroring the same repo never clobber each other.
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
    # Message prefix that identifies a (transient) WIP commit.
    WIP_MESSAGE = "WIP: autosnapshot"

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
            raise GitError(
                f"`git {' '.join(args)}` failed (code {res.returncode}): "
                f"{res.stderr.strip()}"
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
        return msg is not None and msg.startswith(self.WIP_MESSAGE)

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
        res = self._run(["rev-parse", "--git-dir"], check=False)
        gd = res.stdout.strip() or ".git"
        return gd if os.path.isabs(gd) else os.path.join(self.path, gd)

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

    def ensure_wip(self) -> bool:
        """Ensure HEAD is a WIP commit. Returns True if it created one."""
        if self.head_is_wip():
            return False
        # `--allow-empty` works even in a repo with no commits (creates the root).
        self._run(["commit", "--allow-empty", "-m", self.WIP_MESSAGE])
        return True

    def changed_paths(self) -> list:
        """Paths with changes (modified, new, deleted), one per entry."""
        res = self._run(
            ["status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all"]
        )
        paths = []
        for token in res.stdout.split("\0"):
            if not token:
                continue
            # porcelain v1 with -z: "XY PATH" (XY = 2 chars, then a space).
            path = token[3:]
            if path:
                paths.append(path)
        return paths

    def stage_changes(self, file_filter, on_drop=None, on_skip=None) -> bool:
        """Run `git add` ONLY on files that pass the filter.

        Deletions of already-tracked files are always staged.
        Returns True if anything was staged.

        `on_drop(relpath, reason)` is called for files that are ALREADY tracked
        but the filter now rejects (e.g. a text file that grew past the size
        limit) so the caller can warn the user. New untracked binaries/large
        files and user-configured excludes are skipped silently (expected).

        `on_skip(relpath, reason)` is called for EVERY filtered-out file whose
        reason isn't a user exclude (binary / too large), tracked or not — so the
        caller can spot a high-churn "noise" folder and suggest excluding it.
        """
        to_stage = []
        dropped = []  # (rel, reason) for existing files the filter rejected
        for rel in self.changed_paths():
            full = os.path.join(self.path, rel)
            if os.path.exists(full):
                reason = file_filter.reason_to_skip(full, rel)
                if reason is None:
                    to_stage.append(rel)
                else:
                    log.debug("filtered out (%s): %s", reason, rel)
                    dropped.append((rel, reason))
                    if on_skip is not None and reason != "excluded":
                        on_skip(rel, reason)
            else:
                # The file is no longer on disk => deletion of something tracked.
                to_stage.append(rel)

        # Only warn about TRACKED files that stopped being snapshotted, and not
        # for explicit excludes (those are intentional). The tracked check is one
        # extra git call, made only when something was dropped (usually nothing).
        if on_drop and dropped:
            reportable = [(r, why) for r, why in dropped if why != "excluded"]
            if reportable:
                tracked = self.list_tracked([r for r, _ in reportable])
                for rel, why in reportable:
                    if rel in tracked:
                        on_drop(rel, why)

        if not to_stage:
            return False

        # A .docx is about to be versioned -> make sure pandoc is resolved now, so
        # the caller's md-gating diff (has_staged_changes) already uses textconv.
        # This is the ONLY moment a .docx repo probes pandoc, and only if one shows
        # up (a no-op once resolved). See Engine._pandoc_cmd.
        if any(p.lower().endswith(".docx") for p in to_stage):
            self._ensure_pandoc()

        # Pass the paths via stdin (NUL-separated) to avoid command-line length
        # limits on Windows with many files.
        data = "\0".join(to_stage) + "\0"
        self._run(
            ["add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul"],
            stdin_data=data,
        )
        return True

    def list_tracked(self, paths: list) -> set:
        """Subset of `paths` that are already tracked (present in the index).

        Used to tell apart "a tracked file dropped out of auto-snapshot" from "a
        brand-new file we never versioned". `paths` is small here (only rejected
        files), so passing them as arguments is fine.
        """
        if not paths:
            return set()
        res = self._run(["ls-files", "-z", "--", *paths], check=False)
        return {p for p in res.stdout.split("\0") if p}

    def has_staged_changes(self) -> bool:
        # `diff --cached --quiet` => code 1 if something is staged, 0 otherwise.
        return self._run(["diff", "--cached", "--quiet"], check=False).returncode != 0

    def amend_keep_message(self):
        """Rewrite the WIP with the current index, keeping its message."""
        self._run(["commit", "--amend", "--no-edit"])

    def wip_differs_from_base(self) -> bool:
        """Does the WIP have content worth sealing?"""
        if self.head_has_parent():
            return self._run(["diff", "--quiet", "HEAD~1", "HEAD"], check=False).returncode != 0
        # Root commit: does it have any file?
        res = self._run(["ls-tree", "-r", "--name-only", "HEAD"], check=False)
        return bool(res.stdout.strip())

    def name_status_for_seal(self) -> list:
        """List of [(status, path)] for the changes in the window to be sealed."""
        if self.head_has_parent():
            res = self._run(["diff", "--name-status", "HEAD~1", "HEAD"])
        else:
            res = self._run(["show", "--name-status", "--format=", "HEAD"])
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

    def seal(self, *messages: str):
        """Turn the current WIP into a sealed commit (reword with --amend)."""
        args = ["commit", "--amend"]
        for m in messages:
            if m:
                args += ["-m", m]
        self._run(args)

    def new_wip(self):
        """Create a new, empty WIP on top of the last sealed commit."""
        self._run(["commit", "--allow-empty", "-m", self.WIP_MESSAGE])

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
            if subj.startswith(self.WIP_MESSAGE):
                continue
            if subj.startswith("sincro:") or subj.startswith("auto:"):
                continue
            return sha
        return None

    # ------------------------------------------------------------- diffs (AI)
    def diff_stat_for_seal(self, max_chars: int = 4000, base: str | None = None) -> str:
        if base:
            out = self._run(["diff", "--stat", base, "HEAD"]).stdout
        elif self.head_has_parent():
            out = self._run(["diff", "--stat", "HEAD~1", "HEAD"]).stdout
        else:
            out = self._run(["show", "--stat", "--format=", "HEAD"]).stdout
        if max_chars and len(out) > max_chars:
            out = out[:max_chars] + "\n... [stat truncated]"
        return out

    def diff_text_for_seal(self, max_chars: int, base: str | None = None) -> str:
        if base:
            out = self._run(["diff", base, "HEAD"]).stdout
        elif self.head_has_parent():
            out = self._run(["diff", "HEAD~1", "HEAD"]).stdout
        else:
            out = self._run(["show", "--format=", "HEAD"]).stdout
        if max_chars and len(out) > max_chars:
            out = out[:max_chars] + "\n... [diff truncated]"
        return out

    # -------------------------------------------------------------- network
    def has_remote(self, remote: str) -> bool:
        return self._run(["remote", "get-url", remote], check=False).returncode == 0

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

    def last_sealed_sha(self) -> str | None:
        """SHA of the most recent commit that is NOT a WIP.

        Robust to the user committing manually on top of the WIP: we never assume
        the positional HEAD~1 is the sealed commit. Returns the sealed commit, or
        a user's manual commit, or None if there's nothing but WIPs yet.
        """
        res = self._run(
            ["log", "--first-parent", "--format=%H%x09%s", "-n", "50"], check=False
        )
        for line in res.stdout.splitlines():
            sha, subj, _ = self._split3(line)
            if sha and not subj.startswith(self.WIP_MESSAGE):
                return sha
        return None

    def has_unpushed_sealed(self, remote: str, branch: str) -> bool:
        """Are there non-WIP commits not already on the remote?"""
        sealed = self.last_sealed_sha()
        if not sealed:
            return False
        res = self._run(
            ["rev-list", "--count", f"{remote}/{branch}..{sealed}"], check=False
        )
        try:
            return int(res.stdout.strip()) > 0
        except (ValueError, AttributeError):
            return True  # when in doubt, attempt the push

    def rebase_onto_remote(self, remote: str, branch: str) -> bool:
        """Rebase the local branch (including the WIP) onto the remote.

        Returns True if it was clean; False if there was a conflict (and the
        rebase is aborted to leave the repo intact). See §3.4 and §4 of DESIGN.md.
        """
        res = self._run(
            ["rebase", "--autostash", f"{remote}/{branch}"], check=False
        )
        if res.returncode == 0:
            return True
        # Conflict or other failure: abort so the repo isn't left half-done.
        self._run(["rebase", "--abort"], check=False)
        log.warning("rebase onto %s/%s failed: %s", remote, branch, res.stderr.strip())
        return False

    def push_sealed(self, remote: str, branch: str, timeout: float | None = None):
        """Push ONLY up to the last sealed commit (HEAD~1), never the WIP.

        Returns (ok: bool, message: str). Pushing the sealed commit also drags
        along any previously-unpushed sealed commits (implicit retry).
        """
        # Push the last NON-WIP commit (sealed or a user's manual commit), never
        # the transient WIP. The refspec source must be a real SHA and the dest
        # the full branch name (in case it doesn't exist on the remote yet).
        sealed = self.last_sealed_sha()
        if not sealed:
            return True, "nothing to push (no sealed commit yet)"
        res = self._run(
            ["push", remote, f"{sealed}:refs/heads/{branch}"],
            check=False,
            timeout=timeout,
        )
        msg = (res.stderr.strip() or res.stdout.strip())
        return res.returncode == 0, msg

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
        """Force-push HEAD (sealed history + the live WIP) to this machine's
        autosnap side ref, so the latest local state survives a total disk failure
        AND your other machines can pick it up (handoff). The ref is single-writer
        (only this host writes it), so a plain --force is safe and it never touches
        the clean `branch`. Returns (ok, msg).
        """
        ref = self.autosnap_ref(user, host, branch)
        res = self._run(
            ["push", "--force", remote, f"HEAD:{ref}"], check=False, timeout=timeout
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

    # ------------------------------------------------------ cross-machine handoff
    def head_sha(self) -> str | None:
        res = self._run(["rev-parse", "HEAD"], check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def _names(self, args: list) -> list:
        """`git <args>` (a --name-only -z diff) -> list of paths."""
        out = self._run(args, check=False).stdout
        return [p for p in out.split("\0") if p]

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
        mine_changed = self._names(["diff", "--name-only", "-z", mb, mine])
        theirs_changed = self._names(["diff", "--name-only", "-z", mb, theirs])
        # "theirs has all my work" iff theirs == mine on every path I changed.
        theirs_has_mine = not (mine_changed and self._names(
            ["diff", "--name-only", "-z", mine, theirs, "--", *mine_changed]))
        mine_has_theirs = not (theirs_changed and self._names(
            ["diff", "--name-only", "-z", mine, theirs, "--", *theirs_changed]))
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
        """Untracked working-tree files that a hard reset to `sha` would overwrite
        (they exist on disk AND are tracked in `sha`'s tree). Used to refuse an
        otherwise-safe fast-forward that would silently destroy unversioned files."""
        untracked = self._run(
            ["ls-files", "--others", "--exclude-standard", "-z"], check=False
        ).stdout.split("\0")
        untracked = {p for p in untracked if p}
        if not untracked:
            return []
        in_tree = self._run(
            ["ls-tree", "-r", "--name-only", "-z", sha], check=False
        ).stdout.split("\0")
        return sorted(untracked.intersection(p for p in in_tree if p))

    def fast_forward_wip(self, sha: str):
        """Move the current branch (and working tree) to `sha`. Caller MUST have
        verified this is loss-free — work_relationship(HEAD, sha) == 'theirs_contains',
        so `sha` matches my content on every path I changed — and that there are no
        untracked_collisions. Reversible via the reflog. Raises GitError."""
        self._run(["reset", "--hard", sha])

    def restore_tree(self, sha: str):
        """Make the working tree (and index) match the tree at `sha`, INCLUDING
        deleting tracked files that aren't present there. HEAD is NOT moved, so the
        restore is captured by the next snapshot (and stays reversible via the
        reflog). Untracked files are left untouched. Raises GitError on failure.
        """
        self._run(["read-tree", "-u", "--reset", f"{sha}^{{tree}}"])

    # ------------------------------------------------------- history / restore
    @staticmethod
    def _split3(line: str):
        parts = (line.split("\t", 2) + ["", "", ""])[:3]
        return parts[0], parts[1], parts[2]

    def file_history(self, relpath: str, limit: int = 50) -> list:
        """Distinct versions of a file, newest first.

        Combines three sources: the reachable history (sealed commits, permanent),
        the reflog (intra-window snapshots, ~30 days) and any fetched autosnap refs
        (other machines' live mirrors). Versions with identical content are
        collapsed. Each item is a dict with:
        sha, blob, epoch, subject, source ('sealed' | 'snapshot' | 'autosnap').
        """
        relpath = relpath.replace("\\", "/")
        info = {}  # sha -> (epoch, commit subject)
        autosnap_label = {}  # sha -> host (these shas are shown as 'autosnap')

        # 1) Reachable history that touched the file (sealed commits + current WIP).
        res = self._run(
            ["log", "--format=%H%x09%ct%x09%s", "--", relpath], check=False
        )
        for line in res.stdout.splitlines():
            sha, ct, subj = self._split3(line)
            if sha and ct.isdigit():
                info.setdefault(sha, (int(ct), subj))

        # 2) Reflog entries (intra-window snapshots), bounded for performance.
        #    %s = the commit's own subject (not the reflog message).
        res = self._run(
            ["log", "-g", "-n", "500", "--format=%H%x09%ct%x09%s"], check=False
        )
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

        # Resolve the file blob at each commit (skip commits where it's absent).
        # The kind is derived from the source: autosnap refs first, then WIP
        # commits are snapshots, the rest are sealed (permanent) commits.
        entries = []
        for sha, (epoch, subj) in info.items():
            blob = self._run(
                ["rev-parse", "--verify", "--quiet", f"{sha}:{relpath}"], check=False
            ).stdout.strip()
            if not blob:
                continue
            if sha in autosnap_label:
                source, subject = "autosnap", f"(autosnap: {autosnap_label[sha]})"
            elif subj.startswith(self.WIP_MESSAGE):
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

    def file_content_at(self, relpath: str, sha: str, max_bytes: int = 400_000) -> str | None:
        relpath = relpath.replace("\\", "/")
        res = self._run(["show", f"{sha}:{relpath}"], check=False)
        if res.returncode != 0:
            return None
        return res.stdout[:max_bytes]

    # --------------------------------------------------- readable text (docx, ...)
    def file_text_at(self, relpath: str, sha: str, max_bytes: int = 400_000) -> str | None:
        """Readable text of a file version. For .docx (with pandoc) it's the
        markdown rendering; otherwise the raw content (file_content_at)."""
        rel = relpath.replace("\\", "/")
        if rel.lower().endswith(".docx"):
            self._ensure_pandoc()  # GUI may preview a .docx without one being staged
        if self._pandoc and rel.lower().endswith(".docx"):
            try:
                res = subprocess.run(
                    ["git", "-C", self.path, "show", f"{sha}:{rel}"],
                    capture_output=True, creationflags=_NO_WINDOW, timeout=30,
                )  # binary blob (no text decode)
                if res.returncode == 0:
                    md = self._docx_bytes_to_md(res.stdout)
                    if md is not None:
                        return md[:max_bytes]
            except (OSError, subprocess.TimeoutExpired):
                pass  # fall back to the raw content below
        return self.file_content_at(rel, sha, max_bytes)

    def worktree_text(self, relpath: str, max_bytes: int = 400_000) -> str:
        """The working-tree file as readable text (markdown for .docx). '' if missing."""
        rel = relpath.replace("\\", "/")
        full = os.path.join(self.path, rel.replace("/", os.sep))
        if rel.lower().endswith(".docx"):
            self._ensure_pandoc()  # GUI may preview a .docx without one being staged
        if self._pandoc and rel.lower().endswith(".docx"):
            try:
                with open(full, "rb") as fh:
                    data = fh.read()
            except OSError:
                return ""
            return (self._docx_bytes_to_md(data) or "")[:max_bytes]
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

    def restore_file(self, relpath: str, sha: str):
        """Write the file's version at `sha` into the working tree (and index).

        SincroGit's next snapshot will commit it, so the restore is itself
        versioned. Raises GitError on failure.
        """
        relpath = relpath.replace("\\", "/")
        self._run(["checkout", sha, "--", relpath])
