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
never runs a diff algorithm; for every file present on both sides it
applies the cheapest test that decides the question, in this order
(WinMerge-like "quick compare", but cheaper):

  1. sizes differ            -> Different   (no read at all)
  2. both empty              -> Identical   (no read at all)
  3. size <= SMALL_FILE_FULL -> one MD5 of each file's full content
     (a single sequential read per side; covers typical source files)
  4. otherwise: "quick hash" = MD5 of the first + last HEAD_TAIL_CHUNK
     bytes of each side. Quick hashes differ -> Different (two 128 KB
     reads at most -- a different head or tail is caught without
     touching the middle of the file).
  5. quick hashes match: either accept it as Identical (the
     "Fast (sampling) compare" option, differ2.dirs.quick_only -- the
     speed-over-certainty mode), or finish the job with a full MD5 of
     both sides so an Identical verdict is always content-proven
     (default).

Files that exist on one side only need no content test at all. MD5 is
used for speed (this is change detection, not security); a collision
would need two files engineered to collide, and even then the worst
case is a wrong "Identical" badge that the double-click diff corrects.

== Threading model ==================================================

The scan runs on a daemon thread that touches ONLY the file system
and its own state -- never the CudaText API (which is main-thread
only). A per-window poll timer (timer_proc, ~200 ms) snapshots the
worker's results under its lock and updates the list view on the main
thread, so huge trees stream into the window while it stays fully
responsive; closing the window or starting a rescan simply sets a
cancel flag the worker checks between every file. Several compare
windows can run scans at once -- each owns one worker and one timer.

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
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time

import cudatext as ct
import cudax_lib as ctx
from cudax_lib import get_translation

_ = get_translation(__file__)  # I18N

# The plugin's user settings file (settings/<name>.json), the same file
# every other module of cuda_differ2 reads/writes via cudax_lib.
MODULE_JSON = 'cuda_differ2.json'

# Compare-speed constants (see the module docstring's "Speed model"):
SMALL_FILE_FULL = 256 * 1024   # up to this size: one full-content MD5
HEAD_TAIL_CHUNK = 64 * 1024    # bigger: MD5 of head + tail chunks first

HISTORY_MAX = 12               # remembered folder paths per side
POLL_MS = 200                  # worker -> UI poll period while scanning
PARTIAL_FILL_STEP = 400        # new rows between two progressive refills
ICON_CACHE_VER = '1'           # bump to force-renew the icon cache dir

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
# Hashing (the cheap "quick compare" primitives)
# ----------------------------------------------------------------------

def _md5_file(path):
    """Full-content MD5 of a file, read in 1 MB chunks."""
    h = hashlib.md5()
    with open(path, 'rb') as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.digest()


def _quick_hash(path, size):
    """Sampling hash of a big file: MD5 over the first and the last
    HEAD_TAIL_CHUNK bytes (a single read covers files smaller than the
    chunk, which then degenerates to a full-content hash). 'size' is the
    stat() size the sampling layout is computed from."""
    h = hashlib.md5()
    with open(path, 'rb') as f:
        h.update(f.read(HEAD_TAIL_CHUNK))
        if size > HEAD_TAIL_CHUNK:
            f.seek(size - HEAD_TAIL_CHUNK)
            h.update(f.read(HEAD_TAIL_CHUNK))
    return h.digest()


def _contents_equal(pl, pr, size, quick_only):
    """Content test for two same-sized files (the caller already knows
    the sizes match and are > 0). Applies the tiered strategy from the
    module docstring; returns True/False, or None when a side cannot be
    read (the caller turns that into ST_ERR)."""
    try:
        if size <= SMALL_FILE_FULL:
            return _md5_file(pl) == _md5_file(pr)
        if _quick_hash(pl, size) != _quick_hash(pr, size):
            return False
        if quick_only:
            return True
        return _md5_file(pl) == _md5_file(pr)
    except OSError:
        return None


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
    """(size, mtime) of 'path', None when it is missing/unreadable."""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime)
    except OSError:
        return None


def _file_row(dir_l, dir_r, rel, sl, sr, quick_only):
    """Row for the file 'rel' given per-side (size, mtime) or None."""
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
        return row  # vanished from both sides mid-scan; caller drops it
    if sl is None:
        row['status'] = ST_RONLY
        return row
    if sr is None:
        row['status'] = ST_LONLY
        return row
    if sl[0] < 0 or sr[0] < 0:
        row['status'] = ST_ERR  # stat() failed at walk time
        return row
    if sl[0] != sr[0]:
        row['status'] = ST_DIFF  # different sizes cannot be equal content
        return row
    if sl[0] == 0:
        row['status'] = ST_SAME  # two empty files
        return row

    same = _contents_equal(os.path.join(dir_l, rel),
                           os.path.join(dir_r, rel),
                           sl[0], quick_only)
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

    os.walk is used with its default followlinks=False, so symlinked
    directories never create cycles; symlinked FILES are stat()ed
    through (their target's size/content is compared -- the useful
    semantic for "did anything change here").

    Walk errors (unreadable folders, permission problems) are collected
    (bounded, first 50) into walk_errors instead of aborting the scan:
    the rest of the tree still gets compared, and the summary line
    reports how many folders were skipped.
    """

    def __init__(self, dir_l, dir_r, recursive, mask, quick_only):
        super().__init__(daemon=True, name='Differ2DirCompare')
        self.dir_l = dir_l
        self.dir_r = dir_r
        self.recursive = recursive
        self.mask = mask
        self.quick_only = quick_only
        self.cancel_evt = threading.Event()
        self.lock = threading.Lock()
        self.rows = []          # completed rows, in relpath order
        self.total = 0          # rows the scan will produce (after walks)
        self.finished = False
        self.fatal = None       # exception text of an aborted run
        self.walk_errors = []   # bounded list of (dirpath, message)

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
        try:
            self._scan()
        except Exception as ex:  # never let the thread die silently
            self.fatal = '{}: {}'.format(type(ex).__name__, ex)
        finally:
            with self.lock:
                self.finished = True

    def _walk_err(self, e):
        if len(self.walk_errors) < 50:
            try:
                self.walk_errors.append((e.filename or '?', str(e)))
            except Exception:
                pass

    def _walk(self, root):
        """Return (files, dirs): normcase(relpath) -> relpath for dirs,
        normcase(relpath) -> (relpath, size, mtime) for files. Files not
        passing the mask are skipped entirely (WinMerge behavior:
        a mask hides files, it does not mark them). A file whose stat()
        fails gets (-1, -1) so _file_row can flag it unreadable."""
        files = {}
        dirs = {}
        if self.recursive:
            for dirpath, dirnames, filenames in os.walk(
                    root, onerror=self._walk_err):
                if self.cancelled():
                    return files, dirs
                base = os.path.relpath(dirpath, root)
                if base == '.':
                    base = ''
                for d in dirnames:
                    rel = d if not base else os.path.join(base, d)
                    dirs[os.path.normcase(rel)] = rel
                for fn in filenames:
                    if not _mask_ok(fn, self.mask):
                        continue
                    rel = fn if not base else os.path.join(base, fn)
                    try:
                        st = os.stat(os.path.join(dirpath, fn))
                        info = (rel, st.st_size, st.st_mtime)
                    except OSError:
                        info = (rel, -1, -1)
                    files[os.path.normcase(rel)] = info
        else:
            try:
                it = os.scandir(root)
            except OSError as e:
                self._walk_err(e)
                return files, dirs
            with it:
                for entry in it:
                    if self.cancelled():
                        return files, dirs
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            dirs[os.path.normcase(entry.name)] = entry.name
                            continue
                        if not entry.is_file():
                            continue  # sockets, fifos, devices...
                        if not _mask_ok(entry.name, self.mask):
                            continue
                        st = entry.stat()  # through symlinks: compare
                        info = (entry.name, st.st_size, st.st_mtime)  # targets
                    except OSError as e:
                        self._walk_err(e)
                        continue
                    files[os.path.normcase(entry.name)] = info
        return files, dirs

    def _scan(self):
        files_l, dirs_l = self._walk(self.dir_l)
        files_r, dirs_r = self._walk(self.dir_r)
        if self.cancelled():
            return

        dir_keys = sorted(set(dirs_l) | set(dirs_r))
        file_keys = sorted(set(files_l) | set(files_r))
        with self.lock:
            self.total = len(dir_keys) + len(file_keys)

        # Folder rows first (they are also the skeleton of the partial
        # view while the file rows stream in behind them).
        for k in dir_keys:
            if self.cancelled():
                return
            rl = dirs_l.get(k)
            rr = dirs_r.get(k)
            rel = rl if rl is not None else rr
            ml = _stat_side(os.path.join(self.dir_l, rel))[1] \
                if rl is not None else None
            mr = _stat_side(os.path.join(self.dir_r, rel))[1] \
                if rr is not None else None
            with self.lock:
                self.rows.append(_dir_row(rel, ml, mr,
                                          rl is not None, rr is not None))

        for k in file_keys:
            if self.cancelled():
                return
            fl = files_l.get(k)
            fr = files_r.get(k)
            sl = (fl[1], fl[2]) if fl is not None else None
            sr = (fr[1], fr[2]) if fr is not None else None
            rel = (fl or fr)[0]
            row = _file_row(self.dir_l, self.dir_r, rel, sl, sr,
                            self.quick_only)
            with self.lock:
                self.rows.append(row)


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
      | (listview, stretches with the form)                          |
      +--------------------------------------------------------------+
      [ status line / progress                    ][ counts          ]

    The path edits are editable on purpose: type two paths and press
    Refresh (or Enter) to compare them without reopening any dialog.
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

    # Row C (filters).
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
        self._show = {                  # status filter checkboxes
            ST_DIFF: True, ST_LONLY: True, ST_RONLY: True, ST_SAME: True,
        }
        # Dialog plumbing
        self.h = 0
        self.h_sb = 0
        self.h_imglist = 0
        self.ctl = {}                   # name -> control index
        self.ctl_rev = {}               # control index -> name
        self._icon_idx = {}             # icon name -> imagelist index
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

        # -- The list ----------------------------------------------------
        p = {
            'a_l': ('', '['), 'sp_l': 10,
            'a_r': ('', ']'), 'sp_r': 10,
            'a_t': ('ed_left', ']'), 'sp_t': 40,
            'a_b': ('sbar', '['), 'sp_b': 4,
            'columns': '\t'.join(
                '\r'.join((cap, str(w), '', '', align))
                for cap, align, w in _LIST_COLUMNS),
            'on_click_dbl': self._on_list_dbl,
            'on_click_header': self._on_header,
            'on_menu': self._on_list_menu,
        }
        li_bg = _theme_color('ListBg', ed_bg)
        li_fg = _theme_color('ListFont', ed_fg)
        if li_bg is not None:
            p['color'] = li_bg
        if li_fg is not None:
            p['font_color'] = li_fg
        self._add('listview', 'list', p)

        # Per-window imagelist (owned by the form -> freed with it).
        try:
            paths = _icon_paths()
            self.h_imglist = ct.imagelist_proc(0, ct.IMAGELIST_CREATE,
                                               value=self.h)
            for name in ('same', 'diff', 'lonly', 'ronly', 'folder', 'err'):
                idx = ct.imagelist_proc(self.h_imglist, ct.IMAGELIST_ADD,
                                        value=paths[name])
                self._icon_idx[name] = idx if idx is not None else -1
            ct.dlg_proc(h, ct.DLG_CTL_PROP_SET, name='list',
                        prop={'imagelist_small': self.h_imglist})
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
    # List view filling / sorting / formatting
    # ------------------------------------------------------------------

    def _header_str(self):
        """The 'items' header line: 'Title=W' entries ('\r'-joined),
        widths/alignments taken from the LIVE columns so the user's
        manual column drags survive refills; the sort column's caption
        carries the direction marker."""
        cols = []
        try:
            s = ct.dlg_proc(self.h, ct.DLG_CTL_PROP_GET,
                            name='list').get('columns', '')
            for c in s.split('\t'):
                parts = c.split('\r')
                cols.append(parts + [''] * (5 - len(parts)))
        except Exception:
            cols = []
        if len(cols) != len(_LIST_COLUMNS):
            cols = [[cap, str(w), '', '', align]
                    for cap, align, w in _LIST_COLUMNS]
        out = []
        for i, c in enumerate(cols):
            cap = c[0]
            for mark in _SORT_MARK.values():
                if cap.endswith(mark):
                    cap = cap[:-len(mark)]
            if i == self._sort_col:
                cap += _SORT_MARK[self._sort_desc]
            width = c[1] if c[1] else '100'
            align = c[4] if c[4] in ('L', 'R', 'C') else 'L'
            out.append(cap + '=' + align + width)
        return '\r'.join(out)

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
        """Filter+sort 'rows' (default: the finished self._rows) into the
        list view, preserving the live column widths (see _header_str)."""
        if self._torn or not self.h:
            return
        if rows is None:
            rows = self._rows
        view = [r for r in rows if self._filter_ok(r)]
        view.sort(key=self._sort_key)
        if self._sort_desc:
            view.reverse()
        self._view = view
        data = [self._header_str()]
        icons = []
        for r in view:
            data.append('\r'.join(self._row_cells(r)))
            icons.append(str(self._icon_idx.get(_ICON_OF.get(r['status']),
                                                -1)))
        try:
            ct.dlg_proc(self.h, ct.DLG_CTL_PROP_SET, name='list', prop={
                'items': '\t'.join(data),
                'imageindexes': '\t'.join(icons),
            })
        except Exception:
            pass

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
        daemon, harmless) while the new worker takes over the timer."""
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

        if self._worker is not None:
            self._worker.cancel()
        recursive = self._ctl_val('chk_sub') == '1'
        mask = _split_mask(self._ctl_val('ed_mask'))
        self._worker = _Scanner(self._dir_l, self._dir_r, recursive,
                                mask, self._quick_only)
        self._rows = []
        self._view = []
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
        self._worker.start()
        ct.timer_proc(ct.TIMER_START, self._on_timer, POLL_MS)

    def _on_timer(self, tag='', info=''):
        """Main-thread poll of the worker (timer_proc): refresh the
        progress line, stream rows into the list, finish up."""
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
            return
        if not finished:
            self._sb_text(1, _('Comparing... {} / {}').format(
                len(rows), total if total else '?'))
            if len(rows) - self._last_fill >= PARTIAL_FILL_STEP:
                self._last_fill = len(rows)
                self._fill_list(rows)
            return
        # Finished (normally or cancelled): last full update.
        self._stop_timer()
        self._rows = rows
        self._last_fill = -1
        self._fill_list()
        self._update_counts()
        elapsed = time.perf_counter() - self._scan_t0
        if w.cancelled():
            msg = _('Cancelled') if not rows else \
                _('Cancelled ({} of {} rows)').format(len(rows), total)
        else:
            msg = _('Done in {:.1f} s').format(elapsed)
            if w.walk_errors:
                msg += '  ' + _('({} folders unreadable)').format(
                    len(w.walk_errors))
        self._sb_text(1, msg)

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
        toggles the direction (the caption carries the marker)."""
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

    def _sel_index(self):
        try:
            return int(self._ctl_val('list'))
        except (TypeError, ValueError):
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
        one-sided folders open in the OS file manager."""
        st = row['status']
        pl = self._path(row, 'l')
        pr = self._path(row, 'r')
        if st in (ST_SAME, ST_DIFF):
            if os.path.isfile(pl) and os.path.isfile(pr):
                self._cmd.open_compare_pair(pl, pr)
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
        both sides, re-run the same tiered compare the scanner uses, and
        patch the row in place (no full rescan). A row that vanished
        from both sides is dropped."""
        rel = row['rel']
        sl = _stat_side(os.path.join(self._dir_l, rel))
        sr = _stat_side(os.path.join(self._dir_r, rel))
        if sl is None and sr is None:
            self._rows = [r for r in self._rows
                          if r['isdir'] or r['rel'] != rel]
        else:
            new_row = _file_row(self._dir_l, self._dir_r, rel, sl, sr,
                                self._quick_only)
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
        forms itself."""
        self._app_exit = True
        if self._worker is not None:
            self._worker.cancel()
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
    then open a compare window."""
    res = _PickerDialog().show(dir_l, dir_r)
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
        form = DirCompareForm(cmd, dir_l, dir_r)
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
