# 更新流程(同事不用隨身碟,直接線上更新)

更新分兩種,**分開處理**:

| | 內容 | 多久一次 | 怎麼送 |
|---|---|---|---|
| **程式碼更新** | `core/`、`gui.py`、`apps/`、`*.bat`… | 常常 | **git(本文件)**:同事點 `update.bat` 或工具裡「更新」 |
| **Bootstrap / APK** | `python/`、`tools/`、`apk_cache/`(大檔) | 很少 | 一次性:隨身碟/雲端給全新電腦;app 之後靠 Play/主更新台維持 |

> repo 只放**程式碼**(`.gitignore` 排除 videos/logs/config.json/apk_cache/python/tools)。所以 `git pull` 只有幾 KB,**不會覆蓋同事的錄影、設定、app**。

---

## 管理者(buzzliu)一次性設定

1. **建私有 repo + 推上去**(已由工具協助完成;手動指令備查):
   ```
   gh repo create buzzshu/SlotRecorder --private --source . --remote origin --push
   ```
2. **產生唯讀更新金鑰**(fine-grained PAT,只給這個 repo 唯讀):
   - GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token
   - Resource owner = buzzshu;Repository access = Only select repositories → **SlotRecorder**
   - Permissions → Repository permissions → **Contents: Read-only**(其餘不給)
   - 產生後複製那串 `github_pat_...`
3. **把金鑰填進 `setup_git.bat`**:用記事本打開,`set TOKEN=` 那行換成金鑰。
4. **做一次性 bootstrap 包**給同事(只需一次):把整個 SlotRecorder 資料夾(含已填好金鑰的 `setup_git.bat`)壓成 zip,放雲端/隨身碟給同事。
   - `setup_git.bat` 含金鑰 → **不要**上傳到 repo(已被 `.gitignore` 排除)。金鑰唯讀、可隨時在 GitHub 撤銷/輪替。

### 之後每次改版(管理者)
```
# 改完程式碼、bump core/version.py、寫 CHANGELOG
git add -A && git commit -m "v1.9.x ..." && git push
```
同事端就會看到「有更新」。

---

## 同事端

- **第一次**:解壓收到的包 → 跑一次 **`setup_git.bat`**(把資料夾接上更新庫)。需要電腦有 Git for Windows(沒有會提示下載)。
- **之後更新**:點 **`update.bat`**,或在工具裡按 **「更新」**(有新版會自動提示)。更新完重開工具即可。
- 你的錄影、設定、模擬器 app 都不受更新影響。

---

## 金鑰外洩怎麼辦
GitHub → 該 token → Revoke;重發一把新的,更新 `setup_git.bat` 重送即可(唯讀,風險低)。
