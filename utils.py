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


# ----------------------------------------------------------------------
# Opcode-driven unified diff -- a difflib.unified_diff work-alike.
#
# difflib.unified_diff always runs its own SequenceMatcher and offers no
# way to feed it opcodes computed by another engine. Every engine this
# plugin can run -- the native cudatext.diff_proc(DIF_TEXTS) call and
# all the pure-Python matchers behind differ_python.Differ.engine_
# opcodes() -- returns the SAME difflib-compatible opcode format (list
# of (tag, i1, i2, j1, j2) tuples, tag in 'equal'/'delete'/'insert'/
# 'replace', plus the CudaText 'ignore' extension diff_proc emits for
# hunks DIFF_IGN_BLANK_LINES suppressed), so the three functions below
# port difflib's hunk-building pipeline to operate on such a
# precomputed opcode list instead of a matcher instance:
#
#   _format_range_unified()  == difflib._format_range_unified
#   grouped_opcodes()        == SequenceMatcher.get_grouped_opcodes
#   unified_diff_opcodes()   == difflib.unified_diff
#
# Given identical opcodes, unified_diff_opcodes() yields byte-identical
# output to difflib.unified_diff() (verified against the stdlib on
# randomized texts); the only extension is the 'ignore' tag, rendered
# defensively like 'replace' (see unified_diff_opcodes).
# ----------------------------------------------------------------------

def _check_types(a, b, *args):
    """Verbatim port of difflib._check_types: refuse mixed bytes/str
    early -- str.format() silently renders bytes as b'...' reprs, which
    would garble the ---/+++/@@ headers instead of failing loudly."""
    # Checking types is weird, but the alternative is garbled output when
    # someone passes mixed bytes and str to {unified,context}_diff(). E.g.
    # without this check, passing filenames as bytes results in output like
    #   --- b'oldfile.txt'
    #   +++ b'newfile.txt'
    # because of how str.format() incorporates bytes objects.
    if a and not isinstance(a[0], str):
        raise TypeError('lines to compare must be str, not %s (%r)' %
                        (type(a[0]).__name__, a[0]))
    if b and not isinstance(b[0], str):
        raise TypeError('lines to compare must be str, not %s (%r)' %
                        (type(b[0]).__name__, b[0]))
    for arg in args:
        if not isinstance(arg, str):
            raise TypeError('all arguments must be str, not: %r' % (arg,))


def _format_range_unified(start, stop):
    """Convert a half-open [start, stop) line range to the unified-diff
    "ed" format. Verbatim port of difflib._format_range_unified: lines
    are numbered from one, the length prints comma-separated, a length
    of exactly 1 prints the bare start, and an empty range prints the
    line number just BEFORE the range."""
    # Per the diff spec at http://www.unix.org/single_unix_specification/
    beginning = start + 1     # lines start numbering with one
    length = stop - start
    if length == 1:
        return '{}'.format(beginning)
    if not length:
        beginning -= 1        # empty ranges begin at line just before the range
    return '{},{}'.format(beginning, length)


def grouped_opcodes(opcodes, n=3):
    """Isolate change clusters by eliminating ranges with no changes.

    Line-for-line port of difflib.SequenceMatcher.get_grouped_opcodes()
    (Python 3.12 stdlib) operating on a PLAIN opcode list -- the format
    every engine this plugin runs returns (native diff_proc, all the
    differ_python matchers) -- instead of a matcher instance. Yields
    lists of opcodes; each yielded list is one hunk: a run of changes
    with up to n lines of context on each side, split wherever an equal
    run longer than 2*n separates two changes.

    The input list is COPIED first: difflib's own implementation
    mutates the first/last opcode in place (the leading/trailing
    context fixup below), and an engine's returned list must stay
    untouched -- other consumers may walk it afterwards.

    'ignore' opcodes (the CudaText extension) count as CHANGES here,
    like any non-equal opcode: they join the surrounding hunk instead
    of splitting it.
    """
    codes = list(opcodes)
    if not codes:
        codes = [('equal', 0, 1, 0, 1)]
    # Fixup leading and trailing groups if they show no changes.
    if codes[0][0] == 'equal':
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = tag, max(i1, i2-n), i2, max(j1, j2-n), j2
    if codes[-1][0] == 'equal':
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = tag, i1, min(i2, i1+n), j1, min(j2, j1+n)

    nn = n + n
    group = []
    for tag, i1, i2, j1, j2 in codes:
        # End the current group and start a new one whenever
        # there is a large range with no changes.
        if tag == 'equal' and i2-i1 > nn:
            group.append((tag, i1, min(i2, i1+n), j1, min(j2, j1+n)))
            yield group
            group = []
            i1, j1 = max(i1, i2-n), max(j1, j2-n)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == 'equal'):
        yield group


def unified_diff_opcodes(a, b, opcodes, fromfile='', tofile='',
                         fromfiledate='', tofiledate='', n=3,
                         lineterm='\n'):
    """difflib.unified_diff work-alike driven by PRECOMPUTED opcodes.

    Same parameters and the same yielded lines as
    difflib.unified_diff(a, b, fromfile, tofile, fromfiledate,
    tofiledate, n, lineterm) -- the '--- / +++' file headers once, then
    one '@@ -x,y +u,v @@' hunk header per change cluster with up to n
    context lines around it -- except that NO diff engine runs here:
    'opcodes' is the (tag, i1, i2, j1, j2) list computed by whichever
    engine the CALLER chose (native diff_proc DIF_TEXTS, any of the
    differ_python matchers). 'a' / 'b' are the two line lists those
    opcodes index into (keepends lines, split on CR/LF/CRLF only -- see
    split_lines_safe), so the yielded body lines keep their original
    terminators exactly the way difflib renders them; a line missing
    its trailing newline stays without one (difflib emits no
    backslash-newline marker either).

    This is what lets the "Diff current document with..." commands
    honor differ.algorithm.diff_algorithm: all the plugin's engines
    agree on the opcode format, so only the RENDERING had to be
    engine-independent.

    The single behavioral extension over difflib: an 'ignore' opcode
    (the CudaText extension diff_proc emits for blank-line hunks
    suppressed by DIFF_IGN_BLANK_LINES) is rendered like 'replace' --
    its A lines as '-' and its B lines as '+'. Those lines ARE
    physically different (the ignore options only said "don't show
    them as differences"), so emitting them as changes is the only
    rendering that keeps the output a valid patch; emitting them as
    context would claim A == B where they differ byte-wise, and
    patch / git apply would reject the result.
    """
    _check_types(a, b, fromfile, tofile, fromfiledate, tofiledate,
                 lineterm)
    started = False
    for group in grouped_opcodes(opcodes, n):
        if not started:
            started = True
            fromdate = '\t{}'.format(fromfiledate) if fromfiledate else ''
            todate = '\t{}'.format(tofiledate) if tofiledate else ''
            yield '--- {}{}{}'.format(fromfile, fromdate, lineterm)
            yield '+++ {}{}{}'.format(tofile, todate, lineterm)

        first, last = group[0], group[-1]
        file1_range = _format_range_unified(first[1], last[2])
        file2_range = _format_range_unified(first[3], last[4])
        yield '@@ -{} +{} @@{}'.format(file1_range, file2_range, lineterm)

        for tag, i1, i2, j1, j2 in group:
            if tag == 'equal':
                for line in a[i1:i2]:
                    yield ' ' + line
                continue
            if tag in ('replace', 'delete', 'ignore'):
                for line in a[i1:i2]:
                    yield '-' + line
            if tag in ('replace', 'insert', 'ignore'):
                for line in b[j1:j2]:
                    yield '+' + line


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
