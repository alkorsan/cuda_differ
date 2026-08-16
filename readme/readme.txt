Differ plugin for CudaText

Compares two files side-by-side in a single tab, highlights all differences,
and lets you edit the files directly in the compare view -- your changes are
synced back to the original files automatically.


== What it does ==

- Opens two files (or two untitled tabs) in one split tab and highlights
  added, deleted, and changed lines side-by-side.
- Lets you edit either side of the compare view. When you save, your edits
  are written back to the original files on disk -- no need to copy text
  around manually.
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
- Optional built-in overview (mini-map) panel docked to the right of the
  compare view, showing a gap-aware miniature of both editors side-by-side
  with colored diff highlights. The overview's slider can be made
  semi-transparent (WinMerge-style) so the diff colors remain visible
  through it. The slider height is proportional to the visible-vs-total
  ratio, just like real scrollbars in browsers and editors, with a 30px
  minimum so it always stays grabbable. Click or drag the slider to
  navigate.


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

Refresh
    Re-runs the comparison after you edit either side. Useful if
    auto-refresh is off.

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
    Copies the selected difference from the left file to the right file.

Copy current difference to the left
    Copies the selected difference from the right file to the left file.

Copy current line to the right
    Copies the line under the cursor from the left file to the right file.

Copy current line to the left
    Copies the line under the cursor from the right file to the left file.

Config...
    Opens the options dialog.


== Tab context menu ==

Right-clicking a tab title shows "Differ" submenu with:
- Compare with... -- pick a file to compare against this tab
- Compare with focused tab -- compare this tab with the currently focused tab
- Compare with tab -- submenu listing all open tabs; click one to compare.
  If the list is too long, the first entry "More tabs..." opens a dialog
  with a scrollbar to pick any open tab.


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
  restored when you start CudaText again, with the same content and
  comparison.
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

All options are stored in settings/cuda_differ.json. The option names are
listed below in parentheses.

Colors (chapter "colors"):
- Color of changed lines (differ.changed_color)
  Background color for lines that were modified (replaced with different
  content). Leave empty to use the theme default.
- Color of added lines (differ.added_color)
  Background color for lines that exist only in the right file (added).
  Leave empty to use the theme default.
- Color of deleted lines (differ.deleted_color)
  Background color for lines that exist only in the left file (removed).
  Leave empty to use the theme default.
- Color of inter-line gap background (differ.gap_color)
  Background color for the blank gap inserted to keep the two sides
  aligned when one side has fewer lines. Default: LightBG5.

Configuration (chapter "config"):
- Synchronized scrolling (differ.sync_scroll)
  When enabled, scrolling one side of the compare view also scrolls the
  other side. Default: true.
- Detailed comparison (differ.compare_with_details)
  When enabled, changed lines are compared character-by-character and
  the specific changed characters are highlighted within the line.
  When disabled, changed lines are highlighted as a whole. Default: true.
- Similarity threshold in percents (differ.ratio_percents)
  Controls how aggressive the character-level comparison is when
  deciding whether two lines should be treated as "changed" (similar
  but not identical) or as separate "deleted + added" lines. A higher
  value means lines must be more similar to be considered "changed";
  a lower value means more lines will be shown as changed (with
  character-level detail). Range: 1-100. Default: 75.
- Keep carets visible on sync (differ.enable_sync_caret)
  When enabled, moving the cursor in one side also moves the cursor in
  the other side to the corresponding difference block. Default: false.
- Auto-refresh after changes (differ.enable_auto_refresh)
  When enabled, the diff markers are automatically re-calculated after
  you stop editing for 1-2 seconds. When disabled, you must click
  Refresh manually. Default: false.
- Context lines in unified diff (differ.diff_context)
  Number of unchanged context lines shown around each change in the
  unified diff output (produced by the "Diff current document with..."
  commands). Default: 3.
- Enable micromap (differ.enable_micromap)
  When enabled, CudaText's built-in micromap (mini-map) columns are
  shown in the split gutter between the two editors. Column 0 (line
  states) and column 2 (selections) are cleared; column 1 (bookmarks,
  also showing the cursor position) is kept and painted with diff-
  colored line highlights. Note: the micromap does NOT account for
  inter-line gaps, so it may desync from the actual text positions
  when gaps are present -- for a gap-aware alternative, enable
  enable_overview instead (or both). Default: true.
- Enable overview panel (differ.enable_overview)
  When enabled, a custom gap-aware overview (mini-map) panel is docked
  to the right of the compare view. Unlike the built-in micromap, the
  overview accounts for inter-line gaps inserted for visual alignment,
  so it stays in sync with what you actually see. Shows both editors
  side-by-side with colored rectangles for deleted (red), added
  (green), and changed (yellow) lines, plus gray rectangles for gaps
  and white for unchanged lines. Click the overview to scroll the
  corresponding editor, or drag the slider to scroll continuously.
  The slider height is proportional to the visible-page vs total-
  content ratio (like real scrollbars in browsers and editors), with a
  minimum height of 30px so it always stays grabbable. Can be used
  together with the micromap. Default: false.
- Enable overview slider transparency (differ.enable_overview_slider_opacity)
  When enabled, the overview panel's slider is rendered with simulated
  alpha blending (per-row pre-blend of the underlying overview colors
  with the slider fill color), so the colored diff lines remain visible
  through the slider like in WinMerge. When disabled, the slider uses
  a fast opaque solid fill (the old behaviour). Note: when enabled AND
  overview_slider_opacity is below 8%, the slider falls back to a
  border-only style (fully see-through) for performance. Only has an
  effect when enable_overview is on. Default: true.
- Overview slider opacity in percent (differ.overview_slider_opacity)
  Opacity of the overview panel slider, in percent. 0 = fully
  transparent (slider border only, the static overview shows through
  completely), 100 = fully opaque (solid fill). Intermediate values
  (e.g. 40) simulate true alpha blending via per-row pre-blending of
  the underlying overview colors with the slider fill color. Values
  below 8 use the faster border-only path instead of per-row blending.
  Only used when enable_overview_slider_opacity is true. Range: 0-100.
  Default: 40.
- Diff algorithm (differ.diff_algorithm)
  Selects the diff algorithm used by the side-by-side compare view and the
  unified-diff commands. Seven choices are offered in a dropdown:
    * native_histogram -- Native Histogram diff (port of JGit's
      HistogramDiff, the algorithm git uses for `git diff --histogram`).
      Runs in compiled Free Pascal code via cudatext.diff_proc(). Behaves
      like Patience diff when unique common lines exist, with graceful
      fallback when they don't. Fast and high-quality. This is the default
      and recommended option.
    * native_myers -- Native Myers diff (port of JGit's MyersDiff with
      linear-space middle-snake optimization, the algorithm git uses for
      `git diff --myers`). Runs in compiled Free Pascal code via
      cudatext.diff_proc().
    * hybrid -- Pure-Python Hybrid (Patience anchoring on unique lines +
      Myers for the gaps). Best pure-Python quality.
    * myers -- Pure-Python Myers O(NP) (Wu/Manber/Myers/Miller 1989),
      ported from Meld.
    * vscode -- Pure-Python VS Code diff algorithm. Best alignment on
      files with many duplicated lines, but slowest.
    * patience -- Pure-Python Patience diff (via the embedded
      patiencediff library).
    * difflib -- Python's standard difflib SequenceMatcher.
  The native algorithms require a CudaText build that includes the
  diff_proc API. If the API is not available, they silently fall back to
  the closest Python equivalent (native_histogram -> hybrid, native_myers
  -> myers). Default: native_histogram.
- Autojunk heuristic (differ.autojunk)
  Controls the autojunk parameter of difflib's SequenceMatcher. When
  enabled (the Python default), items that appear more than 1% of the
  time and at least 200 times are automatically treated as "junk" and
  ignored for matching. This speeds up diffing of large files with many
  repeated lines but can occasionally cause subtle differences to be
  missed. Disable it if you suspect the diff is skipping matches due
  to frequent repeated lines. This option only applies when
  diff_algorithm is "difflib" -- MyersSequenceMatcher,
  PatienceSequenceMatcher and VSCodeSequenceMatcher do not support
  autojunk. Default: true.
- Enable profiling (differ.enable_profiling)
  When enabled, prints a detailed timing report to the console after each
  compare, breaking down time spent in the diff algorithm, opcode
  realignment, event generation, char-level diffing (native vs Python),
  and UI painting (bookmarks, decor, gaps, attributes). Use for debugging
  performance issues only -- adds small overhead. Default: false.


== Notes ==

- Untitled tabs can be compared just like saved files. Your edits in the
  compare view are synced back to the original untitled tab when you save.
- The compare view uses CudaText's built-in split-editor feature -- no
  temporary files are created on disk.
- If both files become identical after editing, all markers are cleared
  and a message is shown.


== Authors ==

  OlehL, https://github.com/OlehL
  Alexey Torgashin (CudaText)
  Andrey Kvichanskiy, https://github.com/kvichans
  Vivalzar, https://github.com/Vivalzar
  Badr Elmers, https://github.com/badrelmers

License: MIT
