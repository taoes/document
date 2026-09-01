---
title: "多 Agent 协作：LangGraph 与编排模式"
date: 2025-12-13T19:05:00+08:00
category:
  - Agent
tag:
  - 多Agent
  - LangGraph
  - 编排
icon: robot
---

# 多 Agent 协作：LangGraph 与编排模式

当单个 LLM 调用无法可靠完成复杂任务时，工程师会本能地把问题拆成多个"专家"，让它们协作。这个直觉是对的，但也是最容易翻车的地方：多 Agent 系统的失败，很少来自某个模型不够聪明，而几乎全部来自**协调（orchestration）层做得不严谨**。上下文如何在 Agent 之间传递？谁有权决定下一步？失败了谁能兜底？这些问题如果不显式建模，最终得到的往往不是"协作"，而是一群各自为政的 Agent 把 token 和延迟烧光后返回一份缝合怪结果。

本文不打算再复述"多 Agent 很酷"的陈词滥调，而是从工程视角拆解两个本质问题：**控制流应该由图来显式表达，还是让 Agent 自主协商**；以及 **LangGraph 如何把状态、条件边、人机回环这三件事落到可调试、可恢复的生产代码里**。

## 一、两条路线：图式编排 vs 自主协作

多 Agent 系统在工程上大致分成两个极端，中间是一整片光谱：

| 维度 | 图式编排（Graph / Workflow） | 自主协作（Autonomous / Swarm） |
| --- | --- | --- |
| 控制流 | 显式图，节点与边由开发者定义 | 隐式，由 Agent 对话/路由协议涌现 |
| 可预测性 | 高，路径可静态分析、可单测 | 低，路径依赖模型当下的判断 |
| 失败定位 | 精确到节点 | 困难，往往只能回放整段会话 |
| 成本/延迟 | 可控，路径固定 | 波动大，易产生循环与空转 |
| 适应未预见任务 | 弱，需改图 | 强，理论上可自行探索 |
| 适用场景 | 流程确定、需合规审计、多轮批处理 | 开放探索、任务边界模糊 |

**结论先行：生产环境优先选图式编排，把"自主"限制在叶子节点内部。** 原因不是哲学偏好，而是一条朴素的运维定律——*不可观测、不可复现的系统无法被运维*。自主协作听起来优雅，但它把最重要的调度决策交给了一个非确定性组件，出了问题你甚至连"它为什么会找 Agent B"都难以回答。图式编排则把调度权留在代码里，Agent 只负责"节点内部的理解与生成"，这正是两者职责最健康的切分。

一个常见的折中叫 **Supervisor / Router 模式**：用一个轻量的调度 Agent 作为中心，通过结构化输出（如函数调用）选择下一个执行者，但整体仍是一个有明确节点和回边约束的图。这既保留了图的可观测性，又让路由规则具备一定灵活性。下一节我们看它如何用 LangGraph 落地。

## 二、StateGraph：把"协作"还原成"状态迁移"

LangGraph 的核心是 `StateGraph`。它的关键设计是：**节点之间不直接传消息，而是读写一份共享的、带 schema 的状态**。这个看似微小的约束解决了多 Agent 系统最致命的腐化问题——每多一个 Agent 就多一套随意的上下文拼装逻辑，最终没有任何人知道"此刻的上下文到底是什么"。

```python
from typing import TypedDict, Annotated, Literal
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

class TeamState(TypedDict):
    messages: Annotated[list, add_messages]  # 会话历史，add_messages 负责追加而非覆盖
    task: str
    draft: str          # 写手产出
    review: str         # 审稿人意见
    approved: bool      # 门控状态
```

`Annotated[list, add_messages]` 是理解 LangGraph 状态的钥匙：它定义了**归并函数（reducer）**。多个节点并行写同一个 `messages` 字段时，`add_messages` 保证消息按序追加而不是互相覆盖。没有 reducer 的裸字段则是"后写覆盖先写"，这本身也是一种语义，但你必须心里有数，否则并发节点会悄悄吃掉彼此的产出。

节点是纯函数，签名统一为 `(state) -> partial_state`：

```python
def writer(state: TeamState) -> dict:
    # 只返回需要更新的字段，LangGraph 负责 merge 回 state
    return {"draft": f"初稿：{state['task']}"}

def reviewer(state: TeamState) -> dict:
    ok = "错别字" not in state["draft"]
    return {"review": "通过" if ok else "退回", "approved": ok}
```

图本身是声明式的，构建与运行分离：

```python
builder = StateGraph(TeamState)
builder.add_node("writer", writer)
builder.add_node("reviewer", reviewer)
builder.add_edge(START, "writer")
builder.add_edge("writer", "reviewer")
builder.add_edge("reviewer", END)

graph = builder.compile()
# 运行返回最终 state；invoke 内部已完成状态归并
result = graph.invoke({"task": "写一份周报"})
```

这段代码看起来平平无奇，但它已经体现了多 Agent 工程最该被强调的一点：**协作的骨架应该是一个有向图，而不是 prompt 里的几句"请和 XX 协作"**。图可以被可视化、被单测、被版本管理；prompt 里的协作意愿则什么都不能保证。

## 三、条件边：让路由成为可测试的代码

固定边只能表达线性流程，而真实任务的难点在于分支：评审没过要打回重写，关键词命中要转人工，预算超了要降级。LangGraph 用**条件边（conditional edge）**把路由决策从模型嘴里拿出来，变成一段可审计的函数。

```python
from typing import Literal

def route_after_review(state: TeamState) -> Literal["writer", "human_review", "END"]:
    # 规则优先：确定性路由永远比模型判断更可靠、更便宜
    if not state["approved"] and len(state["messages"]) < 3:
        return "writer"          # 最多自动重写 3 轮，防止无限循环
    if "敏感" in state["task"]:
        return "human_review"    # 合规红线，强制转人工
    return "END"

builder.add_conditional_edges(
    "reviewer",
    route_after_review,
    {"writer": "writer", "human_review": "human_review", "END": END},
)
```

条件边函数返回的字面量必须出现在映射表里，否则运行期会抛 `InvalidUpdateError`——这是好事，它把"漏配路由"从线上事故提前变成启动即报错。

这里埋着一个生产环境的经典坑：**循环边必须有明确的退出条件**。上例中 `len(state["messages"]) < 3` 就是防止 writer↔reviewer 形成死循环的护栏。无数团队在演示时一切正常，上线后因为某个评审永远返回"退回"，Agent 就在图里空转，直到触发全局 `recursion_limit`（默认 25）被硬性打断。与其依赖这个兜底，不如在路由函数里显式计数。

当路由规则本身依赖语义判断时，把决策交给一个带结构化输出的模型是合理的，但要给它"退路"：

```python
def llm_router(state: TeamState) -> str:
    resp = router_llm.invoke(
        state["messages"],
        tools=[{"name": "choose", "parameters": {"route": {"enum": ["writer", "human_review", "END"]}}}],
    )
    # 兜底：模型没调用工具、或给非法值，一律走安全路径
    if not resp.tool_calls:
        return "human_review"
    return resp.tool_calls[0]["args"]["route"]
```

原则是：**模型负责"建议"，代码负责"决定"**。任何让模型输出直接变成控制流的写法，都要为它准备一个默认分支和一个超时/校验分支。

## 四、人机回环：checkpoint 与 interrupt 的工程细节

真正的生产 Agent 不会完全无人值守。涉及资金、对外发布、删除数据等高风险动作，需要人在环内（human-in-the-loop，HITL）。LangGraph 为此提供了两件基础设施：**checkpoint（状态持久化）** 与 **interrupt（中断等待）**。

先用 SQLite 持久化检查点：

```python
from langgraph.checkpoint.sqlite import SqliteSaver

conn = sqlite3.connect("agent_state.db", check_same_thread=False)
memory = SqliteSaver(conn)

graph = builder.compile(checkpointer=memory)

# 必须传 thread_id，checkpoint 以线程为隔离单元
config = {"configurable": {"thread_id": "ticket-42"}}
graph.invoke({"task": "审批退款 2000 元"}, config)
```

有了 checkpointer，图在每一步之后都会把完整状态落盘。这意味着三件关键能力：**崩溃后从断点续跑**、**按 `thread_id` 审计完整轨迹**、**支持 `interrupt` 悬停等待外部输入**。

在需要人工确认的节点前插入中断：

```python
from langgraph.types import interrupt

def approve_refund(state: TeamState) -> dict:
    decision = interrupt({
        "question": "是否批准这笔 2000 元退款？",
        "context": state["task"],
    })
    # 代码执行到这里会暂停，直到外部 resume 传入 decision
    return {"approved": decision == "yes", "review": f"人工结论：{decision}"}
```

运行时会抛出 `GraphInterrupt` 并把状态冻结。外部系统（比如你的工单后台）随后用同一个 `thread_id` 恢复：

```bash
# 人工审核员在前端点下"批准"后，后端执行等价于：
curl -X POST /api/agent/resume \
  -d '{"thread_id": "ticket-42", "command": "yes"}'
```

```python
# 服务端实际恢复逻辑
from langgraph.types import Command

graph.invoke(
    Command(resume="yes"),   # resume 值会作为 interrupt() 的返回值继续执行
    config={"configurable": {"thread_id": "ticket-42"}},
)
```

这里有两个极易踩的坑，务必记牢：

1. **`interrupt` 之前的所有步骤必须已经在 checkpoint 里**。如果你在 `interrupt` 之前的节点做了副作用（发通知、写库）却没做幂等处理，恢复时图会重放这些节点，导致重复副作用。正确做法是：副作用要么放到 `interrupt` 之后，要么设计为幂等。
2. **`thread_id` 就是你的业务主键**，不要用随机 UUID 一了了之。它是排障时还原"这个用户当时到底经历了什么"的唯一线索，应绑定到会话、工单或订单 ID。

排查 HITL 问题的标准动作，是直接读检查点库：`SELECT thread_id, checkpoint FROM checkpoints WHERE thread_id = 'ticket-42'`，把序列化后的 state 解出来看 `interrupt` 处冻结的中间态，而不是盲目加日志。

## 五、生产落地：性能、成本与可观测性

图式编排让控制流确定，但性能与成本仍然会失控，根源通常在这几处。

**一是无谓的上下文膨胀。** 每经过一个节点，若都往里塞完整对话史，token 会以 O(n²) 增长。对策是让节点只返回最小状态增量，并在进入昂贵节点前做**上下文裁剪**（只保留任务描述、上一步产出、必要约束），而不是无脑传 `messages`。

**二是并行节点没有真正并行。** LangGraph 的 `Send` API 支持 map-reduce 式的扇出：

```python
from langgraph.types import Send

def fanout(state: TeamState):
    # 对每个子任务各发一个 reviewer 节点，结果经 reducer 归并
    return [Send("reviewer", {"task": sub}) for sub in state["subtasks"]]
```

但并发节点写同一字段时必须用带 reducer 的 `Annotated`，否则后写覆盖先写，你会在线上看到"偶发丢失结果"这种最难查的问题——它只在特定时序下出现。

**三是没有为每个节点埋观测。** 至少记录四件事：节点名、输入/输出 token 数、耗时、以及 checkpoint 写入是否成功。LangGraph 的 callback/`astream_events` 能拿到节点级事件流，建议直接接入现有 APM，而不是自己 print。

**四是模型级别的参数与图级参数的混淆。** `temperature`、`top_p` 属于模型层；`recursion_limit`、并发度、checkpoint 策略属于图层。把 `recursion_limit` 从默认 25 调大要慎之又慎——它是对失控循环的最后一道保险，调大它之前先确认你的条件边退出逻辑是健全的。

最后给一个可操作的排障清单，遇到多 Agent 行为异常时按序排查：

1. 打印整张图的拓扑（`graph.get_graph().draw_mermaid()`），确认实际路径与你脑中的一致；
2. 用最小输入单步 `invoke`，在节点函数里打断点，确认每个节点的 state 进/出是否符合预期；
3. 检查条件边函数返回的字面量是否都在映射表内；
4. 检查循环边是否有计数/退出条件，确认 `recursion_limit` 未被触发；
5. 检查 HITL 节点的副作用幂等性，以及 `thread_id` 是否稳定。

## 小结与建议

- **优先图式编排，把"自主"关进节点里**：调度权留在代码，Agent 只做理解与生成，可观测性才有保障。
- **用 `StateGraph` 统一状态语义**：显式 schema + reducer 归并，杜绝并发覆盖与上下文腐化。
- **路由 = 条件边函数**：确定性规则优先，模型只提供"建议"，代码做最终决定，且必须有兜底分支。
- **循环必须有护栏**：显式计数 + 退出条件，别把 `recursion_limit` 当业务逻辑用。
- **人机回环靠 checkpoint + interrupt**：`thread_id` 绑定业务主键，副作用放中断之后或做幂等，排障先查检查点库。
- **上线前自检四件事**：图的拓扑可视化、节点级 token/耗时观测、并发字段的 reducer、HITL 的幂等与恢复。
