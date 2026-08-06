"""Character-level diff using WinMerge's approach: word-level Myers +
byte-level prefix/suffix refinement.

WinMerge's algorithm (Src/stringdiffs.cpp):
  1. Break each line into "words" (tokens: identifiers, whitespace,
     punctuation, numbers). Words are more unique than individual
     characters, so the O(NP) Myers preprocessing is effective.
  2. Run O(NP) Myers on the word arrays to find word-level diffs.
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


def char_diff(a: str, b: str) -> List[Tuple[str, int, int, int, int]]:
    """Compute character-level diff between two strings.

    Uses WinMerge's two-phase approach:
      1. Word-level Myers diff (via InlineMyersSequenceMatcher) to find
         word-level alignment.
      2. For each word-level diff region, byte-level prefix/suffix trim
         to find the exact character boundaries.

    Returns a list of (tag, a_start, a_end, b_start, b_end) opcodes
    where tag is 'equal', 'delete', 'insert', or 'replace'. Same format
    as difflib.SequenceMatcher.get_opcodes().

    This is much faster than running Myers directly on characters:
    - Word-level Myers runs on a small array (5-50 tokens)
    - Byte-level refinement is O(N) per diff region
    - Total: O(N) for typical lines, vs O(N*P) for char-level Myers
    """
    if a == b:
        return [('equal', 0, len(a), 0, len(b))]

    # Tokenize both strings into words
    tokens_a = _tokenize(a)
    tokens_b = _tokenize(b)

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
