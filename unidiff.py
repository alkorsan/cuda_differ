"""Unified-diff pipeline for the "Diff current document with file... /
tab..." commands (Command.diff_with / diff_with_tab -> create_diff).

Everything those two commands run lives in this module; __init__.py
keeps only the UI glue -- the completion callback that opens the
read-only "Diff N" tab and prints the status line, plus the console-log
sink the runner receives at construction time:

  * RENDERING -- grouped_opcodes() + unified_diff_opcodes(): an
    opcode-driven difflib.unified_diff work-alike. difflib.unified_diff
    always runs its own SequenceMatcher and offers no way to feed it
    opcodes computed by another engine, but every engine this plugin
    can run -- the native cudatext.diff_proc(DIF_TEXTS) call and all
    the pure-Python matchers behind differ_python.Differ.engine_
    opcodes() -- returns the SAME difflib-compatible opcode format
    (list of (tag, i1, i2, j1, j2) tuples, tag in 'equal'/'delete'/
    'insert'/'replace', plus the CudaText 'ignore' extension diff_proc
    emits for hunks DIFF_IGN_BLANK_LINES suppressed), so only the
    rendering had to be made engine-independent. Given identical
    opcodes the output is byte-identical to difflib.unified_diff
    (verified against the stdlib on randomized texts).

  * ENGINE COMPUTE -- which engine produces the opcodes (or the whole
    diff text, for the 'difflib' choice) follows the CONFIGURED
    algorithm (differ.algorithm.diff_algorithm), resolved by
    Command._resolve_algorithm before start():
      - 'difflib' -> stdlib difflib.unified_diff DIRECTLY, keeping its
        classic behavior (autojunk=True included);
      - native algorithms -> the callback (asynchronous) form of
        diff_proc(DIF_TEXTS), ALWAYS, whatever the file sizes;
      - every other algorithm -> differ_python.Differ.engine_opcodes,
        whose opcodes drive unified_diff_opcodes.
    The engine always runs with flags=DIFF_IGN_NONE: under the ignore
    options an 'equal' opcode can cover lines that differ byte-wise
    (e.g. 'abc\r\n' vs 'abc\n' under DIFF_IGN_EOL), which the renderer
    would emit as context lines -- producing a patch patch / git apply
    rejects. The ignore options keep their meaning in the side-by-side
    compare view only. (The 'ignore' opcode is still handled
    defensively inside unified_diff_opcodes -- rendered like
    'replace' -- so even a future engine run WITH flags could never
    yield an invalid patch from this path.)

  * BACKGROUND EXECUTION -- every algorithm runs in the background,
    whatever the file sizes, so no diff can ever freeze the UI:
      - the native algorithms compute on the engine's own background
        OS thread (the async diff_proc form; the command returns at
        once and the completion callback -- marshalled to the main
        thread, carrying its job context through a functools.partial
        -- finishes the command there);
      - the pure-Python engines and the 'difflib' choice compute on a
        background daemon thread whose completion is picked up by a
        poll timer on the main thread -- the same two-phase shape the
        side-by-side compare's Python-engine background mode uses.
        The line splitting (utils.split_lines_safe) runs on the worker
        too, so even that never blocks the UI; on the native path the
        split runs AFTER the engine job is started, overlapping the
        engine's own background run instead of delaying it.
    Both forms marshal the result back to ONE main-thread completion
    (UnidiffRunner -> the on_finish callback -> Command._finish_unidiff),
    which renders, opens the tab and prints the ONE-line status report
    (report_status: total wall time, difference count, algorithm).

  * CANCELLATION -- UnidiffRunner.cancel(job) / cancel_all() stop
    in-flight jobs: diff_proc(DIF_CANCEL) tells the native engine to
    stop cooperatively (its diff loops unwind within a couple of
    seconds; the completion callback of a cancelled job is never
    invoked), while a Python worker's poll timer is stopped and its
    result dropped -- a Python thread cannot be interrupted
    mid-computation safely, the same policy the side-by-side compare
    applies to its own Python-engine jobs. The plugin's "Cancel
    compare" / "Cancel all compares" commands call cancel_all()
    (unified-diff jobs are not bound to any tab -- their result tab
    does not exist until the diff finishes), and on_exit_pre cancels
    everything through the same door.
"""

import difflib
import functools
import threading
import time

import cudatext as ct

from . import differ_native as dfn
from . import differ_python as dfp
from .utils import split_lines_safe
from cudax_lib import get_translation
_ = get_translation(__file__)  # I18N

# Poll period of the completion timer for the Python-engine background
# form -- the twin of __init__.py's PY_ENGINE_POLL_MS (kept separate so
# this module never imports the plugin's main module). The main thread
# is idle while the worker computes, so this only paces how quickly the
# finished result is picked up.
_POLL_MS = 50

# In-flight unified-diff background jobs (native engine jobs and
# Python-engine worker threads). The cancel commands and app-exit walk
# this registry: the result tab cannot be opened anymore, so the engine
# threads are told to stop / their results dropped (see
# UnidiffRunner.cancel).
JOBS = []


# ----------------------------------------------------------------------
# Opcode-driven unified diff -- a difflib.unified_diff work-alike.
#
# The three functions below port difflib's hunk-building pipeline to
# operate on a precomputed opcode list instead of a matcher instance:
#
#   _format_range_unified()  == difflib._format_range_unified
#   grouped_opcodes()        == SequenceMatcher.get_grouped_opcodes
#   unified_diff_opcodes()   == difflib.unified_diff
#
# Given identical opcodes, unified_diff_opcodes() yields byte-identical
# output to difflib.unified_diff() (verified against the stdlib on
# randomized texts); the only extension is the 'ignore' tag, rendered
# defensively like 'replace' (see unified_diff_opcodes).
# ----------------------------------------------------------------------

def _check_types(a, b, *args):
    """Verbatim port of difflib._check_types: refuse mixed bytes/str
    early -- str.format() silently renders bytes as b'...' reprs, which
    would garble the ---/+++/@@ headers instead of failing loudly."""
    # Checking types is weird, but the alternative is garbled output when
    # someone passes mixed bytes and str to {unified,context}_diff(). E.g.
    # without this check, passing filenames as bytes results in output like
    #   --- b'oldfile.txt'
    #   +++ b'newfile.txt'
    # because of how str.format() incorporates bytes objects.
    if a and not isinstance(a[0], str):
        raise TypeError('lines to compare must be str, not %s (%r)' %
                        (type(a[0]).__name__, a[0]))
    if b and not isinstance(b[0], str):
        raise TypeError('lines to compare must be str, not %s (%r)' %
                        (type(b[0]).__name__, b[0]))
    for arg in args:
        if not isinstance(arg, str):
            raise TypeError('all arguments must be str, not: %r' % (arg,))


def _format_range_unified(start, stop):
    """Convert a half-open [start, stop) line range to the unified-diff
    "ed" format. Verbatim port of difflib._format_range_unified: lines
    are numbered from one, the length prints comma-separated, a length
    of exactly 1 prints the bare start, and an empty range prints the
    line number just BEFORE the range."""
    # Per the diff spec at http://www.unix.org/single_unix_specification/
    beginning = start + 1     # lines start numbering with one
    length = stop - start
    if length == 1:
        return '{}'.format(beginning)
    if not length:
        beginning -= 1        # empty ranges begin at line just before the range
    return '{},{}'.format(beginning, length)


def grouped_opcodes(opcodes, n=3):
    """Isolate change clusters by eliminating ranges with no changes.

    Line-for-line port of difflib.SequenceMatcher.get_grouped_opcodes()
    (Python 3.12 stdlib) operating on a PLAIN opcode list -- the format
    every engine this plugin runs returns (native diff_proc, all the
    differ_python matchers) -- instead of a matcher instance. Yields
    lists of opcodes; each yielded list is one hunk: a run of changes
    with up to n lines of context on each side, split wherever an equal
    run longer than 2*n separates two changes.

    The input list is COPIED first: difflib's own implementation
    mutates the first/last opcode in place (the leading/trailing
    context fixup below), and an engine's returned list must stay
    untouched -- other consumers may walk it afterwards.

    'ignore' opcodes (the CudaText extension) count as CHANGES here,
    like any non-equal opcode: they join the surrounding hunk instead
    of splitting it.
    """
    codes = list(opcodes)
    if not codes:
        codes = [('equal', 0, 1, 0, 1)]
    # Fixup leading and trailing groups if they show no changes.
    if codes[0][0] == 'equal':
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = tag, max(i1, i2-n), i2, max(j1, j2-n), j2
    if codes[-1][0] == 'equal':
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = tag, i1, min(i2, i1+n), j1, min(j2, j1+n)

    nn = n + n
    group = []
    for tag, i1, i2, j1, j2 in codes:
        # End the current group and start a new one whenever
        # there is a large range with no changes.
        if tag == 'equal' and i2-i1 > nn:
            group.append((tag, i1, min(i2, i1+n), j1, min(j2, j1+n)))
            yield group
            group = []
            i1, j1 = max(i1, i2-n), max(j1, j2-n)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == 'equal'):
        yield group


def unified_diff_opcodes(a, b, opcodes, fromfile='', tofile='',
                         fromfiledate='', tofiledate='', n=3,
                         lineterm='\n'):
    """difflib.unified_diff work-alike driven by PRECOMPUTED opcodes.

    Same parameters and the same yielded lines as
    difflib.unified_diff(a, b, fromfile, tofile, fromfiledate,
    tofiledate, n, lineterm) -- the '--- / +++' file headers once, then
    one '@@ -x,y +u,v @@' hunk header per change cluster with up to n
    context lines around it -- except that NO diff engine runs here:
    'opcodes' is the (tag, i1, i2, j1, j2) list computed by whichever
    engine the CALLER chose (native diff_proc DIF_TEXTS, any of the
    differ_python matchers). 'a' / 'b' are the two line lists those
    opcodes index into (keepends lines, split on CR/LF/CRLF only -- see
    split_lines_safe), so the yielded body lines keep their original
    terminators exactly the way difflib renders them; a line missing
    its trailing newline stays without one (difflib emits no
    backslash-newline marker either).

    This is what lets the "Diff current document with..." commands
    honor differ.algorithm.diff_algorithm: all the plugin's engines
    agree on the opcode format, so only the RENDERING had to be
    engine-independent.

    The single behavioral extension over difflib: an 'ignore' opcode
    (the CudaText extension diff_proc emits for blank-line hunks
    suppressed by DIFF_IGN_BLANK_LINES) is rendered like 'replace' --
    its A lines as '-' and its B lines as '+'. Those lines ARE
    physically different (the ignore options only said "don't show
    them as differences"), so emitting them as changes is the only
    rendering that keeps the output a valid patch; emitting them as
    context would claim A == B where they differ byte-wise, and
    patch / git apply would reject the result.
    """
    _check_types(a, b, fromfile, tofile, fromfiledate, tofiledate,
                 lineterm)
    started = False
    for group in grouped_opcodes(opcodes, n):
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
            if tag in ('replace', 'delete', 'ignore'):
                for line in a[i1:i2]:
                    yield '-' + line
            if tag in ('replace', 'insert', 'ignore'):
                for line in b[j1:j2]:
                    yield '+' + line


# ----------------------------------------------------------------------
# Engine compute -- runs on the background worker thread (pure Python,
# no CudaText API, no shared state; the GIL interleaves it with the
# main thread's message loop).
# ----------------------------------------------------------------------

def _difflib_direct(a, b, fn0, fn1, n):
    """The 'difflib' algorithm choice: stdlib difflib.unified_diff
    DIRECTLY -- the classic pre-algorithm-wiring behavior, including
    its internal SequenceMatcher(None, a, b) with autojunk=True (the
    autojunk heuristic only triggers on files with >200 occurrences
    of a single line at >1% of the file, and even then the produced
    patch stays valid for patch / git apply).

    Returns (diff_text, n_diffs). The difference count comes from a
    SequenceMatcher configured EXACTLY like unified_diff's internal
    one (autojunk=True default), so the number always matches what
    unified_diff itself rendered: non-'equal' opcodes are the
    difference REGIONS -- the same 'total differences' notion the
    compare view's toolbar reports, and the same count every other
    algorithm's path prints. This does run the matcher twice (once
    here, once inside unified_diff) -- the price of 'unified_diff
    directly' plus an exact count; the whole call already sits on the
    background worker thread (see _start_py_thread), so only CPU is
    doubled, never the UI wait.
    """
    sm = difflib.SequenceMatcher(None, a, b)
    n_diffs = 0
    for tag, _i1, _i2, _j1, _j2 in sm.get_opcodes():
        if tag != 'equal':
            n_diffs += 1
    text = ''.join(difflib.unified_diff(a, b, fn0, fn1, n=n))
    return text, n_diffs


def _py_opcodes(algo, a, b):
    """Run one pure-Python engine ('hybrid', 'myers', 'vscode',
    'patience' -- NOT 'difflib': start routes that choice through
    stdlib difflib.unified_diff directly) on the pre-split keepends
    line lists and return its opcodes -- RAW.

    The unified-diff commands deliberately use the RAW algorithms:
    the beautify passes of the side-by-side view (the
    'differ.algorithm.beautify.*' options) are NOT applied here, so
    what you get is the engine's own hunk structure, exactly what GNU
    diff / WinMerge would show for that algorithm. (The compare view
    can therefore paint a slightly different hunk structure than the
    unified diff when a beautify option is on -- by design.)

    Why no flags are set on the fresh Differ:
      * absorb_trivial_equal_blocks -- a fresh Differ already defaults
        it to False and this path reads no config, so engine_opcodes
        returns the raw engine opcodes by construction;
      * align_by_similarity -- engine_opcodes never consults it. It
        is a paint-walk flag (how lines inside one REPLACE block are
        paired when the side-by-side view renders it), and this path
        never runs the paint walk.
    """
    diff = dfp.Differ()
    diff.diff_algorithm = algo
    return diff.engine_opcodes(a, b)


# ----------------------------------------------------------------------
# Job state + registry + the status report.
# ----------------------------------------------------------------------

class UnidiffJob:
    """Context of one in-flight (or just-finished) unified-diff command
    run -- the light-weight sibling of the compare view's _CompareJob:
    no editors to lock, no overview, no paint events; just the inputs,
    the engine mode, and where the result lands.

    Which fields carry the result depends on the engine path:
      * native / pure-Python engines -> 'opcodes' (rendered later by
        the completion callback through unified_diff_opcodes);
      * the 'difflib' choice -> 'uni_text' + 'n_diffs' straight from
        stdlib difflib.unified_diff (computed fully inside the engine
        phase, so the difflib-direct behavior is identical to an
        opcode-path run).
    'a' / 'b' (the split line lists the opcodes index into) are filled
    by whichever path needs them and BEFORE its result lands: the
    Python worker splits first thing, the native path splits right
    after the engine job is started (overlapping the engine's run).

    Slots keep it allocation-cheap; the JOBS registry holds a reference
    for the whole background run so the cancel commands / app exit can
    stop it. 'start' is the KICK-OFF time (Command.create_diff entry),
    so the status report covers the whole command wall time, background
    engine phase included -- the same accounting the compare view's
    epilogue uses.
    """

    __slots__ = ('txt0', 'txt1', 'a', 'b', 'fn0', 'fn1', 'n_ctx',
                 'algo', 'start', 'job_handle', 'py_poll_cb',
                 'py_engine_thread', 'done', 'opcodes', 'uni_text',
                 'n_diffs', 'error', 'cancelled')

    def __init__(self, txt0, txt1, fn0, fn1, n_ctx, algo, start):
        self.txt0 = txt0            # raw text A (native engine input)
        self.txt1 = txt1            # raw text B (native engine input)
        self.a = None               # line list A (filled by the engine path)
        self.b = None               # line list B (filled by the engine path)
        self.fn0 = fn0              # '---' header name
        self.fn1 = fn1              # '+++' header name
        self.n_ctx = n_ctx          # context lines (diff_context option)
        self.algo = algo            # EFFECTIVE algorithm name (what runs)
        self.start = start          # kick-off time.perf_counter()
        self.job_handle = 0         # native engine job handle (0 = none)
        self.py_poll_cb = None      # poll-timer callback (Python bg mode)
        self.py_engine_thread = None
        self.done = False           # Python worker thread finished
        self.opcodes = None         # engine result (opcode paths)
        self.uni_text = None        # rendered diff text (difflib path /
                                    # set by the completion after render)
        self.n_diffs = None         # difference-region count
        self.error = None           # engine exception / failure marker
        self.cancelled = False      # dropped (cancel command / exit /
                                    # fallback race)


def report_status(elapsed, n_diffs, algo):
    """ONE status-bar line for a finished unified-diff command: the
    total wall time (kick-off -> diff tab opened / report printed), the
    number of difference regions, and the algorithm that actually ran
    -- the unified-diff twin of the compare view's 'compared in ...'
    epilogue, with the count and the algorithm riding the SAME line
    (one line only). Adaptive time units: ms below 1s, tenths of a
    second below a minute, minutes + seconds above."""
    if elapsed < 1.0:
        t_str = _('{:.0f}ms').format(elapsed * 1000.0)
    elif elapsed < 60.0:
        t_str = _('{:.1f}s').format(elapsed)
    else:
        _mins = int(elapsed // 60)
        t_str = _('{}m {:.0f}s').format(_mins, elapsed - _mins * 60)
    if n_diffs == 1:
        d_str = _('1 difference')
    else:
        d_str = _('{} differences').format(n_diffs)
    ct.msg_status(_('Differ: diffed in {}, {}, algo {}').format(
        t_str, d_str, algo))


# ----------------------------------------------------------------------
# The background runner.
# ----------------------------------------------------------------------

class UnidiffRunner:
    """Owns the whole background execution of the unified-diff
    commands. One instance lives on the Command ('self.unidiff'),
    constructed with the two pieces of UI glue this module must not
    know: 'on_finish' -- the main-thread completion callback
    (Command._finish_unidiff: render, open the 'Diff N' tab, print the
    report) -- and 'log' -- the plugin's console-log sink for the rare
    fallback notices.

    start() dispatches by the resolved engine: native algorithms ->
    _start_native (the async diff_proc form, ALWAYS); everything else
    ('difflib' included) -> _start_py_thread (a daemon worker thread +
    a completion poll timer, whatever the file sizes). Both paths end
    in the SAME on_finish callback on the main thread.

    cancel(job) / cancel_all() stop in-flight jobs (see the module
    docstring's CANCELLATION block); notify_app_exiting() is called
    once from on_exit_pre before the final cancel_all() so any
    straggling completion arriving during the exit sequence is dropped
    even if its per-job race guard misses.
    """

    def __init__(self, on_finish, log):
        self._on_finish = on_finish
        self._log = log
        self._exiting = False

    # -- public API (Command-facing) ----------------------------------

    def start(self, txt0, txt1, fn0, fn1, n_ctx, algo, use_native):
        """Kick off one unified-diff run; returns the UnidiffJob so the
        caller (tests / future callers) can watch or cancel it. The
        command itself NEVER blocks: whichever the algorithm, the
        engine computes in the background and the result is delivered
        to the on_finish callback on the main thread."""
        job = UnidiffJob(txt0, txt1, fn0, fn1, n_ctx, algo,
                         time.perf_counter())
        if use_native:
            self._start_native(job)
        else:
            self._start_py_thread(job)
        return job

    def notify_app_exiting(self):
        """App exit started (on_exit_pre): every later completion /
        poll pickup becomes a no-op. Call BEFORE cancel_all()."""
        self._exiting = True

    def cancel(self, job):
        """Drop one in-flight unified-diff background job: mark it
        cancelled (so a straggling completion -- the engine's finishing
        race -- cannot open a tab / print a report), stop its poll
        timer (Python background mode), and tell the native engine to
        stop its thread cooperatively (diff_proc DIF_CANCEL; the
        completion callback of a cancelled job is never invoked). The
        Python worker thread is a daemon: it finishes on its own and
        its result is simply never consumed -- a Python thread cannot
        be interrupted mid-computation safely, the same policy the
        side-by-side compare applies to its own Python-engine jobs."""
        job.cancelled = True
        if job.py_poll_cb is not None:
            try:
                ct.timer_proc(ct.TIMER_STOP, job.py_poll_cb, _POLL_MS)
            except Exception:
                pass
            job.py_poll_cb = None
        job.py_engine_thread = None
        if job.job_handle:
            dfn.cancel_async_line_diff(job.job_handle)
            job.job_handle = 0
        if job in JOBS:
            JOBS.remove(job)

    def cancel_all(self):
        """Cancel every in-flight unified-diff background job (the
        cancel commands / app exit). Returns how many jobs were
        stopped, so the caller can word its status-bar report."""
        jobs = list(JOBS)
        for job in jobs:
            self.cancel(job)
        return len(jobs)

    # -- native path ---------------------------------------------------

    def _start_native(self, job):
        """Native algorithms: ALWAYS the callback (asynchronous) form
        of diff_proc(DIF_TEXTS), whatever the file sizes -- the same
        form the side-by-side compare uses. The raw texts go to the
        engine VERBATIM (no split/join round-trip): it splits them into
        lines internally on CRLF/CR/LF -- exactly the split
        split_lines_safe produces -- so the returned line indices
        address the a / b line lists directly.

        Should the engine refuse to start the background job (rare),
        fall back to the SYNCHRONOUS native call: the command is a
        one-shot user action -- leaving the user without a diff would
        be worse than a blocking run here."""
        handle = dfn.start_async_line_diff(
            job.txt0, job.txt1,
            dfn.algo_id(job.algo),
            dfn.DIFF_IGN_NONE,
            functools.partial(self._on_native_done, job))
        if not handle:
            # Engine refused the background job: the synchronous form
            # is the only remaining way to produce the diff.
            self._log('diff_proc failed to start the background unified '
                      'diff -- running synchronously', level=1)
            try:
                job.a = split_lines_safe(job.txt0)
                job.b = split_lines_safe(job.txt1)
                matcher = dfn.CudaDiffNativeMatcher(
                    None, job.txt0, job.txt1,
                    algo=dfn.algo_id(job.algo),
                    flags=dfn.DIFF_IGN_NONE)
                # RAW engine output -- see _on_native_done.
                job.opcodes = matcher.get_opcodes()
            except Exception as ex:
                job.error = ex
            self._on_finish(job)
            return
        job.job_handle = handle
        # The engine computes on its own OS thread NOW; splitting the
        # texts for the renderer here OVERLAPS that run instead of
        # delaying the kick-off (and the completion callback cannot
        # fire before this call returns -- it is marshalled through the
        # main-thread message loop, which is busy right here).
        job.a = split_lines_safe(job.txt0)
        job.b = split_lines_safe(job.txt1)
        JOBS.append(job)
        ct.msg_status(_('Differ: diffing in background...'))

    def _on_native_done(self, job, opcodes):
        """diff_proc completion callback for the unified-diff commands'
        background line-level compare. The engine invokes this on the
        MAIN thread when its background thread finishes, passing one
        argument: the opcode list -- the same list the synchronous
        diff_proc form returns -- or None when the compare failed. The
        callback arrives through the functools.partial created at
        kick-off, so the job context travels with it. A job cancelled
        through diff_proc(DIF_CANCEL) never reaches this callback at
        all (the engine drops the result instead); the cancelled /
        _exiting checks below are belt-and-braces for the finishing
        race, exactly like the compare view's _on_native_diff_done.
        """
        if self._exiting or job.cancelled:
            return
        job.job_handle = 0
        if job in JOBS:
            JOBS.remove(job)
        if opcodes is None:
            job.error = RuntimeError('diff_proc returned None')
        else:
            # RAW engine output: the unified-diff commands deliberately
            # do NOT run the compare view's beautify passes (the
            # 'differ.algorithm.beautify.*' options,
            # absorb_trivial_equal_blocks included) -- the unified diff
            # shows the algorithm's own hunk structure, exactly what the
            # engine produced.
            job.opcodes = opcodes
        self._on_finish(job)

    # -- Python path (pure-Python engines + the 'difflib' choice) ------

    def _start_py_thread(self, job):
        """The pure-Python engines and the 'difflib' choice ALWAYS run
        on a background daemon thread -- the same two-phase shape the
        compare view's Python-engine background mode uses: kick-off ->
        worker thread -> poll timer on the main thread -> finish. The
        worker is PURE Python (line splitting included, so even that
        never blocks the UI; no CudaText API, no shared state; the GIL
        interleaves it with the main thread's message loop), so the
        app stays responsive for the whole engine run.

        The poll timer (_POLL_MS) is only a completion pickup: the
        main thread is idle while the worker computes, so the period
        paces nothing but how quickly the finished result is consumed.
        """
        def _worker(_job=job):
            try:
                _job.a = split_lines_safe(_job.txt0)
                _job.b = split_lines_safe(_job.txt1)
                if _job.algo == 'difflib':
                    (_job.uni_text,
                     _job.n_diffs) = _difflib_direct(
                        _job.a, _job.b, _job.fn0, _job.fn1, _job.n_ctx)
                else:
                    _job.opcodes = _py_opcodes(_job.algo,
                                               _job.a, _job.b)
            except Exception as _ex:
                _job.error = _ex
            finally:
                _job.done = True

        def _poll(tag='', info=''):
            # MAIN-thread pickup: finish when the worker is done; simply
            # return while it runs (the repeating timer stays armed). A
            # cancelled job (cancel command / app exit) is detected here
            # too -- a stopped timer cannot fire anymore, this covers
            # the dispatch race.
            if not job.done:
                return
            try:
                ct.timer_proc(ct.TIMER_STOP, _poll, _POLL_MS)
            except Exception:
                pass
            if job.py_poll_cb is _poll:
                job.py_poll_cb = None
            if job.cancelled or self._exiting:
                return
            # Registry cleanup is the RUNNER's job (it owns JOBS): a
            # finished background job is no longer cancellable, so it
            # leaves the registry before the completion callback runs.
            if job in JOBS:
                JOBS.remove(job)
            self._on_finish(job)

        try:
            thread = threading.Thread(
                target=_worker,
                name='cuda_differ_unidiff',
                daemon=True)
            thread.start()
            ct.timer_proc(ct.TIMER_START, _poll, _POLL_MS)
            job.py_poll_cb = _poll
            job.py_engine_thread = thread
            JOBS.append(job)
            ct.msg_status(_('Differ: diffing in background...'))
        except Exception:
            # Thread / timer unavailable (exotic host, test sandbox) or a
            # raise in the kick-off tail: disarm whatever half-started
            # (the daemon worker finishes on its own and its result is
            # simply never consumed -- job.cancelled keeps a straggling
            # poll from double-finishing) and run the engine INLINE on a
            # fresh job instead, like the compare view's legacy inline
            # fallback. The fresh job keeps the ORIGINAL kick-off time,
            # so the reported duration still covers the whole command.
            if job.py_poll_cb is _poll:
                job.py_poll_cb = None
            job.py_engine_thread = None
            job.cancelled = True
            self._log('unified diff could not run in the background -- '
                      'running synchronously', level=1)
            inline = UnidiffJob(job.txt0, job.txt1, job.fn0, job.fn1,
                                job.n_ctx, job.algo, job.start)
            try:
                inline.a = split_lines_safe(inline.txt0)
                inline.b = split_lines_safe(inline.txt1)
                if inline.algo == 'difflib':
                    (inline.uni_text,
                     inline.n_diffs) = _difflib_direct(
                        inline.a, inline.b, inline.fn0, inline.fn1,
                        inline.n_ctx)
                else:
                    inline.opcodes = _py_opcodes(inline.algo,
                                                 inline.a, inline.b)
            except Exception as ex:
                inline.error = ex
            self._on_finish(inline)
