# The wake matrix: one CTest entry per (backend, variant, workers, test), each with the verdict
# its variant must give (design/v2-defects.md, section 5). Names: <backend>.<variant>.w<W>.<test>.
add_executable(wakeloop_tests wake_tests.cpp harness.hpp)
target_link_libraries(wakeloop_tests PRIVATE wakeloop)
wakeloop_warnings(wakeloop_tests)

set(WAKE_SEMANTIC_TESTS
    post_runs_once post_before_run fifo_one_producer many_producers self_post stop_before_run
    stop_wakes_blocked stop_from_task idle_blocking_no_spin idle_bounded_passes
    defect_is_delay_not_loss workers_independent)
set(WAKE_DETECTORS
    detect_blocking_single detect_blocking_burst detect_blocking_producers detect_self_post_blocking
    detect_blocking_sequence detect_batch_blocking detect_lazy_forced
    detect_latency21 detect_latency_batch detect_latency_forced)

# The verdict of each variant on each detector, in the order of WAKE_DETECTORS: D detected,
# P passes, A racy (run, but either verdict is accepted). Any other outcome fails the test.
#                                    single burst prod self seq batch lazy  lat21 latbatch latlazy
# A worker re-checks its stack before it parks, so a post made while it runs needs no wake: the
# defects that lose wakes pass D9 and D10, whose posts land behind a running task. Starters are
# posted before run(), when no worker sleeps, so they send no wake and no re-trigger's one-shot
# is still unused when a detector starts.
set(WAKE_EXPECT_none                 P      P     P    P    P   P     P     P     P        P)
set(WAKE_EXPECT_plain_wake           P      P     P    P    P   P     P     P     P        P)
set(WAKE_EXPECT_absent_wake          D      D     D    D    D   D     D     D     P        P)
set(WAKE_EXPECT_unobserved_primitive D      D     D    D    D   D     D     D     P        P)
set(WAKE_EXPECT_lazy_wake_race       P      A     A    P    P   P     D     P     P        D)
set(WAKE_EXPECT_wrong_worker         D      D     D    D    D   D     D     D     P        P)
set(WAKE_EXPECT_no_retrigger         P      A     A    P    D   P     P     D     P        P)
set(WAKE_EXPECT_undrained_edge       P      P     A    P    P   D     P     P     D        P)

# The worker counts each variant runs at. Wrong worker exists only from W = 2.
set(WAKE_VARIANTS none plain_wake absent_wake unobserved_primitive lazy_wake_race wrong_worker no_retrigger undrained_edge)
foreach(_variant IN LISTS WAKE_VARIANTS)
    set(WAKE_WORKERS_${_variant} 1)
endforeach()
set(WAKE_WORKERS_none 1 2)
set(WAKE_WORKERS_wrong_worker 2)

list(LENGTH WAKE_DETECTORS _detectors)
foreach(_backend IN LISTS WAKELOOP_BACKENDS)
    foreach(_variant IN LISTS WAKE_VARIANTS)
        # No re-trigger needs a wake registration; IOCP and the condition variable have none.
        if(_variant STREQUAL "no_retrigger" AND NOT _backend MATCHES "^(epoll|io_uring)$")
            continue()
        endif()
        list(LENGTH WAKE_EXPECT_${_variant} _n)
        if(NOT _n EQUAL _detectors)
            message(FATAL_ERROR "WAKE_EXPECT_${_variant} has ${_n} verdicts for ${_detectors} detectors")
        endif()
        foreach(_w IN LISTS WAKE_WORKERS_${_variant})
            set(_prefix ${_backend}.${_variant}.w${_w})
            foreach(_test IN LISTS WAKE_SEMANTIC_TESTS)
                add_test(NAME ${_prefix}.${_test} COMMAND wakeloop_tests ${_backend} ${_variant} ${_w} ${_test})
                set_tests_properties(${_prefix}.${_test} PROPERTIES TIMEOUT 120 LABELS "${_backend};${_variant};semantics")
            endforeach()
            set(_i 0)
            foreach(_test IN LISTS WAKE_DETECTORS)
                list(GET WAKE_EXPECT_${_variant} ${_i} _want)
                math(EXPR _i "${_i} + 1")
                add_test(NAME ${_prefix}.${_test} COMMAND wakeloop_tests ${_backend} ${_variant} ${_w} ${_test})
                set_tests_properties(${_prefix}.${_test} PROPERTIES TIMEOUT 120 LABELS "${_backend};${_variant};detector")
                if(_want STREQUAL "D")
                    set_tests_properties(${_prefix}.${_test} PROPERTIES PASS_REGULAR_EXPRESSION "DETECTED: ")
                elseif(_want STREQUAL "A")
                    set_tests_properties(${_prefix}.${_test} PROPERTIES PASS_REGULAR_EXPRESSION "PASS: |DETECTED: ")
                elseif(NOT _want STREQUAL "P")
                    message(FATAL_ERROR "WAKE_EXPECT_${_variant}: unknown verdict '${_want}'")
                endif()
            endforeach()
        endforeach()
    endforeach()
    # Four workers, each running its own posts.
    add_test(NAME ${_backend}.none.w4.workers_independent COMMAND wakeloop_tests ${_backend} none 4 workers_independent)
    set_tests_properties(${_backend}.none.w4.workers_independent PROPERTIES TIMEOUT 120 LABELS "${_backend};none;semantics")
endforeach()

add_test(NAME defect_sites COMMAND ${CMAKE_COMMAND} -DROOT=${PROJECT_SOURCE_DIR} -P ${CMAKE_CURRENT_SOURCE_DIR}/defect_sites.cmake)
