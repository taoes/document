---
title: "synchronized 与 ReentrantLock 的底层实现与锁优化"
date: 2022-07-30
category:
  - Java
tag:
  - 并发
  - 锁
  - AQS
icon: code
---

# synchronized 与 ReentrantLock 的底层实现与锁优化

Java 的并发编程里，`synchronized` 与 `ReentrantLock` 是两座绕不开的大山。很多人背得下"前者是隐式锁、后者是显式锁"的八股，却说不清它们背后到底发生了什么：偏向锁为什么在 JDK 15 被废弃？AQS 的 CLH 队列为什么并不真的是 CLH？非公平锁比公平锁快，快的到底是谁、快在哪一步？本文不重复手册，而是从字节码、对象头、AQS 源码和生产事故四个层面，把这两把锁的底层机制与优化路径讲透。

## 1. synchronized 到底是什么：先看字节码

`synchronized` 是 JVM 层面的语言内建机制，加锁与解锁由编译器和 JVM 协同完成，程序员拿不到锁对象。它在字节码层面表现为一对指令：`monitorenter` 与 `monitorexit`。

```java
public class SyncDemo {
    private final Object lock = new Object();
    private int count = 0;

    public void incr() {
        synchronized (lock) {
            count++;
        }
    }
}
```

用 `javap -c -v` 反编译后，方法体大致如下（节选）：

```bash
$ javap -c -v SyncDemo

public void incr();
    Code:
       0: aload_0
       1: getfield      #7    // Field lock:Ljava/lang/Object;
       4: dup
       5: astore_1
       6: monitorenter
       7: aload_0
       8: dup
       9: getfield      #3    // Field count:I
      12: iconst_1
      13: iadd
      14: putfield      #3
      17: aload_1
      18: monitorexit
      19: goto          27
      22: astore_2
      23: aload_1
      24: monitorexit
      25: aload_2
      26: athrow
```

注意一个细节：编译器为 `synchronized` 块生成了**两个** `monitorexit`。第一个对应正常退出（17~18），第二个对应异常路径（22~24）。这是 `synchronized` 的语义保证——无论方法体抛不抛异常，锁都一定会被释放。这一点在锁的实现上非常关键：它意味着 `synchronized` 的解锁是"自动且必然"的，不存在"忘了在 finally 里 unlock"这类人为错误。反观 `ReentrantLock`，释放锁完全是用户的义务，必须包裹在 `try/finally` 里。

`monitorenter` 这条指令的解释执行入口，最终会落到 `ObjectMonitor::enter`。理解它，就要先理解"锁"到底存在哪里——答案是对象的 Mark Word。

## 2. 对象头与 Mark Word：锁状态的物理载体

HotSpot 中，每个 Java 对象在堆里都有对象头（Object Header），其中最重要的是 Mark Word。在 64 位 JVM（未开启压缩指针时占 8 字节）中，Mark Word 是一个**多态复用**的位域结构，同一段内存在不同锁状态下表示完全不同的内容：

| 锁状态 | 标志位（后 2 bit） | 偏向标志（1 bit） | Mark Word 存储内容 |
| --- | --- | --- | --- |
| 无锁 | 01 | 0 | 对象的 hashCode、分代年龄（GC age） |
| 偏向锁 | 01 | 1 | 偏向线程 ID、epoch、分代年龄 |
| 轻量级锁 | 00 | — | 指向线程栈中 Lock Record 的指针 |
| 重量级锁 | 10 | — | 指向 ObjectMonitor 的指针 |
| GC 标记 | 11 | — | 空，供 CMS/GC 使用 |

理解这张表，是理解锁升级的前提：**锁状态不是凭空存在的一个开关，而是对同一块 Mark Word 的重新解释**。升级的本质，就是通过 CAS 把 Mark Word 从一种"形状"改写成另一种"形状"。这带来两个直接推论：

1. 如果对象的 `hashCode()` 已经被调用，Mark Word 里存了 hashCode，就**无法再进入偏向锁**（没地方放偏向线程 ID 了），只能直接从轻量级锁起步。
2. 偏向锁的偏向线程 ID 被写入后，若另一个线程想要它，需要一次"撤销偏向"（revoke），这个撤销需要到达安全点（Safepoint），代价并不便宜。

这也是为什么 JDK 15 通过 JEP 374 正式废弃并默认关闭了偏向锁：在现代多核、高竞争的工作负载下，偏向锁的撤销开销已经盖过了它省下的一次 CAS，反而成了负担。

## 3. 锁升级路径：偏向 → 轻量级 → 重量级

`synchronized` 的加锁是一个**只能升级、不能降级**的单调过程（HotSpot 官方描述里，升级后的锁一般不再降级，个别场景如批量重偏向除外）。理解每一级的"触发条件"和"代价"比背升级方向重要得多。

**偏向锁（Biased Locking）**：当一个对象第一次被线程加锁时，如果它满足偏向条件，Mark Word 会被 CAS 写入当前线程 ID，之后同一线程再次进入临界区，只需比对 Mark Word 的线程 ID 是否是自己——是，就直接进入，**零同步开销**。这针对的是"绝大多数情况下只有一个线程访问"的场景，例如 `StringBuffer`、`Vector` 这类历史遗留的同步容器，以及老式集合的迭代器。

**轻量级锁（Lightweight Locking）**：当出现第二个线程竞争但还**没有真正的阻塞等待**时，锁升级为轻量级锁。JVM 在抢锁线程的栈帧里分配一个 Lock Record，把对象当前的 Mark Word 拷贝进去（Displaced Mark Word），再用 CAS 把 Mark Word 替换成指向该 Lock Record 的指针。CAS 失败的线程进入**自旋**（spin），短时间原地忙等，期望锁很快释放。自旋是"用 CPU 换延迟"，适合临界区极短、竞争不激烈的场景。

**重量级锁（Heavyweight Locking）**：如果自旋超过阈值（JDK 6 之后是自适应自旋，JVM 根据历史自旋成功率动态决定），或者竞争线程数超过一定规模，锁膨胀为重量级锁，Mark Word 指向 `ObjectMonitor`。此时线程通过 `_cxq` / `_EntryList` 排队，未抢到锁的线程被 `park` 挂起，由内核态负责唤醒。

```java
// 演示锁升级的观测手段之一：JDK 8 下用 -XX:+PrintFlagsFinal 查看偏向锁相关参数
public class LockInflateDemo {
    private static final Object LOCK = new Object();

    public static void main(String[] args) throws Exception {
        // 第一段：单线程反复加锁，触发偏向
        for (int i = 0; i < 100_000; i++) {
            synchronized (LOCK) {
                // no-op
            }
        }
        // 第二段：引入第二个线程，制造竞争，触发撤销偏向与轻量级锁
        Thread t = new Thread(() -> {
            for (int i = 0; i < 100_000; i++) {
                synchronized (LOCK) {
                    // no-op
                }
            }
        });
        t.start();
        t.join();
    }
}
```

```bash
# JDK 8：查看偏向锁相关默认参数（JDK 15+ 已废弃，这些参数失效）
java -XX:+PrintFlagsFinal -version | grep -i biased
# 常见项：BiasedLockingStartupDelay=4000（启动延迟 4s 才启用偏向锁）
```

一个真实生产中的坑：**偏向锁的启动延迟**。老版本 JVM 默认 `-XX:BiasedLockingStartupDelay=4000`，即 JVM 启动 4 秒后才启用偏向锁。很多压测或短生命周期应用发现"怎么加不加 `-XX:-UseBiasedLocking` 性能没区别"，原因就是它们压根没活过这 4 秒，偏向锁从未生效。排查锁问题时，先确认 JVM 版本与这个参数，避免在错误的前提下做结论。

## 4. ReentrantLock 与 AQS：可重入与阻塞队列的实现

`ReentrantLock` 是 `java.util.concurrent.locks` 下基于 **AQS（AbstractQueuedSynchronizer）** 的显式锁。与 `synchronized` 依赖 JVM 内建不同，它的核心逻辑全在 Java 代码里，可读、可扩展。AQS 是一个"模板方法 + 状态机"框架：它维护一个 `volatile int state` 表示同步状态，一个 FIFO 等待队列管理阻塞线程，子类只需实现 `tryAcquire` / `tryRelease` 等钩子。

`ReentrantLock` 的可重入性，就是对这个 `state` 的计数：

```java
// ReentrantLock.Sync 的核心逻辑（简化）
final boolean tryAcquire(int acquires) {
    Thread current = Thread.currentThread();
    int c = getState();
    if (c == 0) {
        // 未加锁，尝试 CAS 抢锁（公平/非公平在此分叉）
        if (compareAndSetState(0, acquires)) {
            setExclusiveOwnerThread(current);
            return true;
        }
    } else if (current == getExclusiveOwnerThread()) {
        // 已加锁且是当前线程：state 累加，实现重入
        int nextc = c + acquires;
        if (nextc < 0) {
            throw new Error("Maximum lock count exceeded");
        }
        setState(nextc);
        return true;
    }
    return false;
}

protected final boolean tryRelease(int releases) {
    int c = getState() - releases;
    if (Thread.currentThread() != getExclusiveOwnerThread()) {
        throw new IllegalMonitorStateException();
    }
    boolean free = false;
    if (c == 0) {
        free = true;
        setExclusiveOwnerThread(null);
    }
    setState(c);
    return free;
}
```

这里有几个容易说错的点值得较真：

1. **`state` 不是布尔值，而是计数**。重入 3 次，`state` 就是 3，就必须 `unlock` 3 次才能真正释放。少一次，锁永不释放（泄漏）；多一次，抛 `IllegalMonitorStateException`（`tryRelease` 里 `getState() - releases` 变为负数时）。生产上最常见的 bug 就是 `lock()` 和 `unlock()` 不对称。

2. **AQS 的队列不是教科书 CLH 的照搬**。经典 CLH 是自旋在前驱节点上、靠 `prev` 链串起来的隐式队列；而 AQS 是它的一个变种，引入了 `next` 指针和 `waitStatus` 状态，配合 `LockSupport.park/unpark` 实现阻塞，而非纯自旋。所以"ReentrantLock 用 CLH 队列"这句话，准确的说法是"基于 CLH 变体的双向阻塞队列"。

3. **`unpark` 的"后继传播"**：节点释放锁时，会从队尾向前找最近的、`waitStatus` 为可唤醒的节点去 `unpark`。因为入队、取消（超时/中断）都可能打乱指针，代码里用大量 CAS 与状态判断保证并发正确性。这也是为什么读 AQS 源码时，`cancelAcquire` 和 `unparkSuccessor` 是最难啃、也最能体现工程功底的两段。

AQS 的另一半是 **Condition**：`synchronized` 配 `wait/notify`，`ReentrantLock` 配 `Condition.await/signal`。AQS 用一个**条件队列**（单向链表，区别于同步队列）存储因 `await` 而挂起的线程，`signal` 时把节点从条件队列转移到同步队列。与 `Object.wait` 相比，`Condition` 的杀手锏是**支持多个条件**：一个锁可以挂多个 `Condition`，实现"有界缓冲"这类精细的等待/通知，而 `Object` 只有一个隐式条件队列。

```java
// 可中断、可超时的加锁：synchronized 做不到的事
public class ReentrantLockFeatureDemo {
    private final ReentrantLock lock = new ReentrantLock();

    public void doWithTimeout() {
        boolean acquired = false;
        try {
            // 最多等 500ms，拿不到就走别的分支
            acquired = lock.tryLock(500, TimeUnit.MILLISECONDS);
            if (acquired) {
                // 临界区
            } else {
                // 降级逻辑：记日志、走限流、直接失败
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            // 被中断，恢复中断标记后退出
        } finally {
            if (acquired) {
                lock.unlock();
            }
        }
    }
}
```

## 5. 公平锁 vs 非公平锁：快的是谁、为什么

`ReentrantLock` 通过构造参数区分公平与非公平两种实现，差别只在 `tryAcquire` 的一处：

```java
// NonfairSync：先"插队"抢一把
final boolean tryAcquire(int acquires) {
    return nonfairTryAcquire(acquires);
}

final boolean nonfairTryAcquire(int acquires) {
    final Thread current = Thread.currentThread();
    int c = getState();
    if (c == 0) {
        if (compareAndSetState(0, acquires)) {   // 不看队列，直接 CAS
            setExclusiveOwnerThread(current);
            return true;
        }
    }
    // ... 重入判断同上
}

// FairSync：先看有没有人在排队
protected final boolean tryAcquire(int acquires) {
    final Thread current = Thread.currentThread();
    int c = getState();
    if (c == 0) {
        if (!hasQueuedPredecessors() &&           // 队列里没有更早的等待者
            compareAndSetState(0, acquires)) {
            setExclusiveOwnerThread(current);
            return true;
        }
    }
    // ... 重入判断同上
}
```

差异就在 `c == 0` 那一步：非公平锁**不看队列，直接 CAS 抢**，而公平锁先调用 `hasQueuedPredecessors()` 检查自己前面是否还有等待者。这就是"非公平"二字的全部含义。

**为什么非公平锁通常吞吐更高？** 关键在于减少上下文切换。设想一个刚释放锁、正准备 `unpark` 队列头部线程的瞬间，如果此时有一个新线程恰好到来：

- 公平锁：新线程老老实实排队，刚被唤醒的线程还要经历一次从内核态 park 状态恢复、重新抢占 CPU 的延迟，中间可能出现锁短暂空置；
- 非公平锁：新线程直接 CAS 成功，把"唤醒一个阻塞线程"这件昂贵的事省掉了——不用唤醒谁了，锁已经被新线程拿走。

也就是说，非公平锁省下的是**线程挂起/唤醒的上下文切换开销**，代价是可能让队列里的线程长时间饥饿。在吞吐优先、任务耗时接近的通用场景，非公平锁几乎是默认选择（`ReentrantLock` 默认、`synchronized` 在重量级锁下的行为也偏向非公平）。

```java
// 一个可复现的公平 vs 非公平吞吐对比（示意，数值因机器而异）
import java.util.concurrent.locks.ReentrantLock;

public class FairnessBenchmark {
    static final int THREADS = 8;
    static final int PER_THREAD = 1_000_000;

    static long run(boolean fair) throws InterruptedException {
        final ReentrantLock lock = new ReentrantLock(fair);
        final long[] counter = {0};
        Thread[] ts = new Thread[THREADS];
        long start = System.nanoTime();
        for (int i = 0; i < THREADS; i++) {
            ts[i] = new Thread(() -> {
                for (int j = 0; j < PER_THREAD; j++) {
                    lock.lock();
                    try { counter[0]++; } finally { lock.unlock(); }
                }
            });
            ts[i].start();
        }
        for (Thread t : ts) t.join();
        return System.nanoTime() - start;
    }

    public static void main(String[] args) throws InterruptedException {
        System.out.println("fair   : " + run(true) / 1_000_000 + " ms");
        System.out.println("nonfair: " + run(false) / 1_000_000 + " ms");
    }
}
```

建议遵循一个简单决策规则：**默认用非公平锁；只有当业务语义要求"先到先得"（例如公平的任务调度、需要严格避免饥饿的在线服务）时，才显式选择公平锁，并为此接受吞吐下降。**

## 6. 生产环境：怎么选、怎么调、怎么查

**选型对比**，先把两张表刻在脑子里：

| 维度 | synchronized | ReentrantLock |
| --- | --- | --- |
| 实现层 | JVM 内建，依赖 `monitorenter/monitorexit` 与对象头 | JDK 类库，基于 AQS |
| 释放方式 | 自动，异常也释放 | 手动，必须 `try/finally` |
| 可中断 | 不可中断，阻塞即死等 | `lockInterruptibly()` 可响应中断 |
| 可超时 | 不支持 | `tryLock(timeout)` |
| 公平性 | 无法指定（重量级下偏非公平） | 可选公平/非公平 |
| 条件队列 | 单一 `wait/notify` | 多个 `Condition` |
| 底层优化 | 偏向/轻量级/重量级自动升级 | 无偏向/轻量级，直接走 AQS |
| 可观测性 | 依赖 JVM 参数与工具 | 源码可读，状态可查 |

**何时选谁**：现代 JVM（尤其是 JDK 15+ 关闭偏向锁之后）里，`synchronized` 的优化已经足够好，**无特殊需求的普通互斥，优先用 `synchronized`**——代码更短、不会忘解锁、语义清晰。需要以下能力时再用 `ReentrantLock`：可中断、可超时、公平性要求、多个条件变量、或需要把锁与复杂的并发结构（如读写锁 `ReentrantReadWriteLock` 的读锁释放转写锁）组合。

**调优参数**（JDK 8 及更早）：

```bash
-XX:+UseBiasedLocking          # 开启偏向锁（JDK 15+ 移除）
-XX:-UseBiasedLocking          # 关闭偏向锁，适合竞争明显、短生命周期对象多的场景
-XX:BiasedLockingStartupDelay=0  # 去掉偏向锁启动延迟，压测前先归零对比
-XX:+UseSpinning               # 开启自旋（JDK 6 后默认开启，多核才有意义）
-XX:PreBlockSpin=10            # 自旋次数（自适应自旋下仅作参考）
-XX:-UseBiasedLocking -XX:+UseHeavyMonitors  # 极端：直接重量级，用于定位
```

**排查思路**，线上遇到锁竞争导致的吞吐下降或长尾，按这个顺序走：

1. **先定位热点锁**：`jstack` 连续抓几次线程栈，观察哪些线程长期 `BLOCKED` 在同一个 `- waiting to lock <0x...>` 上；或 `jstack -l` 看锁的持有者（owner）。
2. **看争用指标**：`ReentrantLock` 的 `getQueueLength()`、`isLocked()` 可以做成监控；`synchronized` 侧用 JFR（Java Flight Recorder）的 Java Monitor Blocked 事件统计阻塞次数与时长。
3. **判断是"锁粒度过粗"还是"持锁时间过长"**：前者（锁保护范围太大、无关代码也进临界区）靠拆分锁、缩小临界区、锁分段（类似 `ConcurrentHashMap` 的思路）解决；后者（临界区内有 IO、网络、慢查询）靠把慢操作移出临界区、异步化、批量化解决。
4. **必要时的原子替代**：如果是简单计数、CAS 单变量更新，先评估能否用 `AtomicInteger` / `LongAdder` 这类无锁结构替代，而不是一上来加锁。
5. **别在锁里做日志和监控采样**：这是真实事故高发点——一个看似无害的 `log.info` 或指标埋点，在持锁期间做了同步写盘或网络上报，能把临界区时间放大几个数量级。

一个印象深刻的线上案例：某网关用全局 `ReentrantLock` 保护一个"读多写少"的路由表，高峰时锁竞争把 P99 打到秒级。排查发现真正需要互斥的只是路由表的"热更新"那一步，而大量读请求完全可以走无锁的 volatile 快照。最终把写锁降为 `ReentrantReadWriteLock`，再进一步改成"写时复制 + volatile 引用替换"，P99 直接降了一个数量级。教训是：**锁是手段不是目的，减少共享可变状态，往往比优化锁本身更彻底。**

## 7. 小结与建议

- `synchronized` 的锁状态存在对象 Mark Word 里，偏向 → 轻量级 → 重量级是不可逆的升级路径；偏向锁已随 JDK 15 废弃，别再用旧文里的结论套新 JVM。
- `ReentrantLock` 的可重入 = AQS `state` 计数；`lock/unlock` 必须严格对称，多一次抛异常、少一次锁泄漏。
- AQS 是 CLH 的"阻塞变体"，靠 `volatile state` + `LockSupport.park/unpark` 实现，理解 `tryAcquire/tryRelease` 与 `unparkSuccessor/cancelAcquire` 就能抓住主干。
- 公平锁与非公平锁的唯一实质差异是"抢锁前是否检查 `hasQueuedPredecessors()`"；默认非公平，吞吐优先。
- 无特殊需求优先 `synchronized`；需要可中断、可超时、多条件、公平性时选 `ReentrantLock`。
- 排查锁问题先 `jstack`/JFR 定位热点，再区分"粒度粗"还是"持锁久"；能无锁化就不要加锁，能缩小临界区就不要扩大。
