import time
from difflib import SequenceMatcher as DefaultSequenceMatcher, unified_diff
from .myers import MyersSequenceMatcher, InlineMyersSequenceMatcher
from .patiencediff import PatienceSequenceMatcher
from .vscode_diff import VSCodeSequenceMatcher


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
        self.ratio = 0.75
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
    # removeVeryShortMatchingLinesBetweenDiffs threshold. This means
    # empty lines, whitespace-only lines, and very short lines like '{'
    # or '}' will be absorbed; meaningful lines like 'delete-only' will
    # not (they have 11 non-ws chars).
    _REALIGN_TRIVIAL_THRESHOLD = 4

    def _realign_opcodes(self, opcodes):
        """Merge INSERT+EQUAL(trivial)+DELETE (or DELETE+EQUAL(trivial)+INSERT)
        into a single REPLACE.

        Myers' O(NP) and difflib's SequenceMatcher both produce LCS-based
        diffs. When there are multiple valid LCS of the same length, they
        may pick a different one than VS Code's DP algorithm (which uses
        equality scoring that prefers longer matches). The most visible
        symptom: an INSERT+EQUAL+DELETE sequence where the EQUAL block is
        a trivial line (empty line, whitespace, '{', '}') and the
        INSERT/DELETE blocks contain meaningful lines that could have been
        paired. Example:

            A:  insert-only \n  delete-only  delllll  \n  end
            B:  insert-only insertttt delete-only  \n   \n  end

        Myers produces:  INSERT B[10:12] + EQUAL A[10]<->B[12]('\n') + DELETE A[11:13]
            (matches the empty '\n' instead of 'delete-only')

        VS Code produces: REPLACE A[10]<->B[10] + EQUAL A[11]<->B[11] + REPLACE A[12]<->B[12]
            (matches 'delete-only' — the meaningful line)

        Both are valid LCS of length 4, but VS Code's is more intuitive.
        This method detects the Myers pattern and merges it into a single
        REPLACE. Then _fancy_replace (called in compare() for 'replace'
        tags) finds the best line-level alignment within the REPLACE block,
        naturally matching identical lines like 'delete-only' (ratio 1.0).

        The merge only happens when the EQUAL block is "trivial": its
        total non-whitespace content is <= _REALIGN_TRIVIAL_THRESHOLD
        characters. This prevents absorbing meaningful equal lines.

        This is a no-op for algorithms that don't produce the
        INSERT+EQUAL+DELETE pattern (VS Code, patience).
        """
        if len(opcodes) < 3:
            return opcodes
        result = list(opcodes)
        i = 1
        while i < len(result) - 1:
            prev = result[i - 1]
            cur = result[i]
            nxt = result[i + 1]
            # Pattern: (INSERT or DELETE) + EQUAL + (DELETE or INSERT)
            # The two non-equal opcodes must be different types (one
            # INSERT, one DELETE) -- otherwise there's nothing to merge.
            if (cur[0] == 'equal' and
                    prev[0] in ('insert', 'delete') and
                    nxt[0] in ('insert', 'delete') and
                    prev[0] != nxt[0]):
                # Check if the EQUAL block is trivial
                _, ei1, ei2, ej1, ej2 = cur
                equal_text = ''.join(self.a[ei1:ei2])
                non_ws = equal_text.replace(' ', '').replace('\t', '')
                non_ws = non_ws.replace('\n', '').replace('\r', '')
                if len(non_ws) <= self._REALIGN_TRIVIAL_THRESHOLD:
                    # Merge prev + cur + nxt into a single REPLACE.
                    # The new REPLACE spans from prev's start to nxt's end
                    # on both sides.
                    merged = ('replace',
                              prev[1], nxt[2],  # a_start, a_end
                              prev[3], nxt[4])  # b_start, b_end
                    result[i - 1:i + 2] = [merged]
                    # Don't advance i -- the merged REPLACE might be
                    # adjacent to another mergeable pattern.
                    # But step back to re-check from i-1.
                    if i > 1:
                        i -= 1
                    continue
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
        opcodes = diff.get_opcodes()
        # Post-process: merge INSERT+EQUAL(trivial)+DELETE (or
        # DELETE+EQUAL(trivial)+INSERT) into a single REPLACE. This fixes
        # the LCS tie-breaking issue where Myers (and difflib) match a
        # trivial line (empty line, whitespace, '{', '}') instead of a
        # meaningful line, producing INSERT+EQUAL+DELETE instead of
        # REPLACE+EQUAL+REPLACE. After merging, _fancy_replace finds the
        # best line-level alignment within the REPLACE block and naturally
        # matches identical lines. VSCode and patience don't produce this
        # pattern so the merge is a no-op for them. See
        # _realign_opcodes for details.
        opcodes = self._realign_opcodes(opcodes)
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
                    yield from self._fancy_replace(self.a, i1, i2,
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

    def _fancy_replace(self, a, alo, ahi, b, blo, bhi):
        best_ratio, cutoff = self.ratio-0.01, self.ratio
        if self.diff_algorithm == 'myers':
            # InlineMyersSequenceMatcher is the right Myers variant for
            # character-level diffing: its preprocessing pass uses 3-element
            # k-mers instead of single elements, which is what makes the
            # preprocessing effective on character sequences (where single
            # elements are rarely unique). Meld uses this same class for
            # its inline/character-level highlighting. The base
            # MyersSequenceMatcher's 1-element preprocessing is correct but
            # ineffective for characters -- 'a' appears everywhere, so
            # almost nothing gets discarded and the full O(NP) runs on the
            # raw strings. InlineMyersSequenceMatcher is 2-4x faster on
            # medium/long lines and produces more meaningful character-level
            # diffs. For the main line-level diff (Differ.compare) we still
            # use MyersSequenceMatcher because lines are usually unique
            # enough that 1-element preprocessing is appropriate.
            diff = InlineMyersSequenceMatcher(None)
        elif self.diff_algorithm == 'patience':
            diff = PatienceSequenceMatcher(None)
        else:
            diff = DefaultSequenceMatcher(None, autojunk=self.autojunk)
        eqi, eqj = None, None
        for j in range(blo, bhi):
            bj = b[j]
            diff.set_seq2(bj)
            for i in range(alo, ahi):
                ai = a[i]
                if ai == bj:
                    if eqi is None:
                        eqi, eqj = i, j
                    continue
                diff.set_seq1(ai)
                if diff.real_quick_ratio() > best_ratio and \
                        diff.quick_ratio() > best_ratio and \
                        diff.ratio() > best_ratio:
                    best_ratio, best_i, best_j = diff.ratio(), i, j
        if best_ratio < cutoff:
            if eqi is None:
                yield from self._plain_replace(a, alo, ahi, b, blo, bhi)
                return
            best_i, best_j, best_ratio = eqi, eqj, 1.0
        else:
            eqi = None
        yield from self._fancy_helper(a, alo, best_i, b, blo, best_j)
        aelt, belt = a[best_i], b[best_j]
        if eqi is None:
            diff.set_seqs(aelt, belt)
            deca, decb = 0, 0
            for tag, ai1, ai2, bj1, bj2 in diff.get_opcodes():
                la, lb = ai2 - ai1, bj2 - bj1
                if tag == 'delete':
                    deca += 1
                    yield (A_SYMBOL_DEL, best_i, ai1, la)
                elif tag == 'insert':
                    decb += 1
                    yield (B_SYMBOL_ADD, best_j, bj1, lb)
                elif tag == 'replace':
                    deca += 1
                    decb += 1
                    yield (A_SYMBOL_DEL, best_i, ai1, la)
                    yield (B_SYMBOL_ADD, best_j, bj1, lb)
            yield (A_LINE_CHANGE, best_i)
            yield (B_LINE_CHANGE, best_j)
            yield (A_DECOR_YELLOW, best_i) if deca == 0 else \
                  (A_DECOR_RED, best_i)
            yield (B_DECOR_YELLOW, best_j) if decb == 0 else \
                  (B_DECOR_GREEN, best_j)
        # The best pair (best_i, best_j) is the visually-matched anchor of
        # this replace block. Yield ALIGN so the wrapper can add a
        # compensating gap when the two lines wrap to different heights.
        yield (ALIGN, best_i, best_j)
        yield from self._fancy_helper(a, best_i+1, ahi, b, best_j+1, bhi)

    def _fancy_helper(self, a, alo, ahi, b, blo, bhi):
        if alo < ahi:
            if blo < bhi:
                yield from self._fancy_replace(a, alo, ahi, b, blo, bhi)
            else:
                # Only A lines (blo == bhi). Gap in B after line blo-1,
                # compensating for A lines [alo, ahi).
                yield (B_GAP, blo, alo, ahi)
                for y in range(alo, ahi):
                    yield (A_LINE_DEL, y)
        elif blo < bhi:
            # Only B lines (alo == ahi). Gap in A after line alo-1,
            # compensating for B lines [blo, bhi).
            yield (A_GAP, alo, blo, bhi)
            for y in range(blo, bhi):
                yield (B_LINE_ADD, y)

    def _plain_replace(self, a, alo, ahi, b, blo, bhi):
        """Fallback when no good match is found inside a 'replace' block.
        Pairs up the first min(da, db) lines so the wrapper can keep them
        visually aligned, then adds a single gap on the appropriate side for
        the leftover lines."""
        da, db = ahi-alo, bhi-blo
        common = min(da, db)
        for k in range(common):
            yield (ALIGN, alo + k, blo + k)
        if da > db:
            # Extra A lines: [alo+common, ahi). Gap in B after line bhi-1.
            yield (B_GAP, bhi, alo + common, ahi)
        elif db > da:
            # Extra B lines: [blo+common, bhi). Gap in A after line ahi-1.
            yield (A_GAP, ahi, blo + common, bhi)
        for y in range(blo, bhi):
            yield (B_LINE_ADD, y)
        for y in range(alo, ahi):
            yield (A_LINE_DEL, y)

    def _plain_replace_simple(self, a, alo, ahi, b, blo, bhi):
        """Non-detailed replace (withdetail=False). Same pairing strategy as
        _plain_replace, but marks all lines as A_LINE_CHANGE/B_LINE_CHANGE
        with yellow decor (preserving the original non-detailed look)."""
        da, db = ahi-alo, bhi-blo
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
