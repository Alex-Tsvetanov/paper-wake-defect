# P1: a minimal event loop (wakeloop), its wake tests, and the wakeprobe measurement program.
#
#   -DWAKELOOP_DEFECT=ON        the defect arm: the wake is seeded out at one site per backend
#   -DWAKELOOP_SANITIZER=...    address+undefined, thread or memory with clang on Linux;
#                               address with MSVC or clang-cl on Windows
#   -DWAKELOOP_MSAN_LIBCXX=DIR  prefix of an MSan-instrumented libc++ (memory only)
#
# The repository root is the project root, so every first-party file lies under the CMake home
# directory and counts in the inputs hash of the sanitizer gate.
cmake_minimum_required(VERSION 3.25)
project(wakedefect LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

option(WAKELOOP_DEFECT "Seed the wake defect in every backend (measurement only)" OFF)
set(WAKELOOP_SANITIZER "" CACHE STRING "address+undefined, thread, memory (clang, Linux); address (Windows)")
set(WAKELOOP_MSAN_LIBCXX "$ENV{HOME}/opt/libcxx-msan-gcc" CACHE PATH "Prefix of an MSan-instrumented libc++")

# The backends this platform compiles. Recorded in the cache, where the sanitizer gate reads it.
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    set(WAKELOOP_BACKENDS "epoll;io_uring" CACHE INTERNAL "Backends compiled on this platform")
elseif(WIN32)
    set(WAKELOOP_BACKENDS "iocp" CACHE INTERNAL "Backends compiled on this platform")
else()
    message(FATAL_ERROR "wakeloop has backends for Linux and Windows only")
endif()

# Sanitizers apply to every target, so the whole of each binary is instrumented.
if(WAKELOOP_SANITIZER)
    if(MSVC)
        if(NOT WAKELOOP_SANITIZER STREQUAL "address")
            message(FATAL_ERROR "on Windows WAKELOOP_SANITIZER can only be address")
        endif()
        add_compile_options(/fsanitize=address /Zi)
        add_link_options(/INCREMENTAL:NO /DEBUG)
    else()
        string(REPLACE "+" "," _san "${WAKELOOP_SANITIZER}")
        add_compile_options(-fsanitize=${_san} -fno-omit-frame-pointer -fno-sanitize-recover=all -g)
        add_link_options(-fsanitize=${_san})
        if(_san MATCHES "memory")
            if(NOT EXISTS "${WAKELOOP_MSAN_LIBCXX}/include/c++/v1")
                message(FATAL_ERROR "WAKELOOP_SANITIZER=memory needs an instrumented libc++; "
                                    "none at WAKELOOP_MSAN_LIBCXX=${WAKELOOP_MSAN_LIBCXX}")
            endif()
            add_compile_options(-fsanitize-memory-track-origins=2 -stdlib=libc++
                                "-isystem${WAKELOOP_MSAN_LIBCXX}/include/c++/v1")
            add_link_options(-stdlib=libc++ "-L${WAKELOOP_MSAN_LIBCXX}/lib"
                             "-Wl,-rpath,${WAKELOOP_MSAN_LIBCXX}/lib" -lc++abi)
        endif()
    endif()
    message(STATUS "wakeloop: sanitizer ${WAKELOOP_SANITIZER}")
endif()

function(wakeloop_warnings target)
    if(MSVC)
        target_compile_options(${target} PRIVATE /W4)
    else()
        target_compile_options(${target} PRIVATE -Wall -Wextra)
    endif()
endfunction()

find_package(Threads REQUIRED)

add_library(wakeloop STATIC loop/src/common.cpp loop/src/common.hpp
            loop/include/wakeloop/loop.hpp loop/include/wakeloop/task_stack.hpp)
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_sources(wakeloop PRIVATE loop/src/epoll.cpp loop/src/uring.cpp loop/src/linux_fd.hpp)
else()
    target_sources(wakeloop PRIVATE loop/src/iocp.cpp)
endif()
target_include_directories(wakeloop PUBLIC loop/include)
# 0 or 1, always defined: the header reads its value, and each backend source has exactly one
# conditional on it (tests/defect_sites.cmake checks that).
target_compile_definitions(wakeloop PUBLIC WAKELOOP_DEFECT=$<IF:$<BOOL:${WAKELOOP_DEFECT}>,1,0>)
target_link_libraries(wakeloop PUBLIC Threads::Threads)
wakeloop_warnings(wakeloop)
message(STATUS "wakeloop: backends ${WAKELOOP_BACKENDS}, defect ${WAKELOOP_DEFECT}")

enable_testing()
add_subdirectory(tests)
add_subdirectory(bench)
