// Host-specific pieces of wakeprobe: timer controls, the gap sleep, host and compiler names.
#pragma once

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <string>
#include <thread>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <timeapi.h>
#else
#include <unistd.h>
#endif
#if defined(__linux__)
#include <cerrno>
#include <cstdlib>
#include <fcntl.h>
#include <grp.h>
#include <sys/prctl.h>
#include <sys/types.h>
#endif

namespace wakeprobe
{

	// ---------------------------------------------------------------------------
	// Linux timer slack
	// ---------------------------------------------------------------------------

	constexpr bool kHaveTimerSlack =
#if defined(__linux__)
		true;
#else
		false;
#endif

	/// This thread's timer slack in ns (PR_GET_TIMERSLACK), or -1 where there is none.
	inline long long read_timer_slack() noexcept
	{
#if defined(__linux__)
		return static_cast<long long>(::prctl(PR_GET_TIMERSLACK, 0, 0, 0, 0));
#else
		return -1;
#endif
	}

	/// Sets this thread's timer slack. Threads created afterwards by this thread inherit it.
	/// Returns the prctl result (0 on success), or -1 where there is none.
	inline int set_timer_slack(unsigned long ns) noexcept
	{
#if defined(__linux__)
		return ::prctl(PR_SET_TIMERSLACK, ns, 0, 0, 0);
#else
		(void)ns;
		return -1;
#endif
	}

	// ---------------------------------------------------------------------------
	// Linux PM QoS CPU latency request
	// ---------------------------------------------------------------------------

	/// Holds a /dev/cpu_dma_latency request for the life of the object; the kernel drops
	/// it when the descriptor closes, so nothing persists. Opening needs root.
	class PmQos
	{
	public:
		PmQos() = default;
		PmQos(const PmQos&) = delete;
		PmQos& operator=(const PmQos&) = delete;
		~PmQos()
		{
#if defined(__linux__)
			if (fd_ >= 0) ::close(fd_);
#endif
		}

		/// Writes `us` as the request. False with errno_ set on failure.
		bool request(int us)
		{
#if defined(__linux__)
			fd_ = ::open("/dev/cpu_dma_latency", O_RDWR | O_CLOEXEC);
			if (fd_ < 0)
			{
				errno_ = errno;
				return false;
			}
			const std::int32_t value = us;
			if (::write(fd_, &value, sizeof(value)) != static_cast<ssize_t>(sizeof(value)))
			{
				errno_ = errno;
				return false;
			}
			std::int32_t now = -1;
			if (::read(fd_, &now, sizeof(now)) == static_cast<ssize_t>(sizeof(now))) readback_ = now;
			ok_ = true;
			return true;
#else
			(void)us;
			errno_ = -1;
			return false;
#endif
		}

		bool ok() const noexcept { return ok_; }
		int error() const noexcept { return errno_; }
		long long readback() const noexcept { return readback_; }  // the aggregate target, us

	private:
		[[maybe_unused]] int fd_ = -1;  // used on Linux only
		bool ok_ = false;
		int errno_ = 0;
		long long readback_ = -1;
	};

	/// Under sudo, returns to the invoking user once root-only setup is done, so the run
	/// itself and the files it writes are not root's. Returns the uid now in effect, or -1
	/// when dropping failed.
	inline long long drop_sudo_privileges() noexcept
	{
#if defined(__linux__)
		if (::geteuid() != 0) return static_cast<long long>(::getuid());
		const char* uid_s = std::getenv("SUDO_UID");
		const char* gid_s = std::getenv("SUDO_GID");
		if (uid_s == nullptr || gid_s == nullptr) return 0;  // plain root, nothing to return to
		const auto uid = static_cast<uid_t>(std::strtoul(uid_s, nullptr, 10));
		const auto gid = static_cast<gid_t>(std::strtoul(gid_s, nullptr, 10));
		if (::setgroups(1, &gid) != 0 || ::setgid(gid) != 0 || ::setuid(uid) != 0) return -1;
		// The uid change makes the process non-dumpable, which stops LeakSanitizer's exit-time
		// scan (it ptrace-attaches to the threads). The only root-obtained resource left is
		// the PM QoS descriptor, so dumpability is restored.
		if (::prctl(PR_SET_DUMPABLE, 1, 0, 0, 0) != 0) return -1;
		return static_cast<long long>(::getuid());
#else
		return -1;
#endif
	}

	// ---------------------------------------------------------------------------
	// Windows timer resolution
	// ---------------------------------------------------------------------------

	constexpr bool kHaveTimerPeriod =
#ifdef _WIN32
		true;
#else
		false;
#endif

	/// The system timer resolution in 100ns units (NtQueryTimerResolution), or -1.
	/// This is the system-wide current value; since Windows 10 2004 a process that did not
	/// ask for a finer resolution can still be served at the default one.
	inline long long timer_resolution_100ns() noexcept
	{
#ifdef _WIN32
		using QueryFn = LONG(WINAPI*)(PULONG, PULONG, PULONG);
		HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
		if (ntdll == nullptr) return -1;
		auto query = reinterpret_cast<QueryFn>(reinterpret_cast<void*>(GetProcAddress(ntdll, "NtQueryTimerResolution")));
		ULONG coarsest = 0, finest = 0, current = 0;
		if (query == nullptr || query(&coarsest, &finest, &current) != 0) return -1;
		return static_cast<long long>(current);
#else
		return -1;
#endif
	}

	/// timeBeginPeriod(ms) for the life of the object; 0 leaves the system default.
	class TimerPeriod
	{
	public:
		explicit TimerPeriod(unsigned ms) : ms_(ms)
		{
#ifdef _WIN32
			if (ms_ == 0) return;
#ifndef PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
#define PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION 0x4
#endif
			// Windows 11 may ignore the request for a process it considers invisible; opt out.
			PROCESS_POWER_THROTTLING_STATE state{};
			state.Version = PROCESS_POWER_THROTTLING_CURRENT_VERSION;
			state.ControlMask = PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION;
			state.StateMask = 0;
			optout_ok_ = SetProcessInformation(GetCurrentProcess(), ProcessPowerThrottling, &state, sizeof(state)) != 0;
			result_ = static_cast<long>(timeBeginPeriod(ms_));
			active_ = (result_ == TIMERR_NOERROR);
#endif
		}
		~TimerPeriod()
		{
#ifdef _WIN32
			if (active_) timeEndPeriod(ms_);
#endif
		}
		TimerPeriod(const TimerPeriod&) = delete;
		TimerPeriod& operator=(const TimerPeriod&) = delete;

		unsigned requested_ms() const noexcept { return ms_; }
		bool requested() const noexcept { return ms_ != 0; }
		long begin_result() const noexcept { return result_; }  // MMRESULT, 0 = TIMERR_NOERROR
		bool optout_ok() const noexcept { return optout_ok_; }

	private:
		unsigned ms_;
		long result_ = -1;
		[[maybe_unused]] bool active_ = false;  // used on Windows only
		bool optout_ok_ = false;
	};

	// ---------------------------------------------------------------------------
	// Gap sleep
	// ---------------------------------------------------------------------------

	/// Sleeps the gap between posts. On Windows std::this_thread::sleep_for is tick-granular,
	/// so a high-resolution waitable timer is used there; it does not change the system timer
	/// resolution. Failed timer calls fall back to sleep_for and are counted.
	class GapSleeper
	{
	public:
		GapSleeper()
		{
#ifdef _WIN32
#ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#endif
			timer_ = CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
#endif
		}
		~GapSleeper()
		{
#ifdef _WIN32
			if (timer_ != nullptr) CloseHandle(timer_);
#endif
		}
		GapSleeper(const GapSleeper&) = delete;
		GapSleeper& operator=(const GapSleeper&) = delete;

		const char* method() const noexcept
		{
#ifdef _WIN32
			return timer_ != nullptr ? "high_res_waitable_timer" : "sleep_for";
#else
			return "sleep_for";
#endif
		}
		int set_failures() const noexcept { return set_failures_; }
		int wait_failures() const noexcept { return wait_failures_; }

		void sleep(std::chrono::microseconds d)
		{
			if (d.count() <= 0) return;
#ifdef _WIN32
			if (timer_ != nullptr)
			{
				LARGE_INTEGER due;
				due.QuadPart = -static_cast<LONGLONG>(d.count()) * 10;  // relative, 100ns units
				if (!SetWaitableTimerEx(timer_, &due, 0, nullptr, nullptr, nullptr, 0))
				{
					++set_failures_;
				}
				else if (WaitForSingleObject(timer_, INFINITE) != WAIT_OBJECT_0)
				{
					++wait_failures_;
				}
				else
				{
					return;
				}
			}
#endif
			std::this_thread::sleep_for(d);
		}

	private:
#ifdef _WIN32
		HANDLE timer_ = nullptr;
#endif
		int set_failures_ = 0;
		int wait_failures_ = 0;
	};

	// ---------------------------------------------------------------------------
	// Names
	// ---------------------------------------------------------------------------

	inline std::string compiler_name()
	{
#if defined(__clang__) && defined(_MSC_VER)
		std::string name = std::string("clang-cl ") + __clang_version__;
#elif defined(__clang__)
		std::string name = std::string("clang ") + __clang_version__;
#elif defined(__GNUC__)
		std::string name = std::string("gcc ") + __VERSION__;
#elif defined(_MSC_VER)
		std::string name = "msvc " + std::to_string(_MSC_FULL_VER);
#else
		std::string name = "unknown";
#endif
		// __clang_version__ ends in a space on some builds.
		while (!name.empty() && name.back() == ' ') name.pop_back();
		return name;
	}

	inline std::string host_name()
	{
#ifdef _WIN32
		char buf[256];
		DWORD len = sizeof(buf);
		return GetComputerNameA(buf, &len) ? std::string(buf, len) : "unknown";
#else
		char buf[256] = {};
		return gethostname(buf, sizeof(buf) - 1) == 0 ? std::string(buf) : "unknown";
#endif
	}

	inline std::string utc_now()
	{
		const std::time_t t = std::time(nullptr);
		std::tm tm{};
#ifdef _WIN32
		gmtime_s(&tm, &t);
#else
		gmtime_r(&t, &tm);
#endif
		char buf[32];
		std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tm);
		return buf;
	}

}  // namespace wakeprobe
