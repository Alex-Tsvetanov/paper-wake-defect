// IOCP backend. See design/minimal-loop.md, section 2.6.
//
// One completion port. A post that finds the stack empty queues a packet with the wake key; stop
// queues a packet with the stop key. The worker waits in GetQueuedCompletionStatus with the bound
// rounded up to whole milliseconds, as the call takes it, so the process's timer resolution sets
// the period of timed returns. The stack is drained after every return, timed or not.
#include "wakeloop/loop.hpp"

#if defined(_WIN32)

#include "common.hpp"

#include <cstdint>
#include <stdexcept>
#include <system_error>

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

namespace wakeloop
{

	namespace
	{

		constexpr ULONG_PTR kWakeKey = 1;
		constexpr ULONG_PTR kStopKey = 2;

		[[noreturn]] void throw_last_error(const char* what)
		{
			throw std::system_error(static_cast<int>(GetLastError()), std::system_category(), what);
		}

		DWORD to_wait_ms(std::int64_t wait_us)
		{
			if (wait_us == 0) return INFINITE;
			const std::int64_t ms = (wait_us + 999) / 1000;
			return ms >= static_cast<std::int64_t>(INFINITE) ? INFINITE - 1 : static_cast<DWORD>(ms);
		}

		void post_packet(void* port, ULONG_PTR key, const char* what) noexcept
		{
			if (!PostQueuedCompletionStatus(static_cast<HANDLE>(port), 0, key, nullptr))
			{
				detail::fatal(what, static_cast<long>(GetLastError()));
			}
		}

	}  // namespace

	IocpLoop::IocpLoop(std::int64_t wait_us) : wait_ms_(to_wait_ms(detail::checked_bound(wait_us)))
	{
		port_ = CreateIoCompletionPort(INVALID_HANDLE_VALUE, nullptr, 0, 1);
		if (port_ == nullptr) throw_last_error("CreateIoCompletionPort");
	}

	IocpLoop::~IocpLoop() { CloseHandle(static_cast<HANDLE>(port_)); }

	void IocpLoop::run()
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

	void IocpLoop::stop() noexcept
	{
		stop_.store(true, std::memory_order_release);
		post_packet(port_, kStopKey, "iocp: posting the stop packet failed");
	}

	void IocpLoop::post(Task& task) noexcept
	{
		if (stack_.push(task))
		{
#if !WAKELOOP_DEFECT
			wake();
#endif
		}
	}

	void IocpLoop::wake() noexcept { post_packet(port_, kWakeKey, "iocp: posting the wake packet failed"); }

	const char* IocpLoop::wait_method() const noexcept { return "GetQueuedCompletionStatus"; }

	/// One wait. A packet's key is not needed: stop() sets its flag before it posts, and posted
	/// work is taken from the stack after every return.
	void IocpLoop::wait_once()
	{
		DWORD bytes = 0;
		ULONG_PTR key = 0;
		OVERLAPPED* overlapped = nullptr;
		if (GetQueuedCompletionStatus(static_cast<HANDLE>(port_), &bytes, &key, &overlapped, wait_ms_)) return;
		const DWORD error = GetLastError();
		if (overlapped == nullptr && error == WAIT_TIMEOUT) return;
		throw std::system_error(static_cast<int>(error), std::system_category(), "GetQueuedCompletionStatus");
	}

}  // namespace wakeloop

#endif  // _WIN32
