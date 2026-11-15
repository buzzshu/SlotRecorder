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
    a = ap.parse_args()
    if a.emu is not None: os.environ['SLOTREC_LD_INDEX'] = str(a.emu)     # before importing config -> children inherit
    from .config import ROOT as _R
    _jp = os.path.join(_R, 'logs', f'job_{a.job}.json')
    if os.path.exists(_jp):                                    # resume: the job may have moved to a new emulator meanwhile
        _j = json.load(open(_jp, encoding='utf-8'))
        if _j.get('emulator') is not None: os.environ['SLOTREC_LD_INDEX'] = str(_j['emulator'])
        a.split = a.split if a.split > 1 else int(_j.get('split') or 1); a.group = a.group or _j.get('group') or ''
    if os.environ.get('SLOTREC_LD_INDEX'): os.environ.setdefault('SLOTREC_STOP_IDX', str(a.emu if a.emu is not None else os.environ['SLOTREC_LD_INDEX']))
    if a.group: os.environ['SLOTREC_GROUP'] = a.group; os.environ['SLOTREC_SPLIT'] = str(max(1, a.split))
    from .config import ROOT
    LOG_DIR = os.path.join(ROOT, 'logs'); os.makedirs(LOG_DIR, exist_ok=True)
    os.environ.setdefault('SLOTREC_LOG', os.path.join(LOG_DIR, f'job_{a.job}.log'))   # children inherit -> one log file
    from . import device as d
    d._LOGFILE = os.environ['SLOTREC_LOG']
    if os.path.exists(d.stop_file()):
        d.log('STOP file present from an earlier run -> removed'); os.remove(d.stop_file())
    sys.exit(run(a.app, [s for s in a.slots.split(',') if s], a.job, LOG_DIR, a.minutes, max(1, a.split), a.group))

def ensure_app(app, say):
    """a fresh emulator (GUI multi-run creates SR_ ones) has no app yet -> install it from apk_cache, which is filled
    from any running emulator that has the app. Then first start: new-account screens + long downloads.
    -> True when the app is installed (and, if it was just installed, in its lobby)"""
    from . import device as d
    from .config import serial_for
    pkg = app.p['package']
    if any(d.installed(pkg) or time.sleep(3) for _ in range(3)): return True    # 3 tries: one adb hiccup != not installed
    say(f'{app.p["name"]} is not installed on emulator {d.IDX} -> installing')
    t0 = time.time()
    while not d.apk_cached(pkg):
        src = next((serial_for(i) for i, _, run in d.instances() if run and i != d.IDX and d.installed(pkg, serial=serial_for(i))), None)
        if src and d.cache_apk(pkg, src, say): break
        if time.time() - t0 > 600: say('no running emulator with the app to copy it from (10 min) -> give up'); return False
        d.check_stop(); time.sleep(20)                         # the source emulator may still be booting
    if not d.install_cached(pkg, d.SER, say): return False
    say('first start of the app on this emulator (new account, downloads: can take 5-15 min)')
    app.restart(wait=60)
    if not app.to_lobby(max_steps=80, restart_ok=False): say('lobby not reached yet -> the recorder keeps trying')
    return True

def run(app_id, slot_ids, job, log_dir, minutes=None, split=1, group=''):
    from .app import App
    from . import device as d
    from .device import log
    app = App(app_id); rows = {r['id']: r for r in app.slots()}
    jp = os.path.join(log_dir, f'job_{job}.json')
    st = json.load(open(jp, encoding='utf-8')) if os.path.exists(jp) else {'app': app_id, 'slots': slot_ids, 'status': {}}
    st['emulator'] = d.IDX; st['split'] = split; st['group'] = group
    sw = os.path.join(log_dir, f'job_{job}.emu'); os.environ['SLOTREC_SWITCH_FILE'] = sw   # record writes a new emulator index here
    def save(): json.dump(st, open(jp, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    from .version import VERSION
    save(); log(f'JOB {job}: {len(slot_ids)} slot(s) on emulator {d.IDX} ({d.SER})  SlotRecorder v{VERSION}'
                + (f'  [group {group}: 1 of {split} emulators, each records 1/{split} of the minutes]' if group else ''))
    try:
        if not d.ensure_running(timeout=360): log('emulator not available -> job stops'); return 2
        if not ensure_app(app, log): log('app could not be installed -> job stops'); return 2
    except d.StopRequested:
        log('STOP requested during setup'); return 2
    for i, sid in enumerate(slot_ids, 1):
        if d.stop_requested(): log('STOP requested -> batch stops'); break
        if st['status'].get(sid, {}).get('result') == 'done': log(f'skip {sid} (already done)'); continue
        r = rows.get(sid, {'name': sid, 'minutes': 60})
        mins = minutes or float(r.get('minutes') or 60)
        if split > 1 and not minutes: mins = max(1, math.ceil(mins / split))
        log(f'SLOT {i}/{len(slot_ids)} {sid}')
        st['status'][sid] = {'result': 'running', 'start': time.strftime('%F %T')}; save()
        rc = subprocess.call([sys.executable, '-u', '-m', 'core.record', '--app', app_id, '--slot', sid,
                              '--minutes', str(mins), '--name', r.get('name') or sid], cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        st['status'][sid].update(result='done' if rc == 0 else f'failed rc={rc}', end=time.strftime('%F %T'))
        if os.path.exists(sw):                                 # out of coins -> recorder moved to a brand-new emulator
            new = open(sw).read().strip(); os.remove(sw)
            if new.isdigit():
                os.environ['SLOTREC_LD_INDEX'] = new; d.use(int(new)); st['emulator'] = int(new); log(f'next slots use emulator {new}')
        save()
    res = {k: v['result'] for k, v in st['status'].items()}
    log('JOB FINISHED', res)
    name = next((n for i, n, _ in d.instances() if i == d.IDX), '')
    if group and name.startswith(d.SR_PREFIX):                # helper emulator of a multi-run: shut it down (kept for next time)
        log(f'shut down helper emulator {d.IDX} {name}'); d.ld('quit', '--index', str(d.IDX))
    return 0 if all(res.get(s) == 'done' for s in slot_ids) else 2

if __name__ == '__main__':
    main()
