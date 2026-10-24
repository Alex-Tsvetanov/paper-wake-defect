// wakeloop: a minimal event loop with W workers, for measuring bounded-wait wake defects.
//
// Each worker owns a task stack and one backend object (backends.hpp) and waits with a bound of
// B microseconds (B = 0 blocks). Any thread may post a task to a worker. A post whose push finds
// that worker's stack empty wakes it only if the worker has announced that it parks; a worker
// that is running re-checks its stack before it parks, so it finds the post itself. This is the
// park handshake (design/v2-defects.md, section 1): a seq_cst push and load on the poster's side,
// a seq_cst store and load on the worker's, so at least one side sees the other. The 2026-09
// loop, which wakes on every push onto an empty stack, is the variant plain_wake.
//
// The defect D is a compile-time parameter. This header holds the core's sites (plain wake,
// absent wake, lazy-wake race, wrong worker, undrained edge); the backends hold the others.
// tests/defect_sites.cmake counts every site. See design/v2-defects.md.
#pragma once

#include "wakeloop/backends.hpp"
#include "wakeloop/defect.hpp"
#include "wakeloop/task_stack.hpp"

#include <atomic>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <vector>

namespace wakeloop
{

	/// The measured hooks: none. Tests substitute a type whose in_park() can stop a worker between
	/// the two steps of its park handshake (design/v2-defects.md, section 4).
	struct NoHooks
	{
		void in_park() noexcept {}
	};

	struct Config
	{
		std::int64_t wait_us = 0;  // the bound B in microseconds; 0 blocks
		unsigned workers = 1;      // W
		bool poll = false;         // every wait returns at once (H4's busy worker); wait_us unused
	};

	namespace detail
	{

		/// A broken invariant on a path that cannot report an error. Prints `what` and the OS
		/// error, then aborts: a lost wake must never be silent. Defined in loop/src/common.cpp.
		[[noreturn]] void fatal(const char* what, long os_error) noexcept;

		/// Appends list b to list a (both oldest first) and returns the joined list.
		inline Task* append(Task* a, Task* b) noexcept
		{
			if (a == nullptr) return b;
			Task* tail = a;
			while (tail->next != nullptr) tail = tail->next;
			tail->next = b;
			return a;
		}

		/// Runs at most n tasks of a list and returns the rest. Reads each link before running.
		inline Task* run_some(Task* task, unsigned n) noexcept
		{
			for (; task != nullptr && n > 0; --n)
			{
				Task* next = task->next;
				task->run(*task);
				task = next;
			}
			return task;
		}

	}  // namespace detail

	template <template <Defect> class Backend, Defect D, class Hooks = NoHooks>
	class Loop
	{
	public:
		using backend_type = Backend<D>;
		template <class H>
		using with_hooks = Loop<Backend, D, H>;

		static constexpr const char* name = backend_type::name;
		static constexpr Defect defect = D;

		/// One worker with bound wait_us, as the 2026-09 loop.
		explicit Loop(std::int64_t wait_us) : Loop(Config{wait_us, 1, false}) {}

		/// Throws std::invalid_argument for no workers or a negative bound, and std::system_error
		/// when a backend's setup fails.
		explicit Loop(const Config& cfg)
		{
			if (cfg.workers == 0) throw std::invalid_argument("wakeloop: a loop needs at least one worker");
			const WaitSpec spec{cfg.wait_us, cfg.poll};
			workers_.reserve(cfg.workers);
			for (unsigned i = 0; i < cfg.workers; ++i) workers_.push_back(std::make_unique<Worker>(spec));
		}

		Loop(const Loop&) = delete;
		Loop& operator=(const Loop&) = delete;

		/// The calling thread becomes worker `worker` until stop(). Call it once per worker.
		/// Throws std::out_of_range for a worker the loop does not have.
		void run(unsigned worker = 0)
		{
			if (worker >= workers_.size()) throw std::out_of_range("wakeloop: run() names a worker the loop does not have");
			Worker& w = *workers_[worker];
			if (w.ran.exchange(true)) throw std::logic_error("wakeloop: run() may be called once per worker");
			if (stop_.load(std::memory_order_acquire)) return;
			w.backend.start();
			// Started before drained: a post whose push precedes this drain is run by it, and any
			// later post signals a wake object the backend already watches.
			if (stop_.load(std::memory_order_acquire)) return;
			drain(w);
			while (!stop_.load(std::memory_order_acquire))
			{
				park_and_wait(w);
				w.passes.fetch_add(1, std::memory_order_relaxed);
				w.backend.after_wait();
				if (stop_.load(std::memory_order_acquire)) return;
				w.backend.before_drain();
				drain(w);
			}
		}

		/// Any thread, any time, more than once. Ends every worker's run(), blocked or not.
		void stop() noexcept
		{
			stop_.store(true, std::memory_order_release);
			for (auto& w : workers_) w->backend.stop();
		}

		/// Posts to worker 0. Any thread. Never blocks, never allocates.
		void post(Task& task) noexcept { post_unchecked(0, task); }

		/// Posts to worker `worker`. Any thread. Never blocks, never allocates. A worker the loop
		/// does not have is a broken invariant: the process aborts.
		void post_to(unsigned worker, Task& task) noexcept
		{
			if (worker >= workers_.size()) detail::fatal("wakeloop: post_to() names a worker the loop does not have", 0);
			post_unchecked(worker, task);
		}

		unsigned workers() const noexcept { return static_cast<unsigned>(workers_.size()); }

		/// Number of passes of a worker's loop so far: wait returns, plus parks that found work
		/// on the re-check and skipped the wait.
		std::uint64_t passes(unsigned worker = 0) const noexcept
		{
			return workers_[worker]->passes.load(std::memory_order_relaxed);
		}

		const char* wait_method() const noexcept { return workers_[0]->backend.wait_method(); }

		Hooks& hooks() noexcept { return hooks_; }

	private:
#if defined(_MSC_VER)
#pragma warning(push)
#pragma warning(disable : 4324)  // the padding is the point: workers do not share a cache line
#endif
		struct alignas(64) Worker
		{
			explicit Worker(WaitSpec spec) : backend(spec) {}

			detail::TaskStack stack;
			backend_type backend;
			std::atomic<std::uint64_t> passes{0};
			std::atomic<bool> ran{false};
			std::atomic<bool> sleeping{false};  // the worker has announced that it parks
			Task* leftover = nullptr;           // undrained edge only; worker thread only
		};
#if defined(_MSC_VER)
#pragma warning(pop)
#endif

		/// The poster's side of the handshake: a seq_cst push, then a seq_cst load of the flag.
		void post_unchecked(unsigned index, Task& task) noexcept
		{
			Worker& w = *workers_[index];
			if (!w.stack.push(task)) return;  // an earlier post's wake, or the worker's re-check, covers it
			if constexpr (D == Defect::absent_wake)
			{
				return;
			}
			else if constexpr (D == Defect::plain_wake)
			{
				w.backend.wake();
			}
			else
			{
				if (!w.sleeping.load(std::memory_order_seq_cst)) return;  // running: its re-check finds the task
				if constexpr (D == Defect::wrong_worker)
				{
					workers_[(index + 1) % workers_.size()]->backend.wake();
				}
				else
				{
					w.backend.wake();
				}
			}
		}

		/// The worker's side of the handshake: announce, then re-check, then wait; a non-empty
		/// re-check skips a wait that could block. Every pass runs it, also when the wait returns
		/// at once (poll mode): the loop does not know that its wait will not block. In poll mode
		/// the wait itself still runs every pass, as a busy loop collects its other completions
		/// every pass, so every variant makes the same wait calls and differs only in its wakes.
		void park_and_wait(Worker& w)
		{
			if constexpr (D == Defect::plain_wake)
			{
				hooks_.in_park();
				w.backend.wait();
			}
			else if constexpr (D == Defect::lazy_wake_race)
			{
				// The look comes before the announcement: a post in between finds no sleeper,
				// skips its wake, and the worker then waits with the post queued.
				const bool idle = w.stack.empty();
				hooks_.in_park();
				if (!idle)
				{
					if (w.backend.polls()) w.backend.wait();
					return;
				}
				w.sleeping.store(true, std::memory_order_seq_cst);
				w.backend.wait();
				w.sleeping.store(false, std::memory_order_relaxed);
			}
			else
			{
				w.sleeping.store(true, std::memory_order_seq_cst);
				hooks_.in_park();
				if (w.stack.empty() || w.backend.polls()) w.backend.wait();
				w.sleeping.store(false, std::memory_order_relaxed);
			}
		}

		void drain(Worker& w) noexcept
		{
			if constexpr (D == Defect::undrained_edge)
			{
				w.leftover = detail::run_some(detail::append(w.leftover, w.stack.take_all()), kUndrainedBudget);
			}
			else
			{
				detail::run_all(w.stack.take_all());
			}
		}

		std::vector<std::unique_ptr<Worker>> workers_;
		std::atomic<bool> stop_{false};
		[[no_unique_address]] Hooks hooks_;
	};

}  // namespace wakeloop
