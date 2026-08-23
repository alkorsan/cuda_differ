"""Differ module for native algorithms (native_histogram, native_myers).

This file is self-contained: it contains everything needed to run the
native diff engine implemented in Free Pascal and exposed via
cudatext.diff_proc. It does NOT depend on differ_python.py.

The native engine is 10-30x faster than the pure-Python matchers on
large files. For Python-only algorithms (hybrid, myers, vscode, patience,
difflib), use differ_python.py instead.

Code is intentionally duplicated from differ_python.py to allow
independent evolution of the native and Python codepaths. As more
diff logic moves into the Pascal native engine, this file will shrink
to a thin wrapper around cudatext.diff_proc if God wills.

Alignment modes (self.beautify_alignment):
  True  = 'beautified' alignment: similar lines inside a changed block are
          re-paired by similarity (VS Code-like). Uses _find_best_pairs.
  False = WinMerge-faithful: the engine's hunks are rendered exactly the
          way WinMerge / diffutils side-by-side (sdiff) output does —
          positional top-down pairing, leftovers as plain add/delete.
          Nothing is re-paired, re-ordered or split. Default.
"""

import time
from difflib import SequenceMatcher as DefaultSequenceMatcher, unified_diff
from .char_diff import char_diff
from .profiling import Profiler
from collections import Counter
import cudatext as _ct
from cudax_lib import get_translation
_ = get_translation(__file__)  # I18N

# Import cudatext and detect whether the native diff_proc API is available.
try:
    _HAS_NATIVE_DIFF = hasattr(_ct, 'diff_proc')
except ImportError:
    _HAS_NATIVE_DIFF = False


class CudaDiffNativeMatcher:
    """Difflib-compatible wrapper around cudatext.diff_proc().

    Calls the native Free Pascal diff engine exposed at cudatext.diff_proc(). The native engine is
    dramatically faster than any of the pure-Python matchers on large
    files (10-30x speedup is typical).

    This class exposes the same minimal interface Differ.compare() uses
    from the other matchers: get_opcodes() returning a list of
    (tag, i1, i2, j1, j2) tuples with tag in
    {'equal', 'delete', 'insert', 'replace'}.

    The native API takes two LF-joined strings and returns exactly that
    opcode format, so this wrapper is essentially a type adapter:
    list-of-lines -> LF-joined-string -> native call -> opcodes.

    NOTE: cudatext.diff_proc expects strings with line terminators
    attached (keepends=True). The plugin already stores sequences that
    way (see __init__.py: splitlines(True)), so we just join with ''.
    """

    # Algorithm IDs — accessed directly from cudatext (_ct) at the call
    # sites. These class attributes are kept for API compatibility with
    # code that references CudaDiffNativeMatcher._ALGO_MYERS etc.
    _ALGO_MYERS = 0  # DIFF_ALGO_MYERS
    _ALGO_HISTOGRAM = 1  # DIFF_ALGO_HISTOGRAM

    def __init__(self, isjunk=None, a='', b='', algo=1):
        """Create a native diff matcher.

        Args:
            isjunk: ignored (kept for difflib API compatibility; the
                native engine does not support junk heuristics).
            a, b: sequences of lines (list of str, each with its line
                terminator attached -- i.e. keepends=True).
            algo: CudaDiffNativeMatcher._ALGO_MYERS (0) — WinMerge's GNU
                  diffutils Myers with Eggert heuristic. Faster on large /
                  different files.
                  CudaDiffNativeMatcher._ALGO_HISTOGRAM (1, default) — JGit
                  HistogramDiff with MyersDiff as internal fallback for
                  sub-regions. Patience-style anchoring on unique lines,
                  more human-readable for normal files.
        """
        self.a = a
        self.b = b
        self._algo = algo
        self.opcodes = None

    def get_matching_blocks(self):
        """Return list of (i, j, n) matching blocks, difflib-style.

        Derived from get_opcodes() -- equal runs become matching blocks,
        with a sentinel (len(a), len(b), 0) at the end.
        """
        blocks = []
        for tag, i1, i2, j1, j2 in self.get_opcodes():
            if tag == 'equal':
                blocks.append((i1, j1, i2 - i1))
        blocks.append((len(self.a), len(self.b), 0))
        return blocks

    def get_opcodes(self):
        """Return difflib-compatible opcodes by calling cudatext.diff_proc.

        Returns:
            list of (tag, i1, i2, j1, j2) tuples where tag is a lowercase
            string. Identical in format to difflib.SequenceMatcher.get_opcodes().
        """
        if self.opcodes is not None:
            return self.opcodes
        Profiler.start('native:join_strings')
        text_a = ''.join(self.a)
        text_b = ''.join(self.b)
        Profiler.stop('native:join_strings')
        Profiler.start('native:diff_proc_call')
        result = _ct.diff_proc(
            _ct.DIF_TEXTS,
            text_a,
            text_b,
            self._algo,
            0,                   # flags: DIFF_IGN_NONE
        )
        Profiler.stop('native:diff_proc_call')
        # The native API always returns a valid opcode list.
        if result is None:
            # Defensive: should never happen, but fall back to a single
            # REPLACE covering everything so the caller's opcode-walking
            # loop still produces sensible output (everything painted as changed) instead of crashing.
            result = [('replace', 0, len(self.a), 0, len(self.b))]
        self.opcodes = result
        return result


# Internal benchmark toggle. When set to True, Differ.compare() prints the
# total time elapsed from when the generator starts executing until it is
# fully consumed (or closed).
_BENCHMARK = True


# --- Event constants ---
# These are the event IDs yielded by Differ.compare(). __init__.py
# consumes them to paint the side-by-side compare view.
# They are duplicated in differ_python.py — both modules define the
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
    """Differ for native algorithms (native_histogram, native_myers).

    Only handles native_histogram and native_myers algorithms. For Python
    algorithms (hybrid, myers, vscode, patience, difflib), use
    differ_python.Differ instead.

    This class is self-contained: it does not import from differ_python.
    Code that overlaps with differ_python.Differ (event constants,
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

        Sets default options: native_histogram algorithm, autojunk on,
        detailed compare on. The algorithm can be changed later via
        self.diff_algorithm before calling compare().
        """
        self.withdetail = True
        # Algorithm key: 'native_histogram' (default) or 'native_myers'.
        # Only native algorithms are supported by this Differ.
        self.diff_algorithm = 'native_histogram'
        self.autojunk = True
        self.ratio = 0.75  # kept for API compat with __init__.py; unused
        # alignment mode toggle.
        #   True  = OLD 'beautified' alignment (_find_best_pairs re-pairs
        #           similar lines inside unequal-count replace blocks).
        #   False = WinMerge-faithful positional rendering (default).
        # __init__.py overrides this from the 'differ.beautify_alignment'
        # option — see Command._create_differ.
        self.beautify_alignment = False
        self.set_seqs(a, b)
        self.diffmap = []

    def set_seqs(self, a, b):
        """Set the two line sequences to compare."""
        self.a = a
        self.b = b

    def _char_diff(self, line_a, line_b):
        """Compute char-level diff between two single-line strings.

        Uses the native cudatext.diff_proc(DIF_CHARS) API
        Falls back to the Python char_diff only if the native
        API is unavailable.

        Returns: list of (tag, a_start, a_end, b_start, b_end) tuples
        where tag is 'equal'/'delete'/'insert'/'replace' and offsets are
        character positions into line_a / line_b. Same format as
        difflib.SequenceMatcher.get_opcodes() operating on characters.
        """
        # Early bail-out for very long lines: skip the native call
        # entirely and return a single REPLACE. The Pascal DoDiffChars
        # would do the same inside diff_proc(DIF_CHARS) (its Tokenize
        # pre-allocates N tokens where N = byte length, which for
        # 100KB+ lines causes massive heap allocation that leads to
        # EAccessViolation when repeated across many line pairs).
        # Checking here avoids the Python→C→Pascal→C→Python round-trip
        # and the heap churn entirely.
        if len(line_a) > 100000 or len(line_b) > 100000:
            return [('replace', 0, len(line_a), 0, len(line_b))]

        if _HAS_NATIVE_DIFF:
            Profiler.start('char_diff:native_call')
            try:
                result = _ct.diff_proc(
                    _ct.DIF_CHARS,
                    line_a,
                    line_b,
                    0,                   # algo: unused for DIF_CHARS
                    0,                   # flags: DIFF_IGN_NONE
                )
            finally:
                _ct.msg_status(_('Differ: Native char_diff used'))
                Profiler.stop('char_diff:native_call')
            if result is None:
                # Defensive: should never happen — fall back to a single
                # REPLACE covering everything.
                result = [('replace', 0, len(line_a), 0, len(line_b))]
            return result
        else:
            # Native API not available — fall back to Python char_diff.
            _ct.msg_status(_('Differ: Native API not available — fall back to Python char_diff'))
            Profiler.start('char_diff:python_call')
            try:
                return char_diff(line_a, line_b)
            finally:
                Profiler.stop('char_diff:python_call')

    def compare(self):
        """Generator that yields diff events for side-by-side display.

        Pure translation of the engine's opcodes into paint events —
        nothing is added, removed or re-paired here.
        
        The alignment mode (beautify_alignment) only affects how
        unequal-count REPLACE blocks are laid out — see _replace_block.
        
        Runs the native diff algorithm,
        then walks the opcodes and yields events (A_LINE_DEL, B_LINE_ADD,
        A_GAP, B_GAP, ALIGN, A_SYMBOL_DEL, etc.) that __init__.py
        consumes to paint the compare view.

        Also populates self.diffmap with [i1, i2, j1, j2] for each
        non-equal opcode, used by jump()/copy()/select_current().

        NOTE: _realign_opcodes is NOT called here — native algorithms
        don't produce the INSERT+EQUAL(trivial)+DELETE patterns that
        _realign_opcodes fixes (that's a Python Myers/difflib issue). TODO: this is not true, correct it
        """
        # Benchmark: when _BENCHMARK is True, measure the total time from
        # when the generator starts executing until it is fully consumed
        # (or closed).
        _bm_start = time.perf_counter() if _BENCHMARK else None

        Profiler.start('compare:total')

        self.diffmap = []
        Profiler.start('compare:algorithm')
        if self.diff_algorithm == 'native_myers':
            diff = CudaDiffNativeMatcher(
                None, self.a, self.b, algo=CudaDiffNativeMatcher._ALGO_MYERS)
        else:
            # Default to histogram (covers 'native_histogram' and any
            # unexpected value — histogram is the recommended default).
            diff = CudaDiffNativeMatcher(
                None, self.a, self.b, algo=CudaDiffNativeMatcher._ALGO_HISTOGRAM)

        # get_opcodes() calls diff_proc (the native engine) — this is
        # where the actual algorithm runs.
        opcodes = diff.get_opcodes()
        Profiler.stop('compare:algorithm')

        # No _realign_opcodes call — native algorithms don't need it.
        # See docstring for details.

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
                  '(algo={}, a={}lines, b={}lines, opcodes={}diffs)'.format(
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

    def _positional_pairs(self, a, alo, b, blo, count):
        """Pair the k-th A line with the k-th B line, top-down, for
        `count` pairs. For identical lines yield ALIGN only; for
        different lines run the native char diff and yield its paint
        events, then ALIGN. No heuristics — this is the sdiff/WinMerge
        alignment, also used by the OLD beautify mode when line counts
        are equal (the old code did exactly this in that case).
        """
        for k in range(count):
            ai, bj = alo + k, blo + k
            if a[ai] == b[bj]:
                yield (ALIGN, ai, bj)
            else:
                Profiler.start('char_diff:per_line')
                ops = self._char_diff(a[ai], b[bj])
                Profiler.stop('char_diff:per_line')
                yield from self._char_diff_pair(ai, bj, ops)
                yield (ALIGN, ai, bj)

    def _replace_block(self, a, alo, ahi, b, blo, bhi):
        """Process a 'replace' opcode: a[alo:ahi] is replaced by b[blo:bhi].
        
        Aligns lines within a REPLACE block for visual display.

        Two rendering modes, selected by self.beautify_alignment:

        beautify_alignment = True (OLD, 'beautified' alignment)
            Unequal line counts use _find_best_pairs(): anchor on the
            longest unique exact match or the best prefix/suffix-similar
            pair, char-diff it, recurse on both sides. Lines with < 3
            chars of similarity are shown as separate delete+add.
            VS Code-like; re-arranges the engine's output.
    
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
    
            Note: the line-level diff algorithm (cudadiffmyers.pas for
            DIFF_ALGO_MYERS, cudadiffhistogram.pas for DIFF_ALGO_HISTOGRAM)
            already found ALL exactly-equal lines and emitted them as
            separate EQUAL opcodes. So this function does NOT re-run a
            line-level diff — the exact-match search in _find_best_pairs
            only finds matches that the engine missed (rare; can happen
            with the Eggert TOO_EXPENSIVE heuristic under DIFF_ALGO_MYERS,
            or with HistogramDiff's max_chain_length fallback). The main
            value of _find_best_pairs is the prefix/suffix scoring for
            similar-but-not-equal lines, which neither engine does.
            
        beautify_alignment = False (NEW, WinMerge-faithful, default)
            Render exactly the way WinMerge / GNU diffutils side-by-side
            (sdiff) output does: pair the first min(da, db) lines
            top-down by position (char-diff each pair via the native
            engine), and show leftover lines on the longer side as plain
            added/deleted lines against a gap at the bottom of the
            shorter side. Nothing is re-paired or re-ordered.

        Equal line counts (da == db) are positional in BOTH modes, so the modes
        diverge only in the da != db branch below.
        """
        Profiler.start('replace_block:total')
        da, db = ahi - alo, bhi - blo

        # Defensive only — a 'replace' opcode from the engine always has
        # both sides non-empty (pure insert/delete arrive as their own
        # opcodes in compare()). These branches contain no heuristics;
        # they just render a degenerate opcode faithfully.
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
            # Shared fast path — positional pairing (both modes).
            Profiler.start('replace_block:positional_pair')
            yield from self._positional_pairs(a, alo, b, blo, da)
            Profiler.stop('replace_block:positional_pair')
            Profiler.stop('replace_block:total')
            return

        # ---- da != db: the two modes diverge here ----
        if self.beautify_alignment:
            # OLD: anchor + prefix/suffix scoring + threshold + staggering.
            # Different line counts (da != db): use _find_best_pairs which
            # finds the best-matching pair by exact unique match (longest
            # wins) or prefix/suffix length scoring, then recurses.
            yield from self._find_best_pairs(a, alo, ahi, b, blo, bhi)
        else:
            # NEW (WinMerge): positional top-down pairing; leftovers on
            # the longer side are plain added/deleted lines against a gap
            # at the bottom of the shorter side's block (same convention
            # as _plain_replace_simple).
            Profiler.start('replace_block:positional_pair')
            common = min(da, db)
            yield from self._positional_pairs(a, alo, b, blo, common)
            if da > common:
                yield (B_GAP, bhi, alo + common, ahi)
                for y in range(alo + common, ahi):
                    yield (A_LINE_DEL, y)
            elif db > common:
                yield (A_GAP, ahi, blo + common, bhi)
                for y in range(blo + common, bhi):
                    yield (B_LINE_ADD, y)
            Profiler.stop('replace_block:positional_pair')

        Profiler.stop('replace_block:total')

    def _find_best_pairs(self, a, alo, ahi, b, blo, bhi):
        """Find the best line alignment within a sub-REPLACE block.

        ONLY USED WHEN self.beautify_alignment is True (the OLD
        'beautified' alignment mode).

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

        The line-level diff (cudadiffmyers.pas for DIFF_ALGO_MYERS,
        cudadiffhistogram.pas for DIFF_ALGO_HISTOGRAM) already found all exactly-equal
        lines, so the exact-match search here mainly catches rare cases
        where the TOO_EXPENSIVE heuristic produced a suboptimal REPLACE.
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
        given the char-level opcodes from native engine char_diff().
        Shared by BOTH alignment modes
        Pure glue — no decisions made here.

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
        and marks all as changed (A_LINE_CHANGE/B_LINE_CHANGE) with yellow decor
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
