"""Configuration loading and validation (config.yaml).

Each repo inherits the values from `defaults` and may override them.
See the example in config.example.yaml and §8 of DESIGN.md.
"""

import logging
import math
import os
import re
from dataclasses import dataclass, field

import yaml

log = logging.getLogger("sincrogit.config")

# Words (any case) that disable an interval/threshold, i.e. "never fire" / "no
# limit". They normalize to math.inf, which flows through the engine's deadline
# arithmetic untouched (a due-time of inf is never reached, and min(x, inf)==x),
# so disabling needs NO special-casing in the loop. NOTE: YAML only parses '.inf'
# (with the dot) as a float; a bare 'inf'/'off' arrives here as a string/bool,
# which is exactly why this normalization exists.
_DISABLE_TOKENS = {"inf", "infinity", "none", "never", "off", "false", "disabled"}

# Fields where "disable" makes sense: scheduling intervals (never fire) and size
# thresholds (no limit). git_timeout_sec and booleans keep their own semantics.
_DISABLEABLE_FIELDS = (
    "snapshot_interval_sec", "debounce_sec", "seal_interval_min",
    "pull_interval_min", "autosnap_interval_min", "seal_on_leave_min",
    "max_file_bytes", "max_include_bytes",
)


def _disabled_to_inf(value):
    """Map a disable sentinel (inf/off/none/never as a string, Python None/False,
    or an already-infinite float) to math.inf. Any real number passes through."""
    if value is None or value is False:
        return math.inf
    if isinstance(value, str) and value.strip().lower() in _DISABLE_TOKENS:
        return math.inf
    if isinstance(value, float) and math.isinf(value):
        return math.inf
    return value


def _to_number(name: str, value):
    """Coerce a config value to a non-negative number, accepting numeric strings
    (a quoted '300' in the YAML). Anything else fails AT LOAD with a clear
    message — instead of TypeError-ing inside the engine loop hours later.
    (load_config's callers already surface ValueError as a config error.)"""
    if isinstance(value, bool):  # bool is an int subclass: reject it explicitly
        raise ValueError(f"config: '{name}' must be a number, got a boolean ({value!r})")
    if isinstance(value, str):
        try:
            value = float(value) if any(c in value for c in ".eE") else int(value)
        except ValueError:
            raise ValueError(f"config: '{name}' must be a number, got {value!r}") from None
    if not isinstance(value, (int, float)):
        raise ValueError(f"config: '{name}' must be a number, got {value!r}")
    # Reject NaN and overflow-to-inf: a real disable sentinel ('inf'/'off'/...) is
    # already mapped to math.inf BEFORE _to_number runs, so a non-finite value
    # here means garbage or a numeric string that overflowed (e.g. "1e999") —
    # which would silently DISABLE the field instead of setting a big number.
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"config: '{name}' must be a finite number, got {value!r} "
                         f"(use 'off'/'inf' to disable it explicitly)")
    if value < 0:
        raise ValueError(f"config: '{name}' must be >= 0, got {value!r}")
    return value


def _require_map(name: str, value) -> dict:
    """A config section that must be a mapping (or absent). Anything else fails
    AT LOAD with a ValueError — the error type every caller already surfaces as
    a clean "configuration error" — instead of an AttributeError deeper down
    (which a windowed exe would show as PyInstaller's crash box)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(
            f"config: '{name}:' must be a mapping (key: value), got {value!r}")
    return value


def _require_pattern_list(name: str, value) -> list:
    """extra_excludes/extra_includes must be a LIST of string patterns. A bare
    string is the classic slip (`extra_excludes: "**/build/**"`): it would load
    fine and then blow up inside pathspec at setup, skipping the whole repo
    with a cryptic 'setup failed' — so refuse it here, with the fix spelled out."""
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(
            f"config: '{name}' must be a list of patterns, got {value!r} — "
            f"write it as:  {name}: [\"{value}\"]  or as a '- ' list")
    bad = [v for v in value if not isinstance(v, str)]
    if bad:
        raise ValueError(
            f"config: '{name}' entries must be strings, got {bad[0]!r}")
    return list(value)


def _norm_handoff(value) -> str:
    """Normalize live_handoff to 'auto' | 'ask' | 'off'. Accepts booleans
    (true->auto, false->off) and the obvious word spellings. Anything ELSE is
    an error, not a silent 'auto': a typo like `live_handoff: aks` would
    otherwise flip the machine into applying remote trees on its own — the
    one mode the user explicitly did NOT pick."""
    if value is True:
        return "auto"
    if value is False or value is None:
        return "off"
    s = str(value).strip().lower()
    if s in ("ask", "prompt", "confirm", "notify"):
        return "ask"
    if s in ("off", "false", "none", "no", "0", "disabled", "never"):
        return "off"
    if s in ("auto", "on", "yes", "true", "1", "always"):
        return "auto"
    raise ValueError(
        f"config: 'live_handoff' must be auto/ask/off (or true/false), got {value!r}")

# Keys a repo can inherit from `defaults` or override.
_INHERITABLE = [
    "snapshot_interval_sec",
    "debounce_sec",
    "seal_interval_min",
    "pull_interval_min",
    "max_file_bytes",
    "extra_excludes",
    "push",
    "pull",
    "git_timeout_sec",
    "autosnap",
    "autosnap_interval_min",
    "seal_on_leave_min",
    "live_handoff",
    "track_current_branch",
    "suggest_excludes",
    "suggest_commit",
    "extra_includes",
    "max_include_bytes",
]


@dataclass
class RepoConfig:
    path: str
    name: str
    remote: str = "origin"
    branch: str = "main"
    snapshot_interval_sec: int = 300      # 5 min (local WIP amend)
    debounce_sec: int = 25
    seal_interval_min: int = 360          # 6 h (with autosnap on, the seal is just
                                          # the coarse permanent timeline; disk-failure
                                          # RPO is covered by autosnap, not the seal)
    pull_interval_min: int = 10
    max_file_bytes: int = 1_048_576       # 1 MB
    extra_excludes: list = field(default_factory=list)
    extra_includes: list = field(default_factory=list)  # glob patterns versioned even
                                          # if binary (e.g. "**/*.docx"); opt-in
    max_include_bytes: int = 26_214_400   # 25 MB size cap for extra_includes
    push: bool = True                     # push sealed commits to the remote
    pull: bool = True                     # periodic pull from the remote
    git_timeout_sec: int = 60             # limit for network operations (fetch/push)
    autosnap: bool = True                 # mirror the shadow tip (latest snapshot: sealed
                                          # history + the live WIP) to a side ref on the
                                          # remote -> disk-failure RPO ~= autosnap_interval
    autosnap_interval_min: int = 30       # how often the live mirror is force-pushed
    seal_on_leave_min: int = 20           # LEAVE SEAL: seal (+push) this many minutes
                                          # after the machine is LOCKED, unless you come
                                          # back first — "lock + walk away" is the
                                          # session-over proxy, so home pulls a fresh
                                          # branch. 'off'/inf disables. IGNORED in purist
                                          # mode (seal_interval_min: inf): the branch
                                          # stays 100% yours there.
    live_handoff: object = "auto"         # pick up your OTHER machine's live WIP. 'auto'
                                          # = fast-forward automatically (notify on apply);
                                          # 'ask' = notify + one-click Apply (no silent
                                          # reset); 'off'/false = manual. Needs autosnap on.
    track_current_branch: bool = False    # follow the CURRENT branch instead of pausing
                                          # off `branch`: snapshot/autosnap/handoff/push on
                                          # whatever branch HEAD is on (pairs with purist
                                          # mode for a feature-branch workflow). Opt-in.
    suggest_excludes: bool = True         # suggest adding a high-churn folder (many
                                          # filtered-out files) to extra_excludes — a
                                          # one-time notification per folder, never auto-edits.
    suggest_commit: bool = True           # PURIST MODE ONLY: nudge (once/day, at a quiet
                                          # moment) to Smart Commit when un-sealed work piles
                                          # up on a stagnant branch. No-op unless auto-seal
                                          # is disabled (seal_interval_min: inf).

    def __post_init__(self):
        # Normalize disable sentinels (inf/off/none/never, None, False) to
        # math.inf for the interval/threshold fields, so 'seal_interval_min: inf'
        # (purist mode: no auto-seal) or 'max_file_bytes: off' (no size limit)
        # just work. See _disabled_to_inf. Everything that survives must be a
        # real non-negative number (numeric strings are coerced): a mistyped
        # value fails here, at load, with a clear message — not as a TypeError
        # deep inside the engine loop hours later.
        for f in _DISABLEABLE_FIELDS:
            v = _disabled_to_inf(getattr(self, f))
            if not (isinstance(v, float) and math.isinf(v)):
                v = _to_number(f, v)
            setattr(self, f, v)
        # git_timeout_sec keeps its own semantics (not disableable; None = no
        # timeout) but must otherwise be a real number too.
        if self.git_timeout_sec is not None:
            self.git_timeout_sec = _to_number("git_timeout_sec", self.git_timeout_sec)
        self.live_handoff = _norm_handoff(self.live_handoff)
        # Pattern lists: a bare string here would only explode inside pathspec
        # at engine setup, silently skipping the repo. See _require_pattern_list.
        self.extra_excludes = _require_pattern_list("extra_excludes", self.extra_excludes)
        self.extra_includes = _require_pattern_list("extra_includes", self.extra_includes)

    @property
    def seal_interval_sec(self) -> float:
        return self.seal_interval_min * 60

    @property
    def pull_interval_sec(self) -> float:
        return self.pull_interval_min * 60

    @property
    def autosnap_interval_sec(self) -> float:
        return self.autosnap_interval_min * 60

    @property
    def seal_on_leave_sec(self) -> float:
        return self.seal_on_leave_min * 60


@dataclass
class LogConfig:
    file: str = "sincrogit.log"
    level: str = "INFO"


_AI_MODES = ("hybrid", "local", "cloud", "none")


@dataclass
class AiConfig:
    mode: str = "hybrid"                  # hybrid | local | cloud | none
    cloud_provider: str = "gemini"
    cloud_model: str = "gemini-2.5-flash-lite"  # current free Flash model (2026)
    cloud_send_content: bool = False      # if False, only names + --stat go to the cloud
    api_key_env: str = "SINCROGIT_GEMINI_KEY"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    timeout_sec: int = 30
    max_diff_chars: int = 6000
    language: str = "en"

    def __post_init__(self):
        # A mistyped mode ('hibrid', 'Hybrid ') would otherwise register NO
        # providers in ai.py — the AI silently off, and --doctor printing no AI
        # line at all for an unknown mode. Fail at load instead, like every
        # other field.
        mode = str(self.mode).strip().lower()
        if mode not in _AI_MODES:
            raise ValueError(
                f"config: 'ai.mode' must be one of "
                f"{', '.join(_AI_MODES)}; got {self.mode!r}")
        self.mode = mode


@dataclass
class Config:
    repos: list
    log: LogConfig
    ai: AiConfig
    # Path to the `pandoc` executable (machine-specific). Used to show readable
    # diffs of .docx and similar via a git textconv driver. "pandoc" assumes it's
    # on PATH; set a full path if not (forward slashes are fine on Windows).
    pandoc_path: str = "pandoc"
    # GUI theme: "auto" follows Windows' app theme; or force "light" / "dark".
    theme: str = "auto"


def load_config(path: str) -> Config:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Configuration not found: {path}\n"
            f"Copy config.example.yaml to config.yaml and edit it."
        )

    with open(path, "r", encoding="utf-8") as fh:
        try:
            raw = yaml.safe_load(fh) or {}
        except yaml.YAMLError as e:
            # Normalize to ValueError so every caller's existing "configuration
            # error" handling catches it (a raw YAMLError would crash a windowed
            # exe with PyInstaller's unhandled-exception dialog). The yaml message
            # already includes file, line and column. A tab instead of spaces is
            # the classic hand-editing slip, so name it explicitly.
            hint = ""
            if "'\\t'" in str(e) or "\\t" in str(e):
                hint = "\nHint: YAML forbids TAB indentation — use spaces."
            raise ValueError(f"Invalid YAML in the configuration:\n{e}{hint}") from e

    if not isinstance(raw, dict):
        raise ValueError(
            f"config: the top level must be a mapping (key: value), got {raw!r}")
    defaults = _require_map("defaults", raw.get("defaults"))
    repos_raw = raw.get("repos") or []
    # An empty repos list is valid: repos can be added later from the GUI.
    if not isinstance(repos_raw, list):
        raise ValueError(f"config: 'repos:' must be a list, got {repos_raw!r}")

    unknown = set(defaults) - set(_INHERITABLE)
    if unknown:
        # A typo here silently configures nothing — worth a warning, but not an
        # error (forward/backward compatibility across versions).
        log.warning("config: unknown key(s) under 'defaults:' ignored (typo?): %s",
                    ", ".join(sorted(unknown)))

    repos = []
    for entry in repos_raw:
        # A `repos:` item must be a mapping. A bare string (a common hand-editing
        # slip: `- ~/code/proj` instead of `- path: ~/code/proj`) would make
        # `"path" not in entry` a SUBSTRING test and then `entry["path"]` raise a
        # TypeError — not the ValueError callers normalize into a clean message.
        if not isinstance(entry, dict) or "path" not in entry:
            raise ValueError(f"Each repo needs a 'path:' mapping; got: {entry!r}")
        abspath = os.path.abspath(os.path.expanduser(str(entry["path"])))
        name = entry.get("name") or os.path.basename(abspath.rstrip("/\\")) or abspath
        unknown = set(entry) - set(_INHERITABLE) - {"path", "name", "remote", "branch"}
        if unknown:
            log.warning("config: unknown key(s) in repo '%s' ignored (typo?): %s",
                        name, ", ".join(sorted(unknown)))

        merged = {}
        for key in _INHERITABLE:
            if key in entry:
                merged[key] = entry[key]
            elif key in defaults:
                merged[key] = defaults[key]

        repos.append(
            RepoConfig(
                path=abspath,
                name=name,
                remote=entry.get("remote", "origin"),
                branch=entry.get("branch", "main"),
                **merged,
            )
        )

    log_raw = _require_map("log", raw.get("log"))
    log_file = log_raw.get("file", "sincrogit.log")
    # A relative log path is resolved next to the config file (predictable for the
    # standalone exe), not against the current working directory.
    if not os.path.isabs(log_file):
        log_file = os.path.join(os.path.dirname(os.path.abspath(path)), log_file)
    log_cfg = LogConfig(file=log_file, level=log_raw.get("level", "INFO"))

    ai_raw = _require_map("ai", raw.get("ai"))
    unknown = set(ai_raw) - set(AiConfig.__dataclass_fields__)
    if unknown:
        log.warning("config: unknown key(s) under 'ai:' ignored (typo?): %s",
                    ", ".join(sorted(unknown)))
    ai_cfg = AiConfig(
        **{k: v for k, v in ai_raw.items() if k in AiConfig.__dataclass_fields__}
    )

    pandoc_path = raw.get("pandoc_path", "pandoc")
    theme = str(raw.get("theme", "auto")).strip().lower()
    if theme not in ("auto", "light", "dark"):
        theme = "auto"

    return Config(repos=repos, log=log_cfg, ai=ai_cfg, pandoc_path=pandoc_path, theme=theme)


def atomic_write_text(path: str, text: str) -> None:
    """Write via a temp file + os.replace so a crash mid-write can never leave
    a truncated config.yaml (the same power-cut scenario gitrepo already heals
    for git's ref files — the config deserves no less)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _entry_name(entry: dict) -> str:
    """The name a repo entry resolves to — the same rule load_config applies
    (explicit `name`, else the basename of the absolute path)."""
    abspath = os.path.abspath(os.path.expanduser(str(entry.get("path", ""))))
    return entry.get("name") or os.path.basename(abspath.rstrip("/\\")) or abspath


def _validate_entry(entry: dict, defaults: dict) -> None:
    """Build the RepoConfig that load_config would build from `entry` (+ inherited
    defaults) — raising ValueError on anything invalid. Called BEFORE writing an
    edited entry, so a bad value can't brick the config file."""
    if not isinstance(entry, dict) or "path" not in entry:
        raise ValueError(f"Each repo needs a 'path:' mapping; got: {entry!r}")
    abspath = os.path.abspath(os.path.expanduser(str(entry["path"])))
    merged = {}
    for key in _INHERITABLE:
        if key in entry:
            merged[key] = entry[key]
        elif key in defaults:
            merged[key] = defaults[key]
    RepoConfig(
        path=abspath,
        name=_entry_name(entry),
        remote=entry.get("remote", "origin"),
        branch=entry.get("branch", "main"),
        **merged,
    )


def _write_repos_section(config_path: str, text: str, data: dict, repos: list) -> None:
    """Write the config back with `repos` as its repos list, preserving existing
    comments/formatting ABOVE the section.

    If `repos:` is the last top-level section (as in the generated default) only
    that section is rewritten, keeping everything above untouched. Otherwise we
    fall back to a full safe_dump (comments are not preserved). Comments INSIDE
    the repos section are rewritten either way — the same trade Add repo and the
    Settings form already make.
    """
    lines = text.splitlines()
    repos_idx = next(
        (i for i, ln in enumerate(lines) if re.match(r"^repos\s*:", ln)), None
    )
    # Is there another top-level key after `repos:`? (line starting in column 0
    # that isn't a list item, comment or blank)
    repos_is_last = repos_idx is not None and not any(
        ln and ln[0] not in (" ", "\t", "-", "#")
        for ln in lines[repos_idx + 1:]
    )

    repos_block = yaml.safe_dump(
        {"repos": repos}, sort_keys=False, allow_unicode=True, default_flow_style=False
    )

    if repos_is_last:
        # Re-dumping the repos section would drop any comment/blank lines trailing at
        # the end of the file — preserve them (re-append after the dumped block).
        tail = []
        for ln in reversed(lines):
            if ln.strip() == "" or ln.lstrip().startswith("#"):
                tail.append(ln)
            else:
                break
        tail.reverse()
        prefix = "\n".join(lines[:repos_idx]).rstrip("\n")
        out = (prefix + "\n" if prefix else "") + repos_block
        if tail:
            out = out.rstrip("\n") + "\n" + "\n".join(tail).rstrip("\n") + "\n"
    else:
        data = dict(data)
        data["repos"] = repos
        out = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    atomic_write_text(config_path, out)


def inheritable_overrides(entry: dict) -> dict:
    """The subset of a RAW repo entry that is an inheritable OPTION override —
    i.e. everything except the identity keys (path/name/branch/remote). This is
    exactly what one machine publishes for another to inherit."""
    return {k: entry[k] for k in _INHERITABLE if k in entry}


def overrides_to_yaml(overrides: dict) -> str:
    """Serialize an overrides dict for the published config ref. Values are the
    RAW entry values (e.g. the 'inf' token, kept verbatim), so the consumer
    gets back exactly what was written."""
    return yaml.safe_dump(overrides, sort_keys=True, allow_unicode=True,
                          default_flow_style=False)


def parse_published_overrides(text: str) -> dict:
    """Parse a published config YAML into inheritable overrides, DEFENSIVELY:
    the text came off a remote (a shared repo, a hand-edited ref) so anything
    that isn't a known inheritable key is dropped, and malformed YAML yields an
    empty dict rather than raising."""
    try:
        data = yaml.safe_load(text) if text else None
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in _INHERITABLE}


def safe_published_overrides(overrides: dict) -> tuple:
    """Keep only the overrides that yield a VALID RepoConfig; return
    (clean, dropped_keys).

    The published config ref is remote-controlled — it is namespaced only by the
    author's git-email, so a shared-repo teammate (or a hand-edited / cross-version
    ref) can put anything there. parse_published_overrides already dropped unknown
    KEYS; this drops bad VALUES too, so a malformed number, a broken pattern list
    or a bogus live_handoff can never reach config.yaml — where a single bad value
    fails the load for EVERY repo. Each key is validated on its own, so one bad
    value doesn't discard the good ones."""
    clean, dropped = {}, []
    for key, value in overrides.items():
        if key not in _INHERITABLE:
            dropped.append(key)
            continue
        try:
            # Empty defaults: an override supplies its own value for its key, so
            # validating {path, key: value} exercises exactly that field.
            _validate_entry({"path": ".", key: value}, {})
        except (ValueError, TypeError):
            dropped.append(key)
        else:
            clean[key] = value
    return clean, dropped


def append_repo(config_path: str, repo_entry: dict) -> None:
    """Append a repo to the config file, preserving existing comments/formatting
    above the repos section (see _write_repos_section)."""
    with open(config_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    data = yaml.safe_load(text) or {}
    repos = list(data.get("repos") or [])
    repos.append(repo_entry)
    _write_repos_section(config_path, text, data, repos)


def find_repo_entry(config_path: str, name: str) -> dict | None:
    """The RAW config entry (explicit keys only, no defaults merged) of the repo
    called `name`, or None. Lets the GUI distinguish an explicit override from
    an inherited default."""
    with open(config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh.read()) or {}
    for entry in data.get("repos") or []:
        if isinstance(entry, dict) and _entry_name(entry) == name:
            return dict(entry)
    return None


def update_repo(config_path: str, name: str, changes: dict) -> tuple:
    """Set keys on ONE repo's entry in the config file. Returns (ok, msg).

    math.inf is written as the documented 'inf' token; other values pass through
    as given. The merged entry is validated (RepoConfig construction) BEFORE
    anything is written. Comment preservation: same trade as append_repo.
    """
    with open(config_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    data = yaml.safe_load(text) or {}
    repos = list(data.get("repos") or [])
    idx = next((i for i, e in enumerate(repos)
                if isinstance(e, dict) and _entry_name(e) == name), None)
    if idx is None:
        return False, f"repo '{name}' not found in the config file"
    entry = dict(repos[idx])
    for key, value in changes.items():
        entry[key] = "inf" if isinstance(value, float) and math.isinf(value) else value
    try:
        _validate_entry(entry, data.get("defaults") or {})
    except (ValueError, TypeError) as e:
        return False, str(e)
    repos[idx] = entry
    _write_repos_section(config_path, text, data, repos)
    return True, "saved"


def reset_repo_overrides(config_path: str, name: str) -> tuple:
    """Delete every INHERITABLE override from ONE repo's entry, returning it to
    pure inheritance of the global defaults. Identity keys (path, name, remote,
    branch) are kept — they aren't inheritable. Returns (ok, msg); the msg says
    how many overrides were dropped. Comment preservation: same trade as
    append_repo."""
    with open(config_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    data = yaml.safe_load(text) or {}
    repos = list(data.get("repos") or [])
    idx = next((i for i, e in enumerate(repos)
                if isinstance(e, dict) and _entry_name(e) == name), None)
    if idx is None:
        return False, f"repo '{name}' not found in the config file"
    entry = dict(repos[idx])
    dropped = [k for k in entry if k in _INHERITABLE]
    if not dropped:
        return True, "no overrides to reset"
    for k in dropped:
        del entry[k]
    try:
        _validate_entry(entry, data.get("defaults") or {})
    except (ValueError, TypeError) as e:
        return False, str(e)
    repos[idx] = entry
    _write_repos_section(config_path, text, data, repos)
    return True, f"reset {len(dropped)} override(s): {', '.join(sorted(dropped))}"


def remove_repo(config_path: str, name: str) -> tuple:
    """Remove ONE repo's entry from the config file. Returns (ok, msg). The git
    repository on disk is not touched. Comment preservation: same trade as
    append_repo."""
    with open(config_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    data = yaml.safe_load(text) or {}
    repos = list(data.get("repos") or [])
    idx = next((i for i, e in enumerate(repos)
                if isinstance(e, dict) and _entry_name(e) == name), None)
    if idx is None:
        return False, f"repo '{name}' not found in the config file"
    del repos[idx]
    _write_repos_section(config_path, text, data, repos)
    return True, "removed"
