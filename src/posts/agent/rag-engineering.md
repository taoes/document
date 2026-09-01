---
title: "RAG 检索增强生成的工程化实践"
date: 2025-08-16T16:45:00+08:00
category:
  - Agent
tag:
  - RAG
  - 检索
  - 向量
icon: robot
---

# RAG 检索增强生成的工程化实践

## 引言：RAG 不是"向量库 + LLM"的拼接

很多团队把 RAG 理解成"把文档灌进向量库、召回 Top-K、拼进 Prompt"，结果上线后幻觉率不降反升，用户反馈"答非所问"甚至"引用编造"。RAG 是一个典型的 **Garbage In, Garbage Out** 系统：检索质量决定了生成质量的上限。真正决定成败的，不是模型能力，而是文档切分、混合检索、重排、引用溯源与评估这五个环节的工程细节。

本文不重复讲"什么是向量检索"，而是聚焦我在生产环境踩过的坑与最终沉淀下来的流水线设计。文中代码均为可运行的简化实现，可直接作为脚手架。

## 一、文档切分：切分策略决定召回天花板

切分是 RAG 的起点，也是被低估最严重的环节。切分错误会在源头丢失上下文，后面所有环节都无法挽回。

### 1.1 固定长度切分的致命缺陷

最简单的 `RecursiveCharacterTextSplitter` 按字符数切分，遇到表格、代码块、跨段落语义时会被拦腰截断。一个真实的故障：用户问"退款政策中对虚拟商品的规定"，文档里这句话跨越了两个 chunk 边界，召回的 chunk 只有前半句，模型于是编造了后半句。

### 1.2 生产级切分策略

我的做法是 **结构感知 + 语义兜底 + 重叠窗口** 三层组合：

1. 优先按文档结构（标题、段落、表格、列表）切分；
2. 对超长段落再用语义切分器兜底；
3. 相邻 chunk 保留 10%–15% 的重叠，缓解边界截断。

```python
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

headers = [
    ("#", "h1"),
    ("##", "h2"),
    ("###", "h3"),
]

md_splitter = MarkdownHeaderTextSplitter(headers, strip_headers=False)
structured = md_splitter.split_text(markdown_doc)

# 对仍超长的块做兜底切分，并保留重叠
fallback = RecursiveCharacterTextSplitter(
    chunk_size=512,
    chunk_overlap=64,          # 约 12% 重叠
    separators=["\n\n", "\n", "。", ".", " ", ""],
)

chunks = []
for block in structured:
    text = block.page_content
    if len(text) <= 512:
        chunks.append(block)
    else:
        for piece in fallback.split_text(text):
            chunks.append({**block.metadata, "content": piece})
```

### 1.3 关键调优参数

| 参数 | 典型取值 | 说明 |
| --- | --- | --- |
| chunk_size | 256–512 token | 中文约 300–600 字；问答场景偏小，长文档理解偏大 |
| chunk_overlap | 10%–20% | 过小边界易截断，过大则冗余、召回重复 |
| 结构优先级 | 标题 > 表格 > 段落 | 表格应整表保留，绝不从中间切开 |
| 元数据 | 标题/章节/页码/版本 | 溯源与过滤的基础，缺失则无法做路由 |

**坑**：重叠虽然降低截断风险，但会引入重复内容。若不做去重，同一段话会被召回多次，浪费上下文窗口并稀释信噪比。生产上必须在入库前做 **chunk 级指纹去重**（如 MinHash 或哈希）。

## 二、混合检索：单独用向量或关键词都会翻车

### 2.1 为什么纯向量不够

向量检索擅长语义近似，但对以下场景几乎必错：精确编号（"RFC 6455"）、缩写（"CPU" vs "中央处理器"）、专有名词、ID、代码标识符。反之，纯关键词检索（BM25）无法理解"加班补偿"与"996 相关赔付"的语义等价。

### 2.2 混合检索与融合

生产级方案是 **向量 + BM25 并行召回，再做 RRF（Reciprocal Rank Fusion）融合**。Elasticsearch 8.x 原生支持向量字段，可把两种检索合并到一次查询：

```bash
curl -s -X POST 'http://localhost:9200/docs/_search' -H 'Content-Type: application/json' -d '{
  "size": 20,
  "query": {
    "bool": {
      "should": [
        { "match": { "content": { "query": "虚拟商品退款政策" } } }
      ]
    }
  },
  "knn": {
    "field": "embedding",
    "query_vector": [0.01, 0.02, 0.03],
    "k": 20,
    "num_candidates": 100
  },
  "rank": {
    "rrf": { "window_size": 50, "rank_constant": 60 }
  }
}'
```

### 2.3 融合参数与路由

- `rank_constant`（RRF 中的 k，默认 60）：越大越削弱排名靠前文档的优势，结果更"平均"；调小则强化头部文档。我一般保持 60，遇到"第一名过于集中"时调到 20。
- 路由（Router）比无脑融合更有效：先对 query 分类，精确查询走 BM25，语义问答走向量，综合查询才融合。一个小分类模型或 LLM 判定即可，能显著减少噪声。

| 检索方式 | 适用场景 | 典型弱点 |
| --- | --- | --- |
| 纯向量 | 语义改写、同义表达 | 精确匹配、低频词、ID |
| 纯 BM25 | 编号、专名、代码、法律条文 | 无语义泛化 |
| 混合 + RRF | 通用生产场景 | 参数需针对语料调优 |
| 混合 + 路由 | 高精度要求 | 多一跳分类，增加延迟 |

## 三、重排（Rerank）：把"相关"变成"准确"

向量/BM25 召回的 Top-K 只是"粗排"，排序质量远不足以支撑生成。重排器用更强的交叉编码器（cross-encoder）对 query-doc 逐对打分，是性价比最高的质量提升手段。

```python
from sentence_transformers import CrossEncoder

reranker = CrossEncoder(
    "BAAI/bge-reranker-v2-m3",
    max_length=512,       # 只对前 512 token 打分，长文档需截断策略
)

def rerank(query: str, docs: list[str]) -> list[dict]:
    pairs = [(query, d) for d in docs]
    scores = reranker.predict(pairs)
    ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
    return [{"text": d, "score": float(s)} for d, s in ranked]
```

### 3.1 关键实践

- **两阶段**：粗排召回 50–100 条，重排后只取 Top 5–10 送入 LLM。这样既控制延迟，又保证上下文里都是"高信噪比"内容。
- **截断风险**：重排器输入上限多为 512 token，长 chunk 会被静默截断，导致"后半段明明有关键答案却被判低分"。方案是给 reranker 传入"query + chunk 摘要"或在入库时同时存一份摘要字段。
- **分数阈值**：重排分数低（如 <0.1）说明"库里根本没有答案"，此时应触发拒答或"我不知道"，而不是硬拼上下文让模型编。

**坑**：把重排分数直接当成"置信度"展示给用户是有害的。交叉编码器分数只代表"相对相关"，不代表"事实正确"，两者必须分离。

## 四、引用溯源：让每个句子有据可查

生产级 RAG 的可信度，最终落在"引用能否被验证"上。用户不只要答案，还要能点开出处核对。

### 4.1 溯源链路设计

1. 每个 chunk 入库时携带唯一 `chunk_id` 和来源元数据（文件、章节、页码、URL）；
2. Prompt 中强制模型只依据给定文档回答，并要求逐句标注引用；
3. 生成后用 **NLI（自然语言推理）模型** 校验"引用是否真的支持该句"，过滤掉"张冠李戴"的引用。

```yaml
# 溯源元数据示例（入库时随向量一并写入）
- chunk_id: "chunk_8f3a2b"
  source:
    file: "terms_of_service_v3.pdf"
    page: 12
    section: "5.2 退款政策"
    url: "https://example.com/tos#5.2"
  content: "虚拟商品一经激活或使用，不支持退款……"
```

### 4.2 引用的两个常见翻车点

- **幻觉引用**：模型引用了一个不存在或内容不符的 `chunk_id`。必须在解析层做校验：引用的 id 必须在当次上下文召回集合内，否则直接丢弃。
- **来源失效**：文档更新后旧引用指向过期内容。做法是给每个 chunk 打 `version` 与 `valid_until`，并在检索时过滤过期 chunk。

一个可运行的引用校验片段：

```python
def validate_citations(answer: str, retrieved_ids: set[str]) -> list[str]:
    import re
    cited = set(re.findall(r"\[(chunk_\w+)\]", answer))
    valid = cited & retrieved_ids       # 引用必须是本次真正召回的
    invalid = cited - retrieved_ids
    if invalid:
        raise ValueError(f"幻觉引用: {invalid}")
    return sorted(valid)
```

## 五、评估：没有度量，所有优化都是玄学

RAG 系统的评估必须覆盖 **检索 + 生成** 两个层面，且必须线上持续回归，而不是上线前跑一次就完事。

### 5.1 检索评估指标

| 指标 | 含义 | 何时关注 |
| --- | --- | --- |
| Recall@K | Top-K 召回中是否含答案 | 首要指标，召回不足则后续全废 |
| MRR | 第一个相关文档的排名倒数均值 | 相关文档是否靠前 |
| nDCG@K | 考虑排序质量的归一化折损累计增益 | 重排效果评估 |
| Hit Rate | 是否命中 | 粗粒度兜底指标 |

### 5.2 生成评估指标

RAGAS 是目前最常用的框架，核心是把答案拆成三个维度独立打分：

```python
from ragas import evaluate
from ragas.metrics import faithfulness, answer_relevancy, context_precision
from datasets import Dataset

ds = Dataset.from_dict({
    "question": ["虚拟商品能退款吗？"],
    "answer": ["虚拟商品一经激活不支持退款。 [chunk_8f3a2b]"],
    "contexts": [["虚拟商品一经激活或使用，不支持退款……"]],
})

result = evaluate(ds, metrics=[faithfulness, answer_relevancy, context_precision])
print(result)
```

- **Faithfulness（忠实度）**：答案是否严格由上下文支撑，直接对应用户最怕的"编造"。
- **Answer Relevancy**：答案是否切题。
- **Context Precision**：上下文里相关内容的比例，反映"拼进来的噪声多不多"。

### 5.3 生产评估流水线

不要只靠"人工看几组样例"。我的做法是：

1. 从线上日志采样真实 query，用 LLM 自动生成评测集（golden set），人工抽检校正；
2. 每次改动切分参数、重排模型、Prompt 后，跑一轮离线回归，产出 Recall@K / Faithfulness 对比；
3. 保留一组"历史翻车用例"作为回归集，任何改动若让旧 case 变差即回滚。

## 六、生产级 RAG 流水线总览

把上述环节串起来，一条可落地的流水线如下：

```
离线入库：原始文档 → 解析 → 结构切分 → 元数据/指纹 → Embedding → 向量库(ES/向量DB)
在线查询：Query → 意图路由 → 混合召回(向量+BM25) → RRF融合 → 重排 → 阈值判定
        → 上下文组装 → LLM生成(带引用约束) → 引用校验 → 输出
评估回流：线上日志 → 评测集生成 → RAGAS回归 → 指标看板 → 参数/模型迭代
```

几个贯穿全链路的生产铁律：

- **上下文预算**：LLM 上下文有限，拼进去的每条 chunk 都要"值得"。宁可少而精，不要多而滥。
- **缓存与去重**：相同 query 命中缓存，避免重复高成本检索与生成。
- **可观测性**：为每个 query 记录检索耗时、召回条数、重排分数、最终引用 id，出问题才能定位是"没召回"还是"召回了没生成对"。
- **降级与兜底**：检索超时降级为 BM25；重排服务不可用跳过重排；置信度不足走拒答而非硬答。

## 小结与建议

- 切分优先保证**结构与表格完整性**，用重叠缓解截断，但必须配合 chunk 去重。
- 检索用**混合召回 + RRF 融合**，精确查询走路由，别迷信单一向量检索。
- 重排是性价比最高的提升点：粗排宽召回、重排精筛选，注意 512 token 截断坑。
- 引用要做**逐句溯源 + NLI 校验**，并过滤幻觉 chunk_id 与过期来源。
- 用 Recall@K 与 Faithfulness 双指标做**持续回归**，上线前建立 golden set，任何改动先过评测。
- 把"检索没有答案就拒答"写进 Prompt 与流程，比任何模型微调都更能抑制幻觉。

RAG 的工程价值不在于模型有多强，而在于能否把"对的内容"稳定、可验证地送到模型面前。抓住切分、检索、重排、溯源、评估这五个抓手，就能避免绝大多数生产事故。
