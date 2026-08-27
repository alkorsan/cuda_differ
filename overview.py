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
      stores the colored line/gap rectangles. Only repainted on compare/resize
      via repaint_static(). This is the expensive part (hundreds of
      CANVAS_RECT_FILL calls).
    * Dynamic: on each paint(), copy the static bitmap to the image's
      embedded bitmap via CANVAS_BITMAP, then draw the slider and
      grabber on top. This is cheap (one bitmap copy + a few
      CANVAS_RECT / CANVAS_LINE calls).
  - On scroll (debounced 150ms): paint() is called. It only copies the
    static bitmap and draws the dynamic part — the static bitmap is reused.
    This avoids repainting hundreds of rectangles on every scroll.
  - On compare/resize: repaint_static() is called first, which frees the
    old static bitmap and creates a new one with fresh content. Then
    paint() copies it + draws the dynamic part.

  - PROPORTIONAL SLIDER HEIGHT:
    Like real scrollbars in browsers/editors, the slider grows/shrinks
    based on the visible-page vs total-content ratio. CudaText reports:
      smooth_max     = total content height (pixels, INCLUDES page)
      smooth_page    = visible viewport height (pixels)
      smooth_pos     = current scroll position (pixels)
    So slider_height = h * smooth_page / smooth_max, clamped to a 30px
    minimum so the slider stays grabbable. When the entire file fits in
    the viewport, slider_height == h (full track). When the file is huge,
    slider_height shrinks toward 30px. The slider_top formula
    `py_top = h * smooth_pos / smooth_max` is unchanged — mathematically,
    when smooth_pos reaches its max (smooth_max - smooth_page), py_top
    becomes h - slider_height, so the slider lands flush at the bottom.

  - SLIDER OPACITY (three paint methods, dispatched by _paint_dynamic):
    CudaText canvas_proc has NO alpha-blend primitive (only BRUSH_SOLID =
    opaque, BRUSH_CLEAR = no fill). Three methods are provided so users
    can trade off looks vs performance:

    1. SOLID (opt_slider_opacity_enabled = False):
       Original method. One CANVAS_RECT call (pen + solid brush). Fastest.

    2. CLEAR (opt_slider_opacity_enabled = True AND opacity < 8%):
       Border-only slider via BRUSH_CLEAR. One CANVAS_RECT call with no
       fill. Fastest transparent option — used for very low opacity.

    3. BLENDED (opt_slider_opacity_enabled = True AND opacity >= 8%):
       Per-row pre-blend. A per-row segment index (_row_segments) is built
       in _paint_static(), recording the final color of every Y row of
       the static bitmap (background / gap / line-state). On each paint(),
       _paint_dynamic() walks the slider rows (py_height of them) and
       fills each segment with `blend(orig_color, slider_fill, alpha)` —
       per-channel `orig*(1-α) + fill*α`. This produces a pixel-exact
       simulation of true alpha blending. Cost: ~2 * py_height
       CANVAS_RECT_FILL calls per scroll. The slider border + grabber
       lines are drawn on top, unchanged.

  - SLIDER GRABBER PATTERN:
    3 horizontal dark-grey lines, each 2px thick, spaced 6px apart
    (center-to-center) vertically centered in the slider. Triple the
    spacing of the old 1px-thick 2px-apart 6-line bevel pattern, with
    the white bevel lines removed for a cleaner modern scrollbar look
    (matches WinMerge / modern editor sliders). The 2px thickness makes
    the lines clearly visible against any fill (solid, clear, or
    pre-blended).

  See: https://github.com/CudaText-addons/cuda_differ/issues/29
"""

import cudatext as ct
from .profiling import Profiler

# Overview dialog width in pixels (docked to the right)
OVERVIEW_WIDTH = 40
# Default width of the overview panel docked to the right of the compare
# view. Was 80px; reduced to 40px (50% smaller) — the overview shows
# both files side-by-side at half-width each, which still gives enough
# resolution to spot colored diff blocks while taking less horizontal
# space. The panel is docked and resizable, so users can drag it wider
# if they want more detail.


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
        # Stores the colored line/gap rectangles so they don't need to be
        # repainted on every scroll. See paint() for how it's used.
        self._h_static_bmp = None
        self._h_static_cnv = None
        self._static_w = 0
        self._static_h = 0
        # Line states: {('a', line): color, ('b', line): color}
        self.line_states = {}
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
        # Slider state for drag-to-scroll
        self._slider_top = 0
        self._slider_height = 0
        self._overview_height = 0
        self._dragging = False
        self._drag_offset = 0
        self._smooth_max = 0

        # --- Slider opacity options (user-configurable) ---
        # opt_slider_opacity_enabled: if False, use the OLD solid-fill
        #   slider (fast, no blending). If True, use either BRUSH_CLEAR
        #   (opacity < 8%) or the pre-blend method (opacity >= 8%).
        # opt_slider_opacity: 0.0 = invisible, 1.0 = fully opaque.
        #   Default 0.4 = 40% opaque (WinMerge-style).
        #   See _paint_dynamic() for the dispatch logic.
        # Threshold below which we use BRUSH_CLEAR instead of pre-blend
        # (BRUSH_CLEAR is faster — no per-row blending loop).
        self.opt_slider_opacity_enabled = True
        self.opt_slider_opacity = 0.4
        self._slider_clear_threshold = 0.08  # <8% → BRUSH_CLEAR

        # Slider fill color used by SOLID, BLENDED, and CLEAR (border only).
        # Light grey, matching the original solid-fill slider.
        self._slider_fill = 0xEAEAEA
        # Border color, shared by all three methods. Darker than the old
        # 0x999999 so the border stays clearly visible against any
        # background (light theme, dark theme, or pre-blended fill).
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

        # Per-row segment index: list of length h, where each row is a
        # list of (x_start, x_end, color) tuples in paint order (later
        # entries visually overwrite earlier ones). Rebuilt only in
        # _paint_static(); consumed by _paint_dynamic(). Empty until the
        # first static repaint completes. Only used by the BLENDED method.
        self._row_segments = []

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
            'on_click': self._on_click,
            'on_mouse_down': self._on_mouse_down,
            'on_mouse_move': self._on_mouse_move,
            'on_mouse_up': self._on_mouse_up,
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

        The static bitmap stores the colored line/gap rectangles (the
        expensive part that takes hundreds of CANVAS_RECT_FILL calls).
        It's only repainted here when the size changes or when
        repaint_static() is called after a fresh compare.

        Once created, the static bitmap is reused on every paint() call
        — paint() just copies it to the image's embedded bitmap and draws
        the cursor/viewport on top, which is very fast.
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

    def set_slider_options(self, opacity_enabled=None, opacity=None):
        """Configure the overview slider's transparency behaviour.

        Two user-facing options:
          - differ.micromap.enable_overview_slider_opacity
                (bool, default True):
                False -> use the OLD solid-fill slider (fast, no blending).
                True  -> use BRUSH_CLEAR if opacity < 8%, else the pre-blend
                         method (per-row simulated alpha blend).
          - differ.micromap.overview_slider_opacity
                (int 0..100 in the JSON, passed here as float 0..1,
                default 0.4):
                Slider opacity. 0 = invisible (BRUSH_CLEAR), 1 = opaque.
                Only used when opacity_enabled is True.

        Args:
            opacity_enabled: bool or None. None = leave unchanged.
            opacity: float in [0.0, 1.0], or None = leave unchanged.
        """
        if opacity_enabled is not None:
            self.opt_slider_opacity_enabled = bool(opacity_enabled)
        if opacity is not None:
            self.opt_slider_opacity = max(0.0, min(1.0, float(opacity)))

    def set_colors(self, color_bg, color_deleted, color_added, color_changed,
                   color_gap, color_ignored_gap=None):
        """Set the colors used for painting the overview.

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

    def _line_visual_rows(self, side, line):
        """Return the number of visual rows a line occupies (1 if no
        wrapping, or wrap_counts[i] if wrapping is on)."""
        if side == 'a':
            wc = self.wrap_counts_a
        else:
            wc = self.wrap_counts_b
        if wc is None or line < 0 or line >= len(wc):
            return 1
        return max(1, wc[line])

    def add_line_state(self, side, line, color):
        """Record that a line has a specific color (deleted/added/changed).

        Args:
            side: 'a' or 'b'
            line: line index (0-based)
            color: RGB int color
        """
        self.line_states[(side, line)] = color

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
        self.line_states.clear()
        self.gaps_a.clear()
        self.gaps_b.clear()

    def _compute_visual_height(self, side):
        """Compute the total visual height (lines + gaps) for one side.

        Each line's visual height is its wrap count (1 if no wrapping),
        plus gap rows. This ensures both sides have equal visual heights
        when the diff is correct.

        Returns: int — total visual rows for the given side.
        """
        if side == 'a':
            line_count = self.a_line_count
            gaps = self.gaps_a
        else:
            line_count = self.b_line_count
            gaps = self.gaps_b
        # Sum line visual rows (wrap-aware)
        total = 0
        for line in range(line_count):
            total += self._line_visual_rows(side, line)
        # Add gap rows
        for _, gap_rows, _ign in gaps:
            total += gap_rows
        return max(total, 1)

    def _sorted_gaps(self, side):
        """Return gaps for the given side, sorted by after_line.

        Gaps are collected in event order (which may not be sorted),
        so we sort them before using in position calculations.
        """
        gaps = self.gaps_a if side == 'a' else self.gaps_b
        return sorted(gaps, key=lambda g: g[0])

    def _line_to_visual_y(self, side, line):
        """Map a line index to its visual Y position (in visual rows),
        accounting for both gaps and wrapping.

        Args:
            side: 'a' or 'b'
            line: line index (0-based)

        Returns: float — visual Y position in visual-row units.
        """
        gaps = self._sorted_gaps(side)
        # Sum visual rows of all lines before this one (wrap-aware)
        y = 0
        for i in range(line):
            y += self._line_visual_rows(side, i)
        # Add gap rows for gaps at or before this line
        for after_line, gap_rows, _ign in gaps:
            if after_line <= line:
                y += gap_rows
            else:
                break
        return y

    def _visual_y_to_line_wrap_aware(self, side, visual_y):
        """Map a visual Y position (in visual rows) back to a line index,
        accounting for BOTH gaps AND wrapping.

        Walks lines in order, subtracting each line's wrap count and
        gap rows from the visual Y until we reach the target line.

        Args:
            side: 'a' or 'b'
            visual_y: visual Y position in visual-row units

        Returns: int — line index (0-based).
        """
        if side == 'a':
            line_count = self.a_line_count
        else:
            line_count = self.b_line_count
        gaps = self._sorted_gaps(side)
        gap_map = {}
        for after_line, gap_rows, _ign in gaps:
            gap_map[after_line] = gap_map.get(after_line, 0) + gap_rows

        remaining = visual_y
        for line in range(line_count):
            # Subtract gap before this line
            if line in gap_map:
                remaining -= gap_map[line]
                if remaining < 0:
                    return max(0, line - 1)
            # Subtract this line's visual rows (wrap-aware)
            line_vr = self._line_visual_rows(side, line)
            remaining -= line_vr
            if remaining < 0:
                return line
        return max(0, line_count - 1)

    def _get_scale(self, h):
        """Compute the pixel-per-visual-row scale factor.

        Both sides use the same scale (based on the taller side) so that
        visually-aligned lines in the editors are also aligned in the
        overview.

        Args:
            h: pixel height of the image control

        Returns: float — pixels per visual row.
        """
        vis_h_a = self._compute_visual_height('a')
        vis_h_b = self._compute_visual_height('b')
        max_vis_h = max(vis_h_a, vis_h_b)
        return h / max_vis_h if max_vis_h > 0 else 1

    def _get_size(self):
        """Get the current image control size (w, h) in pixels."""
        props = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_GET, index=self._ctl_index)
        w = props.get('w', OVERVIEW_WIDTH)
        h = props.get('h', 600)
        return w, h

    def _paint_static(self, c, w, h):
        """Paint the static part (background, line states, gaps) on the
        given canvas. This is the expensive part that only needs to run
        on compare/resize, not on scroll.

        The static bitmap stores the result of this method so it can be
        reused on every paint() call without re-executing the expensive
        CANVAS_RECT_FILL loop.

        Also builds _row_segments — the per-row segment index used by
        _paint_dynamic() to precompute transparent-slider blends. Each
        row starts as a full-width background segment; _paint_side()
        appends line/gap segments on top in paint order so the last
        color wins (matching the on-screen visual).

        Args:
            c: canvas handle (static bitmap canvas)
            w: width in pixels
            h: height in pixels
        """
        # Clear background
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_bg, style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=0, y=0, x2=w, y2=h)

        # (Re)build the per-row segment index — start with full-width
        # background for every row. _paint_side() will append layers on
        # top via _record_segment().
        self._row_segments = [[(0, w, self.color_bg)] for _ in range(h)]
        # Also record the background itself for completeness.
        self._record_segment(0, 0, w, h, self.color_bg)

        # Paint both sides
        scale = self._get_scale(h)
        half_w = w // 2
        self._paint_side(c, 'a', 0, half_w, h, scale)
        self._paint_side(c, 'b', half_w, w, h, scale)

    def _record_segment(self, x1, y1, x2, y2, color):
        """Record a colored rectangle in the per-row segment index.

        Called from _paint_side() for every CANVAS_RECT_FILL it issues.
        Each affected row gets (x1, x2, color) appended to its segment
        list — later appends visually overwrite earlier ones (matching
        the on-screen paint order). Used by _paint_dynamic() to know
        the exact final color of every Y row, so the transparent
        slider can be filled with pre-blended colors.

        Args:
            x1, y1, x2, y2: rectangle in pixels (y1 inclusive, y2 exclusive)
            color: RGB int color of the rectangle
        """
        if not self._row_segments:
            return
        h = len(self._row_segments)
        row_start = max(0, y1)
        row_end = min(h, y2)
        for row in range(row_start, row_end):
            self._row_segments[row].append((x1, x2, color))

    @staticmethod
    def _blend_color(orig, fill, alpha):
        """Per-channel alpha blend: result = orig*(1-α) + fill*α.

        Works for any slider color (light or dark). Returns an RGB int.
        CudaText colors are 0xRRGGBB stored as 0xBBGGRR (little-endian
        BGR int), so we mask channels accordingly.

        Args:
            orig: underlying pixel color (BGR int, as used by canvas_proc)
            fill: slider fill color (BGR int)
            alpha: slider opacity in [0.0, 1.0]; 0 = invisible, 1 = opaque
        """
        inv = 1.0 - alpha
        r1 = orig & 0xFF
        g1 = (orig >> 8) & 0xFF
        b1 = (orig >> 16) & 0xFF
        r2 = fill & 0xFF
        g2 = (fill >> 8) & 0xFF
        b2 = (fill >> 16) & 0xFF
        r = int(r1 * inv + r2 * alpha)
        g = int(g1 * inv + g2 * alpha)
        b = int(b1 * inv + b2 * alpha)
        return r | (g << 8) | (b << 16)

    def _paint_gap_rect(self, c, x_start, x_end, py, scale, rows_total,
                        rows_ignored):
        """Paint one inter-line gap rectangle.

        Regular rows are painted with color_gap; ignored rows (gaps that
        compensate a DIFF_IGN_BLANK_LINES-suppressed hunk) are painted
        with color_ignored_gap on top, stacked inside the same rect.
        When a regular and an ignored gap share a position their heights
        add up, and the ignored portion is drawn at the top of the rect.
        Both segments are recorded for the transparent-slider blending.
        """
        gap_h = max(1, int(rows_total * scale))
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_gap,
                       style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=x_start, y=py,
                       x2=x_end, y2=py + gap_h)
        self._record_segment(x_start, py, x_end, py + gap_h,
                             self.color_gap)
        if rows_ignored > 0:
            ign_h = min(gap_h, max(1, int(rows_ignored * scale)))
            ct.canvas_proc(c, ct.CANVAS_SET_BRUSH,
                           color=self.color_ignored_gap,
                           style=ct.BRUSH_SOLID)
            ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=x_start, y=py,
                           x2=x_end, y2=py + ign_h)
            self._record_segment(x_start, py, x_end, py + ign_h,
                                 self.color_ignored_gap)

    def _paint_side(self, c, side, x_start, x_end, h, scale):
        """Paint one side of the overview (either a_ed or b_ed half).

        Walks the lines in order, painting each line as a 1-pixel-tall
        colored rectangle. Gaps are painted as rectangles in the regular
        gap color; gaps that compensate ignored (suppressed) differences
        are painted in the ignored-gap color (see _paint_gap_rect). The visual
        position accounts for gaps so the overview stays in sync with
        the actual editor layout.

        Args:
            c: canvas handle
            side: 'a' or 'b'
            x_start: left X pixel of this side's area
            x_end: right X pixel of this side's area
            h: total pixel height
            scale: pixels per visual row
        """
        if side == 'a':
            line_count = self.a_line_count
            gaps = self._sorted_gaps('a')
        else:
            line_count = self.b_line_count
            gaps = self._sorted_gaps('b')

        # Build a gap map: after_line -> [total gap rows, ignored rows]
        # at that position. A gap with after_line == N means it appears
        # between line N-1 and line N (i.e., BEFORE line N in visual
        # order). Several gaps (regular + ignored) can share a position;
        # their heights add up and the ignored portion is painted on
        # top (see _paint_gap_rect).
        gap_map = {}
        for after_line, gap_rows, gap_ign in gaps:
            ent = gap_map.setdefault(after_line, [0, 0])
            ent[0] += gap_rows
            if gap_ign:
                ent[1] += gap_rows

        vis_y = 0

        # Paint gaps and lines in order
        for line in range(line_count):
            # Paint gap before this line (if any).
            # Gap with after_line == line means: between line-1 and line.
            if line in gap_map:
                rows_total, rows_ign = gap_map[line]
                py = int(vis_y * scale)
                self._paint_gap_rect(c, x_start, x_end, py, scale,
                                     rows_total, rows_ign)
                vis_y += rows_total

            # Paint the line (only if it has a state — changed lines)
            color = self.line_states.get((side, line))
            if color is not None:
                py = int(vis_y * scale)
                # Use wrap-aware line height
                line_vr = self._line_visual_rows(side, line)
                line_h = max(1, int(line_vr * scale) + 1)
                ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=color, style=ct.BRUSH_SOLID)
                ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=x_start, y=py, x2=x_end, y2=py + line_h)
                # Record segment for transparent-slider blending
                self._record_segment(x_start, py, x_end, py + line_h, color)
            vis_y += self._line_visual_rows(side, line)

        # Paint any remaining gaps after the last line
        for after_line, gap_rows, gap_ign in gaps:
            if after_line >= line_count:
                py = int(vis_y * scale)
                self._paint_gap_rect(c, x_start, x_end, py, scale,
                                     gap_rows, 1 if gap_ign else 0)
                vis_y += gap_rows

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

        The static bitmap (line states + gaps) is reused — it's only
        repainted when the diff changes (via repaint_static). On scroll,
        this method just:
        1. Ensures the static bitmap exists and matches current size.
        2. Resizes the image's embedded bitmap to match (if needed).
        3. Copies the static bitmap to the image's embedded bitmap via
           CANVAS_BITMAP (fast — one bitmap copy).
        4. Draws the dynamic part (slider + grabber)
           on top of the image's bitmap (a few CANVAS_RECT / CANVAS_LINE
           calls).

        This avoids the expensive CANVAS_RECT_FILL loop on every scroll,
        dramatically reducing CPU usage. The image control's embedded
        bitmap handles resize/minimize/restore automatically, so no
        on_act/on_resize/on_show handlers are needed.
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

        # Draw dynamic part (cursor + viewport) on top
        self._paint_dynamic(w, h)

    def _paint_dynamic(self, w, h):
        """Draw the dynamic part: the scrollbar slider.

        Uses pure pixel-based mapping (no line/gap calculations):
        - Get the editor's total content height (smooth_max), current
          scroll position (smooth_pos), and visible page (smooth_page)
          from PROP_SCROLL_VERT_INFO.
        - Map to overview pixels:
            slider_height = h * smooth_page / smooth_max (proportional,
                            clamped to min 30px so it stays grabbable)
            slider_top    = h * smooth_pos / smooth_max
        - Dispatch to one of three slider-paint methods based on options:
            * opt_slider_opacity_enabled == False  → _paint_slider_solid
            * opacity < 8%                          → _paint_slider_clear
            * opacity >= 8%                         → _paint_slider_blended

        The slider_top formula is mathematically consistent: when
        smooth_pos reaches its max (smooth_max - smooth_page),
        py_top = h * (smooth_max - smooth_page) / smooth_max =
        h - slider_height, so the slider lands flush at the bottom of
        the track. No special-casing needed for the bottom edge.

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

        # --- Compute proportional slider height (like a real scrollbar) ---
        # smooth_max is the total content height in pixels (includes the
        # page size, per CudaText API docs). smooth_page is the visible
        # viewport height. slider_height proportional to page/total,
        # clamped to [min_height, h] so the slider always fits the track
        # and stays grabbable.
        min_h = self._slider_min_height
        if smooth_page > 0:
            py_height = int(h * smooth_page / smooth_max)
            py_height = max(min_h, min(h, py_height))
        else:
            # No page info (e.g., very early init) — fall back to min.
            py_height = min(min_h, h)

        # Map editor scroll position to overview pixels:
        # slider_top = overview_height * editor_scroll / editor_total
        py_top = int(h * smooth_pos / smooth_max)

        # Clamp slider within the overview
        py_top = max(0, min(py_top, h - py_height))

        # --- Dispatch to the selected slider-paint method ---
        if not self.opt_slider_opacity_enabled:
            # Method 1 (OLD): solid fill — fastest, no transparency.
            self._paint_slider_solid(c, w, py_top, py_height)
        elif self.opt_slider_opacity < self._slider_clear_threshold:
            # Method 2: border-only via BRUSH_CLEAR. Used for very low
            # opacity (0..7%) because pre-blending at near-zero alpha
            # would be wasteful — the visual difference is invisible.
            self._paint_slider_clear(c, w, py_top, py_height)
        else:
            # Method 3 (NEW): per-row pre-blend — true simulated alpha.
            self._paint_slider_blended(c, w, py_top, py_height)

        # Store slider geometry and scroll info for click/drag handling
        self._slider_top = py_top
        self._slider_height = py_height
        self._overview_height = h
        self._smooth_max = smooth_max

    def _paint_slider_solid(self, c, w, py_top, py_height):
        """OLD slider: opaque solid fill + border + grabber. Fastest method.

        Used when opt_slider_opacity_enabled is False (the user explicitly
        disabled the transparent look). One CANVAS_RECT call draws both
        the fill (brush) and border (pen) in one shot.
        """
        ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=self._slider_border, size=1)
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self._slider_fill, style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT, x=0, y=py_top, x2=w - 1, y2=py_top + py_height)
        self._paint_slider_grabber(c, w, py_top, py_height)

    def _paint_slider_clear(self, c, w, py_top, py_height):
        """Border-only slider via BRUSH_CLEAR (no fill). Fastest transparent
        option — used when opacity is 0%..7%.

        The static bitmap's colors show through the slider rectangle
        completely, just outlined by the pen border. Equivalent to the
        "empty rectangle" suggestion from the CudaText author, but only
        used at very low opacity where the pre-blend method would be
        wasteful (the alpha-blended fill would be visually indistinguishable
        from no fill).
        """
        ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=self._slider_border, size=1)
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self._slider_fill, style=ct.BRUSH_CLEAR)
        ct.canvas_proc(c, ct.CANVAS_RECT, x=0, y=py_top, x2=w - 1, y2=py_top + py_height)
        self._paint_slider_grabber(c, w, py_top, py_height)

    def _paint_slider_blended(self, c, w, py_top, py_height):
        """NEW slider: per-row pre-blend + border + grabber. Simulates
        true alpha blending without needing canvas alpha support.

        Walks the slider's py_height rows and fills each segment with
        `blend(orig_color, slider_fill, alpha)` per-channel. The segment
        index is built in _paint_static() and records the final color
        of every Y row of the static bitmap (background / gap / line).

        Cost: ~2 * py_height CANVAS_RECT_FILL calls per paint. For a
        30px slider that's ~60 calls; for a 100px slider (large viewport)
        ~200 calls. All negligible vs the static repaint's hundreds.

        Falls back to solid fill if the segment index isn't built yet
        (e.g., before the first compare completes).
        """
        alpha = self.opt_slider_opacity
        fill_color = self._slider_fill

        if self._row_segments and alpha > 0.0:
            # Pre-blend per row. Walk all py_height rows of the slider.
            row_count = len(self._row_segments)
            for i in range(py_height):
                row = py_top + i
                if 0 <= row < row_count:
                    for x1, x2, col in self._row_segments[row]:
                        blended = self._blend_color(col, fill_color, alpha)
                        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH,
                                       color=blended, style=ct.BRUSH_SOLID)
                        ct.canvas_proc(c, ct.CANVAS_RECT_FILL,
                                       x=x1, y=row, x2=x2, y2=row + 1)
        else:
            # No segment index yet (e.g., before first compare) — fall
            # back to a solid fill so the slider is always visible.
            ct.canvas_proc(c, ct.CANVAS_SET_BRUSH,
                           color=fill_color, style=ct.BRUSH_SOLID)
            ct.canvas_proc(c, ct.CANVAS_RECT_FILL,
                           x=0, y=py_top, x2=w, y2=py_top + py_height)

        # Border: use CANVAS_RECT + BRUSH_CLEAR (NOT CANVAS_RECT_FRAME).
        # The SOLID and CLEAR methods both draw the border via CANVAS_RECT
        # (pen + brush). BLENDED must use the SAME call so the border
        # renders identically. With BRUSH_CLEAR, the brush does not fill
        # anything (so the pre-blended fill underneath is preserved), and
        # the pen draws the border on top — same as the other two methods.
        # CANVAS_RECT_FRAME was previously used here, but it renders a
        # thinner frame that becomes nearly invisible against the
        # pre-blended fill.
        ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=self._slider_border, size=1)
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self._slider_fill, style=ct.BRUSH_CLEAR)
        ct.canvas_proc(c, ct.CANVAS_RECT, x=0, y=py_top, x2=w - 1, y2=py_top + py_height)

        self._paint_slider_grabber(c, w, py_top, py_height)

    def _paint_slider_grabber(self, c, w, py_top, py_height):
        """Draw the 3 horizontal grabber lines centered in the slider.

        Pattern (matches modern scrollbar look — WinMerge, VS Code, etc.):
          - 3 horizontal lines, evenly spaced around the slider's vertical
            center (mid_y - spacing, mid_y, mid_y + spacing)
          - Each line is 2px thick (pen size = 2) so it stays clearly
            visible against any fill (solid, clear, or pre-blended)
          - Lines are 6px apart center-to-center (triple the original
            2px spacing), giving a clean modern look with proper
            visual padding from the slider's top/bottom border
          - Single dark grey color (no white bevel) for a flat modern
            appearance instead of the old 3D bevelled look

        Shared by all three slider-paint methods so the visual identity
        of the slider stays consistent regardless of which fill
        strategy is active.
        """
        mid_y = py_top + py_height // 2
        grab_x1 = max(2, w // 4)
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

    def _on_click(self, id_dlg, id_ctl, data='', info=''):
        """Called when the image is clicked. Scrolls the editor so the
        clicked position becomes the center of the slider.

        Pure pixel-based mapping:
          editor_scroll = smooth_max * click_y / overview_height
        Then centers by subtracting half the visible page.
        """
        if not info:
            return
        try:
            parts = info.split(',')
            x = int(parts[0])
            y = int(parts[1])
        except (ValueError, IndexError):
            return

        self._scroll_overview_pixel(y, center=True)

    def _on_mouse_down(self, id_dlg, id_ctl, data='', info=''):
        """Called on mouse down. Supports click-to-scroll and drag-to-scroll.

        If click is inside the slider, start dragging.
        If click is outside, jump to that position (centered).
        """
        if isinstance(data, dict):
            x = data.get('x', 0)
            y = data.get('y', 0)
        elif info:
            try:
                parts = info.split(',')
                x = int(parts[0])
                y = int(parts[1])
            except (ValueError, IndexError):
                return
        else:
            return

        slider_top = getattr(self, '_slider_top', 0)
        slider_height = getattr(self, '_slider_height', 0)

        if slider_top <= y <= slider_top + slider_height:
            # Click inside slider — start drag
            self._dragging = True
            self._drag_offset = y - slider_top
        else:
            # Click outside slider — jump (centered on click)
            self._dragging = False
            self._scroll_overview_pixel(y, center=True)

    def _on_mouse_move(self, id_dlg, id_ctl, data='', info=''):
        """Called on mouse move. If dragging, scroll the editor to follow."""
        if not getattr(self, '_dragging', False):
            return
        if isinstance(data, dict):
            y = data.get('y', 0)
        elif info:
            try:
                y = int(info.split(',')[1])
            except (ValueError, IndexError):
                return
        else:
            return
        # Drag: the slider top follows the mouse (accounting for offset)
        target_y = y - self._drag_offset
        self._scroll_overview_pixel(target_y, center=False)

    def _on_mouse_up(self, id_dlg, id_ctl, data='', info=''):
        """Called on mouse up. Stops dragging."""
        self._dragging = False

    def _scroll_overview_pixel(self, overview_y, center=True):
        """Scroll the editors based on a pixel Y position in the overview.

        Pure pixel-based mapping (no line/gap calculations):
          editor_target_scroll = smooth_max * overview_y / overview_height

        If center=True, subtract half the visible page so the clicked
        position becomes the center of the viewport.
        If center=False (dragging), the clicked position becomes the top.

        Args:
            overview_y: pixel Y position in the overview
            center: if True, center the viewport on Y. If False, Y
                    becomes the top of the viewport (for dragging).
        """
        h = getattr(self, '_overview_height', 0)
        if h <= 0:
            w, h = self._get_size()
        if h <= 0:
            return

        smooth_max = getattr(self, '_smooth_max', 0)
        if smooth_max <= 0:
            # Get it from the editor
            scroll_info = self.a_ed.get_prop(ct.PROP_SCROLL_VERT_INFO) if self.a_ed else None
            if scroll_info:
                smooth_max = scroll_info.get('smooth_max', 0)
            if smooth_max <= 0:
                return

        # Map overview pixel to editor scroll pixel:
        # editor_scroll = smooth_max * overview_y / overview_height
        target_smooth_pos = int(smooth_max * overview_y / h)

        if center:
            # Subtract half the visible page so the click is centered
            scroll_info = self.a_ed.get_prop(ct.PROP_SCROLL_VERT_INFO) if self.a_ed else None
            smooth_page = scroll_info.get('smooth_page', 0) if scroll_info else 0
            target_smooth_pos -= smooth_page // 2

        # Clamp to valid range
        target_smooth_pos = max(0, target_smooth_pos)

        # Scroll both editors via set_prop(PROP_SCROLL_VERT_INFO, {'smooth_pos': ...})
        if self.a_ed is not None:
            try:
                self.a_ed.set_prop(ct.PROP_SCROLL_VERT_INFO, {'smooth_pos': target_smooth_pos})
            except Exception:
                pass
        if self.b_ed is not None:
            try:
                self.b_ed.set_prop(ct.PROP_SCROLL_VERT_INFO, {'smooth_pos': target_smooth_pos})
            except Exception:
                pass
