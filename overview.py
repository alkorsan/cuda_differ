"""Image-based overview panel for the Differ plugin.

Replaces the micromap with a custom image control docked to the right side
of the editor's parent-of-parent form. Unlike the micromap, the overview is
gap-aware: it accounts for the inter-line gaps inserted by Differ for visual
alignment, so the overview stays in sync with what the user actually sees.

Architecture:
  - PaintboxOverview creates a non-modal dialog with an 'image' control.
  - The dialog is docked to the RIGHT side of PROP_HANDLE_PARENT2.
  - The 'image' control has an embedded bitmap that handles resize/minimize/
    restore automatically — no need for on_act/on_resize/on_show handlers.
  - Uses a two-bitmap optimization (see _ensure_static_bitmap / paint):
    * Static bitmap: a separate persistent bitmap (via bitmap_proc) that
      stores the colored line/gap rectangles. Only repainted on compare/resize
      via repaint_static(). This is the expensive part (hundreds of
      CANVAS_RECT_FILL calls).
    * Dynamic: on each paint(), copy the static bitmap to the image's
      embedded bitmap via CANVAS_BITMAP, then draw the cursor marker and
      viewport rectangle on top. This is cheap (one bitmap copy + a few
      CANVAS_LINE / CANVAS_RECT_FRAME calls).
  - On scroll (debounced 150ms): paint() is called. It only copies the
    static bitmap and draws the dynamic part — the static bitmap is reused.
    This avoids repainting hundreds of rectangles on every scroll.
  - On compare/resize: repaint_static() is called first, which frees the
    old static bitmap and creates a new one with fresh content. Then
    paint() copies it + draws the dynamic part.

  - TRANSPARENT SLIDER (WinMerge-style):
    CudaText's canvas_proc API has NO alpha-blend primitive (only
    BRUSH_SOLID = opaque, BRUSH_CLEAR = no fill). To get a 40%-transparent
    slider we precompute the blend: a per-row segment index
    (_row_segments) is built in _paint_static(), recording the final color
    of every Y row of the static bitmap (background / gap / line-state).
    On each paint(), _paint_dynamic() walks the 30 slider rows and fills
    each segment with `blend(orig_color, slider_fill, alpha)` — i.e.
    per-channel `orig*(1-α) + fill*α`. This produces a pixel-exact
    simulation of true alpha blending without needing any canvas alpha
    support, and costs ≤60 CANVAS_RECT_FILL calls per scroll (negligible).
    The slider border + grabber lines are drawn on top, unchanged.

  See: https://github.com/CudaText-addons/cuda_differ/issues/29
"""

import cudatext as ct
from .profiling import Profiler

# Overview dialog width in pixels (docked to the right)
OVERVIEW_WIDTH = 80


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
        # Gap info: list of (after_line, gap_visual_rows) for each gap
        self.gaps_a = []  # list of (after_line, gap_visual_rows)
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
        self.color_cursor = 0x000000   # overridden by set_colors() with theme font color
        # Slider state for drag-to-scroll
        self._slider_top = 0
        self._slider_height = 0
        self._overview_height = 0
        self._dragging = False
        self._drag_offset = 0
        self._smooth_max = 0

        # --- Transparent slider (WinMerge-style) ---
        # CudaText canvas_proc has no alpha primitive. We precompute the
        # blend per channel: result = orig*(1-α) + fill*α.
        # Slider opacity: 0.0 = invisible, 1.0 = fully opaque.
        # 0.6 = 60% opaque / 40% transparent — matches WinMerge's look.
        # Tune freely; no other code needs to change.
        self._slider_alpha = 0.6
        self._slider_fill = 0xEAEAEA  # light grey, same as the old solid fill
        # Per-row segment index: list of length h, where each row is a
        # list of (x_start, x_end, color) tuples in paint order (later
        # entries visually overwrite earlier ones). Rebuilt only in
        # _paint_static(); consumed by _paint_dynamic(). Empty until the
        # first static repaint completes.
        self._row_segments = []

    def is_created(self):
        """Return True if the overview dialog has been created."""
        return self.h_dlg is not None

    def create(self, a_ed, b_ed):
        """Create the overview as a separate dialog docked to the right
        side of the editor's parent-of-parent form.

        Uses PROP_HANDLE_PARENT2 (the parent of the editor's parent) for
        docking. PROP_HANDLE_PARENT (the editor's direct parent) is the
        split container — docking to it places the overview inside the
        split area, causing it to jump to the middle when side panels
        (like the Tabs sidebar) are toggled. PROP_HANDLE_PARENT2 is the
        outer form that holds the split container + side panels + tab
        bar. Docking to it keeps the overview stable regardless of side
        panel state.

        Args:
            a_ed: left editor (primary)
            b_ed: right editor (secondary)
        """
        self.a_ed = a_ed
        self.b_ed = b_ed
        # PROP_HANDLE_PARENT2: parent-of-parent of the editor.
        h_parent = a_ed.get_prop(ct.PROP_HANDLE_PARENT2)
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

    def set_colors(self, color_bg, color_deleted, color_added, color_changed, color_gap, color_cursor=None):
        """Set the colors used for painting the overview.

        Args:
            color_bg: background color (theme EdTextBg)
            color_deleted: color for deleted lines (config color_deleted)
            color_added: color for added lines (config color_added)
            color_changed: color for changed lines (config color_changed)
            color_gap: color for gap rectangles (config color_gaps)
            color_cursor: color for cursor marker (theme EdTextFont).
                          If None, keeps the previous value.
        """
        self.color_bg = color_bg
        self.color_deleted = color_deleted
        self.color_added = color_added
        self.color_changed = color_changed
        self.color_gap = color_gap
        if color_cursor is not None:
            self.color_cursor = color_cursor

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

    def add_gap(self, side, after_line, gap_visual_rows):
        """Record a gap inserted after a line.

        Args:
            side: 'a' or 'b'
            after_line: the line index after which the gap was inserted
            gap_visual_rows: number of visual rows the gap occupies
        """
        if side == 'a':
            self.gaps_a.append((after_line, gap_visual_rows))
        else:
            self.gaps_b.append((after_line, gap_visual_rows))

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
        for _, gap_rows in gaps:
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
        for after_line, gap_rows in gaps:
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
        for after_line, gap_rows in gaps:
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

    def _paint_side(self, c, side, x_start, x_end, h, scale):
        """Paint one side of the overview (either a_ed or b_ed half).

        Walks the lines in order, painting each line as a 1-pixel-tall
        colored rectangle. Gaps are painted as gray rectangles. The visual
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

        # Build a gap map: after_line -> total gap rows at that position.
        # A gap with after_line == N means it appears between line N-1
        # and line N (i.e., BEFORE line N in visual order).
        gap_map = {}
        for after_line, gap_rows in gaps:
            gap_map[after_line] = gap_map.get(after_line, 0) + gap_rows

        vis_y = 0

        # Paint gaps and lines in order
        for line in range(line_count):
            # Paint gap before this line (if any).
            # Gap with after_line == line means: between line-1 and line.
            if line in gap_map:
                gap_rows = gap_map[line]
                gap_h = max(1, int(gap_rows * scale))
                py = int(vis_y * scale)
                ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_gap, style=ct.BRUSH_SOLID)
                ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=x_start, y=py, x2=x_end, y2=py + gap_h)
                # Record segment for transparent-slider blending
                self._record_segment(x_start, py, x_end, py + gap_h, self.color_gap)
                vis_y += gap_rows

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
        for after_line, gap_rows in gaps:
            if after_line >= line_count:
                gap_h = max(1, int(gap_rows * scale))
                py = int(vis_y * scale)
                ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_gap, style=ct.BRUSH_SOLID)
                ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=x_start, y=py, x2=x_end, y2=py + gap_h)
                # Record segment for transparent-slider blending
                self._record_segment(x_start, py, x_end, py + gap_h, self.color_gap)
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
        4. Draws the dynamic part (cursor line + viewport rectangle)
           on top of the image's bitmap (a few CANVAS_LINE / CANVAS_RECT_FRAME
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

        # Copy static bitmap to the image's embedded bitmap
        ct.canvas_proc(self.h_canvas, ct.CANVAS_BITMAP,
                       text=str(self._h_static_bmp), x=0, y=0)

        # Draw dynamic part (cursor + viewport) on top
        self._paint_dynamic(w, h)

    def _paint_dynamic(self, w, h):
        """Draw the dynamic part: a single scrollbar slider with fixed height.

        Uses pure pixel-based mapping (no line/gap calculations):
        - Get the editor's total scrollable height (smooth_max) and
          current scroll position (smooth_pos) from PROP_SCROLL_VERT_INFO.
        - Map to overview pixels: slider_top = h * smooth_pos / smooth_max
        - Slider height is fixed at 30px.

        This is independent of lines, gaps, and wrapping — it purely
        maps editor pixels to overview pixels, like a real scrollbar.

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

        if smooth_max <= 0:
            smooth_max = 1

        # Fixed slider height (30px)
        py_height = 30

        # Map editor scroll position to overview pixels:
        # slider_top = overview_height * editor_scroll / editor_total
        py_top = int(h * smooth_pos / smooth_max)

        # Clamp slider within the overview
        py_top = max(0, min(py_top, h - py_height))

        # --- Draw the scrollbar slider (transparent, WinMerge-style) ---

        # 1. Fill the slider area with PRE-BLENDED colors, row by row.
        #    CudaText canvas_proc has no alpha primitive, so we can't say
        #    "draw this rect at 40% opacity". Instead we walk the per-row
        #    segment index built in _paint_static() — each segment knows
        #    its final color on the static bitmap — and fill it with
        #    `blend(orig, slider_fill, alpha)`. This produces a pixel-exact
        #    simulation of true alpha blending. Cost: ≤60
        #    CANVAS_RECT_FILL calls per scroll (negligible vs the static
        #    repaint's hundreds of fills).
        alpha = self._slider_alpha
        fill_color = self._slider_fill
        if alpha >= 0.999:
            # Fully opaque — use the original solid fill (fast path)
            ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=fill_color, style=ct.BRUSH_SOLID)
            ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=0, y=py_top, x2=w, y2=py_top + py_height)
        elif alpha > 0.001 and self._row_segments:
            # Transparent slider — pre-blend per row
            row_count = len(self._row_segments)
            for i in range(py_height):
                row = py_top + i
                if 0 <= row < row_count:
                    for x1, x2, col in self._row_segments[row]:
                        blended = self._blend_color(col, fill_color, alpha)
                        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=blended, style=ct.BRUSH_SOLID)
                        ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=x1, y=row, x2=x2, y2=row + 1)
        else:
            # alpha ~ 0 OR no segment index yet — fall back to solid fill
            # so the slider is always visible even before first compare.
            ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=fill_color, style=ct.BRUSH_SOLID)
            ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=0, y=py_top, x2=w, y2=py_top + py_height)

        # 2. Border (pen only — does not overwrite the blended fill)
        ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=0x999999, size=1)
        ct.canvas_proc(c, ct.CANVAS_RECT_FRAME, x=0, y=py_top, x2=w - 1, y2=py_top + py_height)

        # 3. Draw 3 horizontal grabber lines (dark shadow)
        mid_y = py_top + py_height // 2
        ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=0x666666, size=1)
        grab_x1 = max(2, w // 4)
        grab_x2 = min(w - 3, w * 3 // 4)
        ct.canvas_proc(c, ct.CANVAS_LINE, x=grab_x1, y=mid_y - 2, x2=grab_x2, y2=mid_y - 2)
        ct.canvas_proc(c, ct.CANVAS_LINE, x=grab_x1, y=mid_y,     x2=grab_x2, y2=mid_y)
        ct.canvas_proc(c, ct.CANVAS_LINE, x=grab_x1, y=mid_y + 2, x2=grab_x2, y2=mid_y + 2)

        # 3. Draw 3 horizontal highlight lines (white bevel)
        ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=0xFFFFFF, size=1)
        ct.canvas_proc(c, ct.CANVAS_LINE, x=grab_x1, y=mid_y - 1, x2=grab_x2, y2=mid_y - 1)
        ct.canvas_proc(c, ct.CANVAS_LINE, x=grab_x1, y=mid_y + 1, x2=grab_x2, y2=mid_y + 1)
        ct.canvas_proc(c, ct.CANVAS_LINE, x=grab_x1, y=mid_y + 3, x2=grab_x2, y2=mid_y + 3)

        # Store slider geometry and scroll info for click/drag handling
        self._slider_top = py_top
        self._slider_height = py_height
        self._overview_height = h
        self._smooth_max = smooth_max

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
