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
scan). Folder rows take their mtime from the parent listing the
same way; _stat_side survives only in the main thread's single-row
refresh after a copy/delete action. Pure os.scandir, no platform
calls: the module has no ctypes / Win32 dependencies and runs
unchanged on Windows, Linux and macOS.

In the THREADED shapes (option differ2.dirs.scan_threading 'pool'
-- the opt-in engine for very big trees) the directory scans run
CONCURRENTLY on a small thread pool (SCAN_POOL_THREADS): both
roots are submitted at once, and the moment a directory's listing
arrives, its subfolders are submitted too -- no wave barrier, no
scan ever waits behind an unrelated slow directory. The wall time
approaches the longest CHAIN of directories plus one listing
latency, not the sum of all directories. The equal-size content
tests ride the same pool afterwards. On a fast local disk all this
changes little; on a source with high per-call latency the
difference is the scan -- WHEN background threads get to run at
full speed (see the update-10 finding below for the box where they
do not). While the cProfile layer is on, the scan deliberately
falls back to the serial one-thread mode: cProfile only traces the
thread that started it, so pool threads would be invisible in the
function-level report (the section report still attributes the
parallel run via thread-safe standalone marks) -- a profiled scan
is therefore somewhat slower than a normal one, on purpose.

The scan's threading SHAPE is switchable (option differ2.dirs.scan_
threading), and the update-10 measurement settled which shape
deserves the default. On the reporting box (Windows 7 / 4 cpus /
no antivirus, same folders, profiling on) the three shapes paid:

  pool    scan wall 12570 ms, 10 listings avg 813 ms
  serial  scan wall 12989 ms, 10 listings avg 548 ms
  main    scan wall     8.1 ms, 10 listings avg   0.1 ms

The mechanism, confirmed by a temporary deep-probe build since
removed: a GIL convoy -- every background thread's listing waited
up to ~200-412 ms to re-acquire the interpreter lock after each
metadata syscall (pure wait, no CPU burned) while the UI thread
ran its message loop, process-wide for as long as the scan ran,
and ~1500x slower than the same calls on the main thread (which
paid 0.1 ms per listing). The disk, the folders, the filter
stack, the listing call and the engine were exonerated one by one;
the THREAD was the tax. Therefore:

  main    (the DEFAULT) the whole scan runs SYNCHRONOUSLY on the
          UI thread: run() called inline, no worker thread, no
          timer ticks, no repaint, no cancel; rows appear at the
          end and the window freezes for the scan's duration.
          On a GIL-convoy box this is the only fast shape (8 ms
          vs 12.6 s); on a healthy box the freeze is the whole
          cost -- a normal tree scans in well under a second.
  pool    the threaded engine: scanner thread + pool, rows
          streaming into a live, cancelable window. The choice
          for VERY BIG trees, where a frozen window for the
          walk's whole duration is worse than a slower walk.
  serial  the scanner thread alone, no pool -- kept for the
          cProfile layer (which forces it) and for A/B-ing the
          pool's concurrent bursts away.

The option is reloaded on every start_scan (set it, press
Refresh); the facts block prints a 'scan mode:' line naming the
shape that produced the report.

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

When EVERY listing costs hundreds of milliseconds and WinMerge on
the same folders is instant, the cause is usually not the folder at
all: a real-time antivirus can bill every file operation of THIS
python.exe (an unsigned interpreter) while a signed, well-known
executable like WinMerge.exe passes its filter untouched -- same
calls, different process, 100x different bill. The report's
dirs:listing rows show exactly that (their latency is the per-call
bill this process pays), and the cProfile layer's stdlib import
cost -- seconds, when CudaText's Python lives on such a drive -- is
pre-warmed on the MAIN thread (dirs:cprofile_import, outside the
scan wall) instead of hiding as an unexplained gap inside the
scanner thread. For folders compared often, excluding them (and
CudaText's Python) from real-time antivirus scanning removes that
cost entirely.

== Threading model ==================================================

This is the THREADED engine (option differ2.dirs.scan_threading
'pool'/'serial') -- the opt-in shape for very big trees since
update 10. The scan runs on a daemon thread that touches ONLY the
file system and its own state -- never the CudaText API (which is
main-thread only). A per-window poll timer (timer_proc, ~200 ms)
snapshots the worker's results under its lock and updates the list
view on the main thread, so huge trees stream into the window
while it stays fully responsive; closing the window or starting a
rescan simply sets a cancel flag the worker checks between every
file. Several compare windows can run scans at once -- each owns
one worker, one timer and its own scan pool (the worker thread
itself only coordinates). The DEFAULT shape ('main') has none of
this machinery: run() is called inline on the UI thread, the
window freezes for the scan's duration and rows appear at the end
-- see "The scan engine" for why that trade-off won.

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
    block (see _prof_facts_block): the scan mode, listings count +
    their total/slowest/average latency, tree sizes, how much
    content the compare read, and whether the run was serial
    because the cProfile layer was on. A cancelled scan (rescan,
    window closed) cancels its pending pieces and prints nothing,
    like the tab compare's cancel path.

    NESTED ACCOUNTING (this is what makes the report's numbers
    add up): the dirs:* bookings carry their start times as SPANS
    (profiling.py, _Row.spans), and the report rebuilds the nesting
    a shared section stack cannot give cross-thread work: listings
    nest under the walk that listed them (a parent hint pins each
    to its own tree even when the two walks overlap), the walks,
    row phases, content tests, UI rows and lags nest under
    dirs:worker, and worker's SELF becomes just the glue left
    over. Rows whose spans overlapped a sibling carry the
    "(parallel)" tag: their totals are per-thread walls, and the
    report prints the parallel overlap under the table -- counting
    it once, the SELF column totals ~100% of the outermost row in
    every scan mode (a serial scan simply sums to ~100%). The
    report's baseline is likewise the widest SPAN, not the largest
    summed total (nine parallel listings can sum above the scan
    wall; their widest single span cannot).
  * the cProfile layer, additionally gated by
    differ2.advanced.enable_cprofile (same double gate as the tab
    compare): ONE profile, started INSIDE the scanner thread -- it
    traces the actual walk/compare work, i.e. the part of a folder
    compare that can actually be slow (the main thread's UI phases
    are already phase-measured by the section rows dirs:ui_*, and
    the main thread is what the tab compare's own cProfile layer
    covers). Its stdlib (cProfile/profile/pstats) is pre-warmed by
    _prof_begin_scan on the MAIN thread, BEFORE the scan starts
    (booked as dirs:cprofile_import -- a per-SESSION cost; without
    the pre-warm those imports ran inside the scanner thread, and
    on a drive where every file open costs ~300-800 ms they hid
    there as a seconds-wide gap between dirs:worker's wall and the
    profiled window); the scanner thread's own setup is booked as
    dirs:cprofile_setup and is ~0 once pre-warmed. While this
    layer is on the scan runs in its SERIAL one-thread mode, so
    the profile sees every walk/hash call. On Python up to 3.11
    profiles are per-thread and the scanner's layer would coexist
    with a simultaneously profiled tab compare anyway; Python 3.12+
    allows one active profiling tool per PROCESS -- there the
    scanner claims the slot, and if something else already holds
    it, start_profiling() fails quietly, the layer stays off for
    this scan and the scan stays parallel. Printed after the
    section report at the scan's natural end, sorted by internal
    time (sort_key='time'), max_lines=100: the real bottleneck
    function sits at the top of the table.

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
  Identical / Folder    -> no fill (the theme's TreeBg -- the exact
                          color the listbox control paints its own
                          background with, so uncolored rows are
                          indistinguishable from the empty list area;
                          the dialog listbox takes its background from
                          TreeBg, NOT from the control's color prop)
  Mixed (folder)        -> no fill by design -- one-sided AND
                          identical content in one both-sides folder;
                          a one-sided tint would overstate it the
                          same way its pre-25th 'Only left' caption
                          did, so the caption alone carries the news

So a file painted yellow in the folder list is painted yellow line
by line when double-clicked into a compare tab; a red "only left"
file opens with red (deleted) gaps on the left side, a green "only
right" file with green (added) gaps on the right. The selected row is
painted with the theme's ListSelBg/ListSelFont and overrides the
status color. The plain listview control was abandoned on purpose:
the dialog API's listview has no per-row colors at all --
owner-drawing was the only way to give the folder list
WinMerge/Beyond-Compare-style row colors.

A FOLDER row carries the worst status of its subtree (WinMerge's
rolled-up result): a folder that only CONTAINS a different file is
painted with color_changed and its Status cell reads "Different",
while a folder whose contents are all identical stays plain. Folders
always carry the folder icon -- one-sided folders communicate their
sidedness by color and caption, not by swapping the icon.

== Row font =========================================================

The rows render in the CudaText UI font -- the very font the listbox's
own header, the command palette and every themed control use. The
painter SETS it explicitly on every pass: CANVAS_SET_FONT with the
name from ui_font_name and the size _ui_font_pt() (ui_font_size as
the app draws it, DoScaleFont = size * ui_scale_font // 100). Relying
on TATListbox's canvas preset was tried and did NOT survive the
field: current upstream atlistbox.pas presets the themed font in
DoPaintTo, but the reported box rendered small anyway -- older
builds evidently skip the preset, so an empty-name CANVAS_SET_FONT
left the canvas at the LCL default GUI font (~75-80% of the UI
size). Passing the values is build-proof: on builds WITH the preset
the same numbers are written twice, on builds without it the fix is
the values themselves. The row height keeps the control's own
auto-fit wherever that band fits the drawn text -- and the painter
GUARDS it where it does not: it measures the glyphs it just drew and
raises ItemHeight to measured+6px when the auto-fit band is tighter
(the reported build's auto-fit rides the same broken theme chain
that swallowed the preset, so the band stayed 9pt-sized under the
grown font and ate the text; the +6px padding is the user's request
-- at small fonts the built-in auto-fit can sit within a pixel of
glyph+6, so the guard may bump such a band a pixel or two, by
design; LISTBOX_SET_ITEM_H freezes the auto-fit and never shrinks
-- see _fit_item_h). What IS the plugin's business is every
pixel metric that must fit that text -- column widths, the tree
gutter, the expand-marker zone -- all scaled by _px_scale() =
DPI factor x UI-font factor (_font_scale, from ui_font_size and the
ui_scale_font option read via PROC_CONFIG_SCALE_GET; a 14pt UI font
draws ~1.5x-wider glyphs than the 9pt default the base widths assume).
The statusbar cells carry no width at all anymore: the status cell
AUTOSIZEs to its text and the counts cell AUTOSTRETCHes over the rest
of the bar, so they shrink and grow with the dialog (TATStatus
re-fits an autosized cell on every paint).

== Theming (the clNone trap) ========================================

Every theme color comes from PROC_THEME_UI_DICT_GET through
_theme_color, which reads the LCL clNone sentinel ($1FFFFFFF) as 'not
themed': the dict carries EVERY TAppThemeColor key and the optional
ones (StatusBg/StatusFont, ...) sit at clNone when the theme leaves
them out -- passing that sentinel into a color API is how the
statusbar once painted white on black themes. The chains mirror the
app's own wiring (formmain_themes.inc):

  form / statusbar bg   StatusBg -> TabBg (the app's exact pair for
                       its main statusbar)
  statusbar font       StatusFont -> ButtonFont (= ATFlatTheme's
                       ColorFont, TATStatus's own default)
  statusbar borders    ButtonBorderPassive (as the app assigns)
  picker combos (bg)   OtherTextBg -> EdTextBg; (font) OtherTextFont
                       -> EdTextFont -- the app's OWN chain for every
                       single-line input it draws (form_find's find/
                       replace boxes, the Command Palette input,
                       CodeTreeFilterInput, the one-line dialog
                       editors -- proc_customdialog.pas lines 745-746
                       and four more call sites). NOT ListBg: that
                       key's built-in default (SetColor nColorListBack,
                       proc_colors.pas) is LIGHT GREEN $b4d8a8 -- a
                       theme that does not spell ListBg out (the grey
                       theme of the 6th-round report) rendered every
                       input box green. OtherTextBg/OtherTextFont
                       default to clNone (skipped by the guard) and
                       EdTextBg/EdTextFont carry concrete defaults
                       ($e4e4e4/$202020) AND are spelled out by every
                       real theme -- a theme without them would paint
                       the app's own find dialog just as broken, so
                       the chain can never go green and never diverges
                       from what the user sees in the app's own inputs.

The results dialog's three inputs (ed_left/ed_right/ed_mask) go one
step further and are one-line 'editor' controls (TATSynEdit) -- the
control type the app itself builds its single-line inputs from:

* the app themes them at creation (EditorApplyTheme +
  DoControl_ApplyEditorProps's one-line branch: Colors.TextFont/
  TextBG = OtherTextFont/OtherTextBg -> EdTextFont/EdTextBg) -- the
  plugin passes NO color props for them at all;
* the hint ('texthint' prop -> OptTextHint) is painted BY THE CONTROL
  in Colors.TextHintFont (clGray, italic) -- exactly the placeholder
  the app's find dialog and Command Palette show. A native TEdit
  cannot do this: its TextHint goes to Windows as EM_SETCUEBANNER
  and WINDOWS paints it in the system gray -- no plugin API can
  recolor it (the 6th-round report: unreadable hints in dark
  themes). clGray is readable on every theme's input bg (the app
  ships it everywhere) and italic marks it as a hint;
* 'font_name'/'font_size' pin the UI font: the dialog API creates
  editors in the EDITOR font (EditorOps, monospaced) -- pinning
  _ui_font()/_ui_font_pt() keeps the text at the size the boxes
  always had;
* one-line behavior needs PROP_ONE_LINE (True): ModeOneLine hides
  the gutter/ruler/scrollbars, enforces a single line and centers
  the text vertically (TATSynEdit.SetOneLine);
* text I/O goes through Editor(handle) ('ed.set_text_all' /
  'get_text_all'): the dialog API's 'val' does NOT handle TATSynEdit
  (DoControl_SetStateFromString has no TATSynEdit branch) -- the
  handles are cached in self._ed_handles and _ctl_val/_set_ctl_val
  route editor-backed names transparently, so every existing call
  site (start_scan, swap, Browse, mask) is unchanged.

The BUTTONS are DRAWN buttons (_DrawnBtn) -- one-item owner-drawn
listbox_ex controls whose whole face is the plugin's. Why not the
control kinds the API offers (three rounds of reports -- 'always
grey', then 'text is invisible' on dark themes, then 'hard to
differentiate a button from an input box'):
* 'button' (native TButton) is drawn by the OS visual style and
  stays OS-gray in every theme;
* 'button_ex' (non-flat TATButton) paints bg/border/caption STRICTLY
  from the GLOBAL ATFlatTheme (wired from the UI theme keys
  ButtonBgPassive/ButtonBorderPassive/ButtonFont,
  formmain_themes.inc). A theme that defines the dark ButtonBgPassive
  but not ButtonFont leaves the caption at the built-in dark default
  $202020 (proc_colors.pas nColorText) -- INVISIBLE on the dark face
  (the 21st report), and NO dialog prop or button_proc action can
  reach those painted surfaces (proc_customdialog.pas creates
  TATButton bare; 'color'/'font_color' set Control.Color/Font.Color,
  which DoPaintTo overwrites);
* 'label' is fully colorable but cannot center its caption
  (TLabel support in proc_customdialog.pas: ex0 = right-align only).
So each drawn button paints its face in a color DERIVED from the
form's own TabBg/TabFont pair (_btn_palette: face = 10% blend of the
form bg toward its text color, pressed 24%, hover 17%, border 38%),
caption in the form's own text color -- readable in EVERY theme by
construction, distinct from both the form and the paper-colored
'editor' inputs (a button reads as a button), with hover and pressed
shading through the on_mouse_enter/exit/down/up events every control
kind gets. The width is MEASURED -- a hidden button_ex probe
(_add_probe) whose 'autosize' re-trigger measures the caption in the
themed UI font (TATButton.SetAutoSize, the app's own math), so no
caption can ever be eaten at any font/DPI/translation. The single
item's band is pinned to EXACTLY the control's pixel height
(_DrawnBtn.sync): TATListbox's auto-scrollbar rule
(ItemCount*ItemHeight > Height, the full height) stays false and
the band still covers the whole face -- ONE background color, no
scrollbar strip, no arrow buttons (the 22nd-release report: a +4px
pin had put a themed scrollbar down every button's right edge).
And the caption itself paints on that same ONE background: LCL
TextOut fills the glyphs' rectangle with the canvas's current
brush, so the painter re-sets the brush to the face color right
before the text call (the border frame leaves it at the border
color -- the 23rd-release report saw that as 'one background on
the button and one on the text').

The status FILTER CHECKS are the same _DrawnBtn in flat 'chk' mode:
the form's own color shows through (no button face -- a check must
not look like a button), the glyph is a hand-drawn checkbox (box +
check stroke on the canvas, scaling with _px_scale -- a fixed 16px
PNG cannot), and the caption is the form's own text color (a native
TCheckBox draws its caption through the OS theme and ignores
Font.Color, the 5th-round black-on-black captions). The check STATE
lives in the plugin (self._show / self._recursive) because the
dialog API's 'val' does not handle TATButton -- _on_check flips the
state and _set_chk_state re-renders the drawn glyph.

== Windows / instances =============================================

Every compare is a separate NON-MODAL dlg_proc form: run as many
folder compares side by side as you like. A recursive scan's results
are shown as a WinMerge-style TREE: every folder is folded by
default; clicking its +/- marker, double-clicking it, pressing Enter
or the context menu expands it INLINE -- the children appear indented
under the folder row, in the same window (no drill-down windows;
those remain only for folders without scanned children, i.e. with
Subfolders off or truly empty folders). Sorting is the column
header's own click-sort (the marker is the U+2191/U+2193 arrow); the
column WIDTHS are drag-resizable -- press near a boundary line in
the list (the cursor turns into an H-split) and drag; the widths are
remembered across sessions and resettable from the context menu.
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

# The plugin's STATE file: machine-local runtime state (the dir
# histories, both window geometries, the drag-resized column widths).
# Not user-editable options but remembered UI state, so it lives in
# its own JSON, NOT in the settings file (25th release request:
# 'move this config you save in cuda_differ2.json to
# cuda_differ2_state.json'). The same 'differ2.*' option names are
# used inside it -- only the FILE changes; cudax_lib's get_opt/
# set_opt take the file name as their user_json argument and create
# the file on its first write. No backward compatibility is kept
# (this code was never published): the old keys are DELETED from the
# settings file once per session (_purge_stale_state_keys), never
# read from there again.
STATE_JSON = 'cuda_differ2_state.json'

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

# Threading SHAPES of the folder scan (option differ2.dirs.scan_
# threading; see the module docstring's "The scan engine"):
#   main    EVERYTHING synchronously on the MAIN thread: run() is
#           called INLINE (never .start()), so the whole scan --
#           walks, rows -- runs on the thread that owns the UI. No
#           worker thread, no pool, no timer,
#           no repaint, no cancel: the window freezes for the scan's
#           duration and rows appear at the end. THE DEFAULT since
#           update 10: on boxes where background threads starve for
#           the GIL while the UI is busy (the update-9/10 finding:
#           the same folders 8 ms on main vs 12.6 s on the pool --
#           a ~1500x gap) this is not just the fastest shape, it is
#           the only fast one.
#   pool    the scanner thread + the scan pool: both trees walked
#           concurrently, rows streaming into a live window, cancel
#           mid-scan -- the engine for VERY BIG trees, where a
#           frozen window for minutes is worse than a slower walk
#   serial  the scanner thread ALONE: the walk runs directory by
#           directory on it, no pool (the cProfile layer's shape)
SCAN_MODE_POOL = 'pool'
SCAN_MODE_SERIAL = 'serial'
SCAN_MODE_MAIN = 'main'
# The default shape (update 10): 'main'. Every fallback below (a
# missing option, a hand-edited bogus value) lands here -- one
# constant so a future flip is a one-line change.
SCAN_MODE_DEFAULT = SCAN_MODE_MAIN
_SCAN_MODES = (SCAN_MODE_POOL, SCAN_MODE_SERIAL, SCAN_MODE_MAIN)

HISTORY_MAX = 12               # remembered folder paths per side
POLL_MS = 200                  # worker -> UI poll period while scanning
ICON_CACHE_VER = '1'           # bump to force-renew the icon cache dir

# Owner-drawn list metrics (the results list is a listbox_ex in
# LISTBOX_SET_DRAWN mode -- see the module docstring "Row coloring").
# The ROW HEIGHT defaults to the control's own auto-fit (ATFlatControls
# atlistbox.pas, Paint -> UpdateItemHeight -> GetItemHeightDefault =
# CanvasFontSizeToPixels(DoScaleFont(theme font)) * Max(96, PPI) div
# 96) -- the exact height every CudaText list (command palette, tab
# list) uses. But the auto-fit rides the THEME font, and the reported
# build's theme chain ignores the UI font size (the same broken chain
# that swallowed the canvas preset -- 4 reports in a row now), so the
# painter GUARDS the band instead of trusting it: it measures the
# glyphs it actually draws and raises ItemHeight to fit when the band
# is tighter (LISTBOX_SET_ITEM_H -- SetItemHeight sets
# FItemHeightIsFixed, freezing the auto-fit; measured, never guessed,
# never shrinks, never fires where the auto-fit already fits -- see
# _fit_item_h). A fixed height as the ONLY mechanism (the 15th
# release) froze the rows at OUR estimate on every box; the guard
# keeps the control's value everywhere it works. LISTBOX_GET_ITEM_H
# (used by the painter's hit-test calibration and _row_at_y) always
# returns the live value, auto-fitted or guarded.

# Column separator for the listbox items/header. A control character
# is used (not '|'): item captions are built from real file names,
# and '|' legally occurs in file names on Linux -- an invisible
# separator the file system cannot produce keeps the header columns
# and the fallback (non-drawn) rendering aligned.
COL_SEP = chr(31)              # ASCII unit separator

# Row statuses. Files: ST_SAME / ST_DIFF / ST_LONLY / ST_RONLY / ST_ERR.
# Folders: ST_DIR (on both sides) / ST_DIR_LONLY / ST_DIR_RONLY.
# ST_MIXED is never a row's own status -- it is the ROLLED-UP status
# (_es, see _build_view) of a both-sides folder whose subtree holds
# one-sided content: the folder itself sits on BOTH sides, so saying
# 'Only left'/'Only right' about it would be a lie (the 25th report:
# 'when a folder have identical and only left files, the folder status
# show only left ... it must show a diferent status').
ST_SAME = 'same'
ST_DIFF = 'diff'
ST_LONLY = 'lonly'
ST_RONLY = 'ronly'
ST_ERR = 'err'
ST_DIR = 'dir'
ST_DIR_LONLY = 'dir_lonly'
ST_DIR_RONLY = 'dir_ronly'
ST_MIXED = 'mixed'

STATUS_CAPTION = {
    ST_SAME:      _('Identical'),
    ST_DIFF:      _('Different'),
    ST_LONLY:     _('Only left'),
    ST_RONLY:     _('Only right'),
    ST_ERR:       _('Cannot read'),
    ST_DIR:       _('Folder'),
    ST_DIR_LONLY: _('Only left'),
    ST_DIR_RONLY: _('Only right'),
    ST_MIXED:     _('Mixed'),
}

# Sort order of the Status column: the interesting rows (differences,
# one-sided files) first, the noise (identical files, plain folders)
# last. dir_* rows sit right after their file counterparts; a MIXED
# folder (one-sided content inside a both-sides folder) sits right
# after the one-sided folders -- more interesting than an error, less
# than a difference. Only the ORDER matters, never the values.
STATUS_SEVERITY = {
    ST_DIFF:      0,
    ST_LONLY:     1,
    ST_RONLY:     2,
    ST_DIR_LONLY: 3,
    ST_DIR_RONLY: 4,
    ST_MIXED:     5,
    ST_ERR:       6,
    ST_SAME:      7,
    ST_DIR:       8,
}

# Row statuses painted with a full-line background color in the
# owner-drawn list, mapped to the Command.cfg key that holds the
# color (the SAME keys the diff tabs use for their hunk lines --
# see the module docstring "Row coloring"). Statuses absent from
# this mapping keep the plain list background: a MIXED folder gets
# NO tint on purpose -- it contains one-sided AND identical content,
# so a one-sided tint would overstate exactly the way its old
# 'Only left' caption did; the caption carries the information.
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
# Widths are 96-DPI, 9pt-font BASE pixels -- _col_spec/_col_layout
# multiply them by _px_scale() (DPI x UI-font factor) so the cells keep
# fitting the text the control actually draws (the reporting box
# ellipsized every date at 125% DPI with the raw widths; the same
# happens at a bigger UI font, which draws 1.x-times-wider glyphs into
# the old widths). They are also only the DEFAULTS: the fixed columns
# are drag-resizable (see _on_mouse_down) and persist across sessions
# as 'dirs.col_widths' (base values, in the plugin's STATE file --
# cuda_differ2_state.json, not the settings file).
_LIST_COLUMNS = (
    (_('Name'), 'L', 230),
    (_('Folder'), 'L', 170),
    (_('Status'), 'L', 110),
    (_('Left size'), 'R', 85),
    (_('Left date'), 'L', 140),
    (_('Right size'), 'R', 85),
    (_('Right date'), 'L', 140),
)

# Drag-resize clamps for a fixed column, in the same base 96-DPI
# pixels: below MIN the header caption would clip, above MAX one
# column would eat the list. Name is never resized directly -- it is
# the stretch column and takes whatever the fixed columns leave.
MIN_COL_W = 24
MAX_COL_W = 600

# The Name column's guaranteed floor, in the same base pixels (21st
# release: 'when compare dir window is shrinked the name column
# become invisible, it must have a minimum width so it can be always
# visible'). TATListbox.UpdateColumnWidths gives a 0-sized auto
# column Max(0, ClientWidth - fixed sum) -- with the six fixed
# columns summing past a narrow list, Name went to ZERO pixels and
# vanished (atlistbox.pas, source-verified). _effective_cols instead
# shrinks the FIXED columns proportionally so Name never drops below
# this floor: the file name is the one cell a shrunken window must
# still show.
NAME_MIN_W = 140


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


# Window/UI STATE (histories, geometries, column widths) -- the same
# get_opt/set_opt machinery pointed at STATE_JSON instead of the
# settings file. val=None deletes (cudax_lib's convention, same as
# _set_opt).
def _get_state(key, def_val):
    """Read a 'differ2.dirs.*' state key from the plugin's STATE file
    (settings/cuda_differ2_state.json) -- never from the settings
    file (see STATE_JSON above)."""
    return ctx.get_opt('differ2.' + key, def_val, user_json=STATE_JSON)


def _set_state(key, val):
    """Write (or, with val=None, delete) a 'differ2.dirs.*' state key
    in settings/cuda_differ2_state.json."""
    return ctx.set_opt('differ2.' + key, val, user_json=STATE_JSON)


# The state keys as they lived in cuda_differ2.json before the 25th
# release -- 'dirs.col_sizes' was never even read by this module (a
# pre-21st-release leftover sitting in the file); all six are deleted
# from the SETTINGS file once per session so nothing stale lingers
# there. set_opt(path, None) is cudax_lib's line-based delete; a
# missing file or a missing key is a clean no-op (verified against
# cudax_lib's simple-key branch), so this is safe on any box.
_STALE_SETTINGS_KEYS = (
    'dirs.hist_left', 'dirs.hist_right', 'dirs.win_geom',
    'dirs.picker_geom', 'dirs.col_sizes', 'dirs.col_widths',
)
_stale_state_purged = False


def _purge_stale_state_keys():
    """One-time-per-session cleanup: delete the moved state keys from
    cuda_differ2.json (they now live in cuda_differ2_state.json).
    Called from compare_dialog and compare_directories -- together
    they front every entry point (menu/picker, CLI, drill-down)."""
    global _stale_state_purged
    if _stale_state_purged:
        return
    _stale_state_purged = True
    for key in _STALE_SETTINGS_KEYS:
        try:
            ctx.set_opt('differ2.' + key, None, user_json=MODULE_JSON)
        except Exception:
            pass


def _theme_ui():
    """Current UI-theme dict (values are {'color': int} dicts), or {}."""
    try:
        return ct.app_proc(ct.PROC_THEME_UI_DICT_GET, '') or {}
    except Exception:
        return {}


# LCL clNone = TColor($1FFFFFFF): the UI-theme dict's 'not defined'
# sentinel -- PyHelper_GetThemeDict_UI dumps EVERY TAppThemeColor key
# and the optional ones (StatusBg/StatusFont, ...) sit at clNone when
# the theme leaves them out (see _theme_color).
_CLNONE = 0x1FFFFFFF


def _theme_color(key, fallback=None):
    """int color of a UI-theme key, or fallback when unavailable.

    The clNone GUARD is the point (5th dark-theme round): the dict
    from PROC_THEME_UI_DICT_GET carries EVERY TAppThemeColor key --
    including the ones the theme does not define, which sit at
    clNone ($1FFFFFFF; SetColor's default for the optional keys like
    StatusBg/StatusFont). Passing that sentinel on to a color API is
    a bug: STATUSBAR_SET_COLOR_BACK would set TATStatus.Color to
    clNone and the bar paints WHITE on a black theme -- exactly the
    reported look. So clNone reads as 'not themed' and the caller's
    fallback chain decides (the app itself resolves StatusBg->TabBg
    in formmain_themes.inc; the same chains live at our call sites)."""
    try:
        c = _theme_ui().get(key, {}).get('color')
        if c is not None and int(c) != _CLNONE:
            return int(c)
    except Exception:
        pass
    return fallback


def _blend(c1, c2, k):
    """Per-channel linear blend of two CudaText color ints (k=0 ->
    c1, k=1 -> c2). Channel-agnostic about the byte order: both
    inputs come from -- and the result goes back to -- the same
    color API, so whether ints are RGB or BGR cancels out."""
    out = 0
    for shift in (16, 8, 0):
        a = (int(c1) >> shift) & 255
        b = (int(c2) >> shift) & 255
        out |= int(a + (b - a) * k) << shift
    return out


def _btn_palette():
    """The DRAWN buttons' color set, derived from the form's OWN
    background/font pair -- the one pair a usable theme cannot get
    wrong (the form is painted with it and every label on it is
    readable by definition). NOT from the Button* trio: TATButton
    paints bg/border/caption strictly from the global ATFlatTheme
    (wired ButtonBgPassive/ButtonBorderPassive/ButtonFont,
    formmain_themes.inc), and themes that define the dark bg but not
    the font leave ButtonFont at its built-in dark default $202020
    (proc_colors.pas nColorText) -- dark-on-dark captions, the 21st
    release's 'the text is invisible' report; no dialog prop can
    reach those painted surfaces (proc_customdialog.pas creates
    TATButton bare; button_proc has no color action; 'color' /
    'font_color' set Control.Color/Font.Color, which the paint
    overwrites). Deriving from TabBg/TabFont instead makes the face
    a subtle shade of the form toward its text color: distinct from
    BOTH the form and the one-line 'editor' inputs (the paper-colored
    EdTextBg), with a clearly darker border -- a button that reads
    as one in every theme, light or dark."""
    bg = _theme_color('TabBg', _theme_color('ListBg'))
    fg = _theme_color('TabFont', _theme_color('ListFont'))
    if bg is None:
        bg = 0xF0F0F0
    if fg is None:
        fg = 0x000000
    return {
        'bg': bg,
        'fg': fg,
        'face': _blend(bg, fg, 0.10),
        'face_hover': _blend(bg, fg, 0.17),
        'face_down': _blend(bg, fg, 0.24),
        'border': _blend(bg, fg, 0.38),
        'flat_hover': _blend(bg, fg, 0.06),
    }


# Symmetric left/right padding inside a drawn button, and the drawn
# checkbox glyph's base size, in 96-DPI/9pt pixels (scaled by
# _px_scale like every other pixel metric).
BTN_PAD_X = 16
CHK_GLYPH = 13


def _chk_glyph_px():
    """The drawn checkbox glyph's side in live pixels (clamped: tiny
    at the smallest fonts, capped so a 3x UI font cannot blow the
    row apart)."""
    return max(9, min(26, int(CHK_GLYPH * _px_scale())))


def _add_probe(add):
    """Create the hidden measuring probe on a form ('add' is the
    dialog's own _add -- it registers the name and returns the index
    exactly like every other control). A button_ex whose autosize is
    re-triggered per measurement: TATButton.SetAutoSize (the
    'autosize' prop) measures the CURRENT caption in the themed UI
    font and sets Width := text + GapForAutoSize -- the app's own
    button math, reused as a ruler. Hidden ('vis': False) and
    out of the tab order."""
    add('button_ex', '_probe', {
        'cap': '', 'w': 4, 'h': 6, 'vis': False,
        'a_l': ('', '['), 'a_t': ('', '['), 'sp_l': 0, 'sp_t': 0,
    })


def _probe_gap(h):
    """The probe button's own overhead (GapForAutoSize) in pixels, or
    None when the probe cannot measure (the caller then falls back to
    a width estimate). SetAutoSize(True) re-measures on every call --
    TATButton.SetAutoSize has no same-value early exit -- so the
    empty caption pins the gap itself."""
    try:
        ct.dlg_proc(h, ct.DLG_CTL_PROP_SET, name='_probe',
                    prop={'cap': ''})
        ct.dlg_proc(h, ct.DLG_CTL_PROP_SET, name='_probe',
                    prop={'autosize': True})
        d = ct.dlg_proc(h, ct.DLG_CTL_PROP_GET, name='_probe') or {}
        return max(0, int(d.get('w', 0) or 0))
    except Exception:
        return None


def _probe_text_w(h, cap, gap):
    """A caption's width in live pixels via the probe (see
    _add_probe), gap-corrected; len*7 when measuring is impossible
    (pre-autosize builds: the drawn buttons then simply run a touch
    wide -- they can never CLIP, the width is ours)."""
    if gap is None:
        return len(cap) * 7
    try:
        ct.dlg_proc(h, ct.DLG_CTL_PROP_SET, name='_probe',
                    prop={'cap': cap})
        ct.dlg_proc(h, ct.DLG_CTL_PROP_SET, name='_probe',
                    prop={'autosize': True})
        d = ct.dlg_proc(h, ct.DLG_CTL_PROP_GET, name='_probe') or {}
        return max(0, int(d.get('w', 0) or 0) - gap)
    except Exception:
        return len(cap) * 7


class _DrawnBtn:
    """One THEMED-BY-US button: a single-item owner-drawn listbox_ex.

    Why not the obvious controls (four button rounds of reports --
    'always grey', then 'text is invisible' on dark themes, then
    'hard to differentiate a button from an input box', then 'two
    backgrounds, one on the button and one on the text'):
    * 'button' (native TButton) is drawn by the OS visual style and
      stays gray in every theme.
    * 'button_ex' (TATButton) paints bg/border/caption STRICTLY from
      the global ATFlatTheme; a theme that defines ButtonBgPassive
      but not ButtonFont leaves the caption at the dark built-in
      default -> invisible on dark faces, and no dialog prop or
      button_proc action reaches those surfaces (source-verified:
      proc_customdialog.pas creates TATButton bare; atbuttons.pas
      DoPaintTo reads only Theme^).
    * 'label' is fully colorable but cannot center its caption
      (TLabel support in proc_customdialog.pas: ex0 = right-align
      only) -- no button look.

    A one-item drawn listbox gives the plugin the WHOLE face:
    * colors from _btn_palette() -- derived from the form's own
      bg/font pair, guaranteed contrast in every theme (the 5th
      report), a distinct face + border vs both the form and the
      paper-colored input boxes (the 6th);
    * the caption CENTERED, in the UI font, width MEASURED through
      the hidden probe (the app's own autosize math) -- text can
      never be eaten from either end (the 4th), at any font/DPI and
      in any translation;
    * hover / pressed feedback (on_mouse_enter/exit/down/up are
      wired for every control kind -- proc_customdialog.pas wires
      TControlHack(Ctl).OnMouseEnter/Leave/Down/Up/Move);
    * 'chk' mode: the flat toggle of the filter row -- form-colored
      face, a hand-drawn checkbox glyph that scales with the font
      (the old 16px PNG could not), state kept by the owner.

    The single item's band is pinned to EXACTLY the control's pixel
    height (sync) -- never a pixel more. TATListbox pre-fills its
    whole face with the theme's list bg, then paints item 0's band
    over (0, 0, ClientWidth, ItemHeight) (DoPaintTo): a band of
    exactly h covers the entire face (no underfill strip), while
    UpdateScrollbars' auto rule (ItemCount*ItemHeight > Height,
    the FULL height, not ClientHeight) stays false -- the themed
    scrollbar strip (its light track + arrow buttons) can never
    appear beside the face, so the button keeps ONE background
    color and no arrows (the 22nd-release report: the v11 pin of
    h+4 put that strip down the right edge of every button).
    repaint() is the documented-cheap invalidate for a drawn
    listbox: SetItemHeight invalidates but early-exits on equal
    values (atlistbox.pas), so a -1 jitter is always a real
    repaint of the tiny control.

    The caption paints with ONE background under it -- the face.
    CANVAS_TEXT is Canvas.TextOut, which fills the glyphs' rectangle
    with the canvas's CURRENT brush before drawing them; the brush
    at caption time must therefore be the FACE color (it is re-set
    right before the text call, because the border frame leaves it
    at pal['border'] -- a second, darker box behind every caption,
    the 23rd-release report)."""

    def __init__(self, owner, name, cap, prop, on_click, mode='btn',
                 checked=False, colors=None, text_w=None):
        self.owner = owner
        self.name = name
        self.cap = cap
        self.mode = mode
        self.checked = checked
        self.colors = colors or _btn_palette()
        self.hover = False
        self.pressed = False
        self.h_ctl = 0
        self._ih = 0              # pinned item band (0 = not synced)
        self._text_w = text_w     # measured caption px (None: est.)
        prop = dict(prop)
        prop.update({
            # the caption also rides the item text: on a build where
            # LISTBOX_SET_DRAWN is missing the control degrades to a
            # plain listbox that still shows the caption
            'on_draw_item': self._ev_draw,
            'on_click': on_click,
            'on_mouse_enter': self._ev_enter,
            'on_mouse_exit': self._ev_exit,
            'on_mouse_down': self._ev_down,
            'on_mouse_up': self._ev_up,
        })
        owner._add('listbox_ex', name, prop)
        try:
            self.h_ctl = ct.dlg_proc(owner.h, ct.DLG_CTL_HANDLE,
                                     name=name)
        except Exception:
            self.h_ctl = 0
        try:
            ct.listbox_proc(self.h_ctl, ct.LISTBOX_ADD, index=-1,
                            text=cap)
            ct.listbox_proc(self.h_ctl, ct.LISTBOX_SET_DRAWN, index=1)
        except Exception:
            pass    # pre-listbox_proc build: plain listbox fallback

    # -- geometry ------------------------------------------------------

    def width_px(self):
        """The measured pixel width: caption + symmetric padding (+
        the glyph and its gaps in chk mode)."""
        s = _px_scale()
        tw = self._text_w if self._text_w is not None \
            else len(self.cap) * 7
        if self.mode == 'chk':
            g = _chk_glyph_px()
            return (tw + int(BTN_PAD_X * s) + g +
                    int(6 * s) + int(4 * s))
        return tw + 2 * int(BTN_PAD_X * s)

    def sync(self):
        """Post-DLG_SCALE pinning (the build-time 'w' was dlg units
        that the scale pass has applied; the real pixel width is
        re-set here, like the saved-geometry restore does): the
        measured width, then the item band from the control's LIVE
        pixel height so the single drawn item covers the face."""
        try:
            ct.dlg_proc(self.owner.h, ct.DLG_CTL_PROP_SET,
                        name=self.name, prop={'w': self.width_px()})
        except Exception:
            pass
        try:
            d = ct.dlg_proc(self.owner.h, ct.DLG_CTL_PROP_GET,
                            name=self.name) or {}
            hh = int(d.get('h', 0) or 0)
            if hh > 0:
                # EXACTLY the live pixel height, never hh+N: the
                # scrollbar rule is ItemCount*ItemHeight > Height
                # (atlistbox.pas UpdateScrollbars, the FULL height),
                # so hh+4 flashed the themed scrollbar -- a second
                # background (its track) plus its arrow buttons
                # down the right edge of every button. hh alone
                # keeps 1*hh > hh false AND covers the whole face
                # (DoPaintTo paints item 0's band at
                # (0,0,ClientWidth,ItemHeight) over the theme's
                # pre-filled bg): one color, no arrows.
                self._ih = hh
                ct.listbox_proc(self.h_ctl, ct.LISTBOX_SET_ITEM_H,
                                index=self._ih)
        except Exception:
            pass

    # -- state / repaint -------------------------------------------------

    def set_checked(self, on):
        """Render the toggle state (chk mode); no-op for plain
        buttons."""
        if self.mode != 'chk' or self.checked == bool(on):
            return
        self.checked = bool(on)
        self.repaint()

    def repaint(self):
        if not self.h_ctl or not self._ih or \
                getattr(self.owner, '_torn', False):
            return
        try:
            # jitter DOWNWARD (hh-1 then hh): both calls land before
            # the next paint, but should anything ever pump messages
            # between them, the transient state must be the invisible
            # one -- a 1px underfill -- never hh+1, which flips
            # ItemCount*ItemHeight > Height true and flashes the
            # scrollbar the sync() comment describes.
            ct.listbox_proc(self.h_ctl, ct.LISTBOX_SET_ITEM_H,
                            index=max(1, self._ih - 1))
            ct.listbox_proc(self.h_ctl, ct.LISTBOX_SET_ITEM_H,
                            index=self._ih)
        except Exception:
            pass

    # -- events ------------------------------------------------------------

    def _ev_draw(self, id_dlg, id_ctl, data='', info=''):
        if not isinstance(data, dict):
            return
        try:
            if int(data.get('index', -1)) != 0:
                return
        except (TypeError, ValueError):
            return
        rect = data.get('rect')
        canvas = data.get('canvas')
        if canvas is not None and rect is not None:
            self._paint(canvas, rect)

    def _ev_enter(self, id_dlg, id_ctl, data='', info=''):
        if not self.hover:
            self.hover = True
            self.repaint()

    def _ev_exit(self, id_dlg, id_ctl, data='', info=''):
        if self.hover or self.pressed:
            self.hover = False
            self.pressed = False
            self.repaint()

    def _ev_down(self, id_dlg, id_ctl, data='', info=''):
        if isinstance(data, dict):
            try:
                if int(data.get('btn', -1)) == 0:
                    self.pressed = True
                    self.repaint()
            except (TypeError, ValueError):
                pass

    def _ev_up(self, id_dlg, id_ctl, data='', info=''):
        if self.pressed:
            self.pressed = False
            self.repaint()

    # -- painting ------------------------------------------------------

    def _paint(self, canvas, rect):
        x0, y0, x1, y1 = rect
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            return
        pal = self.colors
        try:
            if self.mode == 'chk':
                # flat toggle: the form's own color shows through, a
                # whisper of tint on hover/press -- matches the row's
                # other flat furniture (labels, edits' chrome)
                face = pal['flat_hover'] \
                    if (self.hover or self.pressed) else pal['bg']
                ct.canvas_proc(canvas, ct.CANVAS_SET_BRUSH,
                               color=face, style=ct.BRUSH_SOLID)
                ct.canvas_proc(canvas, ct.CANVAS_RECT_FILL,
                               x=x0, y=y0, x2=x1, y2=y1)
            else:
                # the button face: a shade of the form toward its text
                # color, pressed > hover > passive, plus the border
                # that makes it read as a button next to the
                # paper-colored input boxes
                face = pal['face_down'] if self.pressed else \
                    (pal['face_hover'] if self.hover else pal['face'])
                ct.canvas_proc(canvas, ct.CANVAS_SET_BRUSH,
                               color=face, style=ct.BRUSH_SOLID)
                ct.canvas_proc(canvas, ct.CANVAS_RECT_FILL,
                               x=x0, y=y0, x2=x1, y2=y1)
                ct.canvas_proc(canvas, ct.CANVAS_SET_BRUSH,
                               color=pal['border'],
                               style=ct.BRUSH_SOLID)
                ct.canvas_proc(canvas, ct.CANVAS_RECT_FRAME,
                               x=x0, y=y0, x2=x1 - 1, y2=y1 - 1)

            # caption: the UI font, SET like the row painter sets it
            # (build-proof: never trust a preset), centered on the
            # face (left of it in chk mode, after the glyph)
            ct.canvas_proc(canvas, ct.CANVAS_SET_FONT,
                           text=_ui_font()[0], color=pal['fg'],
                           size=_ui_font_pt(), style=0)
            sz = ct.canvas_proc(canvas, ct.CANVAS_GET_TEXT_SIZE,
                                text=self.cap) or (0, 0)
            tw, th = int(sz[0]), int(sz[1])
            ty = y0 + max(0, (h - th) // 2)
            if self.mode == 'chk':
                g = _chk_glyph_px()
                s = _px_scale()
                gx = x0 + int(BTN_PAD_X * s) // 2
                gy = y0 + max(0, (h - g) // 2)
                # the box...
                ct.canvas_proc(canvas, ct.CANVAS_SET_BRUSH,
                               color=pal['border'],
                               style=ct.BRUSH_SOLID)
                ct.canvas_proc(canvas, ct.CANVAS_RECT_FRAME,
                               x=gx, y=gy, x2=gx + g, y2=gy + g)
                # ...and the check stroke when on
                if self.checked:
                    ct.canvas_proc(canvas, ct.CANVAS_SET_PEN,
                                   color=pal['fg'],
                                   size=max(1, g // 7),
                                   style=ct.PEN_STYLE_SOLID)
                    m = max(2, g // 4)
                    ct.canvas_proc(canvas, ct.CANVAS_LINE,
                                   x=gx + m, y=gy + g // 2,
                                   x2=gx + g // 2, y2=gy + g - m)
                    ct.canvas_proc(canvas, ct.CANVAS_LINE,
                                   x=gx + g // 2, y=gy + g - m,
                                   x2=gx + g - m, y2=gy + m)
                tx = gx + g + int(6 * s)
            else:
                tx = x0 + max(0, (w - tw) // 2)
            # ONE background, caption included (the 23rd-release
            # report): CANVAS_TEXT is Canvas.TextOut, and LCL TextOut
            # paints its glyphs on an OPAQUE rectangle of the canvas's
            # CURRENT brush -- which the border frame above had just
            # left at pal['border'], so every caption sat on a second,
            # darker box (the user's screenshot). Re-set the brush to
            # the very face color underneath: the caption's text
            # rectangle then lands in the face color pixel-for-pixel
            # and disappears. (BRUSH_CLEAR would kill the box too, but
            # it leaves the canvas's brush TRANSPARENT into the next
            # paint pass, silently nulling TATListbox's own pre-fill
            # and border FrameRect -- DoPaintTo assigns Brush.Color
            # only, never the style, atlistbox.pas 504/586. A solid
            # face brush is state-neutral: every later fill/frame
            # re-sets its own brush first.)
            ct.canvas_proc(canvas, ct.CANVAS_SET_BRUSH,
                           color=face, style=ct.BRUSH_SOLID)
            if self.cap:
                ct.canvas_proc(canvas, ct.CANVAS_TEXT,
                               text=self.cap, x=tx, y=ty)
        except Exception:
            return


_UI_SCALE = None


def _ui_scale():
    """The scale factor the form's DLG_SCALE call applies (1.0 when it
    cannot be measured). CudaText scales a dialog by
    Screen.PixelsPerInch/96: control geometry AND control fonts grow
    by that factor. Everything the plugin passes through dlg_proc
    (anchors, autosize labels, w/h of controls) is scaled by the
    form's own DLG_SCALE -- but the raw pixel values handed to
    listbox_proc / statusbar_proc (column widths, status cell sizes)
    BYPASS it, while the text drawn into them does not.
    Unscaled, a 125%-DPI box draws 1.25x-wide text into 96-DPI
    columns: the date cells were the first casualty ('2026-10-04 …'
    ellipsized on the reporting box).

    Measured, not configured: a throwaway 96x96 probe form is
    DLG_SCALEd and read back, so the factor is whatever the HOST
    actually applies (a no-op DLG_SCALE -- older builds, the test
    simulator -- yields exactly 1.0). Cached per process; never
    raises (any probe failure sticks to 1.0 = today's layout)."""
    global _UI_SCALE
    if _UI_SCALE is None:
        try:
            h = ct.dlg_proc(0, ct.DLG_CREATE)
            try:
                ct.dlg_proc(h, ct.DLG_PROP_SET, prop={'w': 96, 'h': 96})
                ct.dlg_proc(h, ct.DLG_SCALE)
                d = ct.dlg_proc(h, ct.DLG_PROP_GET) or {}
                w = float(d.get('w', 96) or 96)
            finally:
                ct.dlg_proc(h, ct.DLG_FREE)
            _UI_SCALE = max(1.0, w / 96.0)
        except Exception:
            _UI_SCALE = 1.0
    return _UI_SCALE


_FONT_SCALE = None


def _ui_font():
    """(name, size_pt) of the CudaText UI font -- the font every themed
    control renders with: menus, tabs, side panels, and the AT
    listboxes/statusbars/buttons of plugin dialogs (ATFlatTheme), our
    results list included. Source: the global options 'ui_font_name' /
    'ui_font_size' (+ the OS suffix PROC_GET_OS_SUFFIX returns -- ''
    on Windows, '__linux', '__mac', ...; both app_procs predate the
    plugin's api 1.0.483 requirement by ~100 releases). The app loads
    the very same options into UiOps.VarFontName/VarFontSize and from
    there into ATFlatTheme, which TATListbox's canvas starts every
    owner-draw pass from -- so this is the plugin's view of what the
    list is about to draw. Defaults 'default'/9 = the app's own
    fallbacks; never raises (any failure keeps them)."""
    name = 'default'
    size = 9
    try:
        suffix = ct.app_proc(ct.PROC_GET_OS_SUFFIX, '') or ''
        n = ctx.get_opt('ui_font_name' + suffix, '',
                        user_json='user.json')
        if isinstance(n, str) and n:
            name = n
        s = ctx.get_opt('ui_font_size' + suffix, 0,
                        user_json='user.json')
        if s:
            size = max(4, min(72, int(s)))
    except Exception:
        pass
    return name, size


def _font_scale():
    """How much wider/taller the UI font renders vs the 9pt default
    (1.0 on a default-config box). Replicates the app's own chain:
    ATFlatTheme.FontSize = ui_font_size, TATListbox draws it at
    DoScaleFont(size) = size * ScaleFontPercents div 100, where
    ScaleFontPercents is the 'ui_scale_font' option (read via
    PROC_CONFIG_SCALE_GET's 2nd tuple item), and converts points to
    pixels with CanvasFontSizeToPixels(s) = s*18 div 10 + 2. The DPI
    factor is deliberately dropped: it multiplies the current and the
    default font alike, so it cancels in the ratio -- and the metrics
    this feeds are already _ui_scale()d. Feeds _px_scale(); cached per
    process like _ui_scale; clamped to a sane 0.8..3.0 so a corrupt
    option cannot lay the window out absurdly."""
    global _FONT_SCALE
    if _FONT_SCALE is None:
        px = 18.2       # CanvasFontSizeToPixels(9) -- the default font
        try:
            size = _ui_font()[1]
            pct = 100
            sc = ct.app_proc(ct.PROC_CONFIG_SCALE_GET, '')
            if isinstance(sc, (tuple, list)) and len(sc) == 2:
                pct = int(sc[1] or 100)
            px = (size * max(10, min(500, pct)) / 100.0) * 1.8 + 2
        except Exception:
            pass
        _FONT_SCALE = max(0.8, min(3.0, px / 18.2))
    return _FONT_SCALE


_UI_FONT_PT = None


def _ui_font_pt():
    """The UI font size in POINTS, exactly as the app's themed controls
    draw it: DoScaleFont(ui_font_size) = ui_font_size *
    ui_scale_font // 100 -- the same ScaleFontPercents PROC_CONFIG_
    SCALE_GET's 2nd item carries (integer division, like Pascal's
    div, so 9 * 150 // 100 = 13 on both sides of the API). This is
    the value the painter hands to CANVAS_SET_FONT: the row text was
    reported small twice -- once with text='default' (v5: overwrote
    the name only, the size fell back to the canvas default ~8pt) and
    once with an empty name kept for TATListbox's DoPaintTo preset
    (v6: the preset is real in current upstream but demonstrably
    absent on the reported build -- the rows stayed small), so the
    size is no longer inferred from anything: it is SET, from the
    same options the app loads into ATFlatTheme. Guarded like its
    siblings (never raises, min 1pt so the >0 skip in canvas_proc
    can never drop it); cached per process."""
    global _UI_FONT_PT
    if _UI_FONT_PT is None:
        size = _ui_font()[1]
        pct = 100
        try:
            sc = ct.app_proc(ct.PROC_CONFIG_SCALE_GET, '')
            if isinstance(sc, (tuple, list)) and len(sc) == 2:
                pct = int(sc[1] or 100)
        except Exception:
            pass
        _UI_FONT_PT = max(1, size * max(10, min(500, pct)) // 100)
    return _UI_FONT_PT


def _px_scale():
    """The factor for every raw-pixel metric that must follow the LIST
    TEXT: DPI (_ui_scale) times the UI-font factor (_font_scale) --
    column widths, tree-gutter offsets, expand-marker zone, statusbar
    cell sizes. Values passed through dlg_proc (control w/h, sp_*)
    need only the font part: the form's own DLG_SCALE handles their
    DPI. Mouse-precision constants (the boundary grab tolerance) stay
    at _ui_scale() -- they track the hand, not the glyphs."""
    return _ui_scale() * _font_scale()


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
    # Checkbox glyphs for the themed checks (button_ex + imagelist,
    # cuda_prefs' own pattern -- see _add_check): semi-transparent
    # gray, so the SAME glyph reads on light and dark themes.
    'chk_on': (
        'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAeUlEQVR42mNgGDQgMTFRE4'
        'iDicG4DAju7OzcumbNmiP4MFDdMpwGgBQ8JgDINiA7O/suCJNlAEijk5PTHaIM2L1798PJ'
        'kyffh2kG8WGaifICSDNIA4iGaQZhEJvoMIBpQtdMtAHINiN7h6RYQA8LogwASRKDGYYXAA'
        'DbMC4B9VuU7gAAAABJRU5ErkJggg=='
    ),
    # 178 bytes
    'chk_off': (
        'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAOUlEQVR42mNgGDQgMTFRE4'
        'iDicG4DAju7OzcumbNmiP4MFDdMpwGgBQ8JgBGDRj+BoAkicEMwwsAABiEUk9D2QrZAAAA'
        'AElFTkSuQmCC'
    ),
    # 114 bytes
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


def _book_listing(dt, n_entries, side, t0=None):
    """One directory listing finished: profiler row + facts counters.
    t0 (the listing's start) is passed through as the row's SPAN, with
    the owning walk as its parent hint: the report nests this row
    under that walk (see profiling.py, _Row.spans) instead of double-
    counting it against the walk AND the worker wall. The hint matters
    in a parallel scan, where both walks start at the same instant
    and time containment alone could pin a listing to the wrong
    tree's walk.
    """
    if Profiler.is_enabled():
        parent = 'dirs:walk_' + side if side in ('left', 'right') \
            else None
        Profiler.mark_standalone('dirs:listing', dt, 1, dt, t0=t0,
                                 parent=parent)
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
            Profiler.mark_standalone('dirs:quick_content', dt, 1, dt,
                                     t0=t0)
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
    come straight from the directory listing: on Windows scandir's
    DirEntry carries the stat data from the listing itself, so a
    whole tree walks with ZERO per-file stat() calls and zero
    lstat() calls -- on a source where every metadata syscall costs
    100-200 ms (cold antivirus pass, network share, cloud
    placeholder filter) that alone halves the syscall count of the
    old os.walk + os.stat walk.

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
    _book_listing(time.perf_counter() - t0, len(entries), side, t0)
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
                 method=METHOD_CONTENTS, cprofile_on=False,
                 scan_mode=SCAN_MODE_DEFAULT):
        super().__init__(daemon=True, name='Differ2DirCompare')
        self.dir_l = dir_l
        self.dir_r = dir_r
        self.recursive = recursive
        self.mask = mask
        self.quick_only = quick_only
        self.method = method
        # Threading shape of this scan (differ2.dirs.scan_threading;
        # see the module docstring). The default is 'main' (update
        # 10): the FORM calls run() inline so the whole scan runs on
        # the main thread -- the attribute stays on the worker so
        # the facts block can name the shape that produced the run.
        # 'pool'/'serial' (the threaded engine, for very big trees)
        # are the opt-in shapes.
        self.scan_mode = scan_mode if scan_mode in _SCAN_MODES \
            else SCAN_MODE_DEFAULT
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
            # slow -- the row keeps that question answerable. Booked
            # with its span: the report nests it under dirs:worker.
            lag = time.perf_counter() - self._t_spawn
            try:
                Profiler.mark_standalone('dirs:spawn_lag', lag, 1, lag,
                                         t0=self._t_spawn)
            except Exception:
                pass
        pr = s = None
        if self.cprofile_on and Profiler.is_enabled():
            # The cProfile layer's SETUP is timed and booked: the
            # imports inside start_profiling (cProfile + profile on
            # first use; pstats is pre-warmed by _prof_begin_scan on
            # the main thread) can cost SECONDS when CudaText and its
            # Python live on a slow / heavily-filtered drive -- a
            # per-SESSION cost that used to hide inside dirs:worker's
            # wall with no row of its own.
            try:
                t0 = time.perf_counter()
                pr, s = start_profiling()
                self.cprofile = (pr, s)
                dt = time.perf_counter() - t0
                Profiler.mark_standalone('dirs:cprofile_setup',
                                         dt, 1, dt, t0=t0)
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
        thread, so the scan runs serially on purpose, SCAN_POOL_THREADS
        is 1, or the scan shape is serial/main -- differ2.dirs.scan_
        threading) the same _scan_dir runs inline, the left tree first,
        then the right.

        Per-side totals are booked as dirs:walk_left / dirs:walk_right
        (thread-safe standalone marks, WITH their time spans: the
        report nests the listings that ran inside each walk under it,
        and nests the walks under dirs:worker -- so the family's SELF
        times sum to ~100% of the scan wall instead of counting the
        same milliseconds two or three times) when a side fully drains
        -- its last outstanding job completed and discovered nothing
        new.
        """
        prof = Profiler.is_enabled()
        sides = ('left', 'right')
        # facts-counter key suffix per side ('files_l' / 'files_r' ...
        # -- the keys _prof_facts_block prints; booking 'files_' +
        # side used to create files_left/files_right, which nothing
        # ever printed: the report said "file rows: 0" about a scan
        # that had built every row)
        fkey = {'left': 'l', 'right': 'r'}
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
                        _facts_add(**{'files_' + fkey[side]: len(f),
                                      'dirs_' + fkey[side]: len(d)})
                    if self.recursive:
                        stack.extend(subs)
                if prof:
                    Profiler.mark_standalone(
                        'dirs:walk_' + side,
                        time.perf_counter() - t0, 1,
                        time.perf_counter() - t0, t0=t0,
                        parent='dirs:worker')
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
                    _facts_add(**{'files_' + fkey[side]: len(f),
                                  'dirs_' + fkey[side]: len(d)})
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
                                time.perf_counter() - t0s[side], 1,
                                time.perf_counter() - t0s[side],
                                t0=t0s[side], parent='dirs:worker')
        return files['left'], dirs['left'], files['right'], dirs['right']

    def _scan(self):
        """Walk both trees, then build the rows. Every step is booked
        to the profiler as thread-safe standalone marks (this method
        runs on the worker thread -- see the note in _quick_contents_equal
        about why not sections). The walk and the equal-size content
        tests run on the scan pool (see the module docstring's "The
        scan engine"); the pool is skipped while the cProfile layer
        traces this thread or the scan shape is serial/main
        (differ2.dirs.scan_threading), so every call runs HERE."""
        prof = Profiler.is_enabled()
        pool = None
        if (self.cprofile is None and SCAN_POOL_THREADS > 1
                and self.scan_mode == SCAN_MODE_POOL):
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
                dt = time.perf_counter() - t0
                Profiler.mark_standalone('dirs:pair_keys', dt, 2, dt,
                                         t0=t0)  # two merges+sorts per call
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
                dt = time.perf_counter() - t0
                # Booked with its span: the report nests it under
                # dirs:worker (and nests the content tests that ran
                # inside this stretch under IT).
                Profiler.mark_standalone('dirs:dir_rows', dt,
                                         len(dir_keys), dt, t0=t0)

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
                # safe standalone marks WITH spans, so the report
                # subtracts them from THIS row); the row-building self
                # time is the difference. Booked with the file count so
                # the per-file average is readable in the report.
                dt = time.perf_counter() - t0
                Profiler.mark_standalone('dirs:file_rows', dt, n_files,
                                         dt, t0=t0)
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
            # cProfile stdlib PRE-WARM, on the MAIN thread, BEFORE the
            # scan starts: start_profiling() lazily imports cProfile +
            # profile, stop_profiling() imports pstats. When CudaText
            # and its Python live on a slow or heavily-filtered drive,
            # those imports cost SECONDS -- and they used to happen
            # inside the scanner thread, hiding as an unexplained gap
            # between dirs:worker's wall and the profiled window (the
            # imports run before pr.enable(), so cProfile itself never
            # saw them). Imported here, the cost becomes a named,
            # per-SESSION row outside dirs:scan_wall, and the scan
            # itself starts clean.
            if cprof_scan:
                try:
                    import importlib
                    t0 = time.perf_counter()
                    importlib.import_module('cProfile')  # pre-warm
                    importlib.import_module('pstats')    # pre-warm
                    dt = time.perf_counter() - t0
                    Profiler.mark('dirs:cprofile_import', dt, 1, dt,
                                  t0=t0)
                except Exception:
                    pass
        _prof_users += 1
        token = Profiler.start_async_pair('dirs:scan_wall', 'dirs:worker')
    return token, cprof_scan


def _prof_facts_block(worker):
    """The 'folder scan facts' epilogue lines: what the scan-facts
    counters say about THIS run (see _SCAN_FACTS). Printed right after
    the section report -- deliberately compact, four statements that
    answer the four questions every slow-compare report raises:

      1. which threading SHAPE produced the numbers (the scan mode
         line -- differ2.dirs.scan_threading);
      2. how many directory listings ran, and how much of the wall
         time was pure listing latency (metadata round trips -- the
         same number any other tool, WinMerge included, pays on a
         cold tree);
      3. how big the trees were (entries / file rows / folder rows);
      4. how much file CONTENT the compare had to read (pairs stopped
         at the first differing chunk vs read to the end), and
         whether the scan ran serially because the cProfile layer was
         on (the function-report mode -- slower on purpose; turn it
         off to measure speed)."""
    facts = _SCAN_FACTS
    if not facts:
        return ''
    lines = ['--- Differ 2 folder scan facts ---']
    # The scan's threading SHAPE (differ2.dirs.scan_threading) --
    # which engine produced every number below. First content line
    # so every pasted report names its own shape. 'main' is the
    # default; the threaded shapes are the opt-in for very big trees.
    mode = getattr(worker, 'scan_mode', SCAN_MODE_DEFAULT) \
        if worker is not None else SCAN_MODE_DEFAULT
    thr_nm = 'MAIN' if mode == SCAN_MODE_MAIN else 'SCANNER'
    if worker is not None and getattr(worker, 'cprofile_on', False):
        mode_txt = 'SERIAL on the {} thread (differ2.advanced.' \
                   'enable_cprofile is on: the pool is skipped so ' \
                   'cProfile sees every call) -- set it off and ' \
                   'rescan to measure real speed'.format(thr_nm)
    elif mode == SCAN_MODE_MAIN:
        mode_txt = ('MAIN thread, synchronous (differ2.dirs.scan_'
                    'threading=main, the default): no worker thread, '
                    'no pool, no UI ticks while scanning -- the '
                    'window was frozen for the whole scan, rows '
                    'appeared at the end')
    elif mode == SCAN_MODE_SERIAL:
        mode_txt = ('SERIAL on the SCANNER thread (differ2.dirs.scan_'
                    'threading=serial): no pool, walk directory by '
                    'directory, UI ticks + row streaming as usual')
    else:
        mode_txt = ('PARALLEL (scanner thread + scan pool of {}) -- '
                    'the threaded engine, for very big trees (a '
                    'frozen window there is worse than a slower '
                    'walk)').format(SCAN_POOL_THREADS)
    lines.append('scan mode: ' + mode_txt)

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
    'vis' prop). show() returns (left, right) or None.

    Resizable (DBORDER_SIZE): the combos stretch between the labels
    and the Browse buttons; the size is remembered in the settings
    (dirs.picker_geom, "w,h") and re-applied after DLG_SCALE. The
    layout is DLG_SCALEd like the main window -- without it a 125%-DPI
    box keeps 96-DPI label widths under a bigger font and clips the
    captions (the reported "Left folder (ol" bug).

    Anchor discipline (the API's anchor semantics are EAGER and
    ADDITIVE -- both mattered here, see history.txt 13th release):
    * a control is born with a_l/a_t ALREADY anchored to the form
      (LCL default). Setting only a_r/a_b therefore gives the control
      left AND right (top AND bottom) anchors -> it STRETCHES to fill
      the form. Every right/bottom-anchored control below clears the
      opposite side with 'a_l': None / 'a_t': None first.
    * an anchor target is resolved by NAME at prop-set time; a target
      that does not exist YET silently anchors to the form instead.
      So every target is created before the control that anchors to
      it: per row label -> Browse -> combo (the combo's a_r targets
      the Browse button), and Cancel before Compare (Compare's a_r
      targets Cancel).

    The two combos end up equally wide by construction, not by
    coincidence: they stretch between their row label's right edge
    and the identical right-anchored Browse buttons, so their widths
    differ by exactly the label-caption difference -- _equalize_
    labels() pins both labels to the wider caption's live width
    after the build (prop_get returns the LIVE control geometry),
    giving both combos the same left edge. See history.txt 17th
    release."""

    W = 640
    H = 150
    MIN_W = 560
    MIN_H = 140
    LABEL_W = 150          # "Right folder (new):" fits at 96 DPI
    BRW_W = 90
    ROW_Y0 = 12
    ROW_DY = 34

    def __init__(self):
        self.h = 0
        self.result = None
        self.ctl = {}       # name -> control index
        self.ctl_rev = {}   # control index -> name
        self._drawn = {}    # name -> _DrawnBtn (the 21st release:
                            # the picker's four buttons are drawn
                            # buttons, themed like the main window's)
        self._probe_gap = None   # filled by _build (hidden probe)

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
            'h': self.H,
            'w_min': self.MIN_W, 'h_min': self.MIN_H,
            'border': ct.DBORDER_SIZE,   # resizable; combos stretch
        }
        if bg is not None:
            prop['color'] = bg
        ct.dlg_proc(self.h, ct.DLG_PROP_SET, prop=prop)

        # Folder-input colors (6th theme round: 'the box background in
        # grey theme is green'). The combos are native TComboBox
        # controls, so the plugin must color them itself -- with the
        # app's OWN single-line-input chain OtherTextBg/OtherTextFont
        # -> EdTextBg/EdTextFont (what form_find / the Command Palette
        # / CodeTreeFilterInput / the one-line dialog editors all
        # use). The 5th round's ListBg chain was wrong: ListBg's
        # built-in default (nColorListBack, proc_colors.pas) is LIGHT
        # GREEN $b4d8a8, and themes that skip the key (the grey
        # theme) rendered the boxes GREEN. EdTextBg/EdTextFont are
        # spelled out by every real theme (light and dark) and their
        # built-in defaults are plain light gray -- a theme without
        # them paints the app's own find dialog equally broken, so
        # this chain can never go green and matches the app exactly.
        ed_bg = _theme_color('OtherTextBg', _theme_color('EdTextBg'))
        ed_fg = _theme_color('OtherTextFont', _theme_color('EdTextFont'))

        # The measuring probe + the drawn-button palette (the same
        # machinery as the main window's rows -- see _DrawnBtn): the
        # picker's four buttons are DRAWN buttons, so their captions
        # are guaranteed readable (the form's own bg/font pair, not
        # the theme's Button* trio) and their widths MEASURED (no
        # eaten text), in every theme and at any UI font.
        _add_probe(self._add)
        self._probe_gap = _probe_gap(self.h)
        _pal = _btn_palette()

        for i, (side, label, hist, init) in enumerate((
                ('left',  _('Left folder (old):'),  hist_l, dir_l),
                ('right', _('Right folder (new):'), hist_r, dir_r))):
            top = ('', '[') if i == 0 else ('left', ']')
            sp_t = self.ROW_Y0 if i == 0 else self.ROW_DY - 26
            # autosize: the caption never clips at any DPI/font -- the
            # combo follows the label's real right edge via its a_l
            self._add('label', 'lab_' + side, {
                'cap': label,
                'w': self.LABEL_W, 'h': 20,
                'autosize': True,
                'a_l': ('', '['), 'sp_l': 12,
                'a_t': top, 'sp_t': sp_t + 5,
                'font_color': _theme_color('TabFont'),
            })
            # Browse BEFORE the combo: the combo's a_r targets it, and
            # an anchor target must exist (and carry its name) when the
            # anchor is set. 'a_l': None clears the form-left anchor a
            # new control is born with -- without it the button keeps
            # left AND right anchors and stretches across the form.
            # A DRAWN button (see the probe block above): themed face
            # + measured width, like every button in the main window.
            self._drawn['brw_' + side] = _DrawnBtn(
                self, 'brw_' + side, _('Browse...'), {
                    'w': self.BRW_W, 'h': 26,
                    'a_l': None, 'a_r': ('', ']'), 'sp_r': 12,
                    'a_t': top, 'sp_t': sp_t,
                },
                self._on_button, colors=_pal,
                text_w=_probe_text_w(self.h, _('Browse...'),
                                     self._probe_gap),
            )
            # the only control meant to stretch: BOTH a_l (label's
            # right) and a_r (Browse's left) -- the two fixed controls
            # it sits between.
            p = {
                'w': 330, 'h': 26,
                'a_l': ('lab_' + side, ']'), 'sp_l': 4,
                'a_r': ('brw_' + side, '['), 'sp_r': 6,
                'a_t': top, 'sp_t': sp_t,
                'items': '\t'.join(hist),
                'val': init,
                'texthint': _('Type or pick a folder'),
            }
            if ed_bg is not None:
                p['color'] = ed_bg
            if ed_fg is not None:
                p['font_color'] = ed_fg
            self._add('combo', side, p)

        # Cancel FIRST: Compare's a_r targets it. Both clear the born-
        # with a_l/a_t ('a_l': None, 'a_t': None) -- right+bottom
        # anchored, fixed size; a half-cleared pair stretches into a
        # button covering the whole dialog (the 12th-release bug).
        # DRAWN buttons (see the Browse comment): readable captions +
        # measured widths in every theme.
        self._drawn['cancel'] = _DrawnBtn(
            self, 'cancel', _('Cancel'), {
                'w': 90, 'h': 28,
                'a_l': None, 'a_t': None,
                'a_r': ('', ']'), 'sp_r': 12,
                'a_b': ('', ']'), 'sp_b': 12,
            },
            self._on_button, colors=_pal,
            text_w=_probe_text_w(self.h, _('Cancel'), self._probe_gap),
        )
        self._drawn['ok'] = _DrawnBtn(
            self, 'ok', _('Compare'), {
                'w': 100, 'h': 28,
                'a_l': None, 'a_t': None,
                'a_r': ('cancel', '['), 'sp_r': 8,
                'a_b': ('', ']'), 'sp_b': 12,
            },
            self._on_button, colors=_pal,
            text_w=_probe_text_w(self.h, _('Compare'), self._probe_gap),
        )
        # Scale the built layout to the OS DPI (the main window has
        # always done this; the picker's fixed 96-DPI geometry is what
        # clipped the labels on high-DPI boxes), THEN re-apply the
        # saved size: it was captured post-scale, so applying it after
        # DLG_SCALE never double-scales (same order as the main
        # window's geometry).
        ct.dlg_proc(self.h, ct.DLG_SCALE)
        geom = self._restore_geom()
        if geom:
            try:
                ct.dlg_proc(self.h, ct.DLG_PROP_SET, prop=geom)
            except Exception:
                pass
        # LAST, with the layout final: pin the drawn buttons' measured
        # pixel widths and item bands (same as the main window).
        for b in self._drawn.values():
            b.sync()
        # and pin both row labels to one width so the two
        # combos render equally wide.
        self._equalize_labels()

    def _equalize_labels(self):
        """Both folder combos must have the SAME width (reported:
        'folder selector boxes must have same width'). A combo's left
        edge follows its row label's right edge and its right edge
        the Browse button (all four buttons right-anchored at the
        same offset), so the two combos differ by exactly the
        difference of the two autosized captions ('Left folder
        (old):' is narrower than 'Right folder (new):') -- the right
        combo came out a few px narrower. The fix measures the two
        labels AFTER the layout is final -- DLG_CTL_PROP_GET returns
        the LIVE control geometry (proc_customdialog.pas fills
        x/y/w/h from C.Left/Top/Width/Height), so the autosized,
        DLG_SCALEd widths are in -- takes the wider one and pins
        BOTH labels to it ('autosize': False + that 'w'): each
        caption still fits (every label is at least as wide as its
        own caption needed), and with identical label widths the
        anchored combos come out exactly equal. Run once after the
        build: the picker is modal, the font and DPI cannot change
        under it; guarded like every dlg helper (a failure just
        leaves the labels as built)."""
        try:
            ws = []
            for side in ('left', 'right'):
                d = ct.dlg_proc(self.h, ct.DLG_CTL_PROP_GET,
                                name='lab_' + side) or {}
                ws.append(int(d.get('w', 0) or 0))
            w = max(ws)
            if w > 0:
                for side in ('left', 'right'):
                    ct.dlg_proc(self.h, ct.DLG_CTL_PROP_SET,
                                name='lab_' + side,
                                prop={'autosize': False, 'w': w})
        except Exception:
            pass

    # -- geometry persistence ------------------------------------------

    @staticmethod
    def _restore_geom():
        """Saved picker size ("w,h" string) as a prop dict, or None.
        Single-line string for the same reason as the main window's
        geometry (cudax_lib's flat-key updater). State file, not the
        settings file (25th release)."""
        g = _get_state('dirs.picker_geom', '')
        if isinstance(g, str) and g:
            try:
                w, h = (int(v) for v in g.split(','))
                if w >= _PickerDialog.MIN_W and h >= _PickerDialog.MIN_H:
                    return {'w': w, 'h': h}
            except (TypeError, ValueError):
                pass
        return None

    @staticmethod
    def _save_geom(h):
        try:
            d = ct.dlg_proc(h, ct.DLG_PROP_GET) or {}
            w, hh = int(d.get('w', 0)), int(d.get('h', 0))
            if w >= _PickerDialog.MIN_W and hh >= _PickerDialog.MIN_H:
                _set_state('dirs.picker_geom', '%d,%d' % (w, hh))
        except Exception:
            pass

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
        self._save_geom(self.h)
        self.result = (pl, pr)
        ct.dlg_proc(self.h, ct.DLG_HIDE)

    @staticmethod
    def _remember(pl, pr):
        # History is stored as ONE '\n'-joined string per side (not a
        # list): cudax_lib's flat-key settings updater only handles
        # single-line values, and '\n' cannot occur in a path. In the
        # STATE file since the 25th release (with the picker geometry
        # and the window's geometry/widths).
        for key, path in (('dirs.hist_left', pl), ('dirs.hist_right', pr)):
            try:
                hist = _PickerDialog._load_hist(key)
                hist = [p for p in hist if p != path]
                hist.insert(0, path)
                _set_state(key, '\n'.join(hist[:HISTORY_MAX]))
            except Exception:
                pass

    # -- entry -----------------------------------------------------------

    @staticmethod
    def _load_hist(key):
        s = _get_state(key, '')
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

# Which icon (by _ICONS_PNG name) each FILE row status uses. Folder
# rows always paint the folder icon (see the painter) -- a one-sided
# folder must not swap it for a file icon: its color and Status
# caption carry the sidedness, the icon carries the KIND.
_ICON_OF = {
    ST_SAME:      'same',
    ST_DIFF:      'diff',
    ST_LONLY:     'lonly',
    ST_RONLY:     'ronly',
    ST_ERR:       'err',
    ST_DIR:       'folder',
    ST_DIR_LONLY: 'folder',
    ST_DIR_RONLY: 'folder',
}

# Tree-mode metrics (base 96-DPI pixels; _ui_scale multiplies them at
# paint time like every other raw-pixel metric): per-level indent,
# the expand-marker zone, and the icon/text x-offsets inside the Name
# cell. The expand marker is the ASCII '+'/'-' pair -- narrow glyphs
# every font has (the 14th release drew U+2192/U+2193 arrows there,
# wide enough on large-font boxes to collide with the folder icon at
# GUTTER_ICON, and the geometric triangles U+25B4/U+25BE render as
# tofu on real-world UI fonts). The header's sort marker keeps the
# U+2191/U+2193 arrows.
TREE_INDENT = 14         # per tree level
ARROW_W = 16             # hit zone of the +/- expand marker
GUTTER_ICON = 18         # status icon x inside the Name cell
GUTTER_TEXT = 36         # name text x inside the Name cell

# Sort-direction marker appended to the header caption of the sort
# column: the U+2191/U+2193 arrows (the user-requested glyphs -- the
# geometric triangles used before, U+25B4/U+25BE, are missing from
# real-world UI fonts and rendered as tofu boxes; plain ASCII 'v'/'^'
# render everywhere but were rejected as too crude).
_SORT_MARK = {False: ' \u2191', True: ' \u2193'}


class DirCompareForm:
    """One non-modal folder-compare window (there can be any number of
    them at once -- see the module docstring).

    Layout (all sizes are base pixels; DLG_SCALE adjusts them to the
    OS DPI before the saved geometry is re-applied, and _px_scale()
    -- DPI x UI-font factor -- does the same for the raw-pixel metrics
    that bypass DLG_SCALE: list columns, tree gutter, status cells.
    The row height is the control's own auto-fit, which the painter
    both feeds (it SETS the UI font before drawing) and guards (it
    measures the drawn glyphs and raises ItemHeight when the
    auto-fit band is tighter -- see _fit_item_h):

      [ New compare... ][ Swap sides ][ Refresh ]            [ Close ]
      [ left path edit           ][Browse...] [ right path edit      ][Br...]
      [x]Different [x]Only left [x]Only right [x]Identical [x]Subfolders
                              Mask:[ stretch edit ][ Apply ]
      +--------------------------------------------------------------+
      | Name | Folder | Status | Left size | Left date | R.size | R.date |
      | (owner-drawn listbox_ex, stretches with the form; WinMerge-  |
      |  style tree: folders folded by default, +/-/dbl-click/Enter |
      |  expands them INLINE -- children appear indented below the   |
      |  folder row, in the same window; column boundaries are       |
      |  drag-resizable -- see COLUMN RESIZE below)                  |
      +--------------------------------------------------------------+
      [ status line / progress                    ][ counts          ]

    The path edits are editable on purpose: type two paths and press
    Refresh (or Enter) to compare them without reopening any dialog.

    FLEX LAYOUT (21st release): nothing on the form has a live width
    that can collide with anything else. The two path edits STRETCH
    -- each between its form edge and its Browse button -- and the
    Browse buttons sit against a tiny label CENTERED on the form
    (a_l=('', '-') = the LCL asrCenter anchor), so each edit is
    exactly half the row at EVERY window width (the report: 'each
    one must occupy 50% width of the line ... when window is small
    they overlap on each other'). The mask edit stretches between
    the checks and Apply the same way. The list's Name column keeps
    a guaranteed floor (NAME_MIN_W -- _effective_cols) and the
    form's on_resize re-derives the column split; a restored
    geometry below the (font-scaled) w_min floor is dropped.

    THEMING: every color comes from the UI theme through
    _theme_color, whose clNone guard is the difference between
    'not themed' and a literal $1FFFFFFF passed to a color API (the
    5th-round white statusbar). EVERY button and toggle is a
    _DrawnBtn -- a one-item owner-drawn listbox whose face, border,
    caption color, hover/press shading and width are ALL the
    plugin's: TATButton (button_ex) paints strictly from the GLOBAL
    ATFlatTheme, so a theme that defines ButtonBgPassive without
    ButtonFont leaves captions at the dark built-in default
    (invisible on dark faces -- the 21st report) and no dialog prop
    can reach those painted surfaces; a native TCheckBox draws its
    caption through the OS theme and ignores Font.Color (the 5th
    round); a native TButton stays OS-gray. The drawn buttons'
    palette derives from the form's OWN bg/font pair (TabBg/TabFont
    -- _btn_palette): readable on every theme, and the face+border
    are distinct from both the form and the paper-colored 'editor'
    inputs, so a button reads as a button (the 21st report's last
    item). Widths are MEASURED through a hidden probe button's
    autosize (the app's own font math -- _add_probe), so captions
    can never be eaten. The toggle state lives in the plugin
    (self._show / self._recursive) -- _on_check flips it and
    _set_chk_state re-renders the drawn glyph. The three path/mask
    inputs are one-line 'editor' controls (_add_input: themed by
    the app, hint painted by the control). See the module
    docstring's 'Theming' chapter for the color chains.

    The results list is an owner-drawn listbox_ex (LISTBOX_SET_DRAWN):
    the control never paints items itself, it calls on_draw_item for
    every visible row and the form paints background + marker + icon +
    all cells -- which is what makes the full-line status colors
    possible (the dialog API's listview has no per-row colors at
    all). The colors are the diff-tab hunk colors (color_changed /
    color_deleted / color_added of Command.cfg), so a row's color
    matches exactly what its double-clicked compare tab paints; a
    FOLDER row is painted with the worst status of its subtree
    (WinMerge's rolled-up result -- a folder merely CONTAINING a
    different file is "Different"), and folders always carry the
    folder icon. The built-in column header is driven with the same
    pixel widths the painter uses, so header clicks (sorting -- the
    ONLY sort UI, the marker is the U+2191/U+2193 arrow) align with
    the drawn cells.

    TREE MODE (WinMerge): a recursive scan's flat row list is
    displayed as a tree -- each level sorted by the current column
    exactly like the old flat list, every folder FOLDED until the
    user expands it (click its +/- marker / double-click it / press
    Enter / the context menu); the children then appear indented
    under the folder row IN THIS WINDOW. A folder whose rows are all
    filtered out stays visible as long as anything below it is
    visible (a container); folders without scanned children
    (Subfolders off, or empty) keep the old double-click behavior --
    a drill-down window / the file manager. The expansion state is
    keyed by rel path and survives rescans of the same folder pair.

    COLUMN RESIZE: the dialog API's listbox header cannot be dragged,
    but the control DOES deliver on_mouse_down/up/move (data =
    {'btn','state','x','y'} client coords) over its rows area. The
    six internal column boundaries run through the rows; a left
    press within ~6px of one (the hover shows the H-split cursor)
    starts a drag that moves that boundary the way usual apps do
    (the 24th report: 'not like usual apps'): the column LEFT of
    the boundary takes what the drag gives it, the column RIGHT of
    it gives that width back -- every other column (Name included)
    keeps its width, so only the two cells flanking the dragged
    line change. Boundary 1 (Name|Folder) is the one exception:
    Name is the stretch column and simply absorbs, so the drag
    adjusts Folder INVERSELY (dragging right grows Name), with
    Folder's growth capped so Name never drops below its floor
    (NAME_MIN_W). Every column is now resizable from BOTH its
    edges -- including the LAST one (Right date, via the boundary
    on its left; the 24th report: 'last column cannot be
    resized' -- before, that boundary only adjusted Right size).
    Widths are clamped to MIN/MAX_COL_W, pushed live via
    LISTBOX_SET_COLUMNS (+ the header re-split over them), persisted
    as 'dirs.col_widths' at window close and resettable from the
    context menu. A move without the button held ends the drag (the
    release happened outside the control -- the state string's 'L').
    The pushed widths are the EFFECTIVE set (_effective_cols): on a
    window too narrow for the fixed sum plus NAME_MIN_W the fixed
    columns are scaled down proportionally, so the Name column is
    always visible (the 21st report: it went to ZERO -- TATListbox
    gives an auto column Max(0, ClientWidth - fixed sum)).

    HEADER/ROW ALIGNMENT: the pushed column widths are the ONLY
    thing that keeps the header and the drawn rows together --
    TATListbox.UpdateColumnWidths (atlistbox.pas) passes explicit
    sizes through RAW on every paint and never re-derives them from
    the live width, while the plugin's owner-drawn rows DO re-derive
    their cell layout from the painted row width on every paint.
    So any width change that does not run through _apply_cols
    leaves the header on the stale split while the rows spread over
    the live width (the 24th report's screenshot: every cell far
    right of its caption). The form's on_resize re-pushes, but a
    programmatic size change can MISS that event (the saved-geometry
    PROP_SET runs before the form is shown, when a formless LCL
    bounds change fires no OnResize), so _apply_cols also runs once
    more AFTER the geometry restore at build, and the painter
    GUARDS the alignment on every painted row: when the painted row
    width no longer matches the width the spec was pushed for
    (_cols_pushed_w), a one-shot timer re-pushes the spec outside
    the paint cycle (never re-entrant) -- the header can never stay
    misaligned, whatever event was missed.
    """

    DEF_W = 940
    DEF_H = 560
    MIN_W = 700
    MIN_H = 380

    # Row A (toolbar buttons) -- the w values are pre-DLG_SCALE
    # FALLBACKS only: every button's real width is MEASURED (the
    # hidden probe -- see _DrawnBtn), so no caption can ever clip.
    TOOLBAR_BTNS = (
        ('btn_new', _('New compare...'), 130),
        ('btn_swap', _('Swap sides'), 100),
        ('btn_refresh', _('Refresh'), 100),
    )

    # Row B (path edits): both editors STRETCH -- left one between
    # the form's left edge and its Browse button, right one between
    # its Browse button and the form's right edge -- and the two
    # Browse buttons sit against a tiny label CENTERED on the form
    # (a_l=('', '-')), so the two editors are exactly half the row
    # each at EVERY window width (21st report: 'each one must occupy
    # 50% width of the line'). PATH_W is the pre-DLG_SCALE fallback;
    # PATH_GAP is the centered splitter's width.
    PATH_W = 330
    PATH_GAP = 12
    BRW_W = 82

    # Row C (filters). The checks are DRAWN toggles now (_DrawnBtn
    # chk mode): the click fires on_click, the state lives here
    # (self._show / self._recursive) exactly as in the button_ex
    # rounds -- only the renderer changed.
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
        # Tree state (see the class docstring "TREE MODE"): expanded
        # folder rel paths -- keyed by path so it survives sort/filter
        # changes and rescans of the same pair (cleared when the
        # compared pair itself changes). _arrow_echo suppresses the
        # second on_click of a double-click on the expand marker
        # (the dbl handler leaves marker clicks to on_click). _hdr_h
        # (listbox header height in px) and _row_x0/_row_w (a row's
        # left edge / width) are calibrated from the painter and turn
        # the click's (x, y) into a row + marker-zone hit test -- and
        # the mouse events' x into a column-boundary one.
        # _item_h_fix: the row height the painter's guard raised the
        # band to (None = the control's own auto-fit already fit the
        # drawn font -- see _fit_item_h).
        self._expand = set()
        self._exp_roots = None          # (dir_l, dir_r) _expand is for
        self._arrow_echo = (-1, 0.0)    # (view index, perf_counter)
        self._hdr_h = None
        self._row_x0 = 0
        self._row_w = 0
        self._item_h_fix = None
        # Column-resize state (see the class docstring "COLUMN
        # RESIZE"): the live pixel widths of the six fixed columns
        # (Name stretches -- _col_spec), None while no drag runs;
        # _drag = (boundary 1..6, press x, left width at press,
        # right width at press or None on boundary 1), _zone =
        # the cursor is currently over a boundary (edge-triggered
        # cursor updates). _cols_pushed_w = the row width the
        # current column spec was pushed for (the painter's
        # alignment guard -- see "HEADER/ROW ALIGNMENT"),
        # _cols_heal_armed = a re-push one-shot is pending.
        self._col_w = self._load_col_w()
        self._drag = None
        self._zone = False
        self._cols_pushed_w = 0
        self._cols_heal_armed = False
        self._quick_only = bool(_get_opt('dirs.quick_only', False))
        self._method = _get_method()
        self._show = {                  # status filter checkboxes
            ST_DIFF: True, ST_LONLY: True, ST_RONLY: True, ST_SAME: True,
        }
        # The Subfolders toggle + the drawn-button registry. The
        # checks are _DrawnBtn toggles now (see _build Row C): the
        # LIVE state lives here (self._show / self._recursive) and
        # the drawn glyph just renders it -- a native TCheckBox has
        # no usable state channel in the dialog API ('val' does not
        # handle TATButton) and its caption ignores Font.Color under
        # Windows visual styles.
        self._recursive = True
        self._drawn = {}                 # name -> _DrawnBtn
        self._probe_gap = None           # filled by _build (probe)
        self._floor_px = 0               # measured w_min floor (build)
        # Handles of the one-line 'editor' inputs (ed_left/ed_right/
        # ed_mask): their text goes through Editor(handle) -- the
        # dialog API's 'val' does not handle TATSynEdit (see
        # _add_input); _ctl_val/_set_ctl_val route by this cache.
        self._ed_handles = {}
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
        return _theme_color('TabFont', _theme_color('ListFont', 0x000000))

    def _add_input(self, name, prop, text):
        """One single-line INPUT: an 'editor' control (TATSynEdit) in
        ModeOneLine -- the very control the app builds its own
        single-line inputs from (the find dialog's boxes, the Command
        Palette input; see the module docstring's Theming chapter).

        Why not a native 'edit' (TEdit), the 6th theme round in a
        row it bit us: (a) TEdit's TextHint goes to Windows as
        EM_SETCUEBANNER and WINDOWS paints the cue in the system
        gray -- no plugin API can recolor it, so the hints were
        reported unreadable on dark themes; TATSynEdit paints its
        OptTextHint itself in Colors.TextHintFont (clGray, italic,
        exactly the app's own placeholder look). (b) the box bg had
        to be themed by hand and the ListBg chain went green on
        themes that skip that key; the app themes the editor ITSELF
        at creation (EditorApplyTheme + the one-line branch:
        OtherText* -> EdText*), so no color props are passed at all.

        PROP_ONE_LINE gives the single-line behavior (gutter/
        scrollbars hidden, one line enforced, text centered).
        The UI font is pinned (dialog editors are born in the
        EDITOR's monospaced font) so the text keeps the size the
        TEdit boxes always had. 'val' does not handle TATSynEdit:
        the handle is cached in _ed_handles, the text goes through
        Editor.set_text_all, and _ctl_val/_set_ctl_val route
        editor-backed names the same way -- no call site changes."""
        prop = dict(prop)
        prop['font_name'] = _ui_font()[0]
        prop['font_size'] = _ui_font_pt()
        n = self._add('editor', name, prop)
        try:
            h = ct.dlg_proc(self.h, ct.DLG_CTL_HANDLE, name=name)
            ed = ct.Editor(h)
            try:
                ed.set_prop(ct.PROP_ONE_LINE, True)
            except Exception:
                pass        # pre-PROP_ONE_LINE build: multiline, but all
            self._ed_handles[name] = h   # the text/hint logic still works
            ed.set_text_all(text)
        except Exception:
            # Pre-Editor API: the control renders and shows the hint;
            # only programmatic text is lost (Browse/swap re-set it).
            self._ed_handles.pop(name, None)
        return n

    def _build(self):
        self.h = ct.dlg_proc(0, ct.DLG_CREATE)
        h = self.h
        geom = self._restore_geom()
        prop = {
            'cap': _('Compare folders'),
            'w': self.DEF_W, 'h': self.DEF_H,
            # The width floor carries the UI-FONT factor (DLG_SCALE
            # already covers the DPI half): the row-C checks autosize
            # to their captions, and a 14pt box needs ~1.5x the 9pt
            # floor or the rows collide no matter how flexible the
            # layout is. _restore_geom clamps to the same value.
            'w_min': int(self.MIN_W * max(1.0, _font_scale())),
            'h_min': self.MIN_H,
            'border': ct.DBORDER_SIZE,
            'taskbar': 1,          # own OS taskbar entry: the window
                                    # is restorable/pinnable like a
                                    # normal app window
            'keypreview': True,    # form-level Enter/Esc/F5 (on_key_down)
            'on_close': self._on_close,
            'on_key_down': self._on_key,
            'on_resize': self._on_resize,   # re-derive the column split
        }
        bg = _theme_color('TabBg', _theme_color('ListBg'))
        if bg is not None:
            prop['color'] = bg
        ct.dlg_proc(h, ct.DLG_PROP_SET, prop=prop)

        # Per-window imagelist (owned by the form -> freed with it):
        # the ROW icons the list's painter draws (the 21st release's
        # drawn checkbox toggles paint their glyph on the canvas
        # instead -- it scales with the font, a fixed 16px PNG cannot).
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

        tcol = self._text_color()
        # NO input-box colors here anymore: the three inputs are
        # one-line 'editor' controls themed BY THE APP at creation
        # (OtherText* -> EdText* -- _add_input), the fix for the
        # 6th-round green boxes (ListBg's built-in default is green
        # $b4d8a8) and the unreadable TEdit hints.

        # The measuring probe (see _add_probe) and the drawn buttons'
        # shared palette: every button and toggle below is a
        # _DrawnBtn -- a one-item owner-drawn listbox whose face,
        # border, caption, hover and width are ALL the plugin's (the
        # 21st release: TATButton's global-theme colors left captions
        # invisible on dark themes and its fixed widths ate captions
        # -- see the class docstring).
        _add_probe(self._add)
        self._probe_gap = _probe_gap(h)
        pal = _btn_palette()

        def _btn(name, cap, prop, mode='btn', checked=False):
            b = _DrawnBtn(self, name, cap, prop,
                          self._on_check if mode == 'chk'
                          else self._on_button,
                          mode=mode, checked=checked, colors=pal,
                          text_w=_probe_text_w(h, cap, self._probe_gap))
            self._drawn[name] = b
            return b

        # -- Row A: toolbar buttons ------------------------------------
        # DRAWN buttons (_DrawnBtn): the app's own button control
        # (button_ex / TATButton) paints strictly from the GLOBAL
        # ATFlatTheme -- themed faces on good themes, but a theme
        # that defines ButtonBgPassive without ButtonFont leaves the
        # caption at the dark built-in default: invisible on the dark
        # face (the 21st report), and no dialog prop reaches those
        # painted surfaces. The drawn button's face/border/caption
        # come from the form's own bg/font pair instead, and its
        # width is MEASURED (the hidden probe), so the caption can
        # never be eaten (the 21st report's 4th item). Same
        # name/on_click routing as the old button_ex -- the handlers
        # do not change.
        prev = None
        for name, cap, w in self.TOOLBAR_BTNS:
            p = {
                'cap': cap, 'w': w, 'h': 26,
                'a_t': ('', '['), 'sp_t': 8,
                'sp_l': 10 if prev is None else 8,
            }
            if prev is None:
                p['a_l'] = ('', '[')
            else:
                p['a_l'] = (prev, ']')
            _btn(name, cap, p)
            prev = name
        _btn('btn_close', _('Close'), {
            'w': 80, 'h': 26,
            'a_l': None, 'a_r': ('', ']'),
            'a_t': ('', '['), 'sp_t': 8, 'sp_r': 10,
        })

        # -- Row B: the two sides' paths -------------------------------
        # 50/50 BY ANCHOR, not by fixed widths (21st report: 'the path
        # box in dir compare window should shrink and grow with the
        # window, and each one must occupy 50% width of the line ...
        # they have fixed width, and when window is small they overlap
        # on each other'). The old 330px-fixed pair needed 856px and
        # overlapped from the 700px floor down. The anchor skeleton:
        #   [ed_left stretch][btn_lbrw] <sp_mid> [btn_rbrw][ed_right stretch]
        # with sp_mid a tiny label CENTERED on the form (a_l=('', '-')
        # is the LCL asrCenter anchor -- the control is centered over
        # the target, proc_customdialog.pas maps '-' to asrCenter).
        # Each Browse button sits against the spacer's outer side,
        # and each editor STRETCHES from its form edge to its Browse
        # button -- symmetric geometry, so both editors are exactly
        # (W - 2*10 - GAP - 2*6 - 2*brw_w)/2 wide at EVERY window
        # width: no overlap possible, equal halves by construction.
        # (Creation order: spacer, then BOTH Browse buttons, then the
        # editors -- an anchor target must exist when the anchor is
        # set, and each editor's a_r/a_l targets its Browse button.)
        self._add('label', 'sp_mid', {
            'cap': '', 'w': self.PATH_GAP, 'h': 8,
            'a_l': ('', '-'), 'sp_l': 0,
            'a_t': ('btn_new', ']'), 'sp_t': 14,
        })
        ed_prop = {'w': self.PATH_W, 'h': 26, 'a_t': ('btn_new', ']'),
                   'sp_t': 8}      # w: pre-DLG_SCALE fallback only --
                                   # the stretch anchors decide live
        _btn('btn_lbrw', _('Browse...'), {
            'w': self.BRW_W, 'h': 26,
            'a_l': None, 'a_r': ('sp_mid', '['), 'sp_r': 0,
            'a_t': ('btn_new', ']'), 'sp_t': 8,
        })
        _btn('btn_rbrw', _('Browse...'), {
            'w': self.BRW_W, 'h': 26,
            'a_l': ('sp_mid', ']'), 'sp_l': 0,
            'a_r': None,
            'a_t': ('btn_new', ']'), 'sp_t': 8,
        })
        p = dict(ed_prop)
        p.update({'a_l': ('', '['), 'sp_l': 10,
                  'a_r': ('btn_lbrw', '['), 'sp_r': 6,
                  'texthint': _('left folder')})
        self._add_input('ed_left', p, self._dir_l)
        p = dict(ed_prop)
        p.update({'a_l': ('btn_rbrw', ']'), 'sp_l': 6,
                  'a_r': ('', ']'), 'sp_r': 10,
                  'texthint': _('right folder')})
        self._add_input('ed_right', p, self._dir_r)

        # -- Row C: filters (left) | Mask + Apply (right) --------------
        # Left group: the four status filters + Subfolders, chained
        # left to right (DRAWN toggles now -- see Row A; a native
        # TCheckBox draws its caption through the OS theme and ignores
        # Font.Color, the 5th-round black-on-black captions, and the
        # 20th-release button_ex glyphs rode the same global theme
        # that goes invisible on dark faces). The toggle state is
        # OURS (self._show / self._recursive) exactly as before:
        # _on_check flips it and re-renders the drawn glyph.
        # Right group: Apply at the form's right edge, the mask edit
        # STRETCHING from the checks to it (its old fixed 170px was
        # the row's only collision left -- the stretch makes Row C
        # overlap-free at the 700px floor too), the label riding the
        # mask's left edge.
        prev = None
        for name, cap, checked in self.FILTER_CHECKS:
            p = {'cap': cap, 'h': 24,
                 'a_t': ('ed_left', ']'), 'sp_t': 10}
            if prev is None:
                p.update({'a_l': ('', '['), 'sp_l': 10})
            else:
                p.update({'a_l': (prev, ']'), 'sp_l': 12})
            _btn(name, cap, p, mode='chk', checked=checked)
            prev = name
        _btn('chk_sub', _('Subfolders'), {
            'h': 24,
            'a_l': ('chk_same', ']'), 'sp_l': 24,
            'a_t': ('ed_left', ']'), 'sp_t': 10,
        }, mode='chk', checked=True)
        _btn('btn_apply', _('Apply'), {
            'w': 70, 'h': 24,
            'a_l': None, 'a_r': ('', ']'), 'sp_r': 10,
            'a_t': ('ed_left', ']'), 'sp_t': 8,
        })
        # The mask input: a one-line 'editor' like the path boxes
        # (themed + self-painted hint -- _add_input). h 26 (was 22:
        # TATSynEdit's chrome needs the path boxes' height) and the
        # label at sp_t 12 still centers on it exactly (12+10 ==
        # 9+13); the row bottom stays clear of the list's sp_t 44.
        # The chain runs left to right -- chk_sub -> label -> edit ->
        # Apply -- and the edit STRETCHES between the label and
        # Apply: Row C is overlap-free at the 700px floor (the old
        # fixed 170px edit was the row's last collision), and on a
        # wide window the mask grows with it.
        self._add('label', 'lab_mask', {
            'cap': _('Mask:'), 'h': 20, 'autosize': True, 'w': 44,
            'a_l': ('chk_sub', ']'), 'sp_l': 20,
            'a_t': ('ed_left', ']'), 'sp_t': 12,
            'font_color': tcol,
        })
        p = {'w': 170, 'h': 26,
             'a_l': ('lab_mask', ']'), 'sp_l': 6,
             'a_r': ('btn_apply', '['), 'sp_r': 6,
             'a_t': ('ed_left', ']'), 'sp_t': 9,
             'texthint': _('*.py; *.txt')}
        self._add_input('ed_mask', p, '')

        # The honest WIDTH FLOOR (all rows are measured now): the
        # buttons autosize to the UI font, so a fixed 700-unit floor
        # cannot guarantee a fit at 14pt -- the floor is the widest
        # row's MEASURED content (Row C: the five checks + the mask
        # group; Row A: the toolbar + Close; Row B: both Browse
        # buttons + a usable 120px minimum per path edit), never
        # below the classic MIN_W. Re-pushed as 'w_min' in dlg units
        # (DLG_SCALE multiplies it by the DPI factor _ui_scale --
        # dividing the measured pixels by the same factor converts)
        # and stashed in px for the saved-geometry guard below.
        try:
            s = _ui_scale() or 1.0
            row_c = sum(self._drawn[n].width_px() for n in
                        ('chk_diff', 'chk_lonly', 'chk_ronly',
                         'chk_same', 'chk_sub'))
            row_c += (10 + 12 * 3 + 24)          # the checks' spacings
            row_c += 20 + _probe_text_w(h, _('Mask:'),
                                        self._probe_gap) + 4 + 6
            row_c += 60                          # a usable mask minimum
            row_c += self._drawn['btn_apply'].width_px() + 6 + 10
            row_a = 10 + 8 * (len(self.TOOLBAR_BTNS) - 1) + 10
            row_a += sum(self._drawn[n].width_px()
                         for n, _c, _w in self.TOOLBAR_BTNS)
            row_a += self._drawn['btn_close'].width_px()
            row_b = (2 * self._drawn['btn_lbrw'].width_px() +
                     self.PATH_GAP + 2 * 10 + 2 * 6 + 2 * 120)
            self._floor_px = max(int(self.MIN_W * max(1.0, _font_scale())
                                     * s), row_a, row_b, row_c) + 16
            ct.dlg_proc(h, ct.DLG_PROP_SET,
                        prop={'w_min': int(self._floor_px / s) + 1})
        except Exception:
            self._floor_px = 0   # the classic fixed floor stands

        # -- Statusbar (created before the list: the list anchors to it) --
        # TATStatus renders its cells in the themed UI font (like the
        # list), so the bar's height follows the font too -- the 'h'
        # prop is a dlg coordinate (the form's DLG_SCALE covers DPI),
        # it needs only the FONT factor or a big UI font clips the
        # cell text vertically.
        self._add('statusbar', 'sbar', {
            'h': max(26, int(26 * _font_scale())),
            'a_l': ('', '['), 'a_r': ('', ']'), 'a_b': ('', ']'),
            'sp_l': 0, 'sp_r': 0, 'sp_b': 0,
        })
        self.h_sb = ct.dlg_proc(h, ct.DLG_CTL_HANDLE, name='sbar')
        try:
            ct.statusbar_proc(self.h_sb, ct.STATUSBAR_ADD_CELL, tag=1)
            ct.statusbar_proc(self.h_sb, ct.STATUSBAR_ADD_CELL, tag=2)
            # NO defined cell widths (reported: 'status bar elements
            # must not use a defined width so it can shrink and grow
            # with ui dialog'): the 15th release pinned them with
            # SET_CELL_SIZE, and a resized dialog either clipped the
            # bar or left it half empty. TATStatus has the two flex
            # modes for exactly this (atstatusbar.pas
            # DoPanelAutoWidth / DoPanelStretch):
            # * cell 1 (scan status / progress messages, short and
            #   variable) AUTOSIZEs -- TATStatus re-fits it to its
            #   text on every paint, so the cell is never wider than
            #   its message needs;
            # * cell 2 (the counts line, the long one) AUTOSTRETCHes
            #   -- it takes whatever width the bar has left, so it
            #   grows and shrinks with the dialog (the bar itself is
            #   full-width anchored to the form's sides).
            ct.statusbar_proc(self.h_sb, ct.STATUSBAR_SET_CELL_AUTOSIZE,
                              tag=1, value='1')
            ct.statusbar_proc(self.h_sb,
                              ct.STATUSBAR_SET_CELL_AUTOSTRETCH,
                              tag=2, value='1')
            # Theming, 5th dark-theme round ('status bar background
            # and font colors does not use theme colors'): TATStatus
            # does NOT take its colors from ATFlatTheme -- its Color
            # property defaults to clBtnFace (light!) and the whole
            # bar rendered a white strip on black themes. The chains
            # below mirror what the app itself wires up for its own
            # main statusbar (formmain_themes.inc):
            #   StatusbarMain.Color := GetAppColor(StatusBg, TabBg)
            #     -- bg: StatusBg, TabBg when the theme leaves it at
            #     clNone (the _theme_color guard reads that sentinel
            #     as 'not themed');
            #   the font: per CELL (STATUSBAR_SET_COLOR_FONT does not
            #     exist as an action -- the old call raised and the
            #     broad except ate it silently), StatusFont falling
            #     back to ButtonFont = ATFlatTheme.ColorFont, exactly
            #     what TATStatus's UpdateCanvasFont uses when a cell
            #     carries no ColorFont;
            #   borders: ButtonBorderPassive, like the app's own
            #     StatusbarMain.ColorBorderTop/R assignments.
            sb_bg = _theme_color('StatusBg', bg)
            if sb_bg is not None:
                ct.statusbar_proc(self.h_sb, ct.STATUSBAR_SET_COLOR_BACK,
                                  value=sb_bg)
            sb_fg = _theme_color('StatusFont',
                                 _theme_color('ButtonFont'))
            if sb_fg is not None:
                for tag in (1, 2):
                    ct.statusbar_proc(
                        self.h_sb, ct.STATUSBAR_SET_CELL_COLOR_FONT,
                        tag=tag, value=sb_fg)
            sb_line = _theme_color('ButtonBorderPassive')
            if sb_line is not None:
                ct.statusbar_proc(self.h_sb,
                                  ct.STATUSBAR_SET_COLOR_BORDER_TOP,
                                  value=sb_line)
                ct.statusbar_proc(self.h_sb,
                                  ct.STATUSBAR_SET_COLOR_BORDER_R,
                                  value=sb_line)
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
            'a_t': ('ed_left', ']'), 'sp_t': 44,   # clears Row C (10+24)
            'a_b': ('sbar', '['), 'sp_b': 4,
            'on_click': self._on_list_click,
            'on_click_dbl': self._on_list_dbl,
            'on_click_header': self._on_header,
            'on_menu': self._on_list_menu,
            'on_draw_item': self._on_draw_item,
            # column-resize drag (on_mouse_down starts it, move
            # tracks it, up ends it -- see the class docstring)
            'on_mouse_down': self._on_mouse_down,
            'on_mouse_move': self._on_mouse_move,
            'on_mouse_up': self._on_mouse_up,
        }
        # The list's colors: TreeBg/TreeFont (the treeview pair --
        # what CudaText maps listbox_ex onto), ListBg/ListFont next,
        # the input chain (OtherText* -> EdText*) as the innermost
        # fallback -- the old innermost was the (deleted) TEdit box
        # color; this one is the app's own single-line-input color
        # and can never go green (see _add_input).
        li_bg = _theme_color('TreeBg', _theme_color(
            'ListBg', _theme_color('OtherTextBg',
                                   _theme_color('EdTextBg'))))
        li_fg = _theme_color('TreeFont', _theme_color(
            'ListFont', _theme_color('OtherTextFont',
                                     _theme_color('EdTextFont'))))
        if li_bg is not None:
            p['color'] = li_bg
        if li_fg is not None:
            p['font_color'] = li_fg
        self._add('listbox_ex', 'list', p)
        self.h_list = ct.dlg_proc(h, ct.DLG_CTL_HANDLE, name='list')
        try:
            # NOTE: no LISTBOX_SET_ITEM_H here -- the row height starts
            # as the control's own AUTO-FIT and the painter GUARDS it
            # from the first drawn row on (_fit_item_h: measured glyph
            # box + 6px, only when the auto-fit band is tighter -- the
            # 15th release's fixed height froze every box at OUR
            # estimate; the guard keeps the control's value wherever
            # it already fits).
            ct.listbox_proc(self.h_list, ct.LISTBOX_SET_COLUMN_SEP,
                            text=COL_SEP)
            # columns + header through the ONE pusher (tracks
            # _cols_pushed_w for the painter's alignment guard)
            self._apply_cols()
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

        # Scale the built layout to the OS DPI, then re-apply the saved
        # window geometry (saved values were captured post-scale, so
        # applying them after DLG_SCALE never double-scales). A saved
        # window NARROWER than the measured floor is dropped (the
        # report's overlapping small window: a programmatic PROP_SET
        # bypasses the form's w_min constraints -- this is where the
        # floor is actually enforced for restored windows).
        ct.dlg_proc(h, ct.DLG_SCALE)
        if geom and int(geom.get('w', 0) or 0) >= \
                max(self._floor_px, int(self.MIN_W * max(1.0,
                    _font_scale()) * _ui_scale())):
            try:
                ct.dlg_proc(h, ct.DLG_PROP_SET, prop=geom)
            except Exception:
                pass
        # LAST, with the layout final (DLG_SCALE applied, saved size
        # restored): pin every drawn button's measured pixel width and
        # its item band (see _DrawnBtn.sync -- the build-time 'w' was
        # dlg units the scale pass has applied; from here the widths
        # are live pixels, like the saved geometry itself).
        for b in self._drawn.values():
            b.sync()
        # ...and re-push the column spec for the FINAL geometry: the
        # restore's PROP_SET can change the form's size without ever
        # firing on_resize (a bounds change on a not-yet-shown LCL
        # form fires no event -- see "HEADER/ROW ALIGNMENT"), which
        # is exactly how a reopened compare window ended up with its
        # header on the default-width split while the rows painted
        # the restored width (the 24th report's misaligned
        # screenshot).
        self._apply_cols()

    def _restore_geom(self):
        # Stored as a single-line "x,y,w,h" STRING, not a list:
        # cudax_lib's flat-key settings updater only handles single-line
        # values (a list value written twice would corrupt the JSON --
        # the second update leaves the old multi-line block's orphan
        # lines behind).
        #
        # The floor is in the same LIVE PIXELS the saved values are:
        # w_min (set in _build with the font factor) times the DPI
        # factor _ui_scale() = exactly what DLG_SCALE makes of it. The
        # old raw comparison let a too-small saved window through on
        # any DPI>100% box (700 px < 875 px at 125%) -- the small
        # window the 21st report's screenshots show, with the fixed
        # 330px path boxes overlapping. A saved size below the floor
        # is DROPPED (the default size opens instead): a programmatic
        # PROP_SET bypasses the form's constraints, so this guard is
        # the only place the floor is enforced for restored windows.
        g = _get_state('dirs.win_geom', '')
        if isinstance(g, str) and g:
            try:
                x, y, w, h = (int(v) for v in g.split(','))
                if w >= int(self.MIN_W * max(1.0, _font_scale())
                            * _ui_scale()) \
                        and h >= int(self.MIN_H * _ui_scale()):
                    return {'x': x, 'y': y, 'w': w, 'h': h}
            except (TypeError, ValueError):
                pass
        return None

    def show(self):
        ct.dlg_proc(self.h, ct.DLG_SHOW_NONMODAL)

    def focus(self):
        """Bring this window to the z-front AND give it keyboard focus
        (the 25th report: a compare window opened from the CLI
        'cudatext -p=cuda_differ2#dir1#dir2' ended up BEHIND the main
        window, which kept the focus). Source-verified pair:
        DLG_TO_FRONT runs Form.BringToFront (z-order), DLG_FOCUS runs
        Form.SetFocus -- the app guards the latter with Visible and
        Enabled (formmain_py_api.inc), so this only works AFTER
        show(); both are no-ops on a torn window's dead handle."""
        if self._torn or not self.h:
            return
        try:
            ct.dlg_proc(self.h, ct.DLG_TO_FRONT)
            ct.dlg_proc(self.h, ct.DLG_FOCUS)
        except Exception:
            pass

    def focus_later(self, ms=500):
        """One-shot DELAYED re-focus for the CLI path (armed by
        compare_directories(..., from_cli=True)): the -p dispatch
        happens while CudaText itself is still coming up, and the
        app's own startup pass can (re)activate the main window AFTER
        on_cli already opened and focused this one -- one immediate
        focus then loses the race. The delayed shot re-pulls the
        window once the startup storm has settled; it runs at most
        once and never fights the user twice."""
        def do_focus(tag='', info=''):
            self.focus()
        try:
            ct.timer_proc(ct.TIMER_START_ONE, do_focus, ms)
        except Exception:
            pass

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

    def _load_col_w(self):
        """Pixel widths of the six FIXED columns (Name stretches: 0
        in _col_spec). Defaults come from _LIST_COLUMNS; a saved
        'dirs.col_widths' (base 96-DPI/9pt values, written at window
        close -- see _teardown) overrides them, sanitized per column
        (a hand-edited junk value must not break the layout). Scaled
        by _px_scale() here, ONCE: _col_spec/_col_layout and the
        drag hit tests all share the same pixel values."""
        base = [w for _c, _a, w in _LIST_COLUMNS[1:]]
        g = _get_state('dirs.col_widths', '')
        if isinstance(g, str) and g:
            try:
                v = [int(x) for x in g.split(',')]
            except ValueError:
                v = None
            if v is not None and len(v) == len(base):
                base = [min(MAX_COL_W, max(MIN_COL_W, x)) for x in v]
        s = _px_scale()
        return [int(w * s) for w in base]

    def _col_spec(self):
        """Column widths for LISTBOX_SET_COLUMNS, in _LIST_COLUMNS
        order -- all SEVEN as explicit pixel widths, over the same
        width _apply_cols pushes (the live row width once a row has
        been painted, else the list control's width), so this always
        mirrors what is on screen (the header splits its captions
        over exactly these numbers, and the drawn cells use the very
        same list via _col_layout, so header and rows cannot drift
        apart). The drag-resizable widths behind them live in
        self._col_w (persisted across sessions as 'dirs.col_widths');
        _on_resize re-pushes this spec whenever the window's width
        change makes the effective set move."""
        return self._effective_cols(self._cols_source_w())

    def _cols_source_w(self):
        """The width the column spec is derived from: the last PAINTED
        row's width once the painter has calibrated one (ground truth
        -- it is the very ClientWidth the header and the cells share),
        else the list control's live width. _apply_cols pushes from
        the same source, so the pushed spec and the painted layout
        are one and the same split."""
        return self._row_w if getattr(self, '_row_w', 0) > 0 \
            else self._list_px_w()

    def _effective_cols(self, width):
        """The seven on-screen column widths for a row 'width' pixels
        wide -- the single source of truth for the pushed column spec
        (the header), the painter's cell layout (_col_layout) and the
        drag hit tests. Name gets everything the six fixed columns
        leave -- but never less than NAME_MIN_W: when the fixed sum
        would eat past that floor (a shrunken window -- the 21st
        release report), the FIXED columns are scaled down
        proportionally instead, so the name stays visible and every
        column stays on screen. On a wide window the result is the
        plain stretch split and the proportional factor is 1."""
        try:
            width = int(width)
        except (TypeError, ValueError):
            width = 0
        avail = width - 4
        fixed = list(self._col_w)
        name_min = int(NAME_MIN_W * _px_scale())
        s = sum(fixed)
        if avail < 60:
            # degenerate (pre-layout call, collapsed window): the
            # raw desired widths -- the next real paint re-derives
            return [max(name_min, 60)] + fixed
        if s + name_min > avail and s > 0:
            k = float(avail - name_min) / s
            fixed = [int(w * k) for w in fixed]
        name_w = avail - sum(fixed)
        return [name_w] + fixed

    def _list_px_w(self):
        """The list control's live pixel width (PROP_GET returns the
        control's real geometry), 0 when unavailable (the caller's
        effective set then degrades to the desired widths -- the next
        on_resize/paint corrects it)."""
        try:
            d = ct.dlg_proc(self.h, ct.DLG_CTL_PROP_GET, name='list')
            return int(d.get('w', 0) or 0)
        except Exception:
            return 0

    def _col_layout(self, width):
        """Drawn-cell layout for a row 'width' pixels wide: a list of
        (x, w, align) per column, mirroring LISTBOX_SET_COLUMNS'
        pushed widths (both come from _effective_cols -- one source
        of truth) so the drawn cells land exactly under the header
        columns, at any DPI, after any drag and at any window width."""
        eff = self._effective_cols(width)
        out = [(0, eff[0], 'L')]
        x = eff[0] + 2
        for (_cap, align, _w), cw in zip(_LIST_COLUMNS[1:], eff[1:]):
            out.append((x, cw - 6, align))
            x += cw
        return out

    def _apply_cols(self):
        """Push the current column widths to the control: the header
        re-splits its captions over the very same widths. Called at
        build (twice -- before owner-draw, and again after the saved
        geometry restore, whose PROP_SET can change the form size
        without firing on_resize), after every drag step, by
        _reset_cols, by _on_resize (a width change moves the effective
        split -- see _effective_cols: a shrunken window scales the
        fixed columns down so Name keeps its floor) and by the
        painter's alignment heal (see "HEADER/ROW ALIGNMENT"). The
        width the spec was derived for is remembered in
        _cols_pushed_w -- the painter compares it against every
        painted row width and re-arms this pusher when they drift
        apart (a missed resize event would otherwise leave the header
        on a stale split: TATListbox never re-derives explicit
        widths, atlistbox.pas UpdateColumnWidths)."""
        if self._torn or not self.h_list:
            return
        w = self._cols_source_w()
        try:
            ct.listbox_proc(self.h_list, ct.LISTBOX_SET_COLUMNS,
                            text=self._effective_cols(w))
            ct.listbox_proc(self.h_list, ct.LISTBOX_SET_HEADER,
                            text=self._header_text())
            self._cols_pushed_w = w
        except Exception:
            pass

    def _on_cols_heal(self, tag='', info=''):
        """One-shot timer body for the painter's alignment guard: the
        last painted row was WIDER or NARROWER than the width the
        column spec was pushed for, so the header sat on a stale
        split (the 24th report: 'the column headers does not align
        correctly with their corresponding rows'). Re-push OUTSIDE
        the paint cycle -- listbox_proc during a paint could trigger
        the control's own repaint; the deferred one-shot cannot. The
        push converges: it is derived from the very row width the
        guard measured, so the next paint finds them equal and arms
        nothing."""
        self._cols_heal_armed = False
        if not self._torn:
            self._apply_cols()

    def _on_resize(self, id_dlg, id_ctl, data='', info=''):
        """Form resize (the dialog API's form-level on_resize event):
        the one layout piece that depends on the live WIDTH is the
        column split -- the effective widths are re-derived and
        re-pushed, so the header never sits on a stale split (the
        rows themselves re-derive per painted row in _col_layout).
        Everything else on the form is fully anchored and follows the
        resize by itself."""
        if self._torn:
            return
        self._apply_cols()

    def _sort_key(self, r):
        c = self._sort_col
        if c == 0:
            return (r['name'].lower(), r['rel'].lower())
        if c == 1:
            return (r['dir'].lower(), r['name'].lower())
        if c == 2:
            # folders sort by their ROLLED-UP status (_es -- see
            # _build_view): the Status cell shows it, so the sort and
            # the display must agree (a red 'Different' folder sorts
            # with the differences, not with the plain folders)
            return (STATUS_SEVERITY.get(r.get('_es', r['status']), 99),
                    r['rel'].lower())
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
        st = r.get('_es', r['status'])   # folders: rolled-up status
        return (
            r['name'],
            r['dir'],
            STATUS_CAPTION.get(st, st),
            '' if r['isdir'] else _fmt_size(r['size_l']),
            _fmt_date(r['mtime_l']),
            '' if r['isdir'] else _fmt_size(r['size_r']),
            _fmt_date(r['mtime_r']),
        )

    def _filter_ok(self, r):
        # Folders filter by their ROLLED-UP status (_es -- the very
        # thing the Status cell shows, set by _build_view before this
        # runs): a 'Different' folder follows the Different checkbox,
        # not Identical. A MIXED folder is one-sided + identical
        # content by construction (no diffs reached it, else the
        # rollup would say Different) -- it shows when ANY of the
        # statuses it can contain is checked. One-sided folders roll
        # up to one-sided _es (or have no _es), so their checkbox is
        # unchanged; files never carry _es.
        st = r.get('_es', r['status'])
        if st == ST_ERR:
            return True  # always visible: rare and important
        if st in (ST_LONLY, ST_DIR_LONLY):
            return self._show[ST_LONLY]
        if st in (ST_RONLY, ST_DIR_RONLY):
            return self._show[ST_RONLY]
        if st == ST_DIFF:
            return self._show[ST_DIFF]
        if st == ST_MIXED:
            return (self._show[ST_LONLY] or self._show[ST_RONLY]
                    or self._show[ST_SAME])
        return self._show[ST_SAME]  # ST_SAME and plain ST_DIR rows

    def _fill_list(self, rows=None):
        """Filter+sort 'rows' (default: the finished self._rows) into
        the owner-drawn list, arranged as the WinMerge-style tree (see
        _build_view). The listbox only holds one caption string per row
        (the joined cells + the depth indent -- what a non-drawn
        fallback would show); the painted content comes from
        self._view, so keeping the two in sync is this method's whole
        job.

        The list is REBUILT whenever the view changes (the old
        append-only incremental fill could not express tree mode: a
        streamed row can appear anywhere in the tree -- under a
        collapsed folder it changes nothing visible, under an
        expanded one it lands in the middle of the list). An
        UNCHANGED view skips the rebuild entirely -- with a collapsed
        default, most streamed rows change nothing on screen, and a
        collapsed tree keeps the visible row count small, so the
        rebuild stays cheap exactly when it runs often. The selection
        follows its row by identity (wherever the rebuild moved it)
        and the scroll position is kept."""
        if self._torn or not self.h_list:
            return
        if rows is None:
            rows = self._rows
        t0 = time.perf_counter()
        view = self._build_view(rows)

        old_view = self._view
        # An unchanged view (identity + order, and the listbox really
        # holds it) skips the whole listbox rebuild: while a scan
        # streams, most new rows land under COLLAPSED folders and
        # change nothing on screen -- re-adding an identical item list
        # every tick would only churn repaints. The count check keeps
        # the skip honest (start_scan resets _view without touching
        # the control -- a fresh scan MUST clear the old rows).
        try:
            lb_n = int(ct.listbox_proc(self.h_list, ct.LISTBOX_GET_COUNT))
        except Exception:
            lb_n = -1
        if (lb_n == len(old_view) and
                len(view) == len(old_view) and
                all(a is b for a, b in zip(view, old_view))):
            self._view = view
            if Profiler.is_enabled():
                dt = time.perf_counter() - t0
                Profiler.mark('dirs:ui_fill_list', dt, 0, t0=t0)
            return

        sel_row = None
        i = self._sel_index()
        if 0 <= i < len(old_view):
            sel_row = old_view[i]
        top = -1
        try:
            top = int(ct.listbox_proc(self.h_list, ct.LISTBOX_GET_TOP))
        except Exception:
            top = -1
        try:
            ct.listbox_proc(self.h_list, ct.LISTBOX_DELETE_ALL)
            for r in view:
                ct.listbox_proc(self.h_list, ct.LISTBOX_ADD,
                                index=-1, text=self._item_caption(r))
            # Selection: the SAME row object, wherever it landed
            # (identity, not index -- a toggle moves rows around).
            if sel_row is not None:
                for j, r in enumerate(view):
                    if r is sel_row:
                        ct.listbox_proc(self.h_list, ct.LISTBOX_SET_SEL,
                                        index=j)
                        break
            # Scroll: keep the first visible row (clamped to the new
            # count) so toggling does not jump the list to the top.
            if top > 0:
                ct.listbox_proc(self.h_list, ct.LISTBOX_SET_TOP,
                                index=min(top, max(0, len(view) - 1)))
        except Exception:
            pass
        self._view = view
        if Profiler.is_enabled():
            dt = time.perf_counter() - t0
            Profiler.mark('dirs:ui_fill_list', dt, len(view), t0=t0)

    def _build_view(self, rows):
        """The display order of 'rows': a depth-first walk of the
        folder tree, each level sorted by the current column exactly
        like the old flat list (folders and files interleaved -- the
        Status severity table puts plain folders last, and the sort
        semantics must not change between the flat and tree display),
        folders expanded only when their rel path is in self._expand.
        Every visited row carries the display fields the painter and
        the click hit test read:

          '_d'   depth (0 = directly under the compared roots)
          '_hk'  folder HAS scanned children (the +/- marker is drawn)
          '_ex'  folder is expanded (the marker is '-')
          '_es'  folder's ROLLED-UP status: the worst status found in
                 its subtree (the row color + Status caption + the
                 Status-column sort key -- WinMerge's 'a folder that
                 contains a difference is Different', and the 21st
                 release's converse: a folder whose whole subtree is
                 'Identical' shows 'Identical' too, not 'Folder');
                 set whenever the rollup beats a plain 'Folder', i.e.
                 only a folder with NOTHING scanned below (or an
                 empty one) keeps the plain 'Folder' look

        The rollup is computed over the WHOLE subtree (visible rows
        or not) BEFORE the walk: hidden-but-present differences must
        still color the container that keeps them reachable.

        A folder whose own row is filtered out stays visible as a
        CONTAINER while anything below it is visible (without this,
        hiding 'Identical' would hide a folder's Different children
        too -- the flat list never did that); a folder with nothing
        visible inside is hidden with its subtree. Rows are the scan's
        own dicts (shared with self._rows) -- the display fields are
        recomputed on every fill, never persisted."""
        kids = {}
        for r in rows:
            kids.setdefault(r['dir'], []).append(r)

        # Rolled-up folder status (the '_es' field above): worst
        # status of the folder's own row and everything under it,
        # memoized per rel path (each rel is unique -- the kids graph
        # is a well-founded tree by construction). Severity order =
        # STATUS_SEVERITY, i.e. the display's order of interesting.
        sev = STATUS_SEVERITY
        memo = {}

        def rollup(rel, own):
            w, ws = own, sev.get(own, 99)
            for r in kids.get(rel, ()):
                cw = rollup(r['rel'], r['status']) \
                    if r['isdir'] else r['status']
                cs = sev.get(cw, 99)
                if cs < ws:
                    w, ws = cw, cs
            memo[rel] = w
            return w

        for r in rows:
            if r['isdir'] and r['rel'] not in memo:
                rollup(r['rel'], r['status'])
        for r in rows:
            if r['isdir']:
                w = memo.get(r['rel'])
                # The bar is ST_DIR, not ST_SAME (21st release: 'when
                # folders are identical, the status column shows
                # "folder" instead of "identical"'): a both-sides
                # folder whose whole subtree rolled up to ST_SAME now
                # carries _es=ST_SAME -> the Status cell says
                # 'Identical', the row sorts with the identical files
                # and the filter's 'Identical' checkbox governs it.
                # One-sided folders are unaffected (their rollup can
                # never reach ST_SAME -- their own status already sits
                # above it in the severity table), so the delta of the
                # widened bar is EXACTLY the all-identical subtree.
                if w is not None and sev.get(w, 99) < sev[ST_DIR]:
                    # 25th release: a BOTH-sides folder never carries a
                    # ONE-SIDED caption. Its subtree rolled up to
                    # one-sided content, but the folder itself exists
                    # on both sides -- the user's report was exactly
                    # this: a folder with identical + only-left files
                    # showing 'Only left'. Such a folder now reads
                    # MIXED (ST_MIXED); genuinely one-sided folders
                    # (r['status'] is dir_lonly/dir_ronly) keep their
                    # own captions.
                    if r['status'] == ST_DIR and w in (
                            ST_LONLY, ST_RONLY,
                            ST_DIR_LONLY, ST_DIR_RONLY):
                        w = ST_MIXED
                    r['_es'] = w
                else:
                    r.pop('_es', None)   # stale value from an old fill

        view = []

        def level(rel, depth):
            items = list(kids.get(rel, ()))
            items.sort(key=self._sort_key)
            if self._sort_desc:
                items.reverse()
            for r in items:
                has_kids = bool(kids.get(r['rel']))
                r['_d'] = depth
                r['_hk'] = has_kids
                r['_ex'] = has_kids and r['rel'] in self._expand
                if not self._filter_ok(r):
                    if not (r['_hk'] and self._subtree_visible(r['rel'],
                                                                kids)):
                        continue     # hidden folder, nothing visible inside
                    # else: keep it as the container of visible rows
                view.append(r)
                if r['_ex']:
                    level(r['rel'], depth + 1)

        level('', 0)
        return view

    def _subtree_visible(self, rel, kids):
        """Any row at or below 'rel' passes the current filter? (Only
        called for folders whose own row was filtered out -- see
        _build_view's container rule.)"""
        for r in kids.get(rel, ()):
            if self._filter_ok(r):
                return True
            if r['isdir'] and self._subtree_visible(r['rel'], kids):
                return True
        return False

    @staticmethod
    def _item_caption(r):
        """The listbox item string of a row: cells joined by COL_SEP,
        the Name cell indented by two spaces per tree level. The drawn
        list never shows it (the painter draws the cells one by one);
        it exists for the non-drawn fallback and for copy/paste
        friendliness of the raw control content."""
        cells = DirCompareForm._row_cells(r)
        d = r.get('_d', 0)
        if d:
            cells = ('  ' * d + cells[0],) + cells[1:]
        return COL_SEP.join(cells)

    # ------------------------------------------------------------------
    # Row painting (owner-drawn listbox_ex)
    # ------------------------------------------------------------------

    def _on_draw_item(self, id_dlg, id_ctl, data='', info=''):
        """LISTBOX_SET_DRAWN painter: draws one row -- background
        (status color: the SAME colors the diff tabs paint their hunks
        with, and for a FOLDER the worst status of its subtree -- see
        _build_view's rollup), the tree gutter (+/- marker + status
        icon; folders ALWAYS the folder icon), and the seven cells at
        _col_layout offsets (the Name cell shifted and narrowed by
        the gutter + the row's tree indent).

        Runs inside the control's paint cycle: only canvas_proc /
        imagelist_proc calls here (paint-only, no re-entrant repaints),
        and it must stay fast (every repaint of every visible row goes
        through here -- ~15 API calls per row). As a side effect it
        calibrates the click hit test's geometry (header height, row
        left edge) -- one-time, two API calls.
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

        # One-time calibration for _on_list_click's (x, y) hit test:
        # the header height from the drawn row's own position. The
        # geometry never changes after build, so once is enough.
        # _row_x0/_row_w also feed the column-resize drag's boundary
        # hit test (they DO move on a form resize -- hence refreshed
        # on every painted row, not just the first).
        self._row_x0 = x0
        self._row_w = w
        # the alignment guard (see "HEADER/ROW ALIGNMENT"): a painted
        # row width that no longer matches the pushed spec's source
        # width means a resize event was missed and the header sits
        # on a stale split -- arm the deferred re-push (once; the
        # heal itself re-arms if anything drifts again). Cheap: one
        # comparison per painted row.
        if self._cols_pushed_w and abs(w - self._cols_pushed_w) > 1 \
                and not self._cols_heal_armed:
            self._cols_heal_armed = True
            try:
                ct.timer_proc(ct.TIMER_START_ONE, self._on_cols_heal, 30)
            except Exception:
                self._cols_heal_armed = False
        if self._hdr_h is None:
            try:
                ih = ct.listbox_proc(self.h_list, ct.LISTBOX_GET_ITEM_H)
                top = ct.listbox_proc(self.h_list, ct.LISTBOX_GET_TOP)
                if ih:
                    self._hdr_h = max(0, y0 - (index - int(top)) * int(ih))
            except Exception:
                self._hdr_h = None

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
            # est: the folder's rolled-up status (worst of its
            # subtree) -- plain rows fall back to their own status
            bg = col.get(ST_COLOR_KEY.get(row.get('_es', st), ''))
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

        # -- tree gutter: +/- marker + status icon, then the cells ---
        # [+/-][icon][name...] all shifted by the row's depth (the
        # marker only on folders with scanned children). Icon and
        # text have separate offsets -- the pre-tree code painted the
        # icon at x0+3 with the Name text at x0+4, i.e. ON TOP of it.
        # The offsets carry _px_scale() (DPI x UI-font factor): the
        # glyphs the canvas draws are the UI font, so the gutter must
        # grow with it or the +/- marker collides with the icon.
        depth = row.get('_d', 0)
        ind = int(TREE_INDENT * _px_scale()) * depth
        ax = x0 + 2 + ind
        ix = x0 + int(GUTTER_ICON * _px_scale()) + ind

        cells = self._row_cells(row)
        layout = self._col_layout(w)
        try:
            # THE font fix, 3rd round: the row text is drawn in the
            # CudaText UI font by SETTING it -- name from ui_font_name,
            # size _ui_font_pt() (DoScaleFont(ui_font_size)). v5 set
            # only the name ('default') and left the size to the
            # canvas; v6 set neither and trusted TATListbox's DoPaintTo
            # preset; both left the rows SMALL on the reported build
            # (the preset is real in current upstream atlistbox.pas but
            # evidently missing there -- an unset canvas falls back to
            # the LCL default GUI font at ~75-80% of the UI size).
            # Explicit values are build-proof: preset or no preset,
            # every glyph after this line is the themed UI font.
            # canvas_proc skips the assignment for ''/-1, so only the
            # color and the style reset are extra; style=0 clears any
            # bold/italic a previous row may have left on the canvas.
            ct.canvas_proc(canvas, ct.CANVAS_SET_FONT,
                           text=_ui_font()[0],
                           color=fg, size=_ui_font_pt(), style=0)
            # one measure, two jobs: the vertical centering baseline
            # AND the row-height guard -- the measured glyph box is
            # the ground truth the row BAND must fit (see _fit_item_h:
            # the 4th-round report -- big font, small band, text
            # eaten -- is fixed right here, from this very number)
            sz = ct.canvas_proc(canvas, ct.CANVAS_GET_TEXT_SIZE,
                                text='Ag')
            th = sz[1] if sz else 0
            if th:
                self._fit_item_h(th)
            ty = y0 + max(0, (h - (th or 13)) // 2)
        except Exception:
            ty = y0 + 5
            sz = None

        if row.get('_hk'):
            # the +/- expand marker (ASCII -- see the TREE_* metrics
            # comment: arrows can collide with the folder icon on
            # large-font boxes, triangles render as tofu)
            try:
                ct.canvas_proc(canvas, ct.CANVAS_TEXT,
                               text='-' if row.get('_ex') else '+',
                               x=ax, y=ty)
            except Exception:
                pass

        # folders ALWAYS paint the folder icon (a one-sided folder's
        # color + Status caption carry the sidedness); files paint
        # their own status icon
        icon = 'folder' if row['isdir'] else _ICON_OF.get(st)
        if icon is not None:
            idx = self._icon_idx.get(icon, -1)
            if idx is not None and idx >= 0 and self.h_imglist:
                try:
                    # 16px icons in an auto-fitted row: guard the
                    # centering against rows TIGHTER than the icon
                    # (a small UI font can auto-fit below 16px)
                    iy = y0 + max(0, (h - 16) // 2)
                    ct.imagelist_proc(
                        self.h_imglist, ct.IMAGELIST_PAINT,
                        value=(canvas, ix, iy, idx))
                except Exception:
                    pass

        # the Name cell loses the gutter + indent to the ellipsis too
        gut = int(GUTTER_TEXT * _px_scale()) + ind
        for i, (text, (cx, cw, align)) in enumerate(zip(cells, layout)):
            if i == 0:
                cx, cw = cx + gut, cw - gut
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
            dt = time.perf_counter() - t0
            Profiler.mark('dirs:ui_draw_item', dt, t0=t0)

    def _fit_item_h(self, text_h):
        """Grow the list's row height to fit the glyphs the painter
        draws -- the height half of the font fix.

        The 17th release made the painter SET the UI font explicitly
        (the reported build's TATListbox neither presets the canvas
        font nor carries the UI size in its theme), so the text grew
        -- but the row BAND still came from the control's own
        auto-fit, GetItemHeightDefault over the THEME font, which on
        that same build stays at the small default: big glyphs in a
        9pt-sized band, the text eaten top and bottom (4th-round
        report: 'the list items height does not grow when font grow').

        Self-correcting and MEASURED, not guessed: the painter passes
        the very glyph box it just drew (CANVAS_GET_TEXT_SIZE under
        the font it just SET -- real font, real DPI, real point size),
        and only when the band is tighter than that box + 6px does it
        raise ItemHeight (LISTBOX_SET_ITEM_H; SetItemHeight sets
        FItemHeightIsFixed, the auto-fit freezes and the value rules
        from then on -- the control repaints once and every later
        pass finds the height already fitting, so this fires at most
        once per window). The +6 padding is the 6th-round request
        ('instead of 2px use 6px'): with it a small-font build can
        gain a pixel or two over the control's own auto-fit (the
        built-in formula is (1.8*pt + 2)*k against a real glyph box
        of ~1.35*pt*k -- at 9pt/96dpi the margin over glyph+6 is
        under a pixel), which is the point: the band should never sit
        tight on the glyphs. Never shrinks either way. The hit tests
        need no change: _row_at_y and the painter's calibration read
        GET_ITEM_H live, and the header height they derive from the
        first row is independent of the item height. _item_h_fix
        remembers the applied height (None = never needed) for the
        functional tests."""
        try:
            need = int(text_h) + 6
            cur = ct.listbox_proc(self.h_list, ct.LISTBOX_GET_ITEM_H)
            if cur is not None and need > int(cur):
                ct.listbox_proc(self.h_list, ct.LISTBOX_SET_ITEM_H,
                                index=need)
                self._item_h_fix = need
        except Exception:
            pass

    def _update_counts(self):
        # Counters follow the EFFECTIVE status (_es) -- the very text
        # the Status cell shows. Before the 25th release every
        # both-sides folder counted as 'Identical' no matter what its
        # cell said (a 'Different'/'Mixed' folder inflated the
        # Identical number). MIXED folders count in no bucket: they
        # are containers of one-sided + identical content and no
        # single counter is true about them.
        rows = self._rows
        n_diff = n_l = n_r = n_same = 0
        for r in rows:
            st = r.get('_es', r['status'])
            if st == ST_DIFF:
                n_diff += 1
            elif st in (ST_LONLY, ST_DIR_LONLY):
                n_l += 1
            elif st in (ST_RONLY, ST_DIR_RONLY):
                n_r += 1
            elif st in (ST_SAME, ST_DIR):
                n_same += 1
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
        """A control's value, editor-backed names included: the three
        inputs are one-line TATSynEdit controls whose 'val' the dialog
        API does not handle (DoControl_SetStateFromString has no
        TATSynEdit branch) -- their text lives behind the cached
        Editor handle (_add_input), every other control reads the
        API's 'val' as before."""
        h = self._ed_handles.get(name)
        if h:
            try:
                return ct.Editor(h).get_text_all()
            except Exception:
                return ''
        try:
            return ct.dlg_proc(self.h, ct.DLG_CTL_PROP_GET,
                               name=name).get('val', '')
        except Exception:
            return ''

    def _set_ctl_val(self, name, val):
        """Write a control's value (editor-backed names through
        Editor.set_text_all -- see _ctl_val)."""
        h = self._ed_handles.get(name)
        if h:
            try:
                ct.Editor(h).set_text_all(val)
                return
            except Exception:
                pass
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
        # Tree state: expansion survives rescans of the SAME pair
        # (Refresh / filter changes / swap-back all keep it); a NEW
        # pair starts folded everywhere.
        if self._exp_roots != (self._dir_l, self._dir_r):
            self._expand.clear()
            self._exp_roots = (self._dir_l, self._dir_r)
        self._quick_only = bool(_get_opt('dirs.quick_only', False))
        self._method = _get_method()
        # Threading shape of THIS scan (differ2.dirs.scan_
        # threading): 'main' (the DEFAULT since update 10) runs the
        # whole scan synchronously HERE, 'serial' walks on the
        # scanner thread alone, 'pool' is the full threaded engine
        # -- both opt-in for very big trees, where a frozen window
        # is worse than a slower background walk. Reloaded per
        # scan, so the shapes can be A/B-ed from the same window
        # (set the option, press Refresh).
        mode = _get_opt('dirs.scan_threading', SCAN_MODE_DEFAULT)
        if mode not in _SCAN_MODES:
            mode = SCAN_MODE_DEFAULT
        self._scan_mode = mode

        if self._worker is not None:
            self._worker.cancel()
            _prof_abandon_scan(self._cmd, self._prof_token, self._worker)
            self._prof_token = None
        recursive = self._recursive   # button_ex check state (no 'val')
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
        # 'bg' MUST be the color the listbox control paints its own
        # background with. The dialog API's listbox_ex takes its
        # background from the theme's TreeBg (CudaText maps the
        # dialog listbox onto the treeview colors), NOT from the
        # control's 'color' prop -- so uncolored rows (identical
        # files, plain folders) painted with anything else show up
        # as a tint. On the reporting box ListBg-themed F0F0F0 rows
        # sat on a white list: every identical file looked grey.
        cfg = getattr(self._cmd, 'cfg', None) or {}
        self._colors = {
            'sel_bg': _theme_color('ListSelBg'),
            'sel_font': _theme_color('ListSelFont'),
            'bg': _theme_color('TreeBg',
                               _theme_color('ListBg', 0xE4E4E4)),
            'font': _theme_color('TreeFont',
                                 _theme_color('ListFont', 0x000000)),
        }
        for k in ('color_changed', 'color_deleted', 'color_added'):
            v = cfg.get(k)
            if isinstance(v, int):
                self._colors[k] = v

        self._worker = _Scanner(self._dir_l, self._dir_r, recursive,
                                mask, self._quick_only, self._method,
                                cprof_scan, scan_mode=mode)
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
        self._fill_list()          # empty the list right away
        if mode == SCAN_MODE_MAIN:
            # Synchronous main-thread scan: run() called INLINE
            # (never .start()) executes the whole scan body -- spawn
            # lag, walks, rows -- on THIS thread: the same code, the
            # same section bookings, one thread only. The window
            # is intentionally NOT pumped meanwhile: no timer, no
            # repaint, no streaming (rows appear at the end), no
            # cancel -- the purest measurement of what a single
            # thread pays for these folders on this box. The finish
            # path is the timer's own (_scan_done), shared here.
            self._worker.run()
            _rows, _total, _fin = self._worker.snapshot()
            self._scan_done(self._worker, _rows, _total)
            return
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
                dt = time.perf_counter() - t0
                Profiler.mark('dirs:ui_timer_tick', dt, t0=t0)
            return
        # Finished (normally or cancelled): last full update.
        self._scan_done(w, rows, total)
        if Profiler.is_enabled():
            dt = time.perf_counter() - t0
            Profiler.mark('dirs:ui_timer_tick', dt, t0=t0)

    def _scan_done(self, w, rows, total):
        """Shared finish path: the timer's last tick AND the
        synchronous main-thread scan (dirs.scan_threading=main, which
        calls this straight after run()). Stop the timer, book the
        finish lag, final fill, counts, status line, profiling
        epilogue."""
        self._stop_timer()
        if Profiler.is_enabled() and getattr(w, 'run_end', None):
            # Wall time from the worker's real end to THIS moment:
            # the timer quantum + whatever kept the main thread busy
            # -- with dirs:spawn_lag this closes the loop on where
            # every millisecond of dirs:scan_wall went. Booked with
            # its span so the report nests it under dirs:worker.
            # (In the synchronous main mode this is ~0 by
            # construction: the same thread continues straight from
            # run()'s finally into this call.)
            try:
                lag = time.perf_counter() - w.run_end
                Profiler.mark('dirs:finish_lag', lag, 1, lag,
                              t0=w.run_end)
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
            # the drawn toggles carry no 'val' (see _DrawnBtn): the
            # click ITSELF is the toggle -- flip the Python state and
            # re-render the drawn glyph.
            self._show[mapping[name]] = not self._show[mapping[name]]
            self._set_chk_state(name, self._show[mapping[name]])
            self._fill_list()
        elif name == 'chk_sub':
            self._recursive = not self._recursive
            self._set_chk_state('chk_sub', self._recursive)
            self.start_scan()

    def _set_chk_state(self, name, on):
        """Render a filter toggle's state on its drawn button (the
        glyph is painted by _DrawnBtn; a missing button -- a degraded
        build -- just skips the repaint)."""
        b = self._drawn.get(name)
        if b is not None:
            b.set_checked(on)

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
        toggles the direction (the rebuilt header carries the U+2191/
        U+2193 marker, aligned with the drawn cells -- same width
        table). This is the ONLY sort UI (the Sort-by combo of the
        12th/13th releases was removed: the header renders and sorts
        on real builds, so the box was redundant)."""
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

    # ------------------------------------------------------------------
    # Tree mode: expand / collapse (WinMerge-style, inline)
    # ------------------------------------------------------------------

    def _toggle_expand(self, row):
        """Show/hide a folder's rows inline. The state is keyed by the
        folder's rel path, so it survives sort/filter changes and
        rescans of the same pair (see start_scan)."""
        if self._torn:
            return
        rel = row['rel']
        if rel in self._expand:
            self._expand.discard(rel)
        else:
            self._expand.add(rel)
        self._fill_list()

    def _expand_all(self):
        if self._torn:
            return
        self._expand = {r['rel'] for r in self._rows if r['isdir']}
        self._fill_list()

    def _collapse_all(self):
        if self._torn:
            return
        self._expand.clear()
        self._fill_list()

    def _in_arrow(self, row, x):
        """Is client-x inside the row's expand-marker zone? (The zone
        the painter draws the +/- glyph into, widened to ARROW_W for
        an easy hit. Same _px_scale() math as the painter -- the zone
        must sit exactly under the drawn glyph at any DPI/font.)"""
        ax = self._row_x0 + 2 + \
            int(TREE_INDENT * _px_scale()) * row.get('_d', 0)
        return ax <= x <= ax + int(ARROW_W * _px_scale())

    def _row_at_y(self, idx, y):
        """Is client-y inside row idx's band? Separates row clicks
        from clicks on the listbox header (which shares the control's
        client area -- whether the header also fires on_click is a
        TATListbox detail this check must not depend on). The header
        height comes from the painter's one-time calibration; until
        then (nothing drawn yet) the check trusts the selection."""
        if self._hdr_h is None:
            return True
        try:
            ih = int(ct.listbox_proc(self.h_list, ct.LISTBOX_GET_ITEM_H))
            top = int(ct.listbox_proc(self.h_list, ct.LISTBOX_GET_TOP))
            if ih <= 0:
                return True
        except Exception:
            return True
        row_top = self._hdr_h + (idx - top) * ih
        return row_top <= y < row_top + ih

    # ------------------------------------------------------------------
    # Column resize (mouse drag on the boundary lines)
    # ------------------------------------------------------------------

    def _boundaries(self):
        """Client-x positions of the six internal column boundaries
        (Name|Folder, Folder|Status, ..., Right size|Right date), or
        None until a row has been painted (the painter calibrates
        _row_x0/_row_w; the built-in header is not addressable, so
        the boundaries live in the rows area)."""
        if self._row_w <= 0:
            return None
        lay = self._col_layout(self._row_w)
        return [self._row_x0 + cx for (cx, _cw, _al) in lay[1:]]

    def _boundary_at(self, x):
        """The boundary number (1..6, leftmost wins) whose x is
        within the grab tolerance of a left press, or None."""
        bs = self._boundaries()
        if not bs:
            return None
        tol = int(6 * _ui_scale())
        for i, bx in enumerate(bs):
            if abs(x - bx) <= tol:
                return i + 1
        return None

    def _set_cursor(self, h_split):
        """Show/restore the H-split resize cursor on the list
        (edge-triggered from _on_mouse_move -- cheap on hover, and
        skipped entirely on builds without the cursor prop).
        H_SPLIT, not V_SPLIT (reported: 'use CURSOR_H_SPLIT instead
        of CURSOR_V_SPLIT'): the glyph is a left-right arrow, the
        right shape for dragging a VERTICAL boundary line; V_SPLIT
        is the up-down arrow and reads as a row-height resize."""
        self._zone = h_split
        try:
            ct.dlg_proc(self.h, ct.DLG_CTL_PROP_SET, name='list',
                        prop={'cursor': ct.CURSOR_H_SPLIT if h_split
                              else ct.CURSOR_DEFAULT})
        except Exception:
            pass

    def _on_mouse_down(self, id_dlg, id_ctl, data='', info=''):
        """Left press near an internal column boundary starts a
        column-RESIZE drag (the dialog API's listbox header cannot be
        dragged; the control does deliver mouse events over its rows,
        where the boundary lines run). data =
        {'btn': 0/1/2, 'state': 's/c/a/L/...', 'x': int, 'y': int}.
        The drag moves the pressed boundary the way usual apps move
        it (the 24th report: 'not like usual apps'): the column LEFT
        of the boundary takes the drag's delta, the column RIGHT of
        it gives that width back -- every OTHER column (Name
        included) keeps its width, so exactly the two flanking cells
        change and the boundary follows the mouse. Boundary 1
        (Name|Folder) is the exception: Name is the stretch column
        and absorbs, so the drag adjusts Folder INVERSELY (dragging
        right grows Name) with Folder's growth capped at Name's
        floor (NAME_MIN_W). Boundary 6 finally makes the LAST column
        resizable too (its left boundary now grows/shrinks Right
        date -- the 24th report: 'last column cannot be resized').
        _drag = (boundary, press x, left width at press, right
        width at press -- None on boundary 1)."""
        if self._torn or not self.h_list:
            return
        if not isinstance(data, dict):
            return
        try:
            if int(data.get('btn', -1)) != 0:    # left button only
                return
            x = int(data.get('x', -1))
        except (TypeError, ValueError):
            return
        j = self._boundary_at(x)
        if j is None:
            return
        if j == 1:
            self._drag = (1, x, self._col_w[0], None)
        else:
            self._drag = (j, x, self._col_w[j - 2], self._col_w[j - 1])
        if not self._zone:
            self._set_cursor(True)   # a fast press may skip the hover

    def _on_mouse_move(self, id_dlg, id_ctl, data='', info=''):
        """Hover: show the H-split cursor over a boundary
        (edge-triggered: the prop is touched only when the zone is
        entered/left; steady hovering costs one comparison). Drag:
        move the boundary with the mouse, clamped to
        MIN_COL_W..MAX_COL_W, pushed live via _apply_cols. A move
        without the left button held ENDS the drag -- the release
        happened outside the control, and without this check the drag
        would stick to the cursor (the state string carries 'L'
        exactly while the button is down)."""
        if self._torn or not self.h_list:
            return
        if not isinstance(data, dict):
            return
        try:
            x = int(data.get('x', -1))
            held = 'L' in str(data.get('state', ''))
        except (TypeError, ValueError):
            return
        if self._drag is None:
            if not held:
                in_zone = self._boundary_at(x) is not None
                if in_zone != self._zone:
                    self._set_cursor(in_zone)
            return
        if not held:
            self._drag = None          # released outside the control
            return
        # the new boundary math (the 24th report: 'not like usual
        # apps'): the column LEFT of the pressed boundary takes the
        # drag's delta, the one RIGHT of it gives that width back --
        # every other column keeps its width. Both flanking columns
        # are clamped at MIN_COL_W (the boundary stops where either
        # one bottoms out -- the standard grid behavior) and MAX.
        j, x0, w0l, w0r = self._drag
        s = _px_scale()      # the clamps live in the same scaled
        lo = int(MIN_COL_W * s)         # pixel space as _col_w
        hi = int(MAX_COL_W * s)
        dx = x - x0
        if w0r is None:
            # boundary 1 (Name|Folder): Name absorbs, so Folder
            # adjusts INVERSELY; its growth is capped where Name
            # would drop below the guaranteed floor (NAME_MIN_W)
            avail = (self._cols_source_w() or 0) - 4
            others = sum(self._col_w) - self._col_w[0]
            cap = min(hi, avail - int(NAME_MIN_W * s) - others)
            new_w = int(max(lo, min(w0l - dx, cap)))
            if new_w != self._col_w[0]:
                self._col_w[0] = new_w
                self._apply_cols()
            return
        new_l = int(max(lo, min(w0l + dx, hi)))
        take = new_l - w0l
        if w0r - take < lo:
            take = w0r - lo            # the right column bottoms out:
            new_l = w0l + take        # the boundary stops here
        if new_l != self._col_w[j - 2]:
            self._col_w[j - 2] = new_l
            self._col_w[j - 1] = w0r - take
            self._apply_cols()

    def _on_mouse_up(self, id_dlg, id_ctl, data='', info=''):
        """Ends a column-resize drag. The widths persist with the
        window geometry at close (see _teardown), not per drag step --
        settings writes stay rare."""
        self._drag = None

    def _reset_cols(self):
        """Context-menu action: restore the default column widths
        (dragged widths are per-window live state and session-
        persisted only at close -- this resets both the window and,
        via the close-time save, the session)."""
        if self._torn:
            return
        s = _px_scale()
        self._col_w = [int(w * s) for _c, _a, w in _LIST_COLUMNS[1:]]
        self._apply_cols()

    def _on_list_click(self, id_dlg, id_ctl, data='', info=''):
        """Single click on the list (data = (x, y) in the control's
        client coords -- the same event cuda_prefs/cuda_tabs_list
        use). A click on a folder row's +/- expand marker toggles the
        folder inline -- WinMerge's tree behavior; anything else just
        selects (the control's own behavior, nothing to do here).

        A double-click fires on_click for its FIRST press, and
        possibly again for the second (LCL detail); _on_list_dbl
        leaves marker-zone clicks to THIS handler, so a repeat on the
        same row within the double-click quantum is the echo, not a
        new toggle (else expand+echo would cancel out). Column
        boundary presses are handled by _on_mouse_down and never land
        in the marker zone (it sits at the row's left edge)."""
        if self._torn:
            return
        idx = self._sel_index()
        if not (0 <= idx < len(self._view)):
            return
        row = self._view[idx]
        if not row['isdir'] or not row.get('_hk'):
            return
        try:
            x, y = int(data[0]), int(data[1])
        except (TypeError, ValueError, IndexError):
            return
        if not self._in_arrow(row, x) or not self._row_at_y(idx, y):
            return
        now = time.perf_counter()
        if self._arrow_echo[0] == idx and now - self._arrow_echo[1] < 0.5:
            return     # the double-click's second press
        self._arrow_echo = (idx, now)
        self._toggle_expand(row)

    def _on_list_dbl(self, id_dlg, id_ctl, data='', info=''):
        """Double-click (data = (x, y) client coords). Folder rows
        with scanned children expand/collapse INLINE -- the content
        appears under the folder in THIS window (the user-requested
        WinMerge behavior; the old drill-down window remains only for
        folders without scanned children). Marker-zone double-clicks
        are left to _on_list_click: it already toggled on the first
        press, and toggling again here would undo it. File rows keep
        the open/compare handoff."""
        idx = self._sel_index()
        if not (0 <= idx < len(self._view)):
            return
        row = self._view[idx]
        if row['isdir'] and row.get('_hk'):
            try:
                x = int(data[0])
            except (TypeError, ValueError, IndexError):
                x = None
            if x is None or not self._in_arrow(row, x):
                self._toggle_expand(row)
            return
        self._open_row(row)

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
        """Double-click / Enter semantics. File pairs (Identical or
        Different) open in a Differ 2 compare tab via the Command
        object; one-sided files open alone; folders with scanned
        children expand/collapse INLINE (tree mode); folders without
        scanned children (Subfolders off, or empty) keep the old
        behavior -- a drill-down compare window for a pair, the OS
        file manager for a one-sided folder. The whole handoff
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
        elif st in (ST_DIR, ST_DIR_LONLY, ST_DIR_RONLY):
            if row.get('_hk'):
                # the scan already walked inside: expand inline
                self._toggle_expand(row)
            elif st == ST_DIR:
                compare_directories(self._cmd, pl, pr)
            elif st == ST_DIR_LONLY:
                _open_in_file_manager(pl)
            else:
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
        else:
            # Tree-mode actions (folders): inline expand/collapse of
            # this folder + the global pair, WinMerge's menu items
            if row.get('_hk'):
                add(_('Collapse') if row.get('_ex') else _('Expand'),
                    lambda: self._toggle_expand(row))
            add(_('Expand all'), self._expand_all)
            add(_('Collapse all'), self._collapse_all)
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
        ct.menu_proc(hm, ct.MENU_ADD, caption='-')
        add(_('Reset column widths'), self._reset_cols)

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
        # write, see _restore_geom). The drag-resized column widths
        # go the same way, as base 96-DPI/9pt values (the scale AND
        # the UI font of a future session may differ). Both live in
        # the STATE file since the 25th release.
        if not self._app_exit and self.h:
            try:
                d = ct.dlg_proc(self.h, ct.DLG_PROP_GET)
                _set_state('dirs.win_geom',
                           ','.join(str(d.get(k, 0))
                                    for k in ('x', 'y', 'w', 'h')))
            except Exception:
                pass
            try:
                s = _px_scale()
                _set_state('dirs.col_widths',
                           ','.join(str(int(round(w / s)))
                                    for w in self._col_w))
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
    _purge_stale_state_keys()
    Profiler.start('dirs:picker')
    try:
        res = _PickerDialog().show(dir_l, dir_r)
    finally:
        Profiler.stop()
    if res:
        compare_directories(cmd, res[0], res[1])


def compare_directories(cmd, dir_l, dir_r, from_cli=False):
    """Open a NEW compare window for the two folders (any number of
    windows can be open at once). Called by the picker, by the CLI
    handler (Command.on_cli with two folders) and by drill-downs from
    an existing window's folder rows. Every fresh window is brought
    to the front and focused (focus()); from_cli=True additionally
    arms focus_later() -- the CLI dispatch races the app's own
    startup activation of the main window, and the delayed re-pull
    is what actually lands the focus on the compare window."""
    _purge_stale_state_keys()
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
    form.focus()
    if from_cli:
        form.focus_later()


def close_all():
    """Command.on_exit_pre: stop every scan worker and timer. No dialog
    calls here -- CudaText destroys the plugin's forms itself."""
    for form in list(_forms):
        try:
            form.notify_app_exit()
        except Exception:
            pass
    del _forms[:]
