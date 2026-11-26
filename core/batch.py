"""Record a list of slots one after another on one emulator. Resumable: a job file remembers which slots are done.

usage:  python -m core.batch --app lightning_link --slots bzzf_golden5,other_slot [--job 20261112_1400] [--emu 1]
        re-running with the same --job skips slots already done (resume after a crash / STOP / reboot).
        --emu = LDPlayer instance index (default: config ld_index). Stop: create STOP_<index> (or STOP for all jobs).
        --split N --group G : one of N emulators recording the SAME slots together (GUI 同時用 N 台) -> each records
        minutes/N of every slot; videos carry the group id so core.analyze --group G merges them per slot.
"""
import argparse, json, math, os, subprocess, sys, time

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--app', required=True); ap.add_argument('--slots', required=True)
    ap.add_argument('--job', default=time.strftime('%Y%m%d_%H%M%S')); ap.add_argument('--emu', type=int)
    ap.add_argument('--minutes', type=float, help='override slots.csv minutes (tests)')
    ap.add_argument('--split', type=int, default=1, help='this job is 1 of N emulators sharing the minutes')
    ap.add_argument('--group', default='', help='id shared by the N jobs of one multi-emulator run')
    ap.add_argument('--precise', action='store_true', help='夠準就停: record each slot until its RTP range is narrow enough')
    a = ap.parse_args()
    if a.emu is not None: os.environ['SLOTREC_LD_INDEX'] = str(a.emu)     # before importing config -> children inherit
    from .config import ROOT as _R
    _jp = os.path.join(_R, 'logs', f'job_{a.job}.json')
    if os.path.exists(_jp):                                    # resume: the job may have moved to a new emulator meanwhile
        _j = json.load(open(_jp, encoding='utf-8'))
        if _j.get('emulator') is not None: os.environ['SLOTREC_LD_INDEX'] = str(_j['emulator'])
        a.split = a.split if a.split > 1 else int(_j.get('split') or 1); a.group = a.group or _j.get('group') or ''
        a.precise = a.precise or bool(_j.get('precise'))
    if os.environ.get('SLOTREC_LD_INDEX'): os.environ.setdefault('SLOTREC_STOP_IDX', str(a.emu if a.emu is not None else os.environ['SLOTREC_LD_INDEX']))
    if a.group: os.environ['SLOTREC_GROUP'] = a.group; os.environ['SLOTREC_SPLIT'] = str(max(1, a.split))
    if a.precise: os.environ['SLOTREC_PRECISE'] = '1'
    from .config import ROOT
    LOG_DIR = os.path.join(ROOT, 'logs'); os.makedirs(LOG_DIR, exist_ok=True)
    os.environ.setdefault('SLOTREC_LOG', os.path.join(LOG_DIR, f'job_{a.job}.log'))   # children inherit -> one log file
    from . import device as d
    d._LOGFILE = os.environ['SLOTREC_LOG']
    if os.path.exists(d.stop_file()):
        d.log('STOP file present from an earlier run -> removed'); os.remove(d.stop_file())
    res = {'reason': 'error'}                                # why the job ended -> GUI auto-resume only after a crash / reboot
    try:
        rc = run(a.app, [s for s in a.slots.split(',') if s], a.job, LOG_DIR, a.minutes, max(1, a.split), a.group, res, a.precise)
    except d.StopRequested:
        res['reason'] = 'stopped'; rc = 2
    finally:
        mark_finished(os.path.join(LOG_DIR, f'job_{a.job}.json'), res['reason'])
    sys.exit(rc)

def mark_finished(jp, reason):
    """job json 'finished' = done / stopped / failed / error. A job json WITHOUT it (but with 'pid') whose process is gone
    was cut off by a crash or a reboot -> the GUI offers to resume it."""
    try:
        with open(jp, encoding='utf-8') as f: st = json.load(f)
        st['finished'] = reason; st['finished_at'] = time.strftime('%F %T')
        with open(jp, 'w', encoding='utf-8') as f: json.dump(st, f, indent=1, ensure_ascii=False)
    except (OSError, ValueError): pass

def store_redirect(app, say):
    """a Play-protected app (Google Play integrity / pairip, e.g. 包你發) refuses to run when it was sideloaded instead of
    installed from the Play Store: on first launch it bounces to the Store. -> True (blocked) when that is what happens."""
    from . import device as d
    store = seen_app = 0
    for _ in range(6):
        f = d.focus()
        if f.endswith(app.p['activity']):
            seen_app += 1
            if seen_app >= 2: return False                     # app is running normally -> not redirected; stop waiting
        elif 'com.android.vending' in f: store += 1
        time.sleep(2)
    if store >= 2 and seen_app == 0:
        pkg = app.p['package']
        say(f'✘ {app.p["name"]} 一開啟就被導向 Google Play 商店 -> 這個 app 有 Play 商店驗證（防側載），複製安裝的版本無法開啟。')
        say(f'  請在模擬器 {d.IDX} 登入 Google 帳號，從 Play 商店搜尋並安裝「{app.p["name"]}」後再錄（登入請自行操作，工具不會代填帳密）。')
        d.adb('shell', 'am', 'start', '-a', 'android.intent.action.VIEW', '-d', f'market://details?id={pkg}')   # open its Play page
        return True
    return False

def refresh_cache(app, say):
    """keep apk_cache current automatically: if any running emulator has a NEWER version of the app than the cache,
    re-cache from it (and update version.txt). Then ensure_app upgrades the other emulators to it. Pairs with Play
    auto-update on one 'master' emulator (index 0): Play updates that one, this propagates it to the cache + all台."""
    from . import device as d
    from .config import serial_for
    pkg = app.p['package']
    lock = os.path.join(d.APK_CACHE, f'.refresh_{pkg}.lock')
    try:
        os.makedirs(d.APK_CACHE, exist_ok=True)
        try: fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)   # one refresh at a time (multi-run has N processes)
        except FileExistsError:
            if time.time() - os.path.getmtime(lock) < 600: return      # another emulator is already refreshing -> skip
            os.remove(lock); fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)   # stale lock (crash) -> take over
        try:
            cached = d.cached_version(pkg) or 0
            best_s, best_v = None, cached
            for i, _, run in d.instances():
                if not run: continue
                s = serial_for(i)
                if not d.installed(pkg, serial=s): continue
                v = d.app_version(pkg, serial=s) or 0
                if v > best_v: best_s, best_v = s, v
            if best_s:
                say(f'偵測到較新的 {app.p["name"]}（版本 {best_v} > 快取 {cached}）-> 更新打包用的安裝檔')
                d.cache_apk(pkg, best_s, say, force=True)
        finally:
            os.close(fd);
            try: os.remove(lock)
            except OSError: pass
    except Exception as e: say('refresh_cache skipped', repr(e))    # never let a cache refresh stop a recording

def ensure_app(app, say):
    """a fresh emulator (GUI multi-run creates SR_ ones) has no app yet -> install it from apk_cache (bundled with the tool,
    or filled from any running emulator that has the app). Then first start: new-account screens + long downloads.
    -> True when the app is installed (and, if it was just installed, in its lobby)"""
    from . import device as d
    from .config import serial_for
    pkg = app.p['package']
    if any(d.installed(pkg) or time.sleep(3) for _ in range(3)):                 # 3 tries: one adb hiccup != not installed
        cv, iv = d.cached_version(pkg), d.app_version(pkg)                       # already installed: is it too old to run?
        if cv and iv and cv > iv:                                               # a stale pre-installed app gets stuck on 「請更新」
            say(f'✘ {app.p["name"]} 已安裝版本（{iv}）比打包的（{cv}）舊 -> 自動升級，避免卡在「請更新」畫面')
            if d.install_cached(pkg, d.SER, say): app.restart(wait=60)          # -r keeps the account data
            else: say(f'  自動升級失敗（簽章不符或空間不足）-> 請在模擬器 {d.IDX} 從 Play 商店手動更新 {app.p["name"]}')
        return True
    say(f'{app.p["name"]} is not installed on emulator {d.IDX} -> installing')
    if not d.apk_cached(pkg):
        t0 = time.time()
        while not d.apk_cached(pkg):
            others = [serial_for(i) for i, _, run in d.instances() if run and i != d.IDX]
            src = next((s for s in others if d.installed(pkg, serial=s)), None)
            if src and d.cache_apk(pkg, src, say): break
            if not others:                                     # fresh PC: no running emulator can ever become a source
                say(f'✘ 這台電腦沒有「{app.p["name"]}」的安裝檔（apk_cache 沒有打包這個 app），也沒有其他已開機、已裝此 app 的模擬器可複製。')
                say( '  解法：把含安裝檔的 apk_cache 資料夾放進 SlotRecorder，或在這台模擬器登入 Google Play 手動安裝後再試。')
                return False
            if time.time() - t0 > 300: say(f'✘ 等了 5 分鐘仍找不到「{app.p["name"]}」的安裝來源 -> 放棄（請確認來源模擬器已開機並已安裝）'); return False
            d.check_stop(); time.sleep(20)                     # a source emulator may still be booting
    if not d.install_cached(pkg, d.SER, say): return False
    say('first start of the app on this emulator (new account, downloads: can take 5-15 min)')
    app.restart(wait=60)
    if store_redirect(app, say): return False                  # Play-protected app, sideload won't run -> stop with a clear message
    if not app.to_lobby(max_steps=80, restart_ok=False): say('lobby not reached yet -> the recorder keeps trying')
    return True

def run(app_id, slot_ids, job, log_dir, minutes=None, split=1, group='', res=None, precise=False):
    from .app import App
    from . import device as d
    from .device import log
    app = App(app_id); rows = {r['id']: r for r in app.slots()}
    jp = os.path.join(log_dir, f'job_{job}.json')
    st = json.load(open(jp, encoding='utf-8')) if os.path.exists(jp) else {'app': app_id, 'slots': slot_ids, 'status': {}}
    st['emulator'] = d.IDX; st['split'] = split; st['group'] = group; st['precise'] = precise
    st['pid'] = os.getpid(); st['finished'] = None; st['started'] = time.strftime('%F %T')     # running (see mark_finished)
    res = res if res is not None else {}
    sw = os.path.join(log_dir, f'job_{job}.emu'); os.environ['SLOTREC_SWITCH_FILE'] = sw   # record writes a new emulator index here
    def save(): json.dump(st, open(jp, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    from .version import VERSION
    save(); log(f'JOB {job}: {len(slot_ids)} slot(s) on emulator {d.IDX} ({d.SER})  SlotRecorder v{VERSION}'
                + (f'  [group {group}: 1 of {split} emulators, each records the full length (more samples)]' if group else ''))
    try:
        if not d.ensure_running(timeout=360): log('emulator not available -> job stops'); res['reason'] = 'failed'; return 2
        refresh_cache(app, log)                                # pull in a newer version (Play-updated master emulator) before install/upgrade
        if not ensure_app(app, log): log('app could not be installed -> job stops'); res['reason'] = 'failed'; return 2
    except d.StopRequested:
        log('STOP requested during setup'); res['reason'] = 'stopped'; return 2
    for i, sid in enumerate(slot_ids, 1):
        if d.stop_requested(): log('STOP requested -> batch stops'); break
        if st['status'].get(sid, {}).get('result') == 'done': log(f'skip {sid} (already done)'); continue
        r = rows.get(sid, {'name': sid, 'minutes': 60})
        mins = minutes or float(r.get('minutes') or 60)
        if precise and not minutes:                            # 夠準就停: the list's minutes become the per-game cap
            from .config import adv_nz
            mins = float(adv_nz('precise_max_minutes'))
        # NOTE: in a group run every emulator records the FULL minutes (N emulators x R cycles = more samples of the same slot),
        # not minutes/N. The videos all carry the group id, so core.analyze --group merges them into one report per slot.
        log(f'SLOT {i}/{len(slot_ids)} {sid}')
        if precise:
            try:
                from .live import already_precise
                done_, n_, h_ = already_precise(app_id, sid, group)
                if done_:
                    log(f'PRECISION reached already: {n_} spins, +-{h_ * 100:.1f} pp -> skip {sid}')
                    st['status'][sid] = {'result': 'done', 'note': 'already precise', 'end': time.strftime('%F %T')}; save(); continue
            except Exception as e: log('precision pre-check failed', repr(e))
        st['status'][sid] = {'result': 'running', 'start': time.strftime('%F %T')}; save()
        rc = subprocess.call([sys.executable, '-u', '-m', 'core.record', '--app', app_id, '--slot', sid,
                              '--minutes', str(mins), '--name', r.get('name') or sid], cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        st['status'][sid].update(result='done' if rc == 0 else f'failed rc={rc}', end=time.strftime('%F %T'))
        if os.path.exists(sw):                                 # out of coins -> recorder moved to a brand-new emulator
            new = open(sw).read().strip(); os.remove(sw)
            if new.isdigit():
                os.environ['SLOTREC_LD_INDEX'] = new; d.use(int(new)); st['emulator'] = int(new); log(f'next slots use emulator {new}')
        save()
    out = {k: v['result'] for k, v in st['status'].items()}
    log('JOB FINISHED', out)
    res['reason'] = 'done' if all(out.get(s) == 'done' for s in slot_ids) else ('stopped' if d.stop_requested() else 'failed')
    name = next((n for i, n, _ in d.instances() if i == d.IDX), '')
    if group and name.startswith(d.SR_PREFIX) and not os.environ.get('SLOTREC_KEEP_EMU'):   # helper of a multi-run; keep it
        log(f'shut down helper emulator {d.IDX} {name}'); d.ld('quit', '--index', str(d.IDX))   # up between repeat cycles
    return 0 if all(out.get(s) == 'done' for s in slot_ids) else 2

if __name__ == '__main__':
    main()
