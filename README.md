# sre-failover-runbook-concurrency-validator

校验并发故障切换手册中的依赖、互斥和越权动作。

> **重要：本工具只做静态校验，不执行任何 runbook 步骤。**
> 它读取一份计划（runbook 集合），分析所有可能的并发交错是否安全，
> 然后输出报告。它永远不会连接数据库、修改流量入口或扩容任何系统。

## 背景

故障时多个值班人员会同时执行不同的 runbook：一个人在切主库，另一个人在
改流量入口，第三个人在扩容。每个步骤单独看都合法，但并发交错执行后可能
互相撤销对方的成果。本工具在**执行前**对计划做并发校验。

## 输入模型

计划以 JSON 描述（见 `examples/incident_plan.json`），每个步骤包含：

- `operator`：执行该 runbook 的值班人员；
- `depends_on`：同一 runbook 内的步骤依赖（DAG）；
- `reads` / `writes`：步骤读取 / 修改的资源；
- `locks`：步骤声明持有的资源锁；
- `preconditions`：步骤开始时检查的前置条件（`resource: expected`）；
- `checkpoint`：完成后是否记录 checkpoint（操作者重启后从最近的
  checkpoint 恢复，跳过已完成步骤）；
- `timeout`：步骤可能等待的秒数（等锁、等 drain 等）；
- `rollback`：撤销本步骤的步骤 id；
- `fencing`：提交前必须重新校验的 fencing 条件（如 epoch 检查）；
- `idempotent`：重复执行是否无害。

## 检测内容

- `write_conflict`：两个并发步骤修改同一资源且没有共同锁；
- `rollback_overwrite`：回滚把资源恢复到旧状态，覆盖了无序的后续提交；
- `stale_precondition`：前置检查在等待期间被其他 runbook 改写而失效；
- `duplicate_execution`：非幂等且没有 checkpoint 保护的步骤，操作者
  重启后会被重复执行（包括重复回滚）；
- `missing_fencing`：破坏性步骤缺少 fencing 条件；
- `lock_bypass`：写入被已知锁保护的资源却未声明持有该锁。

只读检查（`kind: read`）之间永不冲突，可以安全并行；破坏性步骤
（`kind: destructive`）必须声明 fencing 条件。

## 输出

- **危险交错（hazards）**：每条包含类型、涉及步骤、资源和说明；
- **推荐暂停点（pause points）**：建议在哪些步骤前暂停并人工确认；
- **最小互斥范围（exclusion scopes）**：必须在同一把锁下互斥执行的
  最小资源集合及涉及的步骤；
- **可串行化顺序（serializable order）**：满足依赖与冲突约束的一种
  串行执行顺序；只读步骤保持无序，可并行。

## 使用

```bash
python -m runbook_validator examples/incident_plan.json          # 文本报告
python -m runbook_validator examples/incident_plan.json --json   # JSON 报告
```

计划存在危险交错时退出码为 1，安全时为 0。

## 测试

```bash
python -m unittest
```

覆盖并发切主、重复回滚、checkpoint 恢复和资源锁绕过等场景。

兼容 Python 3.8.2+。
