"""Record one slot for N minutes: auto-spin + popup/ad recovery + auto account reset when out of coins.

usage:  python -m core.record --app lightning_link --slot bzzf_golden5 --minutes 60 [--name BaoZhuZhaoFu_Golden5]
exit:   0 = video OK, 2 = incomplete/failed (batch retries it next run)
stop:   create <SlotRecorder>/STOP_<emulator index> (or STOP = all jobs) -> stops gracefully, keeps what was recorded

Every 5 s: "normal" = in-game marker lit and screen not frozen. Otherwise the ladder runs, heavy steps first so
cheap taps can never starve them; every tap-type step has a repeat limit:
  foreign app / ad in front .......... BACK, still foreign -> restart app
  out of coins (LOW ON COINS template, or balance < low_balance_bets x bet)
                                       pause rec -> new guest account -> re-enter -> new segment (max 3;
                                       out of coins again <5 min after a reset -> level-2 reset once)
  off >420 s or 3 BACKs in a row ..... restart app + re-enter slot
  frozen >=45 s, off >240 s, or >=10 centre taps without effect ... BACK
  back in lobby ...................... re-enter slot + min bet
  X button (lit, top layer) .......... tap   (skip after 5 taps on the same spot)
  known button template .............. tap   (skip after 3 same)
  lit SPIN under an overlay .......... tap   (skip after 3 in a row)
  OCR safe button .................... tap   (skip after 3 same; never on pages showing $/BUY/UNLOCK/...)
  off >10 s (portrait games) ......... tap screen centre

Output next to the video: _stats.json (counters, timeline of off-game spans with video timestamps, bet),
_timeline.csv, _clean.mp4 (long stuck spans cut out; original kept), then core.analyze runs in the background.
Machine-readable lines for the GUI:  PROGRESS <recorded>/<total>   STATUS <state> key=value ...
"""
import argparse, csv, json, math, os, subprocess, sys, time
import cv2
from .config import CFG, TOOLS, ROOT, video_dir
from . import device as d
from .device import log, StopRequested
from .app import App
from .vision import signature, changed, imwrite
from .ocr import safe_button
from . import ocr

MAX_RESETS = 3
FORCE_RESET_AT = float(os.environ.get('FORCE_RESET_AT', 0))   # test only
_NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
MIN_FREE_DISK_GB = 1.0
LOW_RAM_GB = 1.0

def probe(path):
    r = subprocess.run([TOOLS['ffprobe'] or 'ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                       capture_output=True, text=True, creationflags=_NOWIN)
    try: return float(r.stdout.strip())
    except ValueError: return 0.0

def keyframes(path):
    r = subprocess.run([TOOLS['ffprobe'] or 'ffprobe', '-v', 'error', '-select_streams', 'v', '-skip_frame', 'nokey',
                        '-show_entries', 'frame=pts_time', '-of', 'csv=p=0', path], capture_output=True, text=True, creationflags=_NOWIN)
    out = []
    for l in r.stdout.split():
        try: out.append(float(l.strip(',')))
        except ValueError: pass
    return sorted(out)

def ffmpeg(*a): return subprocess.run([TOOLS['ffmpeg'] or 'ffmpeg', '-y', '-loglevel', 'error', *a], creationflags=_NOWIN).returncode

def hms(s): s = int(s); return f'{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}'

def cut_clean(mp4, spans, min_len=20):
    """write <video>_clean.mp4 without the removable spans. Stream copy (fast); a span starts when the problem was
    detected and ends 5 s before play resumed, then snaps back to a keyframe -> at most ~1-2 s of play is lost at a cut."""
    kf = keyframes(mp4); dur = probe(mp4)
    if not kf or dur <= 0: return None, 0
    cuts = []
    for a, b in spans:
        b2 = max([k for k in kf if k <= b] or [0])          # resume exactly on a keyframe
        if b2 - a >= min_len: cuts.append((a, b2))
    if not cuts: return None, 0
    keep, pos = [], 0.0
    for a, b in cuts:
        if a - pos > 1: keep.append((pos, a))
        pos = b
    if dur - pos > 1: keep.append((pos, dur))
    lst = mp4[:-4] + '_clean.txt'; out = mp4[:-4] + '_clean.mp4'
    with open(lst, 'w', encoding='utf-8') as f:
        for a, b in keep:
            f.write("file '" + mp4.replace(os.sep, '/') + f"'\ninpoint {a:.3f}\noutpoint {b:.3f}\n")
    rc = ffmpeg('-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', out)
    try: os.remove(lst)
    except OSError: pass
    if rc != 0 or not os.path.exists(out): return None, 0
    return out, sum(b - a for a, b in cuts)

def record(app_id, slot_id, minutes, name=None, interval=None):
    app = App(app_id); p = app.p
    interval = interval or float(os.environ.get('SLOTREC_SPIN_INTERVAL') or 0) or CFG.get('spin_interval', 0.25)   # continuous tap
    for n in [slot_id, p['ingame_marker'], p['lobby_marker'], p['spin_btn']] + ([p['low_coins']] if p.get('low_coins') else []):
        assert app.t.has(n), f'missing template {n}.png in apps/{app_id}/tpl or slots'
    missing = [k for k in ('scrcpy', 'ffmpeg', 'ffprobe') if not TOOLS[k]]
    if missing: log('missing tools:', ', '.join(missing), '-> run install.bat or set paths in 設定'); return 2
    name = name or slot_id
    vdir = video_dir(app_id)
    if d.free_disk_gb(vdir) < MIN_FREE_DISK_GB + minutes / 60 * 1.5:
        log(f'not enough disk space in {vdir}: {d.free_disk_gb(vdir):.1f} GB free'); return 2
    group = os.environ.get('SLOTREC_GROUP', '')              # multi-emulator run: several jobs start the same slot together
    fname = name if name.isascii() else slot_id               # Chinese display names: ASCII file names (OpenCV / tools)
    base = os.path.join(vdir, f'{time.strftime("%Y%m%d_%H%M%S")}_{fname}' + (f'_e{d.IDX}' if group else ''))
    snapdir = base + '_snaps'; os.makedirs(snapdir, exist_ok=True)
    log(f'=== {p["name"]} / {name}  {minutes} min  (emulator {d.IDX} {d.SER}) ===')
    log('STATUS entering')
    if not d.ensure_running(): log('device not found:', d.SER); return 2

    from .version import VERSION
    st = dict(version=VERSION, app=app_id, slot=slot_id, name=name, emulator=d.IDX, spins=0, popups=0, restarts=0, reenter=0, offgame_s=0,
              stalls=0, backs=0, escalations=[], errors=0, resets=0, bet=None, bet_changes=0, timeline=[], taps=[],
              group=group, split=int(os.environ.get('SLOTREC_SPLIT') or 1))
    try:
        app.guard_focus()
        # always enter through the lobby: a slot that is already open may be a DIFFERENT game (left by an earlier job)
        if True:
            if not app.enter_slot(slot_id):
                if p.get('leveling') and app.level_until_slot(slot_id) and app.enter_slot(slot_id): pass
                else:
                    app.restart()
                    if not app.enter_slot(slot_id): log('cannot enter slot'); return 2
        st['bet'] = app.min_bet()
        if p.get('paytable') and CFG.get('capture_paytable', True):    # once per game: paytable / rules screenshots
            try:
                from .paytable import capture, has
                if not has(app, slot_id): log('STATUS paytable'); capture(app, slot_id, say=log)
            except StopRequested: raise
            except Exception as e: log('paytable capture failed', repr(e))
    except StopRequested:
        log('STOP requested before recording started'); return 2

    secs = int(minutes * 60)
    env = dict(os.environ, ADB=TOOLS['adb'])
    segs = []; cur = {'p': None, 'path': None, 't0': 0}
    kfi = CFG.get('keyframe_interval', 1)
    def recorded(): return sum(x for _, x in segs)
    def vt(): return recorded() + (time.time() - cur['t0'] if cur['p'] is not None else 0)   # position in the final video
    def start_seg(remaining):
        path = f'{base}_seg{len(segs)+1}.mkv'; lim = max(1, math.ceil(remaining))
        cmd = [TOOLS['scrcpy'], '-s', d.SER, '--no-window', '--no-audio', f'--max-fps={CFG["max_fps"]}',
               f'--video-bit-rate={CFG["bitrate"]}', f'--record={path}', f'--time-limit={lim}']
        if kfi: cmd.append(f'--video-codec-options=i-frame-interval:float={kfi}')   # dense keyframes -> precise clean cuts
        cur['p'] = subprocess.Popen(cmd, env=env, stdout=open(base + '_scrcpy.log', 'a'), stderr=subprocess.STDOUT, creationflags=_NOWIN)
        cur['path'] = path; cur['t0'] = time.time(); log('recording segment', len(segs)+1, f'({lim}s left)')
    def close_seg():
        pr, path = cur['p'], cur['path']
        if pr is None: return
        cur['p'] = cur['path'] = None                         # mark closed first -> never appended twice
        if pr.poll() is None:
            pr.terminate()
            try: pr.wait(15)
            except subprocess.TimeoutExpired: pr.kill(); pr.wait(15)
        if not (os.path.exists(path) and os.path.getsize(path) > 0): log('segment empty'); return
        fixed = path[:-4] + '_fixed.mkv'                       # remux: restores duration/index after a hard stop
        if ffmpeg('-i', path, '-c', 'copy', fixed) == 0 and os.path.getsize(fixed) > 0: os.remove(path)
        else: fixed = path
        segs.append((fixed, probe(fixed))); log(f'segment closed {segs[-1][1]:.0f}s, total {recorded():.0f}/{secs}s')

    t0 = time.time()
    def snap(tag, im): imwrite(os.path.join(snapdir, f'{tag}_{int(time.time()-t0):05d}.png'), im)
    def esc(what, im):
        log('ESCALATE', what); st['escalations'].append((int(time.time()-t0), what)); snap('esc', im)
        span['esc'] = True
    def tapev(kind, label=''):
        st['taps'].append((round(vt(), 1), kind, str(label))); span[kind] = span.get(kind, 0) + 1
    state = {'s': None}
    def status(s, force=False):
        if s != state['s'] or force:
            state['s'] = s
            el = max(1, time.time() - t0)
            log(f'STATUS {s} spins={st["spins"]} popups={st["popups"]} off={100 * st["offgame_s"] / el:.0f}% '
                f'resets={st["resets"]} bet={st["bet"] or "-"} ram={d.free_ram_gb():.1f}GB')

    start_seg(secs)
    live = None; last_live = 0; precise = os.environ.get('SLOTREC_PRECISE') == '1'; READ_EVERY_S = 2.5
    def read_bal(im):
        """balance with the CURRENT layout; never re-detects orientation from this screenshot (a lobby / popup frame
        would flip the spin position). Frames whose orientation differs from the slot's -> None"""
        L = app.lay()
        if bool(getattr(app, '_land', False)) != (im.shape[1] > im.shape[0]): return None
        box = (L.get('meters') or {}).get('balance')
        return ocr.read_box(im, box) if box else None
    if CFG.get('live_stats', True) and (p.get('meters') or {}).get('balance'):
        try:
            from .live import Live, READ_EVERY_S
            live = Live(app_id, slot_id, group, d.IDX)
            if precise: log(f'夠準就停: earlier videos of this game give {len(live.prior)} spins')
        except Exception as e: log('live stats off:', repr(e))
    last_chk = last_snap = last_prog = last_meter = 0; last_betchk = time.time(); off_since = None; back_streak = 0
    prev_sig = None; same_since = None; popup_times = []; last_reset_t = None; centre_taps = 0
    span = {}                                                  # current off-game span (video time + what was done)
    rep = {'x': (None, 0), 'spin': 0, 'ocr': (None, 0), 'skip': set()}
    feat = {'on': False, 'xy': None}                           # off-game win-celebration / feature -> keep tapping (fast loop) to skip it
    bal_ref = {'val': None, 't': 0.0}                          # last time the balance meter changed -> reels not resolving = feature / long win
    dims = {'w': 0, 'h': 0}                                     # last grabbed frame size -> screen centre for skip taps
    FEAT_SKIP_S = float(os.environ.get('SLOTREC_FEAT_SKIP_S') or CFG.get('feat_skip_s', 6))   # in-game but balance frozen this long -> tap centre to skip feature animation
    def reset_rep(): rep.update(x=(None, 0), spin=0, ocr=(None, 0), skip=set())
    def close_span():
        nonlocal span
        if span.get('vt0') is not None:
            a = span.pop('vt0'); b = max(a, vt() - 5)
            st['timeline'].append([round(a, 1), round(b, 1), json.dumps(span, ensure_ascii=False)])
        span = {}
    status('game', True)
    try:
        while True:
            if d.stop_requested(): log('STOP requested'); st['stopped'] = True; break
            if cur['p'].poll() is not None:
                rc = cur['p'].returncode; close_span(); close_seg()
                if recorded() >= secs * 0.98: break
                if segs and segs[-1][1] < 10:
                    st['errors'] += 1
                    if sum(1 for _, x in segs if x < 10) >= 3: raise RuntimeError('scrcpy keeps exiting immediately')
                log('scrcpy exited early rc', rc, '-> new segment'); start_seg(secs - recorded())
                if off_since is not None: span = {'vt0': vt()}
            if time.time() - last_chk >= 5:
                last_chk = time.time()
                try:
                    app.refresh()                               # pick up new templates / profile edits live
                    if app.guard_focus(): st['restarts'] += 1
                    im = d.grab()
                except StopRequested: raise
                except Exception as e:
                    st['errors'] += 1; log('check failed', e); time.sleep(interval); continue
                try:
                    now = time.time()
                    ig = app.in_game(im)
                    if live and ig and off_since is None:
                        _b = read_bal(im); live.add(vt(), _b); last_live = now
                        if _b is not None and (bal_ref['val'] is None or _b != bal_ref['val']): bal_ref['val'] = _b; bal_ref['t'] = now

                    # ---- out of coins: template, or balance meter below N bets (checked once a minute while in game)
                    poor = False
                    if ig and st['bet'] and p.get('low_balance_bets') and now - last_meter >= 60:
                        last_meter = now; bal = app.read_meter(im, 'balance')
                        if bal is not None:
                            st.setdefault('balance_first', bal); st['balance_last'] = bal
                            thr = p['low_balance_bets'] * st['bet']
                            if bal < thr:
                                reads = [bal]
                                for _ in range(2): time.sleep(1.5); reads.append(app.read_meter(d.grab(), 'balance'))
                                if all(r is not None and r < thr for r in reads) and max(reads) - min(reads) <= 3 * st['bet']:
                                    log(f'balance {bal:,} < {p["low_balance_bets"]} bets (confirmed {reads})'); poor = True
                                else: log('low balance read not confirmed', reads)
                    force = FORCE_RESET_AT and now - t0 > FORCE_RESET_AT and st['resets'] == 0
                    if force or poor or app.low_coins(im):
                        snap('lowcoins', im)
                        mode = p.get('reset', 'pm_clear')
                        if mode == 'new_instance' and not CFG.get('new_instance_reset', True): mode = 'none'   # GUI 設定 switch
                        if mode == 'none':
                            log('OUT OF COINS -> stop (no account reset for this app). Video so far is kept.')
                            st['stopped'] = 'out of coins'; break
                        close_span(); close_seg(); status('reset', True)
                        if secs - recorded() < max(60, secs * 0.02): log('out of coins near the end -> finish'); break
                        if st['resets'] >= MAX_RESETS: raise RuntimeError(f'out of coins again after {MAX_RESETS} resets')
                        level = max(p.get('reset_level', 1), 2 if (last_reset_t and now - last_reset_t < 300) else 1)
                        log(f'LOW ON COINS -> reset level {level} ({st["resets"]+1}/{MAX_RESETS})')
                        st['resets'] += 1; last_reset_t = now; entered = False
                        for attempt in range(1 if mode == 'new_instance' else 2):   # never create two emulators in a row
                            try:
                                if app.reset_account(level if attempt == 0 else p.get('reset_level', 1)) and (app.enter_slot(slot_id) or (
                                        p.get('leveling') and app.level_until_slot(slot_id) and app.enter_slot(slot_id))):
                                    entered = True; break
                            except StopRequested: raise
                            except Exception as e:
                                st['errors'] += 1; log('reset attempt failed', repr(e))
                        if not entered:
                            if mode == 'new_instance':                    # fallback: stop, keep the video
                                log('OUT OF COINS -> new emulator did not work -> stop. Video so far is kept.')
                                st['stopped'] = 'out of coins (new emulator failed)'; break
                            raise RuntimeError('cannot re-enter slot after reset')
                        if d.IDX != st['emulator']: st.setdefault('emulators', [st['emulator']]).append(d.IDX); st['emulator'] = d.IDX
                        st['bet'] = app.min_bet() or st['bet']; start_seg(secs - recorded())
                        if live: live.boundary(vt())
                        off_since = same_since = prev_sig = None; reset_rep(); last_chk = time.time(); bal_ref['val'], bal_ref['t'] = None, 0.0; status('game', True)
                        continue

                    H, W = im.shape[:2]; dims['w'], dims['h'] = W, H
                    sig = signature(im)
                    same_since = None if changed(prev_sig, sig) else (same_since or now)
                    prev_sig = sig
                    frozen = 0 if same_since is None else now - same_since
                    if ig: app.lay(im)                                   # portrait or landscape slot -> its button positions
                    if ig and frozen < 30:
                        if off_since is not None: close_span()
                        off_since = None; back_streak = 0; centre_taps = 0; reset_rep(); status('game')
                        if st['bet'] and now - last_betchk >= 600:      # bet drifted (level-up / feature end)? -> back to minimum
                            last_betchk = now; b = app.read_meter(im, 'bet')
                            if b and b != st['bet']:
                                log(f'bet changed {st["bet"]:,} -> {b:,} -> min bet again'); st['bet_changes'] += 1
                                st['bet'] = app.min_bet() or st['bet']
                    else:
                        if off_since is None:
                            off_since = now; span = {'vt0': vt()}; snap('stall' if ig else 'off', im)
                            if ig: st['stalls'] += 1; log('stall: in game but screen frozen 30s')
                        if frozen >= 30: span['frozen'] = max(span.get('frozen', 0), int(frozen))
                        dur = now - off_since; st['offgame_s'] += 5; feat['on'] = False   # re-enabled only by the celebration/feature branch below
                        status('stuck' if (frozen >= 30 or dur > 60) else 'popup')
                        if dur > 420 or back_streak >= 3:
                            esc(f'restart app (off {int(dur)}s, backs {back_streak})', im)
                            app.restart(); st['restarts'] += 1; back_streak = 0; centre_taps = 0; reset_rep()
                            if app.enter_slot(slot_id): st['bet'] = app.min_bet() or st['bet']
                            off_since = same_since = None; bal_ref['val'], bal_ref['t'] = None, 0.0; close_span()
                        elif frozen >= 45 or (dur > 240 and int(dur) % 60 < 5) or (centre_taps >= 10 and dur >= 120):
                            esc(f'BACK (frozen {int(frozen)}s, off {int(dur)}s, centre taps {centre_taps})', im)
                            d.back(); st['backs'] += 1; back_streak += 1; same_since = None; centre_taps = 0
                        elif app.is_lobby(im):
                            log('in lobby -> re-enter'); st['reenter'] += 1; span['lobby'] = 1; status('lobby')
                            if app.enter_slot(slot_id):
                                st['bet'] = app.min_bet() or st['bet']; off_since = None; reset_rep(); bal_ref['val'], bal_ref['t'] = None, 0.0; close_span()
                            else: esc('re-enter failed', im)
                        else:
                            if rep['ocr'][1] >= 3: rep['skip'].add(rep['ocr'][0])   # tapped 3x, no effect -> skip until back in game
                            s, x = app.find_x(im)
                            if not x: s, x = app.find_x(im, full=True)
                            lx, nx = rep['x']
                            nx = nx + 1 if (x and lx and abs(x[0]-lx[0]) + abs(x[1]-lx[1]) < 10) else 0
                            rep['x'] = (x, nx); ob = (None, None)
                            if x and nx < 5:
                                d.tap(*x); st['popups'] += 1; popup_times.append(now); log('popup X', x, f'{s:.2f}'); snap('pop', im)
                                tapev('x')
                            elif (tb := app.find_button(im)) and not (tb[0] == rep['ocr'][0] and rep['ocr'][1] >= 3):
                                rep['ocr'] = (tb[0], rep['ocr'][1] + 1 if tb[0] == rep['ocr'][0] else 1)
                                d.tap(*tb[1]); st['popups'] += 1; popup_times.append(now); log('tap', tb[0], tb[1]); snap('btn', im)
                                tapev('btn', tb[0])
                            elif rep['spin'] < 3 and app.spin_visible(im):
                                d.tap(*app.lay()['spin_xy']); rep['spin'] += 1; log('tap lit SPIN (overlay/tutorial)')
                            elif (ob := safe_button(im, rep['skip'], app.ocr_ignore(im)))[0] and not (ob[1] == rep['ocr'][0] and rep['ocr'][1] >= 3):
                                rep['ocr'] = (ob[1], rep['ocr'][1] + 1 if ob[1] == rep['ocr'][0] else 1)
                                d.tap(*ob[0]); st['popups'] += 1; popup_times.append(now); log('OCR tap', ob[1], ob[0]); snap('ocr', im)
                                tapev('ocr', ob[1])
                            elif dur > 10 and ocr.LAST_DANGER:            # price / BUY page without a NO-THANKS button
                                d.back(); st['backs'] += 1; back_streak += 1; log('purchase-like page -> BACK (no centre tap)')
                            elif dur > 4 and (H > W or p.get('centre_tap_landscape')) and not ocr.LAST_DANGER:
                                feat['xy'] = (W // 2, H // 2); feat['on'] = True   # celebration/feature: fast loop below spams this to skip it
                                d.tap(W // 2, H // 2); centre_taps += 1; span['centre'] = span.get('centre', 0) + 1
                                log('tap centre (off, feature/celebration -> fast tapping)')
                        popup_times = [t for t in popup_times if now - t < 120]
                        if len(popup_times) >= 6: esc(f'popup storm: {len(popup_times)} in 2 min', im); popup_times.clear()
                    if now - last_snap >= 600: last_snap = now; snap('t', im)
                    if now - last_prog >= 60:                     # machine-readable progress line for the GUI
                        last_prog = now
                        log(f'PROGRESS {int(vt())}/{secs}'); status(state['s'], True)
                        if live:
                            try:
                                offs = [(a, b) for a, b, info in st['timeline'] if removable((a, b, info))]
                                r = live.update(st['bet'], st['taps'], offs)
                                if r:
                                    st['live'] = r; pc = lambda x: '-' if x is None else f'{x:.4f}'
                                    log(f"LIVE spins={r['spins']} rtp={r['rtp']:.4f} lo={pc(r['lo'])} hi={pc(r['hi'])} feats={r['feats']} "
                                        f"total={r['total']} half={pc(r['half'])} reads={r['reads']}")
                                    if precise and live.precise():
                                        log(f"PRECISION reached: {r['total']} spins, RTP {r['total_rtp']:.1%} +-{r['half'] * 100:.1f} pp -> stop")
                                        st['stopped'] = 'precise'; break
                            except Exception as e: log('live stats failed', repr(e))
                        if d.free_disk_gb(vdir) < MIN_FREE_DISK_GB:
                            log(f'DISK almost full ({d.free_disk_gb(vdir):.1f} GB) -> stop and keep what was recorded')
                            st['stopped'] = 'disk full'; break
                        if d.free_ram_gb() < LOW_RAM_GB: log(f'WARNING low memory: {d.free_ram_gb():.1f} GB free')
                except StopRequested: raise
                except Exception as e:
                    st['errors'] += 1; log('check step failed:', repr(e))
                    if st['errors'] > 100: raise
            if off_since is None:
                now2 = time.time()
                if live and now2 - last_live >= READ_EVERY_S:   # balance before a spin -> live statistics
                    try:
                        _b = read_bal(d.grab()); live.add(vt(), _b)
                        if _b is not None and (bal_ref['val'] is None or _b != bal_ref['val']): bal_ref['val'] = _b; bal_ref['t'] = now2
                    except StopRequested: raise
                    except Exception: pass
                    last_live = now2
                if dims['w'] and bal_ref['t'] and now2 - bal_ref['t'] > FEAT_SKIP_S:   # in game but balance frozen -> feature / long win animation: tap centre to skip it
                    try: d.tap(dims['w'] // 2, dims['h'] // 2); st['anim_taps'] = st.get('anim_taps', 0) + 1
                    except StopRequested: raise
                    except Exception: pass
                d.tap(*app.lay()['spin_xy']); st['spins'] += 1
            elif feat['on'] and feat['xy']:                            # win celebration / feature: keep tapping to skip it (no per-game design)
                try: d.tap(*feat['xy']); st['feat_taps'] = st.get('feat_taps', 0) + 1
                except StopRequested: raise
                except Exception: pass
            time.sleep(interval)
    except StopRequested:
        log('STOP requested'); st['stopped'] = True
    except BaseException as e:
        log('main loop aborted:', repr(e)); st['aborted'] = repr(e)
    finally:
        try: close_span()
        except Exception: pass
        if live: live.close()
        try: close_seg()
        except Exception as e: log('close_seg failed', repr(e))
        log('STATUS finishing')
        st['elapsed_s'] = int(time.time() - t0); st['segments'] = [(os.path.basename(x), round(y, 1)) for x, y in segs]
        mp4 = base + '.mp4'; ok = False
        parts = list(dict.fromkeys(x for x, _ in segs if os.path.exists(x) and os.path.getsize(x) > 0))
        if parts:
            lst = base + '_concat.txt'
            with open(lst, 'w', encoding='utf-8') as fh:
                for x in parts: fh.write("file '" + x.replace(os.sep, '/') + "'\n")
            if ffmpeg('-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', mp4) == 0 and os.path.exists(mp4) and os.path.getsize(mp4) > 0:
                for x in parts:
                    try: os.remove(x)
                    except OSError: pass
                os.remove(lst)
                st['video_s'] = round(probe(mp4), 1)
                ok = st['video_s'] >= secs * 0.95 or st.get('stopped') == 'precise'
        st['ok'] = ok; st['file'] = mp4
        if os.path.exists(mp4):
            try: finish_outputs(mp4, st)
            except Exception as e: log('timeline / clean cut failed', repr(e))
        with open(base + '_stats.json', 'w', encoding='utf-8') as f: json.dump(st, f, indent=1, ensure_ascii=False)
        log('done', 'OK' if ok else 'INCOMPLETE', f"video {st.get('video_s', 0)}s ->", mp4)
        if os.path.exists(mp4) and st.get('video_s', 0) >= 60 and CFG.get('auto_analyze', True):
            flag = base + '.analyzing'                  # set BEFORE the spawn: a merge must not start the same analysis
            try:                                        # statistics in the background, low priority; next slot starts now
                open(flag, 'w').close()
                pr = subprocess.Popen([sys.executable, '-m', 'core.analyze', mp4], cwd=ROOT, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, creationflags=_NOWIN | getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0))
                with open(flag, 'w') as f: f.write(str(pr.pid))
                log('analysis started in background ->', os.path.basename(base) + '_summary.html')
            except Exception as e:
                log('could not start analysis', repr(e))
                try: os.remove(flag)
                except OSError: pass
        try:                                            # recognition pipelines (SpookyLink ...) get their copy
            from .exports import wanted
            if os.path.exists(mp4) and st.get('video_s', 0) >= 60 and wanted(app_id, slot_id):
                subprocess.Popen([sys.executable, '-m', 'core.exports', mp4], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=_NOWIN | getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0))
                log('export to recognition pipeline started in background')
        except Exception as e: log('could not start export', repr(e))
    return 0 if ok else 2

def removable(row):
    """off-game span worth cutting from the clean video: stuck / lobby / popups dismissed / repeated centre taps.
    Spans with only OCR taps (feature START / COLLECT screens) are kept."""
    a, b, info = row; info = json.loads(info) if isinstance(info, str) else info
    return (b - a >= 20) and bool(info.get('esc') or info.get('lobby') or info.get('frozen') or info.get('x')
                                  or info.get('btn') or info.get('centre', 0) >= 3)

def finish_outputs(mp4, st):
    base = mp4[:-4]
    with open(base + '_timeline.csv', 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f); w.writerow(['start', 'end', 'seconds', 'cut_in_clean', 'what_happened'])
        for a, b, info in st['timeline']: w.writerow([hms(a), hms(b), round(b - a), 'yes' if removable((a, b, info)) else '', info])
        for i, (_, x) in enumerate(st.get('segments', [])[:-1]):
            at = sum(y for _, y in st['segments'][:i+1]); w.writerow([hms(at), hms(at), 0, '', 'segment boundary (account reset / scrcpy restart)'])
    spans = [(a, b) for a, b, info in st['timeline'] if removable((a, b, info))]
    out, cut = cut_clean(mp4, spans) if spans else (None, 0)
    st['clean_file'] = out; st['clean_cut_s'] = round(cut, 1)
    if out: log(f'clean video: cut {cut:.0f}s of stuck screens -> {os.path.basename(out)}')

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--app', required=True); ap.add_argument('--slot', required=True)
    ap.add_argument('--minutes', type=float, default=60); ap.add_argument('--name')
    a = ap.parse_args()
    sys.exit(record(a.app, a.slot, a.minutes, a.name))
