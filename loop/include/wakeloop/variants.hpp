// Run-time selection of a backend and a variant. Every variant of every backend this platform
// compiles is instantiated in the program that includes this header; one binary serves the
// whole matrix (design/v2-defects.md, section 2).
#pragma once

#include "wakeloop/defect.hpp"
#include "wakeloop/loop.hpp"

#include <array>
#include <string_view>

namespace wakeloop
{

#if defined(__linux__)
	inline constexpr std::array<std::string_view, 3> kBackends{"epoll", "io_uring", "condvar"};
#elif defined(_WIN32)
	inline constexpr std::array<std::string_view, 2> kBackends{"iocp", "condvar"};
#endif

	/// True when the defect has a site on the backend. No re-trigger needs a wake registration,
	/// which only epoll and io_uring have (design/v2-defects.md, section 4).
	constexpr bool has_site(std::string_view backend, Defect d) noexcept
	{
		if (d == Defect::no_retrigger) return backend == "epoll" || backend == "io_uring";
		return true;
	}

	/// The least worker count at which the variant differs from the fixed loop.
	constexpr unsigned min_workers(Defect d) noexcept { return d == Defect::wrong_worker ? 2u : 1u; }

	namespace detail
	{

		template <template <Defect> class Backend, class Hooks, class F>
		bool with_defect(Defect d, F& f)
		{
			switch (d)
			{
				case Defect::none: f.template operator()<Loop<Backend, Defect::none, Hooks>>(); return true;
				case Defect::absent_wake: f.template operator()<Loop<Backend, Defect::absent_wake, Hooks>>(); return true;
				case Defect::unobserved_primitive:
					f.template operator()<Loop<Backend, Defect::unobserved_primitive, Hooks>>();
					return true;
				case Defect::lazy_wake_race: f.template operator()<Loop<Backend, Defect::lazy_wake_race, Hooks>>(); return true;
				case Defect::wrong_worker: f.template operator()<Loop<Backend, Defect::wrong_worker, Hooks>>(); return true;
				case Defect::no_retrigger: f.template operator()<Loop<Backend, Defect::no_retrigger, Hooks>>(); return true;
				case Defect::undrained_edge: f.template operator()<Loop<Backend, Defect::undrained_edge, Hooks>>(); return true;
			}
			return false;
		}

	}  // namespace detail

	/// Calls f.template operator()<L>() with L the loop type of the backend and variant named.
	/// False, without calling f, when this platform has no such backend or the defect has no
	/// site on it.
	template <class Hooks = NoHooks, class F>
	bool with_loop(std::string_view backend, Defect d, F&& f)
	{
		if (!has_site(backend, d)) return false;
#if defined(__linux__)
		if (backend == "epoll") return detail::with_defect<EpollBackend, Hooks>(d, f);
		if (backend == "io_uring") return detail::with_defect<UringBackend, Hooks>(d, f);
#elif defined(_WIN32)
		if (backend == "iocp") return detail::with_defect<IocpBackend, Hooks>(d, f);
#endif
		if (backend == "condvar") return detail::with_defect<CondvarBackend, Hooks>(d, f);
		return false;
	}

	/// True when this platform compiles the backend.
	constexpr bool known_backend(std::string_view backend) noexcept
	{
		for (std::string_view b : kBackends)
		{
			if (b == backend) return true;
		}
		return false;
	}

}  // namespace wakeloop
