"""Screenshots of a slot's paytable / rules pages (for 玩法拆解), taken once per game.

Inside a slot: tap the profile's paytable.open buttons (templates, e.g. LL: the i button; 88F: menu -> i), then page with
NEXT (found by OCR) until the pages wrap around to the first one or stop changing, then close (EXIT / BACK TO GAME / BACK).
Saved to videos/<app>/paytables/<slot>/01.jpg, 02.jpg ... (not packed, not in git).

usage:  python -m core.paytable --app lightning_link --slot bzzf_golden5 [--emu 0] [--force]   (enters the slot first)
"""
import argparse, glob, os, re, time
import cv2, numpy as np
from . import device as d
from .config import video_dir
from .device import log

NEXT_WORDS = ('NEXT',)
CLOSE_WORDS = ('BACKTOGAME', 'EXIT', 'CLOSE', 'RETURN')

def out_dir(app, slot_id): return os.path.join(video_dir(app.id), 'paytables', slot_id)

def has(app, slot_id):
    return bool(glob.glob(os.path.join(out_dir(app, slot_id), '*.jpg')))

def _find_word(im, words):
    """centre of the first OCR text whose letters contain one of words (lower 45% of the screen first, then all)"""
    from .ocr import _read_safe, _key
    h = im.shape[0]
    for y0 in (int(h * 0.55), 0):
        for b, t, p in _read_safe(im[y0:]):
            k = _key(t)
            if p > 0.5 and any(w in k for w in words) and len(k) <= max(len(w) for w in words) + 4:
                return int(sum(q[0] for q in b) / 4), int(sum(q[1] for q in b) / 4) + y0
    return None

def _diff(a, b):
    h = a.shape[0]
    if a.shape != b.shape: return 255.0
    y0, y1 = int(h * 0.15), int(h * 0.85)                     # top / bottom bars animate (timers, coins)
    return float(np.abs(a[y0:y1].astype(np.int16) - b[y0:y1]).mean())

def capture(app, slot_id, force=False, say=log, max_pages=30):
    """call while inside the slot. -> number of pages saved (0 = already had them / nothing to do, None = failed)"""
    cfg = app.p.get('paytable') or {}
    if not cfg.get('open'): return 0
    if has(app, slot_id) and not force: return 0
    out = out_dir(app, slot_id); os.makedirs(out, exist_ok=True)
    claim = os.path.join(out, '.capturing')                    # several emulators of a group run enter the same slot
    try:
        if os.path.exists(claim) and time.time() - os.path.getmtime(claim) > 600: os.remove(claim)   # left by a crash
        os.close(os.open(claim, os.O_CREAT | os.O_EXCL))
    except OSError: say('paytable: another emulator is capturing this game'); return 0
    try: return _capture(app, out, cfg, force, say, max_pages)
    finally:
        try: os.remove(claim)
        except OSError: pass

def _capture(app, out, cfg, force, say, max_pages):
    im = d.grab()
    if not app.in_game(im): say('paytable: not in the slot'); return None
    tapped = []
    for step in cfg['open']:                                   # e.g. [['btn_menu_ingame', 'btn_menu_ingame_land'], ...]
        names = [step] if isinstance(step, str) else step     # alternatives: portrait / landscape look of the button
        s, c = max((app.t.find(im, nm, cfg.get('threshold', 0.8)) for nm in names), key=lambda r: (r[1] is not None, r[0]))
        if c is None:
            say(f'paytable: button {names[0]} not found ({s:.2f})')
            if tapped: d.tap(*tapped[0]); time.sleep(1)       # close the menu we opened (it toggles)
            return None
        d.tap(*c); tapped.append(c); time.sleep(cfg.get('step_wait', 1.0)); im = d.grab()
    time.sleep(cfg.get('open_wait', 2.5)); first = d.grab()
    if not _find_word(first, NEXT_WORDS):                      # paytable pages always have NEXT; anything else = not opened
        say('paytable: info pages did not open')
        if not app.in_game(first): d.back(); time.sleep(2)
        return None
    if force:
        for f in glob.glob(os.path.join(out, '*.jpg')): os.remove(f)
    pages = [first]; cv2.imwrite(os.path.join(out, '01.jpg'), first, [cv2.IMWRITE_JPEG_QUALITY, 92])
    im = first
    for _ in range(max_pages - 1):
        c = _find_word(im, NEXT_WORDS)
        if not c: break                                        # single page / no NEXT button
        d.tap(*c); time.sleep(cfg.get('page_wait', 2.0)); im = d.grab()
        if _diff(im, first) < 4: break                         # wrapped around to page 1
        if _diff(im, pages[-1]) < 1.5:                         # NEXT did nothing: last page
            time.sleep(1.5); im = d.grab()
            if _diff(im, pages[-1]) < 1.5: break
        pages.append(im); cv2.imwrite(os.path.join(out, f'{len(pages):02d}.jpg'), im, [cv2.IMWRITE_JPEG_QUALITY, 92])
    c = _find_word(im, CLOSE_WORDS)
    if c: d.tap(*c)
    else: d.back()
    time.sleep(2.5)
    if not app.in_game(d.grab()): say('paytable: not back in the game yet (the recorder will handle it)')
    say(f'paytable: {len(pages)} page(s) -> {out}')
    return len(pages)

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--app', required=True); ap.add_argument('--slot', required=True)
    ap.add_argument('--emu', type=int); ap.add_argument('--force', action='store_true')
    a = ap.parse_args()
    if a.emu is not None: d.use(a.emu)
    from .app import App
    app = App(a.app)
    if not d.ensure_running(): raise SystemExit('emulator not available')
    if not app.enter_slot(a.slot): raise SystemExit('cannot enter slot')       # via the lobby: the right game
    print('pages:', capture(app, a.slot, force=a.force))
