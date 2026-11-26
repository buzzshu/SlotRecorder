"""Tool paths + user settings. Everything machine-specific lives in config.json; empty values are auto-detected."""
import glob, json, os, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, 'config.json')
DEFAULTS = {
    'ldplayer_dir': r'D:\LDPlayer\LDPlayer14',   # folder that contains ldconsole.exe
    'ld_index': 0,                               # LDPlayer instance index (multi-instance manager)
    'serial': 'emulator-5554',                   # adb serial of that instance
    'adb': '', 'scrcpy': '', 'ffmpeg': '', 'ffprobe': '',   # empty = auto-detect
    'video_dir': 'videos',
    'max_fps': 30, 'bitrate': '3M',             # 3M ~0.6 GB/hr、OCR 仍精準(6M ~1.2 GB/hr);老虎機實際有效碼率才 ~1.4 Mbps
    'spin_interval': 0.25,                        # 連續快速點擊(滾動中點 SPIN=快速停止);新環境打包預設也用 0.25,不靠本機 config.json
}

def load():
    cfg = dict(DEFAULTS)
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding='utf-8') as f: cfg.update(json.load(f))
    return cfg

def save(cfg):
    with open(CFG_PATH, 'w', encoding='utf-8') as f: json.dump(cfg, f, indent=2, ensure_ascii=False)

def _first(*cands):
    for c in cands:
        if c and os.path.isfile(c): return c
    return ''

def _winget(pattern):
    hits = sorted(glob.glob(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Packages', pattern), recursive=True))
    return hits[-1] if hits else ''

def find_tools(cfg=None):
    cfg = cfg or load()
    local = os.path.join(ROOT, 'tools')
    scrcpy = _first(cfg.get('scrcpy'), *glob.glob(os.path.join(local, '**', 'scrcpy.exe'), recursive=True),
                    shutil.which('scrcpy') or '', _winget(r'Genymobile.scrcpy*\**\scrcpy.exe'))
    # adb: prefer the one shipped next to scrcpy so scrcpy and our calls use the same adb server version
    adb = _first(cfg.get('adb'), os.path.join(os.path.dirname(scrcpy), 'adb.exe') if scrcpy else '',
                 shutil.which('adb') or '',
                 os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Android', 'Sdk', 'platform-tools', 'adb.exe'))
    ffmpeg = _first(cfg.get('ffmpeg'), *glob.glob(os.path.join(local, '**', 'ffmpeg.exe'), recursive=True),
                    shutil.which('ffmpeg') or '', _winget(r'Gyan.FFmpeg*\**\bin\ffmpeg.exe'))
    ffprobe = _first(cfg.get('ffprobe'), os.path.join(os.path.dirname(ffmpeg), 'ffprobe.exe') if ffmpeg else '',
                     shutil.which('ffprobe') or '')
    ldconsole = _first(os.path.join(cfg.get('ldplayer_dir', ''), 'ldconsole.exe'),
                       *[os.path.join(dr + ':' + os.sep, 'LDPlayer', v, 'ldconsole.exe') for dr in 'CDE' for v in ('LDPlayer14', 'LDPlayer9', 'LDPlayer4.0')])
    return {'adb': adb, 'scrcpy': scrcpy, 'ffmpeg': ffmpeg, 'ffprobe': ffprobe, 'ldconsole': ldconsole}

def serial_for(idx, cfg=None):
    """adb serial of LDPlayer instance <idx>: the configured one for the main instance, else emulator-(5554+2*idx)"""
    cfg = cfg or load()
    idx = int(idx)
    if idx == int(cfg.get('ld_index', 0)) and cfg.get('serial'): return cfg['serial']
    return f'emulator-{5554 + 2 * idx}'

CFG = load()
# one recording process drives one emulator: the GUI passes SLOTREC_LD_INDEX to each job it starts
if os.environ.get('SLOTREC_LD_INDEX', '').strip().isdigit():
    _i = int(os.environ['SLOTREC_LD_INDEX'])
    CFG['serial'] = serial_for(_i, CFG); CFG['ld_index'] = _i
TOOLS = find_tools(CFG)

def video_dir(app_id):
    d = CFG.get('video_dir') or 'videos'
    if not os.path.isabs(d): d = os.path.join(ROOT, d)
    d = os.path.join(d, app_id); os.makedirs(d, exist_ok=True); return d

# ---- product layout --------------------------------------------------------------------
# One recording = one base path  videos/<app>/<slot>/<ts>_<name>  (the game-root prefix).
# Its products live in bucket subfolders beside it; a lock / unknown suffix stays at game-root.
PROD_GROUP = {
    '.mp4': 'raw', '_clean.mp4': 'raw', '_clean.txt': 'raw', '_concat.txt': 'raw', '_scrcpy.log': 'raw',
    '_meter.json': 'meta', '_stats.json': 'meta', '_handoff.json': 'meta', '_timeline.csv': 'meta',
    '_summary.json': 'report', '_summary.html': 'report', '_summary.pdf': 'report', '_spins.tsv': 'report',
    '_snaps': 'frames', '_features': 'frames',
}
BUCKETS = ('raw', 'meta', 'report', 'frames')
_SUF_BY_LEN = sorted(PROD_GROUP, key=len, reverse=True)

def prod(base, suf, make=True):
    """Full path of product <suf> for recording <base>, routed into its bucket subfolder.
    Locks / unknown suffixes stay at the game-root next to base. Inverse: base_of()."""
    d, name = os.path.split(base)
    sub = PROD_GROUP.get(suf)
    if sub: d = os.path.join(d, sub)
    if make and d: os.makedirs(d, exist_ok=True)
    return os.path.join(d, name + suf)

def base_of(path):
    """Inverse of prod(): a product path (new bucketed OR old flat) -> its canonical base
    (.../<ts>_<name> at the game-root). Strips a trailing bucket dir, then the known suffix."""
    d, fn = os.path.split(path)
    if os.path.basename(d) in BUCKETS: d = os.path.dirname(d)
    for suf in _SUF_BY_LEN:
        if fn.endswith(suf) and len(fn) > len(suf): return os.path.join(d, fn[:-len(suf)])
    return os.path.join(d, os.path.splitext(fn)[0])

def find(base, suf):
    """Path of product <suf> for READING: the new bucketed location if it exists, else the legacy flat path
    if it exists, else the bucketed path (never creates a dir). Lets old flat recordings be read until migrated."""
    new = prod(base, suf, make=False)
    if os.path.exists(new): return new
    flat = base + suf
    return flat if os.path.exists(flat) else new

def outp(base, suf):
    """Write destination: overwrite the product where it already sits (flat or bucketed), else the new bucketed
    location; creates the containing dir. Avoids leaving a flat+bucketed duplicate when re-run before migration."""
    p = find(base, suf)
    d = os.path.dirname(p)
    if d: os.makedirs(d, exist_ok=True)
    return p
