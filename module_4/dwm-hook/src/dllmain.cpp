#include "hook.h"
#include <windows.h>

static DWORD WINAPI HookThread(LPVOID)
{
    // Short sleep so DWM finishes creating its own swap chain before we patch.
    Sleep(300);
    InstallHook();
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
