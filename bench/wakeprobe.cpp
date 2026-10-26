// wakeprobe: post-to-execution delay on an otherwise idle wakeloop.
//
// The workers park in their backend's bounded wait. The main thread posts one task at a
// time: steady_clock just before post_to(), steady_clock again when the task runs, then it
// waits for delivery, sleeps the gap and posts again. The backend and the variant (the fixed
// loop or one of the six defects) are chosen at run time; so is the worker count W.
//
// Four variants delay a single post: absent wake, unobserved primitive, wrong worker (from
// W = 2) and no re-trigger (every post after the worker's first wake). Nothing wakes the
// worker for such a post, so it runs at the worker's next timeout wake. The period of those
// wakes, B_eff, is measured first with calibration posts sent B/10 after each delivery: each
// lands just after a wake and runs at the next, so consecutive deliveries are one period
// apart. It is not derived from the measured posts, because then the prediction below would
// match them by construction. Lazy-wake race and undrained edge do not delay a single post;
// they run the plain protocol, and what they show is a measured property.
//
// Each post records phase = its post time minus the last wake of the worker it went to (that
// worker's previous delivery; for wrong worker also the latest post to the worker before it,
// whose misdirected wake restarted this worker's wait), and for a delaying variant
// predicted = B_eff - (phase mod B_eff) and residual = delay - predicted.
//
// With W > 1 the probe and the calibration posts go to worker 0, so that B_eff is one
// worker's period; the warmup and measured posts go round robin over the workers.
//
// The gap is fixed (--gap-us), a target phase (--phase-frac, W = 1 only), or drawn afresh
// before each post from a seeded uniform distribution over a range of multiples of B
// (--gap-frac-range), so that the phases of successive posts are spread over the wake period.
//
// Output: one JSON object per post, then one summary object (also printed to stdout).

#include "wakeloop/variants.hpp"

#include "bench_common.hpp"
#include "probe_platform.hpp"

#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <exception>
#include <fstream>
#include <future>
#include <iostream>
#include <memory>
#include <optional>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

// Injected by bench/CMakeLists.txt.
#ifndef WAKEPROBE_BENCH_COMMIT
#define WAKEPROBE_BENCH_COMMIT "unknown"
#endif
#ifndef WAKEPROBE_BENCH_DIRTY
#define WAKEPROBE_BENCH_DIRTY 1
#endif
#ifndef WAKEPROBE_BENCH_CODE_COMMIT
#define WAKEPROBE_BENCH_CODE_COMMIT "unknown"
#endif
#ifndef WAKEPROBE_BUILD_TYPE
#define WAKEPROBE_BUILD_TYPE "unknown"
#endif

namespace
{

	using Clock = std::chrono::steady_clock;

	constexpr auto kSettle = std::chrono::milliseconds(50);
	// A post not delivered by then is recorded as lost and the run stops.
	constexpr auto kHardLimit = std::chrono::seconds(10);
	// Timeout used when the bound is 0 (blocking).
	constexpr std::int64_t kUnboundedTimeoutUs = 5'000'000;

	using benchutil::json_escape;

	// The largest worker count the programs accept; the wake tests stop at the same number.
	constexpr unsigned kMaxWorkers = 64;

	struct Options
	{
		std::string backend;
		std::string variant = "none";
		wakeloop::Defect defect = wakeloop::Defect::none;  // the parsed variant
		unsigned workers = 1;
		std::int64_t wait_us = -1;
		std::int64_t gap_us = 0;
		int posts = 51;
		int warmup = 5;
		int calibrate = 11;
		std::string out;
		std::string label;
		std::string design = "adhoc";
		long long timerslack_ns = wakeprobe::kHaveTimerSlack ? 1 : 0;
		unsigned timer_period_ms = 0;
		double phase_frac = -1;  // > 0: post at this fraction of B_eff after each delivery
		// > 0: each gap drawn uniformly from [gap_lo_frac x B, gap_hi_frac x B] with gap_seed
		double gap_lo_frac = -1;
		double gap_hi_frac = -1;
		std::uint64_t gap_seed = 0;
		int pm_qos_us = -1;  // >= 0: hold a /dev/cpu_dma_latency request of this many us

		bool random_gap() const { return gap_lo_frac > 0; }
	};

	enum class Kind
	{
		Probe,
		Calib,
		Warmup,
		Measured
	};

	const char* kind_name(Kind k)
	{
		switch (k)
		{
			case Kind::Probe: return "probe";
			case Kind::Calib: return "calib";
			case Kind::Warmup: return "warmup";
			case Kind::Measured: return "measured";
		}
		return "?";
	}

	struct Record
	{
		int index = 0;
		Kind kind = Kind::Measured;
		unsigned worker = 0;  // the worker the post went to
		std::int64_t post_ns = 0;  // since the run epoch
		std::optional<std::int64_t> exec_ns;
		bool timed_out = false;
		std::optional<std::int64_t> phase_ns;
		std::optional<std::int64_t> predicted_ns;
		std::optional<std::int64_t> residual_ns;
		std::optional<std::int64_t> gap_drawn_ns;  // random gap slept before this post

		std::optional<std::int64_t> delay_ns() const
		{
			return exec_ns ? std::optional<std::int64_t>(*exec_ns - post_ns) : std::nullopt;
		}
	};

	struct Calibration
	{
		std::optional<std::int64_t> b_eff_ns;
		int intervals = 0;
		int dropped = 0;
		std::optional<std::int64_t> p10_ns;
		std::optional<std::int64_t> p90_ns;
	};

	struct RunInfo
	{
		std::string backend;
		std::string started_utc;
		std::int64_t timeout_us = 0;
		Calibration calib;
		std::optional<int> slack_set_rc;  // Linux timer slack
		long long slack_main = -1;
		std::atomic<long long> slack_worker{-1};
		long long timer_res_start = -1;  // Windows timer resolution, 100ns
		long long timer_res_end = -1;
		std::vector<long long> timer_res_gap;
		std::string gap_method;
		int gap_set_failures = 0;
		int gap_wait_failures = 0;
		bool pm_qos_ok = false;  // Linux PM QoS request
		int pm_qos_errno = 0;
		long long pm_qos_readback = -1;
		long long run_uid = -1;
		wakeloop::Defect defect = wakeloop::Defect::none;
		bool delaying = false;  // the variant delays a single post (delays_single_posts)
		std::string wait_method;
		std::uint64_t loop_passes = 0;  // loop passes (wait returns and skipped waits), summed over the workers
		std::vector<std::uint64_t> worker_passes;
		std::string error;
	};

	/// True when a post to a parked worker is not woken, so it runs at the worker's next
	/// timeout wake. No re-trigger counts from each worker's second post: its one-shot
	/// registration still serves the first. Lazy-wake race and undrained edge leave single
	/// posts alone; that is a measured property, not an error.
	constexpr bool delays_single_posts(wakeloop::Defect d, unsigned workers) noexcept
	{
		switch (d)
		{
			case wakeloop::Defect::absent_wake:
			case wakeloop::Defect::unobserved_primitive:
			case wakeloop::Defect::no_retrigger: return true;
			case wakeloop::Defect::wrong_worker: return workers >= 2;
			case wakeloop::Defect::none:
			case wakeloop::Defect::plain_wake:
			case wakeloop::Defect::lazy_wake_race:
			case wakeloop::Defect::lazy_wake_wide:
			case wakeloop::Defect::undrained_edge: return false;
		}
		return false;
	}

	[[noreturn]] void usage(const std::string& why)
	{
		std::cerr << "wakeprobe: " << why << "\n"
		          << "usage: wakeprobe --backend " << benchutil::backend_list() << " --wait-us N --gap-us G --out FILE\n"
		          << "       [--variant " << benchutil::variant_list() << "] (default none)\n"
		          << "       [--workers W] (1 to " << kMaxWorkers << ", default 1; wrong_worker needs 2)\n"
		          << "       [--posts N] [--warmup K] [--calibrate C] [--label TEXT] [--design NAME]\n"
		          << "       --wait-us is the bound B in us; 0 blocks\n"
		          << "       [--timerslack-ns N]   Linux: set before any thread starts; 0 = leave default (default 1)\n"
		          << "       [--timer-period-ms N] Windows: timeBeginPeriod(N); 0 = leave default (default 0)\n"
		          << "       [--phase-frac F]      instead of --gap-us: post F x B_eff after each delivery (a variant that\n"
		          << "                             delays single posts, W = 1)\n"
		          << "       [--gap-frac-range LO:HI] instead of --gap-us: each gap drawn uniformly from\n"
		          << "                             [LO x B, HI x B] (needs B > 0); [--gap-seed N] seeds it (default 0)\n"
		          << "       [--pm-qos-us N]       Linux, needs root: hold a CPU latency request of N us for the run;\n"
		          << "                             under sudo the run then drops back to the invoking user\n"
		          << "  --posts measured posts (default 51); --warmup posts before them (default 5);\n"
		          << "  --calibrate posts to worker 0, B/10 after each delivery, that measure B_eff for a variant that\n"
		          << "              delays single posts (default 11, 0 = none)\n";
		std::exit(2);
	}

	template <typename T>
	T parse_int(std::string_view flag, std::string_view text)
	{
		T value{};
		const auto* end = text.data() + text.size();
		const auto [ptr, ec] = std::from_chars(text.data(), end, value);
		if (ec != std::errc{} || ptr != end)
		{
			usage(std::string(flag) + " expects an integer, got '" + std::string(text) + "'");
		}
		return value;
	}

	// A number in (0, 100), as the gap fractions take.
	double parse_frac(std::string_view flag, std::string_view text)
	{
		const std::string s(text);
		char* end = nullptr;
		const double v = std::strtod(s.c_str(), &end);
		if (end != s.c_str() + s.size() || !(v > 0 && v < 100))
		{
			usage(std::string(flag) + " expects a number in (0, 100), got '" + s + "'");
		}
		return v;
	}

	Options parse_args(int argc, char** argv)
	{
		Options o;
		bool have_wait = false;
		bool have_gap = false;
		for (int i = 1; i < argc; ++i)
		{
			const std::string_view flag = argv[i];
			if (i + 1 >= argc) usage("missing value for " + std::string(flag));
			const std::string_view value = argv[++i];
			if (flag == "--backend") o.backend = value;
			else if (flag == "--variant") o.variant = value;
			else if (flag == "--workers") o.workers = parse_int<unsigned>(flag, value);
			else if (flag == "--wait-us") { o.wait_us = parse_int<std::int64_t>(flag, value); have_wait = true; }
			else if (flag == "--gap-us") { o.gap_us = parse_int<std::int64_t>(flag, value); have_gap = true; }
			else if (flag == "--posts") o.posts = parse_int<int>(flag, value);
			else if (flag == "--warmup") o.warmup = parse_int<int>(flag, value);
			else if (flag == "--calibrate") o.calibrate = parse_int<int>(flag, value);
			else if (flag == "--out") o.out = value;
			else if (flag == "--label") o.label = value;
			else if (flag == "--design") o.design = value;
			else if (flag == "--timerslack-ns")
			{
				if (!wakeprobe::kHaveTimerSlack) usage("--timerslack-ns exists only on Linux");
				o.timerslack_ns = parse_int<long long>(flag, value);
			}
			else if (flag == "--timer-period-ms")
			{
				if (!wakeprobe::kHaveTimerPeriod) usage("--timer-period-ms exists only on Windows");
				o.timer_period_ms = parse_int<unsigned>(flag, value);
			}
			else if (flag == "--phase-frac") o.phase_frac = parse_frac(flag, value);
			else if (flag == "--gap-frac-range")
			{
				const auto colon = value.find(':');
				if (colon == std::string_view::npos) usage("--gap-frac-range expects LO:HI");
				o.gap_lo_frac = parse_frac(flag, value.substr(0, colon));
				o.gap_hi_frac = parse_frac(flag, value.substr(colon + 1));
				if (!(o.gap_lo_frac < o.gap_hi_frac)) usage("--gap-frac-range needs LO < HI");
			}
			else if (flag == "--gap-seed") o.gap_seed = parse_int<std::uint64_t>(flag, value);
			else if (flag == "--pm-qos-us")
			{
				if (!wakeprobe::kHaveTimerSlack) usage("--pm-qos-us exists only on Linux");
				o.pm_qos_us = parse_int<int>(flag, value);
				if (o.pm_qos_us < 0) usage("--pm-qos-us must be >= 0");
			}
			else usage("unknown flag " + std::string(flag));
		}
		if (static_cast<int>(have_gap) + static_cast<int>(o.phase_frac > 0) + static_cast<int>(o.random_gap()) != 1)
		{
			usage("give exactly one of --gap-us, --phase-frac and --gap-frac-range");
		}
		const auto defect = wakeloop::parse_defect(o.variant);
		if (!defect) usage("--variant must be one of " + benchutil::variant_list());
		o.defect = *defect;
		if (!wakeloop::known_backend(o.backend)) usage("--backend must be one of " + benchutil::backend_list());
		if (!wakeloop::has_site(o.backend, o.defect))
		{
			usage("the variant " + o.variant + " has no site on the " + o.backend + " backend");
		}
		if (o.workers < 1 || o.workers > kMaxWorkers)
		{
			usage("--workers must be a whole number from 1 to " + std::to_string(kMaxWorkers));
		}
		if (o.workers < wakeloop::min_workers(o.defect))
		{
			usage("the variant " + o.variant + " needs --workers >= " + std::to_string(wakeloop::min_workers(o.defect)));
		}
		if (!have_wait || o.out.empty()) usage("--backend, --wait-us and --out are required");
		if (o.wait_us < 0) usage("--wait-us must be >= 0 (0 blocks)");
		if (o.random_gap() && o.wait_us <= 0) usage("--gap-frac-range needs a bound (--wait-us > 0)");
		if (o.posts < 1 || o.warmup < 0 || o.calibrate < 0 || o.gap_us < 0 || o.timerslack_ns < 0)
		{
			usage("need posts >= 1 and warmup, calibrate, gap, timerslack >= 0");
		}
		if (o.phase_frac > 0 && o.workers > 1)
		{
			usage("--phase-frac holds a phase against one worker's wake schedule; with --workers > 1 use --gap-us or --gap-frac-range");
		}
		if (o.defect == wakeloop::Defect::no_retrigger && o.workers > 1 && o.warmup < static_cast<int>(o.workers))
		{
			// Each worker's first post uses its one working wake and is fast; the warmup spends it.
			usage("no_retrigger with --workers > 1 needs --warmup >= --workers, so that every worker's first wake is spent "
			      "before the measured posts");
		}
		return o;
	}

	// Nearest-rank percentile of a sorted, non-empty vector (p in 1..100).
	std::int64_t nearest_rank(const std::vector<std::int64_t>& sorted, int p)
	{
		const std::size_t n = sorted.size();
		std::size_t rank = (static_cast<std::size_t>(p) * n + 99) / 100;
		rank = std::clamp<std::size_t>(rank, 1, n);
		return sorted[rank - 1];
	}

	/// One post's task. The nodes live in a deque owned by the run, which outlives the loop's
	/// worker, so a post that is never delivered stays valid while it is still queued.
	struct PostNode : wakeloop::Task
	{
		PostNode() { run = &PostNode::on_run; }
		static void on_run(wakeloop::Task& t) noexcept
		{
			auto& n = static_cast<PostNode&>(t);
			const auto now = Clock::now();
			if (n.slack_out != nullptr) n.slack_out->store(wakeprobe::read_timer_slack());
			n.done.set_value(now);
		}
		std::promise<Clock::time_point> done;
		std::atomic<long long>* slack_out = nullptr;  // the probe post reads the worker's timer slack
	};

	// Runs worker w of the loop on a thread of its own, for every w; stops and joins them on scope
	// exit, so an exception in the measurement cannot reach std::thread's destructor and lose the
	// records collected so far.
	template <class Loop>
	class LoopRunner
	{
	public:
		explicit LoopRunner(Loop& loop) : loop_(loop), errors_(loop.workers())
		{
			threads_.reserve(loop.workers());
			try
			{
				for (unsigned w = 0; w < loop.workers(); ++w) threads_.emplace_back([this, w] { work(w); });
			}
			catch (...)
			{
				stop_and_join();
				throw;
			}
		}
		~LoopRunner() { stop_and_join(); }
		LoopRunner(const LoopRunner&) = delete;
		LoopRunner& operator=(const LoopRunner&) = delete;

		void stop_and_join()
		{
			loop_.stop();
			for (auto& t : threads_)
			{
				if (t.joinable()) t.join();
			}
		}
		// Valid after stop_and_join().
		bool failed() const
		{
			for (const auto& e : errors_)
			{
				if (e != nullptr) return true;
			}
			return false;
		}

	private:
		void work(unsigned w)
		{
			try
			{
				loop_.run(w);
			}
			catch (...)
			{
				errors_[w] = std::current_exception();  // each worker writes its own element
			}
		}

		Loop& loop_;
		std::vector<std::exception_ptr> errors_;
		std::vector<std::thread> threads_;
	};

	// One post to `worker`: returns when the task ran or the hard limit passed.
	template <class Loop>
	Record measure_one(Loop& loop, std::deque<PostNode>& nodes, Kind kind, int index, unsigned worker,
	                   Clock::time_point epoch, std::chrono::microseconds timeout, std::atomic<long long>* slack_out = nullptr)
	{
		PostNode& node = nodes.emplace_back();
		node.slack_out = slack_out;
		auto ran = node.done.get_future();

		Record r;
		r.index = index;
		r.kind = kind;
		r.worker = worker;
		const auto posted = Clock::now();
		loop.post_to(worker, node);
		r.post_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(posted - epoch).count();

		if (ran.wait_until(posted + timeout) != std::future_status::ready)
		{
			r.timed_out = true;
			if (ran.wait_until(posted + kHardLimit) != std::future_status::ready)
			{
				return r;  // lost: exec_ns stays empty
			}
		}
		r.exec_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(ran.get() - epoch).count();
		return r;
	}

	// B_eff from the intervals between consecutive deliveries ending in a calibration post.
	// An interval below half the first median, or below half the bound, is dropped: the
	// post was picked up in the same pass, which a timed wake cannot produce. Long intervals
	// are kept; a wake period that varies must stay visible.
	Calibration calibrate(const std::vector<Record>& records, std::int64_t bound_us)
	{
		const std::int64_t floor_ns = bound_us > 0 ? bound_us * 500 : 0;
		std::vector<std::int64_t> intervals;
		for (std::size_t i = 1; i < records.size(); ++i)
		{
			const Record& r = records[i];
			const Record& prev = records[i - 1];
			if (r.kind == Kind::Calib && r.exec_ns && prev.exec_ns) intervals.push_back(*r.exec_ns - *prev.exec_ns);
		}
		Calibration c;
		c.intervals = static_cast<int>(intervals.size());
		if (intervals.empty()) return c;
		std::sort(intervals.begin(), intervals.end());
		const std::int64_t first = nearest_rank(intervals, 50);
		std::vector<std::int64_t> kept;
		for (const std::int64_t d : intervals)
		{
			if (2 * d >= first && d >= floor_ns) kept.push_back(d);
		}
		c.dropped = c.intervals - static_cast<int>(kept.size());
		if (kept.empty()) return c;
		c.b_eff_ns = nearest_rank(kept, 50);
		c.p10_ns = nearest_rank(kept, 10);
		c.p90_ns = nearest_rank(kept, 90);
		return c;
	}

	// Phase against the last wake of the post's worker: its previous delivery, which is the
	// previous record at W = 1. With a misdirected wake (wrong worker) the post to the worker
	// before it also woke this worker and restarted its wait, so that post's time counts when
	// it is later. Prediction and residual where B_eff is known.
	void annotate(std::vector<Record>& records, const std::optional<std::int64_t>& b_eff, wakeloop::Defect defect,
	              unsigned workers)
	{
		std::vector<std::optional<std::int64_t>> last_exec(workers);  // per worker, ns since the epoch
		std::vector<std::optional<std::int64_t>> last_post(workers);
		const bool misdirected = defect == wakeloop::Defect::wrong_worker && workers >= 2;
		for (Record& r : records)
		{
			std::optional<std::int64_t> anchor = last_exec[r.worker];
			if (misdirected)
			{
				const auto& woken = last_post[(r.worker + workers - 1) % workers];
				if (woken && (!anchor || *woken > *anchor)) anchor = woken;
			}
			if (anchor)
			{
				r.phase_ns = r.post_ns - *anchor;
				if (b_eff && *b_eff > 0 && *r.phase_ns >= 0)
				{
					r.predicted_ns = *b_eff - (*r.phase_ns % *b_eff);
					if (const auto d = r.delay_ns()) r.residual_ns = *d - *r.predicted_ns;
				}
			}
			last_post[r.worker] = r.post_ns;
			if (r.exec_ns) last_exec[r.worker] = *r.exec_ns;
		}
	}

	std::chrono::microseconds timeout_for(std::int64_t bound_us)
	{
		return std::chrono::microseconds(bound_us > 0 ? 5 * bound_us : kUnboundedTimeoutUs);
	}

	// Gaps drawn uniformly from [lo_us, hi_us]. Built on the raw output of std::mt19937_64,
	// whose sequence the standard fixes, and not on uniform_real_distribution, whose
	// algorithm it leaves to the library: the same seed gives the same gaps on every platform.
	class GapDraw
	{
	public:
		GapDraw(double lo_us, double hi_us, std::uint64_t seed) : lo_us_(lo_us), span_us_(hi_us - lo_us), rng_(seed) {}

		std::chrono::microseconds next()
		{
			constexpr double kUnit = 1.0 / 9007199254740992.0;  // 2^-53: 53 random bits in [0, 1)
			const double u = static_cast<double>(rng_() >> 11) * kUnit;
			return std::chrono::microseconds(static_cast<std::int64_t>(lo_us_ + span_us_ * u));
		}

	private:
		double lo_us_;
		double span_us_;
		std::mt19937_64 rng_;
	};

	// The post sequence: probe, calibration (a delaying variant only), warmup, measured. The probe
	// and the calibration posts go to worker 0; warmup and measured posts go round robin over
	// the workers. Records are appended as they are taken, so a throw leaves everything before
	// it in place.
	template <class Loop>
	void run_protocol(Loop& loop, std::deque<PostNode>& nodes, const Options& opt, RunInfo& info, bool delaying,
	                  wakeprobe::GapSleeper& gap, std::vector<Record>& records)
	{
		std::this_thread::sleep_for(kSettle);
		const auto epoch = Clock::now();
		int index = 0;

		// The probe reads the worker's timer slack, which shows whether it was inherited.
		records.push_back(measure_one(loop, nodes, Kind::Probe, index++, 0, epoch, timeout_for(opt.wait_us), &info.slack_worker));
		if (!records.back().exec_ns) return;

		if (delaying)
		{
			for (int k = 0; k < opt.calibrate; ++k)
			{
				// B/10 first, so the worker has re-parked: a post that lands while it is still
				// draining would run in the same pass. Still far inside one period.
				if (opt.wait_us > 0) gap.sleep(std::chrono::microseconds(opt.wait_us / 10));
				records.push_back(measure_one(loop, nodes, Kind::Calib, index++, 0, epoch, timeout_for(opt.wait_us)));
				if (!records.back().exec_ns) return;
			}
			info.calib = calibrate(records, opt.wait_us);
		}
		const std::int64_t b_eff_us = info.calib.b_eff_ns ? *info.calib.b_eff_ns / 1000 : 0;
		info.timeout_us = timeout_for(std::max<std::int64_t>(opt.wait_us, b_eff_us)).count();

		std::optional<std::chrono::nanoseconds> phase_target;
		if (opt.phase_frac > 0)
		{
			if (!info.calib.b_eff_ns) throw std::runtime_error("--phase-frac needs a measured B_eff");
			phase_target = std::chrono::nanoseconds(
				static_cast<std::int64_t>(opt.phase_frac * static_cast<double>(*info.calib.b_eff_ns)));
		}

		std::optional<GapDraw> draw;
		if (opt.random_gap())
		{
			draw.emplace(opt.gap_lo_frac * static_cast<double>(opt.wait_us), opt.gap_hi_frac * static_cast<double>(opt.wait_us),
			             opt.gap_seed);
		}
		std::optional<std::int64_t> drawn_ns;  // the random gap slept before the next post

		const int total = opt.warmup + opt.posts;
		for (int i = 0; i < total; ++i)
		{
			const Kind kind = i < opt.warmup ? Kind::Warmup : Kind::Measured;
			const unsigned worker = static_cast<unsigned>(i) % opt.workers;
			records.push_back(
				measure_one(loop, nodes, kind, index++, worker, epoch, std::chrono::microseconds(info.timeout_us)));
			records.back().gap_drawn_ns = drawn_ns;
			if (!records.back().exec_ns) return;
			if (i + 1 == total) break;
			// A fixed or random gap after the delivery was seen, or a target phase after the
			// delivery itself.
			auto g = std::chrono::microseconds(opt.gap_us);
			if (phase_target)
			{
				const auto due = epoch + std::chrono::nanoseconds(*records.back().exec_ns) + *phase_target;
				g = std::chrono::duration_cast<std::chrono::microseconds>(due - Clock::now());
			}
			else if (draw)
			{
				g = draw->next();
				drawn_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(g).count();
			}
			if (kind == Kind::Warmup && wakeprobe::kHaveTimerPeriod && g.count() > 1)
			{
				// Resolution sampled mid-gap; only on warmup gaps, so measured posts are untouched.
				gap.sleep(g / 2);
				info.timer_res_gap.push_back(wakeprobe::timer_resolution_100ns());
				gap.sleep(g - g / 2);
			}
			else
			{
				gap.sleep(g);
			}
		}
	}

	std::string num(const std::optional<std::int64_t>& v) { return v ? std::to_string(*v) : "null"; }

	std::string ratio(double v)
	{
		char buf[32];
		std::snprintf(buf, sizeof(buf), "%.6g", v);
		return buf;
	}

	std::string gap_json(const Options& o)
	{
		return o.phase_frac > 0 || o.random_gap() ? "null" : std::to_string(o.gap_us);
	}
	std::string frac_json(const Options& o) { return o.phase_frac > 0 ? ratio(o.phase_frac) : "null"; }
	std::string gap_range_json(const Options& o)
	{
		return o.random_gap() ? "[" + ratio(o.gap_lo_frac) + "," + ratio(o.gap_hi_frac) + "]" : "null";
	}
	std::string gap_seed_json(const Options& o) { return o.random_gap() ? std::to_string(o.gap_seed) : "null"; }

	std::string post_line(const Options& o, const std::string& backend, const Record& r)
	{
		std::ostringstream s;
		s << "{\"design\":\"" << json_escape(o.design) << "\",\"label\":\"" << json_escape(o.label)
		  << "\",\"backend\":\"" << backend << "\",\"wait_us\":" << o.wait_us << ",\"gap_us\":" << gap_json(o)
		  << ",\"phase_frac\":" << frac_json(o) << ",\"index\":" << r.index << ",\"worker\":" << r.worker
		  << ",\"kind\":\"" << kind_name(r.kind)
		  << "\",\"warmup\":" << (r.kind != Kind::Measured ? "true" : "false") << ",\"delay_ns\":" << num(r.delay_ns())
		  << ",\"post_ns\":" << r.post_ns << ",\"exec_ns\":" << num(r.exec_ns)
		  << ",\"timed_out\":" << (r.timed_out ? "true" : "false") << ",\"phase_ns\":" << num(r.phase_ns)
		  << ",\"predicted_ns\":" << num(r.predicted_ns) << ",\"residual_ns\":" << num(r.residual_ns)
		  << ",\"gap_drawn_ns\":" << num(r.gap_drawn_ns) << "}";
		return s.str();
	}

	// Statistics over the measured posts.
	struct Stats
	{
		std::vector<std::int64_t> delays;  // sorted
		std::vector<std::int64_t> residuals;  // sorted, signed
		std::vector<std::int64_t> abs_residuals;  // sorted
		int timeouts = 0;
		int lost = 0;
		int jumps = 0;  // |residual| > B_eff / 2
		int over_half_b = 0;  // delay > B / 2, the bound's own half (0 when B = 0)
	};

	Stats measured_stats(const std::vector<Record>& records, const std::optional<std::int64_t>& b_eff, std::int64_t bound_us)
	{
		Stats s;
		for (const auto& r : records)
		{
			if (r.kind != Kind::Measured) continue;
			if (r.timed_out) ++s.timeouts;
			const auto d = r.delay_ns();
			if (!d)
			{
				++s.lost;
				continue;
			}
			s.delays.push_back(*d);
			if (bound_us > 0 && 2 * *d > bound_us * 1000) ++s.over_half_b;
			if (r.residual_ns)
			{
				s.residuals.push_back(*r.residual_ns);
				s.abs_residuals.push_back(*r.residual_ns < 0 ? -*r.residual_ns : *r.residual_ns);
				if (b_eff && 2 * s.abs_residuals.back() > *b_eff) ++s.jumps;
			}
		}
		std::sort(s.delays.begin(), s.delays.end());
		std::sort(s.residuals.begin(), s.residuals.end());
		std::sort(s.abs_residuals.begin(), s.abs_residuals.end());
		return s;
	}

	std::string summary_line(const Options& o, const RunInfo& info, const std::vector<Record>& records,
	                         const wakeprobe::TimerPeriod& period)
	{
		const auto& b_eff = info.calib.b_eff_ns;
		const Stats st = measured_stats(records, b_eff, o.wait_us);
		auto pct = [](const std::vector<std::int64_t>& v, int p) {
			return v.empty() ? std::string("null") : std::to_string(nearest_rank(v, p));
		};

		std::ostringstream s;
		s << "{\"summary\":true,\"design\":\"" << json_escape(o.design) << "\",\"label\":\"" << json_escape(o.label)
		  << "\",\"backend\":\"" << info.backend << "\",\"backend_requested\":\"" << json_escape(o.backend)
		  << "\",\"wait_us\":" << o.wait_us << ",\"gap_us\":" << gap_json(o) << ",\"phase_frac\":" << frac_json(o)
		  << ",\"gap_frac_range\":" << gap_range_json(o) << ",\"gap_seed\":" << gap_seed_json(o)
		  << ",\"posts\":" << o.posts
		  << ",\"warmup\":" << o.warmup << ",\"calibrate\":" << o.calibrate << ",\"count\":" << st.delays.size()
		  << ",\"median_ns\":" << pct(st.delays, 50) << ",\"p10_ns\":" << pct(st.delays, 10)
		  << ",\"p90_ns\":" << pct(st.delays, 90)
		  << ",\"min_ns\":" << (st.delays.empty() ? "null" : std::to_string(st.delays.front()))
		  << ",\"max_ns\":" << (st.delays.empty() ? "null" : std::to_string(st.delays.back()))
		  << ",\"timeouts\":" << st.timeouts << ",\"lost\":" << st.lost << ",\"timeout_us\":" << info.timeout_us
		  << ",\"b_eff_ns\":" << num(b_eff) << ",\"calib_intervals\":" << info.calib.intervals
		  << ",\"calib_dropped\":" << info.calib.dropped << ",\"calib_p10_ns\":" << num(info.calib.p10_ns)
		  << ",\"calib_p90_ns\":" << num(info.calib.p90_ns) << ",\"residual_count\":" << st.residuals.size()
		  << ",\"median_residual_ns\":" << pct(st.residuals, 50)
		  << ",\"median_abs_residual_ns\":" << pct(st.abs_residuals, 50);
		if (b_eff && *b_eff > 0 && !st.residuals.empty())
		{
			const double be = static_cast<double>(*b_eff);
			s << ",\"median_residual_over_beff\":" << ratio(static_cast<double>(nearest_rank(st.residuals, 50)) / be)
			  << ",\"median_abs_residual_over_beff\":"
			  << ratio(static_cast<double>(nearest_rank(st.abs_residuals, 50)) / be);
		}
		else
		{
			s << ",\"median_residual_over_beff\":null,\"median_abs_residual_over_beff\":null";
		}
		s << ",\"residual_jumps\":" << st.jumps << ",\"count_over_half_b\":" << st.over_half_b
		  << ",\"percentile_method\":\"nearest-rank\"";

		if (wakeprobe::kHaveTimerSlack)
		{
			s << ",\"timerslack_ns_requested\":" << o.timerslack_ns
			  << ",\"timerslack_set_rc\":" << (info.slack_set_rc ? std::to_string(*info.slack_set_rc) : "null")
			  << ",\"timerslack_ns_main\":" << info.slack_main
			  << ",\"timerslack_ns_worker\":" << info.slack_worker.load();
			if (o.pm_qos_us >= 0)
			{
				s << ",\"pm_qos_us_requested\":" << o.pm_qos_us << ",\"pm_qos_ok\":" << (info.pm_qos_ok ? "true" : "false")
				  << ",\"pm_qos_errno\":" << info.pm_qos_errno << ",\"pm_qos_readback_us\":" << info.pm_qos_readback;
			}
			else
			{
				s << ",\"pm_qos_us_requested\":null,\"pm_qos_ok\":null,\"pm_qos_errno\":null,\"pm_qos_readback_us\":null";
			}
			s << ",\"run_uid\":" << info.run_uid;
		}
		else
		{
			s << ",\"timerslack_ns_requested\":null,\"timerslack_set_rc\":null,\"timerslack_ns_main\":null,"
			     "\"timerslack_ns_worker\":null,\"pm_qos_us_requested\":null,\"pm_qos_ok\":null,"
			     "\"pm_qos_errno\":null,\"pm_qos_readback_us\":null,\"run_uid\":null";
		}
		if (wakeprobe::kHaveTimerPeriod)
		{
			s << ",\"timer_period_ms_requested\":" << period.requested_ms() << ",\"timer_begin_result\":"
			  << (period.requested() ? std::to_string(period.begin_result()) : "null")
			  << ",\"timer_throttle_optout\":" << (period.requested() ? (period.optout_ok() ? "true" : "false") : "null");
		}
		else
		{
			s << ",\"timer_period_ms_requested\":null,\"timer_begin_result\":null,\"timer_throttle_optout\":null";
		}
		s << ",\"timer_resolution_100ns_start\":" << info.timer_res_start << ",\"timer_resolution_100ns_gap\":[";
		for (std::size_t i = 0; i < info.timer_res_gap.size(); ++i) s << (i ? "," : "") << info.timer_res_gap[i];
		s << "],\"timer_resolution_100ns_end\":" << info.timer_res_end;

		s << ",\"gap_sleep\":{\"method\":\"" << info.gap_method << "\",\"set_failures\":" << info.gap_set_failures
		  << ",\"wait_failures\":" << info.gap_wait_failures << "}"
		  << ",\"defect_mode\":\"" << wakeloop::defect_name(info.defect) << "\""
		  << ",\"defect_active\":" << (info.defect != wakeloop::Defect::none ? "true" : "false")
		  << ",\"variant\":\"" << wakeloop::defect_name(info.defect) << "\",\"workers\":" << o.workers
		  << ",\"delays_single_posts\":" << (info.delaying ? "true" : "false")
		  << ",\"wait_method\":\"" << json_escape(info.wait_method) << "\""
		  << ",\"loop_passes\":" << info.loop_passes << ",\"worker_passes\":[";
		for (std::size_t i = 0; i < info.worker_passes.size(); ++i) s << (i ? "," : "") << info.worker_passes[i];
		s << "],\"compiler\":\""
		  << json_escape(wakeprobe::compiler_name()) << "\",\"build_type\":\"" << WAKEPROBE_BUILD_TYPE
		  << "\",\"bench_commit\":\"" << WAKEPROBE_BENCH_COMMIT << "\",\"bench_dirty\":"
		  << (WAKEPROBE_BENCH_DIRTY ? "true" : "false") << ",\"bench_code_commit\":\"" << WAKEPROBE_BENCH_CODE_COMMIT << "\""
		  << ",\"host\":\"" << json_escape(wakeprobe::host_name()) << "\",\"started_utc\":\"" << info.started_utc
		  << "\",\"clock\":\"steady_clock\",\"error\":"
		  << (info.error.empty() ? std::string("null") : "\"" + json_escape(info.error) + "\"") << "}";
		return s.str();
	}

	// Everything from the loop's creation to the output, for one backend class.
	template <class Loop>
	int run_with(const Options& opt, RunInfo& info, const wakeprobe::TimerPeriod& period)
	{
		std::deque<PostNode> nodes;  // outlives the loop, which may still hold some of them
		std::unique_ptr<Loop> loop;
		try
		{
			loop = std::make_unique<Loop>(wakeloop::Config{.wait_us = opt.wait_us, .workers = opt.workers, .poll = false});
		}
		catch (const std::exception& e)
		{
			std::cerr << "wakeprobe: cannot create the " << Loop::name << " loop: " << e.what() << "\n";
			return 3;
		}
		info.backend = Loop::name;
		info.defect = Loop::defect;
		const bool delaying = delays_single_posts(Loop::defect, opt.workers);
		info.delaying = delaying;
		if (opt.wait_us == 0 && delaying)
		{
			std::cerr << "wakeprobe: a variant that delays single posts, with a blocking wait, never delivers a post\n";
			return 2;
		}
		if (opt.phase_frac > 0 && (!delaying || opt.calibrate < 1))
		{
			std::cerr << "wakeprobe: --phase-frac needs a variant that delays single posts and calibration posts\n";
			return 2;
		}

		std::ofstream out(opt.out, std::ios::trunc);
		if (!out)
		{
			std::cerr << "wakeprobe: cannot write " << opt.out << "\n";
			return 5;
		}

		std::vector<Record> records;
		wakeprobe::GapSleeper gap;
		{
			LoopRunner<Loop> runner(*loop);
			try
			{
				run_protocol(*loop, nodes, opt, info, delaying, gap, records);
			}
			catch (const std::exception& e)
			{
				info.error = std::string("measurement threw: ") + e.what();
			}
			catch (...)
			{
				info.error = "measurement threw a non-standard exception";
			}
			runner.stop_and_join();
			if (runner.failed()) info.error += (info.error.empty() ? "" : "; ") + std::string("the event loop threw");
		}
		info.wait_method = loop->wait_method();
		for (unsigned w = 0; w < opt.workers; ++w)
		{
			info.worker_passes.push_back(loop->passes(w));
			info.loop_passes += info.worker_passes.back();
		}
		info.timer_res_end = wakeprobe::timer_resolution_100ns();
		info.gap_method = gap.method();
		info.gap_set_failures = gap.set_failures();
		info.gap_wait_failures = gap.wait_failures();

		annotate(records, info.calib.b_eff_ns, Loop::defect, opt.workers);
		for (const auto& r : records) out << post_line(opt, info.backend, r) << "\n";
		const std::string summary = summary_line(opt, info, records, period);
		out << summary << "\n";
		out.flush();
		std::cout << summary << std::endl;

		if (!info.error.empty())
		{
			std::cerr << "wakeprobe: " << info.error << "\n";
			return 4;
		}
		if (!out) return 5;
		return (!records.empty() && records.back().exec_ns) ? 0 : 6;
	}

}  // namespace

int main(int argc, char** argv)
{
	const Options opt = parse_args(argc, argv);

	RunInfo info;
	info.started_utc = wakeprobe::utc_now();

	// Root-only setup first, then back to the invoking user. The request lives as long as
	// this object, which is the whole run.
	wakeprobe::PmQos pm_qos;
	if (opt.pm_qos_us >= 0)
	{
		info.pm_qos_ok = pm_qos.request(opt.pm_qos_us);
		info.pm_qos_errno = pm_qos.error();
		info.pm_qos_readback = pm_qos.readback();
		if (!info.pm_qos_ok)
		{
			std::cerr << "wakeprobe: cannot hold a PM QoS request of " << opt.pm_qos_us << " us (errno "
			          << info.pm_qos_errno << "); run it under sudo\n";
			return 3;
		}
	}
	info.run_uid = wakeprobe::drop_sudo_privileges();
	if (wakeprobe::kHaveTimerSlack && info.run_uid < 0)
	{
		std::cerr << "wakeprobe: could not drop back to the sudo user\n";
		return 3;
	}

	// Before any thread exists: a thread inherits the timer slack of the thread creating it,
	// and the loop's workers are the runner threads this thread starts later.
	if (wakeprobe::kHaveTimerSlack && opt.timerslack_ns > 0)
	{
		info.slack_set_rc = wakeprobe::set_timer_slack(static_cast<unsigned long>(opt.timerslack_ns));
	}
	info.slack_main = wakeprobe::read_timer_slack();
	const wakeprobe::TimerPeriod period(opt.timer_period_ms);
	info.timer_res_start = wakeprobe::timer_resolution_100ns();

	int rc = 3;
	const bool dispatched = wakeloop::with_loop(opt.backend, opt.defect, [&]<class L>() { rc = run_with<L>(opt, info, period); });
	if (!dispatched)
	{
		// parse_args checked the backend, the variant and the site; this is not a usage error.
		std::cerr << "wakeprobe: no loop for backend " << opt.backend << " and variant " << opt.variant << "\n";
		return 3;
	}
	return rc;
}
