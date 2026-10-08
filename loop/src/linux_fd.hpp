// Descriptor helpers shared by the Linux backends.
#pragma once

#if defined(__linux__)

#include "common.hpp"

#include <cerrno>
#include <cstdint>
#include <system_error>
#include <utility>

#include <sys/eventfd.h>
#include <unistd.h>

namespace wakeloop::detail
{

	[[noreturn]] inline void throw_errno(const char* what) { throw std::system_error(errno, std::generic_category(), what); }

	/// Owns a descriptor until release(); closes it on scope exit otherwise.
	class Fd
	{
	public:
		explicit Fd(int fd) : fd_(fd) {}
		~Fd()
		{
			if (fd_ >= 0) ::close(fd_);
		}
		Fd(Fd&& other) noexcept : fd_(other.release()) {}
		Fd(const Fd&) = delete;
		Fd& operator=(const Fd&) = delete;
		Fd& operator=(Fd&&) = delete;
		int get() const noexcept { return fd_; }
		int release() noexcept { return std::exchange(fd_, -1); }

	private:
		int fd_;
	};

	/// A non-blocking eventfd; throws on failure.
	inline Fd make_eventfd(const char* what)
	{
		Fd fd(::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC));
		if (fd.get() < 0) throw_errno(what);
		return fd;
	}

	/// Adds 1 to an eventfd. A saturated counter (EAGAIN) is read to zero and written again.
	/// Any other failure is a broken invariant (see fatal()).
	inline void signal_eventfd(int fd, const char* what) noexcept
	{
		const std::uint64_t one = 1;
		for (;;)
		{
			const ssize_t n = ::write(fd, &one, sizeof(one));
			if (n == static_cast<ssize_t>(sizeof(one))) return;
			if (n < 0 && errno == EINTR) continue;
			if (n < 0 && errno == EAGAIN)
			{
				std::uint64_t drained = 0;
				if (::read(fd, &drained, sizeof(drained)) < 0 && errno != EAGAIN) fatal(what, errno);
				continue;
			}
			fatal(what, errno);
		}
	}

}  // namespace wakeloop::detail

#endif  // __linux__
