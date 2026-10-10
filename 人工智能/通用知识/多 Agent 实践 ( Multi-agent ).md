**Multi-agent** 将复杂的应用程序分解为多个协同工作的专业化Agent。与依赖单个Agent处理所有步骤不同，**Multi-agent架构**允许你将更小、更专注的Agent组合成协调的工作流。

Multi-agent系统在以下情况下很有用：

- 单个Agent拥有太多工具，难以做出正确的工具选择决策
- 上下文或记忆增长过大，单个Agent难以有效跟踪
- 任务需要**专业化**（例如：规划器、研究员、数学专家）

## Multi-agent模式

Spring AI Alibaba支持以下Multi-agent模式：

| 模式 | 工作原理  | 控制流 | 使用场景 |
| --- | --- | --- | --- |
| **Tool Calling**| Supervisor Agent将其他Agent作为_工具_调用。<br/>"工具"Agent不直接与用户对话——它们只执行任务并返回结果。 | 集中式：<br/>所有路由都通过调用Agent。 | 任务编排、结构化工作流。 |
| **Handoffs**  | 当前的Agent决定将控制权转移给另一个Agent。<br/>活动Agent随之变更，用户可以继续与新的Agent直接交互。 | 去中心化: <br/> Agent可以改变当前由谁来担当活跃Agent。 | 跨领域对话、专家接管。  |



## Handoffs 模式


## Agent 作为工具
