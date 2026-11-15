"""adb wrapper for one Android device (LDPlayer instance) + emulator management (launch, list, copy app)."""
import ctypes, os, re, shutil, subprocess, tempfile, time
import cv2, numpy as np
from .config import CFG, TOOLS, ROOT, serial_for

SER = CFG['serial']                      # current target; use(idx) switches it (GUI drives several emulators)
IDX = int(CFG.get('ld_index', 0))
ADB = TOOLS['adb'] or 'adb'
_NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

_LOGFILE = os.environ.get('SLOTREC_LOG')
STOP_ALL = os.path.join(ROOT, 'STOP')    # stops every job

# the job keeps listening to the stop flag of the emulator it STARTED on, even after switching to a new instance
JOB_IDX = int(os.environ['SLOTREC_STOP_IDX']) if os.environ.get('SLOTREC_STOP_IDX', '').isdigit() else IDX

def stop_file(idx=None):
    """per-emulator stop flag: <ROOT>/STOP_<index>"""
    return os.path.join(ROOT, f'STOP_{JOB_IDX if idx is None else int(idx)}')

class StopRequested(Exception):
    """raised inside long navigation loops when the user pressed STOP"""

def stop_requested():
    return os.path.exists(STOP_ALL) or os.path.exists(stop_file())

def check_stop():
    if stop_requested(): raise StopRequested()

def use(idx):
    """point all adb calls of this process at LDPlayer instance <idx>"""
    global SER, IDX
    IDX = int(idx); SER = serial_for(IDX, CFG)

def log(*a):
    line = time.strftime('%H:%M:%S') + ' ' + ' '.join(str(x) for x in a)
    try: print(line, flush=True)
    except (OSError, ValueError): pass          # GUI closed -> stdout pipe gone; keep working
    if _LOGFILE:
        try:
            with open(_LOGFILE, 'a', encoding='utf-8') as f: f.write(line + '\n')
        except OSError: pass

def adb(*a, out=False, timeout=20, serial=None):
    try:
        r = subprocess.run([ADB, '-s', serial or SER, *a], capture_output=True, timeout=timeout, creationflags=_NOWIN)
    except subprocess.TimeoutExpired:
        return b'' if out else None
    return r.stdout if out else None

def _devices():
    try: return subprocess.run([ADB, 'devices'], capture_output=True, text=True, creationflags=_NOWIN, timeout=20).stdout
    except subprocess.TimeoutExpired: return ''

def _listed(serial):
    return bool(re.search(rf'^{re.escape(serial)}\s+device\b', _devices(), re.M))

def connect():
    """make sure the device is visible to adb. LDPlayer normally shows up as emulator-<5554+2i>; on some PCs only as
    127.0.0.1:<5555+2i> after 'adb connect' -> then that TCP serial is used for everything (adb calls + scrcpy)."""
    global SER
    if _listed(SER): return True
    tcp = f'127.0.0.1:{5555 + 2 * IDX}'
    try: subprocess.run([ADB, 'connect', tcp], capture_output=True, creationflags=_NOWIN, timeout=20)
    except subprocess.TimeoutExpired: pass
    if _listed(SER): return True
    if SER != tcp and _listed(tcp):
        log(f'adb: {SER} not listed, using {tcp}'); SER = tcp; return True
    return False

def booted():
    return (adb('shell', 'getprop', 'sys.boot_completed', out=True) or b'').strip() == b'1'

# ---------------- LDPlayer instances ----------------
def ld(*a, timeout=120):
    if not TOOLS['ldconsole']: return ''
    try:
        r = subprocess.run([TOOLS['ldconsole'], *a], capture_output=True, timeout=timeout, creationflags=_NOWIN)
        return r.stdout.decode('mbcs', errors='ignore') if os.name == 'nt' else r.stdout.decode(errors='ignore')
    except subprocess.TimeoutExpired: return ''

def instances():
    """[(index, name, running)] from ldconsole list2 (empty if ldconsole is not found)"""
    out = []
    for line in ld('list2').splitlines():
        p = line.strip().split(',')
        if len(p) >= 5 and p[0].isdigit(): out.append((int(p[0]), p[1], p[4] == '1'))
    return out

def _ld_config(idx):
    import json
    if not TOOLS['ldconsole']: return None, None
    cfgp = os.path.join(os.path.dirname(TOOLS['ldconsole']), 'vms', 'config', f'leidian{int(idx)}.config')
    try:
        with open(cfgp, encoding='utf-8') as f: return cfgp, json.load(f)
    except (OSError, ValueError): return cfgp, None

def adb_enabled(idx):
    """True / False from the instance config, None if unknown"""
    _, c = _ld_config(idx)
    return None if c is None else c.get('basicSettings.adbDebug') == 1

def enable_adb(idx, say=log):
    """LDPlayer's 'ADB debugging -> local connection' is a per-instance setting (vms/config/leidian<idx>.config).
    Turn it on: quit the instance, set basicSettings.adbDebug=1 (backup kept as .bak), caller relaunches."""
    import json
    if not TOOLS['ldconsole']: say('找不到 ldconsole.exe（設定 → LDPlayer 資料夾）'); return False
    cfgp, c = _ld_config(idx)
    if c is None: say(f'找不到或讀不到 {cfgp}'); return False
    if c.get('basicSettings.adbDebug') == 1: return True
    ld('quit', '--index', str(idx)); time.sleep(6)
    shutil.copy2(cfgp, cfgp + '.bak')
    c['basicSettings.adbDebug'] = 1
    with open(cfgp, 'w', encoding='utf-8') as f: json.dump(c, f, indent=4, ensure_ascii=False)
    say(f'模擬器 {idx}：已開啟 ADB 本地連接'); return True

def diagnose(say=log):
    """why can't we reach the emulator? -> log everything useful in one go"""
    say('---- emulator diagnosis ----')
    say('adb:', ADB, '| ldconsole:', TOOLS['ldconsole'] or 'NOT FOUND')
    try: say('adb version:', subprocess.run([ADB, 'version'], capture_output=True, text=True, timeout=20, creationflags=_NOWIN).stdout.splitlines()[0])
    except Exception as e: say('adb version failed', repr(e))
    say('adb devices:', ' | '.join(l for l in _devices().splitlines()[1:] if l.strip()) or '(none)')
    say(f'target index {IDX} serial {SER} (tcp 127.0.0.1:{5555 + 2 * IDX})')
    say('ldconsole list2:', ' | '.join(ld('list2').split()) or '(no output)')
    cfgp, c = _ld_config(IDX)
    say('instance config:', cfgp, '| adbDebug =', None if c is None else c.get('basicSettings.adbDebug'),
        '| resolution =', None if c is None else c.get('advancedSettings.resolution'))
    say('ldconsole adb get-state:', ld('adb', '--index', str(IDX), '--command', 'get-state').strip() or '(no output)')
    say('----------------------------')

def ensure_running(timeout=240):
    """emulator off / not reachable -> make sure 'ADB local connection' is on, launch via ldconsole, wait for boot.
    returns True when adb sees a booted device; on failure logs a diagnosis."""
    if connect() and booted(): return True
    if not TOOLS['ldconsole']: log('emulator not connected and ldconsole.exe not found (設定 -> LDPlayer 資料夾)'); diagnose(); return False
    if adb_enabled(IDX) is False:                         # new PC / new instance: local ADB is off -> adb can never see it
        log(f'emulator {IDX}: ADB local connection is off -> turning it on'); enable_adb(IDX)
    log(f'launch emulator index {IDX}'); ld('launch', '--index', str(IDX))
    t0 = time.time(); relaunched = False
    while time.time() - t0 < timeout:
        check_stop()
        if connect():
            if booted(): time.sleep(15); log('emulator booted'); return True
        elif not relaunched and time.time() - t0 > 90 and adb_enabled(IDX) is False:
            log('still not reachable and ADB local connection is off -> enable + relaunch'); relaunched = True
            enable_adb(IDX); ld('launch', '--index', str(IDX)); t0 = time.time()
        time.sleep(5)
    log('emulator did not boot / not reachable within', timeout, 's'); diagnose(); return False

APK_CACHE = os.path.join(ROOT, 'apk_cache')
SR_PREFIX = 'SR_'                        # instances created by SlotRecorder; the ONLY ones it may delete

def cache_apk(pkg, src_serial, say=log):
    """keep a copy of the app's (split) APKs in apk_cache/<pkg>/ so a brand-new emulator can be set up without a source"""
    dst = os.path.join(APK_CACHE, pkg)
    if apk_cached(pkg): return True
    out = (adb('shell', 'pm', 'path', pkg, out=True, serial=src_serial) or b'').decode(errors='ignore')
    paths = [l.split(':', 1)[1].strip() for l in out.splitlines() if l.startswith('package:')]
    if not paths: say(f'cache_apk: {pkg} not installed on {src_serial}'); return False
    tmp = f'{dst}.tmp{os.getpid()}'; os.makedirs(tmp, exist_ok=True)      # pull aside, then move in one step:
    for i, p in enumerate(paths):                                        # other jobs never see a half-filled cache
        subprocess.run([ADB, '-s', src_serial, 'pull', p, os.path.join(tmp, f'{i}_{os.path.basename(p)}')],
                       capture_output=True, timeout=600, creationflags=_NOWIN)
    ok = all(os.path.exists(os.path.join(tmp, f'{i}_{os.path.basename(p)}')) for i, p in enumerate(paths))
    if ok:
        try: os.replace(tmp, dst)
        except OSError: ok = apk_cached(pkg)                         # another job finished first -> use theirs
    shutil.rmtree(tmp, ignore_errors=True)
    say(f'cache_apk {pkg}: {len(paths)} apk(s) ' + ('ok' if ok else 'FAILED')); return ok

def apk_cached(pkg):
    d_ = os.path.join(APK_CACHE, pkg)
    return os.path.isdir(d_) and any(f.endswith('.apk') for f in os.listdir(d_))

def install_cached(pkg, serial, say=log):
    d_ = os.path.join(APK_CACHE, pkg)
    apks = sorted(os.path.join(d_, f) for f in os.listdir(d_) if f.endswith('.apk')) if os.path.isdir(d_) else []
    if not apks: say(f'no cached apk for {pkg}'); return False
    r = subprocess.run([ADB, '-s', serial, 'install-multiple' if len(apks) > 1 else 'install', '-r', *apks],
                       capture_output=True, text=True, timeout=900, creationflags=_NOWIN)
    say(f'install {pkg} -> {serial}: {(r.stdout + r.stderr).strip()[-120:]}')
    return 'Success' in r.stdout

def new_instance(say=log):
    """create a fresh LDPlayer instance (= a device the games have never seen) set up like ours. -> index or None"""
    name = SR_PREFIX + time.strftime('%m%d_%H%M%S')
    ld('add', '--name', name); time.sleep(2)
    idx = next((i for i, n, _ in instances() if n == name), None)
    if idx is None: say('new_instance: ldconsole add failed'); return None
    ld('modify', '--index', str(idx), '--resolution', '1280,720,240', '--cpu', '4', '--memory', '4096')
    enable_adb(idx, say)
    say(f'new emulator {idx} {name} created'); return idx

def remove_instance(idx, say=log):
    """delete an emulator, but only one SlotRecorder created itself (name starts with SR_)"""
    name = next((n for i, n, _ in instances() if i == int(idx)), '')
    if not name.startswith(SR_PREFIX): return False
    ld('quit', '--index', str(idx)); time.sleep(5); ld('remove', '--index', str(idx))
    say(f'removed old emulator {idx} {name}'); return True

def copy_app(pkg, src_serial, dst_serial, say=log):
    """install <pkg> on dst by pulling the (split) APKs from src. returns True on success"""
    out = (adb('shell', 'pm', 'path', pkg, out=True, serial=src_serial) or b'').decode(errors='ignore')
    paths = [l.split(':', 1)[1].strip() for l in out.splitlines() if l.startswith('package:')]
    if not paths: say(f'來源模擬器沒有安裝 {pkg}'); return False
    tmp = tempfile.mkdtemp(prefix='slotrec_apk_')
    try:
        local = []
        for i, p in enumerate(paths):
            dst = os.path.join(tmp, f'{i}_{os.path.basename(p)}'); say(f'pull {os.path.basename(p)}')
            subprocess.run([ADB, '-s', src_serial, 'pull', p, dst], capture_output=True, timeout=600, creationflags=_NOWIN)
            if not os.path.exists(dst): say('pull failed'); return False
            local.append(dst)
        say(f'install {len(local)} apk(s) -> {dst_serial}')
        cmd = [ADB, '-s', dst_serial, 'install-multiple' if len(local) > 1 else 'install', '-r', *local]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, creationflags=_NOWIN)
        say((r.stdout + r.stderr).strip()[-300:])
        return 'Success' in r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ---------------- PC resources ----------------
def boot_time():
    """epoch seconds when Windows started (a process recorded before that cannot still be running)"""
    try:
        k = ctypes.windll.kernel32; k.GetTickCount64.restype = ctypes.c_ulonglong
        return time.time() - k.GetTickCount64() / 1000
    except Exception: return 0

def pid_alive(pid):
    """is process <pid> still running? (Windows: OpenProcess + GetExitCodeProcess == STILL_ACTIVE)"""
    try: pid = int(pid)
    except (TypeError, ValueError): return False
    if os.name != 'nt':
        try: os.kill(pid, 0); return True
        except OSError: return False
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)                                  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h: return False
    code = ctypes.c_ulong(); k.GetExitCodeProcess(h, ctypes.byref(code)); k.CloseHandle(h)
    return code.value == 259

def free_ram_gb():
    class MS(ctypes.Structure):
        _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong), ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong), ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong), ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
    try:
        m = MS(); m.dwLength = ctypes.sizeof(MS); ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / 2**30
    except Exception: return 99.0

def free_disk_gb(path):
    try: return shutil.disk_usage(path).free / 2**30
    except OSError: return 99.0

# ---------------- input / screen ----------------
def grab(retries=5):
    for _ in range(retries):
        b = adb('exec-out', 'screencap', '-p', out=True)
        if b:
            im = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
            if im is not None: return im
        time.sleep(2)
    raise RuntimeError('screencap failed')

def tap(x, y): adb('shell', 'input', 'tap', str(int(x)), str(int(y)))
def back(): adb('shell', 'input', 'keyevent', '4')
def swipe(x1, y1, x2, y2, ms=500): adb('shell', 'input', 'swipe', *map(str, (x1, y1, x2, y2, ms)))

def tap_by_id(id_suffix):
    """tap the Android view whose resource-id ends with id_suffix (uiautomator; works in any system language)"""
    adb('shell', 'rm', '-f', '/sdcard/slotrec_ui.xml')
    adb('shell', 'uiautomator', 'dump', '/sdcard/slotrec_ui.xml', timeout=30)
    x = (adb('exec-out', 'cat', '/sdcard/slotrec_ui.xml', out=True) or b'').decode('utf-8', 'ignore')
    for n in re.findall(r'<node [^>]*>', x):
        m = re.search(r'resource-id="[^"]*' + re.escape(id_suffix) + r'"', n)
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
        if m and b:
            x1, y1, x2, y2 = map(int, b.groups()); tap((x1 + x2) // 2, (y1 + y2) // 2); return True
    return False

def focus():
    out = (adb('shell', 'dumpsys', 'window', out=True) or b'').decode(errors='ignore')
    m = re.search(r'mCurrentFocus=Window\{\S+ \S+ ([^\s}]+)\}', out)
    return m.group(1) if m else ''

def screen_size():
    out = (adb('shell', 'wm', 'size', out=True) or b'').decode(errors='ignore')
    m = re.search(r'(\d+)x(\d+)', out)
    return (int(m.group(1)), int(m.group(2))) if m else None

def installed(pkg, serial=None):
    out = (adb('shell', 'pm', 'list', 'packages', pkg, out=True, serial=serial) or b'').decode(errors='ignore')
    return f'package:{pkg}' in out
