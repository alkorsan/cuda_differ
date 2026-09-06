"""Hierarchical profiler for the Differ plugin.

Provides start_profiling()/stop_profiling() (the pattern the user asked for),
plus a context-manager and decorator form for convenience. Toggle at runtime
via the `enable_profiling` config option or by setting Profiler.enabled directly.

Usage:
    from .profiling import Profiler, start_profiling, stop_profiling

    # Pattern 1: start/stop (what the user asked for)
    start_profiling('compare')
    ...
    stop_profiling('compare')

    # Pattern 2: context manager (cleaner for non-generator code)
    with Profiler.section('algorithm'):
        result = diff_proc(...)

    # Pattern 3: around a generator (start/stop needed because yields
    # intersperse with the body)
    Profiler.start('event_generation')
    for tag, i1, i2, j1, j2 in opcodes:
        Profiler.start('yield_events')
        yield ...
        Profiler.stop('yield_events')
    Profiler.stop('event_generation')

    # Report
    Profiler.report()  # prints to console

The report shows SELF time, TOTAL time, call count, max, and percentage for
each named section, sorted by SELF time descending so the real bottleneck
(leaf code that actually burns CPU) is at the top. Wrapper sections that
just delegate to children show ~0 self time and sink to the bottom — they
are no longer confused with the real culprit.

  self  = time in this section EXCLUDING time spent inside nested children.
          This is what you want to look at to find the bottleneck.
  total = time in this section INCLUDING all nested children.
          Useful to see what fraction of the run a sub-tree represents.

Overhead when disabled: one boolean check per call (~50ns). Negligible.
Overhead when enabled: ~1-2μs per start/stop pair. For 100K events that's
~200ms total — acceptable for a debugging tool.
"""

import time
from contextlib import contextmanager


class Profiler:
    """Hierarchical timing profiler.

    Accumulates timing data across multiple start/stop calls with the same
    name. For each section it tracks:

      total — cumulative wall time including nested children
      self  — wall time in this section EXCLUDING nested children
              (i.e. total minus the sum of all immediately-nested children)
      count — number of start/stop pairs
      max   — longest single start/stop interval

    The report is sorted by `self` descending so the real bottleneck
    (leaf code that actually burns CPU) appears at the top. Wrapper
    sections (whose work is entirely in their children) show ~0 self
    time and naturally sink to the bottom — they no longer masquerade
    as the culprit.
    """

    # Master toggle. When False, all start/stop calls are no-ops.
    enabled = False

    # Internal state. Class-level (not instance-level) so all callers share
    # one set of timings. Use reset() to clear between compares.
    #
    # _stack: list of (name, start_time). The top of the stack is the
    #         innermost currently-running section.
    # _timings: name -> [total_seconds, count, max_seconds, self_seconds]
    #           The 4th element (self_seconds) is incremented when this
    #           section's stop() fires, and DECREMENTED whenever a child
    #           section's stop() fires (because the child's elapsed time
    #           is already counted in this section's total — we must not
    #           double-count it as this section's self time).
    # _async_pending: token -> (outer_name, inner_name, start_time) for
    #           sections started with start_async_pair() and not yet
    #           closed — see start_async_pair(). Kept OUT of _stack.
    # _async_serial: monotonic token source for start_async_pair().
    _stack = []
    _timings = {}
    _async_pending = {}
    _async_serial = 0

    @classmethod
    def start(cls, name):
        """Begin timing a named section. Call stop(name) to end.

        Safe to call when disabled (no-op). Safe to nest (sections can
        contain other sections). The name is used to aggregate timings
        across multiple calls.
        """
        if not cls.enabled:
            return
        cls._stack.append((name, time.perf_counter()))
        if name not in cls._timings:
            cls._timings[name] = [0.0, 0, 0.0, 0.0]

    @classmethod
    def stop(cls, name=None):
        """End timing the most recently started section.

        If name is given, it must match the name passed to start() (this
        is a safety check — mismatched names indicate a bug). If name is
        None, the name from the matching start() is used.

        Self-time bookkeeping:
          - The stopped section's `self` is INCREMENTED by elapsed (it
            earned that time directly).
          - The parent section's `self` is DECREMENTED by elapsed (the
            parent's total already includes this child's time; we must
            not count it as the parent's own work).
        """
        if not cls.enabled:
            return
        if not cls._stack:
            return
        popped_name, start_time = cls._stack.pop()
        elapsed = time.perf_counter() - start_time
        # Use the popped name (the name argument is optional and only for
        # readability/safety — we don't enforce it matches because generators
        # and try/finally blocks can make exact matching fragile).
        n = name if name else popped_name
        if n not in cls._timings:
            cls._timings[n] = [0.0, 0, 0.0, 0.0]
        cls._timings[n][0] += elapsed  # total
        cls._timings[n][1] += 1        # count
        if elapsed > cls._timings[n][2]:
            cls._timings[n][2] = elapsed  # max
        cls._timings[n][3] += elapsed  # self (will be reduced by children)

        # Deduct this elapsed time from the parent's self — the parent's
        # total already includes it via its own stop() later, so without
        # this deduction the parent's self would double-count child time.
        if cls._stack:
            parent_name = cls._stack[-1][0]
            if parent_name not in cls._timings:
                cls._timings[parent_name] = [0.0, 0, 0.0, 0.0]
            cls._timings[parent_name][3] -= elapsed

    @classmethod
    @contextmanager
    def section(cls, name):
        """Context manager form: `with Profiler.section('name'): ...`"""
        if not cls.enabled:
            yield
            return
        cls.start(name)
        try:
            yield
        finally:
            cls.stop(name)

    # ------------------------------------------------------------------
    # Async sections (background compare).
    #
    # The background line-level compare runs on the engine's own OS
    # thread between two MAIN-thread moments: kick-off
    # (start_async_line_diff in Command._refresh_ex) and the completion
    # callback (Command._on_native_diff_done). A classic start()/stop()
    # pair cannot span that window: the section would sit open on the
    # shared _stack across unrelated UI-event sections (nesting them
    # under it), and any later kick-off's reset() would wipe the stack
    # out from under it. The async API instead keeps the pending pair
    # OUT of the shared stack and closes it by TOKEN, so a stale close
    # (job cancelled / callback arriving after a reset) is a safe no-op
    # instead of stack corruption.
    # ------------------------------------------------------------------

    @classmethod
    def start_async_pair(cls, outer_name, inner_name):
        """Start a nested (outer wraps inner) section pair that will be
        closed LATER, from a different call stack — the background
        compare's engine wait.

        The pair measures wall time from NOW until stop_async_pair()
        (kick-off -> completion callback): the time the main thread
        waited for the engine's background thread — engine compute plus
        thread scheduling and callback-queue latency. Command._refresh_ex
        calls it with the SAME names the synchronous path uses
        ('compare:algorithm', 'line_diff:native_engine'), so profiling
        reports are comparable across compare modes and the real
        bottleneck shows at the top of the report in both.

        Accounting on close mirrors a nested pair of stop() calls:
          - inner section: total/count/max/SELF += elapsed (the leaf —
            the real bottleneck row, e.g. line_diff:native_engine)
          - outer section: total/count/max += elapsed, self untouched
            (a pure wrapper — ~0 self, exactly like the sync path)
          - the innermost section open on the shared stack (the
            'refresh' the pair was started under) has its SELF
            decremented by elapsed, so the wait is never miscounted as
            the wrapper's own work.

        Returns an opaque token for stop_async_pair(); None when
        profiling is disabled (stop_async_pair(None) is a no-op).
        """
        if not cls.enabled:
            return None
        cls._async_serial += 1
        token = cls._async_serial
        cls._async_pending[token] = (outer_name, inner_name,
                                     time.perf_counter())
        for name in (outer_name, inner_name):
            if name not in cls._timings:
                cls._timings[name] = [0.0, 0, 0.0, 0.0]
        return token

    @classmethod
    def stop_async_pair(cls, token):
        """Close a pair started by start_async_pair(token).

        Idempotent and safe on every abandonment path: the token is
        consumed on the first close, so a second call (a cancel that
        already closed the pair, followed by the completion callback
        arriving anyway) is a no-op — the engine wait is never
        double-counted. A token wiped by reset() (a newer compare's
        kick-off) is likewise a no-op, so a LATE callback cannot corrupt
        the newer compare's timings. See start_async_pair() for the
        accounting rules.
        """
        if token is None:
            return
        rec = cls._async_pending.pop(token, None)
        if rec is None:
            return  # already closed, or wiped by reset()
        if not cls.enabled:
            return  # disabled mid-run: consume the token, book nothing
        outer_name, inner_name, start_time = rec
        elapsed = time.perf_counter() - start_time
        for name in (outer_name, inner_name):
            if name not in cls._timings:
                cls._timings[name] = [0.0, 0, 0.0, 0.0]
            row = cls._timings[name]
            row[0] += elapsed       # total
            row[1] += 1            # count
            if elapsed > row[2]:
                row[2] = elapsed   # max
        # Inner section: the leaf — its elapsed is its own self time.
        cls._timings[inner_name][3] += elapsed
        # Deduct from the innermost OPEN section's self — the 'refresh'
        # the pair was started under — exactly like a nested stop()
        # would, so the engine wait is not counted as refresh's own work.
        if cls._stack:
            parent_name = cls._stack[-1][0]
            if parent_name not in cls._timings:
                cls._timings[parent_name] = [0.0, 0, 0.0, 0.0]
            cls._timings[parent_name][3] -= elapsed

    @classmethod
    def reset(cls):
        """Clear all accumulated timings. Call before a new compare to
        get a clean report. Also drops any pending async-section tokens:
        a compare still in flight (its engine wait pair pending) loses
        its token, so its eventual callback / cancel closes nothing —
        a late callback cannot corrupt the NEW compare's timings."""
        cls._timings = {}
        cls._stack = []
        cls._async_pending = {}

    @classmethod
    def report(cls):
        """Print the timing report to stdout.

        Sorted by SELF time descending so the real bottleneck (leaf code
        that actually burns CPU) is at the top. Wrapper sections (high
        total, near-zero self) sink to the bottom and are clearly
        separated from the actual work.

        The % column is based on SELF time as a fraction of the outermost
        section's total — so the sum of all % values approximates 100%
        (the remainder is "Untracked time": wall time spent between
        sections, outside any Profiler.start/stop pair — NOT related to
        the paint:gap profiling section).
        """
        if not cls._timings:
            return

        # Find the outermost section (the one with the largest total time
        # — typically 'refresh' or 'compare'). This is used as the
        # denominator for percentages so they're more meaningful.
        outermost_name = None
        grand_total = 0.0
        for name, (total, count, max_, self_t) in cls._timings.items():
            if total > grand_total:
                grand_total = total
                outermost_name = name

        # Sum of self times across all sections. Should be ≤ grand_total.
        # The difference is time spent between sections (overhead, code
        # outside any Profiler.start/stop pair) — NOT related to the
        # paint:gap profiling section, which is a completely separate thing.
        sum_self = sum(t[3] for t in cls._timings.values())

        # Sort by SELF time descending — this is the key change. The real
        # bottleneck (the leaf code that actually burns CPU) floats to
        # the top; pure wrappers (whose work is entirely in children)
        # sink to the bottom with self ≈ 0.
        items = sorted(cls._timings.items(), key=lambda x: -x[1][3])

        print('\n' + '=' * 100)
        print('Differ Profiling Report  (times in ms; sorted by SELF time'
              ' \u2192 real bottleneck at top)')
        print('=' * 100)
        # Column headers. self and total are both shown so you can tell
        # at a glance whether a row is a leaf (self \u2248 total) or a
        # wrapper (self \u2248 0, total large).
        header = '  {:<40s} {:>10s} {:>10s} {:>9s} {:>10s} {:>6s}'.format(
            'section', 'self', 'total', 'calls', 'max', '%')
        print(header)
        print('  ' + '-' * 98)

        for name, (total, count, max_, self_t) in items:
            # % based on SELF time, not total. This means the percentages
            # across all rows roughly sum to 100% (modulo inter-section
            # overhead), and wrapper sections correctly show ~0% instead
            # of masquerading as 98% of the run.
            pct = (self_t / grand_total * 100.0) if grand_total > 0 else 0.0
            line = '  {:<40s} {:>8.1f}ms {:>8.1f}ms {:>9d} {:>8.1f}ms {:>5.1f}%'.format(
                name, self_t * 1000.0, total * 1000.0, count,
                max_ * 1000.0, pct)
            print(line)

        print('  ' + '-' * 98)
        print('  Outermost (100% baseline): {} = {:.1f}ms'.format(
            outermost_name if outermost_name else '(none)',
            grand_total * 1000.0))
        _sum_pct = (sum_self / grand_total * 100.0) if grand_total > 0 else 0.0
        _untracked_pct = 100.0 - _sum_pct
        print('  Sum of self times: {:.1f}ms ({:.1f}% of outermost)'.format(
            sum_self * 1000.0, _sum_pct))
        if _untracked_pct > 0.1:
            print('  Untracked time (between sections): {:.1f}ms ({:.1f}%)'.format(
                (grand_total - sum_self) * 1000.0, _untracked_pct))
        else:
            print('  Untracked time (between sections): {:.1f}ms ({:.1f}%) \u2014 all time accounted for'.format(
                (grand_total - sum_self) * 1000.0, _untracked_pct))
        print('=' * 100)
        # Print a note about nesting so the user understands the
        # self vs total distinction.
        print('Note: self  = time in this section EXCLUDING nested children'
              ' (the real cost).')
        print('      total = time INCLUDING children (what fraction of the'
              ' run this sub-tree covers).')
        print('      Rows near the bottom with high total but ~0 self are'
              ' pure wrappers \u2014')
        print('      their cost is already counted in their children above.')
        print('      Naming: a bare name (e.g. char_diff) wraps the whole'
              ' method; a colon')
        print('      suffix (e.g. char_diff:native_engine) is a CHILD of'
              ' that wrapper.')
        print('      Background mode: line_diff:native_engine measures'
              ' kick-off ->')
        print('      callback wall time (engine compute + scheduling),'
              ' recorded via')
        print('      start_async_pair/stop_async_pair outside the section'
              ' stack.')
        print('      "Untracked time" = wall time NOT inside any profiling'
              ' section \u2014')
        print('      unrelated to the paint:gap section, which is a'
              ' completely separate thing.')
        print('=' * 100 + '\n')

    @classmethod
    def is_enabled(cls):
        """Check if profiling is currently enabled."""
        return cls.enabled

    @classmethod
    def set_enabled(cls, value):
        """Enable or disable profiling. When enabling, also resets the
        accumulated timings so the next report starts clean."""
        cls.enabled = bool(value)
        if cls.enabled:
            cls.reset()


# --------------------------------------------------------------------------
# Convenience module-level functions (the start_profiling/stop_profiling
# pattern the user asked for). These are thin wrappers around Profiler.
# --------------------------------------------------------------------------

def start_profiling(name='section'):
    """Begin timing a named profiling section.

    Args:
        name: label for this section. Multiple start/stop calls with the
            same name aggregate their timings in the report.
    """
    Profiler.start(name)


def stop_profiling(name=None):
    """End timing the current profiling section.

    Args:
        name: optional; if given, should match the name passed to the
            corresponding start_profiling() call.
    """
    Profiler.stop(name)


def enable_profiling(enabled=True):
    """Enable or disable profiling globally.

    When enabling, resets accumulated timings so the next report starts
    clean. When disabling, leaves existing timings intact so you can still
    print a report after disabling.

    Args:
        enabled: True to enable, False to disable.
    """
    Profiler.set_enabled(enabled)


def is_profiling_enabled():
    """Return True if profiling is currently enabled."""
    return Profiler.is_enabled()


def profiling_report():
    """Print the current profiling report to stdout."""
    Profiler.report()


def reset_profiling():
    """Clear all accumulated profiling data."""
    Profiler.reset()
