// epoll backend. See design/minimal-loop.md, section 2.4, and design/v2-defects.md, section 4.
//
// The wake eventfd is registered edge-triggered and is never read by the worker: each write is a
// new wakeup, so it ends one wait whatever the counter holds. The stop eventfd is
// level-triggered and never read, so once stop() writes it every later wait returns at once.
//
// Defect sites: unobserved primitive creates a spare eventfd that is never registered and
// signals it instead of the wake eventfd; no re-trigger registers the wake eventfd one-shot.
#include "wakeloop/backends.hpp"

#if defined(__linux__)

#include "common.hpp"
#include "linux_fd.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <climits>
#include <cstdint>
#include <ctime>

#include <sys/epoll.h>
#include <unistd.h>

namespace wakeloop
{

	namespace
	{

		constexpr std::uint64_t kWakeTag = 1;
		constexpr std::uint64_t kStopTag = 2;

		void add(int epoll_fd, int fd, std::uint32_t events, std::uint64_t tag, const char* what)
		{
			epoll_event ev{};
			ev.events = events;
			ev.data.u64 = tag;
			if (::epoll_ctl(epoll_fd, EPOLL_CTL_ADD, fd, &ev) != 0) detail::throw_errno(what);
		}

	}  // namespace

	template <Defect D>
	EpollBackend<D>::EpollBackend(WaitSpec spec) : wait_us_(detail::checked_bound(spec.wait_us)), poll_(spec.poll)
	{
		detail::Fd epoll_fd(::epoll_create1(EPOLL_CLOEXEC));
		if (epoll_fd.get() < 0) detail::throw_errno("epoll_create1");
		detail::Fd wake_fd = detail::make_eventfd("eventfd (wake)");
		detail::Fd stop_fd = detail::make_eventfd("eventfd (stop)");
		std::uint32_t wake_events = EPOLLIN | EPOLLET;
		if constexpr (D == Defect::no_retrigger) wake_events |= EPOLLONESHOT;
		add(epoll_fd.get(), wake_fd.get(), wake_events, kWakeTag, "epoll_ctl (wake)");
		add(epoll_fd.get(), stop_fd.get(), EPOLLIN, kStopTag, "epoll_ctl (stop)");
		// Last, so nothing after it can throw and leak it.
		if constexpr (D == Defect::unobserved_primitive) spare_fd_ = detail::make_eventfd("eventfd (spare)").release();
		epoll_fd_ = epoll_fd.release();
		wake_fd_ = wake_fd.release();
		stop_fd_ = stop_fd.release();
	}

	template <Defect D>
	EpollBackend<D>::~EpollBackend()
	{
		::close(epoll_fd_);
		::close(wake_fd_);
		::close(stop_fd_);
		if (spare_fd_ >= 0) ::close(spare_fd_);
	}

	template <Defect D>
	void EpollBackend<D>::wake() noexcept
	{
		if constexpr (D == Defect::unobserved_primitive)
		{
			detail::signal_eventfd(spare_fd_, "epoll: write to the spare eventfd failed");
		}
		else
		{
			detail::signal_eventfd(wake_fd_, "epoll: write to the wake eventfd failed");
		}
	}

	template <Defect D>
	void EpollBackend<D>::stop() noexcept
	{
		detail::signal_eventfd(stop_fd_, "epoll: write to the stop eventfd failed");
	}

	template <Defect D>
	const char* EpollBackend<D>::wait_method() const noexcept
	{
		return use_pwait2_.load(std::memory_order_relaxed) ? "epoll_pwait2" : "epoll_wait";
	}

	/// One wait. The events themselves are not needed: the core checks its stop flag and takes
	/// posted work after every return.
	template <Defect D>
	void EpollBackend<D>::wait()
	{
		std::array<epoll_event, 4> events{};
		const int max_events = static_cast<int>(events.size());
		int n = -1;
		bool waited = false;
		if (use_pwait2_.load(std::memory_order_relaxed))
		{
			timespec ts{};  // zero in poll mode
			if (!poll_)
			{
				ts.tv_sec = static_cast<time_t>(wait_us_ / 1000000);
				ts.tv_nsec = static_cast<long>(wait_us_ % 1000000) * 1000;
			}
			const bool blocks = !poll_ && wait_us_ == 0;
			n = ::epoll_pwait2(epoll_fd_, events.data(), max_events, blocks ? nullptr : &ts, nullptr);
			waited = n >= 0 || errno != ENOSYS;
			if (!waited) use_pwait2_.store(false, std::memory_order_relaxed);
		}
		if (!waited)
		{
			// Whole milliseconds, rounded up; -1 blocks; 0 polls.
			const std::int64_t ms = (wait_us_ + 999) / 1000;
			int timeout = static_cast<int>(std::min<std::int64_t>(ms, INT_MAX));
			if (poll_) timeout = 0;
			else if (wait_us_ == 0) timeout = -1;
			n = ::epoll_wait(epoll_fd_, events.data(), max_events, timeout);
		}
		if (n < 0 && errno != EINTR) detail::throw_errno(wait_method());
	}

	WAKELOOP_INSTANTIATE_ALL(EpollBackend)

}  // namespace wakeloop

#endif  // __linux__
