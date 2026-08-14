"""Paintbox-based overview panel for the Differ plugin.

Replaces the micromap with a custom paintbox docked to the right side of
the editor's parent form. Unlike the micromap, the paintbox overview is
gap-aware: it accounts for the inter-line gaps inserted by Differ for
visual alignment, so the overview stays in sync with what the user
actually sees.

Architecture:
  - PaintboxOverview creates a non-modal dialog with a paintbox control.
  - The dialog is docked to the RIGHT side of the editor's parent form
    via DLG_DOCK.
  - The paintbox is painted on-demand (there is no on_paint event in
    CudaText) — we repaint on resize, show, scroll, caret move, and
    timer.
  - The overview shows BOTH editors side-by-side (left half = a_ed,
    right half = b_ed), with colored rectangles for changed lines and
    gray rectangles for gaps.

Data model:
  - self.line_states: dict mapping (editor_side, line_index) -> color
  - self.gaps: dict mapping (editor_side, visual_position) -> gap_size
  - Visual positions are computed by walking the editor's lines and
    adding gap sizes, so the overview reflects the actual visual layout.
"""

import cudatext as ct
from .profiling import Profiler

# Overview dialog width in pixels (docked to the right)
OVERVIEW_WIDTH = 80


class PaintboxOverview:
    """Manages a docked paintbox overview panel for a compare tab.

    The overview shows a gap-aware mini-map of both editors side by side.
    Created once per compare tab, destroyed when the tab closes.
    """

    def __init__(self):
        self.h_dlg = None       # parent form handle (or dialog handle)
        self.h_canvas = None    # paintbox canvas handle
        self._ctl_index = None  # control index in the parent form
        self._owns_dlg = False  # True if we created a separate dialog
        # Line states: {('a', line): color, ('b', line): color}
        self.line_states = {}
        # Gap info: list of (side, after_line, gap_lines) for each gap
        self.gaps_a = []  # list of (after_line, gap_visual_rows)
        self.gaps_b = []
        # Editor references and line counts
        self.a_ed = None
        self.b_ed = None
        self.a_line_count = 0
        self.b_line_count = 0
        # Colors
        self.color_bg = 0xFFFFFF
        self.color_deleted = 0xAAAAAA
        self.color_added = 0xAAAAAA
        self.color_changed = 0xAAAAAA
        self.color_gap = 0xEEEEEE
        self.color_cursor = 0x000000

    def is_created(self):
        """Return True if the overview has been created."""
        return self.h_dlg is not None

    def create(self, a_ed, b_ed):
        """Create the overview as a separate dialog docked to the right
        side of the editor's parent form.

        Per CudaText author's instructions: after DLG_DOCK with prop='R',
        set the form's 'x' prop to a large value (e.g. 6000) to force it
        to the right side, past the secondary editor's X position.

        Args:
            a_ed: left editor (primary)
            b_ed: right editor (secondary)
        """
        self.a_ed = a_ed
        self.b_ed = b_ed
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
            'on_resize': self._on_resize,
            'on_show': self._on_resize,
        })

        # Add paintbox control, filling the entire dialog
        self._ctl_index = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_ADD, 'paintbox')
        ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=self._ctl_index, prop={
            'name': 'paint',
            'align': ct.ALIGN_CLIENT,
            'on_click': self._on_click,
            'on_mouse_down': self._on_mouse_down,
        })
        self.h_canvas = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_HANDLE, index=self._ctl_index)

        # Dock to the RIGHT side of the editor's parent form, then set
        # x to a large value to force the dialog to the right of the
        # secondary editor (per CudaText author's fix).
        ct.dlg_proc(self.h_dlg, ct.DLG_SHOW_NONMODAL)
        ct.dlg_proc(self.h_dlg, ct.DLG_DOCK, prop='R', index=h_parent)
        # ct.dlg_proc(self.h_dlg, ct.DLG_PROP_SET, prop={'x': 6000})

    def destroy(self):
        """Undock and free the overview dialog."""
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
            self.h_canvas = None
            self._ctl_index = None

    def set_colors(self, color_bg, color_deleted, color_added, color_changed, color_gap):
        """Set the colors used for painting the overview."""
        self.color_bg = color_bg
        self.color_deleted = color_deleted
        self.color_added = color_added
        self.color_changed = color_changed
        self.color_gap = color_gap

    def set_line_counts(self, a_count, b_count):
        """Set the total line counts for both editors (without gaps)."""
        self.a_line_count = a_count
        self.b_line_count = b_count

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

        Returns: int — total visual rows for the given side.
        """
        if side == 'a':
            line_count = self.a_line_count
            gaps = self.gaps_a
        else:
            line_count = self.b_line_count
            gaps = self.gaps_b
        total = line_count
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
        accounting for gaps.

        Args:
            side: 'a' or 'b'
            line: line index (0-based)

        Returns: float — visual Y position in visual-row units.
        """
        gaps = self._sorted_gaps(side)
        y = line
        for after_line, gap_rows in gaps:
            if after_line <= line:
                y += gap_rows
            else:
                break
        return y

    def _visual_y_to_line(self, side, visual_y):
        """Map a visual Y position (in visual rows) back to a line index,
        accounting for gaps.

        Args:
            side: 'a' or 'b'
            visual_y: visual Y position in visual-row units

        Returns: int — line index (0-based).
        """
        gaps = self._sorted_gaps(side)
        remaining = visual_y
        for after_line, gap_rows in gaps:
            if after_line < remaining:
                # The gap is above the current position; subtract it
                remaining -= gap_rows
            else:
                break
        return max(0, int(remaining))

    def paint(self):
        """Repaint the entire overview. Called on resize, show, scroll,
        and after a fresh compare.

        The overview shows both editors side-by-side:
        - Left half: a_ed lines + gaps
        - Right half: b_ed lines + gaps
        - Each line is painted as a 1-pixel-tall colored rectangle
        - Gaps are painted as gray rectangles
        - The cursor position is marked with a thin horizontal line
        """
        if self.h_canvas is None:
            return

        c = self.h_canvas
        # Get the paintbox size from the control properties
        props = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_GET, index=self._ctl_index)
        w = props.get('w', OVERVIEW_WIDTH)
        h = props.get('h', 600)
        if w <= 0 or h <= 0:
            return

        # Clear background
        ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_bg, style=ct.BRUSH_SOLID)
        ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=0, y=0, x2=w, y2=h)

        # Compute visual heights for both sides
        vis_h_a = self._compute_visual_height('a')
        vis_h_b = self._compute_visual_height('b')
        max_vis_h = max(vis_h_a, vis_h_b)

        # Scale: pixels per visual row
        scale = h / max_vis_h if max_vis_h > 0 else 1

        # Paint left side (a_ed) — left half of the paintbox
        half_w = w // 2
        self._paint_side(c, 'a', 0, half_w, h, scale, vis_h_a)
        # Paint right side (b_ed) — right half of the paintbox
        self._paint_side(c, 'b', half_w, w, h, scale, vis_h_b)

        # Paint cursor positions (thin horizontal lines)
        if self.a_ed is not None:
            try:
                caret_a = self.a_ed.get_carets()
                if caret_a:
                    y_a = caret_a[0][1]
                    vis_y = self._line_to_visual_y('a', y_a)
                    py = int(vis_y * scale)
                    ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=self.color_cursor, size=1)
                    ct.canvas_proc(c, ct.CANVAS_LINE, x=0, y=py, x2=half_w, y2=py)
            except Exception:
                pass
        if self.b_ed is not None:
            try:
                caret_b = self.b_ed.get_carets()
                if caret_b:
                    y_b = caret_b[0][1]
                    vis_y = self._line_to_visual_y('b', y_b)
                    py = int(vis_y * scale)
                    ct.canvas_proc(c, ct.CANVAS_SET_PEN, color=self.color_cursor, size=1)
                    ct.canvas_proc(c, ct.CANVAS_LINE, x=half_w, y=py, x2=w, y2=py)
            except Exception:
                pass

    def _paint_side(self, c, side, x_start, x_end, h, scale, vis_h):
        """Paint one side of the overview (either a_ed or b_ed half).

        Walks the lines in order, painting each line as a 1-pixel-tall
        rectangle. Gaps are painted as gray rectangles. The visual
        position accounts for gaps so the overview stays in sync with
        the actual editor layout.
        """
        if side == 'a':
            line_count = self.a_line_count
            gaps = self._sorted_gaps('a')
            ed = self.a_ed
        else:
            line_count = self.b_line_count
            gaps = self._sorted_gaps('b')
            ed = self.b_ed

        # Build a gap map: after_line -> total gap rows at that position
        gap_map = {}
        for after_line, gap_rows in gaps:
            gap_map[after_line] = gap_map.get(after_line, 0) + gap_rows

        vis_y = 0
        w = x_end - x_start

        # Paint gaps and lines in order
        for line in range(line_count):
            # Paint gap before this line (if any)
            if line in gap_map:
                gap_rows = gap_map[line]
                gap_h = max(1, int(gap_rows * scale))
                py = int(vis_y * scale)
                ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_gap, style=ct.BRUSH_SOLID)
                ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=x_start, y=py, x2=x_end, y2=py + gap_h)
                vis_y += gap_rows

            # Paint the line
            color = self.line_states.get((side, line))
            if color is not None:
                py = int(vis_y * scale)
                line_h = max(1, int(scale) + 1)
                ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=color, style=ct.BRUSH_SOLID)
                ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=x_start, y=py, x2=x_end, y2=py + line_h)
            vis_y += 1

        # Paint any remaining gaps after the last line
        for after_line, gap_rows in gaps:
            if after_line >= line_count:
                gap_h = max(1, int(gap_rows * scale))
                py = int(vis_y * scale)
                ct.canvas_proc(c, ct.CANVAS_SET_BRUSH, color=self.color_gap, style=ct.BRUSH_SOLID)
                ct.canvas_proc(c, ct.CANVAS_RECT_FILL, x=x_start, y=py, x2=x_end, y2=py + gap_h)
                vis_y += gap_rows

    def _on_resize(self, id_dlg, id_ctl, data='', info=''):
        """Called when the dialog is resized or shown. Triggers a repaint."""
        self.paint()

    def _on_click(self, id_dlg, id_ctl, data='', info=''):
        """Called when the paintbox is clicked. Scrolls the corresponding
        editor to the clicked position.

        info is "x,y" — the click position in paintbox coordinates.
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
        props = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_GET, index=self._ctl_index)
        w = props.get('w', OVERVIEW_WIDTH)
        half_w = w // 2
        side = 'a' if x < half_w else 'b'

        # Get the paintbox height
        h = props.get('h', 600)

        # Map Y pixel to visual row
        vis_h_a = self._compute_visual_height('a')
        vis_h_b = self._compute_visual_height('b')
        max_vis_h = max(vis_h_a, vis_h_b)
        if max_vis_h <= 0:
            return
        scale = h / max_vis_h
        visual_y = y / scale if scale > 0 else 0

        # Map visual row to line index
        line = self._visual_y_to_line(side, visual_y)

        # Scroll the corresponding editor
        ed = self.a_ed if side == 'a' else self.b_ed
        if ed is not None:
            try:
                ed.action(ct.EDACTION_SHOW_POS, (0, line), (0, 0))
            except Exception:
                pass

    def _on_mouse_down(self, id_dlg, id_ctl, data='', info=''):
        """Called on mouse down in the paintbox. data is a dict with
        btn, state, x, y. We treat it like a click for scrolling."""
        if isinstance(data, dict):
            x = data.get('x', 0)
            y = data.get('y', 0)
            self._on_click(id_dlg, id_ctl, info='{},{}'.format(x, y))
