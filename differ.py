import time
from difflib import SequenceMatcher as DefaultSequenceMatcher, unified_diff
from .myers import MyersSequenceMatcher, InlineMyersSequenceMatcher
from .patiencediff import PatienceSequenceMatcher
from .vscode_diff import VSCodeSequenceMatcher
from .char_diff import char_diff


# Internal benchmark toggle. When set to True, Differ.compare() prints the
# total time elapsed from when the generator starts executing until it is
# fully consumed (or closed).
_BENCHMARK = True


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
# compare path (``Differ.compare`` and ``Differ._fancy_replace``); without this
# wrapper, the unified-diff path (the "Diff with file..." / "Diff with tab..."
# commands) would silently ignore that option when the user sets
# ``autojunk=False``.
#
# Usage policy (see ``Differ.unidiff``):
#   * When ``self.autojunk`` is True (the default), we call
#     ``difflib.unified_diff`` directly -- the stdlib already builds its
#     ``SequenceMatcher`` with ``autojunk=True``, so there is nothing to
#     override. This keeps the hot path on stdlib code (zero maintenance,
#     automatic benefit from any future CPython improvement), and our
#     reimplementation only runs when the user explicitly opts out of the
#     heuristic.
#   * When ``self.autojunk`` is False, we fall back to ``_unified_diff``
#     below, which is the only way to forward ``autojunk=False`` today.
#
# Why we did NOT use the monkey-patch workaround suggested in that issue
# (``unittest.mock.patch`` on ``SequenceMatcher.__init__`` combined with
# ``functools.partialmethod(..., autojunk=False)``):
#   * ``unittest.mock`` is a testing tool; pulling it into production code
#     just to override one keyword is heavy and surprising to readers.
#   * Patching a stdlib class is global for the duration of the ``with``
#     block -- any other thread/call that hits ``SequenceMatcher`` meanwhile
#     is affected too.
#   * The snippet hardcodes ``autojunk=False`` and therefore cannot honor a
#     user config of ``autojunk=True`` without extra conditionals; in our
#     setup that means we'd be patching even on the default path, paying the
#     cost and risk for no benefit.
#   * Forward compatibility: if CPython ever adds ``autojunk`` to
#     ``unified_diff`` (the very point of the issue above), ``partialmethod``'s
#     preset ``autojunk=False`` would silently override the new parameter's
#     default whenever the caller does not pass it explicitly, masking the
#     stdlib behavior. Our reimplementation has no such issue -- and once
#     CPython ships ``autojunk`` on ``unified_diff``, we can delete this
#     function and call ``difflib.unified_diff(..., autojunk=autojunk)``
#     directly in both branches.
#
# Implementation is a verbatim copy of ``difflib.unified_diff`` from CPython
# with two differences:
#   1. ``SequenceMatcher(None, a, b, autojunk=autojunk)`` instead of
#      ``SequenceMatcher(None, a, b)`` -- the whole point.
#   2. ``fromfiledate``/``tofiledate`` of ``None`` fall back to an empty
#      string instead of the current system timestamp. The plugin never
#      passes dates, so this is a non-issue here; if you need timestamps,
#      format them yourself and pass them as strings.
def _unified_diff(a, b, fromfile='', tofile='',
                  fromfiledate='', tofiledate='',
                  n=3, lineterm='\n', autojunk=True):
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
    """
    compare function return tuples for paint text
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
        self.withdetail = True
        # 'myers'   (MyersSequenceMatcher — O(NP) Wu/Manber/Myers/Miller
        #            1989 with common prefix/suffix trimming and a
        #            non-matching-line discard preprocessing pass;
        #            default, fastest in the common case),
        # 'vscode'  (VS Code-style DP/Myers diff with equality scoring —
        #            best quality for duplicated-line files, slowest),
        # 'patience' (PatienceSequenceMatcher — anchors on unique lines),
        # or 'difflib' (Python stdlib SequenceMatcher with autojunk).
        self.diff_algorithm = 'myers'
        self.autojunk = True
        self.set_seqs(a, b)
        self.diffmap = []

    def set_seqs(self, a, b):
        self.a = a
        self.b = b

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

        # Pass 4: re-align REPLACE blocks that contain lines with exact
        # matches in other REPLACE blocks. Myers' LCS sometimes matches
        # a line with a similar (but not equal) line instead of its exact
        # counterpart. This causes identical lines (like 'def
        # on_change_slow') to end up in different REPLACE blocks instead
        # of being matched as EQUAL. We detect this and merge the blocks
        # so _replace_block can re-diff and find the exact match.
        # LIMIT: only merge if the intervening blocks total <= 30 lines,
        # to avoid merging huge sections (which would fall back to
        # positional pairing and defeat the purpose).
        i = 0
        while i < len(result):
            if result[i][0] != 'replace':
                i += 1
                continue
            _, ri1, ri2, rj1, rj2 = result[i]
            found_merge = False
            for ai in range(ri1, ri2):
                a_line = self.a[ai]
                a_non_ws = a_line.replace(' ', '').replace('\t', '')
                a_non_ws = a_non_ws.replace('\n', '').replace('\r', '')
                if len(a_non_ws) < 5:
                    continue
                for k in range(i + 1, len(result)):
                    if result[k][0] != 'replace':
                        continue
                    # Check merge distance: count lines in intervening blocks
                    intervening = 0
                    for m in range(i + 1, k):
                        intervening += max(result[m][2] - result[m][1],
                                           result[m][4] - result[m][3])
                    if intervening > 30:
                        break  # too far, stop searching
                    _, _, _, kj1, kj2 = result[k]
                    for bj in range(kj1, kj2):
                        if self.b[bj] == a_line:
                            merged = ('replace',
                                      result[i][1], result[k][2],
                                      result[i][3], result[k][4])
                            result[i:k + 1] = [merged]
                            found_merge = True
                            break
                    if found_merge:
                        break
                if found_merge:
                    break
            if not found_merge:
                i += 1

        return result

    def compare(self):
        # Benchmark: when _BENCHMARK is True, measure the total time from
        # when the generator starts executing until it is fully consumed
        # (or closed).
        _bm_start = time.perf_counter() if _BENCHMARK else None

        self.diffmap = []
        if self.diff_algorithm == 'myers':
            diff = MyersSequenceMatcher(None, self.a, self.b)
        elif self.diff_algorithm == 'vscode':
            diff = VSCodeSequenceMatcher(None, self.a, self.b)
        elif self.diff_algorithm == 'patience':
            diff = PatienceSequenceMatcher(None, self.a, self.b)
        else:
            diff = DefaultSequenceMatcher(None, self.a, self.b, autojunk=self.autojunk)
        opcodes = self._realign_opcodes(diff.get_opcodes())
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

        Runs a line-level diff WITHIN the REPLACE block to find exactly-
        equal lines (anchor points), then for sub-REPLACE blocks where
        lines are similar but not equal, finds the best-matching line
        pairs by char-level similarity (difflib ratio). This matches VS
        Code's behavior of aligning similar lines within a REPLACE block.

        Algorithm:
          1. Run MyersSequenceMatcher on a[alo:ahi] vs b[blo:bhi] to find
             exactly-equal lines.
          2. Apply _realign_opcodes to fix INSERT+EQUAL(trivial)+DELETE
             patterns within the sub-block.
          3. EQUAL sub-blocks: yield ALIGN.
          4. DELETE sub-blocks: yield A_LINE_DEL + B_GAP.
          5. INSERT sub-blocks: yield B_LINE_ADD + A_GAP.
          6. Sub-REPLACE blocks: call _find_best_pairs to align similar
             lines by char-level ratio, then do char_diff on each pair.
        """
        da, db = ahi - alo, bhi - blo
        if da == 0 and db == 0:
            return
        if da == 0:
            yield (A_GAP, alo, blo, bhi)
            for y in range(blo, bhi):
                yield (B_LINE_ADD, y)
            return
        if db == 0:
            yield (B_GAP, blo, alo, ahi)
            for y in range(alo, ahi):
                yield (A_LINE_DEL, y)
            return

        # Fast path: when both sides have the same number of lines, use
        # positional pairing directly. This avoids the overhead of running
        # a line-level Myers diff + _realign_opcodes on every REPLACE
        # block. For large files (e.g. 33k-line HTML), most REPLACE blocks
        # have da == db, so this keeps performance at ~30s instead of ~80s.
        # The ratio-based search (_find_best_pairs) is only needed when
        # da != db, because that's when positional pairing might misalign
        # similar lines.
        if da == db:
            common = min(da, db)
            for k in range(common):
                ai, bj = alo + k, blo + k
                if a[ai] == b[bj]:
                    yield (ALIGN, ai, bj)
                else:
                    ops = char_diff(a[ai], b[bj])
                    yield from self._char_diff_pair(ai, bj, ops)
                    yield (ALIGN, ai, bj)
            return

        # Different line counts (da != db): need to find the best alignment.
        # For large blocks, run a line-level Myers diff to find exact-match
        # anchor points, then use positional pairing for sub-REPLACE blocks.
        # This is needed for Pass 4 merged blocks (where identical lines
        # like 'def on_change_slow' need to be re-matched after merging).
        # For small blocks, use ratio-based best-pair search.
        if da > self._BEST_PAIR_MAX_LINES or db > self._BEST_PAIR_MAX_LINES:
            # Run line-level diff to find exact matches (anchor points).
            # Use PatienceSequenceMatcher instead of MyersSequenceMatcher
            # because patience diff anchors on unique matching lines,
            # correctly matching identical lines like 'def on_change_slow'
            # that Myers' LCS might skip in favor of a different LCS of
            # the same length. This matches Beyond Compare/WinMerge behavior.
            sub_a = a[alo:ahi]
            sub_b = b[blo:bhi]
            matcher = PatienceSequenceMatcher(None, sub_a, sub_b)
            sub_ops = matcher.get_opcodes()
            for tag, i1, i2, j1, j2 in sub_ops:
                abs_i1, abs_i2 = alo + i1, alo + i2
                abs_j1, abs_j2 = blo + j1, blo + j2
                if tag == 'equal':
                    for k in range(i2 - i1):
                        yield (ALIGN, abs_i1 + k, abs_j1 + k)
                elif tag == 'delete':
                    yield (B_GAP, abs_j1, abs_i1, abs_i2)
                    for y in range(abs_i1, abs_i2):
                        yield (A_LINE_DEL, y)
                elif tag == 'insert':
                    yield (A_GAP, abs_i1, abs_j1, abs_j2)
                    for y in range(abs_j1, abs_j2):
                        yield (B_LINE_ADD, y)
                elif tag == 'replace':
                    # Sub-REPLACE: positional pairing + char_diff
                    sub_da = abs_i2 - abs_i1
                    sub_db = abs_j2 - abs_j1
                    common = min(sub_da, sub_db)
                    for k in range(common):
                        ai, bj = abs_i1 + k, abs_j1 + k
                        if a[ai] == b[bj]:
                            yield (ALIGN, ai, bj)
                        else:
                            ops = char_diff(a[ai], b[bj])
                            yield from self._char_diff_pair(ai, bj, ops)
                            yield (ALIGN, ai, bj)
                    if sub_da > sub_db:
                        yield (B_GAP, abs_j2, abs_i1 + common, abs_i2)
                        for y in range(abs_i1 + common, abs_i2):
                            yield (A_LINE_DEL, y)
                    elif sub_db > sub_da:
                        yield (A_GAP, abs_i2, abs_j1 + common, abs_j2)
                        for y in range(abs_j1 + common, abs_j2):
                            yield (B_LINE_ADD, y)
            return

        # Small block with da != db: use ratio-based best-pair search
        # directly. _find_best_pairs finds the best-matching pair by
        # char-level ratio (exact matches get ratio 1.0 and are paired
        # first), then recurses on the parts before and after. This
        # aligns similar (but not equal) lines like VS Code does, and
        # correctly pairs identical lines (like 'delete-only') without
        # needing a separate line-level diff + _realign_opcodes pass.
        yield from self._find_best_pairs(a, alo, ahi, b, blo, bhi)

    # Maximum lines per side for the ratio-based best-pair search.
    # Blocks larger than this use positional pairing (fast). The value
    # 20 means typical code diffs (1-20 lines per REPLACE block) get
    # high-quality char-level alignment, while large minified/data files
    # get fast positional pairing. At 20 lines per side, the worst case
    # is 20*20=400 difflib.ratio() calls per block (C-optimized, ~0.4ms).
    _BEST_PAIR_MAX_LINES = 20

    def _find_best_pairs(self, a, alo, ahi, b, blo, bhi):
        """Find the best line alignment within a sub-REPLACE block.

        For small blocks (<= _BEST_PAIR_MAX_LINES lines per side): find
        the best-matching line pair by char-level similarity (difflib
        ratio), do char_diff on that pair, then recurse on the parts
        before and after. No ratio threshold -- we always pick the best
        pair, even if its ratio is low. For completely different lines
        (ratio ~0), this gives the same result as positional pairing.

        For large blocks: use positional pairing (1st with 1st, etc.)
        to avoid the O(N*M) ratio search. This trades alignment quality
        for speed on large files.

        This approach is fast because:
        - difflib's ratio() uses C-optimized quick_ratio/real_quick_ratio
          filters that skip most pairs without computing the full ratio
        - The size guard limits the search to small blocks
        - For completely different lines, the filters skip all pairs
          after the first, so it's O(N+M) not O(N*M)
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

        # Size guard: large blocks use positional pairing
        if da > self._BEST_PAIR_MAX_LINES or db > self._BEST_PAIR_MAX_LINES:
            common = min(da, db)
            for k in range(common):
                ai, bj = alo + k, blo + k
                if a[ai] == b[bj]:
                    yield (ALIGN, ai, bj)
                else:
                    ops = char_diff(a[ai], b[bj])
                    yield from self._char_diff_pair(ai, bj, ops)
                    yield (ALIGN, ai, bj)
            if da > db:
                yield (B_GAP, bhi, alo + common, ahi)
                for y in range(alo + common, ahi):
                    yield (A_LINE_DEL, y)
            elif db > da:
                yield (A_GAP, ahi, blo + common, bhi)
                for y in range(blo + common, bhi):
                    yield (B_LINE_ADD, y)
            return

        # Find the best-matching pair by char-level similarity.
        # Use common-prefix length as the primary metric (fast O(N) per
        # pair, and accurately identifies lines that share a long prefix
        # — the common case in code diffs where a line is slightly
        # modified). Break ties with common-suffix length, then total
        # matching chars (real_quick_ratio). This avoids the expensive
        # difflib.ratio() call (which uses find_longest_match and takes
        # ~0.3ms per pair, adding ~30s on a 33k-line file).
        best_score = -1
        best_prefix = 0
        best_i, best_j = alo, blo
        max_prefix_any = 0  # track the max prefix across ALL pairs
        for j in range(blo, bhi):
            bj_line = b[j]
            for i in range(alo, ahi):
                ai_line = a[i]
                if ai_line == bj_line:
                    # Exact match -- use it immediately
                    best_i, best_j, best_score = i, j, 1000000
                    best_prefix = 1000000
                    max_prefix_any = 1000000
                    break
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
            else:
                continue
            break  # found exact match

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
            ops = char_diff(a_line, b_line)
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
