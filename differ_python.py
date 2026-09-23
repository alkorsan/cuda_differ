"""Differ module for Python algorithms (hybrid, myers, vscode, patience, difflib).

This file is self-contained: it contains everything needed to run the
pure-Python diff algorithms. It does NOT depend on differ_native.py.

For native algorithms (native_histogram, native_myers), use
differ_native.py instead — the native engine is 10-30x faster.

Code is intentionally duplicated from differ_native.py to allow
independent evolution of the native and Python codepaths.

Alignment modes (self.beautify_alignment):
  True  = 'beautified' alignment: similar lines inside a changed block are
          re-paired by similarity (VS Code-like). Uses
          _find_best_pairs_events.
  False = WinMerge-faithful: the engine's hunks are rendered exactly the
          way WinMerge / diffutils side-by-side (sdiff) output does —
          positional top-down pairing, leftovers as plain add/delete.
          Nothing is re-paired, re-ordered or split. Default.
"""

import time
from difflib import SequenceMatcher as DefaultSequenceMatcher
from .py_algo.myers_onp_diff import MyersSequenceMatcher, InlineMyersSequenceMatcher
from .py_algo.patience_diff.patiencediff import PatienceSequenceMatcher
from .py_algo.vscode_diff import VSCodeSequenceMatcher
from .py_algo.char_diff import char_diff
from .profiling import Profiler
from collections import Counter
import cudatext as _ct
from cudax_lib import get_translation
_ = get_translation(__file__)  # I18N

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
# Ignored (suppressed) difference events — emitted ONLY by the native
# path (differ_native) for 'ignore' opcodes under DIFF_IGN_BLANK_LINES.
# This module NEVER yields them (pure-Python engines compare strictly,
# so they cannot suppress hunks), but the constants must exist here too:
# __init__.py's paint loop references them off whichever differ module
# produced the event stream.
A_LINE_IGN = '-i'   # ignored line in file a: (id, y)
B_LINE_IGN = '+i'   # ignored line in file b: (id, y)
A_GAP_IGN  = '-^i'  # ignored gap in file a: (id, y, start, end)
B_GAP_IGN  = '+^i'  # ignored gap in file b: (id, y, start, end)
# Alignment event: a pair of lines (one in A, one in B) that must be kept
# at the same visual Y position. Used by __init__.py to add compensating
# gaps when word-wrap is on and the two lines wrap to a different number
# of visual rows.
ALIGN = '='
# Composite CHANGED-PAIR event (mirrored from differ_native — see its
# comment there for the full rationale): one event per char-diffed pair,
#           return (id, a_line, b_line, deca, decb)
# replacing A_LINE_CHANGE + B_LINE_CHANGE + A_DECOR_* + B_DECOR_* and the
# pair's trailing ALIGN. deca/decb are the pair's char-opcode counts;
# deca > 0 / == 0 pick A_DECOR_RED's / A_DECOR_YELLOW's old colors, decb
# > 0 / == 0 pick B_DECOR_GREEN's / B_DECOR_YELLOW's. The consumer paints
# both lines and runs the pair's wrap-compensation from this one event.
PAIR_CHANGED = '*'


class Differ:
    """Differ for Python algorithms (hybrid, myers, vscode, patience, difflib).

    Handles all non-native algorithms. For native algorithms
    (native_histogram, native_myers), use differ_native.Differ instead.

    This class is self-contained: it does not import from differ_native.
    Code that overlaps with differ_native.Differ (event constants,
    _replace_block_chunks, _find_best_pairs_events,
    _char_diff_pair_events, etc.) is duplicated intentionally to allow
    independent evolution.

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
          * composite changed line pair (a_line, b_line, deca, decb)
              return (id, a_line, b_line, deca, decb)
              One event per char-diffed REPLACE pair; replaces the old
              A_LINE_CHANGE / B_LINE_CHANGE / A_DECOR_* / B_DECOR_*
              quartet and the pair's trailing ALIGN (see PAIR_CHANGED
              above). The non-detailed plain-replace path (withdetail=
              False) still emits the old quartet -- it has no pairing.
    """

    def __init__(self):
        """Initialize the Differ.

        Sets default options: hybrid algorithm, detailed compare on.
        The algorithm can be changed later via self.diff_algorithm
        before calling compare().

        The Differ holds NO line lists between compares — neither raw
        text nor line lists. a / b (the line lists) are passed directly
        to compare() by the caller (Command.refresh_compare), used as
        locals inside compare() to drive the pure-Python matcher +
        the painting split, and dropped when compare() returns.
        Between compares, the Differ holds only config (withdetail /
        diff_algorithm / beautify_alignment) and the diffmap (line-
        index tuples, small). The text itself stays in the editor
        tabs' Pascal-side buffers (a_ed / b_ed), which are the source
        of truth; the Python-side line-list copy is built fresh on
        each compare via split_lines_safe(a_ed.get_text_all()).
        """
        self.withdetail = True
        self.diff_algorithm = 'hybrid'
        self.beautify_alignment = False
        self.diffmap = []

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
        # Early bail-out for absurdly long lines: return a single REPLACE
        # covering both lines without running any engine. This mirrors the
        # same guard in differ_native.Differ._char_diff (where it protects
        # the native engine's heap from the Pascal Tokenize allocation).
        # Here the rationale is different: char_diff.py already guards
        # itself with WORD_DIFF_THRESHOLD (20480 words, WinMerge's limit)
        # and falls back to an O(N) prefix/suffix trim, but the tokenizer
        # is still O(N) per line and the word-level Myers can burn real
        # time on adversarial token counts just below the threshold — and
        # a 100k-char line painted as a handful of huge blobs is visually
        # useless anyway. Skipping it costs nothing.
        if len(line_a) > 100000 or len(line_b) > 100000:
            return [('replace', 0, len(line_a), 0, len(line_b))]

        # No profiling section here: the CALLER times this call with a
        # perf_counter pair and books one batched Profiler.mark() per
        # chunk of pairs (see _positional_pairs_events). The old
        # per-pair sections made the report lie on 1M-line files.
        return char_diff(line_a, line_b)

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

    def _realign_opcodes(self, a, opcodes):
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
                equal_text = ''.join(a[ei1:ei2])
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
                    equal_text = ''.join(a[ei1:ei2])
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
                first_line = a[ei1]
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

    def compare(self, a, b):
        """Generator that yields diff events for side-by-side display.

        Runs the selected Python diff algorithm on the two line
        sequences, applies _realign_opcodes to fix LCS tie-breaking
        issues, then walks the opcodes and yields events (A_LINE_DEL,
        B_LINE_ADD, A_GAP, B_GAP, ALIGN, A_SYMBOL_DEL, etc.) that
        __init__.py consumes to paint the compare view.

        The alignment mode (beautify_alignment) only affects how
        unequal-count REPLACE blocks are laid out — see
        _replace_block_chunks.

        Also populates self.diffmap with [i1, i2, j1, j2] for each
        non-equal opcode, used by jump()/copy()/select_current().

        Args:
            a, b: the two line sequences (lists of strings). The
                Differ does NOT store them — they are used as locals
                inside this generator and dropped when it returns.
                The caller (Command.refresh_compare) builds them fresh on
                every compare via split_lines_safe(a_text_all) /
                split_lines_safe(b_text_all), so between compares the
                Differ holds zero line-list bytes — only config +
                diffmap. This mirrors the native Differ's design (which
                takes raw texts as compare() params for the same reason:
                the editor tabs are the source of truth, a Python-side
                persistent copy would be a transient duplicate with no
                consumer after compare() returns).
        """
        # Benchmark: when _BENCHMARK is True, measure the total time from
        # when the generator starts executing until it is fully consumed
        # (or closed).
        _bm_start = time.perf_counter() if _BENCHMARK else None

        # NOTE: no 'compare' wrapper section any more. A section open
        # across this generator's yields would also be open while the
        # CONSUMER works between two next() calls (the generator is
        # suspended inside it), so all consumer-side time would be
        # booked into the generator's section -- the flaw that made the
        # old report show replace_block:positional_pair as a fake giant
        # bottleneck. Only sections that close before a yield remain
        # (compare:algorithm, compare:realign_opcodes, per-chunk
        # compare:positional_pairs / compare:find_best_pairs).

        self.diffmap = []
        Profiler.start('compare:algorithm')
        if self.diff_algorithm == 'hybrid':
            diff = HybridSequenceMatcher(None, a, b)
        elif self.diff_algorithm == 'myers':
            diff = MyersSequenceMatcher(None, a, b)
        elif self.diff_algorithm == 'vscode':
            diff = VSCodeSequenceMatcher(None, a, b)
        elif self.diff_algorithm == 'patience':
            diff = PatienceSequenceMatcher(None, a, b)
        else:
            # Default: difflib stdlib SequenceMatcher with autojunk=False
            diff = DefaultSequenceMatcher(None, a, b, autojunk=False)

        # get_opcodes() runs the selected pure-Python algorithm — this is
        # where the actual diff is computed (no cudatext.diff_proc call
        # happens on this path; that is the native differ's job).
        opcodes = diff.get_opcodes()
        Profiler.stop('compare:algorithm')

        # RELEASE THE MATCHER NOW — we already have the opcodes and the
        # matcher no longer serves any purpose. The pure-Python matchers
        # (difflib SequenceMatcher, PatienceSequenceMatcher, the
        # HybridSequenceMatcher's internal _CombinedMatcher, the
        # MyersSequenceMatcher) all build substantial internal state
        # during get_opcodes() — hash tables of line fingerprints,
        # back-pointer matrices for the LCS walk, the matching-blocks
        # list, junk-detection dicts — and that state stays alive until
        # the matcher object itself is collected. Without this `del`,
        # all of that intermediate state survives through the entire
        # paint loop below alongside the line lists `a`/`b` we still
        # need. For a 33k-line compare that's ~15-25MB of dead matcher
        # state holding the peak up unnecessarily.
        del diff

        # _realign_opcodes fixes LCS tie-breaking issues where Myers
        # matches trivial lines (empty, whitespace) instead of meaningful
        # ones. This is needed for Python Myers/difflib (which produce
        # INSERT+EQUAL(trivial)+DELETE patterns). The line list `a` is
        # passed in so _realign_opcodes can read the EQUAL block's text
        # without storing it on the instance.
        Profiler.start('compare:realign_opcodes')
        opcodes = self._realign_opcodes(a, opcodes)
        Profiler.stop('compare:realign_opcodes')

        # Event production for REPLACE blocks is instrumented per chunk
        # ('compare:positional_pairs' / 'compare:find_best_pairs' open
        # after the chunk list starts and close BEFORE it is yielded —
        # one row per producer, so the report shows WHICH of the two
        # pairing modes a block used); equal/delete/insert production
        # is trivial tuple loops and stays uninstrumented — its cost lands
        # in the consumer's section, which is where it runs.
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
                # Each REPLACE block's events are BUILT into lists (one
                # list per bounded chunk of pairs) and yielded only after
                # the producing section closed: no Profiler section is
                # ever open across a yield. See the NOTE above.
                if self.withdetail:
                    for evlist in self._replace_block_chunks(
                            a, i1, i2, b, j1, j2):
                        yield from evlist
                else:
                    for evlist in self._plain_replace_chunks(
                            a, i1, i2, b, j1, j2):
                        yield from evlist
        if _bm_start is not None:
            _bm_elapsed = time.perf_counter() - _bm_start
            print('Differ: compare took {:.1f}ms '
                  '(algo={}, a={}lines, b={}lines, opcodes={}diffs)'.format(
                      _bm_elapsed * 1000,
                      self.diff_algorithm,
                      len(a), len(b),
                      len(self.diffmap)))

    # Profiling row the char-level engine calls are marked under (one
    # row for the whole _char_diff call: the wrapper's own cost is a
    # fraction of a microsecond and not worth a second row).
    _CHAR_ROW = 'char_diff:python_engine'

    # Pair chunk for huge REPLACE blocks: events are produced (and
    # profiled) per chunk, so neither the event list nor the open
    # 'compare:positional_pairs' frame grows with the block size.
    _REPLACE_CHUNK = 512

    def _positional_pairs_events(self, out, a, alo, b, blo, count):
        """Pair the k-th A line with the k-th B line, top-down, for
        `count` pairs, APPENDING each pair's events to `out` (a builder,
        not a generator — no Profiler section is ever open across a
        yield). For identical lines append ALIGN only; for different
        lines run the Python char diff and append its paint events, then
        ALIGN. No heuristics — this is the sdiff/WinMerge alignment,
        also used by the beautify mode when line counts are equal.

        Profiling: engine calls are timed with perf_counter (a ~60ns
        read — ~1% observer effect on a ~10us engine call) and booked
        ONCE per chunk via Profiler.mark(). No per-pair sections: the
        old design's 800k char_diff + 800k char_diff:native_engine
        sections per 1M-line compare cost more than the pairing itself
        and made the report lie.
        """
        prof_on = Profiler.enabled
        row = self._CHAR_ROW
        char_diff_call = self._char_diff
        append = out.append
        if prof_on:
            eng_dt = 0.0
            eng_n = 0
            eng_max = 0.0
        for k in range(count):
            ai, bj = alo + k, blo + k
            if a[ai] == b[bj]:
                append((ALIGN, ai, bj))
            else:
                if prof_on:
                    t0 = time.perf_counter()
                    ops = char_diff_call(a[ai], b[bj])
                    dt = time.perf_counter() - t0
                    eng_dt += dt
                    eng_n += 1
                    if dt > eng_max:
                        eng_max = dt
                else:
                    ops = char_diff_call(a[ai], b[bj])
                # the composite PAIR_CHANGED event carries the pair's
                # line-level work incl. its wrap-compensation (the old
                # trailing ALIGN's job)
                self._char_diff_pair_events(out, ai, bj, ops)
        if prof_on and eng_n:
            Profiler.mark(row, eng_dt, eng_n, eng_max)

    def _replace_block_chunks(self, a, alo, ahi, b, blo, bhi):
        """Process a 'replace' opcode: a[alo:ahi] is replaced by
        b[blo:bhi]. GENERATOR OF EVENT LISTS: yields the block's paint
        events as one or more lists, in exactly the order the old
        generator-based version yielded them. The producing section
        ('compare:find_best_pairs' for the beautify path,
        'compare:positional_pairs' for the positional path — one row per
        producer so the report shows which mode a block used) opens
        after a list starts and closes before the list is yielded —
        never open across a yield (see compare()'s NOTE). Event lists
        are bounded by _REPLACE_CHUNK pairs, so huge blocks (two
        entirely different 1M-line files come as ONE replace opcode)
        do not materialize their whole event stream at once.

        Two rendering modes, selected by self.beautify_alignment:

        beautify_alignment = True ('beautified' alignment)
            Unequal line counts use _find_best_pairs_events(): anchor on
            the longest unique exact match or the best prefix/suffix-
            similar pair, char-diff it, recurse on both sides. Lines
            with < 3 chars of similarity are shown as separate
            delete+add. VS Code-like; re-arranges the engine's output.

            Fast path (da == db): positional pairing (same as the
            algo-faithful mode) — the common case; avoids the O(N*M)
            prefix/suffix search.

            Slow path (da != db): _find_best_pairs_events — see its
            docstring.

        beautify_alignment = False (algo-faithful, default)
            Render exactly the way the algorithm dictates: pair the
            first min(da, db) lines top-down by position (char-diff each
            pair via the Python char_diff), and show leftover lines on
            the longer
            side as plain added/deleted lines against a gap at the
            bottom of the shorter side. Nothing is re-paired or
            re-ordered — the way WinMerge / GNU diffutils side-by-side
            (sdiff) output does.

        Equal line counts (da == db) are positional in BOTH modes, so
        the modes diverge only in the da != db branch.
        """
        da, db = ahi - alo, bhi - blo

        # Defensive only — a 'replace' opcode from the engine always has
        # both sides non-empty (pure insert/delete arrive as their own
        # opcodes in compare()). These branches contain no heuristics;
        # they just render a degenerate opcode faithfully.
        if da == 0 and db == 0:
            return
        if da == 0:
            yield [(A_GAP, alo, blo, bhi)] + \
                  [(B_LINE_ADD, y) for y in range(blo, bhi)]
            return
        if db == 0:
            yield [(B_GAP, blo, alo, ahi)] + \
                  [(A_LINE_DEL, y) for y in range(alo, ahi)]
            return

        # ---- da != db: the two modes diverge here ----
        if self.beautify_alignment and da != db:
            # anchor + prefix/suffix scoring + threshold + staggering.
            # Produced into ONE list (beautify is opt-in; the recursive
            # scorer makes chunking invasive). Char diffs inside are
            # perf_counter-timed and booked as batched marks.
            Profiler.start('compare:find_best_pairs')
            evs = []
            self._find_best_pairs_events(evs, a, alo, ahi, b, blo, bhi)
            Profiler.stop('compare:find_best_pairs')
            yield evs
            return

        # Shared fast path (both modes): positional pairing for the
        # common line count, chunked; then leftovers on the longer side
        # as plain added/deleted lines against a gap at the bottom of
        # the shorter side's block.
        common = min(da, db)
        k = 0
        while k < common:
            n = self._REPLACE_CHUNK
            if common - k < n:
                n = common - k
            Profiler.start('compare:positional_pairs')
            evs = []
            self._positional_pairs_events(evs, a, alo + k, b, blo + k, n)
            Profiler.stop('compare:positional_pairs')
            yield evs
            k += n

        chunk = self._REPLACE_CHUNK
        if da > common:
            # Leftover A lines, against a gap at the bottom of B's block.
            y = alo + common
            first = True
            while y < ahi:
                y2 = y + chunk if ahi - y > chunk else ahi
                evs = []
                if first:
                    evs.append((B_GAP, bhi, alo + common, ahi))
                    first = False
                for yy in range(y, y2):
                    evs.append((A_LINE_DEL, yy))
                yield evs
                y = y2
        elif db > common:
            # Leftover B lines, against a gap at the bottom of A's block.
            y = blo + common
            first = True
            while y < bhi:
                y2 = y + chunk if bhi - y > chunk else bhi
                evs = []
                if first:
                    evs.append((A_GAP, ahi, blo + common, bhi))
                    first = False
                for yy in range(y, y2):
                    evs.append((B_LINE_ADD, yy))
                yield evs
                y = y2

    def _find_best_pairs_events(self, out, a, alo, ahi, b, blo, bhi):
        """(beautify mode) Find the best line alignment within a
        sub-REPLACE block, APPENDING events to `out`.

        Strategies (same as the old generator version):
        1. Exact unique match: build a dict of unique lines in
           a[alo:ahi], scan b[blo:bhi] for matches, pick the LONGEST
           match as anchor. O(N+M) via Counter-based uniqueness check.
        2. If no exact match: prefix/suffix length scoring, O(N*M) per
           block (each comparison O(line_length)).

        After finding the best pair, char-diff it, then recurse on the
        parts before and after. A minimum prefix threshold (>= 3 chars)
        prevents pairing completely unrelated lines.

        Profiling: both searches and the char diffs are perf_counter-
        timed and booked as batched marks (calls = 1 per invocation) —
        no sections, so recursion depth adds zero instrumentation cost.
        """
        da, db = ahi - alo, bhi - blo
        if da == 0:
            if db > 0:
                out.append((A_GAP, alo, blo, bhi))
                for y in range(blo, bhi):
                    out.append((B_LINE_ADD, y))
            return
        if db == 0:
            if da > 0:
                out.append((B_GAP, blo, alo, ahi))
                for y in range(alo, ahi):
                    out.append((A_LINE_DEL, y))
            return

        # Find the best-matching pair by char-level similarity.
        # First pass: find all unique exact matches and pick the longest.
        # Use a dict-based approach for O(N+M) instead of O(N*M):
        # build a map of unique lines in a[alo:ahi], then scan b[blo:bhi].
        prof_on = Profiler.enabled
        if prof_on:
            _t0 = time.perf_counter()
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
        if prof_on:
            Profiler.mark('find_best_pairs:exact_match_search',
                          time.perf_counter() - _t0)

        best_score = -1
        best_prefix = 0
        best_i, best_j = alo, blo
        max_prefix_any = 0

        if best_exact_i >= 0:
            # Use the longest unique exact match as anchor
            best_i, best_j = best_exact_i, best_exact_j
            best_score = 1000000
            best_prefix = 1000000
            max_prefix_any = 1000000
        else:
            # No unique exact match — prefix/suffix length scoring.
            # NOTE: O(N*M) and RECURSIVE (see the old generator's
            # docstring) — the reason beautify_alignment is off by
            # default on large files.
            if prof_on:
                _t0 = time.perf_counter()
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
            if prof_on:
                Profiler.mark('find_best_pairs:prefix_suffix_search',
                              time.perf_counter() - _t0)

        # Minimum similarity threshold: only pair lines if the BEST pair
        # shares a meaningful common prefix (>= 3 chars) — VS Code's
        # behavior; unrelated lines show as separate delete + add.
        # Exception: 1xN/Nx1 blocks pair positionally when the opposing
        # first line is non-trivial (>= 3 non-whitespace chars).
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
                        out.append((B_GAP, blo, i, i + 1))
                        out.append((A_LINE_DEL, i))
                    for j in range(blo, bhi):
                        out.append((A_GAP, ahi, j, j + 1))
                        out.append((B_LINE_ADD, j))
                    return
            else:
                # Multi-line block with no good match: show all as
                # separate delete + add
                for i in range(alo, ahi):
                    out.append((B_GAP, blo, i, i + 1))
                    out.append((A_LINE_DEL, i))
                for j in range(blo, bhi):
                    out.append((A_GAP, ahi, j, j + 1))
                    out.append((B_LINE_ADD, j))
                return

        # Recurse on the part before the best pair
        self._find_best_pairs_events(out, a, alo, best_i, b, blo, best_j)

        # Process the best pair itself
        a_line, b_line = a[best_i], b[best_j]
        if a_line == b_line:
            out.append((ALIGN, best_i, best_j))
        else:
            if prof_on:
                _t0 = time.perf_counter()
                ops = self._char_diff(a_line, b_line)
                dt = time.perf_counter() - _t0
                Profiler.mark(self._CHAR_ROW, dt, 1, dt)
            else:
                ops = self._char_diff(a_line, b_line)
            self._char_diff_pair_events(out, best_i, best_j, ops)
            # composite PAIR_CHANGED carries the pair's line-level work
            # incl. its wrap-compensation (the old trailing ALIGN's job)

        # Recurse on the part after the best pair
        self._find_best_pairs_events(out, a, best_i + 1, ahi,
                                     b, best_j + 1, bhi)

    def _char_diff_pair_events(self, out, ai, bj, ops):
        """Append character-level diff events for a single line pair,
        given the char-level opcodes from char_diff().
        Shared by BOTH alignment modes. Pure glue — no decisions here.

        The pair's line-level work travels as ONE composite PAIR_CHANGED
        event (see the constant's comment): the consumer paints both
        lines' bookmark / micromap mark / overview state and runs the
        pair's wrap-compensation from that single event, where the walk
        used to emit A_LINE_CHANGE + B_LINE_CHANGE + A_DECOR_* +
        B_DECOR_* (+ a trailing ALIGN). The specific character ranges
        that differ are still emitted as A_SYMBOL_DEL / B_SYMBOL_ADD
        events so the wrapper can highlight them inline. Same final
        painted state as the old four-event form, byte-for-byte.
        """
        deca = 0
        decb = 0
        append = out.append
        for tag, a_start, a_end, b_start, b_end in ops:
            if tag == 'delete':
                deca += 1
                append((A_SYMBOL_DEL, ai, a_start, a_end - a_start))
            elif tag == 'insert':
                decb += 1
                append((B_SYMBOL_ADD, bj, b_start, b_end - b_start))
            elif tag == 'replace':
                deca += 1
                decb += 1
                append((A_SYMBOL_DEL, ai, a_start, a_end - a_start))
                append((B_SYMBOL_ADD, bj, b_start, b_end - b_start))
        append((PAIR_CHANGED, ai, bj, deca, decb))

    def _plain_replace_chunks(self, a, alo, ahi, b, blo, bhi):
        """Non-detailed replace (withdetail=False). Pairs lines by
        position and marks all as changed (A_LINE_CHANGE/B_LINE_CHANGE)
        with yellow decor (no char-level highlights). Generator of event
        lists with bounded size; same event order as the old
        _plain_replace_simple generator.
        """
        da, db = ahi - alo, bhi - blo
        common = min(da, db)
        flush_at = self._REPLACE_CHUNK * 4
        evs = []
        append = evs.append
        for k in range(common):
            append((ALIGN, alo + k, blo + k))
            if len(evs) >= flush_at:
                yield evs
                evs = []
                append = evs.append
        if da > db:
            append((B_GAP, bhi, alo + common, ahi))
        elif db > da:
            append((A_GAP, ahi, blo + common, bhi))
        for y in range(alo, ahi):
            append((A_LINE_CHANGE, y))
            append((A_DECOR_YELLOW, y))
            if len(evs) >= flush_at:
                yield evs
                evs = []
                append = evs.append
        for y in range(blo, bhi):
            append((B_LINE_CHANGE, y))
            append((B_DECOR_YELLOW, y))
            if len(evs) >= flush_at:
                yield evs
                evs = []
                append = evs.append
        if evs:
            yield evs
