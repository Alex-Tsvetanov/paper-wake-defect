// A minimal test harness for wakeloop: one test per process, no third-party framework, and no
// exceptions on any test path.
//
// Outcomes: exit 0 and "PASS: ..." on success; exit 1 and "FAIL: <file>:<line>: <reason>" on a
// failed check; exit 2 and "DETECTED: <backend>.<test>: <evidence>" when a wake detector sees a
// missing wake. In a defect build CTest requires the DETECTED line from every detector.
#pragma once

#include <chrono>
#include <condition_variable>
#include <exception>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <utility>

namespace harness
{

	using Clock = std::chrono::steady_clock;
	using namespace std::chrono_literals;

	/// The outcome of a test. Tests return it rather than throw: no test unwinds through an
	/// exception, which keeps them clear of clang-cl's stack-use-after-return report during
	/// unwinding (lab/evidence/2026-09-26-clangcl-asan-uar-false-positive).
	struct Outcome
	{
		enum Kind
		{
			pass,
			fail,      // a failed check
			detected,  // a detector saw a missing wake
		};
		Kind kind = pass;
		std::string what;
	};

	inline std::string where(const char* file, int line)
	{
		std::ostringstream s;
		s << file << ":" << line << ": ";
		return s.str();
	}

#define CHECK(cond, msg)                                                                                  \
	do                                                                                                    \
	{                                                                                                     \
		if (!(cond))                                                                                      \
		{                                                                                                 \
			std::ostringstream check_msg_;                                                                \
			check_msg_ << msg;                                                                            \
			return ::harness::Outcome{::harness::Outcome::fail, ::harness::where(__FILE__, __LINE__) + check_msg_.str()}; \
		}                                                                                                 \
	} while (false)

#define DETECT(msg)                                                                  \
	do                                                                               \
	{                                                                                \
		std::ostringstream detect_msg_;                                              \
		detect_msg_ << msg;                                                          \
		return ::harness::Outcome{::harness::Outcome::detected, detect_msg_.str()}; \
	} while (false)

	/// Counts deliveries; the waiting thread sees every write the delivering thread made
	/// before add() (the mutex orders them).
	class Latch
	{
	public:
		void add()
		{
			{
				std::lock_guard lock(m_);
				++count_;
			}
			cv_.notify_all();
		}

		/// True when the count reached `n` before `deadline`.
		bool wait_until(int n, Clock::time_point deadline)
		{
			std::unique_lock lock(m_);
			return cv_.wait_until(lock, deadline, [&] { return count_ >= n; });
		}

		bool wait_for(int n, Clock::duration d) { return wait_until(n, Clock::now() + d); }

		/// True when the count reaches `n`; false when it stops growing for `stall`. A slow but
		/// progressing loop (for example under MemorySanitizer) is never cut off; CTest's
		/// timeout is the last resort.
		bool wait_progress(int n, Clock::duration stall)
		{
			std::unique_lock lock(m_);
			int last = count_;
			while (count_ < n)
			{
				if (!cv_.wait_for(lock, stall, [&] { return count_ >= n || count_ != last; })) return false;
				last = count_;
			}
			return true;
		}

		int count()
		{
			std::lock_guard lock(m_);
			return count_;
		}

	private:
		std::mutex m_;
		std::condition_variable cv_;
		int count_ = 0;
	};

	/// Runs loop.run() on its own thread. Stops the loop and joins on destruction, so a failed
	/// check cannot leave the worker running.
	template <class Loop>
	class Runner
	{
	public:
		explicit Runner(Loop& loop)
			: loop_(loop), thread_([this] {
				  try
				  {
					  loop_.run();
				  }
				  catch (const std::exception& e)
				  {
					  error_ = e.what();
				  }
				  catch (...)
				  {
					  error_ = "non-standard exception";
				  }
				  finished_.add();
			  })
		{
		}
		~Runner() { stop_and_join(); }
		Runner(const Runner&) = delete;
		Runner& operator=(const Runner&) = delete;

		void stop_and_join()
		{
			if (!thread_.joinable()) return;
			loop_.stop();
			thread_.join();
		}

		/// True when run() returned before `deadline`.
		bool wait_finished(Clock::duration d) { return finished_.wait_for(1, d); }

		std::thread::id id() const { return id_; }
		/// Valid after run() returned.
		const std::string& error() const { return error_; }

	private:
		Loop& loop_;
		std::string error_;
		Latch finished_;
		std::thread thread_;
		std::thread::id id_ = thread_.get_id();
	};

}  // namespace harness
