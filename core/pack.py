"""Zip the tool for another PC: everything except machine-local files (videos, logs, settings, stop flags, caches).

usage:  python -m core.pack   ->  ..\\SlotRecorder_<date>.zip (next to the SlotRecorder folder)
"""
import os, time, zipfile
from .config import ROOT

# apk_cache IS bundled (so a fresh PC can install the apps with no source emulator) -- except Play-protected apps,
# which refuse to run when sideloaded, so bundling them is pointless and they must be installed from the Play Store.
SKIP_DIRS = {'videos', 'logs', '__pycache__', '.git', 'python'}
SKIP_FILES = {'python_dir.txt', 'config.json', 'STOP'}
APK_CACHE = os.path.join(ROOT, 'apk_cache')

def _play_protected(pkg_dir):
    """True if any apk in this cached package has the Play integrity / anti-sideload native lib (pairip)."""
    for f in os.listdir(pkg_dir):
        if not f.endswith('.apk'): continue
        try:
            with zipfile.ZipFile(os.path.join(pkg_dir, f)) as z:
                if any('libpairipcore.so' in n for n in z.namelist()): return True
        except (OSError, zipfile.BadZipFile): continue
    return False

def _bundle_apks():
    """-> {package: True/False protected} for packages whose cached apks will (not) be bundled; prints what it does."""
    out = {}
    if not os.path.isdir(APK_CACHE): return out
    for pkg in sorted(os.listdir(APK_CACHE)):
        d = os.path.join(APK_CACHE, pkg)
        if not os.path.isdir(d): continue
        out[pkg] = _play_protected(d)
        print(('skip (Play-protected, must install from Play Store): ' if out[pkg] else 'bundle apk_cache: ') + pkg)
    return out

def pack(dest=None):
    from .version import VERSION
    dest = dest or os.path.join(os.path.dirname(ROOT), f'SlotRecorder_v{VERSION}_{time.strftime("%Y%m%d_%H%M")}.zip')
    top = os.path.basename(ROOT)
    protected = _bundle_apks()
    n = 0
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as z:
        for dp, dns, fns in os.walk(ROOT):
            rel = os.path.relpath(dp, ROOT)
            dns[:] = [x for x in dns if not (rel == '.' and x in SKIP_DIRS) and x != '__pycache__' and x != 'catalog']   # catalog = lobby screenshots of the last scan
            if rel == 'apk_cache' or rel.startswith('apk_cache' + os.sep):   # only bundle sideloadable packages
                pkg = rel.split(os.sep)[1] if os.sep in rel else None
                if pkg and protected.get(pkg): continue
            for f in fns:
                if rel == '.' and (f in SKIP_FILES or f.startswith('STOP_') or f.endswith('.zip')): continue
                if f.endswith(('.pyc', '.bak')): continue
                z.write(os.path.join(dp, f), os.path.join(top, rel, f) if rel != '.' else os.path.join(top, f)); n += 1
    print(f'{n} files -> {dest} ({os.path.getsize(dest) / 1e6:.1f} MB)')
    return dest

if __name__ == '__main__':
    pack()
