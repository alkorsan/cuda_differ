import os
import json
import typing as tp
from pathlib import Path

import cudatext as ct
import cudatext_cmd as ct_cmd
import cudax_lib as ctx

from . import differ as df
from .scroll import ScrollSplittedTab
from .ui import DifferDialog, file_history

from cudax_lib import get_translation
_ = get_translation(__file__)  # I18N


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
#   },
#   "file_history": ["path1", "path2", ...]
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
    if (fn+'/').startswith(_homedir+'/'):
        fn = fn.replace(_homedir, '~', 1)
    return fn


def get_opt(key, def_val: tp.Any = ''):
    return ctx.get_opt('differ.' + key, def_val, user_json=JSONFILE)


def msg(s, level=0):
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
        self.diff = df.Differ()
        self.diff_dlg = DifferDialog()
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
            return {'sessions': {}, 'file_history': []}
        if not isinstance(data, dict):
            return {'sessions': {}, 'file_history': []}
        if not isinstance(data.get('sessions'), dict):
            data['sessions'] = {}
        if not isinstance(data.get('file_history'), list):
            data['file_history'] = []
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
        auto-loads on next CudaText startup. We use on_start2 (not on_start)
        because on_start fires before session restore completes, which would
        reset our green title color. on_start2 fires after configs and
        session restore, so our color override sticks."""
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

    def choose_files(self):
        files = self.diff_dlg.run()
        if files is None:
            return
        self.set_files(*files)

    def on_cli(self, fn1, fn2):
        self.set_files(fn1, fn2)

    def compare_with(self):
        fn0 = self.get_name(ct.ed)
        fn = ct.dlg_file(True, '!', '', '')
        if not fn:
            return
        self.set_files(fn0, fn)

    def compare_with_tab(self):
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
        fn0 = self.get_name(ct.ed)
        fn = ct.dlg_file(True, '!', '', '')
        if not fn:
            return

        enc = ct.ed.get_prop(ct.PROP_ENC)
        a = ct.ed.get_text_all()
        b = Path(fn).read_text(enc)
        self.create_diff(a, b, fn0, fn)

    def diff_with_tab(self):
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
        fn = e.get_filename()
        if fn:
            return fn==name
        return False

    def set_files(self, file0, file1):
        """Compare two files/tabs in a single split tab without temp files.

        Creates a new untitled tab, unlinks the split editors (so each half
        has independent text), splits vertically, then loads each original's
        content into the two halves. The original tabs stay open."""
        files = [file0, file1]
        lexers = [None, None]
        orig_tab_ids = [None, None]
        orig_texts = [None, None]
        orig_names = ['', '']

        # Find the two original tabs, grab their content, lexer, tab ID,
        # and a display name (file path for real files, title for untitled).
        for (index, name) in enumerate(files):
            for h in ct.ed_handles():
                e = ct.Editor(h)
                if self.is_match_name(e, name):
                    lexers[index] = e.get_prop(ct.PROP_LEXER_FILE)
                    orig_tab_ids[index] = e.get_prop(ct.PROP_TAB_ID)
                    orig_texts[index] = e.get_text_all()
                    fn = e.get_filename()
                    if fn:
                        orig_names[index] = fn
                    else:
                        orig_names[index] = e.get_prop(ct.PROP_TAB_TITLE) or ''
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
        ct.ed.set_prop(ct.PROP_TAB_TITLE, '{} | {}'.format(title0, title1))

        # Set lexers from the originals.
        if lexers[0] is not None:
            a_ed.set_prop(ct.PROP_LEXER_FILE, lexers[0])
        if lexers[1] is not None:
            b_ed.set_prop(ct.PROP_LEXER_FILE, lexers[1])

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
        # next startup and lazy events fire after restart.
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
        if state == ct.APPSTATE_THEME_SYNTAX:
            self.get_config()
            self._refresh_ex(ct.ed)  # automatic -- no dialog

    def on_scroll(self, ed_self):
        if self._is_compare_tab(ed_self.get_prop(ct.PROP_TAB_ID)):
            self.scroll.on_scroll(ed_self)

    def on_caret(self, ed_self):
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
                orig_fn = e.get_filename()
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
        self.tabmenu_init(ed_self)

    def refresh(self):
        """Manual refresh (from menu command or context menu). Shows the
        'identical' dialog if both sides are equal. Only applies to compare
        tabs managed by this plugin."""
        self._refresh_ex(ct.ed, show_dialog=True)

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

        a_ed = ct.Editor(ed.get_prop(ct.PROP_HANDLE_PRIMARY))
        b_ed = ct.Editor(ed.get_prop(ct.PROP_HANDLE_SECONDARY))

        a_text_all = a_ed.get_text_all()
        b_text_all = b_ed.get_text_all()

        if not a_text_all.endswith('\n'):
            a_text_all += '\n'
        if not b_text_all.endswith('\n'):
            b_text_all += '\n'

        if a_text_all == b_text_all:
            self.clear(a_ed)
            self.clear(b_ed)
            self.diff.diffmap = []
            if show_dialog:
                t = _('The two sides are identical.')
                ct.msg_box(t, ct.MB_OK)
            return

        a_ed.set_prop(ct.PROP_WRAP, ct.WRAP_OFF)
        b_ed.set_prop(ct.PROP_WRAP, ct.WRAP_OFF)

        self.clear(a_ed)
        self.clear(b_ed)
        self.config()

        self.diff.set_seqs(a_text_all.splitlines(True),
                           b_text_all.splitlines(True))

        self.scroll.tab_id.add(tab_id)
        self.scroll.toggle(self.cfg.get('sync_scroll'))

        self.diff.withdetail = self.cfg.get('compare_with_details')
        self.diff.ratio = self.cfg.get('ratio')

        for d in self.diff.compare():
            diff_id, y = d[0], d[1]
            if diff_id == df.A_LINE_DEL:
                self.set_bookmark2(a_ed, y, NKIND_DELETED)
                self.set_decor(a_ed, y, DECOR_CHAR, self.cfg.get('color_deleted'))
            elif diff_id == df.B_LINE_ADD:
                self.set_bookmark2(b_ed, y, NKIND_ADDED)
                self.set_decor(b_ed, y, DECOR_CHAR, self.cfg.get('color_added'))
            elif diff_id == df.A_LINE_CHANGE:
                self.set_bookmark2(a_ed, y, NKIND_CHANGED)
            elif diff_id == df.B_LINE_CHANGE:
                self.set_bookmark2(b_ed, y, NKIND_CHANGED)
            elif diff_id == df.A_GAP:
                self.set_gap(a_ed, y, d[2])
            elif diff_id == df.B_GAP:
                self.set_gap(b_ed, y, d[2])
            elif diff_id == df.A_SYMBOL_DEL:
                self.set_attr(a_ed, d[2], y, d[3], self.cfg.get('color_deleted'))
            elif diff_id == df.B_SYMBOL_ADD:
                self.set_attr(b_ed, d[2], y, d[3], self.cfg.get('color_added'))
            elif diff_id == df.A_DECOR_YELLOW:
                self.set_decor(a_ed, y, DECOR_CHAR, self.cfg.get('color_changed'))
            elif diff_id == df.B_DECOR_YELLOW:
                self.set_decor(b_ed, y, DECOR_CHAR, self.cfg.get('color_changed'))
            elif diff_id == df.A_DECOR_RED:
                self.set_decor(a_ed, y, DECOR_CHAR, self.cfg.get('color_deleted'))
            elif diff_id == df.B_DECOR_GREEN:
                self.set_decor(b_ed, y, DECOR_CHAR, self.cfg.get('color_added'))

    def set_attr(self, e, x, y, nlen, bg):
        e.attr(ct.MARKERS_ADD, DIFF_TAG,
               x,
               y,
               nlen,
               color_bg=bg,
               show_on_map=True
               )

    def set_gap(self, e, row, n=1):
        "set gap line after row line"
        __, h = e.get_prop(ct.PROP_CELL_SIZE)
        h_size = h * n
        e.gap(ct.GAP_ADD, row-1, 0,
              tag=DIFF_TAG,
              size=h_size,
              color=self.cfg.get('color_gaps')
              )

    def set_decor(self, e, row, text, color):
        e.decor(ct.DECOR_SET, row, DIFF_TAG, text, color, bold=True)

    def set_bookmark2(self, e, row, nk):
        e.bookmark(ct.BOOKMARK2_SET, row,
                   nkind=nk,
                   text="",
                   auto_del=True,
                   show=False,
                   tag=DIFF_TAG
                   )

    def clear(self, e):
        if e is None:
            return
        e.attr(ct.MARKERS_DELETE_BY_TAG, DIFF_TAG)
        e.gap(ct.GAP_DELETE_ALL, 0, 0)
        e.decor(ct.DECOR_DELETE_BY_TAG, tag=DIFF_TAG)
        e.bookmark(ct.BOOKMARK2_DELETE_BY_TAG, 0, tag=DIFF_TAG)

    def config(self):
        opt_time = os.path.getmtime(JSONPATH) if os.path.exists(JSONPATH) else 0
        theme_name = ct.app_proc(ct.PROC_THEME_SYNTAX_GET, '')
        if self.cfg.get('opt_time') == opt_time and \
           self.cfg.get('theme_name') == theme_name:
            return
        self.cfg = self.get_config()

    @staticmethod
    def get_config():

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
                get_color('gap_color', ct.COLOR_NONE),
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
        }

        new_nkind(NKIND_DELETED, config.get('color_deleted'))
        new_nkind(NKIND_ADDED, config.get('color_added'))
        new_nkind(NKIND_CHANGED, config.get('color_changed'))

        return config

    def clear_history(self):
        file_history.clear()
        file_history.save()

    @property
    def focused(self):
        hndl_self = ct.ed.get_prop(ct.PROP_HANDLE_SELF)
        hndl_primary = ct.ed.get_prop(ct.PROP_HANDLE_PRIMARY)
        hndl_secondary = ct.ed.get_prop(ct.PROP_HANDLE_SECONDARY)
        eds = (ct.Editor(hndl_primary), ct.Editor(hndl_secondary))
        if hndl_self == hndl_primary:
            return 0, eds
        else:
            return 1, eds

    def jump(self, to_next=True):
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
        self.jump()

    def jump_prev(self):
        self.jump(False)

    @property
    def get_current_change(self):
        if not self.diff.diffmap:
            self.refresh()
        fc, eds = self.focused
        p = fc * 2
        y = eds[fc].get_carets()[0][1]
        for dif in self.diff.diffmap:
            if dif[p] <= y < dif[p+1]:
                return dif

    def select_current(self):
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
        self.copy(True)

    def copy_left(self):
        self.copy(False)

    def copy_line(self, to_right=True):
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
        self.copy_line(True)

    def copy_line_left(self):
        self.copy_line(False)

    @staticmethod
    def set_focus_to_opposite_panel():
        ct.ed.cmd(ct_cmd.cmd_ToggleFocusSplitEditors)

    def sync_caret(self):
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
        fn = e.get_filename()
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
                fn = e.get_filename()
                if fn and fn == disabled_fn:
                    return False
        return True

    def tabmenu_init(self, cur_ed: ct.Editor):
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
        ct.menu_proc(self.menuid_withfocused, ct.MENU_SET_ENABLED, command=cur_ok and not cur_is_focused)

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
        callback = 'module=cuda_differ;cmd=tabmenu_chooser_timer;info=_;'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_chooser_timer(self, tag='', info=''):
        self.compare_with()

    def tabmenu_chooser_tab(self):
        """Opens the 'Compare current document with tab...' command, which
        shows the native CudaText tab-picker dialog (with scrollbar)."""
        callback = 'module=cuda_differ;cmd=tabmenu_chooser_tab_timer;info=_;'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_chooser_tab_timer(self, tag='', info=''):
        self.compare_with_tab()

    def tabmenu_refresh(self):
        """Refresh the compare tab -- re-applies diff markers."""
        callback = 'module=cuda_differ;cmd=tabmenu_refresh_timer;info=_;'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_refresh_timer(self, tag='', info=''):
        self.refresh()

    def tabmenu_files(self, info):
        callback = 'module=cuda_differ;cmd=tabmenu_files_timer;info='+info+';'
        ct.timer_proc(ct.TIMER_START_ONE, callback, 100)

    def tabmenu_files_timer(self, tag='', info=''):
        fn0, fn1 = info.split('::', maxsplit=1)
        self.set_files(fn0, fn1)

    def select_all_diff(self):
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
