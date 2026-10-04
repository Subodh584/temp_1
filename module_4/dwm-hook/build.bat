@echo off
setlocal
cd /d "%~dp0"

where cmake >nul 2>&1
if errorlevel 1 (
    echo ERROR: cmake not found. Install Visual Studio 2022 with C++ workload.
    pause & exit /b 1
)

if not exist build mkdir build
cd build

cmake .. -G "Visual Studio 17 2022" -A x64
if errorlevel 1 ( echo CMake configure failed. & pause & exit /b 1 )

cmake --build . --config Release
if errorlevel 1 ( echo Build failed. & pause & exit /b 1 )

echo.
echo Build successful.
echo Output: %~dp0build\Release\liteview_dwm_hook.dll
echo.
echo Copy that DLL next to host.py and re-run the install script (or restart LiteView).
pause
