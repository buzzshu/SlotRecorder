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

BUCKETS = [(0, 0, '沒中'), (0, 1, '贏回不到 1 倍'), (1, 2, '1~2 倍'), (2, 5, '2~5 倍'), (5, 10, '5~10 倍'),
           (10, 20, '10~20 倍'), (20, 50, '20~50 倍'), (50, 100, '50~100 倍'), (100, 1e18, '100 倍以上')]

def spin_stats(wins, bets):
    """RTP / hit rate / win-size distribution of a list of spins (one video, or several videos merged)"""
    wins = np.asarray(wins, float); bets = np.asarray(bets, float); n = len(wins)
    mult = wins / bets; tot_bet = float(bets.sum()); tot_win = float(wins.sum())
    lo, hi = boot_rtp(mult, wins=wins, bets=bets) if len(np.unique(bets)) > 1 else boot_rtp(mult)
    hits = int((wins > 0).sum()); hlo, hhi = wilson(hits, n)
    dist = []
    for lo_, hi_, label in BUCKETS:
        sel = (mult == 0) if hi_ == 0 else ((mult > 0) & (mult < hi_)) if lo_ == 0 else ((mult >= lo_) & (mult < hi_))
        k = int(sel.sum())
        dist.append({'label': label, 'count': k, 'every_n': round(n / k, 1) if k else None, 'rtp_share': float(wins[sel].sum() / tot_bet)})
    return {'spins': n, 'total_bet': tot_bet, 'total_win': tot_win, 'rtp': tot_win / tot_bet, 'rtp_95': [lo, hi],
            'hit_rate': hits / n, 'hit_95': [hlo, hhi], 'hit_every_n': round(n / hits, 2) if hits else None, 'distribution': dist}, mult

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
            if spins and not (n == 0 and in_any(t1, gift_windows)): spins[-1]['win'] += win; spins[-1]['bal'] = B
            else: gifts.append((t1, win))
            win = 0
        for k in range(n):
            spins.append({'t': t1, 'bet': bet, 'win': 0, 'bal': B})
        if win > 0: spins[-1]['win'] += win                 # deduction first, then this new spin's own win
    return spins, gifts, jumps, odd

def analyze(mp4, app_id=None, fps=4, say=print):
    """analyse one video. A <video>.analyzing flag file exists while it runs (a merge waits for it instead of redoing it)."""
    lock = mp4[:-4] + '.analyzing'
    try:
        with open(lock, 'w') as f: f.write(str(os.getpid()))
    except OSError: lock = None
    try: return _analyze(mp4, app_id, fps, say)
    finally:
        if lock:
            try: os.remove(lock)
            except OSError: pass

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
    read_ok = sum(1 for _, v in series if v is not None) / max(1, len(series))
    vals = stable_values(series)
    bet_inf, n_inf = infer_bet(vals)
    bet_ocr = max(set(b for _, b in bet_reads), key=[b for _, b in bet_reads].count) if bet_reads else None
    bet = bet_inf or bet_ocr or stats.get('bet')
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
    n = len(spins)
    if n == 0: raise SystemExit('no spins found')

    wins = np.array([s['win'] for s in spins], float)
    core, mult = spin_stats(wins, [bet] * n)
    tot_bet, tot_win, rtp = core['total_bet'], core['total_win'], core['rtp']; lo, hi = core['rtp_95']
    vid_s = stats.get('video_s') or (series[-1][0] if series else 0)
    off_s = sum(b - a for a, b, _ in (stats.get('timeline') or []))
    play_h = max(1e-6, (vid_s - off_s) / 3600)
    top = sorted(range(n), key=lambda i: -wins[i])[:10]
    summary = {
        'video': os.path.basename(mp4), 'app': prof['name'], 'slot': stats.get('name') or stats.get('slot'),
        'video_length': hms(vid_s), 'play_hours': round(play_h, 3), 'off_game': hms(off_s),
        'bet': bet, 'bet_check': {'from_balance_drops': bet_inf, 'drops_seen': n_inf, 'from_bet_meter': bet_ocr, 'recorder': stats.get('bet')},
        'spins': n, 'spins_per_hour': round(n / play_h), 'total_bet': tot_bet, 'total_win': tot_win,
        'rtp': rtp, 'rtp_95': [lo, hi], 'hit_rate': core['hit_rate'], 'hit_95': core['hit_95'], 'hit_every_n': core['hit_every_n'],
        'net_per_hour': (tot_win - tot_bet) / play_h, 'net_per_hour_in_bets': (tot_win - tot_bet) / bet / play_h,
        'distribution': core['distribution'], 'group': stats.get('group') or '', 'emulator': stats.get('emulator'),
        'top_wins': [{'time': hms(spins[i]['t']), 'win': float(wins[i]), 'x': float(mult[i])} for i in top if wins[i] > 0],
        'gifts': [{'time': hms(t), 'coins': c} for t, c in gifts], 'jumps': [{'time': hms(t), 'coins': c, 'why': w} for t, c, w in jumps],
        'multi_spin_steps': len(odd),
        'balance_first': vals[0][1] if vals else None, 'balance_last': vals[-1][1] if vals else None,
        'meter_read_rate': round(read_ok, 3), 'analysis_seconds': round(time.time() - t_start),
    }
    # sanity: balance change must equal wins - bets + gifts + jumps (all derived from the same series -> checks the bookkeeping)
    summary['balance_check'] = (summary['balance_last'] - summary['balance_first']) - (tot_win - tot_bet + sum(c for _, c in gifts) + sum(c for _, c, _ in jumps))
    with open(base + '_spins.tsv', 'w', encoding='utf-8') as f:
        f.write('spin\ttime\tt_sec\tbet\twin\tx_bet\tbalance_after\n')
        for i, s in enumerate(spins, 1):
            f.write(f"{i}\t{hms(s['t'])}\t{s['t']:.1f}\t{s['bet']}\t{s['win']:.0f}\t{s['win'] / bet:.2f}\t{s['bal']}\n")
    with open(base + '_summary.json', 'w', encoding='utf-8') as f: json.dump(summary, f, indent=1, ensure_ascii=False)
    with open(base + '_summary.html', 'w', encoding='utf-8') as f: f.write(render_html(summary))
    _pdf(base + '_summary.html', say)
    say(f'spins {n}  RTP {rtp:.1%} ({lo and f"{lo:.0%}"}~{hi and f"{hi:.0%}"})  hit 1/{summary["hit_every_n"]}  '
        f'bet {bet:,}  read {read_ok:.0%}  -> {os.path.basename(base)}_summary.html')
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

def render_html(s):
    e = html.escape
    pct = lambda x: '—' if x is None else f'{x:.1%}'
    rows = ''.join(f"<tr><td>{e(d['label'])}</td><td>{d['count']:,}</td><td>{'每 %s 把' % d['every_n'] if d['every_n'] else '—'}</td>"
                   f"<td>{d['rtp_share']:.1%}</td></tr>" for d in s['distribution'])
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
<h3>贏分大小分布（以押注倍數計）</h3>
<table><tr><th>贏分</th><th>次數</th><th>頻率</th><th>佔 RTP</th></tr>{rows}</table>
<h3>前 10 大獎（影片時間，可直接跳去看）</h3>
<table><tr><th>影片時間</th><th>贏分</th><th>押注倍數</th></tr>{tops}</table>
<h3>非贏分的金額變動</h3>
<table><tr><th>影片時間</th><th>金額</th><th>說明</th></tr>{gifts or '<tr><td colspan=3>無</td></tr>'}</table>
<h3>注意事項</h3><ul>{w}<li>贏分＝餘額增加；免費遊戲、特色遊戲的贏分算在觸發的那一把。</li>
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

def load_spins(base):
    """[(time, bet, win)] from <base>_spins.tsv"""
    out = []
    with open(base + '_spins.tsv', encoding='utf-8') as f:
        next(f, None)
        for line in f:
            p = line.rstrip('\n').split('\t')
            if len(p) >= 5: out.append((p[1], float(p[3]), float(p[4])))
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
        'meter_read_rate': min(s['meter_read_rate'] for _, s, _, _ in parts),
    }
    with open(out_base + '_spins.tsv', 'w', encoding='utf-8') as f:
        f.write('spin\tvideo\ttime\tbet\twin\tx_bet\n')
        for i, (vn, t, b, w) in enumerate(allsp, 1): f.write(f'{i}\t{vn}\t{t}\t{b:.0f}\t{w:.0f}\t{w / b:.2f}\n')
    with open(out_base + '_summary.json', 'w', encoding='utf-8') as f: json.dump(summary, f, indent=1, ensure_ascii=False)
    with open(out_base + '_summary.html', 'w', encoding='utf-8') as f: f.write(render_merged_html(summary))
    _pdf(out_base + '_summary.html', say)
    lo, hi = core['rtp_95']
    say(f"MERGED {len(parts)} videos, spins {n}  RTP {core['rtp']:.1%} ({lo and f'{lo:.0%}'}~{hi and f'{hi:.0%}'})  -> {out_base}_summary.html")
    return summary

def render_merged_html(s):
    e = html.escape
    pct = lambda x: '—' if x is None else f'{x:.1%}'
    rows = ''.join(f"<tr><td>{e(d['label'])}</td><td>{d['count']:,}</td><td>{'每 %s 把' % d['every_n'] if d['every_n'] else '—'}</td>"
                   f"<td>{d['rtp_share']:.1%}</td></tr>" for d in s['distribution'])
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
<h3>各支影片</h3>
<table><tr><th>影片</th><th>模擬器</th><th>長度</th><th>局數</th><th>RTP</th><th>押注</th><th>餘額讀取率</th></tr>{vids}</table>
<h3>贏分大小分布（以押注倍數計）</h3>
<table><tr><th>贏分</th><th>次數</th><th>頻率</th><th>佔 RTP</th></tr>{rows}</table>
<h3>前 10 大獎（以押注倍數排序；哪支影片、影片時間）</h3>
<table><tr><th>影片</th><th>影片時間</th><th>贏分</th><th>押注倍數</th></tr>{tops}</table>
<h3>注意事項</h3><ul>{w}<li>單台的 RTP 會因帳號、時段不同而起伏；合併後局數較多，範圍較窄。</li>
<li>每支影片各自的明細見同名的 _summary.html。</li></ul></html>"""

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
            merge(vids, os.path.join(vdir, f'{group}_{name}_merged'), app_id, say)
            out.append(os.path.join(vdir, f'{group}_{name}_merged_summary.html'))
        except SystemExit as e: say(f'{name}: {e}')
        except Exception as e: say(f'{name}: merge failed {e!r}')            # one bad slot must not stop the others
    if not by_slot: say(f'no videos of group {group} in {vdir}')
    return out

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('videos', nargs='*'); ap.add_argument('--app'); ap.add_argument('--fps', type=float, default=4)
    ap.add_argument('--merge', action='store_true', help='videos of the same slot -> also one merged report')
    ap.add_argument('--group', help='merge every video of this multi-emulator run (needs --app)')
    a = ap.parse_args()
    if a.group:
        if not a.app: raise SystemExit('--group needs --app')
        merge_group(a.group, a.app)
        from .overview import safe_build
        safe_build([a.app]); sys.exit(0)
    for v in a.videos:
        try: analyze(v, a.app, a.fps)
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
            try: merge(vids, os.path.join(os.path.dirname(os.path.abspath(vids[0])), f'{time.strftime("%Y%m%d_%H%M%S")}_{name}_merged'), a.app)
            except SystemExit as e: print(name, '->', e)
    if a.videos:
        from .overview import safe_build
        safe_build([a.app] if a.app else None)                 # 總覽 / Excel follow every new analysis
