---
title: "向量数据库选型与检索优化"
date: 2026-05-09T22:10:00+08:00
category:
  - Agent
tag:
  - 向量数据库
  - 检索
  - 相似度
icon: robot
---

# 向量数据库选型与检索优化

在 RAG（检索增强生成）与 Agent 架构里，向量检索几乎成了所有“记忆”与“知识注入”环节的地基。但我在多个生产系统里反复看到同一个误区：团队把注意力全放在“哪个库跑分更高”上，却忽略了真正决定线上体验的，是**索引结构、召回率与 QPS 之间的三角权衡**，以及数据写入、压缩、过滤、版本治理这些看起来“不那么性感”的工程细节。

本文不打算罗列各家厂商的营销参数，而是从索引原理出发，讲清楚 HNSW 与 IVF 各自的适用边界，给出可复现的调优实验方法，最后落到一份可以直接照做的选型清单。

## 一、先厘清一个前提：向量检索的代价模型

很多人以为“ANN 索引 = 加速的暴力搜索”，这是错误的心智模型。暴力搜索（Flat）做的是**精确**的 Top-K：对 N 条向量逐一计算内积或欧氏距离，复杂度是 O(N·d)，d 是维度。当 N 达到百万级、d 达到 1024，单次查询就是几十亿次浮点运算，QPS 必然上不去。ANN 索引（HNSW、IVF、PQ 等）则是用**可控的召回率损失**去换**数量级的延迟下降**。

代价模型可以粗略写成：

- **Flat**：召回率 100%，延迟随 N 线性增长，内存随 N 线性增长。
- **IVF**：召回率取决于 `nprobe`（探测的聚类数），延迟近似正比于 `nprobe × (N/nlist)`，召回率上限由聚类质量决定。
- **HNSW**：召回率取决于 `ef_search`，延迟近似正比于 `ef_search × M × log(N)` 的常数倍，召回率可以非常接近 100%，但内存开销大（每个节点要存多层邻居）。

理解这个代价模型，是后面所有调优和选型判断的基石：**没有免费的召回率，也没有免费的 QPS，你只是在不同的资源维度之间做置换。**

## 二、HNSW 与 IVF 的索引原理

### 2.1 HNSW：分层可导航小世界图

HNSW（Hierarchical Navigable Small World）把向量组织成一个多层图。底层包含全部节点，越往上节点越稀疏，通过指数衰减的概率决定每个节点能“爬”到第几层。查询时从顶层开始做贪心下降，逐层缩小候选集，最后在底层用 `ef_search` 控制候选队列长度。

它的关键参数只有三个：

| 参数 | 含义 | 调大后 | 典型经验值 |
| --- | --- | --- | --- |
| `M` | 每个节点的最大出边数 | 召回率↑、内存↑、构建时间↑ | 16–64 |
| `ef_construction` | 构建时候选队列长度 | 图质量↑、构建变慢 | 100–500 |
| `ef_search` | 查询时候选队列长度 | 召回率↑、QPS↓ | 100–512 |

HNSW 最大的优点有两个：**召回率可以做到近乎精确**（实测 ef_search 调高后 Recall@10 能到 0.99+），以及**查询路径不依赖全局聚类**，因此对数据分布不敏感。代价是内存占用高——M=16 时每个节点大约要额外存储 16 条边和层级信息，原始向量（d=1024, float32）就要 4KB，图结构再叠加上去，内存很容易翻倍。

```python
# 用 hnswlib 复现 HNSW 参数对召回率/QPS 的影响
import hnswlib
import numpy as np
import time

dim = 1024
num_elements = 200_000

p = hnswlib.Index(space="cosine", dim=dim)
p.init_index(max_elements=num_elements, ef_construction=200, M=16)

data = np.random.random((num_elements, dim)).astype(np.float32)
p.add_items(data)
p.set_ef(50)  # 查询时的 ef_search

# 测量单次查询延迟
q = np.random.random((1, dim)).astype(np.float32)
t0 = time.perf_counter()
labels, _ = p.knn_query(q, k=10)
print(f"ef_search=50, latency={time.perf_counter()-t0:.4f}s")
```

### 2.2 IVF：倒排 + 聚类分桶

IVF（Inverted File）先用 k-means 把全量向量聚成 `nlist` 个簇，每个簇维护一个倒排列表。查询时先计算查询向量到各簇心的距离，只探测最近的 `nprobe` 个簇，再在这些簇内做精确（或压缩）距离计算。

它的关键参数：

| 参数 | 含义 | 调大后 | 典型经验值 |
| --- | --- | --- | --- |
| `nlist` | 聚类数 | 每个簇更小、更精准，但需探测更多簇 | `4 × sqrt(N)` 起步 |
| `nprobe` | 查询探测的簇数 | 召回率↑、延迟↑ | 8–64 |

IVF 的致命软肋是**召回率上限受聚类质量制约**：如果某个真实近邻和查询向量被分到了不同的簇，而你 nprobe 不够大没探测到它，那它就永远找不回来。高维空间里 k-means 的聚类边界往往是模糊的，所以 IVF 想要高召回，nprobe 必须拉得很高，延迟优势就会消失。

### 2.3 一句话选型判断

- **数据规模小（<100 万）、内存充裕、要超高召回**：直接 HNSW，甚至 Flat 都行。
- **数据规模大（千万级+）、内存吃紧、能容忍召回率 90%–95%**：IVF + PQ（乘积量化）压缩，用内存换规模。
- **既想要 HNSW 的召回又想要省内存**：考虑 HNSW + 磁盘（如 hnswlib 的磁盘模式，或 Milvus 的 MMap 能力），但延迟会显著上升。

## 三、召回率与 QPS 的实测权衡

光看原理不够，生产上一定要**用自己的数据做一条 recall–QPS 曲线**。下面给一个可直接跑的评测脚本，思路是：固定索引，扫描一组 `ef_search` / `nprobe`，用暴力搜索结果当 ground truth 算 Recall@K，同时压测 QPS。

```python
# recall_vs_qps.py —— 扫描参数，画召回率-QPS 曲线
import faiss
import numpy as np
import time

d = 256
nq = 1000
gt_k = 10
xb = np.random.random((500_000, d)).astype(np.float32)
xq = np.random.random((nq, d)).astype(np.float32)

# ground truth 用 Flat 索引
index_flat = faiss.IndexFlatIP(xb.shape[1])
index_flat.add(xb)
_, gt = index_flat.search(xq, gt_k)

def bench(index, params, label):
    for val in params:
        index.nprobe = val if hasattr(index, "nprobe") else val
        # HNSW 场景下需要额外 set efSearch，这里以 IVF 为例
        t0 = time.perf_counter()
        D, I = index.search(xq, gt_k)
        elapsed = time.perf_counter() - t0
        recall = sum(len(set(a) & set(b)) / gt_k for a, b in zip(I, gt)) / nq
        qps = nq / elapsed
        print(f"{label} param={val:>4}  Recall@{gt_k}={recall:.4f}  QPS={qps:>8.1f}")

# IVF 场景
nlist = 1024
quantizer = faiss.IndexFlatIP(d)
index_ivf = faiss.IndexIVFFlat(quantizer, d, nlist, faiss.METRIC_INNER_PRODUCT)
index_ivf.train(xb)
index_ivf.add(xb)
bench(index_ivf, [4, 8, 16, 32, 64], "IVF")
```

这套脚本的价值不在绝对数字，而在于帮你**在真实数据上锁定那个“拐点”**：通常 Recall@10 从 0.90 提到 0.98，QPS 会掉 3–5 倍。你要问的不是“最高能多准”，而是“业务能接受多低的召回、能承受多高的延迟”。例如客服问答的 RAG 场景，Recall@10 到 0.95 往往就够了；而金融风控里“命中的证据必须给全”，就宁可牺牲 QPS 也要把 recall 顶到 0.99。

### 3.1 一个真实的生产坑：用离线召回率去推算在线表现

我曾踩过一个典型的坑：离线评测时 Recall@10 高达 0.97，上线后用户反馈“总是答不到点上”。排查发现，离线评测用的是**随机查询向量**，而线上的真实查询经过 embedding 模型后，分布明显偏移，且大量是“近重复问法”。解决思路有二：一是**用线上真实 query 日志重放做评测**，二是关注**排序质量**而非只看 recall——Top-10 里有一个对的但排在第 8 位，RAG 仍然可能失败。

### 3.2 另一个坑：过滤条件让召回率雪崩

带元数据过滤（如 `tenant_id = 'x' AND status = 'active'`）的查询，是向量检索翻车高发区。HNSW 天然不支持“边遍历边过滤”，常见实现是“先找 K 个近邻再过滤”，结果过滤后可能只剩 1–2 条，甚至为空。**正确做法是加大预取倍数（oversampling factor）**，例如目标 10 条，实际取 100 条再过滤；或者用支持过滤感知的索引（如 Qdrant 的 payload filtering、pgvector 的部分索引）。这块一定要在压测时用带过滤条件的真实查询来测，否则线上会出现“返回为空”的诡异现象。

## 四、主流向量库对比

下面这张表是选型的核心参考。注意“擅长场景”比“跑分”重要得多，因为跑分测的大多是裸检索，而生产要的是**过滤、混合检索、运维、成本**的综合能力。

| 数据库 | 类型 | 索引能力 | 过滤/混合检索 | 强项 | 短板 |
| --- | --- | --- | --- | --- | --- |
| **Milvus** | 专用向量库 | HNSW/IVF/PQ/GPU/磁盘 | 标量过滤、多向量、分区 | 规模大、吞吐高、生态成熟 | 运维重，需要 etcd/MinIO 等组件 |
| **Qdrant** | 专用向量库 | HNSW + 量化 | 强大的 payload 过滤、稀疏向量 | 过滤 + 向量混合检索体验好 | 超大规模（10 亿级）经验少 |
| **Weaviate** | 专用向量库 | HNSW/Flat | GraphQL、混合检索(带 BM25) | 开箱即用、可插拔模块 | 中文生态较弱 |
| **pgvector** | PostgreSQL 扩展 | HNSW/IVFFlat | SQL 天然支持过滤、事务 | 复用 PG 事务/运维/权限，数据同库 | 单机规模有限，超高 QPS 吃力 |
| **Elasticsearch** | 全文检索库 + 向量 | HNSW/量化 | BM25 + 向量混合检索强 | 已有 ES 团队零成本接入 | 向量写入与内存成本高 |
| **FAISS** | 库（非服务） | 全部主流索引 | 需自己封装 | 灵活、可深度定制、极致性能 | 无服务化、无持久化、无过滤 |

几个容易被忽略但致命的差异点：

1. **持久化与一致性**：FAISS 是内存库，进程一挂全没了，必须自己做落盘和重建。做服务选型时优先考虑自带持久化和副本能力的库。
2. **删除与更新**：向量库的删除大多是“标记删除 + 定期压缩”（tombstone），删除不会立刻释放内存，也不会立刻从召回结果里消失。频繁更新的场景要特别留意。
3. **压缩算法**：`PQ`（乘积量化）、`SQ`（标量量化）能在召回率损失几个点的情况下把内存降到 1/4–1/16。千万级以上的规模，压缩几乎是刚需。
4. **过滤的执行位置**：有的库在召回前过滤（pre-filtering），有的在召回后过滤（post-filtering），还有的能做到召回中过滤。这直接决定了带过滤查询的召回质量。

## 五、生产落地清单

把前面的经验收敛成一份可操作的选型与调优清单：

1. **先问规模**：<100 万用 pgvector 或 Qdrant 单机就能搞定，别上 Milvus 这种重组件；千万级优先 Milvus/专门库；上亿考虑分片 + 磁盘索引。
2. **先问团队**：已经有 PostgreSQL 或 ES 团队，优先复用，向量库的运维成本远高于很多人的想象。
3. **必测三条曲线**：召回率–QPS 曲线、内存–数据量曲线、带过滤查询的召回衰减曲线。缺一不可。
4. **用真实 query 重放评测**：离线随机向量的 recall 会严重高估线上表现。
5. **维度与归一化对齐**：embedding 模型升级会导致维度变化和向量空间漂移，所有历史向量必须**重算重建**，不能混用。用 `metric` 字段显式声明距离类型（内积/余弦/L2），别靠默认值。
6. **给 embedding 加版本**：向量数据要带 `embedding_model_version` 元数据，便于灰度回滚和分批迁移。
7. **过滤查询加大预取**：带过滤的查询把预取倍数调到 5–10 倍，避免返回空结果。
8. **监控四个指标**：P50/P99 延迟、Recall@K（抽样评估）、内存水位、索引构建时长。Recall 不是一次性测完就完事，要定期用抽样 query 回归。
9. **删除要留预算**：频繁更新的场景，定期触发压缩（compact/vacuum），否则内存和磁盘会持续膨胀。

## 小结与建议

- **HNSW 用于“高召回 + 中小规模”**，IVF+PQ 用于“大规模 + 省内存”，Flat 用于“百万以下 + 必须精确”。
- **选型先看过滤、混合检索、运维与团队，再看跑分**；专用库和扩展库各有利弊，别被 benchmark 牵着走。
- **召回率与 QPS 是硬权衡**，用真实数据画出曲线，锁定业务可接受的拐点，而不是一味追求最高 recall。
- **向量库不是一次性建设**：embedding 版本治理、删除压缩、带过滤查询的预取、召回率回归监控，这些“脏活”才是生产系统长期稳定的关键。

向量检索的本质，是用工程手段在“准、快、省”三者之间做可解释的置换。把代价模型吃透，把评测做扎实，选型自然水到渠成。
