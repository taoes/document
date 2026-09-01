---
title: "线上 GC 问题排查实战：从内存泄漏到 OOM 定位"
date: 2023-12-09
category:
  - Java
tag:
  - JVM
  - GC
  - 排查
icon: code
---

# 线上 GC 问题排查实战：从内存泄漏到 OOM 定位

深夜两点，告警群突然炸了：某核心服务的堆内存使用率连续 30 分钟超过 95%，Full GC 频率从平时的每 40 分钟一次飙升到每分钟数次，接口 P99 从 120ms 恶化到 3s，紧接着节点开始轮番 OOM 重启。这类场景对每一个后端工程师来说都不陌生。本文以一次真实的生产内存泄漏事故为蓝本，完整复盘从「发现异常」到「定位根因」再到「止血与根治」的全链路排查过程，重点讲清楚 jstat、jmap、jstack、MAT 这些工具各自该在什么时机用、能拿到什么证据、如何互相印证。

## 一、问题现象：先定性，再动手

拿到告警后的第一件事不是急着 dump 堆，而是先回答三个问题：**这是内存泄漏，还是内存本来就不够？是 Full GC 频发导致的暂停，还是年轻代晋升压力过大？是单点问题还是全集群问题？**

先看监控面板，把现象量化：

- **堆内存曲线**：老年代呈「锯齿状」缓慢抬升，每次 Full GC 只能回收一小部分，水位线越来越高——这是典型的**内存泄漏特征**（每次回收后仍有大量存活对象无法释放）。
- **GC 日志**：`Full GC (Allocation Failure)` 频繁出现，且每次 Full GC 前后老年代占用几乎不变。
- **接口表现**：GC 暂停时间（`GCTime`）占比从 2% 涨到 15%，线程 STW 时间与 Full GC 次数强相关。

这里有一个关键判断：如果内存曲线是「台阶式」上升且每级台阶对应一次大促或任务，大概率是**容量问题**，扩堆或降并发即可；如果是「锯齿缓升」且回收无效，则大概率是**泄漏问题**，必须往下挖。本文的案例属于后者。

## 二、工具链分工：什么时候用什么

很多工程师误以为「dump 一把梭」就能解决问题，其实不同工具对应不同排查阶段，用错时机反而会加剧问题（比如在流量高峰期 `jmap -dump` 会触发长时间 STW，甚至直接把节点打挂）。下表是生产环境常用工具的分工：

| 工具 | 命令示例 | 适用阶段 | 主要输出 | 注意点 |
| --- | --- | --- | --- | --- |
| jstat | `jstat -gcutil <pid> 1000 20` | 实时观测、趋势判断 | 各区使用率、GC 次数/耗时 | 轻量、无侵入，先于一切执行 |
| jmap -histo | `jmap -histo:live <pid> \| head` | 快速定位「谁占堆」 | 类实例数与字节数排行 | 会触发一次 Full GC |
| jmap -dump | `jmap -dump:live,format=b,file=heap.bin <pid>` | 离线深挖引用关系 | 堆转储文件 | 会 STW，务必先摘流量 |
| jstack | `jstack <pid> > thread.txt` | 定位线程卡点、死锁 | 线程堆栈 | 结合 GC 停顿判断线程阻塞 |
| MAT | 分析 heap.bin | 泄漏根因定位 | 支配树、泄漏嫌疑报告 | 内存大文件需调大 MAT 自身堆 |

排查顺序应当遵循「**由轻到重、由外到内**」：

1. `jstat` 先看趋势，确认是泄漏还是容量问题；
2. `jmap -histo` 快速锁定可疑类；
3. `jstack` 结合线程栈判断业务代码卡在哪；
4. 最后才 `jmap -dump` + MAT 做精确的引用链分析。

## 三、定位内存泄漏：jstat / jmap / MAT 实战

### 3.1 jstat 看趋势

第一步在问题节点上执行（`-gcutil` 输出的是各区**百分比**，`-gc` 输出的是**绝对值**）：

```bash
# 每 1 秒采样一次，共 20 次，观察各区使用率与 GC 次数
jstat -gcutil 12345 1000 20
```

输出示例如下：

```
  S0     S1     E      O      M     CCS    YGC     YGCT    FGC    FGCT     GCT
  0.00  18.32  64.21  96.88  92.10  87.45  4820   89.231   142   213.445  302.676
```

解读要点：

- `O`（老年代）96.88%，且多次采样后**不回落**，说明老年代里堆着大量「死不掉」的对象；
- `FGC` 次数持续增长，`FGCT`（Full GC 累计耗时）增长更快，说明每次 Full GC 都几乎收不回东西——回收是「白忙活」。

此时基本可以定性为老年代内存泄漏。用 `jstat -gccause` 再确认触发原因：

```bash
jstat -gccause 12345 1000 5
```

若 `LGCC`/`GCC` 显示 `Allocation Failure`，说明是新生代晋升到老年代时空间不足触发；配合 `O` 高位，进一步坐实「老年代被泄漏对象占满」。

### 3.2 jmap -histo 锁定可疑类

`jmap -histo` 按实例数/字节数排序，是「快速圈定嫌疑对象」性价比最高的手段：

```bash
jmap -histo:live 12345 | head -30
```

```
 num     #instances         #bytes  class name
----------------------------------------------
   1:      18349264     1467941120  [C
   2:       5663120      905799200  com.acme.order.OrderContext
   3:       4332110      346568800  java.lang.String
   4:       1200345       96027600  com.acme.order.OrderItem
```

从输出可以立刻看到异常信号：`OrderContext` 的实例数高达 566 万，且持续增长。正常的业务对象生命周期应该很短（一个请求结束即被回收），实例数却接近堆中的字符数组量级，**这就是泄漏点的高度嫌疑对象**。

> 注意：`-histo:live` 会触发一次 Full GC 以获得「存活对象」统计，生产环境务必先摘流量或选低峰期执行；`-histo`（不带 `:live`）不触发 GC，但会把可回收对象也算进去，数值偏大。

### 3.3 jmap -dump + MAT 找引用链

`jmap -histo` 只能告诉你「谁多」，但解释不了「为什么没被回收」。要找到持有这些对象的 GC Root，必须 dump 堆并用 MAT 分析支配树。

```bash
# 摘流量后，先记录当前存活对象再 dump，避免把可回收对象也 dump 进来
jmap -dump:live,format=b,file=/tmp/heap-$(date +%s).bin 12345
```

拿到 `heap.bin` 后，用 MAT 打开，核心看三个报告：

1. **Leak Suspects（泄漏嫌疑报告）**：MAT 会自动计算支配树，指出「占用内存最多的嫌疑对象」及疑似泄漏的类。
2. **Histogram + Dominator Tree（支配树）**：右键可疑类 → `List objects → with incoming references`，逐级展开引用链，直到找到 GC Root。
3. **OQL 查询**：用 SQL 风格的语句精确检索，例如查某个类的所有实例：

```sql
SELECT * FROM com.acme.order.OrderContext o
WHERE o.status = 0
```

在本案例中，通过支配树向上追溯，最终锁定的引用链如下：

```
Thread[pool-3-thread-17]
  → java.util.HashMap$Node[] (静态缓存 cache)
    → java.util.HashMap
      → com.acme.order.OrderContext (key 的 value)
        → com.acme.order.OrderItem (list)
```

根因是：一个**静态 `HashMap` 被当作本地缓存使用，key 设计为「每次请求都变化的动态字段」，导致缓存只增不减**，`OrderContext` 被无期限持有，最终撑爆老年代。

## 四、频繁 Full GC 的根因分析

内存泄漏只是表象，为什么它会引发「频繁 Full GC」而非直接 OOM？这中间有完整的机制链条，理解它才能理解为什么监控上 GC 频率是「渐变恶化」的：

1. **老年代水位上升**：泄漏对象持续堆积，老年代可用空间越来越小。
2. **晋升失败加剧**：新生代对象晋升到老年代时，因空间不足触发 `Full GC (Allocation Failure)`。
3. **Full GC 收效甚微**：泄漏对象都有 GC Root 强引用，回收器「扫得勤却收不掉」，每次 Full GC 耗时很长但回收空间很小。
4. **STW 恶性循环**：Full GC 的 Stop-The-World 让业务线程停摆，请求积压 → 内存分配更剧烈 → 触发更多 GC，最终 `java.lang.OutOfMemoryError: Java heap space` 兜底，节点重启。

判断「频繁 Full GC」是否由泄漏引起的另一个重要证据是 **Full GC 前后老年代差值**。可以开启 GC 日志精确记录：

```bash
java -Xlog:gc*:file=gc.log:time,uptime,level,tags -jar app.jar
```

重点看每次 Full GC 的 `Heap after GC` 中老年代（Old）的大小变化：

- 若每次 Full GC 后 Old 占用**几乎不降** → 泄漏（对象收不掉）；
- 若每次 Full GC 后 Old 占用**明显下降但很快又涨** → 更可能是晋升速率过快或大对象分配，属于调优问题而非泄漏。

## 五、修复方案与 JVM 参数调优

### 5.1 止血与根治

排查事故要先止血、后根治。本案例的处置分两步：

**止血（当天凌晨）**：先重启并临时扩大堆 + 缩短对象引用，让服务恢复：

```bash
java -Xms4g -Xmx4g -XX:+UseG1GC \
     -XX:MaxGCPauseMillis=200 \
     -jar app.jar
```

同时用「限流 + 降级」把该缓存相关链路切到旁路，避免泄漏继续累积。

**根治（次日发布）**：把「无上限的静态 HashMap 缓存」改为有淘汰策略的本地缓存，并加上容量上限与 TTL：

```java
import com.google.common.cache.Cache;
import com.google.common.cache.CacheBuilder;

import java.util.concurrent.TimeUnit;

public class OrderContextCache {
    private static final Cache<String, OrderContext> CACHE = CacheBuilder.newBuilder()
            .maximumSize(100_000)                 // 硬性容量上限
            .expireAfterWrite(10, TimeUnit.MINUTES) // 写入后 10 分钟过期
            .removalListener(n -> {
                // 可在此记录淘汰日志，验证淘汰策略是否生效
            })
            .build();

    public static OrderContext get(String orderNo) {
        return CACHE.getIfPresent(orderNo);
    }

    public static void put(String orderNo, OrderContext ctx) {
        CACHE.put(orderNo, ctx);
    }
}
```

修复的要点是：**任何作为缓存的容器都必须有「上限 + 淘汰策略」**，否则在流量放大后一定会退化成泄漏。这是无数生产事故换来的教训。

### 5.2 JVM 参数建议

针对此类「对象多、请求密集」的在线服务，给出以下可直接落地的参数组合（JDK 8/11/17 均可，具体按 GC 选择调整）：

```yaml
# 基础堆与元空间
-XX:+UseG1GC                 # 在线低延迟服务首选 G1
-Xms4g -Xmx4g                # 初始/最大堆一致，避免动态扩容抖动
-XX:MaxMetaspaceSize=512m    # 元空间上限，防类加载泄漏撑爆

# GC 调优
-XX:MaxGCPauseMillis=200     # 期望最大停顿
-XX:InitiatingHeapOccupancyPercent=45  # 触发并发标记的老年代阈值，适当下调给回收留缓冲
-XX:G1ReservePercent=10      # 预留空间，降低晋升失败概率

# 排查友好：开启 GC 日志与 OOM 自动 dump
-Xlog:gc*:file=/var/log/app/gc.log:time,uptime,level,tags:filecount=5,filesize=20m
-XX:+HeapDumpOnOutOfMemoryError
-XX:HeapDumpPath=/var/log/app/dump
-XX:+ExitOnOutOfMemoryError  # 视场景：OOM 后主动退出交给编排平台重启，避免僵死节点
```

> 说明：JDK 8 下 `-Xlog:gc*` 需替换为老式参数 `-XX:+PrintGCDetails -XX:+PrintGCDateStamps -Xloggc:gc.log`。生产环境请始终开启 GC 日志与 OOM 自动 dump，这是「出事之后能复盘」的底线保障。

## 六、线上排查的坑与规范

复盘这次事故，有几个容易被忽视、但代价很高的坑，值得单独强调：

1. **高峰期直接 `jmap -dump`**：`-dump:live` 会先触发 Full GC，随后 dump 期间线程 STW，在流量高峰可能直接把节点打挂。正确做法是「先摘流量 → 重启到新节点 → 在无流量/低流量节点上 dump」。
2. **dump 文件落在根分区**：堆有多大 dump 就有多大，若写满根分区会导致节点雪崩。务必落到独立的 `/tmp` 或数据盘，并留足空间。
3. **只看 `-histo` 就下结论**：`-histo` 的实例数包含「待回收对象」，且它无法给出引用链，只能作为「圈定嫌疑」的初筛，最终结论必须由 MAT 支配树/引用链来验证。
4. **忽视元空间泄漏**：老年代正常但 `Full GC` 频发时，也可能是元空间（`java.lang.OutOfMemoryError: Metaspace`）被动态类加载器撑爆，需用 `jstat -gcutil` 里的 `M`/`CCS` 列交叉确认。
5. **用 `System.gc()` 或手动 Full GC 掩盖问题**：显式触发 Full GC 只是把问题往后推，还会打乱 G1 的自适应调节，属于「饮鸩止渴」。

排查规范建议固化为 SOP：**观测（jstat）→ 圈定（jmap -histo）→ 印证（jstack + GC 日志）→ 深挖（dump + MAT）→ 止血 → 根治 → 回归验证**，每一步都要留下可追溯的证据（命令输出、dump 文件、GC 日志）。

## 小结与建议

- **先定性再动手**：用 jstat 的堆曲线与 Full GC 前后老年代差值，区分「泄漏 / 容量不足 / 晋升过快」三种情况，不要盲目 dump。
- **工具要按阶段用**：jstat 看趋势 → jmap -histo 圈对象 → jstack 看线程 → MAT 挖引用链，由轻到重。
- **缓存必有上限与淘汰**：静态容器缓存、`HashMap` 做 key 的本地缓存，是内存泄漏最高发的来源，必须加 `maximumSize` 与 TTL。
- **GC 日志与 OOM dump 是底线**：生产环境默认开启，否则事故后无据可查。
- **dump 要讲时机与落盘**：摘流量、低峰期、独立大分区，避免排查动作本身制造二次故障。
- **修完要回归验证**：发布修复后连续观察 jstat 的 `O` 与 `FGC` 曲线，确认老年代水位回落、Full GC 频率回到基线，才算真正闭环。
