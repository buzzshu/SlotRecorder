"""Game catalog: walk an app's whole lobby and cut out every slot tile, so slots can be picked from a list
instead of being added by hand.

usage:  python -m core.catalog --app 88_fortunes [--emu 0]
output: apps/<id>/catalog/<nnn>.png  (one per unique tile)  +  catalog.json  (frame, rect, OCR'd title guess)
        The GUI / a person then names them; named tiles go to apps/<id>/slots/ + slots.csv (see apply_names).

Tile detection (profile "catalog"): tiles are framed rectangles, so their left/right borders are long straight
vertical edges -> pair edges whose distance matches a tile width class, require top/bottom borders too, then look for
same-size neighbours (tiles of one group are evenly spaced) and split cells holding two stacked small tiles.
The same tile seen in several frames is kept once (its title area is compared, allowing a few px of shift).
"""
import argparse, csv, json, os, re, shutil, time
import cv2, numpy as np
from . import device as d
from .device import log
from .vision import imwrite, imread, signature, changed

# ---------------- detection ----------------
def _longest_run(col):
    best = (0, 0); cur = 0; st = 0
    for i, v in enumerate(col):
        if v:
            if cur == 0: st = i
            cur += 1
            if cur > best[0]: best = (cur, st)
        else: cur = 0
    return best

def _vlines(gx, y0, y1, x0, x1, min_len):
    m = gx[y0:y1, x0:x1] > 60
    m = m | np.roll(m, 1, axis=1)
    out = []
    for x in range(m.shape[1]):
        L, s = _longest_run(m[:, x])
        if L >= min_len:
            l = (x + x0, s + y0, s + y0 + L)
            if out and l[0] - out[-1][0] <= 4:
                if l[2] - l[1] > out[-1][2] - out[-1][1]: out[-1] = l
            else: out.append(l)
    return out

def _extend_down(gy, x, w, bot, limit):
    """vertical border ended early -> the real bottom is the strongest full-width horizontal edge just below"""
    if w < 20: return bot
    rows = [(float((gy[r, x + 5:x + w - 5] > 50).mean()), r) for r in range(bot, min(bot + 160, limit))]
    good = [r for sc, r in rows if sc > 0.7]
    return max(good) if good else bot

def _hborders(gy, x, y, w, h):
    def best(yc):
        band = gy[max(0, yc - 6):yc + 7, x + 5:x + w - 5] > 50
        return band.mean(axis=1).max() if band.size else 0
    return best(y) > 0.6 and best(y + h) > 0.6

def _vstrength(gx, x, y, h):
    if x - 3 < 0 or x + 4 > gx.shape[1]: return 0
    return float((gx[y:y + h, x - 3:x + 4].max(axis=1) > 60).mean())

def _split_stacked(gy, x, y, w, h):
    prof = (gy[y:y + h, x + 6:x + w - 6] > 50).mean(axis=1)
    lo, hi = int(h * 0.35), int(h * 0.65)
    groups = []
    for r in (r for r in range(lo, hi) if prof[r] > 0.85):
        if groups and r - groups[-1][-1] <= 2: groups[-1].append(r)
        else: groups.append([r])
    for g1 in groups:
        for g2 in groups:
            gap = g2[0] - g1[-1]
            if 6 <= gap <= 40 and prof[g1[-1] + 2:g2[0] - 1].mean() < 0.35:
                a, b = g1[-1], g2[0]; return [(x, y, w, a), (x, y + b, w, h - b)]
    return [(x, y, w, h)]

def find_tiles(im, cfg):
    g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gx = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)); gy = np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
    x0, x1, y0, y1 = cfg['region']; classes = cfg['classes']; wmax = max(c['w'][1] for c in classes)
    ls = _vlines(gx, y0, y1, x0, x1, cfg.get('min_len', 90)); cand = []
    for i, a in enumerate(ls):
        for b in ls[i + 1:]:
            w = b[0] - a[0]
            if w > wmax: break
            top, bot = max(a[1], b[1]), min(a[2], b[2])
            bot = _extend_down(gy, a[0], w, bot, min(y1 + 20, gy.shape[0] - 7))   # border partly hidden (ribbon / badge)
            h = bot - top
            if any(c['w'][0] <= w <= c['w'][1] and c['h'][0] <= h <= c['h'][1] for c in classes) and _hborders(gy, a[0], top, w, h):
                cand.append((a[0], top, w, h))
    cand.sort(key=lambda t: -t[2] * t[3]); keep = []
    def overlap(t, k): return not (t[0] + t[2] <= k[0] + 8 or k[0] + k[2] <= t[0] + 8 or t[1] + t[3] <= k[1] + 8 or k[1] + k[3] <= t[1] + 8)
    for t in cand:
        if not any(overlap(t, k) for k in keep): keep.append(t)
    queue = list(keep)                                       # same-size neighbours of every tile found
    while queue:
        x, y, w, h = queue.pop()
        for dr in (-1, 1):
            best = None
            for gap in range(8, 46, 2):
                nx = x + dr * (w + gap)
                if nx < x0 or nx + w > x1: continue
                sc = min(_vstrength(gx, nx, y, h), _vstrength(gx, nx + w, y, h))
                if sc > 0.75 and (best is None or sc > best[0]): best = (sc, nx)
            if best:
                t = (best[1], y, w, h)
                if not any(overlap(t, k) for k in keep) and _hborders(gy, *t): keep.append(t); queue.append(t)
    return [r for t in keep for r in _split_stacked(gy, *t) if r[3] >= 80]

def _key(crop):
    return cv2.cvtColor(cv2.resize(crop, (120, 240))[140:236], cv2.COLOR_BGR2GRAY)

def _same(a, b):
    return float(cv2.matchTemplate(a, b[6:-6, 6:-6], cv2.TM_CCOEFF_NORMED).max()) > 0.55

def collect(frames, cfg):
    """frames: list of (name, image) -> list of dicts {crop, frame, rect, hits}"""
    uniq = []
    for name, im in frames:
        for x, y, w, h in find_tiles(im, cfg):
            l, t, r, b = cfg.get('inset', [0.08, 0.06, 0.08, 0.06])       # skip frame + live jackpot meters
            crop = im[y + int(h * t):y + h - int(h * b), x + int(w * l):x + w - int(w * r)]; k = _key(crop)
            m = next((u for u in uniq if _same(u['key'], k)), None)
            if m: m['hits'] += 1
            else: uniq.append({'key': k, 'crop': crop, 'frame': name, 'rect': [x, y, w, h], 'hits': 1})
    return uniq

def ocr_title(crop):
    """best guess of the game title: the largest text lines in the tile"""
    from . import ocr
    big = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    res = [(b, t, p) for b, t, p in ocr._read_safe(big) if p > 0.6 and len(re.sub(r'[^A-Za-z]', '', t)) >= 3]
    res.sort(key=lambda r: -(max(q[1] for q in r[0]) - min(q[1] for q in r[0])))      # tallest text first
    return ' '.join(t for _, t, _ in res[:3])

# ---------------- walking the lobby ----------------
def walk(app, max_steps=150, say=log):
    """from the far left to the end of the lobby, one screenshot per step"""
    sc = app.p['lobby_scan']; cfg = app.p['catalog']; x0, x1, y0, y1 = cfg['region']
    if not app.to_lobby(): raise RuntimeError('cannot reach the lobby')
    app.rewind(); time.sleep(1); frames = []; prev = None; same = 0
    for i in range(max_steps):
        d.check_stop()
        im = d.grab()
        if not app.is_lobby(im):
            act = app.dismiss_once(im, allow_back=False); say('popup', act); time.sleep(3); im = d.grab()
        frames.append((f'{i:03d}', im))
        yl = y0 + (y1 - y0) * 2 // 5                              # lower tile half: side ads / jackpot meters keep animating
        sig = signature(im[yl:y1, x0:x1])
        same = same + 1 if (app.is_lobby(im) and prev is not None and not changed(prev, sig, 1.5)) else 0
        prev = sig
        if same >= 3: say(f'end of lobby after {i} steps'); break
        d.swipe(*sc['step_swipe']); time.sleep(2.5)
    return frames

def scan(app, say=log):
    """walk + collect + OCR guesses -> apps/<id>/catalog/"""
    out = os.path.join(app.dir, 'catalog')
    if os.path.isdir(out): shutil.rmtree(out)
    os.makedirs(os.path.join(out, 'frames'))
    frames = walk(app, say=say)
    for n, im in frames: imwrite(os.path.join(out, 'frames', n + '.png'), im)
    tiles = collect(frames, app.p['catalog'])
    meta = []
    for i, t in enumerate(tiles):
        imwrite(os.path.join(out, f'{i:03d}.png'), t['crop'])
        meta.append({'i': i, 'frame': t['frame'], 'rect': t['rect'], 'hits': t['hits'], 'ocr': ocr_title(t['crop'])})
    with open(os.path.join(out, 'catalog.json'), 'w', encoding='utf-8') as f: json.dump(meta, f, indent=1, ensure_ascii=False)
    say(f'catalog: {len(tiles)} tiles from {len(frames)} lobby screens -> {out}')
    return meta

def ignore_tile(app, img, name):
    """remember a tile that is not wanted, so later scans do not add it again"""
    d_ = os.path.join(app.dir, 'ignore'); os.makedirs(d_, exist_ok=True)
    imwrite(os.path.join(d_, f'{slug(name) or "tile"}_{time.strftime("%m%d%H%M%S")}.png'), img)

def slug(s): return re.sub(r'[^0-9a-z]+', '_', s.lower()).strip('_')

def apply_names(app, names, minutes=60, unique=False):
    """names = {catalog index: 'Game Name'} -> slots/<slug>.png + slots.csv rows (unticked).
    unique=False: a name already listed is skipped (= duplicate tile). unique=True (automatic scan): clashes get _2, _3 ..."""
    out = os.path.join(app.dir, 'catalog'); rows = app.slots(); have = {r['id'] for r in rows}
    added = 0
    def taken(s): return s in have or os.path.exists(os.path.join(app.dir, 'tpl', s + '.png')) or app.t.path(s) is not None
    for i, name in sorted(names.items()):
        sid = slug(name)
        if not sid: continue
        if taken(sid):
            if not unique: continue
            k = 2
            while taken(f'{sid}_{k}'): k += 1
            sid = f'{sid}_{k}'; name = f'{name} {k}'
        im = imread(os.path.join(out, f'{int(i):03d}.png'))
        if im is None: continue
        imwrite(os.path.join(app.dir, 'slots', sid + '.png'), im)
        meta = json.load(open(os.path.join(out, 'catalog.json'), encoding='utf-8'))
        hint = next((m['frame'] for m in meta if m['i'] == int(i)), '')
        rows.append({'id': sid, 'name': re.sub(r'[^0-9A-Za-z_\-]+', '_', name).strip('_'), 'minutes': str(minutes), 'enabled': '0',
                     'hint': str(int(hint)) if str(hint).isdigit() else ''})
        have.add(sid); added += 1
    app.save_slots(rows); return added

def add_new(app, say=log):
    """after scan(): tiles that match none of the slots already listed -> added unticked, named from OCR (or New_Game_<n>)"""
    out = os.path.join(app.dir, 'catalog'); meta = json.load(open(os.path.join(out, 'catalog.json'), encoding='utf-8'))
    ids = [r['id'] for r in app.slots()]; names = {}; x0, x1 = app.p['catalog']['region'][:2]; stamp = time.strftime('%m%d')
    from .vision import Templates
    ign_dir = os.path.join(app.dir, 'ignore'); ign = Templates(ign_dir)     # tiles a person deleted / marked as not-a-game
    ign_names = [f[:-4] for f in os.listdir(ign_dir) if f.endswith('.png')] if os.path.isdir(ign_dir) else []
    for m in meta:
        fr = imread(os.path.join(out, 'frames', m['frame'] + '.png')); x, y, w, h = m['rect']
        if fr is None or x <= x0 + 10 or x + w >= x1 - 10: continue        # clipped by the scan area -> not a whole tile
        roi = fr[max(0, y - 30):y + h + 30, max(0, x - 30):x + w + 30]
        if max((app.t.find(roi, sid, 0.0)[0] for sid in ids), default=0) >= 0.5: continue   # already listed (crops differ a bit)
        if max((ign.find(roi, n, 0.0)[0] for n in ign_names), default=0) >= 0.5: continue    # deleted before -> stays out
        guess = re.sub(r'[^A-Za-z0-9 ]+', ' ', m.get('ocr', '')).strip().title()
        names[m['i']] = (guess[:40] if len(guess) >= 4 else '') or f'New Game {stamp} {m["i"]}'
    n = apply_names(app, names, unique=True); say(f'catalog: {n} new game(s) added to the list (unticked) - check their names in the GUI')
    refresh_hints(app, say)
    return n

def refresh_hints(app, say=log):
    """lobby order changes between app versions / days -> record for every listed slot the lobby step where it shows now"""
    out = os.path.join(app.dir, 'catalog', 'frames'); region = tuple(app.p['lobby_scan']['region'])
    frames = [(int(f[:-4]), imread(os.path.join(out, f))) for f in sorted(os.listdir(out)) if f.endswith('.png')]
    rows = app.slots(); found = 0
    for r in rows:
        hit = next((i for i, im in frames if app.t.find(im, r['id'], app.p['lobby_scan']['threshold'], region=region)[1]), None)
        r['hint'] = str(hit) if hit is not None else ''; found += hit is not None   # not seen -> no hint (= full search)
    app.save_slots(rows); say(f'catalog: lobby position updated for {found}/{len(rows)} slots (not seen this time: {len(rows) - found})')

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--app', required=True); ap.add_argument('--emu', type=int)
    ap.add_argument('--add-new', action='store_true', help='add tiles not in slots.csv yet')
    ap.add_argument('--hints', action='store_true', help='only re-record the lobby position of listed slots')
    a = ap.parse_args()
    if a.emu is not None: d.use(a.emu)
    from .app import App
    app = App(a.app)
    if not d.ensure_running(): raise SystemExit('emulator not available')
    if not d.focus().endswith(app.p['activity']): app.restart()
    scan(app)
    if a.add_new: add_new(app)
    elif a.hints: refresh_hints(app)
