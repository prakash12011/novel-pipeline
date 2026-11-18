@echo off
setlocal
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
rem Starts the coding LLM on Kaggle (novel kllm up) and waits until it answers; Pi is configured when it does.
py -X utf8 -m novel kllm up %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
