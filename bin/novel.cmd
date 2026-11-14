@echo off
setlocal
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
py -X utf8 -m novel %*
exit /b %ERRORLEVEL%
