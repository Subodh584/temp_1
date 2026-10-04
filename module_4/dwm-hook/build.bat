@echo off
setlocal
cd /d "%~dp0"

rem -- Find cmake: try PATH first, then search inside Visual Studio install --------
where cmake >nul 2>&1
if errorlevel 1 (
    echo cmake not in PATH, searching Visual Studio installation...
    for /f "tokens=*" %%i in ('powershell -NoProfile -Command ^
        "Get-ChildItem 'C:\Program Files\Microsoft Visual Studio' -Recurse -Filter cmake.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty DirectoryName"') do set "CMAKE_DIR=%%i"
    if not defined CMAKE_DIR (
        echo ERROR: cmake.exe not found under 'C:\Program Files\Microsoft Visual Studio'.
        echo Make sure 'Desktop development with C++' workload is installed in Visual Studio.
        pause & exit /b 1
    )
    set "PATH=%CMAKE_DIR%;%PATH%"
    echo Using cmake from: %CMAKE_DIR%
)

if not exist build mkdir build
cd build

cmake .. -A x64
if errorlevel 1 ( echo CMake configure failed. & pause & exit /b 1 )

cmake --build . --config Release
if errorlevel 1 ( echo Build failed. & pause & exit /b 1 )

echo.
echo Build successful.
echo Output: %~dp0build\Release\liteview_dwm_hook.dll
echo.
echo Copy that DLL next to host.py and re-run the install script (or restart LiteView).
pause
