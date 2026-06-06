"""Configuration loading and validation (config.yaml).

Each repo inherits the values from `defaults` and may override them.
See the example in config.example.yaml and §8 of DESIGN.md.
"""

import os
import re
from dataclasses import dataclass, field

import yaml

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
]


@dataclass
class RepoConfig:
    path: str
    name: str
    remote: str = "origin"
    branch: str = "main"
    snapshot_interval_sec: int = 300      # 5 min
    debounce_sec: int = 25
    seal_interval_min: int = 120          # 2 h
    pull_interval_min: int = 10
    max_file_bytes: int = 1_048_576       # 1 MB
    extra_excludes: list = field(default_factory=list)
    push: bool = True                     # push sealed commits to the remote
    pull: bool = True                     # periodic pull from the remote
    git_timeout_sec: int = 60             # limit for network operations (fetch/push)

    @property
    def seal_interval_sec(self) -> int:
        return self.seal_interval_min * 60

    @property
    def pull_interval_sec(self) -> int:
        return self.pull_interval_min * 60


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

    return Config(repos=repos, log=log_cfg, ai=ai_cfg)


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
