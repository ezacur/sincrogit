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
import socket
import subprocess

log = logging.getLogger("sincrogit.git")


def autosnap_host() -> str:
    """This machine's name, sanitized for use inside a git ref path.

    The autosnap ref is per-machine (refs/autosnap/<host>/<branch>) so two
    machines mirroring the same repo never clobber each other.
    """
    name = socket.gethostname() or "host"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._-")
    return name or "host"

# On Windows, a windowed (--noconsole) app spawns a console window for every
# child process. CREATE_NO_WINDOW suppresses that flash for each git call.
# (0 on other platforms, where it has no effect.)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class GitError(RuntimeError):
    pass


class GitRepo:
    # Message prefix that identifies a (transient) WIP commit.
    WIP_MESSAGE = "WIP: autosnapshot"

    def __init__(self, path: str):
        self.path = path

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
            *args,
        ]
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
        try:
            res = subprocess.run(
                cmd,
                cwd=self.path,
                input=stdin_data,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                creationflags=_NO_WINDOW,  # no console flash on Windows
            )
        except subprocess.TimeoutExpired:
            raise GitError(
                f"`git {' '.join(args)}` timed out ({timeout}s)"
            )
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
    def ensure_gitattributes(self) -> bool:
        """Create a .gitattributes with '* text=auto' if none exists.

        This normalizes line endings inside the repo (text blobs stored as LF)
        regardless of each machine's core.autocrlf, so a CRLF/LF-only change is
        never seen as an edit and machines don't fight over line endings. Written
        with LF itself. Returns True if it created the file. (The next snapshot
        commits it; on a repo whose blobs were CRLF this triggers a one-time,
        intended renormalization.)
        """
        path = os.path.join(self.path, ".gitattributes")
        if os.path.exists(path):
            return False
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(
                    "# Added by SincroGit: normalize line endings (store text blobs as\n"
                    "# LF regardless of each machine's core.autocrlf), so a CRLF/LF-only\n"
                    "# change is never an edit and machines don't fight over endings.\n"
                    "* text=auto\n"
                )
            return True
        except OSError:
            return False

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

    def stage_changes(self, file_filter, on_drop=None) -> bool:
        """Run `git add` ONLY on files that pass the filter.

        Deletions of already-tracked files are always staged.
        Returns True if anything was staged.

        `on_drop(relpath, reason)` is called for files that are ALREADY tracked
        but the filter now rejects (e.g. a text file that grew past the size
        limit) so the caller can warn the user. New untracked binaries/large
        files and user-configured excludes are skipped silently (expected).
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
    @staticmethod
    def autosnap_ref(host: str, branch: str) -> str:
        return f"refs/autosnap/{host}/{branch}"

    def push_autosnap(self, remote: str, branch: str, host: str, timeout: float | None = None):
        """Force-push HEAD (sealed history + the live WIP) to this machine's
        autosnap side ref, so the latest local state survives a total disk
        failure. The ref is single-writer (only this host writes it), so a plain
        --force is safe and it never touches the clean `branch`. Returns (ok, msg).
        """
        ref = self.autosnap_ref(host, branch)
        res = self._run(
            ["push", "--force", remote, f"HEAD:{ref}"], check=False, timeout=timeout
        )
        msg = (res.stderr.strip() or res.stdout.strip())
        return res.returncode == 0, msg

    def fetch_autosnaps(self, remote: str, timeout: float | None = None) -> bool:
        """Fetch every machine's autosnap refs into local refs/autosnap/* (for
        cross-machine recovery). Returns True on success."""
        res = self._run(
            ["fetch", "--quiet", remote, "+refs/autosnap/*:refs/autosnap/*"],
            check=False, timeout=timeout,
        )
        if res.returncode != 0:
            log.warning("fetch of autosnap refs from '%s' failed: %s", remote, res.stderr.strip())
        return res.returncode == 0

    def list_autosnap_refs(self) -> list:
        """Local refs/autosnap/* as dicts: ref, host, branch, sha, epoch, subject.
        Newest first. Present only after fetch_autosnaps (or this host's own)."""
        # NOTE: for-each-ref does NOT understand %x09 (that's git-log syntax); use a
        # literal tab as the field separator.
        fmt = "--format=%(refname)\t%(objectname)\t%(committerdate:unix)\t%(contents:subject)"
        res = self._run(["for-each-ref", fmt, "refs/autosnap/"], check=False)
        out = []
        for line in res.stdout.splitlines():
            ref, sha, ct, subj = (line.split("\t", 3) + ["", "", "", ""])[:4]
            if not ref or not sha:
                continue
            rest = ref[len("refs/autosnap/"):]
            host, _, br = rest.partition("/")
            out.append({
                "ref": ref, "host": host, "branch": br, "sha": sha,
                "epoch": int(ct) if ct.isdigit() else 0, "subject": subj,
            })
        out.sort(key=lambda e: e["epoch"], reverse=True)
        return out

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

    def restore_file(self, relpath: str, sha: str):
        """Write the file's version at `sha` into the working tree (and index).

        SincroGit's next snapshot will commit it, so the restore is itself
        versioned. Raises GitError on failure.
        """
        relpath = relpath.replace("\\", "/")
        self._run(["checkout", sha, "--", relpath])
