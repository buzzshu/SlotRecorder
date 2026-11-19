@echo off
rem SlotRecorder 一鍵更新:從私有 repo 拉最新「程式碼」(幾 KB)。
rem 不會動到你的 videos / logs / config.json / 模擬器 app(這些都不在 repo 裡)。
rem 第一次請先跑 setup_git.bat;之後更新點這個就好。
cd /d "%~dp0"
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo [!] 這份不是從 git 複製的 ^-^> 請先跑一次 setup_git.bat
  pause & exit /b 1
)
echo 更新 SlotRecorder ...
git pull --ff-only
if errorlevel 1 (
  echo.
  echo [!] 更新沒成功。若訊息提到 local changes,代表工具檔被改過^(不該手改^);
  echo     或用「設定」裡的還原,再試一次。
  pause & exit /b 1
)
echo.
echo 完成。請重新開啟 SlotRecorder。
pause
