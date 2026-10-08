# X1: the defect switch changes one site per backend and nothing else.
#
#   cmake -DROOT=<repository root> -P tests/defect_sites.cmake
#
# Each backend source (loop/src/{epoll,uring,iocp}.cpp) must hold exactly one preprocessor
# conditional on WAKELOOP_DEFECT, and no other first-party source may hold any. The public
# header reads the macro's value once (kDefectSeeded), which is not a conditional.
cmake_minimum_required(VERSION 3.25)
if(NOT ROOT)
    message(FATAL_ERROR "pass -DROOT=<repository root>")
endif()

set(_backends loop/src/epoll.cpp loop/src/uring.cpp loop/src/iocp.cpp)
file(GLOB_RECURSE _all RELATIVE "${ROOT}"
     "${ROOT}/loop/*.cpp" "${ROOT}/loop/*.hpp" "${ROOT}/tests/*.cpp" "${ROOT}/tests/*.hpp")
# bench/ holds build trees; only its own sources are first-party.
file(GLOB _bench RELATIVE "${ROOT}" "${ROOT}/bench/*.cpp" "${ROOT}/bench/*.hpp")
list(APPEND _all ${_bench})

set(_bad 0)
foreach(_f IN LISTS _all)
    file(STRINGS "${ROOT}/${_f}" _hits REGEX "^[ \t]*#[ \t]*(if|ifdef|ifndef|elif)[ \t].*WAKELOOP_DEFECT")
    list(LENGTH _hits _n)
    if(_f IN_LIST _backends)
        set(_want 1)
    else()
        set(_want 0)
    endif()
    if(NOT _n EQUAL _want)
        message(SEND_ERROR "${_f}: ${_n} conditionals on WAKELOOP_DEFECT, expected ${_want}")
        set(_bad 1)
    else()
        message(STATUS "ok: ${_f} (${_n})")
    endif()
endforeach()
foreach(_b IN LISTS _backends)
    if(NOT EXISTS "${ROOT}/${_b}")
        message(SEND_ERROR "missing backend source ${_b}")
        set(_bad 1)
    endif()
endforeach()
if(_bad)
    message(FATAL_ERROR "defect sites: FAIL")
endif()
message(STATUS "defect sites: PASS")
