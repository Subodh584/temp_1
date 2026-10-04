cmake_minimum_required(VERSION 3.20)
project(liteview_dwm_hook LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(liteview_dwm_hook SHARED
    src/dllmain.cpp
    src/hook.cpp
)

target_include_directories(liteview_dwm_hook PRIVATE src)

target_link_libraries(liteview_dwm_hook PRIVATE
    d3d11.lib
    dxgi.lib
)

# Strip debug info from Release build to keep the DLL small.
set_target_properties(liteview_dwm_hook PROPERTIES
    OUTPUT_NAME "liteview_dwm_hook"
    MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
)

target_compile_options(liteview_dwm_hook PRIVATE
    $<$<CONFIG:Release>:/O2 /GS- /Gy>
)
target_link_options(liteview_dwm_hook PRIVATE
    $<$<CONFIG:Release>:/OPT:REF /OPT:ICF>
)
