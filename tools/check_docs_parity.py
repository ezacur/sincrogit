"""Guard against drift between the paired English/Spanish docs — and between
the docs' factual claims and the code.

The project keeps four bilingual pairs (README/LEAME, DESIGN/DISENO,
MANUAL/MANUAL_ES, GUIDE/GUIA). Prose is translated by hand, so it will always
differ — but three things are meant to mirror exactly, and each gets a check:

1. STRUCTURE: the same sequence of section headings at the same nesting depth
   (heading LEVELS compared, language-agnostic, skipping fenced code blocks).
2. FACTS IN CODE SPANS: placeholders inside inline `code` are legitimately
   translated (`<branch>` -> `<rama>`), so spans aren't compared verbatim.
   Instead the FACTS are extracted and compared: config keys and default
   values (introspected from config.py), CLI flags (scraped from
   __main__.py), and bare numbers. This is what catches "the ES table still
   says the old default" — drift the heading check is blind to.
3. CONFIG EXAMPLE: every key in config.example.yaml must exist in config.py's
   dataclasses (introspected, not hardcoded), so the example can't teach a
   key the loader ignores.

A mismatch means one side changed alone — the kind of drift the review
flagged. Run it in CI (see .github/workflows/ci.yml).

Usage:  python tools/check_docs_parity.py        # exit 0 = in sync, 1 = drift
"""

import os
import re
import sys
from collections import Counter

# (english, spanish) filename pairs, relative to the repo root.
PAIRS = [
    ("README.md", "LEAME.md"),
    ("DESIGN.md", "DISENO.md"),
    ("MANUAL.md", "MANUAL_ES.md"),
    ("GUIDE.md", "GUIA.md"),
]

_HEADING = re.compile(r"^(#{1,6})\s+\S")
_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")


def _body_lines(path: str):
    """The file's lines with fenced code blocks removed (a '#' comment or a
    `span` inside a shell/yaml example is an illustration, not a claim)."""
    in_fence = False
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if _FENCE.match(line):
                in_fence = not in_fence
                continue
            if not in_fence:
                yield line


def heading_levels(path: str) -> list:
    """The sequence of heading depths in `path` (see _body_lines)."""
    return [len(m.group(1)) for line in _body_lines(path)
            if (m := _HEADING.match(line))]


_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
_FLAG = re.compile(r"--[a-z][a-z-]*")


def _code_vocabulary(root: str) -> set:
    """The identifiers a doc can state as fact: config keys and their default
    values (introspected from config.py's dataclasses) plus the CLI flags
    scraped from __main__.py. Translated placeholders/examples are, by
    construction, never in here."""
    vocab = set()
    try:
        import dataclasses
        sys.path.insert(0, root)
        from sincrogit import config as cfgmod
        for dc in (cfgmod.Config, cfgmod.RepoConfig, cfgmod.AiConfig,
                   cfgmod.LogConfig):
            for f in dataclasses.fields(dc):
                vocab.add(f.name)
                if isinstance(f.default, (str, int, float, bool)):
                    vocab.update(_TOKEN.findall(str(f.default)))
    except Exception:  # noqa: BLE001 — no schema, weaker (numbers-only) check
        pass
    main_py = os.path.join(root, "sincrogit", "__main__.py")
    if os.path.exists(main_py):
        with open(main_py, "r", encoding="utf-8") as fh:
            vocab.update(_FLAG.findall(fh.read()))
    return vocab


_PLACEHOLDER = re.compile(r"<[^>]*>")


def _is_fact(tok: str, vocab: set) -> bool:
    """A number is always a fact. A vocabulary token only counts when it's
    DISTINCTIVE (longer than 4 chars, or carrying _ . -): short keys like
    `file`/`name`/`en` double as ordinary words inside translated examples
    (`name (stamp).ext` -> `nombre (fecha).ext`) and would read as drift."""
    if tok.isdigit():
        return True
    return tok in vocab and (len(tok) > 4 or any(c in tok for c in "_.-"))


def span_facts(path: str, vocab: set) -> Counter:
    """Multiset of FACTS inside `path`'s inline `code` spans: CLI flags,
    distinctive tokens the code vocabulary knows, and bare numbers.
    Everything else in a span — `<placeholders>` (legitimately translated),
    sample filenames, prose in disguise — stays out of the comparison."""
    facts = Counter()
    for line in _body_lines(path):
        for m in _CODE_SPAN.finditer(line):
            text = _PLACEHOLDER.sub(" ", m.group(1))
            for flag in _FLAG.findall(text):
                facts[flag] += 1
            for tok in _TOKEN.findall(text):
                if _is_fact(tok, vocab):
                    facts[tok] += 1
    return facts


def _first_divergence(a: list, b: list) -> int | None:
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    return None if len(a) == len(b) else min(len(a), len(b))


def check_pair(root: str, en: str, es: str, vocab: set) -> list:
    """Return a list of human-readable problems for one pair ([] if in sync)."""
    en_path, es_path = os.path.join(root, en), os.path.join(root, es)
    problems = []
    for p in (en_path, es_path):
        if not os.path.exists(p):
            problems.append(f"missing file: {os.path.basename(p)}")
    if problems:
        return problems

    en_lv, es_lv = heading_levels(en_path), heading_levels(es_path)
    if en_lv != es_lv:
        at = _first_divergence(en_lv, es_lv)
        detail = f"{en} has {len(en_lv)} headings, {es} has {len(es_lv)}"
        if at is not None:
            en_at = f"level {en_lv[at]}" if at < len(en_lv) else "(end)"
            es_at = f"level {es_lv[at]}" if at < len(es_lv) else "(end)"
            detail += f"; first divergence at heading #{at + 1}: {en}={en_at}, {es}={es_at}"
        problems.append(f"heading structure drift — {detail}")

    problems += _fact_drift(span_facts(en_path, vocab),
                            span_facts(es_path, vocab), en, es)
    return problems


def _fact_drift(en_facts: Counter, es_facts: Counter, en: str, es: str) -> list:
    """Human-readable factual (in-span) differences between the two sides."""
    problems = []
    for label, extra in ((f"only in {en}", en_facts - es_facts),
                         (f"only in {es}", es_facts - en_facts)):
        if extra:
            sample = ", ".join(f"`{t}`" + (f" x{n}" if n > 1 else "")
                               for t, n in sorted(extra.items())[:8])
            more = "" if len(extra) <= 8 else f" (+{len(extra) - 8} more)"
            problems.append(f"in-code fact drift ({label}): {sample}{more}")
    return problems


def check_config_example(root: str) -> list:
    """Every key in config.example.yaml must be one the loader actually knows
    (introspected from config.py's dataclasses). Catches the example teaching
    a renamed/removed key that the loader would silently ignore."""
    example = os.path.join(root, "config.example.yaml")
    if not os.path.exists(example):
        return ["missing file: config.example.yaml"]
    try:
        import dataclasses

        import yaml
        sys.path.insert(0, root)
        from sincrogit import config as cfgmod
    except Exception as e:  # noqa: BLE001 — an unimportable config IS the finding
        return [f"cannot import the config schema: {e}"]

    def names(dc) -> set:
        return {f.name for f in dataclasses.fields(dc)}

    with open(example, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    repo_keys = names(cfgmod.RepoConfig)
    sections = {  # yaml section -> keys the loader accepts there
        "defaults": repo_keys,
        "ai": names(cfgmod.AiConfig),
        "log": names(cfgmod.LogConfig),
    }
    problems = []
    top_allowed = names(cfgmod.Config) | set(sections)
    for key in data:
        if key not in top_allowed:
            problems.append(f"config.example.yaml: unknown top-level key '{key}'")
    for section, allowed in sections.items():
        for key in (data.get(section) or {}):
            if key not in allowed:
                problems.append(
                    f"config.example.yaml: '{section}.{key}' does not exist "
                    f"in config.py")
    for i, repo in enumerate(data.get("repos") or []):
        for key in repo or {}:
            if key not in repo_keys:
                problems.append(
                    f"config.example.yaml: 'repos[{i}].{key}' does not exist "
                    f"in config.py")
    return problems


def main() -> int:
    # The docs are UTF-8 and spans quote arrows/ellipses; a cp1252 Windows
    # console must not crash the report over a character it can't draw.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    vocab = _code_vocabulary(root)
    any_bad = False
    for en, es in PAIRS:
        problems = check_pair(root, en, es, vocab)
        if problems:
            any_bad = True
            for p in problems:
                print(f"[DRIFT] {en} <-> {es}: {p}")
        else:
            print(f"[ OK  ] {en} <-> {es}")

    problems = check_config_example(root)
    if problems:
        any_bad = True
        for p in problems:
            print(f"[DRIFT] {p}")
    else:
        print("[ OK  ] config.example.yaml <-> config.py")

    if any_bad:
        print("\nDocs are out of sync. Mirror the section structure and the "
              "inline-code facts across the language pair (translate prose, "
              "keep headings and `code` parallel), and keep "
              "config.example.yaml's keys real.")
        return 1
    print("\nAll bilingual doc pairs are structurally and factually in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
