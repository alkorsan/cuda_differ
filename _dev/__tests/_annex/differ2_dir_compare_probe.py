# -*- coding: utf-8 -*-
# differ2_probe.py -- Differ 2 slow-listing probe (round 5)
#
# WHERE TO RUN (both, in this order):
#   1) plain terminal:      python differ2_probe.py
#   2) inside CudaText:     paste this WHOLE file into the Python Console
#      (or: exec(open(r"<path>\differ2_probe.py").read()) )
#      The UI freezes up to ~1-2 min if the slow state reproduces -- expected.
#
# WHAT IT MEASURES (same folder, same calls, different threads):
#   1. MAIN battery     : the thread that is always fast in your reports
#   2. WORKER plain      : fresh Python thread, like the scan worker/pool
#   3. WORKER + ticker   : worker while another thread grabs the GIL every
#                          ~200 ms (mimics the plugin's 200 ms UI timer)
#   4. MAIN + churn      : main thread while a background thread does the
#                          listing call + math -- tells us whether the
#                          planned main-thread listing pump would be fast
#   5. WORKER #2         : a second fresh thread (thread-to-thread variance)
#   6-8. COLD dirs       : first-touch cost, on F: and on the TEMP drive,
#                          MAIN-first vs WORKER-first
#   cmd dir              : out-of-process enumeration (the WinMerge control)
#
# Every battery ends with WALL / KERNEL / USER (GetThreadTimes):
#   wall >> kernel+user  -> the thread was WAITING (scheduler / GIL)
#   kernel ~ wall        -> the call blocked INSIDE the kernel (filter / disk)

import os, sys, time, threading, ctypes
from ctypes import wintypes

TARGET = r"F:\_bin\_src\cudatext\cuda_differ2\___diff_proc\4\3cuda_differ2_folder_compare\cuda_differ2"
REPEAT = 3
STAT_SAMPLE = 5   # entries to os.stat() in the "stat xN fresh" row

class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

class FIND_DATAW(ctypes.Structure):
    _fields_ = [("attr", wintypes.DWORD),
                ("ct", FILETIME), ("at", FILETIME), ("wt", FILETIME),
                ("szHi", wintypes.DWORD), ("szLo", wintypes.DWORD),
                ("r0", wintypes.DWORD), ("r1", wintypes.DWORD),
                ("name", ctypes.c_wchar * 260),
                ("alt", ctypes.c_wchar * 14)]

k32 = ctypes.windll.kernel32
k32.FindFirstFileW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(FIND_DATAW)]
k32.FindFirstFileW.restype = ctypes.c_ssize_t      # -1 == INVALID_HANDLE_VALUE
k32.FindClose.argtypes = [wintypes.HANDLE]
k32.Sleep.argtypes = [wintypes.DWORD]
k32.GetThreadTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
k32.GetThreadPriority.argtypes = [wintypes.HANDLE]
k32.GetThreadPriority.restype = ctypes.c_int

CUR_THREAD = wintypes.HANDLE(-2)   # pseudo-handle: the calling thread
CUR_PROC = wintypes.HANDLE(-1)     # pseudo-handle: this process

def _ft_s(ft):
    return ((ft.hi << 32) | ft.lo) / 1e7

def thread_times():
    c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not k32.GetThreadTimes(CUR_THREAD, ctypes.byref(c), ctypes.byref(e),
                              ctypes.byref(k), ctypes.byref(u)):
        return (-1.0, -1.0)
    return (_ft_s(k), _ft_s(u))

def t_listdir(p):
    t0 = time.perf_counter(); n = len(os.listdir(p))
    return (time.perf_counter() - t0) * 1000, n

def t_scandir(p):
    t0 = time.perf_counter(); n = 0
    with os.scandir(p) as it:
        for _e in it:
            n += 1
    return (time.perf_counter() - t0) * 1000, n

def t_stat_some(p, k):
    t0 = time.perf_counter(); n = 0
    for nm in os.listdir(p)[:k]:
        os.stat(os.path.join(p, nm)); n += 1
    return (time.perf_counter() - t0) * 1000, n

def t_findfirst(p):
    fd = FIND_DATAW(); t0 = time.perf_counter()
    h = k32.FindFirstFileW(os.path.join(p, "*"), ctypes.byref(fd))
    dt = (time.perf_counter() - t0) * 1000
    if h != -1:
        k32.FindClose(h)
    return dt

def t_ostat(p):
    t0 = time.perf_counter(); os.stat(p)
    return (time.perf_counter() - t0) * 1000

def t_sleep50():
    t0 = time.perf_counter(); k32.Sleep(50)
    return (time.perf_counter() - t0) * 1000

def t_busy():
    t0 = time.perf_counter(); s = 0
    for i in range(2000000):
        s += i
    return (time.perf_counter() - t0) * 1000

def battery(label, target):
    rows = ["", "===== %s =====" % label,
            "  folder : %s" % target,
            "  tid=%d  priority=%d (0=normal)" % (threading.get_ident(),
                                                  k32.GetThreadPriority(CUR_THREAD))]
    k0, u0 = thread_times(); w0 = time.perf_counter()
    for name, fn in (("os.listdir", lambda: t_listdir(target)),
                     ("os.scandir", lambda: t_scandir(target)),
                     ("stat x%d fresh" % STAT_SAMPLE,
                      lambda: t_stat_some(target, STAT_SAMPLE))):
        ms = []; n = -1
        for _ in range(REPEAT):
            v, n = fn(); ms.append(v)
        rows.append("  %-16s: %s ms   (n=%d)"
                    % (name, " / ".join("%.1f" % x for x in ms), n))
    rows.append("  %-16s: %s ms" % ("FindFirstFileW",
        " / ".join("%.1f" % t_findfirst(target) for _ in range(REPEAT))))
    rows.append("  %-16s: %s ms" % ("os.stat(root)",
        " / ".join("%.1f" % t_ostat(target) for _ in range(REPEAT))))
    rows.append("  %-16s: %s ms   (~50 each healthy; >>50 = GIL re-acquire stall)"
        % ("Sleep(50) x5", " / ".join("%.0f" % t_sleep50() for _ in range(5))))
    rows.append("  %-16s: %.0f ms   (pure CPU; compare across threads)"
        % ("busy loop", t_busy()))
    k1, u1 = thread_times(); w1 = time.perf_counter()
    rows.append("  WALL %.0f ms | KERNEL %.0f ms | USER %.0f ms"
                % ((w1 - w0) * 1000, (k1 - k0) * 1000, (u1 - u0) * 1000))
    rows.append("    -> wall >> kernel+user = WAITING (GIL/scheduler);"
                " kernel ~ wall = blocked in kernel (filter/disk)")
    return "\n".join(rows)

def make_cold(base):
    d = os.path.join(base, "__d2probe_cold_%d" % (int(time.time() * 1000) % 1000000))
    os.makedirs(d)
    for i in range(30):
        with open(os.path.join(d, "f%02d.dat" % i), "wb") as f:
            f.write(b"x" * 64)
    return d

def t_cmd_dir(p):
    import subprocess
    t0 = time.perf_counter()
    subprocess.run(["cmd", "/c", "dir", "/b", p],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=0x08000000)   # CREATE_NO_WINDOW
    return (time.perf_counter() - t0) * 1000

def main():
    out = ["", "Differ 2 listing probe  pid=%d  py=%s"
           % (os.getpid(), sys.version.split()[0]),
           "cpus=%s  gil switch interval=%.1f ms"
           % (os.cpu_count(), sys.getswitchinterval() * 1000)]
    am = ctypes.c_size_t(); pm = ctypes.c_size_t()
    if k32.GetProcessAffinityMask(CUR_PROC, ctypes.byref(am), ctypes.byref(pm)):
        out.append("process affinity mask: 0x%X of 0x%X" % (am.value, pm.value))
    if not os.path.isdir(TARGET):
        out.append("TARGET not found -- edit TARGET at the top: " + TARGET)
        print("\n".join(out)); return
    tmpbase = os.environ.get("TEMP") or os.environ.get("TMP") or "."
    tdrive = os.path.splitdrive(tmpbase)[0] or "(cwd drive)"
    out.append("TEMP base: %s (%s) | TARGET drive: %s"
               % (tmpbase, tdrive, os.path.splitdrive(TARGET)[0]))
    out.append("cmd dir /b (out-of-process, incl. ~50 ms spawn): %.0f / %.0f ms"
               % (t_cmd_dir(TARGET), t_cmd_dir(TARGET)))

    # 1) MAIN
    out.append(battery("1. MAIN thread (always fast in your reports)", TARGET))

    # 2) WORKER plain
    box = {}
    def w1():
        box["a"] = battery("2. WORKER thread, plain (fresh thread)", TARGET)
    th = threading.Thread(target=w1); th.start(); th.join()
    out.append(box["a"])

    # 3) WORKER + 200 ms GIL ticker (mimics the plugin's UI timer)
    stop = threading.Event()
    def ticker():
        while not stop.is_set():
            time.sleep(0.19)
            _s = sum(range(200000))       # ~1-2 ms GIL hold, like a UI tick
    tk = threading.Thread(target=ticker); tk.start()
    def w2():
        box["b"] = battery("3. WORKER thread + 200 ms GIL ticker", TARGET)
    th = threading.Thread(target=w2); th.start(); th.join()
    stop.set(); tk.join()
    out.append(box["b"])

    # 4) MAIN while a background thread churns (listing call + math)
    stop2 = threading.Event()
    def churn():
        while not stop2.is_set():
            t_findfirst(TARGET)          # ctypes call: GIL released during it
            _s = 0
            for i in range(100000):
                _s += i
    tc = threading.Thread(target=churn); tc.start()
    time.sleep(0.05)
    out.append(battery("4. MAIN thread + background churn (is the pump viable?)",
                       TARGET))
    stop2.set(); tc.join()

    # 5) second fresh WORKER (thread-to-thread variance)
    def w3():
        box["c"] = battery("5. WORKER thread #2, plain (fresh again)", TARGET)
    th = threading.Thread(target=w3); th.start(); th.join()
    out.append(box["c"])

    # 6-8) cold dirs
    try:
        fbase = os.path.dirname(TARGET)
        d1 = make_cold(fbase)
        out.append(battery("6. COLD dir on F:, MAIN first touch", d1))
        d2 = make_cold(fbase)
        def w4():
            box["d"] = battery("7. COLD dir on F:, WORKER first touch", d2)
        th = threading.Thread(target=w4); th.start(); th.join()
        out.append(box["d"])
        d3 = make_cold(tmpbase)
        def w5():
            box["e"] = battery("8. COLD dir on %s, WORKER first touch" % tdrive, d3)
        th = threading.Thread(target=w5); th.start(); th.join()
        out.append(box["e"])
        import shutil
        for d in (d1, d2, d3):
            shutil.rmtree(d, ignore_errors=True)
    except OSError as ex:
        out.append("cold-dir tests skipped: %r" % ex)

    out.append("")
    out.append("Done. Copy EVERYTHING from 'Differ 2 listing probe' down and paste it back.")
    print("\n".join(out))

main()
