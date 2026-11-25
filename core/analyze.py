"""Statistics from a recorded slot video, read off the BALANCE meter (approach adapted from SpookyLink's meter reading):
  balance drops by exactly one bet  -> one spin
  balance rises                     -> win of the current spin (free games / features add up on their trigger spin)
  drop of n bets + a rise in one step (next spin started while the win was still counting up) -> split accordingly
Coins that arrive while a popup was being dismissed (daily bonus, level-up gifts...) are reported separately, not as wins.

usage:  python -m core.analyze <video.mp4> [more.mp4 ...] [--app lightning_link] [--fps 4] [--merge]
        python -m core.analyze --group <id> --app <app>     merge the videos of one multi-emulator run, per slot
output: <video>_spins.tsv   one row per spin (time in video, bet, win, multiple, balance after)
        <video>_summary.json / _summary.html   RTP (+95% range), hit rate, win-size distribution, coins per hour, top wins
        --merge / --group: <..>_merged_summary.html / .json / _spins.tsv = all spins of the same slot from several videos
"""
import argparse, html, json, math, os, re, subprocess, sys, time
import cv2
import numpy as np
from .config import CFG, TOOLS, ROOT, video_dir
from .ocr import read_number
from .version import VERSION

_NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
W, H = 720, 1280

def hms(s): s = int(s); return f'{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}'

# ---------------- statistics helpers (same formulas as SpookyLink summarize.py) ----------------
def wilson(k, n, z=1.96):
    if n == 0: return (0.0, 0.0)
    p = k / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))

def boot_rtp(mult, n=2000, seed=1, wins=None, bets=None):
    """95% range of RTP by resampling spins (win/bet multiples). Mixed bets (merged videos): resample the ratio
    total win / total bet instead, which is how that RTP is defined."""
    if len(mult) < 30: return (None, None)
    rng = np.random.default_rng(seed); m = np.asarray(mult, float)
    if bets is not None:
        w = np.asarray(wins, float); b = np.asarray(bets, float); means = []
        for _ in range(n):
            k = rng.integers(0, len(m), len(m)); means.append(w[k].sum() / b[k].sum())
        means = np.array(means)
    else: means = np.array([m[rng.integers(0, len(m), len(m))].mean() for _ in range(n)])
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))

# win-size buckets, lut-pipeline convention: '= 0x' on its own, the rest half-open (lo, hi] named by the upper bound.
# fine (1x-wide) up to 10x, then coarser so high multiples don't trail a row of near-empty bins.
BUCKETS = [(0, 0, '= 0 倍（沒中）'),
           (0, 1, '(0, 1] 倍'), (1, 2, '(1, 2] 倍'), (2, 3, '(2, 3] 倍'), (3, 4, '(3, 4] 倍'), (4, 5, '(4, 5] 倍'),
           (5, 6, '(5, 6] 倍'), (6, 7, '(6, 7] 倍'), (7, 8, '(7, 8] 倍'), (8, 9, '(8, 9] 倍'), (9, 10, '(9, 10] 倍'),
           (10, 15, '(10, 15] 倍'), (15, 20, '(15, 20] 倍'), (20, 30, '(20, 30] 倍'), (30, 50, '(30, 50] 倍'),
           (50, 100, '(50, 100] 倍'), (100, 1e18, '> 100 倍')]

def win_dist(mult, wins, tot_bet, denom):
    """win-size distribution over BUCKETS (lut half-open (lo, hi], upper inclusive). denom = spins the 'every_n'
    frequency divides by (pass the total paid spins so base and feature tables share one play-rate basis)."""
    mult = np.asarray(mult, float); wins = np.asarray(wins, float); out = []
    for lo_, hi_, label in BUCKETS:
        sel = (mult == 0) if hi_ == 0 else ((mult > lo_) & (mult <= hi_))
        k = int(sel.sum())
        out.append({'label': label, 'count': k, 'every_n': round(denom / k, 1) if k else None,
                    'rtp_share': float(wins[sel].sum() / tot_bet) if tot_bet else 0.0})
    return out

def spin_stats(wins, bets):
    """RTP / hit rate / win-size distribution of a list of spins (one video, or several videos merged)"""
    wins = np.asarray(wins, float); bets = np.asarray(bets, float); n = len(wins)
    mult = wins / bets; tot_bet = float(bets.sum()); tot_win = float(wins.sum())
    lo, hi = boot_rtp(mult, wins=wins, bets=bets) if len(np.unique(bets)) > 1 else boot_rtp(mult)
    hits = int((wins > 0).sum()); hlo, hhi = wilson(hits, n)
    return {'spins': n, 'total_bet': tot_bet, 'total_win': tot_win, 'rtp': tot_win / tot_bet, 'rtp_95': [lo, hi],
            'hit_rate': hits / n, 'hit_95': [hlo, hhi], 'hit_every_n': round(n / hits, 2) if hits else None,
            'distribution': win_dist(mult, wins, tot_bet, n)}, mult

# ---------------- reading the video ----------------
def video_size(path):
    try:
        r = subprocess.run([TOOLS['ffprobe'] or 'ffprobe', '-v', 'error', '-select_streams', 'v', '-show_entries', 'stream=width,height',
                            '-of', 'csv=p=0', path], capture_output=True, text=True, creationflags=_NOWIN)
        w, h = map(int, r.stdout.strip().split(',')[:2]); return w, h
    except (ValueError, OSError):
        cap = cv2.VideoCapture(path); ok, fr = cap.read(); cap.release()      # ffprobe missing -> read one frame
        if ok: return fr.shape[1], fr.shape[0]
        raise SystemExit(f'cannot read video size: {path}')

def frames(path, fps, size=None):
    w_, h_ = size or (W, H)
    p = subprocess.Popen([TOOLS['ffmpeg'] or 'ffmpeg', '-v', 'error', '-i', path, '-vf', f'fps={fps},scale={w_}:{h_}',
                          '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'], stdout=subprocess.PIPE, creationflags=_NOWIN, bufsize=W * H * 3 * 2)
    i, n = 0, w_ * h_ * 3
    try:
        while True:
            b = p.stdout.read(n)
            if len(b) < n: break
            yield i / fps, np.frombuffer(b, np.uint8).reshape(h_, w_, 3); i += 1
    finally:
        p.stdout.close(); p.kill()

def read_series(path, boxes, fps=4, progress=None, size=None):
    """-> list of (t, balance|None), list of (t, bet) ; OCR only when the meter pixels changed"""
    bx, tx = boxes['balance'], boxes.get('bet')
    bal, bets = [], []; last_crop = last_val = None; last_bet_t = -99
    for t, im in frames(path, fps, size):
        c = im[bx[1]:bx[3], bx[0]:bx[2]]
        if last_crop is not None and np.abs(c.astype(np.int16) - last_crop).mean() < 1.0: v = last_val
        else: v = read_number(c, 0.75); last_crop = c.astype(np.int16); last_val = v
        bal.append((t, v))
        if tx and t - last_bet_t >= 10:
            b = read_number(im[tx[1]:tx[3], tx[0]:tx[2]], 0.75)
            if b: bets.append((t, b)); last_bet_t = t
        if progress and int(t) % 300 == 0 and abs(t - int(t)) < 1e-6: progress(t)
    return bal, bets

def stable_values(series, need=2):
    """values read the same in >= need consecutive samples -> [(t_first, value, n_samples, i_first, i_last)]
    (i = index into series, so the readings BETWEEN two stable values - the roll-up - stay available)"""
    out = []; run_v = None; run_t = 0; run_n = 0; run_i = 0
    for i, (t, v) in enumerate(series):
        if v is not None and v == run_v: run_n += 1
        else:
            if run_v is not None and run_n >= need: out.append((run_t, run_v, run_n, run_i, i - 1))
            run_v, run_t, run_n, run_i = v, t, 1, i
    if run_v is not None and run_n >= need: out.append((run_t, run_v, run_n, run_i, len(series) - 1))
    return out

def clean_jumps(vals, bet, fps):
    """drop OCR misreads: a value that is off by a factor (lost/extra digit) and does not last; long-lasting
    implausible values are kept as a 'jump' (account reset, big gift) and re-anchor the series."""
    out = []
    for r in vals:
        if out:
            pv, v, n = out[-1][1], r[1], r[2]
            weird = v < pv * 0.5 - 10 * bet or v > pv * 3 + 5000 * bet
            if weird and n < 10 * fps: continue            # < 10 s -> misread
        out.append(r)
    return out

def _books_closed(summary):
    """analyze's balance bookkeeping closed: |balance_check| <= one bet (or either is unknown)."""
    bchk = summary.get('balance_check'); bet = summary.get('bet')
    return bchk is None or bet is None or abs(float(bchk)) <= float(bet) + 1e-6

def _read_ge(summary, thr):
    rr = summary.get('meter_read_rate')
    return rr is None or float(rr) >= thr                      # explicit threshold 0 accepts any read rate

def trust_overall(summary):
    """Pool this video's RTP / hit rate / spin count / OVERALL win-size distribution. A decimation test (drop a
    good video's balance reads down to 0.5, even in bursty chunks) showed these barely move — RTP within ~1-3pt,
    the win-size distribution within L1 ~0.05 — so the bar is min_read_rate_overall (default 0.5) AND the balance
    books closed. Feature stats do NOT survive that low -> trust_feature."""
    thr = CFG.get('min_read_rate_overall'); thr = float(thr) if thr is not None else 0.5
    return _read_ge(summary, thr) and _books_closed(summary)

def trust_feature(summary):
    """Pool this video's FEATURE stats (count / feature win-size distribution). Features play out in exactly the
    obscured (animation) spans, so a low-read video loses them — the same decimation test collapsed feature count
    by ~40% at 0.5 and to ~zero at 0.4. So features keep the higher bar: min_read_rate (default 0.6).
    Built on top of trust_overall so trust_feature(v) ALWAYS implies trust_overall(v) — even if a custom config
    sets min_read_rate_overall above min_read_rate (the feature bar then becomes the max of the two)."""
    thr = CFG.get('min_read_rate'); thr = float(thr) if thr is not None else 0.6
    return trust_overall(summary) and _read_ge(summary, thr)

trustworthy = trust_feature          # backward-compat alias: the original single gate was exactly this 0.6 bar

def infer_bet(vals):
    """most common balance drop = the bet"""
    drops = {}
    for r0, r1 in zip(vals, vals[1:]):
        a, b = r0[1], r1[1]
        if b < a: drops[a - b] = drops.get(a - b, 0) + 1
    if not drops: return None, 0
    bet, n = max(drops.items(), key=lambda kv: kv[1])
    return bet, n

# ---------------- spins ----------------
MAX_WIN_BETS = 5000          # a roll-up reading above A + this many bets is treated as a misread

def split_step(A, B, between, bet):
    """one step between two stable balances A -> B, with the raw readings in between (roll-up / deduction frames).
    -> (n_spins_started, win, win_before_spins)
    The win's roll-up peak cannot exceed the true win, so n = fewest spins with  B + n*bet - A >= peak - A.
    win_before_spins: the win was counting up BEFORE the deduction (= it belongs to the previous spin)."""
    rises = [(k, v) for k, v in enumerate(between) if A < v <= A + MAX_WIN_BETS * bet]
    peak = max(v for _, v in rises) if len(rises) >= 2 else A          # >= 2 readings -> a real roll-up, not one misread
    peak = max(peak, B)
    n = max(0, math.ceil((peak - B) / bet - 1e-9))
    win = B + n * bet - A
    first_rise = rises[0][0] if len(rises) >= 2 else None
    first_drop = next((k for k, v in enumerate(between) if v <= A - bet * 0.5), None)
    if first_rise is not None and (first_drop is None or first_rise < first_drop): before = True
    elif first_drop is not None: before = False
    else: before = True                                    # nothing seen in between -> usually the previous spin's win
    return n, win, before

def build_spins(series, vals, bet, gift_windows, seg_bounds):
    """walk balance transitions -> spins list + other events"""
    spins = []; gifts = []; jumps = []; odd = []
    gmax = float(CFG.get('gift_max_x') or 15)               # a balance jump >= this * bet is a win (feature payout), never a gift
    def in_any(t, wins): return any(a <= t <= b for a, b in wins)
    for r0, r1 in zip(vals, vals[1:]):
        A, B, t1 = r0[1], r1[1], r1[0]
        if in_any(t1, seg_bounds):                          # balance across a segment boundary = account reset
            jumps.append((t1, B - A, 'reset / new segment')); continue
        between = [v for _, v in series[r0[4] + 1:r1[3]] if v is not None]
        n, win, before = split_step(A, B, between, bet)
        if n > 20: jumps.append((t1, B - A, 'unexplained drop')); continue
        if n > 1: odd.append((t1, n))
        if win > 0 and (n == 0 or before):                  # win of the spin that was already running
            is_gift = n == 0 and in_any(t1, gift_windows) and win < gmax * bet   # small coin bump at a popup-dismiss tap; a big jump is a feature payout
            if spins and not is_gift: spins[-1]['win'] += win; spins[-1]['bal'] = B
            else: gifts.append((t1, win))
            win = 0
        for k in range(n):
            spins.append({'t': t1, 'bet': bet, 'win': 0, 'bal': B})
        if win > 0: spins[-1]['win'] += win                 # deduction first, then this new spin's own win
    return spins, gifts, jumps, odd

# ---------------- features (free games / hold & spin / bonus) ----------------
FEATURE_GAP_S = 25           # in the base game a spin is paid every ~5-8 s; >= this long without paying = a feature ran

def off_spans(stats):
    """video spans where the recorder was busy with popups / lobby / a stuck screen (not game play)"""
    out = []
    for a, b, info in stats.get('timeline') or []:
        try: i = json.loads(info) if isinstance(info, str) else (info or {})
        except ValueError: i = {}
        if any(i.get(k) for k in ('esc', 'lobby', 'frozen', 'x', 'btn')) or i.get('centre', 0) >= 1: out.append((a, b))
    return out

def feature_gap(spins):
    """gap that marks a feature: 25 s at normal speed; shorter when the game spins faster (turbo), never below 18 s
    (big-win celebrations do not get shorter with turbo). Spin interval = median over 5-spin windows (robust to the
    sparse live readings)."""
    t = [s['t'] for s in spins]
    if len(t) < 12: return FEATURE_GAP_S
    med = float(np.median([(t[k + 5] - t[k]) / 5 for k in range(len(t) - 5)]))
    return max(18.0, FEATURE_GAP_S * min(1.0, med / 3.8))

def _covered(a, b, spans):
    """seconds of [a, b] covered by the union of spans"""
    iv = sorted((max(a, x), min(b, y)) for x, y in spans if y > a and x < b); tot = 0.0; cur = None
    for x, y in iv:
        if cur and x <= cur[1]: cur[1] = max(cur[1], y)
        else:
            if cur: tot += cur[1] - cur[0]
            cur = [x, y]
    return tot + (cur[1] - cur[0] if cur else 0.0)

def find_features(spins, bet, excl=(), gap=None, series=None, strict=()):
    """excl: popup / gift spans - the in-game rest of the gap must still be >= gap (a big-win screen or a rating popup
    after a feature is common). strict: spans that disqualify the whole gap (unexplained balance jumps)"""
    """a spin that won and after which no new bet was paid for >= gap seconds (outside popup / stuck spans) = a feature
    was triggered on it (its win includes the whole feature). -> [{'spin', 't0', 't1', 'seconds', 'win', 'x'}]"""
    feats = []; gap = gap or feature_gap(spins)
    for k in range(len(spins) - 1):
        s, t1 = spins[k], spins[k + 1]['t']; g = t1 - s['t']
        if g < gap or s['win'] <= 0: continue
        if any(a < t1 and b > s['t'] for a, b in strict): continue
        if g - _covered(s['t'], t1, excl) < gap: continue
        if series is not None:                                 # meter unreadable most of the gap -> not evidence of a feature
            inside = [v for t, v in series if s['t'] < t < t1 and not any(x <= t <= y for x, y in excl)]   # in-game part only
            if inside and sum(v is not None for v in inside) / len(inside) < 0.5: continue
        feats.append({'spin': k + 1, 't0': s['t'], 't1': t1, 'seconds': round(g), 'win': float(s['win']), 'x': float(s['win'] / bet)})
    return feats

def confirm_features(feats, mp4, app_id, gift_windows=(), say=print):
    """drop candidates that are really popups: the gap overlaps a gift / X-close tap, or the video is mostly NOT the slot
    during the gap (3 frames checked with the app's in-game template; >= 2 must be in game)"""
    feats = [f for f in feats if not any(a < f['t1'] and b > f['t0'] for a, b in gift_windows)]
    if not feats: return feats
    try:
        from .app import App
        app = App(app_id)
    except Exception as e: say(f'feature check without frames: {e!r}'); return feats
    cap = cv2.VideoCapture(mp4); out = []
    if not cap.isOpened(): say('feature check: video not readable by OpenCV -> features not frame-checked'); return feats
    try:
        for f in feats:
            ok = 0
            for q in (0.3, 0.55, 0.8):
                cap.set(cv2.CAP_PROP_POS_MSEC, (f['t0'] + (f['t1'] - f['t0']) * q) * 1000)
                r, fr = cap.read()
                try:
                    if r and app.in_game(fr): ok += 1
                except Exception: ok += 1                      # cannot check this frame -> do not reject on it
            if ok >= 2: out.append(f)
    finally: cap.release()
    return out

def feature_summary(feats, n_spins, tot_bet, gap=FEATURE_GAP_S):
    xs = [f['x'] for f in feats]
    return {'count': len(feats), 'every_n': round(n_spins / len(feats), 1) if feats else None,
            'avg_x': float(np.mean(xs)) if xs else None, 'max_x': float(max(xs)) if xs else None,
            'rtp_share': float(sum(f['win'] for f in feats) / tot_bet) if tot_bet else 0.0, 'gap_s': round(gap)}

def cut_features(mp4, feats, say=print, max_clips=60):
    """<video>_features/NN_<time>_<x>x.mp4 (5 s before to 5 s after) + _start.jpg / _end.jpg, cut without re-encoding"""
    out = mp4[:-4] + '_features'
    if os.path.isdir(out):
        for f in os.listdir(out): os.remove(os.path.join(out, f))
    if not feats or not CFG.get('feature_clips', True): return
    os.makedirs(out, exist_ok=True); ff = TOOLS['ffmpeg'] or 'ffmpeg'
    for i, f in enumerate(sorted(feats, key=lambda f: -f['x'])[:max_clips], 1):
        name = f"{i:02d}_{hms(f['t0']).replace(':', '')}_{f['x']:.0f}x"
        a = max(0.0, f['t0'] - 5); dur = f['t1'] - f['t0'] + 10
        subprocess.run([ff, '-v', 'error', '-y', '-ss', f'{a:.1f}', '-i', mp4, '-t', f'{dur:.1f}', '-c', 'copy', os.path.join(out, name + '.mp4')],
                       creationflags=_NOWIN, capture_output=True)
        for tag, t in (('start', f['t0'] + 2), ('end', max(f['t0'], f['t1'] - 1))):
            subprocess.run([ff, '-v', 'error', '-y', '-ss', f'{t:.1f}', '-i', mp4, '-frames:v', '1', '-q:v', '3', os.path.join(out, f'{name}_{tag}.jpg')],
                           creationflags=_NOWIN, capture_output=True)
        f['clip'] = os.path.basename(out) + '/' + name + '.mp4'
    say(f'feature clips: {min(len(feats), max_clips)} -> {os.path.basename(out)}')

def _active_runs(vdir, exclude=None):
    """live <video>.analyzing_run markers = analyses past the gate and actually working right now"""
    out = []
    try: names = os.listdir(vdir)
    except OSError: return out
    for f in names:
        if not f.endswith('.analyzing_run'): continue
        p = os.path.join(vdir, f)
        if exclude and os.path.abspath(p) == os.path.abspath(exclude): continue
        if _lock_alive(p): out.append(p)
    return out

def _wait_for_slot(vdir, cap, own_run, say):
    """block until fewer than `cap` other analyses are actively running (soft cap; the marker is made only after this)"""
    told = False
    while len(_active_runs(vdir, own_run)) >= cap:
        if not told: say(f'分析佇列已滿（{cap} 個同時），排隊等候…'); told = True
        time.sleep(5)

def analyze(mp4, app_id=None, fps=4, say=print, gate=0):
    """analyse one video. A <video>.analyzing flag file exists while it runs (a merge waits for it instead of redoing it).
    gate>0: wait until fewer than `gate` analyses are actively running before starting the heavy work (keeps a big batch from thrashing)."""
    base = mp4[:-4]; lock = base + '.analyzing'; run = base + '.analyzing_run'
    try:
        with open(lock, 'w') as f: f.write(str(os.getpid()))
    except OSError: lock = None
    if gate and gate > 0:
        _wait_for_slot(os.path.dirname(os.path.abspath(mp4)) or '.', gate, run, say)
        try:
            with open(run, 'w') as f: f.write(str(os.getpid()))
        except OSError: run = None
    else: run = None
    try: return _analyze(mp4, app_id, fps, say)
    finally:
        for p in (run, lock):
            if p:
                try: os.remove(p)
                except OSError: pass

def _reconstruct(series, bet_reads, stats, fps, say=print):
    """balance series + recorder stats -> bet and the spin / gift / jump lists. No video needed, so both the full
    analysis (from the mp4) and a recompute (from the cached _meter.json) share one code path and cannot drift."""
    read_ok = sum(1 for _, v in series if v is not None) / max(1, len(series))
    vals = stable_values(series)
    bet_inf, n_inf = infer_bet(vals)
    bet_ocr = max(set(b for _, b in bet_reads), key=[b for _, b in bet_reads].count) if bet_reads else None
    bet_rec = stats.get('bet')
    # bet_inf (most common balance drop) is usually the bet, but on a noisy / low-read video it can latch onto a garbage
    # drop (a misread balance, a sub-bet drop). Two independent sources are more trustworthy: the bet the recorder set and
    # the on-screen bet meter. When those two agree, that IS the bet (bet_inf only matters when nothing else is known).
    # When only the recorder bet is known, keep bet_inf unless it is wildly off (> 2x). No recorder bet (old recordings)
    # -> original balance-drop-first behaviour, so this never regresses them.
    _bi = f'{bet_inf:,}' if bet_inf else '無（讀不到餘額下降）'   # bet_inf is None when no clean drop was seen (low-read video)
    if bet_ocr and bet_rec and bet_ocr == bet_rec and bet_inf != bet_rec:
        say(f'bet 推斷 {_bi} 與押注欄＋錄製一致的 {bet_rec:,} 不符（讀取率 {read_ok:.0%}）→ 改用 {bet_rec:,}')
        bet = bet_rec
    elif bet_rec and bet_inf and not (0.5 <= bet_inf / bet_rec <= 2):
        say(f'bet 推斷 {bet_inf:,} 與錄製設定 {bet_rec:,} 差太多（讀取率 {read_ok:.0%}，押注欄 {bet_ocr}）→ 改用錄製設定 {bet_rec:,}')
        bet = bet_rec
    else:
        bet = bet_inf or bet_ocr or bet_rec
    if not bet: raise SystemExit('could not determine the bet (no balance drops found)')
    vals = clean_jumps(vals, bet, fps)
    n_inf = sum(1 for r0, r1 in zip(vals, vals[1:]) if r0[1] - r1[1] == bet)
    GIFT = ('CLAIM', 'GREAT', 'WHEEL', 'GIFT', 'BONUS', 'REWARD')
    gift_windows = [(t - 2, t + 12) for t, kind, label in (stats.get('taps') or [])
                    if kind == 'x' or (kind in ('btn', 'ocr') and any(g in str(label).upper() for g in GIFT))]
    bounds, acc = [], 0.0
    segl = stats.get('segments') if isinstance(stats.get('segments'), list) else []
    for _, dur in segl[:-1]:
        acc += dur; bounds.append((acc - 3, acc + 15))
    spins, gifts, jumps, odd = build_spins(series, vals, bet, gift_windows, bounds)
    return {'read_ok': read_ok, 'vals': vals, 'bet': bet, 'bet_inf': bet_inf, 'n_inf': n_inf, 'bet_ocr': bet_ocr,
            'gift_windows': gift_windows, 'bounds': bounds, 'spins': spins, 'gifts': gifts, 'jumps': jumps, 'odd': odd}

def _summarize(base, prof, stats, series, R, mp4, app_id, t_start, say=print):
    """build the summary dict from a reconstruction R. mp4 given -> features are frame-confirmed and clipped; mp4 None
    (recompute from cache) -> features are the gap-based candidates without frame confirmation. Returns (summary, trig)."""
    read_ok, vals, bet = R['read_ok'], R['vals'], R['bet']
    gift_windows, spins, gifts, jumps, odd = R['gift_windows'], R['spins'], R['gifts'], R['jumps'], R['odd']
    n = len(spins)
    if n == 0: raise SystemExit('no spins found')
    wins = np.array([s['win'] for s in spins], float)
    core, mult = spin_stats(wins, [bet] * n)
    tot_bet, tot_win, rtp = core['total_bet'], core['total_win'], core['rtp']; lo, hi = core['rtp_95']
    vid_s = stats.get('video_s') or (series[-1][0] if series else 0)
    off_s = sum(b - a for a, b, _ in (stats.get('timeline') or []))
    play_h = max(1e-6, (vid_s - off_s) / 3600)
    top = sorted(range(n), key=lambda i: -wins[i])[:10]
    feats = find_features(spins, bet, off_spans(stats) + list(gift_windows), series=series,
                          strict=[(t - 1, t + 1) for t, _, _ in jumps])
    if mp4:                                                  # frame-confirm + clip only when the video is on hand
        feats = confirm_features(feats, mp4, app_id, (), say)
        try: cut_features(mp4, feats, say)
        except Exception as e: say(f'feature clips failed: {e!r}')
    # split the win-size distribution: a feature folds its whole payout onto the spin that triggered it, so those spins
    # would otherwise dominate the high buckets. base = spins that did NOT trigger a feature; feature = each feature's
    # total-payout multiple. counts of the two add up to n; their rtp shares add up to the overall RTP.
    trig = {f['spin'] - 1 for f in feats}                    # f['spin'] is the 1-based index of the trigger spin
    base_i = [i for i in range(n) if i not in trig]
    base_dist = win_dist(mult[base_i], wins[base_i], tot_bet, n) if base_i else []
    feat_dist = win_dist([f['x'] for f in feats], [f['win'] for f in feats], tot_bet, n) if feats else []
    summary = {
        'video': os.path.basename(mp4) if mp4 else (stats.get('file') or os.path.basename(base) + '.mp4'),
        'app': prof['name'], 'slot': stats.get('name') or stats.get('slot'),
        'video_length': hms(vid_s), 'play_hours': round(play_h, 3), 'off_game': hms(off_s),
        'bet': bet, 'bet_check': {'from_balance_drops': R['bet_inf'], 'drops_seen': R['n_inf'], 'from_bet_meter': R['bet_ocr'], 'recorder': stats.get('bet')},
        'spins': n, 'spins_per_hour': round(n / play_h), 'total_bet': tot_bet, 'total_win': tot_win,
        'rtp': rtp, 'rtp_95': [lo, hi], 'hit_rate': core['hit_rate'], 'hit_95': core['hit_95'], 'hit_every_n': core['hit_every_n'],
        'net_per_hour': (tot_win - tot_bet) / play_h, 'net_per_hour_in_bets': (tot_win - tot_bet) / bet / play_h,
        'distribution': core['distribution'], 'base_distribution': base_dist, 'feature_distribution': feat_dist,
        'group': stats.get('group') or '', 'emulator': stats.get('emulator'),
        'top_wins': [{'time': hms(spins[i]['t']), 'win': float(wins[i]), 'x': float(mult[i])} for i in top if wins[i] > 0],
        'gifts': [{'time': hms(t), 'coins': c} for t, c in gifts], 'jumps': [{'time': hms(t), 'coins': c, 'why': w} for t, c, w in jumps],
        'multi_spin_steps': len(odd),
        'features': dict(feature_summary(feats, n, tot_bet, feature_gap(spins)), list=[{'time': hms(f['t0']), 'spin': f['spin'], 'seconds': f['seconds'], 'win': f['win'],
                                                                   'x': f['x'], 'clip': f.get('clip')} for f in feats]),
        'balance_first': vals[0][1] if vals else None, 'balance_last': vals[-1][1] if vals else None,
        'meter_read_rate': round(read_ok, 3), 'analysis_seconds': round(time.time() - t_start),
        'analyzer_version': VERSION,
    }
    # sanity: balance change must equal wins - bets + gifts + jumps (all derived from the same series -> checks the bookkeeping)
    summary['balance_check'] = (summary['balance_last'] - summary['balance_first']) - (tot_win - tot_bet + sum(c for _, c in gifts) + sum(c for _, c, _ in jumps))
    return summary, trig

def write_spins_tsv(path, spins, bet, trig):
    """per-spin table shared by the full analysis and the from-cache recompute (one format, no drift)."""
    with open(path, 'w', encoding='utf-8') as f:
        f.write('spin\ttime\tt_sec\tbet\twin\tx_bet\tbalance_after\ttype\n')      # type: base / feature (trigger spin)
        for i, s in enumerate(spins, 1):
            f.write(f"{i}\t{hms(s['t'])}\t{s['t']:.1f}\t{s['bet']}\t{s['win']:.0f}\t{s['win'] / bet:.2f}\t{s['bal']}"
                    f"\t{'feature' if (i - 1) in trig else 'base'}\n")

def _analyze(mp4, app_id=None, fps=4, say=print):
    base = mp4[:-4]
    stats = {}
    if os.path.exists(base + '_stats.json'):
        with open(base + '_stats.json', encoding='utf-8') as f: stats = json.load(f)
    app_id = app_id or stats.get('app') or os.path.basename(os.path.dirname(os.path.abspath(mp4)))
    prof = json.load(open(os.path.join(ROOT, 'apps', app_id, 'profile.json'), encoding='utf-8'))
    size = video_size(mp4)                                   # landscape slots are recorded as 1280x720 videos
    boxes = (prof.get('landscape') or {}).get('meters') if size[0] > size[1] else prof.get('meters')
    if not boxes or 'balance' not in boxes: raise SystemExit(f'{app_id}: profile has no meters.balance box for a {size[0]}x{size[1]} video')
    t_start = time.time(); say(f'reading balance meter: {os.path.basename(mp4)}')
    cache = base + '_meter.json'
    c = None
    if os.path.exists(cache) and os.path.getmtime(cache) > os.path.getmtime(mp4):
        with open(cache) as f: c = json.load(f)
        if c.get('boxes') != boxes or c.get('fps') != fps: c = None            # profile boxes / orientation changed
    if c:
        series, bet_reads = [tuple(x) for x in c['series']], [tuple(x) for x in c['bets']]
    else:
        series, bet_reads = read_series(mp4, boxes, fps, progress=lambda t: say(f'  {hms(t)} read'), size=size)
        with open(cache, 'w') as f: json.dump({'fps': fps, 'boxes': boxes, 'series': series, 'bets': bet_reads}, f)
    R = _reconstruct(series, bet_reads, stats, fps, say)
    summary, trig = _summarize(base, prof, stats, series, R, mp4, app_id, t_start, say)
    spins, bet = R['spins'], R['bet']
    write_spins_tsv(base + '_spins.tsv', spins, bet, trig)
    with open(base + '_summary.json', 'w', encoding='utf-8') as f: json.dump(summary, f, indent=1, ensure_ascii=False)
    with open(base + '_summary.html', 'w', encoding='utf-8') as f: f.write(render_html(summary))
    try:                                                     # per-spin handoff for a downstream board recognizer (slot-video-recog / te-ldplayer)
        from .handoff import build as _handoff; _handoff(mp4, say)
    except Exception as e: say(f'handoff not made: {e!r}')
    try:                                                     # accumulate this video's features into the persistent per-game ledger
        from .ledger import add as _ledger; _ledger(base + '_summary.json', say)
    except Exception as e: say(f'ledger not updated: {e!r}')
    _pdf(base + '_summary.html', say)
    lo, hi = summary['rtp_95']
    say(f'spins {summary["spins"]}  RTP {summary["rtp"]:.1%} ({lo and f"{lo:.0%}"}~{hi and f"{hi:.0%}"})  hit 1/{summary["hit_every_n"]}  '
        f'bet {bet:,}  read {R["read_ok"]:.0%}  -> {os.path.basename(base)}_summary.html')
    return summary

def _pdf(html_path, say):
    """PDF copy of a report (headless Edge / Chrome); never fails the analysis"""
    if not CFG.get('auto_pdf', True): return
    try:
        from .overview import to_pdf
        to_pdf(html_path, say)
    except Exception as e: say(f'PDF not made: {e!r}')

def fmt(v):
    if v is None: return '—'
    a = abs(v)
    for unit, k in (('兆', 1e12), ('億', 1e8), ('萬', 1e4)):
        if a >= k: return f'{v / k:,.2f} {unit}'
    return f'{v:,.0f}'

def _feat_html(s, merged=False):
    e = html.escape; f = s.get('features') or {}
    if not f: return ''
    if not f.get('count'): return f'<h3>特色遊戲（自動偵測）</h3><p>這段影片沒有偵測到特色遊戲（{s["spins"]:,} 把）。</p>'
    head = (f"<table><tr><th>項目</th><th>數值</th><th>白話</th></tr>"
            f"<tr><td>觸發次數</td><td>{f['count']:,} 次</td><td>平均每 {f['every_n']} 把觸發一次</td></tr>"
            f"<tr><td>特色平均贏分</td><td>{f['avg_x']:,.1f} 倍押注</td><td>最大 {f['max_x']:,.1f} 倍</td></tr>"
            f"<tr><td>佔 RTP</td><td>{f['rtp_share']:.1%}</td><td>總回收裡有這麼多來自特色遊戲</td></tr></table>")
    if merged: return '<h3>特色遊戲（自動偵測）</h3>' + head
    link = lambda x: f"<a href='{e(x['clip'])}'>片段</a>" if x.get('clip') else ''
    rows = ''.join(f"<tr><td>{x['time']}</td><td>{x['seconds']} 秒</td><td>{x['x']:,.1f} 倍</td><td>{fmt(x['win'])}</td>"
                   f"<td>{link(x)}</td></tr>" for x in f.get('list', []))
    return ('<h3>特色遊戲（自動偵測）</h3>' + head +
            f"<table><tr><th>影片時間</th><th>長度</th><th>贏分倍數</th><th>贏分</th><th>片段</th></tr>{rows}</table>")

def _dist_rows(dl, skip_zero=False):
    return ''.join(f"<tr><td>{html.escape(d['label'])}</td><td>{d['count']:,}</td>"
                   f"<td>{'每 %s 把' % d['every_n'] if d['every_n'] else '—'}</td><td>{d['rtp_share']:.1%}</td></tr>"
                   for d in dl if not (skip_zero and d['label'].startswith('= 0')))

def render_html(s):
    e = html.escape
    pct = lambda x: '—' if x is None else f'{x:.1%}'
    rows = _dist_rows(s['distribution'])
    th = '<tr><th>贏分</th><th>次數</th><th>頻率</th><th>佔 RTP</th></tr>'
    if s.get('base_distribution') is not None:               # base game vs feature split (new reports)
        base_rows = _dist_rows(s['base_distribution'])
        feat_rows = _dist_rows(s.get('feature_distribution') or [], skip_zero=True)
        feat_tbl = (f'<table>{th}{feat_rows}</table>' if feat_rows else '<p>此影片未偵測到特色。</p>')
        dist_section = (
            '<h3>贏分大小分布 — base game（未觸發特色的局）</h3>'
            f'<table>{th}{base_rows}</table>'
            '<h3>贏分大小分布 — 特色（整個特色的總贏分，以押注倍數計）</h3>'
            '<p style="color:#666;margin:2px 0">特色觸發局整局歸在這裡；其 win 含該把自己的 base 連線獎（免費遊戲中途無扣注邊界可再切）。兩表次數相加＝總局數。</p>'
            + feat_tbl)
    else:                                                    # old report without the split -> single combined table
        dist_section = f'<h3>贏分大小分布（以押注倍數計）</h3><table>{th}{rows}</table>'
    tops = ''.join(f"<tr><td>{t['time']}</td><td>{fmt(t['win'])}</td><td>{t['x']:,.1f} 倍</td></tr>" for t in s['top_wins'])
    gifts = ''.join(f"<tr><td>{g['time']}</td><td>{fmt(g['coins'])}</td><td>關彈窗時加的錢（不算贏分）</td></tr>" for g in s['gifts']) + \
            ''.join(f"<tr><td>{j['time']}</td><td>{fmt(j['coins'])}</td><td>{e(j['why'])}</td></tr>" for j in s['jumps'])
    warn = []
    if s['meter_read_rate'] < 0.6: warn.append(f"餘額只讀到 {s['meter_read_rate']:.0%} 的畫面，結果可能不完整")
    b = s['bet_check']
    if b['from_bet_meter'] and b['from_balance_drops'] and b['from_bet_meter'] != b['from_balance_drops']:
        warn.append(f"押注不一致：餘額推算 {b['from_balance_drops']:,}，押注欄讀到 {b['from_bet_meter']:,}")
    if s['spins'] < 1000: warn.append(f"只有 {s['spins']:,} 把，RTP 範圍很寬，僅供參考")
    w = ''.join(f'<li>{e(x)}</li>' for x in warn) or '<li>無</li>'
    lo, hi = s['rtp_95']
    return f"""<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>{e(str(s['slot']))} 統計</title>
<style>body{{font-family:"Microsoft JhengHei",sans-serif;margin:24px;max-width:900px;color:#222}}
table{{border-collapse:collapse;margin:8px 0 20px;page-break-inside:avoid}}td,th{{border:1px solid #ccc;padding:4px 10px;text-align:right}}
th{{background:#f3f3f3}}td:first-child,th:first-child{{text-align:left}}.big{{font-size:22px;font-weight:bold}}</style>
<h2>{e(s['app'])}｜{e(str(s['slot']))}</h2>
<p>影片 {e(s['video'])}（{s['video_length']}，其中非遊戲畫面 {s['off_game']}）</p>
<table><tr><th>項目</th><th>數值</th><th>白話</th></tr>
<tr><td>押注</td><td>{fmt(s['bet'])}</td><td>每把</td></tr>
<tr><td>遊戲局數</td><td>{s['spins']:,} 把</td><td>每小時約 {s['spins_per_hour']:,} 把</td></tr>
<tr><td>實測 RTP（回收率）</td><td class=big>{pct(s['rtp'])}</td><td>95% 範圍 {pct(lo)} ~ {pct(hi)}；每押 100 拿回約 {s['rtp']*100:.0f}</td></tr>
<tr><td>中獎率</td><td>{pct(s['hit_rate'])}</td><td>平均每 {s['hit_every_n']} 把中一次（95% 範圍 {pct(s['hit_95'][0])} ~ {pct(s['hit_95'][1])}）</td></tr>
<tr><td>每小時輸贏</td><td>{fmt(s['net_per_hour'])}</td><td>約 {s['net_per_hour_in_bets']:,.0f} 注／小時</td></tr>
<tr><td>總押注 / 總贏分</td><td>{fmt(s['total_bet'])} / {fmt(s['total_win'])}</td><td></td></tr>
<tr><td>餘額</td><td>{fmt(s['balance_first'])} → {fmt(s['balance_last'])}</td><td>帳目核對差額 {fmt(s['balance_check'])}（應為 0）</td></tr></table>
{dist_section}
<h3>前 10 大獎（影片時間，可直接跳去看）</h3>
<table><tr><th>影片時間</th><th>贏分</th><th>押注倍數</th></tr>{tops}</table>
{_feat_html(s)}
<h3>非贏分的金額變動</h3>
<table><tr><th>影片時間</th><th>金額</th><th>說明</th></tr>{gifts or '<tr><td colspan=3>無</td></tr>'}</table>
<h3>注意事項</h3><ul>{w}<li>贏分＝餘額增加；免費遊戲、特色遊戲的贏分算在觸發的那一把。</li>
<li>特色遊戲＝某一把贏分後超過 {(s.get('features') or {}).get('gap_s', FEATURE_GAP_S)} 秒沒有再扣押注（免費遊戲、Hold &amp; Spin 這類期間不扣錢），而且期間畫面仍是遊戲本身；
彈窗、領獎、卡住的時段不算。很長的大獎動畫可能被算進來；整個畫面換成另一個小遊戲的特色可能漏算。請用片段確認。</li>
<li>關彈窗前後 12 秒內的加錢視為「贈送」，不算贏分（可能把少數真的贏分誤判成贈送，見上表）。</li></ul>
<p style="color:#888">餘額讀取率 {s['meter_read_rate']:.0%}，分析耗時 {s['analysis_seconds']} 秒</p></html>"""

# ---------------- several videos of the same slot (multi-emulator run) -> one report ----------------
def _stats_of(mp4):
    p = mp4[:-4] + '_stats.json'
    if not os.path.exists(p): return {}
    with open(p, encoding='utf-8') as f: return json.load(f)

def _pid_alive(pid):
    if os.name != 'nt': 
        try: os.kill(pid, 0); return True
        except OSError: return False
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h: return False
    code = ctypes.c_ulong(); ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)); ctypes.windll.kernel32.CloseHandle(h)
    return code.value == 259                                              # STILL_ACTIVE

def _lock_alive(lock):
    """a <video>.analyzing flag of an analysis that is still running (not one left behind by a crash)"""
    try:
        age = time.time() - os.path.getmtime(lock)
        with open(lock) as f: pid = f.read().strip()
    except OSError: return False
    if pid.isdigit(): return _pid_alive(int(pid))
    return age < 120                                                      # flag just created, pid not written yet

def fresh_summary(mp4, app_id=None, say=print):
    """the video's summary: reuse it if up to date, wait if its analysis is running right now, else analyse"""
    base = mp4[:-4]; js = base + '_summary.json'; lock = base + '.analyzing'; told = False
    while _lock_alive(lock):
        if not told: say(f'waiting for the analysis already running on {os.path.basename(mp4)}'); told = True
        time.sleep(10)
    if os.path.exists(js) and os.path.exists(base + '_spins.tsv') and os.path.getmtime(js) >= os.path.getmtime(mp4):
        with open(js, encoding='utf-8') as f: return json.load(f)
    return analyze(mp4, app_id, say=say)

def load_spins(base, feat=False):
    """[(time, bet, win)] from <base>_spins.tsv. feat=True -> [(time, bet, win, is_feature)] where is_feature is
    True/False from the 'type' column, or None when the file predates that column (old analysis -> type unknown)."""
    out = []
    with open(base + '_spins.tsv', encoding='utf-8') as f:
        next(f, None)
        for line in f:
            p = line.rstrip('\n').split('\t')
            if len(p) < 5: continue
            if not feat: out.append((p[1], float(p[3]), float(p[4]))); continue
            isf = (p[7] == 'feature') if len(p) >= 8 else None
            out.append((p[1], float(p[3]), float(p[4]), isf))
    return out

def merge(videos, out_base, app_id=None, say=print):
    """all spins of the same slot from several videos (e.g. 3 emulators x 20 min) -> one report <out_base>_summary.html"""
    parts = []
    for v in videos:
        try: s = fresh_summary(v, app_id, say)
        except (SystemExit, Exception) as e: say(f'{os.path.basename(v)}: not used ({e!r})'); continue
        parts.append((v, s, load_spins(v[:-4]), _stats_of(v)))
    if not parts: raise SystemExit('none of the videos could be analysed')
    wins = [w for _, _, rows, _ in parts for _, _, w in rows]; bets = [b for _, _, rows, _ in parts for _, b, _ in rows]
    if not wins: raise SystemExit('no spins in these videos')
    core, mult = spin_stats(wins, bets)
    play_h = max(1e-6, sum(s['play_hours'] for _, s, _, _ in parts)); n = core['spins']
    net = core['total_win'] - core['total_bet']; bet_set = sorted(set(bets)); mean_bet = core['total_bet'] / n
    allsp = [(os.path.basename(v), t, b, w) for v, _, rows, _ in parts for t, b, w in rows]
    top = sorted(range(n), key=lambda i: -mult[i])[:10]
    first = parts[0][1]
    # base / feature split (same basis as a single video): mark each spin from its video's detected feature triggers,
    # then bin non-trigger spins (base) and the feature total-payout multiples (feature) over the same buckets.
    feat_flags, all_known = [], True
    for v, s, rows, _ in parts:
        all_known = all_known and (s.get('features') is not None)      # every part must be feature-aware, else a part's
        trig = {x['spin'] - 1 for x in ((s.get('features') or {}).get('list') or [])}   # unlabelled feature wins would land in base
        feat_flags += [(j in trig) for j in range(len(rows))]
    wa = np.asarray(wins, float); fa = np.asarray(feat_flags, bool)
    base_dist = win_dist(mult[~fa], wa[~fa], core['total_bet'], n) if (all_known and (~fa).any()) else None
    feat_dist = win_dist(mult[fa], wa[fa], core['total_bet'], n) if all_known else None
    summary = {
        'merged': True, 'app': first['app'], 'group': first.get('group') or '',
        'slot': next((s['slot'] for _, s, _, _ in parts if s.get('slot')), None) or re.sub(r'^\d{8}_\d{6}_|_merged$', '', os.path.basename(out_base)),
        'videos': [{'video': os.path.basename(v), 'emulator': st.get('emulator', s.get('emulator')), 'video_length': s['video_length'],
                    'play_hours': s['play_hours'], 'spins': s['spins'], 'rtp': s['rtp'], 'bet': s['bet'],
                    'meter_read_rate': s['meter_read_rate'], 'balance_check': s.get('balance_check')} for v, s, _, st in parts],
        'bet': bet_set[0] if len(bet_set) == 1 else None, 'bets': bet_set, 'play_hours': round(play_h, 3),
        'video_hours': round(sum(st.get('video_s') or 0 for _, _, _, st in parts) / 3600, 3),
        'spins_per_hour': round(n / play_h), **core,
        'net_per_hour': net / play_h, 'net_per_hour_in_bets': net / mean_bet / play_h,
        'top_wins': [{'video': allsp[i][0], 'time': allsp[i][1], 'win': allsp[i][3], 'x': float(mult[i])} for i in top if mult[i] > 0],
        'gifts_total': sum(sum(g['coins'] for g in s.get('gifts', [])) for _, s, _, _ in parts),
        'gifts_n': sum(len(s.get('gifts', [])) for _, s, _, _ in parts),
        'base_distribution': base_dist, 'feature_distribution': feat_dist,
        'features': _merge_features(parts, n, core['total_bet']),
        'meter_read_rate': min(s['meter_read_rate'] for _, s, _, _ in parts),
    }
    with open(out_base + '_spins.tsv', 'w', encoding='utf-8') as f:
        f.write('spin\tvideo\ttime\tbet\twin\tx_bet\ttype\n')
        for i, ((vn, t, b, w), isf) in enumerate(zip(allsp, feat_flags), 1):
            f.write(f'{i}\t{vn}\t{t}\t{b:.0f}\t{w:.0f}\t{w / b:.2f}\t{"feature" if isf else "base"}\n')
    with open(out_base + '_summary.json', 'w', encoding='utf-8') as f: json.dump(summary, f, indent=1, ensure_ascii=False)
    with open(out_base + '_summary.html', 'w', encoding='utf-8') as f: f.write(render_merged_html(summary))
    _pdf(out_base + '_summary.html', say)
    lo, hi = core['rtp_95']
    say(f"MERGED {len(parts)} videos, spins {n}  RTP {core['rtp']:.1%} ({lo and f'{lo:.0%}'}~{hi and f'{hi:.0%}'})  -> {out_base}_summary.html")
    return summary

def _merge_features(parts, n, tot_bet):
    with_f = [s for _, s, _, _ in parts if s.get('features') is not None]     # older analyses have no feature data
    if not with_f: return {}
    fl = [x for s in with_f for x in (s['features'].get('list') or [])]
    return feature_summary([{'x': x['x'], 'win': x['win']} for x in fl], sum(s['spins'] for s in with_f),
                           sum(s['total_bet'] for s in with_f), min(s['features'].get('gap_s', FEATURE_GAP_S) for s in with_f))

def render_merged_html(s):
    e = html.escape
    pct = lambda x: '—' if x is None else f'{x:.1%}'
    dh = '<table><tr><th>贏分</th><th>次數</th><th>頻率</th><th>佔 RTP</th></tr>'
    if s.get('base_distribution') is not None:
        feat_n = sum(d['count'] for d in s['feature_distribution'])
        feat_tbl = (dh + _dist_rows(s['feature_distribution'], skip_zero=True) + '</table>'
                    '<p style="color:#888">特色那張略過「= 0 倍」（特色一定有贏分）。特色觸發局的贏分含該局自身的連線獎，無法再切開，'
                    '所以特色列的是整個特色的總贏分倍數；兩張表的局數相加 = 總局數，佔 RTP 相加 = 總 RTP。</p>'
                    if feat_n else '<p>這批影片沒有偵測到特色遊戲。</p>')
        dist_section = ('<h3>贏分大小分布｜base game（未觸發特色的局，以押注倍數計）</h3>' + dh + _dist_rows(s['base_distribution']) + '</table>'
                        '<h3>贏分大小分布｜特色（整個特色的總贏分，以押注倍數計）</h3>' + feat_tbl)
    else:
        dist_section = '<h3>贏分大小分布（以押注倍數計）</h3>' + dh + _dist_rows(s['distribution']) + '</table>'
    vids = ''.join(f"<tr><td>{e(v['video'])}</td><td>{e(str(v['emulator']))}</td><td>{v['video_length']}</td><td>{v['spins']:,}</td>"
                   f"<td>{pct(v['rtp'])}</td><td>{fmt(v['bet'])}</td><td>{v['meter_read_rate']:.0%}</td></tr>" for v in s['videos'])
    tops = ''.join(f"<tr><td>{e(t['video'])}</td><td>{t['time']}</td><td>{fmt(t['win'])}</td><td>{t['x']:,.1f} 倍</td></tr>" for t in s['top_wins'])
    warn = []
    if len(s['bets']) > 1: warn.append('各支影片的押注不同：' + '、'.join(fmt(b) for b in s['bets']) + '；RTP 以總贏分 ÷ 總押注計算，倍數分布以各自押注計算')
    if s['meter_read_rate'] < 0.6: warn.append(f"有影片的餘額只讀到 {s['meter_read_rate']:.0%}，結果可能不完整")
    if s['spins'] < 1000: warn.append(f"只有 {s['spins']:,} 把，RTP 範圍很寬，僅供參考")
    w = ''.join(f'<li>{e(x)}</li>' for x in warn) or '<li>無</li>'
    lo, hi = s['rtp_95']
    bet_txt = fmt(s['bet']) if s['bet'] else '多種（見下表）'
    return f"""<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>{e(str(s['slot']))} 合併統計</title>
<style>body{{font-family:"Microsoft JhengHei",sans-serif;margin:24px;max-width:900px;color:#222}}
table{{border-collapse:collapse;margin:8px 0 20px;page-break-inside:avoid}}td,th{{border:1px solid #ccc;padding:4px 10px;text-align:right}}
th{{background:#f3f3f3}}td:first-child,th:first-child{{text-align:left}}.big{{font-size:22px;font-weight:bold}}</style>
<h2>{e(s['app'])}｜{e(str(s['slot']))}（{len(s['videos'])} 支影片合併）</h2>
<p>同一款遊戲由 {len(s['videos'])} 支影片（不同模擬器、各自的帳號）合計，影片共 {s['video_hours']:.2f} 小時，其中遊戲中 {s['play_hours']:.2f} 小時。</p>
<table><tr><th>項目</th><th>數值</th><th>白話</th></tr>
<tr><td>押注</td><td>{bet_txt}</td><td>每把</td></tr>
<tr><td>遊戲局數</td><td>{s['spins']:,} 把</td><td>每台每小時約 {s['spins_per_hour']:,} 把</td></tr>
<tr><td>實測 RTP（回收率）</td><td class=big>{pct(s['rtp'])}</td><td>95% 範圍 {pct(lo)} ~ {pct(hi)}；每押 100 拿回約 {s['rtp']*100:.0f}</td></tr>
<tr><td>中獎率</td><td>{pct(s['hit_rate'])}</td><td>平均每 {s['hit_every_n']} 把中一次（95% 範圍 {pct(s['hit_95'][0])} ~ {pct(s['hit_95'][1])}）</td></tr>
<tr><td>每小時輸贏（一台）</td><td>{fmt(s['net_per_hour'])}</td><td>約 {s['net_per_hour_in_bets']:,.0f} 注／小時</td></tr>
<tr><td>總押注 / 總贏分</td><td>{fmt(s['total_bet'])} / {fmt(s['total_win'])}</td><td></td></tr>
<tr><td>關彈窗時加的錢</td><td>{fmt(s['gifts_total'])}</td><td>{s['gifts_n']} 次，不算贏分</td></tr></table>
{_feat_html(s, merged=True)}
<h3>各支影片</h3>
<table><tr><th>影片</th><th>模擬器</th><th>長度</th><th>局數</th><th>RTP</th><th>押注</th><th>餘額讀取率</th></tr>{vids}</table>
{dist_section}
<h3>前 10 大獎（以押注倍數排序；哪支影片、影片時間）</h3>
<table><tr><th>影片</th><th>影片時間</th><th>贏分</th><th>押注倍數</th></tr>{tops}</table>
<h3>注意事項</h3><ul>{w}<li>單台的 RTP 會因帳號、時段不同而起伏；合併後局數較多，範圍較窄。</li>
<li>每支影片各自的明細見同名的 _summary.html。</li></ul></html>"""

def _fsafe(name, slot=None):
    """file-name part: ASCII names as they are, others -> the slot id"""
    n = str(name or '')
    return n if (n.isascii() and not re.search(r'[\\/:*?"<>|]', n)) else re.sub(r'[^0-9A-Za-z_\-]+', '_', str(slot or 'game'))

def merge_group(group, app_id, say=print):
    """every video of one multi-emulator run (stats.json group == id) -> one merged report per slot. -> [html paths]"""
    vdir = video_dir(app_id); by_slot = {}
    for f in sorted(os.listdir(vdir)):
        if not f.endswith('_stats.json'): continue
        try:
            with open(os.path.join(vdir, f), encoding='utf-8') as fh: st = json.load(fh)
        except (OSError, ValueError): continue
        mp4 = os.path.join(vdir, f[:-len('_stats.json')] + '.mp4')
        if st.get('group') == group and os.path.exists(mp4) and (st.get('video_s') or 0) >= 60:
            by_slot.setdefault((st.get('slot'), st.get('name') or st.get('slot')), []).append(mp4)
    out = []
    for (slot, name), vids in by_slot.items():
        say(f'--- {name}: {len(vids)} video(s) ---')
        try:
            merge(vids, os.path.join(vdir, f'{group}_{_fsafe(name, slot)}_merged'), app_id, say)
            out.append(os.path.join(vdir, f'{group}_{_fsafe(name, slot)}_merged_summary.html'))
        except SystemExit as e: say(f'{name}: {e}')
        except Exception as e: say(f'{name}: merge failed {e!r}')            # one bad slot must not stop the others
    if not by_slot: say(f'no videos of group {group} in {vdir}')
    return out

def _is_current(summary_json):
    """True if this per-video report was already made by the current analyzer version."""
    try:
        with open(summary_json, encoding='utf-8') as f: return json.load(f).get('analyzer_version') == VERSION
    except (OSError, ValueError): return False

def reanalyze_all(app_id, force=False, gate=0, say=print):
    """Re-run analysis on every video of one app so old reports pick up the current buckets / tables, then rebuild
    the per-run merged reports and the overview. Resumable: videos already on this version are skipped unless force."""
    vdir = video_dir(app_id)
    vids = [os.path.join(vdir, f) for f in sorted(os.listdir(vdir)) if f.endswith('.mp4') and not f.endswith('_clean.mp4')]
    todo = [v for v in vids if force or not _is_current(v[:-4] + '_summary.json')]
    say(f'重新辨識：{len(todo)} / {len(vids)} 支（{len(vids) - len(todo)} 支已是 v{VERSION}，跳過）')
    for i, v in enumerate(todo, 1):
        say(f'[{i}/{len(todo)}] {os.path.basename(v)}')
        try: analyze(v, app_id, gate=gate, say=say)
        except SystemExit as e: say(f'  {os.path.basename(v)} -> {e}')
        except Exception as e: say(f'  {os.path.basename(v)} -> failed {e!r}')
    groups = set()
    for f in os.listdir(vdir):
        if not f.endswith('_stats.json'): continue
        try:
            with open(os.path.join(vdir, f), encoding='utf-8') as fh: g = json.load(fh).get('group')
            if g: groups.add(g)
        except (OSError, ValueError): pass
    for g in sorted(groups):
        try: merge_group(g, app_id, say)
        except Exception as e: say(f'merge group {g} -> {e!r}')
    from .overview import safe_build
    safe_build([app_id], say)
    say(f'✅ 重新辨識完成：{len(todo)} 支已更新到 v{VERSION}')

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('videos', nargs='*'); ap.add_argument('--app'); ap.add_argument('--fps', type=float, default=4)
    ap.add_argument('--merge', action='store_true', help='videos of the same slot -> also one merged report')
    ap.add_argument('--group', help='merge every video of this multi-emulator run (needs --app)')
    ap.add_argument('--gate', type=int, default=0, help='max analyses running at once (0 = no limit); extras queue')
    ap.add_argument('--reanalyze-all', action='store_true', help='re-run every video of --app (skips ones already on this version unless --force), then re-merge + overview')
    ap.add_argument('--force', action='store_true', help='with --reanalyze-all: redo even up-to-date videos')
    a = ap.parse_args()
    if a.reanalyze_all:
        if not a.app: raise SystemExit('--reanalyze-all needs --app')
        reanalyze_all(a.app, force=a.force, gate=a.gate); sys.exit(0)
    if a.group:
        if not a.app: raise SystemExit('--group needs --app')
        merge_group(a.group, a.app)
        from .overview import safe_build
        safe_build([a.app]); sys.exit(0)
    for v in a.videos:
        try: analyze(v, a.app, a.fps, gate=a.gate)
        except SystemExit as e: print(v, '->', e)
    if a.merge and len(a.videos) > 1:
        by_slot = {}
        for v in a.videos:
            st = _stats_of(v)                                   # old videos have no stats -> same name in the file = same slot
            key = st.get('name') or re.sub(r'^\d{8}_\d{6}_|(_PARTIAL|_e\d+)+$', '', os.path.basename(v)[:-4])
            by_slot.setdefault(key, []).append(v)
        for slot, vids in by_slot.items():
            if len(vids) < 2: continue
            name = slot
            try: merge(vids, os.path.join(os.path.dirname(os.path.abspath(vids[0])), f'{time.strftime("%Y%m%d_%H%M%S")}_{_fsafe(name, slot)}_merged'), a.app)
            except SystemExit as e: print(name, '->', e)
    if a.videos:
        from .overview import safe_build
        safe_build([a.app] if a.app else None)                 # 總覽 / Excel follow every new analysis
