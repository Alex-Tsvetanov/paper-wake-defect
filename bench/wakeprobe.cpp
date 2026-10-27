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
// Two protocols (--protocol). "single", the default, is the one described above: protocol S.
// "batch" is protocol Bt (W = 1, a bound): one measured unit is a round. A gate task holds the
// worker, kBatchTasks tasks (3 x the undrained-edge budget) are posted behind it, and the gate is
// released; the round's delay is the latest run time of those tasks minus the release time. Posts
// made behind a running task need no wake, so Bt is blind to a missing wake and sees a worker that
// leaves a batch undrained (tests/wake_tests.cpp, detect_latency_batch, is its model). The gap
// options apply as for posts, counted from the round's last run. --posts and --warmup count
// rounds. Batch runs no calibration.
//
// Output: one JSON object per post (per round in batch), then one summary object (also printed
// to stdout).
//
// --check is a self-test for a machine that times nothing: a few units of the chosen protocol,
// then a check that every unit was delivered within its timeout and every task ran exactly once.
// It prints counts and PASS or FAIL, no delay, time or rate, and writes no record file.
//
// --validate-args checks the command line of a measuring run and measures nothing. It parses every
// argument as a run does, applies the refusals a run makes before its first unit (parse_args, then
// plan_refusal; main calls both for a run and for --validate-args, so the two cannot drift), prints
// "valid" and exits 0, or prints the message the run would print and exits 2. It creates no loop,
// starts no thread, pins nothing, reads no clock, sets neither the timer slack nor the timer period
// and opens no file. --out may be given and is not opened; it may be left out too, because the
// campaign runner appends it. --check is another mode and is refused together with it.
// What it cannot check, because a run learns it only from the system or from its own units:
//   - that the backend's loop can be created (a run exits 3), that the PM QoS request and the drop
//     back to the sudo user succeed (3), and that --out can be opened (5);
//   - that each CPU of --worker-cpus and --poster-cpu exists, is allowed and takes the pin, and
//     that the workers start within 1 s (place_threads throws before the first unit; exit 4);
//   - that the calibration yields the B_eff that --phase-frac needs (run_protocol throws; exit 4).

#include "wakeloop/variants.hpp"

#include "bench_common.hpp"
#include "probe_platform.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <exception>
#include <fstream>
#include <future>
#include <iostream>
#include <memory>
#include <mutex>
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

	/// Tasks in one batch round: three undrained-edge passes (the wake tests' kBatch).
	constexpr int kBatchTasks = 3 * static_cast<int>(wakeloop::kUndrainedBudget);

	// What --check uses where the command line gives nothing.
	constexpr std::int64_t kCheckWaitUs = 10'000;
	/// The largest bound the loop accepts (one day): the number in loop/src/common.cpp, detail::checked_bound, which
	/// a run meets only when it creates the loop, after clock readings and timer settings. plan_refusal asks for it
	/// first, so that --validate-args refuses what a run refuses. The CTest validate_args.limit_is_the_loops compares
	/// the two numbers in the sources.
	constexpr std::int64_t kMaxWaitUs = 86'400'000'000;
	constexpr std::int64_t kCheckGapUs = 2'000;
	constexpr int kCheckPosts = 5;
	constexpr int kCheckRounds = 3;

	enum class Protocol
	{
		single,  // S: one post at a time
		batch    // Bt: rounds of kBatchTasks tasks behind a held worker
	};

	constexpr const char* protocol_name(Protocol p) noexcept { return p == Protocol::batch ? "batch" : "single"; }

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
		// > 0: each gap drawn uniformly from [gap_lo_frac x unit, gap_hi_frac x unit] with gap_seed
		double gap_lo_frac = -1;
		double gap_hi_frac = -1;
		std::uint64_t gap_seed = 0;
		bool gap_unit_timer = false;  // the range's unit: B, or the larger of B and the coarsest timer resolution
		int pm_qos_us = -1;  // >= 0: hold a /dev/cpu_dma_latency request of this many us
		Protocol protocol = Protocol::single;
		bool check = false;  // the no-timing self-test
		bool validate_args = false;  // check the arguments as a run would and stop; see the header
		std::vector<unsigned> worker_cpus;  // empty: unpinned; else one CPU per worker
		int poster_cpu = -1;  // < 0: unpinned

		bool random_gap() const { return gap_lo_frac > 0; }
		bool batch() const { return protocol == Protocol::batch; }
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
		bool timed_out = false;  // the post missed its timeout; a round: its tasks did, after the release
		bool gate_timed_out = false;  // a round only: its gate missed the timeout
		std::optional<std::int64_t> phase_ns;
		std::optional<std::int64_t> predicted_ns;
		std::optional<std::int64_t> residual_ns;
		std::optional<std::int64_t> gap_drawn_ns;  // random gap slept before this post

		// Batch rounds only. post_ns is then the gate's post, exec_ns the latest run of the round's
		// tasks, and the delay counts from the release.
		int tasks = 0;  // > 0: a round of this many tasks
		int ran = 0;  // tasks of the round that ran
		std::optional<std::int64_t> release_ns;

		std::optional<std::int64_t> delay_ns() const
		{
			if (!exec_ns) return std::nullopt;
			return *exec_ns - (release_ns ? *release_ns : post_ns);
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
		std::int64_t gap_unit_us = 0;  // the unit of --gap-frac-range in this run (0 without it)
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
		std::vector<int> worker_cpus_seen;  // the CPU each worker was on when its loop returned
		int poster_cpu_seen = -1;  // the CPU the poster was on after the last unit
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
		          << "       [--protocol single|batch] (default single; batch needs --workers 1 and --wait-us > 0)\n"
		          << "       [--posts N] [--warmup K] [--calibrate C] [--label TEXT] [--design NAME]\n"
		          << "       [--check]             self-test: a few units, counts and PASS or FAIL only, no time printed, no\n"
		          << "                             --out (--wait-us defaults to " << kCheckWaitUs << ", --gap-us to " << kCheckGapUs
		          << ")\n"
		          << "       [--validate-args]     check the arguments as a run does and measure nothing: prints valid and exits 0,\n"
		          << "                             or refuses with exit 2; no loop, thread, pin, clock or file (--out is not\n"
		          << "                             opened, and may be left out); not with --check\n"
		          << "       --wait-us is the bound B in us; 0 blocks\n"
		          << "       [--timerslack-ns N]   Linux: set before any thread starts; 0 = leave default (default 1)\n"
		          << "       [--timer-period-ms N] Windows: timeBeginPeriod(N); 0 = leave default (default 0)\n"
		          << "       [--phase-frac F]      instead of --gap-us: post F x B_eff after each delivery (a variant that\n"
		          << "                             delays single posts, W = 1)\n"
		          << "       [--gap-frac-range LO:HI] instead of --gap-us: each gap drawn uniformly from\n"
		          << "                             [LO x B, HI x B] (needs B > 0); [--gap-seed N] seeds it (default 0);\n"
		          << "                             [--gap-unit timer] uses the larger of B and the coarsest timer\n"
		          << "                             resolution (Windows) as the unit instead of B (default bound)\n"
		          << "       [--worker-cpus LIST]  pin worker i to the i-th CPU of a comma-separated list (one per worker)\n"
		          << "       [--poster-cpu N]      pin the posting thread to CPU N\n"
		          << "       [--pm-qos-us N]       Linux, needs root: hold a CPU latency request of N us for the run;\n"
		          << "                             under sudo the run then drops back to the invoking user\n"
		          << "  --posts measured posts (default 51); --warmup posts before them (default 5);\n"
		          << "  --calibrate posts to worker 0, B/10 after each delivery, that measure B_eff for a variant that\n"
		          << "              delays single posts (default 11, 0 = none; batch takes none)\n"
		          << "  with --protocol batch, --posts and --warmup count rounds of " << kBatchTasks << " tasks\n"
		          << "  with --check the defaults are " << kCheckPosts << " posts or " << kCheckRounds
		          << " rounds, one warm-up per worker (at least 1) and no calibration\n";
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
		bool have_posts = false;
		bool have_warmup = false;
		bool have_calibrate = false;
		for (int i = 1; i < argc; ++i)
		{
			const std::string_view flag = argv[i];
			if (flag == "--check")  // a flag without a value
			{
				o.check = true;
				continue;
			}
			if (flag == "--validate-args")  // the other
			{
				o.validate_args = true;
				continue;
			}
			if (i + 1 >= argc) usage("missing value for " + std::string(flag));
			const std::string_view value = argv[++i];
			if (flag == "--backend") o.backend = value;
			else if (flag == "--variant") o.variant = value;
			else if (flag == "--workers") o.workers = parse_int<unsigned>(flag, value);
			else if (flag == "--worker-cpus")
			{
				o.worker_cpus = benchutil::parse_cpu_list(value);
				if (o.worker_cpus.empty()) usage("--worker-cpus expects a comma-separated list of CPU numbers");
			}
			else if (flag == "--poster-cpu") o.poster_cpu = static_cast<int>(parse_int<std::uint16_t>(flag, value));
			else if (flag == "--protocol")
			{
				if (value == "single") o.protocol = Protocol::single;
				else if (value == "batch") o.protocol = Protocol::batch;
				else usage("--protocol must be single or batch");
			}
			else if (flag == "--wait-us") { o.wait_us = parse_int<std::int64_t>(flag, value); have_wait = true; }
			else if (flag == "--gap-us") { o.gap_us = parse_int<std::int64_t>(flag, value); have_gap = true; }
			else if (flag == "--posts") { o.posts = parse_int<int>(flag, value); have_posts = true; }
			else if (flag == "--warmup") { o.warmup = parse_int<int>(flag, value); have_warmup = true; }
			else if (flag == "--calibrate") { o.calibrate = parse_int<int>(flag, value); have_calibrate = true; }
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
			else if (flag == "--gap-unit")
			{
				if (value == "timer") o.gap_unit_timer = true;
				else if (value != "bound") usage("--gap-unit expects bound or timer");
			}
			else if (flag == "--pm-qos-us")
			{
				if (!wakeprobe::kHaveTimerSlack) usage("--pm-qos-us exists only on Linux");
				o.pm_qos_us = parse_int<int>(flag, value);
				if (o.pm_qos_us < 0) usage("--pm-qos-us must be >= 0");
			}
			else usage("unknown flag " + std::string(flag));
		}
		// A self-test has defaults and refuses --out; a validation is of a measuring run's arguments.
		if (o.validate_args && o.check) usage("--validate-args and --check are separate modes; give one of them");
		if (o.check && !have_gap && !(o.phase_frac > 0) && !o.random_gap())
		{
			o.gap_us = kCheckGapUs;
			have_gap = true;
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
		if (!o.worker_cpus.empty() && o.worker_cpus.size() != o.workers) usage("--worker-cpus needs one CPU per worker");
		{
			std::vector<unsigned> cpus = o.worker_cpus;
			if (o.poster_cpu >= 0) cpus.push_back(static_cast<unsigned>(o.poster_cpu));
			std::sort(cpus.begin(), cpus.end());
			if (std::adjacent_find(cpus.begin(), cpus.end()) != cpus.end()) usage("--worker-cpus and --poster-cpu need distinct CPUs");
		}
		if (o.workers < wakeloop::min_workers(o.defect))
		{
			usage("the variant " + o.variant + " needs --workers >= " + std::to_string(wakeloop::min_workers(o.defect)));
		}
		if (o.check)
		{
			if (!o.out.empty()) usage("--check writes no record file; leave --out out");
			if (o.phase_frac > 0) usage("--check takes no --phase-frac, which needs a measured B_eff");
			if (!have_wait)
			{
				o.wait_us = kCheckWaitUs;
				have_wait = true;
			}
			if (o.wait_us <= 0) usage("--check needs a bound (--wait-us > 0)");
			// One warm-up and a few units per worker, and no calibration: nothing here is timed.
			const int workers = static_cast<int>(o.workers);  // already within 1 to kMaxWorkers
			if (!have_posts) o.posts = o.batch() ? kCheckRounds : std::max(kCheckPosts, workers);
			if (!have_warmup) o.warmup = o.batch() ? 1 : std::max(1, workers);
			if (!have_calibrate) o.calibrate = 0;
		}
		// --validate-args opens nothing, and the campaign runner appends --out to the plan's arguments.
		if (!have_wait || (!o.check && !o.validate_args && o.out.empty()))
		{
			usage("--backend, --wait-us and --out are required");
		}
		if (o.wait_us < 0) usage("--wait-us must be >= 0 (0 blocks)");
		if (o.random_gap() && o.wait_us <= 0) usage("--gap-frac-range needs a bound (--wait-us > 0)");
		if (o.gap_unit_timer && !o.random_gap()) usage("--gap-unit timer needs --gap-frac-range");
		if (o.batch())
		{
			if (o.workers != 1) usage("--protocol batch needs --workers 1");
			if (o.wait_us <= 0) usage("--protocol batch needs a bound (--wait-us > 0)");
			if (o.phase_frac > 0) usage("--protocol batch takes no --phase-frac");
			if (have_calibrate && o.calibrate > 0) usage("--protocol batch takes no calibration posts; leave --calibrate out or give 0");
			o.calibrate = 0;  // the default of 11 does not apply to rounds
		}
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

	/// The refusals that a run makes after its arguments parse and before it creates anything, and
	/// that depend on the arguments alone: the text of the first one, empty when there is none. A
	/// run and --validate-args both ask here, from main, so the two cannot drift. run_with names
	/// the variant by its loop type, Loop::defect; that is opt.defect, since the dispatch is on it.
	std::string plan_refusal(const Options& opt)
	{
		if (opt.wait_us > kMaxWaitUs)
		{
			return "--wait-us must be from 0 to " + std::to_string(kMaxWaitUs) + " (the loop's limit), got " +
			       std::to_string(opt.wait_us);
		}
		const bool delaying = delays_single_posts(opt.defect, opt.workers);
		if (opt.wait_us == 0 && delaying)
		{
			return "a variant that delays single posts, with a blocking wait, never delivers a post";
		}
		if (opt.phase_frac > 0 && (!delaying || opt.calibrate < 1))
		{
			return "--phase-frac needs a variant that delays single posts and calibration posts";
		}
		return {};
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
			// A second run is counted (--check fails on it) and does not touch the promise.
			if (n.runs.fetch_add(1, std::memory_order_acq_rel) == 0) n.done.set_value(now);
		}
		std::promise<Clock::time_point> done;
		std::atomic<long long>* slack_out = nullptr;  // the probe post reads the worker's timer slack
		std::atomic<int> runs{0};
	};

	/// A counter with a timed wait: the batch protocol's gate signals and a round's latch.
	class Latch
	{
	public:
		void add()
		{
			{
				const std::lock_guard lock(m_);
				++count_;
			}
			cv_.notify_all();
		}

		/// True when the count reached n by the deadline.
		bool wait_until(int n, Clock::time_point deadline)
		{
			std::unique_lock lock(m_);
			return cv_.wait_until(lock, deadline, [&] { return count_ >= n; });
		}

	private:
		std::mutex m_;
		std::condition_variable cv_;
		int count_ = 0;
	};

	/// A task that holds its worker until opened, for at most kHardLimit so that a broken run
	/// still ends. It counts its runs; only the first one holds.
	struct Gate : wakeloop::Task
	{
		Gate() { run = &Gate::on_run; }
		static void on_run(wakeloop::Task& t) noexcept
		{
			auto& g = static_cast<Gate&>(t);
			if (g.runs.fetch_add(1, std::memory_order_acq_rel) != 0) return;
			g.entered.add();
			g.release.wait_until(1, Clock::now() + kHardLimit);
		}
		void open() { release.add(); }

		Latch entered;
		Latch release;
		std::atomic<int> runs{0};
	};

	/// Opens a gate when it leaves scope, so that an early return or a throw never leaves the
	/// worker held when the runner joins it.
	struct GateOpener
	{
		Gate& gate;
		~GateOpener() { gate.open(); }
	};

	/// What a round's tasks share: how many have run, and a latch that the last one opens.
	struct RoundSync
	{
		std::atomic<int> ran{0};
		Latch all_ran;
	};

	/// One of a round's tasks: when it ran, and how often. Only the task that runs last takes
	/// the latch's lock, so that the instrument adds as little as it can to the round's delay.
	struct BatchNode : wakeloop::Task
	{
		BatchNode() { run = &BatchNode::on_run; }
		static void on_run(wakeloop::Task& t) noexcept
		{
			auto& n = static_cast<BatchNode&>(t);
			const auto now = Clock::now();
			if (n.runs.fetch_add(1, std::memory_order_acq_rel) != 0) return;
			n.ran_at = now;
			if (n.sync->ran.fetch_add(1, std::memory_order_acq_rel) + 1 == kBatchTasks) n.sync->all_ran.add();
		}
		RoundSync* sync = nullptr;
		Clock::time_point ran_at{};
		std::atomic<int> runs{0};
	};

	/// A round's tasks: the gate, what the others share, and the others.
	struct BatchRound
	{
		BatchRound()
		{
			for (BatchNode& t : tasks) t.sync = &sync;
		}
		Gate gate;
		RoundSync sync;
		std::array<BatchNode, static_cast<std::size_t>(kBatchTasks)> tasks;
		int posted = 0;  // tasks posted so far, the gate included; the main thread only
	};

	/// Every task of a run. Declared before the loop, so that it outlives the workers: a unit that
	/// is never delivered stays valid while it is still queued.
	struct Storage
	{
		std::deque<PostNode> nodes;
		std::deque<BatchRound> rounds;
	};

	// Runs worker w of the loop on a thread of its own, for every w; stops and joins them on scope
	// exit, so an exception in the measurement cannot reach std::thread's destructor and lose the
	// records collected so far. With CPUs given, each worker pins itself before its loop starts.
	template <class Loop>
	class LoopRunner
	{
	public:
		LoopRunner(Loop& loop, const std::vector<unsigned>& cpus)
			: loop_(loop), cpus_(cpus), errors_(loop.workers()), pin_rc_(loop.workers(), 0), cpu_seen_(loop.workers(), -1)
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
		/// Waits until every worker has tried its pin; false if that takes longer than `limit`.
		bool wait_pinned(std::chrono::milliseconds limit) const
		{
			const auto end = Clock::now() + limit;
			while (pinned_.load(std::memory_order_acquire) < loop_.workers())
			{
				if (Clock::now() > end) return false;
				std::this_thread::sleep_for(std::chrono::milliseconds(1));
			}
			return true;
		}
		/// After wait_pinned(): the pins that failed, as text; empty when none did.
		std::string pin_failures() const
		{
			std::string s;
			for (std::size_t w = 0; w < pin_rc_.size(); ++w)
			{
				if (pin_rc_[w] == 0) continue;
				s += (s.empty() ? "" : "; ") + std::string("worker ") + std::to_string(w) + " to CPU " +
				     std::to_string(cpus_[w]) + ": error " + std::to_string(pin_rc_[w]);
			}
			return s;
		}
		// Valid after stop_and_join(): the CPU each worker was on when its loop returned.
		const std::vector<int>& cpus_seen() const { return cpu_seen_; }

	private:
		void work(unsigned w)
		{
			if (!cpus_.empty()) pin_rc_[w] = wakeprobe::pin_current_thread(cpus_[w]);
			pinned_.fetch_add(1, std::memory_order_release);  // publishes pin_rc_[w]
			try
			{
				loop_.run(w);
			}
			catch (...)
			{
				errors_[w] = std::current_exception();  // each worker writes its own element
			}
			cpu_seen_[w] = wakeprobe::current_cpu();
		}

		Loop& loop_;
		std::vector<unsigned> cpus_;
		std::vector<std::exception_ptr> errors_;
		std::vector<int> pin_rc_;  // each worker writes its own element, before pinned_ counts it
		std::vector<int> cpu_seen_;  // each worker writes its own element, before it exits
		std::atomic<unsigned> pinned_{0};
		std::vector<std::thread> threads_;
	};

	// A pinned thread seen on another CPU at the end of the run: its pin did not hold. Empty when
	// every pinned thread ended where it was pinned.
	std::string placement_errors(const Options& opt, const RunInfo& info)
	{
		std::string s;
		const auto add = [&s](const std::string& who, int seen, unsigned want) {
			s += (s.empty() ? "" : "; ") + who + " ended on CPU " + std::to_string(seen) + ", not on CPU " + std::to_string(want);
		};
		for (std::size_t w = 0; w < opt.worker_cpus.size() && w < info.worker_cpus_seen.size(); ++w)
		{
			if (info.worker_cpus_seen[w] != static_cast<int>(opt.worker_cpus[w]))
			{
				add("worker " + std::to_string(w), info.worker_cpus_seen[w], opt.worker_cpus[w]);
			}
		}
		// -1: the measurement threw before the poster's last unit; that error is reported already.
		if (opt.poster_cpu >= 0 && info.poster_cpu_seen != -1 && info.poster_cpu_seen != opt.poster_cpu)
		{
			add("the poster", info.poster_cpu_seen, static_cast<unsigned>(opt.poster_cpu));
		}
		return s;
	}

	// Checks the workers' pins and pins the calling thread, the poster. Throws on any failure, so
	// that no unit is measured with a thread off its CPU.
	template <class Runner>
	void place_threads(const Runner& runner, const Options& opt)
	{
		if (!runner.wait_pinned(std::chrono::seconds(1))) throw std::runtime_error("the workers did not start within 1 s");
		if (const std::string f = runner.pin_failures(); !f.empty()) throw std::runtime_error("cannot pin " + f);
		if (opt.poster_cpu < 0) return;
		const int rc = wakeprobe::pin_current_thread(static_cast<unsigned>(opt.poster_cpu));
		if (rc != 0)
		{
			throw std::runtime_error("cannot pin the poster to CPU " + std::to_string(opt.poster_cpu) + ": error " +
			                         std::to_string(rc));
		}
	}

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

	std::int64_t since(Clock::time_point epoch, Clock::time_point t)
	{
		return std::chrono::duration_cast<std::chrono::nanoseconds>(t - epoch).count();
	}

	// One round of protocol Bt on worker 0: post a gate and wait until the worker is inside it,
	// post kBatchTasks tasks behind it, take the release time, open the gate, and wait for the
	// tasks. The round's delay is the latest run time of the tasks minus the release time. Each
	// wait has the per-post timeout; a round that misses one is timed out, and one that is still
	// short at the hard limit is lost (exec_ns stays empty).
	template <class Loop>
	Record measure_round(Loop& loop, std::deque<BatchRound>& rounds, Kind kind, int index, Clock::time_point epoch,
	                     std::chrono::microseconds timeout)
	{
		BatchRound& round = rounds.emplace_back();
		const GateOpener opener{round.gate};

		Record r;
		r.index = index;
		r.kind = kind;
		r.worker = 0;
		r.tasks = kBatchTasks;
		const auto posted = Clock::now();
		loop.post_to(0, round.gate);
		round.posted = 1;
		r.post_ns = since(epoch, posted);
		if (!round.gate.entered.wait_until(1, posted + timeout))
		{
			r.gate_timed_out = true;
			if (!round.gate.entered.wait_until(1, posted + kHardLimit)) return r;  // lost
		}

		for (BatchNode& t : round.tasks) loop.post_to(0, t);
		round.posted += kBatchTasks;
		const auto released = Clock::now();
		r.release_ns = since(epoch, released);
		round.gate.open();

		if (!round.sync.all_ran.wait_until(1, released + timeout))
		{
			r.timed_out = true;
			if (!round.sync.all_ran.wait_until(1, released + kHardLimit))
			{
				r.ran = round.sync.ran.load(std::memory_order_acquire);
				return r;  // lost
			}
		}
		// The counter reached kBatchTasks, and its acquire and release chain publishes every run time.
		Clock::time_point last{};
		for (const BatchNode& t : round.tasks) last = std::max(last, t.ran_at);
		r.ran = kBatchTasks;
		r.exec_ns = since(epoch, last);
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
	// the workers. Under protocol Bt the warmup and measured units are rounds, all on worker 0,
	// and there is no calibration. Records are appended as they are taken, so a throw leaves
	// everything before it in place.
	template <class Loop>
	void run_protocol(Loop& loop, Storage& store, const Options& opt, RunInfo& info, bool delaying,
	                  wakeprobe::GapSleeper& gap, std::vector<Record>& records)
	{
		std::this_thread::sleep_for(kSettle);
		const auto epoch = Clock::now();
		int index = 0;

		// Until B_eff is measured, a timed return can take the coarsest timer resolution with a
		// short bound (a whole default tick on Windows), so every timeout counts from the larger of
		// B and that floor. B = 0 keeps the unbounded timeout.
		const std::int64_t floor_us = static_cast<std::int64_t>(wakeprobe::coarsest_timer_us());
		const auto bounded_timeout = [&opt, floor_us](std::int64_t b_eff_us) {
			return opt.wait_us > 0 ? timeout_for(std::max({opt.wait_us, b_eff_us, floor_us})) : timeout_for(0);
		};

		// The probe reads the worker's timer slack, which shows whether it was inherited.
		records.push_back(
			measure_one(loop, store.nodes, Kind::Probe, index++, 0, epoch, bounded_timeout(0), &info.slack_worker));
		if (!records.back().exec_ns) return;

		if (delaying)
		{
			for (int k = 0; k < opt.calibrate; ++k)
			{
				// B/10 first, so the worker has re-parked: a post that lands while it is still
				// draining would run in the same pass. Still far inside one period.
				if (opt.wait_us > 0) gap.sleep(std::chrono::microseconds(opt.wait_us / 10));
				records.push_back(measure_one(loop, store.nodes, Kind::Calib, index++, 0, epoch, bounded_timeout(0)));
				if (!records.back().exec_ns) return;
			}
			info.calib = calibrate(records, opt.wait_us);
		}
		// Batch, the fixed loop and --check calibrate nothing; they keep the floor.
		info.timeout_us = bounded_timeout(info.calib.b_eff_ns ? *info.calib.b_eff_ns / 1000 : 0).count();

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
			// With --gap-unit timer the range spans the same share of the wake period whether or not
			// the system rounds B up to its timer resolution.
			info.gap_unit_us = opt.gap_unit_timer ? std::max(opt.wait_us, floor_us) : opt.wait_us;
			const auto unit = static_cast<double>(info.gap_unit_us);
			draw.emplace(opt.gap_lo_frac * unit, opt.gap_hi_frac * unit, opt.gap_seed);
		}
		std::optional<std::int64_t> drawn_ns;  // the random gap slept before the next post

		const int total = opt.warmup + opt.posts;
		for (int i = 0; i < total; ++i)
		{
			const Kind kind = i < opt.warmup ? Kind::Warmup : Kind::Measured;
			const unsigned worker = static_cast<unsigned>(i) % opt.workers;
			const std::chrono::microseconds timeout(info.timeout_us);
			if (opt.batch())
			{
				records.push_back(measure_round(loop, store.rounds, kind, index++, epoch, timeout));
			}
			else
			{
				records.push_back(measure_one(loop, store.nodes, kind, index++, worker, epoch, timeout));
			}
			records.back().gap_drawn_ns = drawn_ns;
			if (!records.back().exec_ns) return;
			if (i + 1 == total) break;
			// A fixed or random gap after the delivery was seen, or a target phase after the
			// delivery itself. In batch every gap counts from the round's last run.
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
			if (opt.batch())
			{
				// Counted from the round's last run, not from when this thread noticed it.
				const auto due = epoch + std::chrono::nanoseconds(*records.back().exec_ns) + g;
				g = std::max(std::chrono::microseconds::zero(),
				             std::chrono::duration_cast<std::chrono::microseconds>(due - Clock::now()));
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

	// The fields that open a post line and a round line.
	void line_head(std::ostringstream& s, const Options& o, const std::string& backend)
	{
		s << "{\"design\":\"" << json_escape(o.design) << "\",\"label\":\"" << json_escape(o.label)
		  << "\",\"backend\":\"" << backend << "\",\"wait_us\":" << o.wait_us << ",\"gap_us\":" << gap_json(o)
		  << ",\"phase_frac\":" << frac_json(o);
	}

	std::string post_line(const Options& o, const std::string& backend, const Record& r)
	{
		std::ostringstream s;
		line_head(s, o, backend);
		s << ",\"index\":" << r.index << ",\"worker\":" << r.worker
		  << ",\"kind\":\"" << kind_name(r.kind)
		  << "\",\"warmup\":" << (r.kind != Kind::Measured ? "true" : "false") << ",\"delay_ns\":" << num(r.delay_ns())
		  << ",\"post_ns\":" << r.post_ns << ",\"exec_ns\":" << num(r.exec_ns)
		  << ",\"timed_out\":" << (r.timed_out ? "true" : "false") << ",\"phase_ns\":" << num(r.phase_ns)
		  << ",\"predicted_ns\":" << num(r.predicted_ns) << ",\"residual_ns\":" << num(r.residual_ns)
		  << ",\"gap_drawn_ns\":" << num(r.gap_drawn_ns) << "}";
		return s.str();
	}

	// A batch round ("unit":"round", "kind" warmup or measured). post_ns is the gate's post,
	// release_ns the moment the gate opened and exec_ns the latest run of the round's tasks;
	// delay_ns is exec_ns minus release_ns. gate_timed_out: the gate missed the timeout;
	// timed_out: the tasks missed it after the release.
	std::string round_line(const Options& o, const std::string& backend, const Record& r)
	{
		std::ostringstream s;
		line_head(s, o, backend);
		s << ",\"index\":" << r.index << ",\"worker\":" << r.worker << ",\"unit\":\"round\",\"kind\":\""
		  << kind_name(r.kind) << "\",\"warmup\":" << (r.kind != Kind::Measured ? "true" : "false")
		  << ",\"tasks\":" << r.tasks << ",\"ran\":" << r.ran
		  << ",\"delay_ns\":" << num(r.delay_ns()) << ",\"post_ns\":" << r.post_ns << ",\"release_ns\":" << num(r.release_ns)
		  << ",\"exec_ns\":" << num(r.exec_ns) << ",\"gate_timed_out\":" << (r.gate_timed_out ? "true" : "false")
		  << ",\"timed_out\":" << (r.timed_out ? "true" : "false") << ",\"gap_drawn_ns\":" << num(r.gap_drawn_ns) << "}";
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
		int over_half_b = 0;  // delay > B / 2 with the nominal B (0 when B = 0); the analysis uses B_eff
	};

	Stats measured_stats(const std::vector<Record>& records, const std::optional<std::int64_t>& b_eff, std::int64_t bound_us)
	{
		Stats s;
		for (const auto& r : records)
		{
			if (r.kind != Kind::Measured) continue;
			if (r.timed_out || r.gate_timed_out) ++s.timeouts;
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
		  << ",\"gap_unit\":" << (o.random_gap() ? (o.gap_unit_timer ? "\"timer\"" : "\"bound\"") : "null")
		  << ",\"gap_unit_us\":" << (o.random_gap() ? std::to_string(info.gap_unit_us) : std::string("null"))
		  << ",\"protocol\":\"" << protocol_name(o.protocol) << "\",\"batch_tasks\":"
		  << (o.batch() ? std::to_string(kBatchTasks) : std::string("null"))
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
		s << ",\"residual_jumps\":" << st.jumps << ",\"count_over_half_b_nominal\":" << st.over_half_b
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
		s << "],\"worker_cpus\":" << benchutil::cpu_list_json(o.worker_cpus)
		  << ",\"worker_cpus_seen\":" << benchutil::cpu_list_json(info.worker_cpus_seen)
		  << ",\"poster_cpu\":" << (o.poster_cpu >= 0 ? std::to_string(o.poster_cpu) : std::string("null"))
		  << ",\"poster_cpu_seen\":" << (info.poster_cpu_seen >= 0 ? std::to_string(info.poster_cpu_seen) : std::string("null"))
		  << ",\"coarsest_timer_us\":"
		  << (wakeprobe::coarsest_timer_us() > 0 ? std::to_string(wakeprobe::coarsest_timer_us()) : std::string("null"));
		s << ",\"compiler\":\""
		  << json_escape(wakeprobe::compiler_name()) << "\",\"build_type\":\"" << WAKEPROBE_BUILD_TYPE
		  << "\",\"bench_commit\":\"" << WAKEPROBE_BENCH_COMMIT << "\",\"bench_dirty\":"
		  << (WAKEPROBE_BENCH_DIRTY ? "true" : "false") << ",\"bench_code_commit\":\"" << WAKEPROBE_BENCH_CODE_COMMIT << "\""
		  << ",\"host\":\"" << json_escape(wakeprobe::host_name()) << "\",\"started_utc\":\"" << info.started_utc
		  << "\",\"clock\":\"steady_clock\",\"error\":"
		  << (info.error.empty() ? std::string("null") : "\"" + json_escape(info.error) + "\"") << "}";
		return s.str();
	}

	// ----------------------------------------------------------------------------------------
	// The self-test (--check): counts only, nothing measured
	// ----------------------------------------------------------------------------------------

	struct CheckTally
	{
		int units = 0;
		int probes = 0;
		int calibrations = 0;
		int warmups = 0;
		int measured = 0;
		int delivered = 0;
		int timed_out = 0;
		int lost = 0;
		long long posted = 0;  // tasks posted: every post, and each round's gate and tasks
		long long once = 0;  // tasks that ran exactly once
		long long repeated = 0;  // tasks that ran more than once
	};

	// Read after the workers have been joined, so that every count is final.
	CheckTally tally(const std::vector<Record>& records, const Storage& store)
	{
		CheckTally t;
		t.units = static_cast<int>(records.size());
		for (const Record& r : records)
		{
			switch (r.kind)
			{
				case Kind::Probe: ++t.probes; break;
				case Kind::Calib: ++t.calibrations; break;
				case Kind::Warmup: ++t.warmups; break;
				case Kind::Measured: ++t.measured; break;
			}
			++(r.exec_ns ? t.delivered : t.lost);
			if (r.timed_out || r.gate_timed_out) ++t.timed_out;
		}
		const auto note = [&t](int runs) {
			if (runs == 1) ++t.once;
			else if (runs > 1) ++t.repeated;
		};
		t.posted += static_cast<long long>(store.nodes.size());
		for (const PostNode& n : store.nodes) note(n.runs.load(std::memory_order_acquire));
		for (const BatchRound& round : store.rounds)
		{
			t.posted += round.posted;
			note(round.gate.runs.load(std::memory_order_acquire));
			if (round.posted <= 1) continue;  // the tasks were never posted
			for (const BatchNode& n : round.tasks) note(n.runs.load(std::memory_order_acquire));
		}
		return t;
	}

	/// What is wrong with a finished --check run, one line each; empty when it is sound.
	std::vector<std::string> check_faults(const CheckTally& t, int expected_units, const std::string& error)
	{
		std::vector<std::string> bad;
		if (!error.empty()) bad.push_back(error);
		if (t.units != expected_units)
		{
			bad.push_back("took " + std::to_string(t.units) + " units, expected " + std::to_string(expected_units));
		}
		if (t.lost > 0) bad.push_back(std::to_string(t.lost) + " units were never delivered");
		if (t.timed_out > 0) bad.push_back(std::to_string(t.timed_out) + " units missed their timeout");
		if (t.repeated > 0) bad.push_back(std::to_string(t.repeated) + " tasks ran more than once");
		if (t.once != t.posted)
		{
			bad.push_back("only " + std::to_string(t.once) + " of " + std::to_string(t.posted) + " posted tasks ran exactly once");
		}
		return bad;
	}

	/// Prints the counts and the verdict; 0 for PASS, 1 for FAIL. No delay, time or rate.
	int report_check(const Options& opt, const CheckTally& t, const std::vector<std::string>& faults)
	{
		const std::string what = std::string(" --check ") + opt.backend + " " + protocol_name(opt.protocol) + " " + opt.variant;
		std::cout << "wakeprobe" << what << ", workers " << opt.workers << "\n"
		          << "units " << t.units << " (" << t.probes << " probe, " << t.calibrations << " calibration, " << t.warmups
		          << " warm-up, " << t.measured << " measured), delivered " << t.delivered << ", timed out " << t.timed_out
		          << ", lost " << t.lost << "\n"
		          << "tasks posted " << t.posted << ", ran exactly once " << t.once << ", ran more than once " << t.repeated << "\n";
		if (!faults.empty())
		{
			for (const std::string& f : faults) std::cerr << "FAIL: wakeprobe" << what << ": " << f << "\n";
			return 1;
		}
		std::cout << "PASS: wakeprobe" << what << ": " << (opt.batch() ? "rounds" : "posts") << " delivered within the timeout, "
		          << "every task ran exactly once\n";
		return 0;
	}

	// Everything from the loop's creation to the output, for one backend class.
	template <class Loop>
	int run_with(const Options& opt, RunInfo& info, const wakeprobe::TimerPeriod& period)
	{
		Storage store;  // outlives the loop, which may still hold some of the tasks
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

		std::ofstream out;
		if (!opt.check)
		{
			out.open(opt.out, std::ios::trunc);
			if (!out)
			{
				std::cerr << "wakeprobe: cannot write " << opt.out << "\n";
				return 5;
			}
		}

		std::vector<Record> records;
		wakeprobe::GapSleeper gap;
		{
			LoopRunner<Loop> runner(*loop, opt.worker_cpus);
			try
			{
				place_threads(runner, opt);
				run_protocol(*loop, store, opt, info, delaying, gap, records);
				info.poster_cpu_seen = wakeprobe::current_cpu();
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
			info.worker_cpus_seen = runner.cpus_seen();
			if (const std::string p = placement_errors(opt, info); !p.empty())
			{
				info.error += (info.error.empty() ? "" : "; ") + p;
			}
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

		if (opt.check)
		{
			const CheckTally counts = tally(records, store);
			const int expected = 1 + (delaying ? opt.calibrate : 0) + opt.warmup + opt.posts;
			return report_check(opt, counts, check_faults(counts, expected, info.error));
		}

		if (!opt.batch()) annotate(records, info.calib.b_eff_ns, Loop::defect, opt.workers);
		for (const auto& r : records)
		{
			out << (r.tasks > 0 ? round_line(opt, info.backend, r) : post_line(opt, info.backend, r)) << "\n";
		}
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

	// First, before any side effect, so that a refused run did nothing and --validate-args, which
	// stops here, did nothing either: no wall clock, no PM QoS request, no timer slack or period,
	// no thread, no loop and no file.
	if (const std::string why = plan_refusal(opt); !why.empty())
	{
		std::cerr << "wakeprobe: " << why << "\n";
		return 2;
	}
	if (opt.validate_args)
	{
		std::cout << "valid\n";
		return 0;
	}

	RunInfo info;
	if (!opt.check) info.started_utc = wakeprobe::utc_now();  // --check reads no wall clock

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
