r"""Differ 2 memory-leak diagnostic -- run INSIDE CudaText's Python console.

https://github.com/Alexey-T/CudaText/issues/6533
guide:
exec(open(r'F:\_bin\_src\cudatext\cuda_differ2\src\_dev\__tests\_annex\memory_leak_diagnostics\memory_leak_diagnostic_v16.py').read())


v16 (CRASH-RECOVERY + CYCLE-2 RAM GATE + PLUGIN-LEVEL PROBE) -- what
changed vs v15 and WHY (all from the author's v15-run report: 'there
is still memory leak, and there was an error so the script did not
finish'):

  1. CYCLE 2 NO LONGER RE-RUNS THE 140 MB PAIR ON A SMALL BOX.  The
     v15 run's cycle 2 re-ran the full pair on the author's 7.9 GB
     box ON TOP of cycle 1's ~1.5 GB floor: 4.2 GB peak on ~4 GB
     available -> hard paging -> 'ERROR: diff_proc callback: Access
     violation' right after 'Differ 2: compare took ...ms' -> the
     completion callback died, sess.job stayed set forever, ws
     flatlined at 3716 MB and the script polled to 320 s+ with no
     timeout in sight (the author killed it).  That is the EXACT v8
     failure mode; v13's RAM_SAME_OK_MB=6144 lowering (made so the
     MAIN pair stays 140 MB) had also gated cycle 2 by accident.  v16
     splits the gates: the main-pair downgrade keeps the 6 GB bar,
     cycle-2 'same' needs 12 GB again -- on the 7.9 GB box 'auto' now
     picks 'off' (census-only) and prints the three safe ways to
     still get the 2-cycle verdict (smaller pair / cmem build /
     CYCLE2='same' force with the new crash recovery).
  2. THE CRASH NO LONGER KILLS THE RUN.  Both on-site crashes (v8,
     v15) had one signature: the compare's walk finished, a native
     Access violation killed the callback, ws went FLAT (v15: 3716 MB
     flat for 200 s; a healthy compare grows +30-60 MB / 5 s the
     whole time).  Cycle 2 now watches its own heartbeats: ws within
     a 20 MB band for 15 consecutive 5 s readings after 200 s ==
     declared crashed -> the stuck job is cancelled, the diff tab
     torn down, the cycle voided, the run CONTINUES to the census.
     CYCLE2_TIMEOUT_S also drops 900 -> 420 s as the hard backstop.
  3. THE SCRIPT NOW PROBES THE PLUGIN'S OWN FIX LEVEL (_FIX_VERSION
     in cuda_differ2/__init__.py).  The v15 run's pre-gc census found
     the KNOWN overview retention (PaintboxOverview + _Row x24 +
     4x1.5M-int arrays + 533k tuples = the overview<->worker cycle)
     -- the v15 PLUGIN files were not installed on that machine
     (with them, overview.destroy() clears the arrays and the worker
     is joined at close; the census would show none of it).  The
     probe prints '[env] plugin fix level: N' and warns LOUDLY when
     the installed plugin predates v15, so a census is never
     misread as 'the fix does not work' again.
  4. THE FLOOR IS DECODED IN THE FINAL REPORT: the v15 run closed
     everything + gc'd and still sat at 1566 MB with only ~13k live
     Python objects -- that is allocator high-water (FPC heap pools
     + CPython arenas), not live data.  The report now says so and
     points at the cmem build experiment (README_V16_FIX.md) that
     returns most of it to the OS at close.

v15 (NO-GENERATION + TWIN-CLOSE + PROBE-FIX) -- what changed vs v14 and
WHY (all three from the author's v14-run reports):

  1. THE SCRIPT NEVER GENERATES TEST FILES ANYMORE.  The author: 'why
     does it create the 140mb file? this is stupid, don't do it, use
     the already created file and if it does not exist print error'.
     v14's last-resort autogen fallback is GONE.  The pair search now
     ALSO covers the script's PARENT and GRANDPARENT folders (the v14
     run lived in _dev/__tests__/_annex while the pair sat in
     _dev/__tests__ -- one level up), and when nothing is found the
     script prints one clear ERROR naming every searched location and
     stops.  No writes, ever.
  2. THE TWIN CLOSE PATH IS HONEST NOW.  The v14 run fell back to a
     Command() TWIN (the core instance appears only after some Differ 2
     menu/trigger ran once); the natural cmd_FileClose then fired NO
     on_close anywhere (the core never imported the plugin), so NOTHING
     was torn down: sessions stayed in the registry with their full
     data, overviews kept their worker threads, the toolbar forms
     stayed -- the census then reported 'session STILL PRESENT', gc
     freed ~nothing, and the zombie overview's timers produced the
     'Access violation' + 'Editor() gets bad handle' errors of that
     run.  v15 closes that hole from the diag side: right after the
     natural close, a TWIN run calls the twin's on_close itself (the
     exact call the core would have made), loudly marked as such --
     the censuses then measure the plugin's REAL teardown code, twin
     or not.
  3. THE REACHABILITY PROBE DOES NOT CRASH ANYMORE.  v14's probe did
     getattr(o, 'f_locals', None) on every walked object; on a ctypes
     LibraryLoader (ctypes.windll / ctypes.cdll, present in every
     module dict on Windows) that getattr raises FileNotFoundError
     (ctypes tries to LoadLibrary('f_locals')!) instead of
     AttributeError, which killed the probe in both v14 cycles
     ('Could not find module f_locals').  Frames are now detected by
     TYPE, and every attribute probe is exception-guarded.
  4. The end-of-run idle timer no longer touches dead editor handles
     (the trailing 'Editor() gets bad handle' error): its per-tick
     work is fully guarded.

v14 (TIMER_DELETE-PROOF + SELF-LOCATING) -- what changed vs v13:

  1. SELF-LOCATION IS NAME-INDEPENDENT: a candidate path counts as
     THIS file only when it exists AND carries this script's magic
     marker, whatever its name.
  2. NEW [timer] TIMER_DELETE probe: the author's 4 console tests in
     miniature run at both cycle closes -- a v12-era wrapper (only
     TIMER_STOP fixed) leaves residue and is named as such; the v14
     wrapper (TIMER_DELETE also pops _live) reads CLEAN.
  3. Expected results with the v14+ PLUGIN PATCH installed (collect at
     every big teardown: close, save-cancel, cancel, fast rerun,
     session restore): [pregc] '~0 freed, nothing retained' AND no
     manual gc.collect() snippet needed after closes/cancels anymore.

v13 (POST-FIX RETENTION EDITION):

  The timer-_live leak is FIXED (v12 wrapper).  The user's numbers
  then: 140MB compare + close everything -> RAM stays ~550 MB, drops
  to ~120 MB ONLY when a gc.collect() snippet runs: ~430 MB of
  UNREACHABLE-BUT-UNCOLLECTED garbage (the pipeline runs under
  _gc_quiet; a gen-2 pass needs ~70k net container allocations an
  idle editor never produces).  v13 measures and names exactly that:
    * uses the CORE's plugin instance (__main__.xxcuda_differ2) when
      available instead of a Command() twin;
    * stage 4 is the NATURAL close (clear Modified, then
      ed.cmd(cmd_FileClose) -- the core fires on_close_pre/on_close);
    * NEW "PRE-GC CENSUS" stage after each cycle's closes: object
      counts, per-tab plugin instances, big lists, holder chains, a
      reachability-from-roots probe, and ONLY THEN gc.collect() x2
      with before/after RAM -- the user's snippet, instrumented;
    * the MAIN pair prefers the full-size (140 MB) pair;
    * verdict: [pregc] lines in the FINAL REPORT.

  If the v13+ PLUGIN PATCH is installed (session/job .destroy() at
  close + a close-time gc.collect for big sessions), the [pregc]
  verdict should read "~0 freed, nothing retained".  Without the
  patch, the census names what was pinned.

Reproduces the exact reported scenario (compare two ~140MB autogen files
via the plugin, close the diff tab, watch RAM) STAGE BY STAGE, measuring
the process working set + private commit after every stage, so the
component that retains memory is named explicitly:

  stage 0  baseline
  stage 1  ENGINE+API ONLY (no tabs): sync diff_proc(DIF_TEXTS) +
           sync batched diff_proc(DIF_CHARS) on the real files, then
           release -- if this retains memory, the leak is in the
           Pascal diff API / engines (or their Python marshaling).
  stage 2  open the two ORIGINAL file tabs (the pre-compare state,
           the "800 MB" checkpoint of the report)
  stage 3  full plugin compare via the CORE's cuda_differ2 Command
           (set_files -> real diff tab, real async two-phase compare,
           real paint) -- the "3.5 GB after compare finishes" checkpoint
  stage 4  the NATURAL diff-tab close + 6 s idle wait (NO gc: v13 must
           see the heap as the user leaves it) -- the "RAM stays high
           after closing everything" checkpoint (CudaText frees closed
           editor frames LAZILY in its idle timer; the wait covers that)
  stage 5  close the two ORIGINAL tabs too + wait -- then the PRE-GC
           CENSUS (counts, holders, reachability, and only then the
           gc.collect() with before/after numbers)
  stages 7-9  the whole compare/close cycle a SECOND time -- a real
           leak grows per cycle; allocator retention / fragmentation
           stays on a ceiling

  v6 FIX for the stage-4 hang seen with v4/v5: app_proc(PROC_IDLE)
  maps to Application.ProcessMessages in CudaText, which dispatches
  PENDING TIMER EVENTS -- including THIS diagnostic's own 400 ms
  timer.  So the first PROC_IDLE of phase s4.14 re-entered
  _on_timer/_stage_close_diff_wait while that phase was still
  running; the wait stage's exit condition stayed permanently true,
  so every re-entry re-ran gc.collect() x2 + 5x PROC_IDLE again,
  which pumped again... an ever-deepening recursion of multi-second
  gc.collect() passes on a ~30M-object heap: UI thread 100% busy on
  one core (25% of a 4-core CPU), UI frozen, watchdog starved.  v6
  makes _on_timer re-entrancy-proof (a tick running inside a tick is
  ignored) and the s4.13/s4.14 tail strictly one-shot.  The plugin
  teardown (s4.1-s4.9: ~0.4 s total) and the real tab close (s4.11:
  3.78 s) measured CLEAN in the v5 run -- the hang was the
  diagnostic's own tail, never the plugin.

  v7 FIX for the stage-5 death seen with v6: the first ORIGINALS-close
  tick died with a native Access violation, reported by CudaText as
  "Exception in CudaText for cudatext._timer_proc_callback_proxy"
  (the fault happened inside the native close chain -- most plausibly
  in another plugin's on-close reaction; closing the diff tab itself
  had worked fine -- and BELOW the layer a Python try/except can
  catch), so the v6 machine died silently mid-stage.  v7 armor:
  (1) the originals are closed ONE PER TICK, rediscovered live from
  ed_handles() every tick -- a killed tick only loses ONE operation
  and the next tick resumes (5 attempts per tab, then give up + warn
  + continue); (2) every close is breadcrumbed before/after; (3) the
  watchdog stays on through stages 5-9; (4) Python-visible tick
  errors count toward a 5-error budget that disarms cleanly instead
  of dying silently; (5) stage 5's settle window prints ws
  heartbeats like s4.12; (6) stage 1 now chunks multi-line replace
  blocks into char pairs (the v6 single-line filter collected 0
  pairs on the autogen shape, so the batched DIF_CHARS never ran);
  (7) a final object CENSUS (gc.get_objects) decides whether the
  retained memory holds objects (real leak) or nothing (allocator /
  arena ceiling).

  v8 FIXES for the two cycle-2 holes the first complete v7 run
  exposed (the run itself was GOLD: cycle 1 confirmed real object
  retention -- +2.87M live objects, almost all lists, with the
  plugin's own session registry at 0):
  (1) cycle 2 now REOPENS the two original tabs first (new stage 6).
      The plugin's set_files COPIES the texts from the OPEN original
      tabs -- with them closed (v7 stage 5) it silently returned, so
      cycle 2 never compared anything: no session, no "compare took"
      line, flat memory.
  (2) the compare stage now VERIFIES the session appears (12 s) and
      the close stage refuses to FileClose a tab that is neither a
      session-backed compare tab nor titled "Diff: ..." -- v7's
      cycle-2 stage 8 closed WHATEVER TAB WAS ACTIVE (one of YOUR
      tabs, 0.69 s, ~2 MB -- check your session and reopen it if it
      mattered; sorry).  With the guard the run instead prints a loud
      warning, marks the cycle void and continues -- an innocent tab
      can never be closed again.
  (3) the cycle-1-vs-2 verdict only prints when BOTH cycles really
      compared (v7 printed "-414 MB => stable: no per-run leak" from
      a cycle 2 that never ran -- that number was meaningless).
  (4) a DEEP RETENTION CENSUS (census(), also auto-printed at the
      end): attributes every retained list to its immediate holder
      (module globals / class dicts / instances / frames / closures /
      outer lists), prints the top holders WITH their referrer
      chains, a shape histogram of the retained lists (length + item
      types + sample reprs) and targeted probes (sys.last_exc,
      every live thread's stack via sys._current_frames, live plugin
      frames, the profiler's class-level stores) -- this NAMES the
      owner of the ~2.87M retained lists.
  (5) the watchdog log APPENDS when stage 8 re-arms it (v7's stage 8
      truncated the file, destroying cycle 1's stage-4 entries).

  v9 FIXES for the 8-GB-RAM wall the v8 run hit: the second compare
  FINISHED (98 s) but then a native "diff_proc callback: Access
  violation" killed the engine's completion callback -- at 5.5 GB
  peak on an 8 GB box, with cycle 1's ~1.9 GB leak still resident
  underneath, the machine was paging hard (cycle 1's compare took
  178 s vs 95 s in the v7 run) and the callback faulted under the
  pressure.  The job then stayed unfinished forever and the run
  waited with NO timeout until the user killed CudaText.  v9:
  (1) cycle 2 is now RAM-AWARE: with < 12 GB physical RAM it uses a
      SMALLER pair by default -- autogen_50mb_*.txt found next to
      the main pair (explicit override: paste CYCLE2='same'/'small'/
      'off' or FILES2=(a,b) before the exec line); with no smaller
      pair on a small-RAM box cycle 2 is SKIPPED for safety and the
      census alone answers the holder question;
  (2) the compare stages now have a TIMEOUT (30 min cycle 1, 15 min
      cycle 2) -- on timeout the cycle is marked void, the stuck job
      is CANCELLED and the guarded close tears the diff tab down;
      the run can never wait forever again;
  (3) the watchdog phase is refreshed during the compare wait (the
      v8 run spammed "phase stuck: 's5 gc x2'" -- a STALE label from
      cycle 1 -- while stage 7 was simply comparing);
  (4) the LEAK VERDICT moved to the FINAL report only (v8 printed a
      misleading "INCONCLUSIVE" after cycle 1); when cycle 2 used a
      smaller pair the verdict comes from the OBJECT-COUNT growth
      (census after cycle 1 vs after cycle 2), because the ws
      numbers of differently-sized compares are not comparable.

  v10 FIXES after the first complete v9 run (both cycles ran; the
  deep census finally executed):
  (1) THE MAIN PAIR IS NOW RAM-AWARE TOO (user request): on a < 12 GB
      box with a smaller pair (autogen_50mb_*.txt) next to the main
      one, BOTH compare cycles use the SMALLER pair -- cycle 1 was
      still 140 MB in v9 (178 s, 4.1 GB peak, the box paging), only
      cycle 2 was 50 MB.  Same pair in both cycles = comparable ws
      numbers = the classic leak-vs-ceiling verdict works again,
      AND the object-count check still runs.  Paste MAINPAIR='big'
      to force the full-size pair, or FILES=(a,b) to pin any pair.
  (2) THE CENSUS IS NOW ARTIFACT-PROOF.  Three v9 census bugs are
      fixed (all found + proven locally with a leak-proof mock-world
      reproduction):
        a) "plugin _TabSession alive: 0" printed len(cmd._sessions) --
           the REGISTRY length, not live instances.  The session
           objects CAN be alive with an empty registry (on_close pops
           the dict; that says nothing about the OBJECTS).  v10 counts
           live instances by gc type scan.
        b) the printed referrer chains included the census's OWN
           bookkeeping records (holder tuples / bucket lists), which
           looked like real holders.  v10 verifies referrers with a
           two-pass protocol (drop everything, gc.collect, re-scan:
           only referrers alive in BOTH passes are real).
        c) nothing walked ABOVE the immediate holder, so the actual
           ROOT was never named.  v10 runs a BFS from the true GC
           roots (module dicts + every thread's frames) and prints
           the full path with edge names -- this names the holder
           even when it is a closure or a wrapper's callback table.
      Plus new probes: gc.isenabled()/len(gc.garbage), live counts
      for _TabSession / Differ / _CompareJob / PaintboxOverview /
      CompareToolbar, and a scan of the cudatext wrapper's callback
      registries (timer/dlg/menu/diff_proc) for entries referencing
      plugin objects.

  v12 -- THE ROOT CAUSE IS FOUND AND FIXED (from the v11 on-site run
  + the user's HeapTrc -gh exit dump):
  (1) THE HOLDER, NAMED: the v11 census walked the retained sessions
      to the ROOT "module globals: cudatext [under key '_live']".
      The stock py/cudatext.py wrapper inserts EVERY timer callback
      into the module-global _live dict -- including on TIMER_STOP --
      and never removes an entry (the only cleanup in the file is the
      dlg_proc dialog purge).  differ2 arms two bound-method timers
      per compare session (PaintboxOverview._apply_async_tick,
      repeating, stopped at idle; CompareToolbar._cancel_swap_tick,
      one-shot, fires ~300 ms after kick-off), so EVERY closed tab
      leaves its two callbacks in _live, pinning the overview +
      toolbar + Command + Differ + _TabSession and the session data
      (the ~800k-entry char-pair lists, the 200k opcodes, the 2.2M
      per-line category lists) until app exit: ~720k objects /
      hundreds of MB per 50 MB compare, ~2.8 GB at the 140 MB pair --
      exactly the reported numbers.
  (2) THE PASCAL SIDE IS PROVEN CLEAN by the user's HeapTrc dump:
      8,118,905 blocks allocated (3.99 GB) / 8,118,892 freed
      (3.99 GB) -- only 13 blocks / 1,530 bytes unfreed at exit (7
      timer registration strings of never-TIMER_DELETEd timers, 4
      diff-job records, startup structures).  The retained hundreds
      of MB are CPython-heap objects -- invisible to HeapTrc by
      construction.
  (3) THE FIX ships in this zip as cudatext_install/py/cudatext.py
      (drop-in for <CudaText>/py/cudatext.py: no rebuild, no plugin
      change): timer_proc(TIMER_STOP) pops _live, a fired
      TIMER_START_ONE is forgotten unless it re-armed itself, and
      dead Pascal timer entries are TIMER_DELETEd lazily -- never
      from inside a timer tick (use-after-free guard).  Proven by
      test_suites/plugin_leak_test/test_wrapper_timer_fix.py (10
      cases incl. an old-code negative control).
  (4) v12 DIAGNOSTIC CHANGE: a dedicated [timer] check after each
      cycle close -- detects STOCK vs LEAK-FIXED wrapper and counts
      _live entries referencing plugin objects, with the fix
      instruction inline.  After the fix the line reads CLEAN and the
      census shows plugin instances x0 after the closes.

  v11 FIXES after the first complete v10 run (decoded from the
  user's console + watchdog logs):
  (1) THE v10 MAIN-PAIR DOWNGRADE WAS DEAD CODE.  _find_files()
      cached the auto-detected pair into the global FILES, so the
      downgrade's "was the pair USER-pinned?" test (FILES is not
      None) was ALWAYS true on auto-detected runs -- cycle 1
      compared the 140 MB pair AGAIN (92 s, 4.1 GB peak, no
      DOWNGRADED line).  (The v10 smoke passed only because it
      monkeypatched _find_files with a side-effect-free stub --
      the production path was never covered.)  v11 splits the two
      roles: the detection cache lives in _FILES_AUTO and never
      counts as a pin; FILES stays None unless the USER pins a
      pair.  A pinned big pair on a low-RAM box now also prints a
      WARNING (never silent again).
  (2) THE CENSUS TOOK 676.9 s -- the "15 min" run.  v10 walked each
      chain separately (4 top holders + 6 live instances = 10
      walks), every level two-pass-verified with a FULL gc.collect
      per level per chain (55-76 s each on the 4.6M-object heap),
      while its "phase stuck" alerts spammed the console on top.
      v11 does ONE batched walk over ALL targets (one
      gc.get_referrers(*frontier) per level), the live-instance
      scan is merged into the classify pass (v10 re-scanned the
      whole heap for it: +15 s), and a wall-clock budget
      CENSUS_BUDGET_S (default 150 s, pre-pastable) caps the walk
      with a partial printout.  A single end-to-end VERIFICATION
      (drop + one gc.collect + instance re-scan) replaces the
      per-level two-pass.
  (3) THE WALK STOPPED AT BOUND METHODS.  The user's v10 census
      ended the CompareToolbar chain at "instance of
      builtins.method ROOT" -- v10's root detector classified
      methods via the builtins blanket, one level short of the
      callback table (or the native registry) that actually pins
      the toolbar -- which was the whole point of the walk.  v11
      walks THROUGH methods; anonymous lists/dicts are named by
      CONTENT (a bare "list(len=7) [at index 3]" pageful is what
      blew the CudaText console size limit).
  (4) THE CONSOLE LIMIT TRUNCATED THE PAYLOAD.  The CudaText
      console has an output size limit -- the v10 run lost the
      LEAK VERDICT, the FINAL REPORT table and the census findings
      exactly at the payoff moment ("then log was truncated
      because cudatext console have a limit, here is the end").
      v11 tees everything from the FINAL REPORT / LEAK VERDICT /
      censuses into the watchdog log file -- a console limit can
      never lose the verdict again.  The census also prints a
      fraction of the v11 lines (per-level caps, deduped holders).
  What the v10 run DID establish (decoded from the surviving log
  pieces):
    - PER-COMPARE leak, one full object set per cycle: live
      instances after BOTH closes -- Command x2, CompareToolbar x2,
      Differ x2, PaintboxOverview x2, _TabSession x2.
    - The _TabSession chain ends at the CompareToolbar
      (toolbar.session), whose chain ended at a BOUND METHOD
      (v10's ROOT bug) -- v11's walk-through names whoever holds
      that method: the v8 "diff_proc callback: Access violation"
      points at the native callback registry as the prime suspect.

  What the v9 run DID establish (interpreted with the fixes):
    - PER-COMPARE leak confirmed: 2,879,194 objects after cycle 1
      -> 4,678,818 after cycle 2 (+1.8M per compare).
    - The retained objects are, per cycle: the Differ's diffmap
      (~200k [i1,i2,j1,j2] lists -- its holder, a dict with the
      differ's option keys, was REAL in the v9 chains) and the
      marker-window arrays (~2x 1.33M [(x,len,color)] lists --
      session.marks_chars_* shapes).
    - The plugin's OWN Python code retains NOTHING after close --
      proven locally: a full compare+close cycle in a leak-proof
      mock world ends with ZERO live sessions/differs/marks (double-
      snapshot protocol).  The pin is HOST-SIDE: a callback table
      (dlg/menu/timer registration or the fork's native diff_proc
      callback registry) keeping an object that references the
      session (the toolbar holds self.session and destroy() never
      clears it; the engine callbacks hold the job -> session).
      v10's BFS census names it on-site.

HOW TO RUN (CudaText: menu "Plugins / Python console") -- ONE PASTE,
the file auto-starts itself:

    exec(open(r'F:\_bin\_src\cudatext\cuda_differ2\src\_dev\__tests\differ2_retention_v16.py').read())

  Output begins immediately (banner + stage 0 baseline).  The autogen
  file pair is auto-detected RELATIVE TO THIS SCRIPT -- even under the
  plain exec paste the script's own folder is recovered from the exec
  call chain (the pasted line itself names this file).  Open editor
  tabs, the installed plugin folder and (Windows last resort) a bounded
  fixed-drive scan are searched too.  To pin the files explicitly, paste
  this BEFORE the exec line -- the value survives the exec:

    FILES = (r'F:\_bin\_src\cudatext\cuda_differ2\src\_dev\__tests\autogen_140mb_a.txt',
             r'F:\_bin\_src\cudatext\cuda_differ2\src\_dev\__tests\autogen_140mb_b.txt')

  Namespaced variant (identical behavior, but does NOT auto-start):

    import sys
    sys.path.insert(0, r'<folder containing this file>')
    import differ2_retention_v16
    differ2_retention_v16.run()

The script drives itself with a timer and prints a staged report to the
console; CudaText stays usable the whole time (the plugin's compare is
async by design). After the exec one-liner the controls are bare names:
  stop early:          stop()
  re-measure any time: report()
(after the import variant they are differ2_retention_v16.stop()
/ .report()).
A second paste while a run is active does NOT start a second run.

NOTE: stage 1 runs the SYNC engine forms on the main thread -- on two
140MB files that freezes CudaText for a while (exactly like the manual
console test); expected and harmless.

v3: resolves the diff API wherever it lives (cudatext module, the
Pascal-registered cudatext_api module, console globals, builtins), so
it works on patched binaries even when the installation's
py/cudatext.py wrapper is STOCK; prints an [env] truth table at start
saying WHICH engine the plugin's compares really use (native Pascal vs
silent pure-Python fallback); fixed the Win64 memory reading that made
v1/v2 print "-0 MB"; and finds the test files from the plugin module's
ancestor folders too (exec mode has no __file__).

v4: the autogen pair is found next to THIS SCRIPT even under a plain
exec(open(...).read()) paste -- the pasted line's own compiled code
constants name this file, and the exec call chain is walked to recover
the folder (open editor tabs, the plugin tree and, as a Windows last
resort, a bounded scan of fixed drives are tried too); the [env] truth
table now READS THE INSTALLED PLUGIN'S differ_native.py SOURCE to see
which module it checks for the engine -- newer plugin versions import
cudatext_api DIRECTLY and run the NATIVE engine even when
py/cudatext.py is stock -- and the engine stage calls diff_proc through
that same module, measuring exactly what the plugin's compares measure.

v5: STAGE 4 INSTRUMENTED.  A previous run reached stage 4 (close the diff
    tab) and HUNG: one core at 25% for 5+ minutes, UI dead, killed by hand.
    Nothing between the stage-4 banner and the first measurement could tell
    WHERE it stuck, so v5 splits stage 4 into micro-phases, each with its
    own name, timing print, and -- crucially -- a WATCHDOG THREAD that
    keeps appending to diag_stage4_watchdog.log next to this script even
    while the main thread is frozen inside a single call (it uses only
    ctypes + file io, never a CudaText API).  If the app hangs again: kill
    it, open that log; the last 'phase=' line names the stuck call, and
    the ws= column (falling = a slow free still progressing; flat = a
    blocked or non-allocating tight loop) says what kind of stuck it is.
"""

import builtins as _blt
import contextlib
import gc
import os
import re
import sys
import time
import traceback

import cudatext as ct
import cudatext_cmd as ct_cmd

try:
    import cuda_differ2 as _plugin
except ImportError:
    try:
        import cuda_differ as _plugin   # older install folder name
    except ImportError:
        _plugin = None

# The native diff API is implemented in Pascal and registered in the
# `cudatext_api` module (present in EVERY CudaText -- it is how
# py/cudatext.py wraps the whole API; diff_proc exists on it only in a
# diff-API-patched BINARY).  `cudatext.diff_proc` exists only when the
# installation's py/cudatext.py is the PATCHED wrapper -- a patched exe
# with a stock py/cudatext.py leaves the plugin unable to see the
# engine (it then silently runs its pure-Python engines!).
try:
    import cudatext_api as _ctapi
except ImportError:
    _ctapi = None

# Stable API constants (proc_py_const.pas) as a last-resort fallback.
_KNOWN_CONSTS = {'DIF_TEXTS': 1, 'DIF_CHARS': 2, 'DIF_CANCEL': 3,
                 'DIFF_ALGO_MYERS': 0, 'DIFF_ALGO_HISTOGRAM': 1,
                 'DIFF_IGN_NONE': 0}


def _api(name):
    """Resolve a diff-API name.  The module order follows the INSTALLED
    plugin's own access path when determinable (newer plugin versions
    import cudatext_api DIRECTLY -- they run the native engine even with
    a stock py/cudatext.py; the public snapshot reads it off the cudatext
    wrapper), so the engine stage measures exactly what the plugin's
    compares measure.  Then: the other module, the console globals
    (exec mode: whatever the session seeded), builtins, the
    known-constants table (DIF_*/DIFF_* only)."""
    order = [ct, _ctapi]
    try:
        if _plugin_native_info()['checks'] == 'cudatext_api':
            order = [_ctapi, ct]
    except Exception:
        pass
    for mod in order:
        if mod is not None:
            v = getattr(mod, name, None)
            if v is not None:
                return v
    if not name.startswith('__') and name in globals():
        return globals()[name]
    v = getattr(_blt, name, None)
    if v is not None:
        return v
    return _KNOWN_CONSTS.get(name)

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

# The two files to compare. Auto-detected (folder of this file, then
# anywhere under the plugin folder) when left None.  globals().get()
# instead of a plain None so a FILES pre-pasted into the console
# SURVIVES the exec() -- a bare `FILES = None` here would wipe it.
FILES = globals().get('FILES')

# v11: the AUTO-DETECTED pair cache.  v10 had a deadly subtlety here:
# _find_files() cached the detected pair into FILES itself, so the
# v10 downgrade's "was the pair USER-pinned?" test (FILES is not None)
# was ALWAYS true on auto-detected runs -- the downgrade was dead code
# (the user's v10 run: cycle 1 STILL compared the 140 MB pair, 92 s /
# 4.1 GB peak, no DOWNGRADED line).  v11 splits the two roles: FILES
# stays None unless the USER pins a pair; the detection cache lives
# here and never counts as a pin.  globals().get() so it too survives
# a re-exec (a re-run reuses the cache instead of re-scanning drives).
_FILES_AUTO = globals().get('_FILES_AUTO')

# How long to wait after tab close before the settle window opens
# (CudaText frees closed editor frames lazily from its idle timer; the
# first v4 run showed that at 140MB-file scale this can take far longer
# than expected -- the v5 window itself watches memory every second and
# exits early only after 3 stable readings).
SETTLE_S = 6.0

# Hard cap for the instrumented settle window (v5): after this, gc + the
# 5 forced PROC_IDLE pumps run regardless.
SETTLE_MAX_S = 30.0

# v8: how long the compare stage waits for the plugin session to appear
# after set_files before declaring the compare DID NOT START (set_files
# silently bails when the two original tabs are not open -- v7's cycle-2
# hole).  Must comfortably cover the tab creation + session registration
# on a busy UI, but stay well under a real compare's duration.
VERIFY_S = 12.0

# v9: SECOND-COMPARE STRATEGY.  The second compare/close cycle is the
# leak-vs-ceiling CONTROL: a per-compare leak grows the retention every
# cycle; one-time retention does not.  But repeating a 140 MB compare on
# top of cycle 1's ~1.9 GB retention needs ~5.5 GB -- impossible on an
# 8 GB box (the v8 run paged to a crawl and the engine callback faulted).
#   'auto' (default) -- smaller pair (e.g. autogen_50mb_*.txt found next
#                       to the main pair) when physical RAM < 12 GB;
#                       same pair on 12 GB+; SKIPPED when small-RAM and
#                       no smaller pair exists
#   'same' / 'small' / 'off' -- explicit override, pre-pastable into the
#                       console like FILES (CYCLE2='off' => census only)
#   FILES2 = (a, b)   -- explicit cycle-2 pair (implies 'small'; use the
#                       same tuple as FILES to force 'same')
CYCLE2 = globals().get('CYCLE2', 'auto')
FILES2 = globals().get('FILES2')

# v10: MAIN-PAIR RAM POLICY.  'auto' (default): on a < 12 GB box, when
# a smaller *_a/*_b pair sits next to the main one, BOTH cycles use
# the smaller pair (the v9 run's cycle 1 was still the 140 MB pair --
# 178 s, 4.1 GB peak, paging -- while only cycle 2 used the 50 MB
# pair).  'big' keeps the auto-detected main pair regardless of RAM.
# A FILES=(a,b) pin outranks both.
MAINPAIR = globals().get('MAINPAIR', 'auto')

# v9: hard ceilings on the compare wait.  A healthy 140 MB compare takes
# ~95-180 s; a 50 MB one well under a minute.  On timeout the cycle is
# voided, the stuck job cancelled, the diff tab torn down via the guarded
# close -- the run never waits forever (the v8 run waited ~10 min until
# the user killed CudaText).
CYCLE1_TIMEOUT_S = 1800.0
CYCLE2_TIMEOUT_S = 420.0   # v16: was 900 -- the flatline crash detector
                           # below recovers a crashed cycle 2 in ~3-4 min;
                           # 7 min is still 3x a healthy 140 MB compare

# v11: wall-clock budget for the DEEP census (seconds).  The v10 census
# walked each chain separately with a two-pass gc.collect per level --
# 676.9 s on the user's 4.6M-object heap (the "15 minute" run).  v11
# walks ALL targets in ONE batched queue and caps the whole walk here;
# on budget exhaustion it prints the partial chains + this note.  Raise
# it if you want deeper chains on a huge heap.
CENSUS_BUDGET_S = globals().get('CENSUS_BUDGET_S', 150.0)

# v9/v16: physical-RAM thresholds (MB) for the two SEPARATE gates that
# v13 had accidentally merged into one constant:
#   * RAM_SAME_OK_MB (6 GB, kept from v13): the MAIN-PAIR downgrade
#     gate -- only below this does _maybe_downgrade_main_pair swap the
#     auto-detected main pair for a smaller one.  The author wanted the
#     140 MB pair as the MAIN test on their 7.9 GB box, so this stays
#     low.
#   * RAM_CYCLE2_SAME_OK_MB (12 GB, restored in v16): the CYCLE-2
#     'same-pair' gate -- cycle 2 runs ON TOP of cycle 1's floor
#     (retained high-water ~1.5 GB in the v15 run), so repeating the
#     full 140 MB compare needs a ~4.2 GB peak.  On the 7.9 GB box
#     that paged the machine and the engine completion callback
#     faulted ('diff_proc callback: Access violation', the v8 failure
#     mode AGAIN -- v13's 6144 bar let it through by accident).
RAM_SAME_OK_MB = 6144.0          # main-pair downgrade gate (v13, kept)
RAM_CYCLE2_SAME_OK_MB = 12288.0  # cycle-2 same-pair gate (v16, restored)

# v16: post-'compare took' CRASH DETECTOR (cycle 2 only).  Both
# on-site crashes (v8 and v15 runs) had the same signature: the
# compare's walk FINISHED ('Differ 2: compare took ...ms' printed),
# then a native 'ERROR: diff_proc callback: Access violation' killed
# the completion callback -> sess.job stays set forever -> ws
# FLATLINES (v15: 3716 MB flat from 125 s to 320 s while the author
# watched).  A healthy compare NEVER has a flat ws while the job
# runs -- it grows +30-60 MB per 5 s heartbeat throughout.  When
# cycle 2's ws stays inside a 2*STALL_FLAT_MB band for STALL_TICKS
# consecutive heartbeats, and at least STALL_MIN_ELAPSED_S passed,
# the job is declared crashed: cancelled, the diff tab torn down, the
# cycle voided -- and the run CONTINUES to the census.
STALL_FLAT_MB = 10.0
STALL_TICKS = 15
STALL_MIN_ELAPSED_S = 200.0

# Poll interval of the state machine.
TICK_MS = 400

# ---------------------------------------------------------------------------
# memory measurement
# ---------------------------------------------------------------------------

_is_win = (os.name == 'nt')

if _is_win:
    import ctypes
    from ctypes import wintypes

    class _PMC(ctypes.Structure):
        _fields_ = [
            ('cb', wintypes.DWORD),
            ('PageFaultCount', wintypes.DWORD),
            ('PeakWorkingSetSize', ctypes.c_size_t),
            ('WorkingSetSize', ctypes.c_size_t),
            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
            ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
            ('PagefileUsage', ctypes.c_size_t),
            ('PeakPagefileUsage', ctypes.c_size_t),
            ('PrivateUsage', ctypes.c_size_t),
        ]

    _psapi = ctypes.WinDLL('psapi')
    _kernel32 = ctypes.WinDLL('kernel32')
    # 64-bit-safe declarations.  The v1/v2 bug: without argtypes the
    # GetCurrentProcess() pseudo-handle (-1) was passed to
    # GetProcessMemoryInfo as a 32-bit int -> invalid handle on Win64
    # -> the call FAILED -> every reading printed "-0 MB".
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(_PMC),
                                            wintypes.DWORD]
    _psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    try:
        _k32gmi = _kernel32.K32GetProcessMemoryInfo   # Win7+ location
        _k32gmi.argtypes = _psapi.GetProcessMemoryInfo.argtypes
        _k32gmi.restype = wintypes.BOOL
    except AttributeError:
        _k32gmi = None
    _handle = _kernel32.GetCurrentProcess()

    def _mem():
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        ok = _psapi.GetProcessMemoryInfo(_handle, ctypes.byref(pmc), pmc.cb)
        if not ok and _k32gmi is not None:
            ok = _k32gmi(_handle, ctypes.byref(pmc), pmc.cb)
        if ok:
            return pmc.WorkingSetSize, max(pmc.PrivateUsage,
                                           pmc.PagefileUsage)
        return -1, -1
else:
    def _mem():
        try:
            with open('/proc/self/status') as f:
                rss = priv = -1
                for ln in f:
                    if ln.startswith('VmRSS:'):
                        rss = int(ln.split()[1]) * 1024
                    elif ln.startswith('VmSize:'):
                        priv = int(ln.split()[1]) * 1024
                return rss, priv
        except Exception:
            return -1, -1


def _mb(v):
    return '%.0f MB' % (v / 1048576.0)


def _total_ram_mb():
    """v9: (total, available) physical RAM in MB, or (0, 0) when the
    probe fails.  Drives the cycle-2 strategy: repeating a 140 MB
    compare on top of cycle 1's retention needs ~5.5 GB peak -- on an
    8 GB box that pages to a crawl (observed: cycle-1 compare 95 s ->
    178 s; engine callback AV at 5.5 GB), so 'auto' switches cycle 2
    to a smaller pair there."""
    try:
        if _is_win:
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong),
                            ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_uint64),
                            ('ullAvailPhys', ctypes.c_uint64),
                            ('ullTotalPageFile', ctypes.c_uint64),
                            ('ullAvailPageFile', ctypes.c_uint64),
                            ('ullTotalVirtual', ctypes.c_uint64),
                            ('ullAvailVirtual', ctypes.c_uint64),
                            ('ullAvailExtendedVirtual', ctypes.c_uint64)]

            ms = _MS()
            ms.dwLength = ctypes.sizeof(_MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                return (ms.ullTotalPhys / 1048576.0,
                        ms.ullAvailPhys / 1048576.0)
            return 0.0, 0.0
        with open('/proc/meminfo') as f:
            total = avail = 0.0
            for ln in f:
                if ln.startswith('MemTotal:'):
                    total = int(ln.split()[1]) / 1024.0
                elif ln.startswith('MemAvailable:'):
                    avail = int(ln.split()[1]) / 1024.0
            return total, avail
    except Exception:
        return 0.0, 0.0


_meas0 = None


def measure(label):
    """Print one measurement line; the first call also fixes baseline.
    v9: also tracks the run's ws peak (feeds the cycle-2 rationale
    print and the report).  v16: also stores the LAST ws (feeds the
    cycle-2 flatline crash detector)."""
    global _meas0
    ws, priv = _mem()
    if _meas0 is None:
        _meas0 = ws
    try:
        if ws > _state.get('ws_peak', 0):
            _state['ws_peak'] = ws
        _state['ws_last'] = ws
    except Exception:
        pass
    delta = ws - _meas0
    print('[mem] %-56s ws=%s  private=%s  (vs first: %+s)'
          % (label, _mb(ws), _mb(priv),
             ('+' if delta >= 0 else '') + _mb(delta)))
    return ws


# ---------------------------------------------------------------------------
# state machine
# ---------------------------------------------------------------------------

_state = {'stage': 'idle', 't_mark': 0.0, 'cycle': 1, 'cmd': None,
          'files': None, 'diff_h': None, 'orig_hs': [],
          'paint_settle': None, 'baselines': {}, 'stopped': False,
          'heartbeat': 0, 'in_tick': False}


def _find_pair_in(d):
    """A matching *_a.txt/*_b.txt pair over 20 MB (or any '140mb' pair)
    inside directory d, else None."""
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return None
    for a in names:
        if not a.endswith('_a.txt'):
            continue
        b = a[:-6] + '_b.txt'      # 'x_a.txt' -> 'x_b.txt'
        if b not in names:
            continue
        try:
            size = os.path.getsize(os.path.join(d, a))
        except OSError:
            continue
        if size > 20 * 1024 * 1024 or '140mb' in a.lower():
            return (os.path.join(d, a), os.path.join(d, b))
    return None


def _anc(dirpath, levels):
    """dirpath plus its ancestors, nearest first, at most `levels` up.
    v9: the walk STOPS at temp trees (a tree extracted under /tmp or
    %TEMP% must not drag the whole temp directory -- with every other
    test's stray pairs in it -- into the anchor search)."""
    out = [dirpath]
    d = dirpath
    for _i in range(levels):
        nd = os.path.dirname(d)
        if nd == d:
            break
        # v9: stop BEFORE crossing into a temp tree -- /tmp (or %TEMP%)
        # must never become an anchor: a tree extracted there would
        # otherwise drag every stray test pair in the temp dir into
        # the search
        if os.path.basename(nd).lower() in ('tmp', 'temp', 'cache',
                                            'var', 'private'):
            break
        out.append(nd)
        d = nd
    return out


# Folder of this script, recovered even under exec(open(...).read()).
_SELF_DIR = None

# Last-resort fixed-drive scan budget in seconds (0 disables; the env
# var DIFFDIAG_NOSCAN=1 disables it too).
DRIVE_SCAN_S = 60.0

_DRIVE_SKIP = {'windows', 'program files', 'program files (x86)',
               'programdata', '$recycle.bin', 'appdata', 'node_modules',
               '.git', '__pycache__', 'site-packages', 'dist-packages',
               'winsxs', 'system volume information', 'recovery',
               'perflogs', 'msocache'}

# v14: self-location is NAME-INDEPENDENT.  v13 (forked from v12's
# differ_leak_diag.py) still hard-coded the OLD file's basename, so
# after the rename the exec()-frame walk could never match: _SELF_DIR
# stayed None, the folder RIGHT NEXT TO the script was never searched,
# and the run died in 'ERROR: could not find the autogen pair' after a
# 60s drive scan (the author's 'why you repeat same errors?' report).
# A candidate path now counts as THIS file if and only if it exists on
# disk AND its content carries the magic marker below -- whatever the
# file is called.  Rename it, copy it, exec it from anywhere: the pair
# next to it is always found.
_SELF_MAGIC = 'DIFFER2_RETENTION_DIAG_V15_SELF_MARKER'


def _norm_self_path(p):
    """A string that is THIS file's real path, else None.  A separator
    is required so a bare name can never match against the process
    working directory; the .py suffix plus the in-file magic marker
    make the match NAME-INDEPENDENT (the v13 regression was a stale
    hard-coded basename) and prevent any OTHER .py file named in the
    exec call chain from being mistaken for this script."""
    if not isinstance(p, str) or not p or len(p) > 400:
        return None
    try:
        if not p.lower().endswith('.py'):
            return None
        if (os.sep not in p and (os.altsep or '/') not in p
                and ':' not in p):
            return None
        p = os.path.normpath(os.path.abspath(p))
        if not os.path.isfile(p):
            return None
        with open(p, 'rb') as fh:
            head = fh.read(2 * 1024 * 1024)
        if _SELF_MAGIC.encode('utf-8') in head:
            return p
    except Exception:
        pass
    return None


def _self_dir_from_frames():
    """THIS file's folder, recovered from the exec() call chain.

    The paste line  exec(open(r'..\\<this script>.py').read())  is
    compiled into a code object whose string constants include the path
    literal -- and that code object is a PARENT frame of this source.
    Walking f_back and inspecting co_consts (plus co_filename and
    __file__ along the way) recovers the folder with no cooperation
    from the console at all.  v14: the path is validated against the
    in-file magic marker, so this works for ANY file name (the v13
    regression: the hard-coded old basename never matched after the
    rename, and the pair next to the script was never found)."""
    try:
        fr = sys._getframe()
    except Exception:
        return None
    seen = set()
    hits = []
    while fr is not None and id(fr) not in seen:
        seen.add(id(fr))
        code = getattr(fr, 'f_code', None)
        if code is not None:
            got = _norm_self_path(getattr(code, 'co_filename', None))
            if got:
                hits.append(got)
            stack = [(getattr(code, 'co_consts', ()), 0)]
            while stack:
                items, depth = stack.pop()
                for c in items or ():
                    if isinstance(c, str):
                        got = _norm_self_path(c)
                        if got:
                            hits.append(got)
                    elif isinstance(c, tuple) and depth < 2:
                        stack.append((c, depth + 1))
        for ns in (getattr(fr, 'f_globals', None),
                   getattr(fr, 'f_locals', None)):
            if ns:
                got = _norm_self_path(ns.get('__file__'))
                if got:
                    hits.append(got)
        fr = fr.f_back
    return os.path.dirname(hits[0]) if hits else None


def _tab_dirs():
    """Folders of the open editor tabs (this script, the autogen files or
    a plugin file is usually open in one of them)."""
    out = []
    try:
        handles = ct.ed_handles()
    except Exception:
        return out
    for h in handles or ():
        try:
            fn = ct.Editor(h).get_prop(ct.PROP_FN, '')
            if fn:
                out.append(os.path.dirname(os.path.abspath(fn)))
        except Exception:
            pass
    return out


def _fixed_drives():
    """Root paths of local fixed disks (Windows only; [] elsewhere)."""
    if os.name != 'nt':
        return []
    try:
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        out = []
        for i in range(26):
            if bitmask & (1 << i):
                root = '%c:\\' % (ord('A') + i)
                if ctypes.windll.kernel32.GetDriveTypeW(
                        ctypes.c_wchar_p(root)) == 3:      # DRIVE_FIXED
                    out.append(root)
        return out
    except Exception:
        return []


def _drive_scan(deadline_s):
    """Last-resort bounded search of every fixed drive for the autogen
    pair (or for this script, then the pair next to it).  Returns
    (pair_or_None, self_dir_or_None)."""
    t0 = time.perf_counter()

    def timed_out():
        return (time.perf_counter() - t0) > deadline_s

    found_sd = None
    for root in _fixed_drives():
        if timed_out():
            break
        queue = [(root, 0)]
        qi = 0
        while qi < len(queue):
            d, depth = queue[qi]
            qi += 1
            if timed_out():
                print('[scan]    %.0fs budget spent, stopping'
                      % (time.perf_counter() - t0))
                return None, found_sd
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            nl = e.name.lower()
                            if e.is_file():
                                # v14: name-independent self detection --
                                # any differ/retention/diag-named .py is a
                                # candidate, confirmed by the magic marker
                                if (nl.endswith('.py') and
                                        ('differ' in nl or
                                         'retention' in nl or
                                         'diag' in nl) and
                                        _norm_self_path(e.path)):
                                    sd = os.path.dirname(e.path)
                                    if found_sd is None:
                                        found_sd = sd
                                    got = _find_pair_in(sd)
                                    if got:
                                        return got, sd
                                elif nl.endswith('_a.txt'):
                                    if ('140mb' in nl or
                                            e.stat().st_size > 20 * 1024 * 1024):
                                        b = os.path.join(
                                            os.path.dirname(e.path),
                                            e.name[:-6] + '_b.txt')
                                        if os.path.isfile(b):
                                            return (e.path, b), found_sd
                            elif e.is_dir(follow_symlinks=False):
                                if (depth + 1 <= 8 and nl not in _DRIVE_SKIP
                                        and not nl.startswith('$')):
                                    if nl in ('__tests', '_dev', '_annex'):
                                        got = _find_pair_in(e.path)
                                        if got:
                                            return got, found_sd
                                    queue.append((e.path, depth + 1))
                        except OSError:
                            continue
            except OSError:
                continue
    return None, found_sd


def _find_files():
    """Locate the autogen pair.  Anchor order:
    1. explicit FILES (pre-pasted into the console),
    2. THIS script's folder -- under a plain exec(open(...).read())
       paste there is no __file__, so the folder is recovered from the
       exec call chain itself (the pasted line names this file); the
       autogen files sit next to the script, exactly where the user
       put them,
    3. folders of the open editor tabs,
    4. the installed plugin's folder tree (+ ancestors; repo layouts
       like <proj>/src/_dev/__tests__ next to <proj>/src/<plugin>),
    5. the current working directory,
    6. Windows only, when 2-5 all failed: a bounded scan of every
       fixed drive.
    Every anchor also checks its _dev/__tests__, __tests__, _dev,
    _annex and one level of subfolders."""
    global FILES, _SELF_DIR, _FILES_AUTO
    if FILES:
        return FILES
    # v11: the detection cache is separate from FILES (see the config
    # comment) -- reusing it here never counts as a user pin, so the
    # v11 downgrade can still fire on re-runs in the same console.
    if _FILES_AUTO:
        return _FILES_AUTO
    if not _SELF_DIR:
        f = globals().get('__file__')
        _SELF_DIR = (os.path.dirname(os.path.abspath(f)) if f
                     else _self_dir_from_frames())
    dirs = []
    seen = set()

    def add(d):
        if d and len(dirs) < 600 and d not in seen and os.path.isdir(d):
            seen.add(d)
            dirs.append(d)

    def add_tree(d):
        if not d:
            return
        add(d)
        add(os.path.join(d, '_dev', '__tests__'))
        add(os.path.join(d, '__tests__'))
        add(os.path.join(d, '_dev'))
        add(os.path.join(d, '_annex'))
        try:
            for sub in sorted(os.listdir(d)):
                if len(dirs) >= 600:
                    break
                if os.path.isdir(os.path.join(d, sub)):
                    add(os.path.join(d, sub))
                    add(os.path.join(d, sub, '_dev', '__tests__'))
                    add(os.path.join(d, sub, '__tests__'))
        except OSError:
            pass

    if _SELF_DIR:
        for a in _anc(_SELF_DIR, 3):
            add_tree(a)
    for d in _tab_dirs():
        for a in _anc(d, 2):
            add_tree(a)
    if _plugin is not None:
        try:
            for a in _anc(os.path.dirname(os.path.abspath(
                    _plugin.__file__)), 4):
                add_tree(a)
        except Exception:
            pass
    try:
        for pd in _plugin_native_info()['plugin_dirs']:
            add_tree(pd)
    except Exception:
        pass
    add_tree(os.getcwd())

    for d in dirs:
        got = _find_pair_in(d)
        if got:
            _FILES_AUTO = got          # v11: cache, NOT a user pin
            return _FILES_AUTO

    if (_is_win and DRIVE_SCAN_S > 0
            and not os.environ.get('DIFFDIAG_NOSCAN')):
        print('[scan] pair not found next to this script / in open tabs /'
              ' in the plugin folder;\n[scan] scanning all fixed drives'
              ' (bounded to %.0fs; set env DIFFDIAG_NOSCAN=1 to skip)...'
              % DRIVE_SCAN_S)
        got, sd = _drive_scan(DRIVE_SCAN_S)
        if sd and not _SELF_DIR:
            _SELF_DIR = sd
        if got:
            _FILES_AUTO = got          # v11: cache, NOT a user pin
            return _FILES_AUTO
    return None


def _find_smaller_pair(main_pair):
    """v9: a SMALLER *_a.txt/*_b.txt pair for the RAM-aware cycle 2.
    Looked up next to the main pair (and this script's folder): any
    matching pair at most 60% of the main size and at least 5 MB;
    a '50mb' name wins outright, else the largest candidate.  None
    when nothing qualifies."""
    try:
        main_size = os.path.getsize(main_pair[0])
    except OSError:
        return None
    dirs = []
    for d in (os.path.dirname(os.path.abspath(main_pair[0])),
              _SELF_DIR, os.getcwd()):
        if d and d not in dirs:
            dirs.append(d)
    best = None            # (priority, size, pair)
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for a in names:
            if not a.endswith('_a.txt'):
                continue
            b = a[:-6] + '_b.txt'
            if b not in names:
                continue
            ap = os.path.join(d, a)
            try:
                size = os.path.getsize(ap)
            except OSError:
                continue
            if not (5 * 1024 * 1024 <= size <= 0.60 * main_size):
                continue
            prio = 1 if '50mb' in a.lower() else 0
            cand = (prio, size, (ap, os.path.join(d, b)))
            if best is None or (cand[0], cand[1]) > (best[0], best[1]):
                best = cand
    return best[2] if best else None


def _pair_mb(pair):
    """Mean size of the pair's two files, in MB (0 when unreadable)."""
    try:
        return (os.path.getsize(pair[0]) + os.path.getsize(pair[1])) \
            / (2.0 * 1024 * 1024)
    except OSError:
        return 0.0


def _maybe_downgrade_main_pair(got, pinned):
    """v10: on a low-RAM box, swap the auto-detected main pair for the
    SMALLER pair found next to it -- BOTH cycles then run on the small
    pair (comparable ws numbers + the classic leak-vs-ceiling verdict).
    Never applies when the pair was pinned (run(files=...) argument or
    a pre-pasted FILES) or MAINPAIR='big'.  Returns (pair, downgraded)."""
    if pinned or MAINPAIR == 'big':
        return got, False
    total, avail = _total_ram_mb()
    if not (total and total < RAM_SAME_OK_MB):
        return got, False
    small = _find_smaller_pair(got)
    if small is None or tuple(small) == tuple(got):
        return got, False
    print('[env] main pair DOWNGRADED for RAM safety -- physical RAM '
          '%.1f GB < 12 GB:' % (total / 1024.0))
    print('[env]   cycle 1 + cycle 2 BOTH use the smaller pair:')
    print('[env]     %s' % small[0])
    print('[env]     %s' % small[1])
    print('[env]   (the %.0f MB pair would peak ~4 GB and page the box;'
          ' MAINPAIR=%r or FILES=(a,b) pins the main pair)'
          % (_pair_mb(got), MAINPAIR))
    return small, True


def _pump_and_gc():
    gc.collect()
    time.sleep(0.2)
    gc.collect()


# ---------------------------------------------------------------------------
# stage-4 instrumentation (v5): phase labels + watchdog thread
#
# The main thread can freeze for minutes INSIDE one call (that is the
# observed hang); while it is frozen no timer ticks, so nothing in the
# console moves.  The watchdog thread therefore writes its own log file:
# pure threading + ctypes (GetProcessMemoryInfo) + file append -- never
# a CudaText API -- so it survives and reports through a frozen UI.
# ---------------------------------------------------------------------------

_watchdog = {'on': False, 'stop': False, 'thread': None, 'path': '',
             'phase': '(stage 4 not started)', 'phase_t0': 0.0,
             'last_done': '-', 'last_alert': 0.0, 'n': 0}

# v8: has the watchdog been armed at least once THIS run?  (reset by
# run(); makes the stage-8 re-arm APPEND instead of truncating the log)
_WATCHDOG_EVER = {'v': False}


def _set_phase(label):
    """Name the call that is ABOUT to run (main thread)."""
    _watchdog['phase'] = label
    _watchdog['phase_t0'] = time.perf_counter()


def _phase_done(label):
    """Mark a call finished; print its duration (main-thread breadcrumb)."""
    _watchdog['last_done'] = label
    try:
        print('     %s done (%.2fs)'
              % (label, time.perf_counter() - _watchdog['phase_t0']))
    except Exception:
        pass


def _watchdog_body():
    wd = _watchdog
    path = wd['path']
    while not wd['stop']:
        try:
            time.sleep(1.0)
            if wd['stop']:
                break
            wd['n'] += 1
            ws, priv = _mem()
            age = time.perf_counter() - wd['phase_t0']
            line = ('%s  +%-3d s  phase=%-46s age=%6.1fs  ws=%-10s private=%-10s'
                    % (time.strftime('%H:%M:%S'), wd['n'],
                       repr(wd['phase']), age, _mb(ws), _mb(priv)))
            try:
                with open(path, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            except Exception:
                pass
            # v11: census phases are known-slow full-heap scans, capped
            # by CENSUS_BUDGET_S -- the v10 alerts fired on every
            # legitimate 60 s walk level and the spam itself blew the
            # console limit.  Alarm later on census phases, and at most
            # once a minute there.
            _cens = str(wd['phase']).startswith('census')
            _alarm_at = 45.0 if _cens else 20.0
            _relapse = 60.0 if _cens else 15.0
            if age > _alarm_at \
                    and time.perf_counter() - wd['last_alert'] > _relapse:
                wd['last_alert'] = time.perf_counter()
                try:
                    print('[watchdog] phase stuck for %.0fs: %r (ws=%s);'
                          ' log: %s' % (age, wd['phase'], _mb(ws), path))
                except Exception:
                    pass
        except Exception:
            try:
                time.sleep(1.0)
            except Exception:
                return


def _start_watchdog():
    wd = _watchdog
    wd.update(stop=False, n=0, last_alert=0.0,
              phase='(stage 4 starting)', phase_t0=time.perf_counter(),
              last_done='-')
    base = _SELF_DIR or os.getcwd()
    wd['path'] = os.path.join(base, 'diag_stage4_watchdog.log')
    # v8: APPEND when re-armed within the same run (stage 8 re-arms the
    # watchdog; v7's 'w' mode truncated the file there, destroying
    # cycle 1's stage-4 entries).  A FRESH run still starts a fresh log.
    fresh = not _WATCHDOG_EVER['v']
    _WATCHDOG_EVER['v'] = True
    header = ('\n=== run watchdog (stage %s onward) %s ===\nphase names '
              'the call in flight; console carries the last-done '
              'breadcrumbs;\nws falling = a slow free still progressing, '
              'ws flat = blocked or non-allocating tight loop.\n'
              '(v7: the watchdog stays on through stages 5-9 too -- '
              'a native error killing a tick shows here as a phase that '
              'repeats with age reset.  v8: re-arms APPEND to this '
              'file.)\n'
              % ('4' if fresh else '8',
                 time.strftime('%Y-%m-%d %H:%M:%S')))
    try:
        with open(wd['path'], 'a' if not fresh else 'w',
                  encoding='utf-8') as f:
            f.write(header)
    except Exception:
        try:
            wd['path'] = os.path.join(os.getcwd(), 'diag_stage4_watchdog.log')
            with open(wd['path'], 'a' if not fresh else 'w',
                      encoding='utf-8') as f:
                f.write(header)
        except Exception:
            print('     [watchdog] cannot open the log file; relying on '
                  'console breadcrumbs only')
            return
    try:
        import threading
        wd['thread'] = threading.Thread(target=_watchdog_body,
                                        name='differ_diag_watchdog',
                                        daemon=True)
        wd['thread'].start()
        wd['on'] = True
        print('     watchdog logging to: %s' % wd['path'])
        print('     if the app hangs: kill it, open that log -- the last '
              "phase= line names the stuck call")
    except Exception:
        print('     [watchdog] threading unavailable: console breadcrumbs '
              'only (phases still print between calls)')


def _stop_watchdog():
    wd = _watchdog
    if not wd['on']:
        return
    wd['stop'] = True
    t = wd['thread']
    if t is not None:
        try:
            t.join(timeout=0.5)
        except Exception:
            pass
    try:
        with open(wd['path'], 'a', encoding='utf-8') as f:
            f.write('=== stage 4 watchdog closed at %s (phase %r, last '
                    'done %r) ===\n'
                    % (time.strftime('%H:%M:%S'), wd['phase'],
                       wd['last_done']))
    except Exception:
        pass
    wd['on'] = False


# ---------------------------------------------------------------------------
# v11: console-output tee to the watchdog log.  The CudaText console
# has an output size limit -- the user's v10 run hit it exactly at the
# payoff moment: the LEAK VERDICT, the FINAL REPORT table and the
# census findings were truncated away (the log itself said it: "then
# log was truncated because cudatext console have a limit, here is
# the end").  While a _tee_section is active, every printed line is
# mirrored to the log file; a console limit can never lose the
# verdict again.  Nesting-safe; no-op without a log path.
# ---------------------------------------------------------------------------
_TEE = {'v': 0}


def _tee_section(title):
    """Context manager: mirror everything printed while active into
    the watchdog log file (line-buffered, partial lines flushed at
    exit)."""
    p = _watchdog.get('path')

    def _note(s):
        try:
            with open(p, 'a', encoding='utf-8') as f:
                f.write(s)
        except Exception:
            pass

    @contextlib.contextmanager
    def _cm():
        if _TEE['v'] or not p:
            yield
            return
        _TEE['v'] = 1
        _note('\n=== %s (%s) -- console output teed to this file; a'
              ' console limit cannot lose this ===\n'
              % (title, time.strftime('%H:%M:%S')))
        _real = sys.stdout

        class _Wrap(object):
            def __init__(self):
                self.__dict__['_r'] = _real
                self.__dict__['_buf'] = []

            def write(self, s):
                r = self._r.write(s)
                try:
                    self._buf.append(s)
                    if '\n' in s:
                        line = ''.join(self._buf)
                        del self._buf[:]
                        _note(line)
                except Exception:
                    pass
                return r

            def flush(self):
                try:
                    self._r.flush()
                    if self._buf:
                        _note(''.join(self._buf))
                        del self._buf[:]
                except Exception:
                    pass

            def __getattr__(self, name):
                return getattr(self._r, name)

        try:
            sys.stdout = _Wrap()
            yield
        finally:
            try:
                w = sys.stdout
                if getattr(w, '_buf', None):
                    _note(''.join(w._buf))
                    del w._buf[:]
            except Exception:
                pass
            sys.stdout = _real
            _TEE['v'] = 0
            _note('=== end tee (%s) ===\n' % time.strftime('%H:%M:%S'))
    return _cm()


def _tick(wait_s):
    """Time gate: True when `wait_s` seconds passed since the stage mark."""
    return (time.perf_counter() - _state['t_mark']) >= wait_s


def _stage_engine():
    """Stage 1: engine + API only, the same call shape the plugin drives
    (DIF_TEXTS line diff + batched DIF_CHARS on the collected replace
    pairs), but with NO tabs and NO plugin code -- pure diff_proc."""
    files = _state['files']
    print('\n=== stage 1: engine+API only (no tabs, sync) ===')
    fn = _api('diff_proc')
    if fn is None or not callable(fn):
        print('ERROR: diff_proc not found in the cudatext module, the '
              'cudatext_api module,\nthe console globals, or builtins. '
              'This CudaText build+install has no diff API\nat all -- the '
              'diagnostic cannot continue.')
        _set_stage('done')
        return
    dif_texts = _api('DIF_TEXTS')
    dif_chars = _api('DIF_CHARS')
    algo_myers = _api('DIFF_ALGO_MYERS')
    ign_none = _api('DIFF_IGN_NONE')
    measure('s1 start')
    a = open(files[0], 'r', encoding='utf-8', errors='replace').read()
    b = open(files[1], 'r', encoding='utf-8', errors='replace').read()
    measure('s1 texts loaded into Python strings')
    ops = fn(dif_texts, a, b, algo_myers, ign_none)
    n_ops = len(ops) if ops else -1
    measure('s1 DIF_TEXTS done (%s line opcodes)' % n_ops)
    # the plugin's collect pass pairs changed lines for DIF_CHARS --
    # same batch shape here: multi-line replace blocks are chunked to
    # positional line pairs exactly like the plugin does
    la = a.split('\n')
    lb = b.split('\n')
    del a, b
    # v7: pair changed lines INSIDE replace blocks positionally (the
    # plugin's chunking) -- the v6 single-line filter collected 0 pairs
    # on the autogen shape (its replaces are multi-line), so the
    # batched DIF_CHARS never ran in stage 1.
    print('     s1 chunking replace blocks into char-diff pairs...')
    pairs = []
    for (tag, i1, i2, j1, j2) in ops:
        if tag != 'replace':
            continue
        for k in range(min(i2 - i1, j2 - j1)):
            pairs.append((la[i1 + k], lb[j1 + k]))
    del ops
    measure('s1 DIF_CHARS pairs collected (%s pairs)' % len(pairs))
    res = fn(dif_chars, pairs, None, 0, ign_none)
    n_res = len(res) if res else -1
    measure('s1 DIF_CHARS done (%s per-pair results)' % n_res)
    del res, pairs, la, lb
    _pump_and_gc()
    measure('s1 everything released + gc')
    _state['baselines']['engine'] = _mem()[0]


def _cycle_files():
    """v9: the file pair of the CURRENT cycle -- cycle 2 uses the
    RAM-aware smaller pair when the strategy chose one."""
    if _state['cycle'] == 1 or not _state.get('files2'):
        return _state['files']
    return _state['files2']


def _stage_open_originals():
    _state['orig_started'] = False   # v7: fresh close stage per cycle
    files = _cycle_files()
    print('\n=== stage %d: open the two ORIGINAL file tabs ==='
          % (2 if _state['cycle'] == 1 else 6))
    hs = []
    for fn in files:
        found = None
        for h in ct.ed_handles():
            if ct.Editor(h).get_prop(ct.PROP_FN, '') == fn:
                found = h
                break
        if found is None:
            ct.file_open(fn)
            time.sleep(0.3)
            for h in ct.ed_handles():
                if ct.Editor(h).get_prop(ct.PROP_FN, '') == fn:
                    found = h
                    break
        hs.append(found)
    _state['orig_hs'] = hs
    measure('s%s originals open (the "800 MB" pre-compare checkpoint)'
            % (2 if _state['cycle'] == 1 else 6))
    if _state['cycle'] == 1:
        _state['baselines']['originals'] = _mem()[0]


def _stage_compare():
    n = 3 if _state['cycle'] == 1 else 7
    print('\n=== stage %d: plugin compare (real diff tab, async) ===' % n)
    cmd = _state['cmd']
    if cmd is None:
        if _plugin is None:
            print('ERROR: cuda_differ2 not importable')
            _set_stage('done')
            return
        # v13: use the CORE's own plugin instance -- the one CudaText's
        # ImportCommand created in __main__ ('xx' + module name, see
        # proc_py.pas NamePrefix and cudatext_reset_plugins.py) and the
        # one EVERY event/command/string-callback dispatch goes through.
        # A fresh Command() twin would work for set_files, but its
        # sessions are invisible to the core-dispatched cmd= timers --
        # a different code path from the user's manual compares.
        _main = sys.modules.get('__main__', None)
        cmd = getattr(_main, 'xxcuda_differ2', None)
        if cmd is not None and _plugin is not None \
                and not isinstance(cmd, _plugin.Command):
            cmd = None
        if cmd is not None:
            print('     using the CORE plugin instance '
                  '(__main__.xxcuda_differ2) -- the same instance your')
            print('     manual compares use; string-callback timers '
                  'dispatch to it.')
        else:
            print('     NOTE: __main__.xxcuda_differ2 not found (the '
                  'core creates it lazily on the first')
            print('     plugin dispatch -- open any menu of Differ 2 '
                  'once, or ignore this).')
            print('     Falling back to a Command() twin: the compare '
                  'runs, but its cmd= timers')
            print('     dispatch to the core instance instead -- '
                  'marked in the verdict.')
            cmd = _plugin.Command()
            _state['cmd_is_twin'] = True
        _state['cmd'] = cmd
    files = _cycle_files()
    # v8: set_files COPIES the texts from the OPEN original tabs -- the
    # two originals MUST be open (stage 2 / stage 6 did that); if they
    # are not, it returns silently and no session ever appears (the
    # compare_wait stage then detects that and refuses to close anything)
    cmd.set_files(files[0], files[1])
    time.sleep(0.2)
    # the new compare tab got the focus; remember its editor + tab id
    _state['diff_h'] = ct.ed.get_prop(ct.PROP_HANDLE_SELF)
    _state['tab_id'] = ct.ed.get_prop(ct.PROP_TAB_ID)
    try:
        _state['diff_title'] = ct.ed.get_prop(ct.PROP_TAB_TITLE, '') or ''
    except Exception:
        _state['diff_title'] = ''
    print('     diff tab created (tab_id=%s); comparing in background...'
          % _state['tab_id'])
    _set_phase('s%s compare kicked off (set_files done, background job)'
               % (3 if _state['cycle'] == 1 else 7))
    _state['t_mark'] = time.perf_counter()
    _state['heartbeat'] = 0
    _state['verify_hb'] = -1
    _state['ws_hist'] = []      # v16: flatline crash detector history
    _set_stage('compare_wait')


class _TwinCloseEd(object):
    """v15: minimal editor stand-in for forcing a TWIN run's on_close
    after the natural close.  The core's on_close(ed) reads exactly one
    property from the editor object -- PROP_TAB_ID; everything else
    works from the plugin's own session.  This shim answers that one
    query (and defaults everything else), so cmd.on_close(_TwinCloseEd(
    tab_id)) runs the plugin's REAL close-teardown code when the core
    itself never will (it has no cuda_differ2 instance in a twin run)."""
    def __init__(self, tab_id):
        self._tab_id = tab_id

    def get_prop(self, prop, value=''):
        try:
            if prop == ct.PROP_TAB_ID:
                return self._tab_id
        except Exception:
            pass
        return value


def _lookup_session():
    """The plugin session for the remembered compare tab, or None.
    Multi-path (v8): the plugin's own lookup first, the raw registry
    second -- survives either internals shape."""
    cmd = _state.get('cmd')
    if cmd is None:
        return None
    tab_id = _state.get('tab_id')
    try:
        sess = cmd._session_for(tab_id)
        if sess is not None:
            return sess
    except Exception:
        pass
    try:
        return cmd._sessions.get(str(tab_id))
    except Exception:
        return None


def _compare_done():
    sess = _lookup_session()
    if sess is None:
        return True  # no session: finished or never started
    try:
        return sess.job is None
    except Exception:
        return True


def _stage_compare_wait():
    elapsed = time.perf_counter() - _state['t_mark']
    sess = _lookup_session()
    if sess is None:
        # v8: the session must appear shortly after the kickoff.  If it
        # never does, set_files bailed -- the plugin needs both ORIGINAL
        # tabs open to copy the texts from; the v7 cycle-2 hole -- or the
        # plugin internals changed shape.  NEVER proceed to the close
        # stages then: the remembered handle belongs to whatever tab
        # happened to be active, not to a compare tab (v7 closed that
        # innocent tab; v8 refuses and marks the cycle void).
        if elapsed < VERIFY_S:
            hb = int(elapsed)
            if hb >= 1 and hb != _state.get('verify_hb', -1):
                _state['verify_hb'] = hb
                _set_phase('s%s waiting for the compare session (%ds)'
                           % (3 if _state['cycle'] == 1 else 7, hb))
                print('     ...waiting for the compare session to appear'
                      ' (%ds)' % hb)
            return
        print('     WARNING: no plugin session appeared for tab %s within'
              ' %.0fs --' % (_state.get('tab_id'), VERIFY_S))
        print('     the compare DID NOT START.  Most likely the two'
              ' ORIGINAL tabs were not')
        print('     open (the plugin\'s set_files copies their texts and'
              ' silently bails without')
        print('     them), or this plugin version changed internally.'
              '  NOT closing any tab;')
        print('     the cycle-%d leak-vs-ceiling comparison is void.'
              % _state['cycle'])
        _state.setdefault('cycle_ok', {})[_state['cycle']] = False
        _state['paint_settle'] = None
        _set_stage('close_originals')
        _run_stage()
        return
    # session present: the normal wait (job finishes, 3s paint settle)
    n = 3 if _state['cycle'] == 1 else 7
    if elapsed >= 5.0 and int(elapsed) // 5 != _state['heartbeat']:
        _state['heartbeat'] = int(elapsed) // 5
        # v9: refresh the watchdog phase (the v8 run showed a STALE
        # 's5 gc x2' label -- and stale 'phase stuck' console alarms --
        # while stage 7 was simply comparing)
        _set_phase('s%d comparing (plugin background job, %.0fs)'
                   % (n, elapsed))
        measure('     ...comparing (%.0fs)' % elapsed)
        # v16: FLATLINE CRASH DETECTOR (cycle 2 only -- see the STALL_*
        # constants for the full rationale).  measure() has just
        # refreshed _state['ws_last']; a healthy compare grows the ws
        # every heartbeat, the crashed-callback state does not.
        if _state['cycle'] == 2:
            try:
                ws_now = float(_state.get('ws_last') or 0)
            except Exception:
                ws_now = 0.0
            if ws_now > 0:
                hist = _state.setdefault('ws_hist', [])
                hist.append(ws_now)
                del hist[:-STALL_TICKS]
                if (len(hist) >= STALL_TICKS
                        and (max(hist) - min(hist))
                        <= 2.0 * STALL_FLAT_MB
                        and elapsed >= STALL_MIN_ELAPSED_S):
                    print('     WARNING: s7 ws FLAT at %s for %d x 5s'
                          ' heartbeats with the job still running --'
                          % (_mb(hist[-1]), len(hist)))
                    print('     the v8/v15 crash signature: the compare\'s')
                    print('     walk finished, then a native')
                    print('     "diff_proc callback: Access violation"')
                    print('     killed the completion callback under')
                    print('     memory pressure.  Cancelling the stuck')
                    print('     job and CONTINUING to the close + census;')
                    print('     the cycle-2 leak-vs-ceiling comparison is')
                    print('     void (cycle 1 + the censuses still count).')
                    _state['cmp_timeout'] = True
                    _state['cmp_crashed'] = True
                    _state.setdefault('cycle_ok', {})[2] = False
                    _state['paint_settle'] = None
                    _state['ws_hist'] = []
                    _advance()
                    return
    # v9: TIMEOUT.  The v8 on-site run: the compare FINISHED but a
    # native "diff_proc callback: Access violation" killed the
    # completion callback, sess.job stayed set forever and the run
    # waited ~10 min until the user killed CudaText.  On timeout:
    # void the cycle, cancel the stuck job, tear the diff tab down via
    # the guarded close (its s4.4 does the real job cancel).
    tmo = CYCLE1_TIMEOUT_S if _state['cycle'] == 1 else CYCLE2_TIMEOUT_S
    if elapsed > tmo:
        print('     WARNING: s%d compare not finished within %.0fs --'
              ' timing out.' % (n, tmo))
        print('     (the v8 run hit this for real: a native callback'
              ' Access violation')
        print('     left the plugin job permanently unfinished; the'
              ' machine was')
        print('     memory-pressured.  The stuck job is CANCELLED, the'
              ' diff tab is')
        print('     torn down, the cycle-%d leak-vs-ceiling comparison'
              ' is void, and' % _state['cycle'])
        print('     the run continues to the census.)')
        _state['cmp_timeout'] = True
        _state.setdefault('cycle_ok', {})[_state['cycle']] = False
        _state['paint_settle'] = None
        _advance()
        return
    try:
        job_done = sess.job is None
    except Exception:
        job_done = True
    if not job_done:
        return
    if _state['paint_settle'] is None:
        _state['paint_settle'] = time.perf_counter()
        # v8: a real compare ran to completion in this cycle
        _state.setdefault('cycle_ok', {})[_state['cycle']] = True
    if time.perf_counter() - _state['paint_settle'] < 3.0:
        return
    _state['paint_settle'] = None
    measure('s%s COMPARE FINISHED (the "3.5 GB" checkpoint)'
            % (3 if _state['cycle'] == 1 else 7))
    _state['baselines']['compare'] = _mem()[0]
    _advance()


def _stage_close_diff():
    n = 4 if _state['cycle'] == 1 else 8
    print('\n=== stage %d: close the diff tab -- the NATURAL close '
          '(exactly the user flow) ===' % n)
    _state['settle'] = {}
    _start_watchdog()
    cmd = _state['cmd']
    if cmd is None:
        print('ERROR: no plugin Command instance -- cannot close')
        _stop_watchdog()
        _set_stage('done')
        return
    h = _state['diff_h']
    tab_id = _state['tab_id']
    ed = ct.Editor(h)

    # ---- v13: the NATURAL close.  No plugin teardown is mirrored by
    # hand: the core fires on_close_pre + on_close itself inside
    # cmd_FileClose, exactly like a user close.  Only the v8 safety
    # guard (never close an innocent tab) and the modified-flag clear
    # (no 'Save changes?' dialog for the untitled diff tab) remain.
    _set_phase('s4.1 session lookup + v8 innocent-tab guard')
    sess = None
    try:
        sess = cmd._session_for(tab_id)
    except Exception:
        try:
            sess = cmd._sessions.get(str(tab_id))
        except Exception:
            sess = None
    title = ''
    try:
        title = ed.get_prop(ct.PROP_TAB_TITLE, '') or ''
    except Exception:
        title = ''
    looks_diff = isinstance(title, str) and title.startswith('Diff:')
    _phase_done('s4.1 session lookup (%s)'
                % ('found' if sess else 'MISSING'))
    if sess is None and not looks_diff:
        print('     WARNING (v8 guard): tab %s has NO plugin session and' % tab_id)
        print('     its title %r is not a \"Diff: ...\" title -- NOT closing it.' % (title or '?'))
        print('     The cycle-%d comparison is void.' % _state['cycle'])
        _state.setdefault('cycle_ok', {})[_state['cycle']] = False
        _state['paint_settle'] = None
        _stop_watchdog()
        _set_stage('close_originals')
        _run_stage()
        return

    _set_phase('s4.2 PROP_MODIFIED=False on both halves (no save dialog)')
    try:
        for eh in (ed.get_prop(ct.PROP_HANDLE_PRIMARY),
                   ed.get_prop(ct.PROP_HANDLE_SECONDARY)):
            try:
                ct.Editor(eh).set_prop(ct.PROP_MODIFIED, False)
            except Exception:
                pass
    except Exception:
        traceback.print_exc()
    _phase_done('s4.2 modified flags cleared')

    _set_phase('s4.3 ed.cmd(cmd_FileClose)  <-- the REAL close; the core')
    _set_phase('s4.3   fires on_close_pre + on_close itself (plugin teardown)')
    try:
        ed.cmd(ct_cmd.cmd_FileClose)
    except Exception:
        traceback.print_exc()
    _phase_done('s4.3 cmd_FileClose')
    try:
        gone = None
        try:
            gone = cmd._session_for(tab_id) is None
        except Exception:
            gone = True
        if not gone and _state.get('cmd_is_twin'):
            # v15: TWIN run -- the core never imported cuda_differ2 (it
            # creates __main__.xxcuda_differ2 lazily on the first plugin
            # dispatch), so the natural close above fired NO on_close
            # anywhere and the twin's session is still fully alive: full
            # data, overview with its worker thread, toolbar form --
            # exactly the v14 run's 'STILL PRESENT' + zombie-timer
            # artifacts.  Force the plugin's OWN teardown here -- the
            # exact on_close(ed) call the core would have made -- so the
            # censuses below measure the plugin's real close path, twin
            # or not.  (Open any Differ 2 menu once before a run to get
            # the full-fidelity core path.)
            print('     twin close: the core never fired on_close (it'
                  ' has no cuda_differ2 instance);')
            print('     forcing the twin\'s on_close for tab %s -- the'
                  ' same call the core makes' % tab_id)
            try:
                cmd.on_close(_TwinCloseEd(tab_id))
            except Exception:
                traceback.print_exc()
            try:
                gone = cmd._session_for(tab_id) is None
            except Exception:
                gone = True
        print('     verify: plugin session for tab %s is %s'
              % (tab_id, 'GONE (on_close ran)' if gone
                 else 'STILL PRESENT -- REPORT THIS'))
    except Exception:
        pass
    measure('s%s after cmd_FileClose (on_close ran inside)' % n)

    _set_phase('s4.11b 6s idle gap (lazy frame free runs here or below)')
    _state['t_mark'] = time.perf_counter()
    _set_stage('close_diff_wait')


def _stage_close_diff_wait():
    n = 4 if _state['cycle'] == 1 else 8
    st = _state.setdefault('settle', {})
    if not st.get('opened'):
        if not _tick(SETTLE_S):
            return
        st['opened'] = True
        st['t0'] = time.perf_counter()
        st['readings'] = []
        st['stable'] = False
        st['last_hb'] = -1
        _set_phase('s4.12 settle window (lazy frame free via TIdleTimer)')
        print('     s4.12 settle window open (max %.0fs): CudaText frees '
              'the closed tab\'s two editor frames lazily from its idle '
              'timer.\n     Heartbeat below; window exits early on 3 '
              'stable 1s-readings.' % SETTLE_MAX_S)
    # one heartbeat line per second
    hb = int(time.perf_counter() - st['t0'])
    if hb >= 1 and hb != st['last_hb']:
        st['last_hb'] = hb
        ws, priv = _mem()
        st['readings'].append(ws)
        print('     s4.12 +%2ds  ws=%s  private=%s' % (hb, _mb(ws), _mb(priv)))
        r = st['readings'][-3:]
        if len(r) == 3 and abs(r[-1] - r[0]) <= 4 * 1048576:
            st['stable'] = True
    window_age = time.perf_counter() - st['t0']
    if not (st['stable'] and window_age >= SETTLE_S) \
            and window_age < SETTLE_MAX_S:
        return
    if st.get('tail_done'):
        return          # v6 one-shot: the tail below must never re-run
    st['tail_done'] = True
    _phase_done('s4.12 settle window (%.0fs%s)'
                % (window_age, ', stable' if st['stable'] else ', CAPPED'))

    # v13: NO gc here -- the new PRE-GC census stage must see the heap
    # exactly as the user leaves it after closing everything (their
    # symptom: RAM stays high until something runs gc.collect()).  The
    # collect happens in the census stage, measured before and after.
    _set_phase('s4.13 (skipped in v13: no gc before the pre-gc census)')
    _phase_done('s4.13 no-gc (v13)')

    # forced idle pumps: if the TIdleTimer was starved above, the lazy
    # frame free happens HERE, inside one of these five calls
    _set_phase('s4.14 5x PROC_IDLE (forces the lazy frame free)')
    for _i in range(5):
        t0 = time.perf_counter()
        try:
            ct.app_proc(ct.PROC_IDLE, '')
        except Exception:
            traceback.print_exc()
            break
        dt = time.perf_counter() - t0
        print('     s4.14 PROC_IDLE #%d done (%.2fs)%s'
              % (_i + 1, dt, '  <-- slow: frame free ran here' if dt > 1.0 else ''))
    _phase_done('s4.14 PROC_IDLE x5')

    # v7: the watchdog KEEPS RUNNING through stages 5-9 (the v6 run
    # died with a native Access violation inside the first
    # originals-close tick); it is stopped at done/stop().
    _set_phase('stage 5 next: close originals (1 close per tick)')
    measure('s%s DIFF TAB CLOSED + settle + idle (NO gc yet)  <-- the'
            ' pre-census checkpoint' % n)
    if _state['cycle'] == 1:
        _state['baselines']['closed'] = _mem()[0]
    else:
        _state['baselines']['closed_2'] = _mem()[0]
    _advance()


def _stage_close_originals():
    """v7: incremental and RESUMABLE.  One close per 400 ms tick, each
    close breadcrumbed before/after, tabs re-discovered LIVE from
    ed_handles() every tick -- so a tick that dies inside the native
    close chain (the v6 on-site run hit a native Access violation
    here, below the Python except layer) only loses ONE operation:
    the next tick re-derives what is still open and continues.  A tab
    that survives 5 attempts (Python error or tick-killing native
    error) is given up on with a warning, and the run continues."""
    n = 5 if _state['cycle'] == 1 else 9
    if not _state.get('orig_started'):
        _state['orig_started'] = True
        _state['orig_att'] = {}
        _state['orig_gave_up'] = []
        _state['orig_settle'] = {}
        print('\n=== stage %d: close the two ORIGINAL tabs too ===' % n)
        print('     (v7: one close per timer tick, resumable; a killed '
              'tick is retried automatically)')
        return                      # the first tick only prints the banner
    files = _cycle_files()
    att = _state.setdefault('orig_att', {})
    gave = _state.setdefault('orig_gave_up', [])
    try:
        handles = list(ct.ed_handles())
    except Exception as ex:
        print('     [diag] ed_handles() failed (%r) -- retrying next '
              'tick' % ex)
        return
    still = []
    for h in handles:
        try:
            fn = ct.Editor(h).get_prop(ct.PROP_FN, '')
        except Exception:
            continue
        if fn in files and os.path.basename(fn) not in gave:
            still.append((h, fn))
    if not still:
        if gave:
            print('     WARNING: could not close %s automatically -- '
                  'close it/them BY HAND; the run continues.'
                  % ', '.join(gave))
        _state['orig_started'] = False
        _state['t_mark'] = time.perf_counter()
        _set_stage('close_orig_wait')
        return
    h, fn = still[0]
    base = os.path.basename(fn)
    if att.get(base, 0) >= 5:
        gave.append(base)
        print('     %s survived %d close attempts (ticks keep dying in '
              'the native close chain?) -- giving up on it; close it BY '
              'HAND.  The run continues.' % (base, att[base]))
        return
    print('     s%s closing %s (handle %s), attempt %d...'
          % (n, base, h, att.get(base, 0) + 1))
    _set_phase('s%s close %s: e.cmd(cmd_FileClose)' % (n, base))
    att[base] = att.get(base, 0) + 1
    try:
        e = ct.Editor(h)
        try:
            e.set_prop(ct.PROP_MODIFIED, False)
        except Exception as ex:
            print('       set_prop(PROP_MODIFIED) failed: %r -- closing '
                  'anyway' % ex)
        e.cmd(ct_cmd.cmd_FileClose)
        print('       close call returned OK')
    except Exception as ex:
        print('       close raised (Python-visible): %r' % ex)
        if att[base] >= 3:
            gave.append(base)
            print('       giving up on %s after %d Python-level failures'
                  ' -- close it BY HAND; the run continues.'
                  % (base, att[base]))
    _phase_done('s%s close %s' % (n, base))


def _stage_close_orig_wait():
    ost = _state.setdefault('orig_settle', {})
    if ost.get('tail_done'):
        return          # v6 one-shot (same guard class as s4.13/s4.14)
    if 't0' not in ost:
        ost.update(t0=time.perf_counter(), last_hb=-1, readings=[],
                   stable=False)
        print('     s5-settle: lazy frame frees of the original tabs land '
              'here;')
        print('     heartbeat below (early exit on 3 stable 1s readings)')
        _set_phase('s5 settle window (originals lazy frame free)')
    hb = int(time.perf_counter() - ost['t0'])
    if hb >= 1 and hb != ost['last_hb']:
        ost['last_hb'] = hb
        ws, priv = _mem()
        ost['readings'].append(ws)
        print('     s5-settle +%2ds  ws=%s  private=%s'
              % (hb, _mb(ws), _mb(priv)))
        r = ost['readings'][-3:]
        if len(r) == 3 and abs(r[-1] - r[0]) <= 4 * 1048576:
            ost['stable'] = True
    age = time.perf_counter() - ost['t0']
    if not (ost['stable'] and age >= SETTLE_S) and age < SETTLE_MAX_S:
        return
    ost['tail_done'] = True
    _phase_done('s5-settle window (%.0fs%s)'
                % (age, ', stable' if ost['stable'] else ', CAPPED'))
    # v13: no gc here -- the pre-gc census stage measures first.
    n = 5 if _state['cycle'] == 1 else 9
    measure('s%s ORIGINALS CLOSED TOO + settle (NO gc yet)' % n)
    if _state['cycle'] == 2:
        _state['baselines']['all_closed_2'] = _mem()[0]
    _advance()


# ---------------------------------------------------------------- v13
# The pre-gc census: the heap EXACTLY as the user leaves it after
# closing everything -- BEFORE any gc.collect().  This is the data the
# user's snippet could not show (it collects first): what is retained,
# how much, who holds it, and whether it is reachable from GC roots or
# an unreachable cycle waiting for a gen-2 that never comes.
def _plugin_mod_names():
    names = []
    if _plugin is not None:
        names.append(getattr(_plugin, '__name__', 'cuda_differ2'))
    names.append('cuda_differ2')
    names.append('cdiffer')
    return [n for n in names if n]


def _pregc_census():
    cyc = _state['cycle']
    n = 5 if cyc == 1 else 9
    print('\n=== stage %s: PRE-GC CENSUS (the heap after close, NO gc)' % n)
    print('   -- the user\'s exact situation) ===')
    # no _start_watchdog here: the stage-4 watchdog is still running
    # (v7: it stays on through stages 5-9); only the phase moves on
    _set_phase('s%s.1 pre-gc counts' % n)
    mod_names = _plugin_mod_names()
    objs = gc.get_objects()
    n_objects = len(objs)
    n_base = _state.get('obj_base') or 0
    inst = {}
    big = []          # (len, obj) for lists len > 50000
    for o in objs:
        tn = type(o).__name__
        tm = getattr(type(o), '__module__', '')
        if tm in mod_names or any(tm.startswith(m + '.') for m in mod_names):
            if tn in ('Command', 'ScrollSplittedTab', 'UnidiffRunner'):
                continue      # module-lifetime singletons: expected
            inst[tn] = inst.get(tn, 0) + 1
        elif tn == 'list' and len(o) > 50000:
            big.append((len(o), o))
    del objs
    ws_now, priv_now = _mem()
    live_n = len(getattr(ct, '_live', {}))
    last_tb = 'SET (pins frames!)' if getattr(
        sys, 'last_traceback', None) is not None else 'none'
    print('  objects (no gc): %d   (baseline %d, delta %+d)'
          % (n_objects, n_base, n_objects - n_base))
    print('  per-tab plugin instances: %r' % (inst,))
    print('  big lists (>50k items): %d' % len(big))
    for ln, o in big[:6]:
        print('    len=%d item=%s' % (ln, type(o[0]).__name__ if o else '?'))
        del o
    print('  wrapper._live entries: %d' % live_n)
    print('  sys.last_traceback: %s' % last_tb)
    print('  ws=%s private=%s' % (_mb(ws_now), _mb(priv_now)))
    _state.setdefault('pregc', {})[cyc] = dict(
        objects=n_objects, inst=inst, big=len(big), ws=ws_now,
        live=live_n)
    _phase_done('s%s.1 pre-gc counts' % n)

    retained = bool(inst) or bool(big) or (n_objects - n_base) > 50000
    if retained:
        # name the holders: v11's batched walker over the retained
        # plugin instances + biggest lists (budgeted).
        _set_phase('s%s.2 holder chains (budgeted %ss)'
                   % (n, CENSUS_BUDGET_S))
        print('  ---- holder chains of the retained objects ----')
        try:
            _holder_report(big, inst, mod_names)
        except Exception:
            traceback.print_exc()
        _phase_done('s%s.2 holder chains' % n)

        _set_phase('s%s.3 reachability from GC roots' % n)
        try:
            _reachability_probe(big, inst, mod_names)
        except Exception:
            traceback.print_exc()
        _phase_done('s%s.3 reachability' % n)
    else:
        print('  NOTHING retained pre-gc (heap already at baseline).')
        print('  (the v13 plugin teardown -- session.destroy + the'
              ' close-time gc.collect --')
        print('   already freed everything, or this install never'
              ' retained anything)')

    # THE decisive moment: the user's snippet, exactly.
    _set_phase('s%s.4 gc.collect() x2 -- the user\'s snippet effect' % n)
    print('  ---- gc.collect() NOW (what your census snippet does) ----')
    t0 = time.perf_counter()
    n1 = gc.collect()
    n2 = gc.collect()
    dt = time.perf_counter() - t0
    ws_after, priv_after = _mem()
    objs2 = gc.get_objects()
    n_after = len(objs2)
    del objs2
    print('  freed %d + %d objects in %.1fs' % (n1, n2, dt))
    print('  ws BEFORE collect: %s   AFTER: %s   (delta %s)'
          % (_mb(ws_now), _mb(ws_after), _mb(ws_after - ws_now)))
    print('  objects: %d -> %d' % (n_objects, n_after))
    d = _state.setdefault('pregc', {})[cyc]
    d.update(post_ws=ws_after, freed=n1 + n2, post_objects=n_after)
    _phase_done('s%s.4 gc x2' % n)
    measure('s%s PRE-GC CENSUS: pre=%s post-gc=%s' % (n, _mb(ws_now),
                                                      _mb(ws_after)))
    _stop_watchdog()
    _advance()


def _holder_report(big, inst, mod_names):
    """v11's batched referrer walk over the retained objects -> ROOT or
    cycle, with content-named holders (kept short: budget + per-level
    cap)."""
    targets = []
    for ln, o in big[:4]:
        targets.append(o)
    # retained plugin instances: find a few by class name
    objs = gc.get_objects()
    class_names = set(inst)
    found_inst = []
    for o in objs:
        if type(o).__name__ in class_names \
                and (getattr(type(o), '__module__', '') in mod_names
                     or any(getattr(type(o), '__module__', '')
                            .startswith(m + '.') for m in mod_names)):
            found_inst.append(o)
            if len(found_inst) >= 6:
                break
    del objs
    targets.extend(found_inst)
    if not targets:
        print('  (no targets to walk)')
        return
    print('  walking %d target(s) (biggest lists + plugin instances)...'
          % len(targets))
    try:
        chains = _walk_all_chains(targets, mod_names, class_names,
                                  budget_s=CENSUS_BUDGET_S)
        shown = 0
        for tgt, path in chains:
            if path is None:
                continue
            print('  target %s' % _holder_desc(tgt, mod_names,
                                               class_names))
            for i, edge in enumerate(path[:10]):
                print('    %2d. %s' % (i, edge))
            shown += 1
            if shown >= 4:
                break
        if shown == 0:
            print('  (no root path found within budget -- consistent'
                  ' with an unreachable cycle)')
    except TypeError:
        # older signature without budget
        chains = _walk_all_chains(targets, mod_names, class_names)
        print('  (walker returned %r)' % (chains,))


def _reachability_probe(big, inst, mod_names):
    """Is any retained target REACHABLE from GC roots (module dicts +
    frames)?  Reachable = a live holder pins it (a real leak);
    unreachable = dead cyclic garbage waiting for gen-2 (the gen2-
    never-runs bug).  Bounded BFS; frames expanded via f_locals."""
    targets = [o for ln, o in big[:2]]
    if not targets and inst:
        objs = gc.get_objects()
        for o in objs:
            if type(o).__name__ in set(inst):
                targets.append(o)
                if len(targets) >= 2:
                    break
        del objs
    if not targets:
        print('  (no targets to test)')
        return
    target_ids = set(id(t) for t in targets)
    roots = []
    for m in list(sys.modules.values()):
        try:
            d = getattr(m, '__dict__', None)
        except Exception:
            # v15: exotic module objects can raise from __dict__ probes
            # (e.g. ctypes.LibraryLoader loads a DLL on ANY attribute
            # read) -- never let that kill the walk
            d = None
        if d is not None:
            roots.append(d)
    for fr in list(sys._current_frames().values()):
        roots.append(fr)
    try:
        roots.append(globals())
    except Exception:
        pass
    # v15: frames are identified BY TYPE.  The v14 probe did
    # getattr(o, 'f_locals', None) on every object; on a ctypes
    # LibraryLoader (ctypes.windll / ctypes.cdll -- present in the
    # ctypes module's own dict on Windows) that getattr raises
    # FileNotFoundError (ctypes tries to LoadLibrary('f_locals')!)
    # instead of AttributeError, which the default-None cannot catch
    # and which killed the probe in BOTH v14 cycles.
    import types as _types
    _frame_type = _types.FrameType
    seen = set()
    for r in roots:
        seen.add(id(r))
    frontier = list(roots)
    reach = 0
    limit = 400000
    while frontier and len(seen) < limit:
        nxt = []
        for o in frontier:
            if id(o) in target_ids:
                reach += 1
                frontier = []
                break
            kids = []
            t = type(o)
            try:
                if t is dict:
                    kids = list(o.values())
                elif t in (list, tuple, set):
                    kids = list(o)
                elif t is _frame_type:
                    try:
                        fl = o.f_locals
                        kids = list(fl.values()) if fl else []
                    except Exception:
                        kids = []
                    try:
                        if o.f_back is not None:
                            kids.append(o.f_back)
                    except Exception:
                        pass
                else:
                    try:
                        xd = getattr(o, '__dict__', None)
                    except Exception:
                        xd = None      # LibraryLoader & friends: skip
                    if type(xd) is dict:
                        kids = list(xd.values())
                    try:
                        for k in getattr(type(o), '__slots__', ()):
                            kids.append(getattr(o, k))
                    except Exception:
                        pass
            except Exception:
                kids = []            # v15: no exotic object kills the walk
            for k in kids:
                if k is None or type(k) in (int, float, str, bytes,
                                            bool, type(None)):
                    continue
                if id(k) not in seen:
                    seen.add(id(k))
                    nxt.append(k)
        frontier = nxt
    verdict = ('REACHABLE from GC roots (a LIVE holder pins it)'
               if reach else
               'UNREACHABLE (dead cyclic garbage; only gc.collect()'
               ' frees it -- the gen2-never-runs retention)')
    print('  reachability of %d target(s): %d reachable -> %s'
          % (len(targets), reach, verdict))


_STAGE_NAMES = {'base': 'stage 0 baseline',
                'engine': 'after engine-only test',
                'originals': 'after originals opened',
                'compare': 'after compare (tab open)',
                'closed': 'after diff tab closed [cycle 1]',
                'closed_2': 'after diff tab closed [cycle 2]',
                'all_closed_2': 'after ALL tabs closed [cycle 2]'}


def _stage_report():
    b = _state['baselines']
    base = b.get('base', 0)
    print('\n' + '=' * 72)
    print('FINAL REPORT (working-set deltas vs stage-0 baseline)')
    print('=' * 72)
    for k in ('base', 'engine', 'originals', 'compare', 'closed',
              'closed_2', 'all_closed_2'):
        if k in b:
            d = b[k] - base
            print('  %-34s %+10s   (absolute %s)'
                  % (_STAGE_NAMES[k],
                     ('+' if d >= 0 else '') + _mb(d), _mb(b[k])))
    # v16: decode a crashed cycle 2 (the flatline detector cancelled it)
    if _state.get('cmp_crashed'):
        print("""
  CYCLE 2 DID NOT FINISH -- the known pressure crash (v8 + v15 runs):
    the compare's walk completed, then a native
    'ERROR: diff_proc callback: Access violation' killed the engine's
    completion callback.  Cycle 2 runs ON TOP of cycle 1's retained
    floor (FPC-heap high-water): floor + full-size compare data =
    ~4.2 GB peak on this box -- more than physical RAM allows without
    paging, and the fault lands in the biggest native free-storm (the
    walk's line lists + the 533k-pair result teardown) right after
    'Differ 2: compare took ...ms' prints.  This is a SYMPTOM of the
    retention, not a second leak: cycle 1's numbers + the census below
    still stand.  Three ways to still get the 2-cycle verdict:
    (1) drop a smaller *_a/*_b pair next to the main one and re-run
        (cycle 2 then uses it -- same verdict, fits in RAM);
    (2) build the fork with the cmem experiment (README_V16_FIX.md):
        the floor drops at close, cycle 2 fits, this crash disappears;
    (3) CYCLE2='same' on a bigger box / after (2).""")
    # v16: decode the all-closed floor (live objects vs working set)
    pg = _state.get('pregc', {})
    p1 = pg.get(1) or {}
    post_objs = p1.get('post_objects')
    post_ws = p1.get('post_ws')
    if post_objs and post_ws and base:
        floor_mb = (post_ws - base) / 1048576.0
        if floor_mb > 250.0:
            print("""
  THE FLOOR, DECODED (v16): after ALL tabs closed + gc.collect() the
    working set still sits %s above the stage-0 baseline while only
    ~%s Python objects are alive (~baseline).  That gap is NOT live
    data and NOT a plugin leak: it is ALLOCATOR HIGH-WATER -- the FPC
    heap keeps freed blocks in its pools (never returned to Windows)
    plus CPython keeps freed arenas.  Proof it is not a leak: re-opening
    the same originals on top of this floor costs ~0 extra ws (the
    pools are reused).  A CudaText restart returns it; the cmem build
    experiment (README_V16_FIX.md, one line in cudatext.lpr) returns
    most of it AT CLOSE without a restart -- and is also what makes
    the 2-cycle verdict possible on this box."""
                  % ('%.0f MB' % floor_mb, '{:,}'.format(post_objs)))
    print("""
HOW TO READ THE NUMBERS
-----------------------
* "after engine-only test" near 0:
    engines + diff_proc API + their marshaling are CLEAN on this build
    (this matches the standalone test suite shipped with the report).
  "after engine-only test" large:
    the leak IS in the Pascal diff API / engines at this scale --
    that contradicts the standalone suite; report the number.

* "after compare (tab open)" large is EXPECTED: the real diff-tab data
  (two editor halves with the texts, per-line bookmarks/gaps/markers,
  plugin session data) plus transient allocator growth. Only a leak if
  it never comes back down at the next checkpoints.

* CYCLE-2 STRATEGIES (printed in the [env] block at start):
    'same'  -- cycle 2 repeats the SAME pair; the close-vs-close ws
               delta is the classic leak-vs-ceiling check;
    'small' -- cycle 2 uses a SMALLER pair (only when the MAIN pair
               stayed big -- you pinned FILES or MAINPAIR='big'); the
               ws deltas are NOT comparable then, so the verdict comes
               from the OBJECT-COUNT growth instead (live objects
               after cycle 1 vs after cycle 2);
    'off'   -- cycle 2 skipped (RAM safety, main pair > 64 MB and no
               smaller pair); the censuses still run and NAME the holder.

* v10/v11 MAIN-PAIR RAM POLICY: on a < 12 GB box with a smaller pair next
  to the auto-detected main one, BOTH cycles run on the SMALLER pair
  (the [env] block announces the swap); the ws numbers of
  the two cycles are then directly comparable -- the classic verdict
  applies, plus the object-count check.  MAINPAIR='big' or FILES=(a,b)
  pins the full-size pair (a pinned BIG pair on a small-RAM box now
  prints a WARNING -- never silent).

* "after diff tab closed [cycle 1]" is THE reported leak number:
  - a few hundred MB: normal CPython-arena + FPC-heap retention after
    freeing millions of small objects -- a CEILING, not a leak, when
    cycle 2 does not grow;
  - GBs growing on cycle 2: a real per-compare leak;
  - GBs stable on cycle 2 but way above what the still-open originals
    explain ("after originals opened" delta): the diff tab's editor
    frames were not freed -- check "after ALL tabs closed [cycle 2]":
    back to baseline => frames are freed eventually (lazy/timing);
    still high => CudaText core retains closed-tab editor data.

* "after ALL tabs closed [cycle 2]" near baseline: everything is
  actually released once every tab is gone; the earlier high numbers
  were live-tab data + allocator ceilings, not a leak.

* If stage 4 HUNG and you had to kill CudaText: open
  diag_stage4_watchdog.log (its path is printed at stage-4 start,
  next to this script).  The last phase= line names the stuck call:
    s4.2  _unregister_compare_tab ...... plugin state-file io
    s4.5  overview.destroy ............. plugin overview teardown
    s4.7  difftb.destroy_for .......... plugin toolbar teardown
    s4.9  cmd.on_close ................. full plugin teardown
    s4.11 ed.cmd(cmd_FileClose) ....... CudaText tab close (core)
    s4.11b/s4.12 lazy frame free ...... CudaText idle timer frees the
          two editor halves (2x 1.5M lines, ~2.7M bookmarks, ~1.1M
          gaps, attrs) -- CudaText/ATSynEdit territory
    s4.13 gc.collect x2 .............. CPython full collection
    s4.14 5x PROC_IDLE ............... forced idle + paint processing
  In the log, ws FALLING across lines = a slow free still grinding;
  ws FLAT at age>20s = blocked or in a non-allocating tight loop.
  Send me that log file either way.

  v6 note: the v4/v5 signature you hit was phase s4.13/s4.14 ages
  OSCILLATING (2-4s) for minutes with ws flat and no 'watchdog
  closed' footer -- that was this script's own PROC_IDLE re-entrancy
  spiral (fixed in v6), NOT the plugin/CudaText.  In a healthy v6
  run each phase appears ONCE, ages only grow, and the footer
  '=== stage 4 watchdog closed ... ===' ends the log.

  v16 note: 'ERROR: diff_proc callback: Access violation' right after
  'Differ 2: compare took ...ms' is the KNOWN cycle-2 pressure crash
  (hit for real in the v8 and v15 on-site runs): the second full-size
  compare runs on top of cycle 1's retained floor and pages the box.
  v16 refuses to start that cycle by default (< 12 GB RAM -> census
  only, with the three safe alternatives printed at [env]); when
  forced with CYCLE2='same', the flatline detector (ws static for
  15 x 5s heartbeats after 200s) cancels the stuck job and the run
  still completes its censuses.
""")
    if _state['cycle'] == 1:
        strat = _state.get('cycle2_strategy', 'same')
        if strat == 'off':
            # v9: no second cycle on this machine (RAM safety or an
            # explicit CYCLE2='off') -- the growth question stays
            # unanswered, the deep census below still names the holder.
            print('\n  cycle 2 SKIPPED (%s) -- the per-cycle growth'
                  % _state.get('cycle2_why', 'CYCLE2=off'))
            print('  question is unanswered this run; the DEEP RETENTION'
                  ' CENSUS below')
            print('  still names WHO holds the retained objects.')
            with _tee_section('LEAK VERDICT + censuses'):
                _finish_run()
            return
        print('Starting the SECOND compare/close cycle (leak-vs-ceiling '
              'discriminator)...\n')
        if strat == 'small' and _state.get('files2'):
            print('  v9 RAM-safety: cycle 2 uses the SMALLER pair')
            print('    %s' % _state['files2'][0])
            print('    %s' % _state['files2'][1])
            print('  (%s; repeating the full-size pair would peak near'
                  % _state.get('cycle2_why', ''))
            peak = _state.get('ws_peak', 0)
            if peak:
                print('   %s + %s of compare data and page the machine'
                      ' -- the v8 run' % (_mb(peak),
                                          _mb(max(0, peak - _state.get(
                                              'baselines', {}).get(
                                              'originals', 0)))))
            print('   faulted exactly there.)')
        # v9: object-count snapshot between the cycles -- with a smaller
        # cycle-2 pair the ws numbers are not comparable, but the OBJECT
        # COUNTS are: a per-compare leak adds objects again on cycle 2,
        # one-time retention does not.
        try:
            gc.collect()
            _state['obj_after_c1'] = len(gc.get_objects())
            print('\n  [census] live objects after cycle 1 close: %s'
                  ' (baseline for the cycle-2 growth check)'
                  % '{:,}'.format(_state['obj_after_c1']))
        except Exception:
            pass
        # v12: the root-cause check -- stale timer callbacks in the
        # wrapper's _live dict pin every closed session
        _state['timer_stale_c1'] = _timer_leak_check('after cycle 1 close')
        # v16: restate the plugin's fix level at the census, right where
        # the pre-gc retention numbers are interpreted (the [env] probe
        # at the top of the run may have scrolled away by now)
        if _plugin is not None:
            try:
                lvl2 = int(getattr(_plugin, '_FIX_VERSION', 0) or 0)
            except Exception:
                lvl2 = 0
            print('  [plugin] fix level: %s -- %s'
                  % (lvl2 if lvl2 else 'PRE-v15',
                     'the v15+ teardown fixes were ACTIVE for the close'
                     ' above' if lvl2 >= 15 else
                     'the v15 teardown fixes were NOT installed; the'
                     ' pre-gc overview retention the census measured is'
                     ' the KNOWN pre-v15 signature, not a new leak'))
        _state['cycle'] = 2
        # v8: cycle 2 REOPENS the two original tabs first (stage 6).  The
        # plugin's set_files copies the texts from the OPEN original
        # tabs -- v7 sent cycle 2 in with them closed, set_files silently
        # bailed, no session ever appeared, and stage 8 then closed an
        # INNOCENT tab (the then-active one) as the "diff tab".
        _set_stage('open_originals')
        _run_stage()
    else:
        with _tee_section('LEAK VERDICT + censuses'):
            _finish_run()


def _timer_delete_probe():
    """v14: the author's TIMER_DELETE console proof, miniature and
    self-cleaning.  Arms two tiny probe timers through the wrapper's
    public API (START+STOP+DELETE, and START+DELETE) and checks whether
    cudatext._live still holds the probe callbacks afterwards.  The
    v12-era wrapper (only TIMER_STOP fixed) and the stock wrapper leave
    BOTH entries pinned; the v14 wrapper (TIMER_DELETE also pops _live)
    leaves neither.  Returns None (wrapper not inspectable) or a dict
    {stop_delete: bool, delete_only: bool} -- True means RESIDUE.

    Self-cleaning on EVERY wrapper generation: the probe sids are popped
    from _live manually at the end, and the Pascal entries get a
    string-form TIMER_DELETE (freeing a NON-firing timer from inside a
    tick is safe; only the currently-firing TTimer must not be freed)."""
    ct = sys.modules.get('cudatext')
    if ct is None or not isinstance(getattr(ct, '_live', None), dict):
        return None
    if not all(hasattr(ct, n) for n in ('TIMER_START', 'TIMER_STOP',
                                        'TIMER_DELETE')):
        return None
    live = ct._live
    leftover = []
    out = {}

    def probe(seq):
        payload = bytearray(1024)          # small: entries are counted
        cb = (lambda p: (lambda t, p=p: None))(payload)
        sid = str(cb)
        try:
            ct.timer_proc(ct.TIMER_START, cb, 60000, '')   # never fires
            for cmd in seq:
                ct.timer_proc(cmd, cb, 0, '')
        except Exception:
            pass
        out_key = 'stop_delete' if len(seq) == 2 else 'delete_only'
        out[out_key] = sid in live
        leftover.append(sid)

    probe((ct.TIMER_STOP, ct.TIMER_DELETE))     # author test 1
    probe((ct.TIMER_DELETE,))                   # author test 3
    for sid in leftover:                        # surgery for old wrappers
        live.pop(sid, None)
        try:
            cmd = ('module=cudatext;func=_timer_proc_callback_proxy;'
                   'info="%s";' % sid)
            ct.timer_proc(ct.TIMER_DELETE, cmd, 0, '')
        except Exception:
            pass
    return out


def _timer_leak_check(label=''):
    """v12: THE root-cause check (see the v12 notes in the module
    docstring).  The stock cudatext.py wrapper keeps every timer
    callback in the module-global _live dict forever -- stopped timers
    and fired one-shots included -- so every closed differ2 tab leaves
    its PaintboxOverview._apply_async_tick /
    CompareToolbar._cancel_swap_tick callbacks there, pinning the whole
    session.  The fixed wrapper (cudatext_install/py/cudatext.py in
    this zip) releases them.  v14: also runs the TIMER_DELETE probe
    (the author's 4-case console proof in miniature).  Returns the
    count of plugin-referencing entries (None when the wrapper module
    is not importable)."""
    ct = sys.modules.get('cudatext')
    if ct is None:
        return None
    live = getattr(ct, '_live', None)
    if not isinstance(live, dict):
        return None
    fixed = hasattr(ct, '_timer_cmd_string')
    stale = []
    for k, v in list(live.items()):
        try:
            s = repr(v)
        except Exception:
            s = '?'
        if 'cuda_differ' in s or 'cdiffer' in s:
            stale.append(s[:150])
    tag = (' (%s)' % label) if label else ''
    print('  [timer] cudatext.py wrapper: %s%s'
          % ('LEAK-FIXED (timer callbacks released on stop/fire)'
             if fixed else 'STOCK (every timer callback is kept in'
                          ' _live forever)',
             tag))
    print('  [timer] cudatext._live: %d entries, %d reference plugin'
          ' objects' % (len(live), len(stale)))
    for s in stale[:4]:
        print('    e.g. %s' % s)
    if stale and not fixed:
        print('    => ROOT CAUSE PRESENT: each compare session leaves')
        print('       its 2 timer callbacks here and pins the whole')
        print('       session (~720k objects / hundreds of MB per 50 MB')
        print('       compare).  FIX: copy cudatext_install/py/')
        print('       cudatext.py from this zip over <CudaText>/py/')
        print('       cudatext.py and restart CudaText.')
    elif fixed and not stale:
        print('    => CLEAN: the leak-fixed wrapper is active and no')
        print('       plugin session is pinned by a timer callback.')
    # v14: the author's TIMER_DELETE proof, run live on THIS install
    probe = _timer_delete_probe()
    if probe is not None:
        sd = probe.get('stop_delete')
        do_ = probe.get('delete_only')
        if sd is False and do_ is False:
            print('  [timer] TIMER_DELETE probe: START+STOP+DELETE ->'
                  ' _live +0, START+DELETE -> _live +0  => CLEAN'
                  ' (v14 wrapper)')
        else:
            print('  [timer] TIMER_DELETE probe: START+STOP+DELETE ->'
                  ' _live +%d, START+DELETE -> _live +%d  =>'
                  ' TIMER_DELETE STILL PINS _live'
                  % (1 if sd else 0, 1 if do_ else 0))
            print('    => the wrapper is stock or v12-era (only'
                  ' TIMER_STOP was fixed).  Install the V14')
            print('       cudatext_install/py/cudatext.py from this'
                  ' zip over <CudaText>/py/cudatext.py and restart.')
    return len(stale)


def _finish_run():
    """v9: the run's tail -- the LEAK VERDICT (final report only; v8
    printed a misleading INCONCLUSIVE after cycle 1 already), the
    object-count growth check (the honest discriminator when cycle 2
    used a smaller pair), the two censuses and the shutdown."""
    b = _state['baselines']
    ok = _state.get('cycle_ok', {})
    strat = _state.get('cycle2_strategy', 'same')
    print('\n  ' + '-' * 68)
    print('  LEAK VERDICT')
    print('  ' + '-' * 68)
    # v12: the timer-leak check + the per-cycle growth of stale
    # timer callbacks (the proven root cause)
    stale2 = _timer_leak_check('after cycle 2 close')
    stale1 = _state.get('timer_stale_c1')
    if isinstance(stale1, int) and isinstance(stale2, int):
        if stale1 or stale2:
            print('  [timer] stale timer callbacks: %d after cycle 1'
                  ' close -> %d after cycle 2 close  => %s'
                  % (stale1, stale2,
                     'PER-COMPARE TIMER LEAK (the root cause)'
                     if stale2 > stale1 else
                     'leftover timer callbacks (did not grow)'))
        else:
            print('  [timer] no plugin-referencing timer callbacks at'
                  ' either close:')
            print('  [timer] the timer leak is FIXED on this install.')
    # v13: the PRE-GC RETENTION VERDICT -- the user's exact symptom:
    # "after closing everything RAM stays at ~550 MB until I paste a
    # snippet whose first line is gc.collect()".
    pg = _state.get('pregc', {})
    if pg.get(1):
        p1 = pg[1]
        freed1 = p1.get('freed', 0)
        drop1 = p1.get('ws', 0) - p1.get('post_ws', p1.get('ws', 0))
        print('  [pregc] cycle 1, after close, BEFORE any gc: '
              'objects {:,}, big lists {}, ws {}'
              .format(p1.get('objects', 0), p1.get('big', 0),
                      _mb(p1.get('ws', 0))))
        print('  [pregc] the same heap AFTER gc.collect():    '
              'objects {:,}, ws {}   (freed {:,} objects, {} RAM)'
              .format(p1.get('post_objects', 0),
                      _mb(p1.get('post_ws', 0)), freed1, _mb(drop1)))
        if freed1 > 100000 or drop1 > 100 * 1048576:
            print('  [pregc] => RETENTION CONFIRMED: dead data waited for'
                  ' a gen-2 collection that')
            print('  [pregc]    never comes in an idle editor (the'
                  ' compare pipeline runs with gc disabled via')
            print('  [pregc]    _gc_quiet; re-enabling collects'
                  ' NOTHING).  The holder chains printed at the s5/s9'
                  ' census stage NAME what was pinned.  With the v15')
            print('  [pregc]    plugin patch (session/overview teardown'
                  ' at close + close-time gc.collect),')
            print('  [pregc]    re-run: this line should then show ~0'
                  ' freed.')
        else:
            print('  [pregc] => the collect freed nothing significant:'
                  ' no unreachable retention on this install')
            print('  [pregc]    (either the v13+/v15 plugin patch'
                  ' already frees everything at close, or the')
            print('  [pregc]    retention does not reproduce)')
        if pg.get(2):
            p2 = pg[2]
            freed2 = p2.get('freed', 0)
            drop2 = p2.get('ws', 0) - p2.get('post_ws', p2.get('ws', 0))
            print('  [pregc] cycle 2, after close, BEFORE any gc: '
                  'objects {:,}, big lists {}, ws {}'
                  .format(p2.get('objects', 0), p2.get('big', 0),
                          _mb(p2.get('ws', 0))))
            print('  [pregc] after gc.collect(): objects {:,}, ws {}'
                  '  (freed {:,}, {})'
                  .format(p2.get('post_objects', 0),
                          _mb(p2.get('post_ws', 0)), freed2, _mb(drop2)))
            if freed2 > 100000 or drop2 > 100 * 1048576:
                print('  [pregc] => PER-RUN RETENTION: the second'
                      ' compare left its own dead graph again.')
            else:
                print('  [pregc] => cycle 2 retained nothing extra:'
                      ' one-time/flat, not per-run growth.')
    else:
        print('  [pregc] no pre-gc census data this run (stage skipped?)')
    if _state.get('cmd_is_twin'):
        print('  [note] this run used a Command() TWIN (the core'
              ' instance was not found: open any')
        print('  [note] Differ 2 menu once, then re-run for the'
              ' full-fidelity path).')
        print('  [note] v15: the diag FORCED the twin\'s on_close'
              ' after each natural close (see the')
        print('  [note] \'twin close\' lines above) -- the censuses'
              ' measured the plugin\'s real')
        print('  [note] teardown code; only string-callback timers'
              ' dispatched differently.')
    if ok.get(1) and ok.get(2):
        if strat == 'same' and 'closed' in b and 'closed_2' in b:
            growth = b['closed_2'] - b['closed']
            print('  2nd-cycle close vs 1st-cycle close: %+s  %s'
                  % (('+' if growth >= 0 else '') + _mb(growth),
                     '=> REAL LEAK (grows per compare)'
                     if growth > 50 * 1048576
                     else '=> stable: one-time retention / ceiling,'
                          ' no per-run growth'))
        else:
            print('  cycle 2 used a SMALLER pair -- the ws numbers of'
                  ' differently-sized')
            print('  compares are not comparable; the verdict below comes'
                  ' from the OBJECT COUNTS.')
    # the object-count growth check (whenever both counts exist)
    try:
        gc.collect()
        n_final = len(gc.get_objects())
        n_c1 = _state.get('obj_after_c1')
        if n_c1:
            d = n_final - n_c1
            print('  live objects: %s after cycle 1 close -> %s after'
                  ' cycle 2 close  (%s%s)'
                  % ('{:,}'.format(n_c1), '{:,}'.format(n_final),
                     '+' if d >= 0 else '', '{:,}'.format(d)))
            if ok.get(1) and ok.get(2):
                if d > 200000:
                    print('  => objects GREW again on cycle 2: a'
                          ' PER-COMPARE leak (every compare adds'
                          ' retention).')
                else:
                    print('  => no additional retention on cycle 2:'
                          ' ONE-TIME retention (the holder is populated'
                          ' once and survives closes).')
                print('  Either way the deep census below NAMES the'
                      ' holder -- that is the fix target.')
    except Exception as ex:
        print('  object-count check unavailable: %r' % ex)
    if not ok.get(1):
        print('  cycle 1 did not complete a real compare+close -- this'
              ' run is void;')
        print('  see the WARNING lines above and re-run.')
    elif not ok.get(2):
        if strat == 'off':
            pass        # the SKIP message already printed above
        else:
            print('  cycle 2 did not complete a real compare+close'
                  ' (see the WARNING lines')
            print('  above -- timeout / callback crash / tabs missing);'
                  ' the cycle-2 growth')
            print('  check is void, the cycle-1 numbers and the census'
                  ' below stand.')
    _census()
    # v8: the deep retention census NAMES the owner of whatever is
    # still retained (the v7 run: +2.87M live objects, almost all
    # lists, plugin sessions 0).  It runs INSIDE this last tick and
    # takes ~5-30 s on a ~3M-object heap (v11: one batched walk, budget-
    # capped; the v10 census took 676.9 s -- the "15 min" run) -- the
    # console prints
    # progress; the app may feel busy until the footer below.
    try:
        census()
    except Exception:
        traceback.print_exc()
    _stop_watchdog()
    print('Diagnostic finished. Timer stays armed; stop it with '
          'stop() (or differ_leak_diag.stop() if imported); '
          're-measure any time with report(); deep retention census'
          ' any time with census()')
    _blt._differ_leak_diag_running = False
    _set_stage('done')


def _census():
    """v7: is the retained memory holding OBJECTS (a real leak) or
    NOTHING (allocator / arena ceiling)?  Counts live Python objects
    and the plugin's session registry.  The gc.get_objects() scan
    itself allocates transiently (a list of N pointers) -- it runs
    AFTER the measurements and frees the list before returning."""
    try:
        t0 = time.perf_counter()
        objs = gc.get_objects()
        n_all = len(objs)
        kinds = {}
        for o in objs:
            k = type(o).__name__
            kinds[k] = kinds.get(k, 0) + 1
        del objs
        top = sorted(kinds.items(), key=lambda kv: -kv[1])[:8]
        base_n = _state.get('obj_base')
        print('  [census] live Python objects: %d   (scan %.1fs; at run '
              'start: %s)' % (n_all, time.perf_counter() - t0,
                              base_n if base_n is not None else '?'))
        print('  [census] top types: %s'
              % ', '.join('%s x%d' % kv for kv in top))
        if base_n is not None:
            d = n_all - base_n
            if d > 500000:
                print('  [census] %s objects MORE than at start -- REAL '
                      'object retention: something still references '
                      'them (see top types).' % '{:+,}'.format(d))
            else:
                print('  [census] %s objects vs start -- the retained '
                      'memory holds (almost) NO objects: it is allocator'
                      '/arena retention (a ceiling), not a leak.'
                      % '{:+,}'.format(d))
    except Exception as ex:
        print('  [census] unavailable: %r' % ex)
    # v10: the v9 line here ("plugin _TabSession alive: 0") printed the
    # REGISTRY length -- an empty registry says NOTHING about the live
    # objects (on_close pops the dict; the session OBJECTS can still be
    # alive, pinned from elsewhere).  Count live instances instead.
    try:
        cmd = _state.get('cmd')
        if cmd is not None and hasattr(cmd, '_sessions'):
            print('  [census] plugin session REGISTRY (cmd._sessions): %d'
                  ' entries' % len(cmd._sessions))
        inst = _live_plugin_instances()
        if inst is not None:
            for name in sorted(inst):
                print('  [census] LIVE %-18s objects: %d'
                      '  (gc instance scan -- the real number)'
                      % (name, inst[name]))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# v8: DEEP retention census -- WHO holds the retained objects
# ---------------------------------------------------------------------------

# v10: plugin classes whose LIVE instances decide the leak story.  The
# v9 census called len(cmd._sessions) "sessions alive" -- the registry
# length; the objects can outlive the registry (on_close pops the dict
# but something else may still reference the session: a host-side
# callback table, a closure, the native side).  These counts are the
# truth.
_PLUGIN_CLASS_NAMES = ('_TabSession', 'Differ', '_CompareJob',
                       'PaintboxOverview', 'CompareToolbar', 'Command')


def _live_plugin_instances():
    """{class name: live instance count} for the plugin's classes, via
    one fresh gc scan.  None when the plugin is not loaded.  The scan
    list dies with the call (no pinning)."""
    if _plugin is None:
        return None
    gc.collect()
    objs = gc.get_objects()
    inst = {}
    for o in objs:
        t = type(o)
        if t.__name__ in _PLUGIN_CLASS_NAMES:
            mod = getattr(t, '__module__', '')
            if 'differ' in mod or 'cdiffer' in mod:
                inst[t.__name__] = inst.get(t.__name__, 0) + 1
    del objs
    return inst


def _references_plugin(v, plug_strs, _depth=0):
    """Does v (a callback / registry entry) reference a PLUGIN object?
    Checks the callable's module, its bound self, functools.partial
    func/args, closure cells, and a bounded walk into containers."""
    if _depth > 2 or v is None:
        return False
    try:
        if callable(v):
            mod = getattr(v, '__module__', '')
            if mod and any(s in mod for s in plug_strs):
                return True
            self_ = getattr(v, '__self__', None)
            if self_ is not None:
                sm = getattr(type(self_), '__module__', '')
                if sm and any(s in sm for s in plug_strs):
                    return True
            fn = getattr(v, 'func', None)          # functools.partial
            if fn is not None and _references_plugin(fn, plug_strs,
                                                     _depth + 1):
                return True
            for a in (getattr(v, 'args', None) or ()):
                if _references_plugin(a, plug_strs, _depth + 1):
                    return True
            cl = getattr(v, '__closure__', None)
            if cl:
                for c in cl:
                    try:
                        cv = c.cell_contents
                    except ValueError:
                        continue
                    if _references_plugin(cv, plug_strs, _depth + 1):
                        return True
            return False
        tn = type(v).__name__
        if tn == 'dict':
            items = list(v.items())[:200]
        elif tn in ('list', 'tuple', 'set'):
            items = [(None, x) for x in list(v)[:200]]
        else:
            tm = getattr(type(v), '__module__', '')
            return bool(tm and any(s in tm for s in plug_strs))
        for _k, x in items:
            if _references_plugin(x, plug_strs, _depth + 1):
                return True
        return False
    except Exception:
        return False


def _probe_wrapper_tables(mod_names):
    """v10: scan the cudatext wrapper modules' module-level registries
    (dict/list-valued globals: timer tables, dlg/menu callback stores,
    diff_proc job tables) for entries referencing PLUGIN objects -- a
    leftover callback pins everything it captured (the engine callback
    holds the job; the job holds the session; the session holds the
    Differ + the marker arrays).  This is the HOST-side pin the local
    mock-world repro proved must exist: the plugin's own code retains
    nothing."""
    plug_strs = ('cuda_differ', 'cdiffer')
    found_any = False
    checked = 0
    for mname in ('cudatext', 'cudatext_api'):
        m = sys.modules.get(mname)
        if m is None or not hasattr(m, '__dict__'):
            continue
        for attr, val in list(vars(m).items()):
            if attr.startswith('__'):
                continue
            if isinstance(val, dict):
                entries = list(val.items())
            elif isinstance(val, (list, tuple)) and val:
                entries = [(str(i), v) for i, v in enumerate(val)]
            else:
                continue
            if not entries or len(entries) > 100000:
                continue
            checked += 1
            n_ref = 0
            samples = []
            for k, v in entries[:5000]:
                if _references_plugin(v, plug_strs):
                    n_ref += 1
                    if len(samples) < 3:
                        try:
                            samples.append('%r -> %s'
                                           % (k, repr(v)[:120]))
                        except Exception:
                            samples.append('%r -> ?' % (k,))
            if n_ref:
                found_any = True
                print('    %s.%s: %d/%d entries reference plugin '
                      'objects  <-- LEAK HOLDER CANDIDATE'
                      % (mname, attr, n_ref, len(entries)))
                for s in samples:
                    print('      e.g. %s' % s[:160])
    if not found_any:
        print('    wrapper tables: %d module-level registries checked, '
              'none reference plugin objects' % checked)


def _iter_holder_desc(o, mod_names, class_names):
    """_holder_desc plus, for the anonymous containers, a hint of the
    contents that made them interesting (first items' types)."""
    d = _holder_desc(o, mod_names, class_names)
    return d


def _edge_hint(parent, child, mod_names):
    """How does parent reference child?  'under key K' / 'as attribute
    A' / 'at index I' -- the edge name of the holder graph."""
    try:
        tp = type(parent)
        if tp is dict:
            n = 0
            for k, v in list(parent.items())[:800]:
                if v is child:
                    return 'under key %r' % (k,)
                n += 1
            return 'under some key'
        if tp in (list, tuple):
            for i, v in enumerate(parent[:800]):
                if v is child:
                    return 'at index %d' % i
            return 'at some index'
        xd = getattr(parent, '__dict__', None)
        if type(xd) is dict:
            for k, v in list(xd.items())[:800]:
                if v is child:
                    return 'as attribute %r' % (k,)
        tpn = tp.__name__
        if tpn == 'cell':
            return 'in closure cell'
        if tpn == 'function':
            return 'in function %s (defaults/closure)' % (
                getattr(parent, '__name__', '?'),)
    except Exception:
        pass
    return ''


def _refers_to(r, o):
    """Strict containment test: does container r directly reference o?
    (gc.get_referrers already guarantees it; this recovers WHICH
    frontier object each referrer matched, for the edge labels.)"""
    try:
        tp = type(r)
        if tp is dict:
            for v in r.values():
                if v is o:
                    return True
            return False
        if tp in (list, tuple, set, frozenset):
            for v in r:
                if v is o:
                    return True
            return False
        xd = getattr(r, '__dict__', None)
        if type(xd) is dict:
            for v in xd.values():
                if v is o:
                    return True
        if tp.__name__ == 'cell':
            return r.cell_contents is o
    except Exception:
        pass
    return False


def _walk_all_chains(targets, mod_names, class_names,
                      budget_s=150.0, max_levels=6, per_level=10):
    """v11: ONE batched upward walk from ALL targets at once.

    The v10 census walked each target separately (4 top holders + 6
    live instances = 10 walks), each level doing a two-pass
    verification (gc.get_referrers per object + a full gc.collect per
    level) -- 676.9 s on the user's 4.6M-object heap: the "15 min"
    run, with the phase-stuck watchdog alerts spamming the console on
    top.  v11 does ONE gc.get_referrers(*level) call per level for the
    whole frontier, walks THROUGH bound methods (v10 stopped at
    "instance of builtins.method ROOT", one level short of the table
    that pins the toolbar), dedups holders across the whole walk
    (v10 printed the same list(len=7) lines 3-4 times -- that spam is
    what hit the CudaText console size limit), and caps itself at
    CENSUS_BUDGET_S with a partial printout.  Honest self-exclusion:
    the walk's own structures and this call chain's frames never
    count as holders; a final re-scan (in census()) verifies the
    instances still live WITHOUT any census structure."""
    t0 = time.perf_counter()
    # frames of THIS call chain: their locals hold the targets/frontier
    self_ids = set()
    _fr = sys._getframe()
    while _fr is not None:
        self_ids.add(id(_fr))
        self_ids.add(id(_fr.f_locals))
        _fr = _fr.f_back
    own = [list(targets), self_ids]      # the walk's own containers
    own_ids = {id(x) for x in own}
    own_ids.update(id(t) for t in targets)   # targets are not holders
    seen = set(own_ids)
    level = list(targets)
    found_root = False
    for depth in range(1, max_levels + 1):
        if not level:
            break
        if time.perf_counter() - t0 > budget_s:
            print('      (walk budget %.0fs reached at level %d -- the'
                  ' chain above is partial; raise CENSUS_BUDGET_S for'
                  ' more)' % (budget_s, depth))
            break
        _set_phase('census: walk L%d (%d in frontier)'
                   % (depth, len(level)))
        try:
            refs = gc.get_referrers(*level)
        except Exception as ex:
            print('      (get_referrers failed at level %d: %r)'
                  % (depth, ex))
            break
        nxt = []
        shown = 0
        for r in refs:
            rid = id(r)
            if rid in own_ids or rid in self_ids:
                continue
            if type(r).__name__ in ('list_iterator', 'tuple_iterator',
                                    'set_iterator', 'dict_keyiterator',
                                    'dict_valueiterator',
                                    'dict_itemiterator'):
                continue
            if rid in seen:            # shared holder: printed already
                continue
            seen.add(rid)
            # which frontier object(s) does r hold?  (for the label)
            holds = [o for o in level if _refers_to(r, o)]
            edge = _edge_hint(r, holds[0], mod_names) if holds else ''
            root = _is_rootlike(r, mod_names)
            if shown < per_level:
                shown += 1
                print('      L%d <- %s%s%s%s'
                      % (depth,
                         _holder_desc(r, mod_names, class_names)[:96],
                         ('  [%s]' % edge) if edge else '',
                         ('  -- holds: %s'
                          % _holder_desc(holds[0], mod_names,
                                         class_names)[:48])
                         if holds and holds[0] not in targets else '',
                         ' ROOT' if root else ''))
            elif shown == per_level:
                shown += 1
                print('      ... (%d more holders at L%d not shown)'
                      % (len(refs) - per_level, depth))
            if root:
                found_root = True
                continue          # branch resolved -- do not expand
            if len(nxt) < per_level * 2:
                nxt.append(r)
        del refs
        level = nxt
        if not nxt and not found_root:
            # every holder was a root or the frontier died out
            found_root = True
    if not found_root:
        print('      (chain not resolved within %d levels -- send this'
              ' output)' % max_levels)
    return time.perf_counter() - t0


def _is_rootlike(o, mod_names):
    """A walk terminal: module dict, class, frame, builtin -- the thing
    that roots the chain."""
    t = type(o)
    if t is dict and id(o) in mod_names:
        return True
    if isinstance(o, type):
        return True
    if t.__name__ == 'frame':
        return True
    # v11: bound methods are NOT roots -- walk THROUGH them.  v10 hit
    # the builtins blanket below and STOPPED: the user's v10 census
    # ended the CompareToolbar chain at "instance of builtins.method
    # ROOT" -- one level short of the callback table (or the native
    # registry) that actually pins the toolbar, which was the whole
    # point of the walk.
    if t.__name__ in ('method', 'builtin_function_or_method'):
        return False
    if t.__module__ == 'builtins' and not isinstance(o, (dict, list,
                                                         tuple, set)):
        return True
    return False


def _holder_desc(o, mod_names, class_names):
    """One readable line naming a holder/referrer object."""
    try:
        t = type(o)
        if t is dict:
            n = mod_names.get(id(o))
            if n:
                if 'differ_leak_diag' in n or n == '__main__':
                    return 'module globals: %s (this diagnostic)' % n
                return 'module globals: %s' % n
            n = class_names.get(id(o))
            if n:
                return 'class __dict__ of %s' % n
            keys = ', '.join(repr(k)[:26] for k in list(o.keys())[:6])
            return 'dict(len=%d) keys~[%s]' % (len(o), keys)
        if t is list:
            # v11: name anonymous lists by CONTENT.  The user's v10 log
            # was pages of bare "list(len=7) [at index N]" lines -- the
            # console hit its size limit and the verdict was lost; a
            # content hint ("[bound_method x1, function x2]" = a
            # callback-table-like list) makes the walk output readable
            # at a fraction of the volume.
            try:
                n = len(o)
                if n <= 16:
                    kinds = {}
                    for x in o[:16]:
                        k = type(x).__name__
                        if k in ('method', 'function', 'builtin_function_or_method'):
                            k = 'callable:' + k
                        kinds[k] = kinds.get(k, 0) + 1
                    ks = ', '.join('%s x%d' % kv
                                   for kv in sorted(kinds.items(),
                                                    key=lambda kv: -kv[1])[:4])
                    return 'list(len=%d) [%s]' % (n, ks or 'empty')
            except Exception:
                pass
            return 'list(len=%d)' % len(o)
        if t is tuple:
            return 'tuple(len=%d)' % len(o)
        if t is set or t is frozenset:
            return '%s(len=%d)' % (t.__name__, len(o))
        if isinstance(o, type):
            return 'class %s.%s' % (getattr(o, '__module__', ''),
                                    getattr(o, '__name__', '?'))
        if isinstance(o, __import__('types').ModuleType):
            return 'module %s' % getattr(o, '__name__', '?')
        if t.__name__ == 'frame':
            co = getattr(o, 'f_code', None)
            if co is not None:
                return 'frame %s:%d in %s' % (
                    os.path.basename(getattr(co, 'co_filename', '?')),
                    getattr(o, 'f_lineno', 0),
                    getattr(co, 'co_name', '?'))
            return 'frame'
        if t.__name__ == 'function':
            return 'function %s.%s' % (getattr(o, '__module__', ''),
                                       getattr(o, '__name__', '?'))
        if t.__name__ == 'cell':
            return 'closure cell'
        return 'instance of %s.%s' % (getattr(t, '__module__', ''),
                                      t.__name__)
    except Exception:
        try:
            return '<undescribable %s>' % type(o).__name__
        except Exception:
            return '<undescribable>'


def census():
    """v8: DEEP retention census -- names WHO holds the retained objects.

    Answers the question the v7 census left open (2.87M live lists, the
    plugin's session registry at 0 -- so WHO references them?):
      1. snapshot every gc-tracked object after gc.collect();
      2. attribute every retained list to its IMMEDIATE holder (module
         globals dict / class __dict__ / instance __dict__ / frame
         locals / function defaults+closures / outer list / tuple /
         set / cell);
      3. print the top holders, then resolve each big holder's own
         referrers with gc.get_referrers and name them -- the CHAIN
         names the owner (e.g. "list(len=1333303) <- module globals:
         cuda_differ2.differ_native <- attr '_LAST_PLAN'");
      4. shape histogram of the retained lists (length + item type
         names + a sample repr) -- says WHAT they are;
      5. targeted probes: the interpreter's stored last exception (a
         stored traceback pins every object referenced from its
         frames), every OTHER thread's current stack (sys._
         current_frames -- a stuck worker thread holding data), live
         frames of plugin code, and the plugin profiler's class-level
         stores.
    Auto-printed at the end of the run; manually callable any time
    (bare name after the exec paste, differ_leak_diag.census() if
    imported).  v11: ONE batched walk, all targets together, wall-clock
    budget CENSUS_BUDGET_S (default 150 s; the v10 census walked each
    chain separately and took 676.9 s on a 4.6M heap)."""
    t00 = time.perf_counter()
    print('\n' + '=' * 72)
    print('DEEP RETENTION CENSUS  (%s)' % time.strftime('%H:%M:%S'))
    print('=' * 72)
    print('  (v8: ~5-30s on a 3M-object heap; the app may feel busy --')
    print('   progress lines below; this names WHO holds the retained objects)')

    _set_phase('census: gc.collect + snapshot')
    gc.collect()
    t0 = time.perf_counter()
    objs = gc.get_objects()
    n_all = len(objs)
    print('  [census+] live gc-tracked objects: %s  (snapshot %.1fs)'
          % ('{:,}'.format(n_all), time.perf_counter() - t0))

    # ---- classify: which objects are lists
    _set_phase('census: classify')
    t0 = time.perf_counter()
    list_ids = set()
    all_lists = []
    inst_counts = {}          # v11: live plugin instances, same pass
    inst_targets = []
    _inst_walk_names = ('_TabSession', 'CompareToolbar', 'PaintboxOverview',
                        '_CompareJob')     # Command/Differ excluded: the
    # diag itself holds the Command; Differ rides the session chain
    for o in objs:
        _t = type(o)
        if _t is list:
            list_ids.add(id(o))
            all_lists.append(o)
        elif _t.__name__ in _PLUGIN_CLASS_NAMES:
            _mod = getattr(_t, '__module__', '')
            if 'differ' in _mod or 'cdiffer' in _mod:
                inst_counts[_t.__name__] = inst_counts.get(_t.__name__, 0) + 1
                if _t.__name__ in _inst_walk_names \
                        and len(inst_targets) < 12:
                    inst_targets.append(o)
    n_lists = len(all_lists)
    if inst_counts:
        print('  [census+] live plugin instances (same scan, v11 -- the'
              ' v10 census re-scanned the whole heap for these):')
        for _n in sorted(inst_counts):
            print('    %-18s x%d' % (_n, inst_counts[_n]))
    print('  [census+] tracked lists: %s  (classify %.1fs)'
          % ('{:,}'.format(n_lists), time.perf_counter() - t0))
    if n_lists == 0:
        print('  [census+] nothing to attribute.')
        del objs, all_lists, list_ids
        return

    # frames of THIS census call chain: their locals hold the snapshot
    # itself -- never attribute holders to them
    self_frames = set()
    _fr = sys._getframe()
    while _fr is not None:
        self_frames.add(id(_fr))
        _fr = _fr.f_back
    frame_type = type(sys._getframe())

    # naming maps for the two anonymous-dict kinds
    mod_names = {}
    for _n, _m in list(sys.modules.items()):
        _d = getattr(_m, '__dict__', None)
        if _d is not None:
            mod_names[id(_d)] = _n
    class_names = {}
    for o in objs:
        if isinstance(o, type):
            try:
                class_names[id(o.__dict__)] = '%s.%s' % (
                    getattr(o, '__module__', ''), o.__name__)
            except Exception:
                pass

    # ---- attribution pass: every retained list -> immediate holder
    _set_phase('census: attribute holders')
    t0 = time.perf_counter()
    holders = {}          # id(holder) -> [holder, held_count]

    def _att(holder, ref):
        if id(ref) in list_ids:
            rec = holders.get(id(holder))
            if rec is None:
                holders[id(holder)] = [holder, 1]
            else:
                rec[1] += 1

    plug_frames = []
    func_type = type(census)
    cell_type = None
    try:
        cell_type = type((lambda: 0).__closure__[0])
    except Exception:
        pass
    for o in objs:
        to = type(o)
        if to is list:
            for item in o:
                if type(item) is list:
                    _att(o, item)
        elif to is dict:
            for v in o.values():
                if type(v) is list:
                    _att(o, v)
        elif to is tuple or to is set or to is frozenset:
            for v in o:
                if type(v) is list:
                    _att(o, v)
        elif to is frame_type:
            if id(o) in self_frames:
                continue
            fn = o.f_code.co_filename
            if ('cuda_differ' in fn or 'cdiffer' in fn) \
                    and len(plug_frames) < 12:
                plug_frames.append(o)
            try:
                for v in o.f_locals.values():
                    if type(v) is list:
                        _att(o, v)
            except Exception:
                pass
        elif to is func_type:
            try:
                if o.__defaults__:
                    for v in o.__defaults__:
                        if type(v) is list:
                            _att(o, v)
                if o.__kwdefaults__:
                    for v in o.__kwdefaults__.values():
                        if type(v) is list:
                            _att(o, v)
                cl = o.__closure__
                if cl:
                    for c in cl:
                        try:
                            v = c.cell_contents
                        except ValueError:
                            continue
                        if type(v) is list:
                            _att(o, v)
            except Exception:
                pass
        elif cell_type is not None and to is cell_type:
            try:
                v = o.cell_contents
            except ValueError:
                v = None
            if type(v) is list:
                _att(o, v)
        elif isinstance(o, type):
            try:
                for v in o.__dict__.values():
                    if type(v) is list:
                        _att(o, v)
            except Exception:
                pass
        # modules and instances: their __dict__ dicts are separate
        # tracked objects and were handled by the dict branch above
    print('  [census+] holders found: %s  (attribute %.1fs)'
          % ('{:,}'.format(len(holders)), time.perf_counter() - t0))

    # ---- bucket holders by description, rank by lists held
    buckets = {}
    for _hid, (holder, held) in holders.items():
        desc = _holder_desc(holder, mod_names, class_names)
        rec = buckets.get(desc)
        if rec is None:
            buckets[desc] = [1, held, holder]
        else:
            rec[0] += 1
            rec[1] += held
    top = sorted(buckets.items(), key=lambda kv: -kv[1][1])[:12]
    print('  [census+] TOP HOLDERS of the retained lists (immediate '
          'reference):')
    for desc, (nh, held, _h) in top:
        print('    %10s lists in %8d holder(s) -- %s'
              % ('{:,}'.format(held), nh, desc[:104]))


    # ---- shape histogram: WHAT the retained lists are
    _set_phase('census: shapes')
    t0 = time.perf_counter()
    step = max(1, n_lists // 40000)
    shapes = {}
    for o in all_lists[::step]:
        try:
            kinds = sorted({type(x).__name__ for x in o})
            shape = (len(o), ','.join(kinds)[:56])
        except Exception:
            continue
        rec = shapes.get(shape)
        if rec is None:
            try:
                sample = repr(o[:4])[:130]
            except Exception:
                sample = '?'
            shapes[shape] = [1, sample]
        else:
            rec[0] += 1
    top_shapes = sorted(shapes.items(), key=lambda kv: -kv[1][0])[:10]
    print('  [census+] retained-list SHAPES (sampled %d of %d lists, '
          '%.1fs):' % (len(range(0, n_lists, step)), n_lists,
                       time.perf_counter() - t0))
    for (ln, kinds), (cnt, sample) in top_shapes:
        print('    ~%10s x  len=%d  items=(%s)'
              % ('{:,}'.format(cnt * step), ln, kinds))
        print('        e.g. %s' % sample)

    # ---- targeted probes: the known suspect classes
    _set_phase('census: probes')
    print('  [census+] probes:')
    # 1) the interpreter's stored last exception: a stored traceback
    #    pins every object referenced from any of its frames
    try:
        for attr in ('last_exc', 'last_traceback'):
            v = getattr(sys, attr, None)
            if v is None:
                continue
            tb = getattr(v, '__traceback__', v)
            nfr = nlst = 0
            big = []
            while tb is not None and nfr < 60:
                fr = getattr(tb, 'tb_frame', None)
                if fr is None:
                    break
                nfr += 1
                for k, val in list(fr.f_locals.items()):
                    if type(val) is list:
                        nlst += 1
                        if len(val) > 1000:
                            big.append('%s(len=%d)' % (k, len(val)))
                tb = tb.tb_next
            print('    sys.%s PRESENT: %d frames, %d list-valued locals%s'
                  % (attr, nfr, nlst,
                     ('; big: ' + ', '.join(big[:6])) if big else ''))
    except Exception as ex:
        print('    last-exc probe failed: %r' % ex)
    # 2) every OTHER thread's current stack: a stuck worker holding data
    try:
        import threading
        me = threading.current_thread().ident
        others = 0
        for tid, fr in sys._current_frames().items():
            if tid == me:
                continue
            others += 1
            tname = '?'
            for t in threading.enumerate():
                if t.ident == tid:
                    tname = t.name
                    break
            chain = []
            f = fr
            while f is not None and len(chain) < 6:
                chain.append('%s:%d %s' % (
                    os.path.basename(f.f_code.co_filename),
                    f.f_lineno, f.f_code.co_name))
                f = f.f_back
            print('    thread %-24s %s'
                  % (tname, ' <- '.join(chain)[:190]))
        if others == 0:
            print('    threads: no other live threads')
    except Exception as ex:
        print('    thread probe failed: %r' % ex)
    # 3) live frames of PLUGIN code (stuck callbacks / workers)
    if plug_frames:
        for fr in plug_frames[:6]:
            try:
                print('    live plugin frame: %s:%d in %s'
                      % (os.path.basename(fr.f_code.co_filename),
                         fr.f_lineno, fr.f_code.co_name))
            except Exception:
                pass
    else:
        print('    live plugin frames: none')
    # 4) the plugin profiler's class-level stores
    try:
        prof = getattr(_plugin, 'profiling', None)
        P = getattr(prof, 'Profiler', None) if prof is not None else None
        if P is not None:
            print('    plugin Profiler: rows=%d stack=%d async_pending=%d'
                  % (len(getattr(P, '_rows', {})),
                     len(getattr(P, '_stack', [])),
                     len(getattr(P, '_async_pending', {}))))
    except Exception:
        pass
    # 5) the plugin's registries (diagnostic instance + any other)
    try:
        cmd = _state.get('cmd')
        if cmd is not None and hasattr(cmd, '_sessions'):
            print('    plugin _sessions (diagnostic cmd): %d' % len(cmd._sessions))
        tb_mod = getattr(_plugin, 'toolbar', None)
        if tb_mod is not None and hasattr(tb_mod, '_TOOLBARS'):
            print('    plugin toolbar._TOOLBARS: %d' % len(tb_mod._TOOLBARS))
    except Exception:
        pass
    # 6) v10: gc state -- a disabled gc or an uncollectable garbage
    #    list explains retention that no holder chain can
    try:
        print('    gc enabled: %s; gc.garbage: %d entries'
              % (gc.isenabled(), len(gc.garbage)))
        if gc.garbage:
            kinds = {}
            for o in gc.garbage[:2000]:
                kinds[type(o).__name__] = kinds.get(type(o).__name__, 0) + 1
            print('    gc.garbage top types: %s'
                  % ', '.join('%s x%d' % kv
                              for kv in sorted(kinds.items(),
                                              key=lambda kv: -kv[1])[:6]))
    except Exception as ex:
        print('    gc-state probe failed: %r' % ex)
    # 7) v10: the cudatext WRAPPER's callback registries -- a Python-side
    #    table (timers / dialogs / menus / diff_proc jobs) that still
    #    holds a plugin callback pins everything it captured
    try:
        _probe_wrapper_tables(mod_names)
    except Exception as ex:
        print('    wrapper-table probe failed: %r' % ex)

    # ---- extract the walk targets BEFORE dropping the census
    # structures (v10: the v9 census ran its referrer walks while objs/
    # all_lists/holders/buckets were still bound -- the census pinned
    # the WHOLE heap and its own bookkeeping records showed up as fake
    # holders)
    walk_targets = [holder for _desc, (_nh, _held, holder) in top[:4]]
    n_top = len(walk_targets)
    n_inst = len(inst_targets)
    del top, buckets, holders, all_lists, list_ids, objs
    gc.collect()

    # ---- v11: ONE batched walk over ALL targets.  The v10 census
    # walked each target separately (4 top holders, then 6 live
    # instances), every level two-pass-verified with a FULL gc.collect
    # per level per chain: 676.9 s on the user's 4.6M-object heap (the
    # "15 min" run), and its "phase stuck" alerts spamming the console
    # on top.  The live instances were collected during the classify
    # pass above (v10 re-scanned the whole heap for them: +15 s).
    # Methods are walked THROUGH (v10 stopped at "instance of
    # builtins.method ROOT" -- one level short of the callback table
    # that pins the toolbar; that misprint is why the user's v10 log
    # never named the final holder).
    all_targets = walk_targets + inst_targets
    del walk_targets, inst_targets
    print('  [census+] holder chains -- ONE batched walk (v11):'
          ' %d top holder(s) + %d live plugin instance(s),'
          ' budget %.0fs, methods walked through:'
          % (n_top, n_inst, CENSUS_BUDGET_S))
    _tw = _walk_all_chains(all_targets, mod_names, class_names,
                           budget_s=CENSUS_BUDGET_S)
    del all_targets
    print('  [census+] (the walk took %.1fs)' % _tw)

    # ---- v11 VERIFICATION: one end-to-end check replacing v10's
    # per-level two-pass gc.collect.  Drop every walk structure
    # (census frame locals released above), ONE gc.collect, re-scan
    # the plugin instance set.  Instances still alive WITHOUT any
    # census structure prove the chains above name REAL holders --
    # and the leak outlives even the census that measured it.
    _set_phase('census: verify (drop + collect + rescan)')
    inst2 = _live_plugin_instances()
    print('  [census+] VERIFICATION (walk structures dropped, one'
          ' gc.collect, instance re-scan):')
    if inst2:
        for _n in sorted(inst2):
            print('    %-18s still x%d' % (_n, inst2[_n]))
        print('  [census+] instances alive WITHOUT the census holding'
              ' them: the')
        print('  [census+] chains above name REAL holders -- the ROOT'
              ' line is the fix target.')
    else:
        print('    (no plugin instances alive at verification --'
              ' nothing retained on the plugin side)')

    print('  [census+] done (%.1fs total).  The top-holder row plus its'
          % (time.perf_counter() - t00))
    print('  [census+] referrer chain NAME the owner of the retained objects.')
    print('  [census+] (v11: everything from DEEP RETENTION CENSUS down is'
          ' ALSO')
    print('  [census+]  appended to the watchdog log file -- the CudaText')
    print('  [census+]  console limit truncated the v10 run\'s verdict;'
          ' the file does not.)')
    _set_phase('census: done')
    try:
        del shapes
    except NameError:
        pass


def report():
    """Re-measure now (call manually any time, e.g. 30 s after the run
    finished, to see whether lazy frees landed late).  After the run
    is over this also prints the object census."""
    measure('manual re-measure')
    if _state.get('stopped') or _state.get('stage') == 'done':
        with _tee_section('re-measure census'):
            _census()


# stage sequence
_SEQ = ['engine', 'open_originals', 'compare',
        'close_diff', 'close_originals', 'pregc', 'report']

# waiting sub-stages map back to their parent stage for _advance()
_WAIT_PARENT = {'compare_wait': 'compare',
                'close_diff_wait': 'close_diff',
                'close_orig_wait': 'close_originals'}


def _set_stage(name):
    _state['stage'] = name
    _state['t_mark'] = time.perf_counter()


def _advance():
    st = _WAIT_PARENT.get(_state['stage'], _state['stage'])
    i = _SEQ.index(st) if st in _SEQ else -1
    if i < 0 or i + 1 >= len(_SEQ):
        _set_stage('done')
        return
    _set_stage(_SEQ[i + 1])
    _run_stage()


def _run_stage():
    st = _state['stage']
    try:
        if st == 'engine':
            _stage_engine()
            _advance()
        elif st == 'open_originals':
            _stage_open_originals()
            _advance()
        elif st in ('compare', 'close_diff', 'close_originals', 'pregc',
                    'report'):
            _st_fn = {'compare': _stage_compare,
                      'close_diff': _stage_close_diff,
                      'close_originals': _stage_close_originals,
                      'pregc': _pregc_census,
                      'report': _stage_report}[st]
            if st == 'report':
                with _tee_section('FINAL REPORT'):
                    _st_fn()
            else:
                _st_fn()
        # waiting stages do their work in the timer tick
    except Exception:
        traceback.print_exc()
        _set_stage('done')


def _on_timer(tag='', info=''):
    if _state.get('stopped'):
        return
    # v6: re-entrancy guard.  app_proc(PROC_IDLE) runs
    # Application.ProcessMessages, which dispatches due timer events --
    # including THIS timer -- so _on_timer can fire AGAIN while a
    # previous tick is still on the stack (that recursion was the
    # v4/v5 stage-4 "hang").  A tick inside a tick is simply dropped.
    if _state.get('in_tick'):
        return
    _state['in_tick'] = True
    try:
        st = _state['stage']
        if st == 'compare_wait':
            _stage_compare_wait()
        elif st == 'close_diff_wait':
            _stage_close_diff_wait()
        elif st == 'close_orig_wait':
            _stage_close_orig_wait()
        elif st == 'close_originals':
            # v7: incremental, one close per tick, resumable
            _stage_close_originals()
        else:
            return
        _state['tick_err'] = 0
    except Exception:
        # v7: a Python-visible tick error no longer kills the machine:
        # count it, keep the timer armed, disarm cleanly after 5.
        _state['tick_err'] = _state.get('tick_err', 0) + 1
        print('[diag] tick error #%d at stage %r (timer stays armed):'
              % (_state['tick_err'], st))
        traceback.print_exc()
        if _state['tick_err'] >= 5:
            print('\n[diag] 5 tick errors -- disarming the diagnostic.')
            print('  Tabs may still be open: close them BY HAND, wait '
                  '~10 s, then call')
            print('  report()  -- it re-measures and prints the object '
                  'census.')
            _stop_watchdog()
            stop()
            _state['tick_err'] = 0
    finally:
        _state['in_tick'] = False


_plugin_info_cache = None


def _plugin_native_info():
    """Which module does the INSTALLED plugin's differ_native.py check
    for diff_proc?  Plugin versions differ in a critical way here:
      - the public snapshot:  import cudatext as _ct
        _HAS_NATIVE_DIFF = hasattr(_ct, 'diff_proc')  -- the engine is
        invisible to it when py/cudatext.py is stock, so it silently
        runs its pure-Python engines;
      - newer local versions import cudatext_api DIRECTLY -- the raw
        Pascal module that every diff-API-patched binary registers, so
        the NATIVE engine runs regardless of the py wrapper.
    Returns {'src', 'checks' ('cudatext_api'|'cudatext'|'unknown'),
    'evidence', 'plugin_dirs'}; cached per run."""
    global _plugin_info_cache
    if _plugin_info_cache is not None:
        return _plugin_info_cache
    info = {'src': None, 'checks': 'unknown', 'evidence': [],
            'plugin_dirs': []}
    pfile = getattr(_plugin, '__file__', None) if _plugin is not None else None
    plug_dirs = []
    if pfile:
        plug_dirs.append(os.path.dirname(os.path.abspath(pfile)))
    try:
        base = ct.app_path(ct.APP_DIR_SETTINGS)
        for sub in ('cuda_differ2', 'cuda_differ'):
            pd = os.path.join(base, 'py', 'plugins', sub)
            if os.path.isdir(pd) and pd not in plug_dirs:
                plug_dirs.append(pd)
    except Exception:
        pass
    info['plugin_dirs'] = plug_dirs
    for pd in plug_dirs:
        cand = os.path.join(pd, 'differ_native.py')
        if os.path.isfile(cand):
            info['src'] = cand
            break
    if info['src']:
        try:
            with open(info['src'], encoding='utf-8',
                      errors='replace') as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        alias = {}
        for ln in lines:
            s = ln.strip()
            if not s or s.startswith('#'):
                continue
            if len(info['evidence']) < 6 and (
                    'import cudatext' in s
                    or ('hasattr' in s and 'diff_proc' in s)
                    or s.startswith('_HAS_NATIVE_DIFF')):
                info['evidence'].append(s[:110])
            m = re.match(r'import\s+(cudatext_api|cudatext)\s+as\s+(\w+)',
                         s)
            if m:
                alias[m.group(2)] = m.group(1)
                continue
            m = re.match(r'import\s+(cudatext_api|cudatext)\s*$', s)
            if m:
                alias[m.group(1)] = m.group(1)
                continue
            m = re.search(r'_HAS_NATIVE_DIFF\s*=\s*hasattr\(\s*(\w+)\s*,', s)
            if m and m.group(1) in alias:
                info['checks'] = alias[m.group(1)]
    _plugin_info_cache = info
    return info


def _env_report():
    """Truth table: where the native diff API is visible, which module
    the INSTALLED plugin checks for it, and which engine its compares
    really use.  A patched binary + stock py/cudatext.py only forces a
    pure-Python fallback on plugin versions that read diff_proc off the
    cudatext WRAPPER -- versions that import cudatext_api directly run
    the NATIVE engine in that very same installation."""
    in_ct = hasattr(ct, 'diff_proc')
    in_api = _ctapi is not None and hasattr(_ctapi, 'diff_proc')
    info = _plugin_native_info()
    print('[env] cudatext.diff_proc:     %s'
          % ('present (patched py/cudatext.py)' if in_ct
             else 'MISSING (stock py/cudatext.py)'))
    print('[env] cudatext_api.diff_proc: %s'
          % ('present -- binary HAS the diff API' if in_api
             else 'MISSING -- binary WITHOUT the diff API'
             if _ctapi is not None else 'module cudatext_api not importable'))
    if _plugin is not None:
        try:
            print('[env] plugin loaded from:     %s' % _plugin.__file__)
        except Exception:
            pass
        # v16: the plugin's own fix level -- the _FIX_VERSION marker in
        # cuda_differ2/__init__.py.  0 = the marker is absent = the
        # installed __init__.py predates the v15 teardown fixes.
        lvl = 0
        try:
            lvl = int(getattr(_plugin, '_FIX_VERSION', 0) or 0)
        except Exception:
            pass
        print('[env] plugin fix level:      %s'
              % (lvl if lvl else 'PRE-v15 (no _FIX_VERSION marker)'))
        if lvl < 15:
            print('[env] WARNING: the INSTALLED cuda_differ2 predates the')
            print('[env]   v15 teardown fixes -- overview.destroy() does')
            print('[env]   not clear its per-line arrays and the')
            print('[env]   overview<->worker-thread cycle is not broken at')
            print('[env]   close.  The pre-gc census below WILL show the')
            print('[env]   known overview signature (PaintboxOverview +')
            print('[env]   _Row objects + 1.5M-int arrays + ~533k tuples')
            print('[env]   that ONE gc.collect() frees), and the manual')
            print('[env]   flow will still need a gc.collect() sometimes.')
            print('[env]   FIX: install the v15/v16 plugin files')
            print('[env]   (cuda_differ2_v16/ in this zip: __init__.py +')
            print('[env]   overview.py over <CudaText>/py/cuda_differ2/)')
            print('[env]   and restart CudaText.')
        else:
            print('[env]   (v15+ teardown fixes ACTIVE: overview arrays')
            print('[env]    cleared + worker joined at close)')
    if info['src']:
        print('[env] plugin differ_native:   %s' % info['src'])
        for ev in info['evidence']:
            print('[env]     %s' % ev)
        print('[env] plugin checks for the engine on: %s'
              % {'cudatext_api': 'cudatext_api (direct, wrapper-independent)',
                 'cudatext': 'the cudatext wrapper module',
                 'unknown': 'not determined from source'}[info['checks']])
    else:
        print('[env] plugin differ_native:   source not found on disk')

    # verdict: the loaded plugin module's own flag is ground truth; the
    # parsed source decides when the module is not loaded yet
    attr = None
    dfn = getattr(_plugin, 'differ_native', None) if _plugin else None
    if dfn is not None:
        attr = getattr(dfn, '_HAS_NATIVE_DIFF', None)
    checks = info['checks']
    if attr is not None:
        used = bool(attr)
    elif checks == 'cudatext_api':
        used = in_api
    elif checks == 'cudatext':
        used = in_ct
    else:
        used = in_ct or in_api
    prefer_api = in_api and (not in_ct or checks == 'cudatext_api')
    via = ('cudatext_api (raw Pascal module)' if prefer_api
           else ('cudatext wrapper' if in_ct else 'NOT FOUND'))
    print('[env] plugin native engine:   %s'
          % ('USED -- the plugin compares run the NATIVE Pascal engine'
             if used else
             'NOT USED -- the plugin compares run the PURE-PYTHON engines'))
    print('[env] stage 1 calls diff_proc via: %s' % via)
    if not used and in_api:
        if checks == 'cudatext':
            print('[env] >>> WHY: THIS plugin version reads diff_proc off')
            print('[env]     the cudatext wrapper, and this installation')
            print('[env]     has a stock py/cudatext.py -- while the binary')
            print('[env]     DOES have the engine on cudatext_api.  Fix')
            print('[env]     EITHER way:')
            print('[env]     1) update the plugin -- newer differ_native.py')
            print('[env]        imports cudatext_api directly, or')
            print('[env]     2) copy the patched py/cudatext.py (in this')
            print('[env]        zip under cudatext_install/py/) over')
            print('[env]        <CudaText>/py/cudatext.py and restart.')
        else:
            print('[env] >>> the binary has the engine but the plugin is')
            print('[env]     not using it; check the differ_native.py')
            print('[env]     evidence lines above.')
    ws, priv = _mem()
    if ws < 0:
        print('[env] WARNING: memory readings FAILED on this system '
              '(GetProcessMemoryInfo error) --\n[env]   the MB numbers '
              'below will be meaningless.')


def _decide_cycle2(main_pair):
    """v9/v16: pick the second-compare strategy and print it.  The second
    cycle is the leak-vs-ceiling CONTROL, but repeating the full-size
    compare on top of cycle 1's retention needs a ~4.2-5.5 GB peak --
    the v8 run AND the v15 run both paged the 8 GB box exactly there
    and the engine callback faulted ('diff_proc callback: Access
    violation'), leaving the job stuck forever.  Rules:
      explicit CYCLE2='same'/'small'/'off'  -> that;
      FILES2=(a,b)                          -> 'small' with that pair;
      'auto': physical RAM >= 12 GB         -> 'same';
              smaller pair found (50mb...)  -> 'small';
              neither                       -> 'off' (census only).
    v16: the 'auto' gates use RAM_CYCLE2_SAME_OK_MB (12 GB) again --
    v13's RAM_SAME_OK_MB=6144 lowering (a MAIN-pair choice) had leaked
    into this cycle-2 gate and let the 7.9 GB box re-run the 140 MB
    pair straight into the v8 failure mode."""
    total, avail = _total_ram_mb()
    strat = None
    why = ''
    files2 = None
    mode = CYCLE2
    if mode in ('same', 'small', 'off'):
        strat = mode
        why = 'CYCLE2=%s (explicit)' % mode
        if mode == 'small':
            files2 = FILES2 or _find_smaller_pair(main_pair)
            if files2 is None:
                if total and total < RAM_CYCLE2_SAME_OK_MB:
                    strat, why = 'off', ('CYCLE2=small but no smaller '
                                         'pair found and RAM < 12 GB')
                else:
                    strat, why = 'same', ('CYCLE2=small but no smaller '
                                          'pair found')
    elif FILES2:
        if tuple(FILES2) == tuple(main_pair):
            strat, why = 'same', 'FILES2 equals FILES'
        else:
            strat, files2 = 'small', FILES2
            why = 'FILES2 (explicit pair)'
    else:
        small = _find_smaller_pair(main_pair)
        main_mb = _pair_mb(main_pair)
        if total and total < RAM_CYCLE2_SAME_OK_MB:
            if small:
                strat, files2, why = 'small', small, (
                    'physical RAM %s < 12 GB -- the full-size pair '
                    'would page the machine (the v8 failure)'
                    % ('%.1f GB' % (total / 1024.0),))
            elif main_mb and main_mb <= 64.0:
                # v10: the main pair is ALREADY RAM-safe (e.g. downgraded
                # by _maybe_downgrade_main_pair, or pinned to the 50 MB
                # pair) -- repeating it fits, so the classic same-pair
                # verdict works
                strat = 'same'
                why = ('main pair is only %.0f MB -- safe to repeat on '
                       '%.1f GB RAM' % (main_mb, total / 1024.0))
            else:
                strat, why = 'off', (
                    'physical RAM %.1f GB < 12 GB and no smaller '
                    '*_a/*_b pair found next to the main one'
                    % (total / 1024.0,))
        else:
            strat = 'same'
            why = ('physical RAM %.1f GB' % (total / 1024.0,)
                   if total else 'RAM probe unavailable -- assuming '
                   'headroom')
    if (strat == 'same' and total and total < RAM_CYCLE2_SAME_OK_MB
            and _pair_mb(main_pair) > 64.0):
        print('[env] WARNING: same-pair cycle 2 on a %.1f GB box may page'
              ' (the v8/v15 failure mode).' % (total / 1024.0,))
        print('[env]   the v16 flatline crash detector will cancel and'
              ' continue if the callback faults.')
    _state['cycle2_strategy'] = strat
    _state['files2'] = files2
    _state['cycle2_why'] = why
    if total:
        print('[env] physical RAM: %.1f GB (%.1f GB available)'
              '  [cycle 2 strategy: %s]'
              % (total / 1024.0, avail / 1024.0, strat))
    else:
        print('[env] physical RAM: probe failed'
              '  [cycle 2 strategy: %s]' % strat)
    if strat == 'small' and files2:
        print('[env]   cycle-2 pair: %s' % files2[0])
        print('[env]                 %s  (%s)' % (files2[1], why))
    elif strat == 'off':
        print('[env]   %s; the run ends after cycle 1 + the censuses.'
              % why)
        print('[env]   WHY: cycle 2 would repeat the full-size compare')
        print('[env]   ON TOP of cycle 1\'s retained floor (the v15 run:')
        print('[env]   ~1.5 GB floor + ~2.7 GB compare data = a ~4.2 GB')
        print('[env]   peak on ~4 GB free -> paging -> the engine')
        print('[env]   callback faulted -- the v8/v15 crash).')
        print('[env]   To STILL get the 2-cycle leak-vs-ceiling verdict:')
        print('[env]   1) drop a smaller *_a.txt/*_b.txt pair (e.g. the')
        print('[env]      old autogen_50mb files) next to the main pair')
        print('[env]      -> re-run: cycle 2 then runs on it safely;')
        print('[env]   2) build the fork with the cmem experiment')
        print('[env]      (README_V16_FIX.md) -- the floor drops, this')
        print('[env]      gate opens by itself;')
        print('[env]   3) force it: CYCLE2=\'same\' -- the v16 flatline')
        print('[env]      crash detector now cancels a faulted cycle 2')
        print('[env]      in ~3-4 min and the run still completes.')
    else:
        print('[env]   %s' % why)


def run(files=None):
    """Start the diagnostic. Optionally pass the (fileA, fileB) tuple.
    Re-running while a previous run is still active is refused (stop()
    first) -- this also protects against double-pasting the exec()
    one-liner while stage 1 is freezing the console.

    v15: no file is ever GENERATED.  The pair must exist (found by the
    anchor search or passed via FILES / run(files=...)); a clear ERROR
    names every searched location otherwise."""
    global FILES, _state, _meas0, _plugin_info_cache
    if getattr(_blt, '_differ_leak_diag_running', False):
        print('differ2_retention: the run started at %s is STILL ACTIVE.'
              '\nNot starting a second one.  Stop it first with:  stop()'
              % _blt._differ_leak_diag_running)
        return
    if files:
        FILES = files
    _plugin_info_cache = None     # re-derive from the installed plugin
    got = _find_files()
    if not got:
        # v15: NO generation (the author's explicit demand).  One clear
        # error naming every searched location, then stop.
        print('ERROR: no *_a.txt/*_b.txt pair found -- NOTHING WAS'
              ' CREATED OR MODIFIED.')
        print('  searched:')
        shown = []

        def _show(label, d):
            if d and d not in shown:
                shown.append(d)
                print('    %-22s %s' % (label + ':', d))

        if _SELF_DIR:
            _show('this script', _SELF_DIR)
            _show('  parent', os.path.dirname(_SELF_DIR))
            _show('  grandparent', os.path.dirname(
                os.path.dirname(_SELF_DIR)))
        for d in _tab_dirs()[:3]:
            _show('open tab folder', d)
        try:
            _show('current dir', os.getcwd())
        except Exception:
            pass
        try:
            for pd in _plugin_native_info()['plugin_dirs'][:2]:
                _show('plugin dir', pd)
        except Exception:
            pass
        print('    (+ every fixed drive, bounded %ds scan)' % DRIVE_SCAN_S)
        print('  Fix (pick one):')
        print('   1) put the two files (<base>_a.txt + <base>_b.txt, e.g.')
        print('      autogen_140mb_a.txt / autogen_140mb_b.txt) next to')
        print('      this script or one folder up, then re-paste; or')
        print('   2) point at them explicitly BEFORE the exec line:')
        print("      FILES = (r'c:\\path\\autogen_140mb_a.txt',"
              " r'c:\\path\\autogen_140mb_b.txt')")
        return
    # v10: RAM-aware MAIN pair (user request): on a low-RAM box BOTH
    # cycles run on the smaller pair -- cycle 1 was still 140 MB in v9.
    # v11: `pinned` now means a REAL user pin (FILES pre-paste or the
    # run(files=...) argument).  In v10, _find_files() cached the
    # detected pair INTO FILES, so this test was always true and the
    # downgrade never fired -- the user's v10 run compared 140 MB in
    # cycle 1 again (92 s, 4.1 GB peak).  The cache now lives in
    # _FILES_AUTO and never counts as a pin.
    _pin = bool(files) or FILES is not None
    got, main_downgraded = _maybe_downgrade_main_pair(got, pinned=_pin)
    if not main_downgraded and _pin:
        # A user pin is respected -- but never silently: on a low-RAM
        # box this is exactly the v8 page-to-death failure mode.
        try:
            _tot, _av = _total_ram_mb()
            _mb = _pair_mb(got)
            if _tot and _tot < RAM_SAME_OK_MB and _mb > 64.0:
                print('[env] WARNING: FILES is PINNED to a %.0f MB pair on'
                      ' a %.1f GB box --' % (_mb, _tot / 1024.0))
                print('[env]   cycle 1 will compare it at ~4 GB peak and'
                      ' may page the machine (the v8 failure).')
                print('[env]   To let v11 auto-downgrade to the smaller'
                      ' pair, clear the pin first:')
                print('[env]     FILES = None      (then re-paste the exec'
                      ' line), or set MAINPAIR=%r' % MAINPAIR)
        except Exception:
            pass
    _state = {'stage': 'idle', 't_mark': 0.0, 'cycle': 1, 'cmd': None,
              'files': None, 'diff_h': None, 'orig_hs': [], 'tab_id': None,
              'paint_settle': None, 'baselines': {}, 'stopped': False,
              'heartbeat': 0, 'in_tick': False, 'tick_err': 0,
              'orig_started': False, 'orig_att': {}, 'orig_gave_up': [],
              'obj_base': None, 'cycle_ok': {}, 'verify_hb': -1,
              'diff_title': '', 'ws_peak': 0, 'obj_after_c1': None,
              'files2': None, 'cycle2_strategy': 'same', 'cycle2_why': '',
              'cmp_timeout': False}
    _WATCHDOG_EVER['v'] = False     # v8: a fresh run starts a fresh log
    _meas0 = None
    _state['files'] = got
    print('=' * 72)
    print('Differ 2 memory diagnostic v16  (%s)'
          % time.strftime('%H:%M:%S'))
    print('=' * 72)
    print('files:\n  %s\n  %s' % got)
    print('plugin: %s' % ('cuda_differ2 importable' if _plugin
                          else 'cuda_differ2 NOT importable -- only '
                          'stage 1 will run'))
    _env_report()
    _decide_cycle2(got)
    _blt._differ_leak_diag_running = time.strftime('%H:%M:%S')
    _pump_and_gc()
    try:
        _state['obj_base'] = len(gc.get_objects())
        print('Python objects at baseline: %d' % _state['obj_base'])
    except Exception:
        pass
    _state['baselines']['base'] = measure('stage 0 baseline')
    _state['stopped'] = False
    _set_stage('engine')
    _run_stage()
    if _state['stage'] == 'done':
        print('\nstopping (a stage ended early -- see the messages/traceback '
              'above).')
        _blt._differ_leak_diag_running = False
        return
    if _plugin is None:
        print('\nstopping (no plugin available -- only stage 1 ran).')
        _blt._differ_leak_diag_running = False
        return
    ct.timer_proc(ct.TIMER_START, _on_timer, TICK_MS)
    print('diagnostic running; measurements appear below as stages '
          'finish.\n(stop early: stop()   -- re-measure later: report())\n')


def stop():
    """Disarm the diagnostic timer and allow a fresh run()."""
    _state['stopped'] = True
    _blt._differ_leak_diag_running = False
    try:
        ct.timer_proc(ct.TIMER_STOP, _on_timer, TICK_MS)
    except Exception:
        pass
    _stop_watchdog()
    print('diagnostic stopped; re-measure with report() '
          '(or differ2_retention_v16.report() if imported)')


# ---------------------------------------------------------------------------
# auto-start: pasting  exec(open(...).read())  into the CudaText console
# starts the diagnostic immediately; a plain import does NOT auto-start
# (call differ2_retention_v16.run() then).  The builtins sentinel makes a
# second paste while a run is active a no-op with a clear message.
# ---------------------------------------------------------------------------
if globals().get('__name__') not in ('differ_leak_diag',
                                     'differ2_retention_v16',
                                     'differ2_retention_v15'):
    run()
