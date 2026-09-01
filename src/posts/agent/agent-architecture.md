---
title: "大模型 Agent 架构总览：ReAct 与工具调用"
date: 2025-06-21T15:30:00+08:00
category:
  - Agent
tag:
  - Agent
  - ReAct
  - LLM
icon: robot
---

# 大模型 Agent 架构总览：ReAct 与工具调用

## 为什么 LLM 需要一个「外壳」

单次对话式的 LLM 本质上是一个「无状态的文本映射函数」：输入一段 prompt，输出一段 token 序列。它没有外部记忆、不能主动获取实时信息、更不会在多次调用之间自我修正。把 LLM 直接暴露给生产环境，会立刻撞上三堵墙：

1. **知识截断**：模型权重内的知识停留在训练截止时间，无法回答「今天股价多少」「当前告警是什么」这类需要实时数据的问题。
2. **幻觉与不可靠计算**：纯靠内隐知识做乘法、查单号、调接口参数，正确率随任务复杂度指数下降。
3. **不可控的长链路**：复杂任务往往需要拆解、重试、回溯，单次生成无法承载。

Agent（智能体）的本质，就是给 LLM 套上一个**控制循环外壳**：由模型负责「推理与决策」，由一段确定性的程序负责「执行与状态管理」。这篇文章从 ReAct 循环讲起，穿过工具调用闭环，落到一个可以真正跑起来的骨架，并讨论生产环境中那些不会写在论文里的坑。

## 核心范式一：ReAct 循环

ReAct（Reasoning + Acting）是 2023 年由 Yao et al. 提出的范式，核心思想一句话：**让模型在生成「最终答案」之前，交替产出「思考（Thought）」和「动作（Action）」，并把动作结果作为「观察（Observation）」喂回模型**。它把「想」和「做」显式地交织在同一个上下文里，而不是先想完再做。

一个最小化的 ReAct 循环可以抽象为：

```text
Thought -> Action -> Observation -> Thought -> Action -> Observation -> ... -> Final Answer
```

真正关键的工程细节是 **Thought 与 Action 如何被解析**。生产上最常见的做法是要求模型输出结构化的动作标记，例如：

```text
Thought: 用户问的是订单 O20250621 的状态，我需要先调用订单查询工具。
Action: query_order(order_id="O20250621")
```

于是宿主机（Agent Runtime）就能用正则或专门的 parser 把 `Action` 后面的内容截下来，交给工具调度器执行，再把结果拼成 `Observation` 追加进上下文。下面是一段 Python 伪代码，展示循环骨架：

```python
def react_loop(model, tools, prompt, max_steps=6):
    context = [{"role": "system", "content": SYSTEM_PROMPT},
               {"role": "user", "content": prompt}]
    for step in range(max_steps):
        raw = model.generate(context)          # 一次 LLM 调用
        if "Final Answer:" in raw:
            return parse_final_answer(raw)

        action = parse_action(raw)             # 解析 Action 与参数
        if action.name not in tools:
            observation = f"Error: unknown tool '{action.name}'"
        else:
            try:
                observation = tools[action.name](**action.args)
            except Exception as e:
                observation = f"ToolError: {e}"

        context.append({"role": "assistant", "content": raw})
        context.append({"role": "user", "content": f"Observation: {observation}"})
    return "Reached max steps without a final answer."
```

这段代码暴露了 ReAct 的第一组工程风险，也是后面所有坑的源头：

- **循环不收敛**：模型可能反复调用同一个工具、在错误参数里打转。必须设置 `max_steps` 硬上限，并在接近上限时注入「请现在给出最终答案」的强制指令。
- **解析失败**：模型偶尔不按格式输出。`parse_action` 必须对空输出、多余文字、非法 JSON 做防御，否则整个循环会静默崩溃。
- **上下文膨胀**：每一步都把「思考 + 动作 + 观察」全量追加，token 成本随步数线性甚至超线性增长，长任务会直接撑爆上下文窗口。

## 核心范式二：规划 - 执行 - 反思

ReAct 是「边想边做」，对短平快的任务很好；但面对「把 50 个仓库的依赖升级并出报告」这种多步骤任务，纯 ReAct 容易在中间迷失。此时需要把控制粒度抬高一层，引入 **Plan-Execute-Reflect** 三层结构：

- **Planner**：一次性（或分阶段）产出步骤清单。
- **Executor**：逐条执行，可以是简单的循环，也可以嵌套一个 ReAct。
- **Reflector**：对执行结果做批判性评估，决定「继续、重试、还是改计划」。

规划层通常输出结构化 JSON，而不是自由文本：

```yaml
plan:
  goal: "分析订单积压原因并给出改进建议"
  steps:
    - id: 1
      tool: fetch_orders
      args: { window: "24h", status: "pending" }
      depends_on: []
    - id: 2
      tool: fetch_warehouse_capacity
      args: {}
      depends_on: [1]
    - id: 3
      tool: summarize
      args: { input: "$steps.1.result + $steps.2.result" }
      depends_on: [1, 2]
```

注意 `depends_on` 与 `$steps.N.result` 这两个字段：它们是**执行图**的雏形。生产系统中，规划的输出一旦变成 DAG（有向无环图），就可以并行执行互不依赖的步骤，这是纯串行 ReAct 做不到的性能优化。

反思层最常见的实现是「自我批评 + 修正」：

```python
def reflect_and_replan(model, plan, results, failure):
    review_prompt = f"""
    原计划：{plan}
    已完成步骤及结果：{results}
    当前失败：{failure}
    请判断失败原因（计划错误 / 执行错误 / 信息不足），并输出修正后的计划 JSON。
    """
    return model.generate_json(review_prompt)
```

一个容易被忽视的工程点是：**反思不能让同一个模型无条件「相信自己」**。当 Reflector 与 Planner 是同一个模型时，它倾向于给自己的错误打圆场。生产上常引入「外部验证器」——例如用单元测试、Schema 校验、数值范围检查这类确定性手段作为反思的客观依据，而不是只依赖模型的自我判断。

## 工具调用闭环：从 Schema 到执行

工具调用（Function Calling / Tool Use）是把 Agent 从「聊天」变成「干活」的关键闭环。这个闭环由四段组成，任何一段出问题，整个 Agent 都会表现异常：

1. **工具声明（Schema）**：把函数签名、参数类型、描述以 JSON Schema 形式传给模型。
2. **意图识别与参数抽取**：模型决定「调不调、调哪个、参数是什么」。
3. **执行（Invocation）**：宿主机真正调用函数、查库、发 HTTP 请求。
4. **结果回填（Result Feedback）**：把工具返回值序列化后塞回上下文，让模型基于真实数据继续生成。

工具声明是**契约**，质量直接决定调用成功率。下面是一个反例与正例的对比：

```python
# 反例：描述含糊，模型不知道该传什么
{
    "name": "get_data",
    "parameters": {"x": {"type": "string"}}
}

# 正例：语义清晰，枚举了合法值，给了解释
{
    "name": "query_order_status",
    "description": "查询订单当前状态，status 只接受 pending/paid/shipped/done 之一",
    "parameters": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "订单号，形如 O+8 位数字"},
            "status": {"type": "string", "enum": ["pending", "paid", "shipped", "done"]}
        },
        "required": ["order_id"]
    }
}
```

真实生产里，工具执行层要做三层防护，这是「Demo 能跑、上线就挂」的分水岭：

- **参数校验**：不能盲信模型抽出的参数。即使 Schema 声明了类型，也要在函数入口做二次校验（类型、范围、长度），把非法调用转成结构化错误回填给模型，让它有机会自我纠正。
- **超时与降级**：外部 API 可能卡死。每个工具调用都要有 `timeout`，超时后返回 `{"error": "timeout"}` 而不是抛异常把整个循环带崩。
- **幂等与副作用隔离**：把工具分为「只读（safe）」和「有副作用（unsafe）」两类。写操作（下单、发消息、删数据）必须单独确认，绝不让模型在自动循环里无约束地执行破坏性动作。

下面是一个 Java 侧的工具注册示例，展示强类型语言里如何做「声明 + 校验 + 副作用标记」一体化：

```java
public record ToolSpec(
    String name,
    String description,
    JsonSchema parameters,
    boolean mutating,          // 是否有副作用
    Function<JsonNode, JsonNode> executor
) {}

public class OrderToolRegistry {
    public ToolSpec queryOrder() {
        return new ToolSpec(
            "query_order",
            "查询订单状态，只读操作",
            JsonSchema.of("""
                {"type":"object",
                 "properties":{"order_id":{"type":"string"}},
                 "required":["order_id"]}
                """),
            false,               // 只读，允许自动循环调用
            args -> {
                String id = args.get("order_id").asText();
                if (!id.matches("O\\d{8}")) {
                    return error("order_id 格式非法");
                }
                return orderDao.findById(id).toJson();
            }
        );
    }
}
```

## 一个可运行的 Agent 骨架

把前面三节串起来，就是一个最小但完整的 Agent。它用 ReAct 作为内层循环，用工具注册表管理执行，用 `max_steps` 与「强制收尾」保证收敛。下面的代码去掉日志与异常兜底后可以直接跑：

```python
import json

class Agent:
    def __init__(self, llm, tools, max_steps=6, force_finish=True):
        self.llm = llm              # 需要实现 generate(messages) -> str
        self.tools = tools          # {name: callable}
        self.max_steps = max_steps
        self.force_finish = force_finish

    def _run_tool(self, name, args):
        if name not in self.tools:
            return {"error": f"unknown tool: {name}"}
        try:
            return {"result": self.tools[name](**args)}
        except TypeError as e:
            return {"error": f"argument mismatch: {e}"}
        except Exception as e:
            return {"error": f"execution failed: {e}"}

    def run(self, user_input):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ]
        for step in range(self.max_steps):
            # 最后一步强制收尾，防止无限循环
            if self.force_finish and step == self.max_steps - 1:
                messages.append({"role": "user",
                                 "content": "已达到步数上限，请直接给出 Final Answer，不要调用工具。"})
            text = self.llm.generate(messages)
            messages.append({"role": "assistant", "content": text})

            if "Final Answer:" in text:
                return text.split("Final Answer:", 1)[1].strip()

            action = self._parse(text)
            if action is None:
                messages.append({"role": "user",
                                 "content": "Observation: 无法解析你的动作，请严格按 Action: name(args) 格式输出。"})
                continue

            obs = self._run_tool(action["name"], action["args"])
            messages.append({"role": "user",
                             "content": f"Observation: {json.dumps(obs, ensure_ascii=False)}"})
        return None

    def _parse(self, text):
        line = next((l for l in text.splitlines() if l.strip().startswith("Action:")), None)
        if not line:
            return None
        expr = line.split("Action:", 1)[1].strip()
        name, _, args_str = expr.partition("(")
        if not args_str.endswith(")"):
            return None
        args_str = args_str[:-1]
        args = {}
        for kv in args_str.split(","):
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            args[k.strip()] = v.strip().strip('"')
        return {"name": name.strip(), "args": args}
```

配套的 `SYSTEM_PROMPT` 决定了行为边界，值得单独认真写：

```text
你是一个订单助手。你的回答必须遵循以下规则：
1. 需要实时数据时，先调用工具，工具结果会以 Observation 形式返回。
2. 每次只输出一个 Thought 和一个 Action，格式如下：
   Thought: <你的思考>
   Action: <tool_name>(arg="value")
3. 当信息足够回答用户时，输出：
   Final Answer: <答案>
4. 禁止编造工具返回结果；工具报错时，向用户说明失败原因而不是假装成功。
5. 只调用 query_order 等只读工具，绝不执行任何修改数据的动作。
```

这段 prompt 的每一句都对应一个真实事故：第 2 条防「一次输出多个动作」导致的解析错乱，第 4 条防「工具挂了模型却编一个结果」，第 5 条是安全红线。

## 生产环境的坑与调优

把 Agent 从笔记本搬到线上，以下问题几乎必然出现，按出现频率排序：

**1. 上下文成本失控。** 每轮工具调用都全量累积，10 步之后 token 可能翻几倍。对策：对 Observation 做压缩（只保留关键字段）、对历史做滑动窗口或摘要、把工具返回的大 JSON 截断到必要字段。可量化的调优参数是 `max_tokens` 与「每步上下文预算」。

**2. 模型「死循环」或「工具成瘾」。** 有的模型会无意义地反复调用工具而不给答案。对策：`max_steps` 硬上限 + 最后一步强制收尾 + 对「连续两次相同调用」做去重拦截（直接返回「你已重复调用该工具，请基于已有结果作答」）。

**3. 工具返回不稳定导致解析崩溃。** 生产上工具来自不同团队，返回格式千奇百怪。对策：在工具边界统一包一层序列化适配器，把异常、超时、空结果都归一化成 `{status, data, error}` 结构，绝不让异常穿透到模型上下文。

**4. 延迟不可控。** 一次任务 = N 次串行 LLM 调用 + 工具 IO，端到端延迟是单次生成延迟的数倍。对策：能并行的步骤用 DAG 并行、降低 `max_steps`、对「一眼能答」的 query 走短路径（先做一次意图分类，简单问题不进循环）。

**5. 安全与成本的双刃剑。** 模型的自由度越高，越可能做出越权调用。对策：工具层鉴权（按调用方身份过滤可用工具）、副作用工具加人工/规则确认、全量记录每次 `Action -> Observation` 用于审计。

下面是一组可落地的默认参数起点，供参考：

| 参数 | 建议默认值 | 说明 |
| --- | --- | --- |
| `max_steps` | 5~8 | 步数上限，兼顾任务完成率与成本 |
| `temperature` | 0.1~0.3 | 工具调用阶段要低温度，保证格式稳定 |
| `max_tokens` | 单次 1024~2048 | 防止单次生成过长拖慢循环 |
| `tool_timeout_ms` | 2000~5000 | 外部调用超时，超时即降级 |
| `observation_truncate` | 2000 字符 | 工具结果回填前的截断上限 |

**排查思路**：Agent 出问题时，第一件事不是调 prompt，而是**看 trace**。把每一步的 `Thought / Action / Observation` 和每一步的 token 数、耗时都结构化落库，问题基本都能定位到三类之一——「模型解析错了」「工具返回了脏数据」「循环策略配置不当」。没有 trace 的 Agent 上线等于闭着眼睛开车。

## 小结与建议

- **先 ReAct 后规划**：短任务用 ReAct 足够，长链路任务再上 Plan-Execute-Reflect，别过度设计。
- **把工具当契约**：Schema 写清楚、参数二次校验、返回统一序列化、副作用隔离，四件事缺一不可。
- **强制收敛**：`max_steps`、最后一步收尾、重复调用去重，是防死循环的三道闸。
- **低温度、稳格式**：工具调用阶段用低 temperature，结构解析比文采重要。
- **可观测优先**：全量记录每一步的动作与观察，出问题先看 trace 再动 prompt。
- **安全红线**：破坏性工具绝不放进自动循环，写操作一律二次确认。
