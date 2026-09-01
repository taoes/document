---
title: "Java 21 虚拟线程：原理、调度与实践"
date: 2024-02-10
category:
  - Java
tag:
  - 虚拟线程
  - Java21
  - 并发
icon: code
---

# Java 21 虚拟线程：原理、调度与实践

在 Java 21 中，虚拟线程（Virtual Threads）作为 LTS 特性正式转正（JEP 444）。它承诺以"每个请求一个线程"的编程模型承载百万级并发，同时保持阻塞式代码的可读性。但虚拟线程不是银弹：它改变了线程的调度方式、栈的存储方式，也引入了 pinning 等一系列新问题。这篇文章不讲"虚拟线程让并发变得简单"这类正确的废话，而是深入 JVM 内部，把载体线程调度、栈存储、pinning 的成因与排查、以及真实生产中的调优参数讲透。

## 一、平台线程与虚拟线程：模型上的本质差异

要理解虚拟线程，必须先明确它与传统平台线程（Platform Thread）的差异。平台线程是操作系统线程（Linux 下的 pthread）的一对一包装，它的调度由内核完成，栈由内核分配，默认约 1MB，创建和切换的开销都相当可观。当你在一个高并发服务里为每个请求开一个平台线程时，瓶颈往往不是 CPU，而是线程数量达到几千后，线程栈的内存占用与内核调度的上下文切换成本。

虚拟线程则是一个由 JVM 自己管理的、运行在"载体线程"（Carrier Thread）之上的轻量级任务。它与 OS 线程是 M:N 的关系：成千上万个虚拟线程由少数几个平台线程（载体线程）执行。关键区别如下：

| 维度 | 平台线程 | 虚拟线程 |
| --- | --- | --- |
| 调度器 | 操作系统内核 | JDK 的 `ForkJoinPool`（FIFO 模式） |
| 栈存储 | 内核栈，固定大小（约 1MB） | 堆上的 `StackChunk` 对象，按需增长 |
| 创建成本 | 高（内核资源分配） | 极低（一个堆对象 + 一个调度任务） |
| 阻塞时 | 线程被挂起，载体无法复用 | 从载体线程卸载（unmount），载体继续执行其它虚拟线程 |
| 数量上限 | 受内存与内核限制，通常数千 | 可到百万级 |
| 可池化复用 | 是 | 否，池化是反模式 |

理解这张表是理解后续所有内容的前提。虚拟线程之所以能支持高并发，核心机制只有两条：**载体线程调度**与**栈的堆上存储**。下面分别展开。

## 二、载体线程调度：mount/unmount 与工作窃取

虚拟线程的生命周期可以抽象为"挂载（mount）—执行—卸载（unmount）"的循环。一个虚拟线程只有在被挂载到某个载体线程上时，才会真正占用 CPU 执行；一旦它执行了会阻塞的操作，JVM 就会把它从载体线程上卸载下来，让载体线程去跑另一个就绪的虚拟线程。

JDK 21 默认使用一个专门的 `ForkJoinPool` 来调度虚拟线程，其任务队列是 FIFO 模式的 `WorkQueue`，载体线程之间保留工作窃取（work-stealing）机制。这个调度器有两个关键参数，直接决定并发行为：

```bash
# 载体线程数量，默认 = Runtime.getRuntime().availableProcessors()
-Djdk.virtualThreadScheduler.parallelism=8

# 载体线程池的硬上限，默认 256（防止不受控扩张）
-Djdk.virtualThreadScheduler.maxPoolSize=256
```

一个容易被忽视的细节是：**`parallelism` 决定的是"同时执行虚拟线程"的载体数量，而非虚拟线程总数**。当你把 `parallelism` 设成 1 时，无论创建多少虚拟线程，同一时刻只有一个在跑。对于 CPU 密集型的虚拟线程任务，这个值设得比核数大毫无意义，甚至因为更多的上下文切换而更慢。

下面是一段可以观察调度行为的示例。注意 `Thread.ofVirtual()` 创建的是虚拟线程，而 `Thread.currentThread().isVirtual()` 用于在运行时确认身份：

```java
import java.time.Duration;
import java.util.concurrent.atomic.AtomicInteger;

public class CarrierDemo {
    public static void main(String[] args) throws InterruptedException {
        AtomicInteger counter = new AtomicInteger();

        Runnable task = () -> {
            var carrier = Thread.currentThread().getName(); // 载体线程名，形如 ForkJoinPool-1-worker-N
            System.out.printf("running on carrier: %s, virtual=%s%n",
                    carrier, Thread.currentThread().isVirtual());
            counter.incrementAndGet();
            try {
                Thread.sleep(Duration.ofMillis(200)); // 阻塞点，触发 unmount
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        };

        var threads = new Thread[100];
        for (int i = 0; i < threads.length; i++) {
            threads[i] = Thread.ofVirtual().name("vt-" + i).start(task);
        }
        for (var t : threads) t.join();

        System.out.println("tasks finished: " + counter.get());
    }
}
```

在这段代码里，100 个虚拟线程会在 `Thread.sleep` 处全部卸载，载体线程池用少数几个 worker 就能把它们依次跑完。真正要警惕的是**阻塞不彻底**的场景——即 pinning。

## 三、栈的存储：从固定栈到堆上栈块

虚拟线程没有预先分配的固定大小栈。它的栈帧被保存在堆上的 `StackChunk` 对象里，挂载到载体线程时，JVM 会把虚拟线程的栈帧"复制"到载体线程的栈上执行（实际通过 `Continuation` 的栈帧保存与恢复完成）；卸载时再保存回堆。每个 `StackChunk` 默认约 512KB，栈按需增长为链式的多个 chunk。

这套设计带来两个直接影响：

1. **不再有因固定栈大小导致的 `StackOverflowError`**，深递归只会让堆上的 chunk 链越来越长。代价是无限递归最终变成 `OutOfMemoryError`，排查时看到的现场从"栈溢出"变成了"堆打满"，容易误判为内存泄漏。

2. **`ThreadLocal` 成为高并发下的内存放大器**。虚拟线程数可达百万级，如果每个虚拟线程都往 `ThreadLocal` 里塞大对象，内存占用会被放得非常大，而且虚拟线程结束后 `ThreadLocal` 并不会自动清理。对虚拟线程而言，应当优先考虑 `ScopedValue`（Java 21 预览特性，随结构化并发一起演进），它绑定于调用作用域而非线程，天然规避了线程复用与泄漏问题：

```java
import java.util.concurrent.StructuredTaskScope;
import java.util.concurrent.StructuredTaskScope.Subtask;

public class StructuredConcurrencyDemo {
    public static void main(String[] args) throws Exception {
        // Java 21 中 StructuredTaskScope 为预览特性，需 --enable-preview
        try (var scope = new StructuredTaskScope.ShutdownOnFailure()) {
            Subtask<String> user = scope.fork(() -> fetch("user"));
            Subtask<String> order = scope.fork(() -> fetch("order"));

            scope.join().throwIfFailed();
            System.out.println(user.get() + " / " + order.get());
        }
    }

    static String fetch(String name) throws InterruptedException {
        Thread.sleep(100);
        return name + "-data";
    }
}
```

## 四、pinning：虚拟线程最大的"隐性吞吐杀手"

pinning（钉住）是指虚拟线程在**无法卸载**的情况下阻塞，导致载体线程被一起阻塞。JDK 21 中有两类典型触发条件：

1. **在 `synchronized` 块/方法内部执行阻塞操作**：`synchronized` 关键字锁的对象是 JVM 的监视器，JDK 21 的实现无法在持有监视器时安全卸载栈，因此虚拟线程会被"钉"在载体线程上。
2. **执行本地方法（JNI）或外部函数（FFM）**：本地代码对 JVM 栈结构一无所知，无法安全地保存/恢复，因此也会 pinning。

pinning 的可怕之处在于它**静默**：代码看起来是"阻塞式 I/O"，你以为虚拟线程会优雅地卸载，实际上它把宝贵的载体线程占死了。当所有载体线程都被钉住时，调度池退化为"一核一线程"，吞吐直接崩回平台线程时代的水平。

判断是否命中 pinning，先用 JDK 自带的追踪开关：

```bash
# short 只打印一条摘要；full 打印完整栈
java -Djdk.tracePinnedThreads=full -jar app.jar
```

输出形如：

```text
Thread[#29,ForkJoinPool-1-worker-1,5,CarrierThreads]
    java.base/java.lang.VirtualThread$VThreadContinuation.onPinned(VirtualThread.java:185)
    java.base/jdk.internal.vm.Continuation.onPinned(Continuation.java:372)
    ...
```

配合 `jcmd` 抓取线程转储，观察虚拟线程是否处于 `parked` 状态却仍占据载体线程：

```bash
jcmd <pid> Thread.dump_to_file -format=json thread-dump.json
```

修复策略按优先级排列：

- **优先用 `ReentrantLock` 替换 `synchronized`**。`ReentrantLock` 基于 AQS，使用 `LockSupport.park` 阻塞，虚拟线程可以正常卸载。
- 若必须用 `synchronized`，**把阻塞 I/O 移出临界区**，让临界区只做纯内存操作。
- 关注依赖库：数据库连接池、HTTP 客户端内部可能使用 `synchronized`，问题不一定出在你自己的代码里。
- 值得指出的是，JDK 24 通过 JEP 491 "Synchronize Virtual Threads without Pinning" 已从实现层面消除 `synchronized` 的 pinning，但 JNI/FFM 的 pinning 依然存在，生产环境升级前仍需评估。

## 五、适用与不适用场景：别把虚拟线程当万能药

虚拟线程解决的是**"线程数量大、单个任务大部分时间在等待 I/O"**的问题。它不改变 CPU 的物理限制，也不改变任务本身的执行量。判断是否引入虚拟线程，先回答两个问题：并发任务是否真的海量？任务的阻塞占比是否足够高？

**适合的场景**：

- 高并发 I/O 密集服务：网关、RPC、HTTP 服务、消息消费，每个请求/消息一个虚拟线程。
- 需要保持阻塞式代码风格（同步调用下游、同步读数据库）却又想撑起高并发的既有系统改造。
- 大量短生命周期的并发任务，如批量调用外部接口、扇出（fan-out）聚合。

**不适合的场景**：

- CPU 密集型计算：虚拟线程不会让计算变快，反而引入调度开销，此时应使用固定大小的平台线程池贴合核数。
- 需要**固定常驻线程**语义的场景：如依赖 `ThreadLocal` 传递上下文、依赖线程优先级/线程组的遗留框架。
- 需要精确控制并发上限的场景：虚拟线程本身不提供背压，必须自己加 `Semaphore` 或结构化并发来限流，否则可能瞬间创建几十万个虚拟线程打爆下游。

一个典型的"自找麻烦"示例——盲目放开并发导致下游被打垮：

```java
import java.util.concurrent.Executors;
import java.util.concurrent.Semaphore;

public class BoundedFanout {
    public static void main(String[] args) {
        int permits = 64; // 对下游的并发上限
        var semaphore = new Semaphore(permits);
        try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            for (int i = 0; i < 100_000; i++) {
                final int id = i;
                executor.submit(() -> {
                    try {
                        semaphore.acquire();
                        callDownstream(id);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    } finally {
                        semaphore.release();
                    }
                });
            }
        }
    }

    static void callDownstream(int id) throws InterruptedException {
        Thread.sleep(50); // 模拟下游调用
    }
}
```

## 六、生产排查与调优清单

把前面散落各节的要点收敛成可直接执行的清单：

1. **载体线程数量**：I/O 密集场景默认的 `availableProcessors()` 通常够用；若观测到载体线程大量空闲却吞吐上不去，优先查 pinning，而不是盲目调大 `parallelism`。
2. **pinning 排查**：上线灰度阶段打开 `-Djdk.tracePinnedThreads=full` 抓证据，重点看 `synchronized` 包裹阻塞 I/O 的代码与三方库。
3. **内存防护**：警惕百万级虚拟线程 + `ThreadLocal` 的组合；用 `ScopedValue` 替代跨调用传递的 `ThreadLocal`。
4. **限流兜底**：`newVirtualThreadPerTaskExecutor()` 不设上限，必须用 `Semaphore` 或 `StructuredTaskScope` 做背压。
5. **池化反模式**：不要用虚拟线程做"线程池复用"，虚拟线程本身就是按任务即时创建、用完即弃的。
6. **不要等`join()`空转**：批量任务用结构化并发或 `ExecutorService` 提交后统一收集，避免主线程忙等。

## 小结与建议

- 虚拟线程的本质是 **M:N 调度 + 堆上栈**，理解 mount/unmount 与 `StackChunk` 是掌握它的关键。
- pinning 是生产中最隐蔽的性能陷阱，JDK 21 下 `synchronized` 与 JNI/FFM 都会触发，用追踪参数定位，用 `ReentrantLock` 或移出阻塞 I/O 修复。
- 虚拟线程适合 **I/O 密集 + 海量并发**，不适合 CPU 密集、依赖固定线程语义、需要强背压控制而不自加限流的场景。
- 上线前先解决调度参数、pinning、`ThreadLocal` 放大、无界并发这四个问题，再谈百万级并发。
