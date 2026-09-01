---
title: "深入 JMM：Java 内存模型与 happens-before 规则全解析"
date: 2022-01-08
category:
  - Java
tag:
  - JMM
  - 并发
  - happens-before
icon: code
---

# 深入 JMM：Java 内存模型与 happens-before 规则全解析

很多工程师对并发的理解停留在"加个 `synchronized`、给字段加 `volatile` 就安全了"的层面，一旦线上出现偶发的数据错乱、死循环或者读到"半个对象"，就无从下手。这类问题的根源，几乎都可以追溯到 Java 内存模型（Java Memory Model，JMM）。JMM 不是 JVM 的某个实现细节，而是 Java 语言规范第 17 章明确定义的一套规则：它规定了在什么条件下，一个线程对共享变量的写对另一个线程可见，以及在什么条件下允许编译器与处理器进行指令重排序。

本文不打算复述规范原文，而是从重排序、可见性、原子性三个切入点，把 happens-before 的每一条规则和 `volatile` / `final` 的内存语义讲透，并结合真实生产环境的 bug 案例给出排查思路与调优建议。

## 一、三个起点：重排序、可见性、原子性

并发编程的一切诡异现象，都可以归结为三类问题。

**重排序（Reordering）**：为了提高性能，编译器、JIT 和处理器会在"不影响单线程执行结果"的前提下，调整指令的执行顺序。这个"不影响"的判据是 **as-if-serial**：只保证单线程语义不变，多线程下就会被放大成灾难。

```java
public class ReorderExample {
    private int a = 0;
    private boolean flag = false;

    public void writer() {
        a = 1;          // 1
        flag = true;    // 2
    }

    public void reader() {
        if (flag) {                 // 3
            int i = a * a;          // 4  可能读到 a == 0
        }
    }
}
```

单线程看 `writer()` 内部，`1` 和 `2` 之间没有数据依赖，处理器完全可能先执行 `flag = true` 再执行 `a = 1`。此时另一个线程在 `reader()` 中看到 `flag == true` 后，`a` 可能还没有被写入，于是 `i` 计算出 `0`。这就是经典的"先看到标志位、后看到数据"的乱序。

**可见性（Visibility）**：每个线程都有自己的工作内存（寄存器、CPU 缓存、Store Buffer），对共享变量的修改不一定立刻刷回主内存，也不一定立刻被其他线程读到。

**原子性（Atomicity）**：像 `long`、`double` 这种 64 位类型，在 32 位 JVM 上允许被拆成两次 32 位读写。`i++` 这种"读-改-写"复合操作本身也不是原子的，两个线程同时执行可能各自加一但最终只加了一次。

这三类问题层层叠加，是理解 JMM 的主线。JMM 的核心工作，就是用一套规则告诉开发者：**哪些操作在多线程下是有序且可见的，哪些是不确定的。**

## 二、JMM 的抽象结构：主内存与工作内存

JMM 用一个抽象模型来屏蔽底层硬件的差异：

| 概念 | 说明 | 对应现实 |
| --- | --- | --- |
| 主内存 | 所有线程共享的变量存储区域 | 堆内存 + 主存 |
| 工作内存 | 每个线程私有的变量副本 | 寄存器、L1/L2/L3 缓存、Store Buffer |
| 线程间通信 | 必须通过主内存完成 | 变量刷回主存 + 缓存一致性协议 |

关键点在于：**线程 A 修改了变量，线程 B 不能直接"看到"，必须先由 A 把值刷新到主内存，再由 B 从主内存重新读取。** 这个刷新与读取的时机，就是 JMM 通过 `volatile`、`synchronized`、`final` 等关键字和 happens-before 规则来约束的。

需要特别纠正一个常见误解：JMM 是 **抽象的内存模型**，它不强制要求 JVM 真的用"主内存 + 工作内存"这套物理结构。JVM 只需要保证"行为符合 JMM 规范"即可，具体用 Store Buffer 还是缓存一致性协议，是实现细节。所以你在内存中看到的"变量副本"并不一定真的存在，不要用物理内存去硬套这个模型。

## 三、happens-before 规则逐条解析

happens-before（先行发生）是 JMM 中最核心的概念。如果操作 A happens-before 操作 B，那么 **A 的执行结果对 B 可见，且 A 的执行顺序排在 B 之前**。注意它是"偏序关系"，不是全序：两个操作之间如果没有 happens-before 关系，它们的执行顺序就是不确定的，JVM 可以随意重排序。

规范中定义了若干条规则，这里逐条拆解。

**1. 程序次序规则（Program Order Rule）**：在一个线程内，按照代码顺序，前面的操作 happens-before 后面的操作。注意这条规则只约束**单线程内**，且允许重排序——因为 as-if-serial 保证单线程结果不变，重排序后"语义上"仍然满足程序次序。

**2. 监视器锁规则（Monitor Lock Rule）**：对一个锁的解锁 happens-before 后续对这个锁的加锁。这是 `synchronized` 保证可见性的理论依据。

**3. volatile 变量规则（Volatile Variable Rule）**：对一个 `volatile` 变量的写 happens-before 后续对这个变量的读。

**4. 线程启动规则（Thread Start Rule）**：主线程启动子线程（`thread.start()`）之前的操作 happens-before 子线程中的任何操作。

**5. 线程终止规则（Thread Termination Rule）**：线程中的所有操作 happens-before 其他线程检测到该线程终止（`thread.join()` 返回、`thread.isAlive()` 返回 false）。

**6. 线程中断规则（Thread Interruption Rule）**：对线程 `interrupt()` 的调用 happens-before 被中断线程检测到中断事件（通过 `interrupted()` 或 `isInterrupted()` 抛出 `InterruptedException`）。

**7. 对象终结规则（Finalizer Rule）**：一个对象的初始化完成 happens-before 它的 `finalize()` 方法的开始。

**8. 传递性（Transitivity）**：如果 A happens-before B，且 B happens-before C，那么 A happens-before C。这条规则是构建复杂可见性链条的基础，也是最容易被忽略的一条。

把这些规则整理成一张速查表：

| 规则 | 先行操作 | 后续操作 | 典型用途 |
| --- | --- | --- | --- |
| 程序次序 | 线程内前面的操作 | 线程内后面的操作 | 单线程顺序保证 |
| 监视器锁 | 解锁 | 加锁 | `synchronized` 可见性 |
| volatile | volatile 写 | volatile 读 | 状态标志、双重检查锁 |
| 线程启动 | `start()` 前 | 子线程任意操作 | 传递初始化数据 |
| 线程终止 | 线程内任意操作 | `join()` 返回后 | 收集线程执行结果 |
| 线程中断 | `interrupt()` | 检测到中断 | 优雅取消任务 |
| 传递性 | A → B、B → C | 推出 A → C | 组合出可见性链条 |

传递性规则是排查复杂 bug 的利器。看一个经典例子：

```java
public class TransitivityExample {
    private int x = 0;
    private volatile boolean v = false;

    public void writer() {
        x = 42;         // A
        v = true;       // B   volatile 写
    }

    public void reader() {
        if (v) {        // C   volatile 读
            int r = x;  // D   能保证读到 42
        }
    }
}
```

分析：A happens-before B（程序次序），B happens-before C（volatile 规则，因为 B 是对 `v` 的写，C 是对 `v` 的读），再根据传递性，A happens-before C；又 D 在 C 之后，由程序次序 C happens-before D，继续传递得到 A happens-before D。因此 `reader()` 只要看到 `v == true`，就**一定能**读到 `x == 42`。这就是 `volatile` 的"写前所有操作对后续读可见"的底层原理。

## 四、volatile 的内存语义

`volatile` 是最轻量的同步机制，但它**只保证可见性和有序性，不保证原子性**。它的内存语义可以拆成两面：

**写 `volatile` 变量时**，JMM 会把该线程工作内存中、写操作之前的所有普通变量一并刷新到主内存。

**读 `volatile` 变量时**，JMM 会把该线程工作内存置为无效，强制从主内存重新读取。

再叠加 happens-before 的传递性，就得到两条实用结论：

1. 线程 A 写 `volatile` 之前的所有写操作，对线程 B 读该 `volatile` 之后的所有读操作可见。
2. `volatile` 禁止编译器与处理器对它进行重排序（通过内存屏障实现），但**不保证**复合操作的原子性。

所以下面这段代码是错的：

```java
public class Counter {
    private volatile int count = 0;

    public void increment() {
        count++;   // 不是原子的！读-改-写三步，多线程下会丢更新
    }
}
```

`count++` 会被编译成"读 count → 加 1 → 写 count"，两个线程可能同时读到 5，各自加 1 后都写回 6，最终丢失一次更新。**`volatile` 解决不了竞态条件（race condition），只能保证读到的值是最新的。**

`volatile` 的正确使用场景有：

- **状态标志位**：如 `running`、`shutdown`，一个线程写、其他线程读，天然满足"写 happens-before 读"。
- **单例的双重检查锁（DCL）**：

```java
public class Singleton {
    private static volatile Singleton instance;

    private Singleton() {}

    public static Singleton getInstance() {
        if (instance == null) {                    // 第一次检查
            synchronized (Singleton.class) {
                if (instance == null) {            // 第二次检查
                    instance = new Singleton();     // 这里的 volatile 必不可少
                }
            }
        }
        return instance;
    }
}
```

为什么这里 `volatile` 不能省？因为 `new Singleton()` 不是一个原子操作，它分为：分配内存 → 初始化对象 → 将引用赋给 `instance`。**在没有 `volatile` 时，后两步可能重排序**：先把未初始化的对象引用赋给 `instance`，再执行初始化。此时另一个线程通过第一次 `if (instance == null)` 检查时，看到 `instance` 非空，直接返回一个**尚未完成构造的对象**，进而触发空指针或读到错误字段。`volatile` 禁止了这种重排序，保证 `instance` 对外可见时对象已经完整构造。

- **读写锁的轻量替代**：读多写少的场景下，用 `volatile` 变量代替锁做"发布"。

## 五、final 的内存语义与对象逸出

`final` 字段的语义经常被低估。JMM 对 `final` 字段的保证是：

1. 在**构造函数内**对一个 `final` 字段的写入，与**随后**把这个对象的引用赋值给其他线程可见的引用之间，存在 happens-before 关系。
2. 换句话说，只要对象是**通过"安全发布"**（例如在构造函数完成后，再把引用赋给 `volatile` 字段、放入并发集合、或通过 `start()` 传给子线程）暴露给其他线程的，那么其他线程**无需同步**就能看到 `final` 字段的正确值。
3. 对 `final` 字段的读，JIT 会把它当作常量，甚至可以做常量折叠等优化。

但 `final` 有一个致命前提：**对象不能在构造期间逸出（this 逸出）**。

```java
public class SafeListener {
    private final int value;

    public SafeListener(EventSource source) {
        source.registerListener(new EventListener() {
            public void onEvent(Event e) {
                doSomething(value);   // 能保证 value 已正确初始化
            }
        });
        value = 10;   // 危险！this 通过匿名内部类逸出，value 此时还是 0
    }
}
```

上面的代码把 `value` 的赋值放在注册监听器**之后**，而匿名内部类隐式持有外部类的 `this` 引用，导致对象在构造完成前就被"发布"给了外部世界。此时回调线程读到 `value` 可能是默认值 0，而不是 10。修复方法是：**先完成所有 `final` 字段的赋值，最后再注册/发布**，或者干脆把监听器抽出来、延迟注册。

实践中还有一个常见坑：老代码里用 `final` 字段 + 不加锁的方式发布不可变对象，以为是安全的。JMM 的保证只对**正确构造且未逸出**的对象成立，一旦对象逸出过，`final` 语义失效，之前的"安全"就崩塌了。

## 六、真实并发 bug 案例与排查思路

下面是一个我在生产环境真实遇到过的"死循环"故障的简化版本，它把可见性和重排序问题压缩到了极致：

```java
public class VisibilityBug {
    private static boolean stop = false;   // 注意：没有 volatile

    public static void main(String[] args) throws InterruptedException {
        Thread worker = new Thread(() -> {
            int i = 0;
            while (!stop) {   // 可能永远读不到 stop == true
                i++;
            }
            System.out.println("worker stopped, i = " + i);
        });
        worker.start();

        Thread.sleep(1000);
        stop = true;          // 主线程写 stop，但 worker 可能看不到
        System.out.println("main set stop = true");
    }
}
```

`stop` 没有 `volatile` 修饰，也没有任何 happens-before 关系连接"主线程写 stop"和"worker 读 stop"，因此 JIT 完全可以把 `while (!stop)` 优化成 `if (!stop) while (true)`——因为 JIT 从 worker 线程的视角看，`stop` 从未被修改，是个"常量"。结果就是 worker 线程陷入死循环，即使主线程已经写了 `stop = true`。

**排查思路**：遇到"改了值但另一线程读不到"的问题，第一步不是猜，而是：

1. 用 `jstack <pid>` 抓线程栈，确认目标线程卡在哪个循环或等待点。
2. 用 `jinfo <pid>` 查看 JVM 参数，确认是否开着 `-XX:+PrintCompilation` 观察 JIT 行为，判断循环是否被 JIT 优化。
3. 检查共享变量是否有 `volatile`、访问是否被 `synchronized` 或 `Lock` 包裹、是否存在 happens-before 链条。
4. 可以用 `-XX:+UnlockDiagnosticVMOptions -XX:+PrintAssembly`（需要 hsdis 插件）看 JIT 生成的汇编，确认是否真的发生了常量折叠或重排序。
5. 修复原则：**不要靠"感觉"和"本地复现不了"来判断，而是要建立显式的 happens-before 关系。** 给 `stop` 加 `volatile` 是最直接的解法。

另一个高频坑是**延迟初始化 + 无 volatile 的 DCL**，已经在第四节讲过；还有 **64 位变量的撕裂读（word tearing）**：在 32 位 JVM 上，无 `volatile` 的 `long`/`double` 可能被读成"半个新值 + 半个旧值"，这是最隐蔽的数据损坏来源之一。

## 七、调优参数与实践建议

JMM 的可见性保证通常不是"免费"的，内存屏障会带来一定的性能开销，但相比于数据正确性，这些开销几乎总是值得的。以下是与 JMM 直接相关的调优与排查参数：

```bash
# 观察 JIT 编译情况，排查"循环被优化掉"类问题
java -XX:+PrintCompilation -XX:+UnlockDiagnosticVMOptions \
     -XX:+PrintAssembly -XX:CompileCommand=print,*VisibilityBug.worker ...

# 关闭某些会放大可见性问题的优化，仅用于临时验证，勿用于生产
-XX:-EliminateLocks          # 关闭锁消除
-XX:-UseBiasedLocking        # 关闭偏向锁（JDK 15 已默认移除偏向锁）

# 抓线程栈定位卡点
jstack <pid> > thread.dump
```

关于 `volatile` 与锁的性能对比，有一个经验结论：**现代 CPU 上，`volatile` 读的成本接近普通读**（x86 上读不会加额外屏障），而 `volatile` 写会触发 StoreLoad 屏障，成本比普通写高，但仍远低于无竞争下的 `synchronized`。因此在"一写多读"的标志位场景，`volatile` 是首选。

实践建议：

1. **能不可变就不可变**：用 `final` 字段 + 安全发布，是成本最低的线程安全方案。
2. **能用 `volatile` 就不用锁**：仅当语义是"单写多读 + 无需原子复合操作"时。
3. **需要原子复合操作时用 `AtomicInteger` / `LongAdder`**：`AtomicInteger` 基于 CAS 提供原子性，同时底层 `value` 是 `volatile`，可见性也一并解决。
4. **复杂同步逻辑优先用 `java.util.concurrent` 并发容器**，而不是手写"锁 + 标志位"，前者已经把 happens-before 关系封装好了。

## 小结与建议

- 并发问题只有三类：**重排序、可见性、原子性**，JMM 的使命就是给它们划定明确的行为边界。
- happens-before 是**偏序关系**，两条操作之间没有它，顺序就是不确定的；传递性规则是串联可见性链条的关键。
- `volatile` 只保证**可见性 + 有序性**，**不保证原子性**；`count++` 这类复合操作请改用 `AtomicInteger`。
- DCL 单例里的 `volatile` 不能省略，它阻止了"先发布引用、后完成构造"的重排序。
- `final` 字段在**安全发布且不逸出**的前提下无需同步即可见，但要警惕 `this` 在构造期间逸出。
- 排查可见性 bug 的路线：抓线程栈 → 看 JIT 编译 → 核对 happens-before 链条 → 用显式同步或 `volatile` 建立关系，而不是靠复现运气。
- 优先选择不可变对象、`volatile` 标志位、原子类与并发容器，把 JMM 的复杂度封装在正确的抽象里。
