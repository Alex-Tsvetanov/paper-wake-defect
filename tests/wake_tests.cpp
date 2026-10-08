// Wake semantics of every wakeloop backend: usage  wakeloop_tests <backend> <test>
//
// Semantics tests (S1 to S11) pass in both arms. Wake detectors (D1 to D5) pass in the fixed
// arm and must report DETECTED in a defect build. See design/minimal-loop.md, section 3.
#include "harness.hpp"

#include "wakeloop/loop.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <optional>
#include <exception>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace
{

	using namespace harness;
	using wakeloop::Defect;
	using wakeloop::Task;

#if defined(_WIN32)
	// The default Windows timer tick rounds every timed wait up to a multiple of it.
	constexpr auto kTick = 16ms;
#else
	constexpr auto kTick = 1ms;
#endif

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

	/// A task that posts another task on the same loop when it runs.
	template <class Loop>
	struct Poster : Task
	{
		Poster(Loop& l, Task& t, Latch& lt) : loop(&l), then(&t), latch(&lt) { run = &Poster::on_run; }
		static void on_run(Task& t) noexcept
		{
			auto& p = static_cast<Poster&>(t);
			p.loop->post(*p.then);
			p.latch->add();
		}
		Loop* loop;
		Task* then;
		Latch* latch;
	};

	/// A task that stops its loop.
	template <class Loop>
	struct Stopper : Task
	{
		explicit Stopper(Loop& l) : loop(&l) { run = &Stopper::on_run; }
		static void on_run(Task& t) noexcept { static_cast<Stopper&>(t).loop->stop(); }
		Loop* loop;
	};

	double ms(Clock::duration d) { return std::chrono::duration<double, std::milli>(d).count(); }

	// ------------------------------------------------------------------ semantics tests

	// S1: a posted task runs exactly once, on the thread inside run().
	template <class Loop>
	Outcome post_runs_once()
	{
		Latch latch;
		Probe p(latch);
		Loop loop(5000);
		Runner r(loop);
		loop.post(p);
		CHECK(latch.wait_for(1, 2s), "the post was not delivered within 2 s");
		std::this_thread::sleep_for(50ms);
		CHECK(p.runs.load() == 1, "the task ran " << p.runs.load() << " times");
		CHECK(p.ran_on == r.id(), "the task did not run on the worker thread");
		return {};
	}

	// S2: a task posted before run() runs in the first pass.
	template <class Loop>
	Outcome post_before_run()
	{
		Latch latch;
		Probe p(latch);
		Loop loop(5000);
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
		Loop loop(5000);
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

	/// Posts `per` tasks from each of `producers` threads and returns once all producers are done.
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
		Loop loop(5000);
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
		Loop loop(5000);
		Poster<Loop> first(loop, second, latch);
		Runner r(loop);
		loop.post(first);
		CHECK(latch.wait_for(2, 2s), "the self-posted task was not delivered within 2 s");
		CHECK(second.ran_on == r.id(), "the self-posted task did not run on the worker");
		return {};
	}

	// S6: run() returns at once after an earlier stop().
	template <class Loop>
	Outcome stop_before_run()
	{
		Loop loop(0);
		loop.stop();
		const auto t0 = Clock::now();
		loop.run();
		const auto took = Clock::now() - t0;
		CHECK(took < 1s, "run() took " << ms(took) << " ms after an earlier stop()");
		return {};
	}

	// S7: with a blocking wait, stop() from another thread ends run() within 1 s. In a defect
	// build this shows that the switch leaves the stop path alone.
	template <class Loop>
	Outcome stop_wakes_blocked()
	{
		Loop loop(0);
		Runner r(loop);
		std::this_thread::sleep_for(50ms);
		loop.stop();
		CHECK(r.wait_finished(1s), "run() did not return within 1 s of stop()");
		r.stop_and_join();
		CHECK(r.error().empty(), "run() threw: " << r.error());
		return {};
	}

	// S8: a task that calls stop() ends run() after that pass.
	template <class Loop>
	Outcome stop_from_task()
	{
		Loop loop(5000);
		Stopper<Loop> s(loop);
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
		Loop loop(0);
		Runner r(loop);
		if constexpr (Loop::defect == Defect::none)
		{
			// A defect build cannot deliver with a blocking wait; the posts are for the fixed arm.
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
		Loop loop(10000);
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

	// S11: with a bound, even a defect build delivers each post within about one period.
	template <class Loop>
	Outcome defect_is_delay_not_loss()
	{
		constexpr auto kBound = 20ms;
		const auto limit = 3 * std::max<Clock::duration>(kBound, kTick);
		Latch latch;
		std::deque<Probe> probes;
		Loop loop(20000);
		Runner r(loop);
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

	// ------------------------------------------------------------------ wake detectors

	// D1: a blocking loop delivers one post.
	template <class Loop>
	Outcome detect_blocking_single()
	{
		Latch latch;
		Probe p(latch);
		Loop loop(0);
		Runner r(loop);
		std::this_thread::sleep_for(20ms);
		loop.post(p);
		if (!latch.wait_for(1, 1s)) DETECT("a post to a parked blocking loop was not delivered within 1 s");
		return {};
	}

	// D2: a blocking loop delivers 64 posts made back to back, which share wakes.
	template <class Loop>
	Outcome detect_blocking_burst()
	{
		constexpr int kPosts = 64;
		Latch latch;
		std::deque<Probe> probes;
		Loop loop(0);
		Runner r(loop);
		std::this_thread::sleep_for(20ms);
		for (int i = 0; i < kPosts; ++i) loop.post(probes.emplace_back(latch));
		if (!latch.wait_for(kPosts, 1s)) DETECT(latch.count() << " of " << kPosts << " back-to-back posts delivered within 1 s");
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
		Loop loop(0);
		Runner r(loop);
		std::this_thread::sleep_for(20ms);
		post_from_threads(loop, nodes, log);
		// Done when all arrive; detected when delivery stalls for 1 s after the producers finish.
		// No overall deadline: a slow but progressing loop is not a missing wake.
		if (!log.latch.wait_progress(kTotal, 1s))
		{
			DETECT(log.latch.count() << " of " << kTotal << " posts from " << kProducers << " threads delivered; none for 1 s");
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
		Loop loop(0);
		Poster<Loop> first(loop, second, first_ran);
		Runner r(loop);
		std::this_thread::sleep_for(20ms);
		loop.post(first);
		if (!first_ran.wait_for(1, 1s)) DETECT("the first post was not delivered within 1 s");
		if (!second_ran.wait_for(1, 1s)) DETECT("a task posted by a task was not delivered within 1 s");
		return {};
	}

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
		Loop loop(200000);
		Runner r(loop);
		std::this_thread::sleep_for(50ms);

		// The delay of one post, or nothing when it was not delivered within 5 B.
		auto post_one = [&](int index) -> std::optional<Clock::duration> {
			auto& p = probes.emplace_back(latch);
			const auto posted = Clock::now();
			loop.post(p);
			if (!latch.wait_for(index + 1, 5 * kBound)) return std::nullopt;
			return p.ran_at - posted;
		};

		// Warmup: the worker has woken once and parked again.
		if (!post_one(0)) DETECT("the warmup post was not delivered within 5 B");
		std::vector<Clock::duration> delays;
		int over = 0;
		for (int i = 1; i <= kPosts; ++i)
		{
			std::this_thread::sleep_until(probes.back().ran_at + kGap);
			const auto delivered = post_one(i);
			if (!delivered) DETECT("post " << i << " was not delivered within 5 B");
			const auto d = *delivered;
			delays.push_back(d);
			if (d > kThreshold && ++over > kPosts / 2)
			{
				DETECT(over << " of " << i << " posts took longer than B / 5 = " << ms(kThreshold) << " ms; the median exceeds it");
			}
		}
		std::sort(delays.begin(), delays.end());
		const auto median = delays[kPosts / 2];
		std::printf("median delay %.3f ms over %d posts (threshold %.1f ms)\n", ms(median), kPosts, ms(kThreshold));
		if (median > kThreshold) DETECT("median delay " << ms(median) << " ms exceeds B / 5 = " << ms(kThreshold) << " ms");
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
		static constexpr std::array<Entry, 16> kTests{{
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
			{"detect_blocking_single", &detect_blocking_single<Loop>},
			{"detect_blocking_burst", &detect_blocking_burst<Loop>},
			{"detect_blocking_producers", &detect_blocking_producers<Loop>},
			{"detect_self_post_blocking", &detect_self_post_blocking<Loop>},
			{"detect_latency21", &detect_latency21<Loop>},
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

	bool dispatch(std::string_view backend, std::string_view test, bool& known_backend, Outcome& out)
	{
		known_backend = true;
#if defined(__linux__)
		if (backend == wakeloop::EpollLoop::name) return run_test<wakeloop::EpollLoop>(test, out);
		if (backend == wakeloop::UringLoop::name) return run_test<wakeloop::UringLoop>(test, out);
#elif defined(_WIN32)
		if (backend == wakeloop::IocpLoop::name) return run_test<wakeloop::IocpLoop>(test, out);
#endif
		known_backend = false;
		return false;
	}

}  // namespace

int main(int argc, char** argv)
{
	if (argc != 3)
	{
		std::fprintf(stderr, "usage: wakeloop_tests <backend> <test>\n");
		return 64;
	}
	const std::string_view backend = argv[1];
	const std::string_view test = argv[2];
	const std::string id = std::string(backend) + "." + std::string(test);
	Outcome out;
	bool known_backend = false;
	try
	{
		if (!dispatch(backend, test, known_backend, out))
		{
			std::fprintf(stderr, "FAIL: unknown %s '%s'\n", known_backend ? "test" : "backend",
			             std::string(known_backend ? test : backend).c_str());
			return 64;
		}
	}
	catch (const std::exception& e)
	{
		// Only a loop that cannot be set up throws (for example, a host without io_uring).
		std::fprintf(stderr, "FAIL: %s: exception: %s\n", id.c_str(), e.what());
		return 1;
	}
	switch (out.kind)
	{
		case Outcome::pass:
			std::printf("PASS: %s (defect build: %s)\n", id.c_str(), wakeloop::kDefectSeeded ? "yes" : "no");
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
