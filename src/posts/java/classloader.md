---
title: "类加载机制与双亲委派模型深入剖析"
date: 2022-12-17
category:
  - Java
tag:
  - JVM
  - 类加载
  - ClassLoader
icon: code
---

# 类加载机制与双亲委派模型深入剖析

类加载机制是 JVM 中最容易被"背八股文"却最难真正吃透的一块。很多人能脱口而出"双亲委派模型"六个字，却说不清 `ClassLoader.loadClass` 和 `Class.forName` 的初始化时机差异，也解释不了 SPI 为什么要打破双亲委派、以及 Tomcat 为什么每个 Web 应用都要有独立的 ClassLoader。本文从字节码加载的完整生命周期出发，结合生产环境的真实踩坑，把这一机制讲透。

## 一、类从字节码到实例：五个阶段的完整链路

一个 `.class` 文件从磁盘到真正可用，要经历**加载（Loading）→ 验证（Verification）→ 准备（Preparation）→ 解析（Resolution）→ 初始化（Initialization）**五个阶段。前三个阶段通常被笼统称为"加载"，但实际上语义完全不同，很多线上问题的根源就是混淆了这些阶段。

**加载阶段**做的事只有三件：通过全限定名获取二进制字节流、将字节流转换为方法区的运行时数据结构、在堆中生成一个 `java.lang.Class` 对象作为访问入口。注意，此时类还未初始化，连静态字段都还没有赋值。字节流的来源非常灵活——除了本地 classpath，还可以来自网络、jar 包、动态代理生成的字节码，甚至内存中的字节数组。

**验证阶段**是 JVM 的第一道安全防线。它会校验 class 文件的魔数（`0xCAFEBABE`）、版本号、常量池引用、字节码指令合法性、符号引用等。这一步的目的是防止恶意或损坏的字节码危害 JVM 自身。生产环境里如果做过字节码增强（ASM、ByteBuddy、AspectJ），大概率遇到过 `VerifyError`——这几乎都是字节码被改写后验证失败导致的。

**准备阶段**是新手最容易误解的：它为类变量（static 字段）分配内存并设置**类型零值**，而不是代码里写的初始值。例如 `static int value = 123;` 在准备阶段 `value` 是 `0`，真正的 `123` 要等到初始化阶段才赋值。唯一例外是 `static final` 修饰的常量（ConstantValue 属性），它在准备阶段就会被赋值为编译期常量。

**解析阶段**把常量池内的符号引用替换为直接引用。所谓符号引用就是"`com.foo.Bar` 这个类、`bar` 这个字段"这样的文本描述，直接引用则是指向方法区目标的内存地址或句柄。JDK 9 之后为了支持 CDS（Class Data Sharing）和 AOT，解析被区分为"静态解析"和"动态解析"，而且解析动作可以延迟到真正使用符号引用时才发生。

**初始化阶段**才真正执行类构造器 `<clinit>()`，也就是静态代码块和静态变量赋值。这也是唯一一个会执行用户代码的阶段。

## 二、初始化时机：loadClass 与 forName 的致命差异

`ClassLoader.loadClass(String name)` 与 `Class.forName(String name)` 是两个高频 API，但它们的行为差异在关键时刻会引发线上事故。

```java
public class LoadDiff {
    public static void main(String[] args) throws Exception {
        // 方式一：只加载，不触发初始化
        Class<?> a = ClassLoader.getSystemClassLoader().loadClass("com.example.Sample");

        // 方式二：加载 + 连接 + 初始化
        Class<?> b = Class.forName("com.example.Sample");

        // 方式三：指定不初始化（initialize = false）
        Class<?> c = Class.forName("com.example.Sample", false, LoadDiff.class.getClassLoader());
    }
}
```

最典型的坑出现在 JDBC 时代。早期代码里 `Class.forName("com.mysql.jdbc.Driver")` 是必须的，因为 Driver 类在 `<clinit>` 里通过 `DriverManager.registerDriver()` 注册自己。如果你改用 `loadClass`，Driver 不会被注册，后续 `getConnection` 就会抛 `No suitable driver found`。这也是为什么很多框架的插件扫描如果误用 `loadClass` 去探测"带初始化副作用的类"，会导致注册逻辑静默丢失。

主动使用触发初始化的条件有且仅有六种：`new`、读写静态字段（final 常量除外）、调用静态方法、反射调用、初始化子类时先初始化父类、以及作为 `main` 方法所在类启动时。被动引用（如通过子类引用父类静态字段、定义数组、引用编译期常量）都不会触发初始化。理解这一点，才能解释为什么有些类"看起来加载了"却一直没执行静态代码块。

## 三、双亲委派模型：源码级剖析

JDK 8 的 `ClassLoader.loadClass` 实现堪称教科书级，核心就十几行，却承载了整个委派模型：

```java
protected Class<?> loadClass(String name, boolean resolve) throws ClassNotFoundException {
    synchronized (getClassLoadingLock(name)) {
        // 1. 检查是否已加载
        Class<?> c = findLoadedClass(name);
        if (c == null) {
            try {
                // 2. 优先委派给父加载器
                if (parent != null) {
                    c = parent.loadClass(name, false);
                } else {
                    c = findBootstrapClassOrNull(name);
                }
            } catch (ClassNotFoundException e) {
                // 父加载器加载失败，继续往下
            }
            if (c == null) {
                // 3. 父加载器找不到，自己尝试加载
                c = findClass(name);
            }
        }
        if (resolve) {
            resolveClass(c);
        }
        return c;
    }
}
```

委派链是：**Bootstrap ClassLoader → Extension ClassLoader → Application ClassLoader → 自定义 ClassLoader**。Bootstrap 由 C++ 实现，负责加载 `<JAVA_HOME>/lib` 下的核心类（JDK 9 之后是 `java.base` 等模块）；Extension 加载 `lib/ext` 或 `java.ext.dirs`；Application 加载 classpath 上的类。

双亲委派的核心价值有三点：

1. **避免核心类被篡改**：你无法用一个自定义的 `java.lang.String` 替换 JDK 的 String，因为委派机制会优先让 Bootstrap 加载核心类。
2. **避免类重复加载**：每个类由唯一的"加载器 + 全限定名"确定身份，JVM 中判断两个类是否相等必须同时满足"同一个 ClassLoader"这一条件。
3. **保证核心 API 的一致性**：无论哪个加载器，最终都从 Bootstrap 拿到同一份 `java.lang.Object`。

## 四、破坏双亲委派：SPI、OSGi 与热部署的必然选择

双亲委派并非铁律，JDK 自身就在某些场景下"官方打破"了它，最典型的是 **SPI（Service Provider Interface）**。

以 JDBC 为例：`java.sql.DriverManager` 位于 `java.sql` 模块（Bootstrap 加载），而具体的 MySQL 驱动在应用 classpath（Application 加载）。按双亲委派，Bootstrap 无法向下委派给 Application，导致 DriverManager 根本找不到第三方驱动。JDK 的解法是引入**线程上下文类加载器（Thread Context ClassLoader, TCCL）**：

```java
// ServiceLoader.load 内部本质就是绕开双亲委派，用 TCCL 加载实现类
ServiceLoader<Driver> loader = ServiceLoader.load(Driver.class);
for (Driver d : loader) {
    System.out.println(d.getClass().getClassLoader());
}
```

在自定义类加载器场景下，正确设置 TCCL 是避免 `ClassNotFoundException` 的关键：

```java
Thread thread = Thread.currentThread();
ClassLoader original = thread.getContextClassLoader();
try {
    // 让 SPI 使用自定义类加载器去发现实现类
    thread.setContextClassLoader(customLoader);
    ServiceLoader.load(SomeSpi.class);
} finally {
    thread.setContextClassLoader(original); // 必须还原，防止线程池污染
}
```

**生产环境最大的坑之一就是 TCCL 泄漏**：在 `ThreadPoolExecutor` 的线程里改了 TCCL 却没还原，后续复用该线程的任务会拿到错误的加载器，导致随机性的 `ClassNotFoundException` 或加载错版本。排查这类问题时，重点检查任务提交前是否在 `finally` 中还原了 TCCL。

另一个典型是 **Tomcat 的 Web 应用隔离**。Tomcat 为每个 Web 应用创建独立的 `WebappClassLoader`，并打破双亲委派——优先加载本地 `WEB-INF/classes` 和 `WEB-INF/lib`，再委派给父加载器。这样不同应用可以依赖不同版本的第三方库而互不干扰，也支持应用级别的热部署（替换 war 后卸载旧 ClassLoader）。

```yaml
# 自定义类加载器"子优先"加载示意（Tomcat 简化模型）
加载顺序:
  - 1. 本地已缓存类
  - 2. Bootstrap 核心类 (java.* 等受保护包)
  - 3. WEB-INF/classes 本地类
  - 4. WEB-INF/lib/*.jar
  - 5. 父加载器 (Shared/Common)
```

理解这一点后，就能解释两个经典现象：一是应用里打包了一个和容器冲突的库版本时报 `NoSuchMethodError` 或 `ClassCastException`（两个 ClassLoader 加载了同名不同版本的类）；二是热部署后老 ClassLoader 无法被 GC，导致 **Metaspace 持续增长**——因为老加载器还被线程、ThreadLocal 或静态引用强引用着。

## 五、热部署实践：ClassLoader 替换与内存泄漏排查

热部署的本质不是"改类"，而是**用一个全新的 ClassLoader 替换旧的**，让新类在新加载器中重新加载。老加载器及它加载的所有类只有在没有任何引用后才能被回收。

一个最小可用的热加载器长这样：

```java
public class HotSwapClassLoader extends ClassLoader {
    public HotSwapClassLoader(ClassLoader parent) {
        super(parent);
    }

    @Override
    protected Class<?> findClass(String name) throws ClassNotFoundException {
        try {
            String path = name.replace('.', '/') + ".class";
            byte[] bytes = Files.readAllBytes(Paths.get("/tmp/hotswap", path));
            return defineClass(name, bytes, 0, bytes.length);
        } catch (IOException e) {
            throw new ClassNotFoundException(name, e);
        }
    }

    public static void main(String[] args) throws Exception {
        while (true) {
            HotSwapClassLoader loader = new HotSwapClassLoader(
                    HotSwapClassLoader.class.getClassLoader());
            Class<?> clazz = loader.loadClass("com.example.Task");
            Runnable task = (Runnable) clazz.getDeclaredConstructor().newInstance();
            task.run();
            loader = null; // 等待下一次替换
            System.gc();
            Thread.sleep(3000);
        }
    }
}
```

但这个写法在真实生产中几乎必然踩坑：`System.gc()` 不保证回收，旧加载器往往因为以下原因滞留内存，最终撑爆 Metaspace：

| 泄漏来源 | 典型表现 | 排查/修复手段 |
| --- | --- | --- |
| 静态字段引用旧类 | `ClassCastException` / Metaspace OOM | 避免被加载类持有外部静态引用 |
| ThreadLocal 未清理 | 旧 ClassLoader 无法被回收 | 用完 `ThreadLocal.remove()` |
| 线程未终止 | 类卸载后线程仍在运行 | 停止线程池再热部署 |
| 日志框架缓存类名 | 出现脏类名 / 泄漏 | 使用支持类卸载的日志实现 |

排查 Metaspace 泄漏的标准思路：先 `jstat -gcutil <pid>` 观察 `MU/MC` 是否只增不减，再 `jmap -dump` 或用 Arthas 的 `classloader` 命令查看存活加载器数量；对疑似泄漏的加载器执行 `classloader -d` 或借助 `-XX:+TraceClassUnloading -XX:+TraceClassLoading` 观察卸载行为。JDK 8 上调参通常关注 `-XX:MetaspaceSize` 与 `-XX:MaxMetaspaceSize`，但更关键的是从根源上断掉引用，而不是一味调大上限。

## 小结与建议

- **分清五个阶段**：准备阶段赋的是零值而非初值，初始化才执行 `<clinit>`；排查"静态变量值不对"先确认初始化是否真正触发。
- **警惕 `loadClass` 与 `forName` 差异**：凡是依赖 `<clinit>` 副作用的场景（驱动注册、SPI 注册、插件自举），务必用 `forName` 或显式初始化。
- **理解 TCCL 的用途与危险**：SPI 加载用 TCCL 打破双亲委派是官方设计，但在线程池中修改后必须在 `finally` 还原，否则埋下随机性故障。
- **隔离即 ClassLoader 隔离**：Tomcat/OSGi 的"子优先"加载是为了版本隔离与热部署，代价是 Metaspace 泄漏风险升高。
- **热部署先断引用再谈调参**：老 ClassLoader 无法卸载的根源是强引用未释放；Metaspace 告警先查加载器数量，再决定是否调大 `-XX:MaxMetaspaceSize`。
