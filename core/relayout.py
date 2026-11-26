"""Move existing recordings from the old flat layout into the new per-game / per-bucket layout.

    old:  videos/<app>/<ts>_<name><suffix>                     (+ <ts>_<name>_snaps/ , _features/)
    new:  videos/<app>/<slot>/<bucket>/<ts>_<name><suffix>     bucket = raw / meta / report / frames

The game folder is the recorder's own slot id (from each recording's _stats.json); merged reports go to the
slot their _merged_summary.json names. A lock (.analyzing / .analyzing_run) is stale once recording stopped, so
it is dropped, not moved. videos/overview.json, 總覽.*, <app>/<app>_統計.xlsx and <app>/paytables/ stay put.

Idempotent: it only looks at files sitting DIRECTLY in an <app> folder (the un-migrated ones); anything already
inside a <slot>/<bucket>/ is left alone. Safe to re-run on either PC after `git pull`.

    python -m core.relayout            # dry-run: print the plan, move nothing
    python -m core.relayout --apply    # actually move
    python -m core.relayout --app 88_fortunes [--apply]   # one app only
"""
import argparse, csv, glob, json, os, re, shutil, sys
from .config import CFG, ROOT, PROD_GROUP, BUCKETS, _SUF_BY_LEN

def _videos_root():
    vd = CFG.get('video_dir') or 'videos'
    return vd if os.path.isabs(vd) else os.path.join(ROOT, vd)

# resolve the game key EXACTLY as overview.collect groups it (inlined so this stays numpy-free)
def _norm(s): return re.sub(r'[^0-9a-z]', '', str(s).lower())

def _slots(app_id):
    p = os.path.join(ROOT, 'apps', app_id, 'slots.csv')
    if not os.path.exists(p): return []
    with open(p, encoding='utf-8') as f: return list(csv.DictReader(f))

def _fname(base_name):
    return re.sub(r'^\d{8}_\d{6}_|(_PARTIAL|_e\d+)+$', '', base_name)

def _key(st, base_name, by_name):
    """the game folder = overview's group key: stats slot id, else slots.csv match on the name, else the bare name."""
    fn = _fname(base_name)
    return st.get('slot') or by_name.get(_norm(fn)) or fn

def _bucket_for(suffix):
    """destination bucket for a product suffix; '' = keep at game-root; None = drop (stale lock)."""
    if suffix in PROD_GROUP: return PROD_GROUP[suffix]
    if suffix.startswith('_seg') or suffix in ('_clean.mp4', '_clean.txt', '_concat.txt'): return 'raw'
    if suffix in ('.analyzing', '.analyzing_run'): return None
    return ''

def _safe(s):
    return re.sub(r'[^0-9A-Za-z_\-]+', '_', str(s)).strip('_') or 'game'

def _split(entry):
    """direct-child entry -> (base_name, suffix) if it is a recording product, else (None, None).
    Recognises every known product suffix, leftover _segN.mkv segments, and the .analyzing* locks."""
    for suf in _SUF_BY_LEN:                                       # known product suffixes, longest first (_clean.mp4 before .mp4)
        if entry.endswith(suf) and len(entry) > len(suf): return entry[:-len(suf)], suf
    m = re.match(r'(.+)(_seg\d+\.mkv)$', entry)                   # leftover recording segment
    if m: return m.group(1), m.group(2)
    for suf in ('.analyzing_run', '.analyzing'):
        if entry.endswith(suf) and len(entry) > len(suf): return entry[:-len(suf)], suf
    return None, None

def _plan_app(app_dir):
    """-> (moves, drops, warnings). moves: [(src, dst)]; drops: [src]; warnings: [str].
    Discovers bases from EVERY direct-child product (like overview.collect), not only _stats.json, so a
    recording missing its stats file is still migrated. Merged reports are placed with their game."""
    moves, drops, warns = [], [], []
    app_id = os.path.basename(app_dir)
    by_name = {_norm(r['name']): r['id'] for r in _slots(app_id)}
    try: entries = sorted(os.listdir(app_dir))
    except OSError: return moves, drops, warns
    items = []                                                   # (entry, base_name, suffix) for every un-migrated product
    for entry in entries:
        if entry in BUCKETS or entry == 'paytables': continue    # bucket dir / app-level assets
        bn, suf = _split(entry)
        if bn is None: continue                                  # xlsx / overview.json / 總覽.* / a <slot> dir
        items.append((entry, bn, suf))
    if not items: return moves, drops, warns
    # resolve each non-merged base to its game key (exactly as overview groups it); build a name->key bridge for merged
    bases, name2key = {}, {}
    def _st(bn):
        p = os.path.join(app_dir, bn + '_stats.json')
        try:
            with open(p, encoding='utf-8') as f: return json.load(f)
        except (OSError, ValueError): return {}
    for _e, bn, _s in items:
        if bn.endswith('_merged') or bn in bases: continue
        st = _st(bn); key = _key(st, bn, by_name); bases[bn] = key
        name2key[_norm(_fname(bn))] = key
        if st.get('name'): name2key[_norm(st['name'])] = key
    for _e, bn, _s in items:                                      # merged reports: map the game name back to its key
        if not bn.endswith('_merged') or bn in bases: continue
        nm = ''
        mj = os.path.join(app_dir, bn + '_summary.json')
        if os.path.exists(mj):
            try:
                with open(mj, encoding='utf-8') as f: nm = json.load(f).get('slot') or ''
            except (OSError, ValueError): nm = ''
        token = re.sub(r'_merged$', '', _fname(bn))
        bases[bn] = name2key.get(_norm(nm)) or name2key.get(_norm(token)) or _safe(nm or token)
    for entry, bn, suffix in items:
        full = os.path.join(app_dir, entry)
        bucket = _bucket_for(suffix)
        if bucket is None: drops.append(full); continue          # stale lock
        slot = bases[bn]
        dstdir = os.path.join(app_dir, slot, bucket) if bucket else os.path.join(app_dir, slot)
        dst = os.path.join(dstdir, entry)
        if os.path.abspath(full) == os.path.abspath(dst): continue
        if os.path.exists(dst): warns.append(f'目標已存在，跳過: {os.path.relpath(dst, _videos_root())}'); continue
        moves.append((full, dst))
    return moves, drops, warns

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--app', help='only this app id (default: every app under the videos dir)')
    ap.add_argument('--apply', action='store_true', help='actually move (default: dry-run)')
    a = ap.parse_args(argv)
    root = _videos_root()
    app_dirs = [os.path.join(root, a.app)] if a.app else [d for d in sorted(glob.glob(os.path.join(root, '*'))) if os.path.isdir(d)]
    tot_mv = tot_drop = 0
    for app_dir in app_dirs:
        if not os.path.isdir(app_dir): print(f'找不到: {app_dir}'); continue
        moves, drops, warns = _plan_app(app_dir)
        app = os.path.basename(app_dir)
        if not moves and not drops and not warns:
            print(f'[{app}] 已是新版面,無須搬移'); continue
        print(f'\n=== {app}  搬 {len(moves)}、丟棄鎖檔 {len(drops)}'
              + (f'、警告 {len(warns)}' if warns else '') + ' ===')
        by_slot = {}
        for src, dst in moves:
            by_slot.setdefault(os.path.relpath(os.path.dirname(os.path.dirname(dst)), root), []).append((src, dst))
        for slot, items in sorted(by_slot.items()):
            print(f'  {slot}/  ({len(items)})')
            if not a.apply:
                for src, dst in items[:4]:
                    print(f'     {os.path.basename(src):52} -> {os.path.relpath(dst, os.path.join(root, os.path.dirname(slot)))}')
                if len(items) > 4: print(f'     … 其餘 {len(items) - 4} 個')
        for w in warns: print(f'  ⚠ {w}')
        if a.apply:
            for src, dst in moves:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try: shutil.move(src, dst)
                except OSError as e: print(f'  ✗ 搬不動 {os.path.basename(src)}: {e!r}')
            for src in drops:
                try: os.remove(src)
                except OSError: pass
        tot_mv += len(moves); tot_drop += len(drops)
    print(f'\n{"已搬移" if a.apply else "試算(未搬)"}: {tot_mv} 個檔案/資料夾'
          + (f',丟棄 {tot_drop} 個過期鎖檔' if tot_drop else ''))
    if not a.apply and tot_mv:
        print('看起來沒問題就加 --apply 真正搬：  python -m core.relayout --apply')
    if a.apply:
        print('接著跑  python -m core.overview  刷新總覽(確認數字不變)。')

if __name__ == '__main__':
    main()
