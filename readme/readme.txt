Differ 2 plugin for CudaText

Compares two files side-by-side in a single tab, highlights all differences,
and lets you edit the files directly in the compare view -- your changes are
synced back to the original files automatically.


== What it does ==

- Compares two files (or two untitled tabs) in one split tab and highlights
  added, deleted, and changed lines side-by-side.
- Compares run in the background: with the native algorithms, the
  line-level diff is computed on a background thread inside CudaText, so
  the editor stays fully responsive while big files are being compared.
  The diff markers appear when the compare finishes; if you edit the
  files during the compare, the plugin re-runs it with the updated text
  automatically. Closing the diff tab (or exiting CudaText) cancels a
  still-running compare so it stops burning CPU for a result nobody
  will see. See "Background comparing" below.
- Lets you edit either side of the compare view. When you save, your edits
  are written back to the original files on disk.
- Keeps the original tabs open while you compare, so you always have both
  the originals and the compare view available.
- Remembers your compare tabs across CudaText restarts. If you close
  CudaText with a compare tab open, it is restored when you start again.
- Every diff tab is a fully standalone session: its diff records, overview
  panel, running compare and unsaved-change tracking live in their own
  per-tab world and are never shared with another diff tab. Open as many
  diff tabs as you like -- comparing in one never disturbs what another
  tab shows, and the hunk/jump/keyboard commands always act on the
  focused tab's own records.
- Provides synchronized scrolling so both sides stay aligned as you
  navigate.
- Supports word-wrap: you can turn wrap on in a compare tab and the two
  sides stay visually aligned even when corresponding lines wrap to
  different heights. Toggling wrap mode on one half turns it on in BOTH
  halves at once (same for turning it off), and the compare is
  re-run automatically afterwards so the alignment gaps are re-sized
  for the new wrap mode.
- Optional gap-aware overview panel docked to the right of the compare
  view, showing a miniature of both editors side-by-side with colored
  diff highlights (WinMerge-style), with its own scrollbar-like slider,
  one-line scroll buttons and separator line. While the overview is on,
  the editors' built-in vertical scrollbars can be hidden (they are
  replaced by the overview's slider).
- Jump to next/previous difference always lands the caret (and moves
  the focus) on the side that HAS text: one-sided differences (added
  or deleted lines, shown as a gap on the other side) put the caret on
  the changed lines, never on the empty gap side, so the copy commands
  keep working right after a jump -- from the changed lines or from
  the line next to a gap.
- Compares two FOLDERS in a WinMerge-style window: pick two folders
  (or pass them on the command line) and get a fast side-by-side list
  of what is identical, different, and unique to each side; double-
  click any file to open it in a real Differ 2 compare tab. See the
  "Compare folders" section below.

The compare engine implements all the best-known diff algorithms, with a
lot of improvements on top of each: two native ones running in compiled
Pascal code inside CudaText (JGit's Histogram Diff and WinMerge/GNU
diffutils' Myers, 10-30x faster than any pure-Python implementation on
large files, comparing on a background thread so the editor never blocks,
with cooperative cancellation when a compare is no longer needed) and
five pure-Python ones (Hybrid, Myers O(NP), VS Code, Patience and
difflib; these compare on a background thread too -- the engine runs on
a daemon thread and a poll timer picks the result up, so the UI stays
responsive, but pure-Python speed is what it is). Combined with character-level
highlighting of the exact changed characters and smart line alignment,
this gives the best human-readable compare results -- better than WinMerge,
VS Code, Meld and Beyond Compare -- while comparing faster than VS Code and
Meld. See the "Diff algorithms and best practices" section below for how
to tune the plugin for maximum speed or maximum readability.


== Commands ==

All commands are in the "Plugins / Differ 2" menu.

Compare current document with file...
    Compares the file in the active tab with another file you pick from
    a dialog.

Compare current document with tab...
    Compares the file in the active tab with another open tab.

Compare current document with next tab
    Compares the file in the active tab with the tab to its right in
    the same tab bar (no tab picker). Also in the tab context menu,
    right below "Compare with focused tab" -- there it acts on the
    right-clicked tab instead. The last tab of a group has no next
    tab: the menu item shows disabled and the command prints a status
    hint. The usual candidate rules apply (the next tab must be a
    text tab, not a Differ 2 compare tab, not a two-files-in-one-tab);
    there is no wrap-around to the group's first tab by design.

Compare clipboard to selection
    Compares the text on the clipboard with the selection of the
    focused editor, opening a new compare tab ("Diff: clipboard |
    selection"): clipboard text on the left, selected text on the
    right. Both sides use the focused editor's syntax highlighting,
    so the two halves render consistently. Also in the diff-tab
    context menu, right below "Compare with tab".

Compare two folders...
    Opens the folder-compare window for two folders you pick -- see
    the "Compare folders" section below for the full story.

Compare current file's folder with folder...
    Same, but the left side is prefilled with the folder of the file
    in the active tab (handy for "compare this file's project with
    that backup" -- no typing, no browsing).

Diff current document with file...
    Produces a unified diff (patch-style) of the active file and a file
    you pick. The result opens in a new read-only tab.

Diff current document with tab...
    Same as above, but compares with another open tab.

Note: the unified-diff output is generated by the algorithm chosen via
  differ2.algorithm.diff_algorithm (with the same native-to-Python
  fallback the compare view applies on builds without the diff_proc
  API). Every engine returns the same difflib-compatible opcode format,
  so the rendering is engine-independent: all algorithms except
  "difflib" drive an opcode-driven difflib.unified_diff work-alike, so
  hunk selection follows the configured algorithm instead of always
  running Python's stdlib difflib; the "difflib" choice calls stdlib
  difflib.unified_diff directly, keeping its classic behavior
  (including its default autojunk=True). The ignore options are
  deliberately NOT applied here: under them an 'equal' region can
  cover lines that differ byte-wise (e.g. CR vs LF endings), which
  would be emitted as context lines and make the patch unapplicable;
  a unified diff is a machine-readable patch format normally consumed
  by tools (patch, git apply, CI, code-review bots), so it must stay
  byte-exact. The ignore options keep their meaning in the side-by-side
  compare only.
  The diff ALWAYS runs in the background, whatever the algorithm and
  the file sizes: the native algorithms through the callback
  (background) form of the diff_proc API -- the same form the compare
  view uses -- and the pure-Python engines (difflib included) on a
  background thread, which also splits the file into lines, so even
  that never blocks the UI. A status-bar message ("diffing in
  background...") shows the run; when it finishes, the status bar
  prints ONE line with the total time (from the command start to the
  diff tab opening), the number of differences and the algorithm
  used, e.g. "Differ 2: diffed in 250ms, 7 differences, algo
  native_myers". A diff still running can be stopped at any time
  with the "Cancel compare" / "Cancel all compares" commands: the
  native engine job is told to stop cooperatively, a Python
  worker's still-computed result is simply dropped -- no tab opens
  and no report prints. A background diff still running at app exit
  is stopped the same way. Inputs with no differences open no tab --
  the status line's "0 differences" is the whole report (an empty
  "Diff N" tab would carry no information).

Recompare
    Re-runs the comparison after you edit either side (formerly called
    "Refresh"). With the native algorithms the compare runs on a
    background thread, so the command returns at once and the markers
    are re-applied when the compare finishes. Useful if
    differ2.advanced.enable_auto_refresh is off.

Focus the opposite file
    Moves the cursor to the other side of the split.

Swap compared editors
    Swaps the two sides of the current compare tab: the text on the
    left moves to the right and vice versa, together with its syntax
    highlighting and the other per-side display settings. Everything
    that belongs to a side follows its text: the tab title, which
    original file each side syncs back to when you save, and unsaved
    changes (a side with unsaved edits stays unsaved after the
    swap). The comparison is re-run automatically, so the difference
    colors simply trade sides -- what was "deleted on the left"
    becomes "added on the right", exactly as if you had compared the
    two files in the opposite order. Swapping twice restores the
    original arrangement. The swap is cheap: it holds no extra memory
    (both texts already exist; only a short-lived copy is made while
    rewriting the two halves) and costs one re-compare. Each side's
    undo history does not survive the swap (undo entries belong to
    the text that just moved to the other side). Not available while
    a comparison is running in that tab -- stop it or let it finish
    first. Also on the compare-tab toolbar (the ⇋ Swap button).

Select current difference
    Selects the difference block under the cursor.

Select all differences
    Selects all difference blocks in both files.

Jump to next difference
    Moves the cursor to the next changed block. When the difference is
    one-sided (lines that exist on one side only, shown as a gap on
    the other side), the cursor AND the focus go to the side that has
    the text, so the copy commands always find the difference you
    jumped to.

Jump to previous difference
    Same, backwards: moves the cursor to the previous changed block,
    also landing on (and focusing) the text side of one-sided
    differences.

Copy current difference to the right
    Copies the difference under the cursor from the left to the right
    side. Works with the caret on either side of the difference,
    including on the line next to a gap (a one-sided difference has no
    line on the gap side): copying the text over the gap fills it in;
    copying from the gap side removes the difference.

Copy current difference to the left
    Same in the other direction: copies the difference under the
    cursor from the right to the left side.

Copy current line to the right
    Copies the line under the cursor from the left to the right side,
    inserting it at the SAME HORIZONTAL LEVEL: inside a difference
    block the k-th line of one side is aligned with the k-th line of
    the other side, and lines past the other side's block end are
    aligned with its gap -- so a line copied from the middle of a
    block lands at its own level in the other side (right after the
    block's line it faces, or in the gap below), not at the block
    start. A whole-line selection copies all its lines at the level
    of its first line. The caret must be on a changed line of the
    difference (a gap has no line to copy); after jumping to a
    one-sided difference the caret is already on the changed line.

Copy current line to the left
    Same in the other direction: copies the line under the cursor
    from the right to the left side, also at the caret line's own
    horizontal level.

Config...
    Opens the options dialog.

Resize editors to equal width
    Resizes the two split editors of the current compare tab back to
    equal widths (the 50/50 split), useful after you drag the editor
    splitter. Also available at the end of the diff tab's right-click
    menu. Works on any split tab; reports a status message when the
    current tab is not split.


== Compare folders ==

"Plugins / Differ 2 / Compare two folders..." (or the command line --
see below) opens a WinMerge-style folder comparison: a non-modal
window listing every file and subfolder of the two trees, with the
per-side sizes and timestamps and a status column:

  Different     exists on both sides, content differs
  Only left     exists only in the left folder
  Only right    exists only in the right folder
  Identical     exists on both sides, same content
  Folder        exists on both sides (a folder row; its contents are
                the rows below it)
  Cannot read   stat/open failed (permissions, broken link...) --
                always shown, whatever the filters say

The FIRST pass is deliberately cheap so even huge trees fill the list
fast: nothing is ever loaded into an editor and no diff algorithm
runs. How a same-named pair is decided is chosen by the option
"differ2.dirs.compare_method":

- "Contents" (default) is WinMerge's "Quick contents" compare (the
  same algorithm, from WinMerge's own ByteCompare.cpp): different
  sizes -> Different (no read at all; the listing's own data decides);
  both empty -> Identical (no read); otherwise both files are opened
  ONCE each and compared in 32 KB chunks -- the first differing chunk
  decides Different, a pair that matches chunk for chunk to the end
  is Identical. No hash is ever computed (a chunk compare is a raw
  memcmp, several times cheaper than MD5 over the same bytes), a
  differing pair stops reading at its FIRST difference instead of
  going to the end, and an "Identical" verdict is still proven byte
  for byte.
- "Size and timestamp" (WinMerge's "Modified date and size" method):
  equal size AND equal mtime -> Identical, anything else ->
  Different. NO file is ever opened -- the whole verdict comes from
  the directory listing itself. Use this when file reads are
  expensive: files on a slow network share, cloud-sync placeholders
  (OneDrive etc. -- opening one can trigger a download), an antivirus
  hooking every open. The scan is then pure directory listing,
  whatever the tree size. Trade-offs: a file rewritten with size and
  mtime preserved can be missed, and a merely touched file (same
  size, newer time) is flagged Different without being read; the
  double-click compare never lies.

Timestamps are never trusted in the "Contents" method -- a copied
file with a fresh mtime compares as identical, as it should. (The
"Fast (sampling) compare" option differ2.dirs.quick_only keeps files
above 128 KB from being read whole: only each side's first and last
64 KB windows are compared -- the fastest mode for giant trees, at
the documented cost that a change confined to the middle of a big
file can be missed in the LIST; the double-click compare never
misses anything.)

The scan runs on a background thread (the window stays responsive and
rows stream in while it works -- watch the "Comparing... X / Y"
progress line); a second scan can be started at any time, it simply
cancels the first. Any number of compare windows can be open at the
same time -- compare two folders, then compare two other folders, and
use both windows at once; closing one never touches the others.
(That is the OPT-IN "parallel" engine described under
differ2.dirs.scan_threading below; by default the whole scan runs
synchronously on the main thread, which measured fastest by far --
see the option.)

How the walk reads the disk (why the same folders can take 30 s once
and 1 s the next time): every directory is listed exactly ONCE with
os.scandir, whose Windows DirEntry carries each entry's size and
mtime from the listing itself -- a whole tree therefore walks with
ZERO per-file stat() calls and no lstat() at all (an os.walk +
os.stat walk doubles the metadata syscalls; on a source where a
round trip is expensive -- antivirus filter, network share, cloud
placeholders -- that difference is the scan). Folder mtimes come
from the parent listing the same way. In the "parallel" scan shape
both trees, every subfolder, and the content tests for equal-size
pairs run CONCURRENTLY on a small thread pool, subfolders submitted
the moment their parent's listing arrives (no waiting behind
unrelated slow directories). The module is pure Python and pure
os.scandir: no ctypes, no platform calls, identical on Windows,
Linux and macOS.
The remaining variance is the file system's own cache: a first-ever
compare of a tree pays the cold cost of every metadata call
(antivirus pass, disk seeks, SMB round trips -- each can cost
100-200 ms), every later compare of the same tree finds it all
cached by the OS and finishes near-instantly. Comparing folders you
just copied/extracted competes with the antivirus's background scan
of those very files -- the classic signature is the FIRST tree
walking several times slower than the second, identical one. When
even the cold runs are too slow, exclude the compared folders (or
the editor's Python) from real-time antivirus scanning, or switch
the compare method to "Size and timestamp" below.

"But WinMerge is instant on the same folders": when every single
listing costs 300-800 ms and another tool on the same folders is
instant, the difference is usually the PROCESS, not the algorithm.
A real-time antivirus can bill every file operation of an unsigned
python.exe while a signed, well-known executable passes its filter
untouched: same calls, different process, a 100x different bill.
The report's dirs:listing rows show exactly what this process pays
per round trip; for folders compared often, adding CudaText (with
its Python) and the compared folders to the antivirus exclusions
removes that cost entirely.

The threading finding, and what it changed: profiling the same
folders across the three scan shapes on a Windows 7 box (no
antivirus) gave "parallel" 12.6 s (listings avg 813 ms), "serial"
13.0 s (avg 548 ms), "main" 8.1 ms (avg 0.1 ms) -- and the
measurement of the mechanism behind it: a GIL convoy -- every
background thread's listing waited up to ~412 ms to re-acquire the
interpreter lock after each metadata syscall (pure wait, zero CPU),
process-wide for as long as the scan ran, while the main thread paid
0.1 ms for the same calls. The disk, the folders, the filter stack
and the listing call were exonerated one by one; the THREAD was the
tax. Consequence: the main-thread scan is now the DEFAULT (a normal
tree freezes the window for a fraction of a second), and the
threaded shapes are the opt-in for very big trees, where rows
streaming into a live, cancelable window beat a frozen one even at
a tenth of the speed.

The window's controls:

- The two path edits are editable: type two paths and press Refresh
  (or Enter) to compare them; the Browse buttons next to them open a
  folder picker for that side.
- Swap sides mirrors the whole comparison (left becomes right).
- New compare... opens the picker dialog again -- the two combos
  remember the last 12 folders used on each side.
- The status filters (Different / Only left / Only right / Identical)
  hide or show rows instantly, without rescanning. Uncheck
  "Identical" for the usual "show me what matters" view.
- The Subfolders checkbox switches between the full recursive tree
  (default) and the two top folders only; changing it rescans.
- The Mask edit filters files by wildcard pattern ("*.py; *.txt"),
  WinMerge-style: matching files are compared, everything else is not
  listed at all. It applies on Apply (or Enter when the list has
  focus).
- Click a column header to sort by that column (Name, Folder, Status,
  left/right size or date); click again to reverse. Column widths are
  fixed (Name stretches with the window) so the header always aligns
  with the drawn rows.
- The status bar shows the scan progress / result line on the left
  and the counts (Different / Only left / Only right / Identical) on
  the right.
- The window remembers its size and position across sessions.

Row colors (the whole line, WinMerge/Beyond-Compare style): every row
is painted with the SAME colors the diff tabs use for their hunks, so
what you see in the list is exactly what the double-clicked compare
will paint:

  Different      the changed-lines color (color_changed)
  Only left      the deleted-lines color (color_deleted)
  Only right     the added-lines color (color_added)
  Identical /    no fill (the theme's list background) -- that is
  Folder         what makes the colored rows pop

The selected row shows the theme's list-selection colors instead
(selection wins, like in an editor). The colors follow the
"Color theme" option like everywhere else in the plugin.

Working with rows:

- Double-click a Different or Identical file row (or select it and
  press Enter): both files open in a real Differ 2 compare tab -- the
  full side-by-side compare with highlighting, hunk jumping, copying,
  everything. This is the moment the real diff algorithms run: the
  folder scan itself never runs them.
- Double-click a one-sided file row: the existing file opens alone in
  an editor tab.
- Double-click a Folder row (both sides): a drill-down -- a NEW
  compare window opens scoped to that subfolder pair, so you can
  compare a deep subfolder without typing paths.
- Double-click a one-sided folder row: it opens in the OS file
  manager.
- Right-click a row for the WinMerge-style sync operations: Copy to
  left/right (copy2 for files -- mtime preserved, so future compares
  see the copy as identical; copytree for folders, only into a
  missing destination), Delete from left/right, open a side's file,
  show a side's file/folder in the OS file manager, copy a side's
  full path. Copy/delete of a FILE patches just that row in place
  (re-stat + re-compare of that one pair -- no rescan); folder
  operations rescan the tree. Everything asks for confirmation first
  (differ2.dirs.confirm_ops).
- Keys: Enter opens the selected row (when the list has focus), F5
  rescans, Esc closes the window.

Notes: hidden files are included (like WinMerge); folder symlinks are
never followed (no cycles), file symlinks compare their target's
content; the filename mask is case-insensitive on Windows and
case-sensitive on Linux/macOS, matching each file system's own
behavior; unreadable folders are skipped and counted in the summary
line instead of aborting the scan.

Profiling a folder compare: the whole pipeline is instrumented with
the plugin's own profiler (see the "Profiling" section of this readme
for the general story). Turn on differ2.advanced.enable_profiling (and
additionally differ2.advanced.enable_cprofile for the function-level
layer), run a compare, and read the report in the console. The rows
NEST by time containment (see below), so the SELF column adds up:
dirs:listing rows show the directory-listing time (calls = listings,
max = the single slowest listing -- pure metadata round-trip latency:
disk, antivirus, network; huge values mean the SOURCE is slow, and
any other tool pays the same on a cold tree); dirs:quick_content
rows show the content-read time (huge values mean the file SOURCE is
slow: network share, cloud placeholders downloading, antivirus --
switch the compare method to "Size and timestamp");
dirs:walk_left/right summarize each tree's walk (listings subtract
from them); dirs:worker is the scan wall MINUS everything inside it
(just the glue); dirs:spawn_lag and dirs:finish_lag measure the
kick-off and tick-adoption overheads; dirs:ui_* show the CudaText
API time on the main thread. Rows printed with a "(parallel)" tag
ran at the same time as a sibling row (the two walks in a parallel
scan): their totals are per-thread walls, and the report prints the
parallel overlap right under the sum line -- counting the overlap
once, the SELF column totals ~100% of the outermost row in every
scan mode; a serial scan simply sums to ~100%.
One row sits OUTSIDE the scan wall, booked before the scan starts:
dirs:cprofile_import (the one-time cost of loading the cProfile
stdlib, pre-warmed on the main thread so it cannot hide inside the
scanner thread; seconds when CudaText's Python lives on a slow,
filtered drive).
After the section report a compact "folder scan facts" block prints
the same story in a few lines: the scan mode (which threading shape
produced the numbers), listings count with total/slowest/average
latency, tree sizes (file and folder rows per side), how many
content pairs stopped at the first difference vs were read to the
end, and whether the run was serial because the cProfile layer was
on.
The cProfile layer runs on the scanner thread (the walk/compare work)
and prints its function report sorted by INTERNAL time
(sort_key='time'), so the real bottleneck function sits at the top.
While that layer is on, the scan runs in serial single-thread mode on
purpose: cProfile traces only the thread that started it, so the pool
threads would be invisible in the function report (the section report
still attributes the parallel run) -- a profiled compare is therefore
somewhat slower than a normal one. To MEASURE speed, profile with
differ2.advanced.enable_profiling ON and enable_cprofile OFF, and
check the facts block's "scan mode:" line for the shape you meant
to measure: the default is now the main-thread scan, so set
differ2.dirs.scan_threading to "parallel" first when the threaded
engine is the thing to measure. A
scan cancelled by a rescan or a closing window prints nothing, like
the tab compare's cancel path.


== Keyboard shortcuts ==

These shortcuts are active only while the caret is in one of the two
halves of a compare tab; in every other tab the keys keep their normal
meaning (your own keybindings included):

    Alt+Down          Jump to next difference
    Alt+Up            Jump to previous difference
    Alt+Right         Copy current difference to the right
    Alt+Left          Copy current difference to the left
    Ctrl+Alt+Right    Copy current line to the right
    Ctrl+Alt+Left     Copy current line to the left
    F5                Recompare (re-run the compare)

They run exactly the same commands as the menu items above (including
their guards: copying is refused while a background compare is running,
jumping reports "No differences were found" on a clean compare, and the
copy commands report when the caret is not at any difference instead
of silently doing nothing).
Alt+Arrow combinations with additional modifiers (Shift/Meta, e.g.
Alt+Shift+Left, Ctrl+Alt+Shift+Left, Ctrl+Alt+Meta+Left), Ctrl+Alt+
Down/Up and modified F5 (Ctrl+F5 etc.) are NOT captured and keep
their normal behavior. When a shortcut fires, the key is consumed, so
the editor's own action (Alt+Arrow caret movement etc.) does not also
run.

The shortcuts can be turned off with the option
"differ2.advanced.enable_keyboard_capture" (Default: on). The plugin
subscribes/unsubscribes to the key events at runtime, so toggling the
option takes effect at once, without a restart.


== Gaps and one-sided differences ==

Lines that exist on one side only (pure additions or deletions) are
shown as a colored inter-line GAP on the other side. A gap is pure
visual space painted between two lines -- it is not a line, so nothing
is ever inserted into your text to keep the sides aligned, and the
caret cannot be placed inside a gap: CudaText editors do not model
carets in the space between lines.

Because of that:
- Jump to next/previous difference lands the caret, and moves the
  focus, on the side that HAS text -- never on the unchanged line next
  to a gap -- so the copy commands work right after a jump.
- Copy current difference (Alt+Left/Alt+Right) also finds a one-sided
  difference from the line next to its gap.
- Copy current line (Ctrl+Alt+Left/Ctrl+Alt+Right) needs the caret on
  a real changed line; a gap has no line to copy. The line is
  inserted at its own horizontal level on the other side (the k-th
  line of a block faces the k-th line of the other block, or the gap
  past its end), so a copy from the middle of a block never jumps to
  the block start.

To add a line where a gap is:
put the caret on the line above or below the gap, press Enter and type
-- the new line takes the gap's place as soon as the compare is re-run
(F5, or automatically with differ2.advanced.enable_auto_refresh on),
and the remaining gap shrinks by one line.


== Tab context menu ==

Right-clicking a tab title shows "Differ 2" submenu with:
- Compare with... -- pick a file to compare against this tab
- Compare with focused tab -- compare this tab with the currently focused tab
- Compare with next tab -- compare this tab with the tab to its right
  in the same tab bar (one click, no picker; disabled when this is
  the last tab of its group or the next tab cannot be compared).
  Same as the command, but acting on the right-clicked tab.
- Compare with tab -- submenu listing all open tabs; click one to compare.
  If the list is too long, the first entry "More tabs..." opens a dialog
  with a scrollbar to pick any open tab.
- Compare clipboard to selection -- open a new compare tab with the
  clipboard text on the left and the focused editor's selection on
  the right (same as the command; enabled only while both sides
  exist: text on the clipboard and a selection).
- Recompare -- re-run the compare on both sides of the compare tab.
- "Cancel compare" / "Cancel all compares" -- stop in-flight background
  compares AND in-flight background unified diffs (the "Diff current
  document with..." commands): the native engine jobs stop
  cooperatively, a Python worker's still-computed result is dropped.
- Resize editors to equal width -- set the split back to 50/50 after
  dragging the editor splitter. This is the last entry of the menu.
- Five checkable "ignore" options (below Recompare, after a separator):
  Ignore case, Ignore whitespace, Ignore blank lines, Ignore line endings,
  Ignore numbers. Shown only while a native algorithm is the effective
  one (they do nothing for the pure-Python algorithms, which always
  compare strictly); the items appear/disappear on the next right-click
  after the algorithm is changed in the config dialog.
  Ticking one re-runs the compare immediately with that option applied;
  the checkmarks always mirror the saved settings, so the config dialog
  and this menu stay in sync in both directions (toggling here writes the
  setting to settings/cuda_differ2.json, and changing a setting in the
  config dialog is reflected here the next time the menu opens). See
  "Ignore options" below for what each option does.

== Toolbar ==

Every compare tab gets a toolbar docked to the top of the compare view
(above the two editors, spanning the tab's width; its background uses
the editor text background color of the current UI theme, so it blends
into the compare view):

    [↻ Recompare or × Cancel] | [↑ Prev][↓ Next] | [→ Copy][← Copy] |
    [≡ Ignore 2/5 ▾] [★ Preset ▾] | [⇋ Swap] [↔ Resize] [▦ View ▾] [⚙ Config]
    ...status

- ↻ Recompare -- re-runs the compare (same as F5 / the menu's
  Recompare). While a compare runs, the button becomes × Cancel and
  cancels that tab's running compare; when the compare finishes or is
  cancelled it becomes ↻ Recompare again. The swap to × Cancel is
  DELAYED by one second after the compare starts, and a click landing
  inside that second is swallowed: a double-click (or a late
  button-up) on Recompare can no longer cancel the compare it just
  kicked off -- the accidental second click hits a Recompare that
  ignores it, not a Cancel. A genuine cancel is available one second
  later, or immediately via the "Cancel compare" command / the tab
  context menu. If the compare finishes within the delay (small
  files compare in well under a second), the button simply stays
  ↻ Recompare with the result.
  While a compare runs, the two editors lock read-only (busy
  placeholder in both halves) -- but only once the compare has been
  running for over 5 seconds: a compare that finishes inside those
  5 seconds never locks the editors at all, they stay fully
  editable for its whole duration. The lock is released as before
  the moment the compare finishes or is cancelled.
- ↑ Prev / ↓ Next -- jump to the previous/next difference (same as
  Alt+Up / Alt+Down). Disabled while there are no differences or a
  compare is running.
- → Copy / ← Copy -- copy the current difference hunk to the right /
  left side (same as Alt+Right / Alt+Left). Both halves' carets are
  parked at the hunk start before the text change, so undo/redo
  lands the caret at the copied hunk instead of jumping to a far-off
  line, and the buttons never move the editor focus: the caret stays
  where the user left it.
- ≡ Ignore 2/5 ▾ -- dropdown with the five "ignore" options as
  CHECKABLE items -- multiple options can be checked at once (they
  combine; see "Ignore options" below). The counter shows how many of
  the five are enabled (omitted when none is). The last item, after a
  separator, is "Uncheck all options". Ticking an item re-runs the
  compare immediately; the dropdown is rebuilt on every compare start
  so it always matches the config dialog and the tab context menu.
  While a pure-Python algorithm is the effective one, the items
  disable themselves and an explanatory item heads the menu (same
  guard as the tab context menu).
- ★ Preset ▾ -- dropdown with quick "preset" combinations: "Preset 1:
  Fastest comparison - Myers, Align Off, Absorb Off" and "Preset 2:
  Better readability (slower) - Histogram, Align On, Absorb Off"; the
  two presets are
  RADIO items (a dot mark instead of a checkmark; clicking one
  unchecks the other), so at most one is ever marked. After a
  separator, "Algorithm 1: Native Histogram" and "Algorithm 2:
  Native Myers" (also radio items, also mutually exclusive) and the
  two independent checkable toggles "Align by similarity" and
  "Absorb trivial equal blocks" (each works with either algorithm).
  The preset marks are DERIVED from the current settings on every
  menu open -- a preset is an EXACT combination of three settings:
  Native Myers + Align off + Absorb off checks Preset 1, Native
  Histogram + Align on + Absorb off checks Preset 2, any other
  combination (Absorb on included) checks NEITHER -- so a custom
  selection is visible at a glance. Picking a preset writes ALL
  THREE settings (differ2.algorithm.diff_algorithm /
  differ2.algorithm.beautify.align_by_similarity /
  differ2.algorithm.beautify.absorb_trivial_equal_blocks -- the same
  settings the config dialog edits; both presets write Absorb off)
  and re-runs this tab's
  compare immediately. The button is disabled while a compare runs
  (like the Ignore dropdown); its tooltip shows the current
  algorithm + both beautify flags.
- ⇋ Swap -- swap the two sides of this compare tab (same as the
  "Swap compared editors" command): the texts trade places together
  with their syntax highlighting and per-side settings, and the
  compare re-runs automatically. Not available while a compare is
  running in this tab (the command's own guard refuses it with a
  status hint).
- ↔ Resize -- resize the two editors to equal width (50/50) after
  dragging the splitter.
- ▦ View ▾ -- dropdown to show / hide the surrounding UI from the
  compare tab: "Hide all" flips everything at once, "Show all"
  restores everything EXCEPT the side and bottom panels (a bulk show
  must not force those docked tool windows open, so their items stay
  unchecked), and CHECKABLE items toggle CudaText's status bar,
  toolbar, sidebar, side panel, bottom panel, tab bar, the gutter's
  columns (both halves of the tab always change together) and the
  gap-aware overview panel. The checkmarks are re-derived from the
  live state on every menu open (the bars can be toggled from
  CudaText's own View menu too). Ticking "Overview" writes
  differ2.micromap.enable_overview (the same setting the config dialog
  edits) and re-runs this tab's compare immediately, whose refresh
  creates or destroys the panel; the bar / gutter items apply at
  once with no re-compare. Unlike Ignore / Preset the button stays
  enabled while a compare runs; its tooltip lists the currently
  hidden items.
- ⚙ Config -- opens the Differ 2 options dialog.
- status label (right side) -- shows the compare state: "Comparing..."
  while a compare runs, "N differences" / "No differences" when it
  finishes, "Cancelled" after a cancel. This replaces the old status
  bar progress spam.

Every button has a tooltip (with the hotkey hint where one exists).
Buttons size themselves to their captions (auto-sized with the real
UI-theme font, DPI-scaled -- nothing can be cut off horizontally) and
are chained with anchors, so the row re-flows by itself whenever a
caption changes; neighboring buttons inside one group are separated
by a roomy gap (DPI-scaled -- the spacing around the separators and
at the row's edges stays tight). The bar ends in a thin full-width
border line that separates it from the editor below: exactly 1 device
pixel at any DPI (a height-1 panel strip filled with the theme's SplitMain color;
dlg_proc never scales control sizes). The toolbar's height = the OS GUI
button height plus a little extra room for the captions' descenders (on
some systems the OS font is taller than the OS 'button' metrics and
letters like the 'g' of "Config" would be clipped) plus the 1px border
line -- the descender room is scaled for high-DPI screens, the border
line itself never is. The
captions' texts can be turned off (icons only) with the option
differ2.toolbar.show_btn_text; the whole toolbar can be hidden with
differ2.toolbar.show_toolbar. Toolbars are restored at startup for
compare tabs restored by the CudaText session, and follow UI theme
switches (the background re-reads EdTextBg; the status label's text
color re-reads EdTextFont of the same theme, so it stays readable on
dark themes too -- a bare label would take the OS's black; the border
line's color re-reads SplitMain).

The toolbar's lifetime follows the tab's: it is destroyed when the
compare tab is really closed. Nothing is cleaned up at app exit: the
toolbar forms are owned by CudaText's main form, which frees them
when the app terminates. (They are deliberately NOT destroyed inside
on_close's app-exit branch: that event fires synthetically from
CudaText's own exit loop, and GUI calls there re-enter the message
processing between the plugin's state-file writes, which could cost
the compare tabs their persisted session entries.)

== Ignore options ==

Five options control what kind of differences the compare treats as
"not a difference". They apply to BOTH the line-level diff and the
char-level highlighting inside changed lines (except "Ignore blank
lines", which is a line-level concept and does not affect the char-level
details), and only to the two NATIVE
algorithms (Native Histogram and Native Myers) -- the pure-Python
algorithms always compare strictly and ignore these options. Because of
that, the options are HIDDEN while a Python algorithm is selected: the
config dialog does not show them and the diff-tab context menu does not
add its checkable items (both pick it up the next time they are opened
after the algorithm is changed). Set them from the diff tab context menu
(see above) or from the config dialog ("differ2.ignoreopt.*" options in
settings/cuda_differ2.json) while a native algorithm is active.

- Ignore case -- 'A' and 'a' count as equal. ASCII only; non-ASCII
  letters are compared byte-for-byte.
- Ignore whitespace -- spaces and tabs anywhere in a line are skipped:
  "a b" equals "ab" and "a   b" equals "a  b". Whitespace means SPACE and
  TAB only -- vertical tab and form feed are not whitespace, and line
  endings are covered by their own option below.
- Ignore blank lines -- changes that only insert or delete blank lines
  are ignored, like WinMerge's "Ignore blank lines" and GNU diff's -B
  option. A hunk is ignored when ALL of its lines are blank on both
  sides; a line is blank when it is empty, or (with "Ignore whitespace"
  also on) contains only spaces and tabs. Ignored regions keep the two
  sides aligned, WinMerge-style: a small compensating gap fills in for
  the missing lines. By default both the ignored lines and the ignored
  gap are painted with the editor text background color, so an ignored
  region looks like normal text -- set "Color of ignored differences"
  (the lines) and/or "Color of ignored difference gaps" (the gap) to
  make them visible. Ignored regions are not counted as differences:
  no bookmarks, skipped by Next/Previous Difference and Copy, and two
  files differing only in blank lines report "No differences found
  (with current ignore options)".
- Ignore line endings -- the line terminators (CR, LF, CRLF) are not
  compared: a Unix file and the same file saved with Windows or old-Mac
  line endings compare as equal. Without this option, differing line
  endings are real differences and the extra CR is highlighted inside
  the changed lines.
- Ignore numbers -- digit characters (0-9) are skipped anywhere in a
  line: "v33" equals "v", "aaa 666666 ttttt" equals "aaa 333333 ttttt",
  and "v12.45 11:13:45" equals "v22.4 12:23:4". Digits are never
  highlighted inside changed lines either -- "aaa 666666 tttttdd" vs
  "aaa 33333 ttttt" highlights only the "dd".

Notes:
- The options can be combined freely; they all apply at once.
- When every difference in the two files is ignored (e.g. the files
  differ only in line endings and 'Ignore line endings' is on, or only
  in blank lines and 'Ignore blank lines' is on), Differ 2
  tells you: a "No differences found (with current ignore options)"
  message appears instead of an uncolored compare tab.
- These options are passed to CudaText's diff_proc API as the
  DIFF_IGN_* bitmask (see the diff_proc documentation in the CudaText
  wiki).
- The related "Word-break characters" option (differ2.algorithm.
  break_chars, "algorithm" chapter of the config dialog) tunes the
  same native char-level engine: the characters words are split at for
  the highlights inside changed lines. It is passed to diff_proc as
  the break_chars parameter.


== Background comparing ==

With the native algorithms (Native Histogram, Native Myers), the
line-level diff runs on a background thread inside CudaText, started
through the callback form of the diff_proc API. The editor never blocks
while two files are being compared: you can keep typing, scrolling,
switching tabs and using menus during the compare, no matter how big the
files are.

- The compare markers are applied when the compare finishes. While it
  runs, the status bar shows "Differ 2: comparing in background...", and
  the usual "Differ 2: compared in Xms" message reports the total time
  (background compare + painting) when it is done.
- If you edit either side while a compare is running, the plugin detects
  it when the compare completes and re-runs the compare with the updated
  text automatically -- the painted result always matches the current
  editor content.
- Closing the diff tab while a compare is still running CANCELS the
  engine compare: the plugin tells the engine to stop through the
  diff_proc(DIF_CANCEL) API, and the engine's diff loops unwind within a
  couple of seconds instead of grinding to the end for a result nobody
  will consume. Cancellation is cooperative -- the engine releases
  everything the compare allocated, nothing leaks -- and the completion
  callback of a cancelled compare is never invoked. Exiting CudaText
  (on_exit_pre) cancels every still-running compare the same way.
- Only one compare runs per compare tab at a time; refresh requests that
  arrive while a compare is running are folded into the next run.
- Comparing two different compare tabs at the same time works: each tab
  gets its own background compare.
- The pure-Python algorithms (Hybrid, Myers, VS Code, Patience, difflib)
  run their engine on a background daemon thread with a poll-timer
  pickup on the main thread, so the UI stays responsive -- but they are
  still far slower than the native ones on big files, which is one more
  reason to prefer the native algorithms there. (The "Diff current
  document with..." unified-diff commands use the same thread form for
  the Python-side engines -- for EVERY input size, just like they use
  the native engine's background form for the native algorithms.)
- Slow-compare offer: when a background compare has been running for
  over a minute and is not already using the fastest combination
  (Native Myers with both beautify options off -- the Preset 1
  combination), the plugin asks once:
  keep waiting, or switch to that faster combination for this compare?
  "Switch" cancels the running compare and re-runs it in the fast mode;
  it is temporary -- it applies to that compare tab until the tab is
  closed (every later re-compare of the tab stays fast) and the
  configured algorithm / beautify options in the settings are never
  touched. "Continue" (or closing the dialog) just keeps waiting.
  Compares already running the fast combination never ask.


== Saving and syncing changes ==

When you edit files inside the compare view and press Ctrl+S:

- Both sides of the compare are synced to their original tabs at once.
- Your edits are written back to the original files on disk immediately,
  so the files on disk reflect your changes.
- If an original was an untitled tab (no file on disk), it is marked as
  modified (dot on the tab) but no Save dialog appears. You can save it
  later if you want.
- The compare tab itself is never saved to disk -- it is an untitled
  scratch tab. Ctrl+S only triggers the sync to the originals.
- After a successful sync, the compare tab's title turns green to
  indicate its changes have been pushed to the originals. When you edit
  again, the title returns to the normal modified color (red).
- Undo/Redo history is preserved in the original tabs, so you can undo
  the synced changes with Ctrl+Z after switching to the original tab.


== Compare tabs, restarts and session switches ==

Compare tabs survive CudaText restarts:

- If you close CudaText with a compare tab open, the compare tab is
  restored when you start CudaText again, with the same content but
  without compare, run Recompare command to start the compare.
- The plugin loads automatically on startup only when compare tabs are
  active, so there is no performance impact when you are not comparing.
- When you close the last compare tab, the plugin stops auto-loading on
  the next startup.
- Each diff tab is its own standalone session. Its diff records,
  overview panel, in-flight compare, change-suppression counter and
  saved/dirty tracking live in a per-tab session object, so nothing
  leaks between diff tabs: comparing in tab B does not change what the
  hunk/copy/jump commands do in tab A, and closing tab B destroys only
  tab B's world. The session also remembers which CudaText session
  file the tab was registered under, so dirty/saved state keeps going
  to the right persisted group even after you switch CudaText sessions.
- Each tab's session also isolates the compare engine: two diff tabs
  can run background compares at the same time, and a running compare
  only blocks refreshes of its OWN tab ("compare already running" is
  per tab). One known limitation: the optional profiling report is a
  global diagnostic -- if you enable profiling and compare two tabs
  concurrently, the printed timings interleave (profiling is off by
  default).

Switching CudaText sessions (the Sessions menu, app_proc(PROC_LOAD_SESSION),
session-manager plugins) keeps the tracking alive too: a session switch
closes the old session's tabs to replace them, and those closes are NOT
user closes -- the plugin recognizes them through CudaText's
APPSTATE_SESSION_LOAD_BEGIN_PRE event (which fires before the switch's
first tab close; APPSTATE_SESSION_LOAD_BEGIN arrives only after the
closes, too late to be usable) and keeps every closed compare tab's
persisted registration, because the tab still exists in the session file
being left. When you switch back, the restored compare tabs are
re-attached exactly like after a restart (per-tab session, toolbar,
title color, dirty/saved state; run Recompare to start the compare), and
closing them afterwards cleans up as usual. This needs a CudaText build
with the APPSTATE_SESSION_LOAD_BEGIN_PRE event (a fresh build with the
author's merged patch).

When you close a compare tab manually (not via app exit):
- While a comparison is running, the tab is busy and cannot be closed
  yet: clicking its close button (the x on the tab) does nothing.
  Stop the comparison first -- the x (Cancel) button on the compare
  toolbar, or the "Cancel compare" command -- or simply wait for it
  to finish; the close button works again right after.
- If you have unsaved changes, CudaText asks whether to save or discard.
- If you save, your changes are synced to the original files.
- If you discard, the originals keep their last-saved content.
- If you cancel, nothing happens -- the compare tab stays open and
  fully functional.


== Command-line support ==

You can start a comparison from the command line:

    cudatext -p=cuda_differ2#filename1#filename2

This launches CudaText with the two given files opened in the Differ 2 plugin.

Pass two FOLDERS instead, and the folder-compare window opens for
them (the parameters are auto-detected):

    cudatext -p=cuda_differ2#/path/to/left/folder#/path/to/right/folder

As with filenames, paths with spaces must be passed inside quotes
around the whole flag:

    cudatext "-p=cuda_differ2#C:\my project#D:\backups\my project"

The command-line also understands -c=cuda_differ2,compare_dirs to
just open the folder picker dialog at startup (CudaText's generic
-c= mechanism; see CudaText's own command-line documentation).


== Options ==

Open the options dialog via "Options / Settings-plugins / Differ 2 / Config"
or "Plugins / Differ 2 / Config...".

All options are stored in settings/cuda_differ2.json. The option names grouped into seven categories: theme, algorithm, ignoreopt, advanced, micromap, toolbar, dirs.

Ignore options section (see the "Ignore options" chapter above for details):
- differ2.ignoreopt.ignore_case: Ignore case (default: off)
- differ2.ignoreopt.ignore_whitespace: Ignore whitespace -- spaces and tabs (default: off)
- differ2.ignoreopt.ignore_blank_lines: Ignore blank lines -- all-blank hunks
  suppressed, painted as ignored regions with a compensating gap (default: off)
- differ2.ignoreopt.ignore_eol: Ignore line endings -- CR/LF/CRLF (default: off)
- differ2.ignoreopt.ignore_numbers: Ignore numbers -- digits 0-9 (default: off)
  These five options build the DIFF_IGN_* bitmask passed to the native
  diff engines. They only affect the native algorithms (Native Histogram
  and Native Myers); the pure-Python algorithms always compare strictly.

Theme section:
- differ2.theme.color_theme: Color theme (default: auto)
  Which compare colors to use:
    * auto -- detect the light family of the current UI theme and use
      its preset. Known UI themes carry a fixed family (amy, cobalt,
      darkwolf, ebony, sub -> black; green, navy, the default "" theme ->
      grey; syn -> white); unknown/custom themes fall back to the
      luminance of the editor background. Recommended.
    * white -- preset tuned for white editor backgrounds:
      changed #f8dfad, added #b3ffb3, deleted #ffc4c4, gap #e3e3e3,
      ignored and ignored gap #ffffff.
    * grey -- preset tuned for light-grey editor backgrounds (#E0E0E0,
      like the green/navy themes): the white family's colors deepened
      ~25 units, so they keep their contrast against grey.
    * black -- preset tuned for dark editor backgrounds (muted, so the
      diff blocks do not glare on dark themes).
    * custom -- use the six color options below; every option left
      empty is filled from the auto-detected preset, so a
      half-configured custom theme never falls back to nothing.
  In the grey and black presets the ignored-difference colors resolve to
  the live editor background, so ignored regions blend into the active
  theme. The six color options below only apply in the "custom" mode.
- differ2.theme.changed_color: Color of changed lines
  Background color for lines that were modified (replaced with different
  content). Also colors the char-level highlights inside modified lines,
  the margin markers, the micromap highlights and the overview panel.
  Only used when "Color theme" is custom; leave empty to fill this slot
  from the auto-detected preset.
- differ2.theme.added_color: Color of added lines
  Background color for lines that exist only in the right file (added).
  Also colors the char-level highlights inside added lines, the margin
  markers, the micromap highlights and the overview panel.
  Only used when "Color theme" is custom; leave empty to fill this slot
  from the auto-detected preset.
- differ2.theme.deleted_color: Color of deleted lines
  Background color for lines that exist only in the left file (removed).
  Also colors the char-level highlights inside deleted lines, the margin
  markers, the micromap highlights and the overview panel.
  Only used when "Color theme" is custom; leave empty to fill this slot
  from the auto-detected preset.
- differ2.theme.gap_color: Color of inter-line gap background
  Background color for the blank gap inserted to keep the two sides
  aligned when one side has fewer lines. Also colors the gap rectangles
  in the overview panel.
  Only used when "Color theme" is custom; leave empty to fill this slot
  from the auto-detected preset.
- differ2.theme.ignored_color: Color of ignored differences
  Background color for lines whose difference is suppressed by the
  "Ignore blank lines" option (WinMerge-style ignored differences).
  Also colors the micromap highlights and the overview panel.
  Only used when "Color theme" is custom; leave empty to fill this slot
  from the auto-detected preset (the editor text background for the grey
  and black families, so the ignored region then looks like normal text).
- differ2.theme.ignored_gap_color: Color of ignored difference gaps
  Background color for the compensating inter-line gap inserted next
  to a suppressed blank-line difference ("Ignore blank lines" option),
  so the two sides stay aligned. Separate from "Color of ignored
  differences" (the lines) and from "Color of inter-line gap
  background" (regular alignment gaps); also colors the ignored-gap
  rectangles in the overview panel.
  Only used when "Color theme" is custom; leave empty to fill this slot
  from the auto-detected preset (the editor text background for the grey
  and black families, so the ignored gap then looks like empty space).

Algorithm section:
- differ2.algorithm.diff_algorithm: Diff algorithm
  Selects the diff algorithm used by the side-by-side compare and by
  the unified-diff commands ("Diff current document with file... /
  with tab..."): all engines except "difflib" drive the unified-diff
  renderer through their opcodes (see the note in the Commands
  section), so hunk selection follows this setting there too; the
  "difflib" choice makes those commands call stdlib
  difflib.unified_diff directly (its classic behavior).
  Native algorithms run in compiled Pascal code and are 10-30x faster
  than the pure-Python implementations on large files. Their line-level
  diff runs on a background thread (the callback form of the diff_proc
  API), so CudaText stays responsive while big files are compared, and a
  compare that is no longer needed (tab closed, app exiting) is stopped
  cooperatively through diff_proc(DIF_CANCEL). They require a CudaText
  build that includes the diff_proc API; if it is not available, they
  silently fall back to the closest Python equivalent
  (native_histogram -> hybrid, native_myers -> myers).
    * native_histogram -- Native Histogram diff (port of JGit's Histogram
      Diff, with JGit's Myers O(ND) Diff as internal fallback for
      sub-regions -- the same algorithm git uses for "git diff
      --histogram"). Behaves like Patience diff when unique common lines
      exist, with graceful fallback when they don't. Fast and
      high-quality (more human-readable in some cases).
    * native_myers -- Native Myers diff (port of WinMerge's bundled GNU
      diffutils Myers O(ND), the same algorithm git uses for "git diff
      --myers"), the fastest on large/very different files. It is faster
      because it builds on top of Myers with additions from diffutils and
      WinMerge that JGit lacks, such as Paul Eggert's TOO_EXPENSIVE
      heuristic, line-purging heuristics like DiscardConfusingLines, and
      other optimizations.
      This is the default: it renders the compare the way WinMerge / GNU
      diffutils side-by-side (sdiff) output does, and it is the fastest
      engine on huge files.
  The other algorithms are pure-Python so they may be slower with very
  big files:
    * hybrid -- Pure-Python Hybrid, combines Patience (anchoring on
      unique lines) with Myers O(NP) for the gaps. Best pure-Python
      quality (more human-readable in some cases).
    * myers -- Pure-Python Myers O(NP) (Wu/Manber/Myers/Miller), ported
      from Meld.
    * vscode -- Pure-Python VS Code diff algorithm. This is a port of
      Microsoft VS Code's diff implementation, which uses dynamic
      programming with equality scoring (best alignment on files with
      many duplicated lines, more human-readable in some cases, but this
      is the slowest).
    * patience -- Pure-Python Patience diff (via the embedded
      patiencediff library), anchors on unique matching lines, more
      human-readable in some cases. Bad alignment on files with many
      duplicated lines like log files.
    * difflib -- Python's standard difflib SequenceMatcher with
      autojunk=False (the unified-diff commands instead call stdlib
      difflib.unified_diff directly, with its default autojunk=True).
  If some parts of a diff are hard to read, try testing a different
  algorithm: Histogram, Hybrid, Patience, or VSCode tend to generate much
  cleaner results.
  If you're comparing massive files and need maximum speed, stick with
  the Native Myers algorithm instead.
  Default: native_myers.
- differ2.algorithm.compare_with_details: Detailed comparison
  When enabled, modified lines are compared character-by-character,
  highlighting specific differences within the line. This uses a ported
  implementation of WinMerge’s character-diff engine, combining Myers O(NP)
  with custom heuristics and optimizations.
  When disabled, modified lines are highlighted as a whole.
  Disabling this speeds up the compare of big files.
  Default: on.
- differ2.algorithm.break_chars: Word-break characters
  Characters that split words for the character-level highlights inside
  modified lines -- the word tokenizer of the native char-diff engine
  (the same setting WinMerge calls "Word break characters"). Every
  character of the string is its own token and a word boundary, so
  words break at it: with the default
  ".,:;?[](){}<=>`'!"#$%&^~\|@+-*/" the line "v1.2.3, done" tokenizes
  as "v1" "." "2" "." "3" "," " done".
  How it changes the highlights: after the word-level compare, the
  engine refines every changed region by trimming its common prefix
  and suffix, so a SINGLE change inside a word always highlights only
  the changed characters -- no matter which break chars are set.
  "v1.2.3" vs "v1.2.4" gives exactly the same "3" / "4" highlight with
  the default, with "!" and with "" (this is expected, not a bug). The
  set matters when one region contains SEVERAL separated changes or
  the separators themselves changed, because equal break chars become
  anchors that split the region into separate highlights:
    * "v1.2.3" vs "v1.9.4" -- with the default (dot breaks) two
      highlights, "2"->"9" and "3"->"4", and the dots between them
      stay unhighlighted; with "" (or any set without the dot, e.g.
      "!") one continuous highlight "2.3" -> "9.4" that includes the
      dots.
    * "v1.2.3" vs "v1-2-3" -- with the default (dash breaks too) both
      sides tokenize the same way, the digits anchor the compare, and
      only the two "." -> "-" swaps are highlighted; with a set that
      has no dash, e.g. ",.;:" (or "!"), the dash-side is the single
      word "v1-2-3" and the whole ".2." -> "-2-" gets highlighted.
  Examples:
    * ".,:;?[](){}<=>`'!"#$%&^~\|@+-*/" -- the default: WinMerge's
      "Word break characters" options list (recommended; it is what
      WinMerge itself runs with -- see the note below).
    * ",.;:" -- the four separators (comma, period, semicolon, colon)
      the WinMerge engine source hard-codes as its internal fallback:
      a coarser set -- dash, slash, brackets, quotes and other
      punctuation no longer break words.
    * "" (empty string) -- punctuation never breaks words: words are
      then split on whitespace only, and a region with several changes
      is painted as one block that includes the unchanged punctuation
      between them.
    * Add characters that are NOT in the default, e.g. "_" for
      snake_case identifiers ("a_b_c" vs "a_x_y" then highlights the
      two letters separately instead of one "b_c"/"x_y" block) --
      paths, URLs, dates and kebab-case are already covered by the
      default's "/" and "-".
  A note on the default (WinMerge research): the engine's
  stringdiffs.cpp Init() hard-codes only the small fallback ",.;:"
  that runs until SetBreakChars() is called, but WinMerge's Options
  dialog (Compare / "Whitespace & breaks") stores the long list above
  as the "Word break characters" setting's default, and on every
  compare WinMerge reads the saved setting and calls SetBreakChars()
  with it -- so the long list is what the engine effectively runs
  with in normal use. This plugin (and the CudaText diff_proc default)
  mirror that effective default, not the never-used fallback.
  Notes:
    * Whitespace, CR/LF and (with "Ignore numbers" enabled) digits are
      classified before the break-char check, so listing them has no
      effect.
    * Non-ASCII characters always break words regardless of this
      option.
    * One set applies to every line pair of a compare; the line-level
      diff (which lines are changed) is not affected by it, only the
      char-level highlights inside the changed lines.
    * A hand-edited non-string value in settings/cuda_differ2.json
      falls back to the default (the engine accepts strings only).
  Only used by the native algorithms (Native Histogram and Native
  Myers) with "Detailed comparison" enabled -- the pure-Python
  algorithms break words at every punctuation character and ignore
  this option, so it is hidden from the config dialog while a Python
  algorithm is the effective one. Requires a CudaText build whose
  diff_proc API supports the break_chars parameter (the 5_add_differ_api
  branch with the break-chars patch).
  How to verify it in 30 seconds: create two 3-line files,
  A = "v1.2.3" / "v1.2.3" / "v1.2.3" and B = "v1.2.4" / "v1.9.4" /
  "v1-2-3", and compare them. With the default: line 1 highlights only
  "3"/"4"; line 2 highlights "2"->"9" and "3"->"4" with the dots
  unhighlighted; line 3 highlights only the two "." -> "-" swaps with
  the "2" unhighlighted. Now set the option to "!" and re-compare:
  line 1 is unchanged (expected -- a single change is always trimmed
  to the changed chars), but line 2 becomes ONE block "2.3" -> "9.4"
  with the dots included and line 3 becomes the whole ".2." -> "-2-"
  (the dot and the dash no longer break words). Set the option to
  ",.;:" and re-compare: line 2 is back to the two separate digits
  (the dot is in the set), line 3 stays the whole ".2." -> "-2-"
  (the dash is not). (All expected results verified against the
  engine.)
- differ2.algorithm.beautify.align_by_similarity: Align by similarity
  Beautify line alignment inside REPLACE blocks where the two sides have
  DIFFERENT line counts.
  - When OFF (algo-faithful): lines are paired top-down by position for
    the first min(da, db) lines, and leftover lines on the longer side
    are shown as plain added/deleted lines against a gap at the bottom
    of the shorter side. Nothing is re-paired or re-ordered.
    This renders exactly the way the algorithm dictates; for example if
    native_myers is used it renders the way WinMerge / GNU diffutils
    side-by-side (sdiff) output does.
  - When ON (VS Code-like): the engine's hunks are re-paired by
    similarity -- finds best pairs anchored on the longest unique exact
    match or the best prefix/suffix-similar pair, char-diffs them, and
    recurses on both sides. Lines with < 3 chars of similarity are shown
    as separate delete+add. This re-arranges the engine's output for a
    more "aligned" look but is no longer a faithful rendering of the
    diff.
  This results in a more human-readable diff in some cases, but the
  compare becomes slower with very big files.
  Applies to both native and Python algorithms.
  Equal-count REPLACE blocks (da == db) are positional in BOTH modes, so
  this option only affects unequal-count REPLACE blocks.
  Default: off.
- differ2.algorithm.beautify.absorb_trivial_equal_blocks: Absorb
  trivial equal blocks
  Opcode beautify pass on the engine's finished result, applied in the
  side-by-side compare view only (the unified-diff commands always use
  the raw algorithms):
  - merges the INSERT + EQUAL(trivial) + DELETE pattern (or its
    DELETE-first mirror) into a single REPLACE. Engines sometimes match
    a trivial line (a blank, a lone '}') across a change instead of a
    meaningful one; the raw opcodes then show the same content as one
    added + one deleted line instead of a paired change;
  - absorbs a short trivial EQUAL block (at most 4 non-whitespace
    characters) stranded between two changed blocks into a single
    REPLACE when at least one of the two changed blocks is large --
    without this, one big changed region can come out fragmented into
    several pieces that read as several unrelated changes and pair a
    line with the wrong line of the other file.
  Ported from VS Code's heuristicSequenceOptimizations (VS Code runs
  the equivalent optimizations unconditionally inside its own diff
  algorithm).
  Guards: only EQUAL blocks with at most 4 non-whitespace characters
  are ever absorbed (matched real content is never merged away); two
  small changes separated by a matched blank stay separate; suppressed
  blank-line differences ("Ignore blank lines") are barriers and are
  never resurrected into a shown change.
  - When OFF (default): the engine's raw opcode stream is rendered
    exactly as the engine produced it -- GNU diffutils / WinMerge
    faithful.
  - When ON: every algorithm's opcodes (the native engines included)
    go through the pass, so switching algorithms changes tie-breaking,
    not hunk structure.
  See _dev/__tests/test_absorb_trivial_equal_blocks.py for worked
  examples (before/after opcode lists and side-by-side renderings of
  both steps).
  Default: off.

Advanced section:
- differ2.advanced.sync_scroll: Synchronized scrolling
  When enabled, scrolling one side of the compare view also scrolls the
  other side, both vertically and horizontally.
  Default: on.
- differ2.advanced.enable_sync_caret: Keep carets visible on sync
  When enabled, moving the cursor in one side also moves the cursor in
  the other side to the corresponding difference block, so both carets
  stay visible in the current screen area.
  Default: off.
- differ2.advanced.enable_auto_refresh: Auto-refresh after changes
  When enabled, the diff markers are automatically re-calculated after
  you stop editing for 1-2 seconds. When disabled, you must use the
  Recompare command manually.
  Default: off.
- differ2.advanced.diff_context: Context lines in unified diff
  Number of unchanged context lines shown around each change in the
  unified diff output (produced by the "Diff current document with..."
  commands).
  Default: 3.
- differ2.advanced.enable_profiling: Enable profiling
  Enable profiling to trace where compare time is consumed.
  When enabled, prints the SECTION report to the console after each
  compare (which PHASE eats the time), and, when the cProfile layer
  is also on (differ2.advanced.enable_cprofile), the cProfile report
  (which FUNCTION eats the time) after it, sorted by self time.
  The section report is sorted by SELF time (time inside a
  row EXCLUDING its nested rows), so the real bottleneck is at the
  top. Rows you will see:
  - line_diff:native_engine -- the background line-level engine
    (kick-off to completion callback).
  - compare:algorithm -- the synchronous engine call / the engine
    wait wrapper.
  - char_diff:native_engine (or :python_engine) -- the char-level
    diff engine calls, timed per pair with perf_counter and booked
    in batches: 'calls' is the number of line PAIRS, 'max' the
    slowest single call.
  - compare:split_lines -- splitting the raw texts into line lists
    (in the native differ this happens inside compare(); the Python
    differ splits in refresh_compare under the same tag). On a
    1M-line / 50MB compare this is ~3.3s of real work.
  - compare:positional_pairs -- building the paint events for
    REPLACE-block chunks in the positional pairing mode (the
    algo-faithful default and the beautify fast path):
    'calls' is the number of produced chunks.
  - compare:align_by_similarity -- the same event production in the
    beautify mode's anchor / prefix-suffix pairing (present only
    when align_by_similarity produced unequal-count blocks):
    'calls' is the number of such blocks. One row per producer, so
    the report always shows WHICH pairing mode the time went to.
  - align_by_similarity:step1_exact_match_search /
    align_by_similarity:step2_prefix_suffix_search -- the two STEPS
    of the Align-by-similarity beautify, timed per invocation with
    perf_counter and booked as batched marks: step1 builds the
    unique-line index and scans for the longest unique exact match
    (the anchor), step2 (only when no anchor was found) does the
    O(N*M) prefix/suffix similarity scoring. The searches also run
    in the native collect pass, so 'calls' can exceed the
    compare:align_by_similarity block count.
  - absorb_trivial_equal_blocks -- the WHOLE Absorb pass as one
    section on the native paths (the collect pass and the
    fresh-split compare branch); its SELF is the pass minus the
    steps below (list copy, guards, fixpoint bookkeeping).
  - absorb_trivial_equal_blocks:step1_merge_ins_eq_del /
    absorb_trivial_equal_blocks:step2_absorb_short_equal -- the two
    STEPS of the Absorb pass, booked per invocation: step1 merges
    INSERT + EQUAL(trivial) + DELETE (or the mirror) into one
    REPLACE, step2 absorbs a short trivial EQUAL between large
    changed blocks. The Python engine books them from its
    background thread via thread-safe standalone marks (no umbrella
    row there -- the steps' sum IS the pass total).
  - refresh:wrapinfo_api -- the ed.get_wrapinfo() calls (one per
    editor, wrap on): the single most expensive editor API of a
    wrapped big-file refresh (~4.9s on 1M lines). Nests under
    refresh:wrap_counts; the Counter pass that turns the API's
    visual-row rows into per-line counts has its own
    refresh:wrapinfo_count row.
  - refresh:compare_and_paint -- the consumer loop: event dispatch,
    marker/bookmark/overview data collection (its self time is the
    honest per-event pipeline cost). compare:produce underneath it
    splits the event-list FETCHES (the differ's walk) from the
    consumer's dispatch.
  - paint:attr / paint:gap / paint:micromap / paint:wrap_calc --
    per-operation paint costs, as BATCHED marks: each gap
    insertion, wrap computation, char-run collection and micromap
    line paint is timed with a perf_counter pair, accumulated per
    category, and booked with one mark after the loop; 'calls' is
    the number of timed operations, 'max' the slowest single one.
    A row appears only when its sites actually ran (paint:micromap
    needs the micromap on, paint:wrap_calc needs wrapping on, and
    paint:gap only runs for insert/delete hunks and wrap-height
    mismatches -- a compare of two files with equal line counts and
    equal wrap counts has ~none, so 'paint:gap 0.0ms 1 call' is
    CORRECT, not a profiling bug).
  - refresh:*, paint:bookmark, paint:marker_window (with the
    paint:marks:build / paint:marks:apply split), paint:overview
    (with the paint:overview:build / paint:overview:draw split) --
    the other refresh phases and their sub-rows.
  The report header names what was compared (per side: the original
  file's path, or the tab title for untitled tabs). After the main
  table a 'Beautify passes' block prints each option-gated pass with
  its steps IN RUN ORDER (align_by_similarity: the
  compare:align_by_similarity umbrella + step1 + step2;
  absorb_trivial_equal_blocks: the umbrella + step1 + step2), so the
  step costs read as the sequence they run in instead of being
  scattered by the self-time sort; a pass whose rows are all absent
  did not run (its option is off, or nothing matched its patterns).
  A final block ESTIMATES the profiler's own overhead with ALL THREE cost
  components (start/stop sections + mark() bookings + the call-site
  perf_counter pairs, each with its micro-benchmarked per-op cost),
  so the observer effect is visible instead of hiding inside the
  rows (~0.4s clean on a 1M-line / 200k-difference compare; the old
  per-event-section profiler added seconds and misattributed them
  -- see history 2026.09.21 part 2).
  When the cProfile layer ran in the same compare, the section
  report prints a WARNING BANNER: while cProfile traces, every
  Python call pays ~1-2us, so rows with millions of cheap calls
  (paint:attr etc.) are inflated 2-3x in that run. The cProfile
  report attributes every Python function call on the main thread:
  use it to find the hot function, then turn the cProfile layer off
  and re-compare for clean section numbers. The tracing is always
  disabled before the section report prints, so the overhead
  estimate's per-op costs are measured clean.
  See the "Profiling and reporting performance problems" section
  below for the full description of both layers and the correct
  way to use them together.
  Use for debugging performance issues only.
  Default: off.
- differ2.advanced.enable_cprofile: Enable cProfile layer
  (function-level report)
  Adds the cProfile tracing profiler ON TOP of the section profiler
  (needs "Enable profiling" on). After each compare the console also
  gets the FUNCTION-level report -- which function/method eats the
  time, the question the section report cannot answer.
  cProfile traces every Python call (~1-2us each), so while it runs
  the compare is 2-3x slower and the section report's rows are
  inflated (the report prints a warning banner; the per-operation
  paint rows are skipped under this layer). This is a diagnostic to
  read, not a mode to keep on: find the hot function, then turn it
  off and re-run for clean numbers. See the "Profiling and reporting
  performance problems" section below.
  Default: off.

Micromap section:
- differ2.micromap.enable_micromap: Enable built-in micromap
  When enabled, switches on CudaText's native micromap (mini-map) column
  in both halves of the compare split, with diff-colored line
  highlights.
  The micromap is fast but does NOT account for the inter-line gaps
  Differ 2 inserts for visual alignment, so it may drift out of sync with
  the text when gaps are present -- for a gap-aware alternative, enable
  differ2.micromap.enable_overview instead (or both).
  Default: off.
- differ2.micromap.enable_overview: Enable gap-aware overview panel
  When enabled, adds a micromap alternative docked to the right side of
  the compare view: a miniature of both editors side-by-side with
  colored rectangles for deleted (red), added (green) and changed
  (yellow) lines, gray rectangles for gaps and white for unchanged
  lines, laid out like a usual scrollbar: a one-line-scroll button
  (arrow) at the top and bottom, the slider track between them, and a
  grey separator line at the panel's left edge (also under the buttons)
  separating it from the editor.
  Unlike the built-in micromap, the overview accounts for the inter-line
  gaps inserted for visual alignment, so it stays in sync with what you
  actually see. The painting uses the WinMerge "Location Pane"
  approach: only the diff segments are drawn, coalesced into runs, and
  any segment that would collapse onto already-painted pixels is
  skipped -- so even a 1M-line file with 200k differences paints at
  most a few hundred rectangles.
  The panel is created BEFORE the compared texts are loaded (docking it
  changes the editors' width -- doing that after the load would
  re-wrap the whole text), and shows the default background until the
  compare finishes; the colored map is filled in then. Recompare also
  creates the panel when it is missing.
  The heavy part of the drawing runs on a background thread, with every
  CudaText API call kept on the main thread -- so a resize or a
  finished compare never freezes the editor while the overview is being
  redrawn; the panel keeps its previous picture until the fresh one is
  ready. Resizes are additionally nearly instant: the size-independent
  part of the computation (prefix sums + the coalesced diff runs, in
  visual-row space) is CACHED and reused across resizes -- it is rebuilt
  only when the compare data, the wrap counts or the colors change --
  and the size-dependent pixel mapping uses a monotone jump search over
  the cached arrays instead of walking every segment, so even a
  million-line compare's overview remaps to a new panel size in about a
  millisecond (the pixel output is identical to the full walk's).
  The slider works like the scrollbar of usual editors and browsers:
  its travel range (the track minus the thumb) maps onto the scrollable
  range, so dragging the slider to the very bottom of the track scrolls
  the text to the very end of the files, and dragging it to the top
  scrolls to the beginning. Its height is proportional to the visible
  part of the text (like a real scrollbar thumb, with a 30px grabbable
  minimum on big files), but it never takes more than 1/7 of the
  overview's height, so it can never dominate the panel on small
  files.
  Default: on.
- differ2.micromap.hide_builtin_scrollbars: Hide built-in scrollbars in
  compare tabs
  When enabled (and the overview panel is on), the vertical scrollbar of
  both compare editors is hidden on every compare start -- the
  overview's own slider (drag it to scroll, click the track to jump,
  hold the arrow buttons to auto-scroll) replaces it, like in WinMerge.
  When the overview panel is disabled, the built-in scrollbars are
  never hidden: without them there would be no way to scroll with the
  mouse.
  Only has an effect when differ2.micromap.enable_overview is on.
  Default: on.

Toolbar section (see the "Toolbar" chapter above for details):
- differ2.toolbar.show_toolbar: Show the compare-tab toolbar (default: on)
  A toolbar docked to the top of every compare tab: Recompare (Cancel
  while a compare runs), Prev, Next, Copy to left, Copy to right, the
  Ignore-options dropdown (multiple checkable options plus
  "Uncheck all"), the Presets dropdown (algorithm / beautify-alignment
  preset combinations), Swap, Resize, the View dropdown (bars / gutters /
  overview visibility), Config, and a status label on the right
  (compare state + difference count). Every button has a tooltip; the
  toolbar follows UI theme switches and is restored at startup for
  session-restored compare tabs.
- differ2.toolbar.show_btn_text: Show button texts in the toolbar
  (default: on)
  When on, buttons show "↻ Recompare", "↑ Prev", "↓ Next", "← Copy",
  "→ Copy", "≡ Ignore 2/5 ▾", "★ Preset ▾", "⇋ Swap", "↔ Resize",
  "▦ View ▾", "⚙ Config"; when off, only the UTF-8 icons are shown (the Ignore
  button keeps its enabled-options counter).

Folders section (see the "Compare folders" chapter above for details):
- differ2.dirs.quick_only: Fast (sampling) folder compare (default:
  off)
  Applies to same-sized files above 128 KB in the "Contents" method.
  When off, every pair is compared to the end, so "Identical" is
  always proven byte for byte. When on, only each side's first and
  last 64 KB windows are compared and matching samples are reported
  as Identical -- the fastest mode for huge trees, at the cost that a
  change confined to the middle of a big file can be missed in the
  LIST; the double-click side-by-side compare never misses anything.
- differ2.dirs.confirm_ops: Confirm copy and delete in the folder
  window (default: on)
  Ask before the folder window's context menu overwrites or deletes
  anything (Copy to left/right, Delete from left/right).
- differ2.dirs.compare_method: Folder compare method (default:
  Contents)
  "Contents" is WinMerge's quick contents (32 KB chunk compare,
  stops at the first difference, proven identical, no hashing);
  "Size and timestamp" (WinMerge's "Modified date and size" method)
  decides by size+mtime alone and never opens a file -- the method
  for slow file sources (network shares, cloud-sync placeholders,
  antivirus-hooked opens).
  See the "Compare folders" chapter for the full trade-offs.
- differ2.dirs.scan_threading: Folder scan threading (default:
  main thread)
  Which threads run the folder scan. "main" (the default) runs the
  whole scan synchronously on the UI thread: the window freezes
  for the scan's duration (no repaint, no cancel, rows appear at
  the end). For a normal tree that freeze is a fraction of a
  second -- and on boxes where background threads starve for the
  interpreter lock while the UI is busy it is the only fast shape
  at all: the measured case (Windows 7, no antivirus, GIL convoy
  confirmed by profiling) did the same folders in 8.1 ms on the
  main thread vs 12.6 s on the scanner threads -- every background
  listing waited ~200 ms to re-acquire the GIL (zero CPU burned),
  while the main thread paid 0.1 ms per listing. "parallel" is the
  engine for very big trees: a scanner thread plus a small pool
  walking both trees concurrently, rows streaming into a live,
  cancelable window -- switch to it when a walk takes so long that
  a frozen window is worse than a slower walk. "serial" walks on
  the scanner thread alone (the shape the cProfile layer forces).
  The option is reloaded on every Refresh; the profiling report's
  "scan mode:" line names the shape.


== Diff algorithms and best practices ==

Differ 2 implements all the best-known diff algorithms, with a lot of
improvements on top of each. Seven algorithms are available: two native
ones that run in compiled Pascal code inside CudaText -- Native Histogram
(a port of JGit's Histogram Diff, the algorithm behind "git diff
--histogram") and Native Myers (a port of WinMerge's bundled GNU
diffutils Myers, the algorithm behind "git diff --myers") -- which are
10-30x faster than any pure-Python implementation on large files, and
five pure-Python ones (Hybrid, Myers, VS Code, Patience, difflib). The
complete description of every algorithm is in the
differ2.algorithm.diff_algorithm option.

Support policy: only the two NATIVE algorithms are supported and
maintained -- only they will receive improvements over time, if God
wills. The five pure-Python algorithms are NOT supported and NOT
maintained, except for bug fixes: they will not receive speed or
readability enhancements. They are kept just as EXPERIMENTAL reference
implementations, to track the divergence of the native algorithms over
time. Any issue opened to speed them up or to improve their output
readability will not be accepted -- only bug reports are welcome.

Two option combinations cover the two extreme needs:

Fastest compare (very big files, minimum CPU and memory) -- this is
also the default configuration except for the two detail options:
- Set differ2.algorithm.diff_algorithm to "native_myers" (the default).
- Disable differ2.algorithm.compare_with_details (no
  character-by-character comparison inside changed lines).
- Disable differ2.algorithm.beautify.align_by_similarity (off by
  default: no similarity-based re-pairing of changed blocks).
- Disable differ2.algorithm.beautify.absorb_trivial_equal_blocks (off
  by default: the engine's raw opcodes are rendered as produced).
- Disable differ2.micromap.enable_overview (no overview panel painting).
This combination gives the fastest compare and the smallest memory
footprint. The output is rendered exactly the way GNU diffutils /
WinMerge side-by-side (sdiff) output does.

Best human-readable compare:
- Set differ2.algorithm.diff_algorithm to "native_histogram".
- Enable differ2.algorithm.compare_with_details (highlights the exact
  changed characters inside each modified line).
- Enable differ2.algorithm.beautify.align_by_similarity (re-pairs
  similar lines inside changed blocks so they appear aligned, like VS
  Code does).
- Enable differ2.algorithm.beautify.absorb_trivial_equal_blocks
  (merges hunks that the engine split on a matched blank/brace line,
  so one change reads as one change).
This combination produces the most readable side-by-side compare, with
better results than WinMerge, VS Code, Meld and Beyond Compare.

If a diff is hard to read, try a different algorithm: Histogram, Hybrid,
Patience or VSCode tend to generate much cleaner results. If you compare
massive files and need maximum speed, use Native Myers instead.

Big files and responsiveness: the whole compare is non-blocking. The
diff engine, the char-level details and the overview rebuild all run on
background threads, and every longer main-thread stretch (collecting
the changed line pairs, applying the diff colors, the bookmark pass) is
split into chunks that hand the message queue back to the application
about 20 times a second -- so CudaText keeps repainting and accepting
input (menus, other tabs, window dragging) WHILE a big compare is being
colored, instead of freezing until it finishes. Both compare-tab editors
show the 'busy' placeholder and stay read-only for the whole run, the
status bar shows a one-shot 'applying diff colors...' message when the
paint phase starts (no running timer -- the toolbar's status label
carries the live state), and the "Differ 2\Cancel compare" command
works at any moment: a compare cancelled mid-paint stops applying
colors at the next chunk instead of running to the end.

Myers vs. Histogram Differences:
native_histogram generally produces more "human-readable" and semantically meaningful alignments. By anchoring the comparison on unique or low-frequency lines first, it keeps moved, refactored, or reordered code blocks intact rather than scrambling them with spurious matches on common elements (like braces or blank lines). native_myers simply looks for the shortest possible edit path without semantic context. For normal files, the speed difference between the two is negligible. However, native_myers is noticeably faster when comparing massive files with extreme differences, thanks to its early-exit heuristics.

== Overview panel and micromap ==

Differ 2 can show one or both of two mini-map styles next to a compare tab:
the plugin's own gap-aware overview panel (default: on) and the built-in
CudaText micromap (default: off). They can be enabled independently and
used at the same time. The overview is the recommended default because
it stays gap-aware; the micromap is faster and cheap but does not account
for inter-line gaps.

The overview panel is laid out like a usual scrollbar:
- The top and bottom rows are one-line scroll buttons: solid arrow
  triangles in the theme's ScrollArrow color, drawn on the overview's
  own background color so the buttons blend into the panel (no
  borders). The arrows are fixed-size geometric triangles (9x7 px in
  the 16px button box) that always fit inside their button rectangle.
  A single click scrolls one line; holding the button pressed starts
  auto-repeating the scroll (like holding a scrollbar's arrow button).
- Between the buttons is the track with the miniature maps of both
  files. The slider (viewport indicator) lives there: drag it to scroll
  (the thumb is glued to the mouse on every raw move, like a real
  scrollbar, while the text repaints asynchronously at whatever rate
  the editors can paint -- see "Overview-driven scrolling" below),
  click the track to jump the viewport there, and use the mouse wheel /
  keyboard as usual in the editors.
  The slider starts from the same theme colors the editor's own
  scrollbars use for their thumb (fill = ScrollFill, border =
  ScrollRect, the 3 grip lines = ScrollRect) -- and because those
  colors are designed against the scrollbar TRACK, not the editor
  background the overview uses, each color is then lightness-adjusted
  just enough to stay clearly visible: in some themes ScrollFill
  equaled the overview background (invisible slider) or ScrollRect
  equaled ScrollFill (invisible grip lines). Themes whose colors
  already contrast well keep them EXACTLY as the theme defines them;
  the adjustment only kicks in for colliding themes, so the slider
  stays visible on black, white and grey theme families alike. On
  LIGHT themes the fill is lifted into a LIGHT band just under the
  background luminance (the modern native-scrollbar thumb look: the
  theme's own ScrollFill is usually a mid grey designed against the
  scrollbar track, and left as-is it reads as "a little bit darker"
  on white overviews), with the thin border and grips providing the
  thumb's definition; dark themes keep the theme's own look and the
  stronger contrast.
- A grey vertical separator line runs along the panel's left edge,
  separating the overview from the editor (and the editor's scrollbar,
  when visible) -- the buttons' boxes sit right of the same line.

While the overview is enabled, the option
"differ2.micromap.hide_builtin_scrollbars" (default: on) hides the
editors' built-in vertical scrollbars: the overview's slider fully
replaces them. With the overview disabled the built-in scrollbars are
never hidden -- without them there would be no way to scroll with the
mouse.

Big files are handled the WinMerge "Location Pane" way: the panel is
only a few hundred pixels tall, so painting every changed line of a
1M-line file would just keep overwriting the same pixel rows (you
cannot paint half a pixel). The overview coalesces consecutive
same-colored lines into runs, converts each run/gap to pixels once, and
skips every segment that would collapse onto already-painted pixels --
the number of actual draw calls is bounded by the panel's pixel height,
not by the file's size or diff count.

The diff MARKERS in the editors are windowed for the same big-file
reason: a 1M-line compare can collect 100-200k markers, and CudaText
walks ALL of an editor's markers on every repaint of its micromap
column, so a fully-marked million-line compare paid 100-200 ms per
paint -- the slider and the text lagging behind the mouse, the ▲/▼
buttons advancing one line only every 100-200 ms (deleting the markers
with ed.attr(MARKERS_DELETE_ALL) made scrolling instant, which pinned
the cost on the marker volume). Differ 2 therefore COLLECTS the markers
during the compare and applies only the window around the current
viewport (+/- 1500 lines) in batched marker calls (one editor update
per batch instead of one per marker -- this also removed the
multi-second marker phase of big compares). When scrolling leaves the
window, it is re-applied around the new position (a few ms; at most a
few thousand markers). Scrolling big files is now as instant as on
small files, exactly like the editors' own scrollbars. After an edit
(which shifts line numbers) the markers already in the editor stay as
they are until the next compare rebuilds them.

Overview-driven scrolling (dragging the slider, holding the ▲/▼
buttons, clicking the track) follows the native scrollbar architecture
exactly, so the thumb is as responsive on a 1M-line file as on a small
one:
- The thumb and the text are DECOUPLED, like in every editor: the
  slider bitmap is repainted at the mouse-derived position on EVERY
  raw mouse move and pushed to the screen through a message-queue
  pump -- but only while the editors have no pending paints. (Pumping
  right after invalidating the editors would deliver both editors'
  full viewport paints synchronously INSIDE the mouse handler; on
  million-line compares each paint walks the inter-line alignment gaps
  several times and costs 50-100 ms -- that was the "slider waits
  100-200 ms to follow the mouse" bug.)
- After writing the scroll position, each editor is invalidated
  ASYNCHRONOUSLY (ed.cmd(cmd_RepaintEditor) -- the exact call the
  editor's own scrollbar path makes) and the handler RETURNS without
  pumping: the editors' paints are delivered by the natural
  message-loop drain between mouse handlers, never synchronously
  inside a handler. A bare position write does not repaint the editor
  at all, and with the built-in scrollbars hidden the text would only
  move when some unrelated repaint happened to arrive. No forced
  SYNCHRONOUS full repaints are ever issued on these paths (a full
  repaint of a huge compare view costs 100+ ms).
- The position writes are paced so no backlog can build: at most every
  30 ms AND only after the editors have PAINTED the previous write
  (CudaText fires on_scroll at the end of every editor viewport paint,
  which re-opens the gate; a 250 ms safety timeout covers lost
  notifications). Every scroll write and every editor repaint walks
  the compare's inter-line alignment gaps internally (O(gaps) -- tens
  of thousands of items on million-line compares), so an unpaced
  stream of writes buried the message queue and made the slider lag
  hundreds of milliseconds behind. Each apply always consumes the
  NEWEST mouse position; a deferred one-shot timer applies it when the
  mouse stops, and the release always lands exactly on the final
  cursor position -- the same behavior as native scrollbars. The ▲/▼
  buttons write one line per repeat tick (like native arrow buttons)
  so the text glides at the editors' paint rate instead of crawling
  one line per paint.


== Profiling and reporting performance problems ==

The plugin ships TWO profilers, and they answer two DIFFERENT questions.
Neither can answer the other's question, which is why a performance
report needs both.

The SECTION profiler (differ2.advanced.enable_profiling)
  The plugin's own lightweight hierarchical profiler. It instruments
  PHASES: one timing row per pipeline stage (text fetch, wrap info,
  line split, engine calls, event production, dispatch, bookmark flush,
  overview build, ...), plus batched per-operation rows for the hot
  loops. Its cost is small (a fraction of a second on a 1M-line
  compare; the report prints an honest estimated-overhead line) and it
  runs while the compare behaves normally.
  It answers: WHICH PHASE eats the time? "positional_pairs is 5.8s of
  the 39.7s refresh" -- a phase-level attribution, with self/total
  separation so wrapper rows never mask their children.

The cProfile LAYER (differ2.advanced.enable_cprofile)
  Python's standard tracing profiler, wrapped around one whole refresh.
  It records EVERY Python function call on the main thread, so it can
  attribute time to individual FUNCTIONS and methods -- the question
  the section report cannot resolve (what inside the dispatch loop?
  dict.get? list.append? the generator's next? a specific helper?).
  Its cost is large: every traced call pays ~1-2us, and the compare's
  hot loops make millions of calls, so a compare under cProfile runs
  2-3x slower, and the section report printed in the same run carries
  INFLATED numbers (it warns you with a banner). The per-operation
  paint rows are skipped under this layer for the same reason.

The two layers interact, and that is exactly why both are needed:
  - The section report from the cProfile-OFF run is the only one with
    clean, comparable phase numbers.
  - The cProfile report from the cProfile-ON run is the only one that
    names functions.
  - Reading only one of them misleads: the inflated section report
    points at the wrong phase magnitudes; the cProfile report alone
    cannot tell which PHASE a hot function belongs to (its rows are
    flat, not nested by pipeline stage) and its numbers include the
    tracing overhead itself.

The correct way to profile:
  1. Open "Options / Settings-plugins / Differ 2 / Config" (or edit
     settings/cuda_differ2.json directly; changes apply from the next
     compare, no restart needed).
  2. Turn ON differ2.advanced.enable_profiling, leave
     differ2.advanced.enable_cprofile OFF. Recompare. Copy the whole
     console output (the section report).
  3. Turn ON differ2.advanced.enable_cprofile too. Recompare the SAME
     files. Copy the whole console output (the banner-carrying section
     report + the function-level cProfile report).
  4. Turn both options OFF when done -- they are diagnostics, not a
     mode to leave on.

Reporting a performance problem:
  Post BOTH console reports (step 2's and step 3's) together with the
  compared files' sizes and a description of what felt slow. Both are
  needed because the maintainers must see the clean phase split (to
  know which stage to attack) AND the function-level attribution (to
  know what inside that stage to attack); each report without the
  other sends half the coordinates. Include the "Estimated profiler
  overhead" line -- it tells whether a small run's numbers are mostly
  instrumentation, and the "Differ 2: compare took ...ms" status line
  from an UNPROFILED run if you have it (the closest number to what
  you actually experience).


== Notes ==

- Untitled tabs can be compared just like saved files. Your edits in the
  compare view are synced back to the original untitled tab when you save.
- The compare view uses CudaText's built-in split-editor feature -- no
  temporary files are created on disk.
- If both files become identical after editing, all markers are cleared
  and a message is shown when refreshing the compare.


== Authors ==

  Badr Elmers, https://github.com/badrelmers
  Forked from Differ by OlehL (https://github.com/CudaText-addons/cuda_differ)
  and rewritten from scratch.

License: MIT
