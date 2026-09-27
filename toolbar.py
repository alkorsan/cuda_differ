"""Differ compare-tab toolbar.

A small toolbar docked to the TOP of every compare tab (the editor
parent's top side), built with dlg_proc:

  [↻ Recompare or × Cancel][↔ Resize] | [↑ Prev][↓ Next] |
  [← Copy][→ Copy] | [≡ Ignore 2/5 ▾] [⚙ Config]        ...status label

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
  top of the compare view and is destroyed with it.

* BUTTONS are 'button_ex' controls (application-themed, CudaText's own
  button look) in flat mode with kind=BTNKIND_TEXT_ONLY, so UTF-8 icon
  captions render like native toolbar buttons. Separators are
  button_ex with kind=BTNKIND_SEP_VERT. Tooltips are the 'hint'
  property of every control.

* The status LABEL on the right side carries the compare state
  ("Comparing...", "Cancelled", "N differences") -- the feedback the
  old status-bar timer spam used to provide, now without any running
  timer.

* The IGNORE dropdown is a popup menu (menu_proc MENU_CREATE) shown
  under the button via MENU_SHOW; its items are checkable and multiple
  can be checked. It is REBUILT on every compare start so it always
  mirrors settings/cuda_differ.json (the config dialog and the tab
  context menu write there too). While a pure-Python algorithm is the
  effective one, the items disable themselves and an explanatory
  item appears -- same guard as the tab context menu / config dialog.

* CALLBACKS: dlg event handlers (on_change) are live callables
  (dlg_proc cleans them up on DLG_FREE); menu item commands are STRING
  callbacks ("module=cuda_differ.toolbar;func=_menu_click;info=...;")
  so nothing accumulates in cudatext's live-callback registry when the
  menu is rebuilt on every compare; the post-toggle refresh goes
  through Command._toolbar_refresh_timer on a 100ms one-shot timer
  (the plugin's established menu-close-first convention).

* SESSION RESTORE: on_start2 schedules Command._toolbar_restore_timer,
  which calls ensure_for_session() for every session-restored compare
  tab, so the toolbars come back at startup.

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


def _ed_text_bg():
    """Live editor text background color from the current UI theme
    (PROC_THEME_UI_DICT_GET / EdTextBg), or None when unavailable."""
    try:
        ui = ct.app_proc(ct.PROC_THEME_UI_DICT_GET, '')
        return ui.get('EdTextBg', {}).get('color')
    except Exception:
        return None


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


# ---------------------------------------------------------------------------
# Text measurement (DPI-correct button widths).
#
# button captions must fit their flat buttons; widths are computed from
# the REAL text size measured on a scratch bitmap canvas (the canvas
# font matches the default UI font the themed buttons use), so they
# follow the OS high-DPI scale without any manual scaling. The results
# are cached; the fallback (~7 px per char) only serves sandboxes where
# the bitmap/canvas API is missing.
# ---------------------------------------------------------------------------

_meas_cache = {}
_CHAR_W_FALLBACK = 7


def _text_size(s):
    """Measured (w, h) of a string, or (None, None) when the
    bitmap/canvas API is unavailable."""
    try:
        hb = ct.bitmap_proc(0, ct.BITMAP_CREATE, 256, 64)
        try:
            hc = ct.bitmap_proc(hb, ct.BITMAP_GET_CANVAS)
            return ct.canvas_proc(hc, ct.CANVAS_GET_TEXT_SIZE, text=s)
        finally:
            ct.bitmap_proc(hb, ct.BITMAP_FREE)
    except Exception:
        return (None, None)


def _text_w(s):
    if s not in _meas_cache:
        w = _text_size(s)[0]
        if not w or w <= 0:
            w = _CHAR_W_FALLBACK * len(s)
        _meas_cache[s] = int(w)
    return _meas_cache[s]


def _text_h(s):
    key = '\x00h:' + s
    if key not in _meas_cache:
        h = _text_size(s)[1]
        if not h or h <= 0:
            h = 16
        _meas_cache[key] = int(h)
    return _meas_cache[key]


# ---------------------------------------------------------------------------
# Layout constants.
# ---------------------------------------------------------------------------

# Horizontal padding inside a flat button around its caption.
_BTN_PAD_X = 12
# Space between two neighboring buttons.
_BTN_GAP = 2
# Extra space around a separator (in addition to _BTN_GAP per side).
_SEP_GAP = 4
# Width of the vertical separator buttons.
_SEP_W = 6
# Minimum width of an icon-only button, so single glyphs stay clickable.
_BTN_W_MIN = 26


def _bar_height():
    """Height of the toolbar: the OS/DPI-correct height of a GUI button
    (PROC_GET_GUI_HEIGHT), clamped to a sane band."""
    try:
        h = ct.app_proc(ct.PROC_GET_GUI_HEIGHT, 'button')
        if h and h > 0:
            return max(24, min(int(h), 44))
    except Exception:
        pass
    return 30


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

    # (name, kind, icon, text) in visual order; the 'ignore' caption is
    # assembled dynamically (see _ignore_caption), the recompare button
    # swaps icon/text with '× Cancel' while a compare runs.
    _BTNS = (
        ('recompare', 'btn', '\u21bb', 'Recompare'),
        ('resize',     'btn', '\u2194', 'Resize'),
        ('sep1',       'sep', None, None),
        ('prev',       'btn', '\u2191', 'Prev'),
        ('next',       'btn', '\u2193', 'Next'),
        ('sep2',       'sep', None, None),
        ('copy_left',  'btn', '\u2190', 'Copy'),
        ('copy_right', 'btn', '\u2192', 'Copy'),
        ('sep3',       'sep', None, None),
        ('ignore',     'btn', '\u2261', 'Ignore'),
        ('config',     'btn', '\u2699', 'Config'),
    )

    def __init__(self, cmd, session, a_ed):
        self.cmd = cmd
        self.session = session
        self.tab_id_str = session.tab_id_str
        self.a_ed = a_ed
        self.h_dlg = None
        self.h_menu = None
        self.ctl = {}          # name -> control index
        self.hbtn = {}         # name -> button_ex handle (button_proc)
        self.menu_items = {}   # ignore key -> popup menu item id
        self.menu_guard_item = None
        self.comparing = False
        self.n_diffs = 0
        self.show_text = True
        self.status = ''
        self.height = _bar_height()

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

        for name, kind, icon, text in self._BTNS:
            if kind == 'sep':
                self._add_separator(name)
            else:
                self._add_button(name, icon, text)

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

    def _add_button(self, name, icon, text):
        n = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_ADD, 'button_ex')
        self.ctl[name] = n
        ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=n, prop={
            'name': name,
            'x': 0,
            'y': 0,
            'w': _BTN_W_MIN,
            'h': self.height,
            'hint': self._tooltip(name),
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

    def _add_separator(self, name):
        n = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_ADD, 'button_ex')
        self.ctl[name] = n
        ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=n, prop={
            'name': name,
            'x': 0,
            'y': 2,
            'w': _SEP_W,
            'h': self.height - 4,
            'tab_stop': False,
        })
        hb = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_HANDLE, index=n)
        self.hbtn[name] = hb
        try:
            ct.button_proc(hb, ct.BTN_SET_KIND, ct.BTNKIND_SEP_VERT)
            ct.button_proc(hb, ct.BTN_SET_FOCUSABLE, False)
        except Exception:
            pass

    def _add_status_label(self):
        n = ct.dlg_proc(self.h_dlg, ct.DLG_CTL_ADD, 'label')
        self.ctl['status'] = n
        h = _text_h('Xg') + 4
        ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=n, prop={
            'name': 'status',
            'cap': '',
            'w': 8,
            'x': 0,
            'y': (self.height - h) // 2,
            'h': h,
            'color': _ed_text_bg() or 0xFFFFFF,
            'a_l': None,          # anchored to the form's RIGHT edge
            'a_r': ('', ']'),
            'sp_r': 10,
            'tab_stop': False,
        })

    # -- captions / layout --------------------------------------------------

    def _caption(self, icon, text):
        """Caption for a button: 'icon text' while button texts are on,
        the bare icon otherwise."""
        if self.show_text and text:
            return icon + ' ' + _(text)
        return icon

    def _btn_width(self, cap):
        return max(_BTN_W_MIN, _text_w(cap) + 2 * _BTN_PAD_X)

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

    def _layout_buttons(self):
        """(Re)assign captions, widths and x positions of all buttons in
        order -- called whenever a caption changes (Recompare <-> Cancel
        swap, Ignore counter, show_btn_text toggle)."""
        if self.h_dlg is None:
            return
        x = 0
        for name, kind, icon, text in self._BTNS:
            n = self.ctl.get(name)
            if n is None:
                continue
            if kind == 'sep':
                x += _SEP_GAP
                ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=n,
                            prop={'x': x, 'w': _SEP_W})
                x += _SEP_W + _SEP_GAP + _BTN_GAP
                continue
            if name == 'ignore':
                cap = self._ignore_caption()
            elif name == 'recompare' and self.comparing:
                cap = self._caption('\u00d7', 'Cancel')
            else:
                cap = self._caption(icon, text)
            w = self._btn_width(cap)
            ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET, index=n,
                        prop={'cap': cap, 'x': x, 'w': w})
            x += w + _BTN_GAP

    def _tooltip(self, name):
        """Tooltip of a button (hotkey hints included)."""
        if name == 'recompare':
            if self.comparing:
                return _('Cancel the running compare')
            return _('Recompare both sides (F5)')
        if name == 'resize':
            return _('Resize the two editors to equal width')
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
        """Text of the right-side status label (resized to fit)."""
        self.status = text
        if self.h_dlg is None:
            return
        try:
            ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET,
                        name='status', prop={
                            'cap': text,
                            'w': (_text_w(text) + 12) if text else 8,
                        })
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
        """Re-apply the toolbar background color for the (possibly
        switched) UI theme -- EdTextBg of the current theme."""
        if self.h_dlg is None:
            return
        bgcolor = _ed_text_bg()
        if bgcolor is None:
            return
        try:
            ct.dlg_proc(self.h_dlg, ct.DLG_PROP_SET,
                        prop={'color': bgcolor})
            ct.dlg_proc(self.h_dlg, ct.DLG_CTL_PROP_SET,
                        name='status', prop={'color': bgcolor})
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
        context menu hiding its ignore items."""
        if self.h_dlg is None:
            return
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
                command='module=cuda_differ.toolbar;func=_menu_click;'
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
            command='module=cuda_differ.toolbar;func=_menu_click;'
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
            ct.msg_status(_('Differ: all ignore options disabled'))
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
        ct.msg_status('{}: {} -- {}'.format(
            _('Differ ignore option'), captions.get(key, key),
            _('enabled') if not old else _('disabled')))
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
            elif name == 'config':
                self.cmd.change_config()
        except Exception:
            import traceback
            traceback.print_exc()

    # -- teardown -----------------------------------------------------------

    def destroy(self):
        """Undock and free the form (+ its live on_change callbacks --
        DLG_FREE cleans them) and drop the popup menu."""
        h = self.h_dlg
        self.h_dlg = None
        if h is not None:
            try:
                ct.dlg_proc(h, ct.DLG_UNDOCK)
            except Exception:
                pass
            try:
                ct.dlg_proc(h, ct.DLG_FREE)
            except Exception:
                pass
        if self.h_menu is not None:
            try:
                ct.menu_proc(self.h_menu, ct.MENU_CLEAR)
                ct.menu_proc(self.h_menu, ct.MENU_REMOVE)
            except Exception:
                pass
            self.h_menu = None
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


def destroy_all():
    """Destroy every toolbar (app exit)."""
    for key in list(_TOOLBARS):
        destroy_for(key)


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
# String-callback entry points (menu items reach these via
# 'module=cuda_differ.toolbar;func=_menu_click;info=<tabid>|<action>;').
# ---------------------------------------------------------------------------

def _menu_click(*args, **kwargs):
    """Menu-item callback: info is '<tab_id_str>|<ignore key or *>'. The
    flexible signature absorbs every way CudaText hands the info value
    to module functions (positional / keyword / mixed)."""
    info = kwargs.get('info', '')
    if not info:
        for a in args:
            if isinstance(a, str) and '|' in a:
                info = a
                break
    if not info:
        return
    try:
        tab_id_str, action = info.split('|', 1)
    except ValueError:
        return
    tb = _TOOLBARS.get(tab_id_str)
    if tb is not None:
        tb.on_menu_action(action)
