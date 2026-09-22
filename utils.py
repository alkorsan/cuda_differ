"""Utility helpers shared across the Differ plugin modules.

This module exists so that `differ_native.py` and `differ_python.py` can
import common helpers at the top of the file (no lazy imports, no
circular-dependency workarounds). `__init__.py` imports these too via
`from .utils import split_lines_safe, ScrollSplittedTab`.
"""

import re
import typing as tp

import cudatext as ct


# Pattern that matches a single line terminator (CRLF, lone CR, or lone LF).
# CRLF is listed FIRST so regex alternation tries the two-byte sequence
# before the lone \r — otherwise \r\n would be split as two boundaries
# (a spurious empty string between \r and \n). See split_lines_safe's
# docstring for the full rationale.
_LINE_SPLIT_RE = re.compile(r'\r\n|\r|\n')

# The exotic line boundaries str.splitlines() ALSO splits on, but
# CudaText (and this plugin) must NOT: VT, FF, FS, GS, RS, NEL, LS, PS.
# Their absence is the guard for split_lines_safe's splitlines() fast
# path — with none of them present, splitlines(True) splits on exactly
# \n / \r\n / \r and nothing else, which is precisely this module's
# contract. Kept as a tuple of 1-char strings so the `c in text` guard
# runs as 8 memchr-class scans with early break.
_EXOTIC_BOUNDARY_CHARS = (
    '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85',
    '\u2028', '\u2029',
)


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
    in-line control pictures. Using str.splitlines() UNCONDITIONALLY here
    would split on those extra characters too, producing more "lines" than
    the editor actually has, which causes every diff event line index to
    drift out of sync with the editor (see refresh_compare). That is why
    the splitlines() fast path below is guarded by an explicit absence
    check of the 8 exotic boundary chars: with none of them present,
    splitlines(True) and this contract coincide EXACTLY (verified by
    randomized equivalence tests against the regex walk below).

    Why the fallback uses re.finditer and not str.split(): split() can't
    do this job at all, for one structural reason -- it discards the
    delimiter. "a\\r\\nb".split('\\r\\n') gives you ['a', 'b'] with the
    \\r\\n gone. But set_seqs/unidiff call this with keepends=True
    semantics -- every line needs its original terminator still attached,
    because the diff engine uses that terminator when
    reconstructing/rendering output. So whatever splits also has to
    capture what it split on.
    Three ways to get delimiter-preserving split, ranked:
    1. re.split() with a capturing group — re.split(r'(\\r\\n|\\r|\\n)', text) returns alternating content/delimiter pieces you'd then have to re-zip back together in a loop. Works, but it's an extra reconstruction pass for no benefit over option 2.
    2. finditer (what I used) — one pass, and at each match I already have m.end(), so I slice text[pos:m.end()] directly — content and its trailing terminator in one slice, no reassembly step. This is what I wrote.
    3. Manual two-pointer scan (no regex) — check each position for \\r, \\n, or \\r\\n by hand, same asymptotic cost, more code, easier to get the "is this \\r followed by \\n" lookahead wrong. Not worth it here.
    There's a subtlety str.split() would also get wrong even ignoring the discard problem: splitting on \\r and \\n as separate single-char delimiters (e.g. chaining two .split() calls, or re.split(r'[\\r\\n]')) treats \\r\\n as two boundaries, producing a spurious empty string between them. My pattern lists r'\\r\\n|\\r|\\n' with \\r\\n first, so regex alternation matches the two-char sequence before it'd consider the lone \\r — that ordering is why CRLF collapses to one boundary instead of two. If I'd written r'\\r|\\n|\\r\\n' instead, alternation still tries left-to-right per position, so \\r would win before \\r\\n got a chance and you'd get the same double-split bug. It's already correctly ordered in the delivered code, but worth knowing why the ordering matters if you ever touch that pattern.

    and finditer is faster than re.split() in my tests
    """
    if not text:
        return []
    # Fast path 0 (the overwhelmingly common case): when the text has NO
    # exotic line-boundary characters, str.splitlines(True) is EXACTLY
    # this function's contract -- it splits on \n, \r\n and lone \r
    # (keeping terminators, matching CudaText's line model) and leaves
    # VT/FF/FS/GS/RS/NEL/LS/PS inside single lines. splitlines is one C
    # pass; the guard is 8 memchr scans (~18ms per 50MB, with early
    # break). Measured on a 1M-line / 63MB LF text: ~0.11s vs ~0.83s
    # for the regex walk below (and CRLF texts ~0.10s vs ~0.64s -- both
    # endings now take the fast path). Only texts that actually contain
    # an exotic boundary char pay the precise regex walk.
    if not any(c in text for c in _EXOTIC_BOUNDARY_CHARS):
        return text.splitlines(True)

    # ---- fast path 1: pure-LF text (no CR at all) ----
    # str.split's last element is '' iff the text ends with the
    # separator: that phantom is the "nothing after the final
    # terminator", not a line -- drop it, then re-attach the terminator
    # every remaining piece DID have. Byte-identical to the regex walk
    # below for every CR-free input (test_split_lines_fast.py).
    if '\r' not in text:
        parts = text.split('\n')
        if parts[-1] == '':
            parts.pop()
            return [p + '\n' for p in parts]
        # no trailing terminator: every piece but the last owns one
        if len(parts) == 1:
            return [text]
        return [p + '\n' for p in parts[:-1]] + [parts[-1]]

    # ---- fast path 2: CRLF-only text ----
    # Safe only when NO lone CR and NO lone LF exists (every '\r' starts
    # a '\r\n' and every '\n' ends one): splitting on '\r\n' then finds
    # exactly the terminator boundaries. Three C-speed count() scans
    # decide; mixed-terminator texts fall through to the regex walk.
    n_cr = text.count('\r')
    if n_cr:
        n_lf = text.count('\n')
        if n_cr == n_lf and n_cr == text.count('\r\n'):
            parts = text.split('\r\n')
            if parts[-1] == '':
                parts.pop()
                return [p + '\r\n' for p in parts]
            if len(parts) == 1:
                return [text]
            return [p + '\r\n' for p in parts[:-1]] + [parts[-1]]

    # ---- general path: mixed terminators, regex walk ----
    lines = []
    pos = 0
    for m in _LINE_SPLIT_RE.finditer(text):
        lines.append(text[pos:m.end()])
        pos = m.end()
    if pos < len(text):
        lines.append(text[pos:])
    return lines


class ScrollSplittedTab:
    """Manages synchronized scrolling for split compare tabs.

    How the timing works (and why this code is shaped the way it is):

    CudaText fires the on_scroll event AFTER the scrolled editor has
    painted (ATSynEdit: Paint -> PaintEx -> DoEventScroll), and program-
    atic scroll changes fire it too (the SmoothPos-change detection runs
    at paint time). So a naive "on event, copy the position to the other
    half" implementation always lags one paint behind AND echoes: the
    mirrored write makes the other half fire on_scroll back at the plugin,
    which (without guards) does a redundant set_prop plus an unconditional
    cmd_RepaintEditor on the already-painted half -- an extra full repaint
    per scroll step that visibly delays the following sync updates on big
    compare files.

    This implementation keeps the two halves lock-step by:

    - copying 'smooth_pos' (per-pixel float position) from the scrolled
      half to the other half inside the event -- the mirrored half's
      repaint then happens in the same message-loop batch, right after
      the scrolled half's paint, so both move in the same display frame;
    - skipping the write entirely when the other half already sits at the
      same position: no position change means no repaint, no echo event,
      and the whole cascade dies out instead of ping-ponging;
    - a re-entrancy guard (like the official cuda_sync_scroll plugin) so
      a synchronously re-entered event can never recurse;
    - the end-of-scroll guard from cuda_sync_scroll: when the scrolled
      half is at its last position, the write is skipped -- workaround
      for a CudaText bug with scrolling at the end of non-equal-height
      files (possible here when word-wrap makes the halves differ in
      visual height).

    WHY THE MIRRORED HALF USES EDACTION_UPDATE (synchronous Repaint)
    AND NOT cmd_RepaintEditor (forced Invalidate):

    Users could see the initiating half scroll first and the mirrored
    half catch up a few milliseconds later whenever the RIGHT half (or
    the overview) initiated. cmd_RepaintEditor maps to Ed.Update(false,
    true, false), which is only a FORCED INVALIDATE -- the paint is
    still delivered asynchronously through the message queue, where it
    competes with pending input messages (a scrollbar / overview drag
    floods the queue with mouse moves, and Windows delivers WM_PAINT
    only when the queue is otherwise empty) and can land in the NEXT
    display frame. ed.action(EDACTION_UPDATE) maps to Ed.Repaint --
    Invalidate + LCL Update, i.e. the mirrored half paints
    SYNCHRONOUSLY, right here inside the event callback, so both halves
    are on screen in the same frame no matter which half initiated or
    how busy the message queue is. The synchronous paint of the
    mirrored half fires its own on_scroll echo; the _busy guard below
    drops it, and by then the positions are equal anyway (no write, no
    cascade).
    """

    def __init__(self, name):
        self.name = name
        self.tab_id = set()
        # Re-entrancy guard: True while an on_scroll handler is mirroring
        # a position, so a nested (synchronous) on_scroll for the opposite
        # half cannot start a second mirror pass. EDACTION_UPDATE's
        # synchronous repaint makes this nesting actually happen now.
        self._busy = False

    def toggle(self, on=True):
        """(Un)subscribe the on_scroll event for this module.

        Subscribe when sync is on AND at least one compare tab exists --
        the focused tab does NOT have to be a compare tab: the event is
        subscribed globally (on_scroll supports no event filter) and
        filtered per event in __init__.py. The old condition (focused tab
        must be a compare tab) could UNSUBSCRIBE while compares were open
        (e.g. change_config ran from a normal tab), silently killing the
        sync until the next compare was created."""
        act = ct.PROC_EVENTS_SUB if on and self.tab_id else ct.PROC_EVENTS_UNSUB
        ct.app_proc(act, self.name+';on_scroll;;')

    def on_scroll(self, ed_self):
        """Mirror the scroll position of ed_self to the opposite half of
        the split tab. Called from __init__.py's on_scroll only for compare
        tabs (already _is_compare_tab-filtered)."""
        if ed_self.get_prop(ct.PROP_SPLIT)[0] == '-':
            return
        if self._busy:
            return
        self._busy = True
        try:
            self._mirror_scroll(ed_self)
        finally:
            self._busy = False

    def _mirror_scroll(self, ed_self):
        info_v = ed_self.get_prop(ct.PROP_SCROLL_VERT_INFO)
        info_h = ed_self.get_prop(ct.PROP_SCROLL_HORZ_INFO)
        pos_v = info_v['smooth_pos']
        pos_h = info_h['smooth_pos']

        hndl_self = ed_self.get_prop(ct.PROP_HANDLE_SELF)
        hndl_primary = ed_self.get_prop(ct.PROP_HANDLE_PRIMARY)
        hndl_secondary = ed_self.get_prop(ct.PROP_HANDLE_SECONDARY)
        if hndl_self == hndl_primary:
            hndl_opposit = hndl_secondary
        else:
            hndl_opposit = hndl_primary
        e = ct.Editor(hndl_opposit)

        info2_v = e.get_prop(ct.PROP_SCROLL_VERT_INFO)
        info2_h = e.get_prop(ct.PROP_SCROLL_HORZ_INFO)

        # End-of-scroll guard (from the official cuda_sync_scroll plugin):
        # skip the write when the scrolled half is at its last position --
        # CudaText misbehaves when scrolling at the end of files whose
        # halves differ in height (word-wrap can do that here).
        # Missing 'smooth_pos_last' (older API) just disables the guard.
        max_v = info_v.get('smooth_pos_last')
        max2_v = info2_v.get('smooth_pos_last')
        max_h = info_h.get('smooth_pos_last')
        max2_h = info2_h.get('smooth_pos_last')

        changed = False

        # Vertical: write only when the position actually differs -- a
        # redundant write would still cost a Python->C call and a
        # scrollbars update, and would keep the echo cascade alive.
        if pos_v != info2_v['smooth_pos']:
            if (max_v is None or pos_v < max_v) and \
               (max2_v is None or pos_v < max2_v):
                e.set_prop(ct.PROP_SCROLL_VERT_INFO, {'smooth_pos': pos_v})
                changed = True

        # Horizontal: same treatment.
        if pos_h != info2_h['smooth_pos']:
            if (max_h is None or pos_h < max_h) and \
               (max2_h is None or pos_h < max2_h):
                e.set_prop(ct.PROP_SCROLL_HORZ_INFO, {'smooth_pos': pos_h})
                changed = True

        # Paint the mirrored half ONLY when its position really changed,
        # and paint it SYNCHRONOUSLY (EDACTION_UPDATE = Ed.Repaint =
        # Invalidate + Update; see the class docstring for why the async
        # forced invalidate of cmd_RepaintEditor left a visible
        # few-millisecond drift when the right half / overview initiated).
        # The scrolled half has already painted (the event is post-paint),
        # so repainting it again would only burn time; and when nothing
        # changed there is nothing to show at all.
        if changed:
            e.action(ct.EDACTION_UPDATE)
