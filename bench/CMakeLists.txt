# The measurement programs, built from the root project:
#   wakeprobe  post-to-execution delay on an idle wakeloop (single-post protocol S, batch protocol Bt)
#   abpair     the paired A/B throughput harness of H4 (design/v2-defects.md, section 6)

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
message(STATUS "bench: repo ${BENCH_COMMIT} dirty=${BENCH_DIRTY}, code ${BENCH_CODE_COMMIT}")

add_executable(wakeprobe wakeprobe.cpp bench_common.hpp probe_platform.hpp)
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

add_executable(abpair abpair.cpp bench_common.hpp probe_platform.hpp)
target_link_libraries(abpair PRIVATE wakeloop)
target_compile_definitions(abpair PRIVATE
    ABPAIR_BENCH_COMMIT="${BENCH_COMMIT}"
    ABPAIR_BENCH_DIRTY=${BENCH_DIRTY}
    ABPAIR_BENCH_CODE_COMMIT="${BENCH_CODE_COMMIT}"
    ABPAIR_BUILD_TYPE="${CMAKE_BUILD_TYPE}")
wakeloop_warnings(abpair)

# abpair's self-test: count-based windows, no clock reading and no time in the output. The only
# run of abpair on a development machine. Names: abpair_check.<backend>.
foreach(_backend IN LISTS WAKELOOP_BACKENDS)
    add_test(NAME abpair_check.${_backend} COMMAND abpair --backend ${_backend} --check)
    # Serial: its poll-mode workers spin, which would slow the wake tests' threads beside it.
    set_tests_properties(abpair_check.${_backend} PROPERTIES TIMEOUT 120 LABELS "${_backend};abpair" RUN_SERIAL TRUE)
    # With task work: the tasks read the clock to spin, but nothing measured is printed.
    add_test(NAME abpair_check_work.${_backend} COMMAND abpair --backend ${_backend} --check --work-ns 1000)
    set_tests_properties(abpair_check_work.${_backend} PROPERTIES TIMEOUT 120 LABELS "${_backend};abpair" RUN_SERIAL TRUE)
endforeach()

# wakeprobe's self-test: a few units of each protocol, no timing in the output, no record file.
# It checks that every unit is delivered within the timeout and every task runs exactly once. The
# only run of wakeprobe on a development machine. Names: wakeprobe_check.<backend>.<protocol>.<variant>.
foreach(_backend IN LISTS WAKELOOP_BACKENDS)
    foreach(_protocol IN ITEMS single batch)
        foreach(_variant IN ITEMS none absent_wake undrained_edge)
            add_test(NAME wakeprobe_check.${_backend}.${_protocol}.${_variant}
                     COMMAND wakeprobe --backend ${_backend} --protocol ${_protocol} --variant ${_variant}
                             --workers 1 --wait-us 10000 --gap-us 2000 --check)
            # Serial: its posts are timed against the bound, which a busy machine would stretch.
            set_tests_properties(wakeprobe_check.${_backend}.${_protocol}.${_variant}
                                 PROPERTIES TIMEOUT 120 LABELS "wakeprobe" RUN_SERIAL TRUE)
        endforeach()
    endforeach()
endforeach()
