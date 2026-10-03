"""Differ 2 -- folder comparison (WinMerge-style "Compare folders").

Everything about comparing two FOLDER TREES lives in this module: the
scanner engine, the folder-picker dialog, the (non-modal, multi-instance)
compare windows and their context-menu actions. The side-by-side file
compare itself is NOT done here -- when the user double-clicks a file
pair, the window hands the two paths to the Command object of
cuda_differ2/__init__.py (open_compare_pair -> set_files), so the
folder view opens real diff tabs exactly like "Compare with file...".
This module never imports cuda_differ2's __init__ (that would be a
circular import -- __init__ imports THIS module); like toolbar.py it
receives the Command object via every module-level entry point.

== Speed model ======================================================

The first pass is deliberately CHEAP -- a fast "are they different?"
overview, with the heavy line/char-level diff algorithms reserved for
the double-click. The scanner never loads a file into an editor and
never runs a diff algorithm. Two compare methods are available
(option differ2.dirs.compare_method):

"contents" (default) is WinMerge's "Quick contents" compare, built
from WinMerge's own source (Src/CompareEngines/ByteCompare.cpp):

  1. sizes differ -> Different   (no read at all; decided from the
     listing's own data)
  2. both empty   -> Identical   (no read at all)
  3. otherwise    -> both files are opened ONCE and compared in
     BYTE_CHUNK-sized reads (the same 32 KB buffers WinMerge's
     ByteCompare uses): the first differing chunk decides Different,
     two files that match chunk for chunk to the end are Identical.
     No hash is ever computed -- a chunk compare is a C-speed memcmp,
     several times cheaper than MD5 over the same bytes, and a
     differing pair stops at its FIRST difference instead of reading
     to the end (typical for edited source files, whose heads differ).
     One sequential pass, one open per side: the cheapest byte-exact
     test there is.

The "Fast (sampling) compare" option (differ2.dirs.quick_only) keeps
files above 2*HEAD_TAIL_CHUNK from being read whole: only each side's
head and tail HEAD_TAIL_CHUNK bytes are compared (two windows, at
most 128 KB per side) -- the speed-over-certainty mode. A change
confined to the middle of a big file can then be missed in the LIST;
the double-click compare still opens the real side-by-side diff,
which never misses anything.

"size_timestamp" (WinMerge's "Modified date and size" method): equal
size AND equal modification time -> Identical, different size or
time -> Different. NO file is ever opened -- the verdict comes from
the size/mtime data the directory listing itself carries. This is
the method to use when content reads are expensive (files on a slow
network share, cloud-sync placeholders that could trigger downloads,
aggressive antivirus hooks on every open): the whole scan is then
pure directory listing, whatever the tree size.
The trade-off is documented: a file rewritten while preserving its
size and mtime can be missed, and a merely touched file (same size,
newer mtime) is flagged Different without reading it -- double-click
still opens the real compare, which never lies.

Files that exist on one side only need no content test at all in either
method.

== The scan engine (what a slow compare is made of) ================

The walk is one flat listing per directory (_scan_dir), NOT os.walk
+ a stat() per file: the listing itself carries each entry's size
and mtime, so a full tree walk needs ZERO per-file stat() calls and
no lstat() at all (os.walk + os.stat doubles the metadata syscalls
-- on a source where every call costs 100-200 ms that is the whole
scan). On Windows the listing goes through the WinMerge-style bulk
finder: FindFirstFileEx(FindExInfoBasic, ..., FIND_FIRST_EX_
LARGE_FETCH) via ctypes -- the same call WinMerge's DirTravel.cpp
makes -- which fetches the WHOLE directory in one 64 KB round trip
and skips the 8.3 short-name lookup, where Python's os.scandir
(plain FindFirstFileW, small buffer) pays a fresh round trip every
few entries. That difference is a large part of "WinMerge is
instant, other tools crawl" on high-latency sources (antivirus
filter, network share, cloud placeholders). Anything unusual
(non-Windows, missing API, long paths, an unexpected error) falls
back to os.scandir, which is always correct -- just slower there.
Folder rows take their mtime from the parent listing the same way;
_stat_side survives only in the main thread's single-row refresh
after a copy/delete action.

The directory scans run CONCURRENTLY on a small thread pool
(SCAN_POOL_THREADS), WinMerge-style (its scan also feeds a thread
pool from a notification queue): both roots are submitted at once,
and the moment a directory's listing arrives, its subfolders are
submitted too -- no wave barrier, no scan ever waits behind an
unrelated slow directory. The wall time approaches the longest
CHAIN of directories plus one listing latency, not the sum of all
directories. The equal-size content tests ride the same pool
afterwards. On a fast local disk all this changes little; on a
source with high per-call latency the difference is the scan. While
the cProfile layer is on, the scan deliberately falls back to the
serial one-thread mode: cProfile only traces the thread that
started it, so pool threads would be invisible in the
function-level report (the section report still attributes the
parallel run via thread-safe standalone marks) -- a profiled scan
is therefore somewhat slower than a normal one, on purpose.

The "same folders, 30 s today / 1 s tomorrow" effect is the file
system, not growing code: each metadata call costs microseconds
warm and can cost 100-200 ms cold (antivirus scanning the file, a
spinning disk seeking, an SMB round trip) -- and a compare run
right after copying/extracting the trees competes with the
antivirus's background scan of those very files (the classic
signature: the FIRST tree walks several times slower than the
second, identical one, because the background scan finished by
then). The engine minimizes the number of metadata round trips and
hides the rest behind concurrency, but the first-ever compare of
freshly copied folders can still be slower than every later one.
For folders you compare often, excluding them (or the editor's
Python) from real-time antivirus scanning removes that cost
entirely.

== Threading model ==================================================

The scan runs on a daemon thread that touches ONLY the file system
and its own state -- never the CudaText API (which is main-thread
only). A per-window poll timer (timer_proc, ~200 ms) snapshots the
worker's results under its lock and updates the list view on the main
thread, so huge trees stream into the window while it stays fully
responsive; closing the window or starting a rescan simply sets a
cancel flag the worker checks between every file. Several compare
windows can run scans at once -- each owns one worker, one timer and
its own scan pool (the worker thread itself only coordinates).

== Profiling ========================================================

The whole folder-compare pipeline is instrumented with the plugin's
own profiler (profiling.py), gated by the same config switch as the
tab compare: differ2.advanced.enable_profiling (Options dialog /
settings/cuda_differ2.json; picked up at every scan start, no restart
needed). When it is on, a scan books BOTH profiler layers, exactly
like a tab compare does:

  * the section Profiler: 'dirs:scan_wall'/'dirs:worker' (an async
    pair -- wall time from the scan kick-off to the finished list),
    the scanner thread's steps as thread-safe standalone marks
    (dirs:walk_left/right, dirs:pair_keys, dirs:dir_rows,
    dirs:file_rows, dirs:listing -- one row per directory listing,
    whose max column is the single slowest listing's latency), the
    content tests (dirs:quick_content, same shape), the wall-time
    decomposition rows dirs:spawn_lag (ctor -> first line of the
    worker thread: scheduling + kick-off) and dirs:finish_lag
    (worker end -> the UI tick that adopted the result), and
    main-thread UI work (dirs:ui_*, dirs:picker, dirs:form_build,
    dirs:open_compare_pair) as sections/marks. The report prints at
    the scan's natural end, followed by the "folder scan facts"
    block (see _prof_facts_block): listings count + their total/
    slowest/average latency, tree sizes, how much content the
    compare read, and whether the run was serial because the
    cProfile layer was on. A cancelled scan (rescan, window closed)
    cancels its pending pieces and prints nothing, like the tab
    compare's cancel path.
  * the cProfile layer, additionally gated by
    differ2.advanced.enable_cprofile (same double gate as the tab
    compare): ONE profile, started INSIDE the scanner thread -- it
    traces the actual walk/compare work, i.e. the part of a folder
    compare that can actually be slow (the main thread's UI phases
    are already phase-measured by the section rows dirs:ui_*, and
    the main thread is what the tab compare's own cProfile layer
    covers). While this layer is on the scan runs in its SERIAL
    one-thread mode, so the profile sees every walk/hash call. On
    Python up to 3.11 profiles are per-thread and the scanner's
    layer would coexist with a simultaneously profiled tab compare
    anyway; Python 3.12+ allows one active profiling tool per
    PROCESS -- there the scanner claims the slot, and if something
    else already holds it, start_profiling() fails quietly, the
    layer stays off for this scan and the scan stays parallel.
    Printed after the section report at the scan's natural end,
    sorted by internal time (sort_key='time'), max_lines=100: the
    real bottleneck function sits at the top of the table.

This is the tool to answer "why is my compare slow": the listing
rows show the directory-listing latency (metadata round trips),
the quick_content rows the content-read time, the ui rows the
CudaText API time, and spawn_lag/finish_lag close the loop so that
walks + content + lags + ui add up to the scan_wall total --
whatever is left over is where to look next.

== Row coloring =====================================================

The results list is an owner-drawn listbox (listbox_ex +
LISTBOX_SET_DRAWN): every row is painted by the plugin, so the whole
line carries a status background color. The colors are the SAME
config colors the diff tabs use for their hunks (Command.cfg / the
theme presets):

  Different             -> color_changed  (the changed-hunks color)
  Only left (+folders)  -> color_deleted  (the deleted-hunks color)
  Only right (+folders) -> color_added    (the added-hunks color)
  Identical / Folder    -> no fill (the theme list background)

So a file painted yellow in the folder list is painted yellow line
by line when double-clicked into a compare tab; a red "only left"
file opens with red (deleted) gaps on the left side, a green "only
right" file with green (added) gaps on the right. The selected row is
painted with the theme's ListSelBg/ListSelFont and overrides the
status color. The plain listview control was abandoned on purpose:
the dialog API's listview has no per-row colors at all --
owner-drawing was the only way to give the folder list
WinMerge/Beyond-Compare-style row colors.

== Windows / instances =============================================

Every compare is a separate NON-MODAL dlg_proc form: run as many
folder compares side by side as you like (double-clicking a folder row
opens a drill-down compare of that folder pair in a new window, too).
Forms register themselves in the module-level _forms list (which keeps
their Python objects -- and thus their timer callbacks -- alive);
on_close tears a form down: cancel the worker, stop the timer, save
the window geometry, then free the form via a one-shot timer (the
menu-close-first convention used by toolbar.py). close_all() is called
from Command.on_exit_pre to stop all workers when CudaText exits.

== File operations ==================================================

The context menu offers WinMerge's core sync operations -- copy a
file/folder to the other side, delete from one side -- each guarded
by a confirmation (differ2.dirs.confirm_ops). File operations patch
the affected row in place (re-stat + re-hash of just that pair, which
is instant for the size test and one small read otherwise); folder
operations restart the scan (the tree shape changed). The window
never syncs anything behind the user's back.
"""

import base64
import fnmatch
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import (FIRST_COMPLETED, ThreadPoolExecutor,
                                wait)

import cudatext as ct
import cudax_lib as ctx
from cudax_lib import get_translation

# The plugin's profiler -- same import surface __init__.py uses; like
# every sibling module, this file never imports the package __init__
# (circular import), but profiling.py is a leaf module, safe to take.
from .profiling import (Profiler, enable_profiling, profiling_report,
                        reset_profiling, start_profiling, stop_profiling,
                        cancel_profiling)

_ = get_translation(__file__)  # I18N

# The plugin's user settings file (settings/<name>.json), the same file
# every other module of cuda_differ2 reads/writes via cudax_lib.
MODULE_JSON = 'cuda_differ2.json'

# Compare-speed constants (see the module docstring's "Speed model"):
# BYTE_CHUNK is WinMerge's own quick-contents buffer size
# (Src/CompareEngines/ByteCompare.cpp, WMCMPBUFF = 32 * KILO).
BYTE_CHUNK = 32 * 1024        # quick-contents compare read size
HEAD_TAIL_CHUNK = 64 * 1024   # quick_only sampling: head+tail window size

# Concurrent directory scans / content tests inside one folder
# compare (see the module docstring's "The scan engine"). Pure I/O
# work, so the count does not scale with CPUs; 1 = serial mode
# (also forced while the cProfile layer traces the scan).
SCAN_POOL_THREADS = 8

HISTORY_MAX = 12               # remembered folder paths per side
POLL_MS = 200                  # worker -> UI poll period while scanning
ICON_CACHE_VER = '1'           # bump to force-renew the icon cache dir

# Owner-drawn list metrics (the results list is a listbox_ex in
# LISTBOX_SET_DRAWN mode -- see the module docstring "Row coloring"):
# one row's height, and the icon gutter before the first column.
LIST_ITEM_H = 26
ICON_GUTTER = 22

# Column separator for the listbox items/header. A control character
# is used (not '|'): item captions are built from real file names,
# and '|' legally occurs in file names on Linux -- an invisible
# separator the file system cannot produce keeps the header columns
# and the fallback (non-drawn) rendering aligned.
COL_SEP = chr(31)              # ASCII unit separator

# Row statuses. Files: ST_SAME / ST_DIFF / ST_LONLY / ST_RONLY / ST_ERR.
# Folders: ST_DIR (on both sides) / ST_DIR_LONLY / ST_DIR_RONLY.
ST_SAME = 'same'
ST_DIFF = 'diff'
ST_LONLY = 'lonly'
ST_RONLY = 'ronly'
ST_ERR = 'err'
ST_DIR = 'dir'
ST_DIR_LONLY = 'dir_lonly'
ST_DIR_RONLY = 'dir_ronly'

STATUS_CAPTION = {
    ST_SAME:      _('Identical'),
    ST_DIFF:      _('Different'),
    ST_LONLY:     _('Only left'),
    ST_RONLY:     _('Only right'),
    ST_ERR:       _('Cannot read'),
    ST_DIR:       _('Folder'),
    ST_DIR_LONLY: _('Only left'),
    ST_DIR_RONLY: _('Only right'),
}

# Sort order of the Status column: the interesting rows (differences,
# one-sided files) first, the noise (identical files, plain folders)
# last. dir_* rows sit right after their file counterparts.
STATUS_SEVERITY = {
    ST_DIFF:      0,
    ST_LONLY:     1,
    ST_RONLY:     2,
    ST_DIR_LONLY: 3,
    ST_DIR_RONLY: 4,
    ST_ERR:       5,
    ST_SAME:      6,
    ST_DIR:       7,
}

# Row statuses painted with a full-line background color in the
# owner-drawn list, mapped to the Command.cfg key that holds the
# color (the SAME keys the diff tabs use for their hunk lines --
# see the module docstring "Row coloring"). Statuses absent from
# this mapping keep the plain list background.
ST_COLOR_KEY = {
    ST_DIFF:      'color_changed',
    ST_LONLY:     'color_deleted',
    ST_DIR_LONLY: 'color_deleted',
    ST_RONLY:     'color_added',
    ST_DIR_RONLY: 'color_added',
}

# Compare methods (differ2.dirs.compare_method).
METHOD_CONTENTS = 'contents'
METHOD_SIZE_TIME = 'size_timestamp'

# Columns of the compare list: (caption, alignment 'L'/'R', width).
# The two sides' size/date columns mirror WinMerge's "Left/Right
# size/date" layout; the Folder column keeps the Name column clean
# ("Name" is the base name, "Folder" the path below the compared roots).
_LIST_COLUMNS = (
    (_('Name'), 'L', 230),
    (_('Folder'), 'L', 170),
    (_('Status'), 'L', 110),
    (_('Left size'), 'R', 85),
    (_('Left date'), 'L', 125),
    (_('Right size'), 'R', 85),
    (_('Right date'), 'L', 125),
)


# ----------------------------------------------------------------------
# Options / small helpers
# ----------------------------------------------------------------------

def _get_opt(key, def_val):
    """Read a 'differ2.dirs.*' option from the plugin's JSON settings.
    'key' is the path BELOW 'differ2.' (e.g. 'dirs.quick_only'), the
    same convention as __init__.py's get_opt -- the full option name
    matches the OPTS_META entries ("differ2.dirs.quick_only")."""
    return ctx.get_opt('differ2.' + key, def_val, user_json=MODULE_JSON)


def _get_method():
    """Sanitized differ2.dirs.compare_method value ('contents' or
    'size_timestamp'); anything hand-edited and unknown falls back to
    the safe default (a broken value must not break the scan)."""
    m = _get_opt('dirs.compare_method', METHOD_CONTENTS)
    return m if m in (METHOD_CONTENTS, METHOD_SIZE_TIME) \
        else METHOD_CONTENTS


def _set_opt(key, val):
    """Write a 'differ2.dirs.*' option to the plugin's JSON settings
    (settings/cuda_differ2.json). Mirrors _get_opt above; cudax_lib's
    set_opt does the comment-preserving line-based update."""
    return ctx.set_opt('differ2.' + key, val, user_json=MODULE_JSON)


def _theme_ui():
    """Current UI-theme dict (values are {'color': int} dicts), or {}."""
    try:
        return ct.app_proc(ct.PROC_THEME_UI_DICT_GET, '') or {}
    except Exception:
        return {}


def _theme_color(key, fallback=None):
    """int color of a UI-theme key, or fallback when unavailable."""
    try:
        c = _theme_ui().get(key, {}).get('color')
        if c is not None:
            return int(c)
    except Exception:
        pass
    return fallback


def _fmt_size(n):
    """Plain number with thin grouping; '?' for unreadable (-1)."""
    if n is None:
        return ''
    if n < 0:
        return '?'
    return '{:,}'.format(n).replace(',', ' ')


def _fmt_date(t):
    """'YYYY-MM-DD HH:MM' from a timestamp, '' for None."""
    if t is None or t < 0:
        return ''
    try:
        return time.strftime('%Y-%m-%d %H:%M', time.localtime(t))
    except (ValueError, OverflowError, OSError):
        return ''


def _open_in_file_manager(path):
    """Open 'path' (file or folder) in the OS file manager."""
    try:
        if sys.platform.startswith('win'):
            os.startfile(path)  # noqa: attribute is Windows-only
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception as ex:
        ct.msg_box(_('Cannot open in file manager:\n{}\n\n{}').format(
            path, ex), ct.MB_OK + ct.MB_ICONERROR)


# ----------------------------------------------------------------------
# Icons
# ----------------------------------------------------------------------

# 16x16 RGBA PNGs, drawn offline by scripts/gen_icons.py (pure-Python
# rasterizer, 2x supersampling) and embedded here as base64 so the
# plugin ships as plain .py files with no binary attachments. On first
# use they are written to settings/cuda_differ2_icons/ (a cache -- the
# imagelist API loads icons from files only) and attached to each
# window's own imagelist (created with the form as its owner, so it is
# freed together with the form).
_ICONS_PNG = {
    'same': (
        'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAxElEQVR42mNgoAVQbjOzB+'
        'J6KN4PYxOrGaThv+F8ZzC2WOMJprUm2fwHiYMMx6sZpNB+ZwBWDDIMpyGENIPw/DvLsRsC'
        '9TNBzSAAokFeAlmIbEA9SBCX5vxT1f9hAMQGiYEsxGoASAFMES7N2AzYD/IbSAJZMbLm82'
        '8vo7gKGiv2WF2ArAmbZqxeQI4BfDYjRyd6NP6HeQMW6tg0Izm/HlvyRTEEj+b9+PLAf2xR'
        'CjIYr2Y0Q/ZDUxtyHvhPdIZCNxCfPACrkX4MLc4gygAAAABJRU5ErkJggg=='
    ),
    # 253 bytes
    'diff': (
        'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAA0klEQVR42rWTMQ7CMAxFex'
        'dYYWRgydSBkcswIeUyDD0Ba7buiIEjcAUQQoYf5UvGdaIuWLKapv4v+XXSdf+I82IZvhlL'
        'Jo7niiGQ23aT896H/BzXK8E84E0xCh/7nZuAVSEUP48HeQ2niZjzLqR4zoUoQryvlx8xA+'
        '+whAU1IGKSAogJ0WKMWYMFqwAN8cQeIMGb9czQdpilK8HdgRV7kIkFts/zbCHshG1j/sAu'
        'WM+EqO1H7/iK/RcV76l1B8R2hNtuig0kldOm74DMvlAW2Pr+Aaw4quznoqrLAAAAAElFTk'
        'SuQmCC'
    ),
    # 267 bytes
    'lonly': (
        'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAP0lEQVR42mNgGAV4wZXlOv'
        '+BuJ5SA0B4PxDbU2LAf4KuwaIYH7an1ADivIRDcz25YbCfkkAkKwbsKUoDQwcAAJsxgJNU'
        'o3dQAAAAAElFTkSuQmCC'
    ),
    # 120 bytes
    'ronly': (
        'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAPklEQVR42mNgGAV4wZXlOv'
        'ZAvB+I/5OjuR6kEYbJspVkA7BpxIUJOplkA/A5n+RAhBpEvgHYwoTitDCMkzsAQ2SGH/3b'
        'Xb4AAAAASUVORK5CYII='
    ),
    # 119 bytes
    'folder': (
        'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAPUlEQVR42mNgGB5g/Y6D9U'
        'D8Hw3X08+AwqqWeiD+TyJGWNAzdX49EP8nEY8aQFUDwhOy6oH4P4m4fpjkIwAoQEJU6k6B'
        'yAAAAABJRU5ErkJggg=='
    ),
    # 118 bytes
    'err': (
        'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAjklEQVR42mNgGPQgPCGrnh'
        'LN+4H4PxDbk6PZHqQ5JbccZMB+smwHaa5rn/ifZFfAbAeBo6fOk+4KkObCqha4AT1T58Nc'
        'UU9UqINsBGlCNgDmFaKcDlIM0gTSvGL9djAbhAl6BRZwMA3ILkB2BdYAhdkOU4zNBSAMCh'
        'usroAFHLJiXBgjQEEcqCBJeHBkNgAJNeIoS6cmqAAAAABJRU5ErkJggg=='
    ),
    # 199 bytes
}


# ----------------------------------------------------------------------
# Content test (WinMerge's "quick contents", byte compare)
# ----------------------------------------------------------------------
#
# Scan-facts counters: filled by the listing (_scan_dir) and the content
# test (_quick_contents_equal) whenever the section profiler is enabled,
# printed as the "folder scan facts" block at the scan's natural end.
# They are the tool that separates "the plugin is slow" from "the disk
# is slow": per-listing latency (pure metadata round-trip time), tree
# size, and how much file content the compare actually had to read.
# Booking is a handful of dict increments per directory / per pair; on
# the pool threads they interleave only at that statement level, which
# can lose a single increment in rare races -- diagnostics only, the
# section rows stay exact.

_SCAN_FACTS = {}


def _facts_reset():
    """(Re)start the scan-facts counters (per profiled scan batch)."""
    global _SCAN_FACTS
    _SCAN_FACTS = {
        'listings': 0,          # directory listings issued
        'listing_ms': 0.0,      # their wall time, total
        'listing_ms_max': 0.0,  # slowest single listing
        'entries_l': 0, 'entries_r': 0, 'entries_x': 0,  # listed entries
        'files_l': 0, 'files_r': 0,
        'dirs_l': 0, 'dirs_r': 0,
        'pairs': 0,             # equal-size content tests
        'pairs_early': 0,       # stopped at the first differing chunk
        'pairs_full': 0,        # read to the end (identical, or sampled)
        'pair_bytes': 0,        # bytes read by content tests
    }


def _facts_add(**kw):
    """_facts_add(listings=1, listing_ms=0.5, ...) -- bump counters."""
    facts = _SCAN_FACTS
    if not facts:
        return
    try:
        for k, v in kw.items():
            facts[k] = facts.get(k, 0) + v
    except Exception:
        pass


_facts_reset()


def _book_listing(dt, n_entries, side):
    """One directory listing finished: profiler row + facts counters."""
    if Profiler.is_enabled():
        Profiler.mark_standalone('dirs:listing', dt, 1, dt)
        _facts_add(listings=1, listing_ms=dt * 1000.0)
        facts = _SCAN_FACTS
        if dt * 1000.0 > facts.get('listing_ms_max', 0.0):
            facts['listing_ms_max'] = dt * 1000.0
        if side == 'left':
            _facts_add(entries_l=n_entries)
        elif side == 'right':
            _facts_add(entries_r=n_entries)
        else:
            _facts_add(entries_x=n_entries)


def _quick_contents_equal(pl, pr, size, quick_only, cancel_evt=None):
    """Content test for two same-sized files (the caller already knows
    the sizes match and are > 0): WinMerge's "quick contents" byte
    compare (Src/CompareEngines/ByteCompare.cpp) -- both files opened
    ONCE, read in BYTE_CHUNK-sized pieces, the first differing piece
    decides. Returns True/False, or None when a side cannot be read or
    the scan was cancelled mid-compare (the caller turns that into
    ST_ERR; a cancelled scan's rows are dropped anyway).

    'quick_only' keeps files bigger than 2*HEAD_TAIL_CHUNK from being
    read whole: only the head and tail HEAD_TAIL_CHUNK windows are
    compared (the old sampling mode, minus the hashing -- comparing the
    windows directly is cheaper than MD5 over the same bytes and stops
    at the first differing byte within a window).

    Booked to the profiler as a thread-safe standalone row
    (dirs:quick_content): deliberately NOT a section -- this runs on
    the scan pool, where pushing frames onto the shared section stack
    would interleave with the main thread's sections (see
    profiling.py's mark_standalone). The timed stretch includes the
    open+read of BOTH sides, which is what a slow file source (network
    share, cloud placeholder, antivirus hook) shows up as."""
    t0 = time.perf_counter()
    n_bytes = 0
    early = False
    try:
        with open(pl, 'rb') as fl, open(pr, 'rb') as fr:
            if quick_only and size > 2 * HEAD_TAIL_CHUNK:
                # sampling: head + tail windows only (see the docstring)
                hl = fl.read(HEAD_TAIL_CHUNK)
                hr = fr.read(HEAD_TAIL_CHUNK)
                n_bytes += len(hl) + len(hr)
                if hl != hr:
                    early = True
                    return False
                fl.seek(size - HEAD_TAIL_CHUNK)
                fr.seek(size - HEAD_TAIL_CHUNK)
                tl = fl.read(HEAD_TAIL_CHUNK)
                tr = fr.read(HEAD_TAIL_CHUNK)
                n_bytes += len(tl) + len(tr)
                if tl != tr:
                    early = True
                    return False
                return True
            while True:
                if cancel_evt is not None and cancel_evt.is_set():
                    return None
                bl = fl.read(BYTE_CHUNK)
                br = fr.read(BYTE_CHUNK)
                n_bytes += len(bl) + len(br)
                if bl != br:
                    early = True
                    return False  # first differing chunk: stop (WinMerge
                if not bl:         # does the same in ByteCompare)
                    return True    # both at EOF, equal all the way
    except OSError:
        return None
    finally:
        if Profiler.is_enabled():
            dt = time.perf_counter() - t0
            Profiler.mark_standalone('dirs:quick_content', dt, 1, dt)
            kw = {'pairs': 1, 'pair_bytes': n_bytes}
            kw['pairs_early' if early else 'pairs_full'] = 1
            _facts_add(**kw)


# ----------------------------------------------------------------------
# Row construction
# ----------------------------------------------------------------------
#
# A row is a plain dict:
#   'rel'     relative path, native separators ('' is impossible; the
#             row's file/folder sits directly in a compared root)
#   'name'    base name (what the Name column shows)
#   'dir'     parent path below the compared root ('' for root items)
#   'isdir'   True for folder rows
#   'status'  one of the ST_* constants
#   'size_l' / 'mtime_l' / 'size_r' / 'mtime_r'
#             per-side numbers, None when the side lacks the item;
#             size -1 = stat() failed at scan time (shown as '?')
#
# Shared by the scanner (worker thread, thousands of rows) and by
# DirCompareForm._refresh_row (main thread, one row after a copy or
# delete action), so a row patched in place after an operation is
# built by exactly the same logic as a scanned row.

def _stat_side(path):
    """(size, mtime) of 'path', None when it is missing/unreadable.
    Main-thread single-row refresh only (_refresh_row): the scanner
    itself never stats -- its rows carry the listing's own data."""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime)
    except OSError:
        return None


def _file_row_base(dir_l, dir_r, rel, sl, sr, method):
    """Stats-only half of _file_row: builds the row and applies every
    verdict the size/mtime data already decides (one-sided presence,
    size mismatch, two empties, unreadable files, the whole timestamp
    method). Returns (row, content): content is None when the verdict
    is final, otherwise (path_l, path_r, size) -- the content test
    still owed, which the caller runs inline (serial mode, single-row
    refresh) or submits to the scan pool."""
    name = os.path.basename(rel)
    row = {
        'rel': rel,
        'name': name,
        'dir': os.path.dirname(rel),
        'isdir': False,
        'status': ST_ERR,
        'size_l': None, 'mtime_l': None,
        'size_r': None, 'mtime_r': None,
    }
    if sl is not None:
        row['size_l'], row['mtime_l'] = sl
    if sr is not None:
        row['size_r'], row['mtime_r'] = sr

    if sl is None and sr is None:
        return row, None  # vanished both sides mid-scan; caller drops
    if sl is None:
        row['status'] = ST_RONLY
        return row, None
    if sr is None:
        row['status'] = ST_LONLY
        return row, None
    if sl[0] < 0 or sr[0] < 0:
        row['status'] = ST_ERR  # stat failed at scan time
        return row, None
    if sl[0] != sr[0]:
        row['status'] = ST_DIFF  # different sizes cannot be equal content
        return row, None
    if sl[0] == 0:
        row['status'] = ST_SAME  # two empty files
        return row, None

    if method == METHOD_SIZE_TIME:
        # WinMerge's "Modified date and size" method: equal size +
        # equal mtime -> Identical; anything else -> Different. No file
        # is ever opened -- the whole verdict comes from the listing's
        # own data, which is what makes this method immune to slow
        # opens (network, cloud placeholders, antivirus). The trade-off
        # is documented in the option's comment; the double-click diff
        # is the honest check. (Deliberately NOT booked to the profiler:
        # timing a float compare would cost more than the work -- the
        # dirs:listing rows and the scan-facts block tell this method's
        # whole story, since the walk IS the scan here.)
        same = (sl[1] == sr[1])
        row['status'] = ST_SAME if same else ST_DIFF
        return row, None

    return row, (os.path.join(dir_l, rel),
                 os.path.join(dir_r, rel),
                 sl[0])


def _file_row(dir_l, dir_r, rel, sl, sr, quick_only, method):
    """Row for the file 'rel' given per-side (size, mtime) or None.
    The serial one-row path: _refresh_row calls this from the main
    thread after a copy/delete; the scanner's fast path calls
    _file_row_base and runs the owed content tests on its pool.
    'method' selects the compare method (METHOD_*; see the module
    docstring's "Speed model") -- the size test runs first in both."""
    row, content = _file_row_base(dir_l, dir_r, rel, sl, sr, method)
    if content is None:
        return row
    same = _quick_contents_equal(content[0], content[1], content[2],
                                 quick_only)
    if same is None:
        row['status'] = ST_ERR
    else:
        row['status'] = ST_SAME if same else ST_DIFF
    return row


def _dir_row(rel, ml, mr, on_l, on_r):
    """Row for the folder 'rel'; ml/mr are mtimes (or None), on_l/on_r
    say on which sides the folder exists."""
    row = {
        'rel': rel,
        'name': os.path.basename(rel),
        'dir': os.path.dirname(rel),
        'isdir': True,
        'status': ST_DIR,
        'size_l': None, 'mtime_l': ml,
        'size_r': None, 'mtime_r': mr,
    }
    if not on_l:
        row['status'] = ST_DIR_RONLY
    elif not on_r:
        row['status'] = ST_DIR_LONLY
    return row


def _split_mask(s):
    """Parse the mask edit's text ('*.py; *.txt') into a pattern list,
    or None when empty (= show everything)."""
    parts = [p.strip() for p in s.replace(',', ';').split(';')]
    parts = [p for p in parts if p]
    return parts or None


def _mask_ok(name, mask):
    """Does 'name' pass the mask (None = pass everything)? fnmatch is
    OS-aware about case (case-insensitive on Windows), which is exactly
    the file system's own behavior."""
    if not mask:
        return True
    for pat in mask:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


# ----------------------------------------------------------------------
# Win32 bulk directory listing (the WinMerge enumeration pattern)
# ----------------------------------------------------------------------
#
# Why this exists: on Windows, os.scandir opens the directory with a
# plain FindFirstFileW, whose enumeration buffer is small -- on a
# source where every metadata round trip is expensive (antivirus
# filter pass, network share, cloud placeholders) the listing pays a
# fresh round trip every few entries. WinMerge lists directories
# with FindFirstFileEx(FindExInfoBasic, ..., FIND_FIRST_EX_
# LARGE_FETCH): a 64 KB batch that returns the WHOLE directory in one
# round trip, plus FindExInfoBasic skips the 8.3 short-name lookup
# (Src/DirTravel.cpp, LoadFiles). That API choice is a large part of
# "WinMerge is instant on folders where other tools crawl". The
# finder below calls the same API through ctypes and returns each
# entry with the same data scandir would have carried (name, dir/
# reparse/device attribute bits, size, mtime); _scan_dir prefers it
# and falls back to the plain scandir path on anything unusual
# (non-Windows, missing API, very long paths). Testable everywhere:
# the tests inject a fake finder with the same return shape.

_WIN_FIND = None   # pattern -> (entries, error); set by _init_win_find


def _init_win_find():
    """Bind the WinMerge-style bulk FindFirstFileEx enumerator (Windows
    only). On success _WIN_FIND is a callable of the directory search
    pattern ('C:\\dir\\*') returning (entries, error): entries is a
    list of {'name', 'is_dir', 'reparse', 'device', 'size', 'mtime'}
    dicts ('.'/'..' excluded), error is an OSError for a failed
    listing (partial entries are still returned; an existing EMPTY
    directory is a normal empty result, not an error). Any problem
    here leaves _WIN_FIND None -- the scandir path then stays in use,
    which is always correct, just slower on high-latency sources."""
    global _WIN_FIND
    if os.name != 'nt':
        return
    try:
        import ctypes
        import ctypes.wintypes as wt

        class _WFD(ctypes.Structure):        # WIN32_FIND_DATAW
            _fields_ = [
                ('dwFileAttributes', wt.DWORD),
                ('ftCreationTime', wt.FILETIME),
                ('ftLastAccessTime', wt.FILETIME),
                ('ftLastWriteTime', wt.FILETIME),
                ('nFileSizeHigh', wt.DWORD),
                ('nFileSizeLow', wt.DWORD),
                ('dwReserved0', wt.DWORD),
                ('dwReserved1', wt.DWORD),
                ('cFileName', ctypes.c_wchar * 260),
                ('cAlternateFileName', ctypes.c_wchar * 14),
            ]

        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.FindFirstFileExW.restype = ctypes.c_void_p
        k32.FindFirstFileExW.argtypes = [
            wt.LPCWSTR, ctypes.c_int, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        k32.FindNextFileW.restype = wt.BOOL
        k32.FindNextFileW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.FindClose.restype = wt.BOOL
        k32.FindClose.argtypes = [ctypes.c_void_p]

        invalid = ctypes.c_void_p(-1).value
        find_ex_info_basic = 1     # FINDEX_INFO_LEVELS
        find_ex_search_name_match = 0  # FINDEX_SEARCH_OPS
        find_first_ex_large_fetch = 2  # FIND_FIRST_EX_FLAGS
        file_attr_dir = 0x10
        file_attr_device = 0x4
        file_attr_reparse = 0x400
        err_no_more_files = 18
        err_file_not_found = 2     # empty existing dir's '*': normal

        def find(pattern):
            fd = _WFD()
            h = k32.FindFirstFileExW(
                pattern, find_ex_info_basic, ctypes.byref(fd),
                find_ex_search_name_match, None, find_first_ex_large_fetch)
            if not h or h == invalid:
                err = ctypes.get_last_error()
                if err == err_file_not_found:
                    return [], None      # empty directory: not an error
                return [], OSError(
                    err, 'FindFirstFileExW failed: %s' % pattern)
            entries = []
            error = None
            try:
                while True:
                    name = fd.cFileName
                    if name != '.' and name != '..':
                        ft = fd.ftLastWriteTime
                        mtime = ((ft.dwHighDateTime << 32)
                                 | ft.dwLowDateTime)
                        mtime = mtime / 10000000.0 - 11644473600.0
                        attr = fd.dwFileAttributes
                        entries.append({
                            'name': name,
                            'is_dir': bool(attr & file_attr_dir),
                            'reparse': bool(attr & file_attr_reparse),
                            'device': bool(attr & file_attr_device),
                            'size': (fd.nFileSizeHigh << 32)
                                    | fd.nFileSizeLow,
                            'mtime': mtime,
                        })
                    if not k32.FindNextFileW(h, ctypes.byref(fd)):
                        err = ctypes.get_last_error()
                        if err != err_no_more_files:
                            # keep whatever was listed before the fault,
                            # exactly like the scandir path does
                            error = OSError(
                                err, 'FindNextFileW failed: %s' % pattern)
                        break
            finally:
                k32.FindClose(h)
            return entries, error

        _WIN_FIND = find
    except Exception:
        _WIN_FIND = None


_init_win_find()


# ----------------------------------------------------------------------
# One-directory scan (the unit of work, serial or pool)
# ----------------------------------------------------------------------

def _scan_dir(root, base, mask, cancel_evt, on_err, side=''):
    """Scan ONE directory of a compared tree. Returns (files, dirs,
    subdirs):

      files    normcase(rel) -> (rel, size, mtime); (-1, -1) when the
               entry's stat data cannot be had (an unreadable file:
               _file_row_base turns that into a Cannot-read row)
      dirs     normcase(rel) -> (rel, mtime or None)
      subdirs  rel paths of real subfolders to scan next -- symlinked
               folders get a row in 'dirs' but never appear here
               (os.walk's followlinks=False semantics: no cycles)

    'base' is this directory's path RELATIVE to 'root' ('' = the root
    itself) -- the walk always knows where it is, no relpath() calls.
    'side' ('left'/'right'/'') only feeds the scan-facts counters
    (_SCAN_FACTS): which tree a listing belongs to. Size and mtime
    come straight from the directory listing: on Windows either the
    WinMerge-style bulk finder (_WIN_FIND: one 64 KB round trip per
    directory, no short names, no per-entry stat) or scandir's
    DirEntry (whose stat data the listing also carries). A whole tree
    therefore walks with ZERO per-file stat() calls and zero lstat()
    calls -- on a source where every metadata syscall costs 100-200 ms
    (cold antivirus pass, network share, cloud placeholder filter)
    that alone halves the syscall count of the old os.walk + os.stat
    walk, and the bulk finder cuts the per-entry round trips on top.

    The listing itself is timed and booked (dirs:listing + facts):
    its per-call wall time is pure metadata round-trip latency -- the
    number that separates "the disk/AV is slow" from "the plugin is
    slow" in the profiling report.

    Built as a pool job: a pure function of its arguments plus an
    error callback, cancellation checked at entry / between entries,
    and every OSError confined -- one unreadable entry or a listing
    that fails mid-way never aborts the directory, let alone the scan
    (whatever was listed before the error is kept)."""
    files = {}
    dirs = {}
    subdirs = []
    full = root if not base else os.path.join(root, base)
    if cancel_evt is not None and cancel_evt.is_set():
        return files, dirs, subdirs

    entries = None
    if _WIN_FIND is not None and len(full) < 240:
        # WinMerge-style bulk listing (see _init_win_find's comment);
        # paths that would need the \\?\ long-path prefix stay on the
        # scandir path (which handles them itself).
        try:
            t0 = time.perf_counter()
            entries, err = _WIN_FIND(os.path.join(full, '*'))
            _book_listing(time.perf_counter() - t0, len(entries), side)
        except Exception:
            entries = None               # unexpected: scandir fallback
        else:
            if err is not None:
                on_err(err, full)
    if entries is not None:
        for e in entries:
            if cancel_evt is not None and cancel_evt.is_set():
                break
            name = e['name']
            if e['is_dir']:
                rel = os.path.join(base, name)
                dirs[os.path.normcase(rel)] = (rel, e['mtime'])
                if not e['reparse']:
                    subdirs.append(rel)  # symlinked dir: row, no descend
                continue
            if e['device']:
                continue                 # NUL, CON and friends
            if not _mask_ok(name, mask):
                continue
            rel = os.path.join(base, name)
            if e['reparse']:
                # A reparse entry that is not a directory (a symlinked
                # FILE, mostly). The scandir path stats through the
                # link and compares the TARGET -- one os.stat keeps
                # the same semantics here.
                try:
                    st = os.stat(os.path.join(root, rel))
                    info = (rel, st.st_size, st.st_mtime)
                except OSError:
                    info = (rel, -1, -1)
            else:
                info = (rel, e['size'], e['mtime'])
            files[os.path.normcase(rel)] = info
        return files, dirs, subdirs

    entries = []
    t0 = time.perf_counter()
    try:
        with os.scandir(full) as it:
            for entry in it:
                entries.append(entry)
                if cancel_evt is not None and cancel_evt.is_set():
                    break
    except OSError as e:
        on_err(e, full)   # keep entries listed before the failure
    _book_listing(time.perf_counter() - t0, len(entries), side)
    for entry in entries:
        if cancel_evt is not None and cancel_evt.is_set():
            break
        try:
            if entry.is_dir(follow_symlinks=False):
                rel = os.path.join(base, entry.name)
                try:
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    mtime = None
                dirs[os.path.normcase(rel)] = (rel, mtime)
                if not entry.is_symlink():
                    subdirs.append(rel)
                continue
            try:
                ok = entry.is_file()       # through symlinks, like the
            except OSError:                # old stat-through walk
                ok = entry.is_symlink()    # broken link: unreadable row
            if not ok:
                continue                   # sockets, fifos, devices...
            if not _mask_ok(entry.name, mask):
                continue
            rel = os.path.join(base, entry.name)
            try:
                st = entry.stat()          # free on Windows (listing
                info = (rel, st.st_size, st.st_mtime)  # data), one
            except OSError:                # stat on other systems
                info = (rel, -1, -1)
            files[os.path.normcase(rel)] = info
        except OSError as e:
            on_err(e, entry.path)
    return files, dirs, subdirs


# ----------------------------------------------------------------------
# Scanner (the background worker)
# ----------------------------------------------------------------------

class _Scanner(threading.Thread):
    """One folder-pair comparison, running as a daemon thread.

    The worker walks both trees, builds folder rows and file rows (via
    the shared _file_row/_dir_row) and appends them to self.rows under
    self.lock, in sorted-by-relpath order, folder rows first. The UI
    timer snapshots rows/total/finished and never blocks: worst case
    it copies a list of a few thousand small dicts -- microseconds.

    The worker NEVER calls the CudaText API (main-thread only) and it
    keeps no reference to any form: a closed window simply cancels it
    and drops its result. Cancellation is cooperative -- checked before
    every directory and every file, so a scan of a huge tree stops
    within one file's read of clicking Close.

    The walk is one _scan_dir per directory (flat scandir; entries
    carry size/mtime on Windows, so no per-file stat() at all).
    Symlinked directories get a row but are never descended (os.walk's
    followlinks=False semantics -- no cycles); symlinked FILES are
    statted through (their target's size/content is compared -- the
    useful semantic for "did anything change here"). Directories and
    equal-size content tests run on the scan pool; with the cProfile
    layer on, everything runs serially on this thread so the profile
    sees it (see the module docstring's "The scan engine").

    Walk errors (unreadable folders, permission problems) are collected
    (bounded, first 50) into walk_errors instead of aborting the scan:
    the rest of the tree still gets compared, and the summary line
    reports how many folders were skipped.
    """

    def __init__(self, dir_l, dir_r, recursive, mask, quick_only,
                 method=METHOD_CONTENTS, cprofile_on=False):
        super().__init__(daemon=True, name='Differ2DirCompare')
        self.dir_l = dir_l
        self.dir_r = dir_r
        self.recursive = recursive
        self.mask = mask
        self.quick_only = quick_only
        self.method = method
        # Scanner-thread cProfile layer (the 2nd profiler mode):
        # enabled by the FORM when the config double-gate is on. The
        # Profile object is created and enabled HERE, on the thread it
        # must trace -- cProfile only traces the thread that called
        # enable(); the main thread's UI work is covered by the form's
        # own Profile. While this is active, _scan runs in SERIAL mode
        # (no pool) so the profile sees every walk/hash call. The pair
        # is stopped+printed by the form at the scan's natural end
        # (never from this thread: printing is a main-thread concern),
        # and cancelled without printing on every abandonment path.
        self.cprofile_on = cprofile_on
        self.cprofile = None      # (pr, stream) once run() started it
        self.cancel_evt = threading.Event()
        self.lock = threading.Lock()
        self.rows = []          # completed rows, in relpath order
        self.total = 0          # rows the scan will produce (after walks)
        self.finished = False
        self.fatal = None       # exception text of an aborted run
        self.walk_errors = []   # bounded list of (dirpath, message)
        # Wall-time decomposition (see the docstring's "Profiling"):
        # _t_spawn is taken HERE, on the main thread inside start_scan;
        # run() books the lag until it actually starts as dirs:spawn_lag
        # -- thread scheduling + anything the main thread still does
        # before/around the start. run_end is stamped right before
        # finished=True, and the UI timer books dirs:finish_lag for the
        # tick quantum on top. Together with dirs:scan_wall/worker the
        # report then accounts for EVERY millisecond of a slow scan.
        self._t_spawn = time.perf_counter()
        self.run_end = None
        self.scanned_dirs = 0   # listings completed (progress line)

    # -- control (called from the main thread) ------------------------

    def cancel(self):
        self.cancel_evt.set()

    def cancelled(self):
        return self.cancel_evt.is_set()

    def snapshot(self):
        """Consistent copy of the worker state for the UI timer."""
        with self.lock:
            return list(self.rows), self.total, self.finished

    # -- the work ------------------------------------------------------

    def run(self):
        if Profiler.is_enabled():
            # Time from the ctor (main thread, inside start_scan) until
            # this thread actually runs: scheduling + the main thread's
            # remaining kick-off work. On a healthy setup this is ~0;
            # a large value means the scan started late, not that it ran
            # slow -- the row keeps that question answerable.
            lag = time.perf_counter() - self._t_spawn
            try:
                Profiler.mark_standalone('dirs:spawn_lag', lag, 1, lag)
            except Exception:
                pass
        pr = s = None
        if self.cprofile_on and Profiler.is_enabled():
            try:
                pr, s = start_profiling()
                self.cprofile = (pr, s)
            except Exception:
                pr = s = None
        try:
            self._scan()
        except Exception as ex:  # never let the thread die silently
            self.fatal = '{}: {}'.format(type(ex).__name__, ex)
        finally:
            if pr is not None:
                cancel_profiling(pr)  # just disable; the form prints it
            self.run_end = time.perf_counter()
            with self.lock:
                self.finished = True

    def _walk_err(self, e, path):
        if len(self.walk_errors) < 50:
            try:
                self.walk_errors.append((path or '?', str(e)))
            except Exception:
                pass

    def _walks(self, pool):
        """Walk BOTH trees; returns (files_l, dirs_l, files_r, dirs_r).

        With a pool: SUBMIT-AS-DISCOVERED, no wave barrier -- both
        roots are submitted at once, and the moment a directory's
        listing arrives, its subfolders are submitted too (the pool
        queue feeds itself; FIRST_COMPLETED processes results as they
        land). No scan ever waits for an unrelated slow directory,
        whatever the tree shape. The wall time therefore approaches
        the longest CHAIN of directories, not the sum over all of
        them -- the win on sources where every metadata call is
        expensive (network share, cloud placeholders, antivirus).
        Without a pool (the cProfile layer is on: it traces only THIS
        thread, so the scan runs serially on purpose, or
        SCAN_POOL_THREADS is 1) the same _scan_dir runs inline, the
        left tree first, then the right.

        Per-side totals are booked as dirs:walk_left / dirs:walk_right
        (thread-safe standalone marks) when a side fully drains -- its
        last outstanding job completed and discovered nothing new.
        """
        prof = Profiler.is_enabled()
        sides = ('left', 'right')
        roots = {'left': self.dir_l, 'right': self.dir_r}
        files = {s: {} for s in sides}
        dirs = {s: {} for s in sides}

        if pool is None:
            for side in sides:
                t0 = time.perf_counter()
                stack = ['']
                i = 0
                while i < len(stack) and not self.cancelled():
                    base = stack[i]
                    i += 1
                    f, d, subs = _scan_dir(roots[side], base, self.mask,
                                           self.cancel_evt, self._walk_err,
                                           side)
                    files[side].update(f)
                    dirs[side].update(d)
                    self.scanned_dirs += 1
                    if Profiler.is_enabled():
                        _facts_add(**{'files_' + side: len(f),
                                      'dirs_' + side: len(d)})
                    if self.recursive:
                        stack.extend(subs)
                if prof:
                    Profiler.mark_standalone('dirs:walk_' + side,
                                             time.perf_counter() - t0)
            return files['left'], dirs['left'], files['right'], dirs['right']

        t0s = {s: time.perf_counter() for s in sides}
        submitted = {s: 0 for s in sides}
        completed = {s: 0 for s in sides}
        booked = set()
        futs = {}

        def submit(side, base):
            fut = pool.submit(_scan_dir, roots[side], base, self.mask,
                              self.cancel_evt, self._walk_err, side)
            futs[fut] = side
            submitted[side] += 1

        for s in sides:
            submit(s, '')
        while futs and not self.cancelled():
            done, _pending = wait(list(futs), return_when=FIRST_COMPLETED)
            for fut in done:
                side = futs.pop(fut)
                try:
                    f, d, subs = fut.result()
                except Exception:
                    continue   # _scan_dir books its own errors; paranoia
                files[side].update(f)
                dirs[side].update(d)
                self.scanned_dirs += 1
                if Profiler.is_enabled():
                    _facts_add(**{'files_' + side: len(f),
                                  'dirs_' + side: len(d)})
                if self.recursive and not self.cancelled():
                    for rel in subs:
                        submit(side, rel)
                completed[side] += 1
                if completed[side] >= submitted[side]:
                    # nothing outstanding, nothing new discovered: the
                    # side's tree is fully walked
                    if side not in booked:
                        booked.add(side)
                        if prof:
                            Profiler.mark_standalone(
                                'dirs:walk_' + side,
                                time.perf_counter() - t0s[side])
        return files['left'], dirs['left'], files['right'], dirs['right']

    def _scan(self):
        """Walk both trees, then build the rows. Every step is booked
        to the profiler as thread-safe standalone marks (this method
        runs on the worker thread -- see the note in _quick_contents_equal
        about why not sections). The walk and the equal-size content
        tests run on the scan pool (see the module docstring's "The
        scan engine"); with the cProfile layer active the pool is
        skipped, so the profile sees every call on THIS thread."""
        prof = Profiler.is_enabled()
        pool = None
        if self.cprofile is None and SCAN_POOL_THREADS > 1:
            try:
                pool = ThreadPoolExecutor(
                    max_workers=SCAN_POOL_THREADS,
                    thread_name_prefix='Differ2DirScan')
            except Exception:
                pool = None
        try:
            files_l, dirs_l, files_r, dirs_r = self._walks(pool)
            if self.cancelled():
                return

            t0 = time.perf_counter()
            dir_keys = sorted(set(dirs_l) | set(dirs_r))
            file_keys = sorted(set(files_l) | set(files_r))
            if prof:
                Profiler.mark_standalone('dirs:pair_keys',
                                         time.perf_counter() - t0,
                                         2)  # two merges+sorts per call
            with self.lock:
                self.total = len(dir_keys) + len(file_keys)

            # Folder rows first (they are also the skeleton of the
            # partial view while the file rows stream in behind them).
            # The walk already collected every folder's mtime with its
            # listing -- zero stat() calls in this whole loop.
            t0 = time.perf_counter()
            for k in dir_keys:
                if self.cancelled():
                    return
                rl = dirs_l.get(k)
                rr = dirs_r.get(k)
                if rl is not None:
                    rel, ml = rl
                else:
                    rel, ml = rr[0], None
                mr = rr[1] if rr is not None else None
                with self.lock:
                    self.rows.append(_dir_row(rel, ml, mr,
                                              rl is not None, rr is not None))
            if prof:
                Profiler.mark_standalone('dirs:dir_rows',
                                         time.perf_counter() - t0,
                                         len(dir_keys))

            # File rows. Pass 1 applies every verdict the stats decide
            # and submits the owed content tests (equal-size pairs) to
            # the pool; pass 2 takes their results in sorted order, so
            # rows still stream into the list one by one, in order,
            # while the remaining tests keep running in parallel.
            t0 = time.perf_counter()
            n_files = 0
            decided = []          # (key, row, content) in file_keys order
            pending = {}          # key -> Future of the content test
            for k in file_keys:
                if self.cancelled():
                    return
                fl = files_l.get(k)
                fr = files_r.get(k)
                sl = (fl[1], fl[2]) if fl is not None else None
                sr = (fr[1], fr[2]) if fr is not None else None
                rel = (fl or fr)[0]
                row, content = _file_row_base(self.dir_l, self.dir_r,
                                              rel, sl, sr, self.method)
                if content is not None and pool is not None:
                    pending[k] = pool.submit(_quick_contents_equal,
                                             content[0], content[1],
                                             content[2], self.quick_only,
                                             self.cancel_evt)
                decided.append((k, row, content))
                n_files += 1
            for k, row, content in decided:
                if self.cancelled():
                    return
                if content is not None:
                    if k in pending:
                        try:
                            same = pending[k].result()
                        except Exception:
                            same = None
                    else:
                        # serial mode (cProfile on / pool off)
                        same = _quick_contents_equal(
                            content[0], content[1], content[2],
                            self.quick_only, self.cancel_evt)
                    row['status'] = (ST_ERR if same is None
                                     else (ST_SAME if same else ST_DIFF))
                with self.lock:
                    self.rows.append(row)
            if prof:
                # NOTE: this stretch INCLUDES the content-test time (the
                # dirs:quick_content rows are booked inside
                # _quick_contents_equal, on the pool threads -- thread-
                # safe standalone marks); the row-building self time is
                # the difference. Booked with the file count so the
                # per-file average is readable in the report.
                Profiler.mark_standalone('dirs:file_rows',
                                         time.perf_counter() - t0, n_files)
        finally:
            if pool is not None:
                # Python 3.8-compatible shutdown (no cancel_futures=
                # parameter there): queued jobs no-op instantly -- the
                # first thing _scan_dir checks is the cancel event --
                # so wait=True returns promptly even after a cancel.
                pool.shutdown(wait=True)


# ----------------------------------------------------------------------
# Profiling lifecycle (module level: several compare windows can scan
# at once, and the enable/disable must be owned by exactly one of them)
# ----------------------------------------------------------------------
#
# The pattern mirrors Command.refresh_compare: config() first (so a
# just-toggled option takes effect on the next scan, no restart), then
# enable_profiling()/reset_profiling() when the config says so, the
# async pair 'dirs:scan_wall'/'dirs:worker' for the whole operation's
# wall time, the cProfile layer under the double gate
# (enable_profiling AND enable_cprofile) started inside the SCANNER
# thread, and the epilogue at the scan's natural end: section report
# first, then the cProfile report, then disable -- but only if THIS
# form was the one that enabled it. A cancelled run (rescan, window
# closed, app exit) cancels its pending pieces without printing, like
# the tab compare's cancel paths.
#
# The user counter handles overlapping scans: the first profiled scan
# of a batch owns the enable/reset/report, later ones only add their
# marks (their rows merge into the same report -- a documented
# diagnostic-tool limitation; one compare at a time is the norm).

_prof_users = 0            # profiled scans currently running
_prof_enabled_here = False # WE flipped Profiler.enabled for this batch


def _prof_begin_scan(cmd):
    """Called by DirCompareForm.start_scan. Returns (token, cprof_scan_on):
    token for stop_async_pair (None when profiling is off), and whether
    the scanner thread should start the cProfile layer.

    The cProfile layer belongs to the SCANNER thread (see the comment
    block above) -- that is where a folder compare can be slow, and
    cProfile only traces the thread that called enable(). Python up
    to 3.11: profiles are per-thread, so it would coexist with a
    simultaneously profiled tab compare anyway. Python 3.12+: one
    active profiling tool per PROCESS -- the scanner claims the slot;
    if another tool already holds it, start_profiling() fails quietly
    and the scan runs with the section profiler only (and stays
    parallel). While the layer IS on, the scan runs serially so the
    profile sees every walk/hash call."""
    global _prof_users, _prof_enabled_here
    # Load the config FIRST so the gate sees the current option value
    # (same order as refresh_compare -- without this, the first scan
    # after enabling profiling in Options would run unprofiled).
    try:
        cmd.config()
    except Exception:
        pass
    was_on = Profiler.is_enabled()
    enabled_here = False
    if not was_on:
        try:
            do_profile = bool(cmd.cfg.get('enable_profiling', False))
        except Exception:
            do_profile = False
        if do_profile:
            enable_profiling(True)
            enabled_here = True
    token = None
    cprof_scan = False
    if Profiler.is_enabled():
        if _prof_users == 0:
            reset_profiling()
            _facts_reset()      # scan-facts counters follow the report
            _prof_enabled_here = enabled_here
            try:
                cprof_scan = bool(cmd.cfg.get('enable_cprofile', False))
            except Exception:
                cprof_scan = False
        _prof_users += 1
        token = Profiler.start_async_pair('dirs:scan_wall', 'dirs:worker')
    return token, cprof_scan


def _prof_facts_block(worker):
    """The 'folder scan facts' epilogue lines: what the scan-facts
    counters say about THIS run (see _SCAN_FACTS). Printed right after
    the section report -- deliberately compact, four statements that
    answer the four questions every slow-compare report raises:

      1. how many directory listings ran, and how much of the wall
         time was pure listing latency (metadata round trips -- the
         disk/antivirus/network, not plugin code: the same number any
         other tool, WinMerge included, pays on a cold tree);
      2. how big the trees were (entries / file rows / folder rows);
      3. how much file CONTENT the compare had to read (pairs stopped
         at the first differing chunk vs read to the end);
      4. whether the scan ran serially because the cProfile layer was
         on (the function-report mode -- slower on purpose; turn it
         off to measure speed)."""
    facts = _SCAN_FACTS
    if not facts:
        return ''
    lines = ['--- Differ 2 folder scan facts ---']
    n = facts.get('listings', 0)
    ms = facts.get('listing_ms', 0.0)
    mx = facts.get('listing_ms_max', 0.0)
    if n:
        avg = ms / n
        lines.append(
            'directory listings: {}, total {:.0f} ms, slowest {:.0f} ms, '
            'avg {:.0f} ms -- pure metadata round-trip time (disk / '
            'antivirus / network), not plugin code'.format(n, ms, mx, avg))
    else:
        lines.append('directory listings: 0')
    lines.append(
        'entries listed: {} left / {} right; file rows: {} left / {} '
        'right; folder rows: {} / {}'.format(
            facts.get('entries_l', 0), facts.get('entries_r', 0),
            facts.get('files_l', 0), facts.get('files_r', 0),
            facts.get('dirs_l', 0), facts.get('dirs_r', 0)))
    pairs = facts.get('pairs', 0)
    if pairs:
        lines.append(
            'content tests: {} equal-size pairs -- {} stopped at the '
            'first difference, {} read to the end; {:.0f} KB read'.format(
                pairs, facts.get('pairs_early', 0),
                facts.get('pairs_full', 0),
                facts.get('pair_bytes', 0) / 1024.0))
    else:
        lines.append('content tests: 0 (every pair was decided by '
                     'size alone / one-sided)')
    if worker is not None and getattr(worker, 'cprofile_on', False):
        lines.append(
            'scan mode: SERIAL (differ2.advanced.enable_cprofile is on) '
            '-- set it off and rescan to run the parallel engine and '
            'measure real speed')
    return '\n'.join(lines)


def _prof_finish_scan(cmd, token, worker, dir_l, dir_r):
    """Natural end of a scan: stop the pair, and when this was the
    last profiled scan, print the reports and give the profiler back
    (only if we took it)."""
    global _prof_users, _prof_enabled_here
    if token is not None:
        Profiler.stop_async_pair(token)
    cprof_scan = getattr(worker, 'cprofile', None) if worker else None
    if _prof_users > 0:
        _prof_users -= 1
    if _prof_users > 0:
        return  # another scan still owns the batch
    if Profiler.is_enabled():
        try:
            profiling_report(
                files=[('Left', dir_l), ('Right', dir_r)],
                cprofile_was_on=bool(cprof_scan))
        except Exception:
            pass
        try:
            block = _prof_facts_block(worker)
            if block:
                print(block)
        except Exception:
            pass
    if cprof_scan:
        try:
            stop_profiling(cprof_scan[0], cprof_scan[1],
                           sort_key='time', max_lines=100,
                           title='Differ 2 folder compare: scanner thread'
                                 ' (cProfile)')
        except Exception:
            pass
    if _prof_enabled_here:
        enable_profiling(False)
        _prof_enabled_here = False


def _prof_abandon_scan(cmd, token, worker):
    """Cancelled run (rescan replaced it, window closed, app exit):
    stop the pair, cancel the scanner's cProfile WITHOUT printing,
    release the user slot; the last one out disables the profiler if
    we took it."""
    global _prof_users, _prof_enabled_here
    if token is not None:
        Profiler.stop_async_pair(token)
    cprof_scan = getattr(worker, 'cprofile', None) if worker else None
    if cprof_scan:
        cancel_profiling(cprof_scan)
        worker.cprofile = None
    if _prof_users > 0:
        _prof_users -= 1
    if _prof_users > 0:
        return
    if _prof_enabled_here:
        enable_profiling(False)
        _prof_enabled_here = False


# ----------------------------------------------------------------------
# Folder-picker dialog
# ----------------------------------------------------------------------

class _PickerDialog:
    """Modal "Select folders to compare" dialog: two editable combos
    (each with its own history, remembered in the plugin settings),
    a Browse button per side, Compare/Cancel buttons. Enter in a combo
    or clicking Compare validates both paths -- a bad path pops a
    message box and keeps the dialog open -- then hides the form, which
    ends DLG_SHOW_MODAL (the cudatext.py modal wait polls the form's
    'vis' prop). show() returns (left, right) or None."""

    W = 580          # fixed size: DBORDER_DIALOG is not resizable
    ROW_Y0 = 12
    ROW_DY = 34

    def __init__(self):
        self.h = 0
        self.result = None
        self.ctl = {}       # name -> control index
        self.ctl_rev = {}   # control index -> name

    # -- helpers -------------------------------------------------------

    def _add(self, kind, name, prop):
        n = ct.dlg_proc(self.h, ct.DLG_CTL_ADD, kind)
        self.ctl[name] = n
        self.ctl_rev[n] = name
        prop = dict(prop)
        prop['name'] = name
        ct.dlg_proc(self.h, ct.DLG_CTL_PROP_SET, index=n, prop=prop)
        return n

    def _val(self, name):
        d = ct.dlg_proc(self.h, ct.DLG_CTL_PROP_GET, name=name)
        return d.get('val', '')

    def _set_val(self, name, val):
        ct.dlg_proc(self.h, ct.DLG_CTL_PROP_SET, name=name,
                    prop={'val': val})

    # -- construction --------------------------------------------------

    def _build(self, dir_l, dir_r, hist_l, hist_r):
        bg = _theme_color('TabBg', _theme_color('ListBg'))
        prop = {
            'cap': _('Differ 2: compare folders'),
            'w': self.W,
            'h': 152,
            'border': ct.DBORDER_DIALOG,
        }
        if bg is not None:
            prop['color'] = bg
        ct.dlg_proc(self.h, ct.DLG_PROP_SET, prop=prop)

        for i, (side, label, hist, init) in enumerate((
                ('left',  _('Left folder (old):'),  hist_l, dir_l),
                ('right', _('Right folder (new):'), hist_r, dir_r))):
            y = self.ROW_Y0 + i * self.ROW_DY
            self._add('label', 'lab_' + side, {
                'cap': label,
                'x': 12, 'y': y + 5, 'w': 108, 'h': 20,
                'autosize': False,
                'font_color': _theme_color('TabFont'),
            })
            self._add('combo', side, {
                'x': 124, 'y': y, 'w': 344, 'h': 26,
                'items': '\t'.join(hist),
                'val': init,
                'texthint': _('Type or pick a folder'),
            })
            self._add('button', 'brw_' + side, {
                'cap': _('Browse...'),
                'x': 478, 'y': y, 'w': 90, 'h': 26,
                'on_change': self._on_button,
            })

        y = self.ROW_Y0 + 2 * self.ROW_DY + 10
        self._add('button', 'ok', {
            'cap': _('Compare'),
            'x': self.W - 200, 'y': y, 'w': 90, 'h': 28,
            'on_change': self._on_button,
        })
        self._add('button', 'cancel', {
            'cap': _('Cancel'),
            'x': self.W - 104, 'y': y, 'w': 90, 'h': 28,
            'on_change': self._on_button,
        })

    # -- events ----------------------------------------------------------

    def _on_button(self, id_dlg, id_ctl, data='', info=''):
        name = self.ctl_rev.get(id_ctl, '')
        if name.startswith('brw_'):
            side = name[4:]
            cur = self._val(side).strip().strip('"').strip("'")
            init = cur if os.path.isdir(cur) else os.path.expanduser('~')
            path = ct.dlg_dir(init, _('Choose folder'))
            if path:
                self._set_val(side, path)
        elif name == 'ok':
            self._try_ok()
        elif name == 'cancel':
            self.result = None
            ct.dlg_proc(self.h, ct.DLG_HIDE)

    def _try_ok(self):
        pl = self._val('left').strip().strip('"').strip("'")
        pr = self._val('right').strip().strip('"').strip("'")
        if not os.path.isdir(pl):
            ct.msg_box(_('Folder not found:\n{}').format(pl),
                       ct.MB_OK + ct.MB_ICONWARNING)
            return
        if not os.path.isdir(pr):
            ct.msg_box(_('Folder not found:\n{}').format(pr),
                       ct.MB_OK + ct.MB_ICONWARNING)
            return
        if os.path.normcase(os.path.abspath(pl)) == \
                os.path.normcase(os.path.abspath(pr)):
            ct.msg_box(_('The two folders are the same folder.'),
                       ct.MB_OK + ct.MB_ICONWARNING)
            return
        self._remember(pl, pr)
        self.result = (pl, pr)
        ct.dlg_proc(self.h, ct.DLG_HIDE)

    @staticmethod
    def _remember(pl, pr):
        # History is stored as ONE '\n'-joined string per side (not a
        # list): cudax_lib's flat-key settings updater only handles
        # single-line values, and '\n' cannot occur in a path.
        for key, path in (('dirs.hist_left', pl), ('dirs.hist_right', pr)):
            try:
                hist = _PickerDialog._load_hist(key)
                hist = [p for p in hist if p != path]
                hist.insert(0, path)
                _set_opt(key, '\n'.join(hist[:HISTORY_MAX]))
            except Exception:
                pass

    # -- entry -----------------------------------------------------------

    @staticmethod
    def _load_hist(key):
        s = _get_opt(key, '')
        if not isinstance(s, str):
            return []
        return [p for p in s.split('\n') if p]

    def show(self, dir_l='', dir_r=''):
        self.result = None
        self.h = ct.dlg_proc(0, ct.DLG_CREATE)
        try:
            self._build(dir_l, dir_r,
                        self._load_hist('dirs.hist_left'),
                        self._load_hist('dirs.hist_right'))
            ct.dlg_proc(self.h, ct.DLG_CTL_FOCUS, name='left')
            ct.dlg_proc(self.h, ct.DLG_SHOW_MODAL)
        finally:
            try:
                ct.dlg_proc(self.h, ct.DLG_FREE)
            except Exception:
                pass
            self.h = 0
        return self.result


# ----------------------------------------------------------------------
# The compare window
# ----------------------------------------------------------------------

# Which icon (by _ICONS_PNG name) each row status uses.
_ICON_OF = {
    ST_SAME:      'same',
    ST_DIFF:      'diff',
    ST_LONLY:     'lonly',
    ST_RONLY:     'ronly',
    ST_ERR:       'err',
    ST_DIR:       'folder',
    ST_DIR_LONLY: 'lonly',
    ST_DIR_RONLY: 'ronly',
}

_SORT_MARK = {False: ' \u25b4', True: ' \u25be'}  # small up/down triangles


class DirCompareForm:
    """One non-modal folder-compare window (there can be any number of
    them at once -- see the module docstring).

    Layout (all sizes are base pixels; DLG_SCALE adjusts them to the
    OS DPI before the saved geometry is re-applied):

      [ New compare... ][ Swap sides ][ Refresh ]            [ Close ]
      [ left path edit          ][Browse...]  [ right path edit   ][Br...]
      [x]Different [x]Only left [x]Only right [x]Identical
                        [x]Subfolders  Mask:[ edit ][ Apply ]
      +--------------------------------------------------------------+
      | Name | Folder | Status | Left size | Left date | R.size | R.date |
      | (owner-drawn listbox_ex, stretches with the form; every row  |
      |  is painted here with its status color -- see below)         |
      +--------------------------------------------------------------+
      [ status line / progress                    ][ counts          ]

    The path edits are editable on purpose: type two paths and press
    Refresh (or Enter) to compare them without reopening any dialog.

    The results list is an owner-drawn listbox_ex (LISTBOX_SET_DRAWN):
    the control never paints items itself, it calls on_draw_item for
    every visible row and the form paints background + icon + all
    cells -- which is what makes the full-line status colors possible
    (the dialog API's listview has no per-row colors at all). The
    colors are the diff-tab hunk colors (color_changed / color_deleted
    / color_added of Command.cfg), so a row's color matches exactly
    what its double-clicked compare tab paints. The built-in column
    header is driven with the same pixel widths the painter uses, so
    header clicks (sorting) align with the drawn cells.
    """

    DEF_W = 940
    DEF_H = 560
    MIN_W = 700
    MIN_H = 380

    # Row A (toolbar buttons) -- fixed widths, chained left to right.
    TOOLBAR_BTNS = (
        ('btn_new', _('New compare...'), 130),
        ('btn_swap', _('Swap sides'), 100),
        ('btn_refresh', _('Refresh'), 100),
    )

    # Row B (path edits) -- fixed widths, the right pair anchored to
    # the form's right edge (fixed widths instead of stretching anchors
    # avoid circular left/right anchor chains; the list takes the flex).
    PATH_W = 330
    BRW_W = 82

    # Row C (filters). NOTE 'act': True is required on every check
    # control (set in _build): without it CudaText does not fire
    # on_change when the user (un)checks the box -- the API doc:
    # "act: active state... control's value change fires events".
    FILTER_CHECKS = (
        ('chk_diff', _('Different'), True),
        ('chk_lonly', _('Only left'), True),
        ('chk_ronly', _('Only right'), True),
        ('chk_same', _('Identical'), True),
    )

    def __init__(self, cmd, dir_l, dir_r):
        self._cmd = cmd                 # Command object of cuda_differ2
        self._dir_l = os.path.normpath(dir_l)
        self._dir_r = os.path.normpath(dir_r)
        # Live state
        self._rows = []                 # all rows of the finished scan
        self._view = []                 # filtered+sorted rows on display
        self._worker = None             # running _Scanner or None
        self._last_fill = -1            # rows at the last progressive fill
        self._scan_t0 = 0.0
        self._sort_col = 1              # default: Folder, ascending
        self._sort_desc = False
        self._quick_only = bool(_get_opt('dirs.quick_only', False))
        self._method = _get_method()
        self._show = {                  # status filter checkboxes
            ST_DIFF: True, ST_LONLY: True, ST_RONLY: True, ST_SAME: True,
        }
        # Profiling state of the CURRENT scan (all None/False when the
        # config gate is off): the async-pair token + whether the
        # scanner thread started its own cProfile layer.
        self._prof_token = None
        self._prof_cprof_scan = False
        # Diff-tab hunk colors for the drawn rows (refreshed at every
        # start_scan from Command.cfg, which is reloaded there first).
        self._colors = {}
        # Dialog plumbing
        self.h = 0
        self.h_sb = 0
        self.h_list = 0                # listbox_ex handle (LISTBOX_*)
        self.h_imglist = 0
        self.ctl = {}                   # name -> control index
        self.ctl_rev = {}               # control index -> name
        self._icon_idx = {}             # icon name -> imagelist index
        self._lb_n = 0                  # items currently in the listbox
        self._lb_key = None             # (sort_col, desc, filter tuple)
                                            # the listbox was filled for
        # Teardown flags (each stage runs at most once)
        self._torn = False              # worker cancelled, timer stopped
        self._freed = False             # DLG_FREE scheduled/done
        self._app_exit = False          # teardown came from app exit

        self._build()
        self.show()
        self.start_scan()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _add(self, kind, name, prop):
        n = ct.dlg_proc(self.h, ct.DLG_CTL_ADD, kind)
        self.ctl[name] = n
        self.ctl_rev[n] = name
        prop = dict(prop)
        prop['name'] = name
        ct.dlg_proc(self.h, ct.DLG_CTL_PROP_SET, index=n, prop=prop)
        return n

    def _text_color(self):
        return _theme_color('TabFont', _theme_color('EdTextFont', 0x000000))

    def _build(self):
        self.h = ct.dlg_proc(0, ct.DLG_CREATE)
        h = self.h
        geom = self._restore_geom()
        prop = {
            'cap': _('Compare folders'),
            'w': self.DEF_W, 'h': self.DEF_H,
            'w_min': self.MIN_W, 'h_min': self.MIN_H,
            'border': ct.DBORDER_SIZE,
            'taskbar': 2,          # never a separate taskbar entry
            'keypreview': True,    # form-level Enter/Esc/F5 (on_key_down)
            'on_close': self._on_close,
            'on_key_down': self._on_key,
        }
        bg = _theme_color('TabBg', _theme_color('ListBg'))
        if bg is not None:
            prop['color'] = bg
        ct.dlg_proc(h, ct.DLG_PROP_SET, prop=prop)

        tcol = self._text_color()
        ed_bg = _theme_color('EdTextBg')
        ed_fg = _theme_color('EdTextFont')

        # -- Row A: toolbar buttons ------------------------------------
        prev = None
        for name, cap, w in self.TOOLBAR_BTNS:
            p = {
                'cap': cap, 'w': w, 'h': 26,
                'a_t': ('', '['), 'sp_t': 8,
                'sp_l': 10 if prev is None else 8,
                'on_change': self._on_button,
            }
            if prev is None:
                p['a_l'] = ('', '[')
            else:
                p['a_l'] = (prev, ']')
            self._add('button', name, p)
            prev = name
        self._add('button', 'btn_close', {
            'cap': _('Close'), 'w': 80, 'h': 26,
            'a_l': None, 'a_r': ('', ']'),
            'a_t': ('', '['), 'sp_t': 8, 'sp_r': 10,
            'on_change': self._on_button,
        })

        # -- Row B: the two sides' paths -------------------------------
        ed_prop = {'w': self.PATH_W, 'h': 26, 'a_t': ('btn_new', ']'),
                   'sp_t': 8}
        if ed_bg is not None:
            ed_prop['color'] = ed_bg
        if ed_fg is not None:
            ed_prop['font_color'] = ed_fg
        p = dict(ed_prop)
        p.update({'a_l': ('', '['), 'sp_l': 10,
                  'val': self._dir_l, 'texthint': _('left folder')})
        self._add('edit', 'ed_left', p)
        self._add('button', 'btn_lbrw', {
            'cap': _('Browse...'), 'w': self.BRW_W, 'h': 26,
            'a_l': ('ed_left', ']'), 'sp_l': 6,
            'a_t': ('btn_new', ']'), 'sp_t': 8,
            'on_change': self._on_button,
        })
        self._add('button', 'btn_rbrw', {
            'cap': _('Browse...'), 'w': self.BRW_W, 'h': 26,
            'a_l': None, 'a_r': ('', ']'), 'sp_r': 10,
            'a_t': ('btn_new', ']'), 'sp_t': 8,
            'on_change': self._on_button,
        })
        p = dict(ed_prop)
        p.update({'a_l': None, 'a_r': ('btn_rbrw', '['), 'sp_r': 6,
                  'val': self._dir_r, 'texthint': _('right folder')})
        self._add('edit', 'ed_right', p)

        # -- Row C: filters + subfolders + mask ------------------------
        prev = None
        for name, cap, checked in self.FILTER_CHECKS:
            p = {
                'cap': cap, 'val': '1' if checked else '0',
                'h': 20, 'autosize': True, 'w': 100,
                'a_t': ('ed_left', ']'), 'sp_t': 10,
                'font_color': tcol,
                'act': True,  # fire on_change on every (un)check
                'on_change': self._on_check,
            }
            if prev is None:
                p.update({'a_l': ('', '['), 'sp_l': 10})
            else:
                p.update({'a_l': (prev, ']'), 'sp_l': 12})
            self._add('check', name, p)
            prev = name
        self._add('check', 'chk_sub', {
            'cap': _('Subfolders'), 'val': '1',
            'h': 20, 'autosize': True, 'w': 100,
            'a_l': ('chk_same', ']'), 'sp_l': 24,
            'a_t': ('ed_left', ']'), 'sp_t': 10,
            'font_color': tcol,
            'act': True,      # without it the toggle would do nothing
            'on_change': self._on_check,
        })
        self._add('label', 'lab_mask', {
            'cap': _('Mask:'), 'h': 20, 'autosize': True, 'w': 44,
            'a_l': ('chk_sub', ']'), 'sp_l': 20,
            'a_t': ('ed_left', ']'), 'sp_t': 12,
            'font_color': tcol,
        })
        p = {'w': 170, 'h': 22, 'a_l': ('lab_mask', ']'), 'sp_l': 6,
             'a_t': ('ed_left', ']'), 'sp_t': 9,
             'texthint': _('*.py; *.txt')}
        if ed_bg is not None:
            p['color'] = ed_bg
        if ed_fg is not None:
            p['font_color'] = ed_fg
        self._add('edit', 'ed_mask', p)
        self._add('button', 'btn_apply', {
            'cap': _('Apply'), 'w': 70, 'h': 24,
            'a_l': ('ed_mask', ']'), 'sp_l': 6,
            'a_t': ('ed_left', ']'), 'sp_t': 8,
            'on_change': self._on_button,
        })

        # -- Statusbar (created before the list: the list anchors to it) --
        self._add('statusbar', 'sbar', {
            'h': 26,
            'a_l': ('', '['), 'a_r': ('', ']'), 'a_b': ('', ']'),
            'sp_l': 0, 'sp_r': 0, 'sp_b': 0,
        })
        self.h_sb = ct.dlg_proc(h, ct.DLG_CTL_HANDLE, name='sbar')
        try:
            ct.statusbar_proc(self.h_sb, ct.STATUSBAR_ADD_CELL, tag=1)
            ct.statusbar_proc(self.h_sb, ct.STATUSBAR_ADD_CELL, tag=2)
            ct.statusbar_proc(self.h_sb, ct.STATUSBAR_SET_CELL_SIZE,
                              tag=1, value=430)
            ct.statusbar_proc(self.h_sb, ct.STATUSBAR_SET_CELL_SIZE,
                              tag=2, value=560)
            sb_bg = _theme_color('StatusBg', _theme_color('ListBg'))
            sb_fg = _theme_color('StatusFont', _theme_color('ListFont'))
            if sb_bg is not None:
                ct.statusbar_proc(self.h_sb, ct.STATUSBAR_SET_COLOR_BACK,
                                  value=sb_bg)
            if sb_fg is not None:
                ct.statusbar_proc(self.h_sb, ct.STATUSBAR_SET_COLOR_FONT,
                                  value=sb_fg)
        except Exception:
            pass

        # -- The list (owner-drawn listbox_ex) ---------------------------
        # NOT a listview: the dialog API's listview has no per-row
        # colors, and full-line status colors are the point (see the
        # class docstring). listbox_ex + LISTBOX_SET_DRAWN hands the
        # painting to on_draw_item; the column header (sorting) comes
        # from LISTBOX_SET_HEADER over LISTBOX_SET_COLUMNS widths --
        # the same pixel widths _col_layout derives the cell offsets
        # from, so header and cells stay aligned.
        p = {
            'a_l': ('', '['), 'sp_l': 10,
            'a_r': ('', ']'), 'sp_r': 10,
            'a_t': ('ed_left', ']'), 'sp_t': 40,
            'a_b': ('sbar', '['), 'sp_b': 4,
            'on_click_dbl': self._on_list_dbl,
            'on_click_header': self._on_header,
            'on_menu': self._on_list_menu,
            'on_draw_item': self._on_draw_item,
        }
        li_bg = _theme_color('ListBg', ed_bg)
        li_fg = _theme_color('ListFont', ed_fg)
        if li_bg is not None:
            p['color'] = li_bg
        if li_fg is not None:
            p['font_color'] = li_fg
        self._add('listbox_ex', 'list', p)
        self.h_list = ct.dlg_proc(h, ct.DLG_CTL_HANDLE, name='list')
        try:
            ct.listbox_proc(self.h_list, ct.LISTBOX_SET_ITEM_H,
                            index=LIST_ITEM_H)
            ct.listbox_proc(self.h_list, ct.LISTBOX_SET_COLUMN_SEP,
                            text=COL_SEP)
            ct.listbox_proc(self.h_list, ct.LISTBOX_SET_COLUMNS,
                            text=self._col_spec())
            ct.listbox_proc(self.h_list, ct.LISTBOX_SET_HEADER,
                            text=self._header_text())
            # Owner-drawn ON after the header/columns exist: from here
            # the control paints nothing itself -- every visible row
            # arrives in _on_draw_item (the header keeps its built-in
            # themed painting; it is not affected by the drawn flag).
            ct.listbox_proc(self.h_list, ct.LISTBOX_SET_DRAWN, index=1)
        except Exception:
            # Pre-listbox_proc CudaText builds: the list degrades to a
            # plain (undrawn, single-column) listbox -- ugly but the
            # window still compares, filters and sorts.
            pass

        # Per-window imagelist (owned by the form -> freed with it).
        try:
            paths = _icon_paths()
            self.h_imglist = ct.imagelist_proc(0, ct.IMAGELIST_CREATE,
                                               value=self.h)
            for name in ('same', 'diff', 'lonly', 'ronly', 'folder', 'err'):
                idx = ct.imagelist_proc(self.h_imglist, ct.IMAGELIST_ADD,
                                        value=paths[name])
                self._icon_idx[name] = idx if idx is not None else -1
        except Exception:
            pass  # icons are decoration; the Status column carries the info

        # Scale the built layout to the OS DPI, then re-apply the saved
        # window geometry (saved values were captured post-scale, so
        # applying them after DLG_SCALE never double-scales).
        ct.dlg_proc(h, ct.DLG_SCALE)
        if geom:
            try:
                ct.dlg_proc(h, ct.DLG_PROP_SET, prop=geom)
            except Exception:
                pass

    def _restore_geom(self):
        # Stored as a single-line "x,y,w,h" STRING, not a list:
        # cudax_lib's flat-key settings updater only handles single-line
        # values (a list value written twice would corrupt the JSON --
        # the second update leaves the old multi-line block's orphan
        # lines behind).
        g = _get_opt('dirs.win_geom', '')
        if isinstance(g, str) and g:
            try:
                x, y, w, h = (int(v) for v in g.split(','))
                if w >= self.MIN_W and h >= self.MIN_H:
                    return {'x': x, 'y': y, 'w': w, 'h': h}
            except (TypeError, ValueError):
                pass
        return None

    def show(self):
        ct.dlg_proc(self.h, ct.DLG_SHOW_NONMODAL)

    # ------------------------------------------------------------------
    # List filling / drawing / sorting
    # ------------------------------------------------------------------

    def _header_text(self):
        """The listbox header line: column captions joined by COL_SEP,
        the sort column's caption carrying the direction marker (the
        header is rebuilt via LISTBOX_SET_HEADER whenever the sort
        changes -- see _apply_header)."""
        out = []
        for i, (cap, _align, _w) in enumerate(_LIST_COLUMNS):
            if i == self._sort_col:
                cap += _SORT_MARK[self._sort_desc]
            out.append(cap)
        return COL_SEP.join(out)

    def _apply_header(self):
        """Push the (possibly re-marked) header captions to the list."""
        if self._torn or not self.h_list:
            return
        try:
            ct.listbox_proc(self.h_list, ct.LISTBOX_SET_HEADER,
                            text=self._header_text())
        except Exception:
            pass

    @staticmethod
    def _col_spec():
        """Column widths for LISTBOX_SET_COLUMNS, in _LIST_COLUMNS
        order: 0 (= auto-stretch) for Name, the fixed pixel width for
        every other column. The header splits its captions over these
        same widths; _col_layout derives the drawn cells' offsets from
        the same table -- one source of truth for all three."""
        return [0] + [w for _cap, _align, w in _LIST_COLUMNS[1:]]

    def _col_layout(self, width):
        """Drawn-cell layout for a row 'width' pixels wide: a list of
        (x, w, align) per column, mirroring LISTBOX_SET_COLUMNS'
        semantics (fixed widths taken from the right edge of the given
        width; Name gets the remainder)."""
        fixed = [w for _cap, _align, w in _LIST_COLUMNS[1:]]
        name_w = max(60, width - sum(fixed) - 4)
        out = [(0, name_w, 'L')]
        x = name_w + 2
        for (_cap, align, w) in _LIST_COLUMNS[1:]:
            out.append((x, w - 6, align))
            x += w
        return out

    def _sort_key(self, r):
        c = self._sort_col
        if c == 0:
            return (r['name'].lower(), r['rel'].lower())
        if c == 1:
            return (r['dir'].lower(), r['name'].lower())
        if c == 2:
            return (STATUS_SEVERITY.get(r['status'], 99), r['rel'].lower())
        if c == 3:
            return (r['size_l'] if r['size_l'] is not None else -1,)
        if c == 4:
            return (r['mtime_l'] if r['mtime_l'] is not None else -1.0,)
        if c == 5:
            return (r['size_r'] if r['size_r'] is not None else -1,)
        if c == 6:
            return (r['mtime_r'] if r['mtime_r'] is not None else -1.0,)
        return (r['rel'].lower(),)

    @staticmethod
    def _row_cells(r):
        return (
            r['name'],
            r['dir'],
            STATUS_CAPTION.get(r['status'], r['status']),
            '' if r['isdir'] else _fmt_size(r['size_l']),
            _fmt_date(r['mtime_l']),
            '' if r['isdir'] else _fmt_size(r['size_r']),
            _fmt_date(r['mtime_r']),
        )

    def _filter_ok(self, r):
        st = r['status']
        if st == ST_ERR:
            return True  # always visible: rare and important
        if st in (ST_LONLY, ST_DIR_LONLY):
            return self._show[ST_LONLY]
        if st in (ST_RONLY, ST_DIR_RONLY):
            return self._show[ST_RONLY]
        if st == ST_DIFF:
            return self._show[ST_DIFF]
        return self._show[ST_SAME]  # ST_SAME and plain ST_DIR rows

    def _fill_list(self, rows=None):
        """Filter+sort 'rows' (default: the finished self._rows) into
        the owner-drawn list. The listbox only holds one caption string
        per row (the joined cells -- what a non-drawn fallback would
        show); the painted content comes from self._view, so keeping
        the two in sync is this method's whole job. Refills are
        INCREMENTAL while a scan streams rows in (same sort+filter ->
        the new view extends the old one -> only the delta is appended,
        one LISTBOX_ADD per new row); a sort or filter change rebuilds
        the list from scratch and restores the selection."""
        if self._torn or not self.h_list:
            return
        if rows is None:
            rows = self._rows
        t0 = time.perf_counter()
        view = [r for r in rows if self._filter_ok(r)]
        view.sort(key=self._sort_key)
        if self._sort_desc:
            view.reverse()

        key = (self._sort_col, self._sort_desc,
               tuple(sorted(self._show.items())))
        old_view = self._view
        keep_sel = self._sel_index()

        incremental = (
            key == self._lb_key and
            len(view) >= len(old_view) and
            all(view[i] is old_view[i] for i in range(len(old_view)))
        )
        try:
            if incremental:
                for r in view[self._lb_n:]:
                    ct.listbox_proc(self.h_list, ct.LISTBOX_ADD,
                                    index=-1, text=self._item_caption(r))
                self._lb_n = len(view)
                # Selection: an unchanged prefix keeps the selection
                # valid -- nothing to restore while streaming.
            else:
                ct.listbox_proc(self.h_list, ct.LISTBOX_DELETE_ALL)
                for r in view:
                    ct.listbox_proc(self.h_list, ct.LISTBOX_ADD,
                                    index=-1, text=self._item_caption(r))
                self._lb_n = len(view)
                self._lb_key = key
                if 0 <= keep_sel < self._lb_n:
                    ct.listbox_proc(self.h_list, ct.LISTBOX_SET_SEL,
                                    index=keep_sel)
        except Exception:
            pass
        self._view = view
        if Profiler.is_enabled():
            Profiler.mark('dirs:ui_fill_list', time.perf_counter() - t0,
                          len(view) - len(old_view) if incremental
                          else len(view))

    @staticmethod
    def _item_caption(r):
        """The listbox item string of a row: cells joined by COL_SEP.
        The drawn list never shows it (the painter draws the cells one
        by one); it exists for the non-drawn fallback and for
        copy/paste friendliness of the raw control content."""
        return COL_SEP.join(DirCompareForm._row_cells(r))

    # ------------------------------------------------------------------
    # Row painting (owner-drawn listbox_ex)
    # ------------------------------------------------------------------

    def _on_draw_item(self, id_dlg, id_ctl, data='', info=''):
        """LISTBOX_SET_DRAWN painter: draws one row -- background
        (status color: the SAME colors the diff tabs paint their hunks
        with), status icon, and the seven cells at _col_layout offsets.

        Runs inside the control's paint cycle: only canvas_proc /
        imagelist_proc calls here (paint-only, no re-entrant repaints),
        and it must stay fast (every repaint of every visible row goes
        through here -- ~15 API calls per row).
        """
        try:
            index = int(data.get('index', -1))
            rect = data.get('rect')
            canvas = data.get('canvas')
            if canvas is None or rect is None:
                return
            if not (0 <= index < len(self._view)):
                return
            row = self._view[index]
        except Exception:
            return
        x0, y0, x1, y1 = rect
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            return

        st = row['status']
        t0 = time.perf_counter() if Profiler.is_enabled() else 0.0

        # -- background: selected > status color > plain list bg ------
        try:
            sel = ct.listbox_proc(self.h_list, ct.LISTBOX_GET_SEL)
        except Exception:
            sel = -1
        col = self._colors
        if index == sel:
            bg = col.get('sel_bg')
            fg = col.get('sel_font')
        else:
            bg = col.get(ST_COLOR_KEY.get(st, ''), )
            fg = col.get('font')
        if bg is None:
            bg = col.get('bg', 0xF0F0F0)
        if fg is None:
            fg = 0x000000

        try:
            ct.canvas_proc(canvas, ct.CANVAS_SET_BRUSH, color=bg,
                           style=ct.BRUSH_SOLID)
            ct.canvas_proc(canvas, ct.CANVAS_RECT_FILL,
                           x=x0, y=y0, x2=x1, y2=y1)
        except Exception:
            return

        # -- status icon in the gutter before the Name column ---------
        icon = _ICON_OF.get(st)
        if icon is not None:
            idx = self._icon_idx.get(icon, -1)
            if idx is not None and idx >= 0 and self.h_imglist:
                try:
                    ct.imagelist_proc(
                        self.h_imglist, ct.IMAGELIST_PAINT,
                        value=(canvas, x0 + 3, y0 + (h - 16) // 2, idx))
                except Exception:
                    pass

        # -- cells ------------------------------------------------------
        cells = self._row_cells(row)
        layout = self._col_layout(w)
        try:
            ct.canvas_proc(canvas, ct.CANVAS_SET_FONT, text='default',
                           color=fg, style=0)
            # one measure for the vertical centering baseline
            sz = ct.canvas_proc(canvas, ct.CANVAS_GET_TEXT_SIZE,
                                text='Ag')
            ty = y0 + max(0, (h - (sz[1] if sz else 13)) // 2)
        except Exception:
            ty = y0 + 5
            sz = None
        for text, (cx, cw, align) in zip(cells, layout):
            if not text or cw <= 4:
                continue
            try:
                tw = ct.canvas_proc(canvas, ct.CANVAS_GET_TEXT_SIZE,
                                    text=text)[0]
                # cheap ellipsis: only names/dates ever overflow; the
                # text is trimmed in ~25% steps until it fits (a few
                # measures at most, and only for overflowing cells).
                while tw > cw and len(text) > 4:
                    cut = max(4, (len(text) * 3) // 4)
                    text = text[:cut - 1] + '\u2026'
                    tw = ct.canvas_proc(canvas, ct.CANVAS_GET_TEXT_SIZE,
                                        text=text)[0]
                tx = cx + 4 if align != 'R' else cx + cw - 4 - tw
                ct.canvas_proc(canvas, ct.CANVAS_TEXT, text=text,
                               x=x0 + tx, y=ty)
            except Exception:
                continue

        if Profiler.is_enabled():
            Profiler.mark('dirs:ui_draw_item', time.perf_counter() - t0)

    def _update_counts(self):
        rows = self._rows
        n_diff = sum(1 for r in rows if r['status'] == ST_DIFF)
        n_l = sum(1 for r in rows
                  if r['status'] in (ST_LONLY, ST_DIR_LONLY))
        n_r = sum(1 for r in rows
                  if r['status'] in (ST_RONLY, ST_DIR_RONLY))
        n_same = sum(1 for r in rows
                     if r['status'] in (ST_SAME, ST_DIR))
        self._sb_text(2, _('Different: {}   Only left: {}   '
                           'Only right: {}   Identical: {}').format(
                               n_diff, n_l, n_r, n_same))

    def _sb_text(self, tag, text):
        if self.h_sb:
            try:
                ct.statusbar_proc(self.h_sb, ct.STATUSBAR_SET_CELL_TEXT,
                                  tag=tag, value=text)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _ctl_val(self, name):
        try:
            return ct.dlg_proc(self.h, ct.DLG_CTL_PROP_GET,
                               name=name).get('val', '')
        except Exception:
            return ''

    def _set_ctl_val(self, name, val):
        try:
            ct.dlg_proc(self.h, ct.DLG_CTL_PROP_SET, name=name,
                        prop={'val': val})
        except Exception:
            pass

    def start_scan(self):
        """(Re)start the comparison for the two paths currently in the
        path edits. A still-running scan is cancelled first -- its
        worker keeps finishing its current file in the background (as a
        daemon, harmless) while the new worker takes over the timer;
        its profiling pieces are abandoned without a report (the new
        scan resets the profiler anyway when it owns the batch)."""
        if self._torn or not self.h:
            return
        pl = self._ctl_val('ed_left').strip().strip('"').strip("'")
        pr = self._ctl_val('ed_right').strip().strip('"').strip("'")
        if not os.path.isdir(pl) or not os.path.isdir(pr):
            ct.msg_box(_('Folder not found:\n{}\n\nFolder not found:\n{}'
                         ).format(pl, pr)
                       if not os.path.isdir(pl) and not os.path.isdir(pr)
                       else _('Folder not found:\n{}').format(
                           pl if not os.path.isdir(pl) else pr),
                       ct.MB_OK + ct.MB_ICONWARNING)
            return
        self._dir_l = os.path.normpath(pl)
        self._dir_r = os.path.normpath(pr)
        self._quick_only = bool(_get_opt('dirs.quick_only', False))
        self._method = _get_method()

        if self._worker is not None:
            self._worker.cancel()
            _prof_abandon_scan(self._cmd, self._prof_token, self._worker)
            self._prof_token = None
        recursive = self._ctl_val('chk_sub') == '1'
        mask = _split_mask(self._ctl_val('ed_mask'))

        # Profiling gate + colors: _prof_begin_scan reloads the Command
        # config FIRST (the same order as refresh_compare), so both the
        # enable_profiling/enable_cprofile gates and the hunk colors
        # below see the CURRENT options -- also on the very first scan
        # after changing them.
        token, cprof_scan = _prof_begin_scan(self._cmd)
        self._prof_token = token
        self._prof_cprof_scan = cprof_scan
        # Colors for the drawn rows: the diff-tab hunk colors (the
        # painter looks them up by the cfg-key names ST_COLOR_KEY
        # maps statuses to) + the themed list/selection colors. A
        # None hunk color (cannot resolve) simply leaves those rows
        # uncolored -- never an error.
        cfg = getattr(self._cmd, 'cfg', None) or {}
        self._colors = {
            'sel_bg': _theme_color('ListSelBg'),
            'sel_font': _theme_color('ListSelFont'),
            'bg': _theme_color('ListBg', 0xF0F0F0),
            'font': _theme_color('ListFont', 0x000000),
        }
        for k in ('color_changed', 'color_deleted', 'color_added'):
            v = cfg.get(k)
            if isinstance(v, int):
                self._colors[k] = v

        self._worker = _Scanner(self._dir_l, self._dir_r, recursive,
                                mask, self._quick_only, self._method,
                                cprof_scan)
        self._rows = []
        self._view = []
        self._lb_n = 0
        self._lb_key = None
        self._last_fill = -1
        self._scan_t0 = time.perf_counter()
        try:
            ct.dlg_proc(self.h, ct.DLG_PROP_SET, prop={
                'cap': '{}: {} {} {}'.format(
                    _('Compare folders'),
                    os.path.basename(self._dir_l) or self._dir_l,
                    '\u2194',
                    os.path.basename(self._dir_r) or self._dir_r),
            })
        except Exception:
            pass
        self._sb_text(1, _('Comparing...'))
        self._sb_text(2, '')
        self._fill_list()          # empty the list right away
        self._worker.start()
        ct.timer_proc(ct.TIMER_START, self._on_timer, POLL_MS)

    def _on_timer(self, tag='', info=''):
        """Main-thread poll of the worker (timer_proc): refresh the
        progress line, stream rows into the list, finish up."""
        t0 = time.perf_counter() if Profiler.is_enabled() else 0.0
        if self._torn:
            self._stop_timer()
            return
        w = self._worker
        if w is None:
            return
        rows, total, finished = w.snapshot()
        if w.fatal:
            self._stop_timer()
            self._rows = rows
            self._fill_list()
            self._update_counts()
            self._sb_text(1, _('Scan failed: {}').format(w.fatal))
            _prof_abandon_scan(self._cmd, self._prof_token, w)
            self._prof_token = None
            return
        if not finished:
            if rows:
                self._sb_text(1, _('Comparing... {} / {}').format(
                    len(rows), total if total else '?'))
            else:
                # Walk phase: no rows exist yet (rows are built from
                # the merged listings of BOTH trees), so the progress
                # line counts what the walk has done instead -- the
                # window then never looks stuck during a slow listing.
                self._sb_text(1, _('Scanning folders... {} listed').format(
                    getattr(w, 'scanned_dirs', 0)))
            # Rows only ever grow during a scan, and _fill_list is
            # INCREMENTAL (append-delta, same sort+filter): filling on
            # every tick that has new rows is cheap and makes small
            # trees (a handful of rows) appear as they are decided
            # instead of all at once at the end. The timer period
            # (POLL_MS) bounds the refill rate for huge trees.
            if len(rows) != self._last_fill:
                self._last_fill = len(rows)
                self._fill_list(rows)
            if Profiler.is_enabled():
                Profiler.mark('dirs:ui_timer_tick',
                              time.perf_counter() - t0)
            return
        # Finished (normally or cancelled): last full update.
        self._stop_timer()
        if Profiler.is_enabled() and getattr(w, 'run_end', None):
            # Wall time from the worker's real end to THIS tick: the
            # timer quantum + whatever kept the main thread busy --
            # with dirs:spawn_lag this closes the loop on where every
            # millisecond of dirs:scan_wall went.
            try:
                lag = time.perf_counter() - w.run_end
                Profiler.mark('dirs:finish_lag', lag, 1, lag)
            except Exception:
                pass
        self._rows = rows
        self._last_fill = -1
        self._fill_list()
        self._update_counts()
        elapsed = time.perf_counter() - self._scan_t0
        if w.cancelled():
            msg = _('Cancelled') if not rows else \
                _('Cancelled ({} of {} rows)').format(len(rows), total)
            _prof_abandon_scan(self._cmd, self._prof_token, w)
        else:
            msg = _('Done in {:.1f} s').format(elapsed)
            if w.walk_errors:
                msg += '  ' + _('({} folders unreadable)').format(
                    len(w.walk_errors))
            # Profiling epilogue at the natural end: section report
            # first, then the two cProfile reports (main + scanner
            # thread), then hand the profiler back if we took it.
            _prof_finish_scan(self._cmd, self._prof_token, w,
                              self._dir_l, self._dir_r)
        self._prof_token = None
        self._sb_text(1, msg)
        if Profiler.is_enabled():
            Profiler.mark('dirs:ui_timer_tick', time.perf_counter() - t0)

    def _stop_timer(self):
        # NOTE: the interval argument is REQUIRED by timer_proc's
        # signature even on TIMER_STOP (where the app ignores it) --
        # the plugin's other modules always pass it too.
        try:
            ct.timer_proc(ct.TIMER_STOP, self._on_timer, POLL_MS)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _on_button(self, id_dlg, id_ctl, data='', info=''):
        name = self.ctl_rev.get(id_ctl, '')
        if name == 'btn_new':
            cur_l = self._ctl_val('ed_left')
            cur_r = self._ctl_val('ed_right')
            res = _PickerDialog().show(cur_l, cur_r)
            if res:
                compare_directories(self._cmd, res[0], res[1])
        elif name == 'btn_swap':
            vl = self._ctl_val('ed_left')
            vr = self._ctl_val('ed_right')
            self._set_ctl_val('ed_left', vr)
            self._set_ctl_val('ed_right', vl)
            self.start_scan()
        elif name == 'btn_refresh':
            self.start_scan()
        elif name == 'btn_close':
            self.close()
        elif name == 'btn_apply':
            self.start_scan()
        elif name in ('btn_lbrw', 'btn_rbrw'):
            side = 'ed_left' if name == 'btn_lbrw' else 'ed_right'
            cur = self._ctl_val(side).strip().strip('"').strip("'")
            init = cur if os.path.isdir(cur) else os.path.expanduser('~')
            path = ct.dlg_dir(init, _('Choose folder'))
            if path:
                self._set_ctl_val(side, path)
                self.start_scan()

    def _on_check(self, id_dlg, id_ctl, data='', info=''):
        name = self.ctl_rev.get(id_ctl, '')
        mapping = {
            'chk_diff': ST_DIFF, 'chk_lonly': ST_LONLY,
            'chk_ronly': ST_RONLY, 'chk_same': ST_SAME,
        }
        if name in mapping:
            self._show[mapping[name]] = self._ctl_val(name) == '1'
            self._fill_list()
        elif name == 'chk_sub':
            self.start_scan()

    def _on_key(self, id_dlg, id_ctl, data='', info=''):
        """Form-level keys (keypreview=True). id_ctl is the key code.
        Esc closes, F5 rescans, Enter opens the selected row -- Enter
        only when the list itself is focused, so it keeps its normal
        meaning on buttons and edits."""
        if id_ctl == 27:  # Esc
            self.close()
            return False
        if id_ctl == 116:  # F5
            self.start_scan()
            return False
        if id_ctl == 13:  # Enter
            try:
                focused = ct.dlg_proc(self.h, ct.DLG_PROP_GET).get(
                    'focused', -1)
                if focused == self.ctl.get('list', -2):
                    self._open_selected()
                    return False
            except Exception:
                pass
        return True

    def _on_header(self, id_dlg, id_ctl, data='', info=''):
        """Column header click: sort by that column; clicking again
        toggles the direction (the rebuilt header carries the marker,
        aligned with the drawn cells -- same width table)."""
        try:
            col = int(data)
        except (TypeError, ValueError):
            return
        if col == self._sort_col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = False
        self._fill_list()
        self._apply_header()

    def _sel_index(self):
        try:
            i = ct.listbox_proc(self.h_list, ct.LISTBOX_GET_SEL)
            return int(i) if i is not None else -1
        except Exception:
            return -1

    def _on_list_dbl(self, id_dlg, id_ctl, data='', info=''):
        self._open_selected()

    def _open_selected(self):
        idx = self._sel_index()
        if 0 <= idx < len(self._view):
            self._open_row(self._view[idx])

    # ------------------------------------------------------------------
    # Row actions (double-click + context menu)
    # ------------------------------------------------------------------

    def _path(self, row, side):
        base = self._dir_l if side == 'l' else self._dir_r
        return os.path.join(base, row['rel'])

    def _open_row(self, row):
        """Double-click semantics. File pairs (Identical or Different)
        open in a Differ 2 compare tab via the Command object; one-sided
        files open alone; folder pairs open a drill-down compare window;
        one-sided folders open in the OS file manager. The whole handoff
        (opening the two editor tabs + set_files' own refresh) is one
        profiler section -- its rows show what the double-click costs
        on top of the tab compare's own refresh:* rows."""
        st = row['status']
        pl = self._path(row, 'l')
        pr = self._path(row, 'r')
        if st in (ST_SAME, ST_DIFF):
            if os.path.isfile(pl) and os.path.isfile(pr):
                Profiler.start('dirs:open_compare_pair')
                try:
                    self._cmd.open_compare_pair(pl, pr)
                finally:
                    Profiler.stop()
            else:
                ct.msg_status(_('File changed on disk -- press Refresh'))
        elif st == ST_LONLY:
            if os.path.isfile(pl):
                ct.file_open(pl)
        elif st == ST_RONLY:
            if os.path.isfile(pr):
                ct.file_open(pr)
        elif st == ST_DIR:
            compare_directories(self._cmd, pl, pr)
        elif st == ST_DIR_LONLY:
            _open_in_file_manager(pl)
        elif st == ST_DIR_RONLY:
            _open_in_file_manager(pr)
        elif st == ST_ERR:
            if os.path.isfile(pl):
                ct.file_open(pl)
            elif os.path.isfile(pr):
                ct.file_open(pr)

    def _on_list_menu(self, id_dlg, id_ctl, data='', info=''):
        """Right-click context menu on the selected row."""
        if self._torn:
            return
        idx = self._sel_index()
        if not (0 <= idx < len(self._view)):
            return
        row = self._view[idx]
        st = row['status']
        has_l = st not in (ST_RONLY, ST_DIR_RONLY)
        has_r = st not in (ST_LONLY, ST_DIR_LONLY)
        isdir = row['isdir']
        # Copying a folder makes sense only when the other side lacks it
        # (copytree cannot merge into an existing tree).
        copy_r_ok = has_l and not (isdir and has_r)
        copy_l_ok = has_r and not (isdir and has_l)
        pl = self._path(row, 'l')
        pr = self._path(row, 'r')

        hm = ct.menu_proc(0, ct.MENU_CREATE)

        def add(cap, fn, enabled=True):
            mi = ct.menu_proc(hm, ct.MENU_ADD, caption=cap, command=fn)
            if not enabled:
                try:
                    ct.menu_proc(mi, ct.MENU_SET_ENABLED, command=False)
                except Exception:
                    pass

        if not isdir:
            add(_('Compare (open in diff tab)'),
                lambda: self._open_row(row),
                st in (ST_SAME, ST_DIFF, ST_ERR))
            ct.menu_proc(hm, ct.MENU_ADD, caption='-')
        add(_('Copy to right'), lambda: self._act_copy(row, True), copy_r_ok)
        add(_('Copy to left'), lambda: self._act_copy(row, False), copy_l_ok)
        ct.menu_proc(hm, ct.MENU_ADD, caption='-')
        add(_('Delete from left'), lambda: self._act_delete(row, 'l'), has_l)
        add(_('Delete from right'), lambda: self._act_delete(row, 'r'),
            has_r)
        ct.menu_proc(hm, ct.MENU_ADD, caption='-')
        if isdir:
            if has_l:
                add(_('Show left folder in file manager'),
                    lambda: _open_in_file_manager(pl))
            if has_r:
                add(_('Show right folder in file manager'),
                    lambda: _open_in_file_manager(pr))
        else:
            if has_l:
                add(_('Open left file'), lambda: ct.file_open(pl))
            if has_r:
                add(_('Open right file'), lambda: ct.file_open(pr))
            if has_l:
                add(_('Show left file in file manager'),
                    lambda: _open_in_file_manager(pl))
            if has_r:
                add(_('Show right file in file manager'),
                    lambda: _open_in_file_manager(pr))
        ct.menu_proc(hm, ct.MENU_ADD, caption='-')
        if has_l:
            add(_('Copy left path'),
                lambda: ct.app_proc(ct.PROC_SET_CLIP, pl))
        if has_r:
            add(_('Copy right path'),
                lambda: ct.app_proc(ct.PROC_SET_CLIP, pr))

        pos = None
        try:
            if isinstance(data, dict):
                sx, sy = ct.dlg_proc(
                    self.h, ct.DLG_COORD_LOCAL_TO_SCREEN,
                    index=int(data.get('x', 0)), index2=int(data.get('y', 0)))
                pos = (sx, sy)
        except Exception:
            pos = None
        try:
            ct.menu_proc(hm, ct.MENU_SHOW, command=pos if pos else '')
        except Exception:
            pass

    def _confirm(self, text):
        if not bool(_get_opt('dirs.confirm_ops', True)):
            return True
        return ct.msg_box(text, ct.MB_YESNO + ct.MB_ICONWARNING) == \
            ct.ID_YES

    def _act_copy(self, row, to_right):
        """Copy the row's item to the other side. Files are copied with
        copy2 (mtime preserved -- future compares see the copy as
        identical without hashing the middle), folders with copytree
        (only into a not-yet-existing destination)."""
        src = self._path(row, 'l' if to_right else 'r')
        dst = self._path(row, 'r' if to_right else 'l')
        if not os.path.exists(src):
            ct.msg_status(_('Source disappeared -- press Refresh'))
            return
        if row['isdir']:
            if os.path.exists(dst):
                ct.msg_box(_('Cannot copy: the folder already exists on '
                             'the other side:\n{}').format(dst),
                           ct.MB_OK + ct.MB_ICONWARNING)
                return
            if not self._confirm(_('Copy the whole folder?\n{}\n->\n{}'
                                   ).format(src, dst)):
                return
            try:
                shutil.copytree(src, dst)
            except Exception as ex:
                ct.msg_box(_('Copy failed:\n{}\n\n{}').format(src, ex),
                           ct.MB_OK + ct.MB_ICONERROR)
                return
            ct.msg_status(_('Folder copied'))
            self.start_scan()  # the tree shape changed
        else:
            if os.path.exists(dst) and not self._confirm(
                    _('Overwrite the file on the other side?\n{}').format(
                        dst)):
                return
            try:
                ddir = os.path.dirname(dst)
                if ddir and not os.path.isdir(ddir):
                    os.makedirs(ddir, exist_ok=True)
                shutil.copy2(src, dst)
            except Exception as ex:
                ct.msg_box(_('Copy failed:\n{}\n\n{}').format(src, ex),
                           ct.MB_OK + ct.MB_ICONERROR)
                return
            ct.msg_status(_('File copied'))
            self._refresh_row(row)  # patch just this row in place

    def _act_delete(self, row, side):
        path = self._path(row, side)
        if not os.path.exists(path):
            ct.msg_status(_('Already gone -- press Refresh'))
            return
        if not self._confirm(_('Delete {}?').format(path)):
            return
        try:
            if row['isdir']:
                shutil.rmtree(path)
            else:
                os.remove(path)
        except Exception as ex:
            ct.msg_box(_('Delete failed:\n{}\n\n{}').format(path, ex),
                       ct.MB_OK + ct.MB_ICONERROR)
            return
        ct.msg_status(_('Deleted'))
        if row['isdir']:
            self.start_scan()
        else:
            self._refresh_row(row)

    def _refresh_row(self, row):
        """Re-evaluate one file row after a copy/delete action: re-stat
        both sides, re-run the same compare (method included) the
        scanner uses, and patch the row in place (no full rescan). A
        row that vanished from both sides is dropped."""
        rel = row['rel']
        sl = _stat_side(os.path.join(self._dir_l, rel))
        sr = _stat_side(os.path.join(self._dir_r, rel))
        if sl is None and sr is None:
            self._rows = [r for r in self._rows
                          if r['isdir'] or r['rel'] != rel]
        else:
            new_row = _file_row(self._dir_l, self._dir_r, rel, sl, sr,
                                self._quick_only, self._method)
            for i, r in enumerate(self._rows):
                if not r['isdir'] and r['rel'] == rel:
                    self._rows[i] = new_row
                    break
            else:
                self._rows.append(new_row)
        self._fill_list()
        self._update_counts()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _on_close(self, id_dlg, id_ctl, data='', info=''):
        """User closed the window (X button / Alt+F4 / Esc)."""
        self._teardown()

    def close(self):
        """Programmatic close (the Close button)."""
        if self._torn or not self.h:
            return
        try:
            ct.dlg_proc(self.h, ct.DLG_HIDE)
        except Exception:
            pass
        self._teardown()

    def _teardown(self):
        if self._torn:
            return
        self._torn = True
        if self._worker is not None:
            self._worker.cancel()
        # The scan this window started (if any) is abandoned: its
        # profiler pieces are cancelled without a report, exactly like
        # the tab compare's cancel path -- closing a window must never
        # print a half-run report.
        if self._prof_token is not None or self._prof_cprof_scan:
            _prof_abandon_scan(self._cmd, self._prof_token,
                               self._worker)
            self._prof_token = None
            self._prof_cprof_scan = False
        self._stop_timer()
        # Remember the window geometry -- a single-line "x,y,w,h"
        # string (lists would corrupt the settings file on the second
        # write, see _restore_geom).
        if not self._app_exit and self.h:
            try:
                d = ct.dlg_proc(self.h, ct.DLG_PROP_GET)
                _set_opt('dirs.win_geom',
                         ','.join(str(d.get(k, 0))
                                  for k in ('x', 'y', 'w', 'h')))
            except Exception:
                pass
        _unregister_form(self)
        self._free_later()

    def _free_later(self):
        """DLG_FREE deferred to a one-shot timer: on_close runs inside
        the form's own close chain, and freeing a form from its own
        callback is the re-entrancy the menu-close-first convention
        avoids (see toolbar.py)."""
        if self._freed:
            return
        self._freed = True
        if self._app_exit:
            return  # CudaText frees all plugin forms on exit itself

        def do_free(tag='', info=''):
            try:
                ct.dlg_proc(self.h, ct.DLG_FREE)
            except Exception:
                pass
        try:
            ct.timer_proc(ct.TIMER_START_ONE, do_free, 50)
        except Exception:
            try:
                ct.dlg_proc(self.h, ct.DLG_FREE)
            except Exception:
                pass

    def notify_app_exit(self):
        """App is exiting (Command.on_exit_pre -> close_all): stop the
        worker and the timer; no dialog calls -- the app destroys the
        forms itself. The scan is abandoned (profiling pieces cancelled
        without a report, like every other abandonment path)."""
        self._app_exit = True
        if self._worker is not None:
            self._worker.cancel()
        if self._prof_token is not None or self._prof_cprof_scan:
            _prof_abandon_scan(self._cmd, self._prof_token, self._worker)
            self._prof_token = None
            self._prof_cprof_scan = False
        self._stop_timer_safe()
        self._torn = True
        self._freed = True

    def _stop_timer_safe(self):
        try:
            ct.timer_proc(ct.TIMER_STOP, self._on_timer, POLL_MS)
        except Exception:
            pass


# ----------------------------------------------------------------------
# Icon cache
# ----------------------------------------------------------------------

def _icon_paths():
    """Write the embedded 16x16 PNGs into settings/cuda_differ2_icons/
    (once -- the directory is a cache, versioned by ICON_CACHE_VER so a
    future icon update renews it) and return {name: path}. The
    imagelist API loads icons from files only, hence the cache."""
    d = os.path.join(ct.app_path(ct.APP_DIR_SETTINGS),
                     'cuda_differ2_icons')
    ver_file = os.path.join(d, '.v' + ICON_CACHE_VER)
    fresh = os.path.exists(ver_file)
    out = {}
    try:
        if not fresh:
            os.makedirs(d, exist_ok=True)
        for name, b64 in _ICONS_PNG.items():
            p = os.path.join(d, name + '.png')
            if not fresh or not os.path.exists(p) or \
                    os.path.getsize(p) == 0:
                with open(p, 'wb') as f:
                    f.write(base64.b64decode(b64))
            out[name] = p
        if not fresh:
            with open(ver_file, 'w') as f:
                f.write(ICON_CACHE_VER)
    except OSError:
        # Read-only settings dir: keep whatever paths exist; a missing
        # icon just means no icon on that row (IMAGELIST_ADD fails ->
        # index -1), never an error for the user.
        for name in _ICONS_PNG:
            p = os.path.join(d, name + '.png')
            if os.path.exists(p):
                out.setdefault(name, p)
    return out


# ----------------------------------------------------------------------
# Module API (used by cuda_differ2/__init__.py)
# ----------------------------------------------------------------------

# All live compare windows. The list keeps the DirCompareForm objects
# (and through them the timer callbacks registered in cudatext's _live
# storage) referenced for as long as their forms live.
_forms = []


def _register_form(form):
    if form not in _forms:
        _forms.append(form)


def _unregister_form(form):
    try:
        _forms.remove(form)
    except ValueError:
        pass


def compare_dialog(cmd, dir_l='', dir_r=''):
    """Menu entry: ask for two folders (history-remembered picker),
    then open a compare window. The picker is a profiler section of
    its own ('dirs:picker') -- a slow-to-open folder chooser would
    otherwise hide inside the operation's wall time."""
    Profiler.start('dirs:picker')
    try:
        res = _PickerDialog().show(dir_l, dir_r)
    finally:
        Profiler.stop()
    if res:
        compare_directories(cmd, res[0], res[1])


def compare_directories(cmd, dir_l, dir_r):
    """Open a NEW compare window for the two folders (any number of
    windows can be open at once). Called by the picker, by the CLI
    handler (Command.on_cli with two folders) and by drill-downs from
    an existing window's folder rows."""
    dir_l = os.path.normpath(dir_l)
    dir_r = os.path.normpath(dir_r)
    if not os.path.isdir(dir_l) or not os.path.isdir(dir_r):
        ct.msg_box(_('Folder not found:\n{}\n\nFolder not found:\n{}'
                     ).format(dir_l, dir_r)
                   if not os.path.isdir(dir_l) and not os.path.isdir(dir_r)
                   else _('Folder not found:\n{}').format(
                       dir_l if not os.path.isdir(dir_l) else dir_r),
                   ct.MB_OK + ct.MB_ICONWARNING)
        return
    if os.path.normcase(dir_l) == os.path.normcase(dir_r):
        ct.msg_box(_('The two folders are the same folder.'),
                   ct.MB_OK + ct.MB_ICONWARNING)
        return
    try:
        Profiler.start('dirs:form_build')
        try:
            form = DirCompareForm(cmd, dir_l, dir_r)
        finally:
            Profiler.stop()
    except Exception as ex:
        ct.msg_box(_('Cannot open the folder compare window:\n\n{}'
                     ).format(ex), ct.MB_OK + ct.MB_ICONERROR)
        return
    _register_form(form)


def close_all():
    """Command.on_exit_pre: stop every scan worker and timer. No dialog
    calls here -- CudaText destroys the plugin's forms itself."""
    for form in list(_forms):
        try:
            form.notify_app_exit()
        except Exception:
            pass
    del _forms[:]
