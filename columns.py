"""Hunk edge columns for the Differ plugin.

Implements the idea from the CudaText issue #6477 discussion
(https://github.com/Alexey-T/CudaText/issues/6477): two narrow custom-drawn
columns, one docked to the left of the compare view and one to the right,
each showing a bracket around every difference block (hunk):

    +----
    |
    |
    +----

A bracket spans the hunk's FULL visual footprint -- the text lines AND the
compensating gap band(s) the engine inserts inside the hunk (so on the
shorter side of a replace, or on the empty side of an insert/delete, the
bracket includes the gap). A user who wants to move a block with
Alt+Left/Alt+Right can therefore see exactly which rows will be moved, like
the rule lines Beyond Compare draws around its difference blocks.

Why columns (and not the older in-text gap bands): CudaText plugins cannot
custom-paint between the two split halves, and the previous implementation
(thin colored e.gap() bands at the hunk boundaries) drew INSIDE the text
area, adding pixels to both editors and stacking on top of the engine's
own compensating gaps. The columns here are drawn OUTSIDE the editors --
the text area is not touched at all, so there is nothing to stack and no
alignment to keep in sync. This is the same technique the overview panel
uses (a borderless dialog with an 'image' control docked to the editor's
parent form), just much cheaper to paint: a background fill plus 3 canvas
lines per VISIBLE hunk, and only visible hunks are painted at all.

Vertical alignment is pixel-exact: the top/bottom of every bracket come
from ed.convert(CONVERT_CARET_TO_PIXELS) queries, which are gap-aware,
wrap-aware and scroll-aware (they return the same Y the editor itself
uses to draw that line, relative to the editor control). Because the
docked column is top-aligned with the editor inside the same parent form,
a Y computed against the editor is directly usable as the column's Y.

Geometry (per diffmap entry [a0, a1, b0, b1], exclusive-end ranges -- the
same records jump()/copy() navigate):

  top:
    The hunk's footprint starts at the first line's top -- EXCEPT when the
    engine put a compensating band ABOVE that side's first hunk line. With
    'Beautify line alignment' on, _find_best_pairs can leave a hunk's
    leading lines unpaired, and the shorter side's compensating band then
    sits directly above the shorter side's first hunk line. The footprint
    top is then the OTHER side's first hunk line top (the two are the same
    row: that is exactly what the band compensates). So:
      - B has OUR compensating band at index b0-1  -> top = y_A(a0)
      - A has OUR compensating band at index a0-1  -> top = y_B(b0)
      - otherwise                                  -> top = y_A(a0)
    ("OUR" = the band compensates lines of THIS hunk on the other side --
    a previous hunk's trailing band may sit at the very same index and
    must NOT move our top; bands are matched by their compensated ranges.)

  bottom:
    The footprint ends below the last line and any trailing bands, i.e. at
    the top of the first line AFTER the hunk (y(k+1) already includes all
    gap bands sitting after line k):
      - a1 < line_count_a  -> bottom = y_A(a1)
      - b1 < line_count_b  -> bottom = y_B(b1)
      - both at EOF        -> convert() clamps a line_count query to the
                             bottom of the last line EXCLUDING trailing
                             gap bands (verified in ATSynEdit's
                             GenericCaretPosToClientPos: the bAfterEnd
                             path adds one char height to the last line's
                             last wrap row but only sums gaps up to index
                             count-2), so the plugin-recorded comp+align
                             band pixels at index count-1 are added back:
                             bottom = max(convert(A, count_a) + bandsA,
                                          convert(B, count_b) + bandsB)

  By the engine's alignment invariant (every line-count and wrap-count
  difference is compensated by a band), the [top, bottom] footprint is the
  SAME on both sides -- both columns draw the same bracket rows for a
  hunk.

Performance: painting runs only for hunks intersecting the visible line
range (PROP_LINE_TOP .. PROP_LINE_BOTTOM, a cheap property read; the
diffmap is scanned with the visible window, not binary-searched, which
is fine because the scan is a tuple-compare loop over integers -- for a
100k-line file with 10k hunks that is ~10k comparisons, far cheaper than
a single canvas call). On scroll, paint() is throttled to ~33 fps by
track_paint() (wall-clock gate, the same approach as the overview slider)
plus a trailing one-shot repaint from the plugin's 150 ms overview timer.
The convert() calls only run for the visible hunks (a couple dozen at
most), each O(log n) in the wrap table.

Colors come from the active CudaText theme -- background EdGutterBg, line
EdGutterFont -- so the columns visually read as part of the editors'
gutters in every theme. (See _ed_gutter_colors() in __init__.py for the
lookup order.) No plugin color option is involved.

See: https://github.com/Alexey-T/CudaText/issues/6477
"""

import time

import cudatext as ct

# Default width of one hunk edge column, in pixels. Narrow on purpose
# ("the columns should not occupy a lot of width space"): wide enough for
# a visible bracket (a vertical bar plus the top/bottom arms), narrow
# enough to cost less horizontal space than a scrollbar. Configurable via
# differ.advanced.hunk_edges_width (clamped 6..40).
COLUMNS_WIDTH_DEFAULT = 12

# Wall-clock interval (seconds) between immediate repaints while a scroll
# burst is running -- the same gate the overview slider uses
# (OVERVIEW_TRACK_INTERVAL). ~33 fps looks instant, and paint() here is
# cheap (one background fill + 3 lines per visible hunk).
TRACK_INTERVAL = 0.030

# Bracket drawing metrics, relative to the column's client width W:
#   - the vertical bar is a 1px line at x = BAR_X (same weight as
#     ATSynEdit's own code-fold staples, EdBlockStaple)
#   - the top/bottom arms run from the bar to x = W-ARM_PAD-1
# With the default W=12: bar at x=3, arms from x=3 to x=8.
BAR_X = 3
ARM_PAD = 3


class HunkColumns:
    """Manages the two hunk edge columns of one compare tab.

    One column is docked to the LEFT of the compare view (bracketing the
    left editor's hunks), one to the RIGHT (bracketing the right
    editor's). Created once per compare tab, destroyed when the tab
    closes or the feature is turned off. All state is per-instance, so
    two tabs' columns can never mix (the session holds one instance).
    """

    def __init__(self):
        # Per-column dialog/control/bitmap/canvas handles. Index 0 = the
        # LEFT column (editor A), index 1 = the RIGHT column (editor B).
        self.h_dlgs = [None, None]
        self.h_images = [None, None]
        self.h_bitmaps = [None, None]
        self.h_canvases = [None, None]
        self._ctl_indices = [None, None]
        # Editors the brackets are computed against.
        self.a_ed = None
        self.b_ed = None
        # Colors: background = EdGutterBg, lines = EdGutterFont (from the
        # active theme; set via set_colors()).
        self.color_bg = 0xF0F0F0
        self.color_line = 0x404040
        # Column width in pixels (set via set_width()).
        self.width = COLUMNS_WIDTH_DEFAULT
        # --- Hunk data, refreshed once per compare via set_data() ---
        # diffmap entries [a0, a1, b0, b1] (exclusive ends), in file order.
        self.hunks = []
        # Line counts of both editors at compare time.
        self.line_count_a = 0
        self.line_count_b = 0
        # Compensating bands per side: {index: [(other_start, other_end)]}
        # -- which other-side line range the band compensates. Used to
        # decide whether a band sitting at a hunk's top-edge index belongs
        # to THIS hunk (staggered beautify band -> the bracket top must
        # move up to the other side's first line) or to the PREVIOUS
        # hunk (trailing band -> the bracket top stays put).
        self.comp_bands_a = {}
        self.comp_bands_b = {}
        # Compensating + wrap-align band pixel sizes per side:
        # {index: px}. Used ONLY for the both-sides-at-EOF bottom edge,
        # where convert() cannot see the trailing bands (see module
        # docstring).
        self.eof_bands_a = {}
        self.eof_bands_b = {}
        # Wall-clock timestamp of the last track_paint(); gates immediate
        # repaints to TRACK_INTERVAL.
        self._track_last_paint = 0.0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def is_created(self):
        """Return True if the column dialogs have been created."""
        return self.h_dlgs[0] is not None or self.h_dlgs[1] is not None

    def create(self, a_ed, b_ed):
        """Create the two columns as borderless dialogs docked to the
        editor parent form: the A column to the LEFT (next to the left
        editor's gutter), the B column to the RIGHT (next to the right
        editor's scrollbar / the overview panel, which docks later and
        therefore lands outside us).

        Docking uses PROP_HANDLE_PARENT -- the stable grouping form that
        parents EdFirst/EdSecond/Splitter (the same handle the overview
        docks to; DLG_DOCK only accepts TForm/TFrame handles, and the
        individual editors are TATSynEdit, so docking between the halves
        is not possible -- the two outer columns are the closest a plugin
        can get to Beyond Compare's center rules).

        Args:
            a_ed: left editor (primary)
            b_ed: right editor (secondary)
        """
        self.a_ed = a_ed
        self.b_ed = b_ed
        h_parent = a_ed.get_prop(ct.PROP_HANDLE_PARENT)
        if not h_parent:
            h_parent = 0
        self._create_one(0, 'L', h_parent, 'DifferHunkEdgesA')
        self._create_one(1, 'R', h_parent, 'DifferHunkEdgesB')

    def _create_one(self, idx, side, h_parent, name):
        """Create one column dialog with an 'image' control and dock it.
        idx: 0 = left column (A), 1 = right column (B). side: 'L' or 'R'
        (the DLG_DOCK side)."""
        h_dlg = ct.dlg_proc(0, ct.DLG_CREATE)
        self.h_dlgs[idx] = h_dlg
        ct.dlg_proc(h_dlg, ct.DLG_PROP_SET, prop={
            'cap': name,
            'w': self.width,
            'h': 600,
            'border': ct.DBORDER_NONE,
            'color': self.color_bg,
        })
        # The 'image' control has an embedded bitmap that handles
        # resize/minimize/restore automatically -- the same trick the
        # overview panel uses (no on_resize/on_show handlers needed).
        ctl = ct.dlg_proc(h_dlg, ct.DLG_CTL_ADD, 'image')
        self._ctl_indices[idx] = ctl
        ct.dlg_proc(h_dlg, ct.DLG_CTL_PROP_SET, index=ctl, prop={
            'name': name + '_image',
            'align': ct.ALIGN_CLIENT,
        })
        self.h_images[idx] = ct.dlg_proc(h_dlg, ct.DLG_CTL_HANDLE,
                                         index=ctl)
        self.h_bitmaps[idx] = ct.image_proc(self.h_images[idx],
                                            ct.IMAGE_GET_BITMAP)
        self.h_canvases[idx] = ct.bitmap_proc(self.h_bitmaps[idx],
                                              ct.BITMAP_GET_CANVAS)
        ct.dlg_proc(h_dlg, ct.DLG_SHOW_NONMODAL)
        ct.dlg_proc(h_dlg, ct.DLG_DOCK, prop=side, index=h_parent)

    def destroy(self):
        """Undock and free both column dialogs."""
        for i in (0, 1):
            if self.h_dlgs[i] is not None:
                try:
                    ct.dlg_proc(self.h_dlgs[i], ct.DLG_UNDOCK)
                    ct.dlg_proc(self.h_dlgs[i], ct.DLG_FREE)
                except Exception:
                    pass
                self.h_dlgs[i] = None
                self.h_images[i] = None
                self.h_bitmaps[i] = None
                self.h_canvases[i] = None
                self._ctl_indices[i] = None
        # Drop the compare data too -- a destroyed column must never
        # paint stale brackets if it gets recreated later.
        self.hunks = []
        self.comp_bands_a = {}
        self.comp_bands_b = {}
        self.eof_bands_a = {}
        self.eof_bands_b = {}

    # ------------------------------------------------------------------
    # data / options
    # ------------------------------------------------------------------

    def set_colors(self, color_bg, color_line):
        """Set the column colors: background (theme EdGutterBg) and the
        bracket line color (theme EdGutterFont)."""
        if color_bg is not None:
            self.color_bg = color_bg
        if color_line is not None:
            self.color_line = color_line

    def set_width(self, width):
        """Set the column width in pixels (clamped to a sane range)."""
        try:
            width = int(width)
        except (TypeError, ValueError):
            width = COLUMNS_WIDTH_DEFAULT
        self.width = max(6, min(40, width))

    def set_data(self, hunks, line_count_a, line_count_b,
                 comp_bands_a=None, comp_bands_b=None,
                 eof_bands_a=None, eof_bands_b=None):
        """Store the fresh compare's hunk records for painting.

        Args:
            hunks: diffmap entries [a0, a1, b0, b1] (exclusive ends),
                in file order. Ignored (suppressed) differences are not
                in the diffmap and never get brackets.
            line_count_a/b: line counts of the two editors.
            comp_bands_a/b: {index: [(other_start, other_end), ...]} --
                compensating bands per side, with the other-side line
                range each band compensates (see the module docstring's
                top-edge rule). A band recorded at index i is drawn by
                the engine between lines i and i+1.
            eof_bands_a/b: {index: px} -- pixel sizes of compensating +
                wrap-align bands per side; only the entries at index
                count-1 are used (the both-sides-at-EOF bottom edge).
        """
        self.hunks = list(hunks or [])
        self.line_count_a = line_count_a
        self.line_count_b = line_count_b
        self.comp_bands_a = comp_bands_a or {}
        self.comp_bands_b = comp_bands_b or {}
        self.eof_bands_a = eof_bands_a or {}
        self.eof_bands_b = eof_bands_b or {}

    # ------------------------------------------------------------------
    # painting
    # ------------------------------------------------------------------

    def _get_size(self, idx):
        """Current client size (w, h) of one column's image control."""
        props = ct.dlg_proc(self.h_dlgs[idx], ct.DLG_CTL_PROP_GET,
                            index=self._ctl_indices[idx])
        return props.get('w', self.width), props.get('h', 600)

    def _y_of(self, ed, line):
        """Pixel Y of a line's top edge, relative to the editor control
        (gap/wrap/scroll aware), via CONVERT_CARET_TO_PIXELS. For
        line == line_count (one past the last line) ATSynEdit returns the
        BOTTOM of the last line's last wrap row, excluding trailing gap
        bands. Returns None when the conversion is unavailable."""
        try:
            pt = ed.convert(ct.CONVERT_CARET_TO_PIXELS, 0, line)
            if pt:
                return pt[1]
        except Exception:
            pass
        return None

    def _band_is_ours(self, bands, index, r0, r1):
        """Does a compensating band recorded at `index` belong to the
        hunk whose OTHER-side range is [r0, r1)? True when the band's
        compensated other-side range overlaps [r0, r1) -- a previous
        hunk's trailing band sits at the same index but compensates the
        PREVIOUS hunk's lines, so it does not overlap and is not ours."""
        for other_start, other_end in bands.get(index, ()):
            if other_start < r1 and other_end > r0:
                return True
        return False

    def _bracket(self, h):
        """Compute the [top, bottom] pixel rows of one hunk's footprint
        (see the module docstring for the rules). Returns (top, bottom)
        or None when a needed conversion failed."""
        a0, a1, b0, b1 = h
        # --- top ---
        if self._band_is_ours(self.comp_bands_b, b0 - 1, a0, a1):
            # B's compensating band sits directly above B's first hunk
            # line and belongs to THIS hunk (beautify-staggered leading
            # lines on A): the footprint starts at A's first hunk line.
            top = self._y_of(self.a_ed, a0)
        elif self._band_is_ours(self.comp_bands_a, a0 - 1, b0, b1):
            # Mirror case: A's band above A's first hunk line.
            top = self._y_of(self.b_ed, b0)
        else:
            # No staggered band above either first line: both first
            # lines sit on the same row (the engine's alignment
            # invariant), so either query works.
            top = self._y_of(self.a_ed, a0)
        # --- bottom ---
        if a1 < self.line_count_a:
            bottom = self._y_of(self.a_ed, a1)
        elif b1 < self.line_count_b:
            bottom = self._y_of(self.b_ed, b1)
        else:
            # Both sides end at EOF: convert() clamps to the bottom of
            # the last line EXCLUDING trailing bands, so add the recorded
            # comp+align band pixels back, per side, and take the lower
            # edge of the two (they are the same row by the alignment
            # invariant; max() is just belt-and-braces).
            ya = self._y_of(self.a_ed, self.line_count_a)
            if ya is not None:
                ya += self.eof_bands_a.get(self.line_count_a - 1, 0)
            yb = self._y_of(self.b_ed, self.line_count_b)
            if yb is not None:
                yb += self.eof_bands_b.get(self.line_count_b - 1, 0)
            vals = [v for v in (ya, yb) if v is not None]
            bottom = max(vals) if vals else None
        if top is None or bottom is None:
            return None
        # A footprint is at least one line tall; guard against a
        # degenerate 0-height bracket (should not happen, but a flat
        # bracket would be invisible).
        if bottom <= top:
            bottom = top + 1
        return top, bottom

    def _paint_column(self, idx, ed, line_count):
        """Paint one column: background fill plus a bracket per hunk
        intersecting the editor's visible line range. Hunks fully above
        or below the viewport are skipped entirely -- with a huge file
        and thousands of hunks, only the visible couple dozen get any
        convert() calls and canvas lines."""
        c = self.h_canvases[idx]
        if c is None:
            return
        w, h = self._get_size(idx)
        if w <= 0 or h <= 0:
            return
        # Resize the embedded bitmap to the control size (the image
        # control starts with a 0x0 bitmap; painting without resizing
        # shows nothing).
        try:
            ct.bitmap_proc(self.h_bitmaps[idx], ct.BITMAP_SET_SIZE, w, h)
        except Exception:
            return
        # Background: the theme gutter color.
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_bg,
                       style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=0, y=0, x2=w, y2=h)
        if not self.hunks:
            return
        # Visible line window of THIS column's editor (cheap property
        # reads; PROP_LINE_BOTTOM considers word wrap).
        try:
            vis_top = ed.get_prop(ct.PROP_LINE_TOP) or 0
            vis_bot = ed.get_prop(ct.PROP_LINE_BOTTOM)
            if vis_bot is None:
                vis_bot = line_count
        except Exception:
            vis_top, vis_bot = 0, line_count
        # The bracket's vertical bar / arms.
        bar_x1 = BAR_X
        arm_x2 = w - ARM_PAD - 1
        ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=self.color_line, size=1)
        for hunk in self.hunks:
            a0, a1, b0, b1 = hunk
            # Defensive: a diffmap entry must cover lines on at least one
            # side; skip malformed all-empty records.
            if a0 >= a1 and b0 >= b1:
                continue
            # This column's own line range for the hunk.
            r0, r1 = (a0, a1) if idx == 0 else (b0, b1)
            # Skip hunks entirely outside the visible window (with a
            # one-hunk margin so a bracket poking in from just above or
            # below the viewport still draws).
            if r1 - 1 < vis_top - 1 or r0 > vis_bot + 1:
                continue
            br = self._bracket(hunk)
            if br is None:
                continue
            top, bottom = br
            # Pure Y clip against the column canvas: partially visible
            # hunks draw their visible part.
            if bottom <= 0 or top >= h:
                continue
            top = max(0, top)
            bottom = min(h - 1, bottom)
            # The bracket: top arm, vertical bar, bottom arm.
            ct.canvas_proc(c, ct.CANVAS_LINE,
                           x=bar_x1, y=top, x2=arm_x2, y2=top)
            ct.canvas_proc(c, ct.CANVAS_LINE,
                           x=bar_x1, y=top, x2=bar_x1, y2=bottom)
            ct.canvas_proc(c, ct.CANVAS_LINE,
                           x=bar_x1, y=bottom, x2=arm_x2, y2=bottom)

    def paint(self):
        """Repaint both columns with the current data."""
        if not self.is_created():
            return
        self._paint_column(0, self.a_ed, self.line_count_a)
        self._paint_column(1, self.b_ed, self.line_count_b)

    def track_paint(self):
        """Immediate repaint for scroll bursts, wall-clock throttled to
        ~33 fps (the same approach as the overview's slider: a wall-clock
        gate, never a timer, because a scroll/drag floods the message
        queue and starves WM_TIMER). The plugin's one-shot 150 ms timer
        does the final full repaint after scrolling stops."""
        now = time.perf_counter()
        if now - self._track_last_paint < TRACK_INTERVAL:
            return
        self._track_last_paint = now
        self.paint()
