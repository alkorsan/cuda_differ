"""Lightweight hierarchical profiler for the Differ plugin.

REWRITTEN 2026-09-21. The old design instrumented every hot-loop
iteration (one section per char-diff pair, per marker, per gap, per
event: ~4 million start/stop pairs on a 1M-line / 200k-block compare).
Each pair cost ~1-4 us of pure bookkeeping, and -- worse -- the
bookkeeping was charged to the section that happened to be open, so the
"replace_block:positional_pair" row absorbed both the profiler's own
overhead and the consumer's per-event dispatch work (the generator is
suspended inside that section while the consumer paints). The report
therefore showed positional_pair as a ~20s "bottleneck" that was mostly
the profiler itself. The rewrite:

1. Two APIs, two granularities:
     start(name)/stop()  -- PHASE sections: refresh phases, engine call,
                            one section per REPLACE-block chunk. Bounded
                            number of instances per compare (~1000s).
     mark(name, dt, calls, dt_max) -- HOT-LOOP rows: the caller times a
                            stretch of hot code with time.perf_counter()
                            (a ~60ns read; safe per pair) and books it
                            WITHOUT pushing a frame. ~0.3 us per mark.
   Per-pair/per-event sections no longer exist anywhere.

2. stop() books to the frame that was actually popped. The name
   argument is accepted but ignored (the old stop() booked to whatever
   name you PASSED, so a mismatched stop corrupted the tree silently).

3. Marks nest correctly: mark() adds its dt to the innermost open
   frame's child time, so the enclosing section's self excludes the
   marked time. No double counting, no negative self times.

4. Sections must not be open across a `yield`: while a generator is
   suspended, consumer code runs with the generator's frame still on
   the stack and everything the consumer does would be booked into the
   suspended section's self. The differ's compare() therefore builds
   each opcode's events into lists and yields only AFTER closing its
   section (see differ_native.compare / differ_python.compare).

5. The report micro-benchmarks the machinery at print time and prints
   an ESTIMATED OVERHEAD line with ALL THREE cost components:
   start/stop sections, mark() bookings, and the call-site timing ops
   (every timed item pays a perf_counter pair + accumulation -- the
   biggest component on big files; the old estimate ignored it, and
   also counted the benchmark's own 3x20000 ops into the totals).
   The benchmark snapshots/restores the counters, so the printed
   counts are the RUN's counts. When the cProfile layer ran during the
   compare, report(cprofile_was_on=True) prints an inflation warning
   banner -- a tracing profiler inflates every cheap-call row 2-3x.

Row semantics:
  total = wall time including nested children/marks
  self  = wall time in this section EXCLUDING nested children/marks
  calls = start/stop pairs (sections) or summed call count (marks)
  max   = longest single section instance; for batched marks, the
          largest single call (or per-call average of one batch when
          the caller reports dt_max from per-call timing)

Usage:
    Profiler.start('refresh')
    ...
    Profiler.start('refresh:get_text') ... Profiler.stop()
    ...
    # hot loop: time with perf_counter, book once per batch
    t0 = time.perf_counter(); ops = engine(a, b); dt = perf_counter()-t0
    Profiler.mark('char_diff', dt, 1, dt)
    ...
    Profiler.stop()
    Profiler.report(files=[('Left', '/a.py'), ('Right', '/b.py')])

Module-level wrappers enable_profiling/is_profiling_enabled/
profiling_report/reset_profiling are kept: the plugin's __init__.py
imports them. start_profiling/stop_profiling are the cProfile LAYER
(see the bottom of this file): function-level attribution for one
whole refresh_compare, printed alongside the section report.
"""

import time


class _Row:
    __slots__ = ('total', 'self_t', 'calls', 'max_dt')

    def __init__(self):
        self.total = 0.0
        self.self_t = 0.0
        self.calls = 0
        self.max_dt = 0.0


class _Frame:
    __slots__ = ('name', 'row', 't0', 'child_dt')

    def __init__(self, name, row):
        self.name = name
        self.row = row
        self.t0 = time.perf_counter()
        self.child_dt = 0.0


class Profiler:
    """Hierarchical timing profiler -- see the module docstring."""

    # Master toggle. When False, every call is a no-op.
    enabled = False

    # Class-level state so all callers share one set of timings.
    # _stack: open _Frame objects (innermost = last).
    # _rows: name -> _Row.
    # _section_ops / _mark_ops: instrumentation instance counts, used by
    #   the report's estimated-overhead line.
    # _timed_ops: total number of TIMED OPERATIONS (sum of every mark's
    #   'calls' argument) -- each one pays a perf_counter pair plus the
    #   caller-side accumulation (dict get + adds), so this count is the
    #   multiplier for the largest overhead component, the call-site
    #   timing pattern. The old estimate ignored it completely and
    #   therefore under-reported the observer effect.
    _stack = []
    _rows = {}
    _section_ops = 0
    _mark_ops = 0
    _timed_ops = 0
    _async_pending = {}
    _async_serial = 0

    # ------------------------------------------------------------------
    # Phase sections
    # ------------------------------------------------------------------

    @classmethod
    def start(cls, name):
        """Open a phase section. Must be closed by stop() before any
        yield (see module docstring, point 4)."""
        if not cls.enabled:
            return
        row = cls._rows.get(name)
        if row is None:
            row = cls._rows[name] = _Row()
        cls._stack.append(_Frame(name, row))
        cls._section_ops += 1

    @classmethod
    def stop(cls, name=None):
        """Close the innermost open section.

        'name' is accepted for call-site readability but IGNORED: the
        time is booked to the frame that was actually popped (the old
        profiler booked to the passed name -- a mismatch corrupted the
        tree)."""
        if not cls.enabled or not cls._stack:
            return
        frame = cls._stack.pop()
        dt = time.perf_counter() - frame.t0
        row = frame.row
        row.total += dt
        row.self_t += dt - frame.child_dt
        row.calls += 1
        if dt > row.max_dt:
            row.max_dt = dt
        if cls._stack:
            cls._stack[-1].child_dt += dt

    # ------------------------------------------------------------------
    # Hot-loop marks
    # ------------------------------------------------------------------

    @classmethod
    def mark(cls, name, dt, calls=1, dt_max=None):
        """Book 'dt' seconds into row 'name' without pushing a frame.

        Use inside hot loops: time the stretch with a perf_counter pair
        in the CALLER (cheap, ~60ns per read), then mark once -- per
        item with calls=1, or once per batch with calls=N and dt_max =
        the largest single item in the batch.

        The dt is added to the innermost OPEN section's child time, so
        the enclosing section's self excludes it -- marks nest under
        sections exactly like real child sections would."""
        if not cls.enabled:
            return
        row = cls._rows.get(name)
        if row is None:
            row = cls._rows[name] = _Row()
        row.total += dt
        row.self_t += dt
        row.calls += calls
        m = dt if dt_max is None else dt_max
        if m > row.max_dt:
            row.max_dt = m
        if cls._stack:
            cls._stack[-1].child_dt += dt
        cls._mark_ops += 1
        cls._timed_ops += calls

    # ------------------------------------------------------------------
    # Context-manager form (rare, non-hot sections only)
    # ------------------------------------------------------------------

    @classmethod
    def section(cls, name):
        """`with Profiler.section('name'): ...` -- context-manager form
        of start()/stop(). Do NOT use around yields."""
        if not cls.enabled:
            return _NullSection()
        return _Section(cls, name)

    # ------------------------------------------------------------------
    # Async sections (background compare engine wait)
    # ------------------------------------------------------------------

    @classmethod
    def start_async_pair(cls, outer_name, inner_name):
        """Start a nested (outer wraps inner) section pair that is closed
        LATER, from a different call stack -- the background compare's
        kick-off -> completion-callback wait. The pair measures wall
        time; on close the inner row gets total+count+max+self, the
        outer row total+count+max (a pure wrapper), and the innermost
        open frame's child time is increased so the enclosing section's
        self never counts the engine wait.

        Returns an opaque token for stop_async_pair(); None when
        profiling is disabled."""
        if not cls.enabled:
            return None
        cls._async_serial += 1
        token = cls._async_serial
        cls._async_pending[token] = (outer_name, inner_name,
                                     time.perf_counter())
        for name in (outer_name, inner_name):
            if name not in cls._rows:
                cls._rows[name] = _Row()
        return token

    @classmethod
    def stop_async_pair(cls, token):
        """Close a pair started by start_async_pair(). Idempotent and
        safe on every abandonment path: the token is consumed on the
        first close, and a token wiped by reset() is a no-op, so a late
        callback cannot corrupt a newer compare's timings."""
        if token is None:
            return
        rec = cls._async_pending.pop(token, None)
        if rec is None:
            return
        if not cls.enabled:
            return
        outer_name, inner_name, start_time = rec
        elapsed = time.perf_counter() - start_time
        outer = cls._rows.get(outer_name)
        if outer is None:
            outer = cls._rows[outer_name] = _Row()
        inner = cls._rows.get(inner_name)
        if inner is None:
            inner = cls._rows[inner_name] = _Row()
        for row in (outer, inner):
            row.total += elapsed
            row.calls += 1
            if elapsed > row.max_dt:
                row.max_dt = elapsed
        inner.self_t += elapsed
        if cls._stack:
            cls._stack[-1].child_dt += elapsed

    # ------------------------------------------------------------------
    # Reset / toggle / report
    # ------------------------------------------------------------------

    @classmethod
    def reset(cls):
        """Clear all timings. Call before a new compare. Drops pending
        async tokens too (a late callback closes nothing)."""
        cls._rows = {}
        cls._stack = []
        cls._async_pending = {}
        cls._section_ops = 0
        cls._mark_ops = 0
        cls._timed_ops = 0

    @classmethod
    def report(cls, files=None, cprofile_was_on=False):
        """Print the timing report (stdout), sorted by SELF time
        descending -- the real bottleneck at the top, wrappers sink.

        'files': optional sequence of (label, name) pairs naming what
        was compared; printed under the title.

        'cprofile_was_on': True when the cProfile LAYER ran during this
        compare (start_profiling was active). MUST be passed then: every
        Python call was traced (~1-2us each) while the sections were
        being timed, so rows dominated by millions of cheap calls
        (paint:attr, paint:wrap_calc, char_diff:*) are inflated 2-3x.
        The report prints a warning banner instead of letting the
        inflated numbers pass silently as truth."""
        if not cls._rows:
            return
        outermost_name = None
        grand_total = 0.0
        for name, row in cls._rows.items():
            if row.total > grand_total:
                grand_total = row.total
                outermost_name = name
        sum_self = sum(r.self_t for r in cls._rows.values())
        items = sorted(cls._rows.items(), key=lambda kv: -kv[1].self_t)

        print('\n' + '=' * 100)
        print('Differ Profiling Report  (times in ms; sorted by SELF time'
              ' \u2192 real bottleneck at top)')
        if files:
            print('Compared files:')
            for label, name in files:
                print('  {:<5s}: {}'.format(str(label), name))
        if cprofile_was_on:
            print('  !! cProfile layer was ON during this run: it traces every')
            print('  !! Python call (~1-2us each), so rows with millions of')
            print('  !! cheap calls (paint:attr etc.) are INFLATED 2-3x here.')
            print('  !! For clean section numbers set ENABLE_CPROFILE=False in')
            print('  !! profiling.py and re-run; use this run for the')
            print('  !! function-level report printed after this one.')
        print('=' * 100)
        print('  {:<40s} {:>10s} {:>10s} {:>9s} {:>10s} {:>6s}'.format(
            'section', 'self', 'total', 'calls', 'max', '%'))
        print('  ' + '-' * 98)
        for name, row in items:
            pct = (row.self_t / grand_total * 100.0) if grand_total > 0 else 0.0
            print('  {:<40s} {:>8.1f}ms {:>8.1f}ms {:>9d} {:>8.1f}ms {:>5.1f}%'.format(
                name, row.self_t * 1000.0, row.total * 1000.0, row.calls,
                row.max_dt * 1000.0, pct))
        print('  ' + '-' * 98)
        print('  Outermost (100% baseline): {} = {:.1f}ms'.format(
            outermost_name if outermost_name else '(none)',
            grand_total * 1000.0))
        _sum_pct = (sum_self / grand_total * 100.0) if grand_total > 0 else 0.0
        print('  Sum of self times: {:.1f}ms ({:.1f}% of outermost)'.format(
            sum_self * 1000.0, _sum_pct))
        if grand_total - sum_self > 0.0001:
            print('  Untracked time (outside any section): {:.1f}ms ({:.1f}%)'.format(
                (grand_total - sum_self) * 1000.0, 100.0 - _sum_pct))
        # Observer effect, measured and shown instead of hidden.
        # per_site_us defaults to 0.0 so the notes below can print it
        # even when nothing was instrumented (ov is None).
        per_site_us = 0.0
        ov = cls._estimated_overhead_ms()
        if ov is not None:
            est_ms, per_sec_us, per_mark_us, per_site_us = ov
            print('  Estimated profiler overhead: ~{:.1f}ms ='.format(est_ms))
            print('      {} sections x {:.2f}us (start/stop pairs) +'.format(
                cls._section_ops, per_sec_us))
            print('      {} mark() calls x {:.2f}us (batched bookings) +'.format(
                cls._mark_ops, per_mark_us))
            print('      {} timed operations x {:.2f}us (call-site'
                  ' perf_counter pairs + accumulation)'.format(
                      cls._timed_ops, per_site_us))
            if cprofile_was_on:
                print('      (clean per-op costs; the cProfile tracing that')
                print('       inflated the rows above is NOT included)')
        print('=' * 100)
        print('Note: self  = time here EXCLUDING nested children/marks'
              ' (the real cost).')
        print('      total = time INCLUDING children.')
        print('      Rows ending in :native_engine / batched rows'
              ' (char_diff:*, paint:attr, paint:gap, paint:wrap_calc,')
        print('      paint:micromap): calls = TIMED ITEMS, not section'
              ' instances; each item')
        print(('      carries ~{:.2f}us of instrumentation (see overhead'
               ' line above).').format(per_site_us))
        print('Row families:')
        print('  refresh:*   phases of one refresh (text fetch, line split,')
        print('              wrap info, clear) + the whole-tree root')
        print('  compare:*   event GENERATION: line split, per-block pairing')
        print('              (positional_pairs / find_best_pairs), engine walk;')
        print('              compare:algorithm wraps line_diff:native_engine')
        print('              (wall time incl. the background wait)')
        print('  char_diff:* char-level engine calls, booked per chunk')
        print('  paint:*     CONSUMER work: per-operation categories')
        print('              (attr/micromap/gap/wrap_calc) + flushes')
        print('              (bookmark, marker_window, overview)')
        print('Zero-time rows are honest: paint:gap only runs for')
        print('  insert/delete hunks and wrap-height mismatches -- a compare')
        print('  with equal line counts and equal wrap counts has ~none')
        print('  (calls shows how many ran). refresh:compare_and_paint SELF =')
        print('  the residual per-event dispatch: generator resume + branch')
        print('  ladder + pending dict/list collection (incl.')
        print('  overview.add_line_state, deliberately uninstrumented: its')
        print('  per-call work is sub-us, timing it would cost more than')
        print('  the work itself).')
        print('=' * 100 + '\n')

    @classmethod
    def _estimated_overhead_ms(cls):
        """Micro-benchmark the instrumentation NOW and estimate its total
        cost for the profiled run. Returns (est_ms, per_section_us,
        per_mark_us, per_callsite_us) or None when nothing ran.

        Three components, because the instrumentation has three costs:
          * start/stop section pairs -- phase sections + one per pairing
            chunk (200k on a 1M-line compare);
          * mark() invocations -- one per batched booking;
          * CALL-SITE timing ops -- every timed operation pays a
            perf_counter pair plus the caller-side accumulation (the
            _end() closure in __init__ / the inline accumulators in the
            differs). On a 1M-line compare this is MILLIONS of ops and
            the single biggest component -- the old estimate ignored it
            entirely and under-reported the observer effect.

        The benchmark snapshots the three counters first: its own
        3 x 20000 ops must not pollute the counts (the old code counted
        them and printed e.g. '220022 sections' for a real
        200022-section run). Callers should also make sure cProfile is
        disabled before calling this -- a tracing profiler would
        triple the measured per-op costs."""
        if (cls._section_ops == 0 and cls._mark_ops == 0
                and cls._timed_ops == 0):
            return None
        n = 20000
        saved_enabled = cls.enabled
        saved_section_ops = cls._section_ops
        saved_mark_ops = cls._mark_ops
        saved_timed_ops = cls._timed_ops
        cls.enabled = True
        cls._stack.clear()
        try:
            t0 = time.perf_counter()
            for _ in range(n):
                cls.start('::ov')
                cls.stop()
            per_section = (time.perf_counter() - t0) / n
            t0 = time.perf_counter()
            for _ in range(n):
                cls.mark('::ovm', 1e-9, 1, 1e-9)
            per_mark = (time.perf_counter() - t0) / n
            # Call-site pattern, mimicking the real hot-loop shape:
            #   _t0 = perf_counter(); <work>; _end(cat, _t0) where _end
            #   does a perf_counter read + dict get + list accumulate.
            # The representative 'work' is a no-op assignment, so the
            # measured loop cost IS the instrumentation overhead.
            site_stats = {}

            def _end_bench(cat, t0_):
                dt = time.perf_counter() - t0_
                rec = site_stats.get(cat)
                if rec is None:
                    site_stats[cat] = [dt, 1, dt]
                else:
                    rec[0] += dt
                    rec[1] += 1
                    if dt > rec[2]:
                        rec[2] = dt

            t0 = time.perf_counter()
            for _ in range(n):
                _t0 = time.perf_counter()
                _w = 0
                _end_bench('::ovs', _t0)
            per_callsite = (time.perf_counter() - t0) / n
        finally:
            cls._rows.pop('::ov', None)
            cls._rows.pop('::ovm', None)
            cls._stack.clear()
            cls.enabled = saved_enabled
            cls._section_ops = saved_section_ops
            cls._mark_ops = saved_mark_ops
            cls._timed_ops = saved_timed_ops
        est = (cls._section_ops * per_section +
               cls._mark_ops * per_mark +
               cls._timed_ops * per_callsite) * 1000.0
        # never report a negative estimate
        if est < 0.0:
            est = 0.0
        return (est, per_section * 1e6, per_mark * 1e6,
                per_callsite * 1e6)

    @classmethod
    def is_enabled(cls):
        return cls.enabled

    @classmethod
    def set_enabled(cls, value):
        """Enable/disable profiling. Enabling also resets timings so the
        next report starts clean."""
        cls.enabled = bool(value)
        if cls.enabled:
            cls.reset()


class _Section:
    """Context manager for enabled profiling (no generator machinery:
    cheap __enter__/__exit__)."""

    __slots__ = ('_prof', '_name')

    def __init__(self, prof, name):
        self._prof = prof
        self._name = name

    def __enter__(self):
        self._prof.start(self._name)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._prof.stop()
        return False


class _NullSection:
    """Context manager for disabled profiling: zero work."""

    __slots__ = ()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


# --------------------------------------------------------------------------
# Module-level convenience wrappers (the API __init__.py imports).
# --------------------------------------------------------------------------

def enable_profiling(enabled=True):
    Profiler.set_enabled(enabled)


def is_profiling_enabled():
    return Profiler.is_enabled()


def profiling_report(files=None, cprofile_was_on=False):
    """Print the section report. 'cprofile_was_on' MUST be True when the
    cProfile layer ran during the compare -- the report then prints the
    inflation warning banner (see Profiler.report)."""
    Profiler.report(files, cprofile_was_on)


def reset_profiling():
    Profiler.reset()


# --------------------------------------------------------------------------
# cProfile layer: function-level attribution for one whole
# refresh_compare.
#
# The section Profiler above answers "which PHASE eats the time"; this
# layer answers "which FUNCTION eats the time" -- the question the
# section report cannot resolve (e.g. what inside the consumer loop or
# the pairing costs: _add_raw_gap? set_gap? setdefault/append? the
# generator's next()?). It is the same tool the section report is:
# a diagnostic you switch on, read, and switch off.
#
# Overheads interact: cProfile traces every Python call on the thread
# (~1-2us per call), so while this layer runs the SECTION report's rows
# are inflated (profiled compares also run 2-3x slower overall). Use
# this layer to find the hot function, then turn it off
# (ENABLE_CPROFILE = False) and read the section report for clean
# phase attribution. Both layers are gated by the SAME config switch
# (differ.advanced.enable_profiling) -- this constant only adds the
# second layer on top.
#
# The names start_profiling/stop_profiling replace the legacy thin
# aliases of Profiler.start/stop that used to live here (nothing
# imported them -- the Profiler class is used directly for sections).
# --------------------------------------------------------------------------

# Master switch for the cProfile layer. True: every profiled compare
# also runs under cProfile and prints the function-level report after
# the section report. False: only the (cheap) section Profiler runs.
ENABLE_CPROFILE = True


def start_profiling():
    """Initializes and enables the profiler, and creates an IO stream."""
    import cProfile
    import io
    pr = cProfile.Profile()
    pr.enable()
    s = io.StringIO()
    return pr, s


def stop_profiling(pr, s, sort_key='cumulative', max_lines=20, title='Profile Results'):
    """
    Disables the profiler, processes the stats, and prints them.
    Accepts pr (cProfile.Profile) and s (io.StringIO) objects.
    """
    import pstats

    # pr and s are guaranteed to be non-None if stop_profiling is called when ENABLE_PROFILING is True.

    try:
        pr.disable()
    except ValueError:
        # This can happen if an exception occurred in the profiled code before pr.enable() finished,
        # or if the profiler was stopped manually beforehand (which is now avoided).
        print(f"ERROR: Profiler for {title} was not properly enabled/disabled.")
        return

    # Get the stats object
    try:
        # Map human-readable sort_key to pstats.SortKey
        # default is sort by cumulative time (time spent in function + all sub-functions)
        sort_map = {
            'cumulative': pstats.SortKey.CUMULATIVE,
            'time': pstats.SortKey.TIME,
        }
        sortby = sort_map.get(sort_key.lower(), pstats.SortKey.CUMULATIVE)

        ps = pstats.Stats(pr, stream=s).sort_stats(sortby)

        # Print the stats to the in-memory stream 's'
        ps.print_stats(max_lines)

        # Print the captured output to the console/log
        print(f"\n--- {title} ---")
        print(s.getvalue())
    except Exception as e:
        print(f"ERROR: Error processing profiling results for {title}: {e}")


def cancel_profiling(pr):
    """Disable an abandoned cProfile run WITHOUT printing (the compare
    was cancelled / the tab closed / the engine refused to start): an
    enabled Profile keeps tracing the main thread until something else
    replaces it, so every abandonment path must turn it off. No-op for
    None and for an already-disabled Profile (after a normal epilogue
    stopped the same pr, this is a harmless second disable)."""
    if pr is None:
        return
    try:
        pr.disable()
    except ValueError:
        pass
