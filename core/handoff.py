"""Per-spin handoff sidecar for a downstream board-symbol recognizer (slot-video-recog / te-ldplayer).

analyze reads the BALANCE meter and never looks at the reels; a recognizer reads the reels but
has to re-derive the spin boundaries from frame diffs -- the fragile step that invents phantom spins
when a 30 fps capture repeats frames of a 20 fps game (te-ldplayer: 825 of 1090 recorded spins were
fake without --min-still, plus a LAG_CLUSTER pass). This file hands the recognizer what analyze already
knows for free:

  <video>_handoff.json  schema slotrec-handoff/1
    spins[]  one row per PAID spin (balance dropped by the bet): i, t, t_next, bet, win, balance_after, feature
             -> the recognizer looks for the settled board only inside [t, t_next); the spin COUNT is fixed
                by the balance, so repeat-frame / lag false-stops cannot add a spin, and the recognizer's own
                "spins == independent count" gate is met by construction.
    win / balance_after  balance-derived, checked by analyze (balance_check ~= 0) -> the recognizer's
             board x paytable reconciliation has a ground-truth answer without its own TOTAL WIN OCR.
    features[]  trigger spin i + [t0, t1]: the long window is ONE feature episode, not base spins.
    off_game[]  spans with no paid spin (popups / lobby / stuck) -> no window there, skipped for free.

LIMITS (state them, don't paper over):
  - Free games / Hold&Spin deduct no bet, so they do NOT appear as separate spins: a feature is ONE window
    (the trigger spin) spanning the whole animation. Per-free-spin boards stay the recognizer's own job.
  - Fast 0.25 s tapping outruns the 4 fps meter: a burst of spins collapses onto one t_sec (a zero-width window).
    Those carry resolvable=false -- count them (bet/win known) but they have no individually captured board to read.
    resolvable_spins (< spins_count on fast footage) is how many DO have a readable per-spin board.
  - balance_trustworthy=false (|balance_check| > a bet, or a shared account polluting the meter, or a low
    read rate) -> use the WINDOWS but not the win / balance_after values.
  - t has the analyze sampling granularity (fps_hint, ~0.25 s at fps 4) and marks the balance transition,
    ~1-2 s after the reels start; search [t - margin, t_next) for the settle. Times reference THIS exact
    video file (see "video"); feed the recognizer the same file, not a different cut.
"""
import json, os, re

SCHEMA = 'slotrec-handoff/1'


def _hms_to_s(t):
    try:
        p = [float(x) for x in str(t).split(':')]
    except ValueError:
        return None
    while len(p) < 3:
        p.insert(0, 0.0)
    return p[0] * 3600 + p[1] * 60 + p[2]


def _spin_rows(base):
    """[(i, t_sec, bet, win, balance_after, time_hms)] from <base>_spins.tsv"""
    rows = []
    with open(base + '_spins.tsv', encoding='utf-8') as f:
        next(f, None)
        for line in f:
            p = line.rstrip('\n').split('\t')
            if len(p) < 7:
                continue
            rows.append((int(p[0]), float(p[2]), float(p[3]), float(p[4]), _num(p[6]), p[1]))
    return rows


def _num(s):
    try:
        return float(s)
    except ValueError:
        return None


def build(mp4, say=print):
    """write <video>_handoff.json from the sidecars analyze already produced. -> path, or None if not analysable yet."""
    base = mp4[:-4] if mp4.lower().endswith('.mp4') else mp4
    sj, tsv = base + '_summary.json', base + '_spins.tsv'
    if not (os.path.exists(sj) and os.path.exists(tsv)):
        return None
    with open(sj, encoding='utf-8') as f:
        s = json.load(f)
    if s.get('merged'):
        return None                                         # a merged report has no single video timeline
    stats = {}
    if os.path.exists(base + '_stats.json'):
        with open(base + '_stats.json', encoding='utf-8') as f:
            stats = json.load(f)
    rows = _spin_rows(base)
    if not rows:
        return None
    t_by_i = {r[0]: r[1] for r in rows}
    video_s = stats.get('video_s') or _hms_to_s(s.get('video_length')) or 0.0
    video_s = max(float(video_s), rows[-1][1] + 2.0)        # never let the last window collapse to zero width
    bet = s.get('bet')

    # feature trigger spins: prefer the exact spin index analyze now writes; fall back to time-match for old summaries
    # (summary 'time' is hms() = FLOORED to whole seconds, so nearest-t_sec can pick the wrong spin when spins are <1 s apart)
    feats_in = (s.get('features') or {}).get('list') or []
    feat_by_i = {}
    feats_out = []
    used = set()
    for fe in feats_in:
        ft = _hms_to_s(fe.get('time'))
        i = fe.get('spin')
        if i is not None and i not in t_by_i:
            i = None                                        # stale index (edited tsv) -> fall back
        if i is None and ft is not None:                   # old summary without 'spin': nearest unused spin to the floored time
            cand = min((r for r in rows if r[0] not in used), key=lambda r: abs(r[1] - ft), default=None)
            i = cand[0] if cand else None
        if i is None:
            continue
        used.add(i)
        feat_by_i[i] = True
        t0 = t_by_i[i]                                      # exact trigger time (not the floored summary time)
        secs = fe.get('seconds') or 0
        feats_out.append({'i': i, 't0': round(t0, 2), 't1': round(t0 + secs, 2), 'seconds': secs,
                          'win': fe.get('win'), 'x': fe.get('x')})

    # fast 0.25s tapping + 4 fps meter -> a burst of spins collapses onto one t_sec (analyze's multi_spin_steps):
    # those spins have NO individually captured board (they happened between two meter samples). Mark them so the
    # recognizer still COUNTS them (bet/win known from balance) but does not try to read a per-spin board.
    MIN_RES = 0.3
    spins = []
    for k, r in enumerate(rows):
        i, t, b, win, bal, _ = r
        t_next = rows[k + 1][1] if k + 1 < len(rows) else round(video_s, 2)
        spins.append({'i': i, 't': round(t, 2), 't_next': round(float(t_next), 2),
                      'bet': b, 'win': win, 'balance_after': bal, 'feature': bool(feat_by_i.get(i)),
                      'resolvable': bool(float(t_next) - t >= MIN_RES)})
    resolvable_n = sum(1 for sp in spins if sp['resolvable'])

    off = [[round(a, 2), round(b, 2)] for a, b, _info in (stats.get('timeline') or [])]
    bchk = s.get('balance_check')
    read_rate = s.get('meter_read_rate')
    trustworthy = (bchk is not None and bet and abs(bchk) <= bet + 1e-6
                   and (read_rate is None or read_rate >= 0.6))

    ho = {
        'schema': SCHEMA,
        'video': s.get('video') or os.path.basename(mp4),
        'app': s.get('app'), 'slot': s.get('slot'),
        'orientation': 'landscape' if _is_landscape(stats, s) else 'portrait',
        'fps_hint': 4,                                       # analyze samples the meter at 4 fps -> t granularity ~0.25 s
        'bet': bet, 'video_s': round(float(video_s), 2),
        'spins_count': len(spins), 'resolvable_spins': resolvable_n,   # < spins_count on fast footage (multi-spin steps have no individual board)
        'balance': {'first': s.get('balance_first'), 'last': s.get('balance_last'),
                    'check': bchk, 'read_rate': read_rate, 'trustworthy': bool(trustworthy)},
        'clean_file': stats.get('clean_file'), 'clean_cut_s': stats.get('clean_cut_s'),
        'spins': spins, 'features': feats_out, 'off_game': off,
    }
    out = base + '_handoff.json'
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(ho, f, indent=1, ensure_ascii=False)
    nfeat = sum(1 for sp in spins if sp['feature'])
    say(f'handoff: {len(spins)} spins ({nfeat} feature), balance {"ok" if trustworthy else "UNTRUSTED"} '
        f'-> {os.path.basename(out)}')
    return out


def _is_landscape(stats, summary):
    for src in (stats, summary):
        w, h = src.get('width'), src.get('height')
        if w and h:
            return w > h
    return False                                            # portrait by default (most mobile slots)


if __name__ == '__main__':
    import sys
    args = [a for a in sys.argv[1:] if a != '--all']
    if '--all' in sys.argv or not args:
        from .config import CFG, ROOT
        root = CFG.get('video_dir') or 'videos'
        if not os.path.isabs(root):
            root = os.path.join(ROOT, root)
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if fn.endswith('_summary.json') and '_merged' not in fn:
                    mp4 = os.path.join(dp, fn[:-len('_summary.json')] + '.mp4')
                    try:
                        if build(mp4) is None:
                            print('skip (no spins/tsv):', os.path.basename(mp4))
                    except Exception as e:
                        print(os.path.basename(mp4), '->', repr(e))
    else:
        for v in args:
            build(v)
