#include "hook.h"
#include <windows.h>
#include <cstdio>

void DebugLog(const char* msg)
{
    HANDLE f = CreateFileW(L"C:\\Windows\\Temp\\lv_dwm_debug.txt",
                           FILE_APPEND_DATA, FILE_SHARE_READ,
                           nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (f != INVALID_HANDLE_VALUE) {
        DWORD w;
        WriteFile(f, msg, (DWORD)strlen(msg), &w, nullptr);
        CloseHandle(f);
    }
}

static DWORD WINAPI HookThread(LPVOID)
{
    DebugLog("HookThread: started\r\n");
    Sleep(300);
    DebugLog("HookThread: calling InstallHook\r\n");
    InstallHook();
    DebugLog("HookThread: InstallHook returned\r\n");
    return 0;
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hModule);
        // InstallHook() creates COM objects — must not run under the loader lock,
        // so spin it off to a new thread.
        CloseHandle(CreateThread(nullptr, 0, HookThread, nullptr, 0, nullptr));
    } else if (reason == DLL_PROCESS_DETACH) {
        RemoveHook();
    }
    return TRUE;
}
