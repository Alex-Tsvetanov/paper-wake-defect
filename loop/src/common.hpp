// Internal helpers shared by the backends.
#pragma once

#include <cstddef>
#include <cstdint>

#if defined(__has_feature)
#if __has_feature(memory_sanitizer)
#define WAKELOOP_MSAN 1
#if __has_include(<sanitizer/msan_interface.h>)
#include <sanitizer/msan_interface.h>
#else
// The documented MemorySanitizer interface, for runtimes installed without their headers.
extern "C" void __msan_unpoison(const volatile void* a, std::size_t size);
#endif
#endif
#endif

namespace wakeloop::detail
{

	/// A broken invariant on a path that cannot report an error (a wake or stop signal that
	/// failed). Prints `what` and the OS error, then aborts: a lost wake must never be silent.
	[[noreturn]] void fatal(const char* what, long os_error) noexcept;

	/// Marks memory the kernel wrote as initialized for MemorySanitizer; nothing otherwise.
	inline void kernel_wrote([[maybe_unused]] const void* p, [[maybe_unused]] std::size_t n) noexcept
	{
#if defined(WAKELOOP_MSAN)
		__msan_unpoison(p, n);
#endif
	}

	/// Checks the bound: B >= 0 microseconds. Throws std::invalid_argument otherwise.
	std::int64_t checked_bound(std::int64_t wait_us);

}  // namespace wakeloop::detail
