@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo Locating Visual Studio...

rem Find vcvars64.bat anywhere under the VS install directory
for /f "tokens=*" %%i in ('powershell -NoProfile -Command ^
    "Get-ChildItem 'C:\Program Files\Microsoft Visual Studio' -Recurse -Filter vcvars64.bat -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName"') do set "VCVARS=%%i"

if not defined VCVARS (
    echo ERROR: Could not find vcvars64.bat.
    echo Make sure "Desktop development with C++" workload is installed in Visual Studio.
    pause & exit /b 1
)

echo Found: %VCVARS%
call "%VCVARS%" >nul 2>&1

if not exist build mkdir build

echo Compiling liteview_dwm_hook.dll...
cl.exe /nologo /LD /O2 /W3 /EHsc ^
    /I src ^
    src\dllmain.cpp src\hook.cpp ^
    /Fe:build\liteview_dwm_hook.dll ^
    /Fo:build\ ^
    /link d3d11.lib dxgi.lib user32.lib kernel32.lib ^
    /DLL /INCREMENTAL:NO /OPT:REF

if errorlevel 1 (
    echo.
    echo Build FAILED.
    pause & exit /b 1
)

echo.
echo Build successful.
echo Output: %~dp0build\liteview_dwm_hook.dll
echo.
echo Restart LiteView as Administrator to activate the DWM hook.
pause
