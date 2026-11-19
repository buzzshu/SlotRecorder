"""Self-update over git (private GitHub repo). The GUI shows 有更新 when origin has a newer version than this
copy, and runs `git pull --ff-only` on request. The repo is CODE ONLY (videos / logs / config.json / apk_cache /
python / tools are .gitignored), so a pull is a few KB and never touches a colleague's recordings or settings.

One-time per machine: setup_git.bat attaches the repo with a read-only token (stored only in the local
.git/config, never in the repo). After that this module, update.bat, or the GUI button all just `git pull`.
"""
import os, re, subprocess
from .config import ROOT
from .version import VERSION

_NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _git(*a, timeout=60):
    try:
        return subprocess.run(['git', '-C', ROOT, *a], capture_output=True, text=True,
                              creationflags=_NOWIN, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        r = subprocess.CompletedProcess(a, 1, '', str(e)); return r


def _vt(v):
    return tuple(int(x) for x in re.findall(r'\d+', v or ''))


def is_repo():
    return _git('rev-parse', '--is-inside-work-tree').stdout.strip() == 'true'


def _remote_version():
    """VERSION on origin's default branch, or None (offline / not a clone / fetch failed)."""
    if _git('fetch', '--quiet', timeout=30).returncode: return None
    for ref in ('origin/HEAD', 'origin/main', 'origin/master'):
        r = _git('show', f'{ref}:core/version.py')
        if r.returncode == 0:
            m = re.search(r"VERSION\s*=\s*'([^']+)'", r.stdout)
            if m: return m.group(1)
    return None


def check_update():
    """-> (status, local, remote). status: 'update' | 'current' | 'not_repo' | 'offline' | 'no_git'."""
    if _git('--version').returncode: return ('no_git', VERSION, None)
    if not is_repo(): return ('not_repo', VERSION, None)
    rv = _remote_version()
    if rv is None: return ('offline', VERSION, None)
    return ('update' if _vt(rv) > _vt(VERSION) else 'current', VERSION, rv)


def do_update():
    """git pull --ff-only. -> (ok, message). config.json / videos / logs are gitignored -> never clobbered."""
    if _git('--version').returncode: return (False, '這台沒有 git(請先裝 Git for Windows)')
    if not is_repo(): return (False, '這份不是從 git 複製的 → 請先跑一次 setup_git.bat')
    r = _git('pull', '--ff-only', timeout=120)
    out = (r.stdout + '\n' + r.stderr).strip()
    if r.returncode == 0:
        return (True, out or '已是最新')
    if re.search(r'local changes|overwritten|unmerged|diverge', out):
        return (False, '本機有改動擋住更新(工具檔請勿手改)→ ' + out)
    return (False, out or 'git pull 失敗')


if __name__ == '__main__':
    st, lo, rv = check_update()
    print(f'status={st}  local={lo}  remote={rv}')
    if st == 'update':
        ok, msg = do_update(); print(('updated: ' if ok else 'failed: ') + msg)
