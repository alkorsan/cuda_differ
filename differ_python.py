"""Differ module for Python algorithms (hybrid, myers, vscode, patience, difflib).

This file is self-contained: it contains everything needed to run the
pure-Python diff algorithms. It does NOT depend on differ_native.py.

For native algorithms (native_histogram, native_myers), use
differ_native.py instead — the native engine is 10-30x faster.

Code is intentionally duplicated from differ_native.py to allow
independent evolution of the native and Python codepaths.
"""

import time
from difflib import SequenceMatcher as DefaultSequenceMatcher, unified_diff
from .myers import MyersSequenceMatcher, InlineMyersSequenceMatcher
from .patiencediff import PatienceSequenceMatcher
from .vscode_diff import VSCodeSequenceMatcher
from .char_diff import char_diff
from .profiling import Profiler
from collections import Counter


def HybridSequenceMatcher(isjunk=None, a='', b=''):
    """Create a hybrid diff matcher that combines patience + Myers.

    Runs patience diff first (finds unique-line anchors like
    'def on_change_slow'), then fills gaps with Myers (finds matches
    in non-unique regions like repeated 'dsds'/'ff' blocks).

    This combines the strengths of both algorithms: patience correctly
    anchors unique lines that Myers' LCS might skip, while Myers handles
    regions with no unique lines (where patience matches nothing).
    """
    patience_matcher = PatienceSequenceMatcher(isjunk, a, b)
    patience_blocks = patience_matcher.get_matching_blocks()

    # For each gap between patience matching blocks, run Myers to find
    # additional matches within the gap
    all_blocks = []
    last_a = 0
    last_b = 0
    for ai, bj, size in patience_blocks:
        # Gap before this patience block
        if ai > last_a or bj > last_b:
            gap_a = a[last_a:ai]
            gap_b = b[last_b:bj]
            if gap_a and gap_b:
                myers = MyersSequenceMatcher(None, gap_a, gap_b)
                for mi, mj, msize in myers.get_matching_blocks():
                    if msize > 0:
                        all_blocks.append((last_a + mi, last_b + mj, msize))
            elif not gap_a and not gap_b:
                pass  # no gap
        # The patience block itself
        if size > 0:
            all_blocks.append((ai, bj, size))
        last_a = ai + size
        last_b = bj + size

    # Build a difflib-compatible matcher from the combined blocks
    return _CombinedMatcher(a, b, all_blocks)


class _CombinedMatcher:
    """Wraps pre-computed matching blocks in a difflib-compatible API.

    Used by HybridSequenceMatcher to present its combined patience+Myers
    matching blocks through the same get_opcodes()/get_matching_blocks()
    interface that Differ.compare() expects."""

    def __init__(self, a, b, matching_blocks):
        """Store the two sequences and their pre-computed matching blocks.

        Args:
            a, b: the two line sequences being compared.
            matching_blocks: list of (i, j, n) tuples from patience+Myers.
        """
        self.a = a
        self.b = b
        self._matching_blocks = matching_blocks
        self.opcodes = None

    def get_matching_blocks(self):
        """Return matching blocks with a sentinel at the end."""
        blocks = list(self._matching_blocks)
        # Ensure sentinel
        if not blocks or blocks[-1] != (len(self.a), len(self.b), 0):
            blocks.append((len(self.a), len(self.b), 0))
        return blocks

    def get_opcodes(self):
        """Convert matching blocks to difflib-style opcodes.

        Walks the matching blocks in order, emitting 'replace'/'delete'/
        'insert' for gaps between blocks and 'equal' for each block.
        """
        if self.opcodes is not None:
            return self.opcodes
        opcodes = []
        i = j = 0
        for ai, bj, size in self.get_matching_blocks():
            if i < ai and j < bj:
                opcodes.append(('replace', i, ai, j, bj))
            elif i < ai:
                opcodes.append(('delete', i, ai, j, bj))
            elif j < bj:
                opcodes.append(('insert', i, ai, j, bj))
            if size > 0:
                opcodes.append(('equal', ai, ai + size, bj, bj + size))
            i = ai + size
            j = bj + size
        self.opcodes = opcodes
        return opcodes


# Internal benchmark toggle. When set to True, Differ.compare() prints the
# total time elapsed from when the generator starts executing until it is
# fully consumed (or closed).
_BENCHMARK = True


# --- Event constants ---
# These are the event IDs yielded by Differ.compare(). __init__.py
# consumes them to paint the side-by-side compare view.
# They are duplicated in differ_native.py — both modules define the
# same constants so they can be used independently.
A_LINE_DEL = '-'
B_LINE_ADD = '+'
A_LINE_CHANGE = '-*'
B_LINE_CHANGE = '+*'
A_GAP = '-^'
B_GAP = '+^'
A_SYMBOL_DEL = '--'
B_SYMBOL_ADD = '++'
A_DECOR_YELLOW = '-y'
A_DECOR_RED = '-r'
B_DECOR_YELLOW = '+y'
B_DECOR_GREEN = '+g'
# Alignment event: a pair of lines (one in A, one in B) that must be kept
# at the same visual Y position. Used by __init__.py to add compensating
# gaps when word-wrap is on and the two lines wrap to a different number
# of visual rows.
ALIGN = '='


def _format_range_unified(start, stop):
    """Convert a (start, stop) line range to unified-diff hunk-header format.

    Verbatim copy of ``difflib._format_range_unified``. It is a private helper
    in CPython, so we keep our own to stay independent of internal renames.
    """
    beginning = start + 1  # lines start numbering with one
    length = stop - start
    if length == 1:
        return '{}'.format(beginning)
    if not length:
        beginning -= 1  # empty ranges begin at line just before the range
    return '{},{}'.format(beginning, length)


# Why this function exists instead of calling ``difflib.unified_diff`` directly:
# ``difflib.unified_diff`` builds its internal ``SequenceMatcher`` with the
# default ``autojunk=True`` and does not expose any way to change it -- see
# https://github.com/python/cpython/issues/118150. The plugin has a
# user-facing ``autojunk`` config option that already controls the side-by-side
# compare path; without this wrapper, the unified-diff path (the "Diff with
# file..." / "Diff with tab..." commands) would silently ignore that option
# when the user sets ``autojunk=False``.
#
# Implementation is a verbatim copy of ``difflib.unified_diff`` from CPython
# with two differences:
#   1. ``SequenceMatcher(None, a, b, autojunk=autojunk)`` instead of
#      ``SequenceMatcher(None, a, b)`` -- the whole point.
#   2. ``fromfiledate``/``tofiledate`` of ``None`` fall back to an empty
#      string instead of the current system timestamp.
def _unified_diff(a, b, fromfile='', tofile='',
                  fromfiledate='', tofiledate='',
                  n=3, lineterm='\n', autojunk=True):
    """Produce unified diff output, same as difflib.unified_diff but with
    explicit autojunk control. See the long comment above for why this
    exists."""
    if fromfiledate is None:
        fromfiledate = ''
    if tofiledate is None:
        tofiledate = ''

    started = False
    for group in DefaultSequenceMatcher(
            None, a, b, autojunk=autojunk).get_grouped_opcodes(n):
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
            if tag in {'replace', 'delete'}:
                for line in a[i1:i2]:
                    yield '-' + line
            if tag in {'replace', 'insert'}:
                for line in b[j1:j2]:
                    yield '+' + line


class Differ:
    """Differ for Python algorithms (hybrid, myers, vscode, patience, difflib).

    Handles all non-native algorithms. For native algorithms
    (native_histogram, native_myers), use differ_native.Differ instead.

    This class is self-contained: it does not import from differ_native.
    Code that overlaps with differ_native.Differ (event constants,
    _replace_block, _find_best_pairs, _char_diff_pair, unidiff, etc.) is
    duplicated intentionally to allow independent evolution.

    compare() function return tuples for paint text:
    id can be:
          - paint deleted line in file a
              return (id, y)
          + paint added line in file b
              return (id, y)
    -* / +* paint changed line in file a / b
              return (id, y)
    -^ / +^ gap in file a / b
              return (id, y, start, end)
              Gap is inserted after line `y-1` (i.e. between lines y-1 and y)
              and compensates for the lines [start, end) on the OTHER side.
              (start/end are line indices in the other file.)
         -- detail paint deleted symbols in file a
         ++ detail paint added symbols in file b
              return (id, y, x, nlen)
         = visually-aligned line pair (a_line, b_line)
              return (id, a_line, b_line)
              Consumed by __init__.py to add a compensating gap when the two
              lines wrap to a different number of visual rows.
    """

    def __init__(self, a='', b=''):
        """Initialize the Differ with two line sequences.

        Sets default options: hybrid algorithm, autojunk on, detailed
        compare on. The algorithm can be changed later via
        self.diff_algorithm before calling compare().
        """
        self.withdetail = True
        # Algorithm key stored in self.diff_algorithm. One of:
        #
        #   'hybrid'          (HybridSequenceMatcher — pure-Python patience
        #                       anchoring on unique lines + Myers for the
        #                       gaps; best pure-Python quality),
        #   'myers'           (MyersSequenceMatcher — pure-Python O(NP)
        #                       Wu/Manber/Myers/Miller 1989 with common
        #                       prefix/suffix trimming and a
        #                       non-matching-line discard preprocessing pass),
        #   'vscode'          (VSCodeSequenceMatcher — pure-Python VS Code-
        #                       style DP/Myers with equality scoring;
        #                       slowest, best quality for duplicated-line
        #                       files),
        #   'patience'        (PatienceSequenceMatcher — pure-Python
        #                       patience diff; anchors on unique lines),
        #   'difflib'         (Python stdlib SequenceMatcher with autojunk).
        #
        # These are pure-Python implementations. For native algorithms
        # (10-30x faster), use differ_native.Differ instead.
        self.diff_algorithm = 'hybrid'
        self.autojunk = True
        self.ratio = 0.75  # kept for API compat with __init__.py; unused
        self.set_seqs(a, b)
        self.diffmap = []

    def set_seqs(self, a, b):
        """Set the two line sequences to compare."""
        self.a = a
        self.b = b

    def _char_diff(self, line_a, line_b):
        """Compute char-level diff between two single-line strings.

        Always uses the pure-Python char_diff.char_diff() — this Differ
        is Python-only. The native DIF_CHARS path is in
        differ_native.Differ._char_diff.

        Returns: list of (tag, a_start, a_end, b_start, b_end) tuples
        where tag is 'equal'/'delete'/'insert'/'replace' and offsets are
        character positions into line_a / line_b. Same format as
        difflib.SequenceMatcher.get_opcodes() operating on characters.
        """
        Profiler.start('char_diff:python_call')
        try:
            return char_diff(line_a, line_b)
        finally:
            Profiler.stop('char_diff:python_call')

    # Threshold for the "trivial equal block" check in _realign_opcodes.
    # If the EQUAL block between an INSERT and a DELETE (or vice versa)
    # has this many or fewer non-whitespace characters total, it is
    # absorbed into a single REPLACE block. The value 4 matches VS Code's
    # removeVeryShortMatchingLinesBetweenDiffs threshold.
    _REALIGN_TRIVIAL_THRESHOLD = 4
    # Minimum combined size for a REPLACE block to be considered "large
    # enough" to absorb a short EQUAL block between it and the next
    # REPLACE. Matches VS Code's
    # removeVeryShortMatchingLinesBetweenDiffs threshold (line 350):
    # before.seq1Range.length + before.seq2Range.length > 5
    _REALIGN_MIN_LARGE_REPLACE = 6

    def _realign_opcodes(self, opcodes):
        """Post-process opcodes to match VS Code's alignment quality.

        Two transformations (both ported from VS Code's
        heuristicSequenceOptimizations.ts):

        1. Merge INSERT+EQUAL(trivial)+DELETE (or DELETE+EQUAL(trivial)+
           INSERT) into a single REPLACE. This fixes the LCS tie-breaking
           issue where Myers matches a trivial line (empty, whitespace)
           instead of a meaningful line, causing identical lines to show
           as one added + one deleted instead of paired.

        2. Absorb short EQUAL blocks (<= 4 non-whitespace chars) between
           two REPLACE blocks into a single REPLACE, if at least one of
           the REPLACE blocks is "large" (combined lines > 5). This is
           VS Code's removeVeryShortMatchingLinesBetweenDiffs (line 325).
           It prevents Myers from fragmenting a large REPLACE region into
           multiple pieces by matching short trivial lines (like '\n' or
           '}') inside it. Without this, A[272] in test_2a.py would be
           in a separate REPLACE block from the lines around it, causing
           positional pairing to misalign it with the wrong B line.

        Both are no-ops for VS Code's own algorithm (it doesn't produce
        these patterns) and for patience (same).
        """
        if len(opcodes) < 3:
            return opcodes
        result = list(opcodes)

        # Pass 1: merge INSERT+EQUAL(trivial)+DELETE patterns
        i = 1
        while i < len(result) - 1:
            prev = result[i - 1]
            cur = result[i]
            nxt = result[i + 1]
            if (cur[0] == 'equal' and
                    prev[0] in ('insert', 'delete') and
                    nxt[0] in ('insert', 'delete') and
                    prev[0] != nxt[0]):
                _, ei1, ei2, ej1, ej2 = cur
                equal_text = ''.join(self.a[ei1:ei2])
                non_ws = equal_text.replace(' ', '').replace('\t', '')
                non_ws = non_ws.replace('\n', '').replace('\r', '')
                if len(non_ws) <= self._REALIGN_TRIVIAL_THRESHOLD:
                    merged = ('replace',
                              prev[1], nxt[2],
                              prev[3], nxt[4])
                    result[i - 1:i + 2] = [merged]
                    if i > 1:
                        i -= 1
                    continue
            i += 1

        # Pass 2: absorb short EQUAL blocks between two non-EQUAL blocks
        # (VS Code's removeVeryShortMatchingLinesBetweenDiffs, line 325).
        # If a short EQUAL (<= 4 non-whitespace chars) sits between two
        # non-EQUAL blocks and at least one of them is "large" (> 5
        # combined lines), join the two non-EQUAL blocks into one REPLACE,
        # absorbing the EQUAL. Iterate until no more changes (VS Code
        # repeats up to 10 times).
        changed = True
        iterations = 0
        while changed and iterations < 10:
            changed = False
            iterations += 1
            i = 1
            while i < len(result) - 1:
                prev = result[i - 1]
                cur = result[i]
                nxt = result[i + 1]
                if (cur[0] == 'equal' and
                        prev[0] != 'equal' and
                        nxt[0] != 'equal'):
                    _, ei1, ei2, ej1, ej2 = cur
                    equal_text = ''.join(self.a[ei1:ei2])
                    non_ws = equal_text.replace(' ', '').replace('\t', '')
                    non_ws = non_ws.replace('\n', '').replace('\r', '')
                    if len(non_ws) <= self._REALIGN_TRIVIAL_THRESHOLD:
                        prev_size = (prev[2] - prev[1]) + (prev[4] - prev[3])
                        nxt_size = (nxt[2] - nxt[1]) + (nxt[4] - nxt[3])
                        if prev_size > 5 or nxt_size > 5:
                            # Join prev and nxt into one REPLACE,
                            # absorbing the EQUAL
                            merged = ('replace',
                                      prev[1], nxt[2],
                                      prev[3], nxt[4])
                            result[i - 1:i + 2] = [merged]
                            changed = True
                            if i > 1:
                                i -= 1
                            continue
                i += 1

        """
        # Pass 3: absorb trivial lines from the edges of an EQUAL block
        # that sits between a REPLACE and the next non-EQUAL block.
        # In difflib opcodes, an EQUAL block can contain multiple lines,
        # some trivial (e.g. '}', '\n') and some meaningful (e.g.
        # 'self._save_state(state)'). VS Code's algorithm works on
        # changed regions only, so it treats the gap as one unit and
        # absorbs it if the total non-ws is <= 4. We need to be smarter:
        # split the EQUAL block and absorb only the trivial prefix/suffix
        # lines, keeping the meaningful middle as EQUAL.
        #
        # Example: EQUAL = ['}', 'self._save_state(state)']
        #   non_ws = '}self._save_state(state)' = 25 chars (> 4, not trivial)
        #   But the first line '}' is trivial (1 non-ws char).
        #   We split into: absorb '}' into prev REPLACE, keep
        #   'self._save_state(state)' as EQUAL.
        i = 1
        while i < len(result):
            prev = result[i - 1]
            cur = result[i]
            if cur[0] == 'equal' and prev[0] == 'replace':
                _, ei1, ei2, ej1, ej2 = cur
                prev_size = (prev[2] - prev[1]) + (prev[4] - prev[3])
                if prev_size <= 5:
                    i += 1
                    continue
                # Check if the first line of the EQUAL block is trivial
                if ei2 - ei1 == 0:
                    i += 1
                    continue
                first_line = self.a[ei1]
                first_non_ws = first_line.replace(' ', '').replace('\t', '')
                first_non_ws = first_non_ws.replace('\n', '').replace('\r', '')
                if len(first_non_ws) == 0:
                    # Only absorb truly trivial lines (empty or
                    # whitespace-only). Don't absorb short words like
                    # 'end' (3 non-ws chars) -- those are meaningful
                    # matches that should stay as EQUAL.
                    merged = ('replace',
                              prev[1], ei1 + 1,
                              prev[3], ej1 + 1)
                    remaining = ('equal', ei1 + 1, ei2, ej1 + 1, ej2)
                    result[i - 1:i + 1] = [merged, remaining]
                    # Don't advance i -- the remaining EQUAL might have
                    # more trivial lines to absorb
                    continue
            i += 1

        """
        return result

    def compare(self):
        """Generator that yields diff events for side-by-side display.

        Runs the selected Python diff algorithm, applies _realign_opcodes
        to fix LCS tie-breaking issues, then walks the opcodes and yields
        events (A_LINE_DEL, B_LINE_ADD, A_GAP, B_GAP, ALIGN, A_SYMBOL_DEL,
        etc.) that __init__.py consumes to paint the compare view.

        Also populates self.diffmap with [i1, i2, j1, j2] for each
        non-equal opcode, used by jump()/copy()/select_current().
        """
        # Benchmark: when _BENCHMARK is True, measure the total time from
        # when the generator starts executing until it is fully consumed
        # (or closed).
        _bm_start = time.perf_counter() if _BENCHMARK else None

        Profiler.start('compare:total')

        self.diffmap = []
        Profiler.start('compare:algorithm')
        if self.diff_algorithm == 'hybrid':
            diff = HybridSequenceMatcher(None, self.a, self.b)
        elif self.diff_algorithm == 'myers':
            diff = MyersSequenceMatcher(None, self.a, self.b)
        elif self.diff_algorithm == 'vscode':
            diff = VSCodeSequenceMatcher(None, self.a, self.b)
        elif self.diff_algorithm == 'patience':
            diff = PatienceSequenceMatcher(None, self.a, self.b)
        else:
            # Default: difflib stdlib SequenceMatcher
            diff = DefaultSequenceMatcher(None, self.a, self.b, autojunk=self.autojunk)

        opcodes = diff.get_opcodes()
        Profiler.stop('compare:algorithm')

        # _realign_opcodes fixes LCS tie-breaking issues where Myers
        # matches trivial lines (empty, whitespace) instead of meaningful
        # ones. This is needed for Python Myers/difflib (which produce
        # INSERT+EQUAL(trivial)+DELETE patterns).
        Profiler.start('compare:realign_opcodes')
        opcodes = self._realign_opcodes(opcodes)
        Profiler.stop('compare:realign_opcodes')

        Profiler.start('compare:event_generation')
        for tag, i1, i2, j1, j2 in opcodes:
            if tag != 'equal':
                self.diffmap.append([i1, i2, j1, j2])
            if tag == 'equal':
                # Yield ALIGN for each matched pair so the wrapper can add
                # compensating gaps when wrap is on.
                for k in range(i2 - i1):
                    yield (ALIGN, i1 + k, j1 + k)
            elif tag == 'delete':
                # Lines i1..i2-1 in A are deleted; gap in B after line j1-1.
                # (j1 == j2 for 'delete'.) The gap compensates for A lines
                # [i1, i2).
                yield (B_GAP, j1, i1, i2)
                for y in range(i1, i2):
                    yield (A_LINE_DEL, y)
            elif tag == 'insert':
                # Lines j1..j2-1 in B are inserted; gap in A after line i1-1.
                # (i1 == i2 for 'insert'.) The gap compensates for B lines
                # [j1, j2).
                yield (A_GAP, i1, j1, j2)
                for y in range(j1, j2):
                    yield (B_LINE_ADD, y)
            elif tag == 'replace':
                if self.withdetail:
                    yield from self._replace_block(self.a, i1, i2,
                                                   self.b, j1, j2)
                else:
                    yield from self._plain_replace_simple(self.a, i1, i2,
                                                          self.b, j1, j2)
        Profiler.stop('compare:event_generation')

        Profiler.stop('compare:total')

        if _bm_start is not None:
            _bm_elapsed = time.perf_counter() - _bm_start
            print('Differ: compare took {:.1f}ms '
                  '(algo={}, a={}lines, b={}lines, '
                  'opcodes={}diffs, events_generated)'.format(
                      _bm_elapsed * 1000,
                      self.diff_algorithm,
                      len(self.a), len(self.b),
                      len(self.diffmap)))

    def unidiff(self, a, b, f1, f2, n):
        """Produce a unified diff string from two line sequences.

        Uses difflib.unified_diff when autojunk=True (the default), or
        _unified_diff when autojunk=False (to forward the flag to
        SequenceMatcher). See the comment on _unified_diff for why.
        """
        # autojunk=True matches the stdlib default -> call difflib.unified_diff
        # directly (no reimplementation on the hot path). Only when the user
        # opts out (autojunk=False) do we need _unified_diff to forward the
        # kwarg, since difflib.unified_diff does not expose it -- see
        # https://github.com/python/cpython/issues/118150 and the comment on
        # _unified_diff above.
        if self.autojunk:
            diff = unified_diff(a, b, f1, f2, n=n)
        else:
            diff = _unified_diff(a, b, f1, f2, n=n, autojunk=False)
        return ''.join(diff)

    def _replace_block(self, a, alo, ahi, b, blo, bhi):
        """Process a 'replace' opcode: a[alo:ahi] is replaced by b[blo:bhi].

        Aligns lines within a REPLACE block for visual display. Two paths:

        Fast path (da == db): positional pairing — pair 1st line of A with
        1st line of B, 2nd with 2nd, etc. For each pair, if lines are
        identical yield ALIGN; otherwise run char_diff and yield the
        char-level changes. This is the common case and avoids the O(N*M)
        prefix/suffix search.

        Slow path (da != db): _find_best_pairs — find the best-matching
        line pair by (1) exact unique match (longest wins), then (2) common
        prefix/suffix length scoring. Recurse on the parts before and after
        the best pair. This aligns similar-but-not-equal lines (e.g.
        'def foo(self):' with 'def bar(self):') so char_diff highlights
        only the differing characters.

        Note: the line-level diff algorithm (Myers/difflib) already
        found ALL exactly-equal lines and emitted them as separate EQUAL
        opcodes. So this function does NOT re-run Myers — the exact-match
        search in _find_best_pairs only finds matches that Myers missed
        (rare, can happen with suboptimal LCS tie-breaking). The main
        value of _find_best_pairs is the prefix/suffix scoring for
        similar-but-not-equal lines, which Myers does not do.
        """
        Profiler.start('replace_block:total')
        da, db = ahi - alo, bhi - blo
        if da == 0 and db == 0:
            Profiler.stop('replace_block:total')
            return
        if da == 0:
            Profiler.start('replace_block:insert_only')
            yield (A_GAP, alo, blo, bhi)
            for y in range(blo, bhi):
                yield (B_LINE_ADD, y)
            Profiler.stop('replace_block:insert_only')
            Profiler.stop('replace_block:total')
            return
        if db == 0:
            Profiler.start('replace_block:delete_only')
            yield (B_GAP, blo, alo, ahi)
            for y in range(alo, ahi):
                yield (A_LINE_DEL, y)
            Profiler.stop('replace_block:delete_only')
            Profiler.stop('replace_block:total')
            return

        # Fast path: when both sides have the same number of lines, use
        # positional pairing directly. This avoids the overhead of running
        # a line-level Myers diff + _realign_opcodes on every REPLACE
        # block. For large files (e.g. 33k-line HTML), most REPLACE blocks
        # have da == db, so this keeps performance at ~30s instead of ~80s.
        # The prefix/suffix search (_find_best_pairs) is only needed when
        # da != db, because that's when positional pairing might misalign
        # similar lines.
        if da == db:
            Profiler.start('replace_block:positional_pair')
            common = min(da, db)
            for k in range(common):
                ai, bj = alo + k, blo + k
                if a[ai] == b[bj]:
                    yield (ALIGN, ai, bj)
                else:
                    Profiler.start('char_diff:per_line')
                    ops = self._char_diff(a[ai], b[bj])
                    Profiler.stop('char_diff:per_line')
                    yield from self._char_diff_pair(ai, bj, ops)
                    yield (ALIGN, ai, bj)
            Profiler.stop('replace_block:positional_pair')
            Profiler.stop('replace_block:total')
            return

        # Different line counts (da != db): use _find_best_pairs which
        # finds the best-matching pair by exact unique match (longest
        # wins) or prefix/suffix length scoring, then recurses.
        yield from self._find_best_pairs(a, alo, ahi, b, blo, bhi)
        Profiler.stop('replace_block:total')

    def _find_best_pairs(self, a, alo, ahi, b, blo, bhi):
        """Find the best line alignment within a sub-REPLACE block.

        Finds the best-matching line pair using two strategies:
        1. Exact unique match: build a dict of unique lines in a[alo:ahi],
           scan b[blo:bhi] for matches, pick the LONGEST match as anchor.
           O(N+M) via Counter-based uniqueness check.
        2. If no exact match: prefix/suffix length scoring. For each (i,j)
           pair, compute common prefix length + common suffix length.
           Score = prefix*100 + suffix. Pick the highest-scoring pair.
           O(N*M) per block but each comparison is O(line_length).

        After finding the best pair, do char_diff on it, then recurse on
        the parts before and after. A minimum prefix threshold (>= 3 chars)
        prevents pairing completely unrelated lines.

        The line-level diff (Myers/difflib) already found all exactly-equal
        lines, so the exact-match search here mainly catches rare cases
        where suboptimal LCS tie-breaking produced a suboptimal REPLACE.
        The main value is the prefix/suffix scoring for similar-but-not-
        equal lines, which the line-level diff does not do.
        """
        da, db = ahi - alo, bhi - blo
        if da == 0:
            if db > 0:
                yield (A_GAP, alo, blo, bhi)
                for y in range(blo, bhi):
                    yield (B_LINE_ADD, y)
            return
        if db == 0:
            if da > 0:
                yield (B_GAP, blo, alo, ahi)
                for y in range(alo, ahi):
                    yield (A_LINE_DEL, y)
            return

        # Find the best-matching pair by char-level similarity.
        # Strategy: find ALL unique exact matches first, then pick the
        # LONGEST one as the anchor (longer lines are more specific and
        # better anchors — 'def on_change_slow(self, ed_self):' is a
        # better anchor than 'else:'). If no unique exact match, fall
        # back to prefix/suffix length scoring.
        best_score = -1
        best_prefix = 0
        best_i, best_j = alo, blo
        max_prefix_any = 0

        # First pass: find all unique exact matches and pick the longest.
        # Use a dict-based approach for O(N+M) instead of O(N*M):
        # build a map of unique lines in a[alo:ahi], then scan b[blo:bhi].
        Profiler.start('find_best_pairs:exact_match_search')
        sub_a_counts = Counter(a[alo:ahi])
        sub_b_counts = Counter(b[blo:bhi])
        best_exact_len = 0
        best_exact_i, best_exact_j = -1, -1
        # Build index of unique lines in sub_a
        sub_a_unique = {}
        for i in range(alo, ahi):
            line = a[i]
            if sub_a_counts.get(line, 0) == 1 and line not in sub_a_unique:
                non_ws = line.replace(' ', '').replace('\t', '')
                non_ws = non_ws.replace('\n', '').replace('\r', '')
                if len(non_ws) >= 3:
                    sub_a_unique[line] = i
        # Scan sub_b for matches
        for j in range(blo, bhi):
            line = b[j]
            if sub_b_counts.get(line, 0) == 1 and line in sub_a_unique:
                if len(line) > best_exact_len:
                    best_exact_len = len(line)
                    best_exact_i = sub_a_unique[line]
                    best_exact_j = j
        Profiler.stop('find_best_pairs:exact_match_search')

        if best_exact_i >= 0:
            # Use the longest unique exact match as anchor
            best_i, best_j = best_exact_i, best_exact_j
            best_score = 1000000
            best_prefix = 1000000
            max_prefix_any = 1000000
        else:
            # No unique exact match — use prefix/suffix length scoring.
            # No size guard: the prefix/suffix metric is O(line_length)
            # per pair, and the exact-match pass above already handled
            # unique lines. For large blocks without unique matches,
            # this is O(N*M) but each pair comparison is fast (just
            # scanning from both ends of the lines).
            #
            # THIS IS THE LIKELY BOTTLENECK for large files with many
            # REPLACE blocks where da != db. Each pair comparison scans
            # the prefix AND suffix character-by-character. For a block
            # with 1000 lines on each side, that's 1M comparisons, each
            # potentially scanning hundreds of chars. And this is
            # RECURSIVE — after finding the best pair, it recurses on
            # both sides, so the total work can be O(N*M*D) where D is
            # the number of differences.
            Profiler.start('find_best_pairs:prefix_suffix_search')
            for j in range(blo, bhi):
                bj_line = b[j]
                for i in range(alo, ahi):
                    ai_line = a[i]
                    if ai_line == bj_line:
                        continue  # skip exact matches (already checked)
                    # Common prefix length
                    min_len = min(len(ai_line), len(bj_line))
                    prefix = 0
                    while prefix < min_len and ai_line[prefix] == bj_line[prefix]:
                        prefix += 1
                    if prefix > max_prefix_any:
                        max_prefix_any = prefix
                    # Common suffix length (only if there's a mismatch)
                    if prefix < min_len:
                        suffix = 0
                        while (suffix < min_len - prefix and
                               ai_line[len(ai_line)-1-suffix] == bj_line[len(bj_line)-1-suffix]):
                            suffix += 1
                    else:
                        suffix = min(len(ai_line), len(bj_line)) - prefix
                    # Score: prefix is most important (lines starting the same
                    # are likely the "same" line), then suffix, then total.
                    # Scale prefix heavily to prefer long-prefix matches.
                    score = prefix * 100 + suffix
                    if score > best_score:
                        best_score, best_i, best_j = score, i, j
                        best_prefix = prefix
            Profiler.stop('find_best_pairs:prefix_suffix_search')

        # Minimum similarity threshold: only pair lines if the BEST pair
        # shares a meaningful common prefix (>= 3 chars). This matches VS
        # Code's behavior — VS Code does NOT pair completely different
        # lines (like 'dsds' vs '\n' or 'ff' vs 'ss'); it shows them
        # as separate delete + add. Without this threshold, the diff
        # looks confusing because unrelated lines get marked as "changed"
        # (orange/yellow) instead of separate red+green.
        # The threshold of 3 means: '"""Register' (9 common chars) pairs,
        # but 'dsds' vs '\n' (0 common chars) does NOT pair.
        # Exception: if the block is 1xN or Nx1 (one side has a single
        # line), pair positionally ONLY if the opposing first line is
        # non-trivial (has >= 3 non-whitespace chars). This matches VS
        # Code: it pairs 'dsds' with 'ggg' (both non-trivial) but shows
        # 'dsds' as deleted when the opposing side starts with '\n'.
        if best_prefix < 3 and best_prefix != 1000000:
            if da == 1 or db == 1:
                # Single-line block: check if the opposing first line
                # is non-trivial (>= 3 non-ws chars)
                if da == 1 and db >= 1:
                    opp_line = b[blo]
                elif db == 1 and da >= 1:
                    opp_line = a[alo]
                else:
                    opp_line = ''
                opp_non_ws = opp_line.replace(' ', '').replace('\t', '')
                opp_non_ws = opp_non_ws.replace('\n', '').replace('\r', '')
                if len(opp_non_ws) >= 3:
                    # Non-trivial opposing line: pair positionally
                    pass
                else:
                    # Trivial opposing line: show as delete + add
                    for i in range(alo, ahi):
                        yield (B_GAP, blo, i, i + 1)
                        yield (A_LINE_DEL, i)
                    for j in range(blo, bhi):
                        yield (A_GAP, ahi, j, j + 1)
                        yield (B_LINE_ADD, j)
                    return
            else:
                # Multi-line block with no good match: show all as
                # separate delete + add
                for i in range(alo, ahi):
                    yield (B_GAP, blo, i, i + 1)
                    yield (A_LINE_DEL, i)
                for j in range(blo, bhi):
                    yield (A_GAP, ahi, j, j + 1)
                    yield (B_LINE_ADD, j)
                return

        # Recurse on the part before the best pair
        yield from self._find_best_pairs(a, alo, best_i, b, blo, best_j)

        # Process the best pair itself
        a_line, b_line = a[best_i], b[best_j]
        if a_line == b_line:
            yield (ALIGN, best_i, best_j)
        else:
            Profiler.start('char_diff:per_line')
            ops = self._char_diff(a_line, b_line)
            Profiler.stop('char_diff:per_line')
            yield from self._char_diff_pair(best_i, best_j, ops)
            yield (ALIGN, best_i, best_j)

        # Recurse on the part after the best pair
        yield from self._find_best_pairs(a, best_i + 1, ahi,
                                          b, best_j + 1, bhi)

    def _char_diff_pair(self, ai, bj, ops):
        """Yield character-level diff events for a single line pair,
        given the char-level opcodes from char_diff().

        The line is marked as A_LINE_CHANGE / B_LINE_CHANGE (yellow/red/
        green decor based on whether char-level deletes or inserts were
        found), and the specific character ranges that differ are emitted
        as A_SYMBOL_DEL / B_SYMBOL_ADD events so the wrapper can highlight
        them inline.
        """
        deca, decb = 0, 0
        for tag, a_start, a_end, b_start, b_end in ops:
            la, lb = a_end - a_start, b_end - b_start
            if tag == 'delete':
                deca += 1
                yield (A_SYMBOL_DEL, ai, a_start, la)
            elif tag == 'insert':
                decb += 1
                yield (B_SYMBOL_ADD, bj, b_start, lb)
            elif tag == 'replace':
                deca += 1
                decb += 1
                yield (A_SYMBOL_DEL, ai, a_start, la)
                yield (B_SYMBOL_ADD, bj, b_start, lb)
        yield (A_LINE_CHANGE, ai)
        yield (B_LINE_CHANGE, bj)
        yield (A_DECOR_YELLOW, ai) if deca == 0 else (A_DECOR_RED, ai)
        yield (B_DECOR_YELLOW, bj) if decb == 0 else (B_DECOR_GREEN, bj)

    def _plain_replace_simple(self, a, alo, ahi, b, blo, bhi):
        """Non-detailed replace (withdetail=False). Pairs lines by position
        and marks all as A_LINE_CHANGE/B_LINE_CHANGE with yellow decor
        (no char-level highlights)."""
        da, db = ahi - alo, bhi - blo
        common = min(da, db)
        for k in range(common):
            yield (ALIGN, alo + k, blo + k)
        if da > db:
            yield (B_GAP, bhi, alo + common, ahi)
        elif db > da:
            yield (A_GAP, ahi, blo + common, bhi)
        for y in range(alo, ahi):
            yield (A_LINE_CHANGE, y)
            yield (A_DECOR_YELLOW, y)
        for y in range(blo, bhi):
            yield (B_LINE_CHANGE, y)
            yield (B_DECOR_YELLOW, y)
