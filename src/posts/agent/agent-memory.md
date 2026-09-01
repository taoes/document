---
title: "Agent 记忆系统设计：短期与长期记忆"
date: 2026-01-24T20:15:00+08:00
category:
  - Agent
tag:
  - 记忆
  - 上下文
  - Agent
icon: robot
---

# Agent 记忆系统设计：短期与长期记忆

在构建生产级 Agent 时，最容易被低估的往往不是模型能力或工具调用，而是**记忆系统**。一个没有记忆的 Agent 每次对话都从零开始，无法识别用户、无法复用已确认的偏好、无法从历史失败中修正策略；而一个记忆失控的 Agent 则会在上下文里堆积大量无关信息，导致成本线性上升、指令遵循能力急剧退化。本文从工程实现角度，拆解短期记忆（会话窗口）、摘要记忆、向量长期记忆，以及检索与遗忘机制的完整设计链路，重点放在真实生产环境中踩过的坑与可落地的调优方案。

## 一、记忆分层：先定义边界，再谈技术

在动手写代码之前，必须明确记忆系统的分层模型，否则很容易把"缓存""状态""记忆"混为一谈。我习惯将 Agent 的记忆按**生命周期与访问延迟**划分为四层：

| 记忆类型 | 生命周期 | 典型存储 | 访问延迟 | 典型容量 | 丢失后果 |
| --- | --- | --- | --- | --- | --- |
| 工作记忆（会话窗口） | 单次会话 | 进程内存 / Redis | < 1ms | 几 K ~ 几十 K token | 丢失近期对话脉络 |
| 摘要记忆（压缩态） | 跨轮次 | Redis / 数据库 | < 10ms | 数百 ~ 数千字 | 丢失历史结论但可重建 |
| 长期事实记忆 | 永久 | 向量库 + 元数据库 | 10 ~ 200ms | 百万级 chunk | 用户画像、偏好丢失 |
| 程序记忆（技能/规则） | 永久 | 代码 / 提示词模板 | 静态 | 固定 | Agent 行为定义被破坏 |

关键认知是：**记忆不是"存得越多越好"，而是"在正确的时间以正确的粒度被召回"**。下面逐层展开实现细节。

## 二、会话窗口：短期记忆的成本陷阱

会话窗口是最基础的记忆载体——把历史对话直接拼进 prompt。实现上最简单，但也是成本与性能问题的重灾区。

### 2.1 朴素实现与 token 爆炸

最常见的做法是把所有历史消息无脑 append 到 messages 数组：

```python
from typing import List, Dict

class NaiveWindowMemory:
    """危险示范：无上限累积，成本随轮次线性增长"""
    def __init__(self):
        self.history: List[Dict[str, str]] = []

    def add(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})

    def build_prompt(self) -> List[Dict[str, str]]:
        # 没有任何截断，第 20 轮时 prompt 可能已达数万 token
        return self.history
```

问题在于：第 1 轮可能只有 500 token，到第 15 轮就可能膨胀到 2 万 token。除了金钱成本，更隐蔽的伤害是**注意力稀释**——模型对长上下文中间位置的指令遵循能力显著下降（即"迷失在中间"问题），且越靠后的无关内容越容易污染生成质量。

### 2.2 滑动窗口与"锚点保留"

工程上通常采用**滑动窗口 + 不可丢弃锚点**策略：保留最近 N 条消息，同时把 system prompt、用户核心诉求、已确认的关键结论作为"锚点"固定在窗口头部。

```python
class SlidingWindowMemory:
    def __init__(self, max_tokens: int = 8000, reserve_tokens: int = 1500):
        self.max_tokens = max_tokens          # 窗口预算上限
        self.reserve_tokens = reserve_tokens  # 锚点保留预算
        self.anchors: List[Dict] = []         # 不可淘汰的固定信息
        self.history: List[Dict] = []

    def add(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        self._trim()

    def _trim(self) -> None:
        # 粗略估算：1 token ≈ 4 个字符（中文约 1.5~2 字符/token，需按模型校准）
        def est_tokens(m) -> int:
            return len(m["content"]) // 2

        budget = self.max_tokens - self.reserve_tokens
        # 从最旧的普通消息开始淘汰，但保证不破坏"一轮问答"的完整性
        while sum(est_tokens(m) for m in self.history) > budget and len(self.history) > 2:
            self.history.pop(0)  # 淘汰最早的消息

    def build_prompt(self) -> List[Dict]:
        return self.anchors + self.history
```

**生产调优点**：

- **token 估算必须用真实 tokenizer**，不要用 `len/4` 这种粗略公式。中文与代码场景差异巨大，建议直接用 `tiktoken` 或模型服务返回的 `usage.prompt_tokens` 做闭环校准。
- **淘汰粒度按"轮次"而非"消息"**：一条 user + 一条 assistant + 工具调用结果应视为一个不可分割的原子单元，避免出现"有问无答"的残缺上下文。
- **锚点要显式管理**：把用户在一次会话中反复强调的约束（如"预算不超过 500 元"、"用 Python 实现"）单独抽出来，即使超出窗口也强制保留，否则 Agent 会在长会话后期"忘记最初的指令"。

### 2.3 踩过的坑

1. **工具结果的体积失控**：一次网页抓取或数据库查询可能返回几万字符，直接塞进窗口会瞬间挤爆预算。务必在工具返回层做截断（`result[:N]` + `...（已截断，共 X 条）`）。
2. **`pop(0)` 的性能问题**：Python 列表头部删除是 O(n)，长列表高频操作会拖慢响应。改用 `collections.deque`，或在 token 预算变化时才重算。
3. **多轮工具调用被打散**：滑动窗口淘汰时若把某个工具调用的结果删了却留下调用记录，模型会看到"未完成"的 tool_calls。必须在淘汰逻辑里维护工具调用的配对关系。

## 三、摘要记忆：用"压缩"换"持久"

滑动窗口只能解决短期问题，一旦超出窗口，早期对话就彻底丢失。摘要记忆的核心思想是：**把历史对话压缩成一段高信息密度的文本，作为跨轮次的持久上下文**。

### 3.1 递归摘要（Rolling Summary）

最稳健的工程模式是**递归摘要**——不重复摘要已摘要的内容，而是"新事件 + 旧摘要 → 新摘要"，把摘要成本控制在 O(1)：

```python
async def rolling_summarize(llm, old_summary: str, new_events: str) -> str:
    """递归摘要：只处理增量事件，避免重复消费历史 token"""
    prompt = f"""你是会话摘要器。请把下面的旧摘要与新增对话合并为一份新的结构化摘要。

要求：
1. 保留用户的核心目标、已确认的决策与硬性约束。
2. 保留关键事实（人名、数字、结论、文件路径）。
3. 丢弃寒暄与已被推翻的中间推理。
4. 输出不超过 500 字，用要点列表。

旧摘要：
{old_summary}

新增对话：
{new_events}

新摘要："""
    return await llm.complete(prompt)
```

**递归摘要 vs 全量重摘要**的对比：

| 方案 | 每轮成本 | 信息损失 | 稳定性 | 适用场景 |
| --- | --- | --- | --- | --- |
| 全量重摘要 | O(n)，随轮次线性增长 | 低（每次看到全部历史） | 高 | 会话较短、对准确度要求极高 |
| 递归摘要 | O(1)，恒定 | 累积性损失 | 中 | 长会话、成本敏感的生产环境 |

递归摘要的代价是**误差会累积**：一旦某轮摘要遗漏了关键事实，后续摘要再也无法恢复它。缓解手段是让摘要器输出**半结构化格式**（如 YAML），把"硬事实"和"过程叙述"分开，并对硬事实做显式保留。

### 3.2 触发时机：不要每轮都摘要

摘要是有损压缩，频繁触发会丢失细节。生产上的策略是**阈值触发**：当会话窗口 token 数超过阈值（如窗口预算的 80%）时，才触发一次摘要，把最早 50% 的内容折叠进摘要，腾出窗口空间。这样既能控制成本，又保留了近期对话的完整细节。

```yaml
# 记忆编排配置示例
memory:
  window:
    max_tokens: 8000
  summary:
    trigger_ratio: 0.8        # 窗口占用 80% 时触发
    fold_ratio: 0.5           # 折叠最旧的 50% 进入摘要
    max_summary_chars: 2000   # 摘要长度上限
  model:
    summarizer: "gpt-4o-mini" # 摘要用轻量模型降本
```

**坑**：摘要模型不要与主 Agent 用同一个重量级模型——摘要任务是结构化的低难度任务，用便宜的小模型即可，能省 80% 以上的摘要成本。但要保证摘要模型与主模型**指令风格一致**，否则摘要里混入的措辞可能带偏主 Agent 的判断。

## 四、向量长期记忆：从"能存"到"能检索"

摘要记忆解决的是"这条会话聊了什么"，而向量长期记忆解决的是"跨会话的持久事实"——用户是谁、喜欢什么、上次用了什么方案、团队约定是什么。这些信息不能靠摘要承载（太稀疏），而要按语义建索引。

### 4.1 架构：写路径与读路径分离

```text
写路径：新事实 → 抽取 → 归一化 → 向量化 → 写入向量库 + 元数据表
读路径：当前 query → 向量化 → 相似检索 → 重排 → 注入 prompt
```

核心代码骨架：

```python
import hashlib
from typing import List

class LongTermMemory:
    def __init__(self, vector_store, embedder, reranker):
        self.store = vector_store      # 如 Milvus / pgvector / Qdrant
        self.embedder = embedder       # 嵌入模型，如 text-embedding-3-small
        self.reranker = reranker       # 可选：重排模型，提升召回精度

    def _stable_id(self, scope: str, text: str) -> str:
        """用 scope+文本哈希作为幂等键，避免重复写入同一事实"""
        return hashlib.sha256(f"{scope}:{text}".encode()).hexdigest()

    async def remember(self, scope: str, fact: str, metadata: dict) -> None:
        vec = await self.embedder.embed(fact)
        await self.store.upsert(
            id=self._stable_id(scope, fact),
            vector=vec,
            payload={"scope": scope, "text": fact, "ts": metadata.get("ts"), **metadata},
        )

    async def recall(self, scope: str, query: str, top_k: int = 5) -> List[dict]:
        qvec = await self.embedder.embed(query)
        candidates = await self.store.search(qvec, top_k=top_k * 3, filter={"scope": scope})
        if self.reranker:
            candidates = await self.reranker.rerank(query, candidates)
        return candidates[:top_k]
```

### 4.2 关键工程决策

- **按 scope（用户/项目/团队）分租户**：检索时必须带 `scope` 过滤，否则会串号——把 A 用户的隐私记忆返回给 B 用户，这是生产事故级别的错误。
- **幂等键设计**：长期记忆的写入经常是"同一事实被反复抽取"，没有幂等键会导致向量库无限膨胀。用 `scope + 归一化文本哈希` 作为主键，重复写入走 upsert。
- **元数据与向量分离**：向量只负责"找到"，元数据负责"可信"。时间戳、来源、置信度、类型（偏好/事实/规则）都存在 payload 里，检索命中后先校验元数据再使用。

### 4.3 检索精度的坑与排查

向量检索最常见的翻车点是"检索到了但不相关"或"相关但没检索到"。排查思路：

1. **先查召回，再查排序**：把 top_k 放大 3~5 倍看候选集里有没有正确答案——如果候选集里都没有，是**嵌入模型或切分问题**；如果候选里有但排序靠后，是**重排问题**。
2. **切分粒度决定天花板**：一个 chunk 混入多个话题，会让向量语义被"平均"掉。用户画像类记忆建议**一条事实一个 chunk**，而不是一段长文本一个 chunk。
3. **阈值要按数据集标定**：不要迷信"相似度 > 0.8 就相关"的拍脑袋阈值。不同嵌入模型的余弦相似度分布差异巨大，应离线跑一遍标注集，画出相似度分布，选一个能平衡精确率与召回率的阈值。

### 4.4 嵌入与向量库选型对比

| 维度 | text-embedding-3-small | text-embedding-3-large | 本地开源（bge-m3） |
| --- | --- | --- | --- |
| 维度 | 1536 | 3072 | 1024 |
| 中文效果 | 良好 | 优秀 | 优秀（中文友好） |
| 成本 | 极低 | 约 3~4 倍 | 无 API 成本，需自建 GPU |
| 数据安全 | 需出域 | 需出域 | 本地可控 |
| 适用 | 大规模高吞吐 | 高精度场景 | 合规敏感、需私有化 |

向量库选型上，`pgvector` 适合已有 PostgreSQL 的中小团队（运维成本最低），`Qdrant`/`Milvus` 适合大规模与需要丰富过滤表达式的场景，`FAISS` 适合离线/实验。

## 五、遗忘机制：记忆系统里最容易被忽略的一半

只做"记住"不做"遗忘"的记忆系统，最终会退化成一座垃圾场——检索命中率下降、噪声污染 prompt、存储成本失控。**遗忘不是 bug，是特性**，需要像缓存淘汰一样精心设计。

### 5.1 三类遗忘策略

| 策略 | 机制 | 适用 | 风险 |
| --- | --- | --- | --- |
| TTL 过期 | 给记忆设置过期时间 | 时效性强的信息（如"本周优惠"） | 误删仍有价值的长效事实 |
| 访问频率淘汰 | LRU/LFU，久未命中的降权或删除 | 海量低价值记忆 | 低频但重要的记忆被误删 |
| 冲突消解 | 新事实与旧事实冲突时，标记旧事实失效 | 可变事实（如用户地址、职位） | 需要可靠的冲突检测 |
| 用户主动遗忘 | 提供删除接口，尊重用户权利 | 合规（GDPR 等） | 需做物理删除而非软删 |

### 5.2 遗忘与记忆的动态平衡

一个实用的工程模型是给每条记忆维护一个**价值分**，综合"新鲜度 + 命中频率 + 来源可信度 + 用户确认度"：

```python
def memory_score(created_at, hit_count, confidence, now) -> float:
    import time
    age_days = (now - created_at) / 86400
    # 指数衰减新鲜度 + 对数增长的命中权重 + 置信度
    freshness = 0.6 * (0.9 ** age_days)
    hit_weight = 0.3 * min(1.0, hit_count / 10.0)
    return freshness + hit_weight + 0.1 * confidence
```

低于阈值的记忆进入"候选遗忘"队列，但**不要立即删除**——先降权观察一段时间，若在观察期内又被命中则恢复，否则才真正清理。这种"软遗忘"能有效避免误删。

### 5.3 合规与安全

长期记忆里存的是用户的真实数据，遗忘必须可审计、可追溯。至少要做到：

1. **物理删除**：软删标记之外，向量库与元数据库里的原始数据要能真正删除，不能只改一个 `is_deleted` 标志。
2. **删除传播**：如果某条事实是从会话摘要派生出来的，删除原事实时要能定位并处理派生副本。
3. **授权边界**：记忆的写入、读取、删除都要走统一的权限校验，避免"工具能读到记忆但日志里查不到谁读的"这种安全盲区。

## 小结与建议

- **先分层再实现**：会话窗口（短期）、摘要记忆（压缩）、向量长期记忆（语义索引）、程序记忆（规则）四层各司其职，不要试图用一个结构解决所有记忆需求。
- **窗口用"滑动 + 锚点"**：token 估算必须用真实 tokenizer，淘汰按"轮次"为原子粒度，工具返回结果务必在入口截断。
- **摘要用"递归 + 阈值触发"**：递归摘要把成本压到 O(1)，用轻量模型做摘要，硬事实用半结构化格式显式保留以对抗误差累积。
- **长期记忆读写分离**：按 scope 隔离租户、幂等键防重、元数据与向量分离；检索先诊断"召回"再诊断"排序"。
- **把遗忘当一等公民**：TTL + 频率淘汰 + 冲突消解 + 用户主动删除四管齐下，软遗忘优先于硬删除，并保证合规可审计。
- **监控是记忆系统的生命线**：至少跟踪窗口 token 占用、摘要触发频率、检索命中率与延迟、记忆库增长速度四个指标，否则记忆系统的退化会悄无声息。

记忆系统的本质不是"存下更多"，而是"在合适的时机、以合适的粒度、注入最相关的那几条信息"。把上面这套分层与遗忘机制跑通，你的 Agent 才能从"每轮失忆的对话机器人"变成"越用越懂你的长期助手"。
