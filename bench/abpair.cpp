// abpair: the paired A/B harness of H4, the throughput cost of the fix (design/v2-defects.md,
// section 6).
//
// One process holds both arms; an arm is a variant of one backend. Every window builds a fresh
// loop of its arm's type in poll mode: the worker's wait returns at once and never sleeps, so
// posted work rides on wait returns. That is the busy worker of H4. The window starts W worker
// threads and P producer threads. A producer owns a ring of K tasks and keeps all K in flight:
// it posts a slot, and posts it again when that slot's task has run. With W > 1 a producer's
// posts go round robin over the workers and completion is tracked per slot, so no order across
// workers is assumed.
//
// A window ends in one of two ways. Count mode (--window-tasks N): the producers post N tasks
// in total, N / P each (the first N mod P producers one more). Timed mode (--window-ms D): the
// producers post for D ms. In both a producer then stops posting and waits until its in-flight
// tasks have run, and only after that does the window stop the loop and join the workers.
//
// Pairs run AB, BA, AB, BA, ... (--order abba), or AB or BA drawn per pair from a seeded
// std::mt19937_64 (--order shuffle). Pairs and positions count from 0.
//
// Output: one JSON object per window (pair, position, side, arm, tasks, passes; timed mode adds
// elapsed_ns and tasks_per_s), then one summary object, which is also printed to stdout. In
// timed mode the clock runs from the opening of the start gate, once every thread is up, to the
// moment the last producer has seen its last task run. Building the loop and starting and
// joining the threads are outside it.
//
// Task work (--work-ns N, default 0): each task spins for N ns of CPU on its worker before it
// counts as run. H4's workload is the decade grid N in {0, 1000, 10000, 100000} with W = 1,
// P = 1, K = 64, reported in full (Alex, 2026-10-25; design/v2-defects.md, section 6).
//
// --check is the self-test for a machine that times nothing: count mode with small numbers and
// no time in the output. It reads no clock, except that with --work-ns each task reads it to
// spin; nothing it measures is printed. It exits 0 only if every window ran exactly N tasks,
// every producer completed what it posted, each side ran R windows, and the windows ran in the
// requested order.

#include "wakeloop/variants.hpp"

#include "bench_common.hpp"
#include "probe_platform.hpp"

#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <exception>
#include <fstream>
#include <iostream>
#include <memory>
#include <optional>
#include <random>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

// Injected by bench/CMakeLists.txt.
#ifndef ABPAIR_BENCH_COMMIT
#define ABPAIR_BENCH_COMMIT "unknown"
#endif
#ifndef ABPAIR_BENCH_DIRTY
#define ABPAIR_BENCH_DIRTY 1
#endif
#ifndef ABPAIR_BENCH_CODE_COMMIT
#define ABPAIR_BENCH_CODE_COMMIT "unknown"
#endif
#ifndef ABPAIR_BUILD_TYPE
#define ABPAIR_BUILD_TYPE "unknown"
#endif

namespace
{

	using Clock = std::chrono::steady_clock;
	using benchutil::json_escape;

	constexpr unsigned kMaxWorkers = 64;  // as the wake tests
	constexpr unsigned kMaxProducers = 64;
	constexpr unsigned kMaxInflight = 1U << 16;

	// The numbers --check uses where the command line gives none.
	constexpr unsigned kCheckWorkers = 2;
	constexpr unsigned kCheckProducers = 2;
	constexpr unsigned kCheckInflight = 16;
	constexpr unsigned kCheckPairs = 2;
	constexpr std::uint64_t kCheckTasks = 2000;
	constexpr std::int64_t kMaxWorkNs = 1'000'000'000;  // one second of work per task

	struct Options
	{
		std::string backend;
		std::string arm_a = "none";
		std::string arm_b = "absent_wake";
		wakeloop::Defect defect_a = wakeloop::Defect::none;
		wakeloop::Defect defect_b = wakeloop::Defect::absent_wake;
		// 0 means not given; every given value is positive. resolve_defaults() fills the rest.
		unsigned workers = 0;
		unsigned producers = 0;
		unsigned inflight = 0;
		unsigned pairs = 0;
		bool shuffle = false;
		std::uint64_t seed = 0;
		std::int64_t window_ms = 0;
		std::uint64_t window_tasks = 0;
		std::int64_t work_ns = 0;  // per-task CPU work; 0 is none
		std::vector<unsigned> worker_cpus;  // empty: unpinned; else one CPU per worker
		std::vector<unsigned> producer_cpus;  // empty: unpinned; else one CPU per producer
		std::string out;
		bool check = false;

		bool timed() const { return window_ms > 0; }
		const std::string& arm(bool side_b) const { return side_b ? arm_b : arm_a; }
		wakeloop::Defect defect(bool side_b) const { return side_b ? defect_b : defect_a; }
	};

	[[noreturn]] void usage(const std::string& why)
	{
		std::cerr << "abpair: " << why << "\n"
		          << "usage: abpair --backend " << benchutil::backend_list() << " (--window-ms D | --window-tasks N) --out FILE\n"
		          << "       [--arm-a VARIANT] (default none) [--arm-b VARIANT] (default absent_wake)\n"
		          << "       VARIANT is one of " << benchutil::variant_list() << "\n"
		          << "       [--workers W] (default 1) [--producers P] (default 1) [--inflight K] (default 64)\n"
		          << "       [--pairs R] (default 8) [--order abba|shuffle] (default abba) [--seed N]\n"
		          << "       [--work-ns N] (default 0) each task spins for N ns of CPU on its worker (at most "
		          << kMaxWorkNs << ")\n"
		          << "       [--worker-cpus LIST] [--producer-cpus LIST] pin each worker (producer) thread to the\n"
		          << "       next CPU of a comma-separated list, one per thread\n"
		          << "  --check  self-test: count windows of small numbers, no time anywhere in the output;\n"
		          << "           takes no --window-ms, needs no --out, and fills W, P, K, R and N with\n"
		          << "           " << kCheckWorkers << ", " << kCheckProducers << ", " << kCheckInflight << ", " << kCheckPairs << " and "
		          << kCheckTasks << " where the command line gives none\n";
		std::exit(2);
	}

	/// A whole number from 1 up.
	template <typename T>
	T parse_positive(std::string_view flag, std::string_view text)
	{
		T value{};
		const auto* end = text.data() + text.size();
		const auto [ptr, ec] = std::from_chars(text.data(), end, value);
		if (ec != std::errc{} || ptr != end || value < 1)
		{
			usage(std::string(flag) + " expects a positive integer, got '" + std::string(text) + "'");
		}
		return value;
	}

	std::uint64_t parse_seed(std::string_view text)
	{
		std::uint64_t value = 0;
		const auto* end = text.data() + text.size();
		const auto [ptr, ec] = std::from_chars(text.data(), end, value);
		if (ec != std::errc{} || ptr != end) usage("--seed expects a whole number, got '" + std::string(text) + "'");
		return value;
	}

	std::int64_t parse_work(std::string_view text)
	{
		std::int64_t value = 0;
		const auto* end = text.data() + text.size();
		const auto [ptr, ec] = std::from_chars(text.data(), end, value);
		if (ec != std::errc{} || ptr != end || value < 0 || value > kMaxWorkNs)
		{
			usage("--work-ns expects a whole number from 0 to " + std::to_string(kMaxWorkNs) + ", got '" + std::string(text) + "'");
		}
		return value;
	}

	/// Checks one arm against the backend and the worker count; returns its variant.
	wakeloop::Defect parse_arm(std::string_view flag, const std::string& name, const Options& o)
	{
		const auto defect = wakeloop::parse_defect(name);
		if (!defect) usage(std::string(flag) + " must be one of " + benchutil::variant_list());
		if (!wakeloop::has_site(o.backend, *defect))
		{
			usage(std::string(flag) + ": the variant " + name + " has no site on the " + o.backend + " backend");
		}
		if (o.workers < wakeloop::min_workers(*defect))
		{
			usage(std::string(flag) + ": the variant " + name + " needs --workers >= " +
			      std::to_string(wakeloop::min_workers(*defect)));
		}
		return *defect;
	}

	void resolve_defaults(Options& o)
	{
		if (o.workers == 0) o.workers = o.check ? kCheckWorkers : 1;
		if (o.producers == 0) o.producers = o.check ? kCheckProducers : 1;
		if (o.inflight == 0) o.inflight = o.check ? kCheckInflight : 64;
		if (o.pairs == 0) o.pairs = o.check ? kCheckPairs : 8;
		if (o.check && o.window_tasks == 0) o.window_tasks = kCheckTasks;
	}

	Options parse_args(int argc, char** argv)
	{
		Options o;
		std::string order = "abba";
		for (int i = 1; i < argc; ++i)
		{
			const std::string_view flag = argv[i];
			if (flag == "--check")
			{
				o.check = true;
				continue;
			}
			if (i + 1 >= argc) usage("missing value for " + std::string(flag));
			const std::string_view value = argv[++i];
			if (flag == "--backend") o.backend = value;
			else if (flag == "--arm-a") o.arm_a = value;
			else if (flag == "--arm-b") o.arm_b = value;
			else if (flag == "--workers") o.workers = parse_positive<unsigned>(flag, value);
			else if (flag == "--producers") o.producers = parse_positive<unsigned>(flag, value);
			else if (flag == "--inflight") o.inflight = parse_positive<unsigned>(flag, value);
			else if (flag == "--pairs") o.pairs = parse_positive<unsigned>(flag, value);
			else if (flag == "--order") order = value;
			else if (flag == "--seed") o.seed = parse_seed(value);
			else if (flag == "--window-ms") o.window_ms = parse_positive<std::int64_t>(flag, value);
			else if (flag == "--window-tasks") o.window_tasks = parse_positive<std::uint64_t>(flag, value);
			else if (flag == "--work-ns") o.work_ns = parse_work(value);
			else if (flag == "--worker-cpus" || flag == "--producer-cpus")
			{
				auto& cpus = flag == "--worker-cpus" ? o.worker_cpus : o.producer_cpus;
				cpus = benchutil::parse_cpu_list(value);
				if (cpus.empty()) usage(std::string(flag) + " expects a comma-separated list of CPU numbers");
			}
			else if (flag == "--out") o.out = value;
			else usage("unknown flag " + std::string(flag));
		}
		if (order != "abba" && order != "shuffle") usage("--order must be abba or shuffle");
		o.shuffle = order == "shuffle";
		if (!wakeloop::known_backend(o.backend)) usage("--backend must be one of " + benchutil::backend_list());
		resolve_defaults(o);
		if (o.workers > kMaxWorkers) usage("--workers is at most " + std::to_string(kMaxWorkers));
		if (o.producers > kMaxProducers) usage("--producers is at most " + std::to_string(kMaxProducers));
		if (o.inflight > kMaxInflight) usage("--inflight is at most " + std::to_string(kMaxInflight));
		if (!o.worker_cpus.empty() && o.worker_cpus.size() != o.workers) usage("--worker-cpus needs one CPU per worker");
		if (!o.producer_cpus.empty() && o.producer_cpus.size() != o.producers)
		{
			usage("--producer-cpus needs one CPU per producer");
		}
		o.defect_a = parse_arm("--arm-a", o.arm_a, o);
		o.defect_b = parse_arm("--arm-b", o.arm_b, o);
		if (o.check)
		{
			if (o.window_ms > 0) usage("--check runs count windows and takes no --window-ms");
		}
		else
		{
			if ((o.window_ms > 0) == (o.window_tasks > 0)) usage("give exactly one of --window-ms and --window-tasks");
			if (o.out.empty()) usage("--out is required");
		}
		return o;
	}

	// ----------------------------------------------------------------------------------------
	// One window
	// ----------------------------------------------------------------------------------------

#if defined(_MSC_VER)
#pragma warning(push)
#pragma warning(disable : 4324)  // the padding is the point: slots do not share a cache line
#endif
	/// One of a producer's K tasks. The worker that runs it counts it and then frees it with a
	/// release store; after that store the worker does not touch the task again, so the
	/// producer, which reads `busy` with acquire, may post it again.
	struct alignas(64) Slot : wakeloop::Task
	{
		Slot() { run = &Slot::on_run; }
		static void on_run(wakeloop::Task& t) noexcept
		{
			auto& s = static_cast<Slot&>(t);
			if (s.work_ns > 0)
			{
				// The task's work: a busy spin on the worker, not a sleep.
				const auto until = Clock::now() + std::chrono::nanoseconds(s.work_ns);
				while (Clock::now() < until)
				{
				}
			}
			++s.completed;
			s.busy.store(false, std::memory_order_release);
		}
		std::atomic<bool> busy{false};
		std::int64_t work_ns = 0;     // set before the window starts
		std::uint64_t completed = 0;  // written by the running worker, read after the joins
	};
#if defined(_MSC_VER)
#pragma warning(pop)
#endif

	struct Producer
	{
		Producer(std::size_t k, std::uint64_t tasks, std::int64_t work_ns) : slots(k), quota(tasks)
		{
			for (auto& s : slots) s.work_ns = work_ns;
		}
		std::vector<Slot> slots;
		std::uint64_t quota;  // count mode: how many tasks this producer posts
		std::uint64_t posted = 0;  // producer thread only; read after the join
		Clock::time_point finished{};  // timed mode: when the last in-flight task had run
	};

	/// What the threads of one window share.
	struct Shared
	{
		std::atomic<bool> go{false};      // the start gate
		std::atomic<bool> stop{false};    // timed mode: stop posting
		std::atomic<bool> failed{false};  // a worker threw or a thread did not start: everyone leaves
		std::atomic<unsigned> ready{0};   // producers waiting at the gate
	};

	struct WindowResult
	{
		std::uint64_t tasks = 0;   // tasks that ran, summed over the slots
		std::uint64_t posted = 0;  // tasks posted, summed over the producers
		std::vector<std::uint64_t> producer_posted;
		std::vector<std::uint64_t> producer_completed;
		std::vector<std::uint64_t> passes;  // per worker
		std::optional<std::int64_t> elapsed_ns;  // timed mode only
		std::string wait_method;
		std::string error;
		std::string ran;  // the variant of the loop type that ran, from L::defect
	};

	/// The tasks producer p posts in count mode: N / P, and one more for the first N mod P.
	std::uint64_t quota_of(const Options& o, unsigned p)
	{
		return o.window_tasks / o.producers + (p < o.window_tasks % o.producers ? 1 : 0);
	}

	/// One producer thread: keeps every slot in flight until told to stop, then waits for the
	/// slots that are still running. Posts go round robin over the workers.
	template <class Loop>
	void produce(Loop& loop, Shared& shared, Producer& p, unsigned first_worker, bool timed)
	{
		shared.ready.fetch_add(1, std::memory_order_release);
		while (!shared.go.load(std::memory_order_acquire)) std::this_thread::yield();

		const unsigned workers = loop.workers();
		const std::size_t size = p.slots.size();
		unsigned target = first_worker;
		std::size_t cursor = 0;
		bool stopping = false;
		while (!shared.failed.load(std::memory_order_acquire))
		{
			std::size_t in_flight = 0;
			bool posted_now = false;
			for (std::size_t n = 0; n < size; ++n)
			{
				Slot& s = p.slots[cursor];
				if (++cursor == size) cursor = 0;
				if (s.busy.load(std::memory_order_acquire))
				{
					++in_flight;
					continue;
				}
				if (!stopping) stopping = timed ? shared.stop.load(std::memory_order_acquire) : p.posted >= p.quota;
				if (stopping) continue;
				s.busy.store(true, std::memory_order_relaxed);
				loop.post_to(target, s);
				if (++target == workers) target = 0;
				++p.posted;
				posted_now = true;
			}
			// A scan that posted nothing and met no running slot has seen every slot free.
			if (stopping && in_flight == 0 && !posted_now) break;
			if (!posted_now) std::this_thread::yield();
		}
		if (timed) p.finished = Clock::now();
	}

	/// One worker thread. A throw ends the window: the producers would wait for it forever.
	template <class Loop>
	void work(Loop& loop, Shared& shared, unsigned w, std::string& error)
	{
		try
		{
			loop.run(w);
		}
		catch (const std::exception& e)
		{
			error = e.what();
			shared.failed.store(true, std::memory_order_release);
		}
		catch (...)
		{
			error = "non-standard exception";
			shared.failed.store(true, std::memory_order_release);
		}
	}

	/// Waits until every producer is at the gate and every worker has made a pass.
	template <class Loop>
	void wait_until_up(const Loop& loop, const Shared& shared, unsigned producers)
	{
		for (;;)
		{
			if (shared.failed.load(std::memory_order_acquire)) return;
			bool up = shared.ready.load(std::memory_order_acquire) == producers;
			for (unsigned w = 0; up && w < loop.workers(); ++w) up = loop.passes(w) > 0;
			if (up) return;
			std::this_thread::yield();
		}
	}

	/// Pins the calling thread to cpus[i] when CPUs were given. A failure ends the window: its
	/// result would come from a thread off its CPU.
	void pin_thread(const std::vector<unsigned>& cpus, unsigned i, Shared& shared, std::string& error, const char* role)
	{
		if (cpus.empty()) return;
		const int rc = wakeprobe::pin_current_thread(cpus[i]);
		if (rc == 0) return;
		error = std::string("cannot pin ") + role + " " + std::to_string(i) + " to CPU " + std::to_string(cpus[i]) +
		        ": error " + std::to_string(rc);
		shared.failed.store(true, std::memory_order_release);
	}

	/// Starts the threads of a window. A thread that cannot start sets `failed` and stops the
	/// rest of the starts. Each thread pins itself first (pin_errors: workers, then producers).
	template <class Loop>
	void start_threads(Loop& loop, Shared& shared, const Options& opt, std::vector<Producer>& producers,
	                   std::vector<std::string>& errors, std::vector<std::string>& pin_errors,
	                   std::vector<std::thread>& worker_threads, std::vector<std::thread>& producer_threads, std::string& error)
	{
		const bool timed = opt.timed();
		try
		{
			for (unsigned w = 0; w < opt.workers; ++w)
			{
				worker_threads.emplace_back([&loop, &shared, &opt, &errors, &pin_errors, w] {
					pin_thread(opt.worker_cpus, w, shared, pin_errors[w], "worker");
					work(loop, shared, w, errors[w]);
				});
			}
			for (unsigned p = 0; p < opt.producers; ++p)
			{
				producer_threads.emplace_back([&loop, &shared, &opt, &producers, &pin_errors, p, timed, first = p % opt.workers] {
					pin_thread(opt.producer_cpus, p, shared, pin_errors[opt.workers + p], "producer");
					produce(loop, shared, producers[p], first, timed);
				});
			}
		}
		catch (const std::exception& e)
		{
			error = std::string("cannot start a thread: ") + e.what();
			shared.failed.store(true, std::memory_order_release);
		}
	}

	/// One window: a fresh loop of the arm's type in poll mode, W workers and P producers.
	template <class Loop>
	WindowResult run_window(const Options& opt)
	{
		WindowResult res;
		const bool timed = opt.timed();

		// The task storage comes first: the tasks must outlive the loop.
		std::vector<Producer> producers;
		producers.reserve(opt.producers);
		for (unsigned p = 0; p < opt.producers; ++p) producers.emplace_back(opt.inflight, quota_of(opt, p), opt.work_ns);
		Shared shared;
		std::vector<std::string> errors(opt.workers);
		std::vector<std::string> pin_errors(opt.workers + opt.producers);

		std::unique_ptr<Loop> loop;
		try
		{
			loop = std::make_unique<Loop>(wakeloop::Config{.wait_us = 0, .workers = opt.workers, .poll = true});
		}
		catch (const std::exception& e)
		{
			res.error = std::string("cannot create the ") + Loop::name + " loop: " + e.what();
			return res;
		}

		std::vector<std::thread> worker_threads;
		std::vector<std::thread> producer_threads;
		worker_threads.reserve(opt.workers);
		producer_threads.reserve(opt.producers);
		start_threads(*loop, shared, opt, producers, errors, pin_errors, worker_threads, producer_threads, res.error);
		wait_until_up(*loop, shared, opt.producers);

		Clock::time_point start{};
		if (timed) start = Clock::now();
		shared.go.store(true, std::memory_order_release);
		if (timed && !shared.failed.load(std::memory_order_acquire))
		{
			std::this_thread::sleep_for(std::chrono::milliseconds(opt.window_ms));
			shared.stop.store(true, std::memory_order_release);
		}
		// The producers leave once every task they posted has run; only then stop the workers.
		for (auto& t : producer_threads) t.join();
		loop->stop();
		for (auto& t : worker_threads) t.join();

		for (const auto& e : errors)
		{
			if (!e.empty()) res.error += (res.error.empty() ? "" : "; ") + std::string("a worker threw: ") + e;
		}
		for (const auto& e : pin_errors)
		{
			if (!e.empty()) res.error += (res.error.empty() ? "" : "; ") + e;
		}
		Clock::time_point last = start;
		for (const Producer& p : producers)
		{
			std::uint64_t done = 0;
			for (const Slot& s : p.slots) done += s.completed;
			res.producer_posted.push_back(p.posted);
			res.producer_completed.push_back(done);
			res.posted += p.posted;
			res.tasks += done;
			if (timed) last = std::max(last, p.finished);
		}
		if (timed) res.elapsed_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(last - start).count();
		for (unsigned w = 0; w < opt.workers; ++w) res.passes.push_back(loop->passes(w));
		res.wait_method = loop->wait_method();
		return res;
	}

	// ----------------------------------------------------------------------------------------
	// Pairs and output
	// ----------------------------------------------------------------------------------------

	/// Per pair, true when it runs BA and false when it runs AB. Shuffle draws from the raw
	/// output of std::mt19937_64, whose sequence the standard fixes, and not from a
	/// distribution, whose algorithm it leaves to the library: the same seed gives the same
	/// order on every platform.
	std::vector<bool> make_order(const Options& o)
	{
		std::vector<bool> ba;
		std::mt19937_64 rng(o.seed);
		for (unsigned k = 0; k < o.pairs; ++k) ba.push_back(o.shuffle ? (rng() >> 63) != 0 : k % 2 == 1);
		return ba;
	}

	/// A window that has run.
	struct Done
	{
		unsigned pair = 0;
		unsigned position = 0;
		bool side_b = false;
		WindowResult result;
	};

	std::string window_line(const Options& o, const Done& d)
	{
		std::ostringstream s;
		s << "{\"pair\":" << d.pair << ",\"position\":" << d.position << ",\"side\":\"" << (d.side_b ? 'B' : 'A')
		  << "\",\"arm\":\"" << json_escape(o.arm(d.side_b)) << "\",\"ran\":\"" << json_escape(d.result.ran)
		  << "\",\"tasks\":" << d.result.tasks;
		// Not in --check: in poll mode a pass count grows with elapsed time, so the two arms' counts
		// over the same task count would compare their throughput on a development machine.
		if (!o.check)
		{
			s << ",\"passes\":[";
			for (std::size_t i = 0; i < d.result.passes.size(); ++i) s << (i ? "," : "") << d.result.passes[i];
			s << "]";
		}
		if (d.result.elapsed_ns)
		{
			const double seconds = static_cast<double>(*d.result.elapsed_ns) * 1e-9;
			char rate[48];
			std::snprintf(rate, sizeof(rate), "%.3f", seconds > 0 ? static_cast<double>(d.result.tasks) / seconds : 0.0);
			s << ",\"elapsed_ns\":" << *d.result.elapsed_ns << ",\"tasks_per_s\":" << rate;
		}
		s << "}";
		return s.str();
	}

	/// "AB" or "BA" for each pair that has both windows.
	std::string sequence_json(const std::vector<Done>& done)
	{
		std::string s = "[";
		for (std::size_t i = 0; i + 1 < done.size(); i += 2)
		{
			s += std::string(i ? ",\"" : "\"") + (done[i].side_b ? "BA" : "AB") + "\"";
		}
		return s + "]";
	}

	std::string summary_line(const Options& o, const std::vector<Done>& done, const std::string& wait_method,
	                         const std::string& started_utc, const std::string& error, const std::vector<std::string>& failures)
	{
		std::ostringstream s;
		s << "{\"summary\":true,\"program\":\"abpair\",\"check\":" << (o.check ? "true" : "false") << ",\"backend\":\""
		  << json_escape(o.backend) << "\",\"arm_a\":\"" << json_escape(o.arm_a) << "\",\"arm_b\":\"" << json_escape(o.arm_b)
		  << "\",\"workers\":" << o.workers << ",\"producers\":" << o.producers << ",\"inflight\":" << o.inflight
		  << ",\"pairs\":" << o.pairs << ",\"order\":\"" << (o.shuffle ? "shuffle" : "abba") << "\",\"seed\":"
		  << (o.shuffle ? std::to_string(o.seed) : std::string("null")) << ",\"mode\":\"" << (o.timed() ? "timed" : "count")
		  << "\",\"window_ms\":" << (o.timed() ? std::to_string(o.window_ms) : std::string("null"))
		  << ",\"window_tasks\":" << (o.timed() ? std::string("null") : std::to_string(o.window_tasks))
		  << ",\"work_ns\":" << o.work_ns << ",\"worker_cpus\":" << benchutil::cpu_list_json(o.worker_cpus)
		  << ",\"producer_cpus\":" << benchutil::cpu_list_json(o.producer_cpus) << ",\"wait\":\"poll\",\"windows\":" << done.size() << ",\"sequence\":" << sequence_json(done)
		  << ",\"wait_method\":\"" << json_escape(wait_method) << "\",\"check_passed\":"
		  << (o.check ? (failures.empty() && error.empty() ? "true" : "false") : "null") << ",\"compiler\":\""
		  << json_escape(wakeprobe::compiler_name()) << "\",\"build_type\":\"" << ABPAIR_BUILD_TYPE << "\",\"bench_commit\":\""
		  << ABPAIR_BENCH_COMMIT << "\",\"bench_dirty\":" << (ABPAIR_BENCH_DIRTY ? "true" : "false")
		  << ",\"bench_code_commit\":\"" << ABPAIR_BENCH_CODE_COMMIT << "\",\"host\":\""
		  << json_escape(wakeprobe::host_name()) << "\",\"started_utc\":"
		  << (started_utc.empty() ? std::string("null") : "\"" + started_utc + "\"") << ",\"clock\":"
		  << (o.check ? "null" : "\"steady_clock\"") << ",\"error\":"
		  << (error.empty() ? std::string("null") : "\"" + json_escape(error) + "\"") << "}";
		return s.str();
	}

	// ----------------------------------------------------------------------------------------
	// The self-test
	// ----------------------------------------------------------------------------------------

	/// What is wrong with a finished --check run, one line each; empty when it is sound. The
	/// sides are A and B whatever the arms are named, so A against A is a valid design.
	std::vector<std::string> verify(const Options& o, const std::vector<Done>& done)
	{
		std::vector<std::string> bad;
		if (done.size() != 2U * o.pairs)
		{
			bad.push_back("ran " + std::to_string(done.size()) + " windows, expected " + std::to_string(2U * o.pairs));
		}
		unsigned windows_a = 0;
		unsigned windows_b = 0;
		for (const Done& d : done)
		{
			++(d.side_b ? windows_b : windows_a);
			const std::string at = "pair " + std::to_string(d.pair) + " position " + std::to_string(d.position) + ": ";
			if (d.result.tasks != o.window_tasks)
			{
				bad.push_back(at + std::to_string(d.result.tasks) + " tasks ran, expected " + std::to_string(o.window_tasks));
			}
			if (d.result.posted != o.window_tasks)
			{
				bad.push_back(at + std::to_string(d.result.posted) + " tasks posted, expected " + std::to_string(o.window_tasks));
			}
			for (std::size_t p = 0; p < d.result.producer_posted.size(); ++p)
			{
				if (d.result.producer_posted[p] != d.result.producer_completed[p])
				{
					bad.push_back(at + "producer " + std::to_string(p) + " posted " + std::to_string(d.result.producer_posted[p]) +
					              " and completed " + std::to_string(d.result.producer_completed[p]));
				}
			}
		}
		if (windows_a != o.pairs) bad.push_back("side A ran " + std::to_string(windows_a) + " windows, expected " + std::to_string(o.pairs));
		if (windows_b != o.pairs) bad.push_back("side B ran " + std::to_string(windows_b) + " windows, expected " + std::to_string(o.pairs));

		// The variant that ran must be the arm's. Then the order: abba is written out here
		// independently of make_order(); shuffle can only be rebuilt from the same generator.
		for (const Done& d : done)
		{
			if (d.result.ran != o.arm(d.side_b))
			{
				bad.push_back("pair " + std::to_string(d.pair) + ": side " + (d.side_b ? "B" : "A") + " ran " + d.result.ran +
				              ", not " + o.arm(d.side_b));
			}
		}
		const std::vector<bool> ba = make_order(o);
		for (std::size_t i = 0; i < done.size() && i / 2 < ba.size(); ++i)
		{
			const bool pair_is_ba = o.shuffle ? ba[i / 2] : (i / 2) % 2 == 1;
			const bool want_b = (i % 2 == 1) != pair_is_ba;
			if (done[i].pair != i / 2 || done[i].position != i % 2 || done[i].side_b != want_b)
			{
				bad.push_back("window " + std::to_string(i) + " is pair " + std::to_string(done[i].pair) + " position " +
				              std::to_string(done[i].position) + " on side " + (done[i].side_b ? "B" : "A") +
				              ", which is not the requested order");
			}
		}
		return bad;
	}

	// ----------------------------------------------------------------------------------------
	// The run
	// ----------------------------------------------------------------------------------------

	/// Where the lines go. Window lines go to the output file, if there is one, and in --check
	/// also to stdout, where a run may have no file. The summary goes to both.
	class Output
	{
	public:
		explicit Output(const Options& o) : file_wanted_(!o.out.empty()), window_to_stdout_(o.check)
		{
			if (file_wanted_) file_.open(o.out, std::ios::trunc);
		}
		bool ok() const { return !file_wanted_ || static_cast<bool>(file_); }
		void window(const std::string& line) { write(line, window_to_stdout_); }
		void summary(const std::string& line) { write(line, true); }

	private:
		void write(const std::string& line, bool also_stdout)
		{
			if (file_wanted_) file_ << line << '\n' << std::flush;
			if (also_stdout) std::cout << line << std::endl;
		}
		bool file_wanted_;
		bool window_to_stdout_;
		std::ofstream file_;
	};

	/// Runs one window of the arm's variant. False when this platform has no such loop.
	bool run_one(const Options& opt, Done& d)
	{
		return wakeloop::with_loop(opt.backend, opt.defect(d.side_b), [&]<class L>() {
			d.result = run_window<L>(opt);
			d.result.ran = std::string(wakeloop::defect_name(L::defect));
		});
	}

	int run_all(const Options& opt)
	{
		Output out(opt);
		if (!out.ok())
		{
			std::cerr << "abpair: cannot write " << opt.out << "\n";
			return 5;
		}
		// No clock reading in --check, not even the wall clock.
		const std::string started_utc = opt.check ? std::string() : wakeprobe::utc_now();

		const std::vector<bool> ba = make_order(opt);
		std::vector<Done> done;
		std::string error;
		std::string wait_method;
		for (unsigned k = 0; k < opt.pairs && error.empty(); ++k)
		{
			for (unsigned position = 0; position < 2 && error.empty(); ++position)
			{
				Done d;
				d.pair = k;
				d.position = position;
				d.side_b = (position == 1) != ba[k];
				if (!run_one(opt, d))
				{
					error = "no loop for backend " + opt.backend + " and variant " + opt.arm(d.side_b);
					break;
				}
				if (!d.result.error.empty())
				{
					error = d.result.error;
					break;
				}
				out.window(window_line(opt, d));
				wait_method = d.result.wait_method;
				done.push_back(std::move(d));
			}
		}

		const std::vector<std::string> failures = opt.check && error.empty() ? verify(opt, done) : std::vector<std::string>{};
		out.summary(summary_line(opt, done, wait_method, started_utc, error, failures));
		if (!error.empty())
		{
			std::cerr << "abpair: " << error << "\n";
			return 3;
		}
		if (!failures.empty())
		{
			for (const std::string& f : failures) std::cerr << "FAIL: abpair --check " << opt.backend << ": " << f << "\n";
			return 1;
		}
		if (!out.ok()) return 5;
		if (opt.check)
		{
			std::cout << "PASS: abpair --check " << opt.backend << ": " << opt.arm_a << " against " << opt.arm_b << ", " << done.size()
			          << " windows of " << opt.window_tasks << " tasks\n";
		}
		return 0;
	}

}  // namespace

int main(int argc, char** argv)
{
	return run_all(parse_args(argc, argv));
}
