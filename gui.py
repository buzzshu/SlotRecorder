"""SlotRecorder GUI — pick an app + emulator, tick slots, press 開始錄製. Several emulators can record at the same time.
Also: add a slot (crop its lobby tile), add popup close-button / button templates, schedule, analyse videos, pack the tool."""
import json, os, queue, re, shutil, subprocess, sys, threading, time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT); sys.path.insert(0, ROOT)
from core.config import CFG, TOOLS, video_dir, save as save_cfg, load as load_cfg
from core.app import App, list_apps, X_LIB
from core.vision import imwrite, imread
from core import device as d
from core.version import VERSION

STOP_ALL = d.STOP_ALL
MAX_MULTI = 6                       # 同時用 N 台: upper limit of the spinbox
MAX_REPEAT = 20                     # 重複 N 次: upper limit of the repeat spinbox
STARTUP_LNK = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup', 'SlotRecorder.lnk')
LOG_DIR = os.path.join(ROOT, 'logs'); os.makedirs(LOG_DIR, exist_ok=True)
NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
PY = sys.executable.replace('pythonw.exe', 'python.exe')
STATE_ZH = {'paytable': '截賠率表', 'entering': '進入 slot', 'game': '遊戲中', 'popup': '關彈窗中', 'stuck': '卡住處理中', 'lobby': '回大廳重進',
            'reset': '金幣用完→重置帳號', 'finishing': '收尾合併影片'}

def slugify(s):
    s = re.sub(r'[^0-9A-Za-z]+', '_', s).strip('_').lower()
    return s or time.strftime('slot_%H%M%S')

def last_job_path(idx): return os.path.join(LOG_DIR, f'last_job_e{idx}.json')

class Gui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'SlotRecorder v{VERSION} — 自動錄製 slot 影片')
        self.geometry('1250x760'); self.minsize(1000, 600)
        self.q = queue.Queue(); self.apps = list_apps(); self.jobs = {}     # emulator index -> job dict
        self.groups = {}                                                    # multi-emulator run id -> {'app', 'idxs', 'merged'}
        self.devlock = threading.Lock()
        self._build(); self.after(150, self._poll); self.after(1000, self._tick)
        self.after(2000, self._check_interrupted)
        self.protocol('WM_DELETE_WINDOW', self._on_close)

    # ---------------- layout ----------------
    def _build(self):
        top = ttk.Frame(self, padding=8); top.pack(fill='x')
        ttk.Label(top, text='App：').pack(side='left')
        self.app_var = tk.StringVar()
        self.app_box = ttk.Combobox(top, state='readonly', width=26, textvariable=self.app_var, values=[f'{n}  ({i})' for i, n in self.apps])
        self.app_box.pack(side='left'); self.app_box.bind('<<ComboboxSelected>>', lambda e: self._load_slots())
        ttk.Label(top, text='  模擬器：').pack(side='left')
        self.emu_var = tk.StringVar(); self.emu_box = ttk.Combobox(top, state='readonly', width=18, textvariable=self.emu_var)
        self.emu_box.pack(side='left'); self._load_emus()
        ttk.Button(top, text='↻', width=3, command=self._load_emus).pack(side='left', padx=(2, 8))
        for text, cmd in (('環境檢查', self._env_check), ('看畫面', self._preview), ('準備模擬器', self._prepare_emu),
                          ('設定', self._settings), ('打包帶走', self._pack)):
            ttk.Button(top, text=text, command=cmd).pack(side='left', padx=2)
        self.status = ttk.Label(top, text='待命', foreground='#555'); self.status.pack(side='right')

        body = ttk.PanedWindow(self, orient='horizontal'); body.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        left = ttk.Frame(body); body.add(left, weight=1)
        ttk.Label(left, text='Slot 清單（雙擊「錄」切換勾選、雙擊「分鐘」「名稱」修改；依大廳順序）').pack(anchor='w')
        flt = ttk.Frame(left); flt.pack(fill='x', pady=(2, 2))
        ttk.Label(flt, text='搜尋：').pack(side='left')
        self.q_var = tk.StringVar(); self.q_var.trace_add('write', lambda *a: self._filter())
        ttk.Entry(flt, textvariable=self.q_var, width=22).pack(side='left')
        self.only_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(flt, text='只看已勾選', variable=self.only_on, command=self._filter).pack(side='left', padx=6)
        ttk.Button(flt, text='勾選樣本不足', command=self._pick_low).pack(side='left')
        self.count_lbl = ttk.Label(flt, text='', foreground='#555'); self.count_lbl.pack(side='right')
        tf = ttk.Frame(left); tf.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(tf, columns=('on', 'name', 'min', 'id'), show='headings', height=12)
        for c, t, w in (('on', '錄', 40), ('name', '名稱', 230), ('min', '分鐘', 50), ('id', '縮圖 id', 120)):
            self.tree.heading(c, text=t); self.tree.column(c, width=w, anchor='center' if c in ('on', 'min') else 'w')
        tsb = ttk.Scrollbar(tf, command=self.tree.yview); self.tree.configure(yscrollcommand=tsb.set)
        tsb.pack(side='right', fill='y'); self.tree.pack(fill='both', expand=True)
        self.tree.bind('<Double-1>', self._tree_dbl); self.tree.bind('<<TreeviewSelect>>', lambda e: self._show_thumb())
        self.thumb = ttk.Label(left); self.thumb.pack(anchor='w', pady=(4, 0))
        bt = ttk.Frame(left); bt.pack(fill='x', pady=(6, 2))
        self.btn_add = ttk.Button(bt, text='＋ 新增 slot', command=self._add_slot); self.btn_add.pack(side='left')
        self.btn_del = ttk.Button(bt, text='刪除', command=self._del_slot); self.btn_del.pack(side='left', padx=4)
        self.btn_test = ttk.Button(bt, text='測試尋找', command=self._test_find); self.btn_test.pack(side='left')
        self.btn_scan = ttk.Button(bt, text='🔍 掃描大廳找新遊戲', command=self._scan_catalog); self.btn_scan.pack(side='left', padx=4)
        ttk.Button(bt, text='📖 賠率表', command=self._paytable).pack(side='left')
        bt2 = ttk.LabelFrame(left, text='卡在新彈窗？把它的 X / 按鈕加成範本（錄製中也可以加，馬上生效）', padding=4); bt2.pack(fill='x', pady=4)
        ttk.Button(bt2, text='＋ 從目前畫面框選', command=lambda: self._add_tpl(False)).pack(side='left')
        ttk.Button(bt2, text='＋ 從截圖檔框選', command=lambda: self._add_tpl(True)).pack(side='left', padx=4)

        right = ttk.Frame(body); body.add(right, weight=2)
        run = ttk.Frame(right); run.pack(fill='x')
        self.btn_start = ttk.Button(run, text='▶ 開始錄製', command=self._start); self.btn_start.pack(side='left')
        ttk.Label(run, text=' 同時用').pack(side='left')
        self.n_var = tk.IntVar(value=1)
        ttk.Spinbox(run, from_=1, to=MAX_MULTI, width=3, textvariable=self.n_var, state='readonly').pack(side='left')
        ttk.Label(run, text='台，重複').pack(side='left')
        self.r_var = tk.IntVar(value=1)
        ttk.Spinbox(run, from_=1, to=MAX_REPEAT, width=3, textvariable=self.r_var, state='readonly').pack(side='left')
        ttk.Label(run, text='次').pack(side='left', padx=(0, 6))
        self.precise_var = tk.BooleanVar(value=bool(CFG.get('precise_default', False)))
        ttk.Checkbutton(run, text='夠準就停', variable=self.precise_var).pack(side='left', padx=(0, 6))
        ttk.Button(run, text='⏰ 定時開始', command=self._schedule).pack(side='left', padx=4)
        ttk.Button(run, text='↻ 接續上次', command=self._resume).pack(side='left')
        ttk.Button(run, text='■ 停止此台', command=self._stop).pack(side='left', padx=(12, 4))
        ttk.Button(run, text='■ 全部停止', command=self._stop_all).pack(side='left')
        ttk.Button(run, text='開啟影片資料夾', command=self._open_videos).pack(side='right')
        ttk.Button(run, text='📊 分析影片', command=self._analyze).pack(side='right', padx=4)
        ttk.Button(run, text='📋 總覽', command=self._overview).pack(side='right')
        ttk.Label(right, text='錄製工作（每台模擬器一列；選一列再按「停止此台」）').pack(anchor='w', pady=(8, 0))
        cols = (('emu', '模擬器', 110), ('app', 'App', 110), ('cur', '目前', 210), ('prog', '進度', 60), ('state', '狀態', 130),
                ('pop', '彈窗', 50), ('off', '非遊戲', 60), ('live', '即時統計', 190), ('note', '備註', 160))
        self.jt = ttk.Treeview(right, columns=[c for c, _, _ in cols], show='headings', height=4)
        for c, t, w in cols: self.jt.heading(c, text=t); self.jt.column(c, width=w, anchor='w')
        self.jt.pack(fill='x')
        self.bar = ttk.Progressbar(right, maximum=100); self.bar.pack(fill='x', pady=(4, 0))
        self.logbox = tk.Text(right, height=18, wrap='none', font=('Consolas', 9))
        sb = ttk.Scrollbar(right, command=self.logbox.yview); self.logbox.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y'); self.logbox.pack(fill='both', expand=True, pady=(6, 0))
        if self.apps: self.app_box.current(0); self._load_slots()

    # ---------------- helpers ----------------
    def app(self):
        if not self.app_var.get(): return None
        return App(self.apps[self.app_box.current()][0])
    def emu(self):
        i = self.emu_box.current()
        return self.emus[i][0] if 0 <= i < len(self.emus) else int(CFG.get('ld_index', 0))
    def emu_name(self, idx):
        for i, n, _ in self.emus:
            if i == idx: return f'{i} {n}'
        return str(idx)
    def _load_emus(self):
        cur = self.emu_var.get()
        self.emus = d.instances() or [(int(CFG.get('ld_index', 0)), 'LDPlayer', True)]
        vals = [f'{i} {n}' + ('' if run else '（未開）') for i, n, run in self.emus]
        self.emu_box['values'] = vals
        names = [v.split('（')[0] for v in vals]
        if cur and cur.split('（')[0] in names: self.emu_box.current(names.index(cur.split('（')[0]))
        elif vals:
            main = [k for k, (i, _, _) in enumerate(self.emus) if i == int(CFG.get('ld_index', 0))]
            self.emu_box.current(main[0] if main else 0)
    def log(self, s):
        self.logbox.insert('end', s.rstrip() + '\n'); self.logbox.see('end')
    def _load_slots(self):
        for i in getattr(self, '_all_ids', []):
            if self.tree.exists(i): self.tree.delete(i)
        self.tree.delete(*self.tree.get_children()); a = self.app(); self._all_ids = []
        if not a: return
        for r in a.slots():
            self.tree.insert('', 'end', iid=r['id'], values=('✓' if r.get('enabled', '1') == '1' else '', r['name'], r['minutes'], r['id']))
            self._all_ids.append(r['id'])
        self._filter()
    def _filter(self):
        """search box / 只看已勾選: detach rows that do not match (they stay in the list and are still saved)"""
        if not hasattr(self, '_all_ids'): return
        q = self.q_var.get().strip().lower().replace(' ', '_'); n = 0
        for k, i in enumerate(self._all_ids):
            if not self.tree.exists(i): continue
            show = (not q or q in self.tree.set(i, 'name').lower() or q in i) and (not self.only_on.get() or self.tree.set(i, 'on'))
            if show: self.tree.reattach(i, '', k); n += 1
            else: self.tree.detach(i)
        on = sum(1 for i in self._all_ids if self.tree.exists(i) and self.tree.set(i, 'on'))
        self.count_lbl['text'] = f'顯示 {n} / 共 {len(self._all_ids)} 款，已勾 {on}'
    def _save_tree(self):
        a = self.app(); old = {r['id']: r for r in a.slots()}          # keep columns the list does not show (hint)
        rows = [dict(old.get(i, {}), id=i, name=self.tree.set(i, 'name'), minutes=self.tree.set(i, 'min'),
                     enabled='1' if self.tree.set(i, 'on') else '0') for i in self._all_ids if self.tree.exists(i)]
        a.save_slots(rows); self._filter()
    def _show_thumb(self):
        sel = self.tree.selection(); a = self.app()
        if not sel or not a: return
        p = a.t.path(sel[0])
        if not p: self.thumb.configure(image='', text='（沒有縮圖）'); return
        import cv2
        from PIL import Image, ImageTk
        im = imread(p); h, w = im.shape[:2]; s = 170 / h
        ph = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(cv2.resize(im, (max(1, int(w * s)), 170)), cv2.COLOR_BGR2RGB)))
        self.thumb.configure(image=ph, text=''); self.thumb.image = ph
    def _locked(self):
        if getattr(self, 'scanning', None) is not None:
            messagebox.showinfo('掃描中', '正在掃描大廳、更新清單，完成後再修改。', parent=self); return True
        return False
    def _tree_dbl(self, e):
        if self._locked(): return
        iid = self.tree.identify_row(e.y); col = self.tree.identify_column(e.x)
        if not iid: return
        if col == '#1': self.tree.set(iid, 'on', '' if self.tree.set(iid, 'on') else '✓')
        elif col == '#3':
            v = simpledialog.askinteger('分鐘', '錄幾分鐘？', initialvalue=int(float(self.tree.set(iid, 'min') or 60)), minvalue=1, maxvalue=600, parent=self)
            if v: self.tree.set(iid, 'min', v)
        elif col == '#2':
            v = simpledialog.askstring('名稱', '遊戲名稱（中英數字；中文名稱的影片檔名會用代號）', initialvalue=self.tree.set(iid, 'name'), parent=self)
            if v: self.tree.set(iid, 'name', re.sub(r'[^0-9A-Za-z_\-一-鿿()]+', '_', v))
        self._save_tree()
    def _bg(self, fn, *a):
        threading.Thread(target=fn, args=a, daemon=True).start()
    def _on_dev(self, idx, fn):
        """run fn() in a thread with the device module pointed at emulator idx; print() inside goes to the log"""
        import io, contextlib
        def wrapped():
            class Wr(io.TextIOBase):
                def write(_, s):
                    if s.strip(): self.q.put(('log', s))
                    return len(s)
            with self.devlock, contextlib.redirect_stdout(Wr()):
                try: d.use(idx); fn()
                except Exception as e: self.q.put(('log', f'錯誤：{e!r}'))
        self._bg(wrapped)
    def _alive(self, j): return bool(j and j.get('proc') and j['proc'].poll() is None)
    def running(self, idx):
        """emulator idx is busy: a lobby scan, a job started on it or moved to it, or reserved by a multi-run being set up"""
        if getattr(self, 'scanning', None) == idx: return True                  # lobby scan uses that emulator
        j = self.jobs.get(idx)
        if j and (j.get('prep') or self._alive(j)): return True
        return any(self._alive(o) and o.get('on') == idx for o in self.jobs.values())
    def _busy(self):
        if self.running(self.emu()):
            messagebox.showinfo('錄製中', f'模擬器 {self.emu_name(self.emu())} 正在錄製，這個動作會干擾錄影。請選別台或先停止。', parent=self); return True
        return False

    # ---------------- environment ----------------
    def _env_check(self):
        idx = self.emu(); a = self.app(); self.log(f'--- 環境檢查（SlotRecorder v{VERSION}，模擬器 {self.emu_name(idx)}）---')
        def work():
            ok = True
            def say(good, msg):
                nonlocal ok; ok &= good; print(('✔ ' if good else '✘ ') + msg)
            for k in ('adb', 'scrcpy', 'ffmpeg', 'ffprobe'): say(bool(TOOLS[k]), f'{k}: {TOOLS[k] or "找不到（執行 install.bat 或在 設定 指定路徑）"}')
            say(bool(TOOLS['ldconsole']), f'LDPlayer ldconsole: {TOOLS["ldconsole"] or "找不到（設定 → LDPlayer 資料夾）"}')
            con = TOOLS['adb'] and d.connect()
            say(bool(con), f'模擬器連線 {d.SER}' + ('' if con else '：連不到。按「準備模擬器」會自動開機並打開 ADB 本地連接'))
            if con and a:
                sz = d.screen_size(); want = tuple(a.p['screen'])
                say(sz is not None and sorted(sz) == sorted(want), f'解析度 {sz}，需要 {want[0]}x{want[1]}（LDPlayer 設定 → 進階設定 → 解析度，DPI 240）')
                say(d.installed(a.p['package']), f'{a.p["name"]} 已安裝' + ('' if d.installed(a.p['package']) else '（按「準備模擬器」可從別台複製）'))
            try:
                from core import ocr; say(ocr.available(), 'OCR 可用')
            except Exception as e: say(False, f'OCR 無法載入：{e}')
            vd = CFG.get('video_dir') or 'videos'; vd = vd if os.path.isabs(vd) else os.path.join(ROOT, vd); os.makedirs(vd, exist_ok=True)
            say(d.free_disk_gb(vd) > 20, f'影片資料夾剩餘空間 {d.free_disk_gb(vd):.0f} GB（每小時約 1.2 GB）')
            say(d.free_ram_gb() > 3, f'記憶體剩餘 {d.free_ram_gb():.1f} GB（每多錄一台模擬器約需 3~4 GB）')
            print('環境檢查：' + ('全部通過' if ok else '有項目需要處理'))
        self._on_dev(idx, work)

    def _prepare_emu(self):
        """open ADB local connection + boot + copy the app from another emulator if missing"""
        idx = self.emu(); a = self.app()
        if self.running(idx): messagebox.showinfo('準備模擬器', '這台正在錄製中', parent=self); return
        if not messagebox.askokcancel('準備模擬器', f'模擬器 {self.emu_name(idx)}：\n1. 若 ADB 本地連接沒開，會先關閉模擬器、打開設定再重開\n'
                                      f'2. 開機\n3. 若沒裝 {a.p["name"] if a else "app"}，從其他已開機的模擬器複製安裝', parent=self): return
        self.log(f'--- 準備模擬器 {self.emu_name(idx)} ---')
        def work():
            if not d.connect():
                d.enable_adb(idx, print)
            print('開機中…' if not d.booted() else '已開機')
            if not d.ensure_running(): print('✘ 模擬器開不起來'); return
            print(f'✔ 已連線 {d.SER}')
            if a and not d.installed(a.p['package']):
                src = None
                for i, n, run in d.instances():
                    if i == idx: continue
                    from core.config import serial_for
                    s = serial_for(i)
                    if d.installed(a.p['package'], serial=s): src = s; break
                if not src: print(f'✘ 找不到已安裝 {a.p["name"]} 的其他模擬器（需要開著），請在這台的 Play 商店手動安裝'); return
                print(f'從 {src} 複製 {a.p["name"]}…（約 1~3 分鐘）')
                print('✔ 安裝完成' if d.copy_app(a.p['package'], src, d.SER, print) else '✘ 安裝失敗')
            elif a: print(f'✔ {a.p["name"]} 已安裝')
            self.q.put(('emus', None))
        self._on_dev(idx, work)

    def _set_startup(self, on):
        """shortcut to run.bat in the user's Startup folder (no admin rights needed)"""
        try:
            if not on:
                os.remove(STARTUP_LNK); self.log('已取消開機自動開啟'); return
            ps = ('$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SR_LNK); $s.TargetPath=$env:SR_TARGET; '
                  '$s.WorkingDirectory=$env:SR_DIR; $s.WindowStyle=7; $s.Description="SlotRecorder"; $s.Save()')
            subprocess.run(['powershell', '-NoProfile', '-Command', ps], creationflags=NOWIN, timeout=60,
                           env=dict(os.environ, SR_LNK=STARTUP_LNK, SR_TARGET=os.path.join(ROOT, 'run.bat'), SR_DIR=ROOT))
            self.log('✔ 已設定開機登入後自動開啟 SlotRecorder' if os.path.exists(STARTUP_LNK) else '✘ 設定開機自動開啟失敗')
        except Exception as e: self.log(f'設定開機自動開啟失敗：{e!r}')

    # ---------------- crash / reboot: resume jobs that were cut off ----------------
    def _interrupted(self):
        """jobs whose batch process vanished without writing why it ended (= GUI / PC crash, reboot)"""
        out = []
        for f in sorted(os.listdir(LOG_DIR)):
            m = re.fullmatch(r'last_job_e(\d+)\.json', f)
            if not m: continue
            idx = int(m.group(1))
            try:
                with open(os.path.join(LOG_DIR, f), encoding='utf-8') as fh: j = json.load(fh)
                jp = os.path.join(LOG_DIR, f'job_{j["job"]}.json')
                with open(jp, encoding='utf-8') as fh: st = json.load(fh)
            except (OSError, ValueError, KeyError): continue
            if 'pid' not in st or st.get('finished') or self.running(idx): continue
            if os.path.exists(d.stop_file(idx)) or os.path.exists(STOP_ALL): continue      # user pressed stop, PC went down first
            try: started = time.mktime(time.strptime(st.get('started', ''), '%Y-%m-%d %H:%M:%S'))
            except ValueError: started = 0
            if started >= d.boot_time() and d.pid_alive(st['pid']): continue              # still running (pid not reused)
            left = [s for s in j['slots'] if st.get('status', {}).get(s, {}).get('result') != 'done']
            if left: out.append((idx, j, jp, left))
        return out
    def _check_interrupted(self):
        try: todo = self._interrupted()
        except Exception as e: self.log(f'檢查中斷的工作失敗：{e!r}'); return
        if not todo: return
        lines = [f'模擬器 {self.emu_name(i)}：{dict(self.apps).get(j["app"], j["app"])}，還有 {len(left)} 款沒錄完'
                 + (f'（{j.get("split")} 台同時錄製之一）' if j.get('group') else '') for i, j, _, left in todo]
        self.log('發現被中斷的錄製工作（當機或重開機）：\n  ' + '\n  '.join(lines))
        win = tk.Toplevel(self); win.title('接續中斷的錄製'); win.attributes('-topmost', True)
        ttk.Label(win, text='上次的錄製被中斷了（當機或重開機）：\n\n' + '\n'.join(lines) + '\n\n已錄完的遊戲會跳過。', padding=12).pack()
        cd = ttk.Label(win, padding=(12, 0)); cd.pack()
        state = {'left': 60, 'done': False}
        def go():
            if state['done']: return
            state['done'] = True; win.destroy()
            for i, j, _, _ in todo:
                self.log(f'--- 自動接續 {j["job"]}（模擬器 {self.emu_name(i)}）---')
                self._launch(i, j['app'], j['slots'], j['job'], int(j.get('split') or 1), j.get('group') or '', bool(j.get('precise')))
        def cancel():
            state['done'] = True; win.destroy()
            for _, _, jp, _ in todo:                        # do not ask again (↻ 接續上次 still works)
                try:
                    with open(jp, encoding='utf-8') as fh: st = json.load(fh)
                    st['finished'] = 'dismissed'
                    with open(jp, 'w', encoding='utf-8') as fh: json.dump(st, fh, indent=1, ensure_ascii=False)
                except (OSError, ValueError): pass
            self.log('已取消自動接續（之後仍可按「↻ 接續上次」）')
        def tick():
            if state['done']: return
            if state['left'] <= 0: go(); return
            cd['text'] = f'{state["left"]} 秒後自動接續'; state['left'] -= 1; self.after(1000, tick)
        bb = ttk.Frame(win, padding=12); bb.pack()
        ttk.Button(bb, text='立即接續', command=go).pack(side='left', padx=4)
        ttk.Button(bb, text='取消（這次不接續）', command=cancel).pack(side='left', padx=4)
        win.protocol('WM_DELETE_WINDOW', cancel); tick()

    def _preview(self):
        def work(): self.q.put(('preview', d.grab()))
        self._on_dev(self.emu(), work)
    def _show_image(self, im, title='模擬器畫面'):
        import cv2
        from PIL import Image, ImageTk
        h, w = im.shape[:2]; s = min(900 / w, 640 / h)
        img = Image.fromarray(cv2.cvtColor(cv2.resize(im, (int(w * s), int(h * s))), cv2.COLOR_BGR2RGB))
        win = tk.Toplevel(self); win.title(title); ph = ImageTk.PhotoImage(img)
        lb = ttk.Label(win, image=ph); lb.image = ph; lb.pack()

    def _settings(self):
        cfg = load_cfg()
        win = tk.Toplevel(self); win.title('設定'); win.grab_set()
        fields = [('ldplayer_dir', 'LDPlayer 資料夾（含 ldconsole.exe）'), ('ld_index', '主要模擬器編號（多開器裡的 index）'),
                  ('serial', '主要模擬器 adb 序號'), ('video_dir', '影片輸出資料夾'), ('spin_interval', 'Spin 間隔（秒）'),
                  ('adb', 'adb.exe（空白=自動）'), ('scrcpy', 'scrcpy.exe（空白=自動）'), ('ffmpeg', 'ffmpeg.exe（空白=自動）'),
                  ('target_spins', '每款目標局數（總覽 / 勾選樣本不足用，預設 1000）'),
                  ('precise_pp', '夠準就停：RTP 95% 範圍 ± 幾個百分點（預設 10）'), ('precise_max_minutes', '夠準就停：每款最多錄幾分鐘（預設 240）')]
        vars_ = {}
        for i, (k, label) in enumerate(fields):
            ttk.Label(win, text=label).grid(row=i, column=0, sticky='w', padx=8, pady=3)
            v = tk.StringVar(value=str(cfg.get(k, ''))); vars_[k] = v
            ttk.Entry(win, textvariable=v, width=60).grid(row=i, column=1, padx=8)
        aa = tk.BooleanVar(value=bool(cfg.get('auto_analyze', True)))
        ttk.Checkbutton(win, text='每支影片錄完自動產出統計（RTP、中獎率…）', variable=aa).grid(row=len(fields), column=1, sticky='w', padx=8)
        ni = tk.BooleanVar(value=bool(cfg.get('new_instance_reset', True)))
        ttk.Checkbutton(win, text='88F 這類換不了帳號的 app 沒錢時：自動建一台全新模擬器接著錄（約 15 分鐘；不勾 = 停止錄影）',
                        variable=ni).grid(row=len(fields) + 1, column=1, sticky='w', padx=8)
        tb = tk.BooleanVar(value=bool(cfg.get('turbo', True)))
        ttk.Checkbutton(win, text='有速度鍵的遊戲（包你發）自動切到最快轉速（結果不變，每小時局數約 2 倍）',
                        variable=tb).grid(row=len(fields) + 2, column=1, sticky='w', padx=8)
        su = tk.BooleanVar(value=os.path.exists(STARTUP_LNK))
        ttk.Checkbutton(win, text='開機登入 Windows 後自動開啟 SlotRecorder（當機 / 重開機中斷的錄影會自動接續）',
                        variable=su).grid(row=len(fields) + 3, column=1, sticky='w', padx=8)
        def ok():
            if su.get() != os.path.exists(STARTUP_LNK): self._set_startup(su.get())
            new = dict(cfg)
            for k, v in vars_.items():
                val = v.get().strip()
                try:
                    if k == 'ld_index': val = int(val)
                    elif k in ('precise_pp', 'precise_max_minutes'):
                        val = float(val or (10 if k == 'precise_pp' else 240))
                        if not (1 <= val <= 50 if k == 'precise_pp' else 10 <= val <= 1440): raise ValueError
                    elif k == 'target_spins':
                        val = int(val or 1000)
                        if not 100 <= val <= 100000: raise ValueError
                    elif k == 'spin_interval':
                        val = float(val)
                        if not 0.3 <= val <= 10: raise ValueError
                except ValueError:
                    messagebox.showwarning('設定', f'「{dict(fields)[k]}」需要是數字（Spin 間隔 0.3~10 秒；目標局數 100~100000；'
                                           f'夠準就停 ±1~50、10~1440 分鐘）', parent=win); return
                if k in ('ldplayer_dir', 'serial') and not val:
                    messagebox.showwarning('設定', f'「{dict(fields)[k]}」不能空白', parent=win); return
                new[k] = val
            new['auto_analyze'] = aa.get(); new['new_instance_reset'] = ni.get(); new['turbo'] = tb.get()
            save_cfg(new); win.destroy()
            messagebox.showinfo('設定', '已儲存。請重新開啟 SlotRecorder 讓設定生效。', parent=self)
        ttk.Button(win, text='儲存', command=ok).grid(row=len(fields) + 4, column=1, sticky='e', padx=8, pady=8)

    # ---------------- crop dialog (shared) ----------------
    def _crop_window(self, im, title, min_px):
        import cv2
        from PIL import Image, ImageTk
        h, w = im.shape[:2]; s = min(960 / w, 640 / h)
        img = Image.fromarray(cv2.cvtColor(cv2.resize(im, (int(w * s), int(h * s))), cv2.COLOR_BGR2RGB))
        win = tk.Toplevel(self); win.title(title); win.grab_set()
        ph = ImageTk.PhotoImage(img); cv = tk.Canvas(win, width=img.width, height=img.height, cursor='cross')
        cv.pack(); cv.create_image(0, 0, image=ph, anchor='nw'); cv.image = ph
        sel = {'x0': 0, 'y0': 0, 'box': None}
        def down(e): sel['x0'], sel['y0'] = e.x, e.y; cv.delete('sel')
        def drag(e): cv.delete('sel'); cv.create_rectangle(sel['x0'], sel['y0'], e.x, e.y, outline='#ff2', width=2, tags='sel')
        def up(e):
            x0, x1 = sorted((sel['x0'], e.x)); y0, y1 = sorted((sel['y0'], e.y))
            x0, x1 = max(0, min(x0, img.width)), max(0, min(x1, img.width)); y0, y1 = max(0, min(y0, img.height)), max(0, min(y1, img.height))
            sel['box'] = (int(x0 / s), int(y0 / s), int(x1 / s), int(y1 / s))
        cv.bind('<ButtonPress-1>', down); cv.bind('<B1-Motion>', drag); cv.bind('<ButtonRelease-1>', up)
        bar = ttk.Frame(win, padding=6); bar.pack(fill='x')
        def get_box():
            if not sel['box']: messagebox.showwarning(title, '請先在圖上拖曳框選', parent=win); return None
            x0, y0, x1, y1 = sel['box']
            if x1 - x0 < min_px or y1 - y0 < min_px: messagebox.showwarning(title, f'框太小了（至少 {min_px}×{min_px} 像素）', parent=win); return None
            return sel['box']
        return win, bar, get_box

    # ---------------- add / delete / test slot ----------------
    def _add_slot(self):
        if self._busy(): return
        if not self.app(): return
        if not messagebox.askokcancel('新增 slot', '1. 在模擬器裡把大廳滑到「看得到要錄的 slot 縮圖」的位置\n'
                                     '2. 按「確定」擷取畫面\n3. 在圖上拖曳框住那個 slot 的縮圖（框內圖案就好，不用含邊框）\n4. 輸入名稱並儲存', parent=self):
            return
        self._on_dev(self.emu(), lambda: self.q.put(('crop', d.grab())))

    def _crop_dialog(self, im):
        a = self.app()
        win, bar, get_box = self._crop_window(im, '拖曳框住 slot 縮圖', 60)
        ttk.Label(bar, text='名稱：').pack(side='left'); name = tk.StringVar(); ttk.Entry(bar, textvariable=name, width=30).pack(side='left')
        ttk.Label(bar, text='分鐘：').pack(side='left', padx=(10, 0)); mins = tk.StringVar(value='60'); ttk.Entry(bar, textvariable=mins, width=6).pack(side='left')
        def save():
            box = get_box()
            if not box: return
            x0, y0, x1, y1 = box
            if not name.get().strip(): messagebox.showwarning('新增 slot', '請輸入名稱', parent=win); return
            try:
                m = float(mins.get() or 60)
                if not 1 <= m <= 600: raise ValueError
            except ValueError: messagebox.showwarning('新增 slot', '分鐘要是 1~600 的數字', parent=win); return
            sid = slugify(name.get()); path = os.path.join(a.dir, 'slots', sid + '.png')
            if os.path.exists(os.path.join(a.dir, 'tpl', sid + '.png')):
                messagebox.showwarning('新增 slot', f'「{sid}」跟介面範本同名，請換個名稱', parent=win); return
            if os.path.exists(path) and not messagebox.askyesno('新增 slot', f'{sid} 已存在，要覆蓋嗎？', parent=win): return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not imwrite(path, im[y0:y1, x0:x1]): messagebox.showerror('新增 slot', f'存檔失敗：{path}', parent=win); return
            rows = [r for r in a.slots() if r['id'] != sid]
            rows.append({'id': sid, 'name': re.sub(r'[^0-9A-Za-z_\-]+', '_', name.get().strip()), 'minutes': str(int(m)) if m == int(m) else str(m), 'enabled': '1'})
            a.save_slots(rows); self._load_slots(); win.destroy()
            self.log(f'已新增 slot：{sid}。建議按「測試尋找」確認程式找得到它。')
        ttk.Button(bar, text='儲存', command=save).pack(side='right')

    def _del_slot(self):
        if self._locked(): return
        sel = self.tree.selection()
        if not sel or not messagebox.askyesno('刪除', f'刪除 {", ".join(sel)}？\n（之後「掃描大廳找新遊戲」也不會再把它加回來）', parent=self): return
        a = self.app()
        from core.catalog import ignore_tile
        for i in sel:
            p = os.path.join(a.dir, 'slots', i + '.png')
            if os.path.exists(p): ignore_tile(a, imread(p), i)            # a later lobby scan will not add it back
            self.tree.delete(i)
        self._all_ids = [i for i in self._all_ids if i not in sel]
        self._save_tree()

    def _scan_catalog(self):
        """walk the whole lobby, add tiles that are not in the list yet (unticked, OCR-guessed names)"""
        if self._busy() or self._locked(): return
        a = self.app(); idx = self.emu()
        if not a: return
        if not messagebox.askokcancel('掃描大廳找新遊戲', f'在模擬器 {self.emu_name(idx)} 把 {a.p["name"]} 的大廳從頭滑到尾（約 5~8 分鐘），\n'
                                      '清單裡還沒有的遊戲會自動加入（不勾選；名字是讀圖猜的，可雙擊「名稱」修改）。\n期間請不要操作這台模擬器。', parent=self): return
        self.log(f'--- 掃描 {a.p["name"]} 大廳（模擬器 {self.emu_name(idx)}）---')
        self.scanning = idx; self.btn_scan['state'] = 'disabled'
        def work():
            rc = -1
            try:
                p = subprocess.Popen([PY, '-u', '-m', 'core.catalog', '--app', a.id, '--emu', str(idx), '--add-new'], cwd=ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NOWIN, env=dict(os.environ, PYTHONIOENCODING='utf-8'))
                for raw in p.stdout: self.q.put(('log', raw.decode('utf-8', 'replace')))
                rc = p.wait()
            except Exception as e: self.q.put(('log', f'掃描無法啟動：{e!r}'))
            self.q.put(('reload', rc))
        self._bg(work)

    def _test_find(self):
        if self._busy(): return
        sel = self.tree.selection(); a = self.app()
        if not sel or not a: messagebox.showinfo('測試尋找', '先在清單選一個 slot', parent=self); return
        sid = sel[0]; self.log(f'--- 測試尋找 {sid}（模擬器 {self.emu_name(self.emu())}）：會自動關彈窗並左右滑動大廳 ---')
        def work():
            import cv2
            d.connect(); c = a.find_slot(sid)
            if c:
                im = d.grab(); cv2.circle(im, c, 40, (0, 255, 255), 6)
                print(f'✔ 找到 {sid}，位置 {c}'); self.q.put(('preview', im))
            else: print(f'✘ 找不到 {sid}。請重新框選縮圖（框小一點、只框圖案）')
        self._on_dev(self.emu(), work)

    # ---------------- popup templates (#X library / app buttons) ----------------
    def _add_tpl(self, from_file):
        if not self.app(): return
        if from_file:
            f = filedialog.askopenfilename(title='選一張卡住時的截圖（例如影片旁 _snaps 資料夾裡的 esc_ / off_ 開頭圖檔）',
                                           initialdir=video_dir(self.app().id), filetypes=[('PNG', '*.png'), ('圖片', '*.png;*.jpg')], parent=self)
            if not f: return
            im = imread(f)
            if im is None: messagebox.showerror('新增範本', '讀不到這張圖', parent=self); return
            self._tpl_dialog(im)
        else:
            self.log(f'擷取模擬器 {self.emu_name(self.emu())} 畫面…')
            self._on_dev(self.emu(), lambda: self.q.put(('tpl', d.grab())))

    def _tpl_dialog(self, im):
        a = self.app()
        win, bar, get_box = self._crop_window(im, '框住要點的 X 或按鈕（貼著邊框就好）', 16)
        kind = tk.StringVar(value='x')
        ttk.Radiobutton(bar, text='X 關閉鈕（所有 app 共用）', variable=kind, value='x').pack(side='left')
        ttk.Radiobutton(bar, text=f'按鈕（只用在 {a.p["name"]}）', variable=kind, value='btn').pack(side='left', padx=8)
        ttk.Label(bar, text='名稱：').pack(side='left'); name = tk.StringVar(); ttk.Entry(bar, textvariable=name, width=18).pack(side='left')
        ttk.Label(win, text='⚠ 不要框購買、看廣告、付費解鎖的按鈕。按鈕請框「文字＋按鈕底色」，X 請框整顆 X（含外圈）。', foreground='#b00').pack(anchor='w', padx=6)
        def save():
            box = get_box()
            if not box: return
            x0, y0, x1, y1 = box; crop = im[y0:y1, x0:x1]
            nm = slugify(name.get() or time.strftime('%m%d%H%M%S'))
            if kind.get() == 'x':
                nm = nm if nm.startswith('x_') else 'x_' + nm
                path = os.path.join(X_LIB, nm + '.png')
            else:
                nm = nm if nm.startswith('btn_') else 'btn_' + nm
                path = os.path.join(a.dir, 'tpl', nm + '.png')
            if os.path.exists(path) and not messagebox.askyesno('新增範本', f'{nm} 已存在，要覆蓋嗎？', parent=win): return
            if not imwrite(path, crop): messagebox.showerror('新增範本', f'存檔失敗：{path}', parent=win); return
            if kind.get() == 'btn':
                pp = os.path.join(a.dir, 'profile.json'); p = json.load(open(pp, encoding='utf-8'))
                lst = p.setdefault('dismiss_templates', [])
                if not any(n == nm for n, _ in lst): lst.append([nm, 0.85])
                with open(pp, 'w', encoding='utf-8') as f: json.dump(p, f, indent=2, ensure_ascii=False)
            win.destroy()
            # immediate check: does the engine find it on this very screenshot?
            a2 = self.app()
            if kind.get() == 'x':
                s, c = a2.find_x(im)
                if not c: s, c = a2.find_x(im, full=True)
            else: s, c = a2.t.find(im, nm, 0.85)
            cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
            hit = c is not None and abs(c[0] - cx) < 30 and abs(c[1] - cy) < 30
            self.log(f'已新增範本 {nm} → {path}\n' + (f'✔ 測試：程式在這張圖上找得到它（相似度 {s:.2f}）。錄製中的工作 5 秒內就會開始用。' if hit else
                     f'⚠ 測試：程式在這張圖上優先找到別的位置 {c}（{s:.2f}），新範本可能沒被用到。可以框緊一點再試。'))
        ttk.Button(bar, text='儲存', command=save).pack(side='right')

    # ---------------- run / schedule / stop ----------------
    def _checked(self):
        return [i for i in self._all_ids if self.tree.exists(i) and self.tree.set(i, 'on')]   # incl. rows hidden by the search
    def _preflight(self, idx, minutes):
        """#10: enough RAM / disk before starting another emulator job? -> True to go on"""
        probs = []
        on = any(i == idx and run for i, _, run in d.instances()) if TOOLS['ldconsole'] else True
        need_ram = 2.5 if on else 5.0
        if d.free_ram_gb() < need_ram: probs.append(f'記憶體只剩 {d.free_ram_gb():.1f} GB（{"已開機" if on else "要開機"}的模擬器建議至少 {need_ram:.0f} GB）')
        vd = video_dir(self.app().id); need = minutes / 60 * 1.5 + 1
        if d.free_disk_gb(vd) < need: probs.append(f'硬碟只剩 {d.free_disk_gb(vd):.1f} GB，這次約需 {need:.0f} GB')
        if not probs: return True
        return messagebox.askyesno('資源不足', '\n'.join(probs) + '\n\n記憶體不足時錄影可能被系統中止。仍要開始嗎？', parent=self)
    def _start(self, at=None):
        a = self.app(); idx = self.emu()
        if not a: return
        if self.running(idx): messagebox.showinfo('開始錄製', f'模擬器 {self.emu_name(idx)} 已經在錄製中。要同時錄，請在上方換一台模擬器。', parent=self); return
        self._save_tree(); ids = self._checked()
        if not ids: messagebox.showinfo('開始錄製', '請先勾選要錄的 slot（雙擊「錄」欄）', parent=self); return
        total = sum(float(self.tree.set(i, 'min') or 60) for i in ids)
        n = max(1, min(MAX_MULTI, int(self.n_var.get() or 1)))
        r = max(1, min(MAX_REPEAT, int(self.r_var.get() or 1)))
        if n > 1 or r > 1: return self._start_multi(a, idx, ids, n, at, r=r)
        if at is None:
            pr = self.precise_var.get()
            if pr: total = len(ids) * float(CFG.get('precise_max_minutes') or 240)
            if not messagebox.askokcancel('開始錄製', f'{a.p["name"]} @ 模擬器 {self.emu_name(idx)}：{len(ids)} 款，' + (
                    f'每款錄到 RTP 範圍 ±{CFG.get("precise_pp") or 10} 個百分點為止（每款最多 {CFG.get("precise_max_minutes") or 240} 分鐘，含之前錄過的局數）'
                    if pr else f'共 {total:.0f} 分鐘') + '\n\n錄製期間請不要操作這台模擬器。要中途停止請按「■ 停止此台」。', parent=self): return
            if not self._preflight(idx, total): return
            self._launch(idx, a.id, ids, time.strftime('%Y%m%d_%H%M%S') + f'_e{idx}', precise=pr)
        else:
            self.jobs[idx] = {'sched': at, 'app': a.id, 'slots': ids, 'precise': self.precise_var.get(),
                              'state': f'排程 {time.strftime("%m/%d %H:%M", time.localtime(at))}', 'cur': f'{len(ids)} 款 {total:.0f} 分'}
            self.log(f'⏰ 已排程：模擬器 {self.emu_name(idx)} 在 {time.strftime("%m/%d %H:%M", time.localtime(at))} 開始錄 {", ".join(ids)}（GUI 視窗要開著）')
            self._refresh_jobs()
    # ---------------- 同時用 N 台 × 重複 R 次：N emulators each record the SAME slots full length, repeated R cycles ----------------
    def _multi_plan(self, idx, n):
        """-> (existing emulator indexes to use, number of new SR_ emulators to create). The selected one first, then
        emulators SlotRecorder created (SR_), then the other existing ones; none that is recording / scanning."""
        inst = d.instances() if TOOLS['ldconsole'] else []
        sched = {k for k, j in self.jobs.items() if j.get('sched') and not j.get('proc')}
        free = [i for i, nm, _ in inst if i != idx and not self.running(i) and i not in sched]
        sr = [i for i in free if dict((k, v) for k, v, _ in inst)[i].startswith(d.SR_PREFIX)]
        use = ([idx] + sr + [i for i in free if i not in sr])[:n]
        return use, n - len(use), {i: (nm, run) for i, nm, run in inst}
    def _start_multi(self, a, idx, ids, n, at=None, ask=True, precise=None, r=1):
        precise = self.precise_var.get() if precise is None else precise
        r = max(1, int(r))
        rows = {i: float(self.tree.set(i, 'min') or 60) for i in ids} if ask else {rw['id']: float(rw.get('minutes') or 60) for rw in a.slots()}
        if precise: rows = {i: float(CFG.get('precise_max_minutes') or 240) for i in ids}; r = 1    # precise stops on its own -> no repeat
        per = {i: max(1, round(rows.get(i, 60))) for i in ids}                    # each emulator records the FULL length
        use, new, inst = self._multi_plan(idx, n)
        if new and not TOOLS['ldconsole']: messagebox.showerror('同時錄製', '找不到 ldconsole.exe，無法建立新模擬器（設定 → LDPlayer 資料夾）', parent=self); return
        if at is not None:
            self.jobs[idx] = {'sched': at, 'app': a.id, 'slots': ids, 'n': n, 'r': r, 'precise': precise, 'state': f'排程 {time.strftime("%m/%d %H:%M", time.localtime(at))}',
                              'cur': f'{len(ids)} 款 × {n} 台 × {r} 次'}
            self.log(f'⏰ 已排程：{n} 台 × {r} 次同時錄 {", ".join(ids)}，{time.strftime("%m/%d %H:%M", time.localtime(at))} 開始（GUI 視窗要開著）')
            self._refresh_jobs(); return
        boot = sum(1 for i in use if not inst.get(i, ('', False))[1]) + new
        one_cycle = sum(per.values())                                            # wall-clock minutes of ONE cycle (N emulators parallel)
        if ask:
            names = [f'{i} {inst.get(i, (str(i),))[0]}' for i in use] + [f'新建 SR_ 模擬器 ×{new}'] * bool(new)
            per_vals = ", ".join(f"{v:.0f}" for v in dict.fromkeys(per.values()))
            vids = len(use) * r                                                  # videos of each slot (N emulators × R cycles)
            msg = (f'{a.p["name"]}：{len(ids)} 款，{len(use)} 台同時錄、每款每台錄 {per_vals} 分鐘完整影片，重複 {r} 次\n'
                   + (f'（夠準就停：RTP 範圍 ±{CFG.get("precise_pp") or 10} 個百分點就提早換下一款，上面是最多的時間）\n' if precise else '')
                   + f'→ 每款會得到 {vids} 支影片（{len(use)} 台 × {r} 次），全部合併成一份統計\n'
                   + f'→ 全程約 {one_cycle * r:.0f} 分鐘（一輪約 {one_cycle:.0f} 分鐘 × {r} 次）\n\n使用：{"、".join(names)}\n')
            if new:
                msg += (f'\n新模擬器會自動建立（約 2 分鐘）、從現有模擬器複製 {a.p["name"]}、以新帳號開始。'
                        + ('\n88 Fortunes 的新帳號要先練到約 5 級才開得了多數遊戲，這幾台會晚 15~40 分鐘才開始錄。' if a.p.get('leveling') else '') +
                        '\n中途不會關機（重複期間一直用同一台），全部錄完後新模擬器會關機但保留，下次優先再用。')
            msg += '\n\n錄製期間請不要操作這些模擬器。'
            if not messagebox.askokcancel('同時錄製', msg, parent=self): return
            need = 4.5 * boot + 1
            if d.free_ram_gb() < need and not messagebox.askyesno('資源不足', f'要開 {boot} 台模擬器，建議至少 {need:.0f} GB 記憶體，目前剩 {d.free_ram_gb():.1f} GB。\n'
                                                                  f'記憶體不足時錄影可能被系統中止。仍要開始嗎？', parent=self): return
            vd = video_dir(a.id); need_disk = one_cycle * len(use) * r / 60 * 1.5 + 1
            if d.free_disk_gb(vd) < need_disk and not messagebox.askyesno('資源不足', f'硬碟只剩 {d.free_disk_gb(vd):.1f} GB，這次約需 {need_disk:.0f} GB。仍要開始嗎？', parent=self): return
        group = 'G' + time.strftime('%Y%m%d_%H%M%S')
        self.groups[group] = {'app': a.id, 'idxs': set(), 'merged': False, 'slots': ids, 'n': n, 'precise': precise,
                              'cycles': r, 'cycle': 0, 'use': list(use)}
        self.log(f'=== 同時錄製 {group}：{n} 台 × {len(ids)} 款 × 重複 {r} 次（新建 {new} 台）===')
        for i in use: self.jobs[i] = {'prep': True, 'app': a.id, 'state': '準備中', 'cur': f'{len(ids)} 款 × {n} 台 × {r} 次', 'group': group}
        self._refresh_jobs()
        def work():
            say = lambda s: self.q.put(('log', s)); got = list(use)
            for k in range(new):
                say(f'建立新模擬器 {k + 1}/{new}…')
                try: i = d.new_instance(say)
                except Exception as e: say(f'✘ 建立失敗 {e!r}'); i = None
                if i is None: say('✘ 建立失敗，用現有的台數開始'); break
                got.append(i)
            self.q.put(('emus', None)); self.q.put(('multi_go', group, got))
        self._bg(work)
    def _run_cycle(self, group):
        """launch (or re-launch) the group's emulators for the next repeat cycle. Helper SR_ emulators stay up between
        cycles (keep=True) and are shut down only on the last cycle."""
        g = self.groups.get(group)
        if not g: return
        g['cycle'] += 1; last = g['cycle'] >= g['cycles']
        if g['cycles'] > 1: self.log(f'--- {group}：第 {g["cycle"]}/{g["cycles"]} 輪開始 ---')
        for i in g['use']:
            self._launch(i, g['app'], g['slots'], f'{group}_c{g["cycle"]}_e{i}', g['n'], group, g['precise'], keep=not last)
    def _schedule(self):
        v = simpledialog.askstring('定時開始', '幾點開始？（24 小時制，例如 22:30；已過的時間 = 明天）\n'
                                   '會用「目前選的 App、模擬器、勾選的 slot」。時間到會自動開模擬器。GUI 視窗要保持開著。', parent=self)
        if not v: return
        m = re.fullmatch(r'\s*(\d{1,2})[:：](\d{2})\s*', v)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59: messagebox.showwarning('定時開始', '格式像 22:30', parent=self); return
        now = time.localtime(); at = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
        if at <= time.time(): at += 86400
        self._start(at=at)
    def _resume(self):
        idx = self.emu(); p = last_job_path(idx)
        if not os.path.exists(p) and idx == int(CFG.get('ld_index', 0)) and os.path.exists(os.path.join(LOG_DIR, 'last_job.json')):
            p = os.path.join(LOG_DIR, 'last_job.json')
        if not os.path.exists(p): messagebox.showinfo('接續上次', f'模擬器 {self.emu_name(idx)} 沒有上次的工作紀錄', parent=self); return
        if self.running(idx): messagebox.showinfo('接續上次', '這台正在錄製中', parent=self); return
        j = json.load(open(p)); self.log(f'--- 接續工作 {j["job"]}（已完成的 slot 會跳過）---')
        self._launch(idx, j['app'], j['slots'], j['job'], int(j.get('split') or 1), j.get('group') or '', bool(j.get('precise')))
    def _launch(self, idx, app_id, ids, job, split=1, group='', precise=False, keep=False):
        if self._alive(self.jobs.get(idx)) or any(self._alive(o) and o.get('on') == idx for o in self.jobs.values()): return
        sf = d.stop_file(idx)
        if os.path.exists(sf): os.remove(sf)
        if os.path.exists(STOP_ALL): os.remove(STOP_ALL); self.log('（移除了上次留下的全域 STOP 檔）')
        json.dump({'app': app_id, 'slots': ids, 'job': job, 'emu': idx, 'split': split, 'group': group, 'precise': precise}, open(last_job_path(idx), 'w'))
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1', SLOTREC_LOG=os.path.join(LOG_DIR, f'job_{job}.log'))
        if keep: env['SLOTREC_KEEP_EMU'] = '1'                 # more repeat cycles coming -> batch must NOT shut the helper down
        else: env.pop('SLOTREC_KEEP_EMU', None)
        extra = (['--split', str(split), '--group', group] if group else []) + (['--precise'] if precise else [])
        proc = subprocess.Popen([PY, '-u', '-m', 'core.batch', '--app', app_id, '--slots', ','.join(ids), '--job', job, '--emu', str(idx), *extra],
                                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NOWIN)
        self.jobs[idx] = {'proc': proc, 'app': app_id, 'slots': ids, 'job': job, 'state': '啟動中', 'cur': '', 'prog': 0, 'pop': '', 'off': '',
                          'note': f'同時錄製 {split} 台之一' if group else '', 'group': group}
        if group:
            g = self.groups.setdefault(group, {'app': app_id, 'idxs': set(), 'merged': False}); g['idxs'].add(idx); g['merged'] = False
        def reader(p=proc):
            for raw in p.stdout: self.q.put(('run', idx, raw.decode('utf-8', 'replace')))
            p.wait(); self.q.put(('ended', idx, p.returncode))
        self._bg(reader)
        self.log(f'=== 模擬器 {self.emu_name(idx)} 開始：{app_id} {", ".join(ids)} ===')
        self._refresh_jobs()
    def _sel_job(self):
        s = self.jt.selection()
        return int(s[0]) if s else self.emu()
    def _stop(self):
        idx = self._sel_job(); j = self.jobs.get(idx)
        if j and j.get('sched') and not j.get('proc'):
            del self.jobs[idx]; self.log(f'已取消模擬器 {self.emu_name(idx)} 的排程'); self._refresh_jobs(); return
        if not self.running(idx): messagebox.showinfo('停止', f'模擬器 {self.emu_name(idx)} 沒有在錄製', parent=self); return
        open(d.stop_file(idx), 'w').close(); j['state'] = '停止中（收尾合併影片，最多約 1 分鐘）'
        self.log(f'--- 模擬器 {self.emu_name(idx)}：已要求停止，會把已錄的部分存好 ---'); self._refresh_jobs()
    def _stop_all(self):
        for idx in [i for i, j in self.jobs.items() if j.get('sched') and not j.get('proc')]: del self.jobs[idx]
        if not any(self.running(i) for i in self.jobs): self._refresh_jobs(); return
        for i, j in self.jobs.items():
            if j.get('proc') and j['proc'].poll() is None: open(d.stop_file(i), 'w').close(); j['state'] = '停止中…'
        self.log('--- 全部停止：每台都會把已錄的部分存好 ---'); self._refresh_jobs()
    def _open_videos(self):
        a = self.app(); os.startfile(video_dir(a.id if a else ''))

    # ---------------- analysis / pack ----------------
    def _analyze(self):
        a = self.app()
        fs = filedialog.askopenfilenames(title='選要分析的影片（可多選）', initialdir=video_dir(a.id if a else ''),
                                         filetypes=[('影片', '*.mp4')], parent=self)
        fs = [f for f in fs if not f.endswith('_clean.mp4')]
        if not fs: return
        self.log(f'--- 分析 {len(fs)} 支影片（每小時影片約 2 分鐘）---')
        def work():
            p = subprocess.Popen([PY, '-u', '-m', 'core.analyze', *fs] + (['--app', a.id] if a else []) + (['--merge'] if len(fs) > 1 else []), cwd=ROOT,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NOWIN, env=dict(os.environ, PYTHONIOENCODING='utf-8'))
            merged = []
            for raw in p.stdout:
                s = raw.decode('utf-8', 'replace'); self.q.put(('log', s))
                m = re.search(r'-> (.+_merged_summary\.html)', s)
                if m: merged.append(m.group(1).strip())
            p.wait()
            if merged: self.q.put(('merged', merged)); return          # several videos of one slot -> show the merged report
            for f in fs:
                h = f[:-4] + '_summary.html'
                if os.path.exists(h) and os.path.getmtime(h) > time.time() - 3600: os.startfile(h)
        self._bg(work)
    def _paytable(self):
        """selected game's paytable screenshots: open them, or (re)take them on the selected emulator"""
        a = self.app(); sel = self.tree.selection()
        if not a or not sel: messagebox.showinfo('賠率表', '先在清單點選一款遊戲', parent=self); return
        sid = sel[0]; name = self.tree.set(sid, 'name')
        from core import paytable as pt
        folder = pt.out_dir(a, sid)
        if pt.has(a, sid):
            n = len([f for f in os.listdir(folder) if f.endswith('.jpg')])
            r = messagebox.askyesnocancel('賠率表', f'{name} 已截過 {n} 頁。\n\n是 = 開啟資料夾\n否 = 重新截（會進遊戲重拍）', parent=self)
            if r is None: return
            if r: os.startfile(folder); return
        if not a.p.get('paytable'): messagebox.showinfo('賠率表', f'{a.p["name"]} 還沒設定賠率表按鈕', parent=self); return
        if self._busy(): return
        idx = self.emu(); self.log(f'--- 截賠率表：{name}（模擬器 {self.emu_name(idx)}，先進遊戲，約 2~4 分鐘）---')
        def work():
            if not d.ensure_running(): print('✘ 模擬器開不起來'); return
            if not a.enter_slot(sid): print('✘ 進不了這款遊戲'); return
            n = pt.capture(a, sid, force=True, say=print)
            print(f'✔ 賠率表 {n} 頁 → {folder}' if n else '✘ 沒截到賠率表（看上面的訊息）')
            if n: os.startfile(folder)
        self._on_dev(idx, work)
    def _overview(self):
        """rebuild 總覽.html + Excel of every app and open it"""
        self.log('--- 更新總覽（所有 app，含 Excel / PDF）---')
        def work():
            say = lambda s: self.q.put(('log', s))
            try:
                from core import overview
                h = overview.build(say=say); os.startfile(h)
            except Exception as e: say(f'總覽失敗：{e!r}')
        self._bg(work)
    def _pick_low(self):
        """tick the games of this app that were recorded but have fewer spins than the target; minutes = estimate to reach it"""
        a = self.app()
        if not a or self._locked(): return
        self.log('計算各款已錄局數…')
        def work():
            try:
                from core import overview
                overview.build([a.id], excel=False, say=lambda s: None)
                with open(os.path.join(overview.videos_root(), 'overview.json'), encoding='utf-8') as f: data = json.load(f)
                self.q.put(('lowpick', a.id, data))
            except Exception as e: self.q.put(('log', f'計算失敗：{e!r}'))
        self._bg(work)
    def _apply_low(self, app_id, data):
        a = self.app()
        if not a or a.id != app_id: return
        games = data['apps'].get(app_id, {}); target = data.get('target', 1000)
        low = {k: g for k, g in games.items() if g['need'] > 0 and k in self._all_ids and self.tree.exists(k)}
        if not low:
            messagebox.showinfo('勾選樣本不足', f'已錄過的遊戲都達到 {target:,} 局了（沒錄過的不算）。', parent=self); return
        mins = {k: min(600, max(10, -(-(g['est_minutes'] or 60) // 10) * 10)) for k, g in low.items()}
        txt = '\n'.join(f'{g["name"]}：{g["spins"]:,} 局，差 {g["need"]:,} 局 → 錄 {mins[k]} 分鐘' for k, g in list(low.items())[:15])
        if len(low) > 15: txt += f'\n…共 {len(low)} 款'
        if not messagebox.askyesno('勾選樣本不足', f'目標每款 {target:,} 局。要勾選這 {len(low)} 款並設定補錄分鐘數嗎？\n'
                                   f'（其他遊戲會取消勾選；分鐘數依目前每小時局數估算）\n\n{txt}', parent=self): return
        for i in self._all_ids:
            if not self.tree.exists(i): continue
            self.tree.set(i, 'on', '✓' if i in low else '')
            if i in low: self.tree.set(i, 'min', mins[i])
        self._save_tree(); self.only_on.set(True); self._filter()
        self.log(f'已勾選 {len(low)} 款樣本不足的遊戲，按 ▶ 開始錄製即可補錄（可配合「同時用 N 台」）')
    def _pack(self):
        self.log('--- 打包中（不含影片、log、本機設定）---')
        def work():
            from core.pack import pack
            try:
                z = pack(); self.q.put(('log', f'✔ 已打包：{z}\n   複製到別台電腦解壓 → 執行 install.bat → run.bat'))
                subprocess.Popen(['explorer', '/select,', z])
            except Exception as e: self.q.put(('log', f'打包失敗：{e!r}'))
        self._bg(work)

    # ---------------- job table / events ----------------
    def _refresh_jobs(self):
        keep = self.jt.selection()
        self.jt.delete(*self.jt.get_children())
        for idx, j in sorted(self.jobs.items()):
            app_name = dict(self.apps).get(j.get('app'), j.get('app', ''))
            self.jt.insert('', 'end', iid=str(idx), values=(self.emu_name(idx), app_name, j.get('cur', ''),
                           f"{j.get('prog', 0):.0f}%" if j.get('proc') else '', j.get('state', ''), j.get('pop', ''), j.get('off', ''),
                           j.get('live', ''), j.get('note', '')))
        keep = [k for k in keep if self.jt.exists(k)]
        if keep: self.jt.selection_set(keep)
        r = [i for i in self.jobs if self.running(i)]
        self.status['text'] = f'錄製中：{len(r)} 台' if r else ('有排程' if self.jobs else '待命')
        cur = self.jobs.get(self._sel_job())
        self.bar['value'] = cur.get('prog', 0) if cur else 0
    def _tick(self):
        try:
            for idx, j in list(self.jobs.items()):
                if j.get('sched') and not j.get('proc') and time.time() >= j['sched']:
                    self.log(f'⏰ 排程時間到：模擬器 {self.emu_name(idx)}（記憶體剩 {d.free_ram_gb():.1f} GB）')
                    if d.free_ram_gb() < 2.5: self.log('⚠ 記憶體偏低，錄影可能被系統中止')
                    if (j.get('n') or 1) > 1 or (j.get('r') or 1) > 1:
                        j['sched'] = None; self._start_multi(App(j['app']), idx, j['slots'], j.get('n') or 1, ask=False, precise=bool(j.get('precise')), r=j.get('r') or 1)
                    else: self._launch(idx, j['app'], j['slots'], time.strftime('%Y%m%d_%H%M%S') + f'_e{idx}', precise=bool(j.get('precise')))
        except Exception as e:
            self.log(f'排程啟動失敗：{e!r}')
            for j in self.jobs.values():
                if j.get('sched') and not j.get('proc') and time.time() >= j['sched']: j['sched'] = None; j['state'] = '排程失敗'
            self._refresh_jobs()
        self.after(1000, self._tick)
    def _poll(self):
        dirty = False
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == 'log': self.log(item[1])
                elif kind == 'preview': self._show_image(item[1])
                elif kind == 'crop': self._crop_dialog(item[1])
                elif kind == 'tpl': self._tpl_dialog(item[1])
                elif kind == 'emus': self._load_emus()
                elif kind == 'lowpick': self._apply_low(item[1], item[2])
                elif kind == 'multi_go':
                    _, group, got = item
                    g = self.groups.get(group)
                    if g:
                        g['use'] = got
                        if len(got) < g.get('n', len(got)): self.log(f'⚠ 只有 {len(got)} 台可用（原本要 {g["n"]} 台），就用這幾台開始')
                        self._run_cycle(group)
                elif kind == 'merged':
                    for h in item[1]:
                        if os.path.exists(h): os.startfile(h)
                elif kind == 'reload':
                    self.scanning = None; self.btn_scan['state'] = 'normal'; self._load_slots()
                    self.log('=== 掃描完成，清單已更新 ===' if item[1] == 0 else f'=== 掃描失敗（rc={item[1]}），看上面的訊息 ===')
                elif kind == 'run':
                    _, idx, v = item; j = self.jobs.get(idx, {}); dirty = True
                    self.log(f'[{self.emu_name(idx)}] {v.rstrip()}')
                    m = re.search(r'PROGRESS (\d+)/(\d+)', v)
                    if m: j['prog'] = 100 * int(m.group(1)) / max(1, int(m.group(2)))
                    m = re.search(r'SLOT (\d+)/(\d+) (\S+)', v)
                    if m: j['cur'] = f'{m.group(1)}/{m.group(2)} {m.group(3)}'; j['prog'] = 0; j['live'] = ''
                    m = re.search(r'STATUS (\w+)(?: spins=(\d+) popups=(\d+) off=(\d+)%)?', v)
                    if m:
                        j['state'] = STATE_ZH.get(m.group(1), m.group(1))
                        if m.group(3): j['pop'] = m.group(3); j['off'] = m.group(4) + '%'
                    if 'LOW ON COINS' in v: j['note'] = '重置帳號 ' + time.strftime('%H:%M')
                    elif 'OUT OF COINS' in v: j['note'] = '⚠ 金幣不足，已停止（影片保留）'
                    elif 'EMULATOR SWITCHED' in v:
                        m2 = re.search(r'SWITCHED (\d+) -> (\d+)', v)
                        if m2: j['note'] = f'已換到新模擬器 {m2.group(2)}'; j['on'] = int(m2.group(2)); self.q.put(('emus', None))
                    elif 'switching emulator' in v: j['note'] = '建立新模擬器中（約 15 分鐘）'
                    elif 'ESCALATE' in v: j['note'] = '處理卡住 ' + time.strftime('%H:%M')
                    elif 'PRECISION reached already' in v: j['note'] = '之前的影片已夠準 → 跳過'
                    elif 'PRECISION reached' in v: j['note'] = '已夠準 → 換下一款'
                    m = re.search(r'LIVE spins=(\d+) rtp=([\d.]+) lo=([\d.-]+) hi=([\d.-]+) feats=(\d+) total=(\d+) half=([\d.-]+)', v)
                    if m:
                        sp, rtp, lo, hi, fe, tot, half = m.groups()
                        j['live'] = f'{int(sp):,} 局 RTP {float(rtp):.0%}' + (f' ±{float(half) * 100:.0f}' if half != '-' else '') + \
                                    (f'（全部 {int(tot):,}）' if int(tot) != int(sp) else '') + (f' 特色 {fe}' if fe != '0' else '')
                    elif 'WARNING low memory' in v: j['note'] = '⚠ 記憶體不足'
                    elif 'DISK almost full' in v: j['note'] = '⚠ 硬碟快滿，已停止'
                elif kind == 'ended':
                    _, idx, rc = item; j = self.jobs.get(idx, {}); dirty = True
                    j['state'] = '完成' if rc == 0 else '結束：有 slot 沒錄完（可按「接續上次」）'
                    j['prog'] = 100 if rc == 0 else j.get('prog', 0)
                    sf = d.stop_file(idx)
                    if os.path.exists(sf): os.remove(sf)
                    self.log(f'=== 模擬器 {self.emu_name(idx)} 工作結束 ===')
                    self._maybe_merge(j.get('group'))
        except queue.Empty: pass
        if dirty: self._refresh_jobs()
        self.after(150, self._poll)

    def _maybe_merge(self, group):
        """a cycle of a multi-emulator run finished -> start the next repeat cycle, or (last cycle) merge videos per slot."""
        g = self.groups.get(group or '')
        if not g or g['merged']: return
        if any(self._alive(self.jobs.get(i)) and self.jobs[i].get('group') == group for i in g['idxs']): return
        if g.get('cycle', 1) < g.get('cycles', 1):            # more repeat cycles to go -> relaunch the same emulators
            if g['cycles'] > 1: self.log(f'--- {group}：第 {g["cycle"]}/{g["cycles"]} 輪完成 ---')
            self._run_cycle(group); return
        g['merged'] = True; self.log(f'--- {group}：全部錄完，合併同一款的影片並產生統計報告（每小時影片約 2 分鐘）---')
        def work():
            p = subprocess.Popen([PY, '-u', '-m', 'core.analyze', '--group', group, '--app', g['app']], cwd=ROOT, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONIOENCODING='utf-8'),
                                 creationflags=NOWIN | getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0))
            out = []
            for raw in p.stdout:
                s = raw.decode('utf-8', 'replace'); self.q.put(('log', s))
                m = re.search(r'-> (.+_merged_summary\.html)', s)
                if m: out.append(m.group(1).strip())
            p.wait(); self.q.put(('log', f'=== {group}：合併完成，{len(out)} 份報告 ===')); self.q.put(('merged', out))
        self._bg(work)

    def _on_close(self):
        r = [i for i in self.jobs if self.running(i)]
        if r:
            if not messagebox.askyesno('離開', f'還有 {len(r)} 台在錄製。要全部停止並離開嗎？', parent=self): return
            for i in r: open(d.stop_file(i), 'w').close()
            self.status['text'] = '停止中，正在收尾合併影片…'
            for _ in range(240):
                if not any(self.running(i) for i in r): break
                self.update(); time.sleep(0.5)
            if any(self.running(i) for i in r):
                messagebox.showinfo('離開', '收尾還沒完成，會在背景繼續（影片與紀錄都會存好）。', parent=self)
        elif any(j.get('sched') for j in self.jobs.values()):
            if not messagebox.askyesno('離開', '有排程還沒開始，關掉視窗排程就不會執行。確定離開？', parent=self): return
        self.destroy()

if __name__ == '__main__':
    try:
        Gui().mainloop()
    except Exception:
        import traceback
        err = traceback.format_exc()
        with open(os.path.join(LOG_DIR, 'gui_error.log'), 'a', encoding='utf-8') as f: f.write(time.strftime('%F %T') + '\n' + err)
        try:
            r = tk.Tk(); r.withdraw(); messagebox.showerror('SlotRecorder 錯誤', err[-1500:]); r.destroy()
        except Exception: pass
        raise
