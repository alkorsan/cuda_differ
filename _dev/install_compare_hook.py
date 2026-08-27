"""
install_compare_hook.py - In-memory measurement hook for cuda_differ
====================================================================

WHAT THIS DOES:
  Monkey-patches the ALREADY-LOADED `cuda_differ.differ_native.Differ.compare`
  (and the pure-Python fallback `cuda_differ.differ_python.Differ.compare`
  if present) with a thin wrapper that prints Windows WorkingSet at each
  phase of the diff:
        - entry to compare()
        - after first opcode yielded  (PEAK MOMENT)
        - every 5000 opcodes          (progress + drift)
        - after last opcode           (generator exhausted)
        - after generator __del__     (cleanup done)

WHY THIS IS BETTER THAN AN EXTERNAL DIAGNOSTIC:
  - No need to enumerate editors or guess CudaText API names.
  - No file modification (the plugin source files stay untouched).
  - The wrapper sees EXACTLY what the real plugin's compare sees,
    including real text sizes and real diff state.
  - Easy to undo: just restart CudaText (the patch is in-memory only).

USAGE:
  1. Save this file to:
       F:\\_bin\\_binz\\_ide\\CudaText\\_last64\\py\\cuda_differ\\install_compare_hook.py
  2. Make sure cuda_differ is loaded:
       - Open a diff tab once (or just trigger any cuda_differ action)
  3. In CudaText Python console, run:
       exec(open(r'F:\\_bin\\_binz\\_ide\\CudaText\\_last64\\py\\cuda_differ\\install_compare_hook.py').read())
  4. You should see "MEASUREMENT HOOK INSTALLED" in the console.
  5. Trigger a REAL diff through the plugin's normal UI flow
     (open two files, use the cuda_differ menu / shortcut to start a diff).
     The wrapper will print WorkingSet measurements as the diff runs.
  6. Copy the console output and paste it back.

TO UNDO:
  Restart CudaText. The patch is in-memory only; no files are modified.
"""

import sys
import gc
import time
import ctypes
from ctypes import wintypes


# ============================================================================
# 1. WINDOWS WorkingSet API (via ctypes, no psutil needed)
# ============================================================================
print()
print("=" * 70)
print(" INSTALLING MEASUREMENT HOOK ON cuda_differ.differ_*.Differ.compare")
print("=" * 70)


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS from <psapi.h>"""
    _fields_ = [
        ("cb",                       wintypes.DWORD),
        ("PageFaultCount",           wintypes.DWORD),
        ("PeakWorkingSetSize",       ctypes.c_size_t),
        ("WorkingSetSize",           ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage",  ctypes.c_size_t),
        ("QuotaPagedPoolUsage",      ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage",   ctypes.c_size_t),
        ("PagefileUsage",            ctypes.c_size_t),
        ("PeakPagefileUsage",         ctypes.c_size_t),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_gpmi = None

# GetProcessMemoryInfo lives in psapi.dll on older Windows, kernel32 on modern.
for _dll_name in ("psapi", "psapi.dll"):
    try:
        _dll = ctypes.WinDLL(_dll_name, use_last_error=True)
        if hasattr(_dll, "GetProcessMemoryInfo"):
            _gpmi = _dll.GetProcessMemoryInfo
            print(f"  loaded GetProcessMemoryInfo from:  {_dll_name}")
            break
    except OSError:
        continue
if _gpmi is None:
    try:
        _gpmi = _kernel32.GetProcessMemoryInfo
        print("  loaded GetProcessMemoryInfo from:  kernel32 (fallback)")
    except AttributeError:
        print("  FATAL: GetProcessMemoryInfo not available")
        raise SystemExit(1)

_gpmi.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
    wintypes.DWORD,
]
_gpmi.restype = wintypes.BOOL


def get_ws():
    """Return (current_WorkingSet_bytes, peak_WorkingSet_bytes) or (None, None)."""
    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(pmc)
    h = _kernel32.GetCurrentProcess()
    if _gpmi(h, ctypes.byref(pmc), pmc.cb):
        return pmc.WorkingSetSize, pmc.PeakWorkingSetSize
    return None, None


def mb(x):
    if x is None:
        return "?"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x/1024/1024:.2f}MB"


# ============================================================================
# 2. THE MEASUREMENT WRAPPER
# ============================================================================

def make_measured_compare(original_compare, module_name):
    """Build a wrapper around an existing Differ.compare function."""

    def measured_compare(self, a_text, b_text):
        a_sz = len(a_text) if a_text else 0
        b_sz = len(b_text) if b_text else 0
        ws_entry, _ = get_ws()
        ts_entry = time.time()

        print()
        print("=" * 70)
        print(f"  [HOOK:{module_name}] Differ.compare() called")
        print("=" * 70)
        print(f"    a_text size:               {mb(a_sz)}")
        print(f"    b_text size:               {mb(b_sz)}")
        print(f"    input total (a+b):         {mb(a_sz + b_sz)}")
        print(f"    WorkingSet at entry:       {mb(ws_entry)}")

        # Call the original compare - returns a generator (no work yet)
        t0 = time.time()
        gen = original_compare(self, a_text, b_text)
        t1 = time.time()
        ws_after_gen, _ = get_ws()
        print(f"    after gen created ({t1-t0:.3f}s):     {mb(ws_after_gen)}  "
              f"(delta from entry: {mb(ws_after_gen - ws_entry)})")

        # Wrap the generator so we can measure on each next()
        class MeasuredGen:
            def __init__(self, g):
                self._g = g
                self._count = 0
                self._peak_ws = ws_entry
                self._ts_first = None
                self._ts_last = None

            def __iter__(self):
                return self

            def __next__(self):
                try:
                    val = next(self._g)
                    self._count += 1
                    if self._ts_first is None:
                        self._ts_first = time.time()
                    self._ts_last = time.time()
                    ws, _ = get_ws()
                    if ws is not None and (self._peak_ws is None or ws > self._peak_ws):
                        self._peak_ws = ws
                    if self._count == 1:
                        print()
                        print(f"  [HOOK:{module_name}] FIRST opcode yielded (PEAK)")
                        print(f"    time to first yield:        {self._ts_first - ts_entry:.3f}s")
                        print(f"    WorkingSet at first yield:  {mb(ws)}  "
                              f"(delta from entry: {mb(ws - ws_entry)})")
                        print(f"    expected peak delta ~3x (a+b) = {mb(3 * (a_sz + b_sz))}")
                        print(f"    (without internal dels, peak would be ~4x; new code peaks ~3x)")
                        print()
                        print(f"  diff in progress - will print every 5000 opcodes...")
                    elif self._count % 5000 == 0:
                        print(f"    after {self._count:>7} opcodes: WorkingSet={mb(ws)}, "
                              f"peak so far={mb(self._peak_ws)}")
                    return val
                except StopIteration:
                    ws_exhausted, _ = get_ws()
                    duration = (self._ts_last or time.time()) - (self._ts_first or ts_entry)
                    print()
                    print(f"  [HOOK:{module_name}] STOPITERATION (diff exhausted)")
                    print(f"    total opcodes yielded:      {self._count}")
                    print(f"    time spent in iteration:    {duration:.3f}s")
                    print(f"    WorkingSet at exhaustion:    {mb(ws_exhausted)}  "
                          f"(delta from entry: {mb(ws_exhausted - ws_entry)})")
                    print(f"    peak WorkingSet during diff: {mb(self._peak_ws)}  "
                          f"(delta from entry: {mb(self._peak_ws - ws_entry)})")
                    raise

            def __del__(self):
                # This fires when the plugin's loop variable goes out of scope
                # (immediate on CPython due to refcounting, unless there's a cycle)
                try:
                    gc.collect()
                    ws_after_gc, _ = get_ws()
                    print()
                    print(f"  [HOOK:{module_name}] MeasuredGen.__del__ (generator released)")
                    print(f"    WorkingSet after del+gc:    {mb(ws_after_gc)}  "
                          f"(delta from entry: {mb(ws_after_gc - ws_entry)})")
                    returned = ws_entry - (ws_after_gc or 0)
                    print(f"    total returned to OS:        {mb(returned)}  "
                          f"(vs input {mb(a_sz + b_sz)})")
                    print()
                    print("=" * 70)
                except Exception:
                    pass

        return MeasuredGen(gen)

    return measured_compare


# ============================================================================
# 3. INSTALL THE WRAPPER ON ALL LOADED Differ.compare FUNCTIONS
# ============================================================================

hooked = []

# Try the native (ctypes-accelerated) module first
for modname, attr in (
    ("cuda_differ.differ_native", "Differ"),
    ("cuda_differ.differ_python", "Differ"),
    ("differ.differ_native",      "Differ"),
    ("differ.differ_python",      "Differ"),
    ("differ_native",             "Differ"),
    ("differ_python",             "Differ"),
):
    if modname not in sys.modules:
        continue
    try:
        mod = __import__(modname, fromlist=[attr])
    except ImportError as e:
        print(f"  cannot import {modname}: {e}")
        continue
    cls = getattr(mod, attr, None)
    if cls is None:
        continue
    orig = getattr(cls, "compare", None)
    if orig is None:
        continue
    # Don't double-hook
    if getattr(orig, "_measured_hook", False):
        print(f"  {modname}.{attr}.compare already hooked - skipping")
        continue

    wrapper = make_measured_compare(orig, modname)
    wrapper._measured_hook = True
    wrapper._original = orig
    cls.compare = wrapper
    hooked.append((modname, attr, orig.__name__))
    print(f"  HOOKED: {modname}.{attr}.compare  (was {orig.__name__})")

if not hooked:
    print()
    print("  ERROR: no cuda_differ.differ_native or differ_python module found in sys.modules.")
    print("  Make sure cuda_differ is loaded: open a diff tab once, then re-run this script.")
    raise SystemExit(1)

print()
print("=" * 70)
print(f" MEASUREMENT HOOK INSTALLED on {len(hooked)} compare function(s).")
print("=" * 70)
print()
print(" Now trigger a REAL diff through the plugin's normal UI flow:")
print("   1. Make sure two files are open in CudaText")
print("   2. Use the cuda_differ menu / shortcut to start a diff")
print("   3. Watch the Python console - the hook prints at each phase")
print()
print(" After the diff finishes, also run this in the console to see final cleanup:")
print("   import gc; gc.collect()")
print()
print(" Restart CudaText to UNDO the hook (it's in-memory only).")
print()
