Differ plugin for CudaText

Compares two files side-by-side in a single tab, highlights all differences,
and lets you edit the files directly in the compare view -- your changes are
synced back to the original files automatically.


== What it does ==

- Opens two files (or two untitled tabs) in one split tab and highlights
  added, deleted, and changed lines side-by-side.
- Lets you edit either side of the compare view. When you save, your edits
  are written back to the original files on disk.
- Keeps the original tabs open while you compare, so you always have both
  the originals and the compare view available.
- Remembers your compare tabs across CudaText restarts. If you close
  CudaText with a compare tab open, it is restored when you start again.
- Provides synchronized scrolling so both sides stay aligned as you
  navigate.
- Supports word-wrap: you can turn wrap on in a compare tab and the two
  sides stay visually aligned even when corresponding lines wrap to
  different heights. Toggling wrap mode re-applies the alignment
  automatically.
- Optional gap-aware overview panel docked to the right of the compare
  view, showing a miniature of both editors side-by-side with colored
  diff highlights (WinMerge-style).

The compare engine implements all the best-known diff algorithms, with a
lot of improvements on top of each: two native ones running in compiled
Pascal code inside CudaText (JGit's Histogram Diff and WinMerge/GNU
diffutils' Myers, 10-30x faster than any pure-Python implementation on
large files) and five pure-Python ones (Hybrid, Myers O(NP), VS Code,
Patience and difflib). Combined with character-level highlighting of the
exact changed characters and smart line alignment, this gives the best
human-readable compare results -- better than WinMerge, VS Code, Meld and
Beyond Compare -- while comparing faster than VS Code and Meld. See the
"Diff algorithms and best practices" section below for how to tune the
plugin for maximum speed or maximum readability.


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

Refresh
    Re-runs the comparison after you edit either side. Useful if
    differ.advanced.enable_auto_refresh is off.

Focus the opposite file
    Moves the cursor to the other side of the split.

Select current difference
    Selects the difference block under the cursor.

Select all differences
    Selects all difference blocks in both files.

Jump to next difference
    Moves the cursor to the next changed block.

Jump to previous difference
    Moves the cursor to the previous changed block.

Copy current difference to the right
    Copies the selected difference from the left to the right side.

Copy current difference to the left
    Copies the selected difference from the right to the left side.

Copy current line to the right
    Copies the line under the cursor from the left to the right side.

Copy current line to the left
    Copies the line under the cursor from the right to the left side.

Config...
    Opens the options dialog.


== Tab context menu ==

Right-clicking a tab title shows "Differ" submenu with:
- Compare with... -- pick a file to compare against this tab
- Compare with focused tab -- compare this tab with the currently focused tab
- Compare with tab -- submenu listing all open tabs; click one to compare.
  If the list is too long, the first entry "More tabs..." opens a dialog
  with a scrollbar to pick any open tab.
- Refresh -- re-run the compare on both sides of the compare tab.
- Five checkable "ignore" options (below Refresh, after a separator):
  Ignore case, Ignore whitespace, Ignore blank lines, Ignore line endings,
  Ignore numbers.
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
algorithms always compare strictly and ignore these options. Set them
from the diff tab context menu (see above) or from the config dialog
("differ.ignoreopt.*" options in settings/cuda_differ.json).

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
  also on) contains only spaces and tabs. Ignored regions stay visible,
  WinMerge-style: their lines get the "ignored" background color (see
  "Color of ignored differences" in the options) and a small colored
  gap fills in for the missing lines so the two sides stay aligned.
  They are not counted as differences: no bookmarks, skipped by
  Next/Previous Difference and Copy, and two files differing only in
  blank lines report "No differences found (with current ignore
  options)".
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
  without compare, run Refresh command to start the compare.
- The plugin loads automatically on startup only when compare tabs are
  active, so there is no performance impact when you are not comparing.
- When you close the last compare tab, the plugin stops auto-loading on
  the next startup.

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
- differ.theme.changed_color: Color of changed lines
  Background color for lines that were modified (replaced with different
  content). Also colors the char-level highlights inside modified lines,
  the margin markers, the micromap highlights and the overview panel.
  Leave empty to use the theme default.
- differ.theme.added_color: Color of added lines
  Background color for lines that exist only in the right file (added).
  Also colors the char-level highlights inside added lines, the margin
  markers, the micromap highlights and the overview panel.
  Leave empty to use the theme default.
- differ.theme.deleted_color: Color of deleted lines
  Background color for lines that exist only in the left file (removed).
  Also colors the char-level highlights inside deleted lines, the margin
  markers, the micromap highlights and the overview panel.
  Leave empty to use the theme default.
- differ.theme.gap_color: Color of inter-line gap background
  Background color for the blank gap inserted to keep the two sides
  aligned when one side has fewer lines. Also colors the gap rectangles
  in the overview panel.
  Leave empty to use the theme default.
- differ.theme.ignored_color: Color of ignored differences
  Background color for lines whose difference is suppressed by the
  "Ignore blank lines" option, and for the compensating gap inserted
  next to them so the two sides stay aligned (WinMerge-style ignored
  differences). Also colors the micromap highlights and the overview
  panel.
  Leave empty to use the theme default.

Algorithm section:
- differ.algorithm.diff_algorithm: Diff algorithm
  Selects the diff algorithm used by the side-by-side compare.
  Native algorithms run in compiled Pascal code and are 10-30x faster
  than the pure-Python implementations on large files. They require a
  CudaText build that includes the diff_proc API; if it is not available,
  they silently fall back to the closest Python equivalent
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
  Refresh command manually.
  Default: off.
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
  and UI painting (bookmarks, decor, gaps, attributes). Use for debugging
  performance issues only -- adds small overhead (~1-2us per timing
  point).
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
