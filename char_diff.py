"""Character-level diff using WinMerge's approach: word-level Myers +
byte-level prefix/suffix refinement.

WinMerge's algorithm (Src/stringdiffs.cpp):
  1. Break each line into "words" (tokens: identifiers, whitespace,
     punctuation, numbers). Words are more unique than individual
     characters, so the O(NP) Myers preprocessing is effective.
  2. Run O(NP) Myers on the word arrays to find word-level diffs --
     but ONLY if both sides have fewer than 20480 words. This is the
     guard WinMerge uses in BuildWordDiffList() (stringdiffs.cpp
     line 398) to keep the O(NP) DP from blowing up on huge lines.
  3. For each word-level diff region, run ComputeByteDiff (stringdiffs.cpp
     line 854) which does a simple O(N) common prefix/suffix trim and
     marks the middle as the changed region.

This is much faster than running Myers directly on characters:
  - Word-level Myers runs on a small array (typically 5-50 words per
    line, not 100-500 characters)
  - Byte-level refinement is O(N) two-pointer scan, not O(N*P)

WinMerge does NOT run Myers at the byte level at all. The word-level
diff already found the alignment; the byte-level just refines the
boundaries within each word-level diff region.

This module provides:
  - char_diff(a, b): returns list of (tag, a_start, a_end, b_start, b_end)
    opcodes, same format as difflib.SequenceMatcher.get_opcodes().
"""
import re
from typing import List, Tuple

# Import at module level to avoid re-importing on every char_diff call
# (the lazy import inside the function added 11ms per call due to
# importlib overhead).
try:
    from .myers import MyersSequenceMatcher
except ImportError:
    # Allow direct import (for testing without the package)
    from myers import MyersSequenceMatcher


# WinMerge stringdiffs.cpp (BuildWordDiffList):
# WinMerge uses 20480 words per side on 64-bit builds, 2048 on 32-bit
# builds. Python has no 32-bit memory constraint, so we always use 20480.
#
# When either side meets or exceeds this threshold, the O(NP) Myers DP
# (BuildWordDiffList_DP -> onp) is skipped and a single coarse wdiff
# covering the entire line is emitted. ComputeByteDiff (our
# _compute_byte_diff) then refines that one big wdiff down to character
# boundaries with a cheap O(N) prefix/suffix trim.
#
# This is essential for very long lines (e.g. 90KB per line): such a
# line tokenizes into tens of thousands of words, and the O(NP) DP
# becomes O(N*P) where P (the edit distance) can be in the tens of
# thousands, producing billions of operations. The byte-level fallback
# is O(N) and finishes in microseconds.
WORD_DIFF_THRESHOLD = 20480


# Word tokenizer: splits into words, whitespace, and punctuation.
# This matches WinMerge's BuildWordsArray (stringdiffs.cpp line 420)
# which uses ICU break iterators. We use a regex that produces similar
# tokens: runs of word chars, runs of whitespace, and individual
# punctuation chars.
_WORD_RE = re.compile(r'\w+|\s+|[^\w\s]', re.UNICODE)


def _tokenize(s: str) -> List[str]:
    """Break a string into tokens (words, whitespace, punctuation).

    Port of WinMerge's BuildWordsArray (stringdiffs.cpp line 420).
    WinMerge uses ICU break iterators for proper Unicode handling;
    we use a regex which is simpler and fast enough for our use case.
    """
    return _WORD_RE.findall(s)


def _compute_byte_diff(a: str, b: str) -> Tuple[int, int, int, int]:
    """Find the common prefix and suffix of two strings.

    Port of WinMerge's ComputeByteDiff (stringdiffs.cpp line 854),
    simplified to ignore whitespace options (we always compare all
    characters). Returns (a_begin, a_end, b_begin, b_end) where
    [a_begin, a_end) is the changed region in a and [b_begin, b_end)
    is the changed region in b. If the strings are identical, returns
    (-1, -1, -1, -1).

    This is O(N) -- just a two-pointer scan from both ends.
    """
    len_a = len(a)
    len_b = len(b)

    if len_a == 0 and len_b == 0:
        return (-1, -1, -1, -1)
    if len_a == 0 or len_b == 0:
        return (0, len_a, 0, len_b)

    # Common prefix
    min_len = min(len_a, len_b)
    prefix = 0
    while prefix < min_len and a[prefix] == b[prefix]:
        prefix += 1

    # Common suffix
    suffix = 0
    while (suffix < min_len - prefix and
           a[len_a - 1 - suffix] == b[len_b - 1 - suffix]):
        suffix += 1

    a_begin = prefix
    a_end = len_a - suffix
    b_begin = prefix
    b_end = len_b - suffix

    if a_begin >= a_end and b_begin >= b_end:
        # No actual difference (can happen if only whitespace differs
        # in a way that prefix/suffix already covered)
        return (-1, -1, -1, -1)

    return (a_begin, a_end, b_begin, b_end)


def _char_diff_byte_level(a: str, b: str) -> List[Tuple[str, int, int, int, int]]:
    """Coarse byte-level diff for very long lines.

    This is the WinMerge fallback path taken in BuildWordDiffList()
    (stringdiffs.cpp line 405-413) when either side has >= 20480
    words: emit one big wdiff spanning both entire strings, then let
    ComputeByteDiff (our _compute_byte_diff) trim the matching prefix
    and suffix. The result is a single (equal-prefix, replace-or-
    delete-or-insert, equal-suffix) opcode list.

    This is O(N) and avoids the O(NP) Myers DP that becomes
    catastrophically slow on 90KB-per-line inputs.
    """
    a_begin, a_end, b_begin, b_end = _compute_byte_diff(a, b)
    if a_begin == -1:
        # Strings are identical after prefix/suffix trim
        return [('equal', 0, len(a), 0, len(b))]

    result: List[Tuple[str, int, int, int, int]] = []

    # Equal prefix (if any)
    if a_begin > 0 or b_begin > 0:
        result.append(('equal', 0, a_begin, 0, b_begin))

    # The changed middle region
    if a_begin < a_end and b_begin < b_end:
        result.append(('replace', a_begin, a_end, b_begin, b_end))
    elif a_begin < a_end:
        result.append(('delete', a_begin, a_end, b_begin, b_end))
    elif b_begin < b_end:
        result.append(('insert', a_begin, a_end, b_begin, b_end))

    # Equal suffix (if any)
    if a_end < len(a) or b_end < len(b):
        result.append(('equal', a_end, len(a), b_end, len(b)))

    return result


def char_diff(a: str, b: str) -> List[Tuple[str, int, int, int, int]]:
    """Compute character-level diff between two strings.

    Uses WinMerge's two-phase approach:
      1. Word-level Myers diff (via MyersSequenceMatcher) to find
         word-level alignment -- BUT only when both sides have fewer
         than WORD_DIFF_THRESHOLD (20480) words. This is the gate
         WinMerge uses (stringdiffs.cpp line 398) to keep the O(NP)
         DP from blowing up on huge lines.
      2. For each word-level diff region, byte-level prefix/suffix trim
         to find the exact character boundaries.
      3. Fallback (huge lines): skip step 1, run a single byte-level
         prefix/suffix trim on the whole strings. This produces a
         single (equal, replace, equal) result -- less granular than
         the word-aligned path but O(N) and always fast.

    Returns a list of (tag, a_start, a_end, b_start, b_end) opcodes
    where tag is 'equal', 'delete', 'insert', or 'replace'. Same format
    as difflib.SequenceMatcher.get_opcodes().

    This is much faster than running Myers directly on characters:
    - Word-level Myers runs on a small array (5-50 tokens)
    - Byte-level refinement is O(N) per diff region
    - Total: O(N) for typical lines, vs O(N*P) for char-level Myers
    - For huge lines (>= 20480 tokens) the byte-level fallback is O(N)
      end-to-end and never enters the O(NP) DP
    """
    if a == b:
        return [('equal', 0, len(a), 0, len(b))]

    # Tokenize both strings into words
    tokens_a = _tokenize(a)
    tokens_b = _tokenize(b)

    # WinMerge's 20480-word threshold gate (stringdiffs.cpp line 398).
    # When either side has too many tokens, skip the O(NP) Myers DP
    # and fall back to a single byte-level prefix/suffix trim.
    # This is the key fix for slow char compare on 90KB-per-line files:
    # such lines tokenize into tens of thousands of words, which makes
    # the O(NP) DP O(N*P) with both N and P in the tens of thousands
    # -- billions of operations. The byte-level fallback is O(N) and
    # finishes in microseconds.
    if (len(tokens_a) >= WORD_DIFF_THRESHOLD or
            len(tokens_b) >= WORD_DIFF_THRESHOLD):
        return _char_diff_byte_level(a, b)

    # Build offset arrays: token_offsets[i] = char offset where token i starts
    offsets_a = []
    pos = 0
    for tok in tokens_a:
        offsets_a.append(pos)
        pos += len(tok)

    offsets_b = []
    pos = 0
    for tok in tokens_b:
        offsets_b.append(pos)
        pos += len(tok)

    # Run word-level diff using MyersSequenceMatcher (the base class,
    # not InlineMyersSequenceMatcher). The base class works on any
    # hashable sequence (including lists of token strings), while
    # InlineMyersSequenceMatcher's k-mer preprocessing assumes the
    # elements are characters (it does a[i:i+3] which produces a list
    # slice, not a hashable k-mer). For word-level diffing, the base
    # class's 1-element preprocessing is appropriate because words are
    # usually unique enough (unlike individual characters).
    matcher = MyersSequenceMatcher(None, tokens_a, tokens_b)
    word_ops = matcher.get_opcodes()

    # For each word-level opcode, generate char-level opcodes
    result = []
    for tag, wa_start, wa_end, wb_start, wb_end in word_ops:
        if tag == 'equal':
            # Tokens are identical -- emit as equal at char level
            a_start = offsets_a[wa_start] if wa_start < len(offsets_a) else len(a)
            a_end = offsets_a[wa_end] if wa_end < len(offsets_a) else len(a)
            b_start = offsets_b[wb_start] if wb_start < len(offsets_b) else len(b)
            b_end = offsets_b[wb_end] if wb_end < len(offsets_b) else len(b)
            if a_start < a_end or b_start < b_end:
                result.append(('equal', a_start, a_end, b_start, b_end))
        else:
            # Tokens differ -- get the char region for this word range
            a_start = offsets_a[wa_start] if wa_start < len(offsets_a) else len(a)
            a_end = offsets_a[wa_end] if wa_end < len(offsets_a) else len(a)
            b_start = offsets_b[wb_start] if wb_start < len(offsets_b) else len(b)
            b_end = offsets_b[wb_end] if wb_end < len(offsets_b) else len(b)

            a_slice = a[a_start:a_end]
            b_slice = b[b_start:b_end]

            # Byte-level prefix/suffix trim (WinMerge's ComputeByteDiff)
            a_begin_rel, a_end_rel, b_begin_rel, b_end_rel = _compute_byte_diff(a_slice, b_slice)

            if a_begin_rel == -1:
                # No visible diff after prefix/suffix trim
                continue

            # Emit equal prefix
            if a_begin_rel > 0 or b_begin_rel > 0:
                result.append(('equal',
                               a_start, a_start + a_begin_rel,
                               b_start, b_start + b_begin_rel))

            # Emit the changed region
            a_change_start = a_start + a_begin_rel
            a_change_end = a_start + a_end_rel
            b_change_start = b_start + b_begin_rel
            b_change_end = b_start + b_end_rel

            if a_change_start < a_change_end and b_change_start < b_change_end:
                result.append(('replace', a_change_start, a_change_end,
                               b_change_start, b_change_end))
            elif a_change_start < a_change_end:
                result.append(('delete', a_change_start, a_change_end,
                               b_change_start, b_change_end))
            elif b_change_start < b_change_end:
                result.append(('insert', a_change_start, a_change_end,
                               b_change_start, b_change_end))

            # Emit equal suffix
            if a_end_rel < len(a_slice) or b_end_rel < len(b_slice):
                result.append(('equal',
                               a_start + a_end_rel, a_end,
                               b_start + b_end_rel, b_end))

    return result
