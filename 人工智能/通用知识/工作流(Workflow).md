Graph 是 Agent Framework 的底层运行时。**我们建议开发者使用Agent Framework，但直接使用Graph API也是完全可行的。** Graph 是一个低级工作流和多智能体编排框架，使开发者能够实现复杂的应用程序编排。

## Agent 编排的核心引擎

Spring AI Alibaba Graph 是 Agent 编排背后的核心引擎，在底层，Spring AI Alibaba 框架会将 Agent 编排为 Graph，组成一个由节点串联而成的 DAG 图。

### Graph 引擎核心概念与定义

Spring AI Alibaba Graph 有以下三个核心概念：

- **状态（State）**：定义了在 Node 与 Edge 之间传递的数据结构，是整个 Agent 上下文传递的核心载体，具体实现上是一个 `Map<String, Object>`。
- **节点（Node）**：Graph 中的每个 Node 是执行逻辑单元，接受当前 State 作为输入，执行某些操作（如调用 LLM 或自定义逻辑），并返回对 State 的更新。
- **边（Edge）**：定义 Node 间的控制流，可为固定连接，也可依据状态条件动态决定下一步执行路径，实现分支逻辑


![[assert/Pasted image 20260930214602.png]]