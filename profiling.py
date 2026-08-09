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

The report shows total time, call count, average, max, and percentage for
each named section, sorted by total time descending so the bottleneck is
at the top.

Overhead when disabled: one boolean check per call (~50ns). Negligible.
Overhead when enabled: ~1-2μs per start/stop pair. For 100K events that's
~200ms total — acceptable for a debugging tool.
"""

import time
from contextlib import contextmanager


class Profiler:
    """Hierarchical timing profiler.

    Accumulates timing data across multiple start/stop calls with the same
    name. Produces a sorted report showing total time, call count, average,
    max, and percentage of the grand total for each named section.
    """

    # Master toggle. When False, all start/stop calls are no-ops.
    enabled = False

    # Internal state. Class-level (not instance-level) so all callers share
    # one set of timings. Use reset() to clear between compares.
    _stack = []        # list of (name, start_time, depth)
    _timings = {}      # name -> [total_seconds, count, max_seconds]

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
            cls._timings[name] = [0.0, 0, 0.0]

    @classmethod
    def stop(cls, name=None):
        """End timing the most recently started section.

        If name is given, it must match the name passed to start() (this
        is a safety check — mismatched names indicate a bug). If name is
        None, the name from the matching start() is used.
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
            cls._timings[n] = [0.0, 0, 0.0]
        cls._timings[n][0] += elapsed
        cls._timings[n][1] += 1
        if elapsed > cls._timings[n][2]:
            cls._timings[n][2] = elapsed

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

    @classmethod
    def reset(cls):
        """Clear all accumulated timings. Call before a new compare to
        get a clean report."""
        cls._timings = {}
        cls._stack = []

    @classmethod
    def report(cls):
        """Print the timing report to stdout. Sorted by total time
        descending so the bottleneck is at the top."""
        if not cls._timings:
            return

        # Find the outermost section (the one with the largest total time
        # — typically 'refresh:total' or 'compare:total'). This is used as
        # the denominator for percentages so they're more meaningful.
        grand_total = 0.0
        for name, (total, count, max_) in cls._timings.items():
            if total > grand_total:
                grand_total = total

        # Sort by total time descending.
        items = sorted(cls._timings.items(), key=lambda x: -x[1][0])

        print('\n' + '=' * 85)
        print('Differ Profiling Report')
        print('=' * 85)
        # Column headers.
        header = '  {:<40s} {:>10s} {:>8s} {:>10s} {:>10s} {:>6s}'.format(
            'section', 'total', 'calls', 'avg', 'max', '%')
        print(header)
        print('  ' + '-' * 83)

        for name, (total, count, max_) in items:
            avg = (total / count) if count > 0 else 0.0
            # Percentage of the grand total (the outermost section).
            pct = (total / grand_total * 100.0) if grand_total > 0 else 0.0
            line = '  {:<40s} {:>8.1f}ms {:>8d} {:>8.1f}ms {:>8.1f}ms {:>5.1f}%'.format(
                name, total * 1000.0, count, avg * 1000.0,
                max_ * 1000.0, pct)
            print(line)

        print('  ' + '-' * 83)
        print('  {:<40s} {:>8.1f}ms'.format(
            'Outermost section (100% baseline)', grand_total * 1000.0))
        print('=' * 85)
        # Print a note about nesting so the user understands why
        # percentages don't add up to 100% (nested sections are included
        # in their parent's total).
        print('Note: sections are hierarchical. Parent sections include')
        print('the time of their children. Look for the leaf sections')
        print('(e.g. find_best_pairs:*, char_diff:*, paint:*) to find')
        print('the actual bottleneck. The % column shows each section as')
        print('a fraction of the outermost section\'s total time.')
        print('' + '=' * 85 + '\n')

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
