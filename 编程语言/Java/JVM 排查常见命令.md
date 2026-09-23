## 1、 线程相关
CPU 高、接口卡顿、线程阻塞、死锁、等待锁、线程池满，优先抓 ThreadDump。

```shell
ps -efH ｜ grep java  # 查询进程情况
top -H -p <pid> #查询 进程情况
jstack -l <pid> > thread1.txt # 连续多份，看线程状态变化，只抓一次容易误判
```

重点看：
1. `RUNNABLE`：正在执行，大量 RUNNABLE 大概率 CPU 飙升（死循环、大计算）
2. `BLOCKED`：被 synchronized 锁阻塞，等待监视器
3. `WAITING`/`TIMED_WAITING`：等待 ReentrantLock、线程池、sleep
4. 查找`Deadlock`，直接定位死锁
    业务例子：供应商绩效批量更新，多个线程争抢同一行记录锁，大量 BLOCKED。

**技巧**: 抓**连续 3 份**，对比线程状态，单次快照容易看到瞬时状态。
**忌讳**: 只抓一次就下结论；只看业务代码栈，忽略 JDK 内部栈。

## 2、内存相关

- OOM 自动生成
```text
-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/xxx/heap.hprof
```

- 手动触发生成
```shell
jmap -dump:format=b,file=heap.hprof <pid>
# jmap 会 STW，生产尽量低峰执行；堆很大时 dump 耗时久，磁盘要预留足够空间。
```

分析工具：MAT（Memory Analyzer Tool）

MAT 重点看：
1. Histogram 直方图：按对象实例数量、占用大小排序，看哪个对象最多（比如大量供应商 DTO 没有释放）
2. Dominator Tree 支配树：找**留存内存最大对象**，定位内存泄漏
3. Leak Suspects 泄漏嫌疑报告（MAT 自动生成）
4. OQL 查询对象

业务场景：供应商批量导出，一次性把上万条供应商数据加载到 List，没有及时释放，OOM。

**技巧**: 优先用 OOM 自动 dump，不要随便在线上大堆手动 jmap；hprof 文件优先下载到本地分析，不要在服务器直接解析。
**忌讳**: 线上随时手动 dump 几十 G 堆，引发业务长时间停顿；把大对象当成泄漏（要区分一次性大对象 vs 持续增长泄漏）。


## 3、GC 日志
GC 频繁、FullGC、停顿长、CPU 波动，优先看 GC 日志，不需要抓快照，持续输出。

```text
-Xloggc:/data/gc.log -XX:+PrintGCDetails -XX:+PrintGCDateStamps

jstat -gc
```

- GC 类型：YGC / FullGC
- 堆前后内存变化：`(1024M)->(200M)`，回收效果好不好
- GC 耗时：用户态、系统态、实际停顿时间 real time
- FullGC 出现频率：频繁 FullGC 是危险信号

**技巧**:  GC 日志优先判断**是不是内存泄漏**；如果每次 FullGC 回收很少内存，大概率泄漏。
**忌讳**: 看到一次 FullGC 就判定内存泄漏；分不清「大对象短期分配」和「持续内存泄漏」。


## 总结

当收到告警 JVM 异常：
1. 看监控，是单机情况还是都这样
		1. 单机： 摘流，但是不要重启，尽量保持现场，必要时扩容
		2. 集群： 检查是否有发布，先扩容，后续考虑回滚
2. 区分是 CPU 高还是内存 / GC 问题。
	- 如果 CPU 高 / 线程阻塞：执行 jstack 连续抓 3 份线程快照，分析 RUNNABLE/BLOCKED 线程，定位死循环、锁等待。
	- 如果 OOM / FullGC 频繁：先查看 GC 日志，判断是内存泄漏还是堆不足；发生 OOM 时自动生成 heap dump，下载后用 MAT 分析支配树，找到泄漏对象和引用链。
	- load 高： 执行 jstack 连续抓 3 份线程快照 看线程有无大量等待在某个节点上



```text
先 jps 拿 pid → top 看负载 CPU 
	CPU 高：top -H 抓线程 → jstack 定位代码，jstat 看 GC 
	OOM：导出 heap dump，MAT 分析引用链，定位内存泄漏 
	FullGC 频繁：看老年代增长，区分内存泄漏 / CMS 碎片 
	线程异常：jstack，查 BLOCKED、死锁、WAITING
```

```text
1. **jps**：查看 Java 进程 PID
2. **jstat**：实时 GC 统计
    - `jstat -gc pid 1000`：每秒输出 GC、YGC、FGC、耗时、堆各区域使用量
3. **jstack pid**：线程快照，定位死锁、BLOCKED、高 CPU 线程
4. **jmap**：堆快照
    - `jmap -dump:format=b,file=heap.hprof pid` 导出堆文件（**业务高峰期慎用，会 STW**）
5. **jhat**：简单分析 hprof（一般用 MAT 代替）
6. **MAT（Memory Analyzer Tool）**：离线分析堆 dump，找大对象、内存泄漏
7. **jvisualvm**：图形化监控
```

# 高频追问

- 追问 1：jmap -histo 是什么？什么时候用？
> jmap -histo <pid> 打印堆对象统计，**不需要完整 dump 文件，开销更小**，快速看对象数量。适合快速粗筛，缺点**没有引用链**，只能看到对象数量，无法定位泄漏原因。

- 追问 2：jstack 和 jmap 的 STW 差异？
> jstack 几乎无 STW；jmap dump 堆会触发 STW，堆越大停顿越久。

- 追问 3：MAT 里面 Shallow Heap 和 Retained Heap？

Shallow：对象本身占用内存；Retained：对象被 GC 回收后，可以释放的总内存（包含它引用的子对象），看泄漏**优先看 Retained Heap**。

技巧：Retained 才是判断泄漏的核心指标。

忌讳：只看 Shallow Heap 判断内存泄漏。