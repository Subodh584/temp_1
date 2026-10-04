#pragma once
#include <cstdint>

// Shared memory layout written by the DLL (inside DWM) and read by LiteView (Python).
// Name is in the Local\ namespace — both DWM and LiteView run in the same user session.

static constexpr wchar_t  SHM_NAME[]     = L"Local\\LiteViewFrame";
static constexpr uint32_t SHM_MAGIC      = 0x4C564448; // 'LVDH'
static constexpr size_t   SHM_PIXEL_MAX  = 3840 * 2160 * 4; // worst-case 4K BGRA
static constexpr size_t   SHM_TOTAL_SIZE = 64 + SHM_PIXEL_MAX; // header + pixels

// Pixel format codes written into FrameHeader::format.
enum PixFmt : uint32_t {
    PIXFMT_BGRA8   = 0, // DXGI_FORMAT_B8G8R8A8_UNORM  (most common for DWM SDR)
    PIXFMT_RGBA8   = 1, // DXGI_FORMAT_R8G8B8A8_UNORM
    PIXFMT_RGB10A2 = 2, // DXGI_FORMAT_R10G10B10A2_UNORM (HDR displays)
    PIXFMT_UNKNOWN = 0xFF,
};

#pragma pack(push, 1)
struct FrameHeader {
    uint32_t magic;    // SHM_MAGIC when the DLL has initialised
    uint32_t width;
    uint32_t height;
    uint32_t format;   // PixFmt
    uint64_t frameNum; // increments every time a new frame is written
    uint32_t ready;    // 1 = pixel data is valid for the current frameNum
    uint32_t _pad[9];  // pad to 64 bytes
};
#pragma pack(pop)
static_assert(sizeof(FrameHeader) == 64, "FrameHeader must be 64 bytes");
