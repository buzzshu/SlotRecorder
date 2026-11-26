"""Recompute per-video summaries from the cached balance series (_meter.json + _stats.json), WITHOUT the mp4.

Why: the balance accounting (bet, spins, wins, gifts, RTP, win-size distribution) is all derivable from the cached
meter series and the recorder stats -- no re-recording, no re-OCR. So when the accounting logic is fixed (e.g. the gift
magnitude guard in build_spins that stopped feature payouts from being miscounted as top-up gifts), every old video can
be corrected in place by replaying _reconstruct / _summarize over its cache.

What it does NOT do: features are the gap-based candidates without frame confirmation (confirm_features needs the mp4),
so feature-level stats are best-effort; and the per-game merged_summary / overview are regenerated afterwards by the
normal `python -m core.overview` pass (which re-reads these per-video summaries). The overall RTP -- the headline number
-- is exact, because it is anchored to the first/last balance read, not to the frames in between.

Usage:
    python -m core.recompute               # every cached video under the videos dir
    python -m core.recompute 88_fortunes   # only this app's videos
    python -m core.recompute path/to/<base>_meter.json ...   # specific videos
"""
import glob, json, os, sys, time
from .analyze import _reconstruct, _summarize, render_html, write_spins_tsv
from .config import CFG, ROOT, find, outp, base_of


def _videos_root():
    vd = CFG.get('video_dir') or 'videos'
    return vd if os.path.isabs(vd) else os.path.join(ROOT, vd)


def _bases(args):
    """resolve CLI args -> list of base paths (the part before '_meter.json') that have a cached series."""
    root = _videos_root()
    meters = []
    if not args:
        meters = glob.glob(os.path.join(root, '**', '*_meter.json'), recursive=True)
    for a in args:
        if a.endswith('_meter.json') and os.path.exists(a):
            meters.append(a)
        elif os.path.isdir(a):
            meters += glob.glob(os.path.join(a, '**', '*_meter.json'), recursive=True)
        else:                                                   # treat as an app id under the videos root
            meters += glob.glob(os.path.join(root, a, '**', '*_meter.json'), recursive=True)
    return sorted({base_of(m) for m in meters})


def recompute_one(base, say=print):
    """replay the accounting over one cached video; rewrite its _summary.json / _spins.tsv / _summary.html in place.
    -> (old_rtp, new_rtp, summary) or None when the cache is incomplete."""
    cache, statsf = find(base, '_meter.json'), find(base, '_stats.json')
    if not (os.path.exists(cache) and os.path.exists(statsf)):
        say(f'  跳過（缺快取）: {os.path.basename(base)}'); return None
    with open(cache, encoding='utf-8') as f: c = json.load(f)
    with open(statsf, encoding='utf-8') as f: stats = json.load(f)
    series = [tuple(x) for x in c['series']]; bet_reads = [tuple(x) for x in c['bets']]
    fps = c.get('fps') or 4
    ab = os.path.abspath(base)
    app_id = stats.get('app') or os.path.basename(os.path.dirname(os.path.dirname(ab)))   # videos/<app>/<slot>/<base>
    prof = json.load(open(os.path.join(ROOT, 'apps', app_id, 'profile.json'), encoding='utf-8'))

    old_rtp = None
    if os.path.exists(find(base, '_summary.json')):
        try:
            with open(find(base, '_summary.json'), encoding='utf-8') as f: old_rtp = json.load(f).get('rtp')
        except (ValueError, OSError): pass

    t0 = time.time()
    R = _reconstruct(series, bet_reads, stats, fps, say=lambda *_: None)   # quiet: no per-video bet chatter
    summary, trig = _summarize(base, prof, stats, series, R, None, app_id, t0, say=lambda *_: None)
    summary['recomputed_from_cache'] = True                     # mark: features are not frame-confirmed

    write_spins_tsv(outp(base, '_spins.tsv'), R['spins'], R['bet'], trig)   # update in place (flat or bucketed), never create a duplicate
    with open(outp(base, '_summary.json'), 'w', encoding='utf-8') as f: json.dump(summary, f, indent=1, ensure_ascii=False)
    with open(outp(base, '_summary.html'), 'w', encoding='utf-8') as f: f.write(render_html(summary))

    nr = summary['rtp']
    tag = f'{old_rtp:.1%} → {nr:.1%}' if old_rtp is not None else f'{nr:.1%}'
    say(f'  {os.path.basename(base)[:46]:46}  RTP {tag}   spins {summary["spins"]}  read {R["read_ok"]:.0%}  gifts {len(summary["gifts"])}')
    return old_rtp, nr, summary


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    bases = _bases(args)
    if not bases:
        print('沒有找到任何快取 _meter.json（檢查 videos 目錄或傳入 app id）'); return
    print(f'重算 {len(bases)} 支（讀快取,不碰 mp4）:')
    tot_old_w = tot_new_w = tot_bet = 0.0; done = failed = 0
    for base in bases:
        try:                                                    # one bad video (no bet / 0 spins / missing profile) must not abort the batch
            r = recompute_one(base)
        except (SystemExit, Exception) as e:
            failed += 1; print(f'  ✗ 跳過（重算失敗 {e!r}）: {os.path.basename(base)}'); continue
        if not r: continue
        _, _, s = r; done += 1
        tot_new_w += s['total_win']; tot_bet += s['total_bet']
    if failed: print(f'（{failed} 支重算失敗已跳過）')
    print(f'\n完成 {done} 支。重算後整體 RTP（所有重算支彙整）= {tot_new_w / tot_bet:.1%}' if tot_bet else f'\n完成 {done} 支。')
    print('接著跑  python -m core.overview  刷新總覽（會重新彙整這些 summary）。')


if __name__ == '__main__':
    main()
