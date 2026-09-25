"""Differ module for native algorithms (native_histogram, native_myers).

This file is self-contained: it contains everything needed to run the
native diff engine implemented in Free Pascal and exposed via
cudatext.diff_proc. It does NOT depend on differ_python.py.

The native engine is 10-30x faster than the pure-Python matchers on
large files. For Python-only algorithms (hybrid, myers, vscode, patience,
difflib), use differ_python.py instead.

TWO-PHASE ASYNCHRONOUS DESIGN (both engine jobs run off the UI thread):

  Phase 1 -- line-level diff. start_async_line_diff() starts
  cudatext.diff_proc(DIF_TEXTS, ..., callback) on the engine's own OS
  thread; the plugin's main thread returns at once, CudaText stays
  responsive, and the engine calls back on the main thread with the
  opcode list (start_async_line_diff returns the job handle; pass it to
  cancel_async_line_diff when the result will never be consumed --
  diff_proc(DIF_CANCEL) stops the engine cooperatively and the callback
  is then never invoked).

  Phase 2 -- BATCHED char-level diff. When the line opcodes arrive
  (Command._on_native_diff_done), the Differ walks them ONCE in COLLECT
  mode (Differ.collect_char_pairs): the exact same pairing walk that
  paints later, but instead of calling the engine per line pair,
  _char_diff just records each (line_a, line_b) pair. The collected
  pairs go to the engine in ONE call -- start_async_char_diff() starts
  cudatext.diff_proc(DIF_CHARS, pairs, ..., callback) (the BATCHED
  DIF_CHARS: the whole list of pairs is compared in a single background
  engine run, re-checking the cancel flag between pairs). When the
  engine calls back with the per-pair opcode lists, the paint pass runs
  the SAME walk in REPLAY mode: _char_diff pops the precomputed result
  for each pair (same walk order -> same pop order), so no engine call
  happens during painting.

  Why batched: the old design made ONE diff_proc(DIF_CHARS) call PER
  pair -- ~800k API round-trips (argument parsing, string marshalling,
  result building) on a 1M-line compare, which dominated the runtime
  (the per-call overhead was ~3/4 of the total char-diff time). The
  batched form pays that overhead ONCE for the whole compare.

  The collect and replay passes run the IDENTICAL walk code
  (_replace_block_chunks -> _positional_pairs_events /
  _find_best_pairs_events) -- the only difference is what _char_diff
  does (record the pair vs pop the precomputed ops). The walk is a
  deterministic function of (a_lines, b_lines, opcodes, config), so
  both passes request char diffs in exactly the same order and the
  replay's pops stay aligned with the collect's records. The line
  lists are split ONCE (in collect_char_pairs) and cached on the
  Differ for the replay pass, so the expensive split does not run
  twice.

Ignore options: the plugin's 'ignoreopt.*' settings are collected into
a DIFF_IGN_* bitmask (see build_ignore_flags) and applied to BOTH the
line-level diff (diff_proc DIF_TEXTS) and the char-level detail diff
(batched diff_proc DIF_CHARS). The pure-Python algorithms in
differ_python.py do NOT support ignore options — they always compare
strictly.

Code is intentionally duplicated from differ_python.py to allow
independent evolution of the native and Python codepaths. As more
diff logic moves into the Pascal native engine, this file will shrink
to a thin wrapper around cudatext.diff_proc if God wills.
"""

import time
from .py_algo.char_diff import char_diff
from .profiling import Profiler
from .utils import split_lines_safe
from collections import Counter
import cudatext as _ct
from cudax_lib import get_translation
_ = get_translation(__file__)  # I18N

# The native diff engine (cudatext.diff_proc) is part of this build's
# CudaText API: when the attribute exists the engine is present, when it
# does not (stock CudaText build) the plugin falls back to the pure-Python
# algorithms in differ_python.py and never calls into this module's
# engine paths.
_HAS_NATIVE_DIFF = hasattr(_ct, 'diff_proc')


# diff_proc ignore-flag constants, mirrored from the cudatext module
# (proc_py_const.pas DIFF_IGN_*). Only meaningful when the native engine
# is available; the pure-Python algorithms compare strictly and never
# read them.
DIFF_IGN_NONE        = _ct.DIFF_IGN_NONE if _HAS_NATIVE_DIFF else 0
DIFF_IGN_CASE        = _ct.DIFF_IGN_CASE if _HAS_NATIVE_DIFF else 1
DIFF_IGN_WHITESPACE  = _ct.DIFF_IGN_WHITESPACE if _HAS_NATIVE_DIFF else 2
DIFF_IGN_EOL         = _ct.DIFF_IGN_EOL if _HAS_NATIVE_DIFF else 4
DIFF_IGN_NUMBERS     = _ct.DIFF_IGN_NUMBERS if _HAS_NATIVE_DIFF else 8
DIFF_IGN_BLANK_LINES = _ct.DIFF_IGN_BLANK_LINES if _HAS_NATIVE_DIFF else 16


def build_ignore_flags(cfg):
    """Build the diff_proc DIFF_IGN_* bitmask from a Differ config dict.

    Maps the five 'ignoreopt.*' boolean settings read by
    Command.get_config() (ignore_case, ignore_whitespace,
    ignore_blank_lines, ignore_eol, ignore_numbers) to the native
    engine's flag bits. Unknown/missing keys count as False.
    """
    flags = DIFF_IGN_NONE
    if cfg.get('ignore_case'):
        flags |= DIFF_IGN_CASE
    if cfg.get('ignore_whitespace'):
        flags |= DIFF_IGN_WHITESPACE
    if cfg.get('ignore_blank_lines'):
        flags |= DIFF_IGN_BLANK_LINES
    if cfg.get('ignore_eol'):
        flags |= DIFF_IGN_EOL
    if cfg.get('ignore_numbers'):
        flags |= DIFF_IGN_NUMBERS
    return flags


class CudaDiffNativeMatcher:
    """Difflib-compatible wrapper around cudatext.diff_proc().

    Calls the native Free Pascal diff engine exposed at cudatext.diff_proc(). The native engine is
    dramatically faster than any of the pure-Python matchers on large
    files (10-30x speedup is typical).

    This class exposes the same minimal interface Differ.compare() uses
    from the other matchers: get_opcodes() returning a list of
    (tag, i1, i2, j1, j2) tuples with tag in
    {'equal', 'delete', 'insert', 'replace', 'ignore'} ('ignore' is a
    CudaText extension: an all-blank hunk suppressed by
    DIFF_IGN_BLANK_LINES — ranges behave like 'replace').

    The native API takes two RAW TEXT strings and returns exactly that
    opcode format. The engine splits the texts into lines internally
    (on \r\n / \r / \n), so this class takes the raw texts directly
    via a_text / b_text and passes them to the engine VERBATIM — no
    Python-side split/join round-trip. Line counts (needed by
    get_matching_blocks and the defensive fallback in get_opcodes)
    are derived lazily from the raw texts via split_lines_safe.
    """

    # Algorithm IDs — accessed directly from cudatext (_ct) at the call
    # sites. These class attributes are kept for API compatibility with
    # code that references CudaDiffNativeMatcher._ALGO_MYERS etc.
    _ALGO_MYERS = 0  # DIFF_ALGO_MYERS
    _ALGO_HISTOGRAM = 1  # DIFF_ALGO_HISTOGRAM

    def __init__(self, isjunk=None, a_text='', b_text='', algo=1, flags=0):
        """Create a native diff matcher.

        Args:
            isjunk: ignored (kept for difflib API compatibility; the
                native engine does not support junk heuristics).
            a_text, b_text: raw text strings. Passed VERBATIM to
                cudatext.diff_proc — the engine splits them into lines
                internally (CRLF/CR/LF). No Python-side split or join is
                done on the input path: ''.join(split_lines_safe(t)) == t
                byte-for-byte, so splitting on the Python side just to
                re-join for the engine call would rebuild a full copy of
                the text for nothing (on a 1M-line compare: ~30ms and ~35MB
                of transient allocation of pure waste).
            algo: CudaDiffNativeMatcher._ALGO_MYERS (0) — WinMerge's GNU
                  diffutils Myers with Eggert heuristic. Faster on large /
                  different files.
                  CudaDiffNativeMatcher._ALGO_HISTOGRAM (1, default) — JGit
                  HistogramDiff with MyersDiff as internal fallback for
                  sub-regions. Patience-style anchoring on unique lines,
                  more human-readable for normal files.
            flags: bitmask of DIFF_IGN_* values (see build_ignore_flags).
        """
        self._a_text = a_text
        self._b_text = b_text
        self._algo = algo
        self._flags = flags
        self.opcodes = None

    def get_matching_blocks(self):
        """Return list of (i, j, n) matching blocks, difflib-style.

        Derived from get_opcodes() -- equal runs become matching blocks,
        with a sentinel (len(a), len(b), 0) at the end.
        """
        blocks = []
        for tag, i1, i2, j1, j2 in self.get_opcodes():
            if tag == 'equal':
                blocks.append((i1, j1, i2 - i1))
        # Sentinel: difflib-compatible API requires a final (n_a, n_b, 0)
        # block. The engine's opcode list is sorted, so the last opcode's
        # i2 / j2 are the total line counts on each side — derive from
        # there instead of recomputing with split_lines_safe (saves a
        # full text scan).
        opcodes = self.opcodes
        if opcodes:
            n_a = opcodes[-1][2]
            n_b = opcodes[-1][4]
        else:
            n_a = n_b = 0
        blocks.append((n_a, n_b, 0))
        return blocks

    def get_opcodes(self):
        """Return difflib-compatible opcodes by calling cudatext.diff_proc.

        Returns:
            list of (tag, i1, i2, j1, j2) tuples where tag is a lowercase
            string. Identical in format to
            difflib.SequenceMatcher.get_opcodes(), plus the CudaText
            extension tag 'ignore' (an all-blank hunk suppressed by
            DIFF_IGN_BLANK_LINES — ranges behave like 'replace').
        """
        if self.opcodes is not None:
            return self.opcodes
        Profiler.start('line_diff:native_engine')
        result = _ct.diff_proc(
            _ct.DIF_TEXTS,
            self._a_text,
            self._b_text,
            self._algo,
            self._flags,         # bitmask of DIFF_IGN_* (Differ.ignore_flags)
        )
        Profiler.stop('line_diff:native_engine')
        # The native API always returns a valid opcode list.
        if result is None:
            # Defensive: should never happen, but fall back to a single
            # REPLACE covering everything so the caller's opcode-walking
            # loop still produces sensible output (everything painted as
            # changed) instead of crashing. Line counts are derived from
            # the raw texts via split_lines_safe.
            result = [('replace', 0, len(split_lines_safe(self._a_text)),
                                  0, len(split_lines_safe(self._b_text)))]
        self.opcodes = result
        return result


def start_async_line_diff(a_text, b_text, algo, flags, callback):
    """Start a line-level compare in the engine's background thread via
    the asynchronous form of cudatext.diff_proc (the `callback`
    argument).

    Returns the engine's job handle (a positive int) when the background
    compare was started: the engine invokes `callback(opcodes)` on the
    main thread when it finishes, with the same opcode list the
    synchronous form returns (None on engine error). Returns 0 when the
    background compare could not be started.

    Keep the job handle and pass it to cancel_async_line_diff() when the
    compare's result will never be consumed (tab closed, plugin exiting):
    diff_proc(DIF_CANCEL, handle) stops the engine's thread
    cooperatively, and the callback of a cancelled compare is never
    invoked.

    'callback' must be a Python callable (the engine also accepts a
    'module.function' string, but a callable -- e.g. a
    functools.partial -- carries per-call context, which the plugin
    needs to know WHICH compare finished).
    """
    if not _HAS_NATIVE_DIFF:
        return 0
    result = _ct.diff_proc(
        _ct.DIF_TEXTS, a_text, b_text, algo, flags, callback)
    if isinstance(result, int) and result > 0:
        return result
    return 0


def cancel_async_line_diff(job):
    """Cooperatively cancel a background compare started by
    start_async_line_diff().

    Returns True when the engine found the job and requested
    cancellation; False when no such job is running (it already
    finished, was cancelled before, or the handle is invalid) -- a
    finished job needs no cancelling.

    Cancellation is cooperative: the engine's diff loops poll the job's
    cancel flag at coarse granularity, so a compare inside a long
    engine phase unwinds within a couple of seconds (the Pascal stack
    unwinds through every try/finally block, releasing everything the
    compare allocated -- nothing leaks). The completion callback of a
    cancelled compare is never invoked.
    """
    if not job:
        return False
    return bool(_ct.diff_proc(_ct.DIF_CANCEL, job))


def start_async_char_diff(pairs, flags, callback):
    """Start a BATCHED char-level compare of ALL line pairs in ONE
    background engine job: the asynchronous form of
    cudatext.diff_proc(DIF_CHARS) (the `callback` argument).

    'pairs' is the list of (text1, text2) string tuples collected by
    Differ.collect_char_pairs() -- the whole batch is marshalled into
    the engine in ONE API call, and the engine compares every pair on
    its own OS thread (it re-checks the job's cancel flag between
    pairs, so a batch of hundreds of thousands of small pairs also
    stops promptly on DIF_CANCEL).

    Returns the engine's job handle (a positive int) when the
    background compare was started: the engine invokes
    `callback(results)` on the main thread when it finishes, where
    'results' is a list with ONE opcode list per input pair, in the
    same order as 'pairs' (element k describes pairs[k][0] vs
    pairs[k][1], in the same (tag, i1, i2, j1, j2) format
    start_async_line_diff's callback delivers for whole texts), or None
    when the engine failed. Returns 0 when the background compare
    could not be started (caller falls back to the synchronous batched
    form, sync_char_diff).

    Keep the job handle and pass it to cancel_async_char_diff() when
    the batch's result will never be consumed (tab closed, plugin
    exiting): diff_proc(DIF_CANCEL, handle) stops the engine's thread
    cooperatively, and the callback of a cancelled compare is never
    invoked. Works exactly like cancel_async_line_diff -- DIF_CANCEL
    does not distinguish line and char jobs.
    """
    if not _HAS_NATIVE_DIFF or not pairs:
        return 0
    result = _ct.diff_proc(
        _ct.DIF_CHARS, pairs, None, 0, flags, callback)
    if isinstance(result, int) and result > 0:
        return result
    return 0


def cancel_async_char_diff(job):
    """Cooperatively cancel a background batched char compare started
    by start_async_char_diff().

    Identical mechanism to cancel_async_line_diff() (the engine's
    DIF_CANCEL finds the job by handle whether it is a DIF_TEXTS or a
    DIF_CHARS job): a DIF_CHARS batch re-checks the cancel flag between
    pairs, so even a batch of many small pairs stops within a couple of
    seconds. Returns True when the job was found and cancellation was
    requested; False when no such job is running.
    """
    if not job:
        return False
    return bool(_ct.diff_proc(_ct.DIF_CANCEL, job))


def sync_char_diff(pairs, flags):
    """Synchronous BATCHED char-level compare: the whole 'pairs' list
    in ONE blocking diff_proc(DIF_CHARS) call.

    Fallback for the rare case start_async_char_diff() could not start
    the background job: still ONE call for the whole batch (the
    per-pair overhead that dominated the old design is paid once), but
    it blocks the main thread for the whole batch time -- acceptable
    only as an emergency path, never the normal flow.

    Returns the per-pair opcode list (same format as
    start_async_char_diff's callback argument), or None on engine
    error (the caller then paints every pair as a full REPLACE via the
    replay mode's None-element fallback).
    """
    if not _HAS_NATIVE_DIFF or not pairs:
        return None
    return _ct.diff_proc(_ct.DIF_CHARS, pairs, None, 0, flags)


def algo_id(algorithm_name):
    """Map a configured algorithm name to the diff_proc DIFF_ALGO_* id:
    'native_histogram' -> DIFF_ALGO_HISTOGRAM, anything else (including
    'native_myers' and unexpected values) -> DIFF_ALGO_MYERS (the
    plugin's default and fastest on large/very different files)."""
    if algorithm_name == 'native_histogram':
        return CudaDiffNativeMatcher._ALGO_HISTOGRAM
    return CudaDiffNativeMatcher._ALGO_MYERS


# Internal benchmark toggle. When set to True, Differ.compare() prints the
# total time elapsed from when the generator starts executing until it is
# fully consumed (or closed).
_BENCHMARK = True


# --- Event constants ---
# These are the event IDs yielded by Differ.compare(). __init__.py
# consumes them to paint the side-by-side compare view.
# They are duplicated in differ_python.py — both modules define the
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
# path for 'ignore' opcodes (DIFF_IGN_BLANK_LINES). WinMerge-style
# "ignored differences": the lines are painted with the ignored color
# and a compensating gap keeps the two sides aligned, but they are NOT
# differences (no bookmarks, no diffmap entry, not counted by
# n_diff_events). Mirrored in differ_python.py (which never yields
# them — pure-Python engines compare strictly — but __init__.py's
# paint loop references the constants off either module).
A_LINE_IGN = '-i'   # ignored line in file a: (id, y)
B_LINE_IGN = '+i'   # ignored line in file b: (id, y)
A_GAP_IGN  = '-^i'  # ignored gap in file a: (id, y, start, end)
B_GAP_IGN  = '+^i'  # ignored gap in file b: (id, y, start, end)
# Alignment event: a pair of lines (one in A, one in B) that must be kept
# at the same visual Y position. Used by __init__.py to add compensating
# gaps when word-wrap is on and the two lines wrap to a different number
# of visual rows.
ALIGN = '='
# Composite CHANGED-PAIR event: one event per positionally-paired,
# char-diffed line pair that REPLACES the four per-pair events the walk
# used to emit (A_LINE_CHANGE + B_LINE_CHANGE + A_DECOR_* + B_DECOR_*)
# and absorbs the pair's trailing ALIGN as well.
#           return (id, a_line, b_line, deca, decb)
# deca/decb are the pair's char-opcode counts (how many delete-side /
# insert-side char runs the pair produced): deca > 0 paints the A line
# with the 'line contains deletions' decor color (A_DECOR_RED's old
# meaning), deca == 0 the plain changed color (A_DECOR_YELLOW); decb
# > 0 / == 0 select B_DECOR_GREEN's / B_DECOR_YELLOW's old colors.
# Rationale: on the 1M-line / 200k-block benchmark the four events plus
# the ALIGN cost ~5 dispatch iterations, 5 tuple allocations and 5 list
# appends PER CHANGED PAIR in BOTH the producer and the consumer (the
# paint loop's branch ladder ran 5.8M times; ~7.4s of dispatch SELF);
# one composite event does the same work with one tuple, one append,
# one dispatch. The consumer's PAIR_CHANGED branch reproduces the four
# branches' painted state EXACTLY (bookmark + micromap + overview line
# state per side, decor colors from the counts) and runs the pair's
# wrap-compensation (the old trailing ALIGN's job) at the same point in
# the stream, so the painted view is identical. Mirrored in
# differ_python.py.
PAIR_CHANGED = '*'


class Differ:
    """Differ for native algorithms (native_histogram, native_myers).

    Only handles native_histogram and native_myers algorithms. For Python
    algorithms (hybrid, myers, vscode, patience, difflib), use
    differ_python.Differ instead.

    This class is self-contained: it does not import from differ_python.
    Code that overlaps with differ_python.Differ (event constants,
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
         -i ignored (suppressed) line in file a
         +i ignored (suppressed) line in file b
              return (id, y)
              Emitted for 'ignore' opcodes (DIFF_IGN_BLANK_LINES):
              lines of an all-blank hunk the ignore options suppressed.
              Painted with the ignored color; NOT a difference (no
              bookmark, no diffmap entry, no char details).
         -^i / +^i ignored gap in file a / b
              return (id, y, start, end)
              Same positioning as -^ / +^ (inserted after line y-1,
              compensates for the lines [start, end) on the OTHER side)
              but colored/tagged as ignored — it compensates the length
              mismatch of a suppressed hunk so lines below stay
              aligned (WinMerge-style ignored differences).
         = visually-aligned line pair (a_line, b_line)
              return (id, a_line, b_line)
              Consumed by __init__.py to add a compensating gap when the two
              lines wrap to a different number of visual rows.
          * composite changed line pair (a_line, b_line, deca, decb)
              return (id, a_line, b_line, deca, decb)
              One event per char-diffed REPLACE pair; replaces the old
              A_LINE_CHANGE / B_LINE_CHANGE / A_DECOR_* / B_DECOR_*
              quartet and the pair's trailing ALIGN. deca/decb are the
              pair's char-opcode counts (see PAIR_CHANGED above); the
              consumer paints bookmarks, micromap marks and overview
              line states for BOTH lines from this one event and runs
              the pair's wrap-compensation.
              Still emitted by the non-detailed plain-replace path as
              the old quartet (withdetail=False), which has no pairing.
    """

    def __init__(self):
        """Initialize the Differ.

        Sets default options: native_myers algorithm, detailed
        compare on. The algorithm can be changed later via
        self.diff_algorithm before calling compare().

        self.ignore_flags is the DIFF_IGN_* bitmask built by
        differ_native.build_ignore_flags() from the plugin's 'ignoreopt.*'
        config settings. Command.refresh_compare sets it before each
        compare(); it is applied to BOTH the line-level diff
        (DIF_TEXTS) and the char-level detail diff (DIF_CHARS).

        The Differ holds NO text between compares — neither raw text
        nor line lists. a_text / b_text are passed directly to
        compare() by the caller (Command.refresh_compare), used as locals
        inside compare() to drive the engine + painting, and dropped
        when compare() returns. Between compares, the Differ holds
        only config (withdetail / diff_algorithm / beautify_alignment /
        ignore_flags) and the diffmap (line-index tuples, small). The
        text itself stays in the editor tabs' Pascal-side buffers
        (a_ed / b_ed), which are the source of truth; the Python-side
        copy is built fresh on each compare via a_ed.get_text_all() /
        b_ed.get_text_all().
        """
        self.withdetail = True
        self.diff_algorithm = 'native_myers'
        self.beautify_alignment = False
        self.ignore_flags = 0  # DIFF_IGN_* bitmask (see build_ignore_flags)
        self.diffmap = []
        # --- two-phase char-diff state (see the module docstring) ---
        # Active pair-collection list of the COLLECT pass, or None when
        # not collecting. _char_diff switches on this: while a list is
        # bound here, every call RECORDS its pair into it and returns
        # [] instead of touching the engine.
        self._char_pairs_pending = None
        # Precomputed per-pair opcode lists of the REPLAY pass, or None
        # when not replaying. While bound, every _char_diff call POPS
        # the next element (walk order == collect order). An element of
        # None is the engine-failure fallback: paint a full REPLACE.
        self._char_ops = None
        # Pop position inside self._char_ops (the list is indexed, not
        # popped, so the engine's result list is never mutated).
        self._char_ops_pos = 0
        # Line lists cached between the collect and replay passes:
        # collect_char_pairs() splits the raw texts once and parks the
        # lists here; the following compare() call takes them over
        # (instead of splitting again) and clears the cache. Also
        # cleared by drop_cached_state() on the abandonment paths.
        self._lines_a = None
        self._lines_b = None
        # ALIGN-event suppression flag of the CURRENT walk (set by
        # compare_lists from its align_gap_events parameter; False =
        # historical full stream). The walk's producers read it to
        # skip emitting ALIGN tuples the consumer would ignore anyway
        # (see compare_lists's docstring). Reset at the walk's end and
        # on every abandonment path, like the fields above.
        self._skip_align = False

    # Very-long-line guard (chars, not bytes): a pair with either side
    # over this limit skips the engine entirely and renders as a
    # single full-line REPLACE. The Pascal DoDiffChars would do the
    # same inside diff_proc(DIF_CHARS) (its Tokenize pre-allocates N
    # tokens where N = byte length, which for 100KB+ lines causes
    # massive heap allocation that led to EAccessViolation when
    # repeated across many line pairs). The SAME check runs in the
    # collect pass (pair not recorded) and in the replay pass (local
    # REPLACE, no pop) so the record/pop positions stay aligned: the
    # condition is a pure function of the pair's two strings, which are
    # identical in both passes. In the legacy synchronous path it
    # simply avoids the engine round-trip and the heap churn.
    _CHAR_GUARD_LEN = 100000

    def _char_diff(self, line_a, line_b):
        """Char-level diff of one line pair -- THREE modes, selected by
        the two-phase state fields (see the module docstring):

        COLLECT pass (self._char_pairs_pending is a list): record the
        pair into the list and return [] -- NO engine call. The caller
        drives the exact same walk that will paint later, so the record
        order equals the future request order.

        REPLAY pass (self._char_ops is not None): pop the precomputed
        result for this pair from the batch the engine delivered. A
        None element is the per-pair engine-failure fallback: a single
        REPLACE covering the whole line. A position overrun (impossible
        when the walk is deterministic -- the defensive branch) also
        falls back to a full REPLACE.

        LEGACY synchronous mode (neither field set: compare() called
        directly without a preceding collect, e.g. tests): ONE
        synchronous BATCHED diff_proc(DIF_CHARS) call for this single
        pair. Falls back to the Python char_diff only when the native
        API is unavailable (strict comparison, no flags).

        Returns: list of (tag, a_start, a_end, b_start, b_end) tuples
        where tag is 'equal'/'delete'/'insert'/'replace' and offsets
        are character positions into line_a / line_b. Same format as
        difflib.SequenceMatcher.get_opcodes() operating on characters.
        """
        # The long-line guard is evaluated FIRST in every mode (same
        # condition, same strings -> same outcome in collect and
        # replay, so the record/pop alignment is preserved).
        if (len(line_a) > self._CHAR_GUARD_LEN or
                len(line_b) > self._CHAR_GUARD_LEN):
            if self._char_pairs_pending is not None:
                # collect pass: DO NOT record the pair (the replay pass
                # will take this same branch instead of popping).
                return []
            return [('replace', 0, len(line_a), 0, len(line_b))]

        # COLLECT pass: record, no engine call.
        collect = self._char_pairs_pending
        if collect is not None:
            collect.append((line_a, line_b))
            return []

        # REPLAY pass: pop the precomputed result.
        ops = self._char_ops
        if ops is not None:
            pos = self._char_ops_pos
            if pos >= len(ops):
                # Defensive: the walk requested one pair more than the
                # collect pass recorded (a determinism break -- should
                # never happen). Fall back instead of crashing; the
                # remaining pops keep their positions.
                return [('replace', 0, len(line_a), 0, len(line_b))]
            self._char_ops_pos = pos + 1
            pair_ops = ops[pos]
            if pair_ops is None:
                # Engine reported this pair as failed: full REPLACE.
                return [('replace', 0, len(line_a), 0, len(line_b))]
            return pair_ops

        # LEGACY synchronous mode: one single-pair batched call.
        # No profiling sections here: the CALLER times this call with a
        # perf_counter pair (a ~60ns read -- ~1% observer effect on the
        # ~10us engine call) and books one batched Profiler.mark() per
        # chunk of pairs. The old per-pair sections cost more than the
        # pairing itself on 1M-line files and made the report lie.
        if _HAS_NATIVE_DIFF:
            result = _ct.diff_proc(
                _ct.DIF_CHARS,
                [(line_a, line_b)],    # one-pair batch
                None,                  # param2: unused for DIF_CHARS
                0,                     # algo: unused for DIF_CHARS
                self.ignore_flags,     # bitmask of DIFF_IGN_*
            )
            if result and result[0] is not None:
                return result[0]
            # Defensive: whole-call None (engine error) or an empty /
            # per-pair None result -- fall back to a single REPLACE
            # covering everything (same degraded output the replay
            # mode's None-element fallback paints).
            return [('replace', 0, len(line_a), 0, len(line_b))]
        else:
            # Native API not available — fall back to Python char_diff.
            return char_diff(line_a, line_b)

    def collect_char_pairs(self, a_text, b_text, opcodes, progress=None):
        """COLLECT pass of the two-phase native compare (phase 2, step 1).

        Splits the two raw texts into line lists (cached on the Differ
        for the following replay pass -- compare() takes them over
        instead of splitting again), then drives the EXACT pairing walk
        the replay/paint pass will run later: every opcode of the
        replace family goes through _replace_block_chunks ->
        _positional_pairs_events / _find_best_pairs_events, whose
        _char_diff calls RECORD their pairs (collect mode) instead of
        touching the engine. The yielded event lists are drained and
        discarded -- only the pair order matters, and it is identical
        to the replay pass's request order because both passes run the
        same code on the same inputs (the walk is deterministic).

        Returns the collected pairs as a list of (line_a, line_b)
        string tuples, in walk order. The caller (Command.
        _on_native_diff_done) sends the whole list to the engine in ONE
        batched asynchronous diff_proc(DIF_CHARS) call. Returns None
        when 'progress' aborted the walk (see below).

        Only 'replace' opcodes are walked: every other tag (equal /
        delete / insert / ignore) produces no char diff -- their events
        are pure line-level bookkeeping generated (again) by the
        replay pass's compare() loop.

        'progress' (optional, no-argument callable -> bool) is the
        ANTI-HANG hook of this O(N) main-thread stretch: called after
        every bounded event-list chunk the walk produces (~512 pairs of
        work), it pumps the UI / checks cancellation in the CALLER
        (Command._on_native_diff_done builds it around
        _pump_checkpoint). Returning False aborts the collect: the
        partially-filled pair list is discarded, the cached state is
        dropped, and None is returned -- the caller then unwinds
        through its normal cancel/release paths. The hook must stay
        cheap (a perf_counter read and compare when no pump is due).

        The pairing walk's per-chunk Profiler sections are suppressed
        while collecting (the collect cost lands in the caller's
        'compare:collect_pairs' section instead, so the
        positional/find_best rows keep reporting ONLY the paint-pass
        walk, one row per producer, calls=1 per chunk as before).
        """
        # Fresh state: a previous compare's leftovers (an abandoned
        # replay pass) must never leak into this collect pass.
        self._char_ops = None
        self._char_ops_pos = 0
        self._skip_align = False  # collect emits no events; never leak a
        # stale ALIGN-suppression flag into the following walk.
        # The split is profiled under the SAME tag the legacy
        # compare_lists path uses ('compare:split_lines'): in the
        # two-phase flow the split runs HERE, and before this row was
        # added its cost (~2x 70-380ms per 50MB side, depending on the
        # terminator mix -- see utils.split_lines_safe's fast paths)
        # silently inflated 'compare:collect_pairs' SELF, making the
        # row look like slow pair-recording when half of it was line
        # splitting. Same-tag instrumentation keeps the row comparable
        # across the sync / collect / replay code paths.
        Profiler.start('compare:split_lines')
        self._lines_a = split_lines_safe(a_text)
        self._lines_b = split_lines_safe(b_text)
        Profiler.stop('compare:split_lines')
        pairs = []
        aborted = False
        self._char_pairs_pending = pairs
        try:
            for tag, i1, i2, j1, j2 in opcodes:
                if tag == 'replace' and self.withdetail:
                    # Drain the chunk generator: drives the pairing
                    # (and records the pairs); the yielded event lists
                    # are throwaway. The progress hook runs at every
                    # chunk boundary -- the same granularity the paint
                    # loop's pump checkpoints use.
                    for _evs in self._replace_block_chunks(
                            self._lines_a, i1, i2, self._lines_b, j1, j2):
                        if progress is not None and not progress():
                            aborted = True
                            break
                if aborted:
                    break
        finally:
            self._char_pairs_pending = None
            if aborted:
                # Discard the partial list (a cancelled compare never
                # reaches its replay pass) and drop the cached line
                # lists so the Differ holds no text between compares.
                del pairs[:]
                self.drop_cached_state()
        if aborted:
            return None
        return pairs

    def drop_cached_state(self):
        """Drop the two-phase transient state: the cached line lists
        (kept between the collect and replay passes) and any replay
        bookkeeping. Called after a compare finishes AND on every
        abandonment path where no replay pass will follow (cancelled /
        stale / closed-tab / engine-failure), so the Differ never holds
        text between compares -- the same invariant compare() keeps.
        """
        self._lines_a = None
        self._lines_b = None
        self._char_ops = None
        self._char_ops_pos = 0
        self._char_pairs_pending = None
        self._skip_align = False

    def compare(self, a_text, b_text, opcodes=None, char_ops=None,
                align_gap_events=True):
        """Generator that yields diff events for side-by-side display,
        ONE EVENT PER YIELD -- the flat public protocol every existing
        consumer and test uses.

        Thin flattening wrapper over compare_lists() (the same walk,
        yielding the same events as bounded LISTS): `yield from` over
        each list reproduces the flat stream the monolithic generator
        produced, in the same order. See compare_lists for the full
        documentation (engine modes, text ownership, the two-phase
        collect/replay state machine).

        align_gap_events: False makes the walk skip the per-line ALIGN
        events (they exist only for the consumer's wrap-gap
        compensation -- see compare_lists); the flat stream then simply
        contains no ALIGN tuples while every other event stays
        identical. Default True keeps the historical stream.
        """
        for evlist in self.compare_lists(
                a_text, b_text, opcodes=opcodes, char_ops=char_ops,
                align_gap_events=align_gap_events):
            yield from evlist

    def compare_lists(self, a_text, b_text, opcodes=None, char_ops=None,
                      align_gap_events=True):
        """Generator that yields LISTS of diff events (bounded by
        _TAG_CHUNK / _REPLACE_CHUNK entries) for side-by-side display;
        the flat per-event protocol is compare(), which flattens these
        lists. Pure translation of the engine's opcodes into paint
        events — nothing is added, removed or re-paired here.

        The alignment mode (beautify_alignment) only affects how
        unequal-count REPLACE blocks are laid out — see
        _replace_block_chunks.

        Runs the native diff algorithm on the two raw texts, then walks
        the opcodes and yields events (A_LINE_DEL, B_LINE_ADD, A_GAP,
        B_GAP, ALIGN, A_SYMBOL_DEL, etc.) that __init__.py consumes to
        paint the compare view.

        Also populates self.diffmap with [i1, i2, j1, j2] for each
        non-equal opcode, used by jump()/copy()/select_current().

        Args:
            a_text, b_text: raw text strings for the two sides. The
                Differ does NOT store them — they are used as locals
                inside this generator and dropped when it returns.
                The caller (Command.refresh_compare) reads them fresh from
                the editor tabs (a_ed.get_text_all() / b_ed.get_text_all())
                on every compare, so between compares the Differ holds
                zero text bytes — only config + diffmap. This is the
                intended design: the editor tabs are the source of
                truth for the text; a Python-side copy would be a
                transient duplicate with no consumer after compare()
                returns (verified by grep — __init__.py reads only
                self.diff.diffmap after the compare loop, never
                self.diff.a_text / self.diff.b_text).
                TWO-PHASE EXCEPTION: after a collect_char_pairs() pass,
                the line lists are already cached on the Differ and
                a_text/b_text are passed as None — the cached lists are
                taken over (no second split) and dropped at the end of
                the generator, preserving the no-text-between-compares
                invariant.
            opcodes: precomputed line-level opcodes for a_text/b_text —
                the result the engine's background thread delivered to
                Command._on_native_diff_done (the diff_proc callback
                form started by start_async_line_diff). When given,
                this generator does NOT run the engine itself: it walks
                the given opcodes directly. None (default) runs the
                engine synchronously here, on the caller's thread,
                while the generator is being consumed.
            char_ops: precomputed BATCHED char-level results — the
                per-pair opcode list the engine's background thread
                delivered to Command._on_char_diff_done (the BATCHED
                diff_proc(DIF_CHARS) callback; element k belongs to
                the k-th pair the COLLECT pass recorded, and the walk
                requests them in that same order). When given (any
                list, including []), _char_diff runs in REPLAY mode:
                no engine call happens while painting — each pair pops
                its ready result. None (default) keeps the legacy
                per-pair synchronous behavior (collect/replay modes
                inactive).
            align_gap_events: True (default) emits the historical full
                stream, ALIGN events included. False skips every ALIGN
                event: they exist ONLY so the paint consumer can add
                compensating gaps for matched pairs that wrap to
                different visual heights; when the consumer already
                knows no line wraps to 2+ visual rows
                (Command.refresh_compare's wrap-count check), each
                ALIGN would be consumed as a NO-OP -- producing,
                dispatching and timing ~1M of them on a 1M-line
                compare is pure overhead. The replay pop positions do
                not change (pops advance on CHANGED pairs only;
                ALIGN skipping touches equal pairs and the post-pair
                ALIGN appends, never the pops), so the collect/replay
                alignment invariant is unaffected.

        NOTE: _realign_opcodes (the VS Code-style post-pass in
        differ_python.py) is NOT applied to native opcodes. Native engines
        CAN occasionally emit the INSERT+EQUAL(trivial)+DELETE pattern it
        fixes (GNU diffutils Myers is not immune to the LCS tie-breaking
        issue) — but the native path is deliberately algo-faithful: the
        engine's hunks are rendered as-is, the way WinMerge / GNU
        diffutils side-by-side output does, with no re-pairing. If the
        misalignment artifact ever becomes a problem here, port
        _realign_opcodes over — it operates on plain opcode lists and
        transfers as-is (feed it the local a_lines for the trivial-EQUAL
        content check).
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
        # old report show replace_block:positional_pair as a fake ~20s
        # bottleneck. Only sections that close before a yield remain
        # (compare:algorithm, per-chunk compare:positional_pairs /
        # compare:find_best_pairs).

        # REPLAY-mode setup: the batched char results the engine
        # delivered. Assignment (not an if-guard) so a compare() call
        # without char_ops (legacy synchronous mode) ALWAYS deactivates
        # any replay state left by an abandoned previous pass -- a
        # stale _char_ops would make _char_diff pop stale results
        # instead of calling the engine. Reset again at the natural end
        # of the generator; drop_cached_state() covers the abandonment
        # paths.
        self._char_ops = char_ops
        self._char_ops_pos = 0
        # ALIGN suppression flag of THIS walk (see the docstring's
        # align_gap_events paragraph). Reset at the walk's end below,
        # like the replay fields.
        self._skip_align = not align_gap_events

        self.diffmap = []
        if opcodes is None:
            # Synchronous mode: the engine runs HERE, on the caller's
            # thread, while the generator is being consumed.
            Profiler.start('compare:algorithm')
            if self.diff_algorithm == 'native_histogram':
                diff = CudaDiffNativeMatcher(
                    None, algo=CudaDiffNativeMatcher._ALGO_HISTOGRAM,
                    flags=self.ignore_flags,
                    a_text=a_text, b_text=b_text)
            else:
                # Default to myers (covers 'native_myers' and any
                # unexpected value — myers is the fastest on large/very
                # different files and the plugin's default).
                diff = CudaDiffNativeMatcher(
                    None, algo=CudaDiffNativeMatcher._ALGO_MYERS,
                    flags=self.ignore_flags,
                    a_text=a_text, b_text=b_text)

            # get_opcodes() calls diff_proc (the native engine) — this is
            # where the actual algorithm runs. The raw texts go to the
            # engine VERBATIM — the engine splits them into lines itself,
            # so no Python-side join happens.
            opcodes = diff.get_opcodes()
            Profiler.stop('compare:algorithm')

            # RELEASE THE MATCHER NOW — we already have the opcodes and
            # the matcher no longer serves any purpose. Without this
            # `del`, the matcher keeps holding its self._a_text /
            # self._b_text refs through the entire paint loop below,
            # which means we end up keeping THREE copies of each text's
            # character data alive at once (the local param a_text/
            # b_text, the matcher's _a_text/_b_text, AND the a_lines/
            # b_lines we're about to build). For a 33k-line / 10MB file
            # that's the difference between ~60MB and ~40MB peak memory
            # during the paint loop.
            del diff
        # else: background mode — 'opcodes' is the result the engine's
        # background thread already computed (delivered to
        # Command._on_native_diff_done via the diff_proc callback). The
        # engine does not run here, so there is no matcher to release.
        # The engine's compute time IS profiled though: kick-off starts
        # an async section pair (Profiler.start_async_pair) under the
        # same names used here in the sync path, closed in the
        # completion callback / on cancel — so line_diff:native_engine
        # (and its compare:algorithm wrapper) appear in the report with
        # the kick-off -> callback wall time instead of the wait being
        # miscounted as refresh's own work.

        # No _realign_opcodes call — the native path renders the engine's
        # hunks faithfully (algo-faithful mode). See docstring for details.

        # Build the line lists LOCALLY for painting — split_lines_safe is
        # O(N) per call, so we split once here and pass the lists to
        # _replace_block_chunks / _plain_replace_chunks. Both
        # a_text/b_text and a_lines/b_lines are locals inside this
        # generator: they go out
        # of scope when compare() returns, so they are garbage-collected
        # after the compare finishes. Between compares, the Differ holds
        # zero text bytes — only config + diffmap.
        #
        # TWO-PHASE REPLAY PASS: the COLLECT pass already split the
        # texts and cached the line lists on the Differ — take them over
        # (a_text/b_text are None here) instead of splitting again: the
        # split costs ~3.3s on a 1M-line compare, real work that would
        # otherwise run twice. Ownership moves here: the cache fields
        # are cleared, and the lists die with the generator's locals.
        #
        # SEQUENTIAL SPLIT (legacy path) — drop each raw text the instant
        # its line list is built. split_lines_safe returns INDEPENDENT string
        # objects per line (CPython string slices are COPIES of the
        # char data, not views into the source str's buffer), so a_lines
        # owns its own char data and a_text is redundant the moment
        # a_lines exists. Releasing a_text BEFORE splitting b_text means
        # the split-phase peak is 3× raw text (b_text + a_lines +
        # b_lines-being-built) instead of 4× (a_text + b_text + a_lines
        # + b_lines-being-built). On a 33k-line / 10MB file that's a
        # ~3.5MB reduction in the OVERALL peak memory during compare —
        # the peak the user actually sees when the diff runs.
        # Profiled as its own section: on a 1M-line / 50MB compare this
        # split costs ~3.3s (two full-text passes + 2M line-string
        # allocations) — real work that used to vanish into the
        # consumer's section SELF time. The Python differ splits in
        # refresh_compare under the SAME tag, so the row is comparable
        # across algorithms.
        if self._lines_a is not None:
            # replay pass: cached by collect_char_pairs()
            a_lines = self._lines_a
            self._lines_a = None
            b_lines = self._lines_b
            self._lines_b = None
            del a_text, b_text
        else:
            Profiler.start('compare:split_lines')
            a_lines = split_lines_safe(a_text)
            del a_text
            b_lines = split_lines_safe(b_text)
            del b_text
            Profiler.stop('compare:split_lines')

        # Event production for REPLACE blocks is instrumented per chunk
        # ('compare:positional_pairs' / 'compare:find_best_pairs' open
        # AFTER the chunk list is built and close BEFORE it is yielded —
        # one row per producer, so the report shows WHICH of the two
        # pairing modes a block used); equal/delete/insert/ignore
        # production is trivial tuple loops and stays uninstrumented — its
        # cost lands in the consumer's section, which is where it runs.
        #
        # This generator yields LISTS (one per bounded chunk; compare()
        # flattens them for the flat protocol). The trivial tags build
        # their lists here with the same _TAG_CHUNK bound, so a 1M-line
        # 'equal' opcode streams its ALIGNs in bounded pieces instead of
        # materializing them all at once.
        skip_align = self._skip_align
        for tag, i1, i2, j1, j2 in opcodes:
            if tag not in ('equal', 'ignore'):
                # 'ignore' hunks are suppressed differences, not diff
                # blocks: jump()/copy()/select_current() must skip them.
                self.diffmap.append([i1, i2, j1, j2])
            if tag == 'equal':
                # ALIGN for each matched pair so the wrapper can add
                # compensating gaps when wrap is on. Skipped ENTIRELY
                # when the consumer suppressed ALIGN events
                # (skip_align): the events would be consumed as no-ops
                # there (no wrap-height mismatch is possible).
                if not skip_align:
                    for k0 in range(i1, i2, self._TAG_CHUNK):
                        k2 = k0 + self._TAG_CHUNK
                        if k2 > i2:
                            k2 = i2
                        yield [(ALIGN, k, j1 + (k - i1))
                               for k in range(k0, k2)]
            elif tag == 'delete':
                # Lines i1..i2-1 in A are deleted; gap in B after line j1-1.
                # (j1 == j2 for 'delete'.) The gap compensates for A lines
                # [i1, i2).
                evs = [(B_GAP, j1, i1, i2)]
                append = evs.append
                for y in range(i1, i2):
                    append((A_LINE_DEL, y))
                    if len(evs) >= self._TAG_CHUNK:
                        yield evs
                        evs = []
                        append = evs.append
                if evs:
                    yield evs
            elif tag == 'insert':
                # Lines j1..j2-1 in B are inserted; gap in A after line i1-1.
                # (i1 == i2 for 'insert'.) The gap compensates for B lines
                # [j1, j2).
                evs = [(A_GAP, i1, j1, j2)]
                append = evs.append
                for y in range(j1, j2):
                    append((B_LINE_ADD, y))
                    if len(evs) >= self._TAG_CHUNK:
                        yield evs
                        evs = []
                        append = evs.append
                if evs:
                    yield evs
            elif tag == 'replace':
                # Each REPLACE block's events are BUILT into lists (one
                # list per bounded chunk of pairs) and yielded only after
                # the producing section closed: no Profiler section is
                # ever open across a yield. See the NOTE above.
                if self.withdetail:
                    for evlist in self._replace_block_chunks(
                            a_lines, i1, i2, b_lines, j1, j2):
                        yield evlist
                else:
                    for evlist in self._plain_replace_chunks(
                            a_lines, i1, i2, b_lines, j1, j2):
                        yield evlist
            elif tag == 'ignore':
                # Suppressed all-blank hunk (DIFF_IGN_BLANK_LINES) —
                # a WinMerge-style "ignored difference". NOT counted as
                # a difference: no diffmap entry, no bookmarks, no char
                # details. Paint both sides' lines with the ignored
                # color and compensate the length mismatch with an
                # ignored gap so lines below stay aligned.
                da = i2 - i1
                db = j2 - j1
                evs = []
                append = evs.append
                if da > db:
                    # Side A has extra blank lines: gap in B before line
                    # j2 (after B's hunk lines), compensating A's extra
                    # lines [i1 + db, i2).
                    append((B_GAP_IGN, j2, i1 + db, i2))
                elif db > da:
                    # Side B has extra blank lines: gap in A before line
                    # i2, compensating B's extra lines [j1 + da, j2).
                    append((A_GAP_IGN, i2, j1 + da, j2))
                if not skip_align:
                    for k in range(da if da < db else db):
                        append((ALIGN, i1 + k, j1 + k))
                        if len(evs) >= self._TAG_CHUNK:
                            yield evs
                            evs = []
                            append = evs.append
                for y in range(i1, i2):
                    append((A_LINE_IGN, y))
                    if len(evs) >= self._TAG_CHUNK:
                        yield evs
                        evs = []
                        append = evs.append
                for y in range(j1, j2):
                    append((B_LINE_IGN, y))
                    if len(evs) >= self._TAG_CHUNK:
                        yield evs
                        evs = []
                        append = evs.append
                if evs:
                    yield evs

        if _bm_start is not None:
            _bm_elapsed = time.perf_counter() - _bm_start
            print('Differ: compare took {:.1f}ms '
                  '(algo={}, a={}lines, b={}lines, opcodes={}diffs)'.format(
                      _bm_elapsed * 1000,
                      self.diff_algorithm,
                      len(a_lines), len(b_lines),
                      len(self.diffmap)))

        # End of the walk: drop the two-phase transient state so the
        # Differ holds no text / no engine results between compares
        # (the line lists were taken over as locals above and die with
        # this frame; _char_ops is the engine's result list, released
        # here). Abandonment paths (an exception in the paint consumer
        # that kills this generator early) are covered by
        # drop_cached_state() in the Command callbacks, and the next
        # collect pass re-initializes everything anyway.
        self._char_ops = None
        self._char_ops_pos = 0
        self._lines_a = None
        self._lines_b = None
        self._skip_align = False

    # Profiling row the char-level engine calls are marked under (one
    # row for the whole _char_diff call: the wrapper's own cost is a
    # fraction of a microsecond and not worth a second row).
    _CHAR_ROW = ('char_diff:native_engine' if _HAS_NATIVE_DIFF
                 else 'char_diff:python_engine')

    # Pair chunk for huge REPLACE blocks: events are produced (and
    # profiled) per chunk, so neither the event list nor the open
    # 'compare:positional_pairs' frame grows with the block size.
    _REPLACE_CHUNK = 512
    # Event-list bound for the non-REPLACE tags (equal / delete /
    # insert / ignore) in compare_lists(): a 1M-line 'equal' opcode
    # would otherwise materialize its whole ALIGN stream at once. Same
    # role as _REPLACE_CHUNK; one list stays well under a few hundred
    # KB even for 4-tuple events.
    _TAG_CHUNK = 4096

    def _positional_pairs_events(self, out, a, alo, b, blo, count):
        """Pair the k-th A line with the k-th B line, top-down, for
        `count` pairs, APPENDING each pair's events to `out` (a builder,
        not a generator — no Profiler section is ever open across a
        yield). For identical lines append ALIGN only; for different
        lines run the char diff and append its paint events, then ALIGN.
        No heuristics — this is the sdiff/WinMerge alignment, also used
        by the beautify mode when line counts are equal.

        Profiling: engine calls are timed with perf_counter (a ~60ns
        read — ~1% observer effect on a ~10us engine call) and booked
        ONCE per chunk via Profiler.mark() — but ONLY in the legacy
        synchronous mode (neither collecting nor replaying): in the
        two-phase flow the engine time is booked by the async section
        pair around the BATCHED diff_proc(DIF_CHARS) job
        (char_diff:native_engine, calls=1), so per-pair timing here
        would pollute that row with record/pop costs. No per-pair
        sections: the old design's 800k char_diff +
        800k char_diff:native_engine sections per 1M-line compare cost
        more than the pairing itself and made the report lie.
        """
        # Legacy synchronous mode only: time the per-pair engine calls.
        prof_on = (Profiler.enabled and
                   self._char_pairs_pending is None and
                   self._char_ops is None)
        row = self._CHAR_ROW
        char_diff_call = self._char_diff
        append = out.append
        if prof_on:
            eng_dt = 0.0
            eng_n = 0
            eng_max = 0.0
        # COLLECT pass: every event appended to `out` here is drained and
        # DISCARDED by collect_char_pairs (only the pair RECORD order
        # matters), so building the ALIGN / A_LINE_CHANGE / B_LINE_CHANGE
        # tuples per pair is dead work -- ~2 tuple allocations + a method
        # call per pair on 1M-line compares. The pairing DECISIONS and the
        # _char_diff record calls run exactly as before (the record order
        # stays identical); only the discarded writes are skipped. The
        # replay pass (not collecting) still emits the full event set.
        collecting = self._char_pairs_pending is not None
        # REPLAY fast path: _char_ops bound + not collecting means the
        # per-pair engine result is a positional POP. Inlined here so
        # the 800k _char_diff calls (a method call + the 3-mode branch
        # switch per pair on a 1M-line compare) collapse into direct
        # list ops. The logic is an EXACT copy of _char_diff's replay
        # branches: the long-line guard REPLACES the pop (no position
        # advance, same fallback list), a None element or a position
        # overrun degrades to a full-line REPLACE, and the pop position
        # advances per pair exactly as _char_diff does. COLLECT mode
        # runs the lean record-only loop below; the LEGACY synchronous
        # mode keeps the original loop below (engine calls + event
        # emission).
        replay_ops = self._char_ops if not collecting else None
        if replay_ops is not None:
            guard = self._CHAR_GUARD_LEN
            ops_len = len(replay_ops)
            emit_align = not self._skip_align
            # Pop position kept in a LOCAL for the whole loop: ONE
            # attribute write-back at the end instead of two attribute
            # accesses per changed pair (~1.6M attribute ops on a
            # 1M-line compare). The inlined emission below never
            # touches the field, and the guard/overrun/None fallbacks
            # below advance the position exactly like _char_diff's
            # replay branches do.
            pos = self._char_ops_pos
            for k in range(count):
                ai, bj = alo + k, blo + k
                la = a[ai]
                lb = b[bj]
                if la == lb:
                    if emit_align:
                        append((ALIGN, ai, bj))
                    continue
                # Inlined _char_diff replay pop + _char_diff_pair_events:
                # the 800k pair_events method calls (one per changed pair
                # on a 1M-line compare) collapse into direct list ops, and
                # the pair's line-level work leaves as ONE composite
                # PAIR_CHANGED event (no trailing ALIGN -- the consumer's
                # PAIR_CHANGED branch runs the pair's wrap-compensation,
                # gated by the same align_gap_events flag that gated the
                # old ALIGN consumption). The pop/guard/None/overrun
                # fallbacks are an EXACT copy of _char_diff's replay
                # branches: the long-line guard REPLACES the pop (no
                # position advance), a None element or a position overrun
                # degrades to a full-line REPLACE, and a popped pair emits
                # exactly the ops it carries.
                if len(la) > guard or len(lb) > guard:
                    pair_ops = None
                elif pos >= ops_len:
                    pair_ops = None
                else:
                    pair_ops = replay_ops[pos]
                    pos += 1
                if pair_ops is None:
                    # full-line REPLACE (guard / engine-failed pair /
                    # overrun) -- the same ops list the old fallback built
                    append((A_SYMBOL_DEL, ai, 0, len(la)))
                    append((B_SYMBOL_ADD, bj, 0, len(lb)))
                    append((PAIR_CHANGED, ai, bj, 1, 1))
                else:
                    deca = 0
                    decb = 0
                    for tag, a_start, a_end, b_start, b_end in pair_ops:
                        if tag == 'replace':
                            deca += 1
                            decb += 1
                            append((A_SYMBOL_DEL, ai, a_start,
                                    a_end - a_start))
                            append((B_SYMBOL_ADD, bj, b_start,
                                    b_end - b_start))
                        elif tag == 'delete':
                            deca += 1
                            append((A_SYMBOL_DEL, ai, a_start,
                                    a_end - a_start))
                        elif tag == 'insert':
                            decb += 1
                            append((B_SYMBOL_ADD, bj, b_start,
                                    b_end - b_start))
                    append((PAIR_CHANGED, ai, bj, deca, decb))
            self._char_ops_pos = pos
            return
        # COLLECT pass, lean loop: the only output that survives
        # collect_char_pairs is the pair RECORD ORDER (every event
        # append below would be drained and discarded). Inlining
        # _char_diff's collect branch (guard check -> record) removes
        # one method call per changed pair (~800k on a 1M-line
        # compare); the guard/negation order mirrors _char_diff
        # EXACTLY, so the recorded pairs -- and their order -- are
        # identical to what the old record loop produced.
        collect = self._char_pairs_pending
        if collect is not None:
            guard = self._CHAR_GUARD_LEN
            collect_append = collect.append
            for k in range(count):
                la = a[alo + k]
                lb = b[blo + k]
                if (la != lb and len(la) <= guard
                        and len(lb) <= guard):
                    collect_append((la, lb))
            return
        # LEGACY synchronous mode (collect finished, no replay data).
        emit_align = not self._skip_align
        for k in range(count):
            ai, bj = alo + k, blo + k
            if a[ai] == b[bj]:
                if emit_align:
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
                # line-level work (incl. its wrap-compensation, the old
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
            pair via the engine), and show leftover lines on the longer
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
            # anchor + prefix/suffix scoring + threshold and staggering.
            # Produced into ONE list (beautify is opt-in; the recursive
            # scorer makes chunking invasive). Char diffs inside are
            # perf_counter-timed and booked as batched marks (legacy
            # mode only). The producing section is suppressed during
            # the COLLECT pass: the collect pass's whole cost lands in
            # the caller's 'compare:collect_pairs' section, so these
            # rows keep reporting ONLY the paint-pass walk.
            _collecting = self._char_pairs_pending is not None
            if not _collecting:
                Profiler.start('compare:find_best_pairs')
            evs = []
            self._find_best_pairs_events(evs, a, alo, ahi, b, blo, bhi)
            if not _collecting:
                Profiler.stop('compare:find_best_pairs')
            yield evs
            return

        # Shared fast path (both modes): positional pairing for the
        # common line count, chunked; then leftovers on the longer side
        # as plain added/deleted lines against a gap at the bottom of
        # the shorter side's block. Section suppression during the
        # COLLECT pass, same rationale as the beautify branch above.
        _collecting = self._char_pairs_pending is not None
        # inline min(da, db): one builtin call per opcode per walk
        # (~400k calls on a 1M-line compare, each a traced call under
        # the cProfile layer)
        common = da if da < db else db
        k = 0
        while k < common:
            n = self._REPLACE_CHUNK
            if common - k < n:
                n = common - k
            if not _collecting:
                Profiler.start('compare:positional_pairs')
            evs = []
            self._positional_pairs_events(evs, a, alo + k, b, blo + k, n)
            if not _collecting:
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
        The SEARCH marks run in every mode (the search itself runs in
        every pass); the CHAR-DIFF timing runs only in the legacy
        synchronous mode (in the two-phase flow the engine time is
        booked by the async pair around the batched DIF_CHARS job).
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
        # char-diff timing: legacy synchronous mode only (see docstring)
        _time_engine = (prof_on and
                        self._char_pairs_pending is None and
                        self._char_ops is None)
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
            if self._char_pairs_pending is None and not self._skip_align:
                out.append((ALIGN, best_i, best_j))
        else:
            if _time_engine:
                _t0 = time.perf_counter()
                ops = self._char_diff(a_line, b_line)
                dt = time.perf_counter() - _t0
                Profiler.mark(self._CHAR_ROW, dt, 1, dt)
            else:
                ops = self._char_diff(a_line, b_line)
            # COLLECT pass: the pair events are drained and discarded by
            # collect_char_pairs (same dead-write skip as
            # _positional_pairs_events) -- the record call above already
            # did the work that matters.
            if self._char_pairs_pending is None:
                # composite PAIR_CHANGED carries the pair's line-level
                # work incl. its wrap-compensation (the old trailing
                # ALIGN's job)
                self._char_diff_pair_events(out, best_i, best_j, ops)

        # Recurse on the part after the best pair
        self._find_best_pairs_events(out, a, best_i + 1, ahi,
                                     b, best_j + 1, bhi)

    def _char_diff_pair_events(self, out, ai, bj, ops):
        """Append character-level diff events for a single line pair,
        given the char-level opcodes from the engine's char_diff().
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
        common = da if da < db else db
        flush_at = self._REPLACE_CHUNK * 4
        evs = []
        append = evs.append
        emit_align = not self._skip_align
        for k in range(common):
            if emit_align:
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
