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
  align_joined.align_block_plan) -- the only difference is what
  _char_diff does (record the pair vs pop the precomputed ops). The
  walk is a deterministic function of (a_lines, b_lines, opcodes,
  config), so both passes request char diffs in exactly the same order
  and the replay's pops stay aligned with the collect's records. The
  line lists are split ONCE (in collect_char_pairs) and cached on the
  Differ for the replay pass, so the expensive split does not run
  twice; the same goes for the beautify mode's ALIGNMENT PLANS -- the
  joined-block search (align_joined.py) runs in the collect pass, its
  per-block plans are cached on the Differ (see _take_align_plan) and
  the replay pass reuses them verbatim, so the whole search runs once
  per compare, not twice. The legacy slow beautify (Method 2 'slow'
  of the align_by_similarity dropdown -> _find_best_pairs_events)
  takes no plan cache: its recursive search
  runs in both passes, but the walk stays deterministic, so the
  collect pass's _char_diff RECORDS and the replay pass's pops keep
  their order-matching guarantee exactly the same way.

Ignore options: the plugin's 'ignoreopt.*' settings are collected into
a DIFF_IGN_* bitmask (see build_ignore_flags) and applied to BOTH the
line-level diff (diff_proc DIF_TEXTS) and the char-level detail diff
(batched diff_proc DIF_CHARS). The pure-Python algorithms in
differ_python.py do NOT support ignore options — they always compare
strictly.

Break chars: the word-break characters of the DIF_CHARS tokenizer are
configurable via the plugin's 'algorithm.break_chars' setting (default
".,:;?[](){}<=>`'!\"#$%&^~\\|@+-*/" -- WinMerge's "Word break
characters" options default; see DEFAULT_BREAK_CHARS /
normalize_break_chars). WinMerge research behind that default: the
engine's stringdiffs.cpp Init() hard-codes only a small ",.;:" fallback
that runs until SetBreakChars() is called, but WinMerge's Options
dialog stores the long list above as the setting's default and pushes
it into the engine on every compare -- so the long list is the
effective default, and the plugin mirrors it (an earlier unreleased
draft of this option used ",.;:" under the assumption it was
WinMerge's default). The configured string is carried on the Differ
(diff.break_chars, set next to diff.ignore_flags by
Command.refresh_compare) and threaded into EVERY DIF_CHARS call of a
compare -- one value per whole batch, exactly like the ignore bitmask.
Only the char-level tokenizer reads it; the line-level DIF_TEXTS diff
ignores it. The pure-Python algorithms do not use it either: their
tokenizer breaks words at EVERY punctuation character.

Code is intentionally duplicated from differ_python.py to allow
independent evolution of the native and Python codepaths. As more
diff logic moves into the Pascal native engine, this file will shrink
to a thin wrapper around cudatext.diff_proc if God wills.
"""

import time
from .py_algo.char_diff import char_diff
from .profiling import Profiler
from .utils import split_lines_safe
from .align_joined import AlignEngine, align_block_plan
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

# Default word-break characters of the DIF_CHARS tokenizer -- WinMerge's
# "Word break characters" OPTIONS default:
#     .,:;?[](){}<=>`'!"#$%&^~\|@+-*/
# (WinMerge research: stringdiffs.cpp's Init() hard-codes only the small
# fallback ",.;:" that runs until SetBreakChars() is called, but the
# WinMerge Options dialog stores the long list above as the setting's
# default and calls SetBreakChars() with it on every compare -- so the
# long list is what the engine effectively runs with in normal use; the
# plugin and the CudaText diff_proc default mirror it). Exposed as the
# plugin option 'differ2.algorithm.break_chars' (config dialog,
# 'algorithm' chapter); the configured value is sanitized by
# normalize_break_chars() and threaded into every diff_proc(DIF_CHARS)
# call: every character of the string is its own token and a word
# boundary, an EMPTY string disables punctuation breaking (words split
# on whitespace/EOL only).
DEFAULT_BREAK_CHARS = ".,:;?[](){}<=>`'!\"#$%&^~\\|@+-*/"


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


def normalize_break_chars(value):
    """Sanitize a configured break-chars value into a str.

    The option lives in settings/cuda_differ2.json, so a hand-edited
    file could hold a non-string (number, list, null...) -- and the
    native engine's diff_proc(DIF_CHARS) accepts a str only (any other
    type is an API error, which would fail EVERY compare). Returns the
    value unchanged when it is a str (including the empty string -- a
    legitimate setting meaning "punctuation never breaks words");
    anything else (None, a wrong type) falls back to
    DEFAULT_BREAK_CHARS so one broken value never kills the plugin.
    """
    if isinstance(value, str):
        return value
    return DEFAULT_BREAK_CHARS


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
    # sites; these class attributes name the two native engine choices.
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


def start_async_char_diff(pairs, flags, callback, break_chars=DEFAULT_BREAK_CHARS):
    """Start a BATCHED char-level compare of ALL line pairs in ONE
    background engine job: the asynchronous form of
    cudatext.diff_proc(DIF_CHARS) (the `callback` argument).

    'pairs' is the list of (text1, text2) string tuples collected by
    Differ.collect_char_pairs() -- the whole batch is marshalled into
    the engine in ONE API call, and the engine compares every pair on
    its own OS thread (it re-checks the job's cancel flag between
    pairs, so a batch of hundreds of thousands of small pairs also
    stops promptly on DIF_CANCEL).

    'break_chars' is the tokenizer's word-break characters (see
    DEFAULT_BREAK_CHARS): the value of the plugin's
    'differ2.algorithm.break_chars' setting, carried on the Differ as
    diff.break_chars. ONE value for the whole batch -- the engine
    applies the same set to every pair (exactly like 'flags').

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
        _ct.DIF_CHARS, pairs, None, 0, flags, callback,
        break_chars=break_chars)
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


def sync_char_diff(pairs, flags, break_chars=DEFAULT_BREAK_CHARS):
    """Synchronous BATCHED char-level compare: the whole 'pairs' list
    in ONE blocking diff_proc(DIF_CHARS) call.

    Fallback for the rare case start_async_char_diff() could not start
    the background job: still ONE call for the whole batch (the
    per-pair overhead that dominated the old design is paid once), but
    it blocks the main thread for the whole batch time -- acceptable
    only as an emergency path, never the normal flow.

    'break_chars' -- see start_async_char_diff (ONE value for the
    whole batch, from the 'differ2.algorithm.break_chars' setting).

    Returns the per-pair opcode list (same format as
    start_async_char_diff's callback argument), or None on engine
    error (the caller then paints every pair as a full REPLACE via the
    replay mode's None-element fallback).
    """
    if not _HAS_NATIVE_DIFF or not pairs:
        return None
    return _ct.diff_proc(
        _ct.DIF_CHARS, pairs, None, 0, flags, break_chars=break_chars)


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
    The line-level alignment of the FAST beautify mode lives in
    align_joined.py (engine-agnostic, shared with differ_python); the
    legacy SLOW beautify (align_by_similarity2 ->
    _find_best_pairs_events, the original pure-Python recursive search)
    is kept here verbatim; everything else that overlapped with
    differ_python.Differ (event constants, _replace_block_chunks,
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

        self.break_chars is the DIF_CHARS tokenizer's word-break
        characters ('differ2.algorithm.break_chars' config setting,
        sanitized by normalize_break_chars). Command.refresh_compare
        sets it next to ignore_flags; it is threaded into every
        DIF_CHARS call (batched and legacy) and ignored by DIF_TEXTS.
        The default is DEFAULT_BREAK_CHARS (WinMerge's "Word break
        characters" options list) -- identical to the engine's own
        default, so an unset value and an explicitly-set default behave
        the same.

        The Differ holds NO text between compares — neither raw text
        nor line lists. a_text / b_text are passed directly to
        compare() by the caller (Command.refresh_compare), used as locals
        inside compare() to drive the engine + painting, and dropped
        when compare() returns. Between compares, the Differ holds
        only config (withdetail / diff_algorithm / the two flags of
        the align_by_similarity dropdown /
        absorb_trivial_equal_blocks /
        ignore_flags / break_chars) and the diffmap (line-index
        tuples, small). The text itself stays in the editor tabs'
        Pascal-side buffers (a_ed / b_ed), which are the source of
        truth; the Python-side copy is built fresh on each compare via
        a_ed.get_text_all() / b_ed.get_text_all().
        """
        self.withdetail = True
        self.diff_algorithm = 'native_myers'
        self.align_by_similarity = False
        # The slow-method bit of the SINGLE 'align_by_similarity'
        # dropdown ('differ2.algorithm.beautify.align_by_similarity' =
        # 'fast' / 'slow' / 'off'): True = Method 2, the OLD slow
        # pure-Python beautify -- the original recursive best-pair
        # search (_find_best_pairs_events: longest unique exact-match
        # anchor, else O(N*M) prefix/suffix scoring per block). Kept
        # side by side with the fast joined-block mapper above so the
        # two methods can be A/B-switched from the toolbar. Exactly
        # one of the two flags is on by construction (get_config feeds
        # them from the one dropdown value); defensively, this one
        # wins if a directly constructed Differ sets both.
        self.align_by_similarity2 = False
        # 'differ2.algorithm.beautify.absorb_trivial_equal_blocks' --
        # when True, the engine's finished opcodes go through
        # _absorb_trivial_equal_blocks (the module-level function at
        # the END of this file) before any phase walks them. Default
        # False: raw engine output.
        self.absorb_trivial_equal_blocks = False
        self.ignore_flags = 0  # DIFF_IGN_* bitmask (see build_ignore_flags)
        # Word-break chars of the DIF_CHARS tokenizer (see
        # normalize_break_chars); set by Command.refresh_compare from
        # cfg['break_chars'] next to ignore_flags.
        self.break_chars = DEFAULT_BREAK_CHARS
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
        # --- joined-block aligner state (see align_joined.py) ---
        # COLLECT pass: the alignment plan computed for each unequal
        # REPLACE block is appended here in walk order (bounded by
        # _PLAN_CACHE_BUDGET plan ops); the REPLAY pass pops them back
        # in the same order (the same collect/replay determinism the
        # batched char diff already relies on -- the plan is a pure
        # function of the lines/opcodes/config both passes share) and
        # never runs the search twice. None = no cache (legacy
        # synchronous compares, or the budget ran out) -> compute
        # fresh. Cleared by drop_cached_state() and at the walk's end.
        self._align_plans = None
        # Pop cursor inside self._align_plans (the cache is indexed,
        # not popped, so the collect pass's list is never mutated).
        self._align_plans_pos = 0
        # Remaining plan-op budget of the cache (see _PLAN_CACHE_BUDGET).
        self._align_plan_budget = 0

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
                None,                  # callback: synchronous call
                self.break_chars,      # tokenizer word-break chars
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
        instead of splitting again), optionally ABSORBS trivial equal
        blocks in the opcodes (the beautify pass -- see the NOTE in
        compare_lists), then drives the EXACT pairing walk the
        replay/paint pass will run
        later: every opcode of the replace family goes through
        _replace_block_chunks -> _positional_pairs_events /
        align_joined.align_block_plan, whose _char_diff calls RECORD
        their
        pairs (collect mode) instead of touching the engine. The
        yielded event lists are drained and discarded -- only the pair
        order matters, and it is identical to the replay pass's request
        order because both passes run the same code on the SAME
        opcodes (the walk is deterministic).

        The absorb pass (when the option is on) mutates the CALLER's
        opcode list IN PLACE (opcodes[:] = ...): _on_native_diff_done
        hands the SAME list object to the char batch's callback
        (functools.partial) and to the paint pass
        (_finish_native_compare), so every later phase walks the
        absorbed structure this pass recorded its pairs against --
        the collect/replay pairing invariant (char result k answers
        the k-th recorded pair) REQUIRES both passes to see identical
        opcodes.

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
        # The joined-block aligner's plan cache starts FRESH here: the
        # collect pass computes the plans, the replay pass (the
        # compare_lists call that follows) pops them. Budget-capped so
        # a pathological compare (two huge entirely-different files ->
        # one block, ~1M plan ops) cannot cache unbounded memory; once
        # the budget is spent the remaining blocks simply recompute
        # their plans in the replay pass.
        self._align_plans = []
        self._align_plans_pos = 0
        self._align_plan_budget = self._PLAN_CACHE_BUDGET
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

        # Opcode beautify pass -- differ_python.py carries its OWN copy
        # of the same code (deliberate duplication: the two codepaths
        # evolve independently); see the NOTE in compare_lists for why
        # the native engines need it too. Runs here, once, on the
        # engine's finished result -- and ONLY when the option
        # 'differ2.algorithm.beautify.absorb_trivial_equal_blocks' is
        # on: this pass's pair walk AND the replay pass's paint walk
        # must both use the absorbed structure (the char-ops pop order
        # depends on it), and the line lists the trivial-content check
        # reads were just split above -- no second split anywhere.
        # In-place slice assignment so the caller's list object -- the
        # one that travels to _on_char_diff_done /
        # _finish_native_compare -- BECOMES the absorbed list. (The
        # engine always delivers a plain Python list; the isinstance
        # guard is belt-and-braces for any future caller passing a
        # tuple.)
        if self.absorb_trivial_equal_blocks:
            if not isinstance(opcodes, list):
                opcodes = list(opcodes)
            # Umbrella section over the whole pass + nested per-step
            # marks (the report's 'Beautify passes' block): collect
            # runs on the main thread, so regular sections/marks nest
            # fine -- the umbrella's SELF is the pass minus its steps
            # (list copy, guards, fixpoint bookkeeping).
            Profiler.start('absorb_trivial_equal_blocks')
            absorbed = _absorb_trivial_equal_blocks(
                self._lines_a, opcodes,
                Profiler.mark if Profiler.enabled else None)
            Profiler.stop('absorb_trivial_equal_blocks')
            if absorbed is not opcodes:
                opcodes[:] = absorbed

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
        self._align_plans = None
        self._align_plans_pos = 0
        self._align_plan_budget = 0

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

        The alignment mode (align_by_similarity / the legacy
        align_by_similarity2) only affects how
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

        NOTE: the opcode beautify pass (absorb trivial equal blocks).
        When 'differ2.algorithm.beautify.absorb_trivial_equal_blocks' is
        ON, the engine's finished opcodes go through
        _absorb_trivial_equal_blocks (the module-level function at the
        END of this file -- differ_python.py carries its own copy):
        it merges INSERT+EQUAL(trivial)+DELETE into one REPLACE and
        absorbs trivial EQUAL blocks stranded inside large replace
        regions. The native engines DO emit both patterns -- measured
        on the plugin's _dev/__tests corpus: GNU-diffutils Myers 1x
        Step-1 + 91x Step-2, JGit Histogram 1+101, JGit Myers 3+62,
        i.e. MORE fragmentation than the pure-Python engines -- which
        is why the pass is offered here too (it is what makes the
        native and Python algorithms agree on hunk structure and keeps
        positional pairing from matching a line with the WRONG line of
        the other file). It is a beautify OPTION, default OFF: with it
        off, the raw engine output is rendered exactly as the engine
        produced it (GNU diffutils / WinMerge faithful). The option
        lives in the same 'differ2.algorithm.beautify.*' group as
        align_by_similarity (or the legacy align_by_similarity2),
        but the two are independent layers:
        the beautify flags re-pair lines INSIDE one replace block
        (rendering), the absorb pass changes WHICH lines belong to
        which hunk (structure).

        WHERE it runs (exactly one place per flow, so no phase ever
        sees different opcodes than the phases around it):
        - two-phase background flow (withdetail on): inside
          collect_char_pairs, right after its line split, BEFORE the
          pair walk -- the collect pass and the paint replay MUST
          walk identical opcodes (the char-ops pop order depends on
          it), and the absorbed list is mutated in place so the
          callback and the paint pass receive it.
        - synchronous engine run and background withdetail-off
          delivery: in the fresh-split branch below, right after the
          line split.
        - the cached-lines replay branch skips it (collect already
          absorbed those opcodes).
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
        # compare:align_by_similarity).

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

        # The absorb pass runs in exactly ONE place per flow (see the
        # NOTE in the docstring above): collect_char_pairs absorbed
        # the two-phase background flow's opcodes before recording the
        # char pairs; the fresh-split branch below absorbs the two
        # remaining flows (synchronous engine run, and the background
        # engine's withdetail-off delivery). Nothing runs here.

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
            # replay pass: cached by collect_char_pairs() -- those
            # opcodes were ABSORBED by the collect pass already (the
            # collect/replay invariant, when the option is on), so no
            # absorb pass runs here.
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

            # Opcode beautify pass (see the docstring NOTE): this
            # branch covers BOTH fresh-engine flows -- the synchronous
            # engine run above (opcodes computed inside this generator)
            # AND the background engine's withdetail-off delivery
            # (opcodes precomputed, no collect pass ran, raw texts
            # passed). The line lists the pass reads were just split;
            # on a 1M-line compare the pass costs ~0.5s -- two cheap
            # tuple sweeps, no engine call.
            if self.absorb_trivial_equal_blocks:
                # Umbrella section + nested per-step marks, same as the
                # collect pass (see collect_char_pairs): this branch
                # runs on the main thread too.
                Profiler.start('absorb_trivial_equal_blocks')
                opcodes = _absorb_trivial_equal_blocks(
                    a_lines, opcodes,
                    Profiler.mark if Profiler.enabled else None)
                Profiler.stop('absorb_trivial_equal_blocks')

        # Event production for REPLACE blocks is instrumented per chunk
        # ('compare:positional_pairs' / 'compare:align_by_similarity' open
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
            print('Differ 2: compare took {:.1f}ms '
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
        # The beautify plans the collect pass cached for THIS walk are
        # consumed by now (or the walk is being abandoned -- the
        # Command callbacks call drop_cached_state()): release them so
        # the Differ holds no alignment state between compares.
        self._align_plans = None
        self._align_plans_pos = 0

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
        ('compare:align_by_similarity' for the beautify path,
        'compare:positional_pairs' for the positional path — one row per
        producer so the report shows which mode a block used) opens
        after a list starts and closes before the list is yielded —
        never open across a yield (see compare()'s NOTE). Event lists
        are bounded by _REPLACE_CHUNK pairs, so huge blocks (two
        entirely different 1M-line files come as ONE replace opcode)
        do not materialize their whole event stream at once.

        Three rendering modes, selected by the two flags of the SINGLE
        align_by_similarity dropdown (Method 1 'fast' / Method 2
        'slow' / 'off' -- see __init__; exactly one bit is on by
        construction, and when both are somehow set on a directly
        constructed Differ, align_by_similarity2 wins in the branch
        below):

        align_by_similarity = True (fast 'beautified' alignment)
            Unequal line counts go to the JOINED-BLOCK MAPPER
            (align_joined.py): ONE native engine call maps the block's
            two sides against each other (EQUAL ranges = the pairing,
            DELETE/INSERT = gaps, REPLACE residuals = recurse with
            progressively looser keys: raw -> stripped -> prefix), the
            ORIGINAL recursive search only for small residuals
            (da*db <= align_joined.SMALL_PRODUCT). VS Code-like;
            re-arranges the engine's output. The plan is computed in
            the COLLECT pass and cached -- the REPLAY pass pops it and
            never searches again (see _take_align_plan).

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
            O(N*M) per block and runs in BOTH the collect and replay
            passes (no plan cache; only the collect pass's _char_diff
            RECORDS survive it).

            Fast path (da == db): positional pairing (same as the
            algo-faithful mode).

            Slow path (da != db): _find_best_pairs_events — see its
            docstring.

        both flags False (algo-faithful, default)
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

        # ---- da != db: the beautify modes diverge here ----
        if (self.align_by_similarity or self.align_by_similarity2) \
                and da != db:
            if self.align_by_similarity2:
                # SLOW legacy beautify (Method 2 'slow' of the single
                # align_by_similarity dropdown): the
                # original pure-Python recursive search, restored
                # verbatim. anchor + prefix/suffix scoring + threshold
                # and staggering. Produced into ONE list (the recursive
                # scorer makes chunking invasive). Char diffs inside
                # are perf_counter-timed and booked as batched marks
                # (legacy synchronous mode only). The producing
                # section is suppressed during the COLLECT pass: the
                # collect pass's whole cost lands in the caller's
                # 'compare:collect_pairs' section, so these rows keep
                # reporting ONLY the paint-pass walk. No plan cache:
                # the search runs identically in both passes (only the
                # collect pass's _char_diff RECORDS are consumed).
                _collecting = self._char_pairs_pending is not None
                if not _collecting:
                    Profiler.start('compare:align_by_similarity')
                evs = []
                self._find_best_pairs_events(evs, a, alo, ahi, b, blo, bhi)
                if not _collecting:
                    Profiler.stop('compare:align_by_similarity')
                yield evs
                return

            # FAST beautify (Method 1 'fast' of the dropdown):
            # joined-block mapping
            # (align_joined.py): the engine maps the block's sides,
            # Python only translates. The plan is computed ONCE
            # (COLLECT pass) and cached -- the REPLAY pass pops it and
            # never searches again (see _take_align_plan). Emission is
            # chunked by _REPLACE_CHUNK plan ops so a huge block's
            # event list stays bounded (the old recursive scorer had
            # to build ONE unbounded list per block). Producing
            # sections are suppressed during the COLLECT pass: the
            # collect pass's whole cost lands in the caller's
            # 'compare:collect_pairs' section (the per-call engine
            # marks still run in every mode -- see
            # align_joined.AlignEngine.call).
            _collecting = self._char_pairs_pending is not None
            if not _collecting:
                Profiler.start('compare:align_by_similarity')
            plan = self._take_align_plan(a, alo, ahi, b, blo, bhi,
                                         _collecting)
            if not _collecting:
                Profiler.stop('compare:align_by_similarity')
            n = len(plan)
            chunk = self._REPLACE_CHUNK
            k = 0
            while k < n:
                k2 = k + chunk
                if k2 > n:
                    k2 = n
                if not _collecting:
                    Profiler.start('compare:align_by_similarity')
                evs = []
                self._emit_align_plan(evs, plan[k:k2], a, b)
                if not _collecting:
                    Profiler.stop('compare:align_by_similarity')
                yield evs
                k = k2
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
        """(LEGACY beautify mode -- Method 2 'slow' of the single
        align_by_similarity dropdown) The OLD slow
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
        'Beautify passes' block prints them under the pass). The
        SEARCH marks run in every mode (the search itself runs in
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

    # Plan-op budget of the joined-block aligner's cache: total plan ops
    # cached for one compare's replay pass (see collect_char_pairs).
    # 65536 ops is a few hundred KB of tuples -- enough for thousands of
    # normal blocks; a pathological compare whose blocks blow past it
    # simply re-computes the uncached plans in the replay pass (one
    # extra engine call per block, no correctness impact).
    _PLAN_CACHE_BUDGET = 65536

    def _take_align_plan(self, a, alo, ahi, b, blo, bhi, collecting):
        """Compute -- or, on the REPLAY pass, POP -- the alignment plan
        of one unequal REPLACE block (see align_joined.align_block_plan
        for what a plan is).

        COLLECT pass ('collecting' True): compute the plan and APPEND it
        to the cache (in walk order) while the budget lasts; the replay
        pass then pops it instead of searching again -- the whole
        joined-block cascade runs ONCE per compare.

        REPLAY / LEGACY pass: pop the next cached plan when its block
        bounds line up with this block (they must -- both passes walk
        the same opcodes on the same line lists, the very invariant the
        char-ops pops rely on); on a mismatch (a determinism break that
        should never happen) drop the remaining cache and compute fresh
        from here on, so a desync can never paint wrong pairings.
        """
        if collecting:
            plan = self._compute_align_plan(a, alo, ahi, b, blo, bhi)
            cache = self._align_plans
            if cache is not None and self._align_plan_budget > 0:
                cache.append((alo, ahi, blo, bhi, plan))
                self._align_plan_budget -= len(plan)
            return plan
        cache = self._align_plans
        if cache is not None and self._align_plans_pos < len(cache):
            entry = cache[self._align_plans_pos]
            if (entry[0] == alo and entry[1] == ahi
                    and entry[2] == blo and entry[3] == bhi):
                self._align_plans_pos += 1
                return entry[4]
            # Desync (should never happen): drop the remaining cache and
            # fall through to fresh computation.
            self._align_plans = None
        return self._compute_align_plan(a, alo, ahi, b, blo, bhi)

    def _compute_align_plan(self, a, alo, ahi, b, blo, bhi):
        """Run the joined-block aligner on one block: builds a fresh
        AlignEngine over this Differ's engine bridges (the algo and
        ignore flags of the CURRENT compare) and returns the plan. The
        engine object is per-call by design -- the config can change
        between compares, and two bound-method refs cost nothing next
        to the first diff_proc call."""
        eng = AlignEngine(self._joined_sub_diff_raw,
                          self._joined_sub_diff_keys)
        return align_block_plan(a, alo, ahi, b, blo, bhi, eng)

    def _joined_sub_diff_raw(self, lines_a, lines_b):
        """AlignEngine depth-0 bridge: diff_proc(DIF_TEXTS) on the two
        RAW keepends line lists of a block.

        The lines carry their own terminators, so ''.join reconstructs
        the sub-text BYTE-FOR-BYTE (the exact inverse of the engine's
        line split -- no separator is inserted, none is needed) and the
        engine's opcodes index the given lines 1:1. Runs with the same
        algo + DIFF_IGN_* flags as the top-level compare, so the
        sub-block's notion of 'equal' matches the user's configuration
        (a whitespace-only difference anchors under
        DIFF_IGN_WHITESPACE, etc.).

        Returns the opcode list, or None when the native engine is not
        available (defensive: this Differ is only used with the engine
        present, but a test instantiation without it must not crash --
        the aligner falls back to its bounded positional tail).
        """
        if not _HAS_NATIVE_DIFF:
            return None
        result = _ct.diff_proc(
            _ct.DIF_TEXTS,
            ''.join(lines_a),
            ''.join(lines_b),
            algo_id(self.diff_algorithm),
            self.ignore_flags,
        )
        if not result:
            # Engine error / empty result: one whole-block REPLACE so
            # the aligner's next cascade level (or its positional tail)
            # takes over.
            return [('replace', 0, len(lines_a), 0, len(lines_b))]
        return result

    def _joined_sub_diff_keys(self, keys_a, keys_b):
        """AlignEngine depth-1+ bridge: diff_proc(DIF_TEXTS) on
        TERMINATOR-FREE key lists (stripped / prefix keys -- see
        align_joined._depth_keys).

        Keys carry no CR/LF, so they are joined with a separator plus a
        TRAILING one: without the trailing newline an empty FINAL key
        would be swallowed by the engine's split ('a\\n' + '' joins to
        'a\\n', which reads as ONE line) and every opcode index after it
        would shift by one -- with it, k keys always re-split into
        exactly k lines (k-1 separators + the trailing one).
        """
        if not _HAS_NATIVE_DIFF:
            return None
        result = _ct.diff_proc(
            _ct.DIF_TEXTS,
            '\n'.join(keys_a) + '\n',
            '\n'.join(keys_b) + '\n',
            algo_id(self.diff_algorithm),
            self.ignore_flags,
        )
        if not result:
            return [('replace', 0, len(keys_a), 0, len(keys_b))]
        return result

    def _emit_align_plan(self, out, plan, a, b):
        """Translate alignment plan ops (see align_joined) into paint
        events appended to `out` -- THREE modes selected by the
        two-phase state fields, an exact mirror of
        _positional_pairs_events' mode handling:

        COLLECT pass: record each CHANGED pair into the pending list
        (equal pairs and D/I/Q ops record nothing -- no char diff will
        ever ask for them); the record order equals the replay pass's
        request order, which is the whole point of the collect pass.

        REPLAY pass: pop each changed pair's precomputed ops (inlined:
        the long-line guard REPLACES the pop, a None element or a
        position overrun degrades to a full-line REPLACE -- an exact
        copy of _char_diff's replay branches) and emit the symbol
        events + the composite PAIR_CHANGED.

        LEGACY synchronous mode: one engine call per changed pair,
        perf_counter-timed and booked as ONE batched mark per plan
        chunk (the same pattern as _positional_pairs_events).

        'P' pairs decide EQUAL vs CHANGED by comparing the RAW lines
        HERE (never in the plan): an engine EQUAL under ignore flags
        may pair raw-different lines, and those must run the char diff
        (the char diff applies the same flags, so an all-ignored
        difference simply paints a pair with deca == decb == 0).
        """
        collecting = self._char_pairs_pending is not None
        replay_ops = self._char_ops if not collecting else None
        if replay_ops is not None:
            guard = self._CHAR_GUARD_LEN
            ops_len = len(replay_ops)
            emit_align = not self._skip_align
            append = out.append
            # Pop position kept in a LOCAL for the whole loop (one
            # attribute write-back at the end -- same rationale as
            # _positional_pairs_events' replay loop).
            pos = self._char_ops_pos
            for op in plan:
                kind = op[0]
                if kind == 'P':
                    ai, bj = op[1], op[2]
                    la = a[ai]
                    lb = b[bj]
                    if la == lb:
                        if emit_align:
                            append((ALIGN, ai, bj))
                        continue
                    if len(la) > guard or len(lb) > guard:
                        pair_ops = None
                    elif pos >= ops_len:
                        pair_ops = None
                    else:
                        pair_ops = replay_ops[pos]
                        pos += 1
                    if pair_ops is None:
                        # full-line REPLACE (guard / engine-failed pair /
                        # overrun) -- the same ops list the fallback in
                        # _positional_pairs_events builds.
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
                elif kind == 'D':
                    append((B_GAP, op[3], op[1], op[2]))
                    for i in range(op[1], op[2]):
                        append((A_LINE_DEL, i))
                elif kind == 'I':
                    append((A_GAP, op[3], op[1], op[2]))
                    for j in range(op[1], op[2]):
                        append((B_LINE_ADD, j))
                else:
                    # 'Q': suppressed all-blank hunk -- exactly the
                    # events compare_lists' top-level 'ignore' branch
                    # emits (ignored gap compensating the longer side,
                    # ALIGN pairs for the common count, ignored lines;
                    # no char pairs, no diffmap).
                    i1, i2, j1, j2 = op[1], op[2], op[3], op[4]
                    dia = i2 - i1
                    djb = j2 - j1
                    if dia > djb:
                        append((B_GAP_IGN, j2, i1 + djb, i2))
                    elif djb > dia:
                        append((A_GAP_IGN, i2, j1 + dia, j2))
                    if emit_align:
                        for k in range(dia if dia < djb else djb):
                            append((ALIGN, i1 + k, j1 + k))
                    for i in range(i1, i2):
                        append((A_LINE_IGN, i))
                    for j in range(j1, j2):
                        append((B_LINE_IGN, j))
            self._char_ops_pos = pos
            return

        collect = self._char_pairs_pending
        if collect is not None:
            # COLLECT: only the CHANGED pairs' record order survives
            # this pass (every event append would be drained and
            # discarded by collect_char_pairs); the guard/negation
            # mirrors _char_diff's collect branch exactly, so the
            # recorded pairs -- and their order -- match what the
            # replay pass will pop.
            guard = self._CHAR_GUARD_LEN
            collect_append = collect.append
            for op in plan:
                if op[0] == 'P':
                    la = a[op[1]]
                    lb = b[op[2]]
                    if (la != lb and len(la) <= guard
                            and len(lb) <= guard):
                        collect_append((la, lb))
            return

        # LEGACY synchronous mode (no collect ran, no replay data).
        prof_on = Profiler.enabled
        char_diff_call = self._char_diff
        emit_align = not self._skip_align
        append = out.append
        if prof_on:
            eng_dt = 0.0
            eng_n = 0
            eng_max = 0.0
        for op in plan:
            kind = op[0]
            if kind == 'P':
                ai, bj = op[1], op[2]
                la = a[ai]
                lb = b[bj]
                if la == lb:
                    if emit_align:
                        append((ALIGN, ai, bj))
                    continue
                if prof_on:
                    t0 = time.perf_counter()
                    ops = char_diff_call(la, lb)
                    dt = time.perf_counter() - t0
                    eng_dt += dt
                    eng_n += 1
                    if dt > eng_max:
                        eng_max = dt
                else:
                    ops = char_diff_call(la, lb)
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
                # 'Q': ignored hunk (see the replay branch above).
                i1, i2, j1, j2 = op[1], op[2], op[3], op[4]
                dia = i2 - i1
                djb = j2 - j1
                if dia > djb:
                    append((B_GAP_IGN, j2, i1 + djb, i2))
                elif djb > dia:
                    append((A_GAP_IGN, i2, j1 + dia, j2))
                if emit_align:
                    for k in range(dia if dia < djb else djb):
                        append((ALIGN, i1 + k, j1 + k))
                for i in range(i1, i2):
                    append((A_LINE_IGN, i))
                for j in range(j1, j2):
                    append((B_LINE_IGN, j))
        if prof_on and eng_n:
            Profiler.mark(self._CHAR_ROW, eng_dt, eng_n, eng_max)
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


# =========================================================================
# Opcode beautify pass -- absorb trivial EQUAL blocks.
#
# Private module-level helper (differ_python.py carries its OWN copy of
# this code -- the duplication is deliberate, so the native and Python
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

    Called from collect_char_pairs() (the two-phase background flow,
    in place, BEFORE the char-pair walk) and from compare_lists()
    (fresh-split branch: synchronous engine run / withdetail-off
    delivery) -- ONLY when the option
    'differ2.algorithm.beautify.absorb_trivial_equal_blocks' is on.

    STEP 1 -- merge INSERT + EQUAL(trivial) + DELETE (or the mirrored
    DELETE + EQUAL(trivial) + INSERT) into one REPLACE.

        Example (the native engines emit this pattern too -- measured
        1x per engine on the _dev/__tests corpus; the same input also
        makes every pure-Python engine emit it. See
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

        Example (this is the pattern the native engines emit MOST:
        GNU-diffutils Myers 91x, JGit Histogram 101x, JGit Myers 62x
        on the corpus -- more than any pure-Python engine):

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
            read as two unrelated changes (and that make positional
            pairing match a line with the WRONG line of the other
            file).

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
      * 'ignore' hunks (this engine's suppressed all-blank differences
        under DIFF_IGN_BLANK_LINES) are BARRIERS for STEP 2: merging
        across one would resurrect the suppressed lines into a shown
        REPLACE, undoing the suppression.

    Args:
        a: the left file's line list (keepends), used ONLY to read the
            EQUAL blocks' text for the trivial-content check.
        opcodes: difflib-style (tag, i1, i2, j1, j2) tuples from any
            engine. Tags: 'equal' / 'delete' / 'insert' / 'replace' /
            'ignore' (the native engine's suppressed all-blank hunks).
        _book: optional profiler booking callable (row_name, dt) ->
            None, used only by the plugin's compare paths when
            profiling is on: this module's collect_char_pairs /
            compare_lists pass Profiler.mark under their
            'absorb_trivial_equal_blocks' umbrella section (both run
            on the main thread, so the step marks nest and the
            umbrella's SELF is the pass minus its steps);
            differ_python.engine_opcodes passes the thread-safe
            Profiler.mark_standalone instead (it runs on the
            background Python-engine thread). Each step books its
            OWN row ('absorb_trivial_equal_blocks:step1_merge_ins_eq_del'
            / ':step2_absorb_short_equal' -- the report's 'Beautify
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
