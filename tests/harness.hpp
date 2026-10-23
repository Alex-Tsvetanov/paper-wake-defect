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
#include <vector>

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

	/// Runs loop.run(w) for every worker w on a thread of its own. Stops the loop and joins on
	/// destruction, so a failed check cannot leave a worker running.
	template <class Loop>
	class Runner
	{
	public:
		explicit Runner(Loop& loop) : loop_(loop), errors_(loop.workers())
		{
			const unsigned n = loop.workers();
			threads_.reserve(n);
			try
			{
				for (unsigned w = 0; w < n; ++w)
				{
					threads_.emplace_back([this, w] { work(w); });
					ids_.push_back(threads_.back().get_id());
				}
			}
			catch (...)
			{
				stop_and_join();
				throw;
			}
		}
		~Runner() { stop_and_join(); }
		Runner(const Runner&) = delete;
		Runner& operator=(const Runner&) = delete;

		void stop_and_join()
		{
			loop_.stop();
			for (auto& t : threads_)
			{
				if (t.joinable()) t.join();
			}
		}

		/// True when every worker's run() returned within `d`.
		bool wait_finished(Clock::duration d) { return finished_.wait_for(static_cast<int>(threads_.size()), d); }

		std::thread::id id(unsigned worker = 0) const { return ids_[worker]; }
		/// The first error a worker's run() threw, or empty. Valid once that worker finished.
		std::string error()
		{
			std::lock_guard lock(m_);
			for (const auto& e : errors_)
			{
				if (!e.empty()) return e;
			}
			return {};
		}

	private:
		void work(unsigned w)
		{
			std::string error;
			try
			{
				loop_.run(w);
			}
			catch (const std::exception& e)
			{
				error = e.what();
			}
			catch (...)
			{
				error = "non-standard exception";
			}
			{
				std::lock_guard lock(m_);
				errors_[w] = std::move(error);
			}
			finished_.add();
		}

		Loop& loop_;
		std::mutex m_;
		std::vector<std::string> errors_;  // guarded by m_
		Latch finished_;
		std::vector<std::thread> threads_;
		std::vector<std::thread::id> ids_;
	};

}  // namespace harness
