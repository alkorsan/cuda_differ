"""Utility helpers shared across the Differ plugin modules.

This module exists so that `differ_native.py` and `differ_python.py` can
import common helpers at the top of the file (no lazy imports, no
circular-dependency workarounds). `__init__.py` imports these too via
`from .utils import split_lines_safe, ScrollSplittedTab`.
"""

import re
import typing as tp

import cudatext as ct
import cudatext_cmd as ct_cmd


# Pattern that matches a single line terminator (CRLF, lone CR, or lone LF).
# CRLF is listed FIRST so regex alternation tries the two-byte sequence
# before the lone \r — otherwise \r\n would be split as two boundaries
# (a spurious empty string between \r and \n). See split_lines_safe's
# docstring for the full rationale.
_LINE_SPLIT_RE = re.compile(r'\r\n|\r|\n')


def split_lines_safe(text: str) -> tp.List[str]:
    """Split text into lines on \\n, \\r\\n, or \\r ONLY.

    Drop-in replacement for text.splitlines(True) that does NOT treat
    VT, FF, NEL, LS, PS...etc as line boundaries (see comment above _LINE_SPLIT_RE).
    Keepends behavior is preserved: each returned line includes its
    original terminator, matching what splitlines(True) callers expect.
    A trailing terminator-less remainder (if the text doesn't end in a
    line ending) is included as the final element with no terminator,
    same as splitlines(True). Empty input returns [].

    Details:
    splitlines splits on the following 11 line boundaries: https://docs.python.org/3/library/stdtypes.html#str.splitlines
    - \\n Line Feed
    - \\r Carriage Return
    - \\r\\n Carriage Return + Line Feed
    - \\v or \\x0b Line Tabulation (Vertical Tab (VT))
    - \\f or \\x0c Form Feed (Page Break)
    - \\x1c File Separator (FS, \\u001C)
    - \\x1d Group Separator (GS, \\u001D):
    - \\x1e Record Separator (RS, \\u001E)
    - \\x85 Next Line (C1 Control Code, NEL, \\u0085)
    - \\u2028 Line Separator (LS)
    - \\u2029 Paragraph Separator (PS)

    This functions splits *text* into a list of lines, keeping each line's terminator
    appended (matching str.splitlines(True)'s keepends=True contract),
    but splitting ONLY on \\r\\n, \\r, \\n -- NOT on NEL/VT/FF/LS/PS...etc.
    This matches how CudaText stores lines internally: a line containing
    an embedded NEL/VT/FF/LS/PS...etc is a single logical line, not two.
    A trailing empty element (after a final terminator) is dropped, so
    "abc\\n" -> ["abc\\n"] -- same as str.splitlines(True).

    CudaText's editor only treats CR, LF, and CRLF as line breaks; the other
    characters stay inside a single logical line and are rendered as
    in-line control pictures. Using str.splitlines() here would split on
    those extra characters too, producing more "lines" than the editor
    actually has, which causes every diff event line index to drift out
    of sync with the editor (see _refresh_ex).

    Why I use re.finditer and not str.split(): split() can't do this job at all, for one structural reason -- it discards the delimiter. "a\\r\\nb".split('\\r\\n') gives you ['a', 'b'] with the \\r\\n gone. But set_seqs/unidiff call this with keepends=True semantics -- every line needs its original terminator still attached, because the diff engine uses that terminator when reconstructing/rendering output. So whatever splits also has to capture what it split on.
    Three ways to get delimiter-preserving split, ranked:
    1. re.split() with a capturing group — re.split(r'(\\r\\n|\\r|\\n)', text) returns alternating content/delimiter pieces you'd then have to re-zip back together in a loop. Works, but it's an extra reconstruction pass for no benefit over option 2.
    2. finditer (what I used) — one pass, and at each match I already have m.end(), so I slice text[pos:m.end()] directly — content and its trailing terminator in one slice, no reassembly step. This is what I wrote.
    3. Manual two-pointer scan (no regex) — check each position for \\r, \\n, or \\r\\n by hand, same asymptotic cost, more code, easier to get the "is this \\r followed by \\n" lookahead wrong. Not worth it here.
    There's a subtlety str.split() would also get wrong even ignoring the discard problem: splitting on \\r and \\n as separate single-char delimiters (e.g. chaining two .split() calls, or re.split(r'[\\r\\n]')) treats \\r\\n as two boundaries, producing a spurious empty string between them. My pattern lists r'\\r\\n|\\r|\\n' with \\r\\n first, so regex alternation matches the two-char sequence before it'd consider the lone \\r — that ordering is why CRLF collapses to one boundary instead of two. If I'd written r'\\r|\\n|\\r\\n' instead, alternation still tries left-to-right per position, so \\r would win before \\r\\n got a chance and you'd get the same double-split bug. It's already correctly ordered in the delivered code, but worth knowing why the ordering matters if you ever touch that pattern.

    and finditer is faster than re.split() in my tests
    """
    if not text:
        return []
    lines = []
    pos = 0
    for m in _LINE_SPLIT_RE.finditer(text):
        lines.append(text[pos:m.end()])
        pos = m.end()
    if pos < len(text):
        lines.append(text[pos:])
    return lines


def split_lines_with_eol(text: str) -> tp.Tuple[tp.List[str], tp.List[str]]:
    """Split text into parallel (contents, eols) lists.

    Same boundary rules as split_lines_safe (CRLF / CR / LF only), but
    instead of keeping each terminator attached to its line, the
    terminator is reported separately:
    - contents[i] is the line's text WITHOUT any terminator (exactly
      what Editor.replace_lines() accepts as an item - its items must
      not contain "\\n" or "\\r").
    - eols[i] is the line's own terminator: '\\r\\n', '\\n', '\\r', or
      '' for the final terminator-less line.

    This is the parsing half of the sync-back EOL preservation (see
    apply_text_keep_undo): log files can mix CR/LF/CRLF, and rewriting
    the user's original endings would destroy their text.
    """
    contents = []
    eols = []
    for ln in split_lines_safe(text):
        if ln.endswith('\r\n'):
            contents.append(ln[:-2])
            eols.append('\r\n')
        elif ln.endswith('\n') or ln.endswith('\r'):
            contents.append(ln[:-1])
            eols.append(ln[-1])
        else:
            contents.append(ln)
            eols.append('')
    return contents, eols


# Editor.set_line_end() values (see cudatext_api wiki: LINEEND_nnnn
# constants). The getattr fallbacks mirror the upstream constants
# (LINEEND_NONE=0, LINEEND_WIN=1, LINEEND_UNIX=2, LINEEND_MAC=3) for
# CudaText builds whose cudatext.py predates the API -- on those builds
# restore_line_ends() detects the missing method and does nothing, so
# the fallback values are never used for an actual call.
_EOL_TO_LINEEND = {
    '\n':   getattr(ct, 'LINEEND_UNIX', 2),
    '\r\n': getattr(ct, 'LINEEND_WIN', 1),
    '\r':   getattr(ct, 'LINEEND_MAC', 3),
}
_LINEEND_NONE = getattr(ct, 'LINEEND_NONE', 0)


def restore_line_ends(ed, eols):
    """Restore per-line line endings on editor `ed` after its lines were
    rewritten with replace_lines() / insert().

    replace_lines() cannot express line endings: its items must not
    contain "\\n"/"\\r" and every replaced line receives the document's
    default EOL. So after the contents are in place, each line whose
    original ending differs is fixed with Editor.set_line_end() -- the
    API added by the CudaText author for exactly this problem.

    Only the MISMATCHING lines are touched: the current state is read
    back once (get_text_all(ends=True), a single API call) and compared
    per line. A document with uniform endings -- the common case -- was
    already written correctly by replace_lines() and needs ZERO
    set_line_end calls; a mixed-EOL document pays one call per deviant
    line only.

    The final line may legitimately have NO terminator (text not ending
    in a newline); LINEEND_NONE is allowed there (app safeguard rejects
    it for non-last lines, which split_lines_with_eol never produces).
    On builds without set_line_end (older CudaText) this is a no-op and
    endings stay whatever replace_lines() wrote.
    """
    if not eols or not hasattr(ed, 'set_line_end'):
        return
    try:
        actual_eols = split_lines_with_eol(ed.get_text_all(ends=True))[1]
    except Exception:
        actual_eols = None
    last = len(eols) - 1
    for i, want in enumerate(eols):
        if actual_eols is not None and i < len(actual_eols) \
                and actual_eols[i] == want:
            continue
        if want:
            ed.set_line_end(i, _EOL_TO_LINEEND[want])
        elif i == last:
            # Last line without a final terminator.
            ed.set_line_end(i, _LINEEND_NONE)


def apply_text_keep_undo(ed, new_text, log=None):
    """Replace the ENTIRE text of editor `ed` with `new_text`, keeping
    Undo history AND each line's original line ending.

    Why not set_text_all(): it destroys Undo (a differ that kills redo
    is impractical -- comparing means you will change your text).
    Why not plain replace_lines(): it re-joins lines with the document's
    default EOL, and its items must not contain "\\n"/"\\r" -- feeding
    it lines split on "\\n" alone left the CR of CRLF inside the line
    content, so syncing a CRLF compare tab into a document produced
    CRCRLF. (Reported upstream; the author's answer was set_line_end.)

    Sequence (one EDACTION_LOCK/UNLOCK group = one Undo step):
    1. split new_text keeping each line's own terminator;
    2. replace_lines() with the clean CONTENTS (no \\n / \\r inside);
    3. restore_line_ends() re-applies the per-line endings.

    On any failure (e.g. read-only editor) falls back to set_text_all()
    -- correct text, but Undo is lost; `log(ex)` receives the exception
    so the caller can report it.
    """
    try:
        contents, eols = split_lines_with_eol(new_text)
        count = ed.get_line_count()
        # Group everything into one Undo step / repaint batch.
        # (Editor.lock/unlock was deleted from the API; EDACTION_LOCK /
        # EDACTION_UNLOCK via Editor.action() is the replacement. Older
        # builds without the constant just skip grouping.)
        locked = False
        try:
            ed.action(ct.EDACTION_LOCK)
            locked = True
        except Exception:
            pass
        try:
            if count > 0:
                ed.replace_lines(0, count - 1, contents)
            else:
                ed.insert(0, 0, new_text)
            restore_line_ends(ed, eols)
        finally:
            if locked:
                try:
                    ed.action(ct.EDACTION_UNLOCK)
                except Exception:
                    pass
    except Exception as ex:
        if log is not None:
            log(ex)
        ed.set_text_all(new_text)


class ScrollSplittedTab:
    """Manages synchronized scrolling for split compare tabs."""

    keep_caret_visible = False

    def __init__(self, name):
        self.name = name
        self.tab_id = set()

    def toggle(self, on=True):
        act = ct.PROC_EVENTS_SUB if on and ct.ed.get_prop(ct.PROP_TAB_ID) in self.tab_id else ct.PROC_EVENTS_UNSUB
        ct.app_proc(act, self.name+';on_scroll;;')

    def on_scroll(self, ed_self):
        if ed_self.get_prop(ct.PROP_SPLIT)[0] == '-':
            return

        pos_v = ed_self.get_prop(ct.PROP_SCROLL_VERT_INFO)['smooth_pos']
        pos_h = ed_self.get_prop(ct.PROP_SCROLL_HORZ_INFO)['smooth_pos']

        hndl_self = ed_self.get_prop(ct.PROP_HANDLE_SELF)
        hndl_primary = ed_self.get_prop(ct.PROP_HANDLE_PRIMARY)
        hndl_secondary = ed_self.get_prop(ct.PROP_HANDLE_SECONDARY)
        if hndl_self == hndl_primary:
            hndl_opposit = hndl_secondary
        else:
            hndl_opposit = hndl_primary
        e = ct.Editor(hndl_opposit)

        e.set_prop(ct.PROP_SCROLL_VERT_INFO, {'smooth_pos': pos_v})
        e.set_prop(ct.PROP_SCROLL_HORZ_INFO, {'smooth_pos': pos_h})

        e.cmd(ct_cmd.cmd_RepaintEditor)
