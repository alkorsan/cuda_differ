@echo off
SETLOCAL
CD /D "%~dp0"

REM check admin
REM fltmc >nul 2>&1 || ( color 4F & echo. & echo RUNME AS ADMIN & echo. & pause & exit )

call addpython38

python diff.py test_3a.txt test_3b.txt

if not "%DoNotPause%"=="yes" pause
