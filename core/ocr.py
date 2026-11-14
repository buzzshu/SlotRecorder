"""OCR fallback: find a *safe* dismiss/continue button by its text. Never taps purchase / ad / exit buttons.
Backend: rapidocr-onnxruntime (light, CPU). Falls back to easyocr if that is what is installed."""
import re

SAFE = ['TAP TO CONTINUE', 'SKIP ALL', 'CONTINUE', 'FINISH', 'CLAIM ALL', 'CLAIM', 'COLLECT ALL', 'GREAT', 'OK', 'OKAY', 'GOT IT',
        'DONE', 'NEXT', 'MAYBE LATER', 'LATER', 'NO THANKS', 'NOT NOW', 'CLOSE', 'SKIP', 'COOL', 'AWESOME',
        "LET'S GO", 'LETS GO', 'GO GO GO', 'END TUTORIAL', 'SKIP TUTORIAL', 'START', 'COLLECT', 'UPGRADE']   # priority order: earlier wins
REFUSE = {'NO THANKS', 'MAYBE LATER', 'NOT NOW', 'LATER', 'CLOSE', 'END TUTORIAL', 'SKIP TUTORIAL'}   # only these on a page that shows DANGER text
# "$4.99" / "$5" = price -> danger;  "$500,000M" jackpot meter and "UNLOCK AT LEVEL 30" banner are not (88F shows them in every slot)
DANGER = re.compile(r'\$\s?\d{1,4}(?:[.,]\d\d)?\b(?![.,]?\d|[KMBT])|\$\s*$|USD|BUY|UNLOCK(?!\s*AT\s*(?:LEVEL|LVL))|PURCHASE|SHOP|WATCH|PAY|PRICE|SUBSCRIBE|TRIAL|OFFER|VIP|INSTALL|QUIT|EXIT|'
                    r'LOG ?OUT|SIGN ?OUT|DELETE|\d+[.,]\d\d\b|\bS\d')
_BASE = list(SAFE)
_SAFE_KEY = {s.replace(' ', '').replace("'", ''): s for s in SAFE}
def set_extra(words):
    """extra safe button words from an app profile (ocr_safe_extra), lowest priority"""
    global SAFE, _SAFE_KEY
    SAFE = _BASE + [w.upper() for w in words if w.upper() not in _BASE]
    _SAFE_KEY = {re.sub(r'[^A-Z]', '', s): s for s in SAFE}

_engine = None
_broken = False
def _read(im):
    """-> list of (box[4 points], text, confidence)"""
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            r = RapidOCR(); _engine = ('rapid', r)
        except ImportError:
            import easyocr
            _engine = ('easy', easyocr.Reader(['en'], gpu=True, verbose=False))
    kind, eng = _engine
    if kind == 'rapid':
        res, _ = eng(im)
        return [(b, t, float(p)) for b, t, p in (res or [])]
    return [(b, t, float(p)) for b, t, p in eng.readtext(im)]

def _read_safe(im):
    global _broken
    if _broken: return []
    try: return _read(im)
    except Exception as e:
        _broken = True
        try: print('OCR unavailable:', repr(e), flush=True)
        except OSError: pass
        return []

def available():
    try: _read(__import__('numpy').zeros((32, 32, 3), 'uint8')); return True
    except Exception: return False

def _key(t): return re.sub(r'[^A-Z]', '', t.upper())

def _pick(res, exclude=()):
    """choose the best safe button among OCR results (coords in the image that was read).
    exclude: labels already tapped without effect (e.g. a button behind a dialog) -> try the next one"""
    res = [(b, t, p) for b, t, p in res if p > 0.4]
    def centre(b): return (int(sum(q[0] for q in b) / 4), int(sum(q[1] for q in b) / 4))
    danger = [centre(b) for b, t, _ in res if DANGER.search(t.upper())]
    cands = []
    for b, t, p in res:
        if re.search(r'\d', t): continue                  # misread prices ("COLLECT 4.99") never qualify
        label = _SAFE_KEY.get(_key(t))
        if not label or label in exclude: continue
        if danger and label not in REFUSE: continue       # purchase / ad / exit page: only explicit "no" buttons
        c = centre(b)
        if any(abs(c[0] - dx) < 120 and abs(c[1] - dy) < 50 for dx, dy in danger): continue   # text sits on a paid button
        cands.append((SAFE.index(label), c, label))
    if not cands: return None, None, bool(danger)
    _, c, label = min(cands)
    return c, label, bool(danger)

LAST_DANGER = False
def safe_button(im, exclude=()):
    """-> ((x, y), label) of the best safe button, or (None, None).
    Portrait screens are also read rotated 90 deg both ways: some games show landscape popups on a portrait screen."""
    global LAST_DANGER
    c, label, danger = _pick(_read_safe(im), exclude); LAST_DANGER = bool(danger and not c)
    if c or danger or im.shape[0] <= im.shape[1]: return c, label
    import cv2
    H, W = im.shape[:2]
    for rot in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE):
        c, label, danger = _pick(_read_safe(cv2.rotate(im, rot)), exclude)
        if danger: LAST_DANGER = not c
        if danger and not c: return None, None
        if c:
            x, y = c        # map back to the original (portrait) coordinates
            return ((y, H - 1 - x) if rot == cv2.ROTATE_90_CLOCKWISE else (W - 1 - y, x)), label
    return None, None

# ---------------- numbers (balance / bet / win meters) ----------------
_SUFFIX = {'K': 10**3, 'M': 10**6, 'B': 10**9, 'T': 10**12}
_FIX = str.maketrans({'O': '0', 'o': '0', 'D': '0', 'Q': '0', 'I': '1', 'l': '1', '|': '1', 'S': '5', 's': '5', 'B': '8', 'Z': '2'})

def parse_number(t):
    """'90,414,000,000' -> 90414000000 ; '1.25M' -> 1250000 ; garbage -> None.
    Thousands separators are checked (groups of 3) so a dropped / extra digit is rejected instead of misread."""
    t = re.sub(r'^[^0-9OoIlSZDQ]+|[^0-9OoIlSZDQKMBT]+$', '', t.strip().replace(' ', '').replace('$', ''))   # stray arrows / icons at the ends
    m = re.fullmatch(r'([0-9OoIl|SsZDQ.,]+?)([KMBT])?', t)
    if not m: return None
    real = sum(ch.isdigit() for ch in m.group(1)); fixed = sum(ch.isalpha() or ch == '|' for ch in m.group(1))
    if real == 0 or fixed * 3 > real: return None           # mostly letters -> not a number ('Coarwa' -> 0 is wrong)
    body, suf = m.group(1).translate(_FIX), m.group(2)
    if suf:
        try: return int(round(float(body.replace(',', '')) * _SUFFIX[suf]))
        except ValueError: return None
    body = body.replace('.', ',')                      # OCR often turns ',' into '.'; meters show whole coins
    parts = body.split(',')
    if not parts[0] or not parts[0].isdigit() or len(parts[0]) > 3 and len(parts) > 1: return None
    if any(len(p) != 3 or not p.isdigit() for p in parts[1:]): return None
    return int(''.join(parts))

def read_number(crop, min_conf=0.6):
    """read one meter (crop = BGR or gray box around the digits only) -> int or None. ~10 ms (recognition only)"""
    global _engine
    if _broken or crop is None or crop.size == 0: return None
    try:
        if _engine is None: _read_safe(__import__('numpy').zeros((32, 32, 3), 'uint8'))
        if _engine is None or _engine[0] != 'rapid': return None
        if crop.ndim == 2:
            import cv2; crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)
        res, _ = _engine[1](crop, use_det=False, use_cls=False, use_rec=True)
    except Exception: return None
    if not res: return None
    t, p = res[0][0], float(res[0][1])
    return parse_number(t) if p >= min_conf else None

def read_box(im, box, min_conf=0.6):
    """box = [x0, y0, x1, y1] in screen coordinates (portrait game layout)"""
    if box is None or im is None: return None
    x0, y0, x1, y1 = box
    if im.shape[0] < y1 or im.shape[1] < x1: return None
    return read_number(im[y0:y1, x0:x1], min_conf)

if __name__ == '__main__':
    import sys, cv2
    for f in sys.argv[1:]: print(f, safe_button(cv2.imread(f)))
