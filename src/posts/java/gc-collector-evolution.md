---
title: "JVM 垃圾收集器演进：从 CMS 到 G1 再到 ZGC 的调优实战"
date: 2022-03-19
category:
  - Java
tag:
  - JVM
  - GC
  - ZGC
icon: code
---

# JVM 垃圾收集器演进：从 CMS 到 G1 再到 ZGC 的调优实战

## 一、为什么要理解垃圾收集器的代际演进

很多工程师对 GC 的认知停留在「STW 越小越好、吞吐量越大越好」的口号层面，一到生产环境就凭感觉堆参数。实际上，不同垃圾收集器的核心差异在于**内存布局、对象标记/复制算法、并发阶段与暂停模型的组合方式**。同样一份负载，在 CMS 上表现良好的配置原封不动搬到 ZGC 上反而会拖慢吞吐量；反过来，一个「大堆、低延迟」的诉求如果硬套 Parallel Scavenge，几乎注定要经历周期性长暂停。

理解演进，本质上是理解一个约束条件的逐步解除过程：**CMS 试图在分代假设下并发做掉大部分标记与清除，但受限于「标记-清除」的内存碎片与「并发标记-再标记」的漏标补偿；G1 用 Region 化布局 + 可预测停顿的增量回收 + 复制式整理，把停顿从「堆大小相关」拉回到「回收集大小相关」；ZGC 则用染色指针与读屏障，把「并发整理」推向极致，让停顿收敛到与堆大小基本无关。** 这一路走来，每一代都在解决上一代最痛的短板，也都引入了新的运维复杂度。

先建立一套统一的观察框架，后面每一节都会回到这个框架上：

1. **回收范围**：一次回收处理多少内存（部分堆 / 整堆 / 分代）。
2. **并发能力**：哪些阶段能与业务线程并发执行，哪些必须停顿。
3. **停顿上界**：最长 STW 与堆大小、活跃对象数量的关系。
4. **碎片风险**：回收后是否产生无法利用的空洞。
5. **吞吐代价**：读屏障/写屏障、并发线程带来的额外 CPU 开销。

## 二、CMS：并发标记-清除的分代先行者

CMS（Concurrent Mark Sweep）的目标很朴素：把最耗时的标记和清除放到业务线程之外并发执行，只在必要节点短暂停顿。它的堆仍然是经典分代布局：新生代用 ParNew 做复制回收，老年代用「标记-清除」。

其回收流程分为四个阶段，其中只有两个是 STW 的：

| 阶段 | 是否 STW | 作用 | 停顿量级 |
| --- | --- | --- | --- |
| 初始标记（Initial Mark） | 是 | 只标记 GC Roots 直接可达对象 | 极短 |
| 并发标记（Concurrent Mark） | 否 | 从 GC Roots 追踪整个对象图 | 不暂停业务 |
| 重新标记（Remark） | 是 | 修正并发标记期间的对象变更 | 中等，可能较久 |
| 并发清除（Concurrent Sweep） | 否 | 清除不可达对象并归还空间 | 不暂停业务 |

CMS 最经典的坑几乎都与「标记-清除」这一算法选择有关，而不是并发本身：

- **并发模式失败（Concurrent Mode Failure）**：并发回收速度赶不上分配速度，老年代在回收完成前就被填满，JVM 被迫回退到一次 Serial Old 全堆 STW。日志里出现 `concurrent mode failure` 时，通常意味着要提前触发 CMS 或加大堆。
- **内存碎片与「晋升失败」**：清除不整理，长期运行后老年代变成千疮百孔。即使空闲总量足够，也可能找不到一块连续空间容纳一个大对象，进而触发 Full GC。
- **浮动垃圾（Floating Garbage）**：并发清理期间新产生的垃圾只能留到下一轮回收，所以 CMS 必须预留「触发阈值」空间，典型默认是老年代占用达到 92% 才触发，这本身又压缩了可用堆。

下面是一段可以实际运行的压力程序，用来复现 CMS 的碎片与并发失败问题：

```java
// 演示 CMS 碎片化：反复分配"中号存活对象 + 大号短命对象"
// 运行：java -Xms512m -Xmx512m -XX:+UseConcMarkSweepGC -XX:+PrintGCDetails
public class CmsFragmentation {
    static final int SURVIVOR_COUNT = 200_000;   // 长期存活，造成碎片
    static final int CHURN_COUNT    = 50_000;    // 短命大对象，制造分配压力

    public static void main(String[] args) throws Exception {
        byte[][] survivors = new byte[SURVIVOR_COUNT][];
        for (int i = 0; i < SURVIVOR_COUNT; i++) {
            survivors[i] = new byte[256];        // 老年代中散落的"钉子"
        }
        for (int round = 0; round < 200; round++) {
            for (int i = 0; i < CHURN_COUNT; i++) {
                byte[] churn = new byte[1024 * 4]; // 4KB 短命对象
            }
            // 周期性分配一个大对象，触发对连续空间的需求
            byte[] big = new byte[1024 * 1024 * 4]; // 4MB，易因碎片晋升失败
            System.gc();  // 显式触发，便于观察日志（生产环境请勿照搬）
            Thread.sleep(5);
        }
    }
}
```

生产上如果必须继续使用 CMS，常见的参数组合是：

```bash
java -Xms8g -Xmx8g \
  -XX:+UseConcMarkSweepGC \
  -XX:CMSInitiatingOccupancyFraction=70 \  # 提前触发，降低并发失败概率
  -XX:+UseCMSInitiatingOccupancyOnly \      # 只用固定阈值触发，不做动态估计
  -XX:+CMSParallelRemarkEnabled \           # 并行化重新标记，压缩 Remark 停顿
  -XX:+ScavengeBeforeFullGC \
  -Xloggc:/var/log/app/gc.log \
  -XX:+PrintGCDetails -XX:+PrintGCDateStamps \
  -jar app.jar
```

需要强调的是：从 JDK 9 起 CMS 被标记为废弃，JDK 14 正式移除。**新系统不要再选择 CMS**，它只存在于历史包袱与需要平滑迁移的存量系统里。

## 三、G1：Region 化与可预测停顿

G1（Garbage First）的设计假设是「停顿不必等到堆满」。它把堆切分为大小相等、逻辑连续的 Region（默认按堆大小划分，目标约 2048 个，每个 1~32MB），Region 可动态扮演 Eden、Survivor、Old 或 Humongous 角色。回收时不再整堆扫描，而是**优先回收「垃圾占比最高」的 Region**，即 Garbage First 名字的由来。

G1 的停顿模型因此发生质变：STW 不再与整个堆大小强相关，而是由「回收集（Collection Set, CSet）」中 Region 数量与其中存活对象决定。调优的关键就变成了「限制每次回收的工作量」。

G1 的核心阶段如下：

| 阶段 | 是否 STW | 说明 |
| --- | --- | --- |
| 初始标记 | 是 | 标记 GC Roots，常随 Young GC 一起完成 |
| 并发标记 | 否 | 追踪对象图 |
| 最终标记（Remark） | 是 | 处理 SATB 漏标补偿 |
| 筛选回收（Cleanup） | 是 | 并行复制存活对象，整理 Region |

G1 使用 **SATB（Snapshot-At-The-Beginning）写屏障**保证并发标记的正确性。它的取舍很清晰：写屏障比 CMS 更重、吞吐量通常比并行收集器低 5%~15%，换取的是停顿可预测与碎片整理能力（复制式回收天然不产生碎片）。

G1 最著名的调优参数是 `-XX:MaxGCPauseMillis`，但这里有个普遍误区：**它只是「期望目标」，不是硬性保证**。设得太低（比如 50ms）会让 G1 每次都只回收很小一部分 Region，结果新生代可能来不及排空，反而触发更多 Young GC 甚至 Full GC。真实经验是：先测出默认配置下的基线停顿，再把目标设为「比基线低 20%~30% 但可稳定达成」的值，而不是拍脑袋写 10ms。

一个典型的 G1 线上配置：

```bash
java -Xms16g -Xmx16g \
  -XX:+UseG1GC \
  -XX:MaxGCPauseMillis=200 \       # 期望停顿目标，需结合实测调整
  -XX:InitiatingHeapOccupancyPercent=45 \  # 并发标记启动阈值
  -XX:G1HeapRegionSize=16m \       # 大对象多时可适当调大
  -XX:G1ReservePercent=10 \        # 预留空间给晋升，降低 to-space exhausted
  -XX:ParallelGCThreads=8 \
  -XX:ConcGCThreads=2 \
  -Xlog:gc*:file=/var/log/app/gc.log:time,uptime,level,tags \
  -jar app.jar
```

G1 有两个高频生产事故，值得单独写清楚：

1. **Humongous 对象导致 Full GC**：单对象超过单个 Region 的 50% 即被视为 Humongous，G1 会分配连续 Region 存放。频繁的巨型对象分配/回收（尤其配合短命大对象）会让老年代 Region 迅速碎片化。日志里反复出现 `Humongous Allocation` 加长停顿时，优先从业务侧减小对象体积（如分批读取、流式处理），而不是继续调参。
2. **to-space exhausted / Full GC 抖动**：当 Mixed GC 没能及时回收、晋升压力过大时，G1 会退回 Full GC（本质是串行的整堆整理）。观察指标是 `-Xlog:gc+ergo` 中的 Ergonomics 决策与 Full GC 频率。若 Full GC 频繁，往往是 `IHOP` 阈值不合理或 `G1ReservePercent` 太小，应让并发标记更早启动。

判断一次 GC 属于哪种类型，以及停顿是否达标，主要靠日志分析。推荐统一输出到文件并用脚本粗筛：

```bash
#!/usr/bin/env bash
# 从统一 GC 日志中抽取各收集器关键事件，便于快速定位停顿来源
GC_LOG=/var/log/app/gc.log

echo "== 各类 GC 次数统计 =="
grep -oE "Pause (Young|Mixed|Full|Initial Mark|Remark|Cleanup)" "$GC_LOG" \
  | sort | uniq -c | sort -rn

echo "== 停顿超过 200ms 的记录 =="
grep -E "Pause .*[0-9]+(\.[0-9]+)?ms" "$GC_LOG" \
  | awk -F' ' '{ for(i=1;i<=NF;i++) if($i ~ /ms$/) { gsub(/ms/,"",$i); if($i+0 > 200) print $0 } }'

echo "== Full GC 触发原因 =="
grep -E "Full GC \(" "$GC_LOG" | tail -20
```

## 四、ZGC：染色指针与亚毫秒级停顿

ZGC 的目标不是「降低停顿」，而是**把停顿从堆大小中彻底解耦**。它的最大 STW 与堆大小无关，官方宣称在 8MB~16TB 的堆上都能把停顿压在 10ms 以内（实际常为亚毫秒到几毫秒）。

ZGC 的两个关键技术是：

- **染色指针（Colored Pointers）**：在 64 位指针里拿出若干位存储标记信息（Marked、Remapped、Finalizable 等），把 GC 元数据「缝」进指针本身，而非对象头。这意味着一次加载就能同时拿到对象地址与 GC 状态，代价是依赖地址空间的「多重映射」技巧，且对指针的每一位有严格要求。
- **读屏障（Load Barrier）**：并发移动对象后，业务线程读到「过期指针」时由读屏障现场修正。这与 G1/CMS 的写屏障不同——**ZGC 把成本从「写」转移到了「读」**，对写密集型负载更友好，但读密集、缓存不友好的场景会有额外开销。

ZGC 的回收阶段几乎全部并发：

| 阶段 | 是否 STW | 说明 |
| --- | --- | --- |
| 初始标记 | 是 | 极短，标记 GC Roots |
| 并发标记/重映射/预备重分配 | 否 | 全并发 |
| 转移根 | 是 | 与堆大小无关的短停顿 |
| 并发重分配 | 否 | 对象搬运与读屏障修正并行进行 |

ZGC 的实战要点与 CMS/G1 完全不同，**它的主要矛盾不再是「停顿」，而是「并发回收线程与业务线程的 CPU 争抢」**。如果你的机器核数不足，ZGC 的并发线程会抢占业务线程，表现为「停顿很好看，但吞吐量/P99 延迟反而变差」。

一个 ZGC 生产配置示例（JDK 15+）：

```bash
java -Xms32g -Xmx32g \
  -XX:+UseZGC \
  -XX:ConcGCThreads=4 \        # 并发线程，一般按 CPU 核数 1/8~1/4
  -XX:ParallelGCThreads=8 \
  -XX:+UseLargePages \         # 大页可显著提升 ZGC 性能，需 OS 配合
  -XX:ZCollectionInterval=0 \  # 0 表示按需触发，不做周期性强制回收
  -Xlog:gc*:file=/var/log/app/zgc.log:time,uptime,level,tags \
  -jar app.jar
```

使用 ZGC 前必须确认运行环境：它依赖 JDK 15 正式支持（JDK 11/13/14 为实验特性需 `-XX:+UnlockExperimentalVMOptions`），且强烈建议使用 Linux/x86_64 或 AArch64；同时它不兼容某些依赖指针位操作的 native 库或过旧的 JVM TI 工具。

关于选型，可以用下面这张对照表快速决策：

| 维度 | CMS | G1 | ZGC |
| --- | --- | --- | --- |
| 算法 | 标记-清除（并发） | 标记-整理（Region 增量） | 标记-整理（染色指针并发） |
| 停顿与堆关系 | 大致相关（碎片与并发失败） | 与回收集大小相关 | 基本无关（<10ms） |
| 碎片 | 严重（不整理） | 无（复制整理） | 无（并发整理） |
| 吞吐代价 | 较低 | 中（写屏障） | 中高（读屏障 + 并发线程） |
| 典型停顿 | 几十 ms ~ 秒级 | 可控，几十~几百 ms | 亚毫秒 ~ 数 ms |
| 适用堆大小 | < 8GB | 4GB ~ 数十 GB | 8GB ~ 数 TB |
| 生产建议 | 已移除，仅存量 | 默认首选（JDK 9+） | 低延迟大堆场景 |

## 五、生产选型决策树与迁移思路

把选择过程固化成一条可执行的决策链，避免「新技术崇拜」：

1. **先确定延迟还是吞吐优先**。若是离线批处理、对单次停顿不敏感，`-XX:+UseParallelGC` 这类并行收集器往往吞吐最高，根本不需要 G1/ZGC。
2. **需要可预测停顿、堆在 4GB~32GB**：首选 G1，它同时也是 JDK 9+ 的默认选择，生态与监控工具支持最成熟。
3. **大堆（>16GB）且停顿必须稳定在个位数毫秒、CPU 有余量**：选 ZGC。典型如实时风控、网关、量化交易、内存型数据库网关。
4. **存量 CMS 系统**：不要原地「参数救火」，优先规划迁移到 G1。迁移不是改一个参数就完事，要做对比压测、观察 Full GC 频率、重新评估堆大小与新生代比例。

迁移到 G1 后，以下几个参数**不要照搬 CMS 时代的习惯**：G1 通常不需要手动设 `-Xmn`（新生代大小）——G1 会按停顿目标自适应调整，手动固定反而破坏其自适应能力；`-XX:SurvivorRatio` 同理应谨慎；调优重心应从「堆分区比例」转向 `MaxGCPauseMillis`、`IHOP` 与 `G1ReservePercent`。

一次可靠的迁移步骤如下：

```yaml
# GC 迁移灰度方案（示意）
migration_plan:
  - step: 基线采集
    action: 用现有收集器跑满 7 天，记录 P50/P99/P999 延迟、Full GC 频率、吞吐
  - step: 灰度切换
    action: 切 5% 流量到 G1，对比同指标，重点看长尾与 CPU 使用率
  - step: 扩大验证
    action: 压测注入 2x 流量，观察停顿是否仍被 MaxGCPauseMillis 约束
  - step: 全量上线
    action: 全量切换并保留回滚开关，持续观察 GC 日志告警
```

## 小结与建议

- 理解每一代收集器的**算法取舍**（CMS 的并发标记-清除、G1 的 Region 增量复制、ZGC 的染色指针读屏障），比背参数更有用。
- CMS 已退出历史舞台，新系统默认 G1；只有「大堆 + 稳定低延迟 + CPU 富余」才是 ZGC 的甜区，低延迟诉求不等于无脑上 ZGC。
- G1 调优重心在 `MaxGCPauseMillis`、`IHOP`、`G1ReservePercent`，并警惕 Humongous 对象与 Full GC 抖动；**停顿目标是期望值，不是硬保证**。
- ZGC 的痛点是并发线程 CPU 争抢与读屏障开销，务必监控吞吐量与 P99，而非只看停顿数字。
- 无论哪个收集器，**先把 GC 日志统一、结构化、接入告警**，用数据驱动调参；任何参数变更都要在灰度环境做 A/B 对比，并保留回滚能力。
- 调优的顺序永远是：先优化业务分配行为（对象大小、生命周期、池化）→ 再调堆大小 → 最后才动收集器与细粒度参数。
