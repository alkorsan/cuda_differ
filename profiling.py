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
   an ESTIMATED OVERHEAD line (instances x measured per-op cost), so
   the observer effect is visible in the report instead of hiding
   inside the rows' self times.

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

Module-level wrappers start_profiling/stop_profiling/enable_profiling/
is_profiling_enabled/profiling_report/reset_profiling are kept: the
plugin's __init__.py imports them.
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
    _stack = []
    _rows = {}
    _section_ops = 0
    _mark_ops = 0
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

    @classmethod
    def report(cls, files=None):
        """Print the timing report (stdout), sorted by SELF time
        descending -- the real bottleneck at the top, wrappers sink.

        'files': optional sequence of (label, name) pairs naming what
        was compared; printed under the title."""
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
        ov_ms = cls._estimated_overhead_ms()
        if ov_ms is not None:
            print('  Estimated profiler overhead: ~{:.1f}ms '
                  '({} sections + {} marks, micro-benchmarked)'.format(
                      ov_ms, cls._section_ops, cls._mark_ops))
        print('=' * 100)
        print('Note: self  = time here EXCLUDING nested children/marks'
              ' (the real cost).')
        print('      total = time INCLUDING children.')
        print('      Hot loops (char_diff etc.) are timed by perf_counter'
              ' and booked in batches (calls = total items).')
        print('=' * 100 + '\n')

    @classmethod
    def _estimated_overhead_ms(cls):
        """Micro-benchmark the section/mark machinery now and estimate
        the total instrumentation cost of the profiled run. Returns
        None when nothing was instrumented."""
        if cls._section_ops == 0 and cls._mark_ops == 0:
            return None
        n = 20000
        saved = cls.enabled
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
        finally:
            cls._rows.pop('::ov', None)
            cls._rows.pop('::ovm', None)
            cls._stack.clear()
            cls.enabled = saved
        est = (cls._section_ops * per_section +
               cls._mark_ops * per_mark) * 1000.0
        # never report a negative estimate
        return est if est >= 0.0 else 0.0

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

def start_profiling(name='section'):
    Profiler.start(name)


def stop_profiling(name=None):
    Profiler.stop(name)


def enable_profiling(enabled=True):
    Profiler.set_enabled(enabled)


def is_profiling_enabled():
    return Profiler.is_enabled()


def profiling_report(files=None):
    Profiler.report(files)


def reset_profiling():
    Profiler.reset()
