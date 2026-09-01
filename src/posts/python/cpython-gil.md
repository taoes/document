---
title: "CPython GIL 机制与多线程/多进程/协程选型"
date: 2024-08-17
category:
  - Python
tag:
  - GIL
  - 并发
  - CPython
icon: terminal
---

# CPython GIL 机制与多线程/多进程/协程选型

GIL（Global Interpreter Lock，全局解释器锁）是 CPython 并发模型里被误解最深、也最常被当成"背锅侠"的机制。很多团队一遇到性能问题就甩出一句"Python 有 GIL，所以慢"，然后转头就换语言，却从来没人去搞清楚：GIL 到底在什么时候锁、什么时候放、多线程在什么场景下依然能拿到可观的吞吐、什么时候才真正需要上多进程或 asyncio。

这篇文章不会停留在"GIL 让多线程只能单核跑"这种正确的废话上，而是从 CPython 的解释器实现出发，把切换机制、锁竞争的真实开销、三种并发范式的选型边界，以及我在生产环境踩过的坑和排查手段，一次性讲透。

## 一、GIL 是什么：一个引用计数引出的历史包袱

GIL 是一个**互斥锁**，它保护的是 CPython 解释器的内部状态，而不是你写的业务代码。为什么需要它？最直接的原因来自 CPython 的对象内存管理策略——**引用计数**。

CPython 的 `PyObject` 结构里有一个 `ob_refcnt` 字段，几乎所有 C 层操作都会对它做 `Py_INCREF` / `Py_DECREF`。考虑下面这个场景：

```python
import threading

data = []

def worker():
    for _ in range(1000000):
        data.append(None)   # 每次 append 都会改 data 的引用计数
```

如果没有一把全局锁，两个线程同时执行 `list.append`，就可能对同一个 `ob_refcnt` 做"读-改-写"，产生竞态：引用计数被改坏，导致对象被提前释放（use-after-free）或者永不释放（内存泄漏），结果就是解释器直接段错误崩溃。你当然可以给每个对象单独加一把细粒度锁，但那会带来海量的锁开销和死锁风险，还要为所有 C 扩展库重新设计内存模型。所以在 1992 年那个年代，Guido 选择了最简单粗暴、但足够可靠的方案：**一把大锁锁住整个解释器**。

要理解的核心是：**GIL 保护的是"执行字节码 + 操作 Python 对象"这段解释器逻辑，而不是用户态的并发语义**。这一区别直接决定了后面所有选型判断。

## 二、GIL 的切换机制：线程如何被强制让出

很多人以为 GIL 是一个线程独占直到跑完才释放，其实不是。CPython 采用的是**周期性强制切换**，核心代码在 `Python/ceval.c` 的字节码执行主循环里，大致逻辑如下：

```c
/* ceval.c 简化示意 */
if (_Py_atomic_load_relaxed(eval_breaker)) {
    if (_Py_OPCODE(*next_instr) == SETUP_FINALLY ||
        ... ) { /* 快速路径 */ }
    else {
        /* 慢速路径：处理信号、pending calls、切换线程等 */
        if (gil_drop_request) {
            give_up_the_gil(ceval, tstate);
        }
    }
}
```

具体机制可以拆成三层：

1. **默认时间片（switch interval）**：默认值是 **5 毫秒**（Python 3.2 之前是 100 个字节码指令）。主循环在执行字节码的同时，会检查一个"是否到了让出 GIL 的时间点"的标志位。
2. **`eval_breaker` 标志**：解释器通过一个原子标志位 `eval_breaker` 来"通知"正在运行的线程"你该停下来处理点别的事了"，比如有信号要处理、有 pending call、或者该让出 GIL 了。
3. **锁竞争与信号量等待**：当线程 A 的时间片到了，它会释放 GIL，并在 GIL 相关的条件变量上等待；线程 B 拿到 GIL 继续跑。

可以用一段代码直观地感受默认时间片的影响：

```python
import sys
import time

print("default switch interval:", sys.getswitchinterval())  # 默认 0.005

def cpu_burn():
    t0 = time.time()
    while time.time() - t0 < 1.0:
        x = 0
        for i in range(1000):
            x += i * i
    return x

# 观察不同 switch interval 下两个 CPU 密集线程的切换行为
for interval in (0.0005, 0.005, 0.05):
    sys.setswitchinterval(interval)
    print(f"switch interval = {interval}")
```

关键结论：**GIL 的时间片是"尽力而为"的，不是硬实时保证**。一个线程执行到一半不会被抢占到任意指令边界，它只会在安全的检查点让出，通常是下一条字节码开始前。这也解释了为什么"纯 Python 的 CPU 密集循环"多线程几乎无法提速——两个线程只是在同一把锁上轮流空转，还要额外付出上下文切换和锁竞争的成本。

### 为什么 IO 密集仍能用多线程

这是本文最重要的一张牌。答案在于：**GIL 会在阻塞式系统调用之前主动释放**。

CPython 在设计上有一条铁律——**绝不让线程抱着 GIL 进入一个可能长时间阻塞的内核调用**。当代码执行到 `socket.recv()`、`file.read()`、`time.sleep()`、`queue.get()` 这类阻塞操作时，CPython 会在进入系统调用前调用 `Py_BEGIN_ALLOW_THREADS` 释放 GIL，在系统调用返回后用 `Py_END_ALLOW_THREADS` 重新获取 GIL。核心宏定义在 `Python/ceval.h` 里：

```c
#define Py_BEGIN_ALLOW_THREADS { \
    PyThreadState *_save; \
    _save = PyEval_SaveThread();   // 释放 GIL

#define Py_END_ALLOW_THREADS \
    PyEval_RestoreThread(_save);   // 重新获取 GIL
}

```

于是，一个典型的网络服务里会发生这样的事：线程 A 发出 `recv()` 后把 GIL 交出去，然后在内核里等数据；此时线程 B、C、D 拿到 GIL，各自发出自己的 `recv()`，又各自把 GIL 交出去。**真正等待 IO 的时间是完全并行的**，只有"解析响应、组装对象"这极少量的 CPU 时间需要抢 GIL。所以，即使只有一个核在跑 Python 字节码，几十上百个线程的 IO 密集程序依然能跑出很高的吞吐。

一句话总结：**IO 密集程序的多线程提速，不是因为多线程并行执行了 Python 代码，而是因为 IO 等待在 GIL 之外发生了**。

## 三、三种并发范式对比：threading / multiprocessing / asyncio

把上面的机制搞清楚了，选型就不再是玄学。三者解决的是不同的瓶颈：

| 维度 | threading | multiprocessing | asyncio |
| --- | --- | --- | --- |
| 并发模型 | 抢占式线程 | 独立进程 + IPC | 单线程事件循环 + 协程 |
| 是否受 GIL 限制 | CPU 密集受限制，IO 密集基本不受 | 完全不受（每进程一把 GIL） | 单线程，无锁竞争 |
| 共享内存 | 天然共享，但要注意线程安全 | 不共享，需 pickle/IPC | 单线程内共享，无需锁 |
| 任务切换开销 | 内核线程切换，~微秒级 | 进程间通信 + 反序列化，开销大 | 协程切换，~纳秒级，极低 |
| 擅长场景 | 阻塞式 IO（DB、网络、文件） | CPU 密集计算 | 海量高并发网络 IO |
| 编程复杂度 | 中（锁、竞态、死锁） | 高（序列化、启动开销） | 高（需全链路非阻塞） |
| 内存开销 | 每线程 ~8MB 栈（Linux） | 每进程独立内存，fork 后可共享 | 每协程极低 |

下面用三段代码分别展示典型用法，以及各自最容易翻车的点。

### 1. threading：给阻塞 IO 用的

```python
import threading
import requests
from concurrent.futures import ThreadPoolExecutor

URLS = [f"https://httpbin.org/delay/1?n={i}" for i in range(20)]

def fetch(url):
    return requests.get(url, timeout=10).status_code

# 阻塞式 requests 每发起一个请求，就会释放 GIL 进入内核等待
with ThreadPoolExecutor(max_workers=20) as pool:
    results = list(pool.map(fetch, URLS))
print(results)
```

这段代码里 `requests.get()` 底层是阻塞 socket，等待响应期间 GIL 被释放，所以 20 个请求几乎是并行完成的。线程池大小通常设成"外部并发上限 + 一点余量"，不是越多越好——线程多了只会增加调度和栈内存开销。

### 2. multiprocessing：给 CPU 密集计算用的

```python
from concurrent.futures import ProcessPoolExecutor

def is_prime(n):
    if n < 2:
        return False
    for i in range(2, int(n ** 0.5) + 1):
        if n % i == 0:
            return False
    return True

nums = [10**14 + i for i in range(1000)]

# 真正的多核并行，每个进程有自己的 GIL
with ProcessPoolExecutor(max_workers=8) as pool:
    primes = list(pool.map(is_prime, nums))
print(sum(primes))
```

这里每个进程独立跑，8 核机器上能接近 8 倍加速。但代价是 `nums` 和结果都要经过 pickle 序列化，启动进程也有开销，所以**任务太小、传参太大的时候，多进程反而亏本**。

### 3. asyncio：给海量高并发网络 IO 用的

```python
import asyncio
import aiohttp

async def fetch(session, url):
    async with session.get(url) as resp:
        return await resp.text()

async def main():
    urls = [f"https://httpbin.org/delay/1?n={i}" for i in range(1000)]
    async with aiohttp.ClientSession() as session:
        tasks = [fetch(session, u) for u in urls]
        return await asyncio.gather(*tasks)

asyncio.run(main())
```

1000 个协程在单个线程里通过事件循环调度，每个协程的切换开销是纳秒级，远低于线程。**前提是全程没有阻塞调用**：只要你在协程里写了一个 `requests.get()` 或 `time.sleep()`，整个事件循环就会被卡死。

## 四、真实生产环境的坑与调优

理论再好，落到线上还是会有各种反直觉的问题。下面是我实际踩过、也是排查最多的几类。

### 坑 1：CPU 密集任务多线程"越开越慢"

很多人直觉上觉得"线程多 = 快"，于是给一个计算任务开了 16 个线程。结果在 4 核机器上，多线程版本反而比单线程慢 30%～50%。原因就是上面说的**锁竞争 + 上下文切换 + convoy（车队）效应**：线程频繁被 5ms 时间片打断，抢锁失败的线程进入睡眠再被唤醒，这些纯开销没有产生任何有效计算。

**验证方式**：用 `py-spy top --pid <pid>` 直接看热点，你会发现 CPU 大量消耗在 `_pthread_cond_wait` / `sem_wait` 这类等待函数上，而不是你的业务计算。

**排查命令示例**：

```bash
# 看线程调度和锁等待情况
py-spy top --pid 12345 --full-filenames
# 或看线程状态分布
cat /proc/12345/task/*/status | grep -E 'State|voluntary'
```

**结论**：CPU 密集一律用 `multiprocessing`，进程数约等于物理核数（`os.cpu_count()`），不要超线程满配。

### 坑 2：multiprocessing 的 fork vs spawn

在 Linux 上，Python 3.8 之前默认用 `fork` 启动子进程，macOS 和 Windows 默认用 `spawn`。`fork` 快但危险：子进程会继承父进程的完整内存快照，**如果父进程此时正好有别的线程持有锁（比如 logging 模块的锁），fork 后子进程里这把锁会永远锁死**，子进程直接卡住。这在"多线程程序里再开进程池"的场景下几乎是必踩的。

**建议**：显式指定启动方式，并在入口处保护：

```python
import multiprocessing as mp

if __name__ == "__main__":
    # 跨平台一致，且规避 fork 的锁继承问题
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=4) as pool:
        pool.map(worker, tasks)
```

### 坑 3：asyncio 里混入阻塞调用

事件循环是单线程的，一个 `time.sleep(1)` 或一次同步数据库查询，会让所有协程一起卡死 1 秒。这种问题最隐蔽，因为它"看起来能跑"，只是吞吐上不去。

**排查思路**：给事件循环装一个"慢回调探测器"，把耗时超过阈值的调用打出来：

```python
import asyncio
import time

loop = asyncio.get_event_loop()
loop.slow_callback_duration = 0.1  # 超过 100ms 的 callback 会告警

# 配合 asyncio debug 模式
asyncio.run(main(), debug=True)
```

或者在代码里用 `loop.set_debug(True)`，它会打印"Executing &lt;Task&gt; took 0.512 seconds"这类警告，直接定位到卡住的那一行。

### 坑 4：线程数失控导致内存/栈耗尽

Linux 下每个线程默认栈大小约 8MB（`ulimit -s` 决定），一个服务如果开 2000 个线程，光栈就吃掉十几 GB 虚拟内存。更隐蔽的是 `threading` 创建的线程不会被 GC，`ThreadPoolExecutor` 的线程池也不会自动收缩。

**建议**：给线程池设上限，给线程起名方便定位，必要时调低栈大小或干脆改用 asyncio：

```python
import threading
import concurrent.futures

# 给线程命名，/proc 和 py-spy 里都能看到
pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=32,
    thread_name_prefix="fetcher",
)
```

### 关键调优参数速查

| 参数 | 作用 | 建议 |
| --- | --- | --- |
| `sys.setswitchinterval()` | GIL 时间片，默认 5ms | IO 密集可调大到 10～20ms 减少切换；CPU 密集别调 |
| 线程池 `max_workers` | 并发线程数 | 设成外部依赖并发上限 + 余量，别拍脑袋 4 倍 CPU |
| 进程池 `max_workers` | 并发进程数 | 约等于物理核数，CPU 密集不要超配 |
| `mp.get_context("spawn")` | 进程启动方式 | 跨平台一致性，规避 fork 锁继承 |
| `loop.slow_callback_duration` | asyncio 慢回调阈值 | 设 0.05～0.2s，尽早暴露阻塞调用 |
| `OMP_NUM_THREADS=1` | 限制 numpy 等原生库线程 | 多进程 + 原生库场景避免线程超订（oversubscription） |

最后这个 `OMP_NUM_THREADS` 是很多人忽略的点：当你用 `multiprocessing` 跑 8 个进程，每个进程里的 numpy/BLAS 又各自开了 8 个线程，总共 64 个线程在 8 个核上疯狂抢占，性能反而崩掉。多进程 + 原生多线程库组合时，务必限制原生库的线程数。

## 五、选型决策与排查方法论

把上面所有结论收敛成一张决策图（按优先级判断）：

1. **任务是纯计算、无 IO？** → `multiprocessing`，进程数 = 物理核数。
2. **是阻塞式 IO（同步 DB、requests、文件读写），且并发量在几百以内？** → `threading` + `ThreadPoolExecutor`，GIL 不会成为瓶颈。
3. **是海量网络 IO，并发量上千甚至上万，要求低延迟？** → `asyncio`，但必须全链路非阻塞，且团队能驾驭协程心智模型。
4. **既要多核计算、又要高并发 IO？** → 混合架构：`asyncio` 做 IO 层 + `run_in_executor` 或独立 `ProcessPoolExecutor` 做计算层，二者用 `asyncio.get_event_loop().run_in_executor()` 衔接。
5. **C 扩展或原生库释放 GIL 的 CPU 密集任务（如 numpy 部分操作）？** → 可以直接用线程，因为计算发生在 GIL 之外。

排查性能问题时，遵循"先定位瓶颈再谈方案"的顺序：

```bash
# 1. 看整体 CPU / 线程状态
top -H -p <pid>

# 2. 采样热点，看时间花在计算还是锁等待
py-spy top --pid <pid>
py-spy dump --pid <pid>

# 3. 看系统调用，确认 IO 是否真的并行发出
strace -f -e trace=network,read,write -p <pid> 2>&1 | head -100

# 4. 多进程场景看 fork/spawn 与内存占用
ps -o pid,ppid,stat,rss,vsz,cmd -p <pid> --ppid <pid>
```

判断"到底是不是 GIL 的锅"有一个简单实验：把同样的逻辑用 `multiprocessing` 跑一遍，如果多进程接近线性加速而多线程没有，那瓶颈就是 GIL/CPU；如果多进程也没加速，瓶颈在别处（可能是磁盘、网络、锁、序列化开销），别急着甩锅给 GIL。

## 小结与建议

- **GIL 保护的是解释器内部状态，源于引用计数**，而不是用户代码；理解这一点才能正确选型。
- **GIL 每 5ms 强制切换**（`sys.setswitchinterval` 可调），且只在安全检查点让出，CPU 密集多线程纯属白费。
- **阻塞式系统调用会先释放 GIL**，所以 IO 密集多线程的并行发生在"等待 IO"这一段，不在"执行 Python"这一段——这是多线程在 IO 场景下依然有效的根本原因。
- **选型口诀**：计算用多进程、阻塞 IO 用多线程、海量网络 IO 用 asyncio，别把三者混为一谈。
- **多线程 CPU 密集会越开越慢**，用 `py-spy` 看锁等待即可验证；多进程记得显式 `spawn`，避免 fork 锁继承。
- **asyncio 最怕阻塞调用**，上线前打开 `slow_callback_duration` 和 debug 模式兜底。
- **多进程 + 原生库**务必限制 `OMP_NUM_THREADS`，防止线程超订导致性能反噬。
- **先定位瓶颈，再选方案**：用"多线程 vs 多进程"对照实验判断瓶颈是否真的在 GIL，不要凭直觉下结论。
