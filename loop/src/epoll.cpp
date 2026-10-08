// epoll backend. See design/minimal-loop.md, section 2.4.
//
// The wake eventfd is registered edge-triggered and is never read by the worker: each write is a
// new wakeup, so it ends one wait whatever the counter holds. The stop eventfd is
// level-triggered and never read, so once stop() writes it every later wait returns at once.
#include "wakeloop/loop.hpp"

#if defined(__linux__)

#include "common.hpp"
#include "linux_fd.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <climits>
#include <cstdint>
#include <ctime>
#include <stdexcept>

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

	EpollLoop::EpollLoop(std::int64_t wait_us) : wait_us_(detail::checked_bound(wait_us))
	{
		detail::Fd epoll_fd(::epoll_create1(EPOLL_CLOEXEC));
		if (epoll_fd.get() < 0) detail::throw_errno("epoll_create1");
		detail::Fd wake_fd = detail::make_eventfd("eventfd (wake)");
		detail::Fd stop_fd = detail::make_eventfd("eventfd (stop)");
		add(epoll_fd.get(), wake_fd.get(), EPOLLIN | EPOLLET, kWakeTag, "epoll_ctl (wake)");
		add(epoll_fd.get(), stop_fd.get(), EPOLLIN, kStopTag, "epoll_ctl (stop)");
		epoll_fd_ = epoll_fd.release();
		wake_fd_ = wake_fd.release();
		stop_fd_ = stop_fd.release();
	}

	EpollLoop::~EpollLoop()
	{
		::close(epoll_fd_);
		::close(wake_fd_);
		::close(stop_fd_);
	}

	void EpollLoop::run()
	{
		if (ran_.exchange(true)) throw std::logic_error("wakeloop: run() may be called once");
		if (stop_.load(std::memory_order_acquire)) return;
		detail::run_all(stack_.take_all());
		while (!stop_.load(std::memory_order_acquire))
		{
			wait_once();
			passes_.fetch_add(1, std::memory_order_relaxed);
			if (stop_.load(std::memory_order_acquire)) return;
			detail::run_all(stack_.take_all());
		}
	}

	void EpollLoop::stop() noexcept
	{
		stop_.store(true, std::memory_order_release);
		detail::signal_eventfd(stop_fd_, "epoll: write to the stop eventfd failed");
	}

	void EpollLoop::post(Task& task) noexcept
	{
		if (stack_.push(task))
		{
#if !WAKELOOP_DEFECT
			wake();
#endif
		}
	}

	void EpollLoop::wake() noexcept { detail::signal_eventfd(wake_fd_, "epoll: write to the wake eventfd failed"); }

	const char* EpollLoop::wait_method() const noexcept
	{
		return use_pwait2_.load(std::memory_order_relaxed) ? "epoll_pwait2" : "epoll_wait";
	}

	/// One wait. The events themselves are not needed: stop() sets its flag before it signals,
	/// and posted work is taken from the stack after every return.
	void EpollLoop::wait_once()
	{
		std::array<epoll_event, 4> events{};
		const int max_events = static_cast<int>(events.size());
		int n = -1;
		bool waited = false;
		if (use_pwait2_.load(std::memory_order_relaxed))
		{
			timespec ts{};
			ts.tv_sec = static_cast<time_t>(wait_us_ / 1000000);
			ts.tv_nsec = static_cast<long>(wait_us_ % 1000000) * 1000;
			n = ::epoll_pwait2(epoll_fd_, events.data(), max_events, wait_us_ == 0 ? nullptr : &ts, nullptr);
			waited = n >= 0 || errno != ENOSYS;
			if (!waited) use_pwait2_.store(false, std::memory_order_relaxed);
		}
		if (!waited)
		{
			// Whole milliseconds, rounded up; -1 blocks.
			const std::int64_t ms = (wait_us_ + 999) / 1000;
			const int timeout = wait_us_ == 0 ? -1 : static_cast<int>(std::min<std::int64_t>(ms, INT_MAX));
			n = ::epoll_wait(epoll_fd_, events.data(), max_events, timeout);
		}
		if (n < 0 && errno != EINTR) detail::throw_errno(wait_method());
	}

}  // namespace wakeloop

#endif  // __linux__
