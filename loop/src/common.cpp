#include "common.hpp"

#include <cstdio>
#include <cstdlib>
#include <stdexcept>
#include <string>

namespace wakeloop::detail
{

	void fatal(const char* what, long os_error) noexcept
	{
		std::fprintf(stderr, "wakeloop: fatal: %s (os error %ld)\n", what, os_error);
		std::fflush(stderr);
		std::abort();
	}

	std::int64_t checked_bound(std::int64_t wait_us)
	{
		// One day. Far above any bound the study uses, and far below the values at which the
		// backends' conversions (rounding to milliseconds, wait_for's nanoseconds) overflow.
		constexpr std::int64_t kMaxBoundUs = 86'400'000'000;
		if (wait_us < 0 || wait_us > kMaxBoundUs)
		{
			throw std::invalid_argument("wakeloop: the wait bound must be from 0 to " + std::to_string(kMaxBoundUs) +
			                            " us, got " + std::to_string(wait_us));
		}
		return wait_us;
	}

}  // namespace wakeloop::detail
