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

def refresh_cache_before_pack(say=print):
    """before zipping, pull the NEWEST version of each already-cached app from any running emulator into apk_cache,
    so every package you build bundles the latest (a 'master' emulator with Play auto-update stays current here)."""
    try:
        from . import device as d
        if not os.path.isdir(APK_CACHE): return
        cached_pkgs = [p for p in os.listdir(APK_CACHE)
                       if os.path.isdir(os.path.join(APK_CACHE, p)) and any(f.endswith('.apk') for f in os.listdir(os.path.join(APK_CACHE, p)))]
        if not cached_pkgs: return
        running = [i for i, _, run in d.instances() if run]
        if not running:
            say('（沒有開機的模擬器可檢查更新，直接打包現有版本）'); return
        best = {}                                               # pkg -> (serial, versionCode) across running emulators
        for i in running:
            d.use(i)
            if not d.connect(): continue
            for pkg in cached_pkgs:
                if not d.installed(pkg): continue
                v = d.app_version(pkg) or 0
                if pkg not in best or v > best[pkg][1]: best[pkg] = (d.SER, v)
        for pkg in cached_pkgs:
            cached = d.cached_version(pkg) or 0
            if pkg in best and best[pkg][1] > cached:
                say(f'打包前更新 {pkg}：{cached} -> {best[pkg][1]}')
                d.cache_apk(pkg, best[pkg][0], say, force=True)
            else:
                say(f'{pkg}：快取已是最新（{cached}）')
    except Exception as e:
        say(f'（打包前更新檢查略過：{e!r}）')                     # never let the update check stop packing

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
    refresh_cache_before_pack()                                 # make sure apk_cache holds the latest before zipping
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
