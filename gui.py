"""SlotRecorder GUI — pick an app + emulator, tick slots, press 開始錄製. Several emulators can record at the same time.
Also: add a slot (crop its lobby tile), add popup close-button / button templates, schedule, analyse videos, pack the tool."""
import glob, json, math, os, queue, re, shutil, subprocess, sys, threading, time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT); sys.path.insert(0, ROOT)
from core.config import CFG, TOOLS, video_dir, find, base_of, save as save_cfg, load as load_cfg
from core.app import App, list_apps, X_LIB
from core.vision import imwrite, imread
from core import device as d
from core.version import VERSION

STOP_ALL = d.STOP_ALL
SCAN_LBL = '🔍 掃描大廳找新遊戲'    # menu entry label; reused to toggle its enabled state during a scan
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
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()         # open relative to the screen so the window is never taller than the display
        self.geometry(f'{min(1250, sw - 80)}x{min(820, sh - 120)}'); self.minsize(900, 520)
        self.q = queue.Queue(); self.apps = list_apps(); self.jobs = {}     # emulator index -> job dict
        self.groups = {}                                                    # multi-emulator run id -> {'app', 'idxs', 'merged'}
        self.devlock = threading.Lock()
        self._build(); self.after(150, self._poll); self.after(1000, self._tick)
        self.after(2000, self._check_interrupted)
        self.protocol('WM_DELETE_WINDOW', self._on_close)

    # ---------------- layout ----------------
    def _build(self):
        self._build_menu()
        # top bar: shared app / emulator pickers + update badge (always visible on every tab)
        top = ttk.Frame(self, padding=(8, 6)); top.pack(fill='x')
        ttk.Label(top, text='App：').pack(side='left')
        self.app_var = tk.StringVar()
        self.app_box = ttk.Combobox(top, state='readonly', width=26, textvariable=self.app_var, values=[f'{n}  ({i})' for i, n in self.apps])
        self.app_box.pack(side='left'); self.app_box.bind('<<ComboboxSelected>>', lambda e: self._load_slots())
        ttk.Label(top, text='  模擬器：').pack(side='left')
        self.emu_var = tk.StringVar(); self.emu_box = ttk.Combobox(top, state='readonly', width=18, textvariable=self.emu_var)
        self.emu_box.pack(side='left'); self._load_emus()
        ttk.Button(top, text='↻', width=3, command=self._load_emus).pack(side='left', padx=(2, 8))
        self.status = ttk.Label(top, text='待命', foreground='#555'); self.status.pack(side='right')
        # update is a menu action (工具 ▸ 檢查更新); the top bar only shows a badge, and only when it matters
        self.upd_lbl = ttk.Label(top, text='', foreground='#c0392b', cursor='hand2'); self.upd_lbl.pack(side='right', padx=6)
        self.upd_lbl.bind('<Button-1>', lambda e: self._update())
        self.after(3000, self._check_update_async)           # background: is there a newer version in the git repo?

        # shared message / progress log pinned to the bottom (visible whichever tab is open)
        logf = ttk.Frame(self, padding=(8, 0)); logf.pack(side='bottom', fill='x')
        ttk.Label(logf, text='訊息 / 進度').pack(anchor='w')
        self.logbox = tk.Text(logf, height=7, wrap='none', font=('Consolas', 9))
        sb = ttk.Scrollbar(logf, command=self.logbox.yview); self.logbox.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y'); self.logbox.pack(fill='x', pady=(2, 8))

        nb = ttk.Notebook(self); nb.pack(fill='both', expand=True, padx=8, pady=(4, 0))
        rec = ttk.Frame(nb, padding=6); nb.add(rec, text='  錄製  ')
        rep = ttk.Frame(nb, padding=6); nb.add(rep, text='  分析 / 報告  ')

        # ---- 錄製 tab: pick games -> record -> watch jobs ----
        # grid layout so the list and the job-status table SHARE the vertical space and shrink with the window
        # (each has its own scrollbar); the buttons / labels / progress keep their size and never get clipped.
        rec.columnconfigure(0, weight=1)
        rec.rowconfigure(2, weight=3)      # game list: takes the most room, shrinks first
        rec.rowconfigure(5, weight=1)      # job-status table: also flexes, so it's never pushed off-screen
        ttk.Label(rec, text='Slot 清單（點一下選要錄的遊戲、雙擊「名稱」「分鐘」修改；新增 / 刪除 / 掃描大廳在上方「遊戲清單」選單）').grid(row=0, column=0, sticky='w')
        flt = ttk.Frame(rec); flt.grid(row=1, column=0, sticky='ew', pady=(2, 2))
        ttk.Label(flt, text='搜尋：').pack(side='left')
        self.q_var = tk.StringVar(); self.q_var.trace_add('write', lambda *a: self._filter())
        ttk.Entry(flt, textvariable=self.q_var, width=22).pack(side='left')
        self.count_lbl = ttk.Label(flt, text='', foreground='#555'); self.count_lbl.pack(side='right')
        tf = ttk.Frame(rec); tf.grid(row=2, column=0, sticky='nsew')
        self.tree = ttk.Treeview(tf, columns=('name', 'min', 'id'), show='headings', height=6, selectmode='browse')
        for c, t, w in (('name', '名稱', 240), ('min', '分鐘', 50), ('id', '縮圖 id', 120)):
            self.tree.heading(c, text=t); self.tree.column(c, width=w, anchor='center' if c == 'min' else 'w')
        rp = ttk.Frame(tf, width=210); rp.pack(side='right', fill='y'); rp.pack_propagate(False)   # thumbnail lives BESIDE the list, out of the 開始錄製 row's vertical path, so selecting a game never shifts the buttons
        self.thumb = ttk.Label(rp); self.thumb.pack(anchor='nw', padx=(6, 0), pady=(2, 0))
        tsb = ttk.Scrollbar(tf, command=self.tree.yview); self.tree.configure(yscrollcommand=tsb.set)
        tsb.pack(side='right', fill='y'); self.tree.pack(side='left', fill='both', expand=True)
        self.tree.bind('<Double-1>', self._tree_dbl); self.tree.bind('<<TreeviewSelect>>', lambda e: self._show_thumb())

        run = ttk.Frame(rec); run.grid(row=3, column=0, sticky='ew', pady=(6, 0))
        self.btn_start = ttk.Button(run, text='▶ 開始錄製', command=self._start); self.btn_start.pack(side='left')
        ttk.Label(run, text=' 同時用').pack(side='left')
        self.n_var = tk.IntVar(value=1)
        ttk.Spinbox(run, from_=1, to=MAX_MULTI, width=3, textvariable=self.n_var, state='readonly').pack(side='left')
        ttk.Label(run, text='台，重複').pack(side='left')
        self.r_var = tk.IntVar(value=1)
        ttk.Spinbox(run, from_=1, to=MAX_REPEAT, width=3, textvariable=self.r_var, state='readonly').pack(side='left')
        ttk.Label(run, text='次').pack(side='left', padx=(0, 6))
        self.precise_var = tk.BooleanVar(value=bool(CFG.get('precise_default', False)))   # kept for _start / 接續; UI toggle removed
        ttk.Button(run, text='■ 停止此台', command=self._stop).pack(side='left', padx=(12, 4))
        ttk.Button(run, text='■ 全部停止', command=self._stop_all).pack(side='left')
        ttk.Button(run, text='💸 沒錢了·清除重跑', command=self._reset_now).pack(side='left', padx=(12, 0))
        ttk.Button(run, text='✅ 開錄前自檢', command=self._selftest).pack(side='left', padx=(12, 0))

        ttk.Label(rec, text='錄製工作（每台模擬器一列；選一列再按「停止此台」）').grid(row=4, column=0, sticky='w', pady=(8, 0))
        cols = (('emu', '模擬器', 110), ('app', 'App', 110), ('cur', '目前', 240), ('prog', '進度', 120), ('state', '狀態', 130),
                ('pop', '彈窗', 50), ('off', '非遊戲', 60), ('live', '即時統計', 190), ('note', '備註', 160))
        jtf = ttk.Frame(rec); jtf.grid(row=5, column=0, sticky='nsew')
        self.jt = ttk.Treeview(jtf, columns=[c for c, _, _ in cols], show='headings', height=3)
        for c, t, w in cols: self.jt.heading(c, text=t); self.jt.column(c, width=w, anchor='w')
        self.jt.tag_configure('crit', foreground='#d00000')   # 已停止 / 需處理：紅字（vista 主題會忽略 tag 底色，改用文字色才顯色）
        self.jt.tag_configure('warn', foreground='#cc6600')   # 處理中的小狀況：橘字
        jsb = ttk.Scrollbar(jtf, command=self.jt.yview); self.jt.configure(yscrollcommand=jsb.set)
        jsb.pack(side='right', fill='y'); self.jt.pack(side='left', fill='both', expand=True)
        self.bar = ttk.Progressbar(rec, maximum=100); self.bar.grid(row=6, column=0, sticky='ew', pady=(4, 0))

        # ---- 分析 / 報告 tab ----
        ttk.Label(rep, text='錄完影片後在這裡看結果，報告會自動開啟。', foreground='#555').pack(anchor='w', pady=(2, 10))
        ob = ttk.Frame(rep); ob.pack(fill='x')
        ttk.Button(ob, text='📊 分析影片（挑影片算 RTP / 中獎率）', command=self._analyze).pack(side='left')
        ttk.Button(ob, text='🔄 重新辨識全部（舊影片套新版報告）', command=self._reanalyze_all).pack(side='left', padx=6)
        ttk.Button(ob, text='📋 總覽（所有遊戲彙總）', command=self._overview).pack(side='left', padx=6)
        ob2 = ttk.Frame(rep); ob2.pack(fill='x', pady=(8, 0))
        ttk.Button(ob2, text='🎰 特色分布（選取遊戲的累積贏分分布）', command=self._feature_report).pack(side='left')
        ttk.Button(ob2, text='📂 開啟影片資料夾', command=self._open_videos).pack(side='left', padx=6)
        ttk.Button(ob2, text='🧹 清理舊影片', command=self._cleanup_videos).pack(side='left')
        ttk.Label(rep, text='「🎰 特色分布」：先到「錄製」分頁的清單點選一款遊戲，再按這顆。數據會跨天 / 跨場次累積。',
                  foreground='#888').pack(anchor='w', pady=(12, 0))

        if self.apps: self.app_box.current(0); self._load_slots()

    def _build_menu(self):
        """menu bar for setup / admin, so the main screen keeps only the daily recording flow"""
        m = tk.Menu(self); self.config(menu=m)
        mm = tk.Menu(m, tearoff=0); m.add_cascade(label='模擬器', menu=mm)
        mm.add_command(label='看畫面', command=self._preview)
        mm.add_command(label='環境檢查', command=self._env_check)
        mm.add_command(label='準備模擬器', command=self._prepare_emu)
        g = tk.Menu(m, tearoff=0); m.add_cascade(label='遊戲清單', menu=g); self.m_games = g
        g.add_command(label='＋ 新增 slot', command=self._add_slot)
        g.add_command(label='刪除選取的遊戲', command=self._del_slot)
        g.add_command(label='測試尋找', command=self._test_find)
        g.add_separator()
        g.add_command(label=SCAN_LBL, command=self._scan_catalog)
        g.add_command(label='📖 賠率表', command=self._paytable)
        g.add_separator()
        g.add_command(label='＋ 加介面範本（卡彈窗時用）', command=self._add_tpl_choose)
        t = tk.Menu(m, tearoff=0); m.add_cascade(label='工具', menu=t)
        t.add_command(label='↻ 接續上次中斷的錄影', command=self._resume)
        t.add_command(label='檢查更新', command=self._update)
        t.add_separator()
        t.add_command(label='打包帶走（做給同事的安裝包）', command=self._pack)
        t.add_separator()
        t.add_command(label='設定…', command=self._settings)

    def _add_tpl_choose(self):
        """one entry for both template sources; ask which, then reuse _add_tpl(from_file=…)"""
        r = messagebox.askyesnocancel('加介面範本',
                '卡在未知彈窗時，把它的 X / 按鈕框起來當範本。\n\n'
                '範本從哪裡來？\n'
                '　是 = 從目前模擬器畫面擷取\n'
                '　否 = 從一張截圖檔選\n'
                '　取消 = 不做', parent=self)
        if r is None: return
        self._add_tpl(r is False)                                # 是→畫面(from_file=False)；否→截圖檔(from_file=True)

    def _scan_menu_enabled(self, on):
        try: self.m_games.entryconfig(SCAN_LBL, state='normal' if on else 'disabled')
        except Exception: pass

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
            self.tree.insert('', 'end', iid=r['id'], values=(r['name'], r['minutes'], r['id']))
            self._all_ids.append(r['id'])
        self._filter()
    def _filter(self):
        """search box: detach rows that do not match (they stay in the list and are still saved)"""
        if not hasattr(self, '_all_ids'): return
        q = self.q_var.get().strip().lower().replace(' ', '_'); n = 0
        for k, i in enumerate(self._all_ids):
            if not self.tree.exists(i): continue
            show = not q or q in self.tree.set(i, 'name').lower() or q in i
            if show: self.tree.reattach(i, '', k); n += 1
            else: self.tree.detach(i)
        self.count_lbl['text'] = f'顯示 {n} / 共 {len(self._all_ids)} 款'
    def _save_tree(self):
        a = self.app(); old = {r['id']: r for r in a.slots()}          # keep columns the list does not show (hint)
        rows = [dict(old.get(i, {}), id=i, name=self.tree.set(i, 'name'), minutes=self.tree.set(i, 'min'))
                for i in self._all_ids if self.tree.exists(i)]
        a.save_slots(rows); self._filter()
    def _show_thumb(self):
        sel = self.tree.selection(); a = self.app()
        if not sel or not a: return
        p = a.t.path(sel[0])
        if not p: self.thumb.configure(image='', text='（沒有縮圖）'); return
        import cv2
        from PIL import Image, ImageTk
        im = imread(p); h, w = im.shape[:2]; s = min(200 / w, 190 / h)   # fit inside the side panel (keep aspect)
        ph = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(cv2.resize(im, (max(1, int(w * s)), max(1, int(h * s)))), cv2.COLOR_BGR2RGB)))
        self.thumb.configure(image=ph, text=''); self.thumb.image = ph
    def _locked(self):
        if getattr(self, 'scanning', None) is not None:
            messagebox.showinfo('掃描中', '正在掃描大廳、更新清單，完成後再修改。', parent=self); return True
        return False
    def _tree_dbl(self, e):
        if self._locked(): return
        iid = self.tree.identify_row(e.y); col = self.tree.identify_column(e.x)
        if not iid: return
        if col == '#2':
            v = simpledialog.askinteger('分鐘', '錄幾分鐘？', initialvalue=int(float(self.tree.set(iid, 'min') or 60)), minvalue=1, maxvalue=600, parent=self)
            if v: self.tree.set(iid, 'min', v)
        elif col == '#1':
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
    # ---------------- self-update over git (code only; see UPDATE.md) ----------------
    def _check_update_async(self):
        """startup: is there a newer version in the repo? updates the button label, never blocks the UI."""
        def work():
            try:
                from core import update
                r = update.check_update()
            except Exception:
                r = ('err', VERSION, None)
            self.after(0, lambda: self._set_update_btn(*r))
        threading.Thread(target=work, daemon=True).start()

    def _set_update_btn(self, st, lo, rv):
        self._upd = (st, lo, rv)
        # top-bar badge: red when a new version is out, a muted hint when update isn't set up, else nothing
        txt, col = {'update': (f'★ 有新版 v{rv}（按這裡更新）', '#c0392b'),
                    'not_repo': ('更新未設定（跑 setup_git.bat）', '#888'),
                    'no_git': ('更新需裝 git', '#888')}.get(st, ('', '#888'))
        try: self.upd_lbl.config(text=txt, foreground=col)
        except Exception: pass

    def _update(self):
        from core import update
        st, lo, rv = getattr(self, '_upd', (None, VERSION, None))
        if st in (None, 'offline', 'err'):                    # not checked yet / was offline -> check now
            st, lo, rv = update.check_update(); self._set_update_btn(st, lo, rv)
        if st == 'no_git':
            messagebox.showinfo('更新', '這台電腦沒有 git。請先安裝 Git for Windows：\nhttps://git-scm.com/download/win'); return
        if st == 'not_repo':
            messagebox.showinfo('更新', '這份還沒接上更新庫。\n請先在工具資料夾裡跑一次 setup_git.bat，之後就能一鍵更新。'); return
        if st == 'offline':
            messagebox.showwarning('更新', '連不上更新庫（檢查網路 / 金鑰是否有效）。'); return
        if st == 'current':
            messagebox.showinfo('更新', f'已是最新版 v{lo}。'); return
        if st != 'update':
            return
        if not messagebox.askyesno('更新', f'目前 v{lo}，有新版 v{rv}。\n現在更新嗎？（只更新程式碼，不會動到你的錄影與設定）'): return
        self.upd_lbl.config(text='更新中…', foreground='#888')
        def work():
            ok, msg = update.do_update()
            self.after(0, lambda: self._update_done(ok, msg))
        threading.Thread(target=work, daemon=True).start()

    def _update_done(self, ok, msg):
        if ok:
            self.upd_lbl.config(text='✔ 更新完成，請重開工具', foreground='#1a7f37')
            messagebox.showinfo('更新完成', '更新完成。請關閉並重新開啟 SlotRecorder 讓新版生效。')
        else:
            self.upd_lbl.config(text='★ 有新版（按這裡更新）', foreground='#c0392b')
            messagebox.showerror('更新失敗', msg)

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
                  ('target_spins', '每款目標局數（總覽用，預設 1000）'),
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
                                      '清單裡還沒有的遊戲會自動加入（名字是讀圖猜的，可雙擊「名稱」修改）。\n期間請不要操作這台模擬器。', parent=self): return
        self.log(f'--- 掃描 {a.p["name"]} 大廳（模擬器 {self.emu_name(idx)}）---')
        self.scanning = idx; self._scan_menu_enabled(False)
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
        return list(self.tree.selection())   # single highlighted row = the game to record
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
        if not ids: messagebox.showinfo('開始錄製', '請先在清單點一下要錄的遊戲', parent=self); return
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
                              'cycles': r, 'use': list(use), 'done': {}, 'stopped': set(),
                              'per': dict(per), 'one_cycle': one_cycle}   # done[idx]=finished cycles; stopped=no more cycles; per/one_cycle feed the batch ETA
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
        """start EVERY emulator on its first repeat cycle (they then advance independently, no waiting for each other)."""
        g = self.groups.get(group)
        if not g: return
        g['done'] = {i: 0 for i in g['use']}; g['stopped'] = set()
        for i in g['use']: self._launch_cycle(group, i)
    def _launch_cycle(self, group, idx):
        """launch emulator idx for its NEXT repeat cycle. Each emulator repeats on its own clock; a slow emulator just
        ends up with fewer videos rather than making the others wait. Helper SR_ stays up until that台's own last cycle."""
        g = self.groups.get(group)
        if not g: return
        c = g['done'].get(idx, 0) + 1                                        # the cycle number this emulator is about to record
        if g['cycles'] > 1: self.log(f'--- {group}：模擬器 {self.emu_name(idx)} 第 {c}/{g["cycles"]} 輪開始 ---')
        self._launch(idx, g['app'], g['slots'], f'{group}_c{c}_e{idx}', g['n'], group, g['precise'], keep=c < g['cycles'])
    def _schedule(self):
        v = simpledialog.askstring('定時開始', '幾點開始？（24 小時制，例如 22:30；已過的時間 = 明天）\n'
                                   '會用「目前選的 App、模擬器、選取的遊戲」。時間到會自動開模擬器。GUI 視窗要保持開著。', parent=self)
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
        if os.path.exists(d.reset_file(idx)): os.remove(d.reset_file(idx))   # clear a stale 清除重跑 flag from a previous run
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
        g = self.groups.get(j.get('group') or '')
        if g and 'done' in g: g.setdefault('stopped', set()).add(idx)   # repeat run: no more cycles for this emulator
        open(d.stop_file(idx), 'w').close(); j['state'] = '停止中（收尾合併影片，最多約 1 分鐘）'
        self.log(f'--- 模擬器 {self.emu_name(idx)}：已要求停止，會把已錄的部分存好 ---'); self._refresh_jobs()
    def _stop_all(self):
        for idx in [i for i, j in self.jobs.items() if j.get('sched') and not j.get('proc')]: del self.jobs[idx]
        if not any(self.running(i) for i in self.jobs): self._refresh_jobs(); return
        for i, j in self.jobs.items():
            if j.get('proc') and j['proc'].poll() is None:
                g = self.groups.get(j.get('group') or '')
                if g and 'done' in g: g.setdefault('stopped', set()).add(i)   # no more repeat cycles
                open(d.stop_file(i), 'w').close(); j['state'] = '停止中…'
        self.log('--- 全部停止：每台都會把已錄的部分存好 ---'); self._refresh_jobs()
    def _reset_now(self):
        """沒錢了·清除重跑: signal the running recorder on the selected emulator to do its out-of-coins
        reset now (clear app data / new instance per the app's profile), instead of waiting for auto-detect."""
        idx = self._sel_job(); j = self.jobs.get(idx)
        if not self.running(idx):
            messagebox.showinfo('清除重跑', f'模擬器 {self.emu_name(idx)} 沒有在錄製，不用清除重跑。', parent=self); return
        try: mode = App(j['app']).p.get('reset', 'pm_clear') if j and j.get('app') else 'pm_clear'
        except Exception: mode = 'pm_clear'
        if mode == 'none' or (mode == 'new_instance' and not CFG.get('new_instance_reset', True)):
            if not messagebox.askyesno('清除重跑',
                    f'這款 app 沒有設定「沒錢自動重來」的方式' + ('（需在設定開啟「自動建新模擬器」）' if mode == 'new_instance' else '') +
                    '。\n現在送出重跑，可能會改成「停止並保存影片」。\n仍要送出嗎？', parent=self):
                return
        elif not messagebox.askyesno('清除重跑',
                f'對模擬器 {self.emu_name(idx)} 立刻「清除重跑」：\n'
                '清掉這款 app 的本機資料 → 重新進遊戲拿回初始金幣 → 接著錄。\n'
                '（你已錄好的影片不受影響。）\n\n現在就做嗎？', parent=self):
            return
        open(d.reset_file(idx), 'w').close()
        if j: j['state'] = '清除重跑中…（清資料→重進遊戲，約 1~3 分鐘）'
        self.log(f'--- 模擬器 {self.emu_name(idx)}：已要求「沒錢清除重跑」，最多約 5 秒後開始 ---'); self._refresh_jobs()
    def _open_videos(self):
        a = self.app(); os.startfile(video_dir(a.id if a else ''))
    def _cleanup_videos(self):
        """刪掉已分析成功、且超過 N 天的原始 .mp4（報告/統計/特色片段全留）。先列清單給使用者確認。"""
        from core import cleanup
        days = int(CFG.get('keep_video_days', 7))
        vdirs = {video_dir(aid) for aid, _ in self.apps} or {video_dir('')}
        items = []
        for vd in vdirs: items += cleanup.purgeable(vd, days)
        if not items:
            messagebox.showinfo('清理舊影片', f'沒有可清理的影片。\n（條件：已分析成功、且超過 {days} 天的原始 .mp4）', parent=self); return
        items.sort(key=lambda x: -x[1]); total = sum(sz for _, sz, _ in items)
        preview = '\n'.join(f'• {os.path.basename(p)}（{sz / 1e9:.2f} GB，{age:.0f} 天前）' for p, sz, age in items[:8])
        more = f'\n…另外還有 {len(items) - 8} 支' if len(items) > 8 else ''
        if not messagebox.askyesno('清理舊影片',
                f'找到 {len(items)} 支「已分析成功且超過 {days} 天」的原始影片，共 {total / 1e9:.1f} GB。\n'
                f'只刪原始 .mp4，報告 / 統計 / 特色片段全部保留。\n\n{preview}{more}\n\n確定刪除嗎？', parent=self):
            return
        def work():
            say = lambda s: self.q.put(('log', s))
            n, freed = cleanup.purge([p for p, _, _ in items], say)
            say(f'🧹 清理完成：刪了 {n} 支，釋出 {freed / 1e9:.1f} GB')
        self._bg(work)
    def _selftest(self):
        """開錄前秒級自檢：工具 / 硬碟 / 記憶體 / 模擬器 / 選取遊戲的模板。不會啟動遊戲。"""
        a = self.app()
        if not a: messagebox.showinfo('開錄前自檢', '先選一個 App', parent=self); return
        ids = list(self._checked()); n, r = self.n_var.get(), self.r_var.get()
        mins = sum(float(self.tree.set(i, 'min') or 60) for i in ids)
        need_disk = mins * max(1, n) * max(1, r) / 60 * 1.5 + 1 if ids else 0
        need_ram = 4.5 * max(1, n) + 1
        from core import selftest
        rows = selftest.check(a, ids, [self.emu()], video_dir(a.id), need_disk, need_ram)
        ok_all = all(ok for ok, _, _ in rows)
        body = '\n'.join(('✓ ' if ok else '✗ ') + f'{label}：{detail}' for ok, label, detail in rows)
        tip = '' if ids else '\n\n（未選遊戲：沒檢查模板。先在清單選取要錄的遊戲再按一次。）'
        messagebox.showinfo('開錄前自檢' + ('' if ok_all else '（有問題）'),
                            ('全部通過，可以開錄 👍' if ok_all else '有項目沒過，建議先處理：') + '\n\n' + body + tip, parent=self)

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
                h = find(base_of(f), '_summary.html')
                if os.path.exists(h) and os.path.getmtime(h) > time.time() - 3600: os.startfile(h)
        self._bg(work)
    def _reanalyze_all(self):
        """Re-run analysis on every video of the selected app so old reports pick up the current buckets / tables.
        Resumable: videos already on this version are skipped (unless the user picks 強制)."""
        a = self.app()
        if not a: messagebox.showinfo('重新辨識全部', '先選一個 App', parent=self); return
        vd = video_dir(a.id)
        vids = [v for v in glob.glob(os.path.join(vd, '*.mp4')) + glob.glob(os.path.join(vd, '*', 'raw', '*.mp4'))   # legacy flat + new per-game raw/
                if not v.endswith('_clean.mp4')]
        if not vids: messagebox.showinfo('重新辨識全部', '這個 App 還沒有影片', parent=self); return
        cur = sec = 0                                                  # sec counts only the videos that will actually re-run
        for v in vids:
            base = base_of(v)
            try:
                with open(find(base, '_summary.json'), encoding='utf-8') as fh:
                    if json.load(fh).get('analyzer_version') == VERSION: cur += 1; continue
            except (OSError, ValueError): pass
            try:
                with open(find(base, '_stats.json'), encoding='utf-8') as fh: sec += json.load(fh).get('video_s') or 0
            except (OSError, ValueError): pass
        todo = len(vids) - cur; est = max(1, math.ceil(sec / 3600 * 2))
        r = messagebox.askyesnocancel('重新辨識全部',
            f'{a.p["name"]}：共 {len(vids)} 支影片，{cur} 支已是最新版（v{VERSION}）。\n\n'
            f'預估重跑 {todo} 支約 {est} 分鐘（每小時影片約 2 分鐘）。\n\n'
            f'是 = 只重跑舊的 {todo} 支（可隨時關掉再按，會接續沒跑完的）\n'
            f'否 = 強制全部 {len(vids)} 支重跑\n取消 = 不跑', parent=self)
        if r is None: return
        force = (r is False)
        self.log(f'--- 重新辨識全部：{a.p["name"]}（{"強制全部" if force else f"舊的 {todo} 支"}）---')
        def work():
            cmd = [PY, '-u', '-m', 'core.analyze', '--reanalyze-all', '--app', a.id] + (['--force'] if force else [])
            p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NOWIN,
                                 env=dict(os.environ, PYTHONIOENCODING='utf-8'))
            for raw in p.stdout: self.q.put(('log', raw.decode('utf-8', 'replace')))
            p.wait()
            self.q.put(('log', '✅ 重新辨識完成，按「📋 總覽」看更新後的報告。\n'))
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
    def _feature_report(self):
        """accumulated feature-win distribution report for the selected game (reads the persistent ledger)"""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo('特色分布報告', '先在清單點選一款遊戲（報告用的是它累積到現在的所有特色資料）', parent=self); return
        sid = sel[0]; name = self.tree.set(sid, 'name')
        self.log(f'--- 產生特色贏分分布報告：{name}（累積帳本，跨天/跨場次）---')
        def work():
            say = lambda s: self.q.put(('log', s))
            try:
                from core import ledger
                out = ledger.report(sid, pdf=True, say=say)
                if not out:
                    say(f'{name}：帳本裡還沒有這款的特色資料。先錄幾場，analyze 完會自動累積進去。')
                    return
                pdf = out[:-5] + '.pdf'
                os.startfile(pdf if os.path.exists(pdf) else out)
            except Exception as e:
                say(f'報告失敗：{e!r}')
        self._bg(work)
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
    @staticmethod
    def _fmt_min(m):
        m = int(round(m))
        if m < 1: return '<1 分'
        if m < 60: return f'{m} 分'
        return f'{m // 60} 時 {m % 60} 分'
    def _cur_cell(self, idx, j):
        """目前欄：輪數(多輪時) + 這一輪的第幾款遊戲 + 名稱"""
        if not j.get('slot_i'): return j.get('cur', '')                       # not recording yet -> placeholder / schedule text
        cur = f"遊戲{j['slot_i']}/{j['slot_n']} {j.get('slot_name', '')}"
        g = self.groups.get(j.get('group'))
        if g and g.get('cycles', 1) > 1:
            cur = f"輪{g['done'].get(idx, 0) + 1}/{g['cycles']}·{cur}"
        return cur
    def _prog_cell(self, j):
        """進度欄：百分比 + 這支影片還要多久錄完"""
        if not j.get('proc'): return ''
        s = f"{j.get('prog', 0):.0f}%"
        if j.get('tot_s'): s += ' · 剩' + self._fmt_min(max(0, j['tot_s'] - j.get('rec_s', 0)) / 60)
        return s
    def _job_eta_min(self, idx, j):
        """這台模擬器把自己剩下的遊戲×輪數全部錄完還要多少分鐘(precise 模式是上限)"""
        g = self.groups.get(j.get('group'))
        if g and j.get('prep'): return g.get('cycles', 1) * (g.get('one_cycle') or 0)   # queued, not started -> whole load
        this_rem = max(0, j.get('tot_s', 0) - j.get('rec_s', 0)) / 60 if j.get('tot_s') else 0
        if not g: return this_rem
        per, slots, one = g.get('per') or {}, g.get('slots') or [], g.get('one_cycle') or 0
        xi = j.get('slot_i') or 0                                             # 1-based current game -> slots[xi:] are the ones after it
        after = sum(per.get(s, 0) for s in slots[xi:]) if xi else 0
        full_left = max(0, g.get('cycles', 1) - (g['done'].get(idx, 0) + 1)) * one
        return this_rem + after + full_left
    @staticmethod
    def _alert_level(j):
        """哪些狀態要在工作表標色：crit=已停止/需你處理(紅)、warn=正在自己處理的小狀況(橘)、''=正常"""
        note, state = j.get('note', ''), j.get('state', '')
        if any(k in note for k in ('金幣不足', '硬碟快滿', '記憶體不足')) or state.startswith('結束：有 slot'):
            return 'crit'
        if state in ('卡住處理中', '金幣用完→重置帳號') or any(k in note for k in ('處理卡住', '重置帳號')):
            return 'warn'
        return ''
    def _refresh_jobs(self):
        keep = self.jt.selection()
        self.jt.delete(*self.jt.get_children())
        alerts = 0
        for idx, j in sorted(self.jobs.items()):
            app_name = dict(self.apps).get(j.get('app'), j.get('app', ''))
            lvl = self._alert_level(j)
            if lvl: alerts += 1
            self.jt.insert('', 'end', iid=str(idx), tags=((lvl,) if lvl else ()),
                           values=(self.emu_name(idx), app_name, self._cur_cell(idx, j),
                           self._prog_cell(j), j.get('state', ''), j.get('pop', ''), j.get('off', ''),
                           j.get('live', ''), j.get('note', '')))
        keep = [k for k in keep if self.jt.exists(k)]
        if keep: self.jt.selection_set(keep)
        r = [i for i in self.jobs if self.running(i)]
        if r:
            eta = max((self._job_eta_min(i, self.jobs[i]) for i in self.jobs if self.running(i) or self.jobs[i].get('prep')), default=0)
            prec = any(g.get('precise') for g in self.groups.values())        # whole batch finishes when the slowest emulator does
            base = f'錄製中：{len(r)} 台' + (f' · 整批約剩 {self._fmt_min(eta)}' + ('（上限）' if prec else '') if eta > 0 else '')
        else:
            base = '有排程' if self.jobs else '待命'
        self.status['text'] = (f'⚠ {alerts} 台需要注意 · ' if alerts else '') + base
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
                    self.scanning = None; self._scan_menu_enabled(True); self._load_slots()
                    self.log('=== 掃描完成，清單已更新 ===' if item[1] == 0 else f'=== 掃描失敗（rc={item[1]}），看上面的訊息 ===')
                elif kind == 'run':
                    _, idx, v = item; j = self.jobs.get(idx, {}); dirty = True
                    self.log(f'[{self.emu_name(idx)}] {v.rstrip()}')
                    m = re.search(r'PROGRESS (\d+)/(\d+)', v)
                    if m:
                        rec, tot = int(m.group(1)), int(m.group(2))
                        j['rec_s'], j['tot_s'] = rec, tot; j['prog'] = 100 * rec / max(1, tot)
                    m = re.search(r'SLOT (\d+)/(\d+) (\S+)', v)
                    if m:
                        j['slot_i'], j['slot_n'], j['slot_name'] = int(m.group(1)), int(m.group(2)), m.group(3)
                        j['cur'] = f'{m.group(1)}/{m.group(2)} {m.group(3)}'; j['prog'] = 0; j['live'] = ''
                        j['rec_s'] = j['tot_s'] = 0          # new game -> no time estimate until its first PROGRESS line
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
                    self._advance_cycle(j.get('group'), idx)
        except queue.Empty: pass
        if dirty: self._refresh_jobs()
        self.after(150, self._poll)

    def _advance_cycle(self, group, idx):
        """one emulator finished one cycle. If it still has cycles left, start its next one right away (no waiting for the
        others). When every emulator has finished all its cycles, merge the group's videos per slot."""
        g = self.groups.get(group or '')
        if not g or g.get('merged'): return
        if 'done' in g and idx in g['done']:                   # multi-run with repeat: advance this emulator on its own
            g['done'][idx] += 1
            if g['done'][idx] < g['cycles'] and idx not in g.get('stopped', ()):   # stopped台 does not start a new cycle
                if self._alive(self.jobs.get(idx)): return     # a stray late event; its next cycle is already running
                self._launch_cycle(group, idx); return
        # this emulator is done (all cycles or stopped) -> merge only once every emulator is finished and none still recording
        if any(g['done'].get(i, 0) < g['cycles'] and i not in g.get('stopped', ()) for i in g.get('use', [])): return
        if any(self._alive(self.jobs.get(i)) and self.jobs[i].get('group') == group for i in g['idxs']): return
        self._maybe_merge(group)
    def _maybe_merge(self, group):
        """every emulator of a multi-run finished all its cycles -> merge the group's videos per slot (background)."""
        g = self.groups.get(group or '')
        if not g or g['merged']: return
        if any(self._alive(self.jobs.get(i)) and self.jobs[i].get('group') == group for i in g['idxs']): return
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
