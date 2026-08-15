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

        Args:
            c: canvas handle (static bitmap canvas)
            w: width in pixels
            h: height in pixels
        """
        # Clear background
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_bg, style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=0, y=0, x2=w, y2=h)

        # Paint both sides
        scale = self._get_scale(h)
        half_w = w // 2
        self._paint_side(c, 'a', 0, half_w, h, scale)
        self._paint_side(c, 'b', half_w, w, h, scale)

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
            vis_y += self._line_visual_rows(side, line)

        # Paint any remaining gaps after the last line
        for after_line, gap_rows in gaps:
            if after_line >= line_count:
                gap_h = max(1, int(gap_rows * scale))
                py = int(vis_y * scale)
                ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_gap, style=ct.BRUSH_SOLID)
                ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=x_start, y=py, x2=x_end, y2=py + gap_h)
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
        """Draw the dynamic part: viewport rectangles and cursor lines.

        The viewport rectangle shows the range of lines currently visible
        in each editor. The cursor line shows the caret position.

        Args:
            w: width in pixels
            h: height in pixels
        """
        c = self.h_canvas
        scale = self._get_scale(h)
        half_w = w // 2

        # Paint viewport + cursor for left editor (a_ed)
        if self.a_ed is not None:
            try:
                self._paint_viewport_and_cursor(c, self.a_ed, 'a',
                                                0, half_w, h, scale)
            except Exception:
                pass

        # Paint viewport + cursor for right editor (b_ed)
        if self.b_ed is not None:
            try:
                self._paint_viewport_and_cursor(c, self.b_ed, 'b',
                                                half_w, w, h, scale)
            except Exception:
                pass

    def _paint_viewport_and_cursor(self, c, ed, side, x_start, x_end, h, scale):
        """Paint the viewport rectangle and cursor line for one editor.

        The viewport rectangle shows the range of lines currently visible
        in the editor. Its top is at the first visible line's visual Y,
        and its height is proportional to the number of visible visual rows.

        Also draws a thin cursor line at the caret position.

        Args:
            c: canvas handle (image's embedded bitmap canvas)
            ed: editor instance
            side: 'a' or 'b'
            x_start: left X pixel
            x_end: right X pixel
            h: total pixel height
            scale: pixels per visual row
        """
        # Get scroll info: 'pos' = first visible line, 'page' = visible line count
        scroll_info = ed.get_prop(ct.PROP_SCROLL_VERT_INFO)
        if scroll_info:
            first_line = scroll_info.get('pos', 0)
            visible_lines = scroll_info.get('page', 1)
        else:
            caret = ed.get_carets()
            if caret:
                first_line = caret[0][1]
                visible_lines = 1
            else:
                return

        # Map first visible line to visual Y (wrap + gap aware)
        vis_y_top = self._line_to_visual_y(side, first_line)
        py_top = int(vis_y_top * scale)

        # Compute viewport height: sum of visual rows for visible lines
        vis_y_bottom = vis_y_top
        line_count = self.a_line_count if side == 'a' else self.b_line_count
        for i in range(first_line, min(first_line + visible_lines, line_count)):
            vis_y_bottom += self._line_visual_rows(side, i)
        # Add gap rows within the visible range
        gaps = self._sorted_gaps(side)
        for after_line, gap_rows in gaps:
            if first_line < after_line <= first_line + visible_lines:
                vis_y_bottom += gap_rows
        py_bottom = int(vis_y_bottom * scale)
        py_height = max(2, py_bottom - py_top)

        # Draw viewport rectangle (frame with transparent brush)
        ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=self.color_cursor, size=1)
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_cursor, style=ct.BRUSH_CLEAR)
        ct.canvas_proc(c, ct.CANVAS_RECT_FRAME, x=x_start, y=py_top, x2=x_end - 1, y2=py_top + py_height)

        # Draw cursor line at caret position (thin line inside the viewport)
        caret = ed.get_carets()
        if caret:
            y_caret = caret[0][1]
            vis_y = self._line_to_visual_y(side, y_caret)
            py = int(vis_y * scale)
            ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=self.color_cursor, size=1)
            ct.canvas_proc(c, ct.CANVAS_LINE, x=x_start, y=py, x2=x_end, y2=py)

    def _on_click(self, id_dlg, id_ctl, data='', info=''):
        """Called when the image is clicked. Scrolls the corresponding
        editor to the clicked position.

        info is "x,y" — the click position in image coordinates.
        Maps the click Y to a visual row (using the same scale as painting),
        then maps the visual row to a line index (accounting for gaps and
        wrapping), then scrolls the editor to that line.
        """
        if not info:
            return
        try:
            parts = info.split(',')
            x = int(parts[0])
            y = int(parts[1])
        except (ValueError, IndexError):
            return

        # Determine which side was clicked
        w, h = self._get_size()
        half_w = w // 2
        side = 'a' if x < half_w else 'b'

        # Map Y pixel to visual row using the same scale as painting
        scale = self._get_scale(h)
        if scale <= 0:
            return
        visual_y = y / scale

        # Map visual row to line index (wrap + gap aware)
        line = self._visual_y_to_line_wrap_aware(side, visual_y)

        # Scroll the corresponding editor to the clicked line
        ed = self.a_ed if side == 'a' else self.b_ed
        if ed is not None:
            try:
                ed.action(ct.EDACTION_SHOW_POS, (0, line), (0, 0))
            except Exception:
                pass

    def _on_mouse_down(self, id_dlg, id_ctl, data='', info=''):
        """Called on mouse down in the image. data is a dict with
        btn, state, x, y. We treat it like a click for scrolling."""
        if isinstance(data, dict):
            x = data.get('x', 0)
            y = data.get('y', 0)
            self._on_click(id_dlg, id_ctl, info='{},{}'.format(x, y))
