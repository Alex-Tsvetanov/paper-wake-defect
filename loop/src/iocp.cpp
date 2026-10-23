// IOCP backend. See design/minimal-loop.md, section 2.6, and design/v2-defects.md, section 4.
//
// One completion port per worker. A post that finds the stack empty queues a packet with the wake
// key; stop queues a packet with the stop key. The worker waits in GetQueuedCompletionStatus with
// the bound rounded up to whole milliseconds, as the call takes it, so the process's timer
// resolution sets the period of timed returns.
//
// Defect site: unobserved primitive creates a second port that nobody waits on and posts the wake
// packet there. No re-trigger has no site: a posted packet needs no registration.
#include "wakeloop/backends.hpp"

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

		DWORD to_wait_ms(WaitSpec spec)
		{
			const std::int64_t wait_us = detail::checked_bound(spec.wait_us);
			if (spec.poll) return 0;
			if (wait_us == 0) return INFINITE;
			const std::int64_t ms = (wait_us + 999) / 1000;
			return ms >= static_cast<std::int64_t>(INFINITE) ? INFINITE - 1 : static_cast<DWORD>(ms);
		}

		HANDLE make_port()
		{
			HANDLE port = CreateIoCompletionPort(INVALID_HANDLE_VALUE, nullptr, 0, 1);
			if (port == nullptr) throw_last_error("CreateIoCompletionPort");
			return port;
		}

		void post_packet(void* port, ULONG_PTR key, const char* what) noexcept
		{
			if (!PostQueuedCompletionStatus(static_cast<HANDLE>(port), 0, key, nullptr))
			{
				detail::fatal(what, static_cast<long>(GetLastError()));
			}
		}

	}  // namespace

	template <Defect D>
	IocpBackend<D>::IocpBackend(WaitSpec spec) : wait_ms_(to_wait_ms(spec))
	{
		port_ = make_port();
		if constexpr (D == Defect::unobserved_primitive)
		{
			try
			{
				spare_ = make_port();
			}
			catch (...)
			{
				CloseHandle(static_cast<HANDLE>(port_));
				throw;
			}
		}
	}

	template <Defect D>
	IocpBackend<D>::~IocpBackend()
	{
		CloseHandle(static_cast<HANDLE>(port_));
		if (spare_ != nullptr) CloseHandle(static_cast<HANDLE>(spare_));
	}

	template <Defect D>
	void IocpBackend<D>::wake() noexcept
	{
		if constexpr (D == Defect::unobserved_primitive)
		{
			post_packet(spare_, kWakeKey, "iocp: posting the wake packet to the spare port failed");
		}
		else
		{
			post_packet(port_, kWakeKey, "iocp: posting the wake packet failed");
		}
	}

	template <Defect D>
	void IocpBackend<D>::stop() noexcept
	{
		post_packet(port_, kStopKey, "iocp: posting the stop packet failed");
	}

	template <Defect D>
	const char* IocpBackend<D>::wait_method() const noexcept
	{
		return "GetQueuedCompletionStatus";
	}

	/// One wait. A packet's key is not needed: the core checks its stop flag and takes posted
	/// work after every return. A stop packet is consumed by one wait, but the core returns on
	/// the flag that stop() set before posting it.
	template <Defect D>
	void IocpBackend<D>::wait()
	{
		DWORD bytes = 0;
		ULONG_PTR key = 0;
		OVERLAPPED* overlapped = nullptr;
		if (GetQueuedCompletionStatus(static_cast<HANDLE>(port_), &bytes, &key, &overlapped, wait_ms_)) return;
		const DWORD error = GetLastError();
		if (overlapped == nullptr && error == WAIT_TIMEOUT) return;
		throw std::system_error(static_cast<int>(error), std::system_category(), "GetQueuedCompletionStatus");
	}

	WAKELOOP_INSTANTIATE_ALL(IocpBackend)

}  // namespace wakeloop

#endif  // _WIN32
