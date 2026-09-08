# CompletableFuture 原理 & 使用场景（Java面试高频）

> 背景：Java 8引入，实现 `Future` + `CompletionStage`。原生`Future`的痛点：get()阻塞，没有回调，多个异步任务编排很麻烦；CompletableFuture解决异步回调、多任务组合、异常处理。

  

## 一、核心原理

1. **实现两个接口**

- `Future<V>`：保留原有异步获取结果能力，get()阻塞拿结果

- `CompletionStage<V>`：定义大量链式、组合回调API，这是能力核心

  

2. **底层线程模型**

- 默认线程池：`ForkJoinPool.commonPool()`，**共享公共线程池**，所有CompletableFuture默认共用。

> ⚠️重要坑：commonPool适合CPU密集；如果是IO密集（RPC、DB调用），不要用默认池，要自定义线程池传入，否则公共池被IO阻塞，全局所有CompletableFuture全部受影响。

- 可以手动指定Executor：`supplyAsync(()->{}, executor)`

  

3. **两种任务创建方式**

- `supplyAsync()`：有返回值

- `runAsync()`：无返回值

  

4. **回调分两类：是否执行异步**

- `thenApply / thenAccept / thenRun`：**同步回调**，用上一步任务的线程执行回调；如果上一步已经完成，调用者线程执行回调。

- `thenApplyAsync / thenAcceptAsync / thenRunAsync`：**异步回调**，交给线程池执行。

  

> 面试高频坑：不带Async的方法不一定同步，要看上一步任务是否结束。

  

5. **任务组合API分类**

| API | 作用 |
|---|---|
| thenApply / thenApplyAsync | 拿到上一步结果，转换返回新结果（有入参，有返回） |
| thenAccept / thenAcceptAsync | 消费结果，无返回（有入参，void） |
| thenRun / thenRunAsync | 不关心结果，只等任务完成执行（无入参，void） |
| thenCompose | **任务嵌套，链式流水线**，接收返回Future的函数，扁平化，避免Future<Future<T>> |
| thenCombine | **两个独立任务都完成后，合并两者结果**，两个任务并行执行 |
| allOf | **等待所有任务全部完成**，不返回结果，get拿到void |
| anyOf | **任意一个任务完成就结束**，取第一个完成的结果 |

  

6. **异常处理**

- `exceptionally(throwable->{...})`：捕获异常，给兜底返回值；**正常执行不会进入**

- `whenComplete((res,ex)->{})`：无论成功失败都会执行，可以拿到结果或者异常，不修改返回值

- `handle((res,ex)->{...})`：无论成功失败，都可以拿到结果/异常，**可以返回新结果**，最强大。

  

> 区别：

> - exceptionally：只有异常才进；
> - whenComplete：只做回调，不能改返回值；
> - handle：成功、异常都进入，可以重写返回结果。

  

7. **完成状态**

CompletableFuture内部维护状态机：未完成、正常完成、异常完成、取消。

调用complete() / completeExceptionally()可以手动完成future，不依赖异步线程。

  

## 二、核心易错点（面试必问）

### 1. 默认ForkJoinPool公共池风险

> IO密集场景（HTTP调用、数据库查询）千万不要用默认supplyAsync。

原因：commonPool线程数有限（CPU核心数‑1），大量IO阻塞占满线程，其他CompletableFuture全部没有线程可用。

✅最佳实践：业务自定义ThreadPoolExecutor传入。

  

```java

//错误，IO任务用默认池

CompletableFuture.supplyAsync(()->httpClient.callApi());

  

//正确，传入自定义线程池

CompletableFuture.supplyAsync(()->httpClient.callApi(), customExecutor);

```

  

### 2. thenApply VS thenCompose 区别

- `thenApply`：函数返回普通对象，如果函数内部又开异步任务返回`CompletableFuture`，结果会包装成 `CompletableFuture<CompletableFuture<T>>` 嵌套，需要二次get；

- `thenCompose`：专门处理返回Future的场景，自动扁平化，消除嵌套。

  

```java

// thenApply 会嵌套

CompletableFuture<CompletableFuture<String>> f1 = future.thenApply(x -> asyncQuery(x));

  

// thenCompose扁平化，正确

CompletableFuture<String> f2 = future.thenCompose(x -> asyncQuery(x));

```

  

###3. thenCombine vs thenCompose

- thenCompose：**前后依赖**，第二个任务依赖第一个任务输出；串行。

- thenCombine：**两个任务互相独立，并行执行**，等两个都结束，再合并结果。

  

###4. allOf 不会收集返回值

`CompletableFuture.allOf(f1,f2,f3).get()` 只会等待全部结束，返回void；需要自己循环每个future拿结果。

  

```java

CompletableFuture<?>[] futures = ...;

CompletableFuture<Void> all = CompletableFuture.allOf(futures);

all.get();

// 之后循环每个future.get()拿各自结果

```

  

###5. anyOf：只要一个成功就返回，但是其他任务还在后台跑

anyOf不会中断剩下的任务，其他异步任务依旧在线程池执行，只是我们不再等待。如果需要取消，要手动调用cancel(true)。

  

###6. 异常容易吞掉

如果不调用get()、不配置exceptionally/handle；异步任务抛出异常，不会打印堆栈，只是把future标记异常完成，容易**静默失败，线上bug很难排查**。

> 生产建议：异步任务务必做异常兜底。

  

## 三、典型业务使用场景

1. **接口并行查询，提升接口响应速度**

> 场景：一个接口需要调用多个互不依赖的RPC/DB查询，串行总耗时=sum；并行后总耗时等于最慢的那一个任务耗时。

用`supplyAsync`开启多个任务，thenCombine / allOf等待全部完成，组装返回。

  

> 例子：查询用户信息、用户订单、用户标签三个独立查询，并行执行。

  

2. **异步任务流水线，任务有依赖关系**

> A任务输出作为B任务输入，B输出作为C输入，链式编排，thenCompose串联多个异步服务调用。

  

3. **多任务竞争，取最快返回（anyOf）**

> 多副本多数据源，多渠道查询，哪个先返回就用哪个结果。

  

4. **异步回调，不需要手动get阻塞**

原来Future需要轮询或者阻塞get；CompletableFuture可以注册回调，主线程不用阻塞。

  

5. **批量任务并行处理**

批量处理一批数据，拆分成多个异步子任务，allOf等待全部完成，再做结果聚合。

  

## 四、面试模拟题&参考回答

> Q：说下CompletableFuture和Future的区别？

**答**

1. Future只能阻塞get获取结果，没有回调；CompletableFuture支持回调，任务完成自动执行逻辑。

2. CompletableFuture提供强大任务编排API，可以串行、并行、等待任意/全部任务完成。

3. CompletableFuture完善异常处理，Future异常调用get才抛出。

4. CompletableFuture可以手动complete，手动设置结果。

  

> Q：thenApply、thenCompose、thenCombine区别？

**答**

1. thenApply：转换结果，函数返回普通对象；如果返回CompletableFuture，会产生嵌套Future。

2. thenCompose：用于上一步输出作为下一个异步任务入参，函数返回CompletableFuture，自动扁平化，用于串行依赖任务。

3. thenCombine：两个完全独立的CompletableFuture并行执行，两者都完成后，把两个结果传入回调合并输出。

  

> Q：业务IO场景，使用CompletableFuture需要注意什么？

**答**

1. IO密集不要使用ForkJoinPool.commonPool公共线程池，要自定义线程池，防止IO耗尽公共池线程，影响全局其他CompletableFuture。

2. 做好异常捕获handle/exceptionally，防止异常静默丢失。

3. allOf只等待完成，不会收集返回结果，需要手动遍历future拿结果。

4. anyOf只是不再等待，其他后台任务不会停止，必要时手动cancel。

5. 区分不带Async（复用已有线程）和带Async（交给线程池）回调。

  

如果你需要，我可以出一段高频手写代码题（并行多RPC调用，异常兜底）。