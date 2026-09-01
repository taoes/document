---
title: "微服务下的接口幂等、限流与熔断设计"
date: 2024-07-13
category:
  - Java
tag:
  - 微服务
  - 幂等
  - 限流
icon: code
---

# 微服务下的接口幂等、限流与熔断设计

在单体拆成微服务之后，一个最容易被低估的事实是：**网络调用不再具备单进程函数调用的可靠性**。HTTP/RPC 天然存在超时、重试、乱序、重复投递，而服务间的调用链一旦拉长，任何一环的抖动都会被下游放大。于是「幂等、限流、熔断」这三件事，从"可选优化"变成了"生产底线"。本文不打算罗列概念，而是从我在生产环境踩过的坑出发，讲清楚每一层的设计取舍、参数调优与排查思路。

## 一、为什么这三件事必须放在一起设计

很多团队把幂等、限流、熔断当成三个独立的工具分别接入，结果往往是：限流拦住了突发流量，但重试风暴依然打垮了下游；幂等保证了重复请求不重复扣款，但熔断打开后客户端还在无脑重试。它们的本质其实是同一件事的三个切面——**在不可靠的分布式环境里，保证系统在异常流量下仍然做出正确且可预期的行为**。

我们可以把三者的职责拆开看：

| 机制 | 解决的核心问题 | 失效时间尺度 | 典型副作用 |
| --- | --- | --- | --- |
| 幂等 | 同一逻辑操作被重复执行 | 业务窗口（分钟~天） | 需要额外存储与唯一键约束 |
| 限流 | 超出承载能力的请求涌入 | 秒级~分钟级 | 误伤正常流量、排队延迟 |
| 熔断 | 下游持续失败时快速失败 | 秒级~分钟级 | 开启期间所有请求被拒 |

理解这三者后，一个重要的设计原则就浮现了：**限流是入口的第一道闸门，幂等是业务正确性的最后一道保险，熔断是依赖不可用时的止损开关**。三者应当围绕同一个"请求标识 + 依赖健康度"的上下文来协作，而不是各自为战。

## 二、幂等键设计：从唯一键到状态机

### 2.1 幂等键的来源与生成

幂等键（Idempotency Key）的核心诉求是：**同一个业务意图，无论调用多少次，只能生效一次**。键的生成有两种主流方式：

1. **客户端生成**：客户端在发起请求前生成一个 UUID 或基于业务字段的确定性键（如 `订单号 + 操作类型`），失败重试时复用同一个键。这是最可靠的方式，因为它把"意图"在源头就唯一化了。
2. **服务端推导**：服务端根据请求里的业务字段（如 `userId + 商品Id + 支付方式`）做哈希。缺点是无法区分"用户真的想买两次"和"重复投递"，需要额外的去重窗口语义。

生产上我更推荐**客户端生成 + 服务端强校验**的组合。服务端不管键来自哪里，只认"键唯一"这一条，把安全边界收在自己手里。

### 2.2 唯一键 + 状态机的落地

最朴素的做法是给幂等键建唯一索引，插入即抢锁：

```sql
CREATE TABLE t_idempotency (
    idem_key     VARCHAR(64)  NOT NULL COMMENT '幂等键',
    biz_type     VARCHAR(32)  NOT NULL COMMENT '业务类型',
    biz_status   TINYINT      NOT NULL DEFAULT 0 COMMENT '0处理中 1成功 2失败',
    biz_payload  TEXT         NULL COMMENT '首次成功的返回快照',
    create_time  DATETIME     NOT NULL,
    PRIMARY KEY (biz_type, idem_key)
) COMMENT '幂等记录表';
```

这里的关键不是表本身，而是**状态机**：`处理中 → 成功/失败`。当重复请求进来时：

```java
public BizResult execute(String idemKey, Supplier<BizResult> action) {
    // 1. 尝试插入"处理中"状态，靠唯一键抢占
    int inserted = idemMapper.tryInsertProcessing(idemKey);
    if (inserted == 1) {
        try {
            BizResult result = action.get(); // 真正执行业务
            idemMapper.markDone(idemKey, result.getStatus(), result.getPayload());
            return result;
        } catch (Exception e) {
            idemMapper.markFailed(idemKey);
            throw e;
        }
    }
    // 2. 抢锁失败：说明已有并发请求在处理
    IdemRecord existing = idemMapper.get(idemKey);
    if (existing.isProcessing()) {
        // 3. 处理中：等待有限时间后重查，或直接返回"请勿重复提交"
        return waitOrReject(existing);
    }
    // 4. 已成功：返回首次成功的快照，保证幂等返回一致
    return BizResult.of(existing.getStatus(), existing.getPayload());
}
```

### 2.3 生产上真实踩过的坑

- **坑一：并发插入依赖"处理中"状态，但业务耗时很长，唯一键锁住了却迟迟不落终态**。解决办法是给 `处理中` 状态加一个过期时间（如 30 秒），超时视为"疑似失败"，允许后续请求接管，但业务侧要保证真正的执行是可重入的。
- **坑二：只幂等了"扣款"，没幂等"退款"**。幂等键要和 `biz_type` 绑定，一个键只在一种操作语义下有效，避免退款复用扣款的键造成误判。
- **坑三：幂等表无限膨胀**。必须配置 TTL 清理，按 `create_time` 分区或定时任务删除超过业务窗口的记录。一个只写不删的幂等表，三个月后就会变成主库的负担。
- **坑四：唯一键冲突被吞成"成功返回"**。如果业务在 `tryInsert` 里把重复插入当作幂等命中直接返回，会掩盖真实的并发冲突。务必区分"我抢到了锁"和"锁被抢了，我要读取既有状态"。

## 三、限流：令牌桶与滑动窗口的工程取舍

限流算法的教科书讲得很多，但生产上真正要紧的是**计数窗口的边界效应和分布式一致性**。

### 3.1 固定窗口 vs 滑动窗口 vs 令牌桶

| 算法 | 突发容忍 | 实现复杂度 | 边界问题 | 适用场景 |
| --- | --- | --- | --- | --- |
| 固定窗口 | 边界处可翻倍 | 低 | 窗口临界瞬时双倍流量 | 粗粒度兜底 |
| 滑动窗口 | 较平滑 | 中 | 需记录时间戳，内存/存储开销 | 精确 QPS 控制 |
| 令牌桶 | 允许合理突发 | 中 | 桶容量需与下游容量对齐 | 网关、突发友好的接口 |
| 漏桶 | 恒定速率 | 低 | 丢弃或排队，延迟敏感场景不友好 | 消息消费整形 |

一个容易忽视的事实：**固定窗口在边界处会让实际 QPS 达到配置值的两倍**。比如 1 秒限 100 次，如果 0.9s 和 1.1s 各来 100 次，虽然分别落在两个窗口，但跨边界的瞬时速率是 200/s。所以对资金、库存这类敏感接口，不要用固定窗口，用滑动窗口或令牌桶。

### 3.2 令牌桶的 Redis 实现

下面是一个可运行的 Lua 脚本，把"取令牌"做成原子操作：

```lua
-- KEYS[1]: 令牌桶 key
-- ARGV[1]: 桶容量
-- ARGV[2]: 每秒补充速率
-- ARGV[3]: 本次请求需要的令牌数
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])

local now = redis.call('TIME') -- 秒 + 微秒
local now_ms = now[1] * 1000 + math.floor(now[2] / 1000)

local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(bucket[1])
local last_refill = tonumber(bucket[2])

if tokens == nil then
    tokens = capacity
    last_refill = now_ms
end

-- 按流逝时间补充令牌
local elapsed = now_ms - last_refill
tokens = math.min(capacity, tokens + elapsed * rate / 1000)

if tokens >= requested then
    tokens = tokens - requested
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now_ms)
    redis.call('PEXPIRE', key, math.ceil(capacity / rate * 1000) * 2)
    return 1
end
-- 令牌不足，返回需要等待的毫秒数（负数表示不足）
redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now_ms)
return math.ceil((requested - tokens) / rate * 1000) * -1
```

Java 侧用 `execute` 调用脚本即可：

```java
DefaultRedisScript<Long> script = new DefaultRedisScript<>(LUA, Long.class);
Long result = redisTemplate.execute(script, List.of("rate:order:pay"), "20", "10", "1");
if (result != null && result > 0) {
    // 拿到令牌，放行
} else {
    // 被限流，返回 429 或降级
}
```

### 3.3 分布式限流的一致性问题

单机限流用 Guava `RateLimiter` 足够，但微服务是多实例的，**单机限流总和会被实例数放大**。如果业务要求"整个集群 1000 QPS"，就必须把计数收敛到 Redis 或 Sentinel 这类共享存储。代价是每次请求多一次网络往返，于是有了折中方案：

- **本地预取 + 远程配额**：每实例从中心限流器预取一批配额（如 50 个），本地耗尽再取。牺牲少量精确性换取延迟，适合大流量网关。
- **精准模式**：直接走 Redis，适合低频但敏感的接口（如支付、发券）。

调优经验：令牌桶的 `capacity` 不要拍脑袋设成和 `rate` 一样，**容量决定你能容忍多长时间的突发**。比如 rate=10/s、capacity=20，意味着允许瞬间 20 个请求，然后 1 秒内不再放行。这个值要和下游连接池、线程池一起压测对齐，而不是孤立设置。

## 四、Sentinel 熔断降级：从规则到高可用

熔断的本质是**当下游依赖持续失败时，快速失败并保留资源，避免级联雪崩**。阿里开源的 Sentinel 是 Java 生态里落地最广的方案，但它的规则模型和调参陷阱值得单独讲透。

### 4.1 熔断策略选型

Sentinel 提供了三种熔断策略，选择的依据是"你拿什么指标判断下游病了"：

```java
@Bean
public void initFlowRules() {
    List<DegradeRule> rules = new ArrayList<>();
    DegradeRule rule = new DegradeRule("orderService")
        .setGrade(RuleConstant.DEGRADE_GRADE_EXCEPTION_RATIO)
        .setCount(0.5)      // 异常比例阈值 50%
        .setMinRequestAmount(20)  // 最小请求数，避免小样本误熔断
        .setStatIntervalMs(10000) // 统计窗口 10s
        .setTimeWindow(5);        // 熔断时长 5s，之后半开
    rules.add(rule);
    DegradeRuleManager.loadRules(rules);
}
```

三种策略的对比：

| 策略 | 触发条件 | 优点 | 致命陷阱 |
| --- | --- | --- | --- |
| 异常比例 | 窗口内异常占比超阈值 | 对偶发慢请求不敏感 | 低流量下样本太小会误判 |
| 异常数 | 窗口内异常绝对数超阈值 | 直观 | 大流量下阈值难设，阈值没变但流量翻倍就会提前熔断 |
| 慢调用比例 | RT 超过阈值的调用占比超阈值 | 能识别"还活着但已经不行了"的下游 | RT 阈值要结合 P99 而非平均 RT |

**关键教训**：异常比例策略必须配 `minRequestAmount`。没有最小请求数兜底，凌晨低峰期一次偶发异常就可能让异常比例 100%，把整个服务误熔断，等白天用户进来时正好赶上熔断周期，体验极差。

### 4.2 降级要分层，而不是一刀切

熔断打开后，`BlockException` 会抛出。很多人在这里只做了"返回默认值"，但这远远不够：

```java
@SentinelResource(value = "orderService", fallback = "fallback", blockHandler = "blockHandler")
public OrderDTO queryOrder(Long orderId) {
    return orderFeign.query(orderId);
}

// 熔断/限流触发：走降级
public OrderDTO blockHandler(Long orderId, BlockException ex) {
    // 1. 优先读本地缓存/兜底数据，而不是直接返回空
    OrderDTO cached = localCache.get(orderId);
    if (cached != null) return cached;
    // 2. 兜底仍不可用，返回明确的业务错误码，让上层决定
    throw new BizException(ErrorCode.DOWNSTREAM_UNAVAILABLE);
}
```

降级分层的思路是：**缓存 → 兜底默认值 → 快速失败**。能读缓存就不要返回空，能快速失败就不要让请求在队列里排 30 秒。降级的目的不是"给用户一个假数据"，而是"用最短的路径给出可预期的结果"。

### 4.3 高可用：Sentinel 控制台与动态规则

单机用 Sentinel 只需要引入 `sentinel-core`，但要动态改规则、看监控，就需要控制台（Dashboard）和数据源。生产上常见的架构是：

```yaml
spring:
  cloud:
    sentinel:
      transport:
        dashboard: sentinel-dashboard.internal:8858
        port: 8719          # 客户端上报端口
      datasource:
        ds-degrade:
          nacos:
            server-addr: nacos.internal:8848
            data-id: ${spring.application.name}-degrade
            group-id: SENTINEL_GROUP
            rule-type: degrade
```

这里的坑在于：**控制台推规则默认走客户端上报的 HTTP 端口，重启即丢**。真正高可用必须接 Nacos/Apollo 这类持久化数据源，让规则存到配置中心，客户端启动时拉取，运行时监听变更。否则你辛苦调好的规则，一次滚动发布就全没了。

排查熔断问题时的标准思路：

1. 先看 `blockHandler` 有没有被触发，确认是熔断（`DegradeException`）还是限流（`FlowException`），两者处理路径完全不同。
2. 看 Dashboard 的 QPS、RT、异常比三个面板是否同时恶化——只有 RT 高而异常低，说明是慢调用，先查下游 GC 和连接池。
3. 检查熔断恢复后的**半开状态**：Sentinel 熔断时间窗结束后，会放一个探测请求，成功才关闭熔断。如果探测请求本身依赖的资源还没恢复，就会再次熔断，形成"假恢复"。必要时延长 `timeWindow`，并确认探测请求走的是真实链路。

## 五、三者的协同：一次完整请求的生命周期

单独讲每一层容易，把它们串起来才见功力。一个资金类请求在生产里的完整路径应该是这样的：

```java
@PostMapping("/pay")
public PayResult pay(@RequestHeader("Idem-Key") String idemKey,
                     @RequestBody PayReq req) {
    // 1. 限流兜底：先于一切业务逻辑，用最少的资源拦掉超额流量
    if (!rateLimiter.tryAcquire("pay", req.getChannel())) {
        throw new BizException(ErrorCode.RATE_LIMITED); // 429
    }
    // 2. 幂等：靠幂等键抢占业务执行权，返回首拍结果
    return idempotencyService.execute(idemKey, () -> {
        // 3. 业务执行：内部对下游走熔断保护
        Account acct = accountService.deduct(req); // @SentinelResource 保护
        return payService.confirm(req, acct);
    });
}
```

这个顺序是有讲究的：**限流在最外层**，因为它是唯一能在不查数据库、不占业务资源的前提下拒绝请求的机制；**幂等在业务入口**，保证即使限流、熔断导致的重试也不会重复扣款；**熔断在最内层的下游调用**，保护的是依赖资源而非本服务。反过来如果先做幂等再限流，就会在流量洪峰时先打满幂等表，让数据库成为新的瓶颈。

## 小结与建议

- **幂等键由客户端生成、服务端强校验**，键要绑定 `biz_type`，用唯一键 + 状态机落地，并给"处理中"状态设过期时间、给表配 TTL 清理。
- **敏感接口禁用固定窗口限流**，用滑动窗口或令牌桶；令牌桶的容量要与下游线程池/连接池对齐，不要孤立设置。
- **分布式限流优先用 Redis Lua 原子脚本**，大流量场景用"本地预取 + 远程配额"折中，避免每次请求都打 Redis。
- **熔断策略务必配 `minRequestAmount`**，异常比例在小流量下会误熔断；慢调用阈值看 P99，别用平均 RT。
- **降级分层：缓存 → 兜底默认值 → 快速失败**，并且区分 `blockHandler`（限流/熔断）与 `fallback`（业务异常）。
- **Sentinel 规则必须接 Nacos/Apollo 持久化**，否则滚动发布丢规则；排查熔断时先确认是 Degrade 还是 Flow，再看半开探测是否"假恢复"。
- **三者的顺序是限流在最外层、幂等在业务入口、熔断在下游调用**，顺序错位会让数据库或幂等表成为新的瓶颈。

这三层机制不是三件孤立的工具，而是一套围绕"请求标识"与"依赖健康度"的防御纵深。把它们当成一个整体来设计，才能真正扛住生产环境的流量洪峰与依赖抖动。
