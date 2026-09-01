---
title: "HashMap 与 ConcurrentHashMap 源码级深度解析"
date: 2022-10-08
category:
  - Java
tag:
  - 集合
  - 源码
  - 哈希
icon: code
---

# HashMap 与 ConcurrentHashMap 源码级深度解析

HashMap 与 ConcurrentHashMap 是 Java 面试的高频题，但真正能在生产环境里"讲清楚为什么"的工程师并不多。很多人背下了"数组 + 链表 + 红黑树""CAS + synchronized"这些结论，却说不清哈希扰动到底解决了什么问题、扩容为什么能把节点分到两个槽位、树化的阈值为什么是 8、以及 `sizeCtl` 为负数到底代表什么。

本文不打算复述 API 手册，而是从源码出发，把这些结论背后的设计权衡拆开，并结合生产环境的真实事故与调优经验，给出可操作的结论。

## 一、数据结构总览与设计权衡

JDK8 的 `HashMap` 底层是一个 `Node<K,V>[] table` 数组。每个桶（bin）里存放的是链表节点或红黑树节点。核心字段如下：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `DEFAULT_INITIAL_CAPACITY` | 16 | 初始桶数组长度（必须为 2 的幂） |
| `DEFAULT_LOAD_FACTOR` | 0.75f | 负载因子，决定扩容时机 |
| `TREEIFY_THRESHOLD` | 8 | 链表转红黑树的长度阈值 |
| `UNTREEIFY_THRESHOLD` | 6 | 红黑树退化回链表的阈值 |
| `MIN_TREEIFY_CAPACITY` | 64 | 触发树化的最小桶数组长度 |
| `MAXIMUM_CAPACITY` | 1 << 30 | 桶数组最大容量 |

容量被强制为 2 的幂，是一个贯穿全类的关键约束。它让 `index = (n - 1) & hash` 可以等价于 `hash % n`，且位运算比取模快得多。这个约束也直接决定了后续的扩容逻辑为什么如此简洁。

`Node` 的 `hash` 字段在普通节点里存的是扰动后的键哈希值；但在 `ConcurrentHashMap` 里，这个 `hash` 字段被赋予了特殊语义——负数表示特殊节点类型：

```java
static final int MOVED     = -1; // 桶正在迁移（ForwardingNode）
static final int TREEBIN   = -2; // 红黑树根节点
static final int RESERVED  = -3; // computeIfAbsent 的占位节点
```

理解这个约定，是读懂 ConcurrentHashMap 扩容协作的前提。

## 二、哈希扰动：为什么是 `(h = key.hashCode()) ^ (h >>> 16)`

这是最容易"知道结论、不知道为什么"的地方。问题出在桶索引的计算方式上。

假设 `table.length = n`（2 的幂），索引为：

```java
int index = (n - 1) & hash;
```

当 `n = 16` 时，`n - 1 = 15 = 0b1111`，这个 `&` 运算只保留了 `hash` 的低 4 位。如果 `hashCode()` 的高位区分度很高、而低位却大量冲突（很多类尤其是字符串、自增 id 的哈希低位分布并不均匀），那么高位信息就被白白浪费，哈希表会退化成链表，性能从 O(1) 退化到 O(n)。

JDK8 的解决方式是"扰动函数"：让高位也参与进低位运算。

```java
static final int hash(Object key) {
    int h;
    return (key == null) ? 0 : (h = key.hashCode()) ^ (h >>> 16);
}
```

即把 `hashCode` 的高 16 位与低 16 位做异或，把高位的特征"折叠"进低位。因为 `table` 长度一般不会超过 `1 << 16`，真正参与索引计算的就是低 16 位，所以用异或让高位影响低位，能显著降低低位冲突。

下面这段代码可以直观看到扰动前后的区别：

```java
public class PerturbDemo {
    public static void main(String[] args) {
        // 构造一批低 16 位相同、高 16 位不同的 hashCode
        int[] keys = {0x0000_0001, 0x0001_0001, 0x0002_0001, 0x0003_0001};
        int n = 16;
        for (int hc : keys) {
            int raw  = (n - 1) & hc;                     // 未扰动
            int mix  = (n - 1) & (hc ^ (hc >>> 16));     // 扰动后
            System.out.printf("hc=%08X  raw=%d  mixed=%d%n", hc, raw, mix);
        }
    }
}
```

输出中，未扰动的 `raw` 全部是 `1`（冲突在一个桶），而扰动后的 `mixed` 被分散到不同桶。这就是扰动函数存在的意义。

顺带一提，JDK7 的扰动函数做了四次移位异或，JDK8 简化为一次 `h ^ (h >>> 16)`。原因是引入了红黑树之后，即使发生一定程度的冲突，最坏复杂度也从 O(n) 降到 O(log n)，扰动的"性价比"权衡发生了变化。

## 三、扩容机制：resize 与 JDK8 的重新散列优化

当 `size > capacity * loadFactor`（即 `threshold`）时触发扩容。JDK8 的 `resize()` 做了两个关键优化：**容量翻倍 + 按位分流**，以及**尾插法**。

**容量翻倍与按位分流。** 旧容量 `oldCap = 16`，新容量 `newCap = 32`。旧索引用的是 `hash & 15`（低 4 位），新索引用的是 `hash & 31`（低 5 位）。多出来的那一位（`oldCap` 对应的位）决定了节点去向：

```java
// 简化后的 JDK8 resize 迁移逻辑
Node<K,V> loHead = null, loTail = null; // 留在原索引 j
Node<K,V> hiHead = null, hiTail = null; // 去新索引 j + oldCap
Node<K,V> next;
do {
    next = e.next;
    if ((e.hash & oldCap) == 0) {
        // 低位：留在 j
        if (loTail == null) loHead = e; else loTail.next = e;
        loTail = e;
    } else {
        // 高位：迁移到 j + oldCap
        if (hiTail == null) hiHead = e; else hiTail.next = e;
        hiTail = e;
    }
} while ((e = next) != null);
if (loTail != null) { loTail.next = null; newTab[j] = loHead; }
if (hiTail != null) { hiTail.next = null; newTab[j + oldCap] = hiHead; }
```

`(e.hash & oldCap) == 0` 判断的是"新参与索引的那一位是否为 0"。这一位为 0，节点留在原位 `j`；为 1，节点移动到 `j + oldCap`。于是**每个桶最多被拆成两个桶**，且不需要为每个节点重新计算索引，也不存在头插法导致的链表倒序。

**头插法与尾插法。** JDK7 的迁移使用头插法（新节点插入链表头部），在**单线程**下没有问题，但在多线程同时 `resize` 时会形成循环链表，导致 `get` 进入死循环、CPU 飙到 100%。这是早期最著名的生产事故之一（JDK7 `transfer` 的无限循环 bug）。JDK8 改为尾插法，虽然 **HashMap 本身仍然不是线程安全的**，但至少规避了环形链表这个最致命的问题。

下面这段代码可以用死循环复现 JDK7 的环形链表问题（务必在 JDK7 运行，JDK8 已修复）：

```java
// 仅在 JDK7 下会大概率触发 resize 死循环（环形链表）
public class HashMapDeadLoop {
    public static void main(String[] args) {
        final HashMap<Integer, Integer> map = new HashMap<>(2, 0.75f);
        for (int i = 0; i < 10_000; i++) {
            new Thread(() -> {
                for (int j = 0; j < 100_000; j++) {
                    map.put(j, j);
                }
            }).start();
        }
        // 观察 CPU 使用率：JDK7 下可能 100%
    }
}
```

**核心结论**：并发读写请一律使用 `ConcurrentHashMap`，不要抱有"我读多写少，HashMap 偶尔用用没事"的侥幸心理。

## 四、链表转红黑树：树化的条件与代价

JDK8 引入红黑树是为了防御**哈希碰撞攻击（Hash DoS）**。攻击者可以精心构造一批哈希值相同的 key，把 HashMap 退化成长度为 N 的链表，使单次查询从 O(1) 变成 O(N)，从而拖垮服务。

但树化并非"链表一到 8 就转树"，它有**两个前提**：

1. 链表长度达到 `TREEIFY_THRESHOLD = 8`；
2. 桶数组长度达到 `MIN_TREEIFY_CAPACITY = 64`。

若链表长度已到 8、但 `table.length < 64`，此时会优先选择**扩容**而不是树化——因为在小表里，扩容本身就是缓解冲突更廉价的手段。相关判断在 `treeifyBin` 中：

```java
final void treeifyBin(Node<K,V>[] tab, int hash) {
    int n, index; Node<K,V> e;
    if (tab == null || (n = tab.length) < MIN_TREEIFY_CAPACITY)
        resize(); // 表太小，先扩容
    else if ((e = tabAt(tab, index = (n - 1) & hash)) != null) {
        // 真正开始链表 -> 红黑树
    }
}
```

为什么阈值是 8 而不是别的数字？源码注释给出的解释基于**泊松分布**：在负载因子 0.75 且哈希分布理想的前提下，单个桶内节点数达到 8 的概率约为一亿分之六（约 `6e-8`），已经极其罕见。因此"链表长度 ≥ 8"几乎可以作为"哈希分布异常或遭遇攻击"的信号，此时付出树化的额外成本是值得的。

树化是有成本的，这一点常被忽略：红黑树节点 `TreeNode` 的体积约是普通 `Node` 的两倍，且旋转、变色、查找的常数因子都更大。所以才会设置 `UNTREEIFY_THRESHOLD = 6`，让树在节点减少到 6 时退化回链表，形成 8→6 的"缓冲区间"，避免在边界值附近反复横跳带来的抖动。

## 五、ConcurrentHashMap：从分段锁到 CAS + synchronized

ConcurrentHashMap 在 JDK7 与 JDK8 的实现是完全不同的两套架构，理解这一演进能帮助你判断"网上文章说的到底是哪个版本"。

| 维度 | JDK7（Segment + 锁） | JDK8（CAS + synchronized） |
| --- | --- | --- |
| 并发粒度 | Segment（默认 16 段） | 单个桶（bin） |
| 锁实现 | ReentrantLock | synchronized（锁桶首节点） |
| 写入方式 | 先锁 Segment 再写 | CAS 无锁尝试 + 失败则锁桶 |
| 扩容 | 仅锁当前 Segment | 全表扩容 + 多线程协作迁移 |
| 读操作 | 需加锁（volatile 读） | 完全无锁（Node 可见性保证） |

JDK8 的写路径核心在于 `putVal` 中的**自旋 CAS + 兜底 synchronized**：

```java
// 简化版 ConcurrentHashMap.putVal 的写入逻辑
for (Node<K,V>[] tab = table;;) {
    Node<K,V> f; int n, i, fh;
    if (tab == null || (n = tab.length) == 0)
        tab = initTable(); // 初始化表，用 sizeCtl 保证只初始化一次
    else if ((f = tabAt(tab, i = (n - 1) & hash)) == null) {
        // 桶为空：直接用 CAS 写入，避免加锁
        if (casTabAt(tab, i, null, new Node<K,V>(hash, key, value, null)))
            break;
    }
    else if ((fh = f.hash) == MOVED)
        tab = helpTransfer(tab, f); // 桶正在迁移，帮忙扩容
    else {
        V oldVal = null;
        synchronized (f) { // 锁住桶首节点，只锁这一个桶
            // ... 遍历链表/红黑树，插入或覆盖
        }
    }
}
```

其中 `tabAt` / `casTabAt` / `setTabAt` 底层都是 `Unsafe` 的 `getObjectVolatile` / `compareAndSwapObject` / `putObjectVolatile`，通过 volatile 语义保证跨线程可见性。这就是为什么 **ConcurrentHashMap 的读操作完全无锁**——所有对 `table` 和 `Node.next` 的访问都建立在 volatile 之上。

`initTable` 里 `sizeCtl` 的经典用法也值得讲透：

```java
private final Node<K,V>[] initTable() {
    Node<K,V>[] tab; int sc;
    while ((tab = table) == null || tab.length == 0) {
        if ((sc = sizeCtl) < 0)
            Thread.yield(); // 别的线程正在初始化，让出 CPU
        else if (U.compareAndSwapInt(this, SIZECTL, sc, -1)) {
            // 抢到初始化权：把 sizeCtl 置为 -1
            try {
                if ((tab = table) == null || tab.length == 0) {
                    int n = (sc > 0) ? sc : DEFAULT_CAPACITY;
                    table = tab = (Node<K,V>[])new Node[n];
                    sc = n - (n >>> 2); // 0.75n，即下一次扩容阈值
                }
            } finally {
                sizeCtl = sc;
            }
            break;
        }
    }
    return tab;
}
```

`sizeCtl` 的多重含义是源码阅读者最容易困惑的点：`-1` 表示正在初始化；小于 `-1` 时，`-(1 + 迁移线程数)` 表示有多少线程正在协助扩容；正数表示下一次扩容的阈值。一个字段承担三种语义，是用少量内存换取无锁协调的典型手法。

## 六、扩容协作：ForwardingNode 与 helpTransfer

ConcurrentHashMap 的扩容是**并发协作**的，这也是它与 HashMap 最大的工程差异。扩容期间，旧表 `table` 里已经完成迁移的桶会被替换成一个 `ForwardingNode`，其 `hash = MOVED`，并持有指向新表的引用。

当某个线程在写入或读取时遇到 `ForwardingNode`，说明该桶数据已迁走，它不会等待，而是主动调用 `helpTransfer` 参与迁移——把大表的迁移工作分摊到多个线程身上，这就是"扩容协作"。

```java
// transfer 的核心分工：按 stride 切分迁移区间
final Node<K,V>[] helpTransfer(Node<K,V>[] tab, Node<K,V> f) {
    Node<K,V>[] nextTab; int sc;
    if (tab != null && (f instanceof ForwardingNode) &&
        (nextTab = ((ForwardingNode<K,V>)f).nextTable) != null) {
        int rs = resizeStamp(tab.length) << RESIZE_STAMP_SHIFT;
        while (nextTab == nextTable && table == tab &&
               (sc = sizeCtl) < 0) {
            if ((sc >>> RESIZE_STAMP_SHIFT) != rs || sc == rs + 1 ||
                sc == rs + MAX_RESIZERS || transferIndex <= 0)
                break;
            if (U.compareAndSwapInt(this, SIZECTL, sc, sc + 1)) {
                transfer(tab, nextTab); // 真正干活
                break;
            }
        }
        return nextTab;
    }
    return table;
}
```

迁移区间由 `transferIndex` 记录（从右向左推进），每个协助线程通过 CAS 抢到一段 `stride` 长度的区间独立迁移，互不干扰。迁移完成后，旧表被替换，`sizeCtl` 恢复为正数阈值。

这套机制的价值在于：**扩容期间读请求不阻塞、写请求不空等**。遇到 `MOVED` 就顺手帮忙，既降低了自己的等待时间，也加速了整体迁移。这是性能敏感的高并发场景里，ConcurrentHashMap 能保持稳定吞吐的关键。

## 七、生产环境的坑、调优与排查思路

理论最终要落到线上。下面是我在实际项目中反复验证过的经验。

**坑 1：用可变对象当 key，改字段后"找不到了"。** HashMap/ConcurrentHashMap 的哈希值在 `put` 时计算并存入 `Node.hash`，一旦 key 对象的 `hashCode` 发生变化，旧值就永远无法再通过新哈希定位，导致"内存泄漏式"的脏数据堆积。务必使用不可变对象（String、包装类、带 `final` 字段的自定义类）作为 key。

**坑 2：HashMap 容量估算错误导致频繁扩容。** 若已知要写入约 10 万条数据，用 `new HashMap<>()` 会在写入过程中反复 `resize`，每次扩容都要重建数组并迁移节点，CPU 与 GC 压力巨大。正确做法是按"元素数量 / 0.75"预分配：

```java
int expected = 100_000;
Map<String, Object> map = new HashMap<>((int)(expected / 0.75f) + 1);
```

**坑 3：小表 + 高并发下的 `size()` 与遍历一致性。** `ConcurrentHashMap.size()` 与 `keySet()` 都是弱一致快照，迭代过程中可能看不到新写入的数据。不要用它的 `size()` 做精确的业务计数，需要精确计数请用 `AtomicLong` 或专门的计数器。

**坑 4：`computeIfAbsent` 的递归更新死锁/活锁。** 在 JDK8 早期版本，`computeIfAbsent` 内部用 `ReservationNode`（`hash = RESERVED`）占位，递归地对同一 key 做 `computeIfAbsent` 会触发 `IllegalStateException: Recursive update`。这类嵌套更新在生产中常由"缓存回填逻辑里又触发了回填"引起，排查时要重点看调用栈里是否有对同一 map 的二次写入。

**排查思路与工具。** 遇到"HashMap 操作卡顿 / 内存占用异常"时，按以下顺序排查：

1. 用 `jmap -histo:live` 看 `HashMap$Node` / `TreeNode` 实例数量，判断是否有 key 泄漏或桶异常膨胀；
2. 用 `jstack` 抓线程栈，若大量线程停在 `HashMap.transfer` 或 `ConcurrentHashMap.transfer`，说明正在疯狂扩容或遭遇迁移热点；
3. 用 `-XX:+PrintGCDetails` 观察 GC，频繁 Young GC 往往伴随扩容产生的短命大对象；
4. 结合监控看单 key 的哈希分布，若某桶链表显著过长，优先排查 key 的 `hashCode` 实现是否合理。

**调优建议。**

```yaml
# 生产环境常见配置建议（示例）
map预分配:
  规则: "容量 = 预期元素数 / 0.75，向上取 2 的幂"
并发场景:
  读写分离: "一律使用 ConcurrentHashMap，禁止并发共享 HashMap"
  高并发写: "关注分片粒度，必要时按业务键预分片降低单桶竞争"
key选择:
  优先: "String / Integer / 不可变自定义对象"
  避免: "数组、可变对象、hashCode 分布极差的类"
```

## 小结与建议

- **哈希扰动**：`(h = hashCode) ^ (h >>> 16)` 解决的是"索引只取低位导致高位信息浪费"的问题，是 JDK8 对 JDK7 四次扰动的简化。
- **扩容**：容量翻倍 + 按 `e.hash & oldCap` 分流到 `j` 与 `j + oldCap` 两个桶，每个桶最多拆成两个；尾插法规避了 JDK7 头插法的环形链表事故。
- **树化**：链表 ≥ 8 **且** 表长度 ≥ 64 才转红黑树，否则优先扩容；阈值 8 来自泊松分布下约 `6e-8` 的碰撞概率；退化阈值 6 形成缓冲区间。
- **ConcurrentHashMap 写**：桶空用 CAS 无锁写入，桶非空 `synchronized` 锁桶首节点；读完全无锁，依赖 volatile 可见性。
- **ConcurrentHashMap 扩容**：`ForwardingNode`（`hash = MOVED`）标记已迁移桶，`helpTransfer` 让写线程协作迁移，`sizeCtl` 负数编码迁移线程数。
- **实践红线**：并发共享数据只认 `ConcurrentHashMap`；用不可变 key；预分配容量；别把 `size()`/迭代当精确快照；排查优先看节点数量、线程栈与 GC。

把这些"为什么"吃透，你就能在面对"HashMap 为什么是 2 的幂""ConcurrentHashMap 为什么读不加锁"这类问题时，给出源码级的、有实战分量的回答，而不是停留在背诵结论。
