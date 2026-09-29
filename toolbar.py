"""Differ compare-tab toolbar.

A small toolbar docked to the TOP of every compare tab (the editor
parent's top side), built with dlg_proc:

  [↻ Recompare or × Cancel] | [↑ Prev][↓ Next] | [← Copy][→ Copy] |
  [≡ Ignore 2/5 ▾] [★ Preset ▾] | [⇋ Swap] [↔ Resize] [▦ View ▾] [⚙ Config]
  ...status label

Everything the toolbar does goes through the Command object of
cuda_differ/__init__.py, which passes itself to every module-level
entry point; this module never imports cuda_differ's __init__ (that
would be a circular import -- __init__ imports THIS module at load).

Implementation notes:

* EMBEDDING: the toolbar is a borderless dlg_proc form shown non-modal
  and DOCKED into the editor's parent control handle
  (ed.prop(PROP_HANDLE_PARENT)) with DLG_DOCK prop='T' -- the same
  docking mechanism the gap-aware overview panel uses for the right
  side ('R'). PROP_HANDLE_PARENT returns the stable grouping panel
  that parents the two split editors, so the toolbar sits exactly on
  top of the compare view. It is NOT freed when that panel is freed
  at tab close: DLG_DOCK only sets the form's Parent, while its OWNER
  stays CudaText's main form (dlg_proc DLG_CREATE does
  TFormDummy.Create(fmMain)), and LCL's TWinControl.Destroy only
  unparents child controls ("controls are freed by the owner") -- so
  the plugin must free the form itself in on_close (see destroy()).

* BUTTONS are 'button_ex' controls (application-themed, CudaText's own
  button look) in flat mode with kind=BTNKIND_TEXT_ONLY, so UTF-8 icon
  captions render like native toolbar buttons. Separators are
  button_ex with kind=BTNKIND_SEP_HORZ (a vertical line: the 'HORZ'
  kind is for controls placed in a horizontal row).

* SIZING: buttons are AUTO-SIZED -- control prop 'autosize' makes
  TATButton compute its width from the caption measured with the REAL
  UI-theme font, DPI-scaled (TATButton.SetAutoSize), so no text or icon
  can ever be cut off HORIZONTALLY. No widths are computed here at all;
  the w_min constraint only keeps icon-only buttons clickable.
  Re-setting the prop (a caption change is always followed by an
  'autosize': True re-set) re-runs the width computation.

  HEIGHT: TATButton.SetAutoSize does NOT size the height -- it centers
  the caption in whatever height the layout gives the control
  ((ClientHeight - TextExtent.cy) div 2) and clips the descenders
  ('g', 'y', 'p'...) when the OS font is a little taller than the
  OS 'button' metrics of PROC_GET_GUI_HEIGHT. So the form's height =
  GUI button height + _TEXT_SLACK extra px (DPI-scaled) + the 1-device-px
  bottom border strip (see below).

* BOTTOM BORDER: the toolbar ends in a full-width 1-device-px line
  separating it from the editor below: a dlg_proc 'panel' control
  of height 1 filled with the theme's SplitMain color (a plain
  TPanel paints a flat solid fill -- the strip IS the line). dlg_proc
  never scales control sizes (scaling is only the explicit DLG_SCALE
  action, which this form never calls), so the strip is exactly one
  device pixel at any DPI; apply_theme() re-feeds the color on every
  UI theme switch. All buttons' a_b anchors reference this strip's
  top ('brd', '[') instead of the form's bottom, so the row sits
  fully above the line.

* POSITIONING: anchors, not coordinates. Every button's LEFT side is
  anchored (a_l) to the RIGHT side (']') of the previous control with
  sp_l spacing, so the whole row re-flows automatically whenever a
  button's width changes (Recompare <-> Cancel swap, Ignore counter,
  show_btn_text toggle). The sp_l gap is roomy between two
  neighboring buttons of one group (_BTN_GAP_INNER, DPI-scaled) and
  tight at the row's start / after a separator (_BTN_GAP); the
  separators' own spacing is unchanged. Tops are anchored to the form
  (a_t), bottoms to the TOP of the bottom-border strip; the status
  label is anchored to the form's right edge (a_r) and vertically
  centered (a_t '-') on the last real button, i.e. on the button
  row's height rather than the (border-including) form height.
  'x'/'w' are never set.

* The status LABEL on the right side carries the compare state
  ("Comparing...", "Cancelled", "N differences") -- the feedback the
  old status-bar timer spam used to provide, now without any running
  timer. It is auto-sized too (TLabel.AutoSize). Its font color is
  set from the UI theme (EdTextFont, the contrast pair of the
  toolbar's EdTextBg background) and re-applied on every theme
  switch: a bare LCL TLabel would draw in the OS widgetset's color
  (black on many systems) and vanish on dark themes.

* The IGNORE dropdown is a popup menu (menu_proc MENU_CREATE) shown
  under the button via MENU_SHOW; its items are checkable and multiple
  can be checked. It is REBUILT on every compare start so it always
  mirrors settings/cuda_differ.json (the config dialog and the tab
  context menu write there too). While a pure-Python algorithm is the
  effective one, the items disable themselves and an explanatory
  item appears -- same guard as the tab context menu / config dialog.

* The PRESETS dropdown is the quick way to set the algorithm +
  beautify combination: two mutually exclusive presets ("Preset 1:
  Fastest comparison - Myers, Beautify Off", "Preset 2: Better
  readability (slower) - Histogram, Beautify On"), a separator, the
  two native algorithms (also mutually exclusive) and the
  independent Beautify alignment toggle. It is REBUILT on every
  open, so the checkmarks always mirror settings/cuda_differ.json,
  and the preset checkmarks are DERIVED from it: native Myers +
  beautify off checks Preset 1, native Histogram + beautify on
  checks Preset 2, any other combination checks NEITHER (a custom
  selection is visible at a glance). Clicks persist
  'differ.algorithm.*' (the config dialog's store) and re-compare
  this tab on the 100ms timer, like the ignore items.

* The VIEW dropdown toggles the surrounding UI from the compare tab:
  "Hide all" flips every item at once; "Show all" everything EXCEPT
  the side and bottom panels (a bulk show must not force those docked
  tool windows open, so their items stay unchecked); the checkable items
  are CudaText's status bar, toolbar, sidebar, side panel, bottom
  panel and tab bar (the PROC_SHOW_* app-proc pairs -- the app's own
  View-menu toggles), the gutter's numbers / bookmarks columns (an
  editor prop, set on BOTH halves of the tab) and the gap-aware
  overview panel (the differ.micromap.enable_overview option; the
  100ms re-compare's refresh then creates or destroys the panel).
  REBUILT on every open, so the checkmarks always mirror the live
  state -- the bars can be toggled from CudaText's own View menu too.
  Unlike Ignore / Presets the button stays enabled while a compare
  runs: visibility is orthogonal to comparing, and the overview
  item's refresh is simply dropped (the usual status hint) when a
  compare is already running.

* CALLBACKS: dlg event handlers (on_change) are live callables
  (dlg_proc cleans them up on DLG_FREE); menu item commands and timers
  use STRING callbacks in the 'module=...;cmd=...;info=...' form
  (Command methods) -- CudaText passes that form's info as the raw
  string, so '<tab id>|<ignore key>' survives without quoting (the
  'func=' form would parse the info value and turn an unquoted
  non-numeric value into None -- the reason the first toolbar version
  silently did nothing on click). The post-toggle refresh goes
  through Command._toolbar_refresh_timer on a 100ms one-shot timer
  (the plugin's established menu-close-first convention).

* SESSION RESTORE: on_start2 schedules Command._toolbar_restore_timer,
  which calls ensure_for_session() for every session-restored compare
  tab, so the toolbars come back at startup.

* TEARDOWN: a toolbar is destroyed when its compare tab is REALLY
  closed (on_close's non-exit branch) -- and that is the ONLY teardown
  there is: nothing is cleaned up at app exit. At app exit CudaText
  fires on_close synthetically from its own exit loop and writes the
  session file only afterwards, so GUI calls there re-enter the
  message loop between the plugin's state-file writes (unregister /
  re-register) and used to cost the compare tabs their persisted
  session entries. The forms are owned by CudaText's main form and
  are freed by it when the app terminates.
  destroy() also NEVER calls menu_proc(MENU_REMOVE) -- that frees the
  popup's ROOT menu item and leaves the (main-form-owned) popup
  dangling; MENU_CLEAR empties it safely.

Options (settings/cuda_differ.json, chapter 'toolbar' in the config
dialog):
* differ.toolbar.show_toolbar (default on) -- create/hide toolbars.
* differ.toolbar.show_btn_text (default on) -- full captions vs
  icon-only buttons.
"""

import cudatext as ct
import cudax_lib as ctx
from cudax_lib import get_translation

_ = get_translation(__file__)  # I18N

# The plugin's settings JSON (same file __init__.JSONFILE points to;
# kept in sync by hand -- toolbar.py must not import __init__).
_JSON_FILE = 'cuda_differ.json'

# Editor text background of the active UI theme -- the toolbar's
# background color (see _ed_text_bg). One flat color that always
# matches the compare view below it, both light and dark themes.
# EdTextFont is its contrast pair: the status label's font color
# (see _ed_text_font). A dlg_proc 'label' is a plain LCL TLabel,
# whose default font color follows the OS WIDGETSET, not CudaText's
# UI theme -- black on many systems, i.e. invisible on the dark
# toolbar background of dark UI themes. So the label's font_color
# is set explicitly from the same theme the background comes from.


def _ed_text_bg():
    """Live editor text background color from the current UI theme
    (PROC_THEME_UI_DICT_GET / EdTextBg), or None when unavailable."""
    try:
        ui = ct.app_proc(ct.PROC_THEME_UI_DICT_GET, '')
        return ui.get('EdTextBg', {}).get('color')
    except Exception:
        return None


def _ed_text_font():
    """Live editor text FONT color from the current UI theme
    (PROC_THEME_UI_DICT_GET / EdTextFont), or None when unavailable.

    The guaranteed-contrast pair of EdTextBg (the toolbar's
    background): whatever the theme does, its editor text color is
    readable on its editor background -- and the toolbar uses that
    very background. Black-on-black status text on dark themes
    (the LCL default font color) is impossible with this."""
    try:
        ui = ct.app_proc(ct.PROC_THEME_UI_DICT_GET, '')
        return ui.get('EdTextFont', {}).get('color')
    except Exception:
        return None


def _split_color():
    """Color of the toolbar's 1px bottom border: the theme's SplitMain
    (the main splitters' color; the overview's left separator reads the
    same key). Fallback 0x808080 = clMedGray."""
    try:
        c = ct.app_proc(ct.PROC_THEME_UI_DICT_GET, '')\
            .get('SplitMain', {}).get('color')
        if c is not None:
            return int(c)
    except Exception:
        pass
    return 0x808080


# The five ignore options, exposed as checkable items of the toolbar's
# Ignore dropdown, of the diff-tab context menu and of the config
# dialog ('differ.ignoreopt.*'). Defined HERE so the toolbar and
# __init__ share one source of truth; __init__ imports it as
# _IGNORE_OPTS. Each entry: (config key suffix, menu caption).
IGNORE_OPTS = (
    ('ignore_case',        _('Ignore case')),
    ('ignore_whitespace',  _('Ignore whitespace')),
    ('ignore_blank_lines', _('Ignore blank lines')),
    ('ignore_eol',         _('Ignore line endings')),
    ('ignore_numbers',     _('Ignore numbers')),
)


def _get_ignore_opt(key):
    """Read one 'differ.ignoreopt.*' boolean from the plugin's settings
    (mtime-cached by cudax_lib, so values changed in the config dialog
    or the tab context menu are picked up at once)."""
    return bool(ctx.get_opt('differ.ignoreopt.' + key, False,
                            user_json=_JSON_FILE))


def _set_ignore_opt(key, val):
    """Write one 'differ.ignoreopt.*' boolean to the plugin's settings
    (same store the config dialog and the tab context menu use)."""
    return ctx.set_opt('differ.ignoreopt.' + key, bool(val),
                       user_json=_JSON_FILE)


# The Presets dropdown's items: two mutually exclusive preset
# combinations, a separator, the two native algorithms (also mutually
# exclusive) and the independent Beautify alignment toggle. Each entry:
# (menu key, menu caption); None = the separator. Toolbar-only -- the
# config dialog / tab context menu keep their own algorithm UIs.
_PRESET_ITEMS = (
    ('preset1',  _('Preset 1: Fastest comparison - Myers, Beautify Off')),
    ('preset2',  _('Preset 2: Better readability (slower) - Histogram, '
                   'Beautify On')),
    (None, None),
    ('algo1',    _('Algorithm 1: Native Histogram')),
    ('algo2',    _('Algorithm 2: Native Myers')),
    ('beautify', _('Beautify alignment')),
)

# Values written to 'differ.algorithm.diff_algorithm' by the preset /
# algorithm items (the same values the config dialog writes; on older
# CudaText builds without diff_proc the plugin falls back to the closest
# pure-Python algorithm).
_ALGO_MYERS = 'native_myers'
_ALGO_HIST = 'native_histogram'


def _get_diff_algo():
    """The configured algorithm ('differ.algorithm.diff_algorithm'),
    read live from the plugin's settings (mtime-cached by cudax_lib,
    so config-dialog / tab-menu writes are picked up at once)."""
    return ctx.get_opt('differ.algorithm.diff_algorithm', 'native_myers',
                       user_json=_JSON_FILE)


def _set_diff_algo(val):
    """Write 'differ.algorithm.diff_algorithm' to the plugin's settings
    (same store the config dialog uses)."""
    return ctx.set_opt('differ.algorithm.diff_algorithm', val,
                       user_json=_JSON_FILE)


def _get_beautify():
    """The Beautify alignment flag
    ('differ.algorithm.beautify_alignment'), read live from the
    settings."""
    return bool(ctx.get_opt('differ.algorithm.beautify_alignment', False,
                            user_json=_JSON_FILE))


def _set_beautify(val):
    """Write 'differ.algorithm.beautify_alignment' to the plugin's
    settings (same store the config dialog uses)."""
    return ctx.set_opt('differ.algorithm.beautify_alignment', bool(val),
                       user_json=_JSON_FILE)


# The View dropdown's checkable items, in the user's order: CudaText's
# six main UI bars, the gutter's two columns, then the overview panel
# ('overview', a single key -- the differ.micromap.enable_overview
# option). Each bar entry: (menu key, caption, app-proc GET id,
# app-proc SET id) -- the PROC_SHOW_* pairs are the app's own
# View-menu toggles. Each gutter entry: (menu key, caption, editor
# prop). 'Hide all' / 'Show all' (above the separator) are plain
# actions, not checkable.
_VIEW_BARS = (
    ('statusbar',   _('Status bar'),
     ct.PROC_SHOW_STATUSBAR_GET, ct.PROC_SHOW_STATUSBAR_SET),
    ('toolbar',     _('Toolbar'),
     ct.PROC_SHOW_TOOLBAR_GET, ct.PROC_SHOW_TOOLBAR_SET),
    ('sidebar',     _('Sidebar'),
     ct.PROC_SHOW_SIDEBAR_GET, ct.PROC_SHOW_SIDEBAR_SET),
    ('sidepanel',   _('Side panel'),
     ct.PROC_SHOW_SIDEPANEL_GET, ct.PROC_SHOW_SIDEPANEL_SET),
    ('bottompanel', _('Bottom panel'),
     ct.PROC_SHOW_BOTTOMPANEL_GET, ct.PROC_SHOW_BOTTOMPANEL_SET),
    ('tabs',        _('Tab bar'),
     ct.PROC_SHOW_TABS_GET, ct.PROC_SHOW_TABS_SET),
)
_VIEW_GUTTERS = (
    ('gutter_num', _('Gutter numbers'), ct.PROP_GUTTER_NUM),
    ('gutter_bm',  _('Gutter bookmarks'), ct.PROP_GUTTER_BM),
)

# 'Show all' restores the main chrome only: the side and bottom panels
# host docked tool windows (Console / Output / TODO...), so the bulk
# show must not force them open -- their menu items stay untouched
# (unchecked when hidden) and are toggled one by one. 'Hide all'
# still hides them (hiding everything keeps meaning everything).
_SHOW_ALL_SKIP = frozenset(('sidepanel', 'bottompanel'))


def _get_overview_opt():
    """The gap-aware overview panel option
    ('differ.micromap.enable_overview'), read live from the plugin's
    settings (mtime-cached by cudax_lib, so config-dialog writes are
    picked up at once)."""
    return bool(ctx.get_opt('differ.micromap.enable_overview', True,
                            user_json=_JSON_FILE))


def _set_overview_opt(val):
    """Write 'differ.micromap.enable_overview' to the plugin's settings
    (same store the config dialog uses)."""
    return ctx.set_opt('differ.micromap.enable_overview', bool(val),
                       user_json=_JSON_FILE)


# ---------------------------------------------------------------------------
# Layout constants.
#
# NO widths are computed anywhere: buttons are auto-sized (control prop
# 'autosize' -> TATButton.SetAutoSize measures the caption with the
# real UI-theme font, DPI-scaled), and positions come from anchors.
# ---------------------------------------------------------------------------

# Tight sp_l spacing at the row's start and after a separator (the
# outer sides of the groups; the separators' own spacing is untouched).
_BTN_GAP = 2
# Roomy sp_l spacing BETWEEN two neighboring buttons of one group (a
# button whose left neighbor is also a button) -- the user asked for
# breathing room between buttons, not around the separators. Logical
# px, DPI-scaled (like _TEXT_SLACK; the tight gaps stay raw: they are
# already fine).
_BTN_GAP_INNER = 8
# Extra spacing to the left of a separator (in addition to _BTN_GAP),
# so groups breathe a little more than single buttons.
_SEP_GAP = 6
# Width of the vertical separator buttons.
_SEP_W = 2
# Minimum width of a button (constraint): keeps single-glyph icon-only
# buttons comfortably clickable.
_BTN_W_MIN = 26
# Vertical inset of buttons/separators from the form's top/bottom.
_BTN_V_PAD = 1
# Spacing between the status label and the form's right edge.
_STATUS_SP_R = 10


def _bar_height():
    """Height of the toolbar's BUTTON ZONE: the OS/DPI-correct height of
    a GUI button (PROC_GET_GUI_HEIGHT), clamped to a sane band."""
    try:
        h = ct.app_proc(ct.PROC_GET_GUI_HEIGHT, 'button')
        if h and h > 0:
            return max(24, min(int(h), 44))
    except Exception:
        pass
    return 30


def _dpi_percent():
    """OS high-DPI scale in percent (100 = no scaling), read from
    PROC_GET_SYSTEM_PPI: usual value is 96ppi = 100%, 144ppi = 150%.
    dlg_proc control sizes are RAW device pixels (no auto-scaling),
    so the plugin's logical constants must be scaled by hand."""
    try:
        ppi = ct.app_proc(ct.PROC_GET_SYSTEM_PPI, '')
        ppi = int(ppi or 0)
        if ppi >= 96:
            return max(100, ppi * 100 // 96)
    except Exception:
        pass
    return 100


def _scaled(px):
    """Logical (96-DPI) pixels -> physical pixels at the current OS DPI
    (at least 1, so a strip is never zero-sized)."""
    return max(1, int(px) * _dpi_percent() // 100)


def _button_gap(prev_kind):
    """sp_l of a button: the roomy _BTN_GAP_INNER (DPI-scaled) when its
    left neighbor is also a button -- two buttons inside one group;
    the tight _BTN_GAP at the row's start and after a separator (the
    separator sides keep their own spacing, _SEP_GAP / _BTN_GAP)."""
    if prev_kind == 'btn':
        return _scaled(_BTN_GAP_INNER)
    return _BTN_GAP


# Height of the bottom-border strip, DEVICE px -- never scaled: the
# line itself is 1 device px at any DPI (see _add_bottom_border).
_BORDER_H = 1
# Extra logical px on top of the OS GUI button height: TATButton centers
# the caption and clips its descenders when the font's TextExtent is taller
# than the stretched button (the 'g' of 'Config' was eaten at the bottom),
# so the zone gets a little breathing room.
_TEXT_SLACK = 4


def _total_height():
    """Total height of the toolbar form: button zone + descender slack
    (DPI-scaled) + the 1-device-px bottom-border strip."""
    return _bar_height() + _scaled(_TEXT_SLACK) + _BORDER_H


# ---------------------------------------------------------------------------
# The toolbar.
# ---------------------------------------------------------------------------

class CompareToolbar:
    """One toolbar docked to the top of ONE compare tab.

    'cmd' is the Command instance of cuda_differ (all actions are its
    methods); 'session' is the tab's _TabSession (the toolbar's
    lifetime is the session's lifetime); 'a_ed' is any editor of that
    tab (used for the parent handle, for focusing the tab before
    commands, and for refreshes).
    """

    # (name, kind, icon, text) in visual order -- five groups:
    # [Recompare/Cancel] | [Prev][Next] | [Copy][Copy] |
    # [Ignore][Preset] | [Swap][Resize][View][Config]. The 'ignore'
    # caption is assembled dynamically (see _ignore_caption), the
    # 'presets' and 'view' captions carry the dropdown arrow (see
    # _presets_caption / _view_caption), the recompare button swaps
    # icon/text with '× Cancel' while a compare runs.
    _BTNS = (
        ('recompare', 'btn', '\u21bb', 'Recompare'),
        ('sep1',       'sep', None, None),
        ('prev',       'btn', '\u2191', 'Prev'),
        ('next',       'btn', '\u2193', 'Next'),
        ('sep2',       'sep', None, None),
        ('copy_left',  'btn', '\u2190', 'Copy'),
        ('copy_right', 'btn', '\u2192', 'Copy'),
        ('sep3',       'sep', None, None),
        ('ignore',     'btn', '\u2261', 'Ignore'),
        ('presets',    'btn', '\u2605', 'Preset'),
        ('sep4',       'sep', None, None),
        ('swap',       'btn', '\u21cb', 'Swap'),
        ('resize',     'btn', '\u2194', 'Resize'),
        ('view',       'btn', '\u25a6', 'View'),
        ('config',     'btn', '\u2699', 'Config'),
    )

    def __init__(self, cmd, session, a_ed):
        self.cmd = cmd
        self.session = session
        self.tab_id_str = session.tab_id_str
        self.a_ed = a_ed
        self.h_dlg = None
        self.h_menu = None
        self.h_pmenu = None
        self.h_vmenu = None
        self.ctl = {}          # name -> control index
        self.hbtn = {}         # name -> button_ex handle (button_proc)
        self.menu_items = {}   # ignore key -> popup menu item id
        self.menu_guard_item = None
        self.comparing = False
        self.n_diffs = 0
        self.show_text = True
        self.status = ''
        self.height = _total_height()

    # -- creation ----------------------------------------------------------

    def create(self):
        """Create the form, the buttons and the ignore popup menu, then
        dock the form to the TOP of the compare tab's editor parent.
        Returns True on success."""
        h_parent = 0
        try:
            h_parent = self.a_ed.get_prop(ct.PROP_HANDLE_PARENT)
        except Exception:
            h_parent = 0
        if not h_parent:
            return False

        bgcolor = _ed_text_bg()
        if bgcolor is None:
            bgcolor = 0xFFFFFF
        self.show_text = bool(self.cmd.cfg.get('show_btn_text', True))

        h = ct.dlg_proc(0, ct.DLG_CREATE)
        self.h_dlg = h
        ct.dlg_proc(h, ct.DLG_PROP_SET, prop={
            'cap': 'Differ',
            'w': 700,
            'h': self.height,
            'border': ct.DBORDER_NONE,
            'color': bgcolor,
        })

        # The bottom border FIRST: the buttons' and separators' a_b
        # anchors reference it by name ('brd'), so it must exist before
        # they are created (dlg_proc resolves anchor targets at
        # prop-set time).
        self._add_bottom_border()

        # Controls are chained left-to-right with anchors: each control's
        # a_l is the PREVIOUS control's ']' (right side), so the row
        # re-flows by itself whenever any button's auto-sized width
        # changes. No 'x'/'w' is ever set on buttons.
        prev = None
        prev_kind = None
        for name, kind, icon, text in self._BTNS:
            if kind == 'sep':
                self._add_separator(name, prev)
            else:
                self._add_button(name, icon, text, prev, prev_kind)
            prev = name
            prev_kind = kind

        # Status label on the right: the compare state / diff count.
        self._add_status_label()

        self._layout_buttons()
        self.set_status('')

        # Show, then dock to the TOP side of the editor's parent (the
        # same DLG_DOCK mechanism the overview panel uses for 'R').
        ct.dlg_proc(h, ct.DLG_SHOW_NONMODAL)
        ct.dlg_proc(h, ct.DLG_DOCK, prop='T', index=h_parent)

        self.rebuild_ignore_menu()
        self._update_nav_enabled()
        return True

    def _add_bottom_border(self):
        """Full-width horizontal line glued to the form's bottom edge:
        a 'panel' control of height 1 filled with the theme's
        SplitMain. dlg_proc never scales control sizes (only the
        explicit DLG_SCALE action scales, and this form never calls
        it), so h=1 is exactly one device pixel at any DPI -- the
        whole strip is the line. Created FIRST: the buttons' a_b
        anchors reference it by name ('brd')."""
        n = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_ADD, 'panel')
        self.ctl['brd'] = n
        ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=n, prop={
            'name': 'brd',
            'cap': '',
            'h': _BORDER_H,      # 1 device px at ANY DPI
            'color': _split_color(),
            'a_l': ('', '['),
            'a_r': ('', ']'),
            'a_t': None,
            'a_b': ('', ']'),
        })

    def _anchor_left(self, prev):
        """a_l value chaining this control to the RIGHT side of the
        previous control ('' = the form for the first one)."""
        return (prev, ']') if prev else ('', '[')

    def _add_button(self, name, icon, text, prev, prev_kind=None):
        n = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_ADD, 'button_ex')
        self.ctl[name] = n
        # Dict order matters: 'cap' BEFORE 'autosize' -- the auto-size
        # pass must measure the final caption. 'w_min' (constraints) is
        # applied by CudaText before everything else.
        ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=n, prop={
            'name': name,
            'w_min': _BTN_W_MIN,
            'hint': self._tooltip(name),
            'cap': self._caption(icon, text),
            'autosize': True,
            'a_l': self._anchor_left(prev),
            'a_t': ('', '['),
            'a_b': ('brd', '['),
            'sp_l': _button_gap(prev_kind),
            'sp_t': _BTN_V_PAD,
            'sp_b': _BTN_V_PAD,
            'tab_stop': False,
            'on_change': self._on_button,
        })
        hb = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_HANDLE, index=n)
        self.hbtn[name] = hb
        try:
            ct.button_proc(hb, ct.BTN_SET_KIND, ct.BTNKIND_TEXT_ONLY)
            ct.button_proc(hb, ct.BTN_SET_FLAT, True)
            ct.button_proc(hb, ct.BTN_SET_FOCUSABLE, False)
        except Exception:
            pass

    def _add_separator(self, name, prev):
        n = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_ADD, 'button_ex')
        self.ctl[name] = n
        ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=n, prop={
            'name': name,
            'w': _SEP_W,
            'a_l': self._anchor_left(prev),
            'a_t': ('', '['),
            'a_b': ('brd', '['),
            'sp_l': _SEP_GAP,
            'sp_t': _BTN_V_PAD + 1,
            'sp_b': _BTN_V_PAD + 1,
            'tab_stop': False,
        })
        hb = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_HANDLE, index=n)
        self.hbtn[name] = hb
        try:
            # BTNKIND_SEP_HORZ is the vertical LINE kind (for controls
            # laid out in a horizontal row); BTNKIND_SEP_VERT draws a
            # horizontal line instead.
            ct.button_proc(hb, ct.BTN_SET_KIND, ct.BTNKIND_SEP_HORZ)
            ct.button_proc(hb, ct.BTN_SET_FOCUSABLE, False)
        except Exception:
            pass

    def _add_status_label(self):
        n = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_ADD, 'label')
        self.ctl['status'] = n
        # A plain LCL TLabel defaults to the OS widgetset font color
        # (black on many systems) -- invisible on the dark toolbar
        # background of dark UI themes. Font color comes from the SAME
        # theme as the background (EdTextFont vs EdTextBg); the black
        # fallback only pairs with create()'s white bg fallback (both
        # hit only when the theme dict itself is unavailable).
        fontcolor = _ed_text_font()
        if fontcolor is None:
            fontcolor = 0x000000
        ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=n, prop={
            'name': 'status',
            'cap': '',
            'autosize': True,    # TLabel sizes itself to the caption
            'a_l': None,         # anchored to the form's RIGHT edge,
            'a_r': ('', ']'),    # vertically centered on the LAST real
            'a_t': ('config', '-'),  # button (the button row's height,
            'sp_r': _STATUS_SP_R,    # not the border-including form)
            'tab_stop': False,
            'font_color': fontcolor,   # theme text color, NOT OS black
        })

    # -- captions / layout --------------------------------------------------

    def _caption(self, icon, text):
        """Caption for a button: 'icon text' while button texts are on,
        the bare icon otherwise."""
        if self.show_text and text:
            return icon + ' ' + _(text)
        return icon

    def _ignore_counts(self):
        """(enabled, total) over the five ignore options, read live from
        the settings file (config-dialog / tab-menu writes included)."""
        enabled = sum(1 for key, _cap in IGNORE_OPTS if _get_ignore_opt(key))
        return enabled, len(IGNORE_OPTS)

    def _ignore_caption(self):
        """'≡ Ignore 2/5 ▾' -- the counter shows how many of the options
        are enabled; with none enabled the numbers are omitted; with
        button texts off only the glyphs remain."""
        n, total = self._ignore_counts()
        counter = ' {}/{}'.format(n, total) if n else ''
        arrow = ' \u25be'
        if self.show_text:
            return '\u2261 ' + _('Ignore') + counter + arrow
        return '\u2261' + counter + arrow

    def _presets_caption(self):
        """'★ Preset ▾' -- the trailing arrow marks it as a dropdown
        (the Ignore twin's convention); with button texts off only the
        glyph and the arrow remain."""
        if self.show_text:
            return '\u2605 ' + _('Preset') + ' \u25be'
        return '\u2605 \u25be'

    def _view_caption(self):
        """'▦ View ▾' -- the trailing arrow marks it as a dropdown (the
        Ignore twin's convention); with button texts off only the glyph
        and the arrow remain."""
        if self.show_text:
            return '\u25a6 ' + _('View') + ' \u25be'
        return '\u25a6 \u25be'

    def _layout_buttons(self):
        """(Re)assign the captions of all buttons -- called whenever a
        caption changes (Recompare <-> Cancel swap, Ignore counter,
        show_btn_text toggle).

        Widths and positions need NO management here: every caption
        change is followed by an 'autosize': True re-set, which re-runs
        TATButton's width computation for the new caption (measured
        with the real UI-theme font), and the a_l anchor chain then
        re-positions all following controls by itself."""
        if self.h_dlg is None:
            return
        for name, kind, icon, text in self._BTNS:
            if kind == 'sep':
                continue
            n = self.ctl.get(name)
            if n is None:
                continue
            if name == 'ignore':
                cap = self._ignore_caption()
            elif name == 'presets':
                cap = self._presets_caption()
            elif name == 'view':
                cap = self._view_caption()
            elif name == 'recompare' and self.comparing:
                cap = self._caption('\u00d7', 'Cancel')
            else:
                cap = self._caption(icon, text)
            try:
                # 'cap' first, 'autosize' second: the width pass must
                # measure the NEW caption.
                ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=n,
                            prop={'cap': cap, 'autosize': True})
            except Exception:
                pass

    def _tooltip(self, name):
        """Tooltip of a button (hotkey hints included)."""
        if name == 'recompare':
            if self.comparing:
                return _('Cancel the running compare')
            return _('Recompare both sides (F5)')
        if name == 'resize':
            return _('Resize the two editors to equal width')
        if name == 'swap':
            return _('Swap the compared editors (left and right)')
        if name == 'prev':
            return _('Jump to previous difference (Alt+Up)')
        if name == 'next':
            return _('Jump to next difference (Alt+Down)')
        if name == 'copy_left':
            return _('Copy current difference to the left (Alt+Left)')
        if name == 'copy_right':
            return _('Copy current difference to the right (Alt+Right)')
        if name == 'config':
            return _('Differ options...')
        if name == 'ignore':
            return self._ignore_tooltip()
        if name == 'presets':
            return self._presets_tooltip()
        if name == 'view':
            return self._view_tooltip()
        return ''

    def _ignore_tooltip(self):
        n, total = self._ignore_counts()
        lines = [_('Ignore options (check any combination)')]
        if n:
            on = [cap for key, cap in IGNORE_OPTS if _get_ignore_opt(key)]
            lines.append(', '.join(on))
        else:
            lines.append(_('None enabled'))
        return '\r'.join(lines)

    def _presets_tooltip(self):
        """Two-line tooltip: what the menu sets + the LIVE combination
        (read from the settings file, so it is always current -- the
        same 'know the current state without opening it' feedback the
        Ignore tooltip's enabled-list gives)."""
        beautify = _get_beautify()
        return '\r'.join((
            _('Comparison presets: algorithm and Beautify alignment'),
            '{}: {} / {} {}'.format(_('Current'), _get_diff_algo(),
                                    _('Beautify alignment'),
                                    _('on') if beautify else _('off')),
        ))

    def _view_tooltip(self):
        """Two-line tooltip: what the menu toggles + which of the nine
        elements are currently hidden (the same live-state feedback
        the Ignore / Presets tooltips give; the bars can change from
        CudaText's own View menu in between)."""
        state = self._view_state()
        hidden = [self._view_caption_of(key)
                  for key in state if not state[key]]
        lines = [_('Show or hide UI parts (bars, gutters, overview)')]
        if hidden:
            lines.append('{}: {}'.format(_('Hidden'), ', '.join(hidden)))
        else:
            lines.append(_('All shown'))
        return '\r'.join(lines)

    def _view_caption_of(self, key):
        """Menu caption of one view item (for tooltips / status
        feedback)."""
        for k, cap, _get, _set in _VIEW_BARS:
            if k == key:
                return cap
        for k, cap, _prop in _VIEW_GUTTERS:
            if k == key:
                return cap
        return _('Overview') if key == 'overview' else key

    def _set_hint(self, name, hint):
        hb = self.hbtn.get(name)
        if hb:
            try:
                ct.button_proc(hb, ct.BTN_SET_HINT, hint)
            except Exception:
                pass

    def _set_enabled(self, name, en):
        n = self.ctl.get(name)
        if n is None:
            return
        try:
            ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=n,
                        prop={'en': bool(en)})
        except Exception:
            pass

    def _update_nav_enabled(self):
        """Enable/disable the diff-navigation buttons: disabled while a
        compare runs (the diffmap is stale and the editors are locked
        read-only) and when the last compare found no differences
        (nothing to jump to / copy)."""
        on = (not self.comparing) and self.n_diffs > 0
        for name in ('prev', 'next', 'copy_left', 'copy_right'):
            self._set_enabled(name, on)
        self._set_enabled('ignore', not self.comparing)
        self._set_enabled('presets', not self.comparing)

    # -- compare state ------------------------------------------------------

    def set_comparing(self, running):
        """Compare kick-off: swap Recompare -> Cancel, disable the
        navigation buttons, rebuild the ignore dropdown (auto-refresh
        each compare start) and set the status label."""
        self.comparing = bool(running)
        if self.h_dlg is None:
            return
        self._layout_buttons()
        self._set_hint('recompare', self._tooltip('recompare'))
        self._set_hint('ignore', self._ignore_tooltip())
        self.rebuild_ignore_menu()
        self._update_nav_enabled()
        if self.comparing:
            self.set_status(_('Comparing...'))

    def compare_finished(self, n_diffs, cancelled=False):
        """Compare end (finished / cancelled): swap Cancel ->
        Recompare, re-enable the navigation buttons when there are
        differences, update the status label."""
        self.comparing = False
        self.n_diffs = int(n_diffs or 0)
        if self.h_dlg is None:
            return
        self._layout_buttons()
        self._set_hint('recompare', self._tooltip('recompare'))
        self._update_nav_enabled()
        if cancelled:
            self.set_status(_('Cancelled'))
        elif self.n_diffs == 0:
            self.set_status(_('No differences'))
        elif self.n_diffs == 1:
            self.set_status(_('1 difference'))
        else:
            self.set_status('{} {}'.format(self.n_diffs,
                                           _('differences')))

    def set_status(self, text):
        """Text of the right-side status label. The label is auto-sized,
        so the caption change alone adjusts its width; the a_r anchor
        keeps it glued to the form's right edge."""
        self.status = text
        if self.h_dlg is None:
            return
        try:
            ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET,
                        name='status', prop={'cap': text})
        except Exception:
            pass

    def set_show_text(self, show_text):
        """Apply the differ.toolbar.show_btn_text option: full captions
        or icons only."""
        show_text = bool(show_text)
        if self.show_text == show_text:
            return
        self.show_text = show_text
        if self.h_dlg is None:
            return
        self._layout_buttons()

    def apply_theme(self):
        """Re-apply the toolbar colors for the (possibly switched) UI
        theme: background EdTextBg, status-label font EdTextFont,
        border-line color SplitMain. All must be re-read together --
        a theme switch can flip light<->dark, and a stale color on the
        new background is the black-on-black bug again."""
        if self.h_dlg is None:
            return
        bgcolor = _ed_text_bg()
        fontcolor = _ed_text_font()
        try:
            if bgcolor is not None and fontcolor is not None:
                ct.dlg_proc(self.h_dlg, ct.DLG_PROP_SET,
                            prop={'color': bgcolor})
                ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET,
                            name='status', prop={'color': bgcolor,
                                                 'font_color': fontcolor})
            # the border line's color is a plain prop re-set (the strip
            # is a 'panel': the color lives in the prop dict, no handle)
            ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET,
                        name='brd', prop={'color': _split_color()})
        except Exception:
            pass

    # -- ignore popup menu --------------------------------------------------

    def rebuild_ignore_menu(self):
        """(Re)build the Ignore dropdown. Called on every compare start
        so the checkmarks always mirror the settings file (the config
        dialog and the tab context menu write there too).

        Python-algorithm guard: while a pure-Python algorithm is the
        effective one the DIFF_IGN_* flags do nothing (Python compares
        strictly), so the items disable themselves and an explanatory
        disabled item heads the menu -- the toolbar twin of the tab
        context menu hiding its ignore items.

        The whole body is guarded: a failure here must never propagate
        into the compare flow that called it."""
        if self.h_dlg is None:
            return
        try:
            self._rebuild_ignore_menu_inner()
        except Exception:
            pass

    def _rebuild_ignore_menu_inner(self):
        if self.h_menu is None:
            self.h_menu = ct.menu_proc(0, ct.MENU_CREATE)
        ct.menu_proc(self.h_menu, ct.MENU_CLEAR)
        self.menu_items = {}
        self.menu_guard_item = None

        use_native = True
        try:
            use_native = bool(self.cmd._resolve_algorithm()[1])
        except Exception:
            use_native = True

        if not use_native:
            # Explanatory disabled item (like the tab menu's guard).
            mi = ct.menu_proc(
                self.h_menu, ct.MENU_ADD,
                caption=_('Ignore options need a native algorithm '
                          '(current one is pure-Python)'))
            try:
                ct.menu_proc(mi, ct.MENU_SET_ENABLED, command=False)
            except Exception:
                pass
            self.menu_guard_item = mi
            ct.menu_proc(self.h_menu, ct.MENU_ADD, caption='-')
            for key, caption in IGNORE_OPTS:
                mi = ct.menu_proc(
                    self.h_menu, ct.MENU_ADD, caption=caption)
                try:
                    ct.menu_proc(mi, ct.MENU_SET_ENABLED, command=False)
                    ct.menu_proc(mi, ct.MENU_SET_CHECKED,
                                 command=_get_ignore_opt(key))
                except Exception:
                    pass
                self.menu_items[key] = mi
            return

        for key, caption in IGNORE_OPTS:
            mi = ct.menu_proc(
                self.h_menu, ct.MENU_ADD,
                caption=caption,
                # 'cmd=' form (a Command method): CudaText passes this
                # form's info to the method as the RAW string, so the
                # '<tab id>|<key>' payload survives. (The 'func=' form
                # would parse the info value and an unquoted value like
                # '501|ignore_case' arrives as None -> the click did
                # nothing in the first toolbar version.)
                command='module=cuda_differ;cmd=toolbar_menu_ignore;'
                        'info={}|{};'.format(self.tab_id_str, key))
            try:
                ct.menu_proc(mi, ct.MENU_SET_CHECKED,
                             command=_get_ignore_opt(key))
            except Exception:
                pass
            self.menu_items[key] = mi

        # 'Uncheck all options' after a separator, at the end.
        ct.menu_proc(self.h_menu, ct.MENU_ADD, caption='-')
        mi = ct.menu_proc(
            self.h_menu, ct.MENU_ADD,
            caption=_('Uncheck all options'),
            command='module=cuda_differ;cmd=toolbar_menu_ignore;'
                    'info={}|*;'.format(self.tab_id_str))
        self.menu_items['*'] = mi

        self._set_hint('ignore', self._ignore_tooltip())

    def popup_ignore_menu(self):
        """Show the Ignore dropdown right under its button."""
        if self.h_menu is None or self.h_dlg is None:
            return
        try:
            props = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_GET,
                                name='ignore')
            x = int(props.get('x', 0))
            y = int(props.get('y', 0)) + int(props.get('h', 0))
            sx, sy = ct.dlg_proc(self.h_dlg, ct.DLG_COORD_LOCAL_TO_SCREEN,
                                 index=x, index2=y)
            ct.menu_proc(self.h_menu, ct.MENU_SHOW, command=(sx, sy))
        except Exception:
            # Fallback: show at the mouse cursor.
            try:
                ct.menu_proc(self.h_menu, ct.MENU_SHOW, command='')
            except Exception:
                pass

    def on_menu_action(self, action):
        """Menu item executed: 'key' toggles one option, '*' unchecks
        all. Persists the change, updates the checkmark / caption /
        tooltip, then re-runs the compare on a 100ms timer (the
        menu-close-first convention) so the new flags apply at once."""
        if action == '*':
            for key, _cap in IGNORE_OPTS:
                _set_ignore_opt(key, False)
                mi = self.menu_items.get(key)
                if mi:
                    try:
                        ct.menu_proc(mi, ct.MENU_SET_CHECKED, command=False)
                    except Exception:
                        pass
            self._layout_buttons()
            self._set_hint('ignore', self._ignore_tooltip())
            try:
                ct.msg_status(_('Differ: all ignore options disabled'))
            except Exception:
                pass
            self._schedule_refresh()
            return
        key = action
        old = _get_ignore_opt(key)
        _set_ignore_opt(key, not old)
        mi = self.menu_items.get(key)
        if mi:
            try:
                ct.menu_proc(mi, ct.MENU_SET_CHECKED, command=not old)
            except Exception:
                pass
        self._layout_buttons()
        self._set_hint('ignore', self._ignore_tooltip())
        captions = dict(IGNORE_OPTS)
        try:
            ct.msg_status('{}: {} -- {}'.format(
                _('Differ ignore option'), captions.get(key, key),
                _('enabled') if not old else _('disabled')))
        except Exception:
            pass
        self._schedule_refresh()

    def _schedule_refresh(self):
        """Recompare THIS tab 100ms later (Command._toolbar_refresh_timer
        resolves the editor by tab id; the timer string command carries
        the tab id)."""
        try:
            callback = ('module=cuda_differ;cmd=_toolbar_refresh_timer;'
                        'info={};'.format(self.tab_id_str))
            ct.timer_proc(ct.TIMER_START_ONE, callback, 100)
        except Exception:
            pass

    # -- presets popup menu -------------------------------------------------

    def rebuild_preset_menu(self):
        """(Re)build the Presets dropdown: the two mutually exclusive
        presets, a separator, the two native algorithms (also mutually
        exclusive) and the independent Beautify alignment toggle.

        The checkmarks are DERIVED from the settings file: native
        Myers + beautify off -> Preset 1 checked; native Histogram +
        beautify on -> Preset 2 checked; any other combination ->
        NEITHER preset checked, so a custom selection is visible at a
        glance. Called on every open (popup_preset_menu); the whole
        body is guarded like the ignore twin's."""
        if self.h_dlg is None:
            return
        try:
            self._rebuild_preset_menu_inner()
        except Exception:
            pass

    def _rebuild_preset_menu_inner(self):
        if self.h_pmenu is None:
            self.h_pmenu = ct.menu_proc(0, ct.MENU_CREATE)
        ct.menu_proc(self.h_pmenu, ct.MENU_CLEAR)

        algo = _get_diff_algo()
        beautify = _get_beautify()
        marks = {
            'preset1': algo == _ALGO_MYERS and not beautify,
            'preset2': algo == _ALGO_HIST and beautify,
            'algo1': algo == _ALGO_HIST,
            'algo2': algo == _ALGO_MYERS,
            'beautify': beautify,
        }
        for key, caption in _PRESET_ITEMS:
            if key is None:
                ct.menu_proc(self.h_pmenu, ct.MENU_ADD, caption='-')
                continue
            mi = ct.menu_proc(
                self.h_pmenu, ct.MENU_ADD,
                caption=caption,
                # 'cmd=' form (a Command method): CudaText passes this
                # form's info to the method as the RAW string, so the
                # '<tab id>|<preset key>' payload survives -- the same
                # convention as the ignore items.
                command='module=cuda_differ;cmd=toolbar_menu_preset;'
                        'info={}|{};'.format(self.tab_id_str, key))
            try:
                ct.menu_proc(mi, ct.MENU_SET_CHECKED, command=marks[key])
            except Exception:
                pass

    def popup_preset_menu(self):
        """Show the Presets dropdown right under its button. The menu is
        rebuilt FIRST, so the checkmarks always mirror the settings
        file (config-dialog / tab-menu / hand-edited changes included)
        -- the preset checkmarks are re-derived on every open."""
        if self.h_dlg is None:
            return
        self.rebuild_preset_menu()
        if self.h_pmenu is None:
            return
        try:
            props = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_GET,
                                name='presets')
            x = int(props.get('x', 0))
            y = int(props.get('y', 0)) + int(props.get('h', 0))
            sx, sy = ct.dlg_proc(self.h_dlg, ct.DLG_COORD_LOCAL_TO_SCREEN,
                                 index=x, index2=y)
            ct.menu_proc(self.h_pmenu, ct.MENU_SHOW, command=(sx, sy))
        except Exception:
            # Fallback: show at the mouse cursor.
            try:
                ct.menu_proc(self.h_pmenu, ct.MENU_SHOW, command='')
            except Exception:
                pass

    def on_preset_action(self, action):
        """Preset-dropdown item executed: persist the combination to
        settings/cuda_differ.json (the config dialog's store), then
        re-compare this tab on the 100ms timer (the menu-close-first
        convention) -- refresh_compare re-reads the settings file (its
        mtime cache), so this very refresh already uses the new
        algorithm / beautify flags. The menu is rebuilt on every open,
        so the new checkmarks show up the next time it pops."""
        algo = None          # None = leave the option unchanged
        beautify = None
        if action == 'preset1':
            algo, beautify = _ALGO_MYERS, False
        elif action == 'preset2':
            algo, beautify = _ALGO_HIST, True
        elif action == 'algo1':
            algo = _ALGO_HIST
        elif action == 'algo2':
            algo = _ALGO_MYERS
        elif action == 'beautify':
            beautify = not _get_beautify()
        else:
            return
        if algo is not None:
            _set_diff_algo(algo)
        if beautify is not None:
            _set_beautify(beautify)
        try:
            ct.msg_status('{}: {} / {} {}'.format(
                _('Differ presets'), _get_diff_algo(),
                _('Beautify alignment'),
                _('on') if _get_beautify() else _('off')))
        except Exception:
            pass
        self._schedule_refresh()

    # -- view popup menu ----------------------------------------------------

    def rebuild_view_menu(self):
        """(Re)build the View dropdown: 'Hide all' / 'Show all', a
        separator, then the checkable items (the six app bars, the two
        gutter columns, the overview panel). REBUILT on every open so
        the checkmarks always mirror the live state -- the bars can be
        toggled from CudaText's own View menu too. The whole body is
        guarded like the ignore / presets twins."""
        if self.h_dlg is None:
            return
        try:
            self._rebuild_view_menu_inner()
        except Exception:
            pass

    def _rebuild_view_menu_inner(self):
        if self.h_vmenu is None:
            self.h_vmenu = ct.menu_proc(0, ct.MENU_CREATE)
        ct.menu_proc(self.h_vmenu, ct.MENU_CLEAR)

        state = self._view_state()

        def _add(key, caption, checked):
            # 'cmd=' form (a Command method): CudaText passes this
            # form's info to the method as the RAW string, so the
            # '<tab id>|<view key>' payload survives -- the same
            # convention as the ignore / preset items.
            mi = ct.menu_proc(
                self.h_vmenu, ct.MENU_ADD,
                caption=caption,
                command='module=cuda_differ;cmd=toolbar_menu_view;'
                        'info={}|{};'.format(self.tab_id_str, key))
            if checked is not None:
                try:
                    ct.menu_proc(mi, ct.MENU_SET_CHECKED,
                                 command=checked)
                except Exception:
                    pass

        _add('hide_all', _('Hide all'), None)
        _add('show_all', _('Show all'), None)
        ct.menu_proc(self.h_vmenu, ct.MENU_ADD, caption='-')
        for key, caption, _get, _set in _VIEW_BARS:
            _add(key, caption, state[key])
        for key, caption, _prop in _VIEW_GUTTERS:
            _add(key, caption, state[key])
        _add('overview', _('Overview'), state['overview'])

    def popup_view_menu(self):
        """Show the View dropdown right under its button. The menu is
        rebuilt FIRST, so the checkmarks always mirror the live
        visibility state (CudaText's own View menu can change the bars
        in between) -- the view twin of the presets popup."""
        if self.h_dlg is None:
            return
        self.rebuild_view_menu()
        if self.h_vmenu is None:
            return
        try:
            props = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_GET,
                                name='view')
            x = int(props.get('x', 0))
            y = int(props.get('y', 0)) + int(props.get('h', 0))
            sx, sy = ct.dlg_proc(self.h_dlg, ct.DLG_COORD_LOCAL_TO_SCREEN,
                                 index=x, index2=y)
            ct.menu_proc(self.h_vmenu, ct.MENU_SHOW, command=(sx, sy))
        except Exception:
            # Fallback: show at the mouse cursor.
            try:
                ct.menu_proc(self.h_vmenu, ct.MENU_SHOW, command='')
            except Exception:
                pass

    def _view_state(self):
        """Live visibility of every checkable View item (dict, menu
        key -> bool): the bars through their PROC_SHOW_* GET pairs, the
        gutter columns from the tab's left half (both halves are always
        set together, so one IS the pair's state), the overview from
        the settings file."""
        state = {}
        for key, _cap, get_id, _set in _VIEW_BARS:
            try:
                state[key] = bool(ct.app_proc(get_id, ''))
            except Exception:
                state[key] = True
        for key, _cap, prop in _VIEW_GUTTERS:
            try:
                state[key] = bool(self.a_ed.get_prop(prop))
            except Exception:
                state[key] = True
        state['overview'] = _get_overview_opt()
        return state

    def _tab_editors(self):
        """Both halves of this compare tab (the toolbar's a_ed + its
        secondary): the gutter column toggles must hit both sides or
        the compare view goes asymmetric."""
        eds = [self.a_ed]
        try:
            h2 = self.a_ed.get_prop(ct.PROP_HANDLE_SECONDARY)
            if h2:
                eds.append(ct.Editor(h2))
        except Exception:
            pass
        return eds

    def _set_view_item(self, key, val):
        """Apply ONE view item's visibility ('val' = the new shown
        state). Returns True when the change needs the 100ms
        re-compare (only the overview: its panel is created /
        destroyed by the refresh)."""
        for k, _cap, _get, set_id in _VIEW_BARS:
            if k == key:
                ct.app_proc(set_id, bool(val))
                return False
        for k, _cap, prop in _VIEW_GUTTERS:
            if k == key:
                for e in self._tab_editors():
                    e.set_prop(prop, bool(val))
                return False
        if key == 'overview':
            _set_overview_opt(bool(val))
            return True
        return False

    def on_view_action(self, action):
        """View-dropdown item executed: 'hide_all' flips EVERY item at
        once, 'show_all' every item EXCEPT the side and bottom panels
        (_SHOW_ALL_SKIP: a bulk show must not force those docked tool
        windows open, so their items stay unchecked); any other key
        toggles ONE element. The overview writes
        differ.micromap.enable_overview (the config dialog's store)
        and re-compares this tab on the
        100ms timer -- the refresh creates or destroys the panel,
        exactly like the preset items; hide/show-all schedule the same
        refresh once for their overview part. The menu is rebuilt on
        every open, so the new checkmarks show up the next time it
        pops; unknown keys are safe no-ops."""
        if action in ('hide_all', 'show_all'):
            show = (action == 'show_all')
            for key in self._view_state():
                if show and key in _SHOW_ALL_SKIP:
                    continue    # side/bottom panels: not force-shown
                self._set_view_item(key, show)
            try:
                ct.msg_status(
                    _('Differ: all view items {}').format(
                        _('shown') if show else _('hidden')))
            except Exception:
                pass
            self._schedule_refresh()
            self._set_hint('view', self._view_tooltip())
            return
        state = self._view_state()
        if action not in state:
            return
        val = not state[action]
        if self._set_view_item(action, val):
            self._schedule_refresh()
        try:
            ct.msg_status('{}: {} -- {}'.format(
                _('Differ view'), self._view_caption_of(action),
                _('shown') if val else _('hidden')))
        except Exception:
            pass
        self._set_hint('view', self._view_tooltip())

    # -- button actions -----------------------------------------------------

    def _focus_tab(self):
        """Focus the tab's left editor before running a command, so the
        focused-session-based commands (jump / copy / cancel) act on
        THIS tab even if the click focused the toolbar form."""
        try:
            self.a_ed.focus()
        except Exception:
            pass

    def _on_button(self, id_dlg, id_ctl, data='', info=''):
        """on_change dispatcher of all toolbar buttons (id_ctl is the
        control index)."""
        name = None
        for key, n in self.ctl.items():
            if n == id_ctl:
                name = key
                break
        if name is None:
            return
        try:
            if name == 'recompare':
                self._focus_tab()
                if self.comparing:
                    self.cmd.cancel_compare(self.a_ed)
                else:
                    self.cmd.refresh_compare(self.a_ed)
            elif name == 'resize':
                self.cmd.resize_equal_width(self.a_ed)
            elif name == 'swap':
                self._focus_tab()
                self.cmd.swap_view(self.a_ed)
            elif name == 'prev':
                self._focus_tab()
                self.cmd.jump_prev()
            elif name == 'next':
                self._focus_tab()
                self.cmd.jump_next()
            elif name == 'copy_left':
                self._focus_tab()
                self.cmd.copy_left()
            elif name == 'copy_right':
                self._focus_tab()
                self.cmd.copy_right()
            elif name == 'ignore':
                self.popup_ignore_menu()
            elif name == 'presets':
                self.popup_preset_menu()
            elif name == 'view':
                self.popup_view_menu()
            elif name == 'config':
                self.cmd.change_config()
        except Exception:
            import traceback
            traceback.print_exc()

    # -- teardown -----------------------------------------------------------

    def destroy(self):
        """Free the form (+ its live on_change callbacks -- DLG_FREE
        cleans them) and empty the popup menus. No DLG_UNDOCK: see the
        comment below.

        The popup menu is NOT disposed with menu_proc(MENU_REMOVE):
        that Pascal handler frees the menu ITEM it is given -- meant
        for items ADDED to an existing menu -- and our handle IS the
        popup's ROOT item. Freeing it would leave the TPopupMenu (owned
        by CudaText's main form) with a dangling root for the rest of
        the app's life, crashing/corrupting the exit that eventually
        frees it. MENU_CLEAR frees all items + their callback data and
        is the safe way to empty the menu; the emptied popup itself is
        owned by the main form and dies safely with the app (the same
        convention CudaText's own plugins use for their popups)."""
        h = self.h_dlg
        self.h_dlg = None
        if h is not None:
            # NO DLG_UNDOCK before DLG_FREE. DLG_UNDOCK does
            # Form.Parent := nil, and LCL's TCustomForm.SetParent
            # immediately allocates a native floating top-level window
            # for a form unparented while Visible
            #   if (Parent = nil) and Visible then HandleNeeded;
            # the window manager maps that window -- the whole screen
            # repaints (the flash that dismissed open menus on tab
            # close) -- and DLG_FREE then hides and frees it a few
            # microseconds later. Freeing the DOCKED form directly is
            # safe and flash-free: DLG_FREE hides it while it is still
            # a child of the (hidden) editor frame -- no screen change
            # -- and TControl.Destroy unparents it itself with
            # Visible=False, so HandleNeeded never runs and no
            # floating window is ever created.
            try:
                ct.dlg_proc(h, ct.DLG_FREE)
            except Exception:
                pass
        if self.h_menu is not None:
            try:
                ct.menu_proc(self.h_menu, ct.MENU_CLEAR)
            except Exception:
                pass
            self.h_menu = None
        if self.h_pmenu is not None:
            try:
                ct.menu_proc(self.h_pmenu, ct.MENU_CLEAR)
            except Exception:
                pass
            self.h_pmenu = None
        if self.h_vmenu is not None:
            try:
                ct.menu_proc(self.h_vmenu, ct.MENU_CLEAR)
            except Exception:
                pass
            self.h_vmenu = None
        self.ctl = {}
        self.hbtn = {}
        self.menu_items = {}
        self.menu_guard_item = None

    def is_created(self):
        return self.h_dlg is not None


# ---------------------------------------------------------------------------
# Module-level registry and entry points (used by cuda_differ/__init__).
# ---------------------------------------------------------------------------

_TOOLBARS = {}  # str(PROP_TAB_ID) -> CompareToolbar


def get_for(session):
    """The toolbar of a compare tab's session, or None."""
    if session is None:
        return None
    return _TOOLBARS.get(session.tab_id_str)


def ensure_for_session(cmd, session, a_ed=None):
    """Create the toolbar of a compare tab if the option is on and it
    does not exist yet. 'a_ed' (any editor of the tab) is optional --
    resolved by tab id when omitted. A toolbar that exists while the
    option is off (hand-edited JSON, no config-dialog OK to sync_all)
    is destroyed here. Returns the toolbar or None."""
    if session is None:
        return None
    tb = _TOOLBARS.get(session.tab_id_str)
    if tb is not None and tb.is_created():
        if not cmd.cfg.get('show_toolbar', True):
            destroy_for(session.tab_id_str)
            return None
        return tb
    if not cmd.cfg.get('show_toolbar', True):
        return None
    if a_ed is None:
        try:
            a_ed = cmd._editor_by_tab_id(session.tab_id)
        except Exception:
            a_ed = None
    if a_ed is None:
        return None
    try:
        tb = CompareToolbar(cmd, session, a_ed)
        if not tb.create():
            return None
    except Exception as ex:
        print('Differ toolbar: failed to create: {}'.format(ex))
        return None
    _TOOLBARS[session.tab_id_str] = tb
    return tb


def destroy_for(tab_id_str):
    """Destroy the toolbar of a compare tab (tab closed / option
    turned off)."""
    tb = _TOOLBARS.pop(str(tab_id_str), None)
    if tb is not None:
        tb.destroy()


def on_compare_start(cmd, session):
    """Compare kick-off hook: swap Recompare -> Cancel, rebuild the
    ignore dropdown (auto-refresh each compare start), status label
    'Comparing...'."""
    tb = get_for(session)
    if tb is not None:
        tb.set_comparing(True)


def on_compare_end(cmd, session, n_diffs=None, cancelled=False):
    """Compare end hook (finished / cancelled): swap Cancel ->
    Recompare, refresh the navigation buttons and the status label
    ('N differences' / 'No differences' / 'Cancelled')."""
    tb = get_for(session)
    if tb is None:
        return
    if n_diffs is None:
        diff = session.diff if session is not None else None
        diffmap = getattr(diff, 'diffmap', None)
        n_diffs = len(diffmap) if diffmap else 0
    tb.compare_finished(n_diffs, cancelled)


def sync_ignore_state(cmd):
    """Refresh the Ignore captions/tooltips of every toolbar after an
    ignore option was changed elsewhere (tab context menu, config
    dialog) -- two-way sync with zero bookkeeping."""
    for tb in list(_TOOLBARS.values()):
        if tb.is_created():
            tb._layout_buttons()
            tb._set_hint('ignore', tb._ignore_tooltip())


def update_theme_all():
    """Re-apply the toolbar background colors after a UI theme switch
    (on_state APPSTATE_THEME_UI)."""
    for tb in list(_TOOLBARS.values()):
        tb.apply_theme()


def sync_all(cmd):
    """Make the set of toolbars match the current sessions and options
    after a config change: destroy toolbars of closed tabs or when
    show_toolbar is off, create missing ones, apply show_btn_text."""
    show = bool(cmd.cfg.get('show_toolbar', True))
    show_text = bool(cmd.cfg.get('show_btn_text', True))
    for key in list(_TOOLBARS):
        tb = _TOOLBARS[key]
        dead = True
        try:
            dead = cmd._session_for(key) is None
        except Exception:
            dead = True
        if not show or dead or not tb.is_created():
            tb.destroy()
            del _TOOLBARS[key]
    if not show:
        return
    for session in list(cmd._sessions.values()):
        tb = _TOOLBARS.get(session.tab_id_str)
        if tb is None:
            ensure_for_session(cmd, session)
        else:
            tb.set_show_text(show_text)
            tb.rebuild_ignore_menu()


# ---------------------------------------------------------------------------
# String-callback entry points.
#
# Menu items use the 'module=cuda_differ;cmd=toolbar_menu_ignore /
# toolbar_menu_preset / toolbar_menu_view; info=<tab id>|<action>;' form
# (Command methods -- info arrives as the RAW string). The
# module-level entries below are the belt-and-braces twins for any
# 'module=cuda_differ.toolbar;
# func=_menu_click; info="<tab id>|<action>;"' callback (info QUOTED:
# the engine's ValueFromString turns an unquoted non-numeric value
# into None).
# ---------------------------------------------------------------------------

def menu_action(info):
    """'info' is '<tab_id_str>|<ignore key or *>': route the click to the
    toolbar of that compare tab. Returns True when a toolbar handled
    it (False: unknown tab / no toolbar / bad payload)."""
    if not info or not isinstance(info, str) or '|' not in info:
        return False
    try:
        tab_id_str, action = info.split('|', 1)
    except ValueError:
        return False
    tb = _TOOLBARS.get(tab_id_str)
    if tb is None:
        return False
    tb.on_menu_action(action)
    return True


def preset_menu_action(info):
    """'info' is '<tab_id_str>|<preset key>': route the click to the
    toolbar of that compare tab. Returns True when a toolbar handled
    it (False: unknown tab / no toolbar / bad payload)."""
    if not info or not isinstance(info, str) or '|' not in info:
        return False
    try:
        tab_id_str, action = info.split('|', 1)
    except ValueError:
        return False
    tb = _TOOLBARS.get(tab_id_str)
    if tb is None:
        return False
    tb.on_preset_action(action)
    return True


def view_menu_action(info):
    """'info' is '<tab_id_str>|<view key>': route the click to the
    toolbar of that compare tab. Returns True when a toolbar handled
    it (False: unknown tab / no toolbar / bad payload)."""
    if not info or not isinstance(info, str) or '|' not in info:
        return False
    try:
        tab_id_str, action = info.split('|', 1)
    except ValueError:
        return False
    tb = _TOOLBARS.get(tab_id_str)
    if tb is None:
        return False
    tb.on_view_action(action)
    return True


def _menu_click(*args, **kwargs):
    """'func=' callback twin of menu_action: info must be QUOTED in the
    command string ('info="501|ignore_case";') so the engine passes it
    as a string -- an unquoted value would arrive as None here (that
    was the silent no-op of the first toolbar version)."""
    info = kwargs.get('info', '')
    if not info:
        for a in args:
            if isinstance(a, str) and '|' in a:
                info = a
                break
    if not info:
        import cudatext as _ct
        try:
            _ct.msg_log_console(
                'Differ toolbar: menu callback got empty info')
        except Exception:
            pass
        return
    menu_action(info)
