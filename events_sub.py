"""Runtime (dynamic) event subscriptions -- the ONE registry that manages
every CudaText event Differ 2 subscribes to at runtime.

install.inf declares exactly ONE static event: ``on_tab_menu`` (NON-lazy
on purpose: right-clicking a tab header must auto-load the plugin so the
"Compare with..." context menu is always available, plugin loaded or
not).  Every other event this plugin reacts to is subscribed HERE, on
demand, while it is actually needed -- and unsubscribed again when it is
not, so an idle Differ 2 (loaded but with no compare tab open) costs
CudaText nothing on the hot editor events (no per-keystroke on_change,
no per-click on_caret, no per-scroll on_scroll, no on_key).

THE EVENTS TABLE (also mirrored as a comment in install.inf) -- three
tiers:

* SURVIVAL set -- subscribed from the moment the plugin is loaded
  (Command.__init__) and never unsubscribed (the app dies anyway):

    on_state~      app-level state changes: CudaText session load
                   bracketing (BEGIN_PRE / LOAD / FAIL -- the re-attach
                   pass of _restore_session_tabs is the ONLY way an
                   idle plugin can notice a session switch just
                   restored compare tabs) and UI / syntax theme switches
                   (reload the compare colors, theme the toolbars).
                   Rare app-level event -- zero cost while idle.
    on_exit_pre~   app exit: cancel every in-flight background job
                   (engine compares, unified diffs, folder compares) so
                   nothing fires into a dying app. Fires once per exit.

* COMPARE set -- subscribed while at least one compare tab exists
  (Command._sessions is non-empty); dropped when the LAST compare tab
  closes:

    on_change~       per keystroke: dirty-half tracking, tab title
                     color, spurious-change suppression counter
    on_close_pre~    tab close: suppress the 'Save changes?' dialog
                     for clean compare tabs
    on_close~        tab close: tear the closing tab's session down
    on_save_pre~     Ctrl+S in a compare tab: sync the dirty halves
                     back to their original tabs
    on_state_ed~     word-wrap toggle on one half: propagate to the
                     other half and re-align

* OPTION-GATED set -- compare set PLUS the named setting on (a config()
  reload re-evaluates these, so the Options dialog takes effect at
  once):

    on_caret~        setting enable_sync_caret (default off):
                     mirror the caret between the two halves
    on_change_slow~  setting enable_auto_refresh (default off):
                     auto-refresh the diff markers after edit pauses
    on_key~          setting enable_keyboard_capture (default on):
                     the in-compare hotkeys (Alt+Arrows / F5) -- always
                     subscribed with the KEY-CODE filter derived from
                     the _HOTKEYS map, so the event fires only for
                     those keys, not every keystroke
    on_scroll~      settings sync_scroll (default on) OR enable_overview
                     (default on): scroll mirroring between the halves,
                     and overview slider tracking (the overview thumb
                     must follow scrolls it does not drive itself --
                     with sync_scroll off this now works too; the
                     mirror itself stays gated on sync_scroll inside
                     Command.on_scroll)

(All runtime subscriptions are lazy -- the bus appends the '~' suffix
itself; see _Ev.)

The subscription transitions happen ONLY through
Command._update_event_subscriptions() -- the single funnel -- which is
called at every state change: plugin load, compare tab created
(set_files, before the texts load), compare tab closed (on_close, after
the session is popped), sessions restored (on_start2 / session-switch
re-attach and the toolbar-restore timer that re-creates the overviews),
and every config reload (config()).

Design notes -- the pitfalls this deliberately avoids (lessons from the
older hand-rolled on_key / on_scroll subscriptions this file replaces,
and from the cuda_sync_editing approach):

* ONE table, ONE funnel. Every managed event, its condition and its
  filter live in the EVENTS block below; nothing else in the plugin may
  call PROC_EVENTS_SUB / PROC_EVENTS_UNSUB (grep for app_proc(
  PROC_EVENTS_ outside this file must stay empty).

* Tracked state, no blind re-subscription. update() remembers what is
  currently subscribed and issues only the missing SUBs and the extra
  UNSUBs -- repeated calls with an unchanged state make NO app_proc
  calls at all. (CudaText's EventSub unsubscribes the event first
  anyway, so even a re-subscribe could not stack duplicates -- but the
  diff keeps update() free to call from any hot-ish path without API
  churn, and the tracked dict can answer "what am I subscribed to
  right now?", e.g. for the console diagnostics below.)

* No parallel bookkeeping that can drift. The conditions read the
  Command's LIVE state (the _sessions registry, the cfg dict) at every
  update() -- there is no second set of "active handles" to keep in
  step by hand. A stuck subscription was exactly the class of bug the
  old ScrollSplittedTab.toggle() focused-tab condition had.

* Static events are untouchable. The bus only ever (un)subscribes
  names listed in its own table and never uses the '*' wildcard, so
  the install.inf on_tab_menu entry and the plugins.ini on_start2
  autostart entry (written by _enable_autostart) can never be killed
  by a dynamic unsubscribe. (CudaText's PROC_EVENTS_UNSUB removes the
  event from ALL of a module's entries -- including install.inf ones.)

* Every runtime subscription is LAZY (the '~' suffix): it can never
  auto-import the plugin module. A stale AppEventList entry after some
  exotic module reload is then a no-op (the event is skipped for an
  unloaded module), not a ghost re-load. on_tab_menu stays the only
  non-lazy subscription in the whole plugin.

* Nothing raises past the callers. The app_proc calls are guarded: an
  events API hiccup must never abort an on_close (its exit branch
  re-registers persisted sessions right after) or a Command.__init__.
"""

import cudatext as ct


class _Ev:
    """One managed runtime event subscription.

    Fields:
      name    CudaText event name, BARE (no '~' suffix -- the bus adds
              the lazy suffix itself when subscribing; the UNSUB call
              must use the bare name, CudaText matches it literally)
      needed  predicate over the update() state: True while this event
              must be subscribed (see the table below)
      keys    optional key-code filter (only on_key uses one) -- the
              subscription is issued as
              'module;event~;;keys' so the event fires only for those
              key codes
    """

    __slots__ = ('name', 'needed', 'keys')

    def __init__(self, name, needed, keys=''):
        self.name = name
        self.needed = needed
        self.keys = keys


def build_events(on_key_filter):
    """Build the EVENTS table for a plugin instance.

    Kept a function (not a module-level tuple) only because the on_key
    entry's key-code filter is derived from the _HOTKEYS map that lives
    in __init__.py (next to the on_key handler that consumes it) -- it
    is passed in here so the filter can never drift from the map.

    Returns a tuple of _Ev, in subscription-priority-agnostic order
    (CudaText keeps one AppEventList entry per module+filter group; the
    order here only matters for the console diagnostics listing.
    """

    return (
        # ----------------------------------------------------------
        # SURVIVAL SET -- while the plugin is loaded, compare tabs or
        # not; subscribed in Command.__init__, never unsubscribed.
        # ----------------------------------------------------------
        _Ev('on_state',
            # Session-switch re-attach (APPSTATE_SESSION_LOAD bracket)
            # works with zero compare tabs open -- the ONLY way an idle
            # plugin notices tabs a session load just restored; theme
            # switches reload the compare colors / re-theme toolbars.
            # Rare app-level event: ~zero cost while idle.
            lambda s: True),
        _Ev('on_exit_pre',
            # App exit: cancel in-flight engine/unidiff/dir-compare
            # jobs. Fires once per exit -- no meaningful overhead.
            lambda s: True),

        # ----------------------------------------------------------
        # COMPARE SET -- while at least one compare tab exists; dropped
        # when the LAST compare tab closes.
        # ----------------------------------------------------------
        _Ev('on_change',
            # Per keystroke: dirty-half tracking, title color, the
            # spurious-change suppression counter. Must be subscribed
            # BEFORE set_files loads the texts (their set_text_all
            # fires the two spurious on_change events the counter
            # swallows).
            lambda s: s.has_compares),
        _Ev('on_close_pre',
            # Tab close: suppress 'Save changes?' for clean compare
            # tabs (PROP_MODIFIED clearing).
            lambda s: s.has_compares),
        _Ev('on_close',
            # Tab close: full teardown of the closing tab's session --
            # this is also where the drop back to the survival set is
            # triggered (update() runs after the session is popped).
            lambda s: s.has_compares),
        _Ev('on_save_pre',
            # Ctrl+S in a compare tab: sync the dirty halves back to
            # their original tabs instead of a Save dialog.
            lambda s: s.has_compares),
        _Ev('on_state_ed',
            # EDSTATE_WRAP on one half: propagate the wrap mode to the
            # other half and re-align the compare.
            lambda s: s.has_compares),

        # ----------------------------------------------------------
        # OPTION-GATED SET -- compare set plus the named setting on.
        # config() reloads re-run update(), so toggling the setting in
        # the Options dialog (or hand-editing the JSON) takes effect
        # without a restart.
        # ----------------------------------------------------------
        _Ev('on_caret',
            # Caret mirroring between the halves.
            lambda s: s.has_compares
                      and s.cfg.get('enable_sync_caret', False)),
        _Ev('on_change_slow',
            # Auto-refresh of the diff markers after edit pauses.
            lambda s: s.has_compares
                      and s.cfg.get('enable_auto_refresh', False)),
        _Ev('on_key',
            # In-compare hotkeys (Alt+Arrows / F5) -- always subscribed
            # with the key-code filter, so only those key codes ever
            # deliver the event (the exact-modifier check stays in the
            # on_key handler).
            lambda s: s.has_compares
                      and s.cfg.get('enable_keyboard_capture', True),
            keys=on_key_filter),
        _Ev('on_scroll',
            # Scroll mirroring (sync_scroll) and/or overview slider
            # tracking (any compare tab has / gets an overview while
            # enable_overview is on). Subscribed whenever compare tabs
            # exist with either feature on; the MIRROR half is still
            # gated on sync_scroll inside Command.on_scroll, so an
            # overview-only subscription never scrolls the opposite
            # half on its own.
            lambda s: s.has_compares
                      and (s.cfg.get('sync_scroll', True)
                           or s.cfg.get('enable_overview', True))),
    )


class _BusState:
    """Snapshot of the plugin state the subscription conditions read.

    Deliberately tiny: has_compares (the Command._sessions registry is
    non-empty) and the config dict. No handles, no per-event
    bookkeeping -- the conditions re-derive everything from these two
    on every update(), so there is nothing that can drift.
    """

    __slots__ = ('has_compares', 'cfg')

    def __init__(self, has_compares, cfg):
        self.has_compares = has_compares
        self.cfg = cfg


class EventSubscriptions:
    """The runtime event subscription bus (one instance per Command).

    Usage: Command constructs it once with its module name and the
    EVENTS table, then calls update() at every state transition
    (through Command._update_event_subscriptions). Everything else --
    the tracking dict, the SUB/UNSUB diffing, the diagnostics -- lives
    here.
    """

    def __init__(self, module_name, events):
        self.module = module_name
        self.events = tuple(events)
        # name -> keys filter of the events CURRENTLY subscribed. The
        # single tracked truth of what update() has issued; consulted
        # by update() to make no-op calls free, and by describe() to
        # answer "what is subscribed right now?". CudaText offers no
        # way to QUERY AppEventList from Python, so this dict is the
        # plugin's own ledger -- sound because this bus is the only
        # place the plugin ever (un)subscribes runtime events.
        self._active = {}

    def update(self, has_compares, cfg):
        """Recompute every runtime subscription from the CURRENT state.

        Computes which table events the state needs, then issues only
        the subscriptions that appeared and the unsubscriptions that
        disappeared -- an unchanged state makes NO app_proc calls, so
        callers never need to think about whether an update() is
        redundant. Never raises: an app_proc failure is reported to the
        console and swallowed (see the module docstring for why a raise
        here could eat a persisted session at app exit).
        """
        state = _BusState(has_compares, cfg)

        wanted = {}
        for ev in self.events:
            try:
                if ev.needed(state):
                    wanted[ev.name] = ev.keys
            except Exception:
                # A broken condition must not kill the whole bus: treat
                # the event as not needed (conservative -- a missing
                # subscription degrades a feature, never corrupts
                # state).
                continue

        for name, keys in wanted.items():
            if self._active.get(name) == keys:
                continue  # already subscribed, exactly this filter
            if self._subscribe(name, keys):
                # Ledger written ONLY on success: a failed call left
                # CudaText without the subscription, so the next
                # update() retries it (EventSub's unsub-first semantics
                # make retries safe -- no duplicates ever).
                self._active[name] = keys
        for name in [n for n in self._active if n not in wanted]:
            if self._unsubscribe(name):
                # Kept in the ledger on failure so the next update()
                # retries the unsub -- a stuck-ON subscription is the
                # worse failure of the two.
                del self._active[name]

    def _subscribe(self, name, keys):
        """Issue the PROC_EVENTS_SUB call for one event.

        Format: 'module;event_list;lexer_list;key_filter' (empty lexer
        list: the event must fire for every lexer -- compare tabs are
        untitled; empty key filter for every event but on_key). The
        exact 4-field shape of the calls this file replaces, so the
        parser sees the same layout it always did. The '~' lazy suffix
        keeps the runtime subscription from ever auto-importing the
        module. CudaText's EventSub unsubscribes the event for the
        module FIRST, so even a re-subscribe after a lost ledger can
        never stack duplicates.
        """
        spec = '{};{}~;;{}'.format(self.module, name, keys)
        try:
            ct.app_proc(ct.PROC_EVENTS_SUB, spec)
            return True
        except Exception as e:
            self._report(name, 'subscribe', e)
            return False

    def _unsubscribe(self, name):
        """Issue the PROC_EVENTS_UNSUB call for one event.

        Format: 'module;event_list' -- and the event name must be BARE
        (no '~'): CudaText matches the unsub names literally, a
        suffixed name would not match anything and the subscription
        would stick around.
        """
        try:
            ct.app_proc(ct.PROC_EVENTS_UNSUB, '{};{}'.format(self.module, name))
            return True
        except Exception as e:
            self._report(name, 'unsubscribe', e)
            return False

    @staticmethod
    def _report(name, action, e):
        # Console only -- the plugin must keep working (minus one
        # event) even when the host's events API misbehaves.
        print('Differ 2 WARNING: cannot {} event {!r}: {}'.format(
            action, name, e))

    def describe(self):
        """Human-readable listing of the currently-subscribed runtime
        events (name + key filter), for console debugging -- e.g. print
        it from any event handler / command while investigating, or
        expose it behind a temporary command:

            print(self.evbus.describe())
        """
        if not self._active:
            return '{}: no runtime events subscribed'.format(self.module)
        items = sorted(
            name + (' [keys: {}]'.format(keys) if keys else '')
            for name, keys in self._active.items())
        return '{}: {}'.format(self.module, ', '.join(items))
