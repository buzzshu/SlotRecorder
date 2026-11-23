"""Delete original .mp4 recordings that have already been analysed and are older than N days.
Everything small stays (report / stats / timeline / spins / snaps / feature clips); only the big source video goes.
A recording is safe to purge when its <base>_summary.json exists and is newer than the .mp4 (= analysis finished
on this video) and no analysis is running on it right now. Manual button first; auto purge can call purgeable()/purge() later.
"""
import os, time

def purgeable(vdir, days):
    """-> [(mp4_path, size_bytes, age_days)] for analysed recordings older than `days`, newest-first by nothing (caller sorts)."""
    out = []
    cutoff = time.time() - days * 86400
    try: names = os.listdir(vdir)
    except OSError: return out
    for f in names:
        if not f.endswith('.mp4') or f.endswith('_clean.mp4'): continue
        p = os.path.join(vdir, f); base = p[:-4]
        try:
            if os.path.exists(base + '.analyzing'): continue           # analysis running right now -> keep
            sj = base + '_summary.json'
            if not os.path.exists(sj): continue                        # never analysed -> keep
            if os.path.getmtime(sj) < os.path.getmtime(p): continue    # summary older than the video -> stale analysis, keep
            if os.path.getmtime(p) > cutoff: continue                  # too new
            out.append((p, os.path.getsize(p), (time.time() - os.path.getmtime(p)) / 86400))
        except OSError: continue
    return out

def purge(paths, say=print):
    """delete the given .mp4 files -> (n_deleted, bytes_freed)"""
    n = freed = 0
    for p in paths:
        try:
            sz = os.path.getsize(p); os.remove(p); freed += sz; n += 1
            say(f'刪除 {os.path.basename(p)}（{sz / 1e9:.2f} GB）')
        except OSError as e: say(f'刪不掉 {os.path.basename(p)}：{e!r}')
    return n, freed
