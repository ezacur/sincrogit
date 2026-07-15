"""Diff HTML rendering (pure functions): unified, side-by-side, intra-line.

Shared by the File history and Time Machine dialogs — both render the same
theme-aware colored diffs, so the render lives here once instead of one dialog
reaching into the other's privates. Everything is a pure function of text +
theme flavor; the dialogs pick `dark` from the app palette.
"""

import difflib
import html

# Diff colors per theme flavor (keyed off the panel palette's background).
# *_hl are the stronger intra-line backgrounds: WHAT changed inside the line.
DIFF_LIGHT = {"meta": "#8a929c", "hunk": "#2b6cb0", "add": "#1a7f37", "add_bg": "#e6f4eb",
              "del": "#cf222e", "del_bg": "#fbebed", "ctx": "#444444",
              "add_hl": "#9fdcb4", "del_hl": "#f4b6bd"}
DIFF_DARK = {"meta": "#9aa3af", "hunk": "#6cb0f0", "add": "#4cc07a", "add_bg": "#203428",
             "del": "#ec7272", "del_bg": "#3a2628", "ctx": "#c8cdd4",
             "add_hl": "#2f5c3f", "del_hl": "#6e3a3f"}

_MAX_SBS_ROWS = 5000  # side-by-side rows cap: keeps QTextEdit responsive on huge files


def mark_intraline(old_line: str, new_line: str, c: dict) -> tuple:
    """(old_html, new_html) of a modified line pair, with the spans that actually
    changed wrapped in a stronger background — you see WHAT changed inside the
    line, not just that the line changed."""
    o, n = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_line, new_line).get_opcodes():
        oseg, nseg = html.escape(old_line[i1:i2]), html.escape(new_line[j1:j2])
        if tag == "equal":
            o.append(oseg)
            n.append(nseg)
        else:
            if oseg:
                o.append(f'<span style="background:{c["del_hl"]};">{oseg}</span>')
            if nseg:
                n.append(f'<span style="background:{c["add_hl"]};">{nseg}</span>')
    return "".join(o), "".join(n)


def diff_html(old_text: str, current_text: str, dark: bool = False) -> str:
    """Unified diff (old version -> current file) as colored HTML, theme-aware,
    with intra-line highlighting on paired -/+ lines."""
    c = DIFF_DARK if dark else DIFF_LIGHT
    diff = list(difflib.unified_diff(
        old_text.splitlines(), current_text.splitlines(),
        fromfile="selected version", tofile="current file", lineterm="",
    ))
    rows = []

    def emit(kind: str, body_html: str):
        rows.append(f'<span style="color:{c[kind]};background:{c[kind + "_bg"]};'
                    f'display:block;">{body_html or "&nbsp;"}</span>')

    in_hunk = False  # the ---/+++ headers only appear before the first @@
    i = 0
    while i < len(diff):
        ln = diff[i]
        if not in_hunk and ln.startswith(("+++", "---")):
            rows.append(f'<span style="color:{c["meta"]};">{html.escape(ln)}</span>')
            i += 1
        elif ln.startswith("@@"):
            in_hunk = True
            rows.append(f'<span style="color:{c["hunk"]};font-weight:bold;">'
                        f'{html.escape(ln)}</span>')
            i += 1
        elif ln.startswith("-"):
            # A run of removals followed by a run of additions is a MODIFICATION:
            # pair them index-wise and highlight what changed inside each line.
            dels = []
            while i < len(diff) and diff[i].startswith("-"):
                dels.append(diff[i][1:])
                i += 1
            adds = []
            while i < len(diff) and diff[i].startswith("+"):
                adds.append(diff[i][1:])
                i += 1
            paired = min(len(dels), len(adds))
            marked = [mark_intraline(dels[k], adds[k], c) for k in range(paired)]
            for k, d in enumerate(dels):
                emit("del", "-" + (marked[k][0] if k < paired else html.escape(d)))
            for k, a in enumerate(adds):
                emit("add", "+" + (marked[k][1] if k < paired else html.escape(a)))
        elif ln.startswith("+"):
            emit("add", html.escape(ln))
            i += 1
        else:
            rows.append(f'<span style="color:{c["ctx"]};">{html.escape(ln) or "&nbsp;"}</span>')
            i += 1
    if not rows:
        return (f'<pre style="color:{c["meta"]};font-family:Consolas,monospace;'
                f'padding:8px;">(no differences vs the current file)</pre>')
    body = "\n".join(rows)
    return (f'<pre style="font-family:Consolas,monospace;font-size:10pt;'
            f'margin:0;padding:6px;line-height:1.35;">{body}</pre>')


def diff_html_sbs(old_text: str, new_text: str, dark: bool = False) -> str:
    """Side-by-side diff (old version | current file) as a two-column HTML table,
    theme-aware. Same palette as the unified view."""
    c = DIFF_DARK if dark else DIFF_LIGHT
    if old_text == new_text:  # same message the unified view shows
        return (f'<pre style="color:{c["meta"]};font-family:Consolas,monospace;'
                f'padding:8px;">(no differences vs the current file)</pre>')
    a, b = old_text.splitlines(), new_text.splitlines()

    def cell(body_html, bg=None, color=None):
        style = f"color:{color or c['ctx']};"
        if bg:
            style += f"background:{bg};"
        return (f'<td style="{style}padding:0 6px;white-space:pre;">'
                f'{body_html or "&nbsp;"}</td>')

    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append("<tr>" + cell(html.escape(a[i1 + k]))
                            + cell(html.escape(b[j1 + k])) + "</tr>")
        else:
            # Paired modified lines also get intra-line highlighting: the spans
            # that actually changed use the stronger *_hl background.
            paired = min(i2 - i1, j2 - j1)
            for k in range(max(i2 - i1, j2 - j1)):
                has_l, has_r = i1 + k < i2, j1 + k < j2
                if k < paired:
                    left, right = mark_intraline(a[i1 + k], b[j1 + k], c)
                else:
                    left = html.escape(a[i1 + k]) if has_l else ""
                    right = html.escape(b[j1 + k]) if has_r else ""
                rows.append(
                    "<tr>"
                    + cell(left, bg=c["del_bg"] if has_l else None,
                           color=c["del"] if has_l else None)
                    + cell(right, bg=c["add_bg"] if has_r else None,
                           color=c["add"] if has_r else None)
                    + "</tr>")
        if len(rows) > _MAX_SBS_ROWS:
            rows.append("<tr>" + cell(f"… truncated at {_MAX_SBS_ROWS} lines …")
                        + cell("") + "</tr>")
            break
    if not rows:
        return (f'<pre style="color:{c["meta"]};font-family:Consolas,monospace;'
                f'padding:8px;">(no differences vs the current file)</pre>')
    head = (f'<tr><td style="color:{c["meta"]};padding:0 6px;">selected version</td>'
            f'<td style="color:{c["meta"]};padding:0 6px;">current file</td></tr>')
    return (f'<table style="font-family:Consolas,monospace;font-size:10pt;'
            f'border-collapse:collapse;width:100%;line-height:1.35;">'
            f'{head}{"".join(rows)}</table>')
