---
title: "asyncio 事件循环与高性能网络编程"
date: 2025-02-15
category:
  - Python
tag:
  - asyncio
  - 异步
  - 事件循环
icon: terminal
---

# asyncio 事件循环与高性能网络编程

写异步 Python 的人很多，真正把事件循环、Future/Task、async/await 的底层机制搞清楚的人却不多。多数人停留在「加个 `async def`、用 `await` 调用」的层面，一旦遇到「为什么我的协程没并发」「为什么 Task 泄漏」「为什么 uvloop 反而更慢」这类问题就束手无策。本文从事件循环的调度本质讲起，一直讲到 uvloop 与 aiohttp 在生产环境的高并发实践，力求把每个结论都落到可运行、可验证的代码与参数上。

## 一、事件循环：单线程里的「协作式调度器」

asyncio 的核心是一个**事件循环（Event Loop）**。它本质上是一个运行在单线程里的协作式调度器，通过 `select`/`epoll`/`kqueue` 等 I/O 多路复用机制，在一个线程内同时「监听」成千上万个 socket。

这里有一个必须先纠正的误区：**异步不等于多线程**。事件循环不会给每个请求开一个线程，而是把「等待 I/O 的这段时间」交还给调度器，让调度器去处理其它已经就绪的任务。换句话说，协程的并发是「I/O 等待期间的并发」，而不是 CPU 计算的并发。

看一个最小的、不依赖任何框架的事件循环调用：

```python
import asyncio


async def fetch(url: str):
    print(f"开始请求 {url}")
    await asyncio.sleep(0.1)  # 模拟 I/O 等待
    print(f"完成请求 {url}")
    return url


async def main():
    # gather 会并发调度多个协程，而不是串行
    results = await asyncio.gather(fetch("a"), fetch("b"), fetch("c"))
    print(results)


asyncio.run(main())
```

上面三个 `fetch` 总耗时约 0.1 秒而不是 0.3 秒，因为 `asyncio.sleep(0.1)` 让出了控制权。理解这一点，就理解了 asyncio 性能优势的边界：**它擅长 I/O 密集，不擅长 CPU 密集**。如果协程里放了 `time.sleep(0.1)`（阻塞式睡眠）或者一段纯计算的循环，整个事件循环会被卡死，其它协程全部饿死。这是新手最常见的坑：在异步代码里混入了阻塞调用。

## 二、async/await 与 Future/Task 的关系

`async def` 定义的是一个**协程函数（coroutine function）**，调用它返回的是**协程对象（coroutine object）**，此时函数体**尚未执行**。只有把协程对象交给事件循环（如 `asyncio.run`、`create_task`、`gather`）后，它才会被调度执行。这就是为什么新手经常看到 `RuntimeWarning: coroutine 'xxx' was never awaited`——他创建了协程对象却忘记去 `await` 或调度它。

再往下一层，`await` 一个协程时，asyncio 内部会把它包装成一个 `Task`。Task 是 `Future` 的子类，两者的区别至关重要：

| 对象 | 谁来驱动 | 是否可被调度 | 典型用途 |
| --- | --- | --- | --- |
| `Coroutine` | 需要被 `await` 或交给事件循环 | 否，惰性的 | `await fetch()` 内部 |
| `Future` | 事件循环在结果就绪时 `set_result` | 是（低层句柄） | 表示「将来某个时刻的值」 |
| `Task` | 事件循环自动驱动协程直到完成 | 是 | `create_task` 后后台并发 |

一句话概括：**Future 是「结果的占位符」，Task 是「被调度起来的协程」**。你可以给一个裸的 `Future` 手动设置结果，这是很多同步库封装成异步库的桥梁：

```python
import asyncio


async def main():
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    # 模拟某个回调线程在稍后填充结果
    loop.call_later(0.5, fut.set_result, 42)

    result = await fut
    print(result)  # 42


asyncio.run(main())
```

生产环境中最值得警惕的是 **Task 泄漏**：调用 `asyncio.create_task()` 却不保存引用、不 `await`、不 `cancel`，一旦任务抛出未捕获异常，就会产生「Task exception was never retrieved」告警，甚至导致任务永远悬挂。规范的写法是：保存 Task 引用，用 `asyncio.wait`/`gather` 统一收口，并用 `try/finally` 保证取消逻辑执行。

## 三、高性能实践的三个关键调优点

### 3.1 用 uvloop 替换默认事件循环

CPython 自带的 `asyncio` 事件循环是用纯 Python + 底层 selector 实现的，而 `uvloop` 基于 libuv（Node.js 使用的同一套高性能事件库），在大量短连接、高并发场景下吞吐量通常有 **2～4 倍** 的提升。切换成本极低：

```python
import asyncio
import uvloop


async def handler(reader, writer):
    data = await reader.read(1024)
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    await writer.drain()
    writer.close()


async def main():
    server = await asyncio.start_server(handler, "0.0.0.0", 8888)
    async with server:
        await server.serve_forever()


uvloop.install()  # 必须在任何事件循环创建之前调用
asyncio.run(main())
```

需要强调的是：**`uvloop.install()` 必须在 `asyncio.run()` 之前调用**，否则不会生效。此外 uvloop 与 Windows 不兼容（libuv 的 Windows 实现不完整），生产通常部署在 Linux 上。如果代码里有依赖默认事件循环私有 API 的逻辑，迁移到 uvloop 后要重新验证。

### 3.2 控制并发，避免打爆上游

`asyncio.gather` 会一次性并发所有任务，在高并发爬虫或批量调用中，这会把数据库/下游服务瞬间打满。正确做法是用 `Semaphore` 限制并发度：

```python
import asyncio

sem = asyncio.Semaphore(50)  # 同时最多 50 个并发


async def bounded_fetch(url):
    async with sem:
        # 真正的请求逻辑
        return await do_request(url)


async def main():
    urls = [f"https://example.com/{i}" for i in range(10000)]
    results = await asyncio.gather(*(bounded_fetch(u) for u in urls))
    return results
```

并发上限不是拍脑袋定的，而应该用**压测**确认下游的容量拐点，并留出 20% 左右的余量。

### 3.3 超时与取消必须显式处理

异步编程里「卡住一个请求」比「失败一个请求」危害更大——悬挂的任务会持续占用连接和内存。所有外部 I/O 都应加超时：

```python
import asyncio


async def fetch_with_timeout(url, timeout=5.0):
    try:
        async with asyncio.timeout(timeout):  # Python 3.11+ 推荐用法
            return await do_request(url)
    except TimeoutError:
        # 记日志、上报指标，而不是静默吞掉
        return None
```

注意 `asyncio.wait_for` 在超时时会 `cancel` 任务，被取消的任务会抛 `CancelledError`；如果你的清理逻辑（如关闭连接、回滚事务）里还有 `await`，必须用 `asyncio.shield` 或把清理放到 `except CancelledError` 里并重新抛出，否则会留下半关闭的连接。

## 四、aiohttp 高并发：架构与踩坑

`aiohttp` 是 asyncio 生态里最成熟的 HTTP 客户端/服务端库。高并发场景下的第一原则是：**复用一个 `ClientSession`，而不是每个请求新建一个**。每个 `ClientSession` 内部维护着连接池（connection pool），重复创建会导致 TCP 连接无法复用、每次都要重新握手，性能断崖式下跌。

```python
import asyncio
import aiohttp


async def main():
    connector = aiohttp.TCPConnector(
        limit=100,           # 每个 host 最大并发连接数
        limit_per_host=20,   # 单个 host 并发上限
        ttl_dns_cache=300,   # DNS 缓存秒数
    )
    timeout = aiohttp.ClientTimeout(total=10, connect=3)

    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout
    ) as session:
        tasks = [get(session, i) for i in range(1000)]
        await asyncio.gather(*tasks)


async def get(session, i):
    async with session.get(f"https://example.com/{i}") as resp:
        return await resp.text()
```

生产环境中 `aiohttp` 有几个反复踩过的坑：

1. **连接泄漏**：`resp` 不用 `async with` 而只 `await session.get()` 后不关闭，连接不会归还连接池，最终连接池耗尽、请求全部挂起。务必用 `async with` 或在 `finally` 里 `resp.release()`。
2. **DNS 与连接复用**：默认情况下连接是按 host 复用的，如果后端有大量不同域名，`limit_per_host` 设置过小会退化成串行。压测时要观察 `connector` 的连接数与等待队列。
3. **回调里的阻塞**：在 aiohttp 的请求回调或中间件里调用同步 `requests`、`time.sleep` 或重 CPU 运算，会阻塞整个事件循环。遇到这类需求，用 `asyncio.to_thread` 丢到线程池。

## 五、生产环境排查思路

事件循环问题往往表现为「偶发卡顿」「延迟尖刺」「连接数暴涨」。有一套可复用的排查顺序：

1. **确认是否有阻塞调用**：用 `PYTHONASYNCIODEBUG=1` 或 `asyncio` 的 debug 模式运行，它会检测超过阈值（默认 100ms）仍不交还控制权的回调，直接打印堆栈。
2. **开启事件循环慢回调告警**：`loop.slow_callback_duration = 0.05`，任何回调执行超过 50ms 都会告警，能快速定位「谁卡住了循环」。
3. **监控任务数量**：通过 `asyncio.all_tasks()` 周期采样，任务数持续增长说明有 Task 泄漏；用 `loop.set_task_factory` 可以给每个 Task 打上创建时的堆栈，便于定位泄漏来源。
4. **压测 + 分层定位**：用 `wrk`、`hey` 或 `locust` 压测，观察 P99 延迟与错误率；对比「本地最小复现」与「完整链路」的差异，判断瓶颈在应用层、连接池还是下游。

一个实用的慢回调排查示例：

```python
import asyncio


async def main():
    loop = asyncio.get_running_loop()
    loop.slow_callback_duration = 0.05  # 50ms

    async def handler():
        # 假设这里不小心放了阻塞逻辑
        await asyncio.sleep(0)
        # time.sleep(0.2)  # 解开注释即触发慢回调告警

    for _ in range(100):
        asyncio.create_task(handler())

    await asyncio.sleep(1)


asyncio.run(main(), debug=True)
```

## 六、小结与建议

- 先理解模型再写代码：异步的并发是「I/O 等待期的并发」，CPU 密集与阻塞调用会直接卡死事件循环。
- 分清 `Coroutine` / `Future` / `Task` 三者关系：协程是惰性的，Task 才会被调度，Future 是结果占位符。
- 服务端高并发优先 `uvloop.install()`，且必须在 `asyncio.run()` 之前调用，并部署在 Linux。
- 所有外部 I/O 显式加超时；保存 Task 引用并用 `gather`/`wait` 收口，杜绝 Task 泄漏。
- 用 `Semaphore` 控制并发度，上限以压测数据为准，而非拍脑袋。
- `aiohttp` 复用一个 `ClientSession` 与连接池，`resp` 用 `async with` 关闭，避免连接泄漏。
- 生产环境开启慢回调告警与任务数监控，把「偶发卡顿」变成可定位、可复现的问题。
