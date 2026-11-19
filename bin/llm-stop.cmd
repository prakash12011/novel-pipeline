@echo off
setlocal
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
rem Stops the coding LLM on Kaggle now (novel kllm down), so the idle wait does not use quota.
py -X utf8 -m novel kllm down %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
