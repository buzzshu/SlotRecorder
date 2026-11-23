"""Overview of every analysed recording, per game: hours, spins, RTP (95% range), how many spins are still missing.

usage:  python -m core.overview [--app lightning_link] [--no-excel] [--no-pdf]
output: videos/總覽.html (+ .pdf)          all apps, one row per game; newest report linked
        videos/overview.json             the same numbers for the GUI (勾選樣本不足)
        videos/<app>/<app>_統計.xlsx      one sheet per game (every spin) + 總表; RTP / hit rate / distribution are live formulas
Rebuilt automatically after every analysis / merge. Videos are grouped by the slot id in <video>_stats.json; old videos
without one are matched by the name in the file name.
"""
import argparse, glob, html, json, math, os, re, shutil, subprocess, sys, tempfile, time
import numpy as np
from .config import CFG, ROOT, video_dir
from .analyze import spin_stats, load_spins, hms, fmt, BUCKETS

_NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
TARGET = lambda: int(CFG.get('target_spins') or 1000)

def videos_root():
    d = CFG.get('video_dir') or 'videos'
    return d if os.path.isabs(d) else os.path.join(ROOT, d)

def _slots(app_id):
    import csv
    p = os.path.join(ROOT, 'apps', app_id, 'slots.csv')
    if not os.path.exists(p): return []
    with open(p, encoding='utf-8') as f: return list(csv.DictReader(f))

def _norm(s): return re.sub(r'[^0-9a-z]', '', str(s).lower())

def collect(app_id):
    """-> {slot_key: game dict} from every per-video _summary.json of this app"""
    vdir = video_dir(app_id); rows = _slots(app_id)
    by_name = {_norm(r['name']): r['id'] for r in rows}; names = {r['id']: r['name'] for r in rows}
    games = {}
    for js in sorted(glob.glob(os.path.join(vdir, '*_summary.json'))):
        if js.endswith('_merged_summary.json'): continue
        base = js[:-len('_summary.json')]
        if not os.path.exists(base + '_spins.tsv'): continue
        try:
            with open(js, encoding='utf-8') as f: s = json.load(f)
        except (OSError, ValueError): continue
        st = {}
        if os.path.exists(base + '_stats.json'):
            try:
                with open(base + '_stats.json', encoding='utf-8') as f: st = json.load(f)
            except (OSError, ValueError): pass
        fname = re.sub(r'^\d{8}_\d{6}_|(_PARTIAL|_e\d+)+$', '', os.path.basename(base))
        key = st.get('slot') or by_name.get(_norm(fname)) or fname
        m = re.match(r'(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d)', os.path.basename(base))
        when = f'{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}' if m else ''
        g = games.setdefault(key, {'id': key, 'name': names.get(key) or st.get('name') or fname, 'videos': [], 'spins': [],
                                   'feat_x': [], 'feat_known': False})
        fs = s.get('features')
        if fs is not None:
            g['feat_known'] = True; g['feat_x'] += [x['x'] for x in fs.get('list') or []]
            g['feat_spins'] = g.get('feat_spins', 0) + s.get('spins', 0)
        sp = load_spins(base)
        g['videos'].append({'video': os.path.basename(base) + '.mp4', 'when': when, 'play_hours': s.get('play_hours', 0),
                            'video_s': st.get('video_s') or _secs(s.get('video_length')), 'spins': len(sp), 'rtp': s.get('rtp'), 'bet': s.get('bet'),
                            'read': s.get('meter_read_rate'), 'emulator': st.get('emulator'), 'html': base + '_summary.html'})
        g['spins'] += [(os.path.basename(base) + '.mp4', t, b, w) for t, b, w in sp]
    for g in games.values():
        n = len(g['spins'])
        g['play_hours'] = sum(v['play_hours'] or 0 for v in g['videos'])
        g['video_hours'] = sum(v['video_s'] for v in g['videos']) / 3600
        g['last'] = max(v['when'] for v in g['videos'])
        g['report'] = max(g['videos'], key=lambda v: v['when'])['html']
        if n:
            core, _ = spin_stats([w for *_, w in g['spins']], [b for _, _, b, _ in g['spins']])
            g.update({k: core[k] for k in ('rtp', 'rtp_95', 'hit_rate', 'hit_every_n', 'total_bet', 'total_win')})
        g['n'] = n
        g['need'] = max(0, TARGET() - n)
        k = len(g['feat_x'])
        g['feat_n'] = k if g['feat_known'] else None
        g['feat_every'] = round(g.get('feat_spins', n) / k, 1) if k else None
        g['feat_avg_x'] = round(sum(g['feat_x']) / k, 1) if k else None
        hours = g['video_hours'] if g['video_hours'] > 0 else g['play_hours']     # incl. popups / lobby: real recording time
        sph = n / hours if hours > 0 and n else None
        g['spins_per_hour'] = round(sph) if sph else None
        g['est_minutes'] = math.ceil(g['need'] / sph * 60) if (sph and g['need']) else (0 if not g['need'] else None)
    return games

def _secs(hms_):
    try: h, m, x = map(int, str(hms_).split(':')); return h * 3600 + m * 60 + x
    except ValueError: return 0

def _atomic_write(path, text):
    tmp = path + f'.tmp{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f: f.write(text)
    for k in range(10):                                        # Windows: a reader may hold the file for a moment
        try: os.replace(tmp, path); return
        except PermissionError: time.sleep(0.5)
    os.remove(tmp); raise PermissionError(f'{path} is locked')

class _Lock:
    """one overview build at a time (several jobs finish analyses together); a dead holder's lock is taken over"""
    def __init__(self, path): self.path = path
    def __enter__(self):
        from .device import pid_alive
        t0 = time.time()
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.write(fd, str(os.getpid()).encode()); os.close(fd); return self
            except FileExistsError:
                try:
                    with open(self.path) as f: pid = f.read().strip()
                except OSError: pid = ''
                if (pid.isdigit() and not pid_alive(int(pid))) or time.time() - t0 > 300:
                    try: os.remove(self.path)
                    except OSError: pass
                time.sleep(1)
    def __exit__(self, *a):
        try: os.remove(self.path)
        except OSError: pass

# ---------------- HTML ----------------
def _feat_cell(g):
    if g.get('feat_n') is None: return '—'
    if not g['feat_n']: return '0 次'
    return f"每 {g['feat_every']} 局 / 平均 {g['feat_avg_x']} 倍"

def render(all_games, target):
    e = html.escape; pct = lambda x: '—' if x is None else f'{x:.1%}'
    secs = []
    for app_id, (app_name, games, n_slots) in all_games.items():
        rows = []
        for g in sorted(games.values(), key=lambda g: (g['need'] == 0, -g['n'])):
            lo, hi = g.get('rtp_95') or (None, None)
            need = '✔ 已達標' if not g['need'] else f"差 {g['need']:,} 局" + (f"（約再錄 {g['est_minutes']} 分鐘）" if g['est_minutes'] else '')
            link = os.path.relpath(g['report'], videos_root()).replace(os.sep, '/')
            rows.append(f"<tr class='{'ok' if not g['need'] else 'low'}'><td>{e(g['name'])}</td><td>{len(g['videos'])}</td>"
                        f"<td>{g['video_hours']:.2f}</td><td>{g['play_hours']:.2f}</td><td>{g['n']:,}</td><td class=b>{pct(g.get('rtp'))}</td>"
                        f"<td>{pct(lo)} ~ {pct(hi)}</td><td>{'每 %s 把' % g['hit_every_n'] if g.get('hit_every_n') else '—'}</td>"
                        f"<td>{_feat_cell(g)}</td><td>{e(g['last'])}</td><td>{need}</td><td><a href='{e(link)}'>最新報告</a></td></tr>")
        tot_h = sum(g['play_hours'] for g in games.values()); tot_n = sum(g['n'] for g in games.values())
        done = sum(1 for g in games.values() if not g['need'])
        secs.append(f"""<h2>{e(app_name)}</h2>
<p>已錄 {len(games)} 款 / 清單共 {n_slots} 款；達到 {target:,} 局的 {done} 款；遊戲中合計 {tot_h:.1f} 小時、{tot_n:,} 局。
Excel：<a href='{e(app_id)}/{e(app_id)}_統計.xlsx'>{e(app_id)}_統計.xlsx</a></p>
<table><tr><th>遊戲</th><th>影片</th><th>影片時數</th><th>遊戲中時數</th><th>局數</th><th>RTP</th><th>95% 範圍</th><th>中獎率</th><th>特色遊戲</th><th>最近錄影</th><th>樣本</th><th>報告</th></tr>
{''.join(rows) or '<tr><td colspan=12>還沒有分析過的影片</td></tr>'}</table>""")
    return f"""<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>錄影總覽</title>
<style>@page{{size:A4 landscape;margin:10mm}}body{{font-family:"Microsoft JhengHei",sans-serif;margin:24px;color:#222;font-size:13px}}
table{{border-collapse:collapse;margin:8px 0 24px;page-break-inside:avoid}}td,th{{border:1px solid #ccc;padding:4px 8px;text-align:right;white-space:nowrap}}
th{{background:#f3f3f3}}td:first-child,th:first-child{{text-align:left}}.b{{font-weight:bold}}tr.low td:nth-child(11){{color:#b45309}}
tr.ok td:nth-child(11){{color:#15803d}}</style>
<h1>錄影總覽</h1><p>更新時間 {time.strftime('%Y-%m-%d %H:%M')}｜每款目標 {target:,} 局（局數少於目標時 RTP 範圍很寬，僅供參考）。
樣本不足的排在前面；在 SlotRecorder 按「勾選樣本不足」可以一鍵勾選並設好補錄分鐘數。</p>
{''.join(secs)}
<p style="color:#888">RTP＝總贏分 ÷ 總押注（同一款的所有影片合計）；95% 範圍以每把重抽樣估計。
特色遊戲＝自動偵測（某把贏分後超過 25 秒沒扣押注、畫面仍在遊戲中），「—」＝影片是舊版分析、還沒偵測過（按 📊 重新分析即可）。</p></html>"""

# ---------------- Excel (live formulas) ----------------
def _ensure_openpyxl(say):
    try:
        import openpyxl; return openpyxl
    except ImportError:
        say('installing openpyxl (needed for the Excel file)...')
        subprocess.run([sys.executable.replace('pythonw.exe', 'python.exe'), '-m', 'pip', 'install', '--disable-pip-version-check', '-q', 'openpyxl'],
                       creationflags=_NOWIN, timeout=300)
        import openpyxl; return openpyxl

def _sheet_name(name, used):
    s = re.sub(r"[\[\]:*?/\\']", '_', name)[:28] or 'game'
    k, base = 2, s
    while s.lower() in used: s = f'{base[:26]}_{k}'; k += 1
    used.add(s.lower()); return s

def write_excel(app_id, app_name, games, path, say=print):
    openpyxl = _ensure_openpyxl(say)
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = '總表'
    head = ['遊戲', '局數', '總押注', '總贏分', 'RTP', '中獎率', '每 N 把中一次', '遊戲中時數', '每小時局數', '影片數', '最近錄影',
            '目標局數', '還差局數', 'RTP 95% 下限（程式算）', 'RTP 95% 上限（程式算）', '工作表',
            '特色次數（程式偵測）', '每 N 局觸發一次', '特色平均倍數（押注）']
    ws.append(head); bold = Font(bold=True)
    for c in ws[1]: c.font = bold; c.fill = PatternFill('solid', fgColor='F3F3F3')
    ws['U1'] = '目標局數'; ws['V1'] = TARGET(); ws['U1'].font = bold                     # one cell -> change it, column M follows
    used = {'總表'}
    for r, g in enumerate(sorted(games.values(), key=lambda g: g['name']), start=2):
        sn = _sheet_name(g['name'], used); sh = wb.create_sheet(sn); q = f"'{sn}'"
        sh.append(['影片', '影片時間', '押注', '贏分', '押注倍數'])
        for c in sh[1]: c.font = bold
        for i, (v, t, b, w) in enumerate(g['spins'], start=2):
            sh.append([v, t, b, w, f'=IF(C{i}>0,D{i}/C{i},0)'])
        # distribution (live): count + share of RTP per win-size bucket, from column E
        sh['G1'] = '贏分（押注倍數）'; sh['H1'] = '下限'; sh['I1'] = '上限'; sh['J1'] = '次數'; sh['K1'] = '頻率（每 N 把）'; sh['L1'] = '佔 RTP'
        for c in ('G1', 'H1', 'I1', 'J1', 'K1', 'L1'): sh[c].font = bold
        for k, (lo, hi, label) in enumerate(BUCKETS, start=2):
            sh[f'G{k}'] = label
            if hi == 0:
                sh[f'J{k}'] = '=COUNTIF(D:D,0)'; sh[f'L{k}'] = 0
            else:                                                      # lut half-open (lo, hi]: lower exclusive, upper inclusive
                sh[f'H{k}'] = lo; sh[f'I{k}'] = hi if hi < 1e17 else 1e18
                sh[f'J{k}'] = f'=COUNTIFS(E:E,">"&H{k},E:E,"<="&I{k},D:D,">0")'
                sh[f'L{k}'] = f'=SUMIFS(D:D,E:E,">"&H{k},E:E,"<="&I{k},D:D,">0")/SUM(C:C)'
            sh[f'K{k}'] = f'=IF(J{k}>0,COUNT(C:C)/J{k},"—")'
            sh[f'L{k}'].number_format = '0.0%'; sh[f'K{k}'].number_format = '0.0'
        sh.column_dimensions['A'].width = 42; sh.column_dimensions['G'].width = 16
        lo, hi = g.get('rtp_95') or (None, None)
        ws.append([g['name'], f'=COUNT({q}!C:C)', f'=SUM({q}!C:C)', f'=SUM({q}!D:D)', f'=IF(C{r}>0,D{r}/C{r},0)',
                   f'=IF(B{r}>0,COUNTIF({q}!D:D,">0")/B{r},0)', f'=IF(F{r}>0,1/F{r},"—")', round(g['play_hours'], 3),
                   f'=IF(H{r}>0,B{r}/H{r},"—")', len(g['videos']), g['last'], '=$V$1', f'=MAX(0,L{r}-B{r})', lo, hi, sn,
                   g.get('feat_n'), f'=IF(N(Q{r})>0,B{r}/Q{r},"—")', g.get('feat_avg_x')])
        for col, fmtx in (('C', '#,##0'), ('D', '#,##0'), ('E', '0.0%'), ('F', '0.0%'), ('G', '0.00'), ('I', '0'), ('N', '0.0%'), ('O', '0.0%')):
            ws[f'{col}{r}'].number_format = fmtx
    for col, w in zip('ABCDEFGHIJKLMNOPQRS', (40, 9, 16, 16, 8, 8, 12, 10, 10, 7, 17, 9, 9, 12, 12, 30, 10, 12, 12)): ws.column_dimensions[col].width = w
    ws.freeze_panes = 'B2'
    tmp = os.path.join(os.path.dirname(path), f'~tmp{os.getpid()}.xlsx'); wb.save(tmp)
    try: os.replace(tmp, path)
    except PermissionError:
        os.remove(tmp); say(f'{os.path.basename(path)} is open in Excel -> not updated this time'); return None
    return path

# ---------------- PDF (headless Edge / Chrome) ----------------
def _browser():
    for p in (CFG.get('browser'), r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
              r'C:\Program Files\Microsoft\Edge\Application\msedge.exe', r'C:\Program Files\Google\Chrome\Application\chrome.exe',
              os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe')):
        if p and os.path.exists(p): return p
    return shutil.which('msedge') or shutil.which('chrome')

def to_pdf(html_path, say=print):
    """<x>.html -> <x>.pdf next to it (tables are kept on one page by their CSS). -> pdf path or None"""
    b = _browser(); html_path = os.path.abspath(html_path)        # the browser resolves relative paths in its own folder
    if not b or not os.path.exists(html_path): return None
    from pathlib import Path
    out = html_path[:-5] + '.pdf'; prof = tempfile.mkdtemp(prefix='slotrec_pdf_')
    try:
        subprocess.run([b, '--headless', '--disable-gpu', '--no-pdf-header-footer', f'--user-data-dir={prof}',
                        f'--print-to-pdf={out}', Path(html_path).resolve().as_uri()], creationflags=_NOWIN, timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e: say(f'PDF failed: {e!r}'); return None
    finally: shutil.rmtree(prof, ignore_errors=True)
    return out if os.path.exists(out) and os.path.getsize(out) > 0 else None

# ---------------- build ----------------
def build(app_ids=None, excel=True, pdf=True, say=print):
    """rebuild 總覽.html / overview.json (all apps) and the Excel of app_ids (default: all). -> html path"""
    os.makedirs(videos_root(), exist_ok=True)
    with _Lock(os.path.join(videos_root(), '.overview.lock')):            # collect + write as one step
        return _build(app_ids, excel, pdf, say)

def _build(app_ids, excel, pdf, say):
    from .app import list_apps
    apps = list_apps(); all_games = {}; data = {}
    for aid, name in apps:
        games = collect(aid); all_games[aid] = (name, games, len(_slots(aid)))
        data[aid] = {k: {'name': g['name'], 'spins': g['n'], 'need': g['need'], 'est_minutes': g['est_minutes'],
                         'rtp': g.get('rtp'), 'play_hours': round(g['play_hours'], 3), 'last': g['last']} for k, g in games.items()}
        if excel and (app_ids is None or aid in app_ids) and games:
            try:
                p = write_excel(aid, name, games, os.path.join(video_dir(aid), f'{aid}_統計.xlsx'), say)
                if p: say(f'Excel -> {p}')
            except Exception as e: say(f'Excel failed for {aid}: {e!r}')
    root = videos_root(); os.makedirs(root, exist_ok=True)
    _atomic_write(os.path.join(root, 'overview.json'), json.dumps({'target': TARGET(), 'updated': time.strftime('%F %T'), 'apps': data},
                                                                    ensure_ascii=False, indent=1))
    h = os.path.join(root, '總覽.html'); _atomic_write(h, render(all_games, TARGET()))
    if pdf and CFG.get('auto_pdf', True): to_pdf(h, say)
    say(f'OVERVIEW -> {h}')
    return h

def safe_build(app_ids=None, say=print):
    """called after analyses: never let the overview break an analysis"""
    try: return build(app_ids, say=say)
    except Exception as e: say(f'overview not updated: {e!r}'); return None

if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--app', action='append'); ap.add_argument('--no-excel', action='store_true')
    ap.add_argument('--no-pdf', action='store_true')
    a = ap.parse_args()
    build(a.app, excel=not a.no_excel, pdf=not a.no_pdf)
