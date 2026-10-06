"""Joined-block mapping -- the fast engine of align_by_similarity.

Replaces the old recursive anchor search (_find_best_pairs_events, the
functions this module was extracted from) as the pairing strategy the
beautify mode uses inside unequal-count REPLACE blocks. The old code
was pure Python and quadratic:

    step1  Counter-based unique exact-match anchor search, O(N+M) per
           RECURSION LEVEL (the Counters were rebuilt from scratch at
           every level, so a block anchored line by line paid O(N+M)
           per anchor -- O(N*(N+M)) overall);
    step2  prefix/suffix similarity scoring, O(N*M) comparisons of
           line pairs per level, each comparison O(line_length) --
           9.0s self on the 33k-line HTML pair (test_4a/test_4b),
           against 130ms for the WHOLE native line diff of the same
           files; 14.3M len() calls under cProfile.

THE JOINED-BLOCK IDEA: the slow part of the old pass was searching for
which lines to pair. But finding the best monotone mapping between two
line sequences IS A LINE DIFF -- the exact job the diff engine already
does at native speed. So instead of searching in Python, JOIN the two
sides of the block back into two small texts and hand them to the diff
engine in ONE call: the returned opcodes ARE the pairing (EQUAL ranges
= paired lines, DELETE/INSERT ranges = unpaired lines, REPLACE ranges
= residuals to handle recursively). The Python side never scans line
pairs at all; it only translates opcodes into plan ops, O(N+M) per
block with tiny constants.

Why the re-diff finds pairs the top-level diff "missed": a REPLACE
block means the engine matched nothing ACROSS that range in the global
alignment -- not that the block's sides share no lines. Heuristic
engines (GNU-diffutils Myers with the Eggert heuristic, JGit histogram)
produce non-minimal scripts on large files that strand matchable lines
inside replace regions, and the 'absorb_trivial_equal_blocks' option
DELIBERATELY merges trivial EQUAL blocks into their replace neighbors
(that pass exists precisely so the pairing pass can re-discover them).
The joined-block call re-runs the engine with full freedom inside the
block and finds every one of those anchors in one shot, monotonically,
at engine speed.

THE KEY CASCADE (one engine call per level, three levels max):

    depth 0   raw lines          (keys = the keepends lines verbatim)
    depth 1   stripped lines     (keys = line.strip() -- catches
                                  re-indentation / trailing-space
                                  churn: the #1 real-world case the old
                                  step2 prefix scoring used to catch)
    depth 2   prefix keys        (keys = stripped line truncated to
                                  PREFIX_KEY_LEN chars -- catches
                                  same-structure lines like markup /
                                  JSON / log lines that differ beyond
                                  the prefix)
    depth 3   positional tail    (pair the first min(da, db) lines,
                                  leftovers against a gap -- the
                                  algo-faithful rendering; bounded,
                                  monotone, honest)

Each level only runs on the REPLACE residuals the previous level left
behind, so the total engine work is proportional to the block sizes,
and every level strictly either shrinks the problem or advances the
depth (a level that matches nothing returns one whole-block REPLACE
and the next level takes over) -- the recursion always terminates.

SMALL BLOCKS keep the ORIGINAL algorithm (ported verbatim below as
_small_block_plan_into): for da*db <= SMALL_PRODUCT the old search is
microseconds, and it pairs similar-but-unequal lines
('color: red;' vs 'color: blue;') that no exact-key cascade can catch.
The quadratic blowup only ever came from LARGE blocks; those now go to
the engine. This keeps the beautify output of the common small-block
cases IDENTICAL to the old implementation.

THE PLAN PROTOCOL: align_block_plan() returns a flat list of primitive
ops describing the block's pairing -- no events, no engine calls, no
mode decisions. The Differ translates a plan into paint events with
its own mode-aware emitter (collect / replay / legacy, mirroring
_positional_pairs_events). Because a plan is a pure function of
(a, b, opcodes, config), the native Differ computes plans ONCE in the
COLLECT pass and reuses them in the REPLAY pass (the same
collect/replay invariant the batched char diff already relies on) --
the whole search runs once per compare, not twice.

    ('P', ai, bj)        pair line ai (A) with line bj (B); whether
                         the pair is EQUAL (ALIGN) or CHANGED (char
                         diff) is decided by the EMITTER comparing the
                         raw lines -- flags-affected engine EQUAL
                         ranges may still be raw-different, and those
                         must go through the char diff
    ('D', i1, i2, jy)    A-only lines [i1, i2): one B-side gap of
                         height i2-i1 inserted after line jy-1
    ('I', j1, j2, iy)    B-only lines [j1, j2): one A-side gap of
                         height j2-j1 inserted after line iy-1
    ('Q', i1, i2, j1, j2)
                         ignored hunk (the native engine's 'ignore'
                         tag under DIFF_IGN_BLANK_LINES): rendered by
                         the emitter exactly like compare_lists's
                         top-level 'ignore' branch (ignored lines +
                         ignored gap, no pairs, no diffmap)

Gap events use ONE range-gap per run (the top-level delete/insert
branches' form), not the old per-line gaps: the paint consumer stacks
gap heights at the same position, so (B_GAP, y, i, i+3) paints exactly
what three (B_GAP, y, i+k, i+k+1) painted -- with 1 event instead of
3.

PROFILING: every engine call is timed and booked as one mark under
'align_by_similarity:joined_block_sub_diff' (the report's 'Beautify
passes' block); the small-block scorer keeps the historical rows
'align_by_similarity:step1_exact_match_search' /
'align_by_similarity:step2_prefix_suffix_search'. The marks run in
every mode (like the old step marks): during the COLLECT pass they
nest under the caller's 'compare:collect_pairs' section.

ENGINE AGNOSTIC: this module never imports cudatext. The Differ
supplies two callables (see AlignEngine):

    sub_diff(lines_a, lines_b)       depth 0 -- RAW keepends lines;
    sub_diff_keys(keys_a, keys_b)    depth 1+ -- TERMINATOR-FREE key
                                     strings;

each returns difflib-style opcodes over the given lists, or None when
no engine is available (the caller then gets the positional tail).
The native Differ bridges them to cudatext.diff_proc(DIF_TEXTS) (raw
keys keep their terminators, so ''.join reconstructs the sub-text
byte-for-byte; stripped keys carry none, so the adapter joins with
'\\n' plus a trailing separator -- see its docstring); the Python
Differ builds its configured SequenceMatcher straight on the key
lists (the strings are opaque to a matcher, terminators included).
"""

import time
from collections import Counter

from .profiling import Profiler

# Profiling row of the joined-block engine calls (one mark per call;
# the report's 'Beautify passes' block prints it between the umbrella
# and the step rows).
ROW_SUB_DIFF = 'align_by_similarity:joined_block_sub_diff'

# Blocks with da*db <= SMALL_PRODUCT keep the ORIGINAL recursive
# search (ported below): at this size the Python scan is microseconds
# and pairs similar-but-unequal lines no exact-key pass can catch.
# 256 == 16x16 lines; tune here if the profile shows the small-block
# scans accumulating on your corpus (the row to watch is step2).
SMALL_PRODUCT = 256

# Length of the depth-2 prefix keys (chars of the stripped line).
# 12 is enough to separate markup/JSON/log structure ('<div class=',
# '"settings":', '2026-10-06 INFO') while still bucketing lines that
# belong together. Shorter = more pairing (looser), longer = stricter.
PREFIX_KEY_LEN = 12

# Depth budget of the cascade: depth 0 raw, 1 stripped, 2 prefix keys;
# depth >= MAX_DEPTH renders the positional tail. A level that matches
# nothing returns one whole-block REPLACE, so every recursion either
# shrinks the block or burns one level -- termination is guaranteed.
MAX_DEPTH = 3


class AlignEngine:
    """Bundle of the two engine bridges + the cascade's tunables.

    Constructed by the Differ per aligned block (two bound-method refs
    and three ints -- allocation is noise next to the first engine
    call). sub_diff / sub_diff_keys are callables
    (list, list) -> opcodes-or-None; see the module docstring for the
    key contracts of each.
    """

    __slots__ = ('sub_diff', 'sub_diff_keys', 'small_product',
                 'prefix_key_len', 'max_depth')

    def __init__(self, sub_diff, sub_diff_keys,
                 small_product=SMALL_PRODUCT,
                 prefix_key_len=PREFIX_KEY_LEN,
                 max_depth=MAX_DEPTH):
        self.sub_diff = sub_diff
        self.sub_diff_keys = sub_diff_keys
        self.small_product = small_product
        self.prefix_key_len = prefix_key_len
        self.max_depth = max_depth

    def call(self, keys_a, keys_b, depth):
        """Run the engine bridge of this depth and book the mark.

        Returns the opcode list (difflib-style tuples over the two key
        lists), or None when the bridge reported no engine -- the
        cascade then falls back to the positional tail instead of
        risking a quadratic Python search on a block that might be
        huge.
        """
        bridge = self.sub_diff if depth == 0 else self.sub_diff_keys
        if Profiler.enabled:
            t0 = time.perf_counter()
            ops = bridge(keys_a, keys_b)
            Profiler.mark(ROW_SUB_DIFF, time.perf_counter() - t0)
        else:
            ops = bridge(keys_a, keys_b)
        return ops


def align_block_plan(a, alo, ahi, b, blo, bhi, eng):
    """Compute the alignment plan for ONE unequal-count REPLACE block:
    a[alo:ahi] vs b[blo:bhi], both non-empty. Returns the list of plan
    ops (see the module docstring). Pure function of its arguments --
    no engine state, no Differ state, no events; the caller (the
    COLLECT pass) may cache and reuse it verbatim in the REPLAY pass.
    """
    plan = []
    _plan_into(plan, a, alo, ahi, b, blo, bhi, eng, 0)
    return plan


def _plan_into(plan, a, alo, ahi, b, blo, bhi, eng, depth):
    """align_block_plan's recursion (plan ops are appended in order).

    Dispatch order (first match wins):
      1. one side empty        -> one range-gap op (the original's
                                  degenerate-branch rendering)
      2. da*db <= small_product-> the original recursive search
                                  (_small_block_plan_into)
      3. depth >= max_depth    -> positional tail (algo-faithful
                                  rendering, plus the original's 1xN
                                  opposing-line triviality rule)
      4. otherwise             -> ONE engine call on this depth's keys
                                  and recursion into its REPLACE
                                  residuals
    """
    da = ahi - alo
    db = bhi - blo
    if da == 0:
        if db:
            plan.append(('I', blo, bhi, alo))
        return
    if db == 0:
        if da:
            plan.append(('D', alo, ahi, blo))
        return
    if da * db <= eng.small_product:
        _small_block_plan_into(plan, a, alo, ahi, b, blo, bhi)
        return
    if depth >= eng.max_depth:
        _positional_tail_plan(plan, a, alo, ahi, b, blo, bhi)
        return

    if depth == 0:
        # RAW pass: the keepends lines themselves are the keys -- the
        # native bridge's ''.join then reconstructs the sub-text
        # byte-for-byte (every line carries its own terminator).
        keys_a = a[alo:ahi]
        keys_b = b[blo:bhi]
    else:
        keys_a, keys_b = _depth_keys(a, alo, ahi, b, blo, bhi,
                                     depth, eng)
    sub = eng.call(keys_a, keys_b, depth)
    if sub is None:
        # No engine (defensive: the native Differ is only used when
        # the engine exists, but a test / fallback instantiation must
        # not explode or go quadratic on a huge block).
        _positional_tail_plan(plan, a, alo, ahi, b, blo, bhi)
        return
    if not sub:
        # Engine returned nothing usable: treat as one whole-block
        # REPLACE so the NEXT depth (or the tail) takes over.
        sub = (('replace', 0, da, 0, db),)

    for tag, i1, i2, j1, j2 in sub:
        if tag == 'equal':
            # Equal per the ENGINE (raw or per the ignore flags): the
            # emitter still raw-compares each pair -- a flags-equal
            # pair of raw-different lines must run the char diff.
            ai = alo + i1
            bj = blo + j1
            for k in range(i2 - i1):
                plan.append(('P', ai + k, bj + k))
        elif tag == 'delete':
            plan.append(('D', alo + i1, alo + i2, blo + j1))
        elif tag == 'insert':
            plan.append(('I', blo + j1, blo + j2, alo + i1))
        elif tag == 'ignore':
            # Suppressed all-blank hunk inside the block (native
            # DIFF_IGN_BLANK_LINES): hand the whole hunk to the
            # emitter as an ignored range -- rendering it as regular
            # del/add pairs would resurrect lines the user asked to
            # ignore (the same barrier rule the absorb pass enforces).
            plan.append(('Q', alo + i1, alo + i2, blo + j1, blo + j2))
        else:
            # 'replace' (any unknown tag defensively included): both
            # sides non-empty -> recurse one depth deeper; a one-sided
            # residual cannot happen from a well-formed engine (pure
            # delete/insert arrive as their own tags) but is rendered
            # directly if it ever does.
            if i2 > i1 and j2 > j1:
                _plan_into(plan, a, alo + i1, alo + i2,
                           b, blo + j1, blo + j2, eng, depth + 1)
            elif i2 > i1:
                plan.append(('D', alo + i1, alo + i2, blo + j1))
            elif j2 > j1:
                plan.append(('I', blo + j1, blo + j2, alo + i1))


def _depth_keys(a, alo, ahi, b, blo, bhi, depth, eng):
    """Key lists of a normalized/prefix pass (depth >= 1).

    Keys are TERMINATOR-FREE (line.strip() removes the keepends
    terminator along with the surrounding whitespace), so no key ever
    contains CR/LF and the native bridge can join them with a
    separator unambiguously. Stripping is recomputed from the raw
    lines at every level (never chained key-of-key): depth 2 keys must
    stay index-aligned with the raw lines, which they are -- one key
    per line, same order, same count.
    """
    if depth == 1:
        ka = [line.strip() for line in a[alo:ahi]]
        kb = [line.strip() for line in b[blo:bhi]]
    else:
        n = eng.prefix_key_len
        ka = [line.strip()[:n] for line in a[alo:ahi]]
        kb = [line.strip()[:n] for line in b[blo:bhi]]
    return ka, kb


def _positional_tail_plan(plan, a, alo, ahi, b, blo, bhi):
    """Cascade-exhausted fallback: the algo-faithful rendering.

    Pair the first min(da, db) lines top-down, leftovers on the longer
    side against a gap at the bottom of the shorter side's block --
    exactly the events _replace_block_chunks' positional path emits
    (gap y = the shorter side's block end). Plus the original pass's
    1xN rule: when one side is a single line, pair it with the first
    opposing line ONLY if that line is non-trivial (>= 3 non-ws chars)
    -- otherwise the lines are strangers and render as separate
    delete + add (VS Code's behavior the old threshold enforced).
    """
    da = ahi - alo
    db = bhi - blo
    if da == 1 or db == 1:
        if da == 1:
            opp = b[blo]
        else:
            opp = a[alo]
        opp = opp.replace(' ', '').replace('\t', '')
        opp = opp.replace('\n', '').replace('\r', '')
        if len(opp) < 3:
            # Trivial opposing line: nothing here is similar enough to
            # pair -- the block's old threshold-fail rendering.
            plan.append(('D', alo, ahi, blo))
            plan.append(('I', blo, bhi, ahi))
            return
    common = da if da < db else db
    for k in range(common):
        plan.append(('P', alo + k, blo + k))
    if da > common:
        plan.append(('D', alo + common, ahi, bhi))
    elif db > common:
        plan.append(('I', blo + common, bhi, ahi))


def _small_block_plan_into(plan, a, alo, ahi, b, blo, bhi):
    """The ORIGINAL _find_best_pairs_events, ported verbatim to plan
    ops (small blocks only -- the caller enforces da*db <=
    eng.small_product, and every recursion below only shrinks the
    ranges, so the whole subtree stays small).

    Kept byte-for-byte in DECISIONS (anchor choice, scoring, ordering,
    thresholds, the 1xN opposing-line rule) so the beautify output of
    small blocks is IDENTICAL to the old implementation; only the
    OUTPUT layer changed (plan ops instead of paint events, and the
    threshold-fail / degenerate renderings emit ONE range-gap per side
    instead of per-line gaps -- the paint consumer stacks gap heights
    at the same position, so the painted view is the same).

    Profiling: same rows as before, booked with the same mark() calls
    at the same points (the searches are the pass's STEPS in the
    report's 'Beautify passes' block).
    """
    da, db = ahi - alo, bhi - blo
    if da == 0:
        if db:
            plan.append(('I', blo, bhi, alo))
        return
    if db == 0:
        if da:
            plan.append(('D', alo, ahi, blo))
        return

    # Find the best-matching pair by char-level similarity.
    # First pass: find all unique exact matches and pick the longest.
    # O(N+M) via the Counter-based uniqueness check.
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
        Profiler.mark('align_by_similarity:step1_exact_match_search',
                      time.perf_counter() - _t0)

    best_score = -1
    best_prefix = 0
    best_i, best_j = alo, blo

    if best_exact_i >= 0:
        # Use the longest unique exact match as anchor
        best_i, best_j = best_exact_i, best_exact_j
        best_score = 1000000
        best_prefix = 1000000
    else:
        # No unique exact match -- prefix/suffix length scoring.
        # NOTE: O(N*M) per block (each comparison O(line_length)) --
        # harmless at the caller's size cap, the reason the OLD
        # unbounded version was slow on large files.
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
            Profiler.mark('align_by_similarity:step2_prefix_suffix_search',
                          time.perf_counter() - _t0)

    # Minimum similarity threshold: only pair lines if the BEST pair
    # shares a meaningful common prefix (>= 3 chars) -- VS Code's
    # behavior; unrelated lines show as separate delete + add.
    # Exception: 1xN/Nx1 blocks pair when the opposing first line is
    # non-trivial (>= 3 non-whitespace chars) -- note the pair then
    # used is the search's best (best_i/best_j), which for a 1xN block
    # is the most-similar opposing line, not necessarily the first.
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
                # Non-trivial opposing line: pair the best match
                pass
            else:
                # Trivial opposing line: show as delete + add
                plan.append(('D', alo, ahi, blo))
                plan.append(('I', blo, bhi, ahi))
                return
        else:
            # Multi-line block with no good match: show all as
            # separate delete + add
            plan.append(('D', alo, ahi, blo))
            plan.append(('I', blo, bhi, ahi))
            return

    # Recurse on the part before the best pair
    _small_block_plan_into(plan, a, alo, best_i, b, blo, best_j)

    # The best pair itself
    plan.append(('P', best_i, best_j))

    # Recurse on the part after the best pair
    _small_block_plan_into(plan, a, best_i + 1, ahi,
                           b, best_j + 1, bhi)
