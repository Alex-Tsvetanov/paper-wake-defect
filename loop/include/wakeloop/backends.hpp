// The wait and wake objects of one worker, per backend. Each backend is a class template on the
// defect: its defect sites are `if constexpr` blocks in its source file, which instantiates every
// variant explicitly. See design/v2-defects.md, section 4.
//
//   epoll     an eventfd in the epoll set, edge-triggered, never read by the worker
//   io_uring  an eventfd watched by a multishot poll in the worker's ring
//   IOCP      a completion packet with the wake key
//   condvar   a predicate set under a mutex, then a notify
//
// Every backend has the same members, which the core (loop.hpp) calls:
//   start()       on the worker thread, once, before its first drain
//   wait()        one wait: bounded by B, blocking for B = 0, or returning at once in poll mode
//   after_wait()  on the worker thread, after each pass's park (whether or not it waited)
//   before_drain() on the worker thread, after the core's stop check and before its drain
//   wake()        any thread; ends the worker's current or next wait
//   stop()        any thread, more than once; every later wait returns at once
//   polls()       true in poll mode, where every wait returns at once
#pragma once

#include "wakeloop/defect.hpp"

#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <mutex>

namespace wakeloop
{

	/// How a worker waits. wait_us is the bound B in microseconds and 0 blocks. In poll mode
	/// every wait returns at once and wait_us is not used (H4's busy worker).
	struct WaitSpec
	{
		std::int64_t wait_us = 0;
		bool poll = false;
	};

#if defined(__linux__)

	template <Defect D>
	class EpollBackend
	{
	public:
		/// Throws std::invalid_argument for a negative bound and std::system_error when a setup
		/// call fails.
		explicit EpollBackend(WaitSpec spec);
		~EpollBackend();
		EpollBackend(const EpollBackend&) = delete;
		EpollBackend& operator=(const EpollBackend&) = delete;

		void start() {}
		void wait();
		void after_wait() {}
		void before_drain() {}
		bool polls() const noexcept { return poll_; }
		void wake() noexcept;
		void stop() noexcept;
		/// "epoll_pwait2", or "epoll_wait" where the kernel lacks it.
		const char* wait_method() const noexcept;

		static constexpr const char* name = "epoll";

	private:
		std::int64_t wait_us_;
		bool poll_;
		std::atomic<bool> use_pwait2_{true};
		int epoll_fd_ = -1;
		int wake_fd_ = -1;
		int stop_fd_ = -1;
		int spare_fd_ = -1;  // unobserved primitive only: an eventfd outside the epoll set
	};

	template <Defect D>
	class UringBackend
	{
	public:
		/// As EpollBackend. Also throws std::system_error when the kernel refuses the ring or
		/// lacks a required feature (extended wait argument, single mmap, no-drop completions).
		explicit UringBackend(WaitSpec spec);
		~UringBackend();
		UringBackend(const UringBackend&) = delete;
		UringBackend& operator=(const UringBackend&) = delete;

		/// Enables the ring, which makes the calling thread its single issuer, and arms the polls.
		void start();
		void wait();
		/// Reaps completions.
		void after_wait();
		/// Re-arms the wake poll if the kernel ended it.
		void before_drain();
		bool polls() const noexcept { return poll_; }
		void wake() noexcept;
		void stop() noexcept;
		const char* wait_method() const noexcept;

		static constexpr const char* name = "io_uring";

	private:
		// Views into the rings the kernel shares with this process.
		struct Ring
		{
			int fd = -1;
			void* map = nullptr;
			std::size_t map_size = 0;
			void* sqes = nullptr;
			std::size_t sqes_size = 0;
			unsigned* sq_head = nullptr;
			unsigned* sq_tail = nullptr;
			unsigned* sq_array = nullptr;
			unsigned sq_mask = 0;
			unsigned sq_entries = 0;
			unsigned* cq_head = nullptr;
			unsigned* cq_tail = nullptr;
			unsigned cq_mask = 0;
			void* cqes = nullptr;
		};

		void arm_wake_poll();
		void arm_stop_poll();
		void submit();
		void reap();
		void* next_sqe();

		std::int64_t wait_us_;
		bool poll_;
		Ring ring_;
		unsigned sq_pending_ = 0;  // SQEs written but not yet submitted; worker only
		bool rearm_ = false;       // the kernel ended the wake poll; worker only
		int wake_fd_ = -1;
		int stop_fd_ = -1;
	};

#elif defined(_WIN32)

	template <Defect D>
	class IocpBackend
	{
	public:
		/// As EpollBackend. The bound is rounded up to whole milliseconds, as the wait call takes it.
		explicit IocpBackend(WaitSpec spec);
		~IocpBackend();
		IocpBackend(const IocpBackend&) = delete;
		IocpBackend& operator=(const IocpBackend&) = delete;

		void start() {}
		void wait();
		void after_wait() {}
		void before_drain() {}
		bool polls() const noexcept { return wait_ms_ == 0; }
		void wake() noexcept;
		void stop() noexcept;
		const char* wait_method() const noexcept;

		static constexpr const char* name = "iocp";

	private:
		unsigned long wait_ms_;    // DWORD; INFINITE for B = 0, 0 in poll mode
		void* port_ = nullptr;     // HANDLE
		void* spare_ = nullptr;    // unobserved primitive only: a second port nobody waits on
	};

#endif

	template <Defect D>
	class CondvarBackend
	{
	public:
		/// Throws std::invalid_argument for a negative bound.
		explicit CondvarBackend(WaitSpec spec);
		CondvarBackend(const CondvarBackend&) = delete;
		CondvarBackend& operator=(const CondvarBackend&) = delete;

		void start() {}
		void wait();
		void after_wait() {}
		void before_drain() {}
		bool polls() const noexcept { return poll_; }
		void wake() noexcept;
		void stop() noexcept;
		const char* wait_method() const noexcept;

		static constexpr const char* name = "condvar";

	private:
		std::int64_t wait_us_;
		bool poll_;
		std::mutex m_;
		std::condition_variable cv_;
		bool pending_ = false;  // a wake not yet seen by a wait; guarded by m_
		bool stopped_ = false;  // guarded by m_
		// Unobserved primitive only: a predicate and condition variable the wait never reads.
		std::mutex spare_m_;
		std::condition_variable spare_cv_;
		bool spare_pending_ = false;
	};

}  // namespace wakeloop
