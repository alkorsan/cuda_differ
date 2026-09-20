"""Image-based overview panel for the Differ plugin.

Replaces the micromap with a custom image control docked to the right side
of the editor's parent form (PROP_HANDLE_PARENT). Unlike the micromap, the
overview is gap-aware: it accounts for the inter-line gaps inserted by Differ
for visual alignment, so the overview stays in sync with what the user
actually sees.

Architecture:
  - PaintboxOverview creates a non-modal dialog with an 'image' control.
  - The dialog is docked to the RIGHT side of PROP_HANDLE_PARENT.
    (Earlier versions used PROP_HANDLE_PARENT2 — the parent-of-parent.
    That was needed because PROP_HANDLE_PARENT was the splitter, which
    caused the overview to jump when side panels were toggled. The
    CudaText author added a grouping panel-control for EdFirst/EdSecond/
    Splitter, so they now are parented by that additional panel.
    PROP_HANDLE_PARENT now returns that stable grouping panel and is the
    recommended docking target. See:
    https://github.com/CudaText-addons/cuda_differ/issues/29)
  - The 'image' control has an embedded bitmap that handles resize/minimize/
    restore automatically — no need for on_act/on_resize/on_show handlers.
  - Uses a two-bitmap optimization (see _ensure_static_bitmap / paint):
    * Static bitmap: a separate persistent bitmap (via bitmap_proc) that
      stores the colored diff segments, the scroll buttons and the
      separator line. Only repainted on compare/resize via
      repaint_static().
    * Dynamic: on each paint(), copy the static bitmap to the image's
      embedded bitmap via CANVAS_BITMAP, then draw the slider and
      grabber on top. This is cheap (one bitmap copy + a few
      CANVAS_RECT / CANVAS_LINE calls).

  - PANEL LAYOUT (like a usual scrollbar):
      +----------------+
      |       ▲        |  BUTTON_HEIGHT px, ▲ scrolls one line up
      |----------------|
      | left | map  |  |  track: the two editors' mini-maps side by side
      | line | area |  |  slider lives here (between the buttons)
      |----------------|
      |       ▼        |  BUTTON_HEIGHT px, ▼ scrolls one line down
      +----------------+
      x=0..SEP_LINE_WIDTH is a grey vertical separator line spanning the
      FULL panel height (the button boxes included), visually separating
      the overview from the editor / the editor's scrollbar.
    The ▲/▼ boxes use the theme's ScrollBack color as background and the
    theme's ScrollArrow color for the arrow glyph, both read via
    PROC_THEME_UI_DICT_GET. The glyph is centered in its box and the box
    has no border, like the scrollbar buttons of editors and browsers.
    Pressing a button scrolls one line at once, and after a short hold it
    starts auto-repeating (like real scrollbar buttons) until released.

  - WINMERGE-STYLE PAINTING (the "Location Pane" approach):
    With huge files (1M lines, 200k diffs) painting every colored line is
    pointless: the panel is only ~600 pixels tall, so thousands of
    consecutive sub-pixel segments would just keep overwriting the same
    pixel rows — you cannot paint half a pixel. WinMerge's LocationView
    solves this by converting only the diff blocks to pixels and SKIPPING
    every block whose end pixel equals the previous block's end pixel
    (it is fully covered by what is already drawn). This plugin does the
    same, plus run coalescing:
      * consecutive same-colored lines merge into one segment;
      * each side's line/gap positions are converted to visual rows ONCE
        via a prefix-sum pass (itertools.accumulate, C speed) instead of
        per-line O(n) Python loops;
      * the draw loop skips every segment whose end pixel equals the
        previous segment's end pixel (the WinMerge
        `nPrevEndY != bottom_coord` rule), so the number of actual
        canvas calls is bounded by the panel's pixel HEIGHT, not by the
        number of diffs — 200k diffs still paint at most ~600 rects per
        side.

  - PROPORTIONAL SLIDER HEIGHT:
    Like real scrollbars in browsers/editors, the slider grows/shrinks
    based on the visible-page vs total-content ratio. CudaText reports:
      smooth_max     = total content height (pixels, INCLUDES page)
      smooth_page    = visible viewport height (pixels)
      smooth_pos     = current scroll position (pixels)
    So slider_height = track_h * smooth_page / smooth_max, clamped to a
    30px minimum so the slider stays grabbable. The slider_top formula
    `py_top = track_y0 + track_h * smooth_pos / smooth_max` is
    mathematically consistent: when smooth_pos reaches its max
    (smooth_max - smooth_page), py_top lands flush at the track bottom.

  - LIVE SLIDER DRAG:
    A drag floods the message queue with mouse moves, and WM_PAINT is
    only delivered when the queue is empty — so although paint() keeps
    updating the bitmap in memory, the on-screen control appears frozen
    until the drag stops. To make the thumb track the mouse like a real
    scrollbar, the mouse handlers pump the message queue once
    (app_proc(PROC_IDLE)) right after painting, which delivers the
    pending WM_PAINT synchronously — guarded against re-entrancy by the
    _pumping flag. The pump also dispatches pending INPUT messages: only
    mouse MOVES are dropped while pumping (breaking the move→pump→move
    recursion; the newest position arrives with the next event).
    Mouse UP / EXIT / DOWN are always processed — a release dispatched
    during a pump must never be swallowed, or the drag and the ▲/▼
    auto-repeat would survive the released button (the "mouse is not
    released" bug).

  See: https://github.com/CudaText-addons/cuda_differ/issues/29
"""

import time
from itertools import accumulate

import cudatext as ct

# Overview dialog width in pixels (docked to the right)
OVERVIEW_WIDTH = 40
# Default width of the overview panel docked to the right of the compare
# view. Was 80px; reduced to 40px (50% smaller) — the overview shows
# both files side-by-side at half-width each, which still gives enough
# resolution to spot colored diff blocks while taking less horizontal
# space. The panel is docked and resizable, so users can drag it wider
# if they want more detail.

# Wall-clock interval (seconds) between immediate slider repaints while
# tracking the mouse (slider drag) or a scroll burst. ~33 fps looks
# instant to the eye, and paint() is cheap here (one cached-bitmap copy
# + the slider drawing -- the expensive static rectangles never run),
# so the CPU cost stays negligible even during fast drags. The gate is
# WALL-CLOCK, not a timer: during a drag the message queue is flooded
# with mouse moves and WM_TIMER is only delivered when the queue drains,
# so a timer-driven repaint would freeze the slider until the drag ends
# (exactly the "thumb does not move until I stop" bug this fixes).
OVERVIEW_TRACK_INTERVAL = 0.030

# Width of the grey vertical separator line at the very left of the
# overview panel (full panel height, buttons included). Separates the
# overview from the editor / the editor's scrollbar.
SEP_LINE_WIDTH = 1
SEP_LINE_COLOR = 0x808080  # grey — visible on both light and dark themes

# Height of the ▲/▼ scroll button boxes at the top and bottom of the
# overview panel (like a scrollbar's arrow buttons).
BUTTON_HEIGHT = 16
# Font used for the ▲/▼ glyphs (falls back to the platform default when
# unavailable; a solid triangle is drawn when even that lacks the glyph).
BUTTON_FONT_NAME = 'Segoe UI'
BUTTON_FONT_SIZE = 9

# Auto-repeat behaviour of the ▲/▼ buttons, mimicking Windows scrollbar
# arrow buttons: the first click scrolls one line; if the button is kept
# pressed, after this initial delay the scrolling repeats at the repeat
# interval until the button is released.
BUTTON_INITIAL_DELAY_MS = 400
BUTTON_REPEAT_MS = 50


class PaintboxOverview:
    """Manages a docked image overview panel for a compare tab.

    The overview shows a gap-aware mini-map of both editors side by side.
    Created once per compare tab, destroyed when the tab closes.
    """

    def __init__(self):
        """Initialize the overview with empty state and default colors."""
        self.h_dlg = None       # dialog handle
        self.h_image = None     # image control handle
        self.h_bitmap = None    # image's embedded bitmap handle
        self.h_canvas = None    # image's embedded bitmap canvas handle
        self._ctl_index = None  # control index in the dialog
        self._owns_dlg = False  # True if we created a separate dialog
        # Static bitmap (persistent — only repainted on compare/resize).
        # Stores the colored diff segments, the scroll buttons and the
        # separator line so they don't need to be repainted on every
        # scroll. See paint() for how it's used.
        self._h_static_bmp = None
        self._h_static_cnv = None
        self._static_w = 0
        self._static_h = 0
        # Line states per side: {line: color}. Filled by add_line_state()
        # while the compare events are painted.
        self.line_states_a = {}
        self.line_states_b = {}
        # Gap info: list of (after_line, gap_visual_rows, ignored) per gap
        self.gaps_a = []  # list of (after_line, gap_visual_rows, ignored)
        self.gaps_b = []
        # Per-line visual row counts (for wrap-aware height computation).
        # If None, each line is 1 visual row. Set via set_wrap_counts().
        self.wrap_counts_a = None  # list where [i] = visual rows for line i
        self.wrap_counts_b = None
        # Editor references and line counts
        self.a_ed = None
        self.b_ed = None
        self.a_line_count = 0
        self.b_line_count = 0
        # Colors (overridden by set_colors() with theme + config values)
        self.color_bg = 0xFFFFFF       # overridden by set_colors() with theme bg
        self.color_deleted = 0xAAAAAA  # overridden by set_colors()
        self.color_added = 0xAAAAAA    # overridden by set_colors()
        self.color_changed = 0xAAAAAA  # overridden by set_colors()
        self.color_gap = 0xEEEEEE      # overridden by set_colors()
        self.color_ignored_gap = 0xEEEEEE  # overridden by set_colors()
        # ▲/▼ button colors (theme ScrollBack / ScrollArrow), refreshed by
        # set_colors() from PROC_THEME_UI_DICT_GET.
        self.color_btn_bg = 0xE0E0E0
        self.color_btn_arrow = 0x000000
        # Slider state for drag-to-scroll
        self._slider_top = 0
        self._slider_height = 0
        self._overview_height = 0
        self._track_y0 = 0
        self._track_h = 0
        self._dragging = False
        self._drag_offset = 0
        self._smooth_max = 0
        # Wall-clock timestamp of the last track_paint(); gates the
        # immediate slider repaints to OVERVIEW_TRACK_INTERVAL.
        self._track_last_paint = 0.0
        # Wall-clock timestamp of the last synchronous editor repaint
        # (EDACTION_UPDATE pair) issued from a slider drag — keeps the
        # heavy full repaints of big-file editors at ~33 fps during the
        # drag while set_prop keeps the scroll position itself exact.
        self._editor_update_last = 0.0
        # Re-entrancy guard for the message pump (_repaint_control_now):
        # True while app_proc(PROC_IDLE) dispatches pending messages so
        # a nested pump can never recurse.
        self._pumping = False

        # --- ▲/▼ button auto-repeat state ---
        # _btn_dir: None while no button is held, -1 while ▲ is held,
        # +1 while ▼ is held. Drives the one-line scroll + auto-repeat.
        self._btn_dir = None

        # Slider fill color (solid fill — one CANVAS_RECT call, no
        # transparency, no per-row overhead).
        self._slider_fill = 0xEAEAEA
        # Border color. Darker than the fill so the border stays clearly
        # visible against any background (light theme or dark theme).
        self._slider_border = 0x666666
        # Grabber line color (3 horizontal lines in the slider middle).
        # Lighter than the border (0x999999 vs 0x666666) so the grabber
        # is visually distinct from the border — the border frames the
        # slider while the grabber sits inside it as a lighter accent.
        self._slider_grabber_dark = 0x999999
        # Grabber geometry: 3 lines, 2px thick, 6px apart (center-to-center).
        # Triple the original 2px spacing; 2x the original 1px thickness.
        self._slider_grabber_thickness = 2
        self._slider_grabber_spacing = 6
        # Min slider height in pixels — keeps the slider grabbable even
        # when the file is much taller than the viewport.
        self._slider_min_height = 30

    def is_created(self):
        """Return True if the overview dialog has been created."""
        return self.h_dlg is not None

    def create(self, a_ed, b_ed):
        """Create the overview as a separate dialog docked to the right
        side of the editor's parent form (PROP_HANDLE_PARENT).

        Uses PROP_HANDLE_PARENT — the handle of the (borderless) dialog
        parenting the editor. As of recent CudaText builds, an additional
        grouping panel-control parents EdFirst/EdSecond/Splitter, so
        PROP_HANDLE_PARENT now returns a stable handle suitable for
        docking. Earlier versions of this plugin used PROP_HANDLE_PARENT2
        (parent-of-parent) because PROP_HANDLE_PARENT used to be the
        splitter itself, which caused the overview to jump to the middle
        when side panels (like the Tabs sidebar) were toggled. With the
        new grouping panel in place, PROP_HANDLE_PARENT2 is no longer
        needed and is also not compatible with dlg_proc() (it returns
        the raw TFrame Lazarus handle). The CudaText author confirmed:
        "PROP_HANDLE_PARENT is enough for docked micromap, PROP_HANDLE_PARENT2
        is not needed anymore."

        Args:
            a_ed: left editor (primary)
            b_ed: right editor (secondary)
        """
        self.a_ed = a_ed
        self.b_ed = b_ed
        # PROP_HANDLE_PARENT: handle of the (borderless) dialog parenting
        # the editor. Since CudaText added the grouping panel for
        # EdFirst/EdSecond/Splitter, this returns a stable handle suitable
        # for DLG_DOCK. PROP_HANDLE_PARENT2 is no longer used.
        h_parent = a_ed.get_prop(ct.PROP_HANDLE_PARENT)
        if not h_parent:
            h_parent = 0

        # Create a separate dialog for the overview
        self.h_dlg = ct.dlg_proc(0, ct.DLG_CREATE)
        self._owns_dlg = True
        ct.dlg_proc(self.h_dlg, ct.DLG_PROP_SET, prop={
            'cap': 'Overview',
            'w': OVERVIEW_WIDTH,
            'h': 600,
            'border': ct.DBORDER_NONE,
            'color': self.color_bg,
            # No on_resize/on_show/on_act handlers needed — the 'image'
            # control handles resize/minimize/restore automatically via
            # its embedded bitmap.
        })

        # Add 'image' control (replaces deprecated 'paintbox'). The image
        # control has an embedded bitmap that handles resize/minimize/
        # restore automatically — no flicker, no manual resize handling.
        self._ctl_index = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_ADD, 'image')
        ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=self._ctl_index, prop={
            'name': 'overview_image',
            'align': ct.ALIGN_CLIENT,
            'on_mouse_down': self._on_mouse_down,
            'on_mouse_move': self._on_mouse_move,
            'on_mouse_up': self._on_mouse_up,
            # Without mouse capture, a button released OUTSIDE the control
            # never delivers on_mouse_up — stop the ▲/▼ auto-repeat (and
            # any drag) when the pointer leaves the panel instead.
            'on_mouse_exit': self._on_mouse_exit,
        })

        # Get the image control handle and its embedded bitmap + canvas
        self.h_image = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_HANDLE, index=self._ctl_index)
        self.h_bitmap = ct.image_proc(self.h_image, ct.IMAGE_GET_BITMAP)
        self.h_canvas = ct.bitmap_proc(self.h_bitmap, ct.BITMAP_GET_CANVAS)

        # Dock to the RIGHT side of the editor's parent-of-parent form
        ct.dlg_proc(self.h_dlg, ct.DLG_SHOW_NONMODAL)
        ct.dlg_proc(self.h_dlg, ct.DLG_DOCK, prop='R', index=h_parent)

    def destroy(self):
        """Undock and free the overview dialog and static bitmap."""
        self._stop_button_repeat()
        self._free_static_bitmap()
        if self.h_dlg is not None:
            try:
                if self._owns_dlg:
                    ct.dlg_proc(self.h_dlg, ct.DLG_UNDOCK)
                    ct.dlg_proc(self.h_dlg, ct.DLG_FREE)
                else:
                    ct.dlg_proc(self.h_dlg, ct.DLG_CTL_DELETE, index=self._ctl_index)
            except Exception:
                pass
            self.h_dlg = None
            self.h_image = None
            self.h_bitmap = None
            self.h_canvas = None
            self._ctl_index = None

    def _free_static_bitmap(self):
        """Free the persistent static bitmap if it exists."""
        if self._h_static_bmp is not None:
            try:
                ct.bitmap_proc(self._h_static_bmp, ct.BITMAP_FREE)
            except Exception:
                pass
            self._h_static_bmp = None
            self._h_static_cnv = None

    def _ensure_static_bitmap(self, w, h):
        """Create or resize the persistent static bitmap to match (w, h).

        The static bitmap stores the colored diff segments, the scroll
        buttons and the separator line (the expensive part). It's only
        repainted here when the size changes or when repaint_static() is
        called after a fresh compare.

        Once created, the static bitmap is reused on every paint() call
        — paint() just copies it to the image's embedded bitmap and draws
        the slider on top, which is very fast.
        """
        if self._h_static_bmp is not None:
            if self._static_w == w and self._static_h == h:
                return  # still valid, reuse
            self._free_static_bitmap()
        self._h_static_bmp = ct.bitmap_proc(0, ct.BITMAP_CREATE, w, h)
        self._h_static_cnv = ct.bitmap_proc(self._h_static_bmp, ct.BITMAP_GET_CANVAS)
        self._static_w = w
        self._static_h = h
        self._paint_static(self._h_static_cnv, w, h)

    def set_colors(self, color_bg, color_deleted, color_added, color_changed,
                   color_gap, color_ignored_gap=None):
        """Set the colors used for painting the overview.

        Also refreshes the ▲/▼ button colors from the current UI theme
        (ScrollBack for the box background, ScrollArrow for the glyph) —
        called on every compare start, so a theme switch is picked up by
        the next compare.

        Args:
            color_bg: background color (theme EdTextBg)
            color_deleted: color for deleted lines (config color_deleted)
            color_added: color for added lines (config color_added)
            color_changed: color for changed lines (config color_changed)
            color_gap: color for gap rectangles (config color_gaps)
            color_ignored_gap: color for the gap rectangles that compensate
                DIFF_IGN_BLANK_LINES-suppressed ("ignored") differences
                (config color_ignored_gap). Optional: when omitted, ignored
                gaps fall back to color_gap (pre-extension behavior).
        """
        self.color_bg = color_bg
        self.color_deleted = color_deleted
        self.color_added = color_added
        self.color_changed = color_changed
        self.color_gap = color_gap
        if color_ignored_gap is not None:
            self.color_ignored_gap = color_ignored_gap
        # ▲/▼ button colors from the UI theme (same source the editor's
        # own scrollbars use). Fall back to neutral values if the theme
        # dict cannot be read.
        try:
            ui = ct.app_proc(ct.PROC_THEME_UI_DICT_GET, '')
            self.color_btn_bg = ui.get('ScrollBack', {}).get('color', 0xE0E0E0)
            self.color_btn_arrow = ui.get('ScrollArrow', {}).get('color', 0x000000)
        except Exception:
            self.color_btn_bg = 0xE0E0E0
            self.color_btn_arrow = 0x000000

    def set_line_counts(self, a_count, b_count):
        """Set the total line counts for both editors (without gaps)."""
        self.a_line_count = a_count
        self.b_line_count = b_count

    def set_wrap_counts(self, wrap_a, wrap_b):
        """Set per-line visual row counts for wrap-aware height computation.

        Args:
            wrap_a: list where wrap_a[i] = visual rows for line i in a_ed,
                    or None if wrapping is off (each line = 1 row).
            wrap_b: same for b_ed.
        """
        self.wrap_counts_a = wrap_a
        self.wrap_counts_b = wrap_b

    def add_line_state(self, side, line, color):
        """Record that a line has a specific color (deleted/added/changed).

        Args:
            side: 'a' or 'b'
            line: line index (0-based)
            color: RGB int color
        """
        if side == 'a':
            self.line_states_a[line] = color
        else:
            self.line_states_b[line] = color

    def add_gap(self, side, after_line, gap_visual_rows, ignored=False):
        """Record a gap inserted after a line.

        Args:
            side: 'a' or 'b'
            after_line: the line index after which the gap was inserted
            gap_visual_rows: number of visual rows the gap occupies
            ignored: True for gaps that compensate a suppressed all-blank
                hunk (DIFF_IGN_BLANK_LINES "ignored differences") -- they
                are painted with color_ignored_gap instead of color_gap.
        """
        if side == 'a':
            self.gaps_a.append((after_line, gap_visual_rows, bool(ignored)))
        else:
            self.gaps_b.append((after_line, gap_visual_rows, bool(ignored)))

    def clear_data(self):
        """Clear all collected line states and gaps. Called before a
        fresh compare."""
        self.line_states_a.clear()
        self.line_states_b.clear()
        self.gaps_a.clear()
        self.gaps_b.clear()

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    def _track_rect(self, h):
        """(y, height) of the slider track — the map area between the
        ▲/▼ button boxes. The slider and the mini-maps live here."""
        track_h = h - 2 * BUTTON_HEIGHT
        if track_h < 1:
            # Degenerate tiny panel: no room for buttons, use everything.
            return 0, max(1, h)
        return BUTTON_HEIGHT, track_h

    def _get_size(self):
        """Get the current image control size (w, h) in pixels."""
        props = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_GET, index=self._ctl_index)
        w = props.get('w', OVERVIEW_WIDTH)
        h = props.get('h', 600)
        return w, h

    # ------------------------------------------------------------------
    # Static painting (WinMerge "Location Pane" model)
    # ------------------------------------------------------------------

    def _paint_static(self, c, w, h):
        """Paint the static part (background, diff segments, buttons,
        separator) on the given canvas. Only runs on compare/resize.

        The heavy work is the per-side segment painting
        (_paint_side_segments), which uses the WinMerge Location Pane
        approach: lines are converted to pixel segments once (prefix
        sums), consecutive same-colored lines are coalesced into runs,
        and every segment that would collapse onto already-painted pixels
        is skipped — so the canvas call count is bounded by the panel's
        pixel height, not by the file's line/diff count.

        Args:
            c: canvas handle (static bitmap canvas)
            w: width in pixels
            h: height in pixels
        """
        # Clear background
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_bg, style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=0, y=0, x2=w, y2=h)

        # Map area: between the buttons, right of the separator line.
        track_y0, track_h = self._track_rect(h)
        x_start = SEP_LINE_WIDTH
        half_w = (w - SEP_LINE_WIDTH) // 2
        if track_h > 0:
            self._paint_side_segments(c, 'a', x_start, x_start + half_w,
                                      track_y0, track_h)
            self._paint_side_segments(c, 'b', x_start + half_w, w,
                                      track_y0, track_h)

        # ▲/▼ buttons and the separator line on top
        self._paint_buttons(c, w, h)
        self._paint_separator(c, w, h)

    def _prefix_sums(self, side):
        """One-pass conversion of a side's line/gap layout to visual rows.

        Returns (cum, total, gap_map):
          cum[i]     visual row where line i starts (all gaps above it
                     included); cum[n] = end of the last line (trailing
                     gaps excluded);
          total      total visual rows (lines + all gaps, trailing ones
                     included);
          gap_map    position -> [total_rows, ignored_rows]: a gap
                     "after line L" sits before line L+1, so its position
                     is L+1 clamped to n (positions >= n are trailing
                     gaps after the last line); several gaps at the same
                     position accumulate.

        Built with itertools.accumulate (C speed) — a 1M-line side is
        one pass instead of the per-line Python loops the old code ran
        (separate O(n) walks in _compute_visual_height, _get_scale and
        the paint loop itself).
        """
        if side == 'a':
            n, gaps, wrap = self.a_line_count, self.gaps_a, self.wrap_counts_a
        else:
            n, gaps, wrap = self.b_line_count, self.gaps_b, self.wrap_counts_b

        gap_map = {}
        for after, rows, ign in gaps:
            # Gap "after line L" sits before line L+1. Clamp to [0, n]:
            # L >= n is a trailing gap (after the last line), and a
            # defensive L < 0 is treated as "before line 0" so an
            # out-of-range position can never write through a negative
            # list index below.
            if after < 0:
                pos = 0
            elif after > n:
                pos = n
            else:
                pos = after
            ent = gap_map.setdefault(pos, [0, 0])
            ent[0] += rows
            if ign:
                ent[1] += rows

        # Gaps that shift line starts (positions < n); gaps at n are
        # trailing and only extend the total height.
        gap_shift = [0] * (n + 1)
        trailing = 0
        for pos, ent in gap_map.items():
            if pos < n:
                gap_shift[pos] += ent[0]
            else:
                trailing += ent[0]

        # cum[i] = (visual rows of lines 0..i-1) + (gap rows at
        # positions <= i) — the accumulated gap shift is aligned with the
        # line prefix by construction. Shortcut when no gaps shift
        # anything: cum is just the line prefix itself (range object,
        # no 1M-element list allocation).
        if wrap is not None and len(wrap) >= n:
            line_prefix = accumulate(wrap[:n], initial=0)
        else:
            line_prefix = range(n + 1)
        has_shifting_gaps = any(ent[0] for pos, ent in gap_map.items()
                                if pos < n)
        if has_shifting_gaps:
            gap_acc = accumulate(gap_shift)
            cum = [lp + ga for lp, ga in zip(line_prefix, gap_acc)]
        elif wrap is not None and len(wrap) >= n:
            cum = list(line_prefix)
        else:
            cum = range(n + 1)
        total = cum[n] + trailing
        return cum, total, gap_map

    def _build_segments(self, side, cum=None, gap_map=None):
        """Build the colored segments of one side, in visual order.

        Returns a list of (start_row, end_row, color) tuples:
          - line runs: consecutive lines with the same color, merged
            when they are visually adjacent (no gap between them) —
            one rect instead of per-line rects;
          - gaps: one rect per gap position, split into its ignored
            portion (if any) and its regular portion.

        The list is ascending by start_row (paint order).

        cum / gap_map may be passed in (from a _prefix_sums call the
        caller already made) or computed here when omitted.
        """
        if side == 'a':
            n, wrap = self.a_line_count, self.wrap_counts_a
            states = self.line_states_a
        else:
            n, wrap = self.b_line_count, self.wrap_counts_b
            states = self.line_states_b
        if cum is None or gap_map is None:
            cum, _total, gap_map = self._prefix_sums(side)

        def line_rows(i):
            if wrap is not None and 0 <= i < len(wrap) and wrap[i] > 0:
                return wrap[i]
            return 1

        # Line runs: consecutive same-colored lines merge while they are
        # visually adjacent (line == prev+1 AND no gap before this line).
        runs = []
        prev_line = -2
        for line, color in sorted(states.items()):
            if line < 0 or line >= n:
                continue
            end_row = cum[line] + line_rows(line)
            if (runs and line == prev_line + 1
                    and line not in gap_map
                    and runs[-1][2] == color):
                runs[-1][1] = end_row  # extend the current run
            else:
                runs.append([cum[line], end_row, color])
            prev_line = line

        # Gap segments (positions in ascending order). The ignored
        # portion of a gap (a DIFF_IGN_BLANK_LINES-suppressed hunk's
        # compensating rows) is emitted as its OWN segment covering the
        # top of the rect, and the regular gap color covers the rest —
        # two non-overlapping segments instead of a paint-on-top
        # overlay, so the dedup rule can never discard the ignored
        # color of a fully-ignored gap (same final look as the old
        # base-then-overlay layering).
        gap_segs = []
        for pos in sorted(gap_map):
            rows_total, rows_ign = gap_map[pos]
            if rows_total <= 0:
                continue
            if pos < n:
                start = cum[pos] - rows_total
            else:
                start = cum[n]  # trailing: right after the last line
            ign_rows = min(rows_ign, rows_total)
            if ign_rows > 0:
                gap_segs.append((start, start + ign_rows,
                                 self.color_ignored_gap))
            if ign_rows < rows_total:
                gap_segs.append((start + ign_rows, start + rows_total,
                                 self.color_gap))

        # Merge both ascending streams by start row; a gap before line i
        # ends where line i starts, so ties cannot happen between a run
        # and a gap. The sort is stable, keeping a gap's split portions
        # in their emitted (ignored, then regular) order.
        segments = gap_segs + [tuple(r) for r in runs]
        segments.sort(key=lambda seg: seg[0])
        return segments

    def _paint_side_segments(self, c, side, x_start, x_end, y0, track_h):
        """Paint one side's colored segments into the track area.

        The WinMerge Location Pane draw loop: convert each segment to
        pixels once, then SKIP every segment whose end pixel equals the
        previous segment's end pixel — it would only repaint pixels that
        are already covered ("we cannot write to half a pixel"). A
        segment that collapses to zero height is bumped to 1 pixel so
        the first sub-pixel diff of a region stays visible. The number
        of CANVAS_RECT_FILL calls is therefore bounded by the track
        height in pixels, not by the line/diff count.
        """
        cum, total, gap_map = self._prefix_sums(side)
        if total <= 0 or track_h <= 0:
            return
        segments = self._build_segments(side, cum, gap_map)
        if not segments:
            return
        scale = track_h / total

        prev_end = -1     # raw end pixel of the previous segment (WinMerge's nPrevEndY)
        last_color = None
        for start_row, end_row, color in segments:
            ps = y0 + int(start_row * scale)
            pe = y0 + int(end_row * scale)
            if pe == prev_end:
                # Collapses onto pixels the previous segment already
                # covered — useless write, skip it.
                continue
            if pe <= ps:
                pe = ps + 1  # sub-pixel segment: draw at least one pixel
            if color != last_color:
                ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=color,
                               style=ct.BRUSH_SOLID)
                last_color = color
            ct.canvas_proc(c, ct.CANVAS_RECT_FILL,
                           x=x_start, y=ps, x2=x_end, y2=pe)
            prev_end = y0 + int(end_row * scale)

    def _paint_buttons(self, c, w, h):
        """Paint the ▲/▼ scroll button boxes at the top and bottom of
        the panel.

        Background: the theme's ScrollBack color; the arrow glyph: the
        theme's ScrollArrow color, centered both vertically and
        horizontally. No borders on the boxes, like the scrollbar arrow
        buttons of usual editors and browsers.
        """
        if h <= 2 * BUTTON_HEIGHT:
            return  # degenerate tiny panel: buttons don't fit
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_btn_bg,
                       style=ct.BRUSH_SOLID)
        for y0 in (0, h - BUTTON_HEIGHT):
            ct.canvas_proc(c, ct.CANVAS_RECT_FILL,
                           x=0, y=y0, x2=w, y2=y0 + BUTTON_HEIGHT)
        self._paint_arrow(c, 0, 0, w, BUTTON_HEIGHT, up=True)
        self._paint_arrow(c, 0, h - BUTTON_HEIGHT, w, BUTTON_HEIGHT, up=False)

    def _paint_arrow(self, c, bx, by, bw, bh, up):
        """Draw one ▲/▼ arrow centered in its button box.

        Draws the ▲ (up) / ▼ (down) glyph with the theme's ScrollArrow
        color, centered vertically and horizontally. When the canvas
        font cannot render the glyph (measured size 0), a solid triangle
        polygon is drawn instead — same shape, resolution-independent.
        """
        glyph = '▲' if up else '▼'
        ct.canvas_proc(c, ct.CANVAS_SET_FONT, text=BUTTON_FONT_NAME,
                       color=self.color_btn_arrow, size=BUTTON_FONT_SIZE)
        size = ct.canvas_proc(c, ct.CANVAS_GET_TEXT_SIZE, text=glyph)
        try:
            tw, th = size
        except (TypeError, ValueError):
            tw = th = 0
        if tw > 0 and th > 0:
            ct.canvas_proc(c, ct.CANVAS_TEXT, text=glyph,
                           x=bx + (bw - tw) // 2, y=by + (bh - th) // 2)
        else:
            # Fallback: solid triangle polygon.
            cx = bx + bw // 2
            cy = by + bh // 2
            r = max(2, min(bw, bh) // 4)
            if up:
                pts = (cx, cy - r, cx + r, cy + r, cx - r, cy + r)
            else:
                pts = (cx, cy + r, cx + r, cy - r, cx - r, cy - r)
            ct.canvas_proc(c, ct.CANVAS_SET_PEN,
                           color=self.color_btn_arrow, size=1)
            ct.canvas_proc(c, ct.CANVAS_SET_BRUSH,
                           color=self.color_btn_arrow, style=ct.BRUSH_SOLID)
            ct.canvas_proc(c, ct.CANVAS_POLYGON, text=pts)

    def _paint_separator(self, c, w, h):
        """Paint the grey vertical separator line at the very left of
        the overview panel (x = 0..SEP_LINE_WIDTH), spanning the FULL
        panel height — the ▲/▼ button boxes included — so the overview
        is visually separated from the editor / the editor's scrollbar
        by the same line all the way down."""
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=SEP_LINE_COLOR,
                       style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT_FILL,
                       x=0, y=0, x2=SEP_LINE_WIDTH, y2=h)

    def repaint_static(self):
        """Force a full repaint of the static bitmap. Called after a
        fresh compare or when colors change.

        Frees the old static bitmap and creates a new one with fresh
        content, then calls paint() to display it with the dynamic part.
        """
        if self.h_canvas is None:
            return
        w, h = self._get_size()
        if w <= 0 or h <= 0:
            return
        self._free_static_bitmap()
        self._ensure_static_bitmap(w, h)
        self.paint()

    def paint(self):
        """Repaint the overview using the static/dynamic bitmap approach.

        The static bitmap (diff segments + buttons + separator) is
        reused — it's only repainted when the diff changes (via
        repaint_static). On scroll, this method just:
        1. Ensures the static bitmap exists and matches current size.
        2. Resizes the image's embedded bitmap to match (if needed).
        3. Copies the static bitmap to the image's embedded bitmap via
           CANVAS_BITMAP (fast — one bitmap copy).
        4. Draws the dynamic part (slider + grabber) on top.

        This avoids the expensive segment loop on every scroll. The
        image control's embedded bitmap handles resize/minimize/restore
        automatically, so no on_act/on_resize/on_show handlers are
        needed.
        """
        if self.h_canvas is None:
            return

        w, h = self._get_size()
        if w <= 0 or h <= 0:
            return

        # Ensure static bitmap exists and matches current size
        self._ensure_static_bitmap(w, h)

        # Resize the image's embedded bitmap to match the control size.
        # The image control starts with a 0x0 bitmap; we must resize it
        # before painting on it, otherwise nothing shows (checkerboard pattern).
        ct.bitmap_proc(self.h_bitmap, ct.BITMAP_SET_SIZE, w, h)

        # Copy static bitmap to the image's embedded bitmap.
        # CudaText API change (recent versions): CANVAS_BITMAP now takes
        # the source bitmap handle as the integer param "p1", NOT as the
        # string param "text" (the change was to avoid str-int
        # conversions). The old form `text=str(handle)` still works on
        # older builds but is deprecated and may be removed. Using p1
        # works on both old and new builds.
        ct.canvas_proc(self.h_canvas, ct.CANVAS_BITMAP,
                       p1=self._h_static_bmp, x=0, y=0)

        # Draw dynamic part (slider) on top
        self._paint_dynamic(w, h)

    def _paint_dynamic(self, w, h):
        """Draw the dynamic part: the scrollbar slider.

        Uses pure pixel-based mapping (no line/gap calculations):
        - Get the editor's total content height (smooth_max), current
          scroll position (smooth_pos), and visible page (smooth_page)
          from PROP_SCROLL_VERT_INFO.
        - Map to the track area between the ▲/▼ buttons:
            slider_height = track_h * smooth_page / smooth_max
                            (proportional, clamped to min 30px so it
                            stays grabbable, and to the track height)
            slider_top    = track_y0 + track_h * smooth_pos / smooth_max
        - Draw via _paint_slider_solid: one CANVAS_RECT call (pen border
          + solid brush fill) + the grabber lines — no transparency
          blending, so zero overhead per paint.

        The slider_top formula is mathematically consistent: when
        smooth_pos reaches its max (smooth_max - smooth_page),
        py_top = track_y0 + track_h - slider_height, so the slider lands
        flush at the bottom of the track. No special-casing needed for
        the bottom edge.

        Args:
            w: width in pixels
            h: height in pixels
        """
        if self.a_ed is None:
            return

        c = self.h_canvas

        # Get pixel-based scroll info from editor a_ed.
        scroll_info = self.a_ed.get_prop(ct.PROP_SCROLL_VERT_INFO)
        if not scroll_info:
            return

        smooth_pos = scroll_info.get('smooth_pos', 0)
        smooth_max = scroll_info.get('smooth_max', 1)
        smooth_page = scroll_info.get('smooth_page', 0)

        if smooth_max <= 0:
            smooth_max = 1

        track_y0, track_h = self._track_rect(h)

        # --- Compute proportional slider height (like a real scrollbar) ---
        # smooth_max is the total content height in pixels (includes the
        # page size, per CudaText API docs). smooth_page is the visible
        # viewport height. slider_height proportional to page/total,
        # clamped to [min_height, track_h] so the slider always fits the
        # track and stays grabbable. Both values use round-half-up so
        # the slider lands EXACTLY flush at the track bottom when the
        # scroll position reaches its max (plain int() truncation would
        # leave a 1px gap: int(t*p/m) + int(t*p/m) != t).
        min_h = self._slider_min_height
        if smooth_page > 0:
            py_height = int(track_h * smooth_page / smooth_max + 0.5)
            py_height = max(min_h, min(track_h, py_height))
        else:
            # No page info (e.g., very early init) — fall back to min.
            py_height = min(min_h, track_h)

        # Map editor scroll position to track pixels:
        # slider_top = track_top + track_h * editor_scroll / editor_total
        py_top = track_y0 + int(track_h * smooth_pos / smooth_max + 0.5)

        # Clamp slider within the track
        py_top = max(track_y0, min(py_top, track_y0 + track_h - py_height))

        # Solid slider: one CANVAS_RECT call (pen border + brush fill).
        # No transparency blending — zero per-row overhead.
        self._paint_slider_solid(c, w, py_top, py_height)

        # Store slider geometry and scroll info for click/drag handling
        self._slider_top = py_top
        self._slider_height = py_height
        self._overview_height = h
        self._track_y0 = track_y0
        self._track_h = track_h
        self._smooth_max = smooth_max

    def _paint_slider_solid(self, c, w, py_top, py_height):
        """Solid-fill slider: opaque fill + border + grabber.

        The only slider-paint method: one CANVAS_RECT call draws both
        the fill (brush) and the border (pen) in one shot, then the
        grabber lines — minimal work per paint, no transparency, no
        per-row blending loop, nothing to configure.
        """
        ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=self._slider_border, size=1)
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self._slider_fill, style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT, x=SEP_LINE_WIDTH, y=py_top, x2=w - 1, y2=py_top + py_height)
        self._paint_slider_grabber(c, w, py_top, py_height)

    def _paint_slider_grabber(self, c, w, py_top, py_height):
        """Draw the 3 horizontal grabber lines centered in the slider.

        Pattern (matches modern scrollbar look — WinMerge, VS Code, etc.):
          - 3 horizontal lines, evenly spaced around the slider's vertical
            center (mid_y - spacing, mid_y, mid_y + spacing)
          - Each line is 2px thick (pen size = 2) so it stays clearly
            visible against the fill
          - Lines are 6px apart center-to-center (triple the original
            2px spacing), giving a clean modern look with proper
            visual padding from the slider's top/bottom border
          - Single dark grey color (no white bevel) for a flat modern
            appearance instead of the old 3D bevelled look
        """
        mid_y = py_top + py_height // 2
        grab_x1 = max(2 + SEP_LINE_WIDTH, w // 4)
        grab_x2 = min(w - 3, w * 3 // 4)
        spacing = self._slider_grabber_spacing  # 6px center-to-center
        thickness = self._slider_grabber_thickness  # 2px per line

        # 3 dark grabber lines, 2px thick, 6px apart (center-to-center).
        # No white bevel — flat modern look.
        ct.canvas_proc(c, ct.CANVAS_SET_PEN,
                       color=self._slider_grabber_dark, size=thickness)
        ct.canvas_proc(c, ct.CANVAS_LINE,
                       x=grab_x1, y=mid_y - spacing, x2=grab_x2, y2=mid_y - spacing)
        ct.canvas_proc(c, ct.CANVAS_LINE,
                       x=grab_x1, y=mid_y,             x2=grab_x2, y2=mid_y)
        ct.canvas_proc(c, ct.CANVAS_LINE,
                       x=grab_x1, y=mid_y + spacing, x2=grab_x2, y2=mid_y + spacing)

    # ------------------------------------------------------------------
    # Mouse handling: slider drag, track jump, ▲/▼ buttons
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_mouse(data, info):
        """Extract (x, y) from a mouse-event callback's arguments.

        data is the dict {'btn', 'state', 'x', 'y'} the live-callback
        form delivers; info is the legacy 'x,y' string form. Returns
        (None, None) when neither carries usable coordinates.
        """
        if isinstance(data, dict):
            return data.get('x', 0), data.get('y', 0)
        if info:
            try:
                parts = info.split(',')
                return int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                return None, None
        return None, None

    def _on_mouse_down(self, id_dlg, id_ctl, data='', info=''):
        """Called on mouse down. Route the press by the panel layout:
        ▲/▼ button boxes scroll one line (with auto-repeat while held);
        inside the track, a press on the slider starts a drag, a press
        elsewhere jumps the slider there (centered on the click).

        Runs even while _pumping is True: a click dispatched by the
        message pump must not be swallowed, or rapid clicks during the
        ▲/▼ auto-repeat repaints get lost (dead clicks). All work here
        is synchronous; the nested _repaint_control_now() calls guard
        themselves."""
        x, y = self._parse_mouse(data, info)
        if x is None:
            return
        w, h = self._get_size()
        if h <= 0:
            return

        # ▲/▼ button boxes (top / bottom).
        if y < BUTTON_HEIGHT and h > 2 * BUTTON_HEIGHT:
            self._start_button_repeat(-1)
            return
        if y >= h - BUTTON_HEIGHT and h > 2 * BUTTON_HEIGHT:
            self._start_button_repeat(1)
            return

        # Track: slider drag or centered jump.
        if self._slider_top <= y <= self._slider_top + self._slider_height:
            # Click inside slider — start drag
            self._dragging = True
            self._drag_offset = y - self._slider_top
        else:
            # Click outside slider — jump (centered on click)
            self._dragging = False
            self._scroll_overview_pixel(y, center=True)
            # Immediate slider update (bypassing the throttle): the
            # thumb must land on the clicked position right away --
            # the 150ms debounce timer fires much later, or not at all
            # if the click turns into a drag.
            self.track_paint(force=True)
            self._repaint_control_now()

    @staticmethod
    def _left_button_held(data):
        """True / False when the event's data says whether the LEFT mouse
        button is held; None when the data carries no button state.

        CudaText encodes the held buttons in data['state']: 'L' = left,
        'R' = right, 'M' = middle (proc_miscutils.pas
        ConvertShiftStateToString). The legacy 'x,y' info-string form
        carries no button info — None (unknown) so callers cannot
        mistake it for "button up".
        """
        if isinstance(data, dict) and 'state' in data:
            return 'L' in data['state']
        return None

    def _on_mouse_move(self, id_dlg, id_ctl, data='', info=''):
        """Called on mouse move. If dragging, scroll the editor to follow
        and repaint the slider so its thumb tracks the mouse like a
        normal scrollbar (throttled to ~33 fps -- see track_paint), then
        force the on-screen control repaint so the thumb actually MOVES
        with the mouse instead of freezing until the drag stops (see
        _repaint_control_now).

        Also SELF-HEALS a lost release: a move arriving without the
        left button held (data['state'] lacks 'L') while a drag or a
        ▲/▼ auto-repeat is running means the button is physically up
        and the mouse-up was lost somewhere (any event path we did not
        foresee) — end the interaction right here instead of letting
        the slider keep following a mouse that presses nothing.
        """
        if self._pumping:
            return
        x, y = self._parse_mouse(data, info)
        if x is None:
            return
        if (self._dragging or self._btn_dir is not None) and \
                self._left_button_held(data) is False:
            # Left button is physically up but the release never
            # reached us -- treat this move as the release.
            self._stop_button_repeat()
            was_dragging = self._dragging
            self._dragging = False
            if was_dragging:
                self.track_paint(force=True)
                self._repaint_control_now()
            return
        if not self._dragging:
            return
        # Drag: the slider top follows the mouse (accounting for offset)
        target_y = y - self._drag_offset
        self._scroll_overview_pixel(target_y, center=False)
        # Immediate (throttled) slider repaint so the thumb moves with
        # the mouse. A timer cannot do this during a drag -- see
        # track_paint for why (WM_TIMER starvation under the flooded
        # message queue).
        self.track_paint()
        self._repaint_control_now()

    def _on_mouse_up(self, id_dlg, id_ctl, data='', info=''):
        """Called on mouse up. Stops the ▲/▼ auto-repeat and dragging,
        then does one final slider repaint with the throttle bypassed,
        so the thumb lands exactly where the drag ended (the throttled
        repaints during the drag may have skipped the very last
        position).

        CRITICAL: the release is processed EVEN WHEN _pumping is True.
        _repaint_control_now() pumps the message queue, and the pump
        dispatches pending mouse messages — a WM_LBUTTONUP pending at
        that moment re-enters this handler with _pumping set. The old
        `if self._pumping: return` swallowed that release, and the
        drag then kept following the mouse / the ▲/▼ auto-repeat kept
        scrolling forever after the button was no longer held (the
        "sometimes the mouse is not released" bug — timing-dependent,
        because the up is only lost when it is queued exactly during
        a pump). _repaint_control_now() guards itself against nested
        pumps, and the pump that dispatched this very event delivers
        the pending WM_PAINT when it returns, so skipping the explicit
        control repaint while pumping loses nothing."""
        self._stop_button_repeat()
        was_dragging = self._dragging
        self._dragging = False
        if was_dragging:
            self.track_paint(force=True)
            self._repaint_control_now()

    def _on_mouse_exit(self, id_dlg, id_ctl, data='', info=''):
        """Called when the pointer leaves the overview panel. On
        platforms where the press is not captured (a button released
        outside the control never delivers on_mouse_up), this is the
        only signal that interaction ended — stop the ▲/▼ auto-repeat
        and any running drag here.

        Like _on_mouse_up, this runs even while _pumping is True: a
        stop event must never be swallowed by the pump."""
        self._stop_button_repeat()
        if self._dragging:
            self._dragging = False
            self.track_paint(force=True)

    # ------------------------------------------------------------------
    # ▲/▼ button auto-repeat (like scrollbar arrow buttons)
    # ------------------------------------------------------------------

    def _start_button_repeat(self, direction):
        """Begin a ▲/▼ button press: scroll one line immediately, then
        arm the initial-delay one-shot timer which starts the repeating
        timer for the auto-scroll (like holding a usual scrollbar's
        arrow button: one line per click, continuous scrolling after a
        short hold)."""
        self._stop_button_repeat()
        self._btn_dir = direction
        self._scroll_one_line(direction)
        ct.timer_proc(ct.TIMER_START_ONE, self._btn_delay_tick,
                      BUTTON_INITIAL_DELAY_MS)

    def _stop_button_repeat(self):
        """Stop the ▲/▼ auto-repeat (mouse released / left the panel /
        dialog destroyed). Disables both timers; the one-shot delay
        timer may already have fired and deleted itself — stopping a
        missing timer is a harmless no-op."""
        if self._btn_dir is None:
            return
        self._btn_dir = None
        ct.timer_proc(ct.TIMER_STOP, self._btn_delay_tick,
                      BUTTON_INITIAL_DELAY_MS)
        ct.timer_proc(ct.TIMER_STOP, self._btn_repeat_tick,
                      BUTTON_REPEAT_MS)

    def _btn_delay_tick(self, tag=''):
        """One-shot timer callback fired BUTTON_INITIAL_DELAY_MS after
        the button went down: the button is still held, so switch to
        the repeating auto-scroll timer."""
        if self._btn_dir is None:
            return
        ct.timer_proc(ct.TIMER_START, self._btn_repeat_tick,
                      BUTTON_REPEAT_MS)

    def _btn_repeat_tick(self, tag=''):
        """Repeating timer callback: the button is still held — scroll
        one more line (stops itself via _stop_button_repeat when the
        mouse is released or leaves the panel)."""
        if self._btn_dir is None:
            return
        self._scroll_one_line(self._btn_dir)

    def _scroll_one_line(self, direction):
        """Scroll both editors one line up (direction -1) or down
        (+1), like a scrollbar's arrow button. One line = the editor's
        char_size (row height in pixels) of smooth scrolling."""
        if self.a_ed is None:
            return
        scroll_info = self.a_ed.get_prop(ct.PROP_SCROLL_VERT_INFO)
        if not scroll_info:
            return
        char_size = scroll_info.get('char_size', 0)
        if char_size <= 0:
            return
        target = max(0, scroll_info.get('smooth_pos', 0) + direction * char_size)
        for e in (self.a_ed, self.b_ed):
            if e is not None:
                try:
                    e.set_prop(ct.PROP_SCROLL_VERT_INFO,
                               {'smooth_pos': target})
                    e.action(ct.EDACTION_UPDATE)
                except Exception:
                    pass
        self.track_paint(force=True)
        self._repaint_control_now()

    # ------------------------------------------------------------------
    # Repaint scheduling
    # ------------------------------------------------------------------

    def track_paint(self, force=False):
        """Repaint the overview immediately, throttled by WALL CLOCK to
        OVERVIEW_TRACK_INTERVAL (~33 fps).

        Used while the slider is dragged (mouse-move handler) and on
        every scroll event (plugin on_scroll) so the slider tracks the
        live position instead of waiting for the 150ms debounce timer.
        That timer is useless during a drag: mouse moves flood the
        message queue and WM_TIMER is only delivered when the queue
        drains, so a timer-debounced slider appears FROZEN until the
        drag stops.

        paint() is cheap on this path (one cached-bitmap copy + the
        slider drawing -- the expensive static segments never run),
        so ~33 repaints per second cost little CPU; the wall-clock gate
        keeps the rate bounded no matter how fast the mouse moves or
        how densely scroll events arrive.

        Args:
            force: bypass the throttle (final repaint on mouse-up and
                after a click jump, where landing on the exact position
                matters more than the rate limit).
        """
        now = time.monotonic()
        if not force and now - self._track_last_paint < OVERVIEW_TRACK_INTERVAL:
            return
        self._track_last_paint = now
        self.paint()

    def _repaint_control_now(self):
        """Force the on-screen image control to repaint IMMEDIATELY.

        paint() draws into the image's embedded bitmap, but the control
        itself only repaints when a WM_PAINT is delivered — and WM_PAINT
        is generated only while the message queue is EMPTY. During a
        slider drag the queue is continuously fed with mouse moves, so
        the thumb would stay frozen on screen (while the bitmap in
        memory is already up to date) until the drag stops.

        Pumping the queue once here (app_proc(PROC_IDLE) —
        Application.ProcessMessages + one idle pass) delivers the
        pending WM_PAINT right away, so the thumb moves with the mouse
        like in usual scrollbars.

        Re-entrancy: the pump also dispatches pending INPUT messages,
        which re-enter the mouse handlers. The _pumping flag makes the
        nested handlers behave correctly: mouse MOVES return at once
        (the newest position is picked up by the next event after the
        pump returns, and dropping them breaks the move→pump→move
        recursion), while mouse UP / EXIT / DOWN are still fully
        processed — a release dispatched by the pump must never be
        swallowed, or the drag / ▲/▼ auto-repeat would keep running
        with the button no longer held (the "mouse is not released"
        bug). _repaint_control_now itself can never nest.
        """
        if self._pumping or self.h_dlg is None:
            return
        self._pumping = True
        try:
            ct.app_proc(ct.PROC_IDLE, 'false')
        except Exception:
            pass
        finally:
            self._pumping = False

    # ------------------------------------------------------------------
    # Scroll mapping
    # ------------------------------------------------------------------

    def _scroll_overview_pixel(self, overview_y, center=True):
        """Scroll the editors based on a pixel Y position in the overview.

        Pure pixel-based mapping onto the TRACK area (between the ▲/▼
        buttons):
          editor_target_scroll = smooth_max * (y - track_y0) / track_h

        If center=True, the clicked position becomes the center of the
        viewport (the target is pulled up by half the slider height,
        which corresponds to half the visible page).
        If center=False (dragging), the position becomes the slider top.

        Args:
            overview_y: pixel Y position in the overview
            center: if True, center the viewport on Y. If False, Y
                    becomes the top of the viewport (for dragging).
        """
        h = self._overview_height
        if h <= 0:
            w, h = self._get_size()
        if h <= 0:
            return
        track_y0, track_h = self._track_rect(h)
        if track_h <= 0:
            return

        ty = overview_y - track_y0
        if center:
            ty -= self._slider_height // 2

        smooth_max = self._smooth_max
        if smooth_max <= 0:
            # Get it from the editor
            scroll_info = self.a_ed.get_prop(ct.PROP_SCROLL_VERT_INFO) if self.a_ed else None
            if scroll_info:
                smooth_max = scroll_info.get('smooth_max', 0)
            if smooth_max <= 0:
                return
            self._smooth_max = smooth_max

        # Map overview track pixel to editor scroll pixel:
        # editor_scroll = smooth_max * overview_y / track_h
        target_smooth_pos = int(smooth_max * ty / track_h)

        # Clamp to valid range
        target_smooth_pos = max(0, target_smooth_pos)

        # Scroll both editors via set_prop(PROP_SCROLL_VERT_INFO, {'smooth_pos': ...})
        for e in (self.a_ed, self.b_ed):
            if e is not None:
                try:
                    e.set_prop(ct.PROP_SCROLL_VERT_INFO,
                               {'smooth_pos': target_smooth_pos})
                except Exception:
                    pass

        # Paint BOTH halves SYNCHRONOUSLY, inside this one callback.
        # set_prop alone leaves each half's repaint to its own
        # invalidation -- subject to anti-flicker timers, to the
        # paint-order quirks of two sibling controls, and to pending
        # input messages (a slider drag floods the queue with mouse
        # moves, and WM_PAINT is delivered only when the queue drains)
        # -- which can put the halves into different display frames:
        # one half visibly scrolls a few milliseconds before the other
        # catches up. ed.action(EDACTION_UPDATE) maps to Ed.Repaint =
        # Invalidate + LCL Update, so each half paints RIGHT HERE,
        # back-to-back, before this mouse-move handler returns: both
        # are on screen in the same frame, atomically. (The older
        # cmd_RepaintEditor approach was only a forced INVALIDATE --
        # still asynchronous, still frame-split under a busy queue.)
        # The synchronous paints fire on_scroll echoes; the positions
        # already match (both halves were just written), so the
        # ScrollSplittedTab mirror no-ops and the cascade dies out.
        # During a drag the repaint pair is wall-clock throttled to
        # ~33 fps: a full repaint of a big-file editor is expensive,
        # and the exact positions are re-asserted by every set_prop
        # anyway, so the editors still land exactly where the mouse
        # says when the queue drains.
        if center:
            # Single click jump: always paint now.
            for e in (self.a_ed, self.b_ed):
                if e is not None:
                    try:
                        e.action(ct.EDACTION_UPDATE)
                    except Exception:
                        pass
        else:
            now = time.monotonic()
            if now - self._editor_update_last >= OVERVIEW_TRACK_INTERVAL:
                self._editor_update_last = now
                for e in (self.a_ed, self.b_ed):
                    if e is not None:
                        try:
                            e.action(ct.EDACTION_UPDATE)
                        except Exception:
                            pass
