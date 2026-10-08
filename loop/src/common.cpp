#include "common.hpp"

#include "wakeloop/loop.hpp"

#include <cstdio>
#include <cstdlib>
#include <stdexcept>
#include <string>

namespace wakeloop
{

	const char* defect_name(Defect d) noexcept
	{
		switch (d)
		{
			case Defect::none: return "none";
			case Defect::omit: return "omit";
			case Defect::misdirect: return "misdirect";
		}
		return "?";
	}

	namespace detail
	{

		void fatal(const char* what, long os_error) noexcept
		{
			std::fprintf(stderr, "wakeloop: fatal: %s (os error %ld)\n", what, os_error);
			std::fflush(stderr);
			std::abort();
		}

		std::int64_t checked_bound(std::int64_t wait_us)
		{
			if (wait_us < 0)
			{
				throw std::invalid_argument("wakeloop: the wait bound must be >= 0 us, got " + std::to_string(wait_us));
			}
			return wait_us;
		}

	}  // namespace detail

}  // namespace wakeloop
