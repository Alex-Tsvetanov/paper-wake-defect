# wakeprobe: post-to-execution delay on an idle wakeloop worker. Built from the root project.

# Provenance, captured at configure time; the run scripts configure every time they build.
find_package(Git REQUIRED)
file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/code_paths.txt" WAKE_CODE_PATHS)
execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${PROJECT_SOURCE_DIR}" rev-parse HEAD
                OUTPUT_VARIABLE BENCH_COMMIT OUTPUT_STRIP_TRAILING_WHITESPACE RESULT_VARIABLE _rc ERROR_QUIET)
if(NOT _rc EQUAL 0)
    set(BENCH_COMMIT "unknown")
endif()
# Dirty: any change to the code paths, tracked or untracked (a new source file is built too).
execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${PROJECT_SOURCE_DIR}" status --porcelain --untracked-files=all
                        -- ${WAKE_CODE_PATHS}
                OUTPUT_VARIABLE _status OUTPUT_STRIP_TRAILING_WHITESPACE RESULT_VARIABLE _rc2 ERROR_QUIET)
if(_rc2 EQUAL 0 AND _status STREQUAL "" AND NOT BENCH_COMMIT STREQUAL "unknown")
    set(BENCH_DIRTY 0)
else()
    set(BENCH_DIRTY 1)
endif()
# The code commit: the last commit that changed a code path. Sanitizer records are named by it.
execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${PROJECT_SOURCE_DIR}" log -1 --format=%H -- ${WAKE_CODE_PATHS}
                OUTPUT_VARIABLE BENCH_CODE_COMMIT OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
if(NOT BENCH_CODE_COMMIT)
    set(BENCH_CODE_COMMIT "unknown")
endif()
message(STATUS "wakeprobe: repo ${BENCH_COMMIT} dirty=${BENCH_DIRTY}, code ${BENCH_CODE_COMMIT}")

add_executable(wakeprobe wakeprobe.cpp probe_platform.hpp)
target_link_libraries(wakeprobe PRIVATE wakeloop)
if(WIN32)
    target_link_libraries(wakeprobe PRIVATE winmm)  # timeBeginPeriod
endif()
target_compile_definitions(wakeprobe PRIVATE
    WAKEPROBE_BENCH_COMMIT="${BENCH_COMMIT}"
    WAKEPROBE_BENCH_DIRTY=${BENCH_DIRTY}
    WAKEPROBE_BENCH_CODE_COMMIT="${BENCH_CODE_COMMIT}"
    WAKEPROBE_BUILD_TYPE="${CMAKE_BUILD_TYPE}")
wakeloop_warnings(wakeprobe)
