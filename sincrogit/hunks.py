"""Line-level (hunk) diffing for a partial restore — pure functions, no git.

The Time Machine can restore a whole file to a past version; this narrows that
to individual HUNKS, so you can pull back one changed block and leave the rest
of your current edits alone. A "hunk" here is a maximal run of changed lines
between the target version and the current file (with a little context for the
UI); restoring one means that region goes back to the target while everything
else stays as it is now.

Reconstruction is exact and reversible: applying the selected hunks yields a
concrete new file content, which the engine writes to the worktree and then
snapshots (so the partial restore is itself versioned, like every other one).
"""

import difflib

_CONTEXT = 3  # lines of unchanged context shown around a hunk (display only)


def compute_hunks(old_lines: list, new_lines: list) -> list:
    """Hunks turning `new_lines` (the current file) back into `old_lines` (the
    target version), one per changed block. Each hunk is a dict:
      index      - 0-based position in the returned list
      header     - a `@@ -a,b +c,d @@`-style label for the UI
      old        - the target lines this hunk would restore
      new        - the current lines this hunk would replace
      context_before / context_after - unchanged lines around it (display only)
    Adjacent changes are NOT merged across equal runs: each SequenceMatcher
    non-equal opcode is its own hunk, so the checkbox list matches what the
    diff shows block by block.
    """
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    hunks = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        hunks.append({
            "index": len(hunks),
            "header": f"@@ -{i1 + 1},{i2 - i1} +{j1 + 1},{j2 - j1} @@",
            "old": old_lines[i1:i2],
            "new": new_lines[j1:j2],
            "old_range": (i1, i2),
            "new_range": (j1, j2),
            "context_before": old_lines[max(0, i1 - _CONTEXT):i1],
            "context_after": old_lines[i2:i2 + _CONTEXT],
        })
    return hunks


def apply_selected(old_lines: list, new_lines: list, selected: set) -> list:
    """Rebuild the file with the SELECTED hunks reverted to `old_lines` and the
    rest left as `new_lines`. `selected` holds hunk indices (as compute_hunks
    numbers them). The walk mirrors compute_hunks exactly, so indices line up.
    """
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    out, hi = [], 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(new_lines[j1:j2])       # unchanged region (old == new)
            continue
        if hi in selected:
            out.extend(old_lines[i1:i2])       # restore this block to the target
        else:
            out.extend(new_lines[j1:j2])       # keep the current version
        hi += 1
    return out
