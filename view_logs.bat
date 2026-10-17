@echo off
echo Cleaning old logs...
del "%TEMP%\cleaner_tool_*.log" 2>nul
echo.
echo Logs cleared. Now run the app and try to restart as admin.
echo.
echo After it closes, press any key to view the logs...
pause >nul
echo.
echo === STARTUP LOG ===
type "%TEMP%\cleaner_tool_startup.log"
echo.
echo === ELEVATION LOG ===
type "%TEMP%\cleaner_tool_elevation.log"
echo.
echo === SINGLE INSTANCE LOG ===
type "%TEMP%\cleaner_tool_single_instance.log"
echo.
pause
