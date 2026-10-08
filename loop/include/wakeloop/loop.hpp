// wakeloop: a minimal event loop with one worker, for measuring bounded-wait wake defects.
//
// The worker waits with a bound of B microseconds (B = 0 blocks). Any thread may post a task;
// a post that finds the queue empty wakes the worker through the backend's wake object:
//
//   epoll     an eventfd in the epoll set, edge-triggered, never read by the worker
//   io_uring  an eventfd watched by a multishot poll in the worker's ring
//   IOCP      a completion packet with the wake key
//
// A build with WAKELOOP_DEFECT=1 seeds the defect at one site per backend: epoll and IOCP omit
// the wake; io_uring still writes the eventfd but never arms the poll that watches it. Stop has
// its own path in every backend and is never affected. See design/minimal-loop.md.
#pragma once

#include "wakeloop/task_stack.hpp"

#include <atomic>
#include <cstddef>
#include <cstdint>

namespace wakeloop
{

	/// The form of the seeded defect in a backend of this build.
	enum class Defect
	{
		none,
		omit,       // the post does not signal
		misdirect,  // the post signals an object the wait does not watch
	};

	const char* defect_name(Defect d) noexcept;

	/// True in a build that seeds the defect. The build defines WAKELOOP_DEFECT as 0 or 1.
	inline constexpr bool kDefectSeeded = WAKELOOP_DEFECT != 0;

#if defined(__linux__)

	class EpollLoop
	{
	public:
		/// wait_us is the bound B in microseconds; 0 blocks. Throws std::invalid_argument for a
		/// negative bound and std::system_error when a setup call fails.
		explicit EpollLoop(std::int64_t wait_us);
		~EpollLoop();
		EpollLoop(const EpollLoop&) = delete;
		EpollLoop& operator=(const EpollLoop&) = delete;

		/// The calling thread becomes the worker until stop(). Call it once.
		void run();
		/// Any thread, any time, more than once. Wakes a blocked worker in both arms.
		void stop() noexcept;
		/// Any thread. Never blocks, never allocates.
		void post(Task& task) noexcept;

		static constexpr const char* name = "epoll";
		static constexpr Defect defect = kDefectSeeded ? Defect::omit : Defect::none;

		/// Number of wait returns so far.
		std::uint64_t passes() const noexcept { return passes_.load(std::memory_order_relaxed); }
		/// The wait call in use: "epoll_pwait2", or "epoll_wait" where the kernel lacks it.
		const char* wait_method() const noexcept;

	private:
		void wake() noexcept;
		void wait_once();

		detail::TaskStack stack_;
		std::atomic<bool> stop_{false};
		std::atomic<bool> ran_{false};
		std::atomic<std::uint64_t> passes_{0};
		std::atomic<bool> use_pwait2_{true};
		std::int64_t wait_us_;
		int epoll_fd_ = -1;
		int wake_fd_ = -1;
		int stop_fd_ = -1;
	};

	class UringLoop
	{
	public:
		/// As EpollLoop. Also throws std::system_error when the kernel refuses the ring or lacks
		/// a required feature (extended wait argument, single mmap, no-drop completions).
		explicit UringLoop(std::int64_t wait_us);
		~UringLoop();
		UringLoop(const UringLoop&) = delete;
		UringLoop& operator=(const UringLoop&) = delete;

		void run();
		void stop() noexcept;
		void post(Task& task) noexcept;

		static constexpr const char* name = "io_uring";
		static constexpr Defect defect = kDefectSeeded ? Defect::misdirect : Defect::none;

		std::uint64_t passes() const noexcept { return passes_.load(std::memory_order_relaxed); }
		const char* wait_method() const noexcept;

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

		void wake() noexcept;
		void arm_wake_poll();
		void arm_stop_poll();
		void submit();
		void wait_once();
		void reap();
		void* next_sqe();

		detail::TaskStack stack_;
		std::atomic<bool> stop_{false};
		std::atomic<bool> ran_{false};
		std::atomic<std::uint64_t> passes_{0};
		std::int64_t wait_us_;
		Ring ring_;
		unsigned sq_pending_ = 0;  // SQEs written but not yet submitted; worker only
		bool rearm_ = false;       // the kernel ended the multishot poll; worker only
		int wake_fd_ = -1;
		int stop_fd_ = -1;
	};

#elif defined(_WIN32)

	class IocpLoop
	{
	public:
		/// As EpollLoop. The bound is rounded up to whole milliseconds, as the wait call takes it.
		explicit IocpLoop(std::int64_t wait_us);
		~IocpLoop();
		IocpLoop(const IocpLoop&) = delete;
		IocpLoop& operator=(const IocpLoop&) = delete;

		void run();
		void stop() noexcept;
		void post(Task& task) noexcept;

		static constexpr const char* name = "iocp";
		static constexpr Defect defect = kDefectSeeded ? Defect::omit : Defect::none;

		std::uint64_t passes() const noexcept { return passes_.load(std::memory_order_relaxed); }
		const char* wait_method() const noexcept;

	private:
		void wake() noexcept;
		void wait_once();

		detail::TaskStack stack_;
		std::atomic<bool> stop_{false};
		std::atomic<bool> ran_{false};
		std::atomic<std::uint64_t> passes_{0};
		unsigned long wait_ms_;  // DWORD; INFINITE for B = 0
		void* port_ = nullptr;   // HANDLE
	};

#endif

}  // namespace wakeloop
