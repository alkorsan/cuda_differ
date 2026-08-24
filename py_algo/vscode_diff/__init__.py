"""VS Code-style line diffing algorithm.

Ported from Microsoft VS Code's diff implementation at:
  https://github.com/microsoft/vscode/tree/03e0f5ddfb3b387ba074581690838f7b07e272a4/src/vs/editor/common/diff

Files ported:
  - defaultLinesDiffComputer/algorithms/diffAlgorithm.ts
      (SequenceDiff, OffsetRange, ISequence interface)
  - defaultLinesDiffComputer/algorithms/myersDiffAlgorithm.ts
      (MyersDiffAlgorithm — O(ND) with snake optimization)
  - defaultLinesDiffComputer/algorithms/dynamicProgrammingDiffing.ts
      (DynamicProgrammingDiffing — O(MN) LCS with equality scoring
       and consecutive-diagonal bonus)
  - defaultLinesDiffComputer/lineSequence.ts
      (LineSequence — trimmed-line hashing with isStronglyEqual)
  - defaultLinesDiffComputer/heuristicSequenceOptimizations.ts
      (optimizeSequenceDiffs, removeVeryShortMatchingLinesBetweenDiffs)
  - defaultLinesDiffComputer/defaultLinesDiffComputer.ts
      (algorithm selection: DP for small files, Myers for large)

Why this exists instead of using difflib or patience:
  1. difflib's SequenceMatcher uses the Ratcliff/Obershelp algorithm
     which, without VS Code's equality scoring and consecutive-diagonal
     bonus, can produce suboptimal alignment on files with many similar
     or duplicated lines. (Note: the differ plugin always passes
     autojunk=False to difflib, so the autojunk 'popular line' heuristic
     is not the issue here -- the issue is the underlying algorithm
     itself.)

  2. Patience diff anchors on UNIQUE matching lines. When a region has
     no unique lines it falls back to pairing lines 1:1 by index, again
     producing a giant misaligned 'replace' block.

  3. VS Code's Dynamic Programming algorithm uses the FULL O(MN) table —
     no junk heuristic, no uniqueness requirement. Every possible
     alignment is considered. The equality scoring (1 + log(1+length)
     for exact matches, 0.99 for whitespace-only matches) and the
     consecutive-diagonal bonus (adding the run length to the score)
     make it prefer long runs of exact matches, producing the same
     alignment that WinMerge and VS Code itself produce.

The class VSCodeSequenceMatcher is a drop-in replacement for
difflib.SequenceMatcher: it exposes get_opcodes() returning the same
list of (tag, i1, i2, j1, j2) tuples.

Performance:
  - DP is O(M*N) in time and space. For the differ plugin's typical
    use case (comparing two source files), M+N < 1700 so DP is used
    and takes a few milliseconds.
  - Myers is O(N*D) where D is the number of differences. It is used
    for large files (M+N >= 1700) as a faster fallback.
  - The threshold 1700 matches VS Code's own threshold.

License: MIT (same as VS Code source)
"""

import math
from typing import List, Tuple, Optional, Callable, Sequence


# ---------------------------------------------------------------------------
# Range and diff primitives (ported from diffAlgorithm.ts)
# ---------------------------------------------------------------------------


class _OffsetRange:
    """Half-open integer range [start, end).

    Ported from VS Code's OffsetRange. Used for both seq1 and seq2 ranges
    in SequenceDiff.
    """

    __slots__ = ('start', 'end')

    def __init__(self, start: int, end: int):
        self.start = start
        self.end = end

    @property
    def length(self) -> int:
        return self.end - self.start

    def is_empty(self) -> bool:
        return self.length == 0

    def join(self, other: '_OffsetRange') -> '_OffsetRange':
        return _OffsetRange(
            min(self.start, other.start),
            max(self.end, other.end),
        )

    def delta(self, offset: int) -> '_OffsetRange':
        if offset == 0:
            return self
        return _OffsetRange(self.start + offset, self.end + offset)

    def delta_start(self, offset: int) -> '_OffsetRange':
        if offset == 0:
            return self
        return _OffsetRange(self.start + offset, self.end)

    def delta_end(self, offset: int) -> '_OffsetRange':
        if offset == 0:
            return self
        return _OffsetRange(self.start, self.end + offset)

    def intersects_or_touches(self, other: '_OffsetRange') -> bool:
        return self.start <= other.end and other.start <= self.end

    def intersect(self, other: '_OffsetRange') -> Optional['_OffsetRange']:
        s = max(self.start, other.start)
        e = min(self.end, other.end)
        if s < e:
            return _OffsetRange(s, e)
        return None

    def __repr__(self):
        return f'[{self.start}, {self.end})'


class _SequenceDiff:
    """A difference region: seq1[seq1_range] differs from seq2[seq2_range].

    Ported from VS Code's SequenceDiff. Note: this represents the CHANGED
    parts (not the equal parts). Between consecutive SequenceDiffs, the
    sequences are equal. This is the opposite of difflib's 'equal' opcode.
    """

    __slots__ = ('seq1_range', 'seq2_range')

    def __init__(self, seq1_range: _OffsetRange, seq2_range: _OffsetRange):
        self.seq1_range = seq1_range
        self.seq2_range = seq2_range

    def swap(self) -> '_SequenceDiff':
        return _SequenceDiff(self.seq2_range, self.seq1_range)

    def join(self, other: '_SequenceDiff') -> '_SequenceDiff':
        return _SequenceDiff(
            self.seq1_range.join(other.seq1_range),
            self.seq2_range.join(other.seq2_range),
        )

    def delta(self, offset: int) -> '_SequenceDiff':
        return _SequenceDiff(
            self.seq1_range.delta(offset),
            self.seq2_range.delta(offset),
        )

    def delta_start(self, offset: int) -> '_SequenceDiff':
        return _SequenceDiff(
            self.seq1_range.delta_start(offset),
            self.seq2_range.delta_start(offset),
        )

    def delta_end(self, offset: int) -> '_SequenceDiff':
        return _SequenceDiff(
            self.seq1_range.delta_end(offset),
            self.seq2_range.delta_end(offset),
        )

    def __repr__(self):
        return f'SeqDiff({self.seq1_range}, {self.seq2_range})'


# ---------------------------------------------------------------------------
# Sequence adapter (ported from lineSequence.ts)
# ---------------------------------------------------------------------------


def _get_indentation(s: str) -> int:
    """Count leading spaces and tabs. Used for boundary scoring."""
    i = 0
    while i < len(s) and s[i] in (' ', '\t'):
        i += 1
    return i


class _LineSequence:
    """Sequence adapter for line lists.

    Ported from VS Code's LineSequence. The key design decisions are:

    1. **Trimmed-line perfect hashing**: lines are hashed by their trimmed
       content (whitespace stripped from both ends). This means lines that
       differ only in indentation are treated as equal for alignment
       purposes, so indentation changes don't disrupt the diff.

       IMPORTANT: the hash map must be SHARED between both sequences
       (seq1 and seq2), so that the same trimmed line gets the same hash
       in both. VS Code builds a single `perfectHashes` map and uses it
       for both originalLines and modifiedLines. If each sequence built
       its own independent hash map, 'dsds' in seq1 and 'ggg' in seq2
       would both get hash 0 and be treated as equal — producing garbage.

    2. **isStronglyEqual**: checks the ORIGINAL (untrimmed) lines for exact
       equality. This is used by the heuristic optimizations to avoid
       shifting diffs across lines that are only equal after trimming.

    3. **getBoundaryScore**: returns a score for how good a boundary position
       is, based on indentation. Boundaries at indentation changes are
       preferred (higher score). Used by shiftSequenceDiffs to place diffs
       at natural code boundaries.
    """

    def __init__(self, lines: Sequence[str], hash_map: dict = None):
        """Create a LineSequence.

        Args:
            lines: the original (untrimmed) lines.
            hash_map: a shared hash map. If None, a new (empty) one is
                created — but for diffing, BOTH sequences must share the
                same hash_map so the same trimmed line gets the same hash.
                Pass the same dict to both _LineSequence constructors,
                matching VS Code's perfectHashes pattern.
        """
        self.lines = list(lines)
        # Use the provided shared hash map, or create a new one.
        # For diffing, the caller MUST pass the same hash_map to both
        # sequences. See class docstring for why.
        if hash_map is None:
            hash_map = {}
        self._hash_map = hash_map
        self._trimmed_hashes: List[int] = []
        for line in self.lines:
            trimmed = line.strip()
            if trimmed not in hash_map:
                hash_map[trimmed] = len(hash_map)
            self._trimmed_hashes.append(hash_map[trimmed])

    def get_element(self, offset: int) -> int:
        return self._trimmed_hashes[offset]

    @property
    def length(self) -> int:
        return len(self._trimmed_hashes)

    def is_strongly_equal(self, offset1: int, offset2: int) -> bool:
        """Check if the ORIGINAL (untrimmed) lines are exactly equal.

        Used by heuristic optimizations to distinguish exact matches from
        whitespace-only matches. This prevents shifting diffs across lines
        that are only equal after trimming."""
        return self.lines[offset1] == self.lines[offset2]

    def get_boundary_score(self, length: int) -> int:
        """Score for splitting at position 'length'.

        Higher = better boundary. VS Code uses indentation: boundaries at
        indentation changes are preferred. For lines without indentation
        (like the test files), all positions score 1000 equally."""
        indent_before = (
            _get_indentation(self.lines[length - 1])
            if 0 < length <= len(self.lines)
            else 0
        )
        indent_after = (
            _get_indentation(self.lines[length])
            if 0 <= length < len(self.lines)
            else 0
        )
        return 1000 - (indent_before + indent_after)

    def get_text(self, rng: _OffsetRange) -> str:
        return ''.join(self.lines[rng.start:rng.end])


# ---------------------------------------------------------------------------
# Myers diff algorithm (ported from myersDiffAlgorithm.ts)
# ---------------------------------------------------------------------------


class _SnakePath:
    """A snake (diagonal run) in the Myers diff graph. Used for backtracking."""

    __slots__ = ('prev', 'x', 'y', 'length')

    def __init__(self, prev, x: int, y: int, length: int):
        self.prev = prev  # Previous SnakePath or None
        self.x = x
        self.y = y
        self.length = length


class _MyersDiff:
    """O(ND) diff algorithm with snake optimization.

    Ported from VS Code's MyersDiffAlgorithm. This is the classic Myers
    diff algorithm (Eugene W. Myers, 'An O(ND) Difference Algorithm and
    Its Variations', 1986) with the snake optimization that follows
    diagonal matches as far as possible before computing the next step.

    VS Code uses this as the fallback for large files (seq1.length +
    seq2.length >= 1700) where the O(MN) dynamic programming algorithm
    would be too slow. For small files the DP algorithm is preferred
    because it produces better results thanks to equality scoring.

    Returns a list of _SequenceDiff representing the changed regions
    (the gaps between them are equal).
    """

    def compute(self, seq1: _LineSequence, seq2: _LineSequence) -> List[_SequenceDiff]:
        if seq1.length == 0 or seq2.length == 0:
            # Trivial: entire sequences differ
            return [_SequenceDiff(
                _OffsetRange(0, seq1.length),
                _OffsetRange(0, seq2.length),
            )]

        seq_x = seq1  # x axis
        seq_y = seq2  # y axis

        def get_x_after_snake(x: int, y: int) -> int:
            """Follow diagonal matches from (x, y) as far as possible."""
            while (x < seq_x.length and y < seq_y.length and
                   seq_x.get_element(x) == seq_y.get_element(y)):
                x += 1
                y += 1
            return x

        # V[k] = x value of furthest-reaching d-path on diagonal k=x-y.
        # We use a dict to support negative k values (VS Code uses
        # FastInt32Array with separate positive/negative arrays).
        V: dict = {}
        V[0] = get_x_after_snake(0, 0)

        # Path tracking for backtracking
        paths: dict = {}
        paths[0] = None if V[0] == 0 else _SnakePath(None, 0, 0, V[0])

        d = 0
        k_final = 0

        while True:
            d += 1
            # The paper has `for (k = -d; k <= d; k += 2)`, but we can
            # ignore diagonals that cannot influence the result.
            lower_bound = -min(d, seq_y.length + (d % 2))
            upper_bound = min(d, seq_x.length + (d % 2))

            found = False
            k = lower_bound
            while k <= upper_bound:
                # maxXofDLineTop: take a vertical step (from k+1)
                max_x_top = -1 if k == upper_bound else V.get(k + 1, -1)
                # maxXofDLineLeft: take a horizontal step (from k-1, +1 x)
                max_x_left = -1 if k == lower_bound else V.get(k - 1, -1) + 1

                x = min(max(max_x_top, max_x_left), seq_x.length)
                y = x - k

                if x > seq_x.length or y > seq_y.length:
                    k += 2
                    continue

                new_max_x = get_x_after_snake(x, y)
                V[k] = new_max_x

                last_path = paths.get(k + 1) if x == max_x_top else paths.get(k - 1)
                if new_max_x != x:
                    paths[k] = _SnakePath(last_path, x, y, new_max_x - x)
                else:
                    paths[k] = last_path

                if V[k] == seq_x.length and V[k] - k == seq_y.length:
                    k_final = k
                    found = True
                    break

                k += 2

            if found:
                break

        # Backtrack to build the result list
        path = paths.get(k_final)
        result: List[_SequenceDiff] = []
        last_align_s1 = seq_x.length
        last_align_s2 = seq_y.length

        while True:
            end_x = path.x + path.length if path else 0
            end_y = path.y + path.length if path else 0

            if end_x != last_align_s1 or end_y != last_align_s2:
                result.append(_SequenceDiff(
                    _OffsetRange(end_x, last_align_s1),
                    _OffsetRange(end_y, last_align_s2),
                ))

            if not path:
                break

            last_align_s1 = path.x
            last_align_s2 = path.y
            path = path.prev

        result.reverse()
        return result


# ---------------------------------------------------------------------------
# Dynamic programming diff (ported from dynamicProgrammingDiffing.ts)
# ---------------------------------------------------------------------------


class _DynamicProgrammingDiff:
    """O(M*N) LCS-based diff with equality scoring and consecutive-diagonal
    bonus.

    Ported from VS Code's DynamicProgrammingDiffing. This is the algorithm
    that makes VS Code's diffs look good even on files with many duplicated
    lines. The two key innovations over standard LCS are:

    1. **Equality scoring**: instead of a binary 0/1 match score, each
       matching pair gets a score based on the line content:
         - Exact match (original lines equal): 1 + log(1 + line_length)
           (or 0.1 for empty lines)
         - Whitespace-only match (trimmed equal, original differs): 0.99
       This means longer exact matches are preferred over shorter ones,
       and exact matches are preferred over whitespace-only matches.

    2. **Consecutive-diagonal bonus**: when the previous cell in the DP
       table was also a diagonal (match), the current diagonal gets a
       bonus equal to the length of the consecutive diagonal run. This
       encourages the algorithm to keep runs of matching lines together
       rather than splitting them up.

    These two innovations are what make the algorithm produce WinMerge-
    style alignment on files where patience diff and difflib both fail.

    Returns a list of _SequenceDiff representing the changed regions.
    """

    def compute(
        self,
        seq1: _LineSequence,
        seq2: _LineSequence,
        equality_score: Optional[Callable[[int, int], float]] = None,
    ) -> List[_SequenceDiff]:
        if seq1.length == 0 or seq2.length == 0:
            return [_SequenceDiff(
                _OffsetRange(0, seq1.length),
                _OffsetRange(0, seq2.length),
            )]

        len1 = seq1.length
        len2 = seq2.length

        # Flat 1D arrays indexed by i + j * len1 (same as VS Code's Array2D).
        # Using flat lists instead of nested lists for better cache locality.
        # lcs_lengths: LCS score of seq1[0..i] and seq2[0..j]
        lcs_lengths = [0.0] * (len1 * len2)
        # directions: 3=diagonal(match), 1=horizontal(skip seq1), 2=vertical(skip seq2)
        directions = [0] * (len1 * len2)
        # lengths: run of consecutive diagonals ending at (i,j)
        lengths = [0] * (len1 * len2)

        def idx(i: int, j: int) -> int:
            return i + j * len1

        for s1 in range(len1):
            for s2 in range(len2):
                horizontal_len = lcs_lengths[idx(s1 - 1, s2)] if s1 > 0 else 0.0
                vertical_len = lcs_lengths[idx(s1, s2 - 1)] if s2 > 0 else 0.0

                if seq1.get_element(s1) == seq2.get_element(s2):
                    # Lines match (at least after trimming)
                    if s1 == 0 or s2 == 0:
                        extended_seq_score = 0.0
                    else:
                        extended_seq_score = lcs_lengths[idx(s1 - 1, s2 - 1)]

                    # Consecutive diagonal bonus: if the previous cell was
                    # also a diagonal, add the run length. This is the key
                    # innovation that encourages keeping runs of matching
                    # lines together.
                    if s1 > 0 and s2 > 0 and directions[idx(s1 - 1, s2 - 1)] == 3:
                        extended_seq_score += lengths[idx(s1 - 1, s2 - 1)]

                    # Add equality score (1+log(1+length) for exact, 0.99
                    # for whitespace-only, or 1 if no scoring function)
                    if equality_score:
                        extended_seq_score += equality_score(s1, s2)
                    else:
                        extended_seq_score += 1.0
                else:
                    # Lines don't match — diagonal is not possible
                    extended_seq_score = -1.0

                new_value = max(horizontal_len, vertical_len, extended_seq_score)

                # Prefer diagonals on ties (checked first), then horizontal,
                # then vertical. This matches VS Code's tie-breaking order.
                if new_value == extended_seq_score:
                    prev_len = lengths[idx(s1 - 1, s2 - 1)] if s1 > 0 and s2 > 0 else 0
                    lengths[idx(s1, s2)] = prev_len + 1
                    directions[idx(s1, s2)] = 3
                elif new_value == horizontal_len:
                    lengths[idx(s1, s2)] = 0
                    directions[idx(s1, s2)] = 1
                else:
                    lengths[idx(s1, s2)] = 0
                    directions[idx(s1, s2)] = 2

                lcs_lengths[idx(s1, s2)] = new_value

        # Backtracking: follow directions from bottom-right to top-left.
        # This mirrors VS Code's backtracking exactly, including the final
        # reportDecreasingAligningPositions(-1, -1) call after the while
        # loop. That final call with (-1, -1) is critical: it handles the
        # leading inserted/deleted lines before the first match. Without
        # it, those leading changes would be silently dropped (producing
        # a single 'equal' block covering the entire sequences, even when
        # one side has leading lines the other doesn't).
        result: List[_SequenceDiff] = []
        last_align_s1 = len1
        last_align_s2 = len2

        def report_decreasing(s1_val: int, s2_val: int):
            """Emit a diff for the gap between (s1_val, s2_val) and the
            last alignment position, then update the last alignment."""
            nonlocal last_align_s1, last_align_s2
            if s1_val + 1 != last_align_s1 or s2_val + 1 != last_align_s2:
                result.append(_SequenceDiff(
                    _OffsetRange(s1_val + 1, last_align_s1),
                    _OffsetRange(s2_val + 1, last_align_s2),
                ))
            last_align_s1 = s1_val
            last_align_s2 = s2_val

        s1 = len1 - 1
        s2 = len2 - 1
        while s1 >= 0 and s2 >= 0:
            if directions[idx(s1, s2)] == 3:
                # Diagonal: seq1[s1] matches seq2[s2]
                report_decreasing(s1, s2)
                s1 -= 1
                s2 -= 1
            elif directions[idx(s1, s2)] == 1:
                # Horizontal: skip seq1[s1]
                s1 -= 1
            else:
                # Vertical: skip seq2[s2]
                s2 -= 1

        # Final call with (-1, -1) — handles leading inserted/deleted
        # lines before the first match. This is the key step that was
        # missing in the original implementation, causing leading changes
        # to be dropped.
        report_decreasing(-1, -1)

        result.reverse()
        return result


# ---------------------------------------------------------------------------
# Heuristic optimizations (ported from heuristicSequenceOptimizations.ts)
# ---------------------------------------------------------------------------


def _join_sequence_diffs_by_shifting(
    seq1: _LineSequence,
    seq2: _LineSequence,
    diffs: List[_SequenceDiff],
) -> List[_SequenceDiff]:
    """Shift pure insertions/deletions left/right to merge with adjacent diffs.

    Ported from VS Code's joinSequenceDiffsByShifting. When a diff is a
    pure insertion (seq1_range empty) or pure deletion (seq2_range empty),
    it can be shifted within the equal region that surrounds it. If shifting
    it all the way to the left or right allows merging with the adjacent
    diff, the two diffs are joined into one.

    Example (from VS Code source):
      import { Baz, Bar } from "foo";
      <->
      import { Baz, Bar, Foo } from "foo";
      Computed: [Add "," after Bar] [Add "Foo " after space]
      Improved: [Add ", Foo" after Bar]

    This is a quality improvement, not a correctness fix. Without it the
    diff is still correct but may have unnecessary fragmentation."""

    if not diffs:
        return diffs

    # First pass: shift left and merge with previous
    result = [diffs[0]]
    for i in range(1, len(diffs)):
        prev = result[-1]
        cur = diffs[i]

        if cur.seq1_range.is_empty() or cur.seq2_range.is_empty():
            # Pure insertion or deletion — try shifting left
            length = cur.seq1_range.start - prev.seq1_range.end
            d = 1
            while d <= length:
                if (seq1.get_element(cur.seq1_range.start - d) !=
                        seq1.get_element(cur.seq1_range.end - d) or
                    seq2.get_element(cur.seq2_range.start - d) !=
                        seq2.get_element(cur.seq2_range.end - d)):
                    break
                d += 1
            d -= 1

            if d == length:
                # Can shift all the way: merge prev and cur
                result[-1] = _SequenceDiff(
                    _OffsetRange(prev.seq1_range.start,
                                 cur.seq1_range.end - length),
                    _OffsetRange(prev.seq2_range.start,
                                 cur.seq2_range.end - length),
                )
                continue

            if d > 0:
                cur = cur.delta(-d)

        result.append(cur)

    # Second pass: shift right and merge with next
    result2: List[_SequenceDiff] = []
    for i in range(len(result) - 1):
        nxt = result[i + 1]
        cur = result[i]

        if cur.seq1_range.is_empty() or cur.seq2_range.is_empty():
            length = nxt.seq1_range.start - cur.seq1_range.end
            d = 0
            while d < length:
                if (not seq1.is_strongly_equal(
                        cur.seq1_range.start + d, cur.seq1_range.end + d) or
                    not seq2.is_strongly_equal(
                        cur.seq2_range.start + d, cur.seq2_range.end + d)):
                    break
                d += 1

            if d == length:
                # Merge cur into next
                result[i + 1] = _SequenceDiff(
                    _OffsetRange(cur.seq1_range.start + length,
                                 nxt.seq1_range.end),
                    _OffsetRange(cur.seq2_range.start + length,
                                 nxt.seq2_range.end),
                )
                continue

            if d > 0:
                cur = cur.delta(d)

        result2.append(cur)

    if result:
        result2.append(result[-1])

    return result2


def _shift_diff_to_better_position(
    diff: _SequenceDiff,
    seq1: _LineSequence,
    seq2: _LineSequence,
    seq1_valid: _OffsetRange,
    seq2_valid: _OffsetRange,
) -> _SequenceDiff:
    """Shift a pure insertion/deletion to the best boundary position.

    Ported from VS Code's shiftDiffToBetterPosition. For pure insertions
    and deletions, the diff can be placed at any position within the
    surrounding equal region. This function finds the position with the
    best boundary score (based on indentation) and moves the diff there.

    This is a quality improvement that makes diffs align at natural code
    boundaries (e.g., at the start of a line rather than in the middle
    of an indentation)."""

    max_shift = 100  # Performance guard

    # How far can we shift backward?
    delta_before = 1
    while (diff.seq1_range.start - delta_before >= seq1_valid.start and
           diff.seq2_range.start - delta_before >= seq2_valid.start and
           seq2.is_strongly_equal(
               diff.seq2_range.start - delta_before,
               diff.seq2_range.end - delta_before) and
           delta_before < max_shift):
        delta_before += 1
    delta_before -= 1

    # How far can we shift forward?
    delta_after = 0
    while (diff.seq1_range.start + delta_after < seq1_valid.end and
           diff.seq2_range.end + delta_after < seq2_valid.end and
           seq2.is_strongly_equal(
               diff.seq2_range.start + delta_after,
               diff.seq2_range.end + delta_after) and
           delta_after < max_shift):
        delta_after += 1

    if delta_before == 0 and delta_after == 0:
        return diff

    # Find the shift with the best boundary score
    best_delta = 0
    best_score = -1
    for delta in range(-delta_before, delta_after + 1):
        s1_off = diff.seq1_range.start + delta
        s2_start = diff.seq2_range.start + delta
        s2_end = diff.seq2_range.end + delta
        score = (seq1.get_boundary_score(s1_off) +
                 seq2.get_boundary_score(s2_start) +
                 seq2.get_boundary_score(s2_end))
        if score > best_score:
            best_score = score
            best_delta = delta

    return diff.delta(best_delta)


def _shift_sequence_diffs(
    seq1: _LineSequence,
    seq2: _LineSequence,
    diffs: List[_SequenceDiff],
) -> List[_SequenceDiff]:
    """Shift all pure insertions/deletions to better boundary positions.

    Ported from VS Code's shiftSequenceDiffs. Iterates over all diffs and
    shifts pure insertions (seq1_range empty) and pure deletions
    (seq2_range empty) to the best boundary position within their valid
    range (between the previous and next diffs)."""

    for i in range(len(diffs)):
        prev_diff = diffs[i - 1] if i > 0 else None
        diff = diffs[i]
        next_diff = diffs[i + 1] if i + 1 < len(diffs) else None

        seq1_valid = _OffsetRange(
            prev_diff.seq1_range.end + 1 if prev_diff else 0,
            next_diff.seq1_range.start - 1 if next_diff else seq1.length,
        )
        seq2_valid = _OffsetRange(
            prev_diff.seq2_range.end + 1 if prev_diff else 0,
            next_diff.seq2_range.start - 1 if next_diff else seq2.length,
        )

        if diff.seq1_range.is_empty():
            diffs[i] = _shift_diff_to_better_position(
                diff, seq1, seq2, seq1_valid, seq2_valid)
        elif diff.seq2_range.is_empty():
            diffs[i] = _shift_diff_to_better_position(
                diff.swap(), seq2, seq1, seq2_valid, seq1_valid).swap()

    return diffs


def _optimize_sequence_diffs(
    seq1: _LineSequence,
    seq2: _LineSequence,
    diffs: List[_SequenceDiff],
) -> List[_SequenceDiff]:
    """Apply all sequence-level heuristic optimizations.

    Ported from VS Code's optimizeSequenceDiffs. Applies:
    1. joinSequenceDiffsByShifting (twice — VS Code notes calling it twice
       improves the result)
    2. shiftSequenceDiffs

    These are quality improvements that make diffs cleaner by merging
    adjacent changes and placing them at natural code boundaries."""

    result = diffs
    result = _join_sequence_diffs_by_shifting(seq1, seq2, result)
    result = _join_sequence_diffs_by_shifting(seq1, seq2, result)
    result = _shift_sequence_diffs(seq1, seq2, result)
    return result


def _remove_very_short_matching_lines_between_diffs(
    seq1: _LineSequence,
    seq2: _LineSequence,
    diffs: List[_SequenceDiff],
) -> List[_SequenceDiff]:
    """Remove very short equal blocks that sit between large diff blocks.

    Ported from VS Code's removeVeryShortMatchingLinesBetweenDiffs. When
    two large diff blocks are separated by a very short equal block (<=4
    non-whitespace characters), the equal block is absorbed into the
    surrounding diffs. This prevents tiny "islands" of equality from
    fragmenting what should be a single large change.

    Example: if you have a 10-line change, then 1 equal line (like a
    closing brace), then another 10-line change, this function merges
    them into a single 21-line change for cleaner display.

    The function iterates up to 10 times because merging can create new
    short matches that should also be removed."""

    if not diffs:
        return diffs

    counter = 0
    should_repeat = True
    while counter < 10 and should_repeat:
        should_repeat = False
        result = [diffs[0]]

        for i in range(1, len(diffs)):
            cur = diffs[i]
            last = result[-1]

            # Check if the equal block between last and cur is very short
            unchanged_start = last.seq1_range.end
            unchanged_end = cur.seq1_range.start
            unchanged_text = seq1.get_text(
                _OffsetRange(unchanged_start, unchanged_end))
            # Remove all whitespace and check length
            unchanged_text_no_ws = unchanged_text.replace(' ', '').replace(
                '\t', '').replace('\n', '').replace('\r', '')

            if (len(unchanged_text_no_ws) <= 4 and
                (last.seq1_range.length + last.seq2_range.length > 5 or
                 cur.seq1_range.length + cur.seq2_range.length > 5)):
                # Join the two diffs, absorbing the short equal block
                should_repeat = True
                result[-1] = result[-1].join(cur)
            else:
                result.append(cur)

        diffs = result
        counter += 1

    return diffs


# ---------------------------------------------------------------------------
# Main class: VS Code sequence matcher with difflib-compatible interface
# ---------------------------------------------------------------------------


class VSCodeSequenceMatcher:
    """Drop-in replacement for difflib.SequenceMatcher using VS Code's
    diffing algorithm.

    Provides get_opcodes() with the same interface as
    difflib.SequenceMatcher.get_opcodes(), returning a list of
    (tag, i1, i2, j1, j2) tuples where tag is one of
    'equal'/'replace'/'delete'/'insert'.

    Algorithm selection (same as VS Code):
    - If seq1.length + seq2.length < 1700: use Dynamic Programming
      with equality scoring and consecutive-diagonal bonus. This produces
      the best results because it considers all possible alignments and
      prefers long runs of exact matches.
    - Otherwise: use Myers diff algorithm (O(ND), faster for large files).

    After the core algorithm, heuristic optimizations are applied:
    - optimizeSequenceDiffs (shift/join adjacent diffs)
    - removeVeryShortMatchingLinesBetweenDiffs (absorb tiny equal blocks)
    """

    # VS Code's threshold for switching from DP to Myers
    _USE_DP_THRESHOLD = 1700

    def __init__(self, isjunk=None, a='', b=''):
        if isjunk is not None:
            raise NotImplementedError(
                'isjunk is not supported by VSCodeSequenceMatcher')
        self.a = a
        self.b = b
        self._dp = _DynamicProgrammingDiff()
        self._myers = _MyersDiff()

    def set_seqs(self, a, b):
        self.a = a
        self.b = b

    def set_seq1(self, a):
        self.a = a

    def set_seq2(self, b):
        self.b = b

    def _make_equality_score(self, seq1: _LineSequence, seq2: _LineSequence):
        """Create VS Code's equality scoring function.

        - Exact match (original lines equal): 1 + log(1 + line_length)
          (or 0.1 for empty lines). Longer exact matches score higher,
          so the algorithm prefers to align long matching lines.
        - Whitespace-only match (trimmed hash equal but original differs):
          0.99. This is lower than any exact match score, so exact matches
          are always preferred over whitespace-only matches.

        Note: VS Code's original code compares originalLines[offset1]
        (from seq1) with modifiedLines[offset2] (from seq2) — i.e. it
        cross-compares the two sequences' original (untrimmed) lines.
        This is NOT the same as is_strongly_equal, which compares two
        offsets within the SAME sequence."""

        def score(offset1: int, offset2: int) -> float:
            # Cross-compare: seq1's original line at offset1 vs seq2's
            # original line at offset2
            if seq1.lines[offset1] == seq2.lines[offset2]:
                # Exact match — score by line length (without newline)
                line = seq2.lines[offset2]
                line_len = len(line)
                # Strip trailing newline for length calculation to match
                # VS Code (which passes lines without newlines)
                if line_len > 0 and line[-1] == '\n':
                    line_len -= 1
                if line_len == 0:
                    return 0.1
                return 1.0 + math.log(1.0 + line_len)
            # Whitespace-only match (trimmed equal, original differs)
            return 0.99

        return score

    def get_opcodes(self) -> List[Tuple[str, int, int, int, int]]:
        """Return list of (tag, i1, i2, j1, j2) describing how to turn
        a[i1:i2] into b[j1:j2].

        Same interface as difflib.SequenceMatcher.get_opcodes()."""
        # Build a SINGLE shared hash map for both sequences, matching VS Code's
        # perfectHashes pattern. This ensures the same trimmed line gets the
        # same hash in both sequences — critical for correct comparison.
        # (If each sequence had its own hash map, 'dsds' in seq1 and 'ggg'
        # in seq2 would both get hash 0 and be wrongly treated as equal.)
        shared_hash_map: dict = {}
        seq1 = _LineSequence(self.a, shared_hash_map)
        seq2 = _LineSequence(self.b, shared_hash_map)

        # Handle trivial cases
        if seq1.length == 0 and seq2.length == 0:
            return []
        if seq1.length == 0:
            return [('insert', 0, 0, 0, seq2.length)]
        if seq2.length == 0:
            return [('delete', 0, seq1.length, 0, 0)]

        # Algorithm selection (same as VS Code):
        # DP for small files (better results), Myers for large files (faster)
        if seq1.length + seq2.length < self._USE_DP_THRESHOLD:
            diffs = self._dp.compute(
                seq1, seq2, self._make_equality_score(seq1, seq2))
        else:
            diffs = self._myers.compute(seq1, seq2)

        # Apply heuristic optimizations
        diffs = _optimize_sequence_diffs(seq1, seq2, diffs)
        diffs = _remove_very_short_matching_lines_between_diffs(seq1, seq2, diffs)

        # Convert SequenceDiff list to difflib-style opcodes
        return self._diffs_to_opcodes(diffs, seq1.length, seq2.length)

    @staticmethod
    def _diffs_to_opcodes(
        diffs: List[_SequenceDiff],
        len1: int,
        len2: int,
    ) -> List[Tuple[str, int, int, int, int]]:
        """Convert VS Code's SequenceDiff list to difflib-style opcodes.

        VS Code's SequenceDiff represents the CHANGED regions. Between
        consecutive SequenceDiffs, the sequences are equal. This function
        fills in the equal gaps and classifies each changed region as
        'replace' (both sides non-empty), 'delete' (seq2 side empty), or
        'insert' (seq1 side empty)."""

        opcodes: List[Tuple[str, int, int, int, int]] = []
        last_s1 = 0
        last_s2 = 0

        for d in diffs:
            # Equal region before this diff
            if d.seq1_range.start > last_s1 or d.seq2_range.start > last_s2:
                opcodes.append((
                    'equal',
                    last_s1, d.seq1_range.start,
                    last_s2, d.seq2_range.start,
                ))

            # The diff itself
            i1, i2 = d.seq1_range.start, d.seq1_range.end
            j1, j2 = d.seq2_range.start, d.seq2_range.end

            if i1 == i2 and j1 < j2:
                opcodes.append(('insert', i1, i2, j1, j2))
            elif i1 < i2 and j1 == j2:
                opcodes.append(('delete', i1, i2, j1, j2))
            else:
                opcodes.append(('replace', i1, i2, j1, j2))

            last_s1 = d.seq1_range.end
            last_s2 = d.seq2_range.end

        # Trailing equal region
        if last_s1 < len1 or last_s2 < len2:
            opcodes.append(('equal', last_s1, len1, last_s2, len2))

        return opcodes
