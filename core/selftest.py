"""Fast (seconds) pre-run check: catch the setup problems that would otherwise waste hours of an overnight run
BEFORE committing to it. Does NOT launch any game (that would be the slow 'deep' check). -> [(ok, label, detail)].
"""
from .config import TOOLS
from . import device as d

def check(app, slot_ids, emu_idxs, vdir, need_disk_gb=0, need_ram_gb=0):
    rows = []
    for k in ('scrcpy', 'ffmpeg', 'ffprobe', 'adb'):
        ok = bool(TOOLS.get(k))
        rows.append((ok, f'工具 {k}', '找到' if ok else '缺少 → 跑 install.bat 或在「設定」指定路徑'))
    try: free_d = d.free_disk_gb(vdir)
    except Exception: free_d = -1
    rows.append((free_d < 0 or free_d >= need_disk_gb, '硬碟空間',
                 (f'剩 {free_d:.0f} GB' if free_d >= 0 else '讀不到') + (f'（這次約需 {need_disk_gb:.0f} GB）' if need_disk_gb else '')))
    try: free_r = d.free_ram_gb()
    except Exception: free_r = -1
    rows.append((free_r < 0 or free_r >= need_ram_gb, '記憶體',
                 (f'剩 {free_r:.1f} GB' if free_r >= 0 else '讀不到') + (f'（建議 ≥ {need_ram_gb:.0f} GB）' if need_ram_gb else '')))
    inst = {i for i, _, _ in d.instances()} if TOOLS.get('ldconsole') else set()
    for idx in emu_idxs:
        ok = (not inst) or idx in inst                              # no ldconsole -> can't list, don't fail
        rows.append((ok, f'模擬器 {idx}', '找得到' if ok else '清單裡找不到這台（LDPlayer 資料夾設定？）'))
    for sid in slot_ids:
        missing = [n for n in app.required_templates(sid) if not app.t.has(n)]
        rows.append((not missing, f'{sid} 模板', '齊' if not missing else '缺 ' + ', '.join(missing)))
    return rows
