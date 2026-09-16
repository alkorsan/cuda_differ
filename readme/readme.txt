Differ plugin for CudaText

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
  navigate. The mirror runs inside the scroll event itself, skips
  redundant writes and never re-enters, and repaints the mirrored half
  SYNCHRONOUSLY (Ed.Repaint) inside that same event -- so the two halves
  move in the same display frame no matter which half initiated the
  scroll: left scrollbar, right scrollbar, mouse wheel over either half,
  or the overview slider. Scrolling from the overview panel writes both
  halves and paints them back-to-back inside the one mouse-event
  callback, so they move atomically.
- Supports word-wrap: you can turn wrap on in a compare tab and the two
  sides stay visually aligned even when corresponding lines wrap to
  different heights. Toggling wrap mode on one half turns it on in BOTH
  halves at once (same for turning it off), and the compare is
  re-run automatically afterwards so the alignment gaps are re-sized
  for the new wrap mode.
- Optional gap-aware overview panel docked to the right of the compare
  view, showing a miniature of both editors side-by-side with colored
  diff highlights (WinMerge-style). Its slider behaves like a normal
  scrollbar: drag it and the thumb tracks the mouse live (~33 fps,
  wall-clock throttled so the CPU cost stays negligible); click the
  track and the view jumps to that position.
- Jump to next/previous difference always lands the caret (and moves
  the focus) on the side that HAS text: one-sided differences (added
  or deleted lines, shown as a gap on the other side) put the caret on
  the changed lines, never on the empty gap side, so the copy commands
  keep working right after a jump -- from the changed lines or from
  the line next to a gap.
- Adds two narrow "hunk edge" columns, one at the left edge of each
  editor of the compare view, each drawing a bracket around every
  difference block -- text lines AND compensating gap band -- so you
  always see exactly which lines a hunk covers (and which lines
  Alt+Left/Alt+Right will move), like the rule lines Beyond Compare
  draws around its difference blocks. See "Hunk edge columns" below.

The compare engine implements all the best-known diff algorithms, with a
lot of improvements on top of each: two native ones running in compiled
Pascal code inside CudaText (JGit's Histogram Diff and WinMerge/GNU
diffutils' Myers, 10-30x faster than any pure-Python implementation on
large files, comparing on a background thread so the editor never blocks,
with cooperative cancellation when a compare is no longer needed) and
five pure-Python ones (Hybrid, Myers O(NP), VS Code, Patience and
difflib; these compare on the main thread). Combined with character-level
highlighting of the exact changed characters and smart line alignment,
this gives the best human-readable compare results -- better than WinMerge,
VS Code, Meld and Beyond Compare -- while comparing faster than VS Code and
Meld. See the "Diff algorithms and best practices" section below for how
to tune the plugin for maximum speed or maximum readability.


== Commands ==

All commands are in the "Plugins / Differ" menu.

Compare current document with file...
    Compares the file in the active tab with another file you pick from
    a dialog.

Compare current document with tab...
    Compares the file in the active tab with another open tab.

Diff current document with file...
    Produces a unified diff (patch-style) of the active file and a file
    you pick. The result opens in a new read-only tab.

Diff current document with tab...
    Same as above, but compares with another open tab.

Note: the unified-diff output is always produced using Python's
  stdlib difflib.unified_diff (the standard library implementation,
  with its default autojunk=True), not the algorithm chosen via
  differ.algorithm.diff_algorithm. Unified diff is a machine-readable
  patch format normally consumed by tools (patch, git apply, CI,
  code-review bots, etc.) rather than read end-to-end by humans;
  the chosen algorithm only affects how lines are paired inside
  REPLACE blocks for the side-by-side view, and can be
  slower than difflib on large files. Applying it to the
  unified-diff path would be wasted work for no user-visible
  benefit. If you want the chosen algorithm's alignment in a
  human-readable form, use the side-by-side compare instead.

Recompare
    Re-runs the comparison after you edit either side (formerly called
    "Refresh"). With the native algorithms the compare runs on a
    background thread, so the command returns at once and the markers
    are re-applied when the compare finishes. Useful if
    differ.advanced.enable_auto_refresh is off.

Focus the opposite file
    Moves the cursor to the other side of the split.

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
"differ.advanced.enable_keyboard_capture" (Default: on). The plugin
subscribes/unsubscribes to the key events at runtime, so toggling the
option takes effect at once, without a restart.


== Gaps and one-sided differences ==

Lines that exist on one side only (pure additions or deletions) are
shown as a colored inter-line GAP on the other side. A gap is pure
visual space painted between two lines -- it is not a line, so nothing
is ever inserted into your text to keep the sides aligned, and the
caret cannot be placed inside a gap: CudaText editors do not model
carets in the space between lines (that would need changes in the
ATSynEdit editor control itself, which a plugin cannot ship).

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

To add a line where a gap is (WinMerge lets you type inside its gaps):
put the caret on the line above or below the gap, press Enter and type
-- the new line takes the gap's place as soon as the compare is re-run
(F5, or automatically with differ.advanced.enable_auto_refresh on),
and the remaining gap shrinks by one line.


== Hunk edge columns ==

Two narrow columns are added at the left and right edges of the
compare view, and every difference block (hunk) gets a bracket drawn
in each column:

    +----
    |
    +----

The bracket spans the hunk's full visual extent -- the text lines AND
the compensating gap band the engine inserts inside the hunk. This
answers "where does this hunk start and end?" the way Beyond Compare
does with the rule lines in its center column: before you move a block
with Alt+Left/Alt+Right you can see exactly which rows will be moved,
even inside a large changed block where the margin markers alone are
hard to follow. A one-sided difference (added or deleted lines, shown
as a colored gap on the other side) gets its bracket around the gap
band too, so the extent is visible on both sides.

Technical notes:
- One column sits at the LEFT edge of each editor: the left column
  before the left editor's gutter, the right column directly right of
  the splitter, before the right editor's gutter. The columns are child
  controls of the grouping panel that parents the two editors, attached
  through the Lazarus align system itself: the left column is
  Align=alLeft (it takes the panel's left-edge strip and the left
  editor, which is Align=alClient, shrinks around it automatically) and
  the right column is Align=alRight, seeded so the align pass sorts it
  between the splitter and the right editor (it stays glued to the
  right editor's left edge). The editors are never modified: no
  coordinates are set on them, so nothing can cover the columns and
  there is nothing to restore when they are removed. The text area is
  not touched at all -- unlike the earlier implementation (thin colored
  inter-line gaps inside the editors), nothing is added to the editors'
  heights, so the side-by-side alignment and the synchronized scrolling
  cannot be affected.
- A background layout guard (a light per-tab timer) re-seeds the right
  column after splitter drags or window resizes that moved the right
  editor's left edge past the column (there the align sort alone drops
  the column to the left of the splitter), repaints after size changes,
  and removes the columns when the split is gone (tab un-split, split
  switched to horizontal, tab closed); deleting the two controls lets
  the align system give the editors their full widths back on its own.
- The brackets are pixel-aligned with the text rows: their tops and
  bottoms come from the editor's own line-to-pixel conversion, which
  accounts for inter-line gaps, word wrap and the current scroll
  position. On scroll the columns follow live (a throttled immediate
  repaint plus a final repaint after scrolling stops).
- "Improve line alignment" (beautify) is fully supported: when the
  beautified pairing pushes a hunk's compensating gap band ABOVE the
  shorter side's first hunk line, the bracket top comes from the other
  side's first line -- so the bracket brackets the band and both
  columns stay level. A previous hunk's trailing band sitting at the
  same line index is told apart from this hunk's own band (bands are
  matched by the line ranges they compensate), so it never shifts the
  bracket. Hunks that end at the end of the file on both sides include
  their trailing bands too (the plugin records the band pixel sizes
  the engine asked for and adds them to the EOF bottom edge).
- Ignored differences (suppressed by the ignore options) are not in
  the diff records, so they never get brackets.
- Only hunks intersecting the visible line range are painted (a
  background fill plus 3 canvas lines each), so the cost stays small
  even for huge files with thousands of differences.
- Colors come from the ACTIVE THEME -- the background uses EdGutterBg
  and the bracket lines use EdGutterFont (read from the theme dicts;
  the columns look like part of the editors' gutters in every theme).
  No plugin color option is involved.
- The columns are re-drawn on every compare (F5 / auto-refresh) and
  destroyed together with the whole per-tab session when the compare
  tab is closed or the feature is turned off.
- Turn the feature off with differ.advanced.enable_hunk_edges; the
  column width is differ.advanced.hunk_edges_width (each editor gives
  up that many pixels to its column). Changes take effect on the next
  Recompare (F5).


== Tab context menu ==

Right-clicking a tab title shows "Differ" submenu with:
- Compare with... -- pick a file to compare against this tab
- Compare with focused tab -- compare this tab with the currently focused tab
- Compare with tab -- submenu listing all open tabs; click one to compare.
  If the list is too long, the first entry "More tabs..." opens a dialog
  with a scrollbar to pick any open tab.
- Recompare -- re-run the compare on both sides of the compare tab.
- Five checkable "ignore" options (below Recompare, after a separator):
  Ignore case, Ignore whitespace, Ignore blank lines, Ignore line endings,
  Ignore numbers. Shown only while a native algorithm is the effective
  one (they do nothing for the pure-Python algorithms, which always
  compare strictly); the items appear/disappear on the next right-click
  after the algorithm is changed in the config dialog.
  Ticking one re-runs the compare immediately with that option applied;
  the checkmarks always mirror the saved settings, so the config dialog
  and this menu stay in sync in both directions (toggling here writes the
  setting to settings/cuda_differ.json, and changing a setting in the
  config dialog is reflected here the next time the menu opens). See
  "Ignore options" below for what each option does.

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
(see above) or from the config dialog ("differ.ignoreopt.*" options in
settings/cuda_differ.json) while a native algorithm is active.

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
  in blank lines and 'Ignore blank lines' is on), Differ
  tells you: a "No differences found (with current ignore options)"
  message appears instead of an uncolored compare tab.
- These options are passed to CudaText's diff_proc API as the
  DIFF_IGN_* bitmask (see the diff_proc documentation in the CudaText
  wiki).


== Background comparing ==

With the native algorithms (Native Histogram, Native Myers), the
line-level diff runs on a background thread inside CudaText, started
through the callback form of the diff_proc API. The editor never blocks
while two files are being compared: you can keep typing, scrolling,
switching tabs and using menus during the compare, no matter how big the
files are.

- The compare markers are applied when the compare finishes. While it
  runs, the status bar shows "Differ: comparing in background...", and
  the usual "Differ: compared in Xms" message reports the total time
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
  compare on the main thread and occupy the UI for the compare time;
  Python plugin code cannot move to a background thread. This is one
  more reason to prefer the native algorithms for big files.


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


== Compare tabs and restarts ==

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

When you close a compare tab manually (not via app exit):
- If you have unsaved changes, CudaText asks whether to save or discard.
- If you save, your changes are synced to the original files.
- If you discard, the originals keep their last-saved content.
- If you cancel, nothing happens -- the compare tab stays open and
  fully functional.


== Command-line support ==

You can start a comparison from the command line:

    cudatext -p=cuda_differ#filename1#filename2

This launches CudaText with the two given files opened in the Differ plugin.


== Options ==

Open the options dialog via "Options / Settings-plugins / Differ / Config"
or "Plugins / Differ / Config...".

All options are stored in settings/cuda_differ.json. The option names grouped into five categories: theme, algorithm, ignoreopt, advanced, micromap.

Ignore options section (see the "Ignore options" chapter above for details):
- differ.ignoreopt.ignore_case: Ignore case (default: off)
- differ.ignoreopt.ignore_whitespace: Ignore whitespace -- spaces and tabs (default: off)
- differ.ignoreopt.ignore_blank_lines: Ignore blank lines -- all-blank hunks
  suppressed, painted as ignored regions with a compensating gap (default: off)
- differ.ignoreopt.ignore_eol: Ignore line endings -- CR/LF/CRLF (default: off)
- differ.ignoreopt.ignore_numbers: Ignore numbers -- digits 0-9 (default: off)
  These five options build the DIFF_IGN_* bitmask passed to the native
  diff engines. They only affect the native algorithms (Native Histogram
  and Native Myers); the pure-Python algorithms always compare strictly.

Theme section:
- differ.theme.color_theme: Color theme (default: auto)
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
      (The hunk edge columns do not use these options -- their colors
      come from the active theme's EdGutterBg/EdGutterFont.)
  In the grey and black presets the ignored-difference colors resolve to
  the live editor background, so ignored regions blend into the active
  theme. The six color options below only apply in the "custom" mode.
- differ.theme.changed_color: Color of changed lines
  Background color for lines that were modified (replaced with different
  content). Also colors the char-level highlights inside modified lines,
  the margin markers, the micromap highlights and the overview panel.
  Only used when "Color theme" is custom; leave empty to fill this slot
  from the auto-detected preset.
- differ.theme.added_color: Color of added lines
  Background color for lines that exist only in the right file (added).
  Also colors the char-level highlights inside added lines, the margin
  markers, the micromap highlights and the overview panel.
  Only used when "Color theme" is custom; leave empty to fill this slot
  from the auto-detected preset.
- differ.theme.deleted_color: Color of deleted lines
  Background color for lines that exist only in the left file (removed).
  Also colors the char-level highlights inside deleted lines, the margin
  markers, the micromap highlights and the overview panel.
  Only used when "Color theme" is custom; leave empty to fill this slot
  from the auto-detected preset.
- differ.theme.gap_color: Color of inter-line gap background
  Background color for the blank gap inserted to keep the two sides
  aligned when one side has fewer lines. Also colors the gap rectangles
  in the overview panel.
  Only used when "Color theme" is custom; leave empty to fill this slot
  from the auto-detected preset.
- differ.theme.ignored_color: Color of ignored differences
  Background color for lines whose difference is suppressed by the
  "Ignore blank lines" option (WinMerge-style ignored differences).
  Also colors the micromap highlights and the overview panel.
  Only used when "Color theme" is custom; leave empty to fill this slot
  from the auto-detected preset (the editor text background for the grey
  and black families, so the ignored region then looks like normal text).
- differ.theme.ignored_gap_color: Color of ignored difference gaps
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
- differ.algorithm.diff_algorithm: Diff algorithm
  Selects the diff algorithm used by the side-by-side compare.
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
      This is the default and recommended option for regular files.
    * native_myers -- Native Myers diff (port of WinMerge's bundled GNU
      diffutils Myers O(ND), the same algorithm git uses for "git diff
      --myers"), the fastest on large/very different files. It is faster
      because it builds on top of Myers with additions from diffutils and
      WinMerge that JGit lacks, such as Paul Eggert's TOO_EXPENSIVE
      heuristic, line-purging heuristics like DiscardConfusingLines, and
      other optimizations.
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
      autojunk=False.
  If some parts of a diff are hard to read, try testing a different
  algorithm: Histogram, Hybrid, Patience, or VSCode tend to generate much
  cleaner results.
  If you're comparing massive files and need maximum speed, stick with
  the Native Myers algorithm instead.
  Default: native_histogram.
- differ.algorithm.compare_with_details: Detailed comparison
  When enabled, modified lines are compared character-by-character,
  highlighting specific differences within the line. This uses a ported
  implementation of WinMerge’s character-diff engine, combining Myers O(NP)
  with custom heuristics and optimizations.
  When disabled, modified lines are highlighted as a whole.
  Disabling this speeds up the compare of big files.
  Default: on.
- differ.algorithm.beautify_alignment: Improve line alignment
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
  Default: on.

Advanced section:
- differ.advanced.sync_scroll: Synchronized scrolling
  When enabled, scrolling one side of the compare view also scrolls the
  other side, both vertically and horizontally.
  Default: on.
- differ.advanced.enable_sync_caret: Keep carets visible on sync
  When enabled, moving the cursor in one side also moves the cursor in
  the other side to the corresponding difference block, so both carets
  stay visible in the current screen area.
  Default: off.
- differ.advanced.enable_auto_refresh: Auto-refresh after changes
  When enabled, the diff markers are automatically re-calculated after
  you stop editing for 1-2 seconds. When disabled, you must use the
  Recompare command manually.
  Default: off.
- differ.advanced.enable_hunk_edges: Hunk edge columns
  When enabled, two narrow columns are added at the left and right
  edges of the compare view, each drawing a bracket around every
  difference block -- the text lines AND the compensating gap band
  inside the hunk -- so you always see exactly what Alt+Left/
  Alt+Right will move, like the rule lines Beyond Compare draws around
  its difference blocks (see "Hunk edge columns" above). The columns
  are drawn OUTSIDE the editors (the text area and the alignment are
  not touched), use the theme's gutter colors (EdGutterBg /
  EdGutterFont), stay pixel-aligned with the text rows while scrolling
  (also with word wrap and "Improve line alignment"), and only visible
  hunks are painted, so the cost stays small on huge files.
  Takes effect on the next Recompare (F5).
  Default: on.
- differ.advanced.hunk_edges_width: Hunk edge column width in pixels
  Width of one hunk edge column, in pixels. The columns are
  intentionally narrow -- a vertical bar plus short top/bottom arms.
  Range: 6-40. Default: 12.
- differ.advanced.diff_context: Context lines in unified diff
  Number of unchanged context lines shown around each change in the
  unified diff output (produced by the "Diff current document with..."
  commands).
  Default: 3.
- differ.advanced.enable_profiling: Enable profiling
  Enable profiling to trace where compare time is consumed.
  When enabled, prints a detailed timing report to the console after
  each compare, breaking down time spent in the diff algorithm, opcode
  realignment, event generation, char-level diffing (native vs Python),
  and UI painting (bookmarks, decor, gaps, attributes). The report
  header also names what was compared: per side the original file's
  path, or -- for untitled tabs -- the original tab's title. Use for
  debugging performance issues only -- adds small overhead (~1-2us per
  timing point).
  Default: off.

Micromap section:
- differ.micromap.enable_micromap: Enable built-in micromap
  When enabled, switches on CudaText's native micromap (mini-map) column
  in both halves of the compare split, with diff-colored line
  highlights.
  The micromap is fast but does NOT account for the inter-line gaps
  Differ inserts for visual alignment, so it may drift out of sync with
  the text when gaps are present -- for a gap-aware alternative, enable
  differ.micromap.enable_overview instead (or both).
  Default: off.
- differ.micromap.enable_overview: Enable gap-aware overview panel
  When enabled, adds a micromap alternative docked to the right side of
  the compare view: a miniature of both editors side-by-side with
  colored rectangles for deleted (red), added (green) and changed
  (yellow) lines, gray rectangles for gaps and white for unchanged
  lines.
  Unlike the built-in micromap, the overview accounts for the inter-line
  gaps inserted for visual alignment, so it stays in sync with what you
  actually see (it works like the micromap but is slower).
  Default: on.
- differ.micromap.enable_overview_slider_opacity: Enable overview slider
  transparency
  When enabled, the overview panel's slider is rendered with simulated
  alpha blending so the colored diff lines remain visible through the
  slider, like in WinMerge. When disabled, the slider uses a fast
  opaque solid fill.
  Only has an effect when differ.micromap.enable_overview is on.
  Default: on.
- differ.micromap.overview_slider_opacity: Overview slider opacity in
  percent
  Opacity of the overview panel slider, in percent.
  Only used when enable_overview_slider_opacity is on.
  Range: 0-100. Default: 40.


== Diff algorithms and best practices ==

Differ implements all the best-known diff algorithms, with a lot of
improvements on top of each. Seven algorithms are available: two native
ones that run in compiled Pascal code inside CudaText -- Native Histogram
(a port of JGit's Histogram Diff, the algorithm behind "git diff
--histogram") and Native Myers (a port of WinMerge's bundled GNU
diffutils Myers, the algorithm behind "git diff --myers") -- which are
10-30x faster than any pure-Python implementation on large files, and
five pure-Python ones (Hybrid, Myers, VS Code, Patience, difflib). The
complete description of every algorithm is in the
differ.algorithm.diff_algorithm option.

Two option combinations cover the two extreme needs:

Fastest compare (very big files, minimum CPU and memory):
- Set differ.algorithm.diff_algorithm to "native_myers".
- Disable differ.algorithm.compare_with_details (no
  character-by-character comparison inside changed lines).
- Disable differ.algorithm.beautify_alignment (no similarity-based
  re-pairing of changed blocks).
- Disable differ.micromap.enable_overview (no overview panel painting).
This combination gives the fastest compare and the smallest memory
footprint. The output is rendered exactly the way GNU diffutils /
WinMerge side-by-side (sdiff) output does.

Best human-readable compare (the recommended default):
- Set differ.algorithm.diff_algorithm to "native_histogram".
- Enable differ.algorithm.compare_with_details (highlights the exact
  changed characters inside each modified line).
- Enable differ.algorithm.beautify_alignment (re-pairs similar lines
  inside changed blocks so they appear aligned, like VS Code does).
This combination produces the most readable side-by-side compare, with
better results than WinMerge, VS Code, Meld and Beyond Compare.

If a diff is hard to read, try a different algorithm: Histogram, Hybrid,
Patience or VSCode tend to generate much cleaner results. If you compare
massive files and need maximum speed, use Native Myers instead.

Myers vs. Histogram Differences:
native_histogram generally produces more "human-readable" and semantically meaningful alignments. By anchoring the comparison on unique or low-frequency lines first, it keeps moved, refactored, or reordered code blocks intact rather than scrambling them with spurious matches on common elements (like braces or blank lines). native_myers simply looks for the shortest possible edit path without semantic context. For normal files, the speed difference between the two is negligible. However, native_myers is noticeably faster when comparing massive files with extreme differences, thanks to its early-exit heuristics.

== Overview panel and micromap ==

Differ can show one or both of two mini-map styles next to a compare tab:
the plugin's own gap-aware overview panel (default: on) and the built-in
CudaText micromap (default: off). They can be enabled independently and
used at the same time. The overview is the recommended default because
it stays gap-aware; the micromap is faster and cheap but does not account
for inter-line gaps.

The overview panel's slider works like a normal scrollbar:
- Drag the slider: the thumb follows the mouse live while you drag. The
  repaints are throttled by wall clock to ~33 fps, so the thumb always
  tracks the mouse yet the CPU cost stays negligible (each repaint is
  one cached-bitmap copy plus the slider drawing; the expensive diff
  rectangles are cached in a separate bitmap and never redrawn on
  scroll). This cannot be done with a timer: during a drag the message
  queue is flooded with mouse moves and Windows only delivers WM_TIMER
  when the queue drains, so a timer-debounced slider would stay frozen
  until the drag stops.
- Click the track: the view jumps so the clicked position becomes the
  center of the viewport, and the slider lands there immediately.
- Scroll the editors (wheel, native scrollbars, keyboard): the slider
  follows the scrolling live (same ~33 fps throttle), with a final
  repaint shortly after scrolling stops.
- Scrolling from the overview panel itself keeps the two editor halves
  in the same display frame: both halves' positions are written first,
  then both are repainted SYNCHRONOUSLY (Ed.Repaint via EDACTION_UPDATE)
  inside the same mouse-event callback, so they paint back-to-back and
  land on screen atomically -- no "one half scrolls a few ms before the
  other" effect, even in the middle of a drag when the message queue is
  flooded with mouse moves. The same synchronous repaint is used when
  mirroring a wheel/scrollbar scroll of one half to the other, so the
  sync is frame-accurate in BOTH directions.


== Notes ==

- Untitled tabs can be compared just like saved files. Your edits in the
  compare view are synced back to the original untitled tab when you save.
- The compare view uses CudaText's built-in split-editor feature -- no
  temporary files are created on disk.
- If both files become identical after editing, all markers are cleared
  and a message is shown when refreshing the compare.


== Authors ==

  OlehL, https://github.com/OlehL
  Alexey Torgashin (CudaText)
  Andrey Kvichanskiy, https://github.com/kvichans
  Vivalzar, https://github.com/Vivalzar
  Badr Elmers, https://github.com/badrelmers

License: MIT
