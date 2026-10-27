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

# --validate-args of both programs (label "validate"). The flag checks the arguments of a measuring run as the
# run does and measures nothing: no loop, thread, pin, clock or file. The cases run each program with the flag
# and check its exit code, its exact output and that --out was not created. ctest's PASS_REGULAR_EXPRESSION
# ignores the exit code and cannot test "exactly valid", so a small driver script does it; it is written into
# the build tree here. Names: validate_args.<program>.<backend>.<case> for what holds on every backend, and
# validate_args.<program>.<case> for the rest.
set(VALIDATE_DRIVER "${CMAKE_CURRENT_BINARY_DIR}/validate_args_driver.cmake")
file(WRITE "${VALIDATE_DRIVER}" [=[
# Runs a program and checks the result. Written by bench/CMakeLists.txt.
#   cmake -DEXPECT_RC=N [-DEXPECT_SUB=TEXT] [-DEXPECT_USAGE=0|1] [-DOUT_FILE=PATH [-DKEEP=1]] -P this.cmake -- PROGRAM ARGS...
# Exit code 0: stdout is exactly "valid" and stderr is empty. Otherwise stdout is empty and stderr holds
# TEXT, with the usage block (EXPECT_USAGE=1) or without it (0). With OUT_FILE the file must not exist
# afterwards. With KEEP it holds "keep" before the run and must still hold exactly that.
set(_cmd "")
set(_collect FALSE)
math(EXPR _last "${CMAKE_ARGC} - 1")
foreach(_i RANGE 0 ${_last})
    if(_collect)
        list(APPEND _cmd "${CMAKE_ARGV${_i}}")
    elseif("${CMAKE_ARGV${_i}}" STREQUAL "--")
        set(_collect TRUE)
    endif()
endforeach()
if("${_cmd}" STREQUAL "")
    message(FATAL_ERROR "validate driver: no program after --")
endif()

if(OUT_FILE)
    get_filename_component(_dir "${OUT_FILE}" DIRECTORY)
    file(MAKE_DIRECTORY "${_dir}")
    file(REMOVE "${OUT_FILE}")
    if(KEEP)
        file(WRITE "${OUT_FILE}" "keep\n")
    endif()
endif()

execute_process(COMMAND ${_cmd} RESULT_VARIABLE _rc OUTPUT_VARIABLE _out ERROR_VARIABLE _err TIMEOUT 60)

set(_bad "")
if(NOT "${_rc}" STREQUAL "${EXPECT_RC}")
    string(APPEND _bad "  exit code [${_rc}], expected ${EXPECT_RC}\n")
endif()
if("${EXPECT_RC}" STREQUAL "0")
    if(NOT _out MATCHES "^valid\r?\n$")
        string(APPEND _bad "  stdout is not exactly valid: [${_out}]\n")
    endif()
    if(NOT "${_err}" STREQUAL "")
        string(APPEND _bad "  stderr is not empty: [${_err}]\n")
    endif()
else()
    if(NOT "${_out}" STREQUAL "")
        string(APPEND _bad "  stdout is not empty: [${_out}]\n")
    endif()
    string(FIND "${_err}" "${EXPECT_SUB}" _at)
    if(_at LESS 0)
        string(APPEND _bad "  stderr lacks [${EXPECT_SUB}]: [${_err}]\n")
    endif()
    if(DEFINED EXPECT_USAGE)
        string(FIND "${_err}" "usage: " _usage_at)
        if(EXPECT_USAGE AND _usage_at LESS 0)
            string(APPEND _bad "  stderr has no usage block\n")
        elseif(NOT EXPECT_USAGE AND NOT _usage_at LESS 0)
            string(APPEND _bad "  stderr has a usage block, which a run does not print for this refusal\n")
        endif()
    endif()
endif()
if(OUT_FILE)
    if(KEEP)
        file(READ "${OUT_FILE}" _kept)
        if(NOT "${_kept}" STREQUAL "keep\n")
            string(APPEND _bad "  the existing --out file was changed: [${_kept}]\n")
        endif()
    elseif(EXISTS "${OUT_FILE}")
        string(APPEND _bad "  --out was created: ${OUT_FILE}\n")
    endif()
endif()
if(NOT "${_bad}" STREQUAL "")
    string(JOIN " " _shown ${_cmd})
    message(FATAL_ERROR "validate driver: ${_shown}\n${_bad}")
endif()
]=])

# One case. ARGS are the program's arguments; the flag and, unless NO_OUT, an --out path under the build tree
# are appended. RAW leaves the flag out (a plain run that must be refused before it measures). RC is the expected
# exit code (default 0, "valid"), SUB the text that stderr must hold, USAGE 1 or 0 whether it holds the usage block.
function(validate_case name program)
    cmake_parse_arguments(V "NO_OUT;RAW;KEEP" "RC;SUB;USAGE" "ARGS" ${ARGN})
    if(NOT DEFINED V_RC)
        set(V_RC 0)
    endif()
    set(_driver ${CMAKE_COMMAND} -DEXPECT_RC=${V_RC} "-DEXPECT_SUB=${V_SUB}")
    if(DEFINED V_USAGE)
        list(APPEND _driver -DEXPECT_USAGE=${V_USAGE})
    endif()
    set(_args ${V_ARGS})
    if(NOT V_RAW)
        list(APPEND _args --validate-args)
    endif()
    if(NOT V_NO_OUT)
        set(_out "${PROJECT_BINARY_DIR}/validate-args/${name}.jsonl")
        list(APPEND _driver "-DOUT_FILE=${_out}")
        if(V_KEEP)
            list(APPEND _driver -DKEEP=1)
        endif()
        list(APPEND _args --out "${_out}")
    endif()
    add_test(NAME validate_args.${name}
             COMMAND ${_driver} -P "${VALIDATE_DRIVER}" -- $<TARGET_FILE:${program}> ${_args})
    set_tests_properties(validate_args.${name} PROPERTIES TIMEOUT 120 LABELS "validate")
endfunction()

list(GET WAKELOOP_BACKENDS 0 _first_backend)
foreach(_backend IN LISTS WAKELOOP_BACKENDS)
    # An argument set of each family of the campaign plan. The pins are not tried: CPU 12, 14 and 2 need not exist here.
    # S: the single-post protocol, a delaying variant with calibration; then W = 2 on the fixed loop, W = 4 on wrong worker.
    validate_case(wakeprobe.${_backend}.S wakeprobe
                  ARGS --backend ${_backend} --variant absent_wake --workers 1 --protocol single --wait-us 1000
                       --gap-frac-range 0.1:3.0 --gap-seed 2026102700 --posts 630 --warmup 5 --calibrate 11
                       --worker-cpus 14 --poster-cpu 2)
    validate_case(wakeprobe.${_backend}.S_w2 wakeprobe
                  ARGS --backend ${_backend} --variant none --workers 2 --protocol single --wait-us 10000
                       --gap-frac-range 0.1:3.0 --gap-unit timer --gap-seed 1 --posts 630 --warmup 5
                       --worker-cpus 12,14 --poster-cpu 2)
    validate_case(wakeprobe.${_backend}.S_w4 wakeprobe
                  ARGS --backend ${_backend} --variant wrong_worker --workers 4 --protocol single --wait-us 10000
                       --gap-us 500 --posts 100 --warmup 5 --calibrate 11)
    # Bt: batch rounds, no calibration.
    validate_case(wakeprobe.${_backend}.Bt wakeprobe
                  ARGS --backend ${_backend} --variant undrained_edge --workers 1 --protocol batch --wait-us 1000
                       --gap-frac-range 0.1:3.0 --gap-seed 1 --posts 200 --warmup 5 --worker-cpus 14 --poster-cpu 2)
    # The phase sweep: a target phase of B_eff, which needs the calibration posts.
    validate_case(wakeprobe.${_backend}.sweep wakeprobe
                  ARGS --backend ${_backend} --variant absent_wake --workers 1 --protocol single --wait-us 10000
                       --phase-frac 0.5 --posts 100 --warmup 5 --calibrate 11 --worker-cpus 14 --poster-cpu 2)
    # H4: the paired throughput harness, timed windows and task work; then count windows.
    validate_case(abpair.${_backend}.H4 abpair
                  ARGS --backend ${_backend} --arm-a none --arm-b absent_wake --workers 1 --producers 1 --inflight 64
                       --pairs 8 --order shuffle --seed 20261027 --window-ms 200 --work-ns 1000
                       --worker-cpus 14 --producer-cpus 2)
    validate_case(abpair.${_backend}.H4_count abpair
                  ARGS --backend ${_backend} --arm-a none --arm-b plain_wake --workers 2 --producers 2 --inflight 16
                       --pairs 2 --window-tasks 2000)
    # No re-trigger needs a wake registration: a site on epoll and io_uring only.
    if(_backend STREQUAL "epoll" OR _backend STREQUAL "io_uring")
        validate_case(wakeprobe.${_backend}.no_retrigger wakeprobe
                      ARGS --backend ${_backend} --variant no_retrigger --workers 2 --protocol single --wait-us 10000
                           --gap-us 500 --posts 100 --warmup 2 --calibrate 11)
    else()
        validate_case(wakeprobe.${_backend}.no_site wakeprobe RC 2 USAGE 1
                      SUB "wakeprobe: the variant no_retrigger has no site on the ${_backend} backend"
                      ARGS --backend ${_backend} --variant no_retrigger --workers 1 --wait-us 10000 --gap-us 500)
        validate_case(abpair.${_backend}.no_site abpair RC 2 USAGE 1
                      SUB "abpair: --arm-b: the variant no_retrigger has no site on the ${_backend} backend"
                      ARGS --backend ${_backend} --arm-b no_retrigger --window-ms 200)
    endif()
endforeach()

# --out may be left out (the campaign runner appends it). A plain run still needs it, but no case here starts a
# program without --validate-args or --check: if that rule broke, the case would measure.
validate_case(wakeprobe.without_out wakeprobe NO_OUT
              ARGS --backend ${_first_backend} --variant none --workers 1 --wait-us 10000 --gap-us 500)
validate_case(abpair.without_out abpair NO_OUT
              ARGS --backend ${_first_backend} --window-ms 200)

# --out is accepted and never opened: no file is created, and an existing one is not truncated.
validate_case(wakeprobe.out_not_created wakeprobe
              ARGS --backend ${_first_backend} --variant absent_wake --workers 1 --wait-us 10000 --gap-us 500
                   --posts 100 --warmup 5 --calibrate 11)
validate_case(wakeprobe.out_kept wakeprobe KEEP
              ARGS --backend ${_first_backend} --variant absent_wake --workers 1 --wait-us 10000 --gap-us 500
                   --posts 100 --warmup 5 --calibrate 11)
validate_case(abpair.out_not_created abpair
              ARGS --backend ${_first_backend} --window-ms 200)
validate_case(abpair.out_kept abpair KEEP
              ARGS --backend ${_first_backend} --window-ms 200)

# Refused, with the message a run prints. What the parser refuses comes with the usage block; the two refusals a
# run makes after parsing (plan_refusal of wakeprobe) come without it, as in a run.
validate_case(wakeprobe.sweep_without_calibration wakeprobe RC 2 USAGE 0
              SUB "wakeprobe: --phase-frac needs a variant that delays single posts and calibration posts"
              ARGS --backend ${_first_backend} --variant absent_wake --workers 1 --wait-us 10000
                   --phase-frac 0.5 --posts 100 --warmup 5 --calibrate 0)
validate_case(wakeprobe.sweep_on_fixed_loop wakeprobe RC 2 USAGE 0
              SUB "wakeprobe: --phase-frac needs a variant that delays single posts and calibration posts"
              ARGS --backend ${_first_backend} --variant none --workers 1 --wait-us 10000
                   --phase-frac 0.5 --posts 100 --warmup 5 --calibrate 11)
validate_case(wakeprobe.blocking_wait_delaying wakeprobe RC 2 USAGE 0
              SUB "wakeprobe: a variant that delays single posts, with a blocking wait, never delivers a post"
              ARGS --backend ${_first_backend} --variant absent_wake --workers 1 --wait-us 0 --gap-us 500)
# The loop refuses a bound above one day when it is created, which a run reaches after its clock readings; --validate-args
# refuses it through the same plan_refusal that a run asks first (review of 2026-10-27, wakeprobe accepted 86400000001).
validate_case(wakeprobe.wait_us_at_the_loop_limit wakeprobe
              ARGS --backend ${_first_backend} --variant none --workers 1 --protocol single --wait-us 86400000000
                   --gap-us 500 --posts 10 --warmup 2)
validate_case(wakeprobe.wait_us_above_the_loop_limit wakeprobe RC 2 USAGE 0
              SUB "wakeprobe: --wait-us must be from 0 to 86400000000 (the loop's limit), got 86400000001"
              ARGS --backend ${_first_backend} --variant none --workers 1 --protocol single --wait-us 86400000001
                   --gap-us 500 --posts 10 --warmup 2)
# The two numbers must stay the same: the loop's limit (loop/src/common.cpp) and wakeprobe's copy of it.
set(LIMIT_CHECK "${CMAKE_CURRENT_BINARY_DIR}/limit_check.cmake")
file(WRITE "${LIMIT_CHECK}" [=[
file(READ "${LOOP_SRC}" _loop)
file(READ "${PROBE_SRC}" _probe)
string(REGEX MATCH "kMaxBoundUs = ([0-9']+);" _unused "${_loop}")
set(_loop_limit "${CMAKE_MATCH_1}")
string(REGEX MATCH "kMaxWaitUs = ([0-9']+);" _unused "${_probe}")
set(_probe_limit "${CMAKE_MATCH_1}")
if("${_loop_limit}" STREQUAL "" OR NOT "${_loop_limit}" STREQUAL "${_probe_limit}")
    message(FATAL_ERROR "the loop's bound limit is [${_loop_limit}] and wakeprobe's copy is [${_probe_limit}]")
endif()
]=])
add_test(NAME validate_args.limit_is_the_loops
         COMMAND ${CMAKE_COMMAND} "-DLOOP_SRC=${PROJECT_SOURCE_DIR}/loop/src/common.cpp"
                 "-DPROBE_SRC=${CMAKE_CURRENT_SOURCE_DIR}/wakeprobe.cpp" -P "${LIMIT_CHECK}")
set_tests_properties(validate_args.limit_is_the_loops PROPERTIES TIMEOUT 60 LABELS "validate")
validate_case(wakeprobe.sweep_two_workers wakeprobe RC 2 USAGE 1
              SUB "wakeprobe: --phase-frac holds a phase against one worker's wake schedule"
              ARGS --backend ${_first_backend} --variant wrong_worker --workers 2 --wait-us 10000
                   --phase-frac 0.5 --posts 100 --warmup 5 --calibrate 11)
validate_case(wakeprobe.batch_phase_frac wakeprobe RC 2 USAGE 1
              SUB "wakeprobe: --protocol batch takes no --phase-frac"
              ARGS --backend ${_first_backend} --variant undrained_edge --workers 1 --protocol batch --wait-us 10000
                   --phase-frac 0.5 --posts 100 --warmup 5)
validate_case(wakeprobe.check_and_validate wakeprobe NO_OUT RAW RC 2 USAGE 1
              SUB "wakeprobe: --validate-args and --check are separate modes"
              ARGS --backend ${_first_backend} --variant none --workers 1 --wait-us 10000 --gap-us 500
                   --check --validate-args)
validate_case(abpair.worker_cpus_mismatch abpair RC 2 USAGE 1
              SUB "abpair: --worker-cpus needs one CPU per worker"
              ARGS --backend ${_first_backend} --workers 2 --worker-cpus 3 --window-ms 200)
validate_case(abpair.producer_cpus_mismatch abpair RC 2 USAGE 1
              SUB "abpair: --producer-cpus needs one CPU per producer"
              ARGS --backend ${_first_backend} --producers 3 --producer-cpus 3,4 --window-ms 200)
validate_case(abpair.check_and_validate abpair NO_OUT RAW RC 2 USAGE 1
              SUB "abpair: --validate-args and --check are separate modes"
              ARGS --backend ${_first_backend} --check --validate-args)
validate_case(abpair.no_window abpair RC 2 USAGE 1
              SUB "abpair: give exactly one of --window-ms and --window-tasks"
              ARGS --backend ${_first_backend} --pairs 2)
validate_case(abpair.wrong_worker_one_worker abpair RC 2 USAGE 1
              SUB "abpair: --arm-b: the variant wrong_worker needs --workers >= 2"
              ARGS --backend ${_first_backend} --arm-b wrong_worker --workers 1 --window-ms 200)
