"""Differ module for Python algorithms (hybrid, myers, vscode, patience, difflib).

This file is self-contained: it contains everything needed to run the
pure-Python diff algorithms. It does NOT depend on differ_native.py.

For native algorithms (native_histogram, native_myers), use
differ_native.py instead — the native engine is 10-30x faster.

Code is intentionally duplicated from differ_native.py to allow
independent evolution of the native and Python codepaths -- no shared
diff-logic modules (the two copies of _absorb_trivial_equal_blocks at
the end of each file are deliberate duplicates for the same reason).
ONE exception: the beautify mode's line ALIGNMENT lives in the shared
align_joined.py -- it is engine-agnostic by construction (the engine
bridges are injected), so duplicating it would only invite drift
between the native and Python pairing behavior. The legacy SLOW
beautify (align_by_similarity2 -> _find_best_pairs_events, the
original pure-Python recursive search) is kept here verbatim, so both
beautify methods exist in BOTH Differ classes and can be A/B-switched
from the toolbar.

Alignment modes (self.align_by_similarity / self.align_by_similarity2
-- mutually exclusive, align_by_similarity2 wins if both are set):
  align_by_similarity = True  = FAST 'beautified' alignment: similar
          lines inside a changed block are re-paired (VS Code-like).
          Uses the joined-block mapper of align_joined.py: the
          configured Python engine sub-diffs the block's sides (raw
          keys -> stripped keys -> prefix keys), the original recursive
          search only for small residuals.
  align_by_similarity2 = True = the OLD SLOW 'beautified' alignment:
          the original pure-Python recursive search
          (_find_best_pairs_events: longest unique exact-match anchor,
          else O(N*M) prefix/suffix scoring per block), kept verbatim
          for A/B comparison with the fast mapper.
  both False = WinMerge-faithful: the engine's hunks are rendered
          exactly the way WinMerge / diffutils side-by-side (sdiff)
          output does — positional top-down pairing, leftovers as
          plain add/delete. Nothing is re-paired, re-ordered or split.
          Default.

The two beautify option pairs (all under
'differ2.algorithm.beautify.*', all OFF by default so the raw engine
output is rendered faithfully):
  align_by_similarity / align_by_similarity2 (this module's
      self.align_by_similarity / self.align_by_similarity2) -- a
      RENDERING choice: how lines inside one REPLACE block are paired
      for display (see the modes above; the two are mutually
      exclusive). Neither ever changes which lines belong to which
      hunk.
  absorb_trivial_equal_blocks (self.absorb_trivial_equal_blocks) -- a
      STRUCTURE choice: run _absorb_trivial_equal_blocks (the function
      at the END of this file) on the engine's finished opcodes inside
      engine_opcodes(). It merges INSERT+EQUAL(trivial)+DELETE into one
      REPLACE and absorbs trivial EQUAL blocks stranded between large
      changes; see its docstring for worked examples of both steps.
      The unified-diff commands never run it (they use raw algorithms).
"""

import time
from difflib import SequenceMatcher as DefaultSequenceMatcher
from .py_algo.myers_onp_diff import MyersSequenceMatcher, InlineMyersSequenceMatcher
from .py_algo.patience_diff.patiencediff import PatienceSequenceMatcher
from .py_algo.vscode_diff import VSCodeSequenceMatcher
from .py_algo.char_diff import char_diff
from .profiling import Profiler
from .align_joined import AlignEngine, align_block_plan
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
    The line-level alignment of the FAST beautify mode lives in
    align_joined.py (engine-agnostic, shared with differ_native); the
    legacy SLOW beautify (align_by_similarity2 ->
    _find_best_pairs_events, the original pure-Python recursive
    search) is kept here verbatim; everything else that overlapped
    with differ_native.Differ (event constants,
    _replace_block_chunks, _char_diff_pair_events, etc.) is duplicated
    intentionally to allow independent evolution.

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
        diff_algorithm / align_by_similarity / align_by_similarity2 /
        absorb_trivial_equal_blocks) and the diffmap (line-
        index tuples, small). The text itself stays in the editor
        tabs' Pascal-side buffers (a_ed / b_ed), which are the source
        of truth; the Python-side line-list copy is built fresh on
        each compare via split_lines_safe(a_ed.get_text_all()).
        """
        self.withdetail = True
        self.diff_algorithm = 'hybrid'
        self.align_by_similarity = False
        # 'differ2.algorithm.beautify.align_by_similarity2' -- the OLD
        # slow pure-Python beautify: the original recursive best-pair
        # search (_find_best_pairs_events: longest unique exact-match
        # anchor, else O(N*M) prefix/suffix scoring per block). Kept
        # side by side with the fast joined-block mapper above so the
        # two methods can be A/B-switched from the toolbar. MUTUALLY
        # EXCLUSIVE with align_by_similarity: the plugin resolves the
        # pair before every compare (align_by_similarity2 wins when a
        # hand-edited JSON sets both) and _replace_block_chunks applies
        # the same priority, so at most one of the two ever runs.
        self.align_by_similarity2 = False
        # 'differ2.algorithm.beautify.absorb_trivial_equal_blocks' --
        # when True, engine_opcodes() runs _absorb_trivial_equal_blocks
        # (the module-level function at the END of this file) on the
        # engine's finished opcodes. Default False: raw engine output.
        self.absorb_trivial_equal_blocks = False
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

    def engine_opcodes(self, a, b):
        """Run ONLY the Python diff engine on the two line sequences and
        return the final opcode list: matcher construction + get_opcodes
        (+ the _absorb_trivial_equal_blocks beautify pass when
        self.absorb_trivial_equal_blocks is on) -- everything compare()
        does before its event walk starts.

        This is the BACKGROUND-THREAD half of a Python compare (see
        Command.refresh_compare's Python kick-off): it is PURE Python
        -- no CudaText API, no Profiler SECTIONS (the section stack is
        main-thread state; the optional per-step absorb marks use the
        thread-safe mark_standalone instead), no editor access -- so
        it runs on a daemon thread while the main thread stays free
        and responsive.
        compare(a, b, opcodes=<this result>) then walks the opcodes
        without running the engine again; the two halves together are
        identical to the old all-in-one compare(a, b).

        The matcher is released before returning (same memory
        rationale as the old inline `del diff`)."""
        diff = self._build_line_matcher(a, b)

        # get_opcodes() runs the selected pure-Python algorithm — this is
        # where the actual diff is computed (no cudatext.diff_proc call
        # happens on this path; that is the native differ's job).
        opcodes = diff.get_opcodes()
        # RELEASE THE MATCHER NOW — we already have the opcodes and the
        # matcher no longer serves any purpose. The pure-Python matchers
        # (difflib SequenceMatcher, PatienceSequenceMatcher, the
        # HybridSequenceMatcher's internal _CombinedMatcher, the
        # MyersSequenceMatcher) all build substantial internal state
        # during get_opcodes() — hash tables of line fingerprints,
        # back-pointer matrices for the LCS walk, the matching-blocks
        # list, junk-detection dicts — and that state stays alive until
        # the matcher object itself is collected. Without this `del`, all
        # of that intermediate state survives until the caller drops the
        # opcode list. For a 33k-line compare that's ~15-25MB of dead
        # matcher state holding the peak up unnecessarily.
        del diff

        # Opcode beautify pass -- ONLY when the option is on
        # ('differ2.algorithm.beautify.absorb_trivial_equal_blocks',
        # default off: raw, algo-faithful hunks). When on, it runs for
        # EVERY algorithm (measured: difflib 2x Step-1 + 22x Step-2
        # patterns on the _dev/__tests corpus, myers 1+61, hybrid 1+3,
        # patience 1+3 -- the old comment claiming patience was immune
        # was wrong -- and even vscode's DP path emits Step-1 patterns
        # on crafted small inputs, so no engine is exempt). The native
        # engines run their own copy of the same pass -- see
        # differ_native.collect_char_pairs / compare_lists. The line
        # list `a` is passed so the pass can read the EQUAL blocks'
        # text without storing it on the instance.
        if self.absorb_trivial_equal_blocks:
            # Per-step profiling rows (the report's 'Beautify passes'
            # block): booked via mark_standalone because THIS half runs
            # on the background Python-engine thread -- a regular
            # mark() would add its dt to whatever section the MAIN
            # thread has open at that moment (the shared stack), and
            # there is deliberately NO umbrella row on this path (the
            # steps' sum IS the pass total here; see the report notes).
            opcodes = _absorb_trivial_equal_blocks(
                a, opcodes,
                Profiler.mark_standalone if Profiler.enabled else None)
        return opcodes

    def compare(self, a, b, opcodes=None):
        """Generator that yields diff events for side-by-side display.

        Runs the selected Python diff algorithm on the two line
        sequences, optionally applies _absorb_trivial_equal_blocks
        (the VS Code-style structure beautify -- only when
        self.absorb_trivial_equal_blocks is on; see the function at
        the end of this file), then walks the opcodes and yields events
        (A_LINE_DEL, B_LINE_ADD, A_GAP, B_GAP, ALIGN, A_SYMBOL_DEL,
        etc.) that __init__.py consumes to paint the compare view.

        The alignment mode (align_by_similarity) only affects how
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
            opcodes: precomputed engine result (engine_opcodes(a, b)),
                delivered by the background Python-engine thread -- the
                same engine/walk split the native differ makes between
                its background line diff and its replay walk. When
                given, the engine and the absorb pass are skipped and
                the walk starts at once. None (default) keeps the
                legacy behavior: the engine runs lazily inside this
                generator on the consumer's thread (tests / the inline
                fallback when threads are unavailable).
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
        # (compare:algorithm -- which folds the absorb pass on the
        # legacy synchronous path when the option is on -- and the
        # per-chunk compare:positional_pairs / compare:align_by_similarity).

        self.diffmap = []
        if opcodes is None:
            # LEGACY synchronous mode: the engine (+ the optional
            # absorb pass) runs HERE, on the consumer's thread, inside
            # this generator's first next(). engine_opcodes is the same
            # code the background Python-engine thread runs -- kept as
            # one call, so the whole engine half (matcher + get_opcodes
            # + optional absorb) folds into 'compare:algorithm' on
            # this path (absorb is O(n) and dwarfed by the engine; the
            # background path books the whole engine wait as an async
            # pair instead, mirroring the native kick-off).
            Profiler.start('compare:algorithm')
            opcodes = self.engine_opcodes(a, b)
            Profiler.stop('compare:algorithm')
        # else: background mode -- 'opcodes' is what the engine thread
        # computed (engine_opcodes); no engine work happens while
        # painting, exactly like the native replay walk.

        # Event production for REPLACE blocks is instrumented per chunk
        # ('compare:positional_pairs' / 'compare:align_by_similarity' open
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
            print('Differ 2: compare took {:.1f}ms '
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
        ('compare:align_by_similarity' for the beautify path,
        'compare:positional_pairs' for the positional path — one row per
        producer so the report shows which mode a block used) opens
        after a list starts and closes before the list is yielded —
        never open across a yield (see compare()'s NOTE). Event lists
        are bounded by _REPLACE_CHUNK pairs, so huge blocks (two
        entirely different 1M-line files come as ONE replace opcode)
        do not materialize their whole event stream at once.

        Three rendering modes, selected by the two MUTUALLY EXCLUSIVE
        beautify flags (align_by_similarity / align_by_similarity2 --
        see __init__; when both are somehow set on a directly
        constructed Differ, align_by_similarity2 wins in the branch
        below):

        align_by_similarity = True (fast 'beautified' alignment)
            Unequal line counts go to the JOINED-BLOCK MAPPER
            (align_joined.py): the configured Python engine sub-diffs
            the block's two sides and the opcodes ARE the pairing
            (EQUAL ranges = pairs, DELETE/INSERT = gaps, REPLACE
            residuals = recurse with progressively looser keys: raw
            -> stripped -> prefix), the ORIGINAL recursive search only
            for small residuals (da*db <= align_joined.SMALL_PRODUCT).
            VS Code-like; re-arranges the engine's output.

            Fast path (da == db): positional pairing (same as the
            algo-faithful mode) — the common case; no search at all.

            Slow path (da != db): the joined-block cascade — see
            align_joined's module docstring.

        align_by_similarity2 = True (slow legacy 'beautified' alignment)
            Unequal line counts use the ORIGINAL pure-Python search,
            _find_best_pairs_events(): anchor on the longest unique
            exact match or the best prefix/suffix-similar pair,
            char-diff it, recurse on both sides. Lines with < 3 chars
            of similarity are shown as separate delete+add. VS Code-
            like; re-arranges the engine's output. Kept side by side
            with the fast mapper for A/B comparison -- the search is
            O(N*M) per block and recursive.

            Fast path (da == db): positional pairing (same as the
            algo-faithful mode).

            Slow path (da != db): _find_best_pairs_events — see its
            docstring.

        both flags False (algo-faithful, default)
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

        # ---- da != db: the beautify modes diverge here ----
        if (self.align_by_similarity or self.align_by_similarity2) \
                and da != db:
            if self.align_by_similarity2:
                # SLOW legacy beautify (align_by_similarity2): the
                # original pure-Python recursive search, restored
                # verbatim. anchor + prefix/suffix scoring + threshold
                # + staggering. Produced into ONE list (the recursive
                # scorer makes chunking invasive). Char diffs inside
                # are perf_counter-timed and booked as batched marks.
                Profiler.start('compare:align_by_similarity')
                evs = []
                self._find_best_pairs_events(evs, a, alo, ahi, b, blo, bhi)
                Profiler.stop('compare:align_by_similarity')
                yield evs
                return

            # FAST beautify (align_by_similarity): joined-block mapping
            # (align_joined.py): the configured Python engine sub-diffs
            # the block's sides; the opcodes ARE the pairing, the
            # original recursive search only for small residuals.
            # Produced into ONE list per block (this Differ has no
            # collect/replay phases -- compare() is a flat generator
            # -- so there is no plan cache here; the search runs
            # exactly once per compare anyway).
            Profiler.start('compare:align_by_similarity')
            eng = AlignEngine(self._joined_sub_diff, self._joined_sub_diff)
            plan = align_block_plan(a, alo, ahi, b, blo, bhi, eng)
            evs = []
            self._emit_align_plan(evs, plan, a, b)
            Profiler.stop('compare:align_by_similarity')
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
        """(LEGACY beautify mode -- align_by_similarity2) The OLD slow
        pure-Python line alignment within a sub-REPLACE block,
        APPENDING events to `out`. Kept verbatim from the original
        implementation (the code this module ran under the
        'align_by_similarity' option before the joined-block mapper
        took that name) so the two methods stay A/B-comparable; the
        only changes here are docstring notes.

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
        The two searches are the pass's STEPS and book their own rows
        ('align_by_similarity:step1_exact_match_search' /
        'align_by_similarity:step2_prefix_suffix_search' — the report's
        'Beautify passes' block prints them under the pass, under the
        'compare:align_by_similarity' umbrella section of the walk).
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
            Profiler.mark('align_by_similarity:step1_exact_match_search',
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
            # docstring) — the reason align_by_similarity2 is the SLOW
            # option on large files.
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
                Profiler.mark('align_by_similarity:step2_prefix_suffix_search',
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

    def _build_line_matcher(self, a, b):
        """Construct the configured pure-Python line matcher on the two
        sequences. Extracted from engine_opcodes so the joined-block
        aligner (align_joined.py) can run the SAME configured engine on
        a block's sub-sequences: raw keepends lines at the cascade's
        depth 0, stripped/prefix key strings at the deeper passes --
        all opaque strings to a matcher, terminators included.

        NOTE: no _absorb_trivial_equal_blocks here -- that is a
        whole-file STRUCTURE pass; the aligner must see the engine's
        raw EQUAL blocks (they are the anchors it is looking for)."""
        if self.diff_algorithm == 'hybrid':
            return HybridSequenceMatcher(None, a, b)
        if self.diff_algorithm == 'myers':
            return MyersSequenceMatcher(None, a, b)
        if self.diff_algorithm == 'vscode':
            return VSCodeSequenceMatcher(None, a, b)
        if self.diff_algorithm == 'patience':
            return PatienceSequenceMatcher(None, a, b)
        # Default: difflib stdlib SequenceMatcher with autojunk=False
        return DefaultSequenceMatcher(None, a, b, autojunk=False)

    def _joined_sub_diff(self, keys_a, keys_b):
        """AlignEngine bridge (BOTH depths -- the callable pair of
        AlignEngine gets this same method twice): the configured
        Python matcher on the two key lists. The Python engines
        compare strictly (no ignore flags), so raw keepends keys and
        normalized/prefix keys work through the same construction --
        the strings are opaque.

        Returns the opcode list; never None (the Python engine is
        always available -- the None contract exists for the native
        Differ's no-engine fallback)."""
        matcher = self._build_line_matcher(keys_a, keys_b)
        ops = matcher.get_opcodes()
        # RELEASE THE MATCHER NOW -- same memory rationale as
        # engine_opcodes: the matchers build substantial internal state
        # (hash tables of fingerprints, back-pointer matrices) that
        # must not survive into the plan walk.
        del matcher
        if not ops:
            ops = [('replace', 0, len(keys_a), 0, len(keys_b))]
        return ops

    def _emit_align_plan(self, out, plan, a, b):
        """Translate alignment plan ops (see align_joined) into paint
        events appended to `out`. This Differ has no collect/replay
        phases (its compare() is a flat one-pass generator), so there
        is exactly ONE mode: run the Python char diff per changed pair,
        perf_counter-timed and booked as ONE batched mark per plan (the
        same pattern as _positional_pairs_events).

        'P' pairs decide EQUAL vs CHANGED by comparing the RAW lines
        here (never in the plan): an engine EQUAL at the cascade's
        normalized depths may pair raw-different lines, and those must
        run the char diff -- which is exactly the beautify intent: pair
        similar lines and paint their difference.
        """
        prof_on = Profiler.enabled
        char_diff_call = self._char_diff
        append = out.append
        if prof_on:
            eng_dt = 0.0
            eng_n = 0
            eng_max = 0.0
        for op in plan:
            kind = op[0]
            if kind == 'P':
                ai, bj = op[1], op[2]
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
                    # line-level work incl. its wrap-compensation (the
                    # old trailing ALIGN's job)
                    self._char_diff_pair_events(out, ai, bj, ops)
            elif kind == 'D':
                append((B_GAP, op[3], op[1], op[2]))
                for i in range(op[1], op[2]):
                    append((A_LINE_DEL, i))
            elif kind == 'I':
                append((A_GAP, op[3], op[1], op[2]))
                for j in range(op[1], op[2]):
                    append((B_LINE_ADD, j))
            else:
                # 'Q': suppressed hunk. The Python engines never emit
                # 'ignore' (strict comparison -- no DIFF_IGN flags on
                # this path), so this branch is purely defensive: an
                # unknown/extension plan op must never silently DROP
                # lines, so the range renders as a regular change (gap
                # compensating the longer side + del/add lines, no
                # pairs).
                i1, i2, j1, j2 = op[1], op[2], op[3], op[4]
                dia = i2 - i1
                djb = j2 - j1
                if dia > djb:
                    append((B_GAP, j2, i1 + djb, i2))
                elif djb > dia:
                    append((A_GAP, i2, j1 + dia, j2))
                for i in range(i1, i2):
                    append((A_LINE_DEL, i))
                for j in range(j1, j2):
                    append((B_LINE_ADD, j))
        if prof_on and eng_n:
            Profiler.mark(self._CHAR_ROW, eng_dt, eng_n, eng_max)
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


# =========================================================================
# Opcode beautify pass -- absorb trivial EQUAL blocks.
#
# Private module-level helper (differ_native.py carries its OWN copy of
# this code -- the duplication is deliberate, so the Python and native
# codepaths can evolve independently; see the module docstring).
#
# Enabled by the option 'differ2.algorithm.beautify.absorb_trivial_equal_blocks'
# (default OFF -- the engine's raw opcode stream is used as-is). Ported
# from VS Code's heuristicSequenceOptimizations.ts
# (removeVeryShortMatchingLinesBetweenDiffs + the adjacent-change joins
# of optimizeSequenceDiffs); VS Code runs the equivalent optimizations
# unconditionally inside its own diff algorithm.
# =========================================================================

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


def _absorb_trivial_equal_blocks(a, opcodes, _book=None):
    """Beautify pass for a finished opcode list -- two steps.

    Called from engine_opcodes() ONLY when the option
    'differ2.algorithm.beautify.absorb_trivial_equal_blocks' is on.

    STEP 1 -- merge INSERT + EQUAL(trivial) + DELETE (or the mirrored
    DELETE + EQUAL(trivial) + INSERT) into one REPLACE.

        Example (verified: difflib, myers, patience and vscode all
        return exactly these raw opcodes on these inputs -- see
        _dev/__tests/test_absorb_trivial_equal_blocks.py):

            a (left)            b (right)
            head                head
            alpha                                   <- deleted
            beta                                    <- deleted
            (blank)             (blank)             <- matched blank
                                alpha2              <- inserted
                                beta2               <- inserted
            tail                tail

            raw opcodes:
              equal   a[0:1]  b[0:1]     (head)
              delete  a[1:3]  b[1:1]     (alpha, beta)
              equal   a[3:4]  b[1:2]     (the blank -- 0 non-ws chars)
              insert  a[4:4]  b[2:4]     (alpha2, beta2)
              equal   a[4:5]  b[4:5]     (tail)

            rendered side-by-side this reads as TWO unrelated edits
            with a stray aligned blank between them:

              head    =  head
              alpha   <
              beta    <
                      =  (blank)
                      >  alpha2
                      >  beta2
              tail    =  tail

            STEP 1 merges the middle triple into ONE replace:

              equal   a[0:1]  b[0:1]
              replace a[1:4]  b[1:4]
              equal   a[4:5]  b[4:5]

              head    =  head
              alpha   |  (blank)
              beta    |  alpha2
              (blank) |  beta2
              tail    =  tail

            -- the changed lines now pair with each other as one edit.

    STEP 2 -- absorb a short trivial EQUAL block that sits BETWEEN two
    changed blocks into a single REPLACE, when at least one of the two
    changed blocks is large (combined lines >= MIN_LARGE_REPLACE).

        Example:

            a (left)            b (right)
            h                   h
            p1                  P1
            p2                  P2
            p3                  P3
            (blank)             (blank)             <- matched blank
            q1                  Q1
            q2                  Q2
            q3                  Q3
            t                   t

            raw opcodes:
              equal   a[0:1]  b[0:1]
              replace a[1:4]  b[1:4]     (p1..p3 -> P1..P3, 6 lines)
              equal   a[4:5]  b[4:5]     (the blank -- trivial)
              replace a[5:8]  b[5:8]     (q1..q3 -> Q1..Q3, 6 lines)
              equal   a[8:9]  b[8:9]

            Each replace is 6 combined lines (>= MIN_LARGE_REPLACE), so
            the matched blank is absorbed and the whole region becomes
            ONE replace a[1:8] b[1:8] -- instead of two fragments that
            read as two unrelated changes.

    Guards (both steps):
      * only EQUAL blocks whose total non-whitespace content is
        <= TRIVIAL_THRESHOLD (4) characters are ever absorbed, so a
        meaningful matched line (a real statement, an identifier) is
        never merged away;
      * STEP 1 only merges an insert/delete PAIR (insert + insert or
        delete + delete neighbors stay untouched);
      * STEP 2 requires one LARGE neighbor, so two small changes
        separated by a matched blank line stay separate (VS Code's own
        guard);
      * 'ignore' hunks (the native engine's suppressed all-blank
        differences under DIFF_IGN_BLANK_LINES) are BARRIERS for STEP 2:
        merging across one would resurrect the suppressed lines into a
        shown REPLACE. The pure-Python engines never emit 'ignore', but
        the guard keeps this copy interchangeable with differ_native's.

    Args:
        a: the left file's line list (keepends), used ONLY to read the
            EQUAL blocks' text for the trivial-content check.
        opcodes: difflib-style (tag, i1, i2, j1, j2) tuples from any
            engine. Tags: 'equal' / 'delete' / 'insert' / 'replace' /
            'ignore' (the native engine's suppressed all-blank hunks).
        _book: optional profiler booking callable (row_name, dt) ->
            None, used only by the plugin's compare paths when
            profiling is on: differ_python.engine_opcodes passes
            Profiler.mark_standalone (it runs on the background
            Python-engine thread, where a regular mark() would
            corrupt the main thread's open sections);
            differ_native's collect_char_pairs / compare_lists pass
            Profiler.mark under their 'absorb_trivial_equal_blocks'
            umbrella section. Each step books its OWN row
            ('absorb_trivial_equal_blocks:step1_merge_ins_eq_del' /
            ':step2_absorb_short_equal' -- the report's 'Beautify
            passes' block). None (default) = run untimed -- the
            standalone tests and any pure-algorithm caller.

    Returns:
        A NEW list with the merges applied (the input list is never
        mutated; identity is returned for lists too short to contain
        any pattern). Idempotent: absorbing twice changes nothing.
        Coverage-preserving: the result still tiles a[0:len(a)] and
        b[0:len(b)] exactly; only tag/range boundaries move.
    """
    if len(opcodes) < 3:
        return opcodes
    result = list(opcodes)

    # ------------------------------------------------------------------
    # STEP 1: merge INSERT+EQUAL(trivial)+DELETE (or the mirrored
    # DELETE+EQUAL(trivial)+INSERT) into one REPLACE.
    #
    # The pattern means: the engine matched a trivial line instead of a
    # meaningful one, so identical lines around it render as one added
    # + one deleted instead of a paired change. Merging the three
    # opcodes into a single REPLACE lets the replace-block pairing
    # (positional or similarity-based) match the identical lines
    # naturally.
    #
    # The back-step after a merge (i -= 1) re-examines the triple that
    # ends at the newly created REPLACE, so cascades of adjacent
    # patterns collapse left-to-right to a fixpoint in one sweep.
    # ------------------------------------------------------------------
    _t0 = time.perf_counter() if _book else 0.0
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
    if _book:
        _book('absorb_trivial_equal_blocks:step1_merge_ins_eq_del',
              time.perf_counter() - _t0)

    # ------------------------------------------------------------------
    # STEP 2: absorb a short trivial EQUAL block between two changed
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
    _t0 = time.perf_counter() if _book else 0.0
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
    if _book:
        _book('absorb_trivial_equal_blocks:step2_absorb_short_equal',
              time.perf_counter() - _t0)

    return result
