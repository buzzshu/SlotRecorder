"""Zip the tool for another PC: everything except machine-local files (videos, logs, settings, stop flags, caches).

usage:  python -m core.pack   ->  ..\\SlotRecorder_<date>.zip (next to the SlotRecorder folder)
"""
import os, time, zipfile
from .config import ROOT

SKIP_DIRS = {'videos', 'logs', '__pycache__', '.git', 'apk_cache', 'python'}
SKIP_FILES = {'python_dir.txt', 'config.json', 'STOP'}

def pack(dest=None):
    from .version import VERSION
    dest = dest or os.path.join(os.path.dirname(ROOT), f'SlotRecorder_v{VERSION}_{time.strftime("%Y%m%d_%H%M")}.zip')
    top = os.path.basename(ROOT)
    n = 0
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as z:
        for dp, dns, fns in os.walk(ROOT):
            rel = os.path.relpath(dp, ROOT)
            dns[:] = [x for x in dns if not (rel == '.' and x in SKIP_DIRS) and x != '__pycache__']
            for f in fns:
                if rel == '.' and (f in SKIP_FILES or f.startswith('STOP_') or f.endswith('.zip')): continue
                if f.endswith(('.pyc', '.bak')): continue
                z.write(os.path.join(dp, f), os.path.join(top, rel, f) if rel != '.' else os.path.join(top, f)); n += 1
    print(f'{n} files -> {dest} ({os.path.getsize(dest) / 1e6:.1f} MB)')
    return dest

if __name__ == '__main__':
    pack()
