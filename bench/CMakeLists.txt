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
        foreach(_variant IN ITEMS none absent_wake undrained_edge lazy_wake_race lazy_wake_wide)
            add_test(NAME wakeprobe_check.${_backend}.${_protocol}.${_variant}
                     COMMAND wakeprobe --backend ${_backend} --protocol ${_protocol} --variant ${_variant}
                             --workers 1 --wait-us 10000 --gap-us 2000 --check)
            # Serial: its posts are timed against the bound, which a busy machine would stretch.
            set_tests_properties(wakeprobe_check.${_backend}.${_protocol}.${_variant}
                                 PROPERTIES TIMEOUT 120 LABELS "wakeprobe" RUN_SERIAL TRUE)
        endforeach()
    endforeach()
    # The campaign's short bound with no calibration: every timeout must cover a whole timer tick
    # (Windows rounds a 1 ms wait up to its resolution), and the timer gap unit must parse.
    foreach(_protocol IN ITEMS single batch)
        add_test(NAME wakeprobe_check.${_backend}.${_protocol}.absent_wake.b1ms
                 COMMAND wakeprobe --backend ${_backend} --protocol ${_protocol} --variant absent_wake
                         --workers 1 --wait-us 1000 --gap-frac-range 0.1:3.0 --gap-unit timer --check)
        set_tests_properties(wakeprobe_check.${_backend}.${_protocol}.absent_wake.b1ms
                             PROPERTIES TIMEOUT 120 LABELS "wakeprobe" RUN_SERIAL TRUE)
    endforeach()
    # Pinned threads. CPUs 0 to 3 must be in the process's allowed set, and CPU 63 must not exist
    # (true of the desktop, the laptop and the development container). A pinned run must pass; a
    # pin that the system refuses must stop the run with its message, before any unit is measured.
    add_test(NAME wakeprobe_check.${_backend}.pinned
             COMMAND wakeprobe --backend ${_backend} --variant none --workers 2 --worker-cpus 0,1 --poster-cpu 2
                     --wait-us 10000 --gap-us 2000 --check)
    add_test(NAME abpair_check.${_backend}.pinned
             COMMAND abpair --backend ${_backend} --check --worker-cpus 0,1 --producer-cpus 2,3)
    set_tests_properties(wakeprobe_check.${_backend}.pinned abpair_check.${_backend}.pinned
                         PROPERTIES TIMEOUT 120 LABELS "pinning" RUN_SERIAL TRUE)
    # Where abpair's pinned threads ended: the summary lists the CPUs in the order of the CPU lists, and a
    # role that is not pinned has null. The expression is the test (ctest then ignores the exit code), so
    # it also asks for check_passed and a null error.
    add_test(NAME abpair_check.${_backend}.cpus_seen.pinned
             COMMAND abpair --backend ${_backend} --check --worker-cpus 0,1 --producer-cpus 2,3)
    set_tests_properties(abpair_check.${_backend}.cpus_seen.pinned PROPERTIES
                         PASS_REGULAR_EXPRESSION "\"worker_cpus_seen\":\\[0,1\\],\"producer_cpus_seen\":\\[2,3\\].*\"check_passed\":true.*\"error\":null"
                         TIMEOUT 120 LABELS "pinning" RUN_SERIAL TRUE)
    add_test(NAME abpair_check.${_backend}.cpus_seen.workers_only
             COMMAND abpair --backend ${_backend} --check --worker-cpus 0,1)
    set_tests_properties(abpair_check.${_backend}.cpus_seen.workers_only PROPERTIES
                         PASS_REGULAR_EXPRESSION "\"worker_cpus_seen\":\\[0,1\\],\"producer_cpus_seen\":null.*\"check_passed\":true.*\"error\":null"
                         TIMEOUT 120 LABELS "pinning" RUN_SERIAL TRUE)
    add_test(NAME abpair_check.${_backend}.cpus_seen.unpinned COMMAND abpair --backend ${_backend} --check)
    set_tests_properties(abpair_check.${_backend}.cpus_seen.unpinned PROPERTIES
                         PASS_REGULAR_EXPRESSION "\"worker_cpus_seen\":null,\"producer_cpus_seen\":null.*\"check_passed\":true.*\"error\":null"
                         TIMEOUT 120 LABELS "${_backend};abpair" RUN_SERIAL TRUE)
    add_test(NAME wakeprobe_check.${_backend}.pin_refused.worker
             COMMAND wakeprobe --backend ${_backend} --variant none --workers 1 --worker-cpus 63
                     --wait-us 10000 --gap-us 2000 --check)
    add_test(NAME wakeprobe_check.${_backend}.pin_refused.poster
             COMMAND wakeprobe --backend ${_backend} --variant none --workers 1 --worker-cpus 0 --poster-cpu 63
                     --wait-us 10000 --gap-us 2000 --check)
    add_test(NAME abpair_check.${_backend}.pin_refused
             COMMAND abpair --backend ${_backend} --check --worker-cpus 63,0 --producer-cpus 2,3)
    set_tests_properties(wakeprobe_check.${_backend}.pin_refused.worker PROPERTIES
                         PASS_REGULAR_EXPRESSION "FAIL: .*cannot pin worker 0 to CPU 63")
    set_tests_properties(wakeprobe_check.${_backend}.pin_refused.poster PROPERTIES
                         PASS_REGULAR_EXPRESSION "FAIL: .*cannot pin the poster to CPU 63")
    set_tests_properties(abpair_check.${_backend}.pin_refused PROPERTIES
                         PASS_REGULAR_EXPRESSION "cannot pin worker 0 to CPU 63")
    set_tests_properties(wakeprobe_check.${_backend}.pin_refused.worker wakeprobe_check.${_backend}.pin_refused.poster
                         abpair_check.${_backend}.pin_refused PROPERTIES TIMEOUT 120 LABELS "pinning" RUN_SERIAL TRUE)
endforeach()

# abpair's failure checks: --check-fault breaks one thing on purpose, and the run must stop with the FAIL
# line of the window instead of hanging (ctest's timeout would fail a hang). Each waits out one hard limit
# (10 s) and the two with a thread that never leaves a second one. The window logic is the same on every
# backend, so these run on the first. Names: abpair_check.<backend>.fault.<fault>.
function(abpair_fault_test backend fault what)
    add_test(NAME abpair_check.${backend}.fault.${fault}
             COMMAND abpair --backend ${backend} --check --check-fault ${fault})
    set_tests_properties(abpair_check.${backend}.fault.${fault} PROPERTIES
                         PASS_REGULAR_EXPRESSION "FAIL: window 0 did not finish ${what} within the hard limit"
                         TIMEOUT 120 LABELS "${backend};abpair" RUN_SERIAL TRUE)
endfunction()
list(GET WAKELOOP_BACKENDS 0 _fault_backend)
abpair_fault_test(${_fault_backend} lost_task "its drain")
abpair_fault_test(${_fault_backend} stuck_task "its drain")
abpair_fault_test(${_fault_backend} stuck_start "starting up")

# The H5 runner's dry run: it writes its plan, prints every wakeprobe command line and runs nothing.
# The script exits non-zero unless the plan has the expected cells: the 2026-09 designs, less the PM QoS
# and random-gap designs (136 on Windows, 140 on Linux). Name: run_h5_dry_run.
if(WIN32)
    add_test(NAME run_h5_dry_run
             COMMAND powershell.exe -NoProfile -ExecutionPolicy Bypass -File "${CMAKE_CURRENT_SOURCE_DIR}/run_h5.ps1"
                     -BuildDir "${PROJECT_BINARY_DIR}" -OutDir "${PROJECT_BINARY_DIR}/h5-dry-run" -DryRun -ExpectCells 136)
else()
    add_test(NAME run_h5_dry_run
             COMMAND bash "${CMAKE_CURRENT_SOURCE_DIR}/run_h5.sh"
                     --build-dir "${PROJECT_BINARY_DIR}" --out-dir "${PROJECT_BINARY_DIR}/h5-dry-run" --dry-run --expect-cells 140)
endif()
set_tests_properties(run_h5_dry_run PROPERTIES TIMEOUT 120 LABELS "h5")

# The campaign runner (run_campaign.py). A dry run of the sample plan prints every command and runs nothing;
# the script exits non-zero unless the plan has the expected 12 processes. The unit tests start stub
# programs only. Nothing is timed. Names: run_campaign_dry_run, run_campaign_unittest.
find_package(Python3 COMPONENTS Interpreter QUIET)
if(Python3_FOUND)
    add_test(NAME run_campaign_dry_run
             COMMAND "${Python3_EXECUTABLE}" -B "${CMAKE_CURRENT_SOURCE_DIR}/run_campaign.py"
                     --plan "${CMAKE_CURRENT_SOURCE_DIR}/testdata/plan-sample.jsonl"
                     --build-dir "${PROJECT_BINARY_DIR}" --out-dir "${PROJECT_BINARY_DIR}/campaign-dry-run"
                     --dry-run --expect-processes 12)
    add_test(NAME run_campaign_unittest
             COMMAND "${Python3_EXECUTABLE}" -B "${CMAKE_CURRENT_SOURCE_DIR}/test_run_campaign.py")
    set_tests_properties(run_campaign_dry_run run_campaign_unittest
                         PROPERTIES TIMEOUT 120 LABELS "campaign" RUN_SERIAL TRUE)
else()
    message(STATUS "bench: no Python 3 found; the run_campaign tests are not defined")
endif()
