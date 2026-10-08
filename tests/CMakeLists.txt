# Tests of the loop's wake semantics, one CTest entry per (backend, test).
add_executable(wakeloop_tests wake_tests.cpp harness.hpp)
target_link_libraries(wakeloop_tests PRIVATE wakeloop)
wakeloop_warnings(wakeloop_tests)

set(WAKELOOP_SEMANTIC_TESTS
    post_runs_once post_before_run fifo_one_producer many_producers self_post stop_before_run
    stop_wakes_blocked stop_from_task idle_blocking_no_spin idle_bounded_passes defect_is_delay_not_loss)
set(WAKELOOP_DETECTOR_TESTS
    detect_blocking_single detect_blocking_burst detect_blocking_producers detect_self_post_blocking
    detect_latency21)

foreach(_backend IN LISTS WAKELOOP_BACKENDS)
    foreach(_test IN LISTS WAKELOOP_SEMANTIC_TESTS WAKELOOP_DETECTOR_TESTS)
        add_test(NAME ${_backend}.${_test} COMMAND wakeloop_tests ${_backend} ${_test})
        set_tests_properties(${_backend}.${_test} PROPERTIES TIMEOUT 60 LABELS "${_backend}")
    endforeach()
    if(WAKELOOP_DEFECT)
        # A defect build passes a detector only when it reports the missing wake.
        foreach(_test IN LISTS WAKELOOP_DETECTOR_TESTS)
            set_tests_properties(${_backend}.${_test} PROPERTIES PASS_REGULAR_EXPRESSION "DETECTED: ")
        endforeach()
    endif()
endforeach()

add_test(NAME defect_sites COMMAND ${CMAKE_COMMAND} -DROOT=${PROJECT_SOURCE_DIR} -P ${CMAKE_CURRENT_SOURCE_DIR}/defect_sites.cmake)
