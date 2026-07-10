@echo off
rem Windows ログオン時に LaunchIt を自動起動するショートカットを登録する
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\LaunchIt.lnk'); $s.TargetPath='%~dp0LaunchIt.vbs'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0launchit.ico,0'; $s.Save()"
echo LaunchIt をスタートアップに登録しました。
pause
