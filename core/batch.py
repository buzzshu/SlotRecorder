"""Record a list of slots one after another on one emulator. Resumable: a job file remembers which slots are done.

usage:  python -m core.batch --app lightning_link --slots bzzf_golden5,other_slot [--job 20261112_1400] [--emu 1]
        re-running with the same --job skips slots already done (resume after a crash / STOP / reboot).
        --emu = LDPlayer instance index (default: config ld_index). Stop: create STOP_<index> (or STOP for all jobs).
"""
import argparse, json, os, subprocess, sys, time

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--app', required=True); ap.add_argument('--slots', required=True)
    ap.add_argument('--job', default=time.strftime('%Y%m%d_%H%M%S')); ap.add_argument('--emu', type=int)
    ap.add_argument('--minutes', type=float, help='override slots.csv minutes (tests)')
    a = ap.parse_args()
    if a.emu is not None: os.environ['SLOTREC_LD_INDEX'] = str(a.emu)     # before importing config -> children inherit
    from .config import ROOT as _R
    _jp = os.path.join(_R, 'logs', f'job_{a.job}.json')
    if os.path.exists(_jp):                                    # resume: the job may have moved to a new emulator meanwhile
        _e = json.load(open(_jp, encoding='utf-8')).get('emulator')
        if _e is not None: os.environ['SLOTREC_LD_INDEX'] = str(_e)
    if os.environ.get('SLOTREC_LD_INDEX'): os.environ.setdefault('SLOTREC_STOP_IDX', str(a.emu if a.emu is not None else os.environ['SLOTREC_LD_INDEX']))
    from .config import ROOT
    LOG_DIR = os.path.join(ROOT, 'logs'); os.makedirs(LOG_DIR, exist_ok=True)
    os.environ.setdefault('SLOTREC_LOG', os.path.join(LOG_DIR, f'job_{a.job}.log'))   # children inherit -> one log file
    from . import device as d
    d._LOGFILE = os.environ['SLOTREC_LOG']
    if os.path.exists(d.stop_file()):
        d.log('STOP file present from an earlier run -> removed'); os.remove(d.stop_file())
    sys.exit(run(a.app, [s for s in a.slots.split(',') if s], a.job, LOG_DIR, a.minutes))

def run(app_id, slot_ids, job, log_dir, minutes=None):
    from .app import App
    from . import device as d
    from .device import log
    app = App(app_id); rows = {r['id']: r for r in app.slots()}
    jp = os.path.join(log_dir, f'job_{job}.json')
    st = json.load(open(jp, encoding='utf-8')) if os.path.exists(jp) else {'app': app_id, 'slots': slot_ids, 'status': {}}
    st['emulator'] = d.IDX
    sw = os.path.join(log_dir, f'job_{job}.emu'); os.environ['SLOTREC_SWITCH_FILE'] = sw   # record writes a new emulator index here
    def save(): json.dump(st, open(jp, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    from .version import VERSION
    save(); log(f'JOB {job}: {len(slot_ids)} slot(s) on emulator {d.IDX} ({d.SER})  SlotRecorder v{VERSION}')
    if not d.ensure_running(): log('emulator not available -> job stops'); return 2
    for i, sid in enumerate(slot_ids, 1):
        if d.stop_requested(): log('STOP requested -> batch stops'); break
        if st['status'].get(sid, {}).get('result') == 'done': log(f'skip {sid} (already done)'); continue
        r = rows.get(sid, {'name': sid, 'minutes': 60})
        log(f'SLOT {i}/{len(slot_ids)} {sid}')
        st['status'][sid] = {'result': 'running', 'start': time.strftime('%F %T')}; save()
        rc = subprocess.call([sys.executable, '-u', '-m', 'core.record', '--app', app_id, '--slot', sid,
                              '--minutes', str(minutes or r.get('minutes') or 60), '--name', r.get('name') or sid], cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        st['status'][sid].update(result='done' if rc == 0 else f'failed rc={rc}', end=time.strftime('%F %T'))
        if os.path.exists(sw):                                 # out of coins -> recorder moved to a brand-new emulator
            new = open(sw).read().strip(); os.remove(sw)
            if new.isdigit():
                os.environ['SLOTREC_LD_INDEX'] = new; d.use(int(new)); st['emulator'] = int(new); log(f'next slots use emulator {new}')
        save()
    res = {k: v['result'] for k, v in st['status'].items()}
    log('JOB FINISHED', res)
    return 0 if all(res.get(s) == 'done' for s in slot_ids) else 2

if __name__ == '__main__':
    main()
