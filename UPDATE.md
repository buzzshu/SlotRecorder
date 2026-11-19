# 更新流程(同事不用隨身碟,直接線上更新)

更新分兩種,**分開處理**:

| | 內容 | 多久一次 | 怎麼送 |
|---|---|---|---|
| **程式碼更新** | `core/`、`gui.py`、`apps/`、`*.bat`… | 常常 | **git(本文件)**:同事點 `update.bat` 或工具裡「更新」 |
| **Bootstrap / APK** | `apk_cache/`(大檔)、首次環境 | 很少 | 一次性:zip 給全新電腦;app 之後靠 Play/主更新台維持 |

> repo 只放**程式碼**(`.gitignore` 排除 videos/logs/config.json/apk_cache/python/tools)。所以 `git pull` 只有幾 KB,**不會覆蓋同事的錄影、設定、app**。
> repo 是**公開**的(`github.com/buzzshu/SlotRecorder`)→ 同事**免金鑰**直接 pull。

---

## 管理者(buzzliu):每次改版

```
# 改完程式碼、bump core/version.py、寫 CHANGELOG
git add -A && git commit -m "v1.9.x ..." && git push
```

同事端就會看到「有更新」。做新電腦的 bootstrap 包:`python -m core.pack`(整包 zip,含 apk_cache)。

---

## 同事端

- **全新電腦第一次**:
  1. 裝 LDPlayer(模擬器)。
  2. 解壓收到的 zip。
  3. 跑 `install.bat`(連網,自動裝 Python + 套件 + scrcpy + ffmpeg)。
  4. 跑 `setup_git.bat`(把資料夾接上更新庫,免金鑰;需要電腦有 Git for Windows,沒有會提示下載)。
  5. 跑 `run.bat` 開工具。
- **之後更新**:點 `update.bat`,或在工具裡按「更新」(有新版會自動提示)。更新完重開工具即可。
- 你的錄影、設定、模擬器 app 都不受更新影響。
