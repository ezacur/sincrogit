"""Configuration loading and validation (config.yaml).

Each repo inherits the values from `defaults` and may override them.
See the example in config.example.yaml and §8 of DESIGN.md.
"""

import math
import os
import re
from dataclasses import dataclass, field

import yaml

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
    "pull_interval_min", "autosnap_interval_min",
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


def _norm_handoff(value) -> str:
    """Normalize live_handoff to 'auto' | 'ask' | 'off'. Accepts booleans
    (true->auto, false->off) and the obvious word spellings."""
    if value is True:
        return "auto"
    if value is False or value is None:
        return "off"
    s = str(value).strip().lower()
    if s in ("ask", "prompt", "confirm", "notify"):
        return "ask"
    if s in ("off", "false", "none", "no", "0", "disabled", "never"):
        return "off"
    return "auto"  # true/auto/on/yes/anything else -> auto

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
    "live_handoff",
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
    autosnap: bool = True                 # mirror HEAD (incl. WIP) to a side ref on the
                                          # remote -> disk-failure RPO ~= autosnap_interval
    autosnap_interval_min: int = 30       # how often the live mirror is force-pushed
    live_handoff: object = "auto"         # pick up your OTHER machine's live WIP. 'auto'
                                          # = fast-forward automatically (notify on apply);
                                          # 'ask' = notify + one-click Apply (no silent
                                          # reset); 'off'/false = manual. Needs autosnap on.

    def __post_init__(self):
        # Normalize disable sentinels (inf/off/none/never, None, False) to
        # math.inf for the interval/threshold fields, so 'seal_interval_min: inf'
        # (purist mode: no auto-seal) or 'max_file_bytes: off' (no size limit)
        # just work. See _disabled_to_inf.
        for f in _DISABLEABLE_FIELDS:
            setattr(self, f, _disabled_to_inf(getattr(self, f)))
        self.live_handoff = _norm_handoff(self.live_handoff)

    @property
    def seal_interval_sec(self) -> float:
        return self.seal_interval_min * 60

    @property
    def pull_interval_sec(self) -> float:
        return self.pull_interval_min * 60

    @property
    def autosnap_interval_sec(self) -> float:
        return self.autosnap_interval_min * 60


@dataclass
class LogConfig:
    file: str = "sincrogit.log"
    level: str = "INFO"


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


@dataclass
class Config:
    repos: list
    log: LogConfig
    ai: AiConfig
    # Path to the `pandoc` executable (machine-specific). Used to show readable
    # diffs of .docx and similar via a git textconv driver. "pandoc" assumes it's
    # on PATH; set a full path if not (forward slashes are fine on Windows).
    pandoc_path: str = "pandoc"


def load_config(path: str) -> Config:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Configuration not found: {path}\n"
            f"Copy config.example.yaml to config.yaml and edit it."
        )

    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    defaults = raw.get("defaults") or {}
    repos_raw = raw.get("repos") or []
    # An empty repos list is valid: repos can be added later from the GUI.

    repos = []
    for entry in repos_raw:
        if "path" not in entry:
            raise ValueError(f"Repo without 'path' in the configuration: {entry!r}")
        abspath = os.path.abspath(os.path.expanduser(str(entry["path"])))
        name = entry.get("name") or os.path.basename(abspath.rstrip("/\\")) or abspath

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

    log_raw = raw.get("log") or {}
    log_file = log_raw.get("file", "sincrogit.log")
    # A relative log path is resolved next to the config file (predictable for the
    # standalone exe), not against the current working directory.
    if not os.path.isabs(log_file):
        log_file = os.path.join(os.path.dirname(os.path.abspath(path)), log_file)
    log_cfg = LogConfig(file=log_file, level=log_raw.get("level", "INFO"))

    ai_raw = raw.get("ai") or {}
    ai_cfg = AiConfig(
        **{k: v for k, v in ai_raw.items() if k in AiConfig.__dataclass_fields__}
    )

    pandoc_path = raw.get("pandoc_path", "pandoc")

    return Config(repos=repos, log=log_cfg, ai=ai_cfg, pandoc_path=pandoc_path)


def append_repo(config_path: str, repo_entry: dict) -> None:
    """Append a repo to the config file, preserving existing comments/formatting.

    If `repos:` is the last top-level section (as in the generated default) we
    rewrite only that section, keeping everything above untouched. Otherwise we
    fall back to a full safe_dump (comments are not preserved).
    """
    with open(config_path, "r", encoding="utf-8") as fh:
        text = fh.read()

    data = yaml.safe_load(text) or {}
    repos = list(data.get("repos") or [])
    repos.append(repo_entry)

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
        prefix = "\n".join(lines[:repos_idx]).rstrip("\n")
        out = (prefix + "\n" if prefix else "") + repos_block
    else:
        out = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(out)
