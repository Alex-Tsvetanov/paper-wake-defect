// The wake matrix: usage  wakeloop_tests <backend> <variant> <workers> <test>
//
// Semantics tests (S1 to S12) pass in every variant: with a bound, a defect is a delay and never
// a loss. Wake detectors (D1 to D10) pass in the fixed loop; tests/CMakeLists.txt holds the
// verdict each defect must give on each detector (design/v2-defects.md, section 5).
#include "harness.hpp"

#include "wakeloop/variants.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <exception>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace
{

	using namespace harness;
	using wakeloop::Config;
	using wakeloop::Defect;
	using wakeloop::Task;

#if defined(_WIN32)
	// The default Windows timer tick rounds every timed wait up to a multiple of it.
	constexpr auto kTick = 16ms;
#else
	constexpr auto kTick = 1ms;
#endif

	/// Tasks behind a gate in D7 and D9: more than one undrained-edge pass can run.
	constexpr int kBatch = 3 * static_cast<int>(wakeloop::kUndrainedBudget);

	/// W, from the command line.
	unsigned g_workers = 1;

	Config cfg(std::int64_t wait_us) { return Config{wait_us, g_workers, false}; }

	/// Records when and where it ran, and signals a latch.
	struct Probe : Task
	{
		explicit Probe(Latch& l) : latch(&l) { run = &Probe::on_run; }
		static void on_run(Task& t) noexcept
		{
			auto& p = static_cast<Probe&>(t);
			p.ran_at = Clock::now();
			p.ran_on = std::this_thread::get_id();
			p.runs.fetch_add(1, std::memory_order_relaxed);
			p.latch->add();
		}
		Latch* latch;
		Clock::time_point ran_at{};
		std::thread::id ran_on{};
		std::atomic<int> runs{0};
	};

	/// A task tagged with its producer and sequence number, for ordering checks.
	struct SeqNode : Task
	{
		SeqNode() { run = &SeqNode::on_run; }
		static void on_run(Task& t) noexcept
		{
			auto& n = static_cast<SeqNode&>(t);
			n.log->record(n.producer, n.seq);
		}
		struct Log
		{
			explicit Log(int producers) : last(static_cast<std::size_t>(producers), -1), count(static_cast<std::size_t>(producers), 0) {}
			// Worker thread only; read after the latch or after join.
			void record(int producer, int seq) noexcept
			{
				auto p = static_cast<std::size_t>(producer);
				if (seq <= last[p]) ++out_of_order;
				last[p] = seq;
				++count[p];
				order.push_back(seq);
				latch.add();
			}
			std::vector<int> last;
			std::vector<int> count;
			std::vector<int> order;
			int out_of_order = 0;
			Latch latch;
		};
		Log* log = nullptr;
		int producer = 0;
		int seq = 0;
	};

	/// A task that posts another task on worker 0 when it runs.
	template <class Loop>
	struct Poster : Task
	{
		Poster(Task& t, Latch& lt) : then(&t), latch(&lt) { run = &Poster::on_run; }
		static void on_run(Task& t) noexcept
		{
			auto& p = static_cast<Poster&>(t);
			p.loop->post(*p.then);
			p.latch->add();
		}
		Loop* loop = nullptr;  // set once the loop exists; the task is declared before it
		Task* then;
		Latch* latch;
	};

	/// A task that stops its loop.
	template <class Loop>
	struct Stopper : Task
	{
		Stopper() { run = &Stopper::on_run; }
		static void on_run(Task& t) noexcept { static_cast<Stopper&>(t).loop->stop(); }
		Loop* loop = nullptr;  // set once the loop exists; the task is declared before it
	};

	/// A task that holds its worker until released (at most 10 s, so a broken test still ends).
	struct Gate : Task
	{
		Gate() { run = &Gate::on_run; }
		static void on_run(Task& t) noexcept
		{
			auto& g = static_cast<Gate&>(t);
			g.entered.add();
			g.release.wait_for(1, 10s);
		}
		Latch entered;
		Latch release;
	};

	/// Opens a gate when it leaves scope, so an early return never holds a worker for 10 s.
	/// Declare it after the Runner, so it opens the gate before the Runner joins.
	struct GateOpener
	{
		Gate& gate;
		~GateOpener()
		{
			if (gate.release.count() == 0) gate.release.add();
		}
	};

	/// Test hooks: stops the first post after arm() just before its push (for the lazy-wake race,
	/// between its look at the stack and its push) until release(), at most 10 s. arm() starts a
	/// new round.
	struct ForceHooks
	{
		void before_push() noexcept
		{
			std::unique_lock lock(m_);
			if (!armed_) return;
			armed_ = false;
			reached_ = true;
			cv_.notify_all();
			cv_.wait_for(lock, 10s, [this] { return released_; });
		}
		void arm()
		{
			std::lock_guard lock(m_);
			armed_ = true;
			reached_ = false;
			released_ = false;
		}
		bool wait_reached(Clock::duration d)
		{
			std::unique_lock lock(m_);
			return cv_.wait_for(lock, d, [this] { return reached_; });
		}
		void release()
		{
			{
				std::lock_guard lock(m_);
				released_ = true;
			}
			cv_.notify_all();
		}

	private:
		std::mutex m_;
		std::condition_variable cv_;
		bool armed_ = false;
		bool reached_ = false;
		bool released_ = false;
	};

	/// One task per worker, posted before run(), so each worker's first drain runs it whether or
	/// not its variant wakes. Once all ran and the workers settle, every worker has drained and
	/// consumed any wake the starters left: a later post reaches a worker only through a wake or
	/// a timed return. This replaces a fixed sleep, which a loaded host can outrun. Declare it
	/// before the loop, so the tasks outlive it.
	struct Starters
	{
		Latch ran;
		std::deque<Probe> probes;

		template <class Loop>
		void post(Loop& loop)
		{
			for (unsigned w = 0; w < loop.workers(); ++w) loop.post_to(w, probes.emplace_back(ran));
		}

		/// True once every worker ran its starter and then settled, within 5 s. A blocking loop
		/// has settled when no worker's pass count changed for 50 ms; a bounded loop when every
		/// worker has returned from its wait at least once since its starter ran.
		template <class Loop>
		bool settle(Loop& loop, bool blocking)
		{
			const auto deadline = Clock::now() + 5s;
			if (!ran.wait_until(static_cast<int>(loop.workers()), deadline)) return false;
			std::vector<std::uint64_t> last(loop.workers());
			for (unsigned w = 0; w < loop.workers(); ++w) last[w] = loop.passes(w);
			while (Clock::now() < deadline)
			{
				std::this_thread::sleep_for(blocking ? 50ms : 1ms);
				bool all_moved = true;
				bool none_moved = true;
				for (unsigned w = 0; w < loop.workers(); ++w)
				{
					const auto now = loop.passes(w);
					if (now == last[w]) all_moved = false;
					else none_moved = false;
					if (blocking) last[w] = now;
				}
				if (blocking ? none_moved : all_moved) return true;
			}
			return false;
		}
	};

	double ms(Clock::duration d) { return std::chrono::duration<double, std::milli>(d).count(); }

	Clock::duration median_of(std::vector<Clock::duration> v)
	{
		std::sort(v.begin(), v.end());
		return v[v.size() / 2];
	}

	int count_over(const std::vector<Clock::duration>& v, Clock::duration limit)
	{
		return static_cast<int>(std::count_if(v.begin(), v.end(), [&](Clock::duration d) { return d > limit; }));
	}

	// ------------------------------------------------------------------ semantics tests

	// S1: a posted task runs exactly once, on its worker's thread.
	template <class Loop>
	Outcome post_runs_once()
	{
		Latch latch;
		Probe p(latch);
		Loop loop(cfg(5000));
		Runner r(loop);
		loop.post(p);
		CHECK(latch.wait_for(1, 2s), "the post was not delivered within 2 s");
		std::this_thread::sleep_for(50ms);
		CHECK(p.runs.load() == 1, "the task ran " << p.runs.load() << " times");
		CHECK(p.ran_on == r.id(0), "the task did not run on worker 0's thread");
		return {};
	}

	// S2: a task posted before run() runs in the first pass.
	template <class Loop>
	Outcome post_before_run()
	{
		Latch latch;
		Probe p(latch);
		Loop loop(cfg(5000));
		loop.post(p);
		Runner r(loop);
		CHECK(latch.wait_for(1, 2s), "a post made before run() was not delivered within 2 s");
		CHECK(p.runs.load() == 1, "the task ran " << p.runs.load() << " times");
		return {};
	}

	// S3: tasks from one thread run in post order.
	template <class Loop>
	Outcome fifo_one_producer()
	{
		constexpr int kPosts = 1000;
		SeqNode::Log log(1);
		std::vector<SeqNode> nodes(kPosts);
		Loop loop(cfg(5000));
		Runner r(loop);
		for (int i = 0; i < kPosts; ++i)
		{
			nodes[static_cast<std::size_t>(i)].log = &log;
			nodes[static_cast<std::size_t>(i)].seq = i;
			loop.post(nodes[static_cast<std::size_t>(i)]);
		}
		CHECK(log.latch.wait_progress(kPosts, 5s), "delivery stalled for 5 s at " << log.latch.count() << " of " << kPosts);
		r.stop_and_join();
		CHECK(static_cast<int>(log.order.size()) == kPosts, "delivered " << log.order.size());
		for (int i = 0; i < kPosts; ++i)
		{
			CHECK(log.order[static_cast<std::size_t>(i)] == i, "position " << i << " holds task " << log.order[static_cast<std::size_t>(i)]);
		}
		return {};
	}

	/// Posts `per` tasks from each of `producers` threads to worker 0 and returns once all
	/// producers are done.
	template <class Loop>
	void post_from_threads(Loop& loop, std::vector<std::vector<SeqNode>>& nodes, SeqNode::Log& log)
	{
		std::vector<std::thread> threads;
		for (std::size_t p = 0; p < nodes.size(); ++p)
		{
			threads.emplace_back([&loop, &nodes, &log, p] {
				int seq = 0;
				for (auto& n : nodes[p])
				{
					n.log = &log;
					n.producer = static_cast<int>(p);
					n.seq = seq++;
					loop.post(n);
				}
			});
		}
		for (auto& t : threads) t.join();
	}

	// S4: 4 producers post 10000 tasks each; each runs once, in each producer's order.
	template <class Loop>
	Outcome many_producers()
	{
		constexpr int kProducers = 4;
		constexpr int kPer = 10000;
		SeqNode::Log log(kProducers);
		std::vector<std::vector<SeqNode>> nodes(kProducers, std::vector<SeqNode>(kPer));
		Loop loop(cfg(5000));
		Runner r(loop);
		post_from_threads(loop, nodes, log);
		CHECK(log.latch.wait_progress(kProducers * kPer, 5s),
		      "delivery stalled for 5 s at " << log.latch.count() << " of " << kProducers * kPer);
		r.stop_and_join();
		CHECK(log.out_of_order == 0, log.out_of_order << " tasks ran out of their producer's order");
		for (int p = 0; p < kProducers; ++p)
		{
			CHECK(log.count[static_cast<std::size_t>(p)] == kPer, "producer " << p << ": " << log.count[static_cast<std::size_t>(p)] << " ran");
		}
		return {};
	}

	// S5: a task posts another task, which runs.
	template <class Loop>
	Outcome self_post()
	{
		Latch latch;
		Probe second(latch);
		Poster<Loop> first(second, latch);
		Loop loop(cfg(5000));
		first.loop = &loop;
		Runner r(loop);
		loop.post(first);
		CHECK(latch.wait_for(2, 2s), "the self-posted task was not delivered within 2 s");
		CHECK(second.ran_on == r.id(0), "the self-posted task did not run on worker 0");
		return {};
	}

	// S6: run() returns at once after an earlier stop().
	template <class Loop>
	Outcome stop_before_run()
	{
		Loop loop(cfg(0));
		loop.stop();
		const auto t0 = Clock::now();
		for (unsigned w = 0; w < loop.workers(); ++w) loop.run(w);
		const auto took = Clock::now() - t0;
		CHECK(took < 1s, "run() took " << ms(took) << " ms after an earlier stop()");
		return {};
	}

	// S7: with a blocking wait, stop() from another thread ends every run() within 1 s. In a
	// defect variant this shows that the defect leaves the stop path alone.
	template <class Loop>
	Outcome stop_wakes_blocked()
	{
		Loop loop(cfg(0));
		Runner r(loop);
		std::this_thread::sleep_for(50ms);
		loop.stop();
		CHECK(r.wait_finished(1s), "run() did not return within 1 s of stop()");
		r.stop_and_join();
		CHECK(r.error().empty(), "run() threw: " << r.error());
		return {};
	}

	// S8: a task that calls stop() ends every run() after that pass.
	template <class Loop>
	Outcome stop_from_task()
	{
		Stopper<Loop> s;
		Loop loop(cfg(5000));
		s.loop = &loop;
		Runner r(loop);
		loop.post(s);
		CHECK(r.wait_finished(2s), "run() did not return within 2 s of a task calling stop()");
		CHECK(r.error().empty(), "run() threw: " << r.error());
		return {};
	}

	// S9: an idle blocking loop does not spin, also after posts have woken it.
	template <class Loop>
	Outcome idle_blocking_no_spin()
	{
		Latch latch;
		std::deque<Probe> probes;
		Loop loop(cfg(0));
		Runner r(loop);
		if constexpr (Loop::defect == Defect::none)
		{
			// A defect variant need not deliver with a blocking wait; the posts are for the fixed loop.
			for (int i = 0; i < 10; ++i)
			{
				loop.post(probes.emplace_back(latch));
				CHECK(latch.wait_for(i + 1, 1s), "post " << i << " was not delivered within 1 s");
			}
		}
		std::this_thread::sleep_for(20ms);
		const auto p0 = loop.passes();
		std::this_thread::sleep_for(200ms);
		const auto grew = loop.passes() - p0;
		CHECK(grew <= 1, "an idle blocking loop made " << grew << " passes in 200 ms");
		return {};
	}

	// S10: an idle loop with B = 10 ms returns from its wait at least once and does not spin.
	template <class Loop>
	Outcome idle_bounded_passes()
	{
		constexpr auto kBound = 10ms;
		Loop loop(cfg(10000));
		Runner r(loop);
		std::this_thread::sleep_for(20ms);
		const auto t0 = Clock::now();
		const auto p0 = loop.passes();
		std::this_thread::sleep_for(300ms);
		const auto grew = loop.passes() - p0;
		const auto elapsed = Clock::now() - t0;
		const auto most = static_cast<std::uint64_t>((elapsed + kBound - 1ns) / kBound) + 2;
		CHECK(grew >= 1, "no timed return in " << ms(elapsed) << " ms with B = 10 ms");
		CHECK(grew <= most, grew << " passes in " << ms(elapsed) << " ms with B = 10 ms (at most " << most << ")");
		return {};
	}

	// S11: with a bound, every variant delivers each post within about one period.
	template <class Loop>
	Outcome defect_is_delay_not_loss()
	{
		constexpr auto kBound = 20ms;
		// Three periods of the timed return: the bound rounded up to whole timer ticks.
		const auto limit = 3 * ((kBound + kTick - 1ns) / kTick) * kTick;
		Latch latch;
		std::deque<Probe> probes;
		Starters starters;
		Loop loop(cfg(20000));
		starters.post(loop);
		Runner r(loop);
		CHECK(starters.settle(loop, false), "the workers did not start and settle within 5 s");
		for (int i = 0; i < 5; ++i)
		{
			auto& p = probes.emplace_back(latch);
			const auto posted = Clock::now();
			loop.post(p);
			CHECK(latch.wait_for(i + 1, 1s), "post " << i << " was lost (not delivered within 1 s)");
			CHECK(p.ran_at - posted <= limit, "post " << i << " took " << ms(p.ran_at - posted) << " ms, limit " << ms(limit));
			std::this_thread::sleep_for(5ms);
		}
		return {};
	}

	// S12: each worker runs the posts made to it, on its own thread.
	template <class Loop>
	Outcome workers_independent()
	{
		Latch latch;
		std::deque<Probe> probes;
		Loop loop(cfg(5000));
		Runner r(loop);
		for (unsigned w = 0; w < loop.workers(); ++w)
		{
			auto& p = probes.emplace_back(latch);
			loop.post_to(w, p);
			CHECK(latch.wait_for(static_cast<int>(w) + 1, 2s), "the post to worker " << w << " was not delivered within 2 s");
			CHECK(p.ran_on == r.id(w), "the post to worker " << w << " ran on another worker's thread");
		}
		return {};
	}

	/// A detection, unless a worker's run() threw: then the test fails, because a dead worker
	/// would look exactly like a missing wake.
#define DETECT_LIVE(runner, msg)                                                   \
	do                                                                             \
	{                                                                              \
		const std::string worker_error_ = (runner).error();                        \
		CHECK(worker_error_.empty(), "a worker's run() threw: " << worker_error_); \
		DETECT(msg);                                                               \
	} while (false)

	// ------------------------------------------------------------------ wake detectors, blocking

	// D1: a blocking loop delivers one post.
	template <class Loop>
	Outcome detect_blocking_single()
	{
		Latch latch;
		Probe p(latch);
		Starters starters;
		Loop loop(cfg(0));
		starters.post(loop);
		Runner r(loop);
		CHECK(starters.settle(loop, true), "the workers did not start and settle within 5 s");
		loop.post(p);
		if (!latch.wait_for(1, 1s)) DETECT_LIVE(r, "a post to a parked blocking loop was not delivered within 1 s");
		return {};
	}

	// D2: a blocking loop delivers 64 posts made back to back, which share wakes.
	template <class Loop>
	Outcome detect_blocking_burst()
	{
		constexpr int kPosts = 64;
		Latch latch;
		std::deque<Probe> probes;
		Starters starters;
		Loop loop(cfg(0));
		starters.post(loop);
		Runner r(loop);
		CHECK(starters.settle(loop, true), "the workers did not start and settle within 5 s");
		for (int i = 0; i < kPosts; ++i) loop.post(probes.emplace_back(latch));
		if (!latch.wait_for(kPosts, 1s)) DETECT_LIVE(r, latch.count() << " of " << kPosts << " back-to-back posts delivered within 1 s");
		return {};
	}

	// D3: a blocking loop delivers 40000 posts from 4 threads.
	template <class Loop>
	Outcome detect_blocking_producers()
	{
		constexpr int kProducers = 4;
		constexpr int kPer = 10000;
		constexpr int kTotal = kProducers * kPer;
		SeqNode::Log log(kProducers);
		std::vector<std::vector<SeqNode>> nodes(kProducers, std::vector<SeqNode>(kPer));
		Starters starters;
		Loop loop(cfg(0));
		starters.post(loop);
		Runner r(loop);
		CHECK(starters.settle(loop, true), "the workers did not start and settle within 5 s");
		post_from_threads(loop, nodes, log);
		// Done when all arrive; detected when delivery stalls for 1 s after the producers finish.
		// No overall deadline: a slow but progressing loop is not a missing wake.
		if (!log.latch.wait_progress(kTotal, 1s))
		{
			DETECT_LIVE(r, log.latch.count() << " of " << kTotal << " posts from " << kProducers << " threads delivered; none for 1 s");
		}
		return {};
	}

	// D4: a blocking loop delivers a task that a task posted.
	template <class Loop>
	Outcome detect_self_post_blocking()
	{
		Latch first_ran;
		Latch second_ran;
		Probe second(second_ran);
		Poster<Loop> first(second, first_ran);
		Starters starters;
		Loop loop(cfg(0));
		first.loop = &loop;
		starters.post(loop);
		Runner r(loop);
		CHECK(starters.settle(loop, true), "the workers did not start and settle within 5 s");
		loop.post(first);
		if (!first_ran.wait_for(1, 1s)) DETECT_LIVE(r, "the first post was not delivered within 1 s");
		if (!second_ran.wait_for(1, 1s)) DETECT_LIVE(r, "a task posted by a task was not delivered within 1 s");
		return {};
	}

	// D6: a blocking loop delivers 10 posts made one after another, each after the last ran.
	template <class Loop>
	Outcome detect_blocking_sequence()
	{
		constexpr int kPosts = 10;
		Latch latch;
		std::deque<Probe> probes;
		Starters starters;
		Loop loop(cfg(0));
		starters.post(loop);
		Runner r(loop);
		CHECK(starters.settle(loop, true), "the workers did not start and settle within 5 s");
		for (int i = 0; i < kPosts; ++i)
		{
			loop.post(probes.emplace_back(latch));
			if (!latch.wait_for(i + 1, 1s)) DETECT_LIVE(r, "post " << i << " of a sequence was not delivered within 1 s");
			std::this_thread::sleep_for(2ms);
		}
		return {};
	}

	// D7: a blocking loop delivers kBatch posts made while a task holds its worker.
	template <class Loop>
	Outcome detect_batch_blocking()
	{
		Gate gate;
		Latch latch;
		std::deque<Probe> probes;
		Starters starters;
		Loop loop(cfg(0));
		starters.post(loop);
		Runner r(loop);
		GateOpener opener{gate};
		CHECK(starters.settle(loop, true), "the workers did not start and settle within 5 s");
		loop.post(gate);
		if (!gate.entered.wait_for(1, 1s)) DETECT_LIVE(r, "the gate task was not delivered within 1 s");
		for (int i = 0; i < kBatch; ++i) loop.post(probes.emplace_back(latch));
		gate.release.add();
		if (!latch.wait_for(kBatch, 1s))
		{
			DETECT_LIVE(r, latch.count() << " of " << kBatch << " posts made behind a running task delivered within 1 s");
		}
		return {};
	}

	/// One forced lazy-wake interleaving. A gate holds the worker; task a is posted (its push
	/// finds the stack empty); a second thread starts to post b and is stopped by the hook before
	/// its push, after its look at the stack (which holds a); the gate opens and the worker drains
	/// a; then b's post completes. The fixed loop decides on b's push, which finds the stack
	/// empty, and wakes; the lazy-wake race decided on the look and does not.
	struct ForcedRound
	{
		Gate gate;
		Latch a_ran;
		Latch b_ran;
		Probe a{a_ran};
		Probe b{b_ran};
		Clock::time_point released_at{};  // when the hook let b's post go on
	};

	/// Runs one round. Returns empty on success; otherwise the detection or failure text, with
	/// `failed` set when the harness itself went wrong.
	template <class HookedLoop>
	std::optional<std::string> forced_round(HookedLoop& loop, ForcedRound& round, Clock::duration deadline, bool& failed)
	{
		GateOpener opener{round.gate};
		auto& hooks = loop.hooks();
		loop.post(round.gate);
		if (!round.gate.entered.wait_for(1, deadline)) return "the gate task was not delivered in time";
		loop.post(round.a);
		hooks.arm();
		std::thread poster([&] { loop.post(round.b); });
		if (!hooks.wait_reached(2s))
		{
			hooks.release();
			poster.join();
			failed = true;
			return "the poster never reached the hook";
		}
		round.gate.release.add();
		const bool a_delivered = round.a_ran.wait_for(1, deadline);
		round.released_at = Clock::now();
		hooks.release();
		poster.join();
		if (!a_delivered) return "task a was not delivered in time";
		if (!round.b_ran.wait_for(1, deadline))
		{
			return "a post whose wake was decided before the worker drained the stack was not delivered in time";
		}
		return std::nullopt;
	}

	// D8: the forced lazy-wake interleaving on a blocking loop.
	template <class Loop>
	Outcome detect_lazy_forced()
	{
		using Hooked = typename Loop::template with_hooks<ForceHooks>;
		ForcedRound round;
		Starters starters;
		Hooked loop(cfg(0));
		starters.post(loop);
		Runner r(loop);
		CHECK(starters.settle(loop, true), "the workers did not start and settle within 5 s");
		bool failed = false;
		if (auto why = forced_round(loop, round, 1s, failed))
		{
			CHECK(!failed, *why);
			DETECT_LIVE(r, *why << " (deadline 1 s)");
		}
		return {};
	}

	// ------------------------------------------------------------------ wake detectors, bounded

	// D5: the paper's latency test. B = 200 ms, 21 posts, each 0.3 B after the previous
	// delivery; a median delay above B / 5 is a missing wake.
	template <class Loop>
	Outcome detect_latency21()
	{
		constexpr auto kBound = 200ms;
		constexpr auto kGap = kBound * 3 / 10;
		constexpr auto kThreshold = kBound / 5;
		constexpr int kPosts = 21;
		Latch latch;
		std::deque<Probe> probes;
		Starters starters;
		Loop loop(cfg(200000));
		starters.post(loop);
		Runner r(loop);
		CHECK(starters.settle(loop, false), "the workers did not start and settle within 5 s");

		// The delay of one post, or nothing when it was not delivered within 5 B.
		auto post_one = [&](int index) -> std::optional<Clock::duration> {
			auto& p = probes.emplace_back(latch);
			const auto posted = Clock::now();
			loop.post(p);
			if (!latch.wait_for(index + 1, 5 * kBound)) return std::nullopt;
			return p.ran_at - posted;
		};

		// Warmup: the worker has woken once and parked again.
		if (!post_one(0)) DETECT_LIVE(r, "the warmup post was not delivered within 5 B");
		std::vector<Clock::duration> delays;
		for (int i = 1; i <= kPosts; ++i)
		{
			std::this_thread::sleep_until(probes.back().ran_at + kGap);
			const auto delivered = post_one(i);
			if (!delivered) DETECT_LIVE(r, "post " << i << " was not delivered within 5 B");
			delays.push_back(*delivered);
		}
		const auto median = median_of(delays);
		std::printf("median delay %.3f ms over %d posts (threshold %.1f ms); %d posts over B / 2\n", ms(median), kPosts,
		            ms(kThreshold), count_over(delays, kBound / 2));
		if (median > kThreshold) DETECT_LIVE(r, "median delay " << ms(median) << " ms exceeds B / 5 = " << ms(kThreshold) << " ms");
		return {};
	}

	// D9: seven rounds of kBatch posts made while a task holds the worker, B = 200 ms. The delay
	// of a round is that of its slowest task, from the gate's release; a median above B / 5 is a
	// missing wake.
	template <class Loop>
	Outcome detect_latency_batch()
	{
		constexpr auto kBound = 200ms;
		constexpr auto kThreshold = kBound / 5;
		constexpr int kRounds = 7;
		struct Round
		{
			Gate gate;
			Latch ran;
			std::deque<Probe> probes;
		};
		std::deque<Round> rounds;
		Starters starters;
		Loop loop(cfg(200000));
		starters.post(loop);
		Runner r(loop);
		CHECK(starters.settle(loop, false), "the workers did not start and settle within 5 s");
		std::vector<Clock::duration> delays;
		for (int k = 0; k < kRounds; ++k)
		{
			auto& round = rounds.emplace_back();
			GateOpener opener{round.gate};
			loop.post(round.gate);
			if (!round.gate.entered.wait_for(1, 5 * kBound)) DETECT_LIVE(r, "round " << k << ": the gate task was not delivered within 5 B");
			for (int i = 0; i < kBatch; ++i) loop.post(round.probes.emplace_back(round.ran));
			const auto released = Clock::now();
			round.gate.release.add();
			if (!round.ran.wait_for(kBatch, 5 * kBound))
			{
				DETECT_LIVE(r, "round " << k << ": " << round.ran.count() << " of " << kBatch << " tasks delivered within 5 B");
			}
			Clock::time_point last{};
			for (const auto& p : round.probes) last = std::max(last, p.ran_at);
			delays.push_back(last - released);
		}
		const auto median = median_of(delays);
		std::printf("median slowest-task delay %.3f ms over %d rounds of %d (threshold %.1f ms); %d rounds over B / 2\n",
		            ms(median), kRounds, kBatch, ms(kThreshold), count_over(delays, kBound / 2));
		if (median > kThreshold) DETECT_LIVE(r, "median delay " << ms(median) << " ms exceeds B / 5 = " << ms(kThreshold) << " ms");
		return {};
	}

	// D10: five forced lazy-wake interleavings with B = 200 ms. The delay of b from the moment
	// the hook let its post go on; a median above B / 5 is a missing wake.
	template <class Loop>
	Outcome detect_latency_forced()
	{
		using Hooked = typename Loop::template with_hooks<ForceHooks>;
		constexpr auto kBound = 200ms;
		constexpr auto kThreshold = kBound / 5;
		constexpr int kRounds = 5;
		std::deque<ForcedRound> rounds;
		Starters starters;
		Hooked loop(cfg(200000));
		starters.post(loop);
		Runner r(loop);
		CHECK(starters.settle(loop, false), "the workers did not start and settle within 5 s");
		std::vector<Clock::duration> delays;
		for (int k = 0; k < kRounds; ++k)
		{
			auto& round = rounds.emplace_back();
			bool failed = false;
			if (auto why = forced_round(loop, round, 5 * kBound, failed))
			{
				CHECK(!failed, "round " << k << ": " << *why);
				DETECT_LIVE(r, "round " << k << ": " << *why << " (deadline 5 B)");
			}
			delays.push_back(round.b.ran_at - round.released_at);
			std::this_thread::sleep_for(kBound / 10);
		}
		const auto median = median_of(delays);
		std::printf("median delay %.3f ms over %d forced rounds (threshold %.1f ms); %d rounds over B / 2\n", ms(median), kRounds,
		            ms(kThreshold), count_over(delays, kBound / 2));
		if (median > kThreshold) DETECT_LIVE(r, "median delay " << ms(median) << " ms exceeds B / 5 = " << ms(kThreshold) << " ms");
		return {};
	}

	// ------------------------------------------------------------------ dispatch

	/// Runs `test` into `out`; false when there is no such test.
	template <class Loop>
	bool run_test(std::string_view test, Outcome& out)
	{
		struct Entry
		{
			std::string_view name;
			Outcome (*fn)();
		};
		static constexpr std::array<Entry, 22> kTests{{
			{"post_runs_once", &post_runs_once<Loop>},
			{"post_before_run", &post_before_run<Loop>},
			{"fifo_one_producer", &fifo_one_producer<Loop>},
			{"many_producers", &many_producers<Loop>},
			{"self_post", &self_post<Loop>},
			{"stop_before_run", &stop_before_run<Loop>},
			{"stop_wakes_blocked", &stop_wakes_blocked<Loop>},
			{"stop_from_task", &stop_from_task<Loop>},
			{"idle_blocking_no_spin", &idle_blocking_no_spin<Loop>},
			{"idle_bounded_passes", &idle_bounded_passes<Loop>},
			{"defect_is_delay_not_loss", &defect_is_delay_not_loss<Loop>},
			{"workers_independent", &workers_independent<Loop>},
			{"detect_blocking_single", &detect_blocking_single<Loop>},
			{"detect_blocking_burst", &detect_blocking_burst<Loop>},
			{"detect_blocking_producers", &detect_blocking_producers<Loop>},
			{"detect_self_post_blocking", &detect_self_post_blocking<Loop>},
			{"detect_blocking_sequence", &detect_blocking_sequence<Loop>},
			{"detect_batch_blocking", &detect_batch_blocking<Loop>},
			{"detect_lazy_forced", &detect_lazy_forced<Loop>},
			{"detect_latency21", &detect_latency21<Loop>},
			{"detect_latency_batch", &detect_latency_batch<Loop>},
			{"detect_latency_forced", &detect_latency_forced<Loop>},
		}};
		for (const auto& e : kTests)
		{
			if (e.name == test)
			{
				out = e.fn();
				return true;
			}
		}
		return false;
	}

	int usage(const char* why)
	{
		std::fprintf(stderr, "FAIL: %s\nusage: wakeloop_tests <backend> <variant> <workers> <test>\n", why);
		return 64;
	}

}  // namespace

int main(int argc, char** argv)
{
	if (argc != 5) return usage("expected 4 arguments");
	const std::string_view backend = argv[1];
	const std::string_view variant = argv[2];
	const std::string_view workers = argv[3];
	const std::string_view test = argv[4];
	const auto defect = wakeloop::parse_defect(variant);
	if (!defect) return usage("unknown variant");
	if (!wakeloop::known_backend(backend)) return usage("unknown backend on this platform");
	if (!wakeloop::has_site(backend, *defect)) return usage("the variant has no site on this backend");
	const auto parsed = std::from_chars(workers.data(), workers.data() + workers.size(), g_workers);
	if (parsed.ec != std::errc{} || parsed.ptr != workers.data() + workers.size() || g_workers == 0 || g_workers > 64)
	{
		return usage("workers must be a whole number from 1 to 64");
	}

	const std::string id = std::string(backend) + "." + std::string(variant) + ".w" + std::string(workers) + "." + std::string(test);
	Outcome out;
	bool known_test = false;
	try
	{
		wakeloop::with_loop(backend, *defect, [&]<class L>() { known_test = run_test<L>(test, out); });
	}
	catch (const std::exception& e)
	{
		// Only a loop that cannot be set up throws (for example, a host without io_uring).
		std::fprintf(stderr, "FAIL: %s: exception: %s\n", id.c_str(), e.what());
		return 1;
	}
	if (!known_test) return usage("unknown test");
	switch (out.kind)
	{
		case Outcome::pass:
			std::printf("PASS: %s\n", id.c_str());
			return 0;
		case Outcome::detected:
			std::printf("DETECTED: %s: %s\n", id.c_str(), out.what.c_str());
			std::fflush(stdout);
			return 2;
		case Outcome::fail:
			break;
	}
	std::fprintf(stderr, "FAIL: %s: %s\n", id.c_str(), out.what.c_str());
	return 1;
}
