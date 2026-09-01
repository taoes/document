---
title: "FastAPI 性能调优与异步实践"
date: 2025-04-19
category:
  - Python
tag:
  - FastAPI
  - 性能
  - 异步
icon: terminal
---

# FastAPI 性能调优与异步实践

FastAPI 凭借 Starlette/ASGI 的异步内核与 Pydantic 的类型体系，成为当下 Python Web 开发的高性能代名词。但"异步框架"并不等于"自动变快"——我在多个生产项目中见过同一套代码在压测下从"宣称的几万 QPS"掉到几百，原因几乎都集中在三类：异步路由里混入了同步阻塞调用、依赖注入被滥用成"隐藏的同步耗时点"、以及部署层连接池与进程模型配置错误。本文不重复官方文档的 API 介绍，而是以真实生产事故为线索，把调优思路、参数依据和排查方法讲透。

## 一、先建立正确的性能观：瓶颈从来不在框架

在动手调参之前，必须先明确一件事：FastAPI 的框架开销（路由分发、参数解析、序列化）在绝大多数场景下只占单次请求耗时的一小部分。真正决定 QPS 的是三件事：**你的代码是否阻塞了事件循环、IO 是否被充分并发、进程/连接是否足够承载并发**。

一个常见的错误认知是"把路由写成 `async def` 就会快"。事实恰恰相反：`async def` 路由运行在事件循环线程上，如果你在其中执行了 CPU 密集计算或阻塞 IO，它会卡死整个循环，导致该 worker 内**所有**请求被拖垮。而 `def`（同步）路由会被 Starlette 丢进线程池执行，反而对这类代码更宽容——但这只是"不容易拖垮别人"，并没有解决吞吐问题。

因此，性能调优的第一步永远是**测量**。先确认 P50/P99 延迟和 QPS 的基线，再逐层定位。可以用 `py-spy` 做火焰图采样，用 `wrk` 或 `locust` 压测，用 `async-profiler` 思路分析事件循环的空闲与阻塞窗口。

## 二、异步路由与同步阻塞陷阱

### 2.1 `await` 只对"真异步"生效

下面这段代码是生产中最常见的坑：

```python
import time
import httpx
from fastapi import FastAPI

app = FastAPI()

@app.get("/slow")
async def slow():
    # 错误：requests 是同步库，会阻塞事件循环
    import requests
    r = requests.get("http://internal-api/item")
    return r.json()

@app.get("/fast")
async def fast():
    async with httpx.AsyncClient() as client:
        r = await client.get("http://internal-api/item")
        return r.json()
```

`/slow` 里的 `requests.get` 会让当前事件循环线程陷入等待，其他并发请求全部排队。修正方式不是简单地"换 httpx"，而是要让**每一个 IO 都通过 `await` 让出控制权**：

```python
@app.get("/fast")
async def fast():
    # 连接复用 + 超时 + 重试，缺一不可
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=1.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    ) as client:
        r = await client.get("http://internal-api/item")
        r.raise_for_status()
        return r.json()
```

### 2.2 CPU 密集任务必须隔离

CPU 密集计算（加密、图像处理、正则回溯、大 JSON 序列化）在事件循环里哪怕只跑几十毫秒，也会显著抬高 P99。标准做法是 `run_in_threadpool` 或 `run_in_processpool`：

```python
from fastapi.concurrency import run_in_threadpool
from hashlib import pbkdf2_hmac

@app.post("/hash")
async def hash_password(password: str):
    # 把 CPU 密集的 PBKDF2 丢到线程池，避免阻塞事件循环
    result = await run_in_threadpool(
        pbkdf2_hmac, "sha256", password.encode(), b"salt", 200_000
    )
    return {"hash": result.hex()}
```

需要特别提醒：GIL 决定了线程池只对**释放 GIL 的 C 扩展**（如哈希、压缩）和**阻塞 IO**有效；纯 Python 的 CPU 密集循环用线程池毫无收益，应改用 `ProcessPoolExecutor` 或独立队列消费者（如 Celery/RQ），或干脆迁移到 `anyio.to_process.run_sync`。

### 2.3 同步阻塞库的识别清单

| 场景 | 危险写法 | 正确写法 |
| --- | --- | --- |
| HTTP 客户端 | `requests` | `httpx.AsyncClient` / `aiohttp` |
| 数据库 ORM | `SQLAlchemy` 同步 Session | `SQLAlchemy 2.0 async` / `asyncpg` |
| 缓存 | `redis-py` 同步 | `redis.asyncio` |
| 文件 IO | 内置 `open()` 大文件 | `aiofiles` 或线程池 |
| DNS/网络 | `socket` 阻塞调用 | `asyncio` 原生解析 |

一个实用的自动化防线是 `blockbuster` 或 `asyncio-debug` 模式：设置 `PYTHONASYNCIODEBUG=1` 可以在开发期发现"事件循环被阻塞超过阈值"的告警，把问题拦截在上线前。

## 三、依赖注入：被低估的开销与正确用法

FastAPI 的 `Depends` 是控制反转的利器，但也是隐藏性能杀手。每次请求都会**重新执行依赖函数**，如果你在依赖里做数据库连接创建、大对象初始化或重复解析，开销会线性叠加。

```python
# 反模式：每个请求都新建连接池
async def get_db():
    engine = create_async_engine(DATABASE_URL)  # 每次请求都执行！
    async with engine.connect() as conn:
        yield conn
    await engine.dispose()
```

上述代码每次请求都 `create_async_engine` + `dispose`，连接池建立和销毁的开销远大于查询本身。正确做法是**全局单例 engine + 请求级 session**：

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

engine = create_async_engine(
    DATABASE_URL,
    pool_size=20,          # 常驻连接数
    max_overflow=10,       # 峰值可临时多开
    pool_timeout=30,       # 拿连接超时
    pool_pre_ping=True,    # 断连自动探活
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_db():
    async with SessionLocal() as session:
        yield session
```

除了连接，还有两类依赖要优化：

1. **可缓存依赖**：`lru_cache` 装饰的 `Depends` 可以在同一请求内复用结果，适合"根据请求头解析出的用户/租户信息"。
2. **跳过重复校验**：`Depends(..., use_cache=True)`（默认）在单个请求中只执行一次，但如果你的依赖里藏了慢 IO，仍会拖慢每个首次调用它的路由。

我的建议是：**依赖函数保持"轻量、纯解析"，把重量级资源（连接池、客户端）提升到模块级单例**，并在压测时用 `py-spy` 单独观察依赖栈帧的耗时占比。

## 四、连接池与数据库/缓存调优

并发上不去，十有八九卡在连接池。连接池的本质是"以空间换时间"：预先建立一批连接，请求来了直接复用，避免 TCP 三次握手与数据库认证开销。

### 4.1 数据库连接池参数推导

连接池大小不是越大越好。PostgreSQL 每个连接都是一个后端进程，连接过多会导致数据库端上下文切换与内存压力激增。经验公式：

```
pool_size = (worker 数量 × 每 worker 事件循环并发) 的上限
         = 需要同时执行查询的协程数
max_overflow 用于吸收突发流量，但要设置 pool_timeout 防止无限等待
```

以 `gunicorn -w 4` 每 worker 单循环为例，若每个请求平均执行 1 个查询、期望单 worker 并发 100，则 `pool_size` 取 20~40、`max_overflow` 取 10~20 通常是安全起点。关键是**用监控反推**：观察数据库侧 `pg_stat_activity` 的活跃连接数，若频繁看到 `idle in transaction` 堆积，说明应用层 session 没及时释放；若 `waiting for connection` 超时，说明池子太小。

### 4.2 缓存连接同样重要

```python
import redis.asyncio as redis

r = redis.Redis(
    host="127.0.0.1",
    port=6379,
    decode_responses=True,
    max_connections=200,       # 与数据库同理，预设连接池
    socket_timeout=5,
    socket_connect_timeout=1,
)
```

Redis 本身单线程，连接池主要影响客户端的建立开销。生产环境务必开启连接复用与超时，避免网络抖动时请求堆积导致雪崩。

### 4.3 一个真实踩坑记录

某项目在灰度上线后 P99 从 120ms 飙升到 3s，QPS 掉到 1/4。排查链路：`slow query log` 无异常 → `pg_stat_activity` 显示大量连接处于 `idle in transaction` → 定位到某个依赖里 `yield` 之后还执行了同步外部调用，导致事务迟迟不提交，连接被长期占用。修复方式是缩短事务窗口 + 设置 `idle_in_transaction_session_timeout`。这再次印证：**性能问题的根因常在不经意的生命周期管理里**。

## 五、gunicorn/uvicorn 部署调优

部署层的参数直接决定前面的优化能否兑现。常见组合是 `gunicorn`（进程管理）+ `uvicorn`（ASGI worker）。

```bash
gunicorn app.main:app \
  -k uvicorn.workers.UvicornWorker \
  -w 4 \
  --threads 1 \
  --worker-connections 1000 \
  --backlog 2048 \
  --keep-alive 5 \
  --timeout 30 \
  --max-requests 10000 \
  --max-requests-jitter 1000 \
  -b 0.0.0.0:8000
```

各参数的作用与调优思路：

| 参数 | 作用 | 调优建议 |
| --- | --- | --- |
| `-w` | worker 进程数 | CPU 核数 × (1~2)；IO 密集可适当多，CPU 密集别超过核数 |
| `-k` | worker 类型 | 必须指定 `uvicorn.workers.UvicornWorker`，否则默认同步 worker 会毁掉异步模型 |
| `--worker-connections` | 每 worker 最大并发连接 | 依据内存与吞吐目标，异步 worker 可开很大 |
| `--backlog` | 监听队列长度 | 高并发突发场景调大，避免内核丢连接 |
| `--keep-alive` | HTTP 长连接保活时长 | 减小可降低 TIME_WAIT，但会增加建连开销，需实测 |
| `--max-requests` | worker 处理多少请求后重启 | 防内存泄漏的兜底，配 `jitter` 避免同时重启 |

### 5.1 关于 `--threads`

对 `UvicornWorker` 而言，`--threads` 会影响 `run_in_threadpool` 的线程池大小。如果你的同步 `def` 路由或 `run_in_threadpool` 调用较多，需要适当调大 `--threads`（如 8~16）；纯 `async` 路由则保持 1 即可，线程池不是越大越好。

### 5.2 SO_REUSEADDR 与容器环境

在 Kubernetes 等容器环境下，如果直接跑 `uvicorn` 而不经 gunicorn，记得检查进程数（容器通常按 1 worker 设计，靠副本数横向扩展）。同时关注 `--proxy-headers` 和 `--forwarded-allow-ips`，错误的代理头解析会让限流、日志和 HTTPS 判断失效，间接影响"性能表象"（如误判为 HTTP 导致重定向）。

### 5.3 验证手段

```bash
# 压测：12 线程、400 连接、持续 30 秒
wrk -t12 -c400 -d30s --latency http://127.0.0.1:8000/api/health

# 火焰图采样，定位热点
py-spy record -o profile.svg --pid $(pgrep -f uvicorn) --duration 30

# 观察事件循环是否被阻塞（开发期）
PYTHONASYNCIODEBUG=1 uvicorn app.main:app --reload
```

## 六、小结与建议

- **先测量后调优**：用 `wrk`/`locust` 建立 QPS 与 P99 基线，用 `py-spy` 火焰图定位热点，别凭直觉改参数。
- **异步不等于快**：`async def` 里禁止同步阻塞调用；CPU 密集任务走线程池/进程池或独立消费者。
- **IO 全面异步化**：HTTP 用 `httpx`，数据库用 `asyncpg`/`SQLAlchemy 2.0 async`，缓存用 `redis.asyncio`，文件用 `aiofiles`。
- **依赖注入保持轻量**：连接池、客户端一律模块级单例，依赖函数只做解析，避免每个请求重建资源。
- **连接池按需配**：`pool_size` 依据真实并发推导，开启 `pool_pre_ping` 与超时，用 `pg_stat_activity` 反推是否泄漏。
- **部署参数要咬合业务**：`gunicorn -k uvicorn.workers.UvicornWorker` 是底线，`--worker-connections`、`--backlog`、`--max-requests` 需压测校准，容器场景按副本数横向扩展。
- **守住生命周期**：缩短事务窗口、及时 `commit/rollback`、释放连接，避免 `idle in transaction` 这类"慢性毒药"。
