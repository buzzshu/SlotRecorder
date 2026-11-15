"""Hand recordings to the existing recognition pipelines (SpookyLink, ...) in the format they read.

A target = which app + slots, the pipeline's input folder, the frame size it works in, and its "ready" marker.
SpookyLink (Lightning Link "Mo' Mummy"): work/canvas/<MMDDHHMMSS>.mp4 at 574x1022 @30 fps + <file>.ok
(its own transcode.sh crops OBS 1080p to that canvas; our recordings ARE the whole game screen, so only scaling).
Targets come from config.json "exports" (list) or the defaults below; a target whose folder does not exist on this
PC is skipped. Only the pipeline's input folder is written; nothing else in that project is touched.

usage:  python -m core.exports <video.mp4> [...] [--dest <folder>]   (--dest: override, e.g. for a test)
Runs automatically in the background after a matching recording.
"""
import argparse, json, os, re, subprocess, sys
from .config import CFG, ROOT, TOOLS

_NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
DEFAULT = [
    {'name': 'SpookyLink', 'app': 'lightning_link', 'slots': ['spooky_link_mo_mummy'],
     'dest': os.path.join('..', '..', 'SpookyLink', 'work', 'canvas'),       # relative to the SlotRecorder folder
     'size': [574, 1022], 'fps': 30, 'marker': '.ok'},
]

def targets(): return CFG.get('exports') or DEFAULT

def _dest(t):
    p = t['dest'] if os.path.isabs(t['dest']) else os.path.normpath(os.path.join(ROOT, t['dest']))
    return p

def wanted(app_id, slot_id):
    """recording of this slot goes to some pipeline that exists on this PC?"""
    return any(t['app'] == app_id and slot_id in t['slots'] and os.path.isdir(_dest(t)) for t in targets())

def _stats(mp4):
    p = mp4[:-4] + '_stats.json'
    if not os.path.exists(p): return {}
    with open(p, encoding='utf-8') as f: return json.load(f)

def export(mp4, dest_override=None, say=print):
    """-> list of written files"""
    mp4 = os.path.abspath(mp4); st = _stats(mp4); out = []
    app_id, slot = st.get('app'), st.get('slot')
    m = re.match(r'\d{4}(\d{4})_(\d{6})', os.path.basename(mp4))
    if not m: say(f'{os.path.basename(mp4)}: no date in the file name'); return out
    vid = m.group(1) + m.group(2) + (f"e{st['emulator']}" if st.get('group') else '')   # MMDDHHMMSS like SpookyLink's ids
    src = st.get('clean_file') if st.get('clean_file') and os.path.exists(st['clean_file']) else mp4   # stuck spans cut out
    for t in targets():
        if t['app'] != app_id or slot not in t['slots']: continue
        dest = dest_override or _dest(t)
        if not os.path.isdir(dest): say(f'{t["name"]}: folder not found {dest} -> skipped'); continue
        f = os.path.join(dest, vid + '.mp4'); marker = f + t.get('marker', '.ok')
        if os.path.exists(marker): say(f'{t["name"]}: {vid} already there'); out.append(f); continue
        w, h = t['size']
        say(f'{t["name"]}: {os.path.basename(src)} -> {f} ({w}x{h} @{t["fps"]})')
        r = subprocess.run([TOOLS['ffmpeg'] or 'ffmpeg', '-v', 'error', '-y', '-i', src, '-vf', f'scale={w}:{h}:flags=lanczos,fps={t["fps"]}',
                            '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p', '-an', f],
                           creationflags=_NOWIN, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(f) or os.path.getsize(f) == 0:
            say(f'{t["name"]}: transcode failed {r.stderr[-300:]}')
            try: os.remove(f)
            except OSError: pass
            continue
        open(marker, 'w').close(); out.append(f)                  # the pipeline only picks up files with the marker
        say(f'{t["name"]}: ready {f}')
    return out

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('videos', nargs='+'); ap.add_argument('--dest')
    a = ap.parse_args()
    for v in a.videos: export(v, a.dest)
