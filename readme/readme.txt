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
