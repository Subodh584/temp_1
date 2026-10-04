// hook.cpp — IDXGISwapChain::Present / Present1 vtable hook inside dwm.exe.
//
// Strategy:
//   1. Create a tiny hidden window + dummy D3D11 device + dummy IDXGISwapChain.
//      Because all IDXGISwapChain objects share the same COM vtable (stored in
//      DXGI.dll's .rdata), patching vtable[8] (Present) and vtable[22] (Present1)
//      on the dummy chain also patches DWM's real swap chain automatically.
//   2. On every Present call our hook fires inside DWM's process.  At this point
//      DWM's back buffer already holds the fully-composited frame — including the
//      real pixels of WDA_EXCLUDEFROMCAPTURE windows — BEFORE the GPU driver's
//      DXGI-capture sanitisation runs.  We copy it to CPU memory and write it
//      to a named shared-memory mapping that LiteView reads.
//
// No kernel driver, no signing required.  Needs Administrator to inject.

#include "hook.h"
#include "shared.h"

#include <d3d11.h>
#include <dxgi1_2.h>
#include <windows.h>
#include <wrl/client.h>
#include <cstring>

extern void DebugLog(const char* msg);

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")

using Microsoft::WRL::ComPtr;

// --------------------------------------------------------------------------
// Function-pointer types for the two Present variants
// --------------------------------------------------------------------------
using PFN_Present  = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain*,  UINT, UINT);
using PFN_Present1 = HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain1*, UINT, UINT,
                                                  const DXGI_PRESENT_PARAMETERS*);

static PFN_Present  g_origPresent  = nullptr;
static PFN_Present1 g_origPresent1 = nullptr;

// --------------------------------------------------------------------------
// Shared-memory state
// --------------------------------------------------------------------------
static HANDLE       g_hMap    = nullptr;
static FrameHeader* g_header  = nullptr;
static uint8_t*     g_pixels  = nullptr; // points just past the header

// --------------------------------------------------------------------------
// D3D11 objects (obtained from DWM's own swap chain on first call)
// --------------------------------------------------------------------------
static ComPtr<ID3D11Device>        g_dev;
static ComPtr<ID3D11DeviceContext> g_ctx;
static ComPtr<ID3D11Texture2D>     g_staging;
static UINT      g_stagingW   = 0;
static UINT      g_stagingH   = 0;
static DXGI_FORMAT g_stagingFmt = DXGI_FORMAT_UNKNOWN;

// --------------------------------------------------------------------------
// Shared-memory helpers
// --------------------------------------------------------------------------
static bool OpenSharedMemory()
{
    DebugLog("OpenSharedMemory: CreateFileMappingW...\r\n");
    g_hMap = CreateFileMappingW(
        INVALID_HANDLE_VALUE, nullptr,
        PAGE_READWRITE,
        0, static_cast<DWORD>(SHM_TOTAL_SIZE),
        SHM_NAME);
    if (!g_hMap) {
        char buf[64];
        wsprintfA(buf, "OpenSharedMemory: FAILED err=%lu\r\n", GetLastError());
        DebugLog(buf);
        return false;
    }
    DebugLog("OpenSharedMemory: MapViewOfFile...\r\n");
    void* p = MapViewOfFile(g_hMap, FILE_MAP_WRITE, 0, 0, SHM_TOTAL_SIZE);
    if (!p) { CloseHandle(g_hMap); g_hMap = nullptr; DebugLog("MapViewOfFile FAILED\r\n"); return false; }

    g_header = static_cast<FrameHeader*>(p);
    g_pixels = reinterpret_cast<uint8_t*>(g_header) + sizeof(FrameHeader);
    ZeroMemory(g_header, sizeof(FrameHeader));
    g_header->magic = SHM_MAGIC;
    DebugLog("OpenSharedMemory: OK\r\n");
    return true;
}

// --------------------------------------------------------------------------
// Map DXGI format → PixFmt code
// --------------------------------------------------------------------------
static PixFmt DxgiToPixFmt(DXGI_FORMAT fmt)
{
    switch (fmt) {
    case DXGI_FORMAT_B8G8R8A8_UNORM:
    case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:
        return PIXFMT_BGRA8;
    case DXGI_FORMAT_R8G8B8A8_UNORM:
    case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB:
        return PIXFMT_RGBA8;
    case DXGI_FORMAT_R10G10B10A2_UNORM:
        return PIXFMT_RGB10A2;
    default:
        return PIXFMT_UNKNOWN;
    }
}

// --------------------------------------------------------------------------
// Core capture: copy swap-chain back buffer → shared memory
// --------------------------------------------------------------------------
static void CaptureFrame(IDXGISwapChain* sc)
{
    if (!g_header || !g_pixels) return;

    // Obtain D3D11 device + context from DWM's swap chain on first call.
    if (!g_dev) {
        if (FAILED(sc->GetDevice(IID_PPV_ARGS(&g_dev)))) return;
        g_dev->GetImmediateContext(&g_ctx);
    }

    ComPtr<ID3D11Texture2D> backBuf;
    if (FAILED(sc->GetBuffer(0, IID_PPV_ARGS(&backBuf)))) return;

    D3D11_TEXTURE2D_DESC desc{};
    backBuf->GetDesc(&desc);

    // (Re)create staging texture when resolution or format changes.
    if (!g_staging || g_stagingW != desc.Width ||
        g_stagingH != desc.Height || g_stagingFmt != desc.Format)
    {
        g_staging.Reset();
        D3D11_TEXTURE2D_DESC sd = desc;
        sd.MipLevels      = 1;
        sd.ArraySize      = 1;
        sd.SampleDesc     = { 1, 0 };
        sd.Usage          = D3D11_USAGE_STAGING;
        sd.BindFlags      = 0;
        sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        sd.MiscFlags      = 0;
        if (FAILED(g_dev->CreateTexture2D(&sd, nullptr, &g_staging))) return;
        g_stagingW   = desc.Width;
        g_stagingH   = desc.Height;
        g_stagingFmt = desc.Format;
    }

    // Resolve MSAA if needed, then copy to staging.
    ComPtr<ID3D11Texture2D> src = backBuf;
    if (desc.SampleDesc.Count > 1) {
        D3D11_TEXTURE2D_DESC rd = desc;
        rd.SampleDesc = { 1, 0 };
        rd.Usage = D3D11_USAGE_DEFAULT;
        rd.CPUAccessFlags = 0;
        ComPtr<ID3D11Texture2D> resolved;
        if (FAILED(g_dev->CreateTexture2D(&rd, nullptr, &resolved))) return;
        g_ctx->ResolveSubresource(resolved.Get(), 0, backBuf.Get(), 0, desc.Format);
        src = resolved;
    }
    g_ctx->CopyResource(g_staging.Get(), src.Get());

    D3D11_MAPPED_SUBRESOURCE mapped{};
    if (FAILED(g_ctx->Map(g_staging.Get(), 0, D3D11_MAP_READ, 0, &mapped))) return;

    // Mark updating.
    g_header->ready = 0;
    g_header->width  = desc.Width;
    g_header->height = desc.Height;
    g_header->format = static_cast<uint32_t>(DxgiToPixFmt(desc.Format));

    // Copy row-by-row (GPU row pitch may include padding).
    const UINT rowBytes = desc.Width * 4;
    const auto* src_row = static_cast<const uint8_t*>(mapped.pData);
    uint8_t*    dst_row = g_pixels;
    for (UINT y = 0; y < desc.Height; ++y) {
        memcpy(dst_row, src_row, rowBytes);
        src_row += mapped.RowPitch;
        dst_row += rowBytes;
    }

    g_ctx->Unmap(g_staging.Get(), 0);

    // Mark ready — Python polls this.
    g_header->frameNum++;
    g_header->ready = 1;
}

// --------------------------------------------------------------------------
// Hooked Present / Present1
// --------------------------------------------------------------------------
static HRESULT STDMETHODCALLTYPE HookedPresent(
    IDXGISwapChain* sc, UINT sync, UINT flags)
{
    CaptureFrame(sc);
    return g_origPresent(sc, sync, flags);
}

static HRESULT STDMETHODCALLTYPE HookedPresent1(
    IDXGISwapChain1* sc, UINT sync, UINT flags,
    const DXGI_PRESENT_PARAMETERS* params)
{
    CaptureFrame(sc);
    return g_origPresent1(sc, sync, flags, params);
}

// --------------------------------------------------------------------------
// VTable patch helper (makes the vtable page temporarily writable)
// --------------------------------------------------------------------------
static bool PatchVTable(void** vtable, int idx, void* newFn, void** origOut)
{
    DWORD old{};
    if (!VirtualProtect(&vtable[idx], sizeof(void*), PAGE_EXECUTE_READWRITE, &old))
        return false;
    *origOut = vtable[idx];
    vtable[idx] = newFn;
    VirtualProtect(&vtable[idx], sizeof(void*), old, &old);
    return true;
}

// --------------------------------------------------------------------------
// Install / remove
// --------------------------------------------------------------------------
void InstallHook()
{
    DebugLog("InstallHook: start\r\n");
    if (!OpenSharedMemory()) { DebugLog("InstallHook: OpenSharedMemory FAILED\r\n"); return; }

    DebugLog("InstallHook: D3D11CreateDevice (WARP)...\r\n");
    ComPtr<ID3D11Device> dummyDev;
    D3D_FEATURE_LEVEL    fl;
    HRESULT hr = D3D11CreateDevice(
        nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0,
        nullptr, 0, D3D11_SDK_VERSION,
        &dummyDev, &fl, nullptr);
    if (FAILED(hr)) {
        char buf[64]; wsprintfA(buf, "InstallHook: D3D11CreateDevice FAILED hr=0x%08X\r\n", hr);
        DebugLog(buf); return;
    }

    DebugLog("InstallHook: getting DXGI factory...\r\n");
    ComPtr<IDXGIDevice>   dxgiDev;
    ComPtr<IDXGIAdapter>  dxgiAdapter;
    ComPtr<IDXGIFactory2> factory;
    if (FAILED(dummyDev.As(&dxgiDev)))                          { DebugLog("As(IDXGIDevice) FAILED\r\n"); return; }
    if (FAILED(dxgiDev->GetAdapter(&dxgiAdapter)))              { DebugLog("GetAdapter FAILED\r\n"); return; }
    if (FAILED(dxgiAdapter->GetParent(IID_PPV_ARGS(&factory)))) { DebugLog("GetParent FAILED\r\n"); return; }

    DebugLog("InstallHook: CreateSwapChainForComposition...\r\n");
    DXGI_SWAP_CHAIN_DESC1 scd{};
    scd.Width        = 1;
    scd.Height       = 1;
    scd.Format       = DXGI_FORMAT_B8G8R8A8_UNORM;
    scd.SampleDesc   = {1, 0};
    scd.BufferCount  = 2;
    scd.BufferUsage  = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    scd.SwapEffect   = DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL;
    scd.AlphaMode    = DXGI_ALPHA_MODE_UNSPECIFIED;
    scd.Scaling      = DXGI_SCALING_STRETCH;

    ComPtr<IDXGISwapChain1> dummySC;
    hr = factory->CreateSwapChainForComposition(
             dummyDev.Get(), &scd, nullptr, &dummySC);
    if (FAILED(hr)) {
        char buf[64]; wsprintfA(buf, "InstallHook: CreateSCForComp FAILED hr=0x%08X\r\n", hr);
        DebugLog(buf); return;
    }

    DebugLog("InstallHook: patching vtable...\r\n");
    void** vtable = *reinterpret_cast<void***>(dummySC.Get());
    PatchVTable(vtable,  8, reinterpret_cast<void*>(HookedPresent),
                reinterpret_cast<void**>(&g_origPresent));
    PatchVTable(vtable, 22, reinterpret_cast<void*>(HookedPresent1),
                reinterpret_cast<void**>(&g_origPresent1));
    DebugLog("InstallHook: DONE — hooks active\r\n");
}

void RemoveHook()
{
    // Restore vtable entries if we saved the originals.
    // (In practice the DLL is never explicitly unloaded while DWM is running,
    //  but clean up anyway.)
    if (g_origPresent) {
        // We'd need the vtable pointer again — skip for now; process exit cleans up.
    }
    if (g_header) {
        g_header->ready = 0;
        g_header->magic = 0;
        UnmapViewOfFile(g_header);
        g_header  = nullptr;
        g_pixels  = nullptr;
    }
    if (g_hMap) {
        CloseHandle(g_hMap);
        g_hMap = nullptr;
    }
}
