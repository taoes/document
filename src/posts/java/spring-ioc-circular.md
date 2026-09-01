---
title: "Spring IoC 容器启动流程与循环依赖的三级缓存"
date: 2023-06-17
category:
  - Java
tag:
  - Spring
  - IoC
  - 循环依赖
icon: code
---

# Spring IoC 容器启动流程与循环依赖的三级缓存

在日常 CRUD 之外，真正能拉开工程师差距的，是对框架底层运行机制的掌握。循环依赖是 Spring 面试的高频考点，但绝大多数人只背下了"三级缓存"这四个字，却说不清"为什么是三级而不是两级"、也讲不明白"构造器循环依赖为什么无解"。本文从容器启动流程出发，沿着 Bean 生命周期的完整链路，把三级缓存的来龙去脉、设计权衡和生产实践一次讲透。

## 一、容器启动流程：一条 refresh() 主线

Spring 的 IoC 容器无论以何种方式创建——`ClassPathXmlApplicationContext`、`AnnotationConfigApplicationContext` 还是 Spring Boot 内嵌的 `AnnotationConfigServletWebServerApplicationContext`——最终都会收敛到 `AbstractApplicationContext#refresh()` 这一个方法上。它是整个容器的"总装线"，理解它也就理解了容器启动的全貌。

```java
// 简化的启动入口
AnnotationConfigApplicationContext ctx =
        new AnnotationConfigApplicationContext(AppConfig.class);
// 内部最终调用 refresh()，完成 BeanDefinition 加载与 Bean 实例化
```

`refresh()` 的 12 个步骤可以归纳为三大阶段：

1. **准备与刷新阶段**（`prepareRefresh`、`obtainFreshBeanFactory`）：创建并配置 `BeanFactory`，加载并解析 `BeanDefinition`。注意此时"加载"的只是元数据，Bean 对象本身还没有被创建。
2. **扩展点回调阶段**（`prepareBeanFactory`、`postProcessBeanFactory`、`invokeBeanFactoryPostProcessors`、`registerBeanPostProcessors`）：注册各种后置处理器。`BeanFactoryPostProcessor` 在 Bean 实例化之前修改 `BeanDefinition`（例如占位符 `${...}` 的解析、`@Configuration` 类的 CGLIB 增强）；`BeanPostProcessor` 则在每个 Bean 实例化后、初始化前后介入。
3. **实例化阶段**（`finishBeanFactoryInitialization`）：这是最重的一步，容器在这里完成所有非懒加载单例 Bean 的**预实例化**（pre-instantiate），循环依赖的三级缓存也主要发生在这个阶段。

```java
// AbstractApplicationContext 中 refresh() 的核心骨架（节选）
public void refresh() throws BeansException {
    prepareRefresh();                                    // 1. 准备：校验环境、初始化 PropertySource
    ConfigurableListableBeanFactory beanFactory = obtainFreshBeanFactory(); // 2. 获取 BeanFactory 并加载 BeanDefinition
    prepareBeanFactory(beanFactory);                     // 3. 配置 BeanFactory 的类加载器、SpEL、Aware 等
    postProcessBeanFactory(beanFactory);                 // 4. 子类扩展点（如 Web 容器注册 RequestScope）
    invokeBeanFactoryPostProcessors(beanFactory);        // 5. 执行 BeanFactoryPostProcessor
    registerBeanPostProcessors(beanFactory);             // 6. 注册 BeanPostProcessor
    initMessageSource();                                 // 7. 国际化
    initApplicationEventMulticaster();                   // 8. 事件广播器
    onRefresh();                                         // 9. 子类扩展（如启动 WebServer）
    registerListeners();                                 // 10. 注册监听器
    finishBeanFactoryInitialization(beanFactory);        // 11. 预实例化所有非懒加载单例 ← 循环依赖重灾区
    finishRefresh();                                     // 12. 发布 ContextRefreshedEvent
}
```

理解第 11 步 `finishBeanFactoryInitialization` 与前面第 5、6 步的先后顺序至关重要：`BeanFactoryPostProcessor` 必须先于任何 Bean 实例化执行，因为它修改的是 Bean 的"图纸"；而 `BeanPostProcessor` 也必须在普通 Bean 之前完成实例化，否则普通 Bean 初始化时就找不到可用的处理器。

## 二、Bean 生命周期：从实例化到销毁

循环依赖之所以棘手，根本原因在于 Bean 的创建不是一步到位的原子操作，而是一条可以被"拆开"的流水线。一个单例 Bean 的完整生命周期大致如下：

```java
// AbstractAutowireCapableBeanFactory#doCreateBean 精简逻辑
protected Object doCreateBean(String beanName, RootBeanDefinition mbd, Object[] args) {
    BeanWrapper instanceWrapper = createBeanInstance(beanName, mbd, args); // ① 实例化：调用构造器/工厂方法
    Object bean = instanceWrapper.getWrappedInstance();

    // ② 提前暴露：单例 && 允许循环依赖 && 正在创建中 → 放入三级缓存
    boolean earlySingletonExposure =
            (mbd.isSingleton() && this.allowCircularReferences &&
             isSingletonCurrentlyInCreation(beanName));
    if (earlySingletonExposure) {
        addSingletonFactory(beanName, () -> getEarlyBeanReference(beanName, mbd, bean));
    }

    populateBean(beanName, mbd, instanceWrapper);        // ③ 属性填充：@Autowired 注入在这里发生
    exposedObject = initializeBean(beanName, exposedObject, mbd); // ④ 初始化：Aware → before → init → after
    registerDisposableBeanIfNecessary(beanName, bean, mbd);      // ⑤ 注册销毁回调
    return exposedObject;
}
```

这里有一个极其关键、但常被忽视的时序点：**三级缓存的"提前暴露"发生在第 ② 步，即实例化之后、属性填充之前**。此时 Bean 只是 `new` 出来的一个"半成品"，它的依赖字段还是 `null`，但它本身已经可以被其他 Bean 引用了。这一条时序，直接决定了后文"构造器循环依赖无解"的结论。

生命周期各阶段的扩展点可以梳理如下：

| 阶段 | 关键方法/接口 | 是否可介入依赖注入 |
| --- | --- | --- |
| 实例化 | `createBeanInstance`（构造器/工厂方法） | 否，此时对象尚未生成 |
| 提前暴露 | `addSingletonFactory` | 是，暴露半成品引用 |
| 属性填充 | `populateBean`、`InstantiationAwareBeanPostProcessor#postProcessProperties` | 是，`@Autowired` 生效 |
| 初始化前 | `BeanPostProcessor#postProcessBeforeInitialization`、`@PostConstruct`、`InitializingBean` | 是，可替换 Bean |
| 初始化后 | `BeanPostProcessor#postProcessAfterInitialization`（AOP 代理在此生成） | 是，可返回代理对象 |
| 销毁 | `@PreDestroy`、`DisposableBean#destroy`、自定义 `destroy-method` | 否 |

注意 AOP 代理的生成时机：默认在**初始化后**（`postProcessAfterInitialization`）由 `AbstractAutoProxyCreator` 完成。这意味着，如果循环依赖中需要注入的是一个被 AOP 增强的 Bean，我们必须在"属性填充"阶段就拿到它的代理，而不是等它初始化结束——这正是三级缓存存在的核心动机。

## 三、循环依赖的产生与三级缓存

考虑一个最经典的场景：`A` 依赖 `B`，`B` 又依赖 `A`。

```java
@Component
public class A {
    private final B b;
    public A(B b) { this.b = b; }         // 字段也可以，这里用 setter/构造便于演示
}

@Component
public class B {
    private final A a;
    public B(A a) { this.a = a; }
}
```

当容器创建 `A` 时：实例化 `A` → 提前暴露 `A` 的半成品 → 填充 `A` 的 `b` 字段 → 触发 `B` 的创建 → 实例化 `B` → 填充 `B` 的 `a` 字段 → **发现 `A` 正在创建中** → 从缓存中取出 `A` 的半成品注入 → `B` 创建完成 → 回到 `A`，把完整的 `B` 注入 → `A` 创建完成。这条链之所以能走通，靠的就是 `DefaultSingletonBeanRegistry` 中的三个缓存：

| 缓存 | 字段名 | 存储内容 | 说明 |
| --- | --- | --- | --- |
| 一级缓存 | `singletonObjects` | 完整可用的成品单例 | 所有"正常" Bean 的最终归宿 |
| 二级缓存 | `earlySingletonObjects` | 提前暴露的早期引用 | 已被"物化"的半成品/代理，保证同一对象只暴露一次 |
| 三级缓存 | `singletonFactories` | `ObjectFactory<?>` | 懒执行的工厂，调用 `getObject()` 才真正生成早期引用 |

核心查找逻辑在 `getSingleton(String beanName, boolean allowEarlyReference)`：

```java
// DefaultSingletonBeanRegistry 精简版
protected Object getSingleton(String beanName, boolean allowEarlyReference) {
    Object singletonObject = this.singletonObjects.get(beanName);      // 一级
    if (singletonObject == null && isSingletonCurrentlyInCreation(beanName)) {
        singletonObject = this.earlySingletonObjects.get(beanName);    // 二级
        if (singletonObject == null && allowEarlyReference) {
            synchronized (this.singletonObjects) {
                singletonObject = this.singletonObjects.get(beanName);
                if (singletonObject == null) {
                    singletonObject = this.earlySingletonObjects.get(beanName);
                    if (singletonObject == null) {
                        ObjectFactory<?> sf = this.singletonFactories.get(beanName); // 三级
                        if (sf != null) {
                            singletonObject = sf.getObject();            // 真正执行工厂
                            this.earlySingletonObjects.put(beanName, singletonObject); // 升到二级
                            this.singletonFactories.remove(beanName);    // 移除三级
                        }
                    }
                }
            }
        }
    }
    return singletonObject;
}
```

这里有两个细节值得留意：一是"正在创建中"这个前置条件——三级缓存只在 `isSingletonCurrentlyInCreation(beanName)` 为 `true` 时才生效，即它专门服务于循环依赖，正常创建顺序下不会走这条旁路；二是三级到二级的"物化"过程是**一次性**的：一旦工厂执行过，就把结果固化到二级缓存并删除三级缓存，从而保证后续引用同一 Bean 时拿到的是**同一个**早期引用。

## 四、为什么是三级而不是两级

这是最容易被问倒的问题。很多人以为"三级缓存 = 二级缓存 + 一个工厂"，却说不清那个工厂到底解决了什么。答案是：**三级缓存本质上是为了解决 AOP 代理与循环依赖的叠加问题，同时兼顾性能与正确性**。

如果只有两级（一级成品 + 二级半成品），那么在第 ② 步"提前暴露"时，我们只能把**尚未代理的原始对象**放进二级缓存。当 `A` 被 `B` 引用时，`B` 拿到的是原始 `A`；而 `A` 最终对外提供的却是经过 `postProcessAfterInitialization` 生成的**代理对象**。于是容器里会出现两个"A"：注入给 `B` 的是裸对象，注入给其他 Bean 的是代理对象——这直接破坏了 AOP 的语义（比如事务、切面失效），是典型的正确性缺陷。

三级缓存引入 `ObjectFactory` 后，把"是否代理、何时代理"的决定权从"提前暴露那一刻"推迟到了"真正被引用那一刻"。工厂内部调用的正是 `getEarlyBeanReference`：

```java
protected Object getEarlyBeanReference(String beanName, RootBeanDefinition mbd, Object bean) {
    Object exposedObject = bean;
    if (!mbd.isSynthetic() && hasInstantiationAwareBeanPostProcessors()) {
        for (SmartInstantiationAwareBeanPostProcessor bp :
                getBeanPostProcessorCache().smartInstantiationAware) {
            exposedObject = bp.getEarlyBeanReference(exposedObject, beanName);
        }
    }
    return exposedObject;
}
```

其中 `AbstractAutoProxyCreator#getEarlyBeanReference` 会判断该 Bean 是否需要被代理，需要则提前创建代理对象。于是当 `B` 引用 `A` 时，工厂执行并返回 `A` 的代理，`B` 注入的就是代理；而 `A` 初始化结束后，`postProcessAfterInitialization` 会检测到"该 Bean 已提前暴露过代理"，直接复用同一个代理，不再二次创建，最终保证容器内外拿到的是**同一个代理实例**。

那二级缓存 `earlySingletonObjects` 又为何不能省？它的职责是**缓存物化结果，避免工厂被重复执行**。假设 `A` 同时被 `B` 和 `C` 依赖，若没有二级缓存，`B`、`C` 各自触发一次 `ObjectFactory.getObject()`，就可能生成两个不同的代理（尤其是某些后置处理器在工厂中执行非幂等逻辑时）。二级缓存把第一次的结果固化下来，保证同一 Bean 在整个创建周期内只有一个早期引用，同时避免了重复执行工厂带来的性能开销。

一句话总结：**三级缓存解决"何时代理"的时机问题，二级缓存解决"只代理一次"的一致性问题，一级缓存解决"成品可用"的归宿问题。**

## 五、构造器循环依赖为何无解

理解了上面的时序，构造器循环依赖无解的原因就呼之欲出了。关键在于：**构造器注入发生在"实例化"阶段，而三级缓存的暴露发生在"实例化完成之后"**。

```java
@Component
public class A {
    private final B b;
    public A(B b) { this.b = b; }   // 构造器注入：创建 A 的第一步就需要 B
}

@Component
public class B {
    private final A a;
    public B(A a) { this.a = a; }   // 创建 B 的第一步又需要 A
}
```

创建 `A` 的流程在 `createBeanInstance` 阶段就卡住了：调用 `A` 的构造器需要先有 `B`；于是转去创建 `B`；而 `B` 的构造器又需要 `A`。此时 `A` 连实例化都没完成，**根本没有机会执行到 `addSingletonFactory`**，三级缓存里自然没有 `A` 的任何记录。容器只能抛出 `BeanCurrentlyInCreationException`：

```text
Error creating bean with name 'a': Requested bean is currently in creation:
Is there an unresolvable circular reference?
```

这解释了为什么官方从未"支持"构造器循环依赖——这不是实现偷懒，而是时序上的逻辑必然：**一个还没有诞生（实例化）的对象，无法被提前暴露**。setter 注入和字段注入之所以可行，正是因为它们把依赖的填充推迟到了"实例化之后"，恰好落在三级缓存的覆盖范围内。

应对构造器循环依赖，业界有三条可操作的出路：

```java
// 方案一：@Lazy 打破实例化强依赖——注入的是代理占位，真正用到时才触发解析
@Component
public class A {
    private final B b;
    public A(@Lazy B b) { this.b = b; }
}
```

```java
// 方案二：改为 setter/字段注入（推荐能改则改）
@Component
public class A {
    @Autowired
    private B b;
}
```

```java
// 方案三：重构职责，抽出公共依赖 C，让 A、B 都只依赖 C
@Component
public class A {
    private final C c;
    public A(C c) { this.c = c; }
}
@Component
public class B {
    private final C c;
    public B(C c) { this.c = c; }
}
```

从工程角度，**方案三最值得优先考虑**：构造器循环依赖往往不是注入方式的问题，而是领域职责划分不清的信号——两个对象互相强依赖，通常意味着它们本该被合并或抽取。

## 六、生产环境的坑与调优建议

理论之外，这些机制在真实线上会以各种形式"反噬"，下面是我实际踩过或见过的高频问题。

**坑一：Spring Boot 2.6 升级后启动直接报错。** Spring Boot 2.6 起，默认 `spring.main.allow-circular-references=false`，即默认禁止循环依赖。很多老项目从 2.4/2.5 升级后，历史遗留的字段注入循环依赖会突然爆炸。临时兜底可以这样放开，但**这是掩盖问题，不是解决问题**：

```yaml
# application.yml —— 仅作迁移期的临时过渡
spring:
  main:
    allow-circular-references: true
```

正确姿势是：迁移窗口内先放开开关保证上线，随后逐个拆解循环依赖，最终把开关重新置为 `false`，并在 CI 中加一条启动测试防止回归。

**坑二：AOP 代理与 `this` 调用导致"注入的代理"和"使用的代理"不一致。** 即便三级缓存正确地把代理注入了循环依赖的另一方，如果业务代码内部用 `this.targetMethod()` 调用被增强的方法，仍然会绕过代理。这类问题排查要点是：打印对象身份看是否是 CGLIB/动态代理类（类名带 `$$` 或 `CGLIB`），并检查调用是否走了 `this` 而非注入的代理。

**坑三：`@Async`、`@Transactional` 等"代理型"增强叠加循环依赖时报错。** 某些后置处理器（如 `AsyncAnnotationBeanPostProcessor`）在早期引用生成阶段尚未注册完毕，若循环依赖的 Bean 恰好需要 `@Async` 代理，可能出现"代理在早期引用阶段不可用"的异常。排查思路是：逐步裁剪依赖链，定位是哪条边、哪个增强点触发了问题，优先用 `@Lazy` 或抽公共类解耦。

**排查工具与思路：**

```bash
# 通过启动日志快速定位循环依赖环
java -jar app.jar 2>&1 | grep -A 20 "BeanCurrentlyInCreationException"
```

更彻底的做法是借助 Actuator 暴露的 Bean 元数据，或用 `DefaultListableBeanFactory#getDependenciesForBean` 编写诊断代码，打印依赖图后人工识别环。对于复杂的生产依赖图，建议在架构评审阶段就用静态工具（如 ArchUnit）约束"禁止 `@Autowired` 字段注入"，从源头压缩循环依赖的产生概率。

**调优参数小结：**

| 参数 | 建议值 | 说明 |
| --- | --- | --- |
| `spring.main.allow-circular-references` | `false`（默认） | 生产环境保持关闭，循环依赖应在设计层消除 |
| `spring.main.allow-bean-definition-overriding` | `false` | 防止同名 Bean 覆盖带来的隐性风险 |
| `spring.main.lazy-initialization` | 视场景 | 可加快启动，但会把错误推迟到运行时，慎用 |
| `@Lazy` | 精准使用 | 仅在构造器循环依赖无法重构时局部打破 |

## 小结与建议

- **先有生命周期，后有三级缓存**：三级缓存的"提前暴露"发生在实例化之后、属性填充之前，这是理解一切循环依赖问题的原点。
- **三级而非两级，是为了 AOP 代理的时机**：`ObjectFactory` 让"是否代理"的决定推迟到真正被引用的那一刻，避免提前暴露裸对象导致代理语义被破坏。
- **二级缓存保证"只物化一次"**：缓存早期引用，确保同一 Bean 被多方引用时拿到同一个代理，兼顾正确性与性能。
- **构造器循环依赖是逻辑无解**：实例化尚未完成，对象无法被暴露，这不是实现缺陷，而是时序必然；用 `@Lazy` 或重构来解。
- **生产上把循环依赖当"坏味道"**：Spring Boot 2.6 默认禁用它，应默认保持关闭，用架构约束和重构从源头消除，而不是靠开关兜底。
- **排查靠环定位，治理靠设计**：启动日志、Actuator、依赖图诊断定位环；ArchUnit、构造器注入、职责抽取预防环。
