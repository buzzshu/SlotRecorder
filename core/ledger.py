"""Persistent feature-win ledger: accumulate every analysed video's feature wins into one growing
store per game, so a distribution report can be produced at any time across many sessions / days / machines.

WHY: analyze writes one <video>_summary.json per video; a merge only combines the videos you still
have on disk in a single run. Collecting 5 h today and 5 h tomorrow, deleting old recordings, or
recording on several PCs then left you unable to pool the numbers. This ledger fixes that: analyze
appends each video's features here (keyed by video id -> re-analysing the same video never double counts),
and `report` reads the WHOLE ledger whenever you ask.

STORE  <ROOT>/stats/            (small plain TSV -> git-mergeable: commit stats/ to sync/back up across PCs)
  <slug>.videos.tsv    one row per analysed video (the denominator: spins, total_bet, trust)
  <slug>.features.tsv  one row per feature trigger (the sample: win, x, duration)

A feature's `win` is the WHOLE feature's total payout (balance stays frozen through the feature and the
full win lands on the trigger spin -> see analyze.py and handoff.py). `trustworthy` is per video: the
meter read rate was decent and analyze's balance bookkeeping closed (|balance_check| <= one bet). An
untrusted video's feature wins may be off; report filters them out by default but keeps the count.

CLI
  python -m core.ledger add <summary.json|video.mp4|base>...   # idempotent; analyze calls this automatically
  python -m core.ledger backfill [videos_dir]                  # add every existing _summary.json under a folder
  python -m core.ledger list                                   # games in the ledger + how many features each
  python -m core.ledger report <game> [--pdf] [--with-untrusted]
"""
import json, os, re, sys, time, statistics as st

try:
    from .config import ROOT, CFG
except ImportError:                                            # run as a plain script
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    CFG = {}

STATS = os.path.join(ROOT, 'stats')
VID_COLS = ['video_id', 'date', 'app', 'slot', 'bet', 'spins', 'total_bet', 'play_hours',
            'features', 'meter_read_rate', 'balance_check', 'trustworthy']
FEAT_COLS = ['video_id', 'date', 'bet', 'spin', 'seconds', 'win', 'x', 'trustworthy']
# feature win as a multiple of the bet; last bucket is open-ended
BUCKETS = [(0, 5), (5, 10), (10, 20), (20, 30), (30, 50), (50, 100), (100, 1e18)]
BUCKET_LABEL = ['0-5x', '5-10x', '10-20x', '20-30x', '30-50x', '50-100x', '100x+']


def _slug(name, slot=None):
    for s in (name, slot):
        s = re.sub(r'[^0-9A-Za-z]+', '_', str(s or '')).strip('_')
        if s:
            return s
    return 'game'


def _date_of(video_id):
    m = re.match(r'(\d{4})(\d{2})(\d{2})_\d{6}', str(video_id))
    return f'{m.group(1)}-{m.group(2)}-{m.group(3)}' if m else ''


def _base_of(path):
    """<base> from a summary.json / mp4 / base path"""
    p = str(path)
    for suf in ('_summary.json', '_handoff.json', '_spins.tsv'):
        if p.endswith(suf):
            return p[:-len(suf)]
    if p.lower().endswith('.mp4'):
        return p[:-4]
    return p


class _Lock:
    """cheap cross-process lock so parallel per-emulator analyses don't interleave appends to one game file"""
    def __init__(self, slug):
        self.path = os.path.join(STATS, slug + '.lock')
    def __enter__(self):
        for _ in range(200):                                   # ~20 s worst case, then proceed anyway
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except FileExistsError:
                time.sleep(0.1)
        return self
    def __exit__(self, *a):
        try: os.remove(self.path)
        except OSError: pass


def _read_tsv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        head = f.readline().rstrip('\n').split('\t')
        rows = []
        for line in f:
            p = line.rstrip('\n').split('\t')
            if len(p) == len(head):
                rows.append(dict(zip(head, p)))
        return rows


def _append_tsv(path, cols, rows):
    new = not os.path.exists(path)
    with open(path, 'a', encoding='utf-8') as f:
        if new:
            f.write('\t'.join(cols) + '\n')
        for r in rows:
            f.write('\t'.join('' if r.get(c) is None else str(r[c]) for c in cols) + '\n')


def add(path, say=print, force=False):
    """add one analysed video's features to its game ledger. Idempotent by video id. -> number of features added, or None."""
    base = _base_of(path)
    sj = base + '_summary.json'
    if not os.path.exists(sj):
        return None
    with open(sj, encoding='utf-8') as f:
        s = json.load(f)
    if s.get('merged'):
        return None                                            # a merged report is not a new video; its parts are already in
    video_id = os.path.basename(base)
    slot = s.get('slot') or ''
    slug = _slug(slot)
    os.makedirs(STATS, exist_ok=True)
    vpath = os.path.join(STATS, slug + '.videos.tsv')
    fpath = os.path.join(STATS, slug + '.features.tsv')
    date = _date_of(video_id)
    bet = s.get('bet')
    bchk = s.get('balance_check')
    read_rate = s.get('meter_read_rate')
    trust = (read_rate is None or float(read_rate) >= 0.6) and (bchk is None or bet is None or abs(float(bchk)) <= float(bet) + 1e-6)
    feats = (s.get('features') or {}).get('list') or []
    with _Lock(slug):
        have = {r['video_id'] for r in _read_tsv(vpath)}
        if video_id in have and not force:
            return 0                                           # already counted
        if video_id in have and force:                         # rewrite without this video, then re-add
            _rewrite_without(vpath, VID_COLS, video_id)
            _rewrite_without(fpath, FEAT_COLS, video_id)
        _append_tsv(vpath, VID_COLS, [{
            'video_id': video_id, 'date': date, 'app': s.get('app'), 'slot': slot, 'bet': bet,
            'spins': s.get('spins'), 'total_bet': s.get('total_bet'), 'play_hours': s.get('play_hours'),
            'features': len(feats), 'meter_read_rate': read_rate, 'balance_check': bchk,
            'trustworthy': int(bool(trust))}])
        frows = []
        for fe in feats:
            w, x = fe.get('win'), fe.get('x')
            if x is None and w is not None and bet:
                x = w / bet
            frows.append({'video_id': video_id, 'date': date, 'bet': bet, 'spin': fe.get('spin'),
                          'seconds': fe.get('seconds'), 'win': w, 'x': x, 'trustworthy': int(bool(trust))})
        _append_tsv(fpath, FEAT_COLS, frows)
    say(f'ledger: +{len(feats)} feature(s) from {video_id} -> stats/{slug}  ({"trusted" if trust else "UNTRUSTED balance"})')
    return len(feats)


def _rewrite_without(path, cols, video_id):
    rows = [r for r in _read_tsv(path) if r.get('video_id') != video_id]
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\t'.join(cols) + '\n')
        for r in rows:
            f.write('\t'.join('' if r.get(c) in (None, '') else str(r[c]) for c in cols) + '\n')


def backfill(vdir=None, say=print):
    vdir = vdir or (CFG.get('video_dir') or os.path.join(ROOT, 'videos'))
    if not os.path.isabs(vdir):
        vdir = os.path.join(ROOT, vdir)
    n_vid = n_feat = 0
    for dp, _dn, fns in os.walk(vdir):
        for fn in sorted(fns):
            if fn.endswith('_summary.json') and '_merged' not in fn:
                r = add(os.path.join(dp, fn), say=lambda *a: None)
                if r:
                    n_vid += 1; n_feat += r
    say(f'backfill: {n_vid} new video(s), {n_feat} feature(s) from {vdir}')
    return n_vid, n_feat


def games(say=print):
    if not os.path.isdir(STATS):
        say('(ledger empty)'); return []
    out = []
    for fn in sorted(os.listdir(STATS)):
        if fn.endswith('.features.tsv'):
            slug = fn[:-len('.features.tsv')]
            feats = _read_tsv(os.path.join(STATS, fn))
            vids = _read_tsv(os.path.join(STATS, slug + '.videos.tsv'))
            spins = sum(float(v['spins']) for v in vids if v.get('spins'))
            out.append((slug, len(feats), len(vids), int(spins)))
    for slug, nf, nv, ns in out:
        say(f'  {slug:<30} {nf:>5} features  {nv:>3} videos  {ns:,} spins')
    if not out:
        say('(ledger empty)')
    return out


# ---------------- report ----------------

def _find_slug(query):
    if not os.path.isdir(STATS):
        return None
    slugs = [fn[:-len('.features.tsv')] for fn in os.listdir(STATS) if fn.endswith('.features.tsv')]
    q = re.sub(r'[^0-9A-Za-z]+', '', query).lower()
    exact = [s for s in slugs if s.lower() == q]
    if exact:
        return exact[0]
    part = [s for s in slugs if q in s.lower()]
    return part[0] if len(part) == 1 else (sorted(part, key=len)[0] if part else None)


def compute(slug, include_untrusted=False):
    feats = _read_tsv(os.path.join(STATS, slug + '.features.tsv'))
    vids = _read_tsv(os.path.join(STATS, slug + '.videos.tsv'))
    use = [f for f in feats if include_untrusted or f.get('trustworthy') == '1']
    xs = sorted(float(f['x']) for f in use if f.get('x') not in (None, ''))
    wins = [float(f['win']) for f in use if f.get('win') not in (None, '')]
    spins = sum(float(v['spins']) for v in vids if v.get('spins'))
    play_h = sum(float(v['play_hours']) for v in vids if v.get('play_hours'))
    tot_bet = sum(float(v['total_bet']) for v in vids if v.get('total_bet'))
    n = len(xs)
    dist = []
    for (lo, hi), lab in zip(BUCKETS, BUCKET_LABEL):
        c = sum(1 for x in xs if lo <= x < hi)
        dist.append({'range': lab, 'count': c, 'prob': c / n if n else 0})
    q = {}
    if n >= 4:
        qs = st.quantiles(xs, n=100)
        q = {'p25': qs[24], 'p50': st.median(xs), 'p75': qs[74], 'p90': qs[89], 'p99': qs[98]}
    elif n:
        q = {'p50': st.median(xs)}
    # per-day accumulation
    by_date = {}
    for v in vids:
        d = v.get('date') or '?'
        by_date.setdefault(d, [0, 0.0])[1] += float(v.get('spins') or 0)
    for f in use:
        by_date.setdefault(f.get('date') or '?', [0, 0.0])[0] += 1
    timeline = []
    cum_f = cum_s = 0
    for d in sorted(by_date):
        nf, ns = by_date[d]; cum_f += nf; cum_s += ns
        timeline.append({'date': d, 'features': nf, 'spins': int(ns), 'cum_features': cum_f, 'cum_spins': int(cum_s)})
    return {
        'slug': slug, 'n': n, 'spins': int(spins), 'play_hours': round(play_h, 1), 'videos': len(vids),
        'total_bet': tot_bet, 'include_untrusted': include_untrusted,
        'untrusted_features': sum(1 for f in feats if f.get('trustworthy') != '1'),
        'trigger_every': (spins / n) if n else None,
        'mean_x': st.mean(xs) if xs else None, 'min_x': xs[0] if xs else None, 'max_x': xs[-1] if xs else None,
        'median_x': st.median(xs) if xs else None, 'quant': q,
        'rtp_share': (sum(wins) / tot_bet) if tot_bet else None,
        'ge50': sum(1 for x in xs if x >= 50), 'ge100': sum(1 for x in xs if x >= 100),
        'dist': dist, 'timeline': timeline,
        'slot_name': (vids[-1].get('slot') if vids else slug),
    }


def _bar(p, w=220):
    return f'<div style="background:#4c7ef3;height:14px;width:{max(1, round(p * w))}px;border-radius:3px"></div>'


def render_html(r):
    e = lambda x: ('%s' % x)
    name = re.sub(r'[_]+', ' ', str(r['slot_name'] or r['slug'])).strip()
    maxp = max((d['prob'] for d in r['dist']), default=0) or 1
    rows = ''.join(
        f"<tr><td>{d['range']}</td><td style='text-align:right'>{d['count']:,}</td>"
        f"<td style='text-align:right'>{d['prob']*100:.1f}%</td>"
        f"<td>{_bar(d['prob']/maxp)}</td></tr>" for d in r['dist'])
    tl = ''.join(
        f"<tr><td>{t['date']}</td><td style='text-align:right'>{t['spins']:,}</td>"
        f"<td style='text-align:right'>{t['features']}</td>"
        f"<td style='text-align:right'>{t['cum_spins']:,}</td>"
        f"<td style='text-align:right'>{t['cum_features']}</td></tr>" for t in r['timeline'])
    q = r['quant']
    qtxt = ' · '.join(f"{k} {v:,.1f}x" for k, v in q.items()) if q else '—'
    trig = f"每 {r['trigger_every']:,.0f} 局觸發一次" if r['trigger_every'] else '—'
    ge50 = f"每 {r['n']/r['ge50']:,.1f} 次特色有一次 ≥50x" if r['ge50'] else '樣本內未出現 ≥50x'
    ge100 = f"每 {r['n']/r['ge100']:,.1f} 次特色有一次 ≥100x" if r['ge100'] else '樣本內未出現 ≥100x'
    warn = (f"<p class='warn'>⚠ 另有 {r['untrusted_features']} 次特色來自餘額讀取不可靠的影片，"
            f"已排除在分布外（可加 --with-untrusted 納入）。</p>" if r['untrusted_features'] and not r['include_untrusted'] else '')
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<title>{e(name)} 特色贏分分布</title><style>
  body{{font-family:"Microsoft JhengHei",system-ui,sans-serif;max-width:820px;margin:24px auto;padding:0 16px;color:#1b1b1b;line-height:1.6}}
  h1{{font-size:22px}} h2{{font-size:17px;margin-top:28px;border-left:4px solid #4c7ef3;padding-left:8px}}
  table{{border-collapse:collapse;width:100%;margin:8px 0;page-break-inside:avoid}}
  th,td{{border:1px solid #ddd;padding:6px 10px;font-size:14px}} th{{background:#f3f6ff;text-align:left}}
  .tiles{{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0}}
  .tile{{flex:1 1 150px;background:#f7f9ff;border:1px solid #e3e9f7;border-radius:8px;padding:10px 12px}}
  .tile b{{display:block;font-size:20px;color:#2a53c0}} .tile span{{font-size:12px;color:#667}}
  .warn{{background:#fff6e5;border:1px solid #ffd27a;border-radius:6px;padding:8px 12px;font-size:13px}}
  .note{{color:#667;font-size:12px}}
</style></head><body>
<h1>{e(name)} — 特色遊戲贏分分布</h1>
<p class="note">累積自 {r['videos']} 支影片 · {r['play_hours']} 小時 · {r['spins']:,} 局 · 產生時間 {time.strftime('%Y-%m-%d %H:%M')}</p>
{warn}
<div class="tiles">
  <div class="tile"><b>{r['n']:,}</b><span>特色樣本數</span></div>
  <div class="tile"><b>{r['median_x']:,.1f}x</b><span>中位數贏分（倍押注）</span></div>
  <div class="tile"><b>{r['mean_x']:,.1f}x</b><span>平均贏分</span></div>
  <div class="tile"><b>{r['max_x']:,.0f}x</b><span>最大一次</span></div>
</div>
<h2>贏分區間機率（以押注倍數分桶）</h2>
<table><tr><th>區間</th><th>次數</th><th>機率</th><th></th></tr>{rows}</table>
<p class="note">分位數：{qtxt}　|　平均 {r['mean_x']:,.1f}x</p>

<h2>白話體感</h2>
<table>
<tr><th>指標</th><th>數值</th></tr>
<tr><td>特色觸發頻率</td><td>{trig}</td></tr>
<tr><td>中到大獎（≥50x）的機會</td><td>{ge50}</td></tr>
<tr><td>中到巨獎（≥100x）的機會</td><td>{ge100}</td></tr>
<tr><td>特色佔整體回收（RTP）</td><td>{(r['rtp_share']*100):.1f}%{'' if r['rtp_share'] is not None else '—'}</td></tr>
</table>

<h2>累積進度（跨天 / 跨場次）</h2>
<table><tr><th>日期</th><th>當日局數</th><th>當日特色</th><th>累積局數</th><th>累積特色</th></tr>{tl}</table>
<p class="note">樣本越多，長尾（高倍區間）越穩；此表可看出每次收集後分布如何變化。</p>
</body></html>"""


def report(query, pdf=False, include_untrusted=False, say=print):
    slug = _find_slug(query)
    if not slug:
        say(f'no game matching "{query}" in the ledger. Try: python -m core.ledger list'); return None
    r = compute(slug, include_untrusted)
    if not r['n']:
        say(f'{slug}: no features yet'); return None
    os.makedirs(STATS, exist_ok=True)
    out = os.path.join(STATS, slug + '_feature_report.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(render_html(r))
    say(f'report: {r["n"]} features, trigger ~1/{r["trigger_every"]:.0f} spins, median {r["median_x"]:.1f}x -> {out}')
    if pdf:
        try:
            from .overview import to_pdf
            to_pdf(out, say)
        except Exception as ex:
            say(f'PDF not made: {ex!r}')
    return out


if __name__ == '__main__':
    args = sys.argv[1:]
    cmd = args[0] if args else 'list'
    rest = args[1:]
    if cmd == 'add':
        for p in rest:
            add(p)
    elif cmd == 'backfill':
        backfill(rest[0] if rest else None)
    elif cmd == 'list':
        games()
    elif cmd == 'report':
        q = [a for a in rest if not a.startswith('--')]
        report(q[0] if q else '', pdf='--pdf' in rest, include_untrusted='--with-untrusted' in rest)
    else:
        print(__doc__)
