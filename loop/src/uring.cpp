// io_uring backend, on raw system calls. See design/minimal-loop.md, section 2.5, and
// design/v2-defects.md, section 4.
//
// Only the worker touches the ring, so it is created SINGLE_ISSUER and DEFER_TASKRUN, disabled,
// and enabled by start() on the worker thread, which makes the worker its issuer. Posters write
// the wake eventfd and never touch the ring. A multishot poll watches that eventfd; io_uring
// polls are edge-triggered unless IORING_POLL_ADD_LEVEL is set, so each write ends one wait and
// the eventfd is never read. Stop has its own eventfd and a single-shot poll.
//
// Defect sites: unobserved primitive never arms the wake poll, so posters write an eventfd the
// ring does not watch (the 2026-09 "misdirect"); no re-trigger arms the wake poll single-shot
// and skips the re-arm when the kernel ends it.
#include "wakeloop/backends.hpp"

#if defined(__linux__)

#include "common.hpp"
#include "linux_fd.hpp"

#include <algorithm>
#include <bit>
#include <cerrno>
#include <csignal>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <system_error>

#include <linux/io_uring.h>
#include <poll.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

namespace wakeloop
{

	namespace
	{

		static_assert(std::endian::native == std::endian::little, "poll32_events is written for little-endian hosts");

		constexpr unsigned kEntries = 8;  // SQEs: the two polls and the rare re-arm
		constexpr std::uint64_t kWakeTag = 1;
		constexpr std::uint64_t kStopTag = 2;
		constexpr unsigned kRequiredFeatures = IORING_FEAT_EXT_ARG | IORING_FEAT_SINGLE_MMAP | IORING_FEAT_NODROP;

		int sys_setup(unsigned entries, io_uring_params* p) { return static_cast<int>(::syscall(__NR_io_uring_setup, entries, p)); }

		int sys_enter(int fd, unsigned to_submit, unsigned min_complete, unsigned flags, const void* arg, std::size_t argsz)
		{
			return static_cast<int>(::syscall(__NR_io_uring_enter, fd, to_submit, min_complete, flags, arg, argsz));
		}

		int sys_register(int fd, unsigned op, const void* arg, unsigned nr)
		{
			return static_cast<int>(::syscall(__NR_io_uring_register, fd, op, arg, nr));
		}

		/// Unmaps a region on scope exit unless released.
		class Mapping
		{
		public:
			Mapping(void* p, std::size_t n) : p_(p), n_(n) {}
			~Mapping()
			{
				if (p_ != nullptr) ::munmap(p_, n_);
			}
			Mapping(const Mapping&) = delete;
			Mapping& operator=(const Mapping&) = delete;
			void release() noexcept { p_ = nullptr; }

		private:
			void* p_;
			std::size_t n_;
		};

		void* map_ring(int fd, std::size_t size, off_t offset, const char* what)
		{
			void* p = ::mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE, fd, offset);
			if (p == MAP_FAILED) detail::throw_errno(what);
			return p;
		}

		template <class T>
		T* at(void* base, unsigned offset)
		{
			return reinterpret_cast<T*>(static_cast<char*>(base) + offset);
		}

		// Ring indices shared with the kernel. The compiler builtins, rather than std::atomic_ref,
		// because the instrumented libc++ used for MemorySanitizer may predate atomic_ref.
		unsigned load_acquire(const unsigned* p) { return __atomic_load_n(p, __ATOMIC_ACQUIRE); }
		unsigned load_relaxed(const unsigned* p) { return __atomic_load_n(p, __ATOMIC_RELAXED); }
		void store_release(unsigned* p, unsigned v) { __atomic_store_n(p, v, __ATOMIC_RELEASE); }

	}  // namespace

	template <Defect D>
	UringBackend<D>::UringBackend(WaitSpec spec) : wait_us_(detail::checked_bound(spec.wait_us)), poll_(spec.poll)
	{
		detail::Fd wake_fd = detail::make_eventfd("eventfd (wake)");
		detail::Fd stop_fd = detail::make_eventfd("eventfd (stop)");

		// The kernel fills this through a raw system call, which MemorySanitizer cannot see: it is
		// value-initialized first, so every field it reads back is defined.
		io_uring_params p{};
		p.flags = IORING_SETUP_SINGLE_ISSUER | IORING_SETUP_DEFER_TASKRUN | IORING_SETUP_R_DISABLED;
		detail::Fd ring_fd(sys_setup(kEntries, &p));
		if (ring_fd.get() < 0) detail::throw_errno("io_uring_setup (single issuer, deferred task run)");
		detail::kernel_wrote(&p, sizeof(p));
		if ((p.features & kRequiredFeatures) != kRequiredFeatures)
		{
			throw std::system_error(ENOTSUP, std::generic_category(),
			                        "io_uring: the kernel lacks the extended wait argument, single mmap or no-drop completions");
		}

		const std::size_t sq_size = p.sq_off.array + p.sq_entries * sizeof(unsigned);
		const std::size_t cq_size = p.cq_off.cqes + p.cq_entries * sizeof(io_uring_cqe);
		const std::size_t map_size = std::max(sq_size, cq_size);
		void* map = map_ring(ring_fd.get(), map_size, IORING_OFF_SQ_RING, "mmap (io_uring rings)");
		Mapping map_guard(map, map_size);
		const std::size_t sqes_size = p.sq_entries * sizeof(io_uring_sqe);
		void* sqes = map_ring(ring_fd.get(), sqes_size, IORING_OFF_SQES, "mmap (io_uring submission entries)");
		Mapping sqes_guard(sqes, sqes_size);

		ring_.map = map;
		ring_.map_size = map_size;
		ring_.sqes = sqes;
		ring_.sqes_size = sqes_size;
		ring_.sq_head = at<unsigned>(map, p.sq_off.head);
		ring_.sq_tail = at<unsigned>(map, p.sq_off.tail);
		ring_.sq_array = at<unsigned>(map, p.sq_off.array);
		ring_.sq_mask = *at<unsigned>(map, p.sq_off.ring_mask);
		ring_.sq_entries = p.sq_entries;
		ring_.cq_head = at<unsigned>(map, p.cq_off.head);
		ring_.cq_tail = at<unsigned>(map, p.cq_off.tail);
		ring_.cq_mask = *at<unsigned>(map, p.cq_off.ring_mask);
		ring_.cqes = at<void>(map, p.cq_off.cqes);

		map_guard.release();
		sqes_guard.release();
		ring_.fd = ring_fd.release();
		wake_fd_ = wake_fd.release();
		stop_fd_ = stop_fd.release();
	}

	template <Defect D>
	UringBackend<D>::~UringBackend()
	{
		::munmap(ring_.sqes, ring_.sqes_size);
		::munmap(ring_.map, ring_.map_size);
		::close(ring_.fd);  // the kernel cancels the polls
		::close(wake_fd_);
		::close(stop_fd_);
	}

	template <Defect D>
	void UringBackend<D>::start()
	{
		// The enabling thread becomes the ring's single issuer.
		if (sys_register(ring_.fd, IORING_REGISTER_ENABLE_RINGS, nullptr, 0) < 0) detail::throw_errno("io_uring_register (enable)");
		arm_stop_poll();
		if constexpr (D != Defect::unobserved_primitive) arm_wake_poll();
		submit();
	}

	template <Defect D>
	void UringBackend<D>::wake() noexcept
	{
		detail::signal_eventfd(wake_fd_, "io_uring: write to the wake eventfd failed");
	}

	template <Defect D>
	void UringBackend<D>::stop() noexcept
	{
		detail::signal_eventfd(stop_fd_, "io_uring: write to the stop eventfd failed");
	}

	template <Defect D>
	const char* UringBackend<D>::wait_method() const noexcept
	{
		return "io_uring_enter(getevents, ext_arg; single_issuer, defer_taskrun)";
	}

	template <Defect D>
	void* UringBackend<D>::next_sqe()
	{
		const unsigned head = load_acquire(ring_.sq_head);
		const unsigned tail = load_relaxed(ring_.sq_tail) + sq_pending_;
		if (tail - head >= ring_.sq_entries) throw std::runtime_error("io_uring: submission queue full");
		const unsigned index = tail & ring_.sq_mask;
		auto* sqe = static_cast<io_uring_sqe*>(ring_.sqes) + index;
		std::memset(sqe, 0, sizeof(*sqe));
		ring_.sq_array[index] = index;
		++sq_pending_;
		return sqe;
	}

	template <Defect D>
	void UringBackend<D>::arm_wake_poll()
	{
		auto* sqe = static_cast<io_uring_sqe*>(next_sqe());
		sqe->opcode = IORING_OP_POLL_ADD;
		sqe->fd = wake_fd_;
		sqe->poll32_events = POLLIN;
		sqe->len = IORING_POLL_ADD_MULTI;
		if constexpr (D == Defect::no_retrigger) sqe->len = 0;  // single-shot
		sqe->user_data = kWakeTag;
	}

	template <Defect D>
	void UringBackend<D>::arm_stop_poll()
	{
		auto* sqe = static_cast<io_uring_sqe*>(next_sqe());
		sqe->opcode = IORING_OP_POLL_ADD;
		sqe->fd = stop_fd_;
		sqe->poll32_events = POLLIN;
		sqe->user_data = kStopTag;
	}

	template <Defect D>
	void UringBackend<D>::submit()
	{
		if (sq_pending_ == 0) return;
		const unsigned n = sq_pending_;
		store_release(ring_.sq_tail, load_relaxed(ring_.sq_tail) + n);
		sq_pending_ = 0;
		int submitted = -1;
		do
		{
			submitted = sys_enter(ring_.fd, n, 0, 0, nullptr, 0);
		} while (submitted < 0 && errno == EINTR);  // a signal: nothing was taken, submit again
		if (submitted < 0) detail::throw_errno("io_uring_enter (submit)");
		if (static_cast<unsigned>(submitted) != n) throw std::runtime_error("io_uring: the kernel took fewer submissions than given");
	}

	/// One non-blocking poll (the widened lazy-wake plant's window): runs the deferred task work.
	/// Completions stay in the ring until reaped, so the next wait still sees them.
	template <Defect D>
	void UringBackend<D>::poll_nowait()
	{
		int r = -1;
		do
		{
			r = sys_enter(ring_.fd, 0, 0, IORING_ENTER_GETEVENTS, nullptr, 0);
		} while (r < 0 && errno == EINTR);
		if (r < 0) detail::throw_errno("io_uring_enter (poll)");
	}

	template <Defect D>
	void UringBackend<D>::wait()
	{
		if (poll_)
		{
			// Poll mode: run the deferred task work and return, waiting for nothing. A zero timeout
			// with min_complete 1 instead takes the kernel's timeout path whenever no completion is
			// ready, which costs more than a ready completion, so arms would differ in the wait.
			int r = -1;
			do
			{
				r = sys_enter(ring_.fd, 0, 0, IORING_ENTER_GETEVENTS, nullptr, 0);
			} while (r < 0 && errno == EINTR);
			if (r < 0) detail::throw_errno("io_uring_enter (poll)");
			return;
		}
		// Value-initialized, as the kernel reads them through a raw system call.
		__kernel_timespec ts{};
		ts.tv_sec = wait_us_ / 1000000;
		ts.tv_nsec = (wait_us_ % 1000000) * 1000;
		io_uring_getevents_arg arg{};
		arg.sigmask = 0;
		arg.sigmask_sz = _NSIG / 8;
		arg.ts = wait_us_ == 0 ? 0 : reinterpret_cast<std::uintptr_t>(&ts);
		const int r = sys_enter(ring_.fd, 0, 1, IORING_ENTER_GETEVENTS | IORING_ENTER_EXT_ARG, &arg, sizeof(arg));
		if (r < 0 && errno != ETIME && errno != EINTR) detail::throw_errno("io_uring_enter (wait)");
	}

	template <Defect D>
	void UringBackend<D>::after_wait()
	{
		reap();
	}

	/// After the core's stop check, as in the 2026-09 loop: a stopping worker submits nothing.
	template <Defect D>
	void UringBackend<D>::before_drain()
	{
		if (!rearm_) return;
		// The kernel ended the wake poll. Re-armed before the core drains, as at start.
		rearm_ = false;
		if constexpr (D != Defect::no_retrigger)
		{
			arm_wake_poll();
			submit();
		}
	}

	template <Defect D>
	void UringBackend<D>::reap()
	{
		unsigned head = load_relaxed(ring_.cq_head);
		const unsigned tail = load_acquire(ring_.cq_tail);
		auto* cqes = static_cast<io_uring_cqe*>(ring_.cqes);
		for (; head != tail; ++head)
		{
			const io_uring_cqe& cqe = cqes[head & ring_.cq_mask];
			detail::kernel_wrote(&cqe, sizeof(cqe));
			if (cqe.user_data == kWakeTag)
			{
				if (cqe.res < 0) throw std::system_error(-cqe.res, std::generic_category(), "io_uring poll on the wake eventfd");
				if ((cqe.flags & IORING_CQE_F_MORE) == 0) rearm_ = true;
			}
			else if (cqe.user_data == kStopTag && cqe.res < 0)
			{
				throw std::system_error(-cqe.res, std::generic_category(), "io_uring poll on the stop eventfd");
			}
		}
		store_release(ring_.cq_head, head);
	}

	WAKELOOP_INSTANTIATE_ALL(UringBackend)

}  // namespace wakeloop

#endif  // __linux__
