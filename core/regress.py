"""Regression check for the statistics: did a code change move any number of any already-analysed video?

Every analysed video keeps its balance series (_meter.json) and recorder stats (_stats.json). Replaying the accounting
over those caches needs no mp4 and no OCR (seconds per video), so the whole library doubles as a test set:

    python -m core.regress snapshot     # BEFORE touching the accounting: remember today's numbers
    ... change core/analyze.py ...
    python -m core.regress              # AFTER: list every video whose numbers moved (exit code 1 if any did)

Nothing is written to the videos; the baseline is one local file (logs/regress_baseline.json — logs/ is not in git,
the numbers are your own game data). Options: an app id to limit the run, --videos <dir>, --baseline <file>.
A change that is SUPPOSED to move numbers (a bug fix) shows up here as exactly the videos it affects — read the
list, then `snapshot` again to accept it.
"""
import argparse, glob, json, os, sys, time
from .analyze import _reconstruct, _summarize
from .config import CFG, ROOT, find, base_of

# what must not move by accident. analysis_seconds / analyzer_version are run-dependent and left out on purpose.
KEYS = ('bet', 'spins', 'total_bet', 'total_win', 'rtp', 'hit_rate', 'balance_first', 'balance_last', 'balance_check',
        'meter_read_rate', 'play_hours', 'off_game', 'multi_spin_steps')

def _videos_root():
    vd = CFG.get('video_dir') or 'videos'
    return vd if os.path.isabs(vd) else os.path.join(ROOT, vd)

def numbers(base):
    """key numbers of one cached video, recomputed with the code as it is now. -> dict, or {'error': ...}"""
    try:
        with open(find(base, '_meter.json'), encoding='utf-8') as f: c = json.load(f)
        with open(find(base, '_stats.json'), encoding='utf-8') as f: stats = json.load(f)
        app_id = stats.get('app') or os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(base))))
        with open(os.path.join(ROOT, 'apps', app_id, 'profile.json'), encoding='utf-8') as f: prof = json.load(f)
        series = [tuple(x) for x in c['series']]; quiet = lambda *_: None
        R = _reconstruct(series, [tuple(x) for x in c['bets']], stats, c.get('fps') or 4, say=quiet)
        s, trig = _summarize(base, prof, stats, series, R, None, app_id, time.time(), say=quiet)
    except (SystemExit, Exception) as e:
        return {'error': repr(e)[:160]}
    out = {k: s.get(k) for k in KEYS}
    out['gifts'] = [len(s['gifts']), sum(g['coins'] for g in s['gifts'])]
    out['jumps'] = [len(s['jumps']), sum(j['coins'] for j in s['jumps'])]
    out['features'] = [s['features']['count'], round(sum(x['win'] for x in s['features']['list']), 2)]
    out['distribution'] = [d['count'] for d in s['distribution']]
    return out

def collect(root, app=None, say=print):
    pat = os.path.join(root, app, '**', '*_meter.json') if app else os.path.join(root, '**', '*_meter.json')
    bases = sorted({base_of(m) for m in glob.glob(pat, recursive=True)})
    out = {}
    for b in bases:
        if not os.path.exists(find(b, '_stats.json')): continue
        out[os.path.relpath(b, root).replace(os.sep, '/')] = numbers(b)
    return out

def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try: return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)), abs(float(b)))
        except (TypeError, ValueError): return a == b
    return a == b

def diff(old, new):
    """-> [(video, [(key, before, after), ...])] for videos whose numbers moved, appeared or disappeared"""
    rows = []
    for v in sorted(set(old) | set(new)):
        if v not in new: rows.append((v, [('(video)', 'in baseline', 'gone')])); continue
        if v not in old: continue                                   # recorded after the snapshot -> nothing to compare
        ch = [(k, old[v].get(k), new[v].get(k)) for k in sorted(set(old[v]) | set(new[v])) if not _same(old[v].get(k), new[v].get(k))]
        if ch: rows.append((v, ch))
    return rows

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('cmd', nargs='?', default='check', choices=['check', 'snapshot'])
    ap.add_argument('app', nargs='?', help='only this app id')
    ap.add_argument('--videos', help='videos folder (default: the configured one)')
    ap.add_argument('--baseline', help='baseline file (default: logs/regress_baseline.json)')
    a = ap.parse_args(argv)
    root = a.videos or _videos_root()
    bl = a.baseline or os.path.join(ROOT, 'logs', 'regress_baseline.json')
    t0 = time.time(); now = collect(root, a.app)
    errs = sum(1 for v in now.values() if 'error' in v)
    print(f'{len(now)} 支已分析影片重算完成（{time.time() - t0:.0f} 秒；其中 {errs} 支本來就算不出來）')
    if a.cmd == 'snapshot':
        os.makedirs(os.path.dirname(bl), exist_ok=True)
        if a.app and os.path.exists(bl):                            # one app only -> keep the other apps' baseline
            with open(bl, encoding='utf-8') as f: keep = {k: v for k, v in json.load(f).items() if k.split('/')[0] != a.app}
            now = {**keep, **now}
        with open(bl, 'w', encoding='utf-8') as f: json.dump(now, f, indent=0, ensure_ascii=False)
        print(f'基準已存：{bl}'); return 0
    if not os.path.exists(bl):
        print(f'還沒有基準（{bl}）。先在改程式之前跑：python -m core.regress snapshot'); return 2
    with open(bl, encoding='utf-8') as f: old = json.load(f)
    if a.app: old = {k: v for k, v in old.items() if k.split('/')[0] == a.app}
    rows = diff(old, now); new_n = len(set(now) - set(old))
    if not rows:
        print(f'✅ 沒有任何數字變動（比對 {len(set(old) & set(now))} 支' + (f'，另有 {new_n} 支是基準之後才錄的' if new_n else '') + '）'); return 0
    print(f'⚠ {len(rows)} 支影片的數字變了：')
    for v, ch in rows:
        print('  ' + v)
        for k, o, n in ch: print(f'      {k}: {o} → {n}')
    print('這些變動如果是預期的（修 bug），再跑一次 snapshot 接受它。'); return 1

if __name__ == '__main__':
    sys.exit(main())
