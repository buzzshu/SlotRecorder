"""Per-app game logic driven by apps/<id>/profile.json: focus guard, popup dismiss, lobby navigation, slot entry, reset."""
import csv, json, os, re, subprocess, time
import cv2
from . import device as d
from .config import ROOT, CFG, TOOLS, adv
from .vision import Templates, signature, changed, imwrite
from .device import log, check_stop

APPS_DIR = os.path.join(ROOT, 'apps')
X_LIB = os.path.join(ROOT, 'core', 'x_library')     # close-button shapes shared by all apps

def list_apps():
    out = []
    for n in sorted(os.listdir(APPS_DIR)):
        p = os.path.join(APPS_DIR, n, 'profile.json')
        if os.path.exists(p): out.append((n, json.load(open(p, encoding='utf-8'))['name']))
    return out

class App:
    def __init__(self, app_id):
        self.id = app_id
        self.dir = os.path.join(APPS_DIR, app_id)
        self.t = Templates(os.path.join(self.dir, 'tpl'), os.path.join(self.dir, 'slots'), X_LIB)
        self._stamp = None; self.refresh()
        self.shots = os.path.join(ROOT, 'logs', 'shots'); os.makedirs(self.shots, exist_ok=True)

    def _files_stamp(self):
        st = []
        for d_ in (self.dir, os.path.join(self.dir, 'tpl'), X_LIB):
            try: st.append(os.stat(d_).st_mtime_ns)
            except OSError: st.append(0)
        st.append(os.stat(os.path.join(self.dir, 'profile.json')).st_mtime_ns)
        return tuple(st)

    def refresh(self):
        """(re)load profile.json + templates when they changed on disk -> fixes apply to a running recording"""
        stamp = self._files_stamp()
        if stamp == self._stamp: return False
        first = self._stamp is None
        try: p = json.load(open(os.path.join(self.dir, 'profile.json'), encoding='utf-8'))
        except (ValueError, OSError) as e:                 # half-written by the GUI right now -> retry next check
            if first: raise
            log('profile.json not readable yet:', e); return False
        self._stamp = stamp; self.p = p
        self.t.clear()
        lib = sorted(f[:-4] for f in os.listdir(X_LIB) if f.lower().endswith('.png')) if os.path.isdir(X_LIB) else []
        self.x_names = list(dict.fromkeys(list(self.p.get('x_templates', [])) + lib))
        from . import ocr; ocr.set_extra(self.p.get('ocr_safe_extra', []))
        if not first: log('profile / templates reloaded')
        return True

    # ---------- slots.csv ----------
    @property
    def slots_csv(self): return os.path.join(self.dir, 'slots.csv')
    def slots(self):
        if not os.path.exists(self.slots_csv): return []
        with open(self.slots_csv, encoding='utf-8-sig') as f: return list(csv.DictReader(f))
    def save_slots(self, rows):
        with open(self.slots_csv, 'w', newline='', encoding='utf-8') as f:
            cols = ['id', 'name', 'minutes', 'enabled', 'hint']   # hint = lobby step where the catalog scan saw the tile
            w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
            for r in rows: w.writerow({k: r.get(k, '') for k in cols})

    def required_templates(self, slot_id):
        """template names a recording of <slot_id> cannot run without (one list for record.py and the pre-run selftest).
        A slot entered through profile 'enter_via' is not found by its lobby thumbnail, so that one is not required."""
        p = self.p
        need = [p['ingame_marker'], p['lobby_marker'], p['spin_btn']] + ([p['low_coins']] if p.get('low_coins') else [])
        if not (p.get('enter_via') or {}).get(slot_id): need = [slot_id] + need
        return need

    def prune_shots(self):
        """logs/shots collects a debug screenshot for every popup step and nothing ever removed them (871 MB on one PC).
        Keep the last shots_keep_days days and at most shots_keep_max of the pop_*.png it wrote. Never raises."""
        try:
            fs = [os.path.join(self.shots, f) for f in os.listdir(self.shots)
                  if f.startswith('pop_') and f.lower().endswith('.png')]     # only what to_lobby() wrote; screenshots a person saved here stay
            fs = sorted(((os.path.getmtime(f), f) for f in fs), reverse=True)            # newest first
            cutoff = time.time() - float(adv('shots_keep_days')) * 86400; keep = int(adv('shots_keep_max')); n = 0
            for i, (mt, f) in enumerate(fs):
                if i >= keep or mt < cutoff:
                    try: os.remove(f); n += 1
                    except OSError: pass
            if n: log(f'logs/shots: removed {n} old debug screenshot(s)')
        except Exception as e: log('prune_shots skipped', repr(e))

    # ---------- state checks ----------
    def in_game(self, im):
        if self.t.find_lit(im, self.p['ingame_marker'], 0.8)[1] is not None: return True
        lm = (self.p.get('landscape') or {}).get('ingame_marker')            # landscape slots may draw it differently
        return bool(lm) and im.shape[1] > im.shape[0] and self.t.find_lit(im, lm, 0.8)[1] is not None
    def is_lobby(self, im): return self.t.find_lit(im, self.p['lobby_marker'])[1] is not None
    def low_coins(self, im): return self.p.get('low_coins') and self.t.find(im, self.p['low_coins'], 0.85)[1] is not None
    def find_x(self, im, full=False):
        thr = 0.9 if full else 0.82
        s, c = self.t.find_x(im, self.x_names, thr, full)
        if c or full or im.shape[0] <= im.shape[1]: return s, c
        # portrait screen showing a landscape popup rotated 90 deg: its X sits bottom-right / top-left -> search rotated copies
        H, W = im.shape[:2]
        for rot in (cv2.ROTATE_90_COUNTERCLOCKWISE, cv2.ROTATE_90_CLOCKWISE):
            rs, rc = self.t.find_x(cv2.rotate(im, rot), self.x_names, thr)
            if rc:
                x, y = rc
                return rs, ((W - 1 - y, x) if rot == cv2.ROTATE_90_COUNTERCLOCKWISE else (y, H - 1 - x))
        return s, None

    # ---------- layout: most slots are portrait, some are landscape (profile 'landscape' = their positions) ----------
    LAYOUT_KEYS = ('spin_xy', 'bet_minus_xy', 'bet_plus_xy', 'exit_slot_xy', 'meters')
    def lay(self, im=None):
        """button / meter positions for the slot on screen. Pass the current in-game screenshot to (re)detect the
        orientation; without it the last detected one is used (e.g. the spin taps between two checks)."""
        if im is not None: self._land = bool(self.p.get('landscape')) and im.shape[1] > im.shape[0]
        base = {k: self.p.get(k) for k in self.LAYOUT_KEYS}
        if getattr(self, '_land', False): base.update(self.p['landscape'])
        return base

    @property
    def portrait_game(self):
        sx, sy = self.p['spin_xy']; return sy > self.p['screen'][1]   # spin y beyond landscape height -> portrait game

    def spin_visible(self, im):
        if self.t.find_lit(im, self.p['spin_btn'], 0.8)[1] is None: return False
        if (im.shape[0] > im.shape[1]) == self.portrait_game: return True
        return bool(self.p.get('landscape')) and self.in_game(im)          # landscape slot (lobby is landscape too)

    # ---------- actions ----------
    def deny_permissions(self):
        """notification / microphone prompts block the app on a fresh install -> deny them up front and mark them
        user-fixed so the app cannot ask again (profile 'deny_permissions' overrides the list)"""
        for perm in self.p.get('deny_permissions', ['android.permission.POST_NOTIFICATIONS', 'android.permission.RECORD_AUDIO']):
            d.adb('shell', 'pm', 'revoke', self.p['package'], perm)
            d.adb('shell', 'pm', 'set-permission-flags', self.p['package'], perm, 'user-set', 'user-fixed')

    def restart(self, wait=None):
        log('restart app')
        self.deny_permissions()
        d.adb('shell', 'am', 'force-stop', self.p['package'])
        d.adb('shell', 'am', 'start', '-n', f"{self.p['package']}/{self.p['activity']}")
        time.sleep(wait or self.p.get('launch_wait', 45))

    def guard_focus(self):
        """ad / store / other app in front -> BACK once, still foreign -> restart. returns True if it acted."""
        f = d.focus()
        if f == '' or f.endswith(self.p['activity']): return False
        log('foreign focus:', f)
        # known system dialogs: always the refusing button (found by view id -> any system language)
        for pkg_part, btn in (('permissioncontroller', 'permission_deny_button'),      # 「要允許傳送通知嗎？」-> 不允許
                              ('com.google.android.gms', 'signin_cancel_button')):     # Play Games 建立個人資料 -> 取消
            if pkg_part in f and d.tap_by_id(btn):
                log(f'system dialog -> {btn}'); time.sleep(3)
                if d.focus().endswith(self.p['activity']): return True
        d.back(); time.sleep(3)
        if not d.focus().endswith(self.p['activity']): self.restart()
        return True

    def find_button(self, im):
        """known safe buttons of this app (profile dismiss_templates) -> (name, (x, y)) or None"""
        for name, thr in self.p.get('dismiss_templates', []):
            s, c = self.t.find(im, name, thr)
            if c: return name, c
        return None

    def ocr_ignore(self, im):
        """profile 'ocr_ignore': {'landscape': [[x0,y0,x1,y1]...], 'portrait': [...]} = screen parts whose text the OCR
        button finder must not read (top bar with VIP badge / balance with decimals, news ticker, a 'buy feature' button)"""
        cfg = self.p.get('ocr_ignore') or {}
        return [tuple(b) for b in cfg.get('landscape' if im.shape[1] > im.shape[0] else 'portrait', [])]

    def lobby_tab(self):
        """profile 'lobby_tab_xy': the lobby has categories (包你發: 老虎機) -> show the slot category before scanning"""
        if self.p.get('lobby_tab_xy'): d.tap(*self.p['lobby_tab_xy']); time.sleep(2)

    def dismiss_once(self, im, allow_back=True, exclude=()):
        for name, thr in self.p.get('dismiss_templates', []):
            s, c = self.t.find(im, name, thr)
            if c: d.tap(*c); return f'tap {name}'
        s, c = self.find_x(im)
        if not c: s, c = self.find_x(im, full=True)
        if c: d.tap(*c); return f'tap X {c} {s:.2f}'
        from .ocr import safe_button
        c, label = safe_button(im, exclude, self.ocr_ignore(im))
        if c: d.tap(*c); return f'OCR tap "{label}" {c}'
        if not allow_back: return None
        d.back(); return 'BACK'

    def settle_in_game(self, tries=4):
        """at start: a popup may cover the slot we are already in -> close popups (no BACK) and look again"""
        for _ in range(tries):
            check_stop(); im = d.grab()
            if self.in_game(im): return True
            act = self.dismiss_once(im, allow_back=False)
            if not act: return False
            log('start: popup ->', act); time.sleep(3)
        return self.in_game(d.grab())

    def to_lobby(self, max_steps=60, stable=2, max_s=600, restart_ok=True):
        """dismiss popups until the clean lobby shows. Waits instead of BACK while the screen is still changing
        (asset download / loading). The same action 4x in a row -> BACK instead."""
        ok = 0; t0 = time.time(); prev = None; waits = 0; last_act = None; same_act = 0; exit_tries = 0; skip_labels = set()
        last_sig = None; restarted = False; sweep_bursts = 0; burn_spins = 0
        for _ in range(max_steps):
            if time.time() - t0 > max_s: break
            check_stop()
            self.guard_focus()
            im = d.grab()
            if self.is_lobby(im):
                ok += 1
                if ok >= stable: return True
                time.sleep(4); continue
            ok = 0
            ex = self.lay(im).get('exit_slot_xy') if self.in_game(im) else None
            if ex and exit_tries < 3:                               # inside a slot: BACK does nothing in some games
                exit_tries += 1; d.tap(*ex); log('in a slot -> exit button'); time.sleep(6); continue
            if ex: log('exit button did not leave the slot 3x -> other ways')
            sig = cv2.resize(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), (64, 36)).astype('int16')
            moving = prev is not None and prev.shape == sig.shape and abs(sig - prev).mean() > 3
            prev = sig
            imwrite(os.path.join(self.shots, f'pop_{time.strftime("%H%M%S")}.png'), im)
            act = self.dismiss_once(im, allow_back=False, exclude=skip_labels) if same_act < 4 else None
            if act is None:
                if moving and self.in_game(im) and burn_spins < 40:   # a feature / free-spins is ANIMATING inside a slot -> tap SPIN to finish it, don't just wait (否則特色只靠遊戲慢速自走,進場卡很久才靠 restart 逃生)
                    burn_spins += 1; d.tap(*(self.lay().get('spin_xy') or (im.shape[1] // 2, int(im.shape[0] * 0.96))))
                    if burn_spins % 8 == 1: log(f'feature animating during entry -> tap SPIN to finish it ({burn_spins}/40)')
                    time.sleep(1.5); continue
                if moving and waits < 6:
                    waits += 1; log('screen changing (loading?) -> wait'); time.sleep(5); continue
                d.back(); act = 'BACK'
            waits = 0
            key = re.sub(r'\d+', lambda m: str(int(m.group()) // 25), re.sub(r'\s\d+\.\d+$', '', act))   # tap spot in 25-px cells, no score
            unchanged = last_sig is not None and abs(sig - last_sig).mean() <= 3
            same_act = same_act + 1 if (key == last_act and unchanged) else 0; last_act = key; last_sig = sig
            m = re.match(r'OCR tap "(.+?)"', act)
            if m and same_act >= 2:                                   # text button tapped 3x without effect -> try others
                skip_labels.add(m.group(1)); log(f'"{m.group(1)}" has no effect -> try other buttons'); same_act = 0
            log('popup:', act); time.sleep(3)
            if key == 'BACK' and same_act >= 4 and sweep_bursts < 3:   # BACK isn't clearing it -> a pick / jackpot bonus (點鑽石湊三個) needs TAPS, not BACK: sweep every cell
                log(f'stuck non-lobby screen -> sweep all cells (pick/jackpot bonus?) burst {sweep_bursts + 1}/3')
                for yy in (0.30, 0.45, 0.58, 0.67, 0.75, 0.83):       # 下三排 = jackpot pick 盤面(y 停在 0.83,押注列 0.88+ 之上)
                    for xx in (0.18, 0.34, 0.50, 0.66, 0.82):
                        d.tap(int(im.shape[1] * xx), int(im.shape[0] * yy)); time.sleep(0.4)
                sweep_bursts += 1; same_act = 0; last_act = last_sig = prev = None; continue   # re-evaluate; if the bonus cleared we reach lobby/game next loop
            if key == 'BACK' and same_act >= 9 and restart_ok and not restarted:   # BACK does nothing here -> start the app fresh (once)
                log('BACK x10 on an unchanged screen -> restart app'); self.restart(); restarted = True
                same_act = 0; skip_labels.clear(); last_act = last_sig = prev = None; waits = 0
        return False

    def rewind(self, max_flings=15):
        """fling the lobby back to its far-left start: repeat until the tiles stop moving (a fixed count is not
        enough when we come back from a slot at the far right end of a long lobby)"""
        self.lobby_tab()
        sc = self.p['lobby_scan']; x0, y0, x1, y1 = sc['region']; prev = None; still = 0
        if self.p.get('catalog'): x0, x1, y0, y1 = self.p['catalog']['region']   # tiles only: side ads keep animating
        y0 = y0 + (y1 - y0) * 2 // 5                                  # lower tile half: jackpot meters on top keep ticking
        for i in range(max_flings):
            for _ in range(4): d.swipe(*sc['rewind_swipe']); time.sleep(0.25)
            time.sleep(1.2); im = d.grab()
            if not self.is_lobby(im): prev = None; still = 0; continue          # a popup covers the lobby
            if sc.get('rewind_by_shift'):                     # animated tiles: measure how far the tiles MOVED instead
                import cv2, numpy as np
                g = cv2.resize(cv2.cvtColor(im[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), None, fx=0.5, fy=0.5).astype(np.float32)
                if prev is not None and getattr(prev, 'shape', None) == g.shape:
                    (dx, dy), resp = cv2.phaseCorrelate(prev, g)
                    still = still + 1 if (abs(dx) < 3 and abs(dy) < 2 and resp > 0.1) else 0   # low response = unrelated frames
                prev = g
                if still >= 2: return True
                continue
            sig = signature(im[y0:y1, x0:x1])
            still = still + 1 if (prev is not None and not changed(prev, sig, 8)) else 0   # a fling moves ~40; banners ~3-6
            prev = sig
            if still >= 1: return True
        log('rewind: lobby still moving after', max_flings * 4, 'flings'); return False

    def find_slot(self, slot_id):
        sc = self.p['lobby_scan']
        if not self.to_lobby(): return None
        self.rewind()
        hint = next((r.get('hint') for r in self.slots() if r['id'] == slot_id), '') or ''
        skip = max(0, int(hint) - 3) if str(hint).isdigit() else 0
        if skip:                                             # far tile: fast-forward without looking (swipes are slow -> exact)
            log(f'fast-forward {skip} steps to {slot_id}')
            for _ in range(skip): check_stop(); d.swipe(*sc["step_swipe"]); time.sleep(0.5)
            time.sleep(1)
        step = 0
        while step < sc['max_steps']:
            step += 1
            if skip and step > 12:                            # hint was wrong (lobby changed) -> search from the start
                log('not near the hint -> full search'); skip = 0; step = 1
                self.rewind()
            check_stop()
            if not self.to_lobby(stable=1): return None
            im = d.grab()
            s, c = self.t.find(im, slot_id, sc['threshold'], region=tuple(sc['region']))
            if c: log(f'slot tile {slot_id} score {s:.2f}'); return c
            d.swipe(*sc['step_swipe']); time.sleep(2.5)
        return None

    def verify_slot(self, slot_id, im):
        """optional identity check with slots/<id>_ingame.png"""
        if not self.t.has(slot_id + '_ingame'): return True
        s, c = self.t.find(im, slot_id + '_ingame', 0.6)
        log(f'verify slot {slot_id}: {s:.2f}'); return c is not None

    def _enter_via(self, steps):
        """Reach a slot that is NOT a lobby-grid tile by a fixed navigation sequence (profile 'enter_via').
        Each step: {"tap":[x,y]} (fixed) or {"template":name,"thresh":0.7,"region":[x0,y0,x1,y1]} (match-and-tap),
        plus optional "wait" seconds. A template that is not found is skipped (best-effort); the caller's in_game
        wait-loop (and the 30 s lobby retry that re-runs this) still guard the result."""
        for s in steps:
            check_stop()
            if s.get('template'):
                _, c = self.t.find(d.grab(), s['template'], s.get('thresh', 0.7),
                                   region=tuple(s['region']) if s.get('region') else None)
                if c: d.tap(*c); log('enter_via', s['template'], c)
                else: log('enter_via', s['template'], 'not found -> skip')
            elif s.get('tap'):
                d.tap(*s['tap']); log('enter_via tap', s['tap'])
            time.sleep(s.get('wait', 2))

    def enter_slot(self, slot_id, timeout=300):
        via = (self.p.get('enter_via') or {}).get(slot_id)
        if via:                                 # slot reached by a fixed sequence (e.g. a tutorial/event-only game), not a grid tile
            if not self.to_lobby(): log('enter_via: lobby not reached'); return False
            self._enter_via(via)
        else:
            c = self.find_slot(slot_id)
            if not c: log('slot not found'); return False
            d.tap(*c)
        t0 = time.time(); lobby_since = None; spin_taps = 0
        while time.time() - t0 < timeout:
            check_stop()
            time.sleep(3); self.guard_focus(); im = d.grab()
            if self.in_game(im):
                if not self.verify_slot(slot_id, im): log('WRONG SLOT'); return False
                log('in game'); return True
            if self.spin_visible(im) and spin_taps < 5:
                d.tap(*self.lay(im)['spin_xy']); spin_taps += 1; log('tap lit SPIN (tutorial)'); continue
            if self.p.get('play_btn'):
                s, pb = self.t.find(im, self.p['play_btn'], 0.75)
                if pb:
                    if self.p.get('before_play_xy'): d.tap(*self.p['before_play_xy']); time.sleep(1.5)   # 包你發: 體驗廳 tab
                    d.tap(*pb); log('tap PLAY'); continue
            if self.p.get('enter_dismiss') and not self.is_lobby(im):     # notices between the tile and the slot
                act = self.dismiss_once(im, allow_back=False)
                if act: log('entering:', act); continue
            s, x = self.find_x(im)
            if x: d.tap(*x); log('popup X', x); continue
            if self.is_lobby(im):               # tap did nothing (slot still downloading?) -> retry after 30 s
                lobby_since = lobby_since or time.time()
                if time.time() - lobby_since > 30:
                    lobby_since = None
                    if via: self._enter_via(via); log('re-run enter_via')
                    else:
                        c = self.find_slot(slot_id)
                        if c: d.tap(*c); log('re-tap slot tile')
            else: lobby_since = None
        return False

    # ---------- meters (balance / bet read by OCR; boxes in profile "meters") ----------
    def read_meter(self, im, name):
        if im is None: return None
        box = (self.lay(im).get('meters') or {}).get(name)
        if not box: return None
        from .ocr import read_box
        return read_box(im, box)

    def read_level(self, im):
        """player level from the 'LVL n' label (profile meters.level for the in-slot portrait screen,
        meters.level_lobby for the landscape lobby). small text -> upscaled 3x + full OCR. -> int or None"""
        import re
        if im.shape[0] > im.shape[1]: box = (self.p.get('meters') or {}).get('level')
        elif self.p.get('landscape') and self.in_game(im): box = (self.p['landscape'].get('meters') or {}).get('level')
        else: box = (self.p.get('meters') or {}).get('level_lobby')
        if not box: return None
        from . import ocr
        x0, y0, x1, y1 = box
        big = cv2.resize(im[y0:y1, x0:x1], None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        txt = ' '.join(t for _, t, p in ocr._read_safe(big) if p > 0.6)
        m = re.search(r'LV\s*L?\s*(\d{1,3})\b', txt.upper()) or re.search(r'^\s*(\d{1,3})\s*$', txt)
        return int(m.group(1)) if m else None

    def _read_bet(self, tries=3):
        for _ in range(tries):
            v = self.read_meter(d.grab(), 'bet')
            if v: return v
            time.sleep(0.7)
        return None

    def min_bet(self):
        """tap bet-minus N times, then confirm with OCR that the bet no longer drops (= minimum).
        Some games wrap from min to max on one more minus -> detected (bet went UP) and undone with bet-plus.
        returns the bet read from screen, or None (no meter box / unreadable)."""
        im = d.grab()
        if not self.in_game(im): return None
        L = self.lay(im)
        if L.get('bet_cycle_xy') or self.p.get('bet_cycle_xy'):
            b = self._min_bet_cycle(L.get('bet_cycle_xy') or self.p['bet_cycle_xy'])
            self.set_turbo(); return b
        for _ in range(self.p.get('bet_minus_taps', 12)): d.tap(*L['bet_minus_xy']); time.sleep(0.4)
        if not (L.get('meters') or {}).get('bet'): return None
        time.sleep(1); a = self._read_bet()
        for _ in range(25):
            if a is None: log('bet: unreadable'); return None
            d.tap(*L['bet_minus_xy']); time.sleep(1.2); b = self._read_bet()
            if b == a: log(f'bet confirmed minimum: {a:,}'); return a
            if b is not None and b > a:                 # wrapped around to max bet
                if L.get('bet_plus_xy'): d.tap(*L['bet_plus_xy']); time.sleep(1.2)
                c = self._read_bet(); log(f'bet wrapped ({b:,}) -> back to {c}'); return c
            a = b
        log('bet: kept dropping after 25 extra taps'); return a

    def set_turbo(self, im=None):
        """profile 'turbo': {'xy': [x, y], 'on': [template, ...]} (+ landscape 'turbo_xy'): the fastest spin-animation
        setting (results are the same, only the animation is shorter). Taps the speed button until an 'on' look shows."""
        cfg = self.p.get('turbo')
        if not cfg or not adv('turbo'): return None
        if time.time() < getattr(self, '_turbo_skip_until', 0): return None        # failed recently -> leave it
        im = im if im is not None else d.grab()
        if not self.in_game(im): return None                                    # popup / lobby: never tap blindly
        xy = self.lay().get('turbo_xy') or cfg['xy']                            # current layout, not re-detected
        x, y = xy; roi = (max(0, x - 70), max(0, y - 50), x + 70, y + 50)
        def look(img, names): return any(self.t.find(img, n, 0.85, region=roi)[1] for n in names)
        for k in range(cfg.get('max_taps', 3) + 1):
            if look(im, cfg['on']): log('turbo on' if k else 'turbo already on'); return True
            if k == cfg.get('max_taps', 3) or not look(im, cfg.get('other', [])) or not self.in_game(im): break
            d.tap(*xy); time.sleep(1.2); im = d.grab()                          # only taps a recognised speed button
        self._turbo_skip_until = time.time() + 1800
        log('turbo: speed button not recognised here -> not touched for 30 min'); return False

    def _min_bet_cycle(self, xy):
        """one bet button that cycles up and wraps to the lowest bet (包你發: 50 -> 100 -> ... -> 1000 -> 50):
        tap until the bet goes DOWN = the minimum"""
        a = self._read_bet(); seen = [a] if a is not None else []; blind = 0 if a is not None else 1
        for _ in range(int(self.p.get('bet_cycle_max', 15))):
            d.tap(*xy); time.sleep(1.2); b = self._read_bet()
            if b is None:
                blind += 1
                if blind >= 3 and not seen: log('bet: unreadable -> not changed further'); return None
                continue
            if a is not None and b < a and b <= min(seen):           # dropped to the lowest value seen = wrapped
                time.sleep(0.8); b2 = self._read_bet()
                if b2 == b: log(f'bet confirmed minimum (wrapped): {b:,}'); return b
                b = b2 if b2 is not None else b                  # misread -> keep cycling
            seen.append(b); a = b
        log('ESCALATE bet: no wrap-around seen (bet may not be the minimum)'); return None

    def level_until_slot(self, slot_id, interval=1.5):
        """new accounts may not list the target slot yet (88F: appears at ~level 6). profile 'leveling':
        {"slot": <starter slot tile template>, "check_from_level": 5, "max_minutes": 40}
        -> spin in the starter slot; from check_from_level on, after every level-up look for slot_id in the lobby.
        returns True when the target tile is visible (caller then enters it)."""
        lv = self.p.get('leveling')
        if not lv: return False
        t0 = time.time(); checked = set(); level = None; last_read = 0; off_since = None
        log(f'leveling: spin in {lv["slot"]} until {slot_id} shows in the lobby')
        if not self.enter_slot(lv['slot']): log('leveling: cannot enter starter slot'); return False
        while time.time() - t0 < lv.get('max_minutes', 40) * 60:
            check_stop()
            im = d.grab()
            if not self.in_game(im):
                off_since = off_since or time.time()
                act = self.dismiss_once(im, allow_back=False)
                if act: log('leveling: popup ->', act)
                elif self.is_lobby(im):
                    if not self.enter_slot(lv['slot']): return False
                elif time.time() - off_since > 10 and im.shape[0] > im.shape[1]:
                    d.tap(im.shape[1] // 2, im.shape[0] // 2)
                time.sleep(3); continue
            off_since = None
            if time.time() - last_read > 20:
                last_read = time.time(); l = self.read_level(im)
                if l and (level is None or level < l <= level + 3): level = l; log(f'leveling: level {level}')
                if level and level >= lv.get('check_from_level', 5) and level not in checked:
                    checked.add(level)
                    if self.find_slot(slot_id): log(f'leveling: {slot_id} unlocked at level {level}'); return True
                    if not self.enter_slot(lv['slot']): return False
                    continue
            for _ in range(3): d.tap(*self.lay(im)['spin_xy']); time.sleep(interval)
        log('leveling: gave up after', lv.get('max_minutes', 40), 'min'); return False

    def reset_new_instance(self):
        """games that tie the guest account to the device (88F: survives pm clear / new androidid / no Google account):
        create a brand-new LDPlayer instance, install the app from apk_cache, switch this process to it.
        The old instance is shut down; it is deleted only if SlotRecorder created it (SR_ name). -> True when in the new lobby"""
        pkg = self.p['package']; old = d.IDX
        if not TOOLS['ldconsole']: log('new-instance reset needs ldconsole.exe'); return False
        if not d.cache_apk(pkg, d.SER): return False
        new = d.new_instance()
        if new is None: return False
        log(f'switching emulator {old} -> {new}')
        d.ld('quit', '--index', str(old)); time.sleep(5)
        d.remove_instance(old)                                 # no-op for the user's own instances
        d.use(new)
        if not d.ensure_running(timeout=360): log('new emulator did not boot'); return False
        if not d.install_cached(pkg, d.SER): return False
        log(f'EMULATOR SWITCHED {old} -> {new}')
        sw = os.environ.get('SLOTREC_SWITCH_FILE')
        if sw:
            with open(sw, 'w') as f: f.write(str(new))
        self.restart(wait=60)
        return self.to_lobby(max_steps=80, restart_ok=False)      # new account: long asset downloads look 'stuck'

    def reset_account(self, level=1):
        """level 1: pm clear (new guest account). level 2: new androidid/imei + emulator reboot, then level 1.
        level 3: brand-new emulator (reset_new_instance) -- last resort when pm clear / new androidid keep coming back
        broke (account bound to the device, a signed-in Google account, or the server, which level 1/2 can't shake off).
        profile reset 'new_instance': always a brand-new emulator (see reset_new_instance)."""
        if self.p.get('reset') == 'new_instance' or level >= 3: return self.reset_new_instance()
        if level >= 2 and not TOOLS['ldconsole']:
            log('WARNING: ldconsole.exe not found (設定 -> LDPlayer 資料夾) -> level-2 reset skipped, doing level 1')
        if level >= 2 and TOOLS['ldconsole']:
            idx = str(d.IDX)
            ld = lambda *a: subprocess.run([TOOLS['ldconsole'], *a], capture_output=True, timeout=120)
            log('reset level 2: new androidid/imei + reboot')
            ld('quit', '--index', idx); time.sleep(8)
            ld('modify', '--index', idx, '--androidid', 'auto', '--imei', 'auto')
            ld('launch', '--index', idx)
            t0 = time.time(); booted = False
            while time.time() - t0 < 240:
                d.connect()
                if (d.adb('shell', 'getprop', 'sys.boot_completed', out=True) or b'').strip() == b'1': booted = True; break
                time.sleep(5)
            if not booted: raise RuntimeError('emulator did not boot within 240 s after level-2 reset')
            time.sleep(15)
        log('pm clear', self.p['package'])
        d.adb('shell', 'pm', 'clear', self.p['package'])
        self.restart(wait=60)
        return self.to_lobby(max_steps=80, restart_ok=False)
