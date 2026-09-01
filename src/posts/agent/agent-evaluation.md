---
title: "Agent 评测体系与可观测性"
date: 2026-11-14T23:25:00+08:00
category:
  - Agent
tag:
  - 评测
  - 可观测性
  - LLMOps
icon: robot
---

# Agent 评测体系与可观测性

把 LLM 评测的经验直接搬到 Agent 上，几乎一定会翻车。一个单轮的 Chat 模型，输入是 prompt、输出是文本，你可以用困惑度、ROUGE、人工打分甚至一次 API 调用的准确率来度量它。但一个 Agent 是"模型 + 工具 + 规划 + 记忆 + 环境"的复合系统，它的输出不是一段文本，而是一段**轨迹**：若干次模型调用、若干次工具调用、若干次状态变更，最终才收敛到某个结果。评测对象从"一句话"变成了"一个过程"，这意味着整个评测体系的底层假设都要重写。

本文不打算罗列 Benchmark 榜单，而是从我在生产环境落地 Agent 评测的几次返工出发，讲清楚端到端任务评测、轨迹追踪、可观测性埋点和回归评测集建设这四件事怎么做才不是"自嗨"。

## 一、为什么 Agent 评测必须端到端

最典型的误区是把 Agent 拆成零件分别打分：工具调用格式对不对、最终答案和参考答案的相似度够不够高。拆开测看似可控，实则丢失了系统最核心的风险点——**中间步骤的错误会级联放大**。一个工具参数少传了 `limit` 字段，格式评测可能给 100 分，但下游分页逻辑拿到 `None` 直接崩掉；一次检索返回了看似相关实则错误的文档，最终答案可能碰巧和参考答案相似，但推理路径完全错误。

端到端评测的核心指标不是"答案像不像"，而是"**任务是否完成**"。我们用任务成功率（Task Success Rate）作为北极星指标：

```
Task Success Rate = 成功完成任务数 / 总任务数
```

但"成功"不能是一个布尔值拍脑袋。生产上我会把结果分成四档，分别计分：

| 档位 | 含义 | 得分 |
| --- | --- | --- |
| full | 结果完全正确，可直接采用 | 1.0 |
| partial | 结果可用但需人工修正，例如格式对、内容缺一项 | 0.6 |
| fail | 结果不可用，但 Agent 在有限步内正常退出 | 0.0 |
| crash | 未收敛：超时、工具异常未被处理、死循环 | -0.5（作为独立告警指标） |

注意 `crash` 一定要单独统计，不要和普通失败混在一起。普通失败是"能力问题"，`crash` 是"工程缺陷"——后者的修复优先级远高于前者，混在一起会掩盖系统的稳定性恶化。

一个容易踩的坑是**用 LLM 当裁判时给连续分而非档位分**。裁判模型对不同样例的"相似度"标定漂移很大，0.72 和 0.78 之间的差异没有工程意义。改成档位 + 明确的判定标准（rubric），裁判的一致性会显著提升。下面是一段裁判 prompt 的核心约束，要点是把"成功"锚定在可验证的事实上，而不是"你觉得好不好"：

```python
RUBRIC = """
你是评测裁判。基于【参考答案】和【Agent 轨迹】判定任务结果，只输出一个档位。

判定规则（逐条满足才升级）：
- full：最终答案与参考答案在事实层面一致，且关键字段（如数值、名称、数量）完全正确。
- partial：核心意图正确，但存在缺失字段、冗余信息或轻微事实偏差，人工可在一分钟内修正。
- fail：核心意图错误、关键事实错误，或最终答案未回答原问题。
- crash：Agent 未收敛（超过最大步数、连续重复调用同一工具、抛出未捕获异常后中止）。

只输出：full / partial / fail / crash。不要输出理由。
"""

def judge(answer: str, reference: str, trajectory: str) -> str:
    prompt = f"{RUBRIC}\n\n【参考答案】\n{reference}\n\n【Agent 轨迹】\n{trajectory}"
    return llm_call(prompt).strip().lower()
```

另外一个生产级细节：端到端评测一定要**限制步数（budget）并注入超时**。没有步数上限的评测，等于把"效率退化"这个最影响用户体验的问题排除在度量之外。我在评测运行器里统一加了 `max_steps` 和 `wall_time` 两个硬约束，超限即判 `crash`，这直接逼出了两轮工具重试逻辑的优化。

## 二、轨迹追踪：把"黑盒"变成可审计资产

端到端指标告诉你 Agent "行不行"，但不告诉你"为什么不行"。当任务成功率从 82% 掉到 74% 时，你需要的是能回放的完整轨迹，而不是一个冷冰冰的数字。

轨迹追踪的最小单元，我建议按"事件"建模，而不是按"对话轮次"。一次 Agent 运行里混杂着模型思考、工具调用、工具返回、状态读写等异构事件，统一成事件流才能做后续的切片分析。一个精简的轨迹事件结构长这样：

```python
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class TraceEvent:
    run_id: str            # 一次端到端任务的唯一标识
    step: int              # 全局步序号
    kind: str              # llm_call | tool_call | tool_result | plan | retry | finish
    ts: float              # 单调时钟或 epoch 秒
    payload: dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None   # 支持嵌套（如一次 llm_call 内部触发多轮 tool_call）
```

这里有两个经验值得强调：

第一，**`step` 用全局自增序号而非嵌套深度**。嵌套能表达调用关系，但分析"第几步开始跑偏"时，全局序号才能直接对齐多个 run 的时间轴。调用关系用 `parent_id` 单独表达，两件事不要混在一个字段里。

第二，**`retry` 和 `plan` 必须是独立事件，而不是藏在 `llm_call` 的 payload 里**。工具调用失败后的重试、多步规划的中途修改，恰恰是 Agent 系统最容易出 bug 的地方。如果它们不暴露为顶层事件，你事后根本无从统计"重试率"和"规划漂移率"。我在一次排障中就是靠 `retry` 事件发现：某个 LLM 网关超时后，Agent 对同一条工具调用重试了 7 次且每次参数相同，这在指标层面毫无异常，但端到端延迟翻了三倍。

轨迹落地存储时，不要急着上重量级平台。先用 JSONL 按天分片落盘，配合 `run_id` 建立索引就能支撑大部分排查场景：

```bash
# 按天分片 + run_id 快速定位
mkdir -p traces/2026-11-14
python - <<'PY'
import json
for ev in events:
    with open(f"traces/{ev['run_id'][:10]}.jsonl", "a") as f:
        f.write(json.dumps(ev) + "\n")
PY

# 快速回放某个 run 的完整轨迹
grep '"run_id": "r_9f3a"' traces/2026-11-14/*.jsonl | jq -r '[.step, .kind, (.payload.name // "")] | @tsv'
```

当轨迹量级上来后，再迁移到支持 trace 语义的观测后端（如 OpenTelemetry 的 span + attribute），保证"埋点层不变、存储层可替换"。

## 三、可观测性埋点：Metrics / Logs / Traces 三支柱的 Agent 化

Agent 的可观测性不是简单地把日志打全，而是要回答三个不同时间尺度的问题：**现在健康吗**（Metrics）、**刚才那次为什么挂了**（Logs）、**这一个任务完整的执行路径是什么**（Traces）。三者定位不同，埋点方式也不同。

**Metrics** 要覆盖四类，缺一类就是盲区：

| 维度 | 关键指标 | 用途 |
| --- | --- | --- |
| 任务层 | 任务成功率、crash 率、平均完成步数 | 北极星与效率 |
| 模型层 | LLM 调用延迟 P50/P95、token 消耗、重试率 | 成本与延迟异常 |
| 工具层 | 工具调用成功率、工具延迟、空结果率 | 依赖方健康度 |
| 护栏层 | 校验拒绝率、幻觉告警率、人工介入率 | 安全与质量兜底 |

以 Python + OpenTelemetry 为例，工具调用这一层的埋点应当把"调用成功/失败"和"耗时"同时暴露，并且用 label 区分工具名，否则聚合出来没有诊断价值：

```python
from opentelemetry import metrics
from time import perf_counter

meter = metrics.get_meter("agent.tools")
tool_calls = meter.create_counter(
    "agent.tool.calls",
    description="tool call count by name and outcome",
)
tool_latency = meter.create_histogram(
    "agent.tool.latency",
    unit="s",
    description="tool call wall time",
)

def instrumented_call(tool_name: str, fn, *args, **kwargs):
    start = perf_counter()
    try:
        result = fn(*args, **kwargs)
        tool_calls.add(1, {"tool": tool_name, "outcome": "ok"})
        return result
    except Exception:
        tool_calls.add(1, {"tool": tool_name, "outcome": "error"})
        raise
    finally:
        tool_latency.record(perf_counter() - start, {"tool": tool_name})
```

**Logs** 的坑是"结构化但不可用"。很多人把 `payload` 整个 JSON dump 进去，看着结构化，其实字段深度不一致、敏感信息没脱敏、也没有关联 ID。生产上我强制三条：每条日志必须带 `run_id` 和 `step`；工具输入输出必须经过脱敏层（API key、用户手机号、邮箱）；错误日志必须带可重试判定字段，例如 `retryable: bool`。否则告警系统无法区分"瞬时抖动"和"确定性故障"。

**Traces** 的坑是**跨度切分过粗**。如果把一次 Agent 运行只打成一个大 span，那和没有 trace 没区别。正确做法是：一个 run 是一个 root span，每次 `llm_call` 和 `tool_call` 都是子 span，重试是嵌套 span 并打上 `retry_attempt` 属性。这样在 Jaeger 或 Grafana 里，你能一眼看到时间都花在了"模型思考"还是"工具 I/O"上。

一个常被忽略的可观测性信号是**成本**。Agent 的 token 消耗不是平滑的，一次失败重试可能翻倍。我建议把每个 run 的累计 token 作为 metric 上报，并在评测报告里同时输出"成功率 / 平均 token / 平均步数"三连指标——只有成功率而没有成本，优化方向会彻底跑偏。

## 四、回归评测集：让评测可重复、可对比、可防退化

没有固定的评测集，一切评测都是"凭感觉"。但 Agent 的评测集不能像分类任务那样拿几万条标注数据，因为**每条样本都很贵**——一个样本就是一个完整任务的真实环境（可能带数据库、带网页、带文件系统），标注要花几分钟甚至几十分钟。所以回归评测集的策略是"小而精、分层建设、持续腐烂修复"。

回归集的来源有三个，优先级从高到低：

1. **线上真实失败样本**：从 `crash` 和 `fail` 轨迹里提炼，这是最值钱的资产，直接对应生产痛点。
2. **人工构造的边界样本**：覆盖工具参数边界、长上下文截断、模糊指令、多轮歧义消解。
3. **合成样本**：由更强的模型批量生成，再用规则过滤和人工抽检，用于扩充覆盖度。

每个样本必须携带元数据，否则无法做分层分析和消融：

```yaml
- id: case-0231
  source: production-failure        # production-failure | handcrafted | synthetic
  domain: order_refund
  difficulty: hard
  tools: [sql_query, order_api]
  budget: {max_steps: 12, wall_time_s: 90}
  setup: fixture/order_refund/fixture_0231.sql
  reference: fixture/order_refund/expected_0231.json
  rubric: refund_amount_must_match_exact
  tags: [multi-hop, numeric_reasoning]
  golden_trajectory: optional       # 仅关键样本提供，用于轨迹级对比
```

这里最关键的一条工程纪律是：**每个样本必须可重复执行**。所谓"可重复"，指的是 `setup` 脚本能把环境恢复到确定性初始状态（数据库 fixture、mock 外部 API、固定随机种子），否则两次跑同一个样本结果不同，你连"是代码改了还是环境漂了"都分不清。我们在 CI 里强制每次评测前重建 fixture 容器：

```bash
# 每次评测前重置 fixture 环境，保证可重复
docker compose -f fixtures/order_refund/docker-compose.yaml up -d --force-recreate
python -m evals.runner \
  --cases fixtures/order_refund/cases.yaml \
  --model ${MODEL_VERSION} \
  --max-steps 12 \
  --judge gpt-4o \
  --output reports/${MODEL_VERSION}.jsonl
```

回归集还有一个容易被忽视的生命周期问题：**样本会"腐烂"**。当你的工具 schema 升级、提示词模板调整、下游 API 变更后，旧样本可能不再成立——比如 `order_api` 新增了必填参数，旧样本里没有提供，Agent 全部判 `crash`，但这不是 Agent 退化而是评测集过期。所以要建立"评测集健康度"机制：定期用基准模型跑一遍，把连续三轮都 `crash` 且原因与 Agent 能力无关的样本标记为 `stale`，移入待修复队列，而不是默默删掉。

## 五、把评测接进迭代闭环：不是"测一次"，而是"每次变更都测"

评测体系真正的价值在闭环。单次评测报告看三分钟就过去了，而把评测挂进 CI、让每次模型或代码变更都跑一遍回归集，才能阻止退化。但全量回归集在 Agent 场景下往往跑不动——样本带真实环境、带 LLM 裁判，跑一轮可能几十分钟且烧钱。

所以要做**分层门禁**，把"快速反馈"和"深度验证"分开：

| 层级 | 触发时机 | 样本量 | 目的 |
| --- | --- | --- | --- |
| smoke | 每次 commit | 5~10 条关键路径 | 拦截崩溃、格式破坏、超时 |
| regression | 每次 release 候选 | 50~200 条分层样本 | 度量任务成功率与成本变化 |
| soak | 每周 / 大版本 | 全量 + 新采集样本 | 发现长尾与漂移 |

smoke 层必须便宜到可以每次提交都跑，否则闭环必然断裂。我的做法是 smoke 层用 mock 工具 + 缓存 LLM 响应，把单样本成本压到接近零、把时长压到分钟级。只有在 smoke 通过后，才允许触发 regression 层的真实环境评测。

评测结果本身也要可观测。输出不要停在"成功率 78.3%"，而要按样本元数据做切片：按 `domain`、`difficulty`、`tools`、`source` 分组统计。很多退化是局部的——"退款域成功率掉了 12 个点，但整体只掉了 2 个点"——不切片就永远看不到。同时把每个样本的得分存成带 `case_id`、`model_version`、`git_commit` 的时间序列，这样任何一次成功率的波动都能回溯到具体提交。

## 小结与建议

- **端到端优先**：以"任务成功率"为北极星，结果分 `full/partial/fail/crash` 四档，`crash` 单独告警，不要只测零件。
- **轨迹是核心资产**：按事件流建模，全局 `step` 对齐时间轴、`parent_id` 表达嵌套；`retry`、`plan` 必须暴露为顶层事件。
- **三支柱各司其职**：Metrics 覆盖任务/模型/工具/护栏四层，Logs 强制带 `run_id` + 脱敏 + `retryable`，Traces 切到 `llm_call`/`tool_call` 子 span 粒度。
- **成本进评测**：成功率和平均 token、平均步数一起上报，防止"高成功率、高浪费"的假优化。
- **回归集小而精、可重复**：样本带完整元数据，用 fixture 保证环境确定性，定期清理腐烂样本。
- **闭环靠分层门禁**：smoke 便宜到每次提交可跑，regression 做切片分析，结果按 `git_commit` 回溯，把"测一次"变成"每次变更都测"。

Agent 评测的难点从来不是某个炫技指标，而是**把"一个过程"当作一等公民来度量**。当你的体系能回答"这个 Agent 刚才为什么失败、退化了多少、花掉了多少成本"这三个问题时，它才算真正脱离了 demo 阶段。
