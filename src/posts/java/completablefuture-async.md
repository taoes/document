---
title: "CompletableFuture 异步编程实战与线程池治理"
date: 2022-05-28
category:
  - Java
tag:
  - 并发
  - 异步
  - 线程池
icon: code
---

# CompletableFuture 异步编程实战与线程池治理

CompletableFuture 自 Java 8 引入以来，几乎成了 Java 异步编程的事实标准。但"会用"和"用对"之间隔着一条巨大的鸿沟：串并联编排失控导致的响应时间劣化、异常被静默吞掉导致的偶发丢单、超时不生效导致的线程堆积、以及默认 ForkJoinPool 在 IO 密集型场景下的集体翻车，都是生产环境里反复出现的真实事故。本文不重复"CompletableFuture 是什么"的入门教程，而是从工程落地视角，拆解编排、异常、超时、线程池隔离四类高频问题，并给出可直接落地的代码与调参建议。

## 一、为什么你的 CompletableFuture 变慢了：先看清默认线程池

最容易踩的第一个坑，是**误以为 CompletableFuture 自带一个"够用"的线程池**。实际上，Java 8 中所有不带 `Executor` 参数的异步方法（如 `supplyAsync`、`runAsync`、`thenApplyAsync` 等），默认都落在 `ForkJoinPool.commonPool()` 上。这个公共池有两个致命特性：

1. **并行度固定为 `CPU 核数 - 1`**，并且是 JVM 全局共享的。
2. **它是为 CPU 密集型的分治计算设计的**，工作线程采用 work-stealing，阻塞一个线程的代价远高于普通线程池。

当你的业务是 IO 密集型（远程调用、数据库、消息队列）时，公共池里的线程会大量阻塞在等待网络返回上，很快把寥寥几个 worker 占满。结果就是：上游流量一大，全 JVM 所有依赖 commonPool 的异步任务互相拖累，响应时间指数级恶化，而且**没有任何队列可以帮你缓冲或拒绝**——`commonPool` 的并行度是静态的，无法在线程池层面扩容。

下面的代码能直观暴露这个问题：用 commonPool 跑一堆 sleep 模拟的 IO 任务，测量吞吐。

```java
public class CommonPoolDemo {
    public static void main(String[] args) {
        int cpu = Runtime.getRuntime().availableProcessors();
        System.out.println("CPU cores = " + cpu);
        System.out.println("commonPool parallelism = " + ForkJoinPool.commonPool().getParallelism());

        long start = System.currentTimeMillis();
        List<CompletableFuture<Void>> futures = new ArrayList<>();
        for (int i = 0; i < 50; i++) {
            // 不传 Executor，默认落在 commonPool
            futures.add(CompletableFuture.runAsync(() -> {
                try { Thread.sleep(1000); } catch (InterruptedException ignored) {}
            }));
        }
        CompletableFuture.allOf(futures.toArray(new CompletableFuture[0])).join();
        System.out.println("elapsed = " + (System.currentTimeMillis() - start) + " ms");
    }
}
```

在 8 核机器上，commonPool 并行度通常是 7。50 个各阻塞 1 秒的任务，理想情况下配一个足够大的池子 1 秒出头就能跑完，而 commonPool 需要排队跑约 8 批，耗时接近 8 秒。**结论先行：所有 IO 型异步任务，一律显式传入自定义线程池，绝不让它落到 commonPool。**

## 二、串并联编排：把依赖关系建模成"图"，而不是"链"

CompletableFuture 的核心价值在于用声明式 API 描述任务依赖，但很多人把它写成了长链式调用，既难读又容易引入隐式阻塞。工程上的正确姿势是：**先画出任务之间的依赖图，再决定用串行、并行还是组合。**

### 2.1 串行：thenApply / thenCompose 的区别

`thenApply` 用于同步转换（普通函数），`thenCompose` 用于链接下一个异步操作（返回 `CompletionStage` 的函数）。二者混用是常见的编译期"意外"来源——`thenCompose` 若被误写成 `thenApply`，会得到一个嵌套的 `CompletableFuture<CompletableFuture<T>>`。

```java
CompletableFuture<String> order = fetchOrder(orderId);            // 异步查订单

// 错误：嵌套 Future
CompletableFuture<CompletableFuture<User>> nested = order.thenApply(o -> fetchUser(o.getUserId()));
// 正确：扁平化
CompletableFuture<User> user = order.thenCompose(o -> fetchUser(o.getUserId()));
```

### 2.2 并行：thenCombine / allOf 的语义差异

多个互不依赖的任务应**并行触发**，而不是顺序 `join`。注意 `allOf` 只返回 `Void`，拿结果需要再从各自的 future 上取；`thenCombine` 则适用于"两个结果合成一个"的场景。

```java
CompletableFuture<Order> orderF = fetchOrder(orderId);
CompletableFuture<User> userF = fetchUser(userId);
CompletableFuture<List<Coupon>> couponF = fetchCoupons(userId);

// 三路并行，全部完成后组装
CompletableFuture<OrderContext> ctxF = CompletableFuture
        .allOf(orderF, userF, couponF)
        .thenApply(v -> new OrderContext(
                orderF.join(),      // allOf 完成后 join 不会阻塞
                userF.join(),
                couponF.join()));
```

一个可操作建议是：**为跨服务编排建立统一的 `AsyncContext`/聚合模型**，把所有中间结果收口到一个不可变对象里，避免散落的局部变量和难以追踪的 join 点。

### 2.3 编排对比速查

| 方法 | 语义 | 入参/返回值 | 典型场景 |
| --- | --- | --- | --- |
| `thenApply` | 同步转换 | `Function<T,U>` → `U` | 纯计算、字段映射 |
| `thenCompose` | 链式异步 | `Function<T,Stage<U>>` → `U` | 依赖前一步的二次调用 |
| `thenCombine` | 双结果合并 | `BiFunction<T,U,V>` → `V` | 两路并行后聚合 |
| `thenAcceptBoth` | 双结果消费 | `BiConsumer` → `Void` | 落库、发消息等副作用 |
| `allOf` | 多路汇合 | `Stage...` → `Void` | 三路及以上并行 |
| `anyOf` | 任意完成 | `Stage...` → `Object` | 竞速、降级兜底 |

## 三、异常处理：别让 CompletableFuture 把异常"藏"起来

CompletableFuture 的异常是**存储在 future 内部、由依赖它的下游阶段传播**的，而不是像同步代码那样立即抛出。这带来两个高频事故：

- **只调 `join` 不调 `get`，且不捕获**：`join` 会把异常包装成 `CompletionException` 抛出，但很多人对 `allOf(...).join()` 不做 try/catch，导致上游异常让整条链路静默失败，日志里只有一句笼统的堆栈，定位困难。
- **主流程无人 `join`**：如果创建了一堆 future 却没有任何地方等待或消费结果，异常会永远沉寂，任务"看起来成功"了。

### 3.1 用 exceptionally / handle 显式兜底

`exceptionally` 只处理异常分支，`handle` 则同时处理正常与异常两条路径（类似 `finally` + 返回值映射）。生产环境建议**在每个服务调用的叶子节点就做降级**，而不是只在最外层兜底，这样能保留足够的上下文。

```java
CompletableFuture<OrderContext> ctxF = CompletableFuture
        .allOf(orderF, userF, couponF)
        .handle((v, ex) -> {
            if (ex != null) {
                log.error("assemble order context failed, orderId={}", orderId, ex);
                return fallbackContext(orderId);   // 降级：返回缓存或默认值
            }
            return new OrderContext(orderF.join(), userF.join(), couponF.join());
        });
```

### 3.2 为什么日志里总是 CompletionException

`CompletionException` 会包裹真正的业务异常，排查时务必 `ex.getCause()` 才能看到根因。推荐一个统一的异常拆解工具：

```java
static Throwable unwrap(Throwable t) {
    Throwable cur = t;
    while ((cur instanceof CompletionException || cur instanceof ExecutionException)
            && cur.getCause() != null) {
        cur = cur.getCause();
    }
    return cur;
}
```

### 3.3 whenComplete 的"后置"语义陷阱

`whenComplete` 的返回值还是**原来的 future**，它不改变结果，也不拦截异常向下游的传播——它只是给你一个"旁观"的机会。若想真正拦截并替换异常，必须用 `handle` 或 `exceptionally`，这是面试和实际排查中都反复出现的混淆点。

## 四、超时控制：orTimeout 与超时后的资源回收

Java 9 提供了 `orTimeout` 和 `completeOnTimeout`，让超时不再依赖 `get(timeout)` 那套笨拙写法。但**超时只是让调用方不再等待，底层任务并不会因此被取消**——如果底层是 RPC，连接和线程依然占用，这是超时治理里最容易被忽略的泄漏点。

```java
CompletableFuture<Order> orderF = fetchOrder(orderId)
        .orTimeout(1500, TimeUnit.MILLISECONDS)   // 超时后抛出 TimeoutException
        .exceptionally(ex -> {
            if (unwrap(ex) instanceof TimeoutException) {
                log.warn("fetchOrder timeout, orderId={}", orderId);
                return queryLocalCache(orderId);  // 降级到本地缓存
            }
            throw new CompletionException(ex);
        });
```

几个工程要点：

1. **超时阈值要分层**：数据库、缓存、下游 RPC、外部第三方服务，超时时间应逐级递增，避免"一个慢依赖拖垮整个聚合"。
2. **配套熔断与限流**：`orTimeout` 只是单次调用的超时，无法替代熔断器（如 Resilience4j/Sentinel）。超时后仍应上报熔断指标，让熔断器感知下游劣化。
3. **关注线程占用而非 CPU**：超时后线程还在等 IO，真正该监控的是**线程池活跃线程数、队列长度和排队等待时间**，而非 CPU 使用率。
4. **Java 8 兼容方案**：若无法升级到 Java 9+，可用 `CompletableFuture.anyOf(future, timeoutFuture())` 模拟，其中 `timeoutFuture` 由一个定时线程池驱动。

```java
// Java 8 下的超时模拟
static <T> CompletableFuture<T> withTimeout(CompletableFuture<T> f, long ms, ScheduledExecutorService scheduler) {
    CompletableFuture<T> timeout = new CompletableFuture<>();
    scheduler.schedule(() -> timeout.completeExceptionally(new TimeoutException()), ms, TimeUnit.MILLISECONDS);
    return (CompletableFuture<T>) CompletableFuture.anyOf(f, timeout);
}
```

## 五、线程池隔离：给每类任务一条独立的"泳道"

线程池治理的核心理念是**隔离与命名**：不同风险等级、不同延迟特征的任务，必须使用不同的线程池，否则一个慢任务就能通过共享队列"挤占"所有资源，产生连锁故障（雪崩）。

### 5.1 按业务域与风险等级隔离

推荐至少划分三层：

```java
public final class AsyncPools {
    // CPU 密集型：并行度贴近核数，队列较小，拒绝策略用 CallerRuns 防饥饿
    public static final Executor CPU = new ThreadPoolExecutor(
            Runtime.getRuntime().availableProcessors(),
            Runtime.getRuntime().availableProcessors(),
            60, TimeUnit.SECONDS,
            new ArrayBlockingQueue<>(1000),
            new NamedThreadFactory("async-cpu"),
            new ThreadPoolExecutor.CallerRunsPolicy());

    // IO 密集型：核心线程多一点，队列有界，队列满时温和拒绝并上报
    public static final Executor IO = new ThreadPoolExecutor(
            32, 64,
            60, TimeUnit.SECONDS,
            new ArrayBlockingQueue<>(2000),
            new NamedThreadFactory("async-io"),
            new ThreadPoolExecutor.AbortPolicy());

    // 第三方外部调用：单独池子，慢依赖不拖垮核心链路
    public static final Executor EXTERNAL = new ThreadPoolExecutor(
            8, 16,
            30, TimeUnit.SECONDS,
            new ArrayBlockingQueue<>(500),
            new NamedThreadFactory("async-external"),
            new ThreadPoolExecutor.AbortPolicy());
}
```

使用自定义线程池后，所有异步方法都必须显式传入 `Executor`：

```java
CompletableFuture.supplyAsync(() -> queryDb(orderId), AsyncPools.IO)
        .thenApplyAsync(OrderContext::compute, AsyncPools.CPU)
        .orTimeout(2, TimeUnit.SECONDS);
```

### 5.2 线程池调参的实战参数

| 参数 | 建议取值 | 理由 |
| --- | --- | --- |
| 核心/最大线程数 | IO 型按 `核数 × 2` 起，实测压测调优 | IO 等待率高，需更多线程掩盖延迟 |
| 队列 | **必须用有界队列** | 无界队列在流量洪峰下会无限堆积，最终 OOM |
| 拒绝策略 | `AbortPolicy` + 告警，或 `CallerRunsPolicy` | 显式失败优于静默排队，便于监控与限流兜底 |
| 线程工厂 | 自定义命名 | 线程名带业务前缀，堆栈定位直接命中问题池 |
| 空闲回收 | `allowCoreThreadTimeOut` 视场景开启 | 低峰期释放资源，避免空转线程占用 |

### 5.3 关键监控指标

治理离不开观测，线程池至少要暴露以下指标到监控大盘：

- 活跃线程数 / 最大线程数（池是否打满）
- 队列长度与队列等待时间（任务是否在排队）
- 拒绝次数（`AbortPolicy` 触发频率）
- 任务平均/ P99 执行耗时

实践中可以用 Micrometer 的 `ExecutorServiceMetrics` 一行接入，或自行扩展 `ThreadPoolExecutor` 覆写 `beforeExecute`/`afterExecute` 埋点。

## 小结与建议

- **永远不要**让 IO 型异步任务使用默认 `ForkJoinPool.commonPool()`，显式传入隔离的线程池。
- 编排先画依赖图：串行用 `thenCompose`，并行用 `thenCombine`/`allOf`，避免链式嵌套和隐式阻塞。
- 异常用 `handle`/`exceptionally` 在叶子节点就近兜底，排查时记得 `unwrap` 拆掉 `CompletionException`。
- 超时用 `orTimeout`（Java 9+）并配套熔断与降级；理解"超时不等于取消底层任务"，关注线程占用泄漏。
- 线程池按风险等级隔离、队列必有界、线程必命名、拒绝必告警，并把活跃线程/队列长度/拒绝数纳入监控。

只有把编排、异常、超时、线程池治理这四件事当成一个整体来设计，CompletableFuture 才能真正成为稳定异步系统的基石，而不是埋在生产环境里的一颗定时炸弹。
