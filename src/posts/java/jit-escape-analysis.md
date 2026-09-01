---
title: "JIT 编译器：逃逸分析、标量替换与内联优化"
date: 2023-01-14
category:
  - Java
tag:
  - JVM
  - JIT
  - 性能
icon: code
---

# JIT 编译器：逃逸分析、标量替换与内联优化

很多人对 JVM 的理解停留在"解释执行 + 垃圾回收"的层面，却忽略了真正决定吞吐上限的那一层——即时编译器（JIT Compiler）。HotSpot 之所以能在长时间运行后逼近甚至反超静态编译语言，靠的不是把字节码逐条翻译成机器码这么简单，而是一整套基于运行时反馈的激进优化：分层编译、内联、逃逸分析、标量替换、同步消除。这些优化彼此咬合，任何一环失效都可能让后续优化整条链断裂。

本文不打算把 JIT 讲成教科书，而是从生产排障的视角出发，讲清楚三个问题：这些优化**在什么条件下才会发生**、**如何验证它真的发生了**、以及**它失效时的典型症状是什么**。

## 从解释执行到分层编译

HotSpot 默认运行在混合模式（`-Xmixed`）下。方法最初由 C1（Client Compiler，也叫 Compiler）或解释器处理，热点方法晋升到 C2（Server Compiler）做深度优化。这个"先用简单编译器快速预热、再用激进编译器深度优化"的机制，就是分层编译（Tiered Compilation，JDK 8 起默认开启）。

分层编译把执行路径分为五层：

| 层级 | 名称 | 特点 |
| ---- | ---- | ---- |
| Level 0 | Interpreter | 解释执行，采集调用次数与循环回边计数 |
| Level 1 | C1 no profiling | 无剖析信息的 C1 编译，启动快 |
| Level 2 | C1 with limited profiling | 轻量剖析，收集调用/分支计数 |
| Level 3 | C1 with full profiling | 完整剖析，为 C2 收集类型/虚方法接收者等数据 |
| Level 4 | C2 | 激进优化，基于 Level 3 收集的 profile |

Level 2/3 的核心价值在于**为 C2 提供运行时类型信息**。Java 是动态分派语言，没有这些 profile，C2 无法把虚方法调用"锁定"到具体实现，内联也就无从谈起。这就是为什么冷启动时性能平平，跑了几分钟吞吐才上去——C2 需要 profile 数据喂饱它。

生产环境里一个常见误区是"为了让 JIT 生效就盲目预热"。实际上，C1 的优化空间有限，真正拉开差距的是 C2。可以用 `-XX:+PrintCompilation` 观察方法在各层级间的迁移：

```bash
java -XX:+PrintCompilation -XX:+UnlockDiagnosticVMOptions \
     -XX:+PrintInlining com.example.Benchmark
```

输出类似：

```text
    111   45       3       com.example.Parser::parse (86 bytes)
    112   46       4       com.example.Parser::parse (86 bytes)
```

第二列是编译任务编号，第三列是编译层级。`3 -> 4` 表示该方法被 C1 编译并携带剖析信息运行一段时间后，又触发了 C2 重编译。如果某个热点方法长期停留在 Level 3，说明它没通过 C2 的门槛，需要排查是否被"去优化"频繁打回。

## 内联：一切优化的地基

所有 JIT 优化的基石是**方法内联（Inlining）**。逃逸分析只能在方法边界内做，同步消除和标量替换的前提也都是代码被"摊平"到一个方法里。如果内联失败，后面的优化基本全军覆没。

内联并非"代码越长越好"。HotSpot 用 `-XX:MaxInlineSize`（默认 35 字节）限制普通方法内联的字节码大小，用 `-XX:FreqInlineSize`（默认 325 字节）放宽对热点方法的内联限制。超过 `-XX:MaxInlineLevel`（默认 9）的嵌套深度也会被拒绝。还有一条容易踩坑的规则：如果某个方法**在 C2 编译时还没有对应的 profile**，它会被视为"冷方法"，内联收益按负值计，从而被 C2 直接排除。

一个典型的反模式是"大而全的工具方法"：

```java
public class Parser {
    // 一个 400+ 字节的巨型方法，几乎不可能被内联
    public Result parse(byte[] input) {
        validate(input);          // 假设 60 字节
        Header h = parseHeader(input);
        Body b = parseBody(input);
        return assemble(h, b);    // 假设 80 字节
    }
}
```

与其写一个 400 字节的 `parse`，不如拆成多个 20~40 字节的小方法。小方法不仅是好代码，也是 JIT 的"口粮"。C2 内联后形成一块连续的、无调用开销的、可以被进一步优化的代码区域，这才是它偏爱的形态。

排查内联是否生效，用 `-XX:+PrintInlining` 会得到最直接的证据：

```text
@ 12   com.example.Parser::validate (60 bytes)   inline (hot)
@ 25   com.example.Utils::log (98 bytes)         too big
```

`inline (hot)` 表示命中；`too big`、`not callee is too large`、`no static binding` 都是典型失败原因。看到某个高频方法标着 `too big`，就该考虑拆分了。

## 逃逸分析：从"分配在堆上"到"分配在栈上"

对象在 Java 里默认分配在堆上，这给 GC 带来持续压力。逃逸分析（Escape Analysis，EA）要回答的问题是：**一个对象是否逃出了创建它的方法或线程**。如果答案是否定，JIT 就获得了巨大的优化空间。

逃逸分为三类：

- **不逃逸（NoEscape）**：对象只在当前方法内使用，方法返回后不可达。
- **参数逃逸（ArgEscape）**：对象作为参数传给被调用方法，但仍未逃出线程。
- **全局逃逸（GlobalEscape）**：对象被返回、存入静态字段、或逃逸到其它线程，彻底失控。

逃逸分析默认开启（`-XX:+DoEscapeAnalysis`），从 JDK 6u23 起稳定可用。它不是免费的午餐：EA 本身要消耗 C2 编译时间，所以只在 C2 层做，C1 不做。这也解释了为什么分层编译下，EA 的效果要等方法升到 Level 4 才显现。

验证 EA 是否生效的最硬核手段是打印 JIT 生成的汇编。用 `-XX:+PrintAssembly` 需要 hsdis 插件，更轻量的办法是看 `-XX:+PrintEscapeAnalysis`（需 `-XX:+UnlockDiagnosticVMOptions`），它会输出每个方法的逃逸分析结果。

### 消除堆分配：标量替换

EA 生效后最直观的收益是**标量替换（Scalar Replacement）**：把对象的字段拆成独立的局部变量，用寄存器/栈槽位代替堆对象。

```java
public class Point {
    private final int x;
    private final int y;

    public Point(int x, int y) { this.x = x; this.y = y; }
    public int getX() { return x; }
    public int getY() { return y; }
}

public static long centroid(Point[] pts) {
    long sx = 0, sy = 0;
    for (Point p : pts) {
        // 每次迭代 new 一个 Point，若未逃逸则被标量替换
        p = new Point(p.getX() + 1, p.getY() + 1);
        sx += p.getX();
        sy += p.getY();
    }
    return sx + sy;
}
```

在 `-XX:+DoEscapeAnalysis` 下，`new Point(...)` 的堆分配被消除，`p.getX()`/`p.getY()` 直接变成对 `x`/`y` 寄存器值的操作。验证时可用 GC 日志观察：开启 EA 时这段循环几乎不产生新生代垃圾。

`-XX:+EliminateAllocations`（默认开启）控制标量替换，`-XX:+PrintEliminateAllocations` 可以看到哪些分配被消除。做基准测试时，务必区分"对象真的被消除了"和"对象只是被 TLAB 快速分配掩盖了"，否则会得出错误的性能结论。

### 消除同步：锁消除

EA 的第二个收益是**锁消除（Lock Elision）**，由 `-XX:+EliminateLocks` 控制（默认开启）。如果一个对象不会逃出当前线程，那么对它加锁/解锁就纯属浪费——没有竞争，锁保护就失去了意义。

```java
public String concat(String a, String b) {
    // StringBuilder 未逃逸，内部用 synchronized 保护，
    // 此处的锁会被消除
    StringBuilder sb = new StringBuilder();
    return sb.append(a).append(b).toString();
}
```

这个例子里 `StringBuilder` 的 `append`/`toString` 在 JDK 9 之前使用 `synchronized` 保证内部状态安全，但对象不逃逸，锁消除后这些同步开销直接归零。这是 EA 对字符串拼接类代码如此有效的原因。

一个需要警惕的反例：**把局部对象写进静态缓存或返回给调用者，锁消除立即失效**。

```java
private static StringBuilder CACHE;

public String bad(String a) {
    StringBuilder sb = new StringBuilder();
    sb.append(a);
    CACHE = sb;      // 全局逃逸：对象逃到静态字段
    return sb.toString();
}
```

一旦发生全局逃逸，EA 无法证明对象不被其它线程访问，锁消除和标量替换同时失效，代码回到笨重的堆分配 + 同步路径。生产环境排查这类问题时，重点看对象是否被存进了 `static` 集合、单例、线程池队列或消息队列。

## 用 PrintCompilation 与 JITWatch 定位问题

`-XX:+PrintCompilation` 是线上排查 JIT 的第一把刀，但它输出密度高、易淹没关键信息。建议配合以下参数缩小范围：

```bash
java -XX:+PrintCompilation \
     -XX:+UnlockDiagnosticVMOptions \
     -XX:+PrintInlining \
     -XX:+PrintOptoAssembly \
     -XX:CompileCommand=print,com.example.HotSpot::method \
     com.example.Main
```

- `-XX:CompileCommand=print,...`：只打印指定方法的汇编与内联树，避免被全量输出淹没。
- `-XX:CompileCommand=inline,com.example.Foo::bar` / `exclude`：强制/禁止某个方法内联，用于 A/B 验证。
- `-XX:CompileCommand=dontinline,...`：排查内联是否"帮倒忙"时反向验证。

JITWatch 是 Chris Newland 维护的开源工具，能把 HotSpot 的编译日志（`-XX:+LogCompilation -XX:LogFile=hotspot.log`）解析成可视化界面。它最有价值的三个视图：

| 视图 | 用途 |
| ---- | ---- |
| Compilation Timeline | 观察方法的层级迁移与去优化时间点 |
| Inline Tree | 逐层查看内联决策与失败原因 |
| Assembly View | 对照源码看标量替换、锁消除后的真实汇编 |

排障时的一个典型套路是：先看 **Inline Tree** 确认内联是否成功，再看 **Compilation Timeline** 里有没有频繁的"去优化（Deoptimization）"。如果方法反复在 Level 3 和 Level 4 之间横跳，说明 C2 基于 profile 做出的激进假设不断被运行时推翻，此时应检查是否存在大量虚方法调用或类加载导致的"类层次分析（CHA）"失效。

## 生产环境中的坑与调优建议

**坑一：过早优化反而拖慢启动。** EA 和标量替换有编译成本，无脑开启全部诊断参数会显著拖慢启动阶段。线上服务不要把 `-XX:+PrintCompilation` 之类的参数长期开启，只在排障窗口临时加。

**坑二：`final` 字段与 `String` 的特殊性。** 逃逸分析对 `final` 字段、`String`、装箱类型（`Integer`/`Long`）有专门优化路径。如果你在热点路径里疯狂 `new Integer(...)` 装箱，EA 能消掉一部分，但更根本的做法是避免无意义的装箱。

**坑三：容器类是最常见的"逃逸泄露点"。** `ArrayList`、`HashMap` 一旦被返回、被存进字段或被 put 进另一个容器，就立即逃逸。写热点代码时要有意识地判断"这个容器能不能只活在方法栈里"。

**坑四：反射、JNI、`System.identityHashCode` 会破坏 EA。** 对对象的反射访问、传给本地方法、以及调用 `identityHashCode`（会用到对象头）都会让对象"被观察"，从而迫使它逃逸到堆上。

可操作的调优清单：

- 热点方法拆小，控制在 35 字节左右，嵌套不过深。
- 避免在循环里创建逃逸对象，优先让对象不逃逸。
- 不要随意把局部对象塞进静态缓存或单例。
- 用 `-XX:+PrintInlining` 定期抽查热点路径的内联树。
- 用 `-XX:+LogCompilation` + JITWatch 做离线分析，别在生产直连诊断输出。
- 基准测试至少预热到 C2 稳定，否则测的只是 C1 甚至解释器。

## 小结与建议

- **分层编译是前提**：方法先经 C1 收集 profile，再升 C2 做深度优化；热点方法长期停留在低层级需要警惕。
- **内联是地基**：小方法、浅调用、可静态绑定的调用更易内联；`too big` 是内联失败的头号原因。
- **逃逸分析是杠杆**：不逃逸的对象才可能被标量替换和锁消除，逃逸一旦发生，整条优化链断裂。
- **验证优于猜测**：用 `-XX:+PrintCompilation`、`-XX:+PrintInlining`、`-XX:+LogCompilation` + JITWatch 拿到证据，而不是靠感觉调参。
- **分清手段与目标**：诊断参数服务于排查，长期开启是负优化；最终目标是把热点代码写成 JIT 友好的形态，而非堆砌参数。
