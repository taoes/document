---
title: "分布式事务与 Seata AT 模式实现原理"
date: 2023-08-19
category:
  - Java
tag:
  - 分布式事务
  - Seata
  - 微服务
icon: code
---

# 分布式事务与 Seata AT 模式实现原理

在单体架构时代，事务靠数据库的 ACID 就能兜底；一旦服务拆成微服务，一笔业务跨越订单、库存、账户三个独立数据源时，`@Transactional` 就彻底失效了——每个服务的事务都是"局部自治"的，任何一个环节失败都不会回滚别人已经提交的改动，最终落得一地数据不一致。本文不打算泛泛介绍"什么是分布式事务"，而是从工程视角拆解四种主流方案的取舍，再深入 Seata AT 模式的两阶段提交、全局锁与 undolog 细节，最后给出我在生产环境踩过的坑与调优清单。

## 一、先想清楚：为什么大部分场景首选"最终一致性"

分布式事务的本质是一场权衡：强一致、高可用、低延迟三者不可兼得，也就是 CAP 的三角博弈。很多人一上来就引入 TCC 或者 Seata，其实先要回答三个问题：

1. 这笔业务对**实时一致性**的诉求有多强？是转账这种必须原子，还是积分发放这种可以延迟到账？
2. 失败后能否通过**补偿 + 幂等**兜底，而不需要回滚？
3. 跨服务的调用链路中，真正的"事务边界"究竟在哪？

如果答案允许秒级或分钟级的最终一致，那么**本地消息表**、**事务消息（RocketMQ 半消息）**或 **SAGA 补偿**往往比强一致方案更轻、更抗故障。只有当业务确实要求"要么全部成功、要么全部回滚"时，才轮到 2PC 类协议（含 Seata AT）上场。把这个前置判断做对，能替团队省下一大半的复杂度和事故。

## 二、四种方案横向对比

下表从一致性、性能、侵入性、适用场景四个维度做一次工程视角的对比，而不是教科书式的罗列：

| 方案 | 一致性 | 性能开销 | 代码侵入 | 典型场景 | 致命短板 |
| --- | --- | --- | --- | --- | --- |
| 2PC（XA） | 强一致 | 高（全程持锁） | 低（JDBC 层） | 传统数据库跨库，链路短 | 同步阻塞、协调者单点、性能差 |
| TCC | 强一致（应用层） | 中（两阶段资源预留） | 高（Try/Confirm/Cancel 都要写） | 资金、账务等强资金场景 | 实现复杂、补偿幂等难、空回滚/悬挂 |
| SAGA | 最终一致 | 低（异步补偿） | 中（需提供正向+逆向操作） | 长流程、跨多个老系统 | 无隔离性，可能读到中间态 |
| Seata AT | 强一致（读已提交级别） | 中（全局锁 + 快照） | 低（注解即可） | 业务改造少、要求回滚的通用场景 | 有锁竞争、依赖 undolog 快照、热点数据易冲突 |

这里要特别纠正一个常见误区：**Seata AT 并非"零侵入、无代价"的银弹**。它的"低侵入"只体现在业务代码上，背后是全局锁、分支事务快照、undolog 生成与回滚比对这一整套运行时成本，以及 TC（Transaction Coordinator）这个新的中心化依赖点。

## 三、Seata 的三角色与 AT 两阶段提交

Seata 把一个全局事务拆成三个角色：

- **TC**：事务协调者，独立部署的 Server，维护全局事务状态与全局锁。
- **TM**：事务发起方，`@GlobalTransactional` 所在的方法，负责开启、提交、回滚全局事务。
- **RM**：资源管理器，各参与服务，向 TC 注册分支事务并汇报状态。

AT 模式的核心思路是：**"先提交本地事务，再根据全局决议决定是否回滚"**，它把 XA 那种"全程锁到二阶段"改造成了"一阶段就释放本地锁、只保留全局锁"。两阶段流程如下：

**一阶段（业务 + 快照）**：

1. TM 向 TC 发起全局事务，拿到全局事务 ID（XID）。
2. 各 RM 在本地事务内执行业务 SQL。
3. 执行前，Seata 通过数据源代理 `DataSourceProxy` 解析 SQL，生成 **before image**（变更前数据快照）。
4. 执行后，生成 **after image**（变更后数据快照）。
5. 将 before/after image 与分支信息写入 **undolog** 表，并向 TC 注册分支事务。
6. 本地事务提交，**本地锁随之释放**；同时 Seata 在 TC 侧为该行数据申请**全局锁**。

**二阶段（提交或回滚）**：

- **全局提交**：TM 通知 TC，TC 标记事务完成，异步删除各分支的 undolog。这个过程非常轻量，几乎零开销。
- **全局回滚**：TC 通知各 RM 回滚。RM 拿到 undolog 中的 before image，**反向校验当前数据是否仍等于 after image**，一致则用 before image 覆盖回滚；不一致说明出现了脏写，抛出异常并触发人工介入。

下面是一段最小可运行的 Seata AT 调用链示例：

```java
// 订单服务：全局事务发起方（TM）
@Service
public class OrderService {
    private final StockClient stockClient;
    private final AccountClient accountClient;

    @GlobalTransactional(name = "create-order", timeoutMills = 300000)
    public void createOrder(OrderDTO order) {
        // 1. 本地：扣减库存（RM 分支事务）
        stockClient.deduct(order.getSkuId(), order.getCount());
        // 2. 本地：扣减余额（RM 分支事务）
        accountClient.debit(order.getUserId(), order.getAmount());
        // 3. 本地：落订单主表
        orderMapper.insert(order);
        // 若第 3 步抛出异常，TC 会回滚第 1、2 步的本地事务
    }
}
```

```java
// 库存服务：资源管理器（RM），只需普通本地事务
@Service
public class StockService {
    @Transactional
    public void deduct(Long skuId, Integer count) {
        int rows = stockMapper.deduct(skuId, count);
        if (rows == 0) {
            throw new BusinessException("库存不足");
        }
    }
}
```

```yaml
# application.yml：Seata 客户端关键配置
seata:
  tx-service-group: my-tx-group        # 需与 TC 的 registry 配置对应
  service:
    vgroup-mapping:
      my-tx-group: default
    grouplist:
      default: 127.0.0.1:8091
  registry:
    type: nacos
    nacos:
      server-addr: 127.0.0.1:8848
      namespace: seata
      group: SEATA_GROUP
  config:
    type: nacos
```

## 四、全局锁：AT 模式防脏写的命门

一阶段提交后本地锁已释放，如果没有额外保护，别的全局事务完全可能在这段"悬挂期"读到并修改同一行数据，从而让回滚时的 after image 校验失效。**全局锁就是用来堵这个洞的**。

全局锁由 TC 统一管理，粒度是**"表 + 主键行"**，在分支事务注册时申请、全局事务结束时释放。它的关键行为：

- 一阶段提交前必须拿到全局锁；拿不到则按 `lockRetryInterval` 重试，直到 `lockRetryTimes` 超限后**回滚本地事务并抛出 `LockConflictException`**。
- 全局锁与本地锁交错存在：本地锁只存在于本地事务期间（毫秒级），全局锁横跨整个全局事务（可能秒级到分钟级）。
- 全局锁不是分布式互斥锁的万能替代品——它防的是**写写冲突**，读到未提交中间态的**脏读**问题它并不能完全消除（AT 默认隔离级别是"读已提交"，中间态仍可能被外部读可见）。

生产上最常见的翻车场景是**热点行**：秒杀、库存扣减这类集中写同一行的业务，全局锁会退化成串行瓶颈，`LockConflictException` 刷屏。这时候应当优先在业务层做削峰、分桶（把一行库存拆成 N 行分片），而不是把 `lockRetryTimes` 无脑调大。

排查全局锁冲突时，可以直接查 TC 侧日志或 `lock_table`（DB 模式存储全局锁时）：

```sql
-- 观察当前占用的全局锁
SELECT xid, transaction_id, table_name, pk, row_key, branch_id
FROM lock_table
ORDER BY create_time DESC;
```

同时注意：**全局锁必须在 TC 处维护，不能放到业务库里自己实现**。一旦 TC 宕机或锁表损坏，全局锁的释放语义必须与全局事务生命周期严格一致，否则会出现"幽灵锁"——事务早已结束，锁却永久残留，后续所有相关分支事务全部超时。

## 五、undolog：快照、回滚与性能代价

undolog 是 AT 模式的"后悔药"，也是它最大的性能开销来源。核心表结构（以 MySQL 为例）大致如下：

```sql
CREATE TABLE `undo_log` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `branch_id` BIGINT NOT NULL COMMENT '分支事务ID',
  `xid` VARCHAR(100) NOT NULL COMMENT '全局事务ID',
  `context` VARCHAR(128) NOT NULL,
  `rollback_info` LONGBLOB NOT NULL COMMENT '序列化后的before/after image',
  `log_status` INT NOT NULL COMMENT '0正常 1已全局完成',
  `log_created` DATETIME NOT NULL,
  `log_modified` DATETIME NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ux_undo_log` (`xid`, `branch_id`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;
```

一阶段里，Seata 会对每条被 `INSERT/UPDATE/DELETE` 影响的行都做快照：before image 是修改前该行的完整字段，after image 是修改后的值，二者序列化后一起塞进 `rollback_info` 的 LONGBLOB。这意味着：

- **批量更新是大忌**：一条 `UPDATE ... WHERE status = 0` 命中 10 万行，就会生成 10 万行的快照，undolog 膨胀到数百 MB，一阶段耗时暴涨，甚至把 TC 和业务库一起拖垮。生产上要严格控制单条 SQL 的影响行数，或改成分批 + 显式主键。
- **大字段（TEXT/BLOB）参与快照**会显著放大序列化体积，建议对含大字段的表单独评估是否纳入全局事务。
- 快照比对依赖**行数据自增主键的确定性**，因此 AT 模式要求被代理的表**必须有主键**；无主键表会直接报错。

回滚时的校验逻辑同样值得警惕：Seata 默认用 after image 与当前数据做逐字段比对，若发现不一致会抛出 `DataValidationException`，意味着在全局事务存续期间有人"绕过 Seata"改写了数据——典型原因是有非 Seata 连接（如手工脚本、DBA 直连、未纳入代理的代码路径）直接动了同一行。这个异常**不应被吞掉**，它其实是数据被脏写的报警信号，需要人工核查业务是否允许该覆盖。

undolog 的清理也要显式配置，否则表会无限增长：

```properties
# Seata Server 配置：定期清理已全局完成的历史 undolog
# 单位毫秒，生产建议设为一个合理周期（如 6 小时）
log.retention.period=21600000
```

## 六、生产环境实战清单

下面这些是我在生产里实际踩过、也被反复验证过的要点，按优先级排序：

1. **TC 必须高可用 + 持久化**。TC 是新的单点，务必用 DB 或 Redis 模式存储全局事务与全局锁，并部署多实例。测试环境用 file 模式图省事，生产一定要切 DB 模式，否则 TC 重启后所有在途事务全部丢失、锁状态错乱。
2. **XID 要贯穿链路**。跨线程、跨消息（RocketMQ/Kafka）时，必须手动透传 `RootContext.getXID()`，否则下游分支事务会各自为战，全局回滚形同虚设。异步场景务必确认 XID 的线程隔离边界。
3. **超时与锁参数按业务调**。`timeoutMills` 默认 30 秒偏大，长链路拖住全局锁会放大锁冲突；`lockRetryInterval` 默认 10ms、`lockRetryTimes` 默认 30 次，热点业务需要配合分桶而不是纯加次数。
4. **幂等是底线**。AT 模式本身不保证"恰好一次"，二阶段提交与回滚在网络抖动下都可能重试，业务侧仍要做幂等，尤其涉及资金与库存。
5. **监控四类指标**：全局事务总数/耗时、回滚率、锁冲突数（`LockConflictException` 频率）、undolog 表增长。回滚率突增通常是业务异常或脏写，锁冲突突增通常是热点设计问题。
6. **禁止大事务**。避免在 `@GlobalTransactional` 里放 RPC 之外的耗时操作（外部通知、慢查询、重试），全局锁持有时间与业务耗时成正比。
7. **版本与序列化对齐**。seata-server 与客户端版本要一致，序列化方式（seata/hessian/protobuf/kryo）要在全链路统一，否则会出现反序列化失败这类隐蔽问题。

## 小结与建议

- 选型先问"是否真的需要强一致"，能用最终一致就别上 AT/TCC。
- AT 模式是"低业务侵入 + 全局锁 + undolog 快照"的组合拳，代价藏在运行时代价与锁竞争里，不是免费的。
- 理解全局锁的"表 + 主键行"粒度与生命周期，是排查锁冲突和脏写的钥匙。
- 生产务必保证 TC 高可用与持久化、XID 全链路透传、undolog 定期清理、业务幂等四件事同时到位。
- 热点行、批量更新、大字段快照是 AT 模式三大性能杀手，要在设计阶段就规避，而不是等事故后调参。

分布式事务没有银弹，Seata AT 只是工具箱里"强一致且改动小"的那个选项。把它用对的前提，是真正理解两阶段提交、全局锁和 undolog 三者如何协作，以及在哪些边界上会失效。
