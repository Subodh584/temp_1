@echo off
setlocal
cd /d "%~dp0"

rem -- Find cmake: try PATH first, then the copy bundled with Visual Studio -------
where cmake >nul 2>&1
if errorlevel 1 (
    set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
    if not exist "%VSWHERE%" set "VSWHERE=%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe"
    if not exist "%VSWHERE%" (
        echo ERROR: vswhere.exe not found. Is Visual Studio installed?
        pause & exit /b 1
    )
    for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -property installationPath`) do set "VS_DIR=%%i"
    set "CMAKE=%VS_DIR%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    if not exist "%CMAKE%" (
        echo ERROR: cmake not found inside Visual Studio. Open VS Installer and ensure
        echo        "Desktop development with C++" workload is installed.
        pause & exit /b 1
    )
    set "PATH=%VS_DIR%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;%PATH%"
    echo Using cmake from: %CMAKE%
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
