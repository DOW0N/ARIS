@echo off
chcp 65001 > nul
rem ARIS.exe 시험 빌드 (윈도우 전용). 결과: dist\ARIS\ARIS.exe
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller || goto :fail
python -m PyInstaller --noconfirm --clean --onedir --name ARIS ^
  --add-data "static;static" --add-data "TC_template.xlsx;." ^
  --collect-all playwright --collect-submodules appium --collect-submodules selenium ^
  --copy-metadata Appium-Python-Client ^
  web.py || goto :fail
rem 사용자 파일은 exe 옆에 둔다 (jira.env는 토큰이 있어 다른 사람에게 보내는 폴더에는 넣지 말 것)
if exist jira.env copy /y jira.env dist\ARIS\ > nul
if exist scripts xcopy /e /i /y scripts dist\ARIS\scripts > nul
echo.
echo 완료: dist\ARIS\ARIS.exe 를 더블클릭해서 실행하세요.
pause
exit /b 0
:fail
echo.
echo 빌드에 실패했어요. 위쪽 빨간 오류 메시지를 캡처해서 보내 주세요.
pause
exit /b 1
