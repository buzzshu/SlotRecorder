"""Template matching helpers. Templates are looked up in a list of folders (app tpl/ then app slots/)."""
import os, cv2, numpy as np

def imread(path):
    """cv2.imread that also works for non-ASCII paths (e.g. 中文 folder names)"""
    try: return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    except (OSError, ValueError): return None

def imwrite(path, im):
    """cv2.imwrite that also works for non-ASCII paths. returns True on success"""
    try:
        ok, buf = cv2.imencode(os.path.splitext(path)[1] or '.png', im)
        if ok: buf.tofile(path)
        return bool(ok)
    except (OSError, cv2.error): return False

class Templates:
    def __init__(self, *dirs):
        self.dirs = dirs; self._c = {}
    def path(self, name):
        n = name if name.endswith('.png') else name + '.png'
        for d in self.dirs:
            p = os.path.join(d, n)
            if os.path.exists(p): return p
        return None
    def get(self, name):
        if name not in self._c:
            p = self.path(name)
            self._c[name] = imread(p) if p else None
        return self._c[name]
    def has(self, name): return self.path(name) is not None
    def clear(self): self._c = {}

    def find(self, img, name, thr=0.85, region=None):
        """-> (score, (cx, cy) | None)"""
        t = self.get(name)
        if t is None: return 0.0, None
        x0 = y0 = 0
        if region:
            x0, y0, x1, y1 = region; img = img[y0:y1, x0:x1]
        if t.shape[0] > img.shape[0] or t.shape[1] > img.shape[1]: return 0.0, None
        r = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx < thr: return mx, None
        return mx, (x0 + loc[0] + t.shape[1] // 2, y0 + loc[1] + t.shape[0] // 2)

    def find_lit(self, img, name, thr=0.85, bright=0.85, region=None):
        """like find, but the matched patch must be as bright as the template (i.e. not dimmed by a popup overlay)"""
        s, c = self.find(img, name, thr, region)
        if c is None: return s, None
        t = self.get(name); h, w = t.shape[:2]
        patch = img[c[1]-h//2:c[1]-h//2+h, c[0]-w//2:c[0]-w//2+w]
        ratio = patch.mean() / max(t.mean(), 1)
        return (s, c) if ratio >= bright else (s * ratio, None)

    def find_x(self, img, names, thr=0.82, full=False):
        """popup close (X) buttons, multi-scale, matched in GRAYSCALE so differently coloured X's of the same shape
        still match (shared library core/x_library + the app's own). default area: upper-right 40 %; full=True: whole screen (use thr>=0.9).
        X buttons dimmed by an overlay (= belonging to a screen *behind* the active popup) are rejected."""
        best = (0.0, None)
        H, W = img.shape[:2]
        x0 = 0 if full else W * 45 // 100
        top = int(H * 0.4)                       # upper 40 % (landscape 288 px, portrait 512 px)
        roi = img if full else img[0:min(top, H), x0:W]
        g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        for n in names:
            t0 = self.get(n)
            if t0 is None: continue
            t0 = cv2.cvtColor(t0, cv2.COLOR_BGR2GRAY)
            for s in (0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.35):
                t = cv2.resize(t0, None, fx=s, fy=s)
                if t.shape[0] > g.shape[0] or t.shape[1] > g.shape[1]: continue
                r = cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED)
                _, mx, _, loc = cv2.minMaxLoc(r)
                if mx > best[0]:
                    best = (mx, (x0 + loc[0] + t.shape[1]//2, loc[1] + t.shape[0]//2))
                    patch = g[loc[1]:loc[1] + t.shape[0], loc[0]:loc[0] + t.shape[1]]
                    bright = patch.mean() / max(t.mean(), 1)     # < 1 when an overlay dims this X
        if best[0] < thr: return best[0], None
        if bright < 0.75: return best[0], None
        return best

def signature(im):
    """tiny grayscale thumbnail used to tell 'frozen' screens from moving ones"""
    return cv2.resize(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), (36, 64)).astype('int16')

def changed(a, b, thr=1.5):
    return a is None or b is None or a.shape != b.shape or abs(a - b).mean() > thr
