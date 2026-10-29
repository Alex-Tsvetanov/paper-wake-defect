// H6 of P1 v2 (hypotheses-v2-h6.md, frozen 2026-10-29): Netty's EpollEventLoop, 4.1.0.CR7 against
// 4.1.0.Final. One process of one protocol on one arm; the arm is only the jar on the class path.
//
//   java -cp <netty-all jar>:<classes> H6Probe --protocol forced|natural --netty-version 4.1.0.CR7
//        --process I --out FILE [--trials 15] [--posts 21] [--warmup-s 10] [--warmup-posts 5]
//        [--slot-ms 1100] [--limit-ms 10000] [--hook-wait-ms 1000]
//        [--loop-cpu 14] [--poster-cpu 2] [--rest-cpu 0] [--check]
//   java -cp ... H6Probe --epoll-only --netty-version V --process 0 --out FILE
//
// --epoll-only checks the Netty version and loads the native library (Epoll.isAvailable()); it
// makes no event loop and no post, so it may run against CR7 before the timed run (section 6).
//
// The runner starts the JVM under `taskset -c 0,2,14` with -XX:+PrintCompilation (stdout) and
// -Dio.netty.native.workdir (section 2). This program writes JSON lines to --out: one line per
// measured post, one per trial (GC), and a summary line. --check writes no time at all (the
// desktop rule, section 6). Exit 0 when the process is good by section 5, 1 when it is not, 2 on
// a usage error.

import io.netty.channel.DefaultSelectStrategyFactory;
import io.netty.channel.EventLoop;
import io.netty.channel.SelectStrategy;
import io.netty.channel.SelectStrategyFactory;
import io.netty.channel.epoll.Epoll;
import io.netty.channel.epoll.EpollEventLoopGroup;
import io.netty.util.IntSupplier;
import io.netty.util.Version;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Semaphore;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.LockSupport;
import java.util.logging.Handler;
import java.util.logging.Level;
import java.util.logging.LogRecord;
import java.util.logging.Logger;

public final class H6Probe {
    static final long B_NS = TimeUnit.SECONDS.toNanos(1);  // the loop's bound with no scheduled task
    static final long HALF_B_NS = B_NS / 2;
    static final int MAX_KEPT_MESSAGES = 5;

    // ---------------------------------------------------------------------------- options

    static final class Options {
        String protocol, nettyVersion, out;
        int process = -1, trials = 15, posts = 21, warmupPosts = 5;
        long warmupS = 10, slotMs = 1100, limitMs = 10000, hookWaitMs = 1000;
        int loopCpu = 14, posterCpu = 2, restCpu = 0;
        boolean check, epollOnly;
        final List<String> raw = new ArrayList<String>();
    }

    static Options parse(String[] args) {
        Options o = new Options();
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            o.raw.add(a);
            if (a.equals("--check")) { o.check = true; continue; }
            if (a.equals("--epoll-only")) { o.epollOnly = true; o.check = true; continue; }
            if (i + 1 >= args.length) throw new IllegalArgumentException(a + " needs a value");
            String v = args[++i];
            o.raw.add(v);
            if (a.equals("--protocol")) o.protocol = v;
            else if (a.equals("--netty-version")) o.nettyVersion = v;
            else if (a.equals("--out")) o.out = v;
            else if (a.equals("--process")) o.process = Integer.parseInt(v);
            else if (a.equals("--trials")) o.trials = Integer.parseInt(v);
            else if (a.equals("--posts")) o.posts = Integer.parseInt(v);
            else if (a.equals("--warmup-s")) o.warmupS = Long.parseLong(v);
            else if (a.equals("--warmup-posts")) o.warmupPosts = Integer.parseInt(v);
            else if (a.equals("--slot-ms")) o.slotMs = Long.parseLong(v);
            else if (a.equals("--limit-ms")) o.limitMs = Long.parseLong(v);
            else if (a.equals("--hook-wait-ms")) o.hookWaitMs = Long.parseLong(v);
            else if (a.equals("--loop-cpu")) o.loopCpu = Integer.parseInt(v);
            else if (a.equals("--poster-cpu")) o.posterCpu = Integer.parseInt(v);
            else if (a.equals("--rest-cpu")) o.restCpu = Integer.parseInt(v);
            else throw new IllegalArgumentException("unknown option " + a);
        }
        if (!o.epollOnly && !"forced".equals(o.protocol) && !"natural".equals(o.protocol))
            throw new IllegalArgumentException("--protocol is forced or natural");
        if (o.nettyVersion == null || o.out == null || o.process < 0)
            throw new IllegalArgumentException("--netty-version, --out and --process are required");
        if (o.trials < 1 || o.posts < 1 || o.warmupPosts < 0 || o.warmupS < 0 || o.slotMs < 1 || o.limitMs < 1)
            throw new IllegalArgumentException("sizes must be positive");
        return o;
    }

    // ---------------------------------------------------------------------------- problems and Netty's log

    static final List<String> PROBLEMS = Collections.synchronizedList(new ArrayList<String>());

    static void problem(String text) {
        PROBLEMS.add(text);
    }

    /** Counts Netty's log records at WARNING or above (section 5): run() sleeps 1 s after any exception it catches. */
    static final class WarnCounter extends Handler {
        volatile int count;
        final List<String> kept = Collections.synchronizedList(new ArrayList<String>());

        @Override
        public synchronized void publish(LogRecord r) {
            if (r.getLevel().intValue() >= Level.WARNING.intValue()) {
                count++;
                if (kept.size() < MAX_KEPT_MESSAGES) kept.add(r.getLoggerName() + ": " + r.getMessage());
            }
        }

        @Override public void flush() { }
        @Override public void close() { }
    }

    static final Logger NETTY_LOGGER = Logger.getLogger("io.netty");  // held, so the handler stays attached
    static final WarnCounter WARN = new WarnCounter();

    // ---------------------------------------------------------------------------- one post

    /** A task that records when it ran and wakes the thread waiting for it. */
    static final class Task implements Runnable {
        volatile long submit, ran;
        final Thread waiter;
        final Runnable before;

        Task(Thread waiter, Runnable before) {
            this.waiter = waiter;
            this.before = before;
        }

        @Override
        public void run() {
            try {
                ran = System.nanoTime();
                if (before != null) before.run();
            } catch (Throwable t) {
                problem("task: " + t);
            } finally {
                if (waiter != null) LockSupport.unpark(waiter);
            }
        }
    }

    /** One forced post: X wakes the loop and arms the hook; the hook has the helper post Y (section 3). */
    static final class Forced {
        final CountDownLatch latch = new CountDownLatch(1);
        volatile Task x, y;
        volatile long hookIn, hookOut;
        volatile boolean latchOk;
        volatile Integer wakenUpAtHook;
    }

    // ---------------------------------------------------------------------------- the forced hook

    static final class ForcedStrategy implements SelectStrategy {
        final Semaphore helperGo = new Semaphore(0);
        final long hookWaitMs;
        volatile boolean armed;
        volatile Forced post;
        volatile int injections, armedWithTasks;
        volatile Field wakenUp;
        volatile Object loop;

        ForcedStrategy(long hookWaitMs) {
            this.hookWaitMs = hookWaitMs;
        }

        Integer readWakenUp() {
            try {
                return wakenUp == null ? null : wakenUp.getInt(loop);
            } catch (Throwable t) {
                problem("reading wakenUp: " + t);
                return null;
            }
        }

        @Override
        public int calculateStrategy(IntSupplier selectSupplier, boolean hasTasks) throws Exception {
            if (!armed) return hasTasks ? selectSupplier.get() : SelectStrategy.SELECT;  // as DefaultSelectStrategy
            if (hasTasks) {
                armedWithTasks++;
                return selectSupplier.get();
            }
            armed = false;
            Forced p = post;
            try {
                p.hookIn = System.nanoTime();
                p.wakenUpAtHook = readWakenUp();
                helperGo.release();
                p.latchOk = p.latch.await(hookWaitMs, TimeUnit.MILLISECONDS);
                p.hookOut = System.nanoTime();
                injections++;
                if (!p.latchOk) problem("the hook's latch timed out");
            } catch (Throwable t) {
                problem("hook: " + t);
            }
            return SelectStrategy.SELECT;
        }
    }

    // ---------------------------------------------------------------------------- the run

    final Options o;
    final List<String> lines = new ArrayList<String>();
    final Map<String, Object> summary = new LinkedHashMap<String, Object>();
    final Thread main = Thread.currentThread();
    EpollEventLoopGroup group;
    EventLoop loop;
    ForcedStrategy strategy;
    Thread helper;
    volatile String loopTid, helperTid, loopSlack;
    long maxDelay;
    int lostWakes;

    H6Probe(Options o) {
        this.o = o;
    }

    Object time(long ns) {
        return o.check ? null : (Object) ns;  // --check writes no time (section 6)
    }

    void run() throws Exception {
        NETTY_LOGGER.addHandler(WARN);
        summary.put("summary", true);
        summary.put("tool", "H6Probe");
        summary.put("protocol", o.protocol);
        summary.put("expected_version", o.nettyVersion);
        summary.put("process", o.process);
        summary.put("seed", 2026102900L + o.process);  // names the process; nothing is drawn (section 3)
        summary.put("check", o.check);
        summary.put("args", o.raw);
        summary.put("java", System.getProperty("java.vm.name") + " " + System.getProperty("java.runtime.version"));
        summary.put("available_processors", Runtime.getRuntime().availableProcessors());
        summary.put("netty_versions", versions());
        summary.put("epoll_available", Epoll.isAvailable());
        if (!Epoll.isAvailable()) {
            problem("Epoll is not available: " + Epoll.unavailabilityCause());
            return;
        }
        summary.put("epoll_only", o.epollOnly);
        if (o.epollOnly) return;
        SelectStrategyFactory factory;
        if (o.protocol.equals("forced")) {
            strategy = new ForcedStrategy(o.hookWaitMs);
            final ForcedStrategy s = strategy;
            factory = new SelectStrategyFactory() {
                @Override public SelectStrategy newSelectStrategy() { return s; }
            };
        } else {
            factory = DefaultSelectStrategyFactory.INSTANCE;
        }
        group = new EpollEventLoopGroup(1, new ThreadFactory() {
            @Override public Thread newThread(Runnable r) {
                Thread t = new Thread(r, "h6-loop");
                t.setDaemon(true);
                return t;
            }
        }, factory);
        loop = group.next();
        startThreads();
        pin();
        warmUp();
        measure();
        checkPins();
        summary.put("hook_injections", strategy == null ? null : (Object) strategy.injections);
        summary.put("hook_armed_with_tasks", strategy == null ? null : (Object) strategy.armedWithTasks);
        int planned = o.warmupPosts + o.trials * o.posts;
        if (strategy != null && strategy.injections != planned)
            problem("the hook injected " + strategy.injections + " times for " + planned + " forced posts");
    }

    // The artifacts on the measured path. netty-all also lists netty-tcnative, which has its own version.
    static final String[] MEASURED_ARTIFACTS = {"netty-common", "netty-transport", "netty-transport-native-epoll"};

    Map<String, String> versions() {
        Map<String, String> out = new LinkedHashMap<String, String>();
        for (Map.Entry<String, Version> e : Version.identify().entrySet()) out.put(e.getKey(), e.getValue().artifactVersion());
        for (String a : MEASURED_ARTIFACTS)
            if (!o.nettyVersion.equals(out.get(a))) problem("Netty artifact " + a + " is " + out.get(a) + ", not " + o.nettyVersion);
        return out;
    }

    void startThreads() throws Exception {
        final CountDownLatch ready = new CountDownLatch(1);
        loop.execute(new Runnable() {
            @Override public void run() {
                try {
                    loopTid = tid();
                    loopSlack = slack();
                } catch (Throwable t) {
                    problem("loop tid: " + t);
                } finally {
                    ready.countDown();
                }
            }
        });
        if (!ready.await(o.limitMs, TimeUnit.MILLISECONDS)) throw new IllegalStateException("the loop did not start");
        if (strategy != null) {
            Field f = loop.getClass().getDeclaredField("wakenUp");
            f.setAccessible(true);
            strategy.wakenUp = f;
            strategy.loop = loop;
            final CountDownLatch helperReady = new CountDownLatch(1);
            helper = new Thread(new Runnable() {
                @Override public void run() {
                    try {
                        helperTid = tid();
                    } catch (Throwable t) {
                        problem("helper tid: " + t);
                    }
                    helperReady.countDown();
                    helperLoop();
                }
            }, "h6-helper");
            helper.setDaemon(true);
            helper.start();
            helperReady.await();
        }
    }

    void helperLoop() {
        for (;;) {
            try {
                strategy.helperGo.acquire();
            } catch (InterruptedException e) {
                return;
            }
            Forced p = strategy.post;
            try {
                Task y = new Task(main, null);
                p.y = y;
                y.submit = System.nanoTime();
                loop.execute(y);
            } catch (Throwable t) {
                problem("helper: " + t);
            } finally {
                p.latch.countDown();
            }
        }
    }

    // ---------------------------------------------------------------------------- pinning (section 2)

    static String tid() throws IOException {
        Path p = Files.readSymbolicLink(Paths.get("/proc/thread-self"));  // "<pid>/task/<tid>"
        return p.getFileName().toString();
    }

    static String pid() throws IOException {
        return Files.readSymbolicLink(Paths.get("/proc/self")).toString();
    }

    static String command(String... cmd) throws Exception {
        Process pr = new ProcessBuilder(cmd).redirectErrorStream(true).start();
        String text = readAll(pr.getInputStream());
        int rc = pr.waitFor();
        if (rc != 0) throw new IllegalStateException(cmd[0] + " exited " + rc + ": " + text.trim());
        return text;
    }

    // Pins the loop thread, the posting threads, and every other thread of the process to the rest CPU.
    static final String PIN_SCRIPT =
        "set -e\n"
        + "pid=$1; lt=$2; lc=$3; pc=$4; rc=$5; shift 5\n"
        + "taskset -p -c \"$lc\" \"$lt\" >/dev/null\n"
        + "for t in \"$@\"; do taskset -p -c \"$pc\" \"$t\" >/dev/null; done\n"
        + "for t in $(ls /proc/$pid/task); do\n"
        + "  case \" $lt $* \" in *\" $t \"*) continue;; esac\n"
        + "  taskset -p -c \"$rc\" \"$t\" >/dev/null 2>&1 || [ ! -e /proc/$pid/task/$t ]\n"
        + "done\n";

    void pin() throws Exception {
        String mainTid = tid();
        command("true");  // starts the JVM's process reaper thread now, so the script below pins it too
        List<String> cmd = new ArrayList<String>();
        Collections.addAll(cmd, "sh", "-c", PIN_SCRIPT, "h6pin", pid(), loopTid, String.valueOf(o.loopCpu),
                String.valueOf(o.posterCpu), String.valueOf(o.restCpu), mainTid);
        if (helperTid != null) cmd.add(helperTid);
        command(cmd.toArray(new String[0]));
        Map<String, String> tids = new LinkedHashMap<String, String>();
        tids.put("loop", loopTid);
        tids.put("main", mainTid);
        if (helperTid != null) tids.put("helper", helperTid);
        summary.put("tids", tids);
    }

    static String taskFile(String tid, String file) throws IOException {
        return new String(Files.readAllBytes(Paths.get("/proc/self/task", tid, file)), StandardCharsets.US_ASCII);
    }

    static String allowed(String tid) throws IOException {
        for (String line : taskFile(tid, "status").split("\n"))
            if (line.startsWith("Cpus_allowed_list:")) return line.substring(line.indexOf(':') + 1).trim();
        return null;
    }

    static Integer lastCpu(String tid) throws IOException {
        String stat = taskFile(tid, "stat");
        String[] f = stat.substring(stat.lastIndexOf(')') + 2).trim().split(" ");
        return Integer.valueOf(f[39 - 3]);  // field 39 of stat; the fields after ") " start at field 3
    }

    void checkPins() throws Exception {
        @SuppressWarnings("unchecked")
        Map<String, String> named = (Map<String, String>) summary.get("tids");
        Map<String, Object> seen = new LinkedHashMap<String, Object>();
        for (Map.Entry<String, String> e : named.entrySet()) {
            int want = e.getKey().equals("loop") ? o.loopCpu : o.posterCpu;
            String got = allowed(e.getValue());
            Integer last = lastCpu(e.getValue());
            Map<String, Object> t = new LinkedHashMap<String, Object>();
            t.put("tid", e.getValue());
            t.put("allowed", got);
            t.put("last_cpu", last);
            seen.put(e.getKey(), t);
            if (!String.valueOf(want).equals(got) || last == null || last.intValue() != want)
                problem("thread " + e.getKey() + " ended with allowed " + got + " and last CPU " + last + ", not " + want);
        }
        List<String> others = new ArrayList<String>();
        String[] tasks = new File("/proc/self/task").list();
        for (String t : tasks == null ? new String[0] : tasks) {
            if (named.containsValue(t)) continue;
            String got;
            try {
                got = allowed(t);
            } catch (IOException gone) {
                continue;  // the thread ended meanwhile
            }
            if (!String.valueOf(o.restCpu).equals(got)) others.add(t + ":" + got);
        }
        seen.put("others_not_on_rest_cpu", others);
        if (!others.isEmpty()) problem("threads not on CPU " + o.restCpu + ": " + others);
        summary.put("pins", seen);
    }

    // ---------------------------------------------------------------------------- warm-up and trials

    /** One back-to-back natural post from this thread: post, spin until it ran (section 3). */
    Task naturalPost() {
        Task t = new Task(null, null);
        t.submit = System.nanoTime();
        loop.execute(t);
        long limit = TimeUnit.MILLISECONDS.toNanos(o.limitMs);
        while (t.ran == 0) {
            if (System.nanoTime() - t.submit > limit) {
                lostWakes++;
                throw new IllegalStateException("a natural post did not run within " + o.limitMs + " ms");
            }
        }
        return t;
    }

    /** One forced post in its slot (section 3); returns once Y has run and the slot has ended. */
    Forced forcedPost() {
        final Forced p = new Forced();
        strategy.post = p;
        final ForcedStrategy s = strategy;
        Task x = new Task(null, new Runnable() {
            @Override public void run() { s.armed = true; }
        });
        p.x = x;
        long slotStart = System.nanoTime();
        x.submit = slotStart;
        loop.execute(x);
        long deadline = slotStart + TimeUnit.MILLISECONDS.toNanos(o.limitMs);
        while (p.y == null || p.y.ran == 0) {
            long left = deadline - System.nanoTime();
            if (left <= 0) {
                lostWakes++;
                throw new IllegalStateException("a forced post did not run within " + o.limitMs + " ms");
            }
            LockSupport.parkNanos(left);
        }
        long slotEnd = slotStart + TimeUnit.MILLISECONDS.toNanos(o.slotMs);
        for (long left; (left = slotEnd - System.nanoTime()) > 0; ) LockSupport.parkNanos(left);
        return p;
    }

    void warmUp() {
        long end = System.nanoTime() + TimeUnit.SECONDS.toNanos(o.warmupS);
        long posts = 0, overHalfB = 0;
        while (System.nanoTime() < end) {
            Task t = naturalPost();
            posts++;
            if (t.ran - t.submit > HALF_B_NS) overHalfB++;
        }
        summary.put("warmup_natural_posts", posts);
        summary.put("warmup_natural_over_half_b", o.check ? null : (Object) overHalfB);
        for (int i = 0; i < o.warmupPosts; i++) {
            if (strategy != null) forcedPost();
            else naturalPost();
        }
        summary.put("warmup_own_posts", o.warmupPosts);
    }

    void measure() {
        List<GarbageCollectorMXBean> gcs = ManagementFactory.getGarbageCollectorMXBeans();
        long uptime = ManagementFactory.getRuntimeMXBean().getUptime();
        summary.put("first_measured_uptime_ms", o.check ? null : (Object) uptime);
        long prevRun = 0;
        for (int trial = 0; trial < o.trials; trial++) {
            long gcCount0 = gcCount(gcs), gcTime0 = gcTime(gcs);
            for (int i = 0; i < o.posts; i++) {
                Map<String, Object> line = new LinkedHashMap<String, Object>();
                line.put("kind", "post");
                line.put("trial", trial);
                line.put("index", i);
                long delay;
                if (strategy != null) {
                    Forced p = forcedPost();
                    delay = p.y.ran - p.y.submit;
                    line.put("delay_ns", time(delay));
                    line.put("x_delay_ns", time(p.x.ran - p.x.submit));
                    line.put("wakenup_at_hook", p.wakenUpAtHook);
                    line.put("latch_ok", p.latchOk);
                    line.put("hook_held_ns", time(p.hookOut - p.hookIn));
                    line.put("y_after_hook_ns", time(p.y.ran - p.hookOut));
                } else {
                    Task t = naturalPost();
                    delay = t.ran - t.submit;
                    line.put("delay_ns", time(delay));
                    line.put("realized_gap_ns", prevRun == 0 ? null : time(t.submit - prevRun));
                    prevRun = t.ran;
                }
                maxDelay = Math.max(maxDelay, delay);
                lines.add(json(line));
            }
            Map<String, Object> t = new LinkedHashMap<String, Object>();
            t.put("kind", "trial");
            t.put("trial", trial);
            t.put("gc_count", gcCount(gcs) - gcCount0);
            t.put("gc_time_ms", o.check ? null : (Object) (gcTime(gcs) - gcTime0));
            lines.add(json(t));
        }
    }

    static long gcCount(List<GarbageCollectorMXBean> gcs) {
        long n = 0;
        for (GarbageCollectorMXBean b : gcs) n += Math.max(0, b.getCollectionCount());
        return n;
    }

    static long gcTime(List<GarbageCollectorMXBean> gcs) {
        long n = 0;
        for (GarbageCollectorMXBean b : gcs) n += Math.max(0, b.getCollectionTime());
        return n;
    }

    // ---------------------------------------------------------------------------- output

    static String readAll(InputStream in) throws IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        for (int n; (n = in.read(buf)) > 0; ) b.write(buf, 0, n);
        return new String(b.toByteArray(), StandardCharsets.UTF_8);
    }

    static String json(Object v) {
        if (v == null) return "null";
        if (v instanceof Boolean || v instanceof Number) return v.toString();
        if (v instanceof Map) {
            StringBuilder sb = new StringBuilder("{");
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (sb.length() > 1) sb.append(',');
                sb.append(json(String.valueOf(e.getKey()))).append(':').append(json(e.getValue()));
            }
            return sb.append('}').toString();
        }
        if (v instanceof List) {
            StringBuilder sb = new StringBuilder("[");
            for (Object e : (List<?>) v) {
                if (sb.length() > 1) sb.append(',');
                sb.append(json(e));
            }
            return sb.append(']').toString();
        }
        StringBuilder sb = new StringBuilder("\"");
        for (char c : v.toString().toCharArray()) {
            if (c == '"' || c == '\\') sb.append('\\').append(c);
            else if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
            else sb.append(c);
        }
        return sb.append('"').toString();
    }

    /** The calling thread's timer slack. /proc/self names the process's first thread, which only CAP_SYS_NICE may read. */
    static String slack() {
        try {
            return new String(Files.readAllBytes(Paths.get("/proc", tid(), "timerslack_ns")), StandardCharsets.US_ASCII).trim();
        } catch (IOException e) {
            return null;
        }
    }

    void finish(Throwable failure) throws IOException {
        if (failure != null) problem("run: " + failure);
        Map<String, String> slack = new LinkedHashMap<String, String>();
        slack.put("main", slack());
        slack.put("loop", loopSlack);
        summary.put("timerslack_ns", slack);
        summary.put("max_delay_ns", o.check ? null : (Object) maxDelay);
        summary.put("lost_wakes", lostWakes);
        summary.put("warn_records", WARN.count);
        summary.put("warn_messages", new ArrayList<String>(WARN.kept));
        if (WARN.count > 0) problem(WARN.count + " Netty log records at WARNING or above");
        summary.put("problems", new ArrayList<String>(PROBLEMS));
        summary.put("good", PROBLEMS.isEmpty());
        lines.add(json(summary));
        StringBuilder all = new StringBuilder();
        for (String l : lines) all.append(l).append('\n');
        Files.write(Paths.get(o.out), all.toString().getBytes(StandardCharsets.UTF_8));
    }

    public static void main(String[] args) throws Exception {
        Options o;
        try {
            o = parse(args);
        } catch (IllegalArgumentException e) {
            System.err.println("H6Probe: " + e.getMessage());
            System.exit(2);
            return;
        }
        H6Probe p = new H6Probe(o);
        Throwable failure = null;
        try {
            p.run();
        } catch (Throwable t) {
            failure = t;
        }
        p.finish(failure);
        if (p.group != null) p.group.shutdownGracefully(0, 1, TimeUnit.SECONDS);
        System.exit(PROBLEMS.isEmpty() ? 0 : 1);
    }
}
