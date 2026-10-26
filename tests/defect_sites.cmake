# X1 v2: each defect changes only its declared sites (design/v2-defects.md, section 4).
#
#   cmake -DROOT=<repository root> -P tests/defect_sites.cmake
#
# A site is a line holding `if constexpr` that names a defect (Defect::<name>). The script counts
# the sites of every defect in every first-party loop source and fails on any count other than
# the table below. The fixed loop is the code outside every site, so it holds no defect code.
cmake_minimum_required(VERSION 3.25)
if(NOT ROOT)
    message(FATAL_ERROR "pass -DROOT=<repository root>")
endif()

set(_defects plain_wake absent_wake unobserved_primitive lazy_wake_race lazy_wake_wide wrong_worker no_retrigger undrained_edge)
# <file>|<defect>|<sites>. A pair not listed expects no site. plain_wake is no defect but the
# 2026-09 loop; its two sites (the post and the park) are counted the same way.
set(_expect
    "loop/include/wakeloop/loop.hpp|plain_wake|2"
    "loop/include/wakeloop/loop.hpp|absent_wake|1"
    "loop/include/wakeloop/loop.hpp|lazy_wake_race|1"
    "loop/include/wakeloop/loop.hpp|lazy_wake_wide|2"
    "loop/src/epoll.cpp|lazy_wake_wide|1"
    "loop/src/iocp.cpp|lazy_wake_wide|1"
    "loop/include/wakeloop/loop.hpp|wrong_worker|1"
    "loop/include/wakeloop/loop.hpp|undrained_edge|1"
    "loop/src/epoll.cpp|unobserved_primitive|2"
    "loop/src/epoll.cpp|no_retrigger|1"
    "loop/src/uring.cpp|unobserved_primitive|1"
    "loop/src/uring.cpp|no_retrigger|2"
    "loop/src/iocp.cpp|unobserved_primitive|2"
    "loop/src/condvar.cpp|unobserved_primitive|1")

set(_bad 0)
foreach(_e IN LISTS _expect)
    string(REPLACE "|" ";" _parts "${_e}")
    list(GET _parts 0 _file)
    if(NOT EXISTS "${ROOT}/${_file}")
        message(SEND_ERROR "missing source ${_file}")
        set(_bad 1)
    endif()
endforeach()

file(GLOB_RECURSE _all RELATIVE "${ROOT}" "${ROOT}/loop/*.cpp" "${ROOT}/loop/*.hpp")
foreach(_f IN LISTS _all)
    file(STRINGS "${ROOT}/${_f}" _lines REGEX "if[ \t]+constexpr")
    foreach(_d IN LISTS _defects)
        set(_n 0)
        foreach(_l IN LISTS _lines)
            if(_l MATCHES "Defect::${_d}([^a-z_]|$)")
                math(EXPR _n "${_n} + 1")
            endif()
        endforeach()
        set(_want 0)
        foreach(_e IN LISTS _expect)
            string(REPLACE "|" ";" _parts "${_e}")
            list(GET _parts 0 _ef)
            list(GET _parts 1 _ed)
            list(GET _parts 2 _ec)
            if(_ef STREQUAL _f AND _ed STREQUAL _d)
                set(_want ${_ec})
            endif()
        endforeach()
        if(NOT _n EQUAL _want)
            message(SEND_ERROR "${_f}: ${_n} sites of ${_d}, expected ${_want}")
            set(_bad 1)
        elseif(_n GREATER 0)
            message(STATUS "ok: ${_f}: ${_d} (${_n})")
        endif()
    endforeach()
endforeach()
if(_bad)
    message(FATAL_ERROR "defect sites: FAIL")
endif()
message(STATUS "defect sites: PASS")
