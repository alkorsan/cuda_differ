import os
import json
import typing as tp

import cudatext as ct
import cudatext_cmd as ct_cmd
import cudax_lib as ctx

from . import differ_native as dfn
from . import differ_python as dfp
from .overview import PaintboxOverview
from .profiling import Profiler, enable_profiling, profiling_report, reset_profiling

# df is used as a namespace for event constants (A_LINE_DEL, B_LINE_ADD, etc.).
# Both differ_native and differ_python define identical constants, so we alias
# df to differ_native for backwards-compatible constant access. The Differ
# class itself is chosen at runtime based on the configured algorithm — see
# Command._create_differ below.
df = dfn

from cudax_lib import get_translation
_ = get_translation(__file__)  # I18N


class ScrollSplittedTab:
    """Manages synchronized scrolling for split compare tabs. Inlined from
    scroll.py to reduce file count -- small enough to live in __init__.py."""

    keep_caret_visible = False

    def __init__(self, name):
        self.name = name
        self.tab_id = set()

    def toggle(self, on=True):
        act = ct.PROC_EVENTS_SUB if on and ct.ed.get_prop(ct.PROP_TAB_ID) in self.tab_id else ct.PROC_EVENTS_UNSUB
        ct.app_proc(act, self.name+';on_scroll;;')

    def on_scroll(self, ed_self):
        if ed_self.get_prop(ct.PROP_SPLIT)[0] == '-':
            return

        pos_v = ed_self.get_prop(ct.PROP_SCROLL_VERT_INFO)['smooth_pos']
        pos_h = ed_self.get_prop(ct.PROP_SCROLL_HORZ_INFO)['smooth_pos']

        hndl_self = ed_self.get_prop(ct.PROP_HANDLE_SELF)
        hndl_primary = ed_self.get_prop(ct.PROP_HANDLE_PRIMARY)
        hndl_secondary = ed_self.get_prop(ct.PROP_HANDLE_SECONDARY)
        if hndl_self == hndl_primary:
            hndl_opposit = hndl_secondary
        else:
            hndl_opposit = hndl_primary
        e = ct.Editor(hndl_opposit)

        e.set_prop(ct.PROP_SCROLL_VERT_INFO, {'smooth_pos': pos_v})
        e.set_prop(ct.PROP_SCROLL_HORZ_INFO, {'smooth_pos': pos_h})

        e.cmd(ct_cmd.cmd_RepaintEditor)


DIFF_TAG = 148
NKIND_DELETED = 24
NKIND_ADDED = 25
NKIND_CHANGED = 26
GAP_WIDTH = 5000
DECOR_CHAR = '■'
DEFAULT_SYNC_SCROLL = '1'
U_PREFIX = 'untitled:'

PLG_NAME = _('Differ')
METAJSONFILE = os.path.dirname(__file__) + os.sep + 'differ_opts.json'
JSONFILE = 'cuda_differ.json'  # To store in settings/cuda_differ.json
JSONPATH = ct.app_path(ct.APP_DIR_SETTINGS) + os.sep + JSONFILE

OPTS_META = [
    {'opt': 'differ.changed_color',
     'cmt': _('Color of changed lines'),
     'def': '',
     'frm': '#rgb-e',
     'chp': 'colors',
     },
    {'opt': 'differ.added_color',
     'cmt': _('Color of added lines'),
     'def': '',
     'frm': '#rgb-e',
     'chp': 'colors',
     },
    {'opt': 'differ.deleted_color',
     'cmt': _('Color of deleted lines'),
     'def': '',
     'frm': '#rgb-e',
     'chp': 'colors',
     },
    {'opt': 'differ.gap_color',
     'cmt': _('Color of inter-line gap background'),
     'def': '',
     'frm': '#rgb-e',
     'chp': 'colors',
     },
    {'opt': 'differ.sync_scroll',
     'cmt': _('Use synchronized scrolling (vertical/horizontal) in two compared files'),
     'def': True,
     'frm': 'bool',
     'chp': 'config',
     },
    {'opt': 'differ.compare_with_details',
     'cmt': _('Perform detailed comparision'),
     'def': True,
     'frm': 'bool',
     'chp': 'config',
     },
    {'opt': 'differ.ratio_percents',
     'cmt': _('Measure of the sequences’ similarity, in percents'),
     'def':  75,
     'frm': 'int',
     'chp': 'config',
     },
    {'opt': 'differ.enable_sync_caret',
     'cmt': _('Keep carets in both editors visible on current screen area'),
     'def':  False,
     'frm': 'bool',
     'chp': 'config',
     },
    {'opt': 'differ.enable_auto_refresh',
     'cmt': _('Auto diff refresh after changes'),
     'def':  False,
     'frm': 'bool',
     'chp': 'config',
     },
     {'opt': 'differ.diff_context',
     'cmt': _('Number of lines of context displayed when diffing files'),
     'def':  3,
     'frm': 'int',
     'chp': 'config',
     },
    # --- diff_algorithm dropdown ------------------------------------------------
    # Method 2 (used here): value/label pairs via 'str2s' + 'dct'.
    #   The dropdown shows the second element of each tuple; on save, the keys
    #   ('difflib'/'patience') are extracted, so the stored value is the raw
    #   plain string. load_definitions auto-derives 'jdc' from 'dct', so 'jdc'
    #   does not need to be set by hand. Use this when you want friendlier
    #   dropdown labels than the raw config value.
    #
    # Method 1 (alternative, kept here as a reminder): plain string list via
    # 'strs' + 'lst'. The combobox is populated straight from 'lst'; on save
    # the raw string itself is stored. Minimal, no separate labels.
    #
    #     {'opt': 'differ.diff_algorithm',
    #      'cmt': _('Diff algorithm to use. Patience anchors on unique matching '
    #               'lines and often produces more human-readable diffs when '
    #               'blocks of code are moved; difflib is Python\'s stdlib '
    #               'SequenceMatcher. Default: patience.'),
    #      'def': 'patience',
    #      'frm': 'strs',
    #      'lst': ['difflib', 'patience'],
    #      'chp': 'config',
    #      },
    # ----------------------------------------------------------------------------
    {'opt': 'differ.diff_algorithm',
     'cmt': _('Diff algorithm to use. Native Histogram and Native Myers '
              'call the built-in cudatext.diff_proc() API and run in '
              'compiled Pascal code (ports of JGit\'s HistogramDiff and '
              'MyersDiff — the same algorithms git uses). They are 10-30x '
              'faster than the pure-Python implementations on large files; '
              'Native Histogram is the default. The other algorithms are '
              'pure-Python: Hybrid combines patience anchoring on unique '
              'lines with Myers for the gaps (best pure-Python quality); '
              'Myers is O(NP) (Wu/Manber/Myers/Miller 1989); VS Code uses '
              'dynamic programming with equality scoring (best for files '
              'with duplicated lines, slowest); Patience anchors on unique '
              'matching lines; difflib is Python\'s stdlib SequenceMatcher. '
              'Default: Native Histogram.'),
     'def': 'native_histogram',
     'frm': 'str2s',
     'dct': [('native_histogram', _('Native Histogram (JGit, fastest, recommended)')),
             ('native_myers',     _('Native Myers (JGit, linear-space)')),
             ('hybrid',           _('Hybrid (Python: Patience + Myers)')),
             ('myers',            _('Myers (Python: O(NP), fastest Python)')),
             ('vscode',           _('VS Code (Python: DP + Myers)')),
             ('patience',         _('Patience Diff (Python)')),
             ('difflib',          _('Python difflib stdlib'))],
     'chp': 'config',
     },
    {'opt': 'differ.autojunk',
     'cmt': _('Enable autojunk heuristic in difflib SequenceMatcher (only '
              'applies when diff_algorithm is "difflib"; '
              'HybridSequenceMatcher, MyersSequenceMatcher, '
              'PatienceSequenceMatcher, VSCodeSequenceMatcher and the '
              'native algorithms do not support autojunk)'),
     'def': True,
     'frm': 'bool',
     'chp': 'config',
     },
    {'opt': 'differ.enable_profiling',
     'cmt': _('Enable profiling to trace where compare time is consumed. '
              'Outputs a detailed timing report to the console after each '
              'compare, breaking down time spent in the diff algorithm, '
              'opcode realignment, event generation, char-level diffing, '
              'and UI painting (bookmarks, decor, gaps, attributes). '
              'Use for debugging performance issues only — adds small '
              'overhead (~1-2us per timing point). '
              'Default: off.'),
     'def': False,
     'frm': 'bool',
     'chp': 'config',
     },
    {'opt': 'differ.enable_micromap',
     'cmt': _('Enable the built-in micromap (mini-map) in compare tabs. '
              'Clears the default micromap columns 0 (line states) and '
              '2 (selections), keeps column 1 (bookmarks — also shows '
              'the cursor position), and paints diff-colored line '
              'highlights on column 1 via attr(show_on_map=1). '
              'Note: the micromap does NOT account for inter-line gaps, '
              'so it may desync from the actual text positions when '
              'gaps are present. For a gap-aware alternative, enable '
              'enable_overview instead (or both). '
              'Default: on.'),
     'def': True,
     'frm': 'bool',
     'chp': 'config',
     },
    {'opt': 'differ.enable_overview',
     'cmt': _('Enable the gap-aware overview panel in compare tabs. '
              'The overview is a custom paintbox added to the right '
              'side of the editor. Unlike the built-in micromap, the '
              'overview accounts for inter-line gaps inserted for '
              'visual alignment, so it stays in sync with what you '
              'actually see. Shows both editors side-by-side with '
              'colored rectangles for deleted (red), added (green), '
              'and changed (yellow) lines, plus gray rectangles for '
              'gaps and white for unchanged lines. Click the overview '
              'to scroll the corresponding editor. Can be used together '
              'with the micromap. '
              'Default: off.'),
     'def': False,
     'frm': 'bool',
     'chp': 'config',
     },
]

DIFF_TAB_COUNT = 1
# Persistent state file: stores compare-tab state grouped by session.
# Structure:
# {
#   "sessions": {
#     "<session_key>": {
#       "<compare_tab_id>": {
#         "primary_orig_tab_id": <int>,
#         "primary_orig_name": "...",
#         "secondary_orig_tab_id": <int>,
#         "secondary_orig_name": "...",
#         "saved": true
#       }
#     }
#   }
# }
# session_key is the session file path, relative to the settings folder if
# the session is inside it (at any depth), or the full path if outside.
STATE_FILE = os.path.join(ct.app_path(ct.APP_DIR_SETTINGS), 'cuda_differ_state.json')
# Path to plugins.ini -- used to persistently subscribe to on_start2 so the
# plugin auto-loads on next CudaText startup when compare tabs are active.
PLUGINS_INI = os.path.join(ct.app_path(ct.APP_DIR_SETTINGS), 'plugins.ini')
PLUGINS_INI_SECTION = 'events'
MODULE_NAME = __name__.split('.')[-1]  # e.g. 'cuda_differ'


_homedir = os.path.expanduser('~')

def collapse_filename(fn):
    """Shorten a filename by replacing the home directory with '~'."""
    if (fn+'/').startswith(_homedir+'/'):
        fn = fn.replace(_homedir, '~', 1)
    return fn


def get_opt(key, def_val: tp.Any = ''):
    """Read a 'differ.*' option from the plugin's JSON settings file."""
    return ctx.get_opt('differ.' + key, def_val, user_json=JSONFILE)


def msg(s, level=0):
    """Print a plugin message to the console. level: 0=info, 1=warning, 2=error."""
    if level == 0:
        print(PLG_NAME + ':', s)
    elif level == 1:
        print(PLG_NAME + _(' WARNING:'), s)
    elif level == 2:
        print(PLG_NAME + _(' ERROR:'), s)


class Command:
    def __init__(self):
        self.scroll = ScrollSplittedTab(__name__)
        self.cfg = self.get_config()
        self.diff = self._create_differ()
        # Set to True by on_exit_pre when CudaText is about to exit, so that
        # on_close (which fires next, once per closing tab) can skip
        # temp-file deletion and let compare tabs persist across restarts.
        self._app_exiting = False
        # In-memory cache of saved/unsaved state per compare tab ID.
        # Avoids redundant JSON writes when on_change fires repeatedly
        # without the state actually changing.
        self._saved_cache = {}
        # In-memory set of all compare tab IDs in the current session.
        # Used by _is_compare_tab for fast O(1) lookup without disk I/O --
        # critical because on_change fires on every keystroke.
        self._compare_tab_ids = set()
        # Cached key for the current session (relative path if inside
        # settings folder, full path otherwise). Set in on_start2 and set_files.
        self._current_session_key = ''
        # Counter of on_change events to suppress per compare tab ID.
        # Set to 2 when a compare is created (one per split half) because
        # set_text_all triggers on_change for each half. Prevents the
        # initial green color from being reset to red.
        self._suppress_change = {}
        # Overview panels per compare tab ID. Each value is a
        # PaintboxOverview instance docked to the right of the editor.
        self._overviews = {}

        self.compare_menu = None
        self.menuid_sep = None
        self.menuid_withfile = None
        self.menuid_withtab = None

    def _session_key(self, session_path):
        """Convert a session file path to a state-file key. If the session
        is inside the CudaText settings folder (at any depth), use a path
        relative to settings -- this makes the state file portable. If the
        session is outside settings, use the full path."""
        if not session_path:
            return ''
        settings_dir = ct.app_path(ct.APP_DIR_SETTINGS)
        if settings_dir:
            try:
                rel = os.path.relpath(session_path, settings_dir)
                # If rel doesn't start with '..', session is inside settings.
                if not rel.startswith('..' + os.sep) and rel != '..':
                    return rel
            except ValueError:
                pass
        return session_path

    def _load_state(self):
        """Load the persisted state from disk."""
        try:
            with open(STATE_FILE, 'r', encoding='utf8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {'sessions': {}}
        if not isinstance(data, dict):
            return {'sessions': {}}
        if not isinstance(data.get('sessions'), dict):
            data['sessions'] = {}
        return data

    def _save_state(self, state):
        """Save the state to disk."""
        try:
            with open(STATE_FILE, 'w', encoding='utf8') as f:
                json.dump(state, f, indent=2)
        except OSError as ex:
            msg('failed to save state file: {}'.format(ex), level=2)

    def _is_compare_tab(self, tab_id):
        """Check if the given PROP_TAB_ID belongs to a compare tab.
        Uses an in-memory set for O(1) lookup -- no disk I/O."""
        return str(tab_id) in self._compare_tab_ids

    def _register_compare_tab(self, compare_tab_id, primary_orig_id, secondary_orig_id,
                              primary_orig_name='', secondary_orig_name='', session_key='',
                              saved=True):
        """Register a compare tab under its session key with the PROP_TAB_IDs
        and display names of its two original tabs. 'saved' tracks whether
        the compare tab's content has been synced to the originals."""
        if not session_key:
            session_key = self._current_session_key
        state = self._load_state()
        if session_key not in state['sessions']:
            state['sessions'][session_key] = {}
        state['sessions'][session_key][str(compare_tab_id)] = {
            'primary_orig_tab_id': primary_orig_id,
            'primary_orig_name': primary_orig_name or '',
            'secondary_orig_tab_id': secondary_orig_id,
            'secondary_orig_name': secondary_orig_name or '',
            'saved': saved,
        }
        self._save_state(state)
        self._saved_cache[str(compare_tab_id)] = saved
        self._compare_tab_ids.add(str(compare_tab_id))

    def _set_saved_state(self, compare_tab_id, saved):
        """Update the 'saved' flag for a compare tab. Uses an in-memory
        cache to avoid redundant JSON writes."""
        key = str(compare_tab_id)
        if self._saved_cache.get(key) == saved:
            return
        self._saved_cache[key] = saved
        state = self._load_state()
        session = state['sessions'].get(self._current_session_key, {})
        if key in session:
            session[key]['saved'] = saved
            self._save_state(state)

    def _unregister_compare_tab(self, compare_tab_id):
        """Remove a compare tab from the persisted state. Returns the
        removed entry dict or None if not found."""
        state = self._load_state()
        key = str(compare_tab_id)
        session = state['sessions'].get(self._current_session_key, {})
        entry = session.pop(key, None)
        if entry is not None:
            # Clean up empty session.
            if not session:
                del state['sessions'][self._current_session_key]
            self._save_state(state)
            self._compare_tab_ids.discard(key)
        return entry

    def _get_orig_tab_ids(self, compare_tab_id):
        """Return (primary_orig_id, secondary_orig_id) for a compare tab,
        or (None, None) if not found."""
        state = self._load_state()
        session = state['sessions'].get(self._current_session_key, {})
        entry = session.get(str(compare_tab_id))
        if not isinstance(entry, dict):
            return (None, None)
        return (entry.get('primary_orig_tab_id'), entry.get('secondary_orig_tab_id'))

    def _enable_autostart(self):
        """Persistently subscribe to on_start2 via plugins.ini so the plugin
        auto-loads on next CudaText startup to restore compare tabs."""
        current = ct.ini_read(PLUGINS_INI, PLUGINS_INI_SECTION, MODULE_NAME, '')
        if current == 'on_start2':
            return
        ct.ini_write(PLUGINS_INI, PLUGINS_INI_SECTION, MODULE_NAME, 'on_start2')

    def _disable_autostart(self):
        """Remove the on_start2 subscription from plugins.ini so the plugin
        does NOT auto-load on next startup (no overhead when no compare
        tabs are active)."""
        current = ct.ini_read(PLUGINS_INI, PLUGINS_INI_SECTION, MODULE_NAME, '')
        if not current:
            return
        ct.ini_proc(ct.INI_DELETE_KEY, PLUGINS_INI, PLUGINS_INI_SECTION, MODULE_NAME)

    def change_config(self):
        """Open the options dialog (cuda_options_editor or cuda_prefs) for
        the 'differ.*' settings. After the dialog closes, reload config and
        re-apply sync scroll setting."""
        try:
            import cuda_options_editor as op_ed
        except ImportError:
            import cuda_prefs as op_ed
        op_ed_dlg = None
        subset = 'differ.'  # Key to isolate settings for op_ed plugin
        how = dict(hide_lex_fil=True,  # If option has not setting for lexer/cur.file
                   stor_json=JSONFILE)
        try:  # New op_ed allows to skip meta-file
            op_ed_dlg = op_ed.OptEdD(
                path_keys_info=OPTS_META, subset=subset, how=how)
        except:
            # Old op_ed requires to use meta-file
            if not os.path.exists(METAJSONFILE) \
            or os.path.getmtime(METAJSONFILE) < os.path.getmtime(__file__):
                # Create/update meta-info file
                open(METAJSONFILE, 'w').write(json.dumps(OPTS_META, indent=4))
            op_ed_dlg = op_ed.OptEdD(
                path_keys_info=METAJSONFILE, subset=subset, how=how)
        if op_ed_dlg.show(_('Differ Options')):  # Dialog caption
            # Need to use updated options
            self.config()
            self.scroll.toggle(self.cfg['sync_scroll'])
            # self.scroll.enable_sync_caret = self.cfg['enable_sync_caret']

    def on_cli(self, fn1, fn2):
        """Called when CudaText gets command-line param -p=cuda_differ#file1#file2.
        Opens both files first (so they exist as tabs), then compares them."""
        # Open both files. file_open activates the tab, so after opening fn2,
        # ct.ed points to fn2. We pass the filenames to set_files which finds
        # them by filename.
        ct.file_open(fn1)
        ct.file_open(fn2)
        self.set_files(fn1, fn2)

    def compare_with(self):
        """Compare current document with a file picked from a dialog.
        If the chosen file is not already open, open it first."""
        fn0 = self.get_name(ct.ed)
        fn = ct.dlg_file(True, '!', '', '')
        if not fn:
            return
        # Check if the file is already open in a tab.
        already_open = False
        for h in ct.ed_handles():
            if ct.Editor(h).get_prop(ct.PROP_FN, '') == fn:
                already_open = True
                break
        if not already_open:
            # Open the file so set_files can find it as a tab.
            ct.file_open(fn)
        self.set_files(fn0, fn)

    def compare_with_tab(self):
        """Compare current document with another open tab, picked from a menu."""
        name0 = self.get_name(ct.ed)
        names = []
        for h in ct.ed_handles():
            e = ct.Editor(h)
            if self.is_match_name(e, name0):
                continue
            names.append(self.get_name(e))
        if not names:
            return

        res = ct.dlg_menu(ct.DMENU_LIST, names, caption=_('Compare file with tab'))
        if res is None:
            return
        name = names[res]
        self.set_files(name0, name)

    def diff_with(self):
        """Create a unified-diff output (read-only tab) comparing current
        document with a file picked from a dialog."""
        fn0 = self.get_name(ct.ed)
        fn = ct.dlg_file(True, '!', '', '')
        if not fn:
            return

        a = ct.ed.get_text_all()
        # Read file b by opening it in CudaText (handles all encodings
        # correctly -- CudaText's encoding names like utf16le, koi8u, etc.
        # don't always match Python's codec names).
        h_orig = ct.ed.get_prop(ct.PROP_HANDLE_SELF)
        ct.file_open(fn, options='/nohistory')
        b = ct.ed.get_text_all()
        ct.ed.cmd(ct_cmd.cmd_FileClose)
        # Restore focus to the original editor.
        if h_orig:
            ct.Editor(h_orig).focus()
        self.create_diff(a, b, fn0, fn)

    def diff_with_tab(self):
        """Create a unified-diff output comparing current document with
        another open tab, picked from a menu."""
        name0 = self.get_name(ct.ed)

        names = []
        ed = []
        for h in ct.ed_handles():
            e = ct.Editor(h)
            if self.is_match_name(e, name0):
                continue
            names.append(self.get_name(e))
            ed.append(h)
        if not names:
            return

        res = ct.dlg_menu(ct.DMENU_LIST, names, caption=_('Diff file with tab'))
        if res is None:
            return

        name = names[res]
        a = ct.ed.get_text_all()
        b = ct.Editor(ed[res]).get_text_all()

        self.create_diff(a, b, name0, name)

    def format_untitled(self, e):
        """Return a display name for an untitled tab: 'untitled:TITLE [TAB_ID]'.
        The [TAB_ID] suffix is used by is_match_name for robust identification."""
        return U_PREFIX + e.get_prop(ct.PROP_TAB_TITLE) + ' [%d]'%e.get_prop(ct.PROP_TAB_ID)

    def is_match_name(self, e, name):
        """Check if editor 'e' matches the given 'name' identifier.

        For untitled tabs: extracts the tab ID from the [id] suffix in the
        name string and matches on ID alone. This is robust against title
        changes between menu build and menu action execution -- only the
        persistent, immutable PROP_TAB_ID is compared.

        For titled tabs: matches by filename (the file's identity)."""
        if name.startswith(U_PREFIX):
            # Extract the tab ID from the [id] suffix at the end of the string.
            # format_untitled always appends ' [ID]' at the end, so the last
            # [...] is always the ID. rfind finds it even if the title itself
            # contains brackets.
            i = name.rfind('[')
            j = name.rfind(']')
            if i > 0 and j > i:
                id_str = name[i+1:j]
                if id_str.isdigit():
                    return str(e.get_prop(ct.PROP_TAB_ID)) == id_str
            # Fallback: full string comparison (for old/odd-format strings)
            return name == self.format_untitled(e)
        fn = e.get_prop(ct.PROP_FN, '')
        if fn:
            return fn==name
        return False

    def set_files(self, file0, file1):
        """Compare two files/tabs in a single split tab without temp files.

        Creates a new untitled tab, unlinks the split editors (so each half
        has independent text), splits vertically, then loads each original's
        content and editor properties into the two halves."""
        files = [file0, file1]
        # Properties to copy from originals to the compare halves.
        # These affect how text is displayed and interpreted.
        _PROPS_TO_COPY = [
            ct.PROP_LEXER_FILE,
            ct.PROP_NEWLINE,
            ct.PROP_ENC,
            ct.PROP_TAB_SPACES,
            ct.PROP_TAB_SIZE,
            ct.PROP_WRAP,
        ]
        orig_props = [None, None]  # list of prop-value dicts per half
        orig_tab_ids = [None, None]
        orig_texts = [None, None]
        orig_names = ['', '']

        # Find the two original tabs, grab their content, properties, tab ID,
        # and a display name (file path for real files, title for untitled).
        for (index, name) in enumerate(files):
            for h in ct.ed_handles():
                e = ct.Editor(h)
                if self.is_match_name(e, name):
                    orig_tab_ids[index] = e.get_prop(ct.PROP_TAB_ID)
                    orig_texts[index] = e.get_text_all()
                    fn = e.get_prop(ct.PROP_FN, '')
                    if fn:
                        orig_names[index] = fn
                    else:
                        orig_names[index] = e.get_prop(ct.PROP_TAB_TITLE) or ''
                    # Capture all properties to copy.
                    orig_props[index] = {p: e.get_prop(p) for p in _PROPS_TO_COPY}
                    break

        # Bail out if we couldn't find both originals.
        if orig_texts[0] is None or orig_texts[1] is None:
            return

        # Create a new untitled tab for the compare view.
        ct.file_open('')

        # Unlink the split editors so each half has independent text,
        # then split vertically into two halves.
        ct.ed.set_prop(ct.PROP_EDITORS_LINKED, False)
        ct.ed.set_prop(ct.PROP_SPLIT, ('v', 500))

        # Load each original's content into the two split halves.
        a_ed = ct.Editor(ct.ed.get_prop(ct.PROP_HANDLE_PRIMARY))
        b_ed = ct.Editor(ct.ed.get_prop(ct.PROP_HANDLE_SECONDARY))
        a_ed.set_text_all(orig_texts[0])
        b_ed.set_text_all(orig_texts[1])

        # Set a readable combined title (just the basenames/titles, no tab IDs).
        title0 = os.path.basename(orig_names[0]) if orig_names[0] else _('Untitled')
        title1 = os.path.basename(orig_names[1]) if orig_names[1] else _('Untitled')
        ct.ed.set_prop(ct.PROP_TAB_TITLE, 'Diff: {} | {}'.format(title0, title1))

        # Copy editor properties (lexer, newline, encoding, tabs, wrap) from
        # each original to its corresponding compare half.
        for ed, props in ((a_ed, orig_props[0]), (b_ed, orig_props[1])):
            if not props:
                continue
            for prop, val in props.items():
                if val is not None:
                    try:
                        ed.set_prop(prop, val)
                    except Exception:
                        pass  # some props may not be settable on untitled tabs

        # Register the compare tab by its PROP_TAB_ID with the original
        # tab IDs and names, plus the session key for grouping.
        compare_tab_id = ct.ed.get_prop(ct.PROP_TAB_ID)
        try:
            session_path = ct.app_path(ct.APP_FILE_SESSION) or ''
        except Exception:
            session_path = ''
        session_key = self._session_key(session_path)
        self._current_session_key = session_key
        self._register_compare_tab(
            compare_tab_id,
            orig_tab_ids[0], orig_tab_ids[1],
            orig_names[0], orig_names[1],
            session_key,
            saved=True)  # initial state: content matches originals = saved

        # Color the tab title green to indicate 'synced' (no unsaved
        # changes yet -- content is identical to the originals).
        ct.ed.set_prop(ct.PROP_TAB_COLOR_FONT, 0x00A000)  # green

        # Suppress the next 2 on_change events (one per split half)
        # because set_text_all triggers on_change, which would reset the
        # green color to red. The counter is decremented in on_change;
        # real user edits after this will work normally.
        self._suppress_change[str(compare_tab_id)] = 2

        # Persistently subscribe to on_start2 so the plugin auto-loads on
        # next startup to restore compare tabs.
        self._enable_autostart()

        # Track this tab for scroll sync.
        self.scroll.tab_id.add(compare_tab_id)
        self.scroll.toggle(self.cfg.get('sync_scroll'))

        # app sets LastLineOnTop automatically on adding 'gaps', but if file
        # don't have gaps, we must set it manually.
        a_ed.set_prop(ct.PROP_LAST_LINE_ON_TOP, True)
        b_ed.set_prop(ct.PROP_LAST_LINE_ON_TOP, True)

        # if file was in group-2, and now group-2 is empty, set "one group" mode
        if ct.app_proc(ct.PROC_GET_GROUPING, '') in [ct.GROUPS_2VERT, ct.GROUPS_2HORZ]:
            e = ct.ed_group(1)
            if not e:
                ct.app_proc(ct.PROC_SET_GROUPING, ct.GROUPS_ONE)

        self.refresh()

    def create_diff(self, txt0, txt1, fn0, fn1):
        """Create a read-only unified-diff tab from two text strings.
        Used by diff_with and diff_with_tab commands."""
        if txt0 and txt0[-1] != '\n': txt0 += '\n'
        if txt1 and txt1[-1] != '\n': txt1 += '\n'
        a = txt0.splitlines(True)
        b = txt1.splitlines(True)
        r = self.diff.unidiff(a, b, fn0, fn1, self.cfg.get('diff_context'))

        global DIFF_TAB_COUNT
        tab = 'Diff ' + str(DIFF_TAB_COUNT)
        DIFF_TAB_COUNT += 1

        ct.file_open('')
        ct.ed.set_text_all(r)
        ct.ed.set_prop(ct.PROP_LEXER_FILE, 'Diff')
        ct.ed.set_prop(ct.PROP_RO, True)
        ct.ed.set_prop(ct.PROP_TAB_TITLE, tab)
        ct.ed.set_prop(ct.PROP_SAVE_HISTORY, False)

    def on_state(self, ed_self, state):
        """Handle theme syntax changes (reload config + refresh) and word-wrap
        state changes (re-apply gaps with wrap-aware sizes)."""
        if state == ct.APPSTATE_THEME_SYNTAX:
            self.get_config()
            self._refresh_ex(ct.ed)  # automatic -- no dialog
        elif state == ct.EDSTATE_WRAP:
            # Word-wrap mode changed on one of the split halves. The
            # inter-line gaps were sized for the previous wrap state, so
            # we must re-apply them with wrap-aware sizes to keep both
            # sides visually aligned.
            if self._is_compare_tab(ed_self.get_prop(ct.PROP_TAB_ID)):
                self._refresh_ex(ed_self)  # automatic -- no dialog

    def on_scroll(self, ed_self):
        """Forward scroll events to ScrollSplittedTab for synchronized
        scrolling, and repaint the overview (cursor position marker)."""
        tab_id = ed_self.get_prop(ct.PROP_TAB_ID)
        if self._is_compare_tab(tab_id):
            self.scroll.on_scroll(ed_self)
            # Repaint the overview to update the cursor position marker.
            overview = self._overviews.get(str(tab_id))
            if overview is not None:
                overview.paint()

    def on_caret(self, ed_self):
        """Mirror caret to opposite editor when sync_caret is enabled."""
        if self.cfg.get('enable_sync_caret', False):
            self.sync_caret()

    def on_change(self, ed_self):
        """Fires immediately on every keystroke. Used for:
        - Resetting the compare tab title color from green to default (red)
          when the user makes changes (indicating unsaved edits).
        - Persisting the 'unsaved' state so it survives restarts.

        Uses on_change (not on_change_slow) because on_change_slow has a
        1-2 second delay which causes race conditions: if the user edits
        then quickly saves, the delayed on_change_slow would fire AFTER
        on_save_pre and reset the green color back to red.

        The first 2 calls after a compare is created are suppressed (see
        _suppress_change) because set_text_all triggers spurious on_change
        events that would reset the initial green color.

        Performance: _is_compare_tab uses an in-memory set (no disk I/O),
        so non-compare tabs return in O(1). The handler is lightweight
        enough for on_change."""
        tab_id = ed_self.get_prop(ct.PROP_TAB_ID)
        if not self._is_compare_tab(tab_id):
            return
        key = str(tab_id)
        if key in self._suppress_change:
            self._suppress_change[key] -= 1
            if self._suppress_change[key] <= 0:
                del self._suppress_change[key]
            # Skip color change and state write for this spurious event.
        else:
            # Real user edit -- reset title color to default (red).
            ed_self.set_prop(ct.PROP_TAB_COLOR_FONT, ct.COLOR_NONE)
            # Persist the unsaved state so on_start2 can restore the
            # correct color after restart.
            self._set_saved_state(tab_id, False)

    def on_change_slow(self, ed_self):
        """Fires after the user edits and a short pause passes. Used only
        for auto-refreshing the diff markers if that option is enabled.
        Color/saved-state logic is handled in on_change (immediate)."""
        if self.cfg.get('enable_auto_refresh', False):
            self._refresh_ex(ed_self)  # automatic -- no dialog

    def on_save_pre(self, ed_self):
        """Intercept Ctrl+S in a compare tab. Instead of saving the untitled
        compare tab to disk (which would show a Save dialog), sync both
        halves' content back to the original tabs and block the save.
        Returns False to block the default save behavior.

        On successful sync, the compare tab's title font is colored green
        to indicate 'synced'. The color is reset to COLOR_NONE (which
        CudaText re-colors red) when the user edits again -- see on_change_slow.
        We do NOT clear PROP_MODIFIED, because that would prevent CudaText's
        session auto-save/restore from persisting the compare tab's content
        across restarts."""
        tab_id = ed_self.get_prop(ct.PROP_TAB_ID)
        if not self._is_compare_tab(tab_id):
            return  # not a compare tab -- let CudaText handle normally

        # Get both split editors.
        a_ed = ct.Editor(ed_self.get_prop(ct.PROP_HANDLE_PRIMARY))
        b_ed = ct.Editor(ed_self.get_prop(ct.PROP_HANDLE_SECONDARY))

        # Look up the original tab IDs from the persisted state.
        orig_a_id, orig_b_id = self._get_orig_tab_ids(tab_id)

        synced_any = False
        if orig_a_id is not None:
            if self._sync_to_original_by_id(orig_a_id, a_ed.get_text_all()):
                synced_any = True
        if orig_b_id is not None:
            if self._sync_to_original_by_id(orig_b_id, b_ed.get_text_all()):
                synced_any = True

        if synced_any:
            # Color the tab title font green to indicate 'synced'.
            # Do NOT clear PROP_MODIFIED -- that would break session
            # auto-save/restore for the compare tab.
            ed_self.set_prop(ct.PROP_TAB_COLOR_FONT, 0x00A000)  # green
            # Persist the saved state so on_start2 can restore green
            # color after restart.
            self._set_saved_state(tab_id, True)
            # Clear any pending suppress counter -- save overrides the
            # initial-creation suppress.
            self._suppress_change.pop(str(tab_id), None)
            # Auto-refresh diff markers so the user sees updated
            # highlights without needing to click Refresh manually.
            self._refresh_ex(ed_self)  # automatic -- no dialog

        # Block the default save (which would show a Save dialog for the
        # untitled compare tab).
        return False

    def _sync_to_original_by_id(self, orig_tab_id, new_text):
        """Find the original tab by PROP_TAB_ID and overwrite its content
        (preserving Undo via replace_lines). If the original is a real file
        on disk, save it. If untitled, mark it modified. Returns True if
        the original was found."""
        target = str(orig_tab_id)
        for h in ct.ed_handles():
            e = ct.Editor(h)
            if str(e.get_prop(ct.PROP_TAB_ID)) == target:
                self._apply_text_preserving_undo(e, new_text)
                orig_fn = e.get_prop(ct.PROP_FN, '')
                if orig_fn:
                    # Real file on disk -- save it immediately.
                    e.save()
                else:
                    # Untitled tab -- mark modified, no Save dialog.
                    e.set_prop(ct.PROP_MODIFIED, True)
                ct.msg_status(_('Differ: synced changes to original tab'))
                return True
        ct.msg_status(_('Differ: original tab no longer open'))
        return False

    def _apply_text_preserving_undo(self, ed, new_text):
        """Replace the entire editor text while preserving Undo history.
        Uses replace_lines() instead of set_text_all() which would destroy
        Undo information."""
        caret = ed.get_carets()
        try:
            lines = new_text.split('\n')
            count = ed.get_line_count()
            if count > 0:
                ed.replace_lines(0, count - 1, lines)
            else:
                ed.insert(0, 0, new_text)
        except Exception as ex:
            msg('replace_lines failed, falling back to set_text_all: {}'.format(ex), level=1)
            ed.set_text_all(new_text)
        if caret:
            x, y, x2, y2 = caret[0]
            try:
                ed.set_caret(x, y, x2, y2)
            except Exception:
                pass

    def on_start2(self, ed_self):
        """Called once on program start, after configs are applied and just
        before the main form shows.

        Performs startup cleanup: removes dead records (compare tabs that
        were not restored by CudaText -- e.g. empty untitled tabs are
        discarded by CudaText on restart). Then rebuilds in-memory caches,
        re-subscribes to on_scroll, re-applies title colors, and re-applies
        diff markers for each surviving compare tab.

        We use on_start2 (not on_start) because on_start fires too early --
        before session restore completes. By on_start2, all editors exist
        and CudaText has finished restoring the modified flag/tab colors."""
        # Get the current session and cache its key.
        try:
            session_path = ct.app_path(ct.APP_FILE_SESSION) or ''
        except Exception:
            session_path = ''
        self._current_session_key = self._session_key(session_path)

        state = self._load_state()

        # --- Cleanup dead records ---
        # Get all open tab IDs so we can check which compare tabs still exist.
        open_tab_ids = set()
        for h in ct.ed_handles():
            e = ct.Editor(h)
            open_tab_ids.add(str(e.get_prop(ct.PROP_TAB_ID)))

        # Check the current session's compare tabs. If a compare tab ID is
        # not in open_tab_ids, it's a dead record (CudaText didn't restore it
        # -- e.g. it was an empty untitled tab that CudaText discards).
        session = state['sessions'].get(self._current_session_key, {})
        dead_keys = [k for k in session if k not in open_tab_ids]
        for k in dead_keys:
            del session[k]
        if not session and self._current_session_key in state['sessions']:
            # Clean up empty session.
            del state['sessions'][self._current_session_key]
        if dead_keys:
            self._save_state(state)

        # --- Rebuild in-memory caches and apply colors/markers ---
        self.scroll.tab_id = set()
        self._compare_tab_ids = set()
        for tab_id_str, entry in session.items():
            if not isinstance(entry, dict):
                continue
            try:
                self.scroll.tab_id.add(int(tab_id_str))
            except (ValueError, TypeError):
                pass
            self._compare_tab_ids.add(tab_id_str)
            # Populate the saved-state cache from disk.
            self._saved_cache[tab_id_str] = entry.get('saved', True)
            # Find an editor for this compare tab and re-apply diff markers.
            for h in ct.ed_handles():
                e = ct.Editor(h)
                if str(e.get_prop(ct.PROP_TAB_ID)) == tab_id_str:
                    self._refresh_ex(e)
                    break
            # Re-apply the title color based on the persisted 'saved' flag.
            if entry.get('saved', True):
                self._apply_color_to_tab(tab_id_str, 0x00A000)  # green
        # Re-subscribe to on_scroll event if sync_scroll is enabled.
        if self.cfg.get('sync_scroll') and self.scroll.tab_id:
            ct.app_proc(ct.PROC_EVENTS_SUB, self.scroll.name+';on_scroll;;')

    def _apply_color_to_tab(self, tab_id_str, color):
        """Apply a title font color to a compare tab by its PROP_TAB_ID.
        Used by on_start2 to restore the green/default color after restart."""
        target = str(tab_id_str)
        for h in ct.ed_handles():
            e = ct.Editor(h)
            if str(e.get_prop(ct.PROP_TAB_ID)) == target:
                e.set_prop(ct.PROP_TAB_COLOR_FONT, color)
                return

    def on_exit_pre(self, ed_self):
        """Called before CudaText is about to exit. Sets a flag so that
        on_close (which fires next, once per closing tab) can skip
        temp-file deletion and let compare tabs persist across restarts."""
        self._app_exiting = True

    '''
    def on_tab_change(self, ed_self):
        self.config()
        self.scroll.toggle(self.cfg.get('sync_scroll'))
    '''

    def on_tab_menu(self, ed_self):
        """Build the right-click tab context menu (Compare with..., Refresh, etc.)."""
        self.tabmenu_init(ed_self)

    def refresh(self):
        """Manual refresh (from menu command or context menu). Shows the
        'identical' dialog if both sides are equal. Only applies to compare
        tabs managed by this plugin."""
        self._refresh_ex(ct.ed, show_dialog=True)

    def _create_differ(self):
        """Create the appropriate Differ instance based on the configured
        algorithm. Returns a differ_native.Differ for native algorithms
        (when the native API is available), or a differ_python.Differ for
        all Python algorithms and as a fallback when native is unavailable.
        """
        algo = self.cfg.get('diff_algorithm', 'native_histogram')
        if algo in ('native_histogram', 'native_myers') and dfn._HAS_NATIVE_DIFF:
            return dfn.Differ()
        return dfp.Differ()

    def _ensure_correct_differ(self):
        """Check if self.diff matches the configured algorithm type, and
        swap it if not. Called at the start of _refresh_ex so the Differ
        is always the right type before a compare runs. Preserves the
        sequences and options from the old Differ."""
        algo = self.cfg.get('diff_algorithm', 'native_histogram')
        want_native = algo in ('native_histogram', 'native_myers') and dfn._HAS_NATIVE_DIFF
        is_native = isinstance(self.diff, dfn.Differ)
        if want_native == is_native:
            return  # already the right type
        # Swap: preserve sequences and options
        old_a = getattr(self.diff, 'a', '')
        old_b = getattr(self.diff, 'b', '')
        old_withdetail = getattr(self.diff, 'withdetail', True)
        old_autojunk = getattr(self.diff, 'autojunk', True)
        old_ratio = getattr(self.diff, 'ratio', 0.75)
        self.diff = dfn.Differ() if want_native else dfp.Differ()
        self.diff.set_seqs(old_a, old_b)
        self.diff.withdetail = old_withdetail
        self.diff.autojunk = old_autojunk
        self.diff.ratio = old_ratio
        self.diff.diff_algorithm = algo

    def _refresh_ex(self, ed, show_dialog=False):
        """Core refresh logic. 'ed' is any editor belonging to the compare
        tab. Only applies to compare tabs managed by this plugin.

        'show_dialog' controls whether the 'two sides are identical' dialog
        is shown. Automatic refreshes (on_start2, on_change_slow, on_state)
        pass False to avoid pestering the user; manual refresh and the
        initial compare pass True."""
        if ed is None:
            return
        if ed.get_prop(ct.PROP_EDITORS_LINKED):
            return
        tab_id = ed.get_prop(ct.PROP_TAB_ID)
        if not self._is_compare_tab(tab_id):
            return  # not a compare tab we manage -- skip

        # Load config FIRST so the profiling check below sees the current
        # value of enable_profiling. Without this, the first compare after
        # enabling profiling in the config dialog would not be profiled
        # (self.cfg would still have the old value).
        self.config()

        # Enable/disable profiling based on config. reset() clears any
        # stale data from a previous compare so the report only shows
        # this compare's timings.
        _profiling_was_enabled = Profiler.is_enabled()
        _profiling_enabled_here = False
        if not _profiling_was_enabled:
            try:
                _do_profile = self.cfg.get('enable_profiling', False)
            except Exception:
                _do_profile = False
            if _do_profile:
                enable_profiling(True)
                _profiling_enabled_here = True
        if Profiler.is_enabled():
            reset_profiling()

        # Wrap the entire compare in try/finally so the profiling report
        # is always printed — even if the compare crashes with an exception.
        try:
            Profiler.start('refresh:total')

            a_ed = ct.Editor(ed.get_prop(ct.PROP_HANDLE_PRIMARY))
            b_ed = ct.Editor(ed.get_prop(ct.PROP_HANDLE_SECONDARY))

            # Set up the micromap on both editors when enabled.
            micromap_on = self.cfg.get('enable_micromap', True)
            if micromap_on:
                self._setup_micromap(a_ed, b_ed)

            # Create or reuse the paintbox overview for this compare tab.
            # The overview is a custom paintbox added to the right side
            # of the editor's parent form. It shows a gap-aware mini-map
            # of both editors side-by-side (unlike the micromap, which
            # doesn't account for the gaps we insert for alignment).
            tab_id_str = str(tab_id)
            overview = self._overviews.get(tab_id_str)
            overview_on = self.cfg.get('enable_overview', False)
            if overview_on:
                if overview is None:
                    overview = PaintboxOverview()
                    overview.create(a_ed, b_ed)
                    self._overviews[tab_id_str] = overview
                else:
                    overview.a_ed = a_ed
                    overview.b_ed = b_ed
                overview.set_colors(
                    0xFFFFFF,  # background: white (unchanged lines)
                    self.cfg.get('color_deleted'),
                    self.cfg.get('color_added'),
                    self.cfg.get('color_changed'),
                    self.cfg.get('color_gaps'))
                overview.clear_data()
            elif overview is not None:
                overview.destroy()
                del self._overviews[tab_id_str]
                overview = None

            Profiler.start('refresh:get_text')
            a_text_all = a_ed.get_text_all()
            b_text_all = b_ed.get_text_all()
            Profiler.stop('refresh:get_text')

            if not a_text_all.endswith('\n'):
                a_text_all += '\n'
            if not b_text_all.endswith('\n'):
                b_text_all += '\n'

            if a_text_all == b_text_all:
                Profiler.start('refresh:clear')
                self.clear(a_ed)
                self.clear(b_ed)
                Profiler.stop('refresh:clear')
                self.diff.diffmap = []
                if show_dialog:
                    t = _('The two sides are identical.')
                    ct.msg_box(t, ct.MB_OK)
                Profiler.stop('refresh:total')
                return

            # NOTE: Do NOT force word-wrap off here. The user may legitimately
            # want to compare files with wrap on (it makes long lines easier to
            # read). Instead, we detect the wrap state below and size the
            # inter-line gaps using the actual number of wrapped visual rows,
            # so the two sides stay visually aligned even when corresponding
            # lines wrap to different heights. See _get_wrap_counts and the
            # A_GAP/B_GAP/ALIGN handling in the loop below.
            # a_ed.set_prop(ct.PROP_WRAP, ct.WRAP_OFF)
            # b_ed.set_prop(ct.PROP_WRAP, ct.WRAP_OFF)

            Profiler.start('refresh:clear')
            self.clear(a_ed)
            self.clear(b_ed)
            Profiler.stop('refresh:clear')

            # config() was already called above (before the profiling check).
            # Don't call it again here.

            # Ensure the Differ instance matches the configured algorithm
            # type (native vs Python). Swaps if the user changed the
            # algorithm in config since the last compare.
            self._ensure_correct_differ()

            Profiler.start('refresh:splitlines')
            self.diff.set_seqs(a_text_all.splitlines(True),
                               b_text_all.splitlines(True))
            Profiler.stop('refresh:splitlines')

            self.scroll.tab_id.add(tab_id)
            self.scroll.toggle(self.cfg.get('sync_scroll'))

            self.diff.withdetail = self.cfg.get('compare_with_details')
            self.diff.ratio = self.cfg.get('ratio')
            self.diff.diff_algorithm = self.cfg.get('diff_algorithm')
            self.diff.autojunk = self.cfg.get('autojunk')

            # Detect word-wrap on either side. When wrap is on, gaps must be
            # sized by the actual number of visual rows on the opposite side
            # (not by logical line count), and matched line pairs that wrap to
            # different heights need an extra compensating gap.
            wrap_a = a_ed.get_prop(ct.PROP_WRAP)
            wrap_b = b_ed.get_prop(ct.PROP_WRAP)
            wrap_on = (wrap_a != ct.WRAP_OFF) or (wrap_b != ct.WRAP_OFF)
            if wrap_on:
                Profiler.start('refresh:wrap_counts')
                wrap_counts_a = self._get_wrap_counts(a_ed)
                wrap_counts_b = self._get_wrap_counts(b_ed)
                __, line_h_a = a_ed.get_prop(ct.PROP_CELL_SIZE)
                __, line_h_b = b_ed.get_prop(ct.PROP_CELL_SIZE)
                Profiler.stop('refresh:wrap_counts')
            else:
                wrap_counts_a = None
                wrap_counts_b = None
                line_h_a = 0
                line_h_b = 0
            color_gaps = self.cfg.get('color_gaps')
            overview_on = self.cfg.get('enable_overview', False)

            # The for loop below consumes events from diff.compare() (a
            # generator) and paints each event. Profiling the loop as a whole
            # captures both compare time (inside the generator) and paint time
            # (inside the loop body). The paint:* sub-sections break down the
            # paint time by operation type. The compare:* sub-sections (from
            # differ.py) break down the compare time by algorithm phase.
            #
            # Bookmarks are NOT set immediately in the loop. Instead, they
            # are collected into pending_bkm_a / pending_bkm_b lists and
            # appended in sorted order after the loop using BOOKMARK2_APPEND
            # (which is much faster than BOOKMARK2_SET but requires sorted
            # input and a manual repaint).
            #
            # Overview line states and gaps are also collected for the
            # paintbox overview (gap-aware mini-map) when enabled.
            # Micromap line highlights are painted via attr(show_on_map=1)
            # when micromap is enabled.
            pending_bkm_a = []  # list of (line, nkind) for a_ed
            pending_bkm_b = []  # list of (line, nkind) for b_ed
            Profiler.start('refresh:compare_and_paint')
            for d in self.diff.compare():
                diff_id, y = d[0], d[1]
                if diff_id == df.A_LINE_DEL:
                    pending_bkm_a.append((y, NKIND_DELETED))
                    Profiler.start('paint:decor')
                    self.set_decor(a_ed, y, DECOR_CHAR, self.cfg.get('color_deleted'))
                    Profiler.stop('paint:decor')
                    if micromap_on:
                        Profiler.start('paint:micromap')
                        self.set_attr(a_ed, y=y, bg=self.cfg.get('color_deleted'),
                                     mptag=1, map_only=1)
                        Profiler.stop('paint:micromap')
                    if overview is not None:
                        overview.add_line_state('a', y, self.cfg.get('color_deleted'))
                elif diff_id == df.B_LINE_ADD:
                    pending_bkm_b.append((y, NKIND_ADDED))
                    Profiler.start('paint:decor')
                    self.set_decor(b_ed, y, DECOR_CHAR, self.cfg.get('color_added'))
                    Profiler.stop('paint:decor')
                    if micromap_on:
                        Profiler.start('paint:micromap')
                        self.set_attr(b_ed, y=y, bg=self.cfg.get('color_added'),
                                     mptag=1, map_only=1)
                        Profiler.stop('paint:micromap')
                    if overview is not None:
                        overview.add_line_state('b', y, self.cfg.get('color_added'))
                elif diff_id == df.A_LINE_CHANGE:
                    pending_bkm_a.append((y, NKIND_CHANGED))
                    if micromap_on:
                        Profiler.start('paint:micromap')
                        self.set_attr(a_ed, y=y, bg=self.cfg.get('color_changed'),
                                     mptag=1, map_only=1)
                        Profiler.stop('paint:micromap')
                    if overview is not None:
                        overview.add_line_state('a', y, self.cfg.get('color_changed'))
                elif diff_id == df.B_LINE_CHANGE:
                    pending_bkm_b.append((y, NKIND_CHANGED))
                    if micromap_on:
                        Profiler.start('paint:micromap')
                        self.set_attr(b_ed, y=y, bg=self.cfg.get('color_changed'),
                                     mptag=1, map_only=1)
                        Profiler.stop('paint:micromap')
                    if overview is not None:
                        overview.add_line_state('b', y, self.cfg.get('color_changed'))
                elif diff_id == df.A_GAP:
                    a_line_after, b_start, b_end = d[1], d[2], d[3]
                    if wrap_on:
                        Profiler.start('paint:wrap_calc')
                        total_visual = self._sum_visual_rows(
                            wrap_counts_b, b_start, b_end)
                        Profiler.stop('paint:wrap_calc')
                        Profiler.start('paint:gap')
                        self._add_raw_gap(a_ed, a_line_after - 1,
                                          total_visual * line_h_a, color_gaps)
                        Profiler.stop('paint:gap')
                        if overview is not None:
                            # Gap appears BEFORE a_line_after (between lines
                            # a_line_after-1 and a_line_after)
                            overview.add_gap('a', a_line_after, total_visual)
                    else:
                        Profiler.start('paint:gap')
                        self.set_gap(a_ed, a_line_after, b_end - b_start)
                        Profiler.stop('paint:gap')
                        if overview is not None:
                            overview.add_gap('a', a_line_after, b_end - b_start)
                elif diff_id == df.B_GAP:
                    b_line_after, a_start, a_end = d[1], d[2], d[3]
                    if wrap_on:
                        Profiler.start('paint:wrap_calc')
                        total_visual = self._sum_visual_rows(
                            wrap_counts_a, a_start, a_end)
                        Profiler.stop('paint:wrap_calc')
                        Profiler.start('paint:gap')
                        self._add_raw_gap(b_ed, b_line_after - 1,
                                          total_visual * line_h_b, color_gaps)
                        Profiler.stop('paint:gap')
                        if overview is not None:
                            overview.add_gap('b', b_line_after, total_visual)
                    else:
                        Profiler.start('paint:gap')
                        self.set_gap(b_ed, b_line_after, a_end - a_start)
                        Profiler.stop('paint:gap')
                        if overview is not None:
                            overview.add_gap('b', b_line_after, a_end - a_start)
                elif diff_id == df.ALIGN:
                    if wrap_on:
                        a_line, b_line = d[1], d[2]
                        Profiler.start('paint:wrap_calc')
                        va = self._visual_rows(wrap_counts_a, a_line)
                        vb = self._visual_rows(wrap_counts_b, b_line)
                        Profiler.stop('paint:wrap_calc')
                        Profiler.start('paint:gap')
                        if va > vb:
                            diff_rows = va - vb
                            self._add_raw_gap(b_ed, b_line,
                                              diff_rows * line_h_b, color_gaps)
                            if overview is not None:
                                overview.add_gap('b', b_line, diff_rows)
                        elif vb > va:
                            diff_rows = vb - va
                            self._add_raw_gap(a_ed, a_line,
                                              diff_rows * line_h_a, color_gaps)
                            if overview is not None:
                                overview.add_gap('a', a_line, diff_rows)
                        Profiler.stop('paint:gap')
                elif diff_id == df.A_SYMBOL_DEL:
                    Profiler.start('paint:attr')
                    self.set_attr(a_ed, d[2], y, d[3], self.cfg.get('color_deleted'))
                    Profiler.stop('paint:attr')
                elif diff_id == df.B_SYMBOL_ADD:
                    Profiler.start('paint:attr')
                    self.set_attr(b_ed, d[2], y, d[3], self.cfg.get('color_added'))
                    Profiler.stop('paint:attr')
                elif diff_id == df.A_DECOR_YELLOW:
                    Profiler.start('paint:decor')
                    self.set_decor(a_ed, y, DECOR_CHAR, self.cfg.get('color_changed'))
                    Profiler.stop('paint:decor')
                elif diff_id == df.B_DECOR_YELLOW:
                    Profiler.start('paint:decor')
                    self.set_decor(b_ed, y, DECOR_CHAR, self.cfg.get('color_changed'))
                    Profiler.stop('paint:decor')
                elif diff_id == df.A_DECOR_RED:
                    Profiler.start('paint:decor')
                    self.set_decor(a_ed, y, DECOR_CHAR, self.cfg.get('color_deleted'))
                    Profiler.stop('paint:decor')
                elif diff_id == df.B_DECOR_GREEN:
                    Profiler.start('paint:decor')
                    self.set_decor(b_ed, y, DECOR_CHAR, self.cfg.get('color_added'))
                    Profiler.stop('paint:decor')
            Profiler.stop('refresh:compare_and_paint')

            # Append all collected bookmarks in sorted order using
            # BOOKMARK2_APPEND (much faster than BOOKMARK2_SET — skips
            # duplicate search, sorting, event firing, and repainting).
            # BOOKMARK2_APPEND requires bookmarks to be added in ascending
            # line order, so we sort first. After appending, we manually
            # repaint both editors via EDACTION_UPDATE.
            Profiler.start('paint:bookmark')
            pending_bkm_a.sort()
            pending_bkm_b.sort()
            for row, nk in pending_bkm_a:
                a_ed.bookmark(ct.BOOKMARK2_APPEND, row,
                              nkind=nk, text='', auto_del=True,
                              show=False, tag=DIFF_TAG)
            for row, nk in pending_bkm_b:
                b_ed.bookmark(ct.BOOKMARK2_APPEND, row,
                              nkind=nk, text='', auto_del=True,
                              show=False, tag=DIFF_TAG)
            # BOOKMARK2_APPEND doesn't repaint — force it.
            a_ed.action(ct.EDACTION_UPDATE)
            b_ed.action(ct.EDACTION_UPDATE)
            Profiler.stop('paint:bookmark')

            # Repaint the overview with the collected line states and gaps.
            # Set line counts first so the overview knows the total height.
            if overview is not None:
                Profiler.start('paint:overview')
                overview.set_line_counts(a_ed.get_line_count(), b_ed.get_line_count())
                overview.paint()
                Profiler.stop('paint:overview')

            Profiler.stop('refresh:total')
        finally:
            # Always print the profiling report — even if the compare
            # crashed with an exception. This ensures you can see WHERE
            # the time was spent (or where it crashed) even on big files.
            if _profiling_enabled_here:
                profiling_report()
                enable_profiling(False)

    def set_attr(self, e, x=0, y=0, nlen=0, bg=0, mptag=-1, map_only=0):
        """Add a colored attribute (background highlight) on editor e.

        Used for char-level text highlights (e.g., highlighting the
        changed characters within a modified line). The overview panel
        is handled separately by PaintboxOverview, not by this method.

        Args:
            e:        Editor instance.
            x:        Column (0-based). Default 0 (start of line).
            y:        Line number (0-based). Required.
            nlen:     Number of characters to highlight. Default 0.
            bg:       Background color (int, e.g. 0xFF0000 for red).
            mptag:    Micromap column tag. -1 = don't show on micromap
                      (default). 1..127 = show on micromap column with
                      that tag. Not used for the overview panel.
            map_only: 0 = text area only (default), 1 = micromap only,
                      2 = both text area and micromap.
        """
        e.attr(ct.MARKERS_ADD, DIFF_TAG,
               x,
               y,
               nlen,
               color_bg=bg,
               show_on_map=mptag,
               map_only=map_only
               )

    def set_gap(self, e, row, n=1):
        """Add a gap of n line-heights after 'row' on editor e. Used to
        compensate for inserted/deleted lines on the opposite side."""
        __, h = e.get_prop(ct.PROP_CELL_SIZE)
        h_size = h * n
        e.gap(ct.GAP_ADD, row-1, 0,
              tag=DIFF_TAG,
              size=h_size,
              color=self.cfg.get('color_gaps')
              )

    def _add_raw_gap(self, e, line_index, pixel_size, color):
        """Add a gap at the given line index with an explicit pixel size.
        `line_index` follows the e.gap() convention: the gap is inserted
        between `line_index` and `line_index+1` (i.e. after `line_index`).
        Use -1 for a gap before the first line. Compared to set_gap(), this
        takes an explicit pixel size instead of computing n*line_height,
        which is needed when wrap is on and the gap must match the actual
        number of wrapped visual rows on the opposite side."""
        e.gap(ct.GAP_ADD, line_index, 0,
              tag=DIFF_TAG,
              size=pixel_size,
              color=color
              )

    def _get_wrap_counts(self, ed):
        """Return a list where wrap_counts[i] = number of visual rows that
        line i occupies on screen. Uses Editor.get_wrapinfo() to query the
        editor's current word-wrap state. For a non-wrapped editor every
        line has 1 visual row. The returned list is indexed by logical line
        index (0-based) and has the same length as the editor's line count.
        Used to size inter-line gaps correctly when word-wrap is on: a gap
        that compensates for N missing logical lines must actually be
        (sum of those lines' visual rows) * line_height pixels tall,
        otherwise the two compare sides drift apart visually."""
        line_count = ed.get_line_count()
        if line_count <= 0:
            return []
        counts = [1] * line_count
        # Force CudaText to refresh its internal WrapInfo structure before
        # we query it. After text changes CudaText usually detects the
        # change automatically, but not always -- EDACTION_UPDATE with
        # param1="1" forces it. Wrapped in try/except in case the constant
        # or action is unavailable in an older CudaText build.
        try:
            ed.action(ct.EDACTION_UPDATE, "1")
        except Exception:
            pass
        try:
            info = ed.get_wrapinfo()
        except Exception:
            return counts
        if not info:
            return counts
        temp = [0] * line_count
        for item in info:
            if isinstance(item, dict):
                line = item.get('line', -1)
            else:
                continue
            if 0 <= line < line_count:
                temp[line] += 1
        for i in range(line_count):
            if temp[i] > 0:
                counts[i] = temp[i]
        return counts

    @staticmethod
    def _visual_rows(wrap_counts, line_index):
        """Visual row count for a single line, with bounds-safe fallback."""
        if wrap_counts is None:
            return 1
        if 0 <= line_index < len(wrap_counts):
            return wrap_counts[line_index]
        return 1

    @staticmethod
    def _sum_visual_rows(wrap_counts, line_start, line_end):
        """Total visual rows for lines [line_start, line_end). Used to size
        a gap that compensates for a whole block of missing lines."""
        if wrap_counts is None:
            return max(0, line_end - line_start)
        total = 0
        for i in range(line_start, line_end):
            if 0 <= i < len(wrap_counts):
                total += wrap_counts[i]
            else:
                total += 1
        return total

    def set_decor(self, e, row, text, color):
        """Set a line decorator (margin symbol) on editor e at row.
        Shows a colored DECOR_CHAR in the left margin to mark changed/added/
        deleted lines."""
        e.decor(ct.DECOR_SET, row, DIFF_TAG, text, color, bold=True)

    def clear(self, e):
        """Remove all diff markers, gaps, decorators, and bookmarks tagged
        with DIFF_TAG from editor e. Called before re-applying a fresh diff."""
        if e is None:
            return
        e.attr(ct.MARKERS_DELETE_BY_TAG, DIFF_TAG)
        e.gap(ct.GAP_DELETE_ALL, 0, 0)
        e.decor(ct.DECOR_DELETE_BY_TAG, tag=DIFF_TAG)
        e.bookmark(ct.BOOKMARK2_DELETE_BY_TAG, 0, tag=DIFF_TAG)

    def config(self):
        """Reload config from disk if the JSON file or theme has changed.
        Caches the result in self.cfg to avoid repeated disk reads."""
        opt_time = os.path.getmtime(JSONPATH) if os.path.exists(JSONPATH) else 0
        theme_name = ct.app_proc(ct.PROC_THEME_SYNTAX_GET, '')
        if self.cfg.get('opt_time') == opt_time and \
           self.cfg.get('theme_name') == theme_name:
            return
        self.cfg = self.get_config()

    def _setup_micromap(self, a_ed, b_ed):
        """Set up the micromap on both split editors when enable_micromap
        is on. Per editor:

        1. Delete default micromap columns:
           - 0 (line states) — not needed, Differ uses its own colors
           - 2 (selections) — disabled because we only want to see
             compare changes, not selection highlights
           Column 1 (bookmarks) is KEPT because it also shows the cursor
           position (like a usual scrollbar), which is useful for
           navigation. We paint diff-colored line highlights on column 1
           via attr(show_on_map=1).
        2. Enable PROP_MICROMAP so the micromap is visible.
        3. Set PROP_MICROMAP_AT_LEFT:
           - Left editor (a_ed): False (default) — micromap on the right
             side, facing the right editor.
           - Right editor (b_ed): True — micromap on the left side,
             facing the left editor.
           This way both micromaps are visible between the two editors,
           in the split gutter area.

        When enable_micromap is off, does nothing.
        """
        try:
            for e in (a_ed, b_ed):
                e.micromap(ct.MICROMAP_DELETE, 0)
                e.micromap(ct.MICROMAP_DELETE, 2)
                e.set_prop(ct.PROP_MICROMAP, True)
            # Place the micromap on the side that faces the other editor:
            # - a_ed (left): micromap on the right (default, PROP_MICROMAP_AT_LEFT=False)
            # - b_ed (right): micromap on the left (PROP_MICROMAP_AT_LEFT=True)
            a_ed.set_prop(ct.PROP_MICROMAP_AT_LEFT, False)
            b_ed.set_prop(ct.PROP_MICROMAP_AT_LEFT, True)
        except Exception as ex:
            msg('failed to set up micromap: {}'.format(ex), level=1)

    @staticmethod
    def get_config():
        """Read all differ.* options from JSON + current theme, and return
        a config dict. Also registers bookmark kinds (NKIND_*) with their
        colors so CudaText can render them."""

        def get_color(key, default_color):
            s = get_opt(key, '')
            if s:
                return ctx.html_color_to_int(s)
            else:
                return default_color

        def new_nkind(val, color):
            ct.ed.bookmark(ct.BOOKMARK_SETUP, 0,
                           nkind=val,
                           ncolor=color,
                           text=''
                           )

        def get_theme():
            data = ct.app_proc(ct.PROC_THEME_SYNTAX_DICT_GET, '')
            th = {}
            th['color_changed'] = data['LightBG2']['color_back']
            th['color_added'] = data['LightBG3']['color_back']
            th['color_deleted'] = data['LightBG1']['color_back']
            th['color_gaps'] = data['LightBG5']['color_back']
            return th

        t = get_theme()
        config = {
            'opt_time':
                os.path.getmtime(JSONPATH) if os.path.exists(JSONPATH) else 0,
            'theme_name':
                ct.app_proc(ct.PROC_THEME_SYNTAX_GET, ''),
            'color_changed':
                get_color('changed_color', t.get('color_changed')),
            'color_added':
                get_color('added_color', t.get('color_added')),
            'color_deleted':
                get_color('deleted_color', t.get('color_deleted')),
            'color_gaps':
                get_color('gap_color', t.get('color_gaps')),
            'sync_scroll':
                get_opt('sync_scroll', DEFAULT_SYNC_SCROLL == '1'),
            'compare_with_details':
                get_opt('compare_with_details', True),
            'ratio':
                get_opt('ratio_percents',  75)/100,
            'enable_sync_caret':
                get_opt('enable_sync_caret', False),
            'enable_auto_refresh':
                get_opt('enable_auto_refresh', False),
            'diff_context':
                get_opt('diff_context', 3),
            'diff_algorithm':
                get_opt('diff_algorithm', 'native_histogram'),
            'autojunk':
                get_opt('autojunk', True),
            'enable_profiling':
                get_opt('enable_profiling', False),
            'enable_micromap':
                get_opt('enable_micromap', True),
            'enable_overview':
                get_opt('enable_overview', False),
        }

        new_nkind(NKIND_DELETED, config.get('color_deleted'))
        new_nkind(NKIND_ADDED, config.get('color_added'))
        new_nkind(NKIND_CHANGED, config.get('color_changed'))

        return config

    @property
    def focused(self):
        """Return (focused_index, (a_ed, b_ed)) where focused_index is 0 if
        the primary (left) editor is focused, 1 if the secondary (right).
        """
        hndl_self = ct.ed.get_prop(ct.PROP_HANDLE_SELF)
        hndl_primary = ct.ed.get_prop(ct.PROP_HANDLE_PRIMARY)
        hndl_secondary = ct.ed.get_prop(ct.PROP_HANDLE_SECONDARY)
        eds = (ct.Editor(hndl_primary), ct.Editor(hndl_secondary))
        if hndl_self == hndl_primary:
            return 0, eds
        else:
            return 1, eds

    def jump(self, to_next=True):
        """Jump caret to the next (or previous) diff hunk in the focused editor.
        Wraps around at the end/start of the diffmap."""
        if not self.diff.diffmap:
            self.refresh()
        cnt = len(self.diff.diffmap)
        if cnt == 0:
            return ct.msg_status(_("No differences were found"))
        fc, eds = self.focused

        i = None
        if fc == 0:
            p = 0 if to_next else 1
        else:
            p = 2 if to_next else 3
        y = eds[fc].get_carets()[0][1]
        line_cnt = eds[fc].get_line_count()

        if to_next:
            for n, dif in enumerate(self.diff.diffmap):
                df_y = dif[p] if dif[p] <= line_cnt - 1 else line_cnt - 1
                if y < df_y:
                    i = n
                    break
        else: # to prev
            for n, dif in reversed(list(enumerate(self.diff.diffmap))):
                _y = y if dif[p] == dif[p-1] else y + 1 # adjust y for empty diff fragments
                if _y > dif[p]:
                    i = n
                    break

        if i is None:
            i = 0 if to_next else cnt - 1
        elif i >= cnt:
            i = 0
        elif i < 0:
            i = cnt - 1
        to = self.diff.diffmap[i]
        ct.msg_status(_("{} of {} difference").format(i+1, cnt))
        a_line_cnt = eds[0].get_line_count()
        b_line_cnt = eds[1].get_line_count()
        to0 = to[0] if to[0] <= a_line_cnt - 1 else a_line_cnt - 1
        to2 = to[2] if to[2] <= b_line_cnt - 1 else b_line_cnt - 1
        eds[0].set_caret(0, to0, id=ct.CARET_SET_ONE)
        eds[1].set_caret(0, to2, id=ct.CARET_SET_ONE)

    def jump_next(self):
        """Jump to the next diff hunk."""
        self.jump()

    def jump_prev(self):
        """Jump to the previous diff hunk."""
        self.jump(False)

    @property
    def get_current_change(self):
        """Return the diffmap entry [a0, a1, b0, b1] containing the caret
        in the focused editor, or None if the caret is not inside a diff hunk."""
        if not self.diff.diffmap:
            self.refresh()
        fc, eds = self.focused
        p = fc * 2
        y = eds[fc].get_carets()[0][1]
        for dif in self.diff.diffmap:
            if dif[p] <= y < dif[p+1]:
                return dif

    def select_current(self):
        """Select the lines of the current diff hunk in both editors."""
        cur_change = self.get_current_change
        if not cur_change:
            return
        esc = self.cfg.get('enable_sync_caret', False)
        fc, eds = self.focused
        self.cfg['enable_sync_caret'] = False
        eds[0].set_caret(0, cur_change[0], 0, cur_change[1])
        eds[1].set_caret(0, cur_change[2], 0, cur_change[3])
        self.cfg['enable_sync_caret'] = esc

    def copy(self, to_right=True):
        """Copy the current diff hunk's text from left to right (or right to
        left), replacing the opposite side's text. Then refresh diff markers."""
        fc, eds = self.focused
        current = self.get_current_change
        if not current:
            return
        else:
            a0, a1, b0, b1 = current
        if to_right:
            text = eds[0].get_text_substr(0, a0, 0, a1)
            eds[1].delete(0, b0, 0, b1)
            if text:
                eds[1].insert(0, b0, text)
        else:
            text = eds[1].get_text_substr(0, b0, 0, b1)
            eds[0].delete(0, a0, 0, a1)
            if text:
                eds[0].insert(0, a0, text)
        eds[0].set_caret(0, a0)
        eds[1].set_caret(0, b0)
        self.refresh()

    def copy_right(self):
        """Copy current hunk from left editor to right editor."""
        self.copy(True)

    def copy_left(self):
        """Copy current hunk from right editor to left editor."""
        self.copy(False)

    def copy_line(self, to_right=True):
        """Copy the caret's line(s) from left to right (or right to left),
        inserting at the current hunk's position. Unlike copy(), this works
        on the current caret line, not the whole hunk."""
        fc, eds = self.focused
        current = self.get_current_change

        def get_lines(ed: ct.Editor):
            carets = ed.get_carets()
            if len(carets) != 1:
                return []
            caret = carets[0]
            __, y1, __, y2 = caret
            if y2 == -1:
                return ed.get_text_line(y1) + '\n'
            else:
                return ''.join([ed.get_text_line(y)+'\n' for y in range(y1, y2)])

        if not current:
            return
        else:
            a0, a1, b0, b1 = current
        if to_right:
            if fc == 1:
                return
            text = get_lines(eds[0])
            if text:
                eds[1].insert(0, b0, text)
        else:
            if fc == 0:
                return
            text = get_lines(eds[1])
            if text:
                eds[0].insert(0, a0, text)
        self.refresh()

    def copy_line_right(self):
        """Copy caret line from left editor to right editor."""
        self.copy_line(True)

    def copy_line_left(self):
        """Copy caret line from right editor to left editor."""
        self.copy_line(False)

    @staticmethod
    def set_focus_to_opposite_panel():
        """Toggle focus between the two split editors."""
        ct.ed.cmd(ct_cmd.cmd_ToggleFocusSplitEditors)

    def sync_caret(self):
        """Mirror the caret position to the opposite editor. If the caret is
        inside a diff hunk, jump the opposite caret to the hunk's start.
        Otherwise, map the caret line through the diffmap to the corresponding
        line on the opposite side."""
        if not self.diff.diffmap:
            return
        fc, eds = self.focused
        op = 0 if fc else 1
        x, y = eds[fc].get_carets()[0][:2]

        esc = self.cfg.get('enable_sync_caret', False)
        p = fc * 2
        for dif in self.diff.diffmap:
            if dif[p] <= y < dif[p+1]:
                self.cfg['enable_sync_caret'] = False
                eds[op].set_caret(0, dif[op*2])
                self.cfg['enable_sync_caret'] = esc
                return
        for dif in self.diff.diffmap:
            if y < dif[p]:
                self.cfg['enable_sync_caret'] = False
                eds[op].set_caret(x, dif[op*2]-dif[p]+y)
                self.cfg['enable_sync_caret'] = esc
                return

    def get_name(self, e):
        """Return a display name for editor e: filename for real files,
        or 'untitled:TITLE [TAB_ID]' for untitled tabs."""
        fn = e.get_prop(ct.PROP_FN, '')
        if fn:
            return fn
        else:
            return self.format_untitled(e)

    def tabmenu_editor_ok(self, e, disabled_fn):
        """Check if editor 'e' is a valid candidate for the 'Compare with tab'
        list. Returns False if the tab should be excluded (not linked, not
        text, or is the disabled/current tab).

        For untitled tabs, compares by tab ID (extracted from disabled_fn)
        instead of full name string, so title changes don't cause the
        current tab to appear in its own compare list."""
        if not e.get_prop(ct.PROP_EDITORS_LINKED):
            return False
        if e.get_prop(ct.PROP_KIND) != 'text':
            return False
        if bool(disabled_fn):
            if disabled_fn.startswith(U_PREFIX):
                # Untitled tab: compare by tab ID, not full name string.
                i = disabled_fn.rfind('[')
                j = disabled_fn.rfind(']')
                if i > 0 and j > i:
                    id_str = disabled_fn[i+1:j]
                    if id_str.isdigit() and str(e.get_prop(ct.PROP_TAB_ID)) == id_str:
                        return False
            else:
                # Titled tab: compare by filename.
                fn = e.get_prop(ct.PROP_FN, '')
                if fn and fn == disabled_fn:
                    return False
        return True

    def tabmenu_init(self, cur_ed: ct.Editor):
        """Build the right-click tab context menu: 'Compare with...',
        'Compare with focused tab', 'Compare with tab' (submenu of all
        open tabs), and 'Refresh'. Only shown for valid compare candidates."""
        cur_fn = self.get_name(cur_ed)
        path_focused = self.get_name(ct.ed)

        if self.menuid_sep is None:
            self.menuid_sep = ct.menu_proc('tab', ct.MENU_ADD,
                caption='-'
                )
            self.compare_menu = ct.menu_proc('tab', ct.MENU_ADD,
                caption=PLG_NAME
                )

        ct.menu_proc(self.compare_menu, ct.MENU_CLEAR)
        self.menuid_withfile = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
            command='module=cuda_differ;cmd=tabmenu_chooser;',
            caption=_('Compare with...')
            )
        self.menuid_withfocused = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
            command='module=cuda_differ;cmd=tabmenu_files;info='+cur_fn+'::'+path_focused+';',
            caption=_('Compare with focused tab')
            )
        self.menuid_withtab = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
            caption=_('Compare with tab')
            )

        handles = ct.ed_handles()

        paths = []
        if len(handles) > 1:
            for h in handles:
                e = ct.Editor(h)
                if self.tabmenu_editor_ok(e, cur_fn):
                    path = self.get_name(e)
                    paths.append(path)

            if paths:
                ct.menu_proc(self.menuid_withtab, ct.MENU_CLEAR)

                # Add a "More tabs..." entry at the top that opens the
                # native CudaText dialog (which has a scrollbar) -- useful
                # when the popup menu is too long to fit on screen.
                ct.menu_proc(self.menuid_withtab, ct.MENU_ADD,
                    command='module=cuda_differ;cmd=tabmenu_chooser_tab;',
                    caption=_('More tabs...')
                    )

                for path in paths:
                    ct.menu_proc(self.menuid_withtab, ct.MENU_ADD,
                        command='module=cuda_differ;cmd=tabmenu_files;info='+cur_fn+'::'+path+';',
                        caption=collapse_filename(path)
                        )

        cur_ok = self.tabmenu_editor_ok(cur_ed, '')
        cur_is_focused = cur_ed.get_prop(ct.PROP_HANDLE_PRIMARY) == \
                         ct.ed.get_prop(ct.PROP_HANDLE_PRIMARY)

        ct.menu_proc(self.menuid_withtab, ct.MENU_SET_ENABLED, command=cur_ok and bool(paths))
        ct.menu_proc(self.menuid_withfile, ct.MENU_SET_ENABLED, command=cur_ok)
        # "Compare with focused tab" is disabled when:
        # - current tab is not a valid compare candidate (cur_ok)
        # - current tab IS the focused tab (nothing to compare with itself)
        # - focused tab is a Differ-managed compare tab (can't compare with a diff tab)
        focused_is_diff = self._is_compare_tab(ct.ed.get_prop(ct.PROP_TAB_ID))
        ct.menu_proc(self.menuid_withfocused, ct.MENU_SET_ENABLED,
            command=cur_ok and not cur_is_focused and not focused_is_diff)

        # Add a separator and "Refresh" entry at the end of the context menu.
        # Only enabled when the current tab is a compare tab managed by Differ.
        ct.menu_proc(self.compare_menu, ct.MENU_ADD, caption='-')
        self.menuid_refresh = ct.menu_proc(self.compare_menu, ct.MENU_ADD,
            command='module=cuda_differ;cmd=tabmenu_refresh;',
            caption=_('Refresh')
            )
        ct.menu_proc(self.menuid_refresh, ct.MENU_SET_ENABLED,
            command=self._is_compare_tab(cur_ed.get_prop(ct.PROP_TAB_ID)))

    def tabmenu_chooser(self):
        """Launch 'Compare with...' via a 100ms timer (needed because menu
        callbacks can't call dlg_file directly)."""
        callback = 'module=cuda_differ;cmd=tabmenu_chooser_timer;info=_;'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_chooser_timer(self, tag='', info=''):
        """Timer callback that actually opens the file dialog."""
        self.compare_with()

    def tabmenu_chooser_tab(self):
        """Opens the 'Compare current document with tab...' command, which
        shows the native CudaText tab-picker dialog (with scrollbar)."""
        callback = 'module=cuda_differ;cmd=tabmenu_chooser_tab_timer;info=_;'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_chooser_tab_timer(self, tag='', info=''):
        """Timer callback that actually opens the tab-picker dialog."""
        self.compare_with_tab()

    def tabmenu_refresh(self):
        """Refresh the compare tab -- re-applies diff markers."""
        callback = 'module=cuda_differ;cmd=tabmenu_refresh_timer;info=_;'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_refresh_timer(self, tag='', info=''):
        """Timer callback that actually runs the refresh."""
        self.refresh()

    def tabmenu_files(self, info):
        """Launch a compare between two files specified in 'info' (format:
        'fn0::fn1') via a 100ms timer."""
        callback = 'module=cuda_differ;cmd=tabmenu_files_timer;info='+info+';'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_files_timer(self, tag='', info=''):
        """Timer callback that actually calls set_files with the two filenames."""
        fn0, fn1 = info.split('::', maxsplit=1)
        self.set_files(fn0, fn1)

    def select_all_diff(self):
        """Select all diff hunks in the focused editor as multi-caret selections."""
        if not self.diff.diffmap:
            self.refresh()
        if len(self.diff.diffmap) == 0:
            return ct.msg_status(_("No differences were found"))
        fc, eds = self.focused
        y1,y2 = (0,1) if fc == 0 else (2,3)

        for n, dif in enumerate(self.diff.diffmap):
            id = ct.CARET_SET_ONE if n == 0 else ct.CARET_ADD
            eds[fc].set_caret(0, dif[y1], 0, dif[y2], id=id)

    def on_close(self, ed_self: ct.Editor):
        """Fires after the close is confirmed. For a compare tab: unregister
        it from the persisted state. If this was the last compare tab,
        disable autostart. No temp files to delete (split-tab approach).

        During app exit, the state entry and autostart are preserved so
        the compare tab can be restored after restart."""
        tab_id = ed_self.get_prop(ct.PROP_TAB_ID)
        entry = self._unregister_compare_tab(tab_id)
        if entry is None:
            return  # not a compare tab

        # Remove from in-memory scroll set.
        try:
            self.scroll.tab_id.discard(int(tab_id))
        except (ValueError, TypeError):
            pass

        # Destroy the paintbox overview for this tab.
        tab_id_str = str(tab_id)
        overview = self._overviews.pop(tab_id_str, None)
        if overview is not None:
            overview.destroy()

        # During app exit, keep the state entry and autostart subscription
        # so compare tabs persist restarts and the plugin auto-loads.
        # Re-register since we already unregistered above, preserving the
        # saved state so on_start2 can restore the correct title color.
        if getattr(self, '_app_exiting', False):
            self._register_compare_tab(
                tab_id,
                entry.get('primary_orig_tab_id'),
                entry.get('secondary_orig_tab_id'),
                entry.get('primary_orig_name', ''),
                entry.get('secondary_orig_name', ''),
                self._current_session_key,
                entry.get('saved', True)
            )
            return

        # If no more compare tabs are open in the current session,
        # disable autostart so the plugin does not load on next startup.
        state = self._load_state()
        if not state['sessions'].get(self._current_session_key, {}):
            self._disable_autostart()
