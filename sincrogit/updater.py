"""Self-update from the project's GitHub Releases (stdlib only, like ai.py).

WHY THE DIGEST AND NOT THE VERSION STRING. `__version__` changes only when
someone bumps it by hand, so the same "0.2.0" ships in many different builds —
including a broken one. The question the user actually asks is "is this the same
binary as the published one?", so that is what we compare: the SHA-256 of the
running exe against the digest published beside the release asset. Same
reasoning as runtime.version_report.

WHY THE RENAME DANCE. Windows refuses to OVERWRITE a running .exe, but it
happily RENAMES one. So the swap is:

    <exe>      ->  <exe>.old      (allowed while that exe is running)
    <exe>.new  ->  <exe>

and the next start deletes the leftover `.old`. Nothing is ever written in place,
so a crash mid-swap leaves either the old binary or the new one at the path —
never a half-written one. The path itself never changes, which also means the
start-at-login entry (autostart, a Run key holding this path) stays valid and the
relaunch picks up the new binary for free.

The download is verified BEFORE the swap, so a truncated transfer can only fail
the update, never install a broken exe.
"""

import hashlib
import json
import os
import urllib.error
import urllib.request

# The project this build updates from. Deliberately a constant: the app has no
# other way to know its own origin, and pointing it at an arbitrary host from
# config would turn "check for updates" into a remote code-execution channel.
GITHUB_REPO = "ezacur/sincrogit"
ASSET_NAME = "SincroGit.exe"
DIGEST_SUFFIX = ".sha256"          # sidecar asset published next to the exe
_API = "https://api.github.com/repos/{repo}/releases/latest"
_UA = "SincroGit-updater"
# A release asset is ~66 MB; refuse anything wildly outside that, so a wrong
# asset (or an HTML error page served as one) fails fast instead of downloading.
MAX_ASSET_BYTES = 300 * 1024 * 1024


class UpdateError(Exception):
    """Anything that stops an update, with a message meant for the user."""


def _get(url: str, timeout: float, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # nosec B310 - https
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("no published release found for this project yet")
        if e.code == 403:
            raise UpdateError("GitHub rate limit reached; try again in a few minutes")
        raise UpdateError(f"GitHub returned HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"could not reach GitHub: {e}")


def sha256_file(path: str) -> str | None:
    """Streamed digest (the exe is ~66 MB; never slurp it). None if unreadable."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def latest_release(timeout: float = 20.0, repo: str = GITHUB_REPO) -> dict:
    """{tag, name, url, size, digest} for the newest release's exe asset.

    `digest` is None when the release carries no .sha256 sidecar — the caller
    then cannot verify the download and must say so rather than pretend.
    """
    try:
        data = json.loads(_get(_API.format(repo=repo), timeout))
    except (ValueError, TypeError):
        raise UpdateError("GitHub sent a response this build could not parse")
    assets = {a.get("name"): a for a in (data.get("assets") or [])
              if isinstance(a, dict)}
    exe = assets.get(ASSET_NAME)
    if not exe or not exe.get("browser_download_url"):
        raise UpdateError(
            f"the latest release ({data.get('tag_name') or '?'}) has no "
            f"{ASSET_NAME} attached")
    size = int(exe.get("size") or 0)
    if size <= 0 or size > MAX_ASSET_BYTES:
        raise UpdateError(f"published {ASSET_NAME} has an implausible size ({size} bytes)")

    digest = None
    side = assets.get(ASSET_NAME + DIGEST_SUFFIX)
    if side and side.get("browser_download_url"):
        try:
            text = _get(side["browser_download_url"], timeout,
                        accept="application/octet-stream").decode("utf-8", "replace")
            # Accept both a bare digest and the `<digest>  <filename>` form.
            token = text.split()[0].strip().lower() if text.split() else ""
            if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
                digest = token
        except (UpdateError, UnicodeDecodeError, IndexError):
            digest = None

    return {"tag": data.get("tag_name") or "?", "name": ASSET_NAME,
            "url": exe["browser_download_url"], "size": size, "digest": digest}


def check(exe_path: str, timeout: float = 20.0, repo: str = GITHUB_REPO) -> tuple:
    """('up-to-date'|'available', release) by comparing digests.

    Falls back to 'available' when the release has no published digest: we cannot
    prove sameness, so we offer the update and let the user decide, rather than
    silently claiming to be current.
    """
    rel = latest_release(timeout, repo)
    mine = sha256_file(exe_path)
    if rel["digest"] and mine and rel["digest"] == mine:
        return "up-to-date", rel
    return "available", rel


def download(url: str, dest: str, expected_size: int, expected_digest: str | None,
             timeout: float = 120.0, progress=None) -> None:
    """Stream `url` to `dest` and VERIFY before returning. Raises UpdateError
    (and removes the partial file) on any mismatch, so the caller can never swap
    in something unverified. `progress(done, total)` is optional."""
    h = hashlib.sha256()
    done = 0
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                              "Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # nosec B310 - https
            with open(dest, "wb") as fh:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > MAX_ASSET_BYTES:
                        raise UpdateError("download exceeded the size limit; aborted")
                    h.update(chunk)
                    fh.write(chunk)
                    if progress:
                        progress(done, expected_size)
    except UpdateError:
        _unlink(dest)
        raise
    except (urllib.error.URLError, OSError) as e:
        _unlink(dest)
        raise UpdateError(f"download failed: {e}")

    if expected_size and done != expected_size:
        _unlink(dest)
        raise UpdateError(f"download is {done} bytes, expected {expected_size} "
                          f"— transfer was truncated")
    if expected_digest and h.hexdigest() != expected_digest:
        _unlink(dest)
        raise UpdateError("downloaded file does not match the published SHA-256 "
                          "— refusing to install it")


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def staging_path(exe_path: str) -> str:
    return exe_path + ".new"


def old_path(exe_path: str) -> str:
    return exe_path + ".old"


def swap_in(exe_path: str, new_path: str) -> str:
    """Move `new_path` onto `exe_path`, parking the current binary at `.old`.

    Returns the parked path. On failure the original is put BACK before raising:
    a failed update must never leave the machine without a working exe.
    """
    if not os.path.exists(new_path):
        raise UpdateError("the downloaded update is gone; nothing was changed")
    parked = old_path(exe_path)
    _unlink(parked)                      # a leftover from a previous update
    try:
        os.replace(exe_path, parked)     # rename, NOT overwrite (see module docstring)
    except OSError as e:
        raise UpdateError(f"could not move the running exe aside: {e}")
    try:
        os.replace(new_path, exe_path)
    except OSError as e:
        try:
            os.replace(parked, exe_path)  # put the working binary back
        except OSError:
            pass
        raise UpdateError(f"could not put the new exe in place: {e}")
    return parked


def cleanup_old(exe_path: str) -> bool:
    """Delete the `.old` binary parked by a previous update. Called at startup —
    the file is only deletable once the process that WAS it has exited. True if
    something was removed."""
    parked = old_path(exe_path)
    if not os.path.exists(parked):
        return False
    try:
        os.remove(parked)
        return True
    except OSError:
        return False    # still locked; the next start tries again
