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

# Advanced knobs. NOT written into config.json by the GUI (so a later change of a default here still reaches every
# PC); add one to config.json by hand to override it. This table is the single place their defaults live — call
# sites read them with adv('<key>').  key: (default, what it does)
ADVANCED = {
    # --- recording ---
    'keyframe_interval':   (1,     'scrcpy 關鍵幀間隔（秒）；0 = 交給編碼器決定'),
    'feat_skip_s':         (6,     '遊戲內餘額凍結超過這麼多秒 → 視為特色／大獎動畫，開始掃點跳過'),
    'feat_tap_s':          (0.1,   '特色／pick bonus 期間每次點擊的間隔（秒）'),
    'stuck_bal_s':         (150,   '遊戲內畫面在動但餘額這麼多秒沒變 → 視為卡住（BACK，再不行重啟 app）'),
    'live_stats':          (True,  '錄製時即時讀餘額算 LIVE 統計'),
    'capture_paytable':    (True,  '每款遊戲第一次錄時截賠付表'),
    'turbo':               (True,  '進遊戲後把速度鍵切到最快（profile 有 turbo 設定才有作用）'),
    'new_instance_reset':  (True,  '沒錢且 app 無法重置帳號時，自動建一台全新模擬器'),
    'shots_keep_days':     (7,     'logs/shots 的除錯截圖保留天數'),
    'shots_keep_max':      (400,   'logs/shots 最多保留幾張（超過刪最舊的）'),
    # --- analysis ---
    'auto_analyze':        (True,  '錄完自動在背景分析'),
    'analyze_concurrency': (2,     '同時進行的分析數上限'),
    'feature_clips':       (True,  '把偵測到的特色剪成片段'),
    'auto_pdf':            (True,  '報告自動另存 PDF'),
    'gift_max_x':          (15,    '餘額跳升 ≥ 這麼多倍押注一律算贏分，不當成關彈窗的贈送'),
    'min_read_rate_overall': (0.5, '餘額讀取率門檻：RTP／中獎率／整體分布'),
    'min_read_rate':       (0.6,   '餘額讀取率門檻：特色次數／特色分布'),
    # --- targets / housekeeping ---
    'target_spins':        (1000,  '總覽裡每款遊戲的目標局數'),
    'precise_pp':          (10,    '「夠準就停」的 RTP 95% 範圍半寬（百分點）'),
    'precise_max_minutes': (240,   '「夠準就停」每款最多錄幾分鐘'),
    'precise_default':     (False, '預設啟用「夠準就停」'),
    'keep_video_days':     (7,     '清理舊影片：已分析且超過這麼多天的 mp4 可刪'),
}

def adv(key):
    """value of an advanced knob: config.json if set there (and not null), else the ADVANCED default"""
    v = CFG.get(key)
    return ADVANCED[key][0] if v is None else v

def adv_nz(key):
    """like adv(), but 0 / empty in config.json also falls back to the default (numbers that must be > 0)"""
    return CFG.get(key) or ADVANCED[key][0]

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
