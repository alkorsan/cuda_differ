"""Opcode realignment -- the shared post-pass every diff engine's opcode
list goes through (native and pure-Python alike) before any consumer
walks it.

WHAT THIS IS

Two structural normalizations applied to a finished difflib-style
opcode list [(tag, i1, i2, j1, j2), ...]:

Pass 1 -- merge INSERT+EQUAL(trivial)+DELETE (or DELETE+EQUAL(trivial)+
INSERT) into a single REPLACE. Fixes the LCS tie-breaking artifact where
an engine matches a trivial line (empty, whitespace, a lone '}' ...)
instead of a meaningful one, making identical content render as one
added line + one deleted line instead of a paired change.

Pass 2 -- absorb a short EQUAL block (<= TRIVIAL_THRESHOLD non-
whitespace characters) that sits between two changed blocks into one
REPLACE, when at least one of the two changed blocks is large
(>= MIN_LARGE_REPLACE combined lines). Prevents an engine from
fragmenting one big changed region into several pieces by matching
isolated trivial lines (blank lines, closing braces) inside it; the
fragments break positional pairing (a line gets paired with the wrong
line of the other file) and make the diff read as several unrelated
changes instead of one.

Both are ported from VS Code's heuristicSequenceOptimizations.ts
(removeVeryShortMatchingLinesBetweenDiffs and the adjacent-change joins
of optimizeSequenceDiffs); VS Code runs them UNCONDITIONALLY inside its
diff algorithm, which is why its output never contains these patterns.

WHY IT RUNS ALWAYS (and is NOT part of 'beautify_alignment')

'beautify_alignment' and this pass live on different layers and must
not be coupled:

  * beautify_alignment chooses how lines INSIDE one replace block are
    paired for display (similarity-anchored pairing vs positional
    top-down). It is a rendering choice, off by default.
  * realign_opcodes normalizes the STRUCTURE of the opcode stream
    itself -- which lines belong to which hunk. It is diff quality,
    not presentation.

Measurements on the plugin's whole _dev/__tests corpus (pattern
occurrences per engine, Pass1/Pass2):

  difflib 2/22   myers 1/61   hybrid 1/3   patience 1/3
  vscode 0 on the corpus files, but crafted small inputs DO make its
  DP path emit Pass-1 patterns -- so no engine is exempt.
  native GNU-Myers 1/91   native JGit-Histogram 1/101
  native JGit-Myers 3/62

(The long-standing comment claiming patience never produces these
patterns was wrong -- it does, 1x Pass 1 + 3x Pass 2 on the corpus.)
Because beautify_alignment defaults to OFF, moving this pass under it
would re-introduce, in the default configuration, the very artifacts it
exists to fix. Every engine -- native included -- goes through the same
normalization so that switching algorithms changes tie-breaking, not
hunk structure.

THE 'ignore' BARRIER

The native engine can re-tag all-blank hunks as 'ignore'
(DIFF_IGN_BLANK_LINES): suppressed differences the UI paints as
ignored, not as changes. An 'ignore' hunk is a BARRIER for Pass 2:
merging across one would resurrect the suppressed lines into a shown
REPLACE. Pass 1 is inherently safe (it only touches insert/delete
neighbors). Pure-Python engines never emit 'ignore' today; the guard
is there because both differs share this one implementation.

INVARIANTS

  * Idempotent: realign_opcodes(a, realign_opcodes(a, ops)) ==
    realign_opcodes(a, ops). Both passes iterate to a fixpoint.
  * Coverage-preserving: opcodes keep tiling a[0:len(a)] and
    b[0:len(b)] exactly; only tag/range boundaries move.
  * Only EQUAL blocks whose total non-whitespace content is <= 4
    characters are ever absorbed, so a meaningful matched line (a real
    statement, an identifier) is never merged away. This is what keeps
    the plugin's test-suite expectation "these empty lines must align
    and not be absorbed" true wherever the surrounding equal block
    carries real content.
"""

# Threshold for the "trivial equal block" check: an EQUAL block with at
# most this many non-whitespace characters total can be absorbed. The
# value 4 matches VS Code's removeVeryShortMatchingLinesBetweenDiffs.
TRIVIAL_THRESHOLD = 4

# Minimum combined size ((i2-i1) + (j2-j1) lines) for a changed block to
# count as "large enough" to absorb a short EQUAL block between it and
# the next changed block. Matches VS Code's
# before.seq1Range.length + before.seq2Range.length > 5
# ("> 5" and ">= 6" are the same test; written as a named constant).
MIN_LARGE_REPLACE = 6


def _non_ws_len(a, i1, i2):
    """Count non-whitespace characters in the joined text a[i1:i2].

    Whitespace = space, tab, CR, LF -- the same set VS Code strips in
    removeVeryShortMatchingLinesBetweenDiffs. 'a' is the keepends line
    list of the LEFT file, so terminators are part of the text and must
    be stripped here.
    """
    text = ''.join(a[i1:i2])
    for ch in (' ', '\t', '\n', '\r'):
        text = text.replace(ch, '')
    return len(text)


def realign_opcodes(a, opcodes):
    """Post-process an opcode list into its normalized form.

    Args:
        a: the left file's line list (keepends), used ONLY to read the
            EQUAL blocks' text for the trivial-content check.
        opcodes: difflib-style (tag, i1, i2, j1, j2) tuples from any
            engine. Tags: 'equal' / 'delete' / 'insert' / 'replace' /
            'ignore' (the native engine's suppressed all-blank hunks).

    Returns:
        A NEW list with the same opcodes merged where the two passes
        apply (the input list is never mutated; identity is returned
        for lists too short to contain any pattern). See the module
        docstring for the transformation semantics.
    """
    if len(opcodes) < 3:
        return opcodes
    result = list(opcodes)

    # ------------------------------------------------------------------
    # Pass 1: merge INSERT+EQUAL(trivial)+DELETE (or the mirrored
    # DELETE+EQUAL(trivial)+INSERT) into one REPLACE.
    #
    # The pattern means: the engine matched a trivial line instead of a
    # meaningful one, so identical lines around it render as one added
    # + one deleted instead of a paired change. Merging the three
    # opcodes into a single REPLACE lets the replace-block pairing
    # (positional or beautified) match the identical lines naturally.
    #
    # The back-step after a merge (i -= 1) re-examines the triple that
    # ends at the newly created REPLACE, so cascades of adjacent
    # patterns collapse left-to-right to a fixpoint in one sweep.
    # ------------------------------------------------------------------
    i = 1
    while i < len(result) - 1:
        prev = result[i - 1]
        cur = result[i]
        nxt = result[i + 1]
        if (cur[0] == 'equal' and
                prev[0] in ('insert', 'delete') and
                nxt[0] in ('insert', 'delete') and
                prev[0] != nxt[0]):
            if _non_ws_len(a, cur[1], cur[2]) <= TRIVIAL_THRESHOLD:
                merged = ('replace',
                          prev[1], nxt[2],
                          prev[3], nxt[4])
                result[i - 1:i + 2] = [merged]
                if i > 1:
                    i -= 1
                continue
        i += 1

    # ------------------------------------------------------------------
    # Pass 2: absorb a short trivial EQUAL block between two changed
    # blocks into a single REPLACE -- VS Code's
    # removeVeryShortMatchingLinesBetweenDiffs. Requires at least one
    # of the two neighbors to be large, so two tiny changes separated
    # by a matched blank line stay separate (VS Code's own guard).
    #
    # 'ignore' hunks are BARRIERS: merging across a suppressed all-blank
    # hunk would resurrect it into a shown REPLACE, undoing
    # DIFF_IGN_BLANK_LINES. Both neighbors must be real changed blocks
    # ('replace' / 'insert' / 'delete').
    #
    # Iterates to a fixpoint (like VS Code's "repeat up to 10 times"):
    # a merge can bring two previously separated changed blocks next to
    # a new short EQUAL, which must be absorbed in a following sweep.
    # ------------------------------------------------------------------
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
                    prev[0] in ('replace', 'insert', 'delete') and
                    nxt[0] in ('replace', 'insert', 'delete')):
                if _non_ws_len(a, cur[1], cur[2]) <= TRIVIAL_THRESHOLD:
                    prev_size = (prev[2] - prev[1]) + (prev[4] - prev[3])
                    nxt_size = (nxt[2] - nxt[1]) + (nxt[4] - nxt[3])
                    if prev_size >= MIN_LARGE_REPLACE or \
                            nxt_size >= MIN_LARGE_REPLACE:
                        merged = ('replace',
                                  prev[1], nxt[2],
                                  prev[3], nxt[4])
                        result[i - 1:i + 2] = [merged]
                        changed = True
                        if i > 1:
                            i -= 1
                        continue
            i += 1

    return result
