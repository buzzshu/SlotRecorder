"""Live statistics while recording: the balance meter is read before every spin tap (~every 2 s), and the same
spin / win bookkeeping as core.analyze runs on those readings once a minute.

  - GUI shows spins / RTP / 95% range / features of the recording in progress (log line 'LIVE ...')
  - 夠準就停 (SLOTREC_PRECISE=1): stop when the RTP 95% range of this game is narrow enough, counting
      earlier analysed videos of the game + every emulator of the same multi-emulator run (logs/live/*.json)
The final numbers still come from core.analyze on the video (4 readings / s, catches every roll-up).
"""
import glob, json, os, time
import numpy as np
from .config import CFG, ROOT, video_dir, find, base_of
from .analyze import stable_values, clean_jumps, build_spins, boot_rtp, find_features, load_spins

LIVE_DIR = os.path.join(ROOT, 'logs', 'live')
MIN_SPINS = 2000                      # never call a game "precise" on fewer spins (heavy tails: big wins are rare)
STREAK = 3                            # ... and only after the target held on this many consecutive checks
READ_EVERY_S = 1.0                    # balance read before every spin tap (loop ~1.9 s; sparser reads lose spins)
GIFT = ('CLAIM', 'GREAT', 'WHEEL', 'GIFT', 'BONUS', 'REWARD')

def despike(series, bet):
    """1) readings of a different magnitude than the balance (LL's top bar alternates with a '395' counter) are dropped;
    2) a single reading far from both (plausible) neighbours while they agree = an OCR misread (one wrong digit)"""
    vals = [v for _, v in series if v is not None]
    if vals:
        med = float(np.median(vals))
        series = [(t, v if v is None or med / 5 <= v <= med * 5 else None) for t, v in series]
    out = [(t, v) for t, v in series if v is not None] + [(t, v) for t, v in series if v is None]
    out = sorted(out, key=lambda x: x[0])
    keep = [(t, v) for t, v in out if v is not None]
    bad = set()
    for i in range(1, len(keep) - 1):                         # neighbours = previous / next readable value
        a, v, b = keep[i - 1][1], keep[i][1], keep[i + 1][1]
        if abs(a - b) <= 2 * bet and abs(v - a) > 5 * bet and abs(v - b) > 5 * bet: bad.add(keep[i][0])
    return [(t, None if t in bad else v) for t, v in out]

def half_width(mult):
    lo, hi = boot_rtp(mult, n=500)
    return None if lo is None else (hi - lo) / 2

def already_precise(app_id, slot_id, group=''):
    """earlier analysed videos alone are enough for 夠準就停? -> (bool, spins, half)"""
    m = Live(app_id, slot_id, group, idx=-1, write=False).prior
    h = half_width(m) if m else None
    pp = float(CFG.get('precise_pp') or 10) / 100
    return bool(h is not None and len(m) >= MIN_SPINS and h <= pp), len(m), h

class Live:
    def __init__(self, app_id, slot_id, group='', idx=0, write=True):
        self.app_id, self.slot, self.group, self.idx = app_id, slot_id, group, idx
        self.series = []; self.bounds = []; self.last = {}; self.streak = 0; self.write = write
        os.makedirs(LIVE_DIR, exist_ok=True)
        for p in glob.glob(os.path.join(LIVE_DIR, '*.json')):            # files of runs older than 2 days
            try:
                if time.time() - os.path.getmtime(p) > 2 * 86400: os.remove(p)
            except OSError: pass
        self.file = os.path.join(LIVE_DIR, f'{app_id}__{slot_id}__{group or "solo" + str(os.getpid())}__e{idx}.json')
        self.prior = self._prior()

    def _prior(self):
        """win/bet multiples of this game from videos analysed before (not the ones of this same run). Videos are
        matched to the game like the overview does (stats.json slot id, else the name in the file name)."""
        import re
        from .overview import _slots, _norm
        by_name = {_norm(r['name']): r['id'] for r in _slots(self.app_id)}
        out = []; vdir = video_dir(self.app_id)
        for js in glob.glob(os.path.join(vdir, '**', '*_summary.json'), recursive=True):
            if js.endswith('_merged_summary.json'): continue
            base = base_of(js); st = {}
            if os.path.exists(find(base, '_stats.json')):
                try:
                    with open(find(base, '_stats.json'), encoding='utf-8') as f: st = json.load(f)
                except (OSError, ValueError): pass
            fname = re.sub(r'^\d{8}_\d{6}_|(_PARTIAL|_e\d+)+$', '', os.path.basename(base))
            if (st.get('slot') or by_name.get(_norm(fname)) or fname) != self.slot: continue
            if self.group and st.get('group') == self.group: continue
            if os.path.exists(find(base, '_spins.tsv')): out += [w / b for _, b, w in load_spins(base) if b]
        return out

    def add(self, t, v): self.series.append((t, v))
    def boundary(self, t): self.bounds.append(t)                       # account reset: balance jumps, not a win

    def update(self, bet, taps=(), off=()):
        """recompute from all readings so far -> dict(spins, rtp, lo, hi, feats, total, half)"""
        if not bet or len(self.series) < 10: return None
        gw = [(t - 2, t + 12) for t, kind, _ in taps if kind in ('x', 'btn', 'ocr')]             # coins while closing popups
        gwf = [(t - 2, t + 12) for t, kind, lab in taps if kind == 'x' or (kind in ('btn', 'ocr') and any(g in str(lab).upper() for g in GIFT))]
        series = despike(self.series, bet); spins = []; jumps = []
        cuts = [-1e9] + sorted(self.bounds) + [1e18]
        for a, b in zip(cuts, cuts[1:]):                       # account resets: clean each part on its own
            part = [(t, v) for t, v in series if a <= t < b]
            if len(part) < 5: continue
            rate = len(part) / max(1.0, part[-1][0] - part[0][0])
            vals = clean_jumps(stable_values(part, 1), bet, rate)    # sparse readings: every reading counts (need=2 drops spins)
            sp, _, jp, _ = build_spins(part, vals, bet, gw, [])
            spins += sp; jumps += jp
        n = len(spins)
        if not n: return None
        mult = [s['win'] / bet for s in spins]
        feats = find_features(spins, bet, list(off) + gwf, series=series, strict=[(t - 1, t + 1) for t, _, _ in jumps])
        lo, hi = boot_rtp(mult, n=500)
        r = {'spins': n, 'rtp': float(np.mean(mult)), 'lo': lo, 'hi': hi, 'feats': len(feats), 'reads': len(self.series)}
        if self.write:
            try:
                tmp = self.file + '.tmp'
                with open(tmp, 'w') as f: json.dump({'mult': [round(m, 4) for m in mult], 'updated': time.time(), 'series': self.series}, f)
                os.replace(tmp, self.file)                     # other emulators never read a half-written file
            except OSError: pass
        # whole game: earlier videos + every emulator of this run
        allm = list(self.prior); complete = True
        for p in glob.glob(os.path.join(LIVE_DIR, f'{self.app_id}__{self.slot}__{self.group}__e*.json')) if self.group else [self.file]:
            try:
                with open(p) as f: allm += json.load(f)['mult']
            except (OSError, ValueError, KeyError): complete = False         # do not decide on partial data
        h = half_width(allm)
        r.update(total=len(allm), total_rtp=float(np.mean(allm)) if allm else None, half=h)
        pp = float(CFG.get('precise_pp') or 10) / 100
        ok = complete and h is not None and len(allm) >= MIN_SPINS and h <= pp
        self.streak = self.streak + 1 if ok else 0
        self.last = r
        return r

    def precise(self):
        """target reached? (CFG precise_pp = allowed +- percentage points of the RTP 95% range)"""
        return self.streak >= STREAK

    def close(self):
        if not self.group and self.write:
            try: os.remove(self.file)
            except OSError: pass
