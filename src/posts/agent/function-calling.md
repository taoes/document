---
title: "Function Calling 与 Tool 设计规范"
date: 2025-10-11T17:20:00+08:00
category:
  - Agent
tag:
  - FunctionCalling
  - Tool
  - LLM
icon: robot
---

# Function Calling 与 Tool 设计规范

Function Calling 常被误解为"模型替你调了个函数"。实际上，LLM 从头到尾没有执行任何代码：它只负责在给定工具描述（schema）与上下文的前提下，输出一段结构化的"意图"——通常是函数名与一组参数 JSON。真正调用函数、校验参数、捕获异常、把结果回灌给模型，全部由你这一侧的编排层完成。换句话说，Function Calling 的本质是一场"结构化输出 + 编排协议"的工程，而不是模型能力本身。

这篇文章不聊"什么是 Function Calling"的科普，而是从生产落地的角度，把工具描述如何写、参数如何校验、错误如何回传、并行与嵌套调用如何设计这几件最容易翻车的事讲透。

## 一、工具描述：OpenAPI/JSON Schema 是你与模型的"接口合同"

模型对工具的所有认知都来自你提供的 schema。schema 写得好不好，直接决定参数幻觉率。下面是一段被广泛滥用的写法：

```yaml
get_order:
  description: Get order info
  parameters:
    type: object
    properties:
      id:
        type: string
```

这段描述有三个致命问题：`description` 过于空泛，模型无法判断何时该用这个工具；参数名 `id` 缺乏语义（是订单号还是用户 ID？）；没有任何约束与示例。生产环境里，工具描述应当遵循"名称具体、语义完整、约束显式、示例充足"的原则：

```yaml
get_order_by_order_no:
  description: >
    根据订单号查询订单详情，返回订单状态、金额、商品列表与物流信息。
    仅当用户明确提供了形如 20 位数字的订单号时才调用；不要猜测或补全订单号。
  parameters:
    type: object
    required: [order_no]
    additionalProperties: false
    properties:
      order_no:
        type: string
        description: 20 位纯数字订单号，来自用户消息或系统上下文，禁止拼接生成。
        pattern: '^\d{20}$'
        examples: ["20251011000000012345"]
```

这里的关键点：

1. **`additionalProperties: false`**：禁止模型自由发挥多余字段，否则它可能输出 schema 之外的键，导致你的反序列化直接失败。
2. **`required` 显式声明**：缺省时模型可能省略关键参数，埋下运行时 NPE。
3. **`pattern` + `examples` 双保险**：正则约束杜绝格式错误，示例则引导模型按正确的风格取值。
4. **描述里写"何时不该调用"**：negative 指令与 positive 指令同样重要，能显著降低误调用率。

一个常见误区是给模型塞入几十个平铺的工具，寄希望于它"自己挑"。工具数量一多，选择准确率会显著下降。生产上的做法是**按领域收敛 + 动态裁剪**：先把工具按意图分组成子命名空间（如 `order.*`、`payment.*`、`logistics.*`），再在每一轮根据上下文只注入相关子集。这样既控制了 prompt 长度，也减少了同义词工具之间的干扰。

## 二、参数校验：schema 只是第一道门，别把希望全押在模型上

模型输出的参数 JSON 即使通过了 JSON 解析，仍然可能是"合法的垃圾"：类型对、格式对，但语义上荒谬（比如退款金额为负数、日期在未来一百年）。因此，**校验必须是双层结构**：先用 JSON Schema 做结构校验，再用业务规则做语义校验。

以 Python 为例，结构校验用 `jsonschema` 库，业务校验放在独立函数里：

```python
import jsonschema
from datetime import date

SCHEMA = {
    "type": "object",
    "required": ["amount", "reason"],
    "additionalProperties": False,
    "properties": {
        "amount": {"type": "number", "exclusiveMinimum": 0},
        "reason": {"type": "string", "minLength": 1, "maxLength": 200},
    },
}

def validate_refund_args(args: dict):
    # 第一层：结构校验
    try:
        jsonschema.validate(instance=args, schema=SCHEMA)
    except jsonschema.ValidationError as e:
        # 把 schema 错误翻译成模型能理解的自然语言，回传给模型修正
        raise ToolArgError(f"参数不合法：{e.message}")

    # 第二层：业务语义校验
    if args["amount"] > 100000:
        raise ToolArgError("单笔退款金额不能超过 10 万元，请与用户确认拆分退款")

def refund(amount: float, reason: str):
    validate_refund_args({"amount": amount, "reason": reason})
    # ... 实际退款逻辑
```

注意这段代码刻意把校验失败包装成自定义异常 `ToolArgError`，而不是直接抛出原始异常。原始异常栈里往往藏着数据库连接串、内部类名等敏感信息，一旦原样回传给模型，轻则污染上下文，重则泄露内部架构。

## 三、错误回传：把"失败"设计成一次可恢复的对话轮次

Function Calling 最常见的翻车现场，是工具执行抛异常后，编排层直接中断，返回给用户一句"系统错误"。这样既浪费了前面几轮的上下文，也让模型失去了自我纠错的机会。正确姿势是：**把错误结果作为 tool message 回传，让模型基于错误信息重新决策**。

以 OpenAI 的消息协议为例：

```python
messages = [
    {"role": "user", "content": "帮我退掉订单 20251011000000012345，原因是不想要了"},
    {"role": "assistant", "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "refund", "arguments": '{"order_no": "20251011000000012345", "reason": "不想要了"}'}}
    ]},
    {"role": "tool", "tool_call_id": "call_1",
     "content": '{"error": "ORDER_NOT_FOUND", "message": "订单不存在，可能订单号有误或订单已删除，请向用户确认订单号"}'},
]
```

回传的错误信息要满足三点：**结构化、可读、可操作**。上例中 `error` 是机器可判定的错误码，`message` 是给模型看的自然语言建议。模型收到后通常会主动向用户澄清，而不是继续盲目重试。

这里有两个容易被忽略的坑：

- **错误信息不要塞原始异常堆栈**。堆栈对模型毫无价值，还会让模型学着"复读"内部细节。
- **区分"可重试"与"不可重试"错误**。瞬时错误（超时、限流）可以提示模型稍后重试；业务错误（余额不足、订单不存在）则应引导模型改变策略。用错误码或 `retryable` 布尔字段显式区分，而不是让模型自己猜。

## 四、并行调用：编排层负责编排，模型只负责表达意图

模型可以在一次响应里返回多个 `tool_calls`，表示"这几个操作相互独立，可以并行执行"。但"可以并行"不等于"应当并行"，真正的调度权在编排层手里。

并行执行的核心是**依赖分析**。当多个工具调用之间没有数据依赖时，用线程池/协程并发执行，能显著降低端到端延迟；一旦存在依赖（例如第二个工具需要第一个工具的返回结果作为入参），就必须串行。下面是一段简化的 Java 并行调度示意：

```java
import java.util.List;
import java.util.concurrent.*;

public class ToolOrchestrator {
    private final ExecutorService pool = Executors.newFixedThreadPool(8);

    public List<ToolResult> runParallel(List<ToolCall> calls) {
        List<CompletableFuture<ToolResult>> futures = calls.stream()
            .map(c -> CompletableFuture.supplyAsync(() -> invoke(c), pool))
            .toList();
        return futures.stream().map(CompletableFuture::join).toList();
    }

    private ToolResult invoke(ToolCall call) {
        try {
            // 每个工具调用都应有独立超时，避免单点拖垮整轮
            return CompletableFuture.supplyAsync(() -> dispatch(call))
                .get(5, TimeUnit.SECONDS);
        } catch (TimeoutException e) {
            return ToolResult.timeout(call.id());
        } catch (Exception e) {
            return ToolResult.failure(call.id(), e);
        }
    }
}
```

这段代码的两个细节值得展开：

1. **每个调用独立超时**。并行执行时，最慢的那个调用决定了整轮的延迟。给每个工具设置独立超时（如 5 秒），超时的调用返回 `timeout` 结果而非整体失败，其余成功结果仍可继续使用。
2. **失败隔离**。一个工具的失败不应拖垮整轮并行调用，把它包装成失败结果回传即可，模型可以只针对失败项重试。

并行度的控制也是调优重点。盲目把并发拉满会打爆下游服务，触发限流甚至雪崩。建议用**信号量（semaphore）限流** + **熔断器（circuit breaker）**，把并发控制在 8~16 这个区间起步，再根据下游 RT 与错误率逐步调整。

## 五、嵌套调用与多步 Agent：控制循环深度，防止"失控"与"死循环"

当模型在工具返回结果之后需要再次调用工具（甚至调用另一个工具）时，就进入了多步循环，也就是 Agent 模式。嵌套调用的核心风险有两个：**深度失控**与**死循环**。

一个典型的生产循环骨架如下：

```python
MAX_ITERATIONS = 8  # 硬性上限，防止失控

for i in range(MAX_ITERATIONS):
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
    )
    message = response.choices[0].message

    if not message.tool_calls:
        # 模型不再调用工具，说明已完成，输出最终回复
        return message.content

    messages.append(message)
    for call in message.tool_calls:
        result = execute_tool(call)
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(result, ensure_ascii=False),
        })

# 达到上限仍未收敛，做降级处理
return fallback_response(messages)
```

这段骨架里最容易被新手忽略的是**循环上限的兜底逻辑**。没有上限的循环，遇到模型"症状反复"（比如模型反复调用同一个工具却得到同样的失败）时，会无限空转、烧掉大量 token。设置 `MAX_ITERATIONS` 并用 `fallback_response` 兜底，是生产必须的护栏。

死循环的典型信号是**连续 N 轮工具调用参数完全相同**，或**返回结果完全一致**。排查时可以埋一个轻量计数器：若检测到连续 3 轮出现相同 `(tool_name, arguments)` 组合，直接中断循环并降级为向用户澄清。例如：

```python
seen = {}
# 在循环体内记录
key = (call.function.name, call.function.arguments)
seen[key] = seen.get(key, 0) + 1
if seen[key] >= 3:
    raise LoopDetectedError(f"检测到工具 {call.function.name} 重复调用，疑似死循环")
```

嵌套调用的另一层含义是**工具之间的依赖链**。一个工具的结果需要作为另一个工具的入参时，不要试图让模型"记住并传递"中间结果——模型的中间状态不可靠，容易在长链路中丢失或篡改关键值。正确做法是把中间结果写入**显式的会话状态或工作内存**，由编排层管理，模型只通过工具结果看到必要的信息切片。

## 六、调优与排查：把 Function Calling 当成一个可观测系统

把模型当作黑盒去"祈祷它调对"，是资深工程师最忌讳的。Function Calling 的调优应当建立在可观测性之上。建议至少采集以下三类信号：

| 指标 | 含义 | 常见阈值参考 |
| --- | --- | --- |
| 工具选择准确率 | 模型选对工具的比例 | 目标 > 95%，低于 90% 需重写 description |
| 参数校验通过率 | 一次输出即通过 schema 校验的比例 | 目标 > 90%，否则补 examples 与约束 |
| 平均工具调用轮数 | 完成一个意图所需往返次数 | 通常 1~3 轮，异常升高说明存在纠错循环 |

排查路径上，几个高频问题与对应手段：

- **模型选错工具**：优先检查两个工具的 `description` 是否语义重叠，用"何时用 A、何时不用 B"的对比性描述消除歧义；必要时合并或拆分工具。
- **参数总被填错/漏填**：检查 `required` 是否完整、`examples` 是否贴近真实业务、字段命名是否自解释。
- **工具反复重试失败**：检查错误回传信息是否给出了"下一步该怎么做"的明确指引，而非只回传错误码。
- **延迟高**：优先排查是否所有工具都被串行执行，改造成并行；其次给每个调用加超时，避免单点拖垮整轮。

## 小结与建议

- 工具描述是模型的唯一认知来源，用 OpenAPI/JSON Schema 写清语义、约束与示例，`additionalProperties: false` 与 `required` 是底线。
- 参数校验做双层结构：JSON Schema 管结构，业务规则管语义；校验失败抛自定义异常，杜绝泄露原始堆栈。
- 错误要作为 tool message 回传，让模型具备自我纠错能力；区分可重试与不可重试错误，用错误码显式表达。
- 并行调用是编排层的职责，独立超时、失败隔离、信号量限流是三个不可省的保护。
- 嵌套/多步调用必须设硬性迭代上限，并对重复调用做死循环检测与降级兜底。
- 把 Function Calling 当可观测系统治理：记录工具选择准确率、校验通过率、调用轮数，用数据驱动 schema 的持续优化。
