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
- Enable built-in micromap (differ.enable_micromap)
  When enabled, switches on CudaText's native micromap (mini-map) column
  in both halves of the compare split. The default micromap columns 0
  (line states) and 2 (selections) are cleared; column 1 (bookmarks) is
  kept because it also shows the cursor position. Diff-colored line
  highlights are painted on column 1. The micromap is fast but does
  NOT account for the inter-line gaps Differ inserts for visual
  alignment, so it may drift out of sync with the text when gaps are
  present -- for a gap-aware alternative, enable enable_overview
  instead (or both). Default: true.
- Enable gap-aware overview panel (differ.enable_overview)
  When enabled, adds a custom image control docked to the right side of
  the editor's outer form. The control renders both files side-by-side
  as colored 1-pixel rectangles (red=deleted, green=added,
  yellow=changed, gray=gap, background=unchanged) and is fully gap- and
  wrap-aware so colored blocks always line up with the corresponding
  editor lines. A dithered viewport rectangle shows the visible range
  and a thin cursor line marks the caret; click anywhere to scroll the
  corresponding editor to that line. Uses a two-bitmap static/dynamic
  split so scrolling only redraws the cheap dynamic part (debounced
  150 ms). Colors come from the active UI theme (EdTextBg, EdTextFont)
  plus the same color_* config options as the editor highlights. Can be
  used together with the micromap. Default: false.


== Overview panel and micromap ==

Differ can show one or both of two mini-map styles next to a compare tab:
the built-in CudaText micromap and the plugin's own gap-aware overview
panel. They can be enabled independently and used at the same time.

Built-in micromap (enable_micromap, default: on)
- Switches on CudaText's native micromap column in both halves of the
  split. The default micromap columns 0 (line states) and 2 (selections)
  are cleared so only diff-relevant information is shown; column 1
  (bookmarks) is kept because it also doubles as a cursor-position
  indicator. Diff-colored line highlights are painted on column 1 via
  attr(show_on_map=1).
- On the left editor the micromap is placed on the right side, on the
  right editor on the left side, so both micromaps sit in the split
  gutter between the two files.
- The micromap is fast and cheap, but it does NOT account for the
  inter-line gaps Differ inserts for visual alignment, so the colored
  blocks can drift out of sync with the text positions when gaps are
  present. For a gap-aware alternative, enable the overview panel.

Gap-aware overview panel (enable_overview, default: off)
- Adds a custom image control docked to the right side of the editor's
  outer form. The control renders both files side-by-side as colored
  1-pixel-tall rectangles: red for deleted lines (left file only),
  green for added lines (right file only), yellow for changed lines,
  gray for inter-line gaps, and the theme background color for
  unchanged lines.
- Unlike the micromap, the overview is fully gap-aware: it walks both
  files in lock-step with the same gap bookkeeping the editor uses, so
  a colored block in the overview always lines up with the
  corresponding line in the editor. The overview is also wrap-aware:
  when word-wrap is on, each line's overview height is multiplied by
  its number of wrapped visual rows, so the overview stays aligned
  even when matching lines wrap to different heights.
- A viewport rectangle shows the range of lines currently visible in
  each editor, and a thin cursor line marks the caret. Because
  Lazarus cannot do real alpha transparency, the viewport fill is
  rendered by copying a pre-computed dithered (every-other-line
  darkened) version of the static bitmap onto the canvas, which
  simulates a 50% dimmed overlay while keeping the colored blocks
  visible through the dither pattern.
- Click anywhere in the overview to scroll the corresponding editor
  to that line. The click is mapped through the same wrap- and
  gap-aware coordinate transform used for painting, so the line you
  click on is the line the editor jumps to.
- Performance: the panel uses a two-bitmap split. A persistent
  "static" bitmap stores the hundreds of colored line/gap rectangles
  and is only rebuilt on compare or resize. On every scroll (debounced
  150 ms), the static bitmap is blitted to the image control's
  embedded bitmap and only the cheap dynamic part (viewport rectangle
  + cursor line) is redrawn on top -- so scrolling does not reissue
  the expensive CANVAS_RECT_FILL loop.
- Colors are taken from the active UI theme (EdTextBg for the
  background, EdTextFont for the cursor / viewport border) so the
  panel matches both light and dark themes automatically. The
  deleted/added/changed/gap colors reuse the same color_* config
  options as the editor highlights.


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
