---
title: "深入 AQS：构建高性能并发工具的地基"
date: 2023-04-08
category:
  - Java
tag:
  - 并发
  - AQS
  - 锁
icon: code
---

# 深入 AQS：构建高性能并发工具的地基

在 Java 并发编程的世界里，`ReentrantLock`、`Semaphore`、`CountDownLatch`、`ReentrantReadWriteLock`、`ThreadPoolExecutor` 的 `Worker`……这些我们几乎每天都在使用的工具，背后都站着一个共同的"地基"——**AbstractQueuedSynchronizer（AQS）**。它是 `java.util.concurrent`（JUC）包中最核心的抽象，没有之一。

很多工程师把 AQS 当作一个"黑盒"：会用锁，却说不清楚 `state` 是怎么流转的、线程竞争失败后去了哪里、为什么公平锁和非公平锁的吞吐量差那么多。本文尝试把这个黑盒拆开，从 CLH 队列、`state` 语义、独占与共享两种模式出发，最终手写一个基于 AQS 的自定义同步器，并落到真实生产环境的坑与调优思路上。

## 一、AQS 的设计内核：一个变量 + 一条队列

AQS 的本质可以浓缩成一句话：**用一个 `volatile int state` 表示同步状态，用一条 FIFO 队列管理获取锁失败的线程**。几乎所有同步语义，都可以被归约为对 `state` 的三种原子操作：

- `compareAndSetState`：CAS 更新状态，是加锁/释放的核心原语；
- `getState` / `setState`：读取、直接设置状态。

```java
// AbstractQueuedSynchronizer 中的核心字段
private transient volatile Node head;      // CLH 队列头
private transient volatile Node tail;      // CLH 队列尾
private volatile int state;                // 同步状态
```

这里需要澄清一个常见的误解：**AQS 的队列并不是教科书意义上的 CLH 锁队列的原样实现**。经典 CLH 锁中，每个线程自旋在自己前驱节点的标志位上；而 AQS 借鉴了 CLH 的"前驱节点"思想，但做了两处关键改造：

1. **用 `Node` 的双向链表 + 前驱引用替换了纯自旋**。后继线程通过 `prev/next` 指针串联，唤醒时通过 `LockSupport.unpark` 精确唤醒下一个节点，而不是让所有线程空转；
2. **`Node` 的状态标志更丰富**，除了 CLH 需要的 `SIGNAL`（-1，提示后继需要唤醒），还有 `CANCELLED`（1，取消）、`CONDITION`（-2，条件队列）、`PROPAGATE`（-3，共享模式传播）以及默认的 0。

用一张表对比两者的差异，能更直观地理解 AQS 为什么在工程上更优：

| 维度 | 经典 CLH 锁 | AQS 队列 |
| --- | --- | --- |
| 队列结构 | 隐式链表（前驱引用） | 显式双向链表 `Node` |
| 等待方式 | 自旋前驱的标志位 | `park/unpark` 阻塞唤醒 |
| CPU 开销 | 竞争激烈时自旋浪费 CPU | 阻塞等待，几乎零 CPU |
| 是否支持超时/中断 | 原生不支持 | 支持 `tryAcquireNanos`、可中断获取 |
| 状态表达 | 单一布尔标志 | `waitStatus` 四位状态语义 |
| 是否支持共享模式 | 否 | 独占 + 共享两种模式 |

这条改造让 AQS 既能支撑高吞吐的短临界区场景，也能在长等待场景下不白白烧 CPU。

## 二、state 与独占/共享两种模式

`state` 是 AQS 的"灵魂"。同一个 `int`，在不同同步器里被赋予完全不同的含义，这是 AQS 精妙设计的关键——它只负责"抢"这个动作，至于"抢什么、什么算抢到"，完全交给子类去定义。子类需要实现的核心模板方法如下：

| 方法 | 语义 | 典型实现者 |
| --- | --- | --- |
| `tryAcquire(int)` | 尝试独占获取 | `ReentrantLock.Sync` |
| `tryRelease(int)` | 尝试独占释放 | `ReentrantLock.Sync` |
| `tryAcquireShared(int)` | 尝试共享获取 | `Semaphore`、`CountDownLatch` |
| `tryReleaseShared(int)` | 尝试共享释放 | `Semaphore`、`CountDownLatch` |
| `isHeldExclusively()` | 是否被当前线程独占 | `ReentrantLock.Sync` |

**独占模式**下，`state` 的典型用法是"重入计数"。以 `ReentrantLock` 为例：

```java
protected final boolean tryAcquire(int acquires) {
    final Thread current = Thread.currentThread();
    int c = getState();
    if (c == 0) {
        // 无锁状态，CAS 抢占（非公平实现忽略排队）
        if (compareAndSetState(0, acquires)) {
            setExclusiveOwnerThread(current);
            return true;
        }
    } else if (current == getExclusiveOwnerThread()) {
        // 重入：同一线程再次获取，state 累加
        int nextc = c + acquires;
        if (nextc < 0) { // 溢出保护
            throw new Error("Maximum lock count exceeded");
        }
        setState(nextc);
        return true;
    }
    return false;
}
```

这里有一个非常隐蔽的生产坑：**重入计数的溢出**。`state` 是 `int`，`ReentrantLock` 的重入上限是 `Integer.MAX_VALUE`。理论上只有同一线程在未释放的前提下重入 21 亿次才会触发，但如果你用锁包裹了会"无限递归"或"循环重入"的逻辑，就可能在一个异常 bug 里迅速逼近该上限，最终抛出 `Error`（注意是 `Error` 而非 `Exception`，普通 `catch (Exception)` 抓不到）。排查时如果看到 `Maximum lock count exceeded`，基本可以断定是**重入失控**，而不是普通的死锁。

**共享模式**下，`state` 通常表示"可用资源数"。`Semaphore` 的 `tryAcquireShared` 会 CAS 扣减，`CountDownLatch` 的 `tryReleaseShared` 则在计数归零时统一放行所有等待线程：

```java
// CountDownLatch.Sync 的共享释放
protected boolean tryReleaseShared(int releases) {
    for (;;) {
        int c = getState();
        if (c == 0) {
            return false; // 已经归零，重复 countDown 无效
        }
        int nextc = c - 1;
        if (compareAndSetState(c, nextc)) {
            return nextc == 0; // 归零的那一刻返回 true，触发唤醒全部等待者
        }
    }
}
```

这个 `return nextc == 0` 是共享模式的精髓：只有当计数真正从 1 变为 0 的那一次 CAS 成功，才返回 `true`，进而触发 `doReleaseShared` 去唤醒队列里所有等待的线程。**只有最后一次 `countDown` 承担唤醒成本**，前面的 `countDown` 都是 O(1) 的 CAS，这是 `CountDownLatch` 高效的原因。

## 三、CLH 队列的入队与唤醒：一次完整的锁竞争全景

理解 AQS 必须理解一次"锁竞争失败 → 入队 → 被唤醒"的完整链路。以独占非公平锁为例：

```java
public final void acquire(int arg) {
    if (!tryAcquire(arg) &&
        acquireQueued(addWaiter(Node.EXCLUSIVE), arg)) {
        selfInterrupt(); // 补上被 park 吞掉的中断标志
    }
}
```

`acquire` 的骨架非常清晰：先快速尝试一次 `tryAcquire`（失败才入队），失败后 `addWaiter` 把当前线程包装成 `Node` 追加到队尾，然后进入 `acquireQueued` 自旋。这里有两个资深工程师必须记住的细节：

1. **`selfInterrupt()` 的补偿**。线程在队列里被 `park` 后，如果它收到的中断是"唤醒后再打补丁"而非"立即抛异常"，那么中断标志会被消费掉。`acquire` 结尾的 `selfInterrupt()` 就是把被吞掉的中断标志补回来，让调用方后续的 `isInterrupted()` 逻辑仍然正确。这是很多人在排查"锁获取期间中断丢失"问题的根因——不是中断丢了，而是 AQS 已经帮你补了一次，你需要在拿到锁后主动检查中断状态。

2. **`acquireQueued` 里的"二次机会"**。进入队列的节点不会立刻 `park`，而是先检查自己是不是头节点的直接后继，如果是就再 `tryAcquire` 一次，抢不到才真正阻塞：

```java
final boolean acquireQueued(final Node node, int arg) {
    boolean interrupted = false;
    for (;;) {
        final Node p = node.predecessor();
        if (p == head && tryAcquire(arg)) { // 头节点的后继才有资格抢
            setHead(node);
            p.next = null; // help GC
            return interrupted;
        }
        if (shouldParkAfterFailedAcquire(p, node) &&
            parkAndCheckInterrupt()) {
            interrupted = true;
        }
    }
}
```

这个"只有头节点后继才竞争"的规则，本质上决定了**公平锁的实现**：公平锁的 `tryAcquire` 会先 `hasQueuedPredecessors()` 判断队列里有没有更早的等待者，有就直接放弃竞争去排队；非公平锁则无视队列，先 CAS 抢一把再说。这就是两者吞吐差异的根源——非公平锁减少了线程上下文切换，代价是可能让新来的线程"插队"，导致等待线程延迟更高。

## 四、手写一个基于 AQS 的自定义同步器

纸上得来终觉浅。我们手写一个生产里真实存在的场景：**一个最多允许 N 个线程并发访问、且支持一次性排他独占（如全量重建/缓存预热）的资源闸门**。共享模式用 `state` 表示"剩余可用名额"，独占模式则把 `state` 全部拿走。

```java
import java.util.concurrent.locks.AbstractQueuedSynchronizer;

/**
 * 基于 AQS 的混合闸门：默认最多 N 个线程共享进入，
 * 支持一次性"独占排空"（例如重建缓存时，先排空所有读者，再让写者独占进入）。
 */
public class HybridGate {

    private final Sync sync;

    public HybridGate(int permits) {
        if (permits <= 0) {
            throw new IllegalArgumentException("permits must be positive");
        }
        this.sync = new Sync(permits);
    }

    /** 共享进入：占用一个名额，阻塞直到成功或被打断。 */
    public void acquireShared() throws InterruptedException {
        sync.acquireSharedInterruptibly(1);
    }

    /** 独占进入：排空所有名额后独占持有。 */
    public void acquireExclusive() throws InterruptedException {
        sync.acquireInterruptibly(1);
    }

    public void releaseShared() {
        sync.releaseShared(1);
    }

    public void releaseExclusive() {
        sync.release(1);
    }

    private static final class Sync extends AbstractQueuedSynchronizer {
        private final int maxPermits;

        Sync(int maxPermits) {
            this.maxPermits = maxPermits;
            setState(maxPermits);
        }

        @Override
        protected int tryAcquireShared(int acquires) {
            for (;;) {
                int c = getState();
                int remaining = c - acquires;
                // remaining < 0 说明名额不足，进入 AQS 队列等待
                if (remaining < 0 || compareAndSetState(c, remaining)) {
                    return remaining;
                }
            }
        }

        @Override
        protected boolean tryReleaseShared(int releases) {
            for (;;) {
                int c = getState();
                int nextc = c + releases;
                if (compareAndSetState(c, nextc)) {
                    return true;
                }
            }
        }

        @Override
        protected boolean tryAcquire(int acquires) {
            // 独占：只有在名额全空（state == maxPermits）时才允许一次性全部拿走
            return compareAndSetState(maxPermits, 0);
        }

        @Override
        protected boolean tryRelease(int releases) {
            setState(maxPermits); // 独占释放：恢复全部名额
            return true;
        }
    }
}
```

这个例子刻意展示了几点工程要点：

- **`tryAcquireShared` 返回负数的含义**：`remaining < 0` 时返回负值，AQS 会把当前线程挂到队列上等待；返回 0 表示恰好拿到最后一个名额（后继共享节点需等待），返回正数表示还有余量（可继续传播唤醒）。理解这个返回值语义是写共享同步器的第一关。
- **独占与共享的语义冲突必须由子类保证**。这里 `tryAcquire` 用 `compareAndSetState(maxPermits, 0)` 保证"只有名额全空才能独占进入"，否则独占请求会一直排队。如果你的业务允许"独占强制打断共享"，就需要引入一个额外的 `exclusiveOwnerThread` 或 `mode` 位，绝不能让共享和独占对 `state` 的读写语义互相打架——这正是 `ReentrantReadWriteLock` 用高 16 位记读锁、低 16 位记写锁来避免冲突的原因。

下面用一个 `yaml` 配置片段说明这类闸门在生产中的典型落点（连接池限流 + 缓存重建排他）：

```yaml
gate:
  sharedPermits: 8            # 最多 8 个线程并发读取
  hotReload:
    exclusiveTimeoutMs: 3000  # 独占排空等待上限，避免写者饿死
  metrics:
    queueDepth: true          # 观测 AQS 队列长度，评估是否上调 permits
```

## 五、生产环境的坑、调优与排查思路

**坑一：把 AQS 的队列长度当成"等待线程数"直接告警。** AQS 的 `getQueueLength()` 是估计值（遍历链表时队列可能正在变化），且它的增长并不等于"性能问题"——短临界区下队列短暂堆积是正常的。正确的告警应该结合"排队时长"和"锁竞争率"（如 `blockedTime` 占比）来判断。

**坑二：公平锁在流量毛刺下可能放大尾延迟。** 公平锁保证 FIFO，但每次获取都要检查前驱，且唤醒存在"惊群→逐个唤醒"的接力成本。在高并发但临界区极短（微秒级）的场景，非公平锁往往吞吐更高、尾延迟更稳。**不要迷信"公平"，要实测。** 只有当等待者之间有明显优先级诉求（如 VIP 请求先于普通请求）或需要严格防饥饿时才选公平锁。

**坑三：共享锁的 `PROPAGATE` 传播与中断的叠加。** 在 `acquireSharedInterruptibly` 场景下，如果线程在等待中被中断，会抛出 `InterruptedException` 并出队，但**名额并不会被多占**（因为 `tryAcquireShared` 的 CAS 未成功就没有扣减）。真正要小心的是你自行实现共享同步器时，在 `tryAcquireShared` 成功后再抛中断——那会导致"已扣减名额但线程以为没拿到"的泄漏。原则是：**扣减 state 与抛出异常必须互斥**。

**调优参数建议：**

| 参数 | 建议 | 说明 |
| --- | --- | --- |
| 锁粒度 | 尽量小 | 临界区只做必要的状态变更，重活移出锁外 |
| `state` 溢出 | 加保护 | 自研重入计数器务必检查 `nextc < 0` |
| 公平性 | 默认非公平 | 无明确优先级诉求时，非公平吞吐更优 |
| 等待策略 | 长等待用 `park` | 短临界区别自旋（`onSpinWait` 仅适用极短场景） |
| 观测指标 | 排队时长 + 竞争率 | 别只看队列长度，长度是瞬态估计值 |

**排查思路**：遇到锁相关线上故障，按"先分模式、再看状态、最后追队列"的顺序定位。① 确认是独占还是共享、公平还是非公平；② dump 出 `state` 的值，判断是"没人持锁但有人排队"（可能是释放逻辑漏了 `setExclusiveOwnerThread(null)`）还是"持锁线程卡死"（拿线程栈看它卡在哪）；③ 用 `jstack` 找 `AbstractQueuedSynchronizer$ConditionObject` 或 `acquireQueued` 栈帧，确认线程是在条件队列还是 CLH 队列上等待。绝大多数"锁没释放"的线上事故，最终都收敛到**重入计数不配对**或**异常路径漏释放**这两类。

## 小结与建议

- 把 AQS 理解为"`volatile int state` + CLH 变体队列"，抓准这两点，其余都是子类的语义演绎。
- 独占用 `tryAcquire/tryRelease`，共享用 `tryAcquireShared/tryReleaseShared`，两者对 `state` 的读写语义必须自洽，冲突时参考 `ReentrantReadWriteLock` 的分段技巧。
- `tryAcquireShared` 的返回值（负/零/正）直接决定是否入队与是否传播唤醒，是实现共享同步器的第一道坎。
- 记住 `acquire` 结尾的 `selfInterrupt()`，排查"锁获取期间中断丢失"时先想这一层。
- 默认用非公平锁换取吞吐，只有存在明确的优先级或防饥饿诉求时才上公平锁，并实测尾延迟。
- 生产监控看"排队时长 + 竞争率"而非队列长度；重入计数务必加溢出保护；异常路径务必保证 `state` 配平释放。

理解了 AQS，你就同时理解了 `ReentrantLock`、`Semaphore`、`CountDownLatch`、`ReentrantReadWriteLock` 乃至 `StampedLock` 之外那一片广袤的 JUC 工具箱——因为它们共享着同一套底层的心智模型。
