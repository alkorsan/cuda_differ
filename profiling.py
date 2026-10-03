"""Lightweight hierarchical profiler for the Differ 2 plugin.

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
     mark_standalone(name, dt, calls, dt_max) -- the THREAD-SAFE mark
                            variant for NON-MAIN threads (the Python
                            engine's background thread books its absorb
                            pass here): books the row without nesting
                            into any open section, so a booking from
                            another thread can never corrupt the main
                            thread's self times.
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
    # 'spans': None, or a list of (t_start, t_end, parent_hint)
    # perf_counter triples recorded by bookings that passed t0= (see
    # mark / mark_standalone / stop_async_pair). Spans let the REPORT
    # rebuild the nesting that a shared section stack cannot give
    # cross-thread work: the report subtracts the union of all child
    # spans from a parent row's total, exactly like the stack subtracts
    # child sections -- so a span-aware family's SELF times sum to
    # ~100% of its outermost row. 'parent_hint' (or None) names the row
    # this stretch belongs to when time containment alone cannot tell:
    # overlapping siblings (the two parallel walks start at the same
    # instant, and the shorter walk's span contains the other side's
    # listings too -- the hint pins each listing to its own walk).
    __slots__ = ('total', 'self_t', 'calls', 'max_dt', 'spans')

    def __init__(self):
        self.total = 0.0
        self.self_t = 0.0
        self.calls = 0
        self.max_dt = 0.0
        self.spans = None


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
    def mark(cls, name, dt, calls=1, dt_max=None, t0=None):
        """Book 'dt' seconds into row 'name' without pushing a frame.

        Use inside hot loops: time the stretch with a perf_counter pair
        in the CALLER (cheap, ~60ns per read), then mark once -- per
        item with calls=1, or once per batch with calls=N and dt_max =
        the largest single item in the batch.

        The dt is added to the innermost OPEN section's child time, so
        the enclosing section's self excludes it -- marks nest under
        sections exactly like real child sections would.

        t0: the stretch's START perf_counter reading. When given, the
        row records the interval (t0, t0+dt) as a SPAN: the report then
        nests this row under whichever span-aware row was running at
        that time (see _Row.spans) -- the cross-thread analogue of the
        stack nesting above. Only pass it when the caller already read
        t0 for the dt (zero extra syscalls); the span tuple is the only
        added cost."""
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
        if t0 is not None:
            if row.spans is None:
                row.spans = []
            row.spans.append((t0, t0 + dt, None))
        if cls._stack:
            cls._stack[-1].child_dt += dt
        cls._mark_ops += 1
        cls._timed_ops += calls

    # ------------------------------------------------------------------
    # Thread-safe marks (non-main threads)
    # ------------------------------------------------------------------

    @classmethod
    def mark_standalone(cls, name, dt, calls=1, dt_max=None, t0=None,
                        parent=None):
        """Book 'dt' seconds into row 'name' WITHOUT touching the
        section stack -- the thread-safe variant of mark().

        For callers that run on a NON-MAIN thread: the Python engine's
        background thread books its _absorb_trivial_equal_blocks steps
        here, and the folder compare books its whole dirs:* family
        here. A regular mark() from another thread would add its dt to
        the innermost section open on the MAIN thread at that moment
        (the shared _stack), driving that section's self time toward
        negative values; a start()/stop() pair would interleave frames
        with the main thread's own pushes.

        t0: the stretch's START perf_counter reading. When given, the
        row records (t0, t0+dt) as a SPAN, and the report nests the
        row by TIME CONTAINMENT (see _Row.spans): a background stretch
        becomes a child of the span-aware row that was open around it
        (e.g. dirs:listing under dirs:walk_left under dirs:worker) --
        the cross-thread equivalent of the stack, WITHOUT any shared
        state at booking time. Span lists are appended from several
        pool threads; list.append is GIL-atomic, so the only risk is
        interleaved ORDER, which containment does not care about.

        parent: the row NAME this stretch belongs to, for the one case
        time containment cannot decide -- overlapping siblings. The
        two parallel walks start at the same instant, so the shorter
        walk's span CONTAINS the other side's early listings; the hint
        (e.g. 'dirs:walk_left', from the listing's side) pins each
        span to its own walk. Ignored when the hinted row has no
        containing span; containment then decides as usual.

        Row booking itself (dict get/set + float adds on distinct
        rows) is GIL-atomic; the only shared counters (_mark_ops /
        _timed_ops) could lose one increment in a rare interleave --
        harmless for the overhead ESTIMATE line."""
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
        if t0 is not None:
            if row.spans is None:
                row.spans = []
            row.spans.append((t0, t0 + dt, parent))
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

        The inner row also records the pair's interval as a SPAN
        (see _Row.spans): span-aware bookings that ran inside the pair
        (mark_standalone with t0=, e.g. the folder compare's dirs:*
        family) are then nested under the inner row by the report, and
        its SELF becomes 'the wait minus what those rows did' instead
        of the whole wall again.

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
        # The pair's interval: the inner row becomes a span container,
        # so the report can subtract everything that ran inside it (the
        # outer stays a pure wrapper with no span -- identical spans
        # from the same pair would otherwise contain each other).
        if inner.spans is None:
            inner.spans = []
        inner.spans.append((start_time, start_time + elapsed, None))
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
    def _span_accounting(cls):
        """Time-containment accounting for span-aware rows (see
        _Row.spans). Returns ({row_name: effective_self_seconds},
        {names of rows that ran in parallel with a sibling},
        parallel_overlap_seconds).

        Every recorded span is attached to a containing row: the one
        its parent HINT names when valid (overlapping siblings cannot
        be told apart by time -- see mark_standalone's 'parent'), else
        the innermost different row whose span contains it (up to
        0.5 ms of booking-order slop, clamped away at the parent's
        bounds). A span-aware row's effective self is its total minus
        the UNION of the spans attached to it -- overlapping children
        (parallel walks on the scan pool) are counted once, exactly
        like nested sections on the stack. Rows without spans keep
        their stack-computed self_t: for them nothing changes.

        parallel_overlap_seconds: the sum over every parent of
        (sum of child span durations - their union) -- the amount of
        SIMULTANEOUS work the (parallel) rows did. A serial run has
        0; a parallel scan's raw SELF sum exceeds the wall by exactly
        this much, so 'sum - overlap' closes back to ~100% of the
        outermost row in every mode."""
        spans = []
        for name, row in cls._rows.items():
            if row.spans:
                for iv in row.spans:
                    spans.append((iv[0], iv[1],
                                  iv[2] if len(iv) > 2 else None, name))
        eff = {}
        parallel = set()
        overlap_total = 0.0
        for name, row in cls._rows.items():
            eff[name] = row.self_t
        if not spans:
            return eff, parallel, overlap_total
        eps = 0.0005  # 0.5 ms of booking-order slop
        child_spans = {}   # parent name -> [(a, b, child, pa, pb)]
        # Parent lookup is a SWEEP with an interval stack, not a scan
        # of all spans: sort by start, keep exactly the spans that are
        # still open at each start (they are the only possible parents
        # -- a containing span must overlap the child's start), then
        # pick the hinted name or the tightest container among those
        # with end >= child end. O(S log S + S * depth); the naive
        # all-pairs search would make the report itself crawl on a
        # 10000-directory scan.
        order = sorted(spans, key=lambda s: (s[0], -(s[1] - s[0])))
        stack = []   # open spans: (a, b, hint, name)
        for a, b, hint, name in order:
            # Prune closed spans. Deliberately NO eps here, and <=:
            # a chain of spans where each ends exactly when the next
            # starts (back-to-back listings) must POP, or the stack
            # grows to O(S) and the scans below turn the whole sweep
            # quadratic. The booking slop is handled on the CONTAINMENT
            # side (sb >= b - eps): a span popped here could only have
            # contained children shorter than that 0.5 ms slop --
            # invisible at millisecond report precision.
            while stack and stack[-1][1] <= a:
                stack.pop()      # closed at/before this span: done
            chosen = None
            if hint:
                for sa, sb, _sh, sn in reversed(stack):
                    if sn == hint and sn != name and sb >= b - eps:
                        chosen = (sa, sb, sn)   # the hint names this row
                        break
            if chosen is None:
                best = None
                for sa, sb, _sh, sn in reversed(stack):
                    if sn != name and sb >= b - eps:
                        ln = sb - sa
                        if best is None or ln < best[1] - best[0]:
                            best = (sa, sb, sn)   # tightest container
                chosen = best
            if chosen is not None:
                child_spans.setdefault(chosen[2], []).append(
                    (max(a, chosen[0]), min(b, chosen[1]), name,
                     chosen[0], chosen[1]))
            stack.append((a, b, hint, name))

        def _union(iv):
            iv.sort()
            tot = 0.0
            cur_a = cur_b = None
            for a, b in iv:
                if cur_b is None or a > cur_b:
                    if cur_b is not None:
                        tot += cur_b - cur_a
                    cur_a, cur_b = a, b
                elif b > cur_b:
                    cur_b = b
            if cur_b is not None:
                tot += cur_b - cur_a
            return tot

        for name, row in cls._rows.items():
            kids = child_spans.get(name)
            if row.spans is None and not kids:
                continue          # plain row: stack self stands
            covered = _union([(k[0], k[1]) for k in kids]) if kids \
                else 0.0
            eff[name] = row.total - covered
            if eff[name] < 0.0:
                eff[name] = 0.0
        for parent, kids in child_spans.items():
            overlap_total += sum(k[1] - k[0] for k in kids) \
                - _union([(k[0], k[1]) for k in kids])
            kids.sort(key=lambda k: k[0])
            for i in range(len(kids)):
                b1, n1 = kids[i][1], kids[i][2]
                for j in range(i + 1, len(kids)):
                    a2, n2 = kids[j][0], kids[j][2]
                    if a2 >= b1 - eps:
                        break     # sorted by start: no later child
                    parallel.add(n1)   # overlaps child i -> siblings
                    parallel.add(n2)   # ran at the same time
        return eff, parallel, overlap_total

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
        # Span-aware effective self (see _span_accounting): rows of
        # cross-thread families (dirs:*) nest by time containment, so
        # their SELF times -- like the stack families' -- sum to ~100%
        # of the outermost row instead of double-counting the wait.
        eff, parallel, overlap = cls._span_accounting()
        outermost_name = None
        grand_total = 0.0
        for name, row in cls._rows.items():
            # The baseline (the true wall) is NOT simply the largest
            # total anymore: a span row's total SUMS its parallel
            # instances (nine pool listings can add up to more than
            # the scan wall), while its WIDEST span is the wall it
            # actually covered. Span rows therefore compete by their
            # widest span; plain section/wrapper rows (no spans) by
            # their total. A tie prefers the span-less row -- the
            # pure wrapper (dirs:scan_wall) over its inner (worker).
            if row.spans:
                key = max(iv[1] - iv[0] for iv in row.spans)
            else:
                key = row.total
            cur = cls._rows.get(outermost_name)
            better = key > grand_total + 1e-9
            if (abs(key - grand_total) <= 1e-9 and cur is not None
                    and cur.spans and not row.spans):
                better = True    # wrapper wins the tie
            if better:
                grand_total = key
                outermost_name = name
        sum_self = sum(eff.values())
        items = sorted(cls._rows.items(), key=lambda kv: -eff[kv[0]])

        print('\n' + '=' * 114)
        print('Differ 2 Profiling Report  (times in ms; sorted by SELF time'
              ' \u2192 real bottleneck at top)')
        if files:
            print('Compared files:')
            for label, name in files:
                print('  {:<5s}: {}'.format(str(label), name))
        if cprofile_was_on:
            print('  !! cProfile layer was ON during this run: it traces every')
            print('  !! Python call (~1-2us each), so rows with millions of')
            print('  !! cheap calls are INFLATED 2-3x here. The per-op paint')
            print('  !! rows (paint:attr/gap/micromap/wrap_calc) are SKIPPED')
            print('  !! under the cProfile layer (their timing calls would be')
            print('  !! traced themselves); their time is in')
            print('  !! refresh:compare_and_paint SELF. For those rows + clean')
            print('  !! section numbers turn OFF differ2.advanced.enable_cprofile')
            print('  !! (Options dialog / settings/cuda_differ2.json) and re-run;')
            print('  !! use this run for the function-level report printed')
            print('  !! after this one.')
        print('=' * 114)
        print('  {:<54s} {:>10s} {:>10s} {:>9s} {:>10s} {:>6s}'.format(
            'section', 'self', 'total', 'calls', 'max', '%'))
        print('  ' + '-' * 112)
        for name, row in items:
            disp = name + (' (parallel)' if name in parallel else '')
            pct = (eff[name] / grand_total * 100.0) if grand_total > 0 \
                else 0.0
            print('  {:<54s} {:>8.1f}ms {:>8.1f}ms {:>9d} {:>8.1f}ms {:>5.1f}%'.format(
                disp, eff[name] * 1000.0, row.total * 1000.0, row.calls,
                row.max_dt * 1000.0, pct))
        print('  ' + '-' * 112)
        print('  Outermost (100% baseline): {} = {:.1f}ms'.format(
            outermost_name if outermost_name else '(none)',
            grand_total * 1000.0))
        _sum_pct = (sum_self / grand_total * 100.0) if grand_total > 0 else 0.0
        print('  Sum of self times: {:.1f}ms ({:.1f}% of outermost)'.format(
            sum_self * 1000.0, _sum_pct))
        if overlap > 0.0005:
            # Parallel rows ran simultaneously: their per-thread SELF
            # times genuinely overlap, so the raw sum exceeds the wall
            # by exactly the overlap. Counting it once closes the
            # accounting back to ~100% in EVERY scan mode.
            _adj = sum_self - overlap
            _adj_pct = (_adj / grand_total * 100.0) if grand_total > 0 \
                else 0.0
            print('  Parallel overlap: {:.1f}ms ran simultaneously on'
                  ' several threads (the (parallel) rows);'.format(
                      overlap * 1000.0))
            print('  counting overlap once, the self times total'
                  ' {:.1f}ms ({:.1f}% of outermost)'.format(
                      _adj * 1000.0, _adj_pct))
        elif grand_total - sum_self > 0.0001:
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

        # -- Beautify passes: grouped step breakdown ------------------
        # The two option-gated post-processing layers, each printed
        # with its steps in RUN order. The flat table above sorts by
        # SELF time, which scatters the steps of one pass all over
        # the report; this block keeps every pass's rows together so
        # the step costs read as the sequence they run in. A pass
        # whose rows are all absent did not run (its option is off,
        # or nothing matched its patterns) -- said explicitly, so a
        # missing pass is never mistaken for a profiling gap.
        print('=' * 114)
        print('Beautify passes (option-gated; steps in run order):')
        for _title, _desc, _rows in (
                ('align_by_similarity',
                 're-pairs lines inside unequal REPLACE blocks',
                 (('total (blocks walked)', 'compare:align_by_similarity'),
                  ('step1 exact_match_search',
                   'align_by_similarity:step1_exact_match_search'),
                  ('step2 prefix_suffix_search',
                   'align_by_similarity:step2_prefix_suffix_search'))),
                ('absorb_trivial_equal_blocks',
                 'merges trivial EQUAL blocks into REPLACEs',
                 (('total (whole pass)', 'absorb_trivial_equal_blocks'),
                  ('step1 merge_ins_eq_del',
                   'absorb_trivial_equal_blocks:step1_merge_ins_eq_del'),
                  ('step2 absorb_short_equal',
                   'absorb_trivial_equal_blocks:step2_absorb_short_equal')))):
            print('  {} -- {}:'.format(_title, _desc))
            _any = False
            for _label, _name in _rows:
                _r = cls._rows.get(_name)
                if _r is None:
                    continue
                _any = True
                print('    {:<26s} {:>8.1f}ms {:>8.1f}ms {:>7d} {:>8.1f}ms'.format(
                    _label, _r.self_t * 1000.0, _r.total * 1000.0,
                    _r.calls, _r.max_dt * 1000.0))
            if not _any:
                print('    (no rows: the option is off, or nothing matched)')
        print('  Notes: an umbrella SELF = the pass minus its steps (list')
        print('  copy, guards, fixpoint bookkeeping); the align searches')
        print('  also run in the COLLECT pass, so step calls can exceed')
        print('  the umbrella block count; the python engine books its')
        print('  absorb steps from the background thread WITHOUT an')
        print('  umbrella row (thread-safe standalone marks -- see')
        print('  differ_python.engine_opcodes), so there the steps sum')
        print('  IS the pass total (and stays part of the engine-wait')
        print('  row too).')
        print('=' * 114)
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
        print('  refresh:*   phases of one refresh (text fetch, wrap info')
        print('              [update + API], clear, release_editors = the')
        print('              final EDACTION_UNLOCK repaint) + the')
        print('              whole-tree root')
        print('  compare:*   event GENERATION: line split, per-block pairing')
        print('              (positional_pairs / align_by_similarity), engine')
        print('              walk, PRODUCE (per event-list fetch -- the walk runs')
        print('              during the fetch; compare_and_paint SELF is then')
        print('              pure DISPATCH); compare:algorithm wraps')
        print('              line_diff:native_engine (wall time incl. the')
        print('              background wait)')
        print('  align_by_similarity:*')
        print('              steps of the Align-by-similarity beautify:')
        print('              step1 exact-match anchor search, step2 prefix/')
        print('              suffix similarity scoring; compare:align_by_')
        print('              similarity is the umbrella section per unequal')
        print('              block (see the Beautify passes block above)')
        print('  absorb_trivial_equal_blocks*')
        print('              steps of the Absorb pass: step1 merges INSERT +')
        print('              EQUAL(trivial) + DELETE into one REPLACE, step2')
        print('              absorbs a short trivial EQUAL between large')
        print('              changed blocks; the umbrella row is a section on')
        print('              the native paths (see the Beautify passes block)')
        print('  char_diff:* char-level engine calls, booked per chunk')
        print('  paint:*     CONSUMER work: per-operation categories')
        print('              (attr/micromap/gap/wrap_calc) + flushes')
        print('              (bookmark, marker_window, overview)')
        print('  dirs:*      folder compare. Its work is cross-thread')
        print('              (scan worker + pool + the UI timer), where a')
        print('              shared section stack cannot nest; these rows')
        print('              therefore record time SPANS and nest by time')
        print('              containment: scan_wall wraps worker; worker')
        print("              SELF = the scan wall minus everything booked")
        print('              inside it (walks, listings, content tests,')
        print('              UI rows, lags) -- the glue that is nobody')
        print('              else\'s row; walk_left/right cover their own')
        print('              listings. The (parallel) tag marks rows whose')
        print('              spans overlapped a SIBLING span (the two')
        print('              walks in a parallel scan): their TOTALS are')
        print('              per-thread walls and their SELF times ran at')
        print('              the same time -- the raw self-sum therefore')
        print('              exceeds the wall by exactly the parallel')
        print('              overlap, printed under the table; counting')
        print('              the overlap once, the self times total ~100%')
        print('              of the outermost row in every scan mode.')
        print('  dirs:env_probe / dirs:cprofile_import')
        print('              one-shot diagnostics booked OUTSIDE scan_wall')
        print('              (before the scan starts): the metadata')
        print('              round-trip probe and the cProfile stdlib')
        print('              import cost -- a per-SESSION cost, not a')
        print('              per-scan one.')
        print('Zero-time rows are honest: paint:gap only runs for')
        print('  insert/delete hunks and wrap-height mismatches -- a compare')
        print('  with equal line counts and equal wrap counts has ~none')
        print('  (calls shows how many ran). refresh:compare_and_paint SELF =')
        print('  the residual per-event dispatch: branch ladder + pending')
        print('  dict/list collection (incl. overview.add_line_state,')
        print('  deliberately uninstrumented: its per-call work is sub-us,')
        print('  timing it would cost more than the work itself).')
        print('Attribution caveats (read before acting on rows):')
        print('  - *_native_engine rows from async pairs measure kick-off ->')
        print('    CALLBACK ENTRY: they include the main-thread conversion')
        print('    of the engine result to Python objects (opcode tuples),')
        print('    not just engine compute time.')
        print('  - refresh:release_editors runs after the refresh root')
        print('    closed on the normal path (top-level sibling row); the')
        print('    status-bar total covers it.')
        print('  - the epilogue (report printing) is intentionally outside')
        print('    all sections: the overhead micro-benchmark clears the')
        print('    section stack.')
        print('=' * 114 + '\n')

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
# (differ2.advanced.enable_profiling) -- this constant only adds the
# second layer on top.
#
# start_profiling / stop_profiling are the cProfile layer's entry
# points (the Profiler class is used directly for sections; nothing
# else needs wrapping here).
# --------------------------------------------------------------------------

# Master switch for the cProfile layer. CONFIG-DRIVEN since v7: the
# plugin reads differ2.advanced.enable_cprofile (settings/cuda_differ2.json
# or the Options dialog, chapter Advanced) at every refresh_compare and
# starts this layer only when BOTH that option and enable_profiling are
# on -- no source edit needed to toggle it. This constant remains only
# as a documented fallback default for embedding/exotic cases; the
# normal way to switch the layer is the option. True: every profiled
# compare also runs under cProfile and prints the function-level report
# after the section report. False (default): only the (cheap) section
# Profiler runs -- clean section numbers, no 2-3x tracing inflation.
ENABLE_CPROFILE = False


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
    stopped the same pr, this is a harmless second disable).

    Accepts EITHER the bare cProfile.Profile OR the (pr, s) pair that
    start_profiling() returns: every caller stores the PAIR (job
    attribute 'cprofile', refresh_compare's _cprof). Passing the pair
    used to raise "'tuple' object has no attribute 'disable'", which
    aborted _cancel_job BEFORE DIF_CANCEL reached the engine -- the
    cancel command then had no effect and the background batch ran to
    its natural end."""
    if pr is None:
        return
    if isinstance(pr, (tuple, list)):
        # start_profiling() returns (pr, s); unpack to the Profile.
        # An empty sequence degrades to the None no-op above.
        pr = pr[0] if pr else None
        if pr is None:
            return
    try:
        pr.disable()
    except ValueError:
        # Already disabled by the epilogue's stop: harmless.
        pass
    except AttributeError:
        # Not a Profile at all (wrong object stored): profiling
        # cleanup must never break the cancel path around it.
        pass
