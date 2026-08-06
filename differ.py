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

    def _realign_opcodes(self, opcodes):
        """Merge INSERT+EQUAL(trivial)+DELETE (or DELETE+EQUAL(trivial)+INSERT)
        into a single REPLACE.

        Myers' O(NP) and difflib's SequenceMatcher both produce LCS-based
        diffs. When there are multiple valid LCS of the same length, they
        may pick a different one than VS Code's DP algorithm (which uses
        equality scoring that prefers longer matches). The most visible
        symptom: an INSERT+EQUAL+DELETE sequence where the EQUAL block is
        a trivial line (empty line, whitespace) and the INSERT/DELETE
        blocks contain meaningful lines that could have been paired.

        This method detects that pattern and merges it into a single
        REPLACE. Then _replace_block pairs lines by position and naturally
        matches identical lines.

        The merge only happens when the EQUAL block is "trivial": its
        total non-whitespace content is <= _REALIGN_TRIVIAL_THRESHOLD
        characters. This is a no-op for algorithms that don't produce
        this pattern (VS Code, patience).
        """
        if len(opcodes) < 3:
            return opcodes
        result = list(opcodes)
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

        This is the WinMerge/VS Code approach: pair lines by position
        (1st with 1st, 2nd with 2nd, etc.) and do character-level diff on
        each pair. No recursive "find best pair" search, no ratio
        threshold, no ratio_percent config option.

        For character-level diffing, we use the WinMerge approach (ported
        to char_diff.py): word-level Myers diff + byte-level prefix/suffix
        refinement. This is much faster than running Myers directly on
        characters because:
          - Word-level Myers runs on a small array (5-50 tokens per line)
          - Byte-level refinement is O(N) per diff region
        See char_diff.py for details and source references.

        For each positionally-paired line:
          - If the two lines are identical: yield ALIGN only (no highlight,
            matching VS Code which shows identical lines in a REPLACE block
            as unchanged context).
          - If the two lines differ: do character-level diff and yield
            A_LINE_CHANGE/B_LINE_CHANGE + A_SYMBOL_DEL/B_SYMBOL_ADD events.

        Leftover lines (when one side has more lines than the other) get
        a gap on the shorter side and A_LINE_DEL / B_LINE_ADD events.
        """
        da, db = ahi - alo, bhi - blo
        common = min(da, db)

        for k in range(common):
            ai, bj = alo + k, blo + k
            a_line, b_line = a[ai], b[bj]

            if a_line == b_line:
                # Identical lines in a REPLACE block: no highlight,
                # just align (matching VS Code).
                yield (ALIGN, ai, bj)
                continue

            # Character-level diff using WinMerge's word-level approach.
            ops = char_diff(a_line, b_line)
            yield from self._char_diff_pair(ai, bj, ops)

            yield (ALIGN, ai, bj)

        # Handle leftover lines (one side has more lines than the other).
        if da > db:
            # Extra A lines: [alo+common, ahi). Gap in B after line bhi-1.
            yield (B_GAP, bhi, alo + common, ahi)
            for y in range(alo + common, ahi):
                yield (A_LINE_DEL, y)
        elif db > da:
            # Extra B lines: [blo+common, bhi). Gap in A after line ahi-1.
            yield (A_GAP, ahi, blo + common, bhi)
            for y in range(blo + common, bhi):
                yield (B_LINE_ADD, y)

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
