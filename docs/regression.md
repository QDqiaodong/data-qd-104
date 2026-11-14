# 全量对账回归防线（tests/test_full_reconciliation_regression.py + tools/mutation_check.py）

为旧全量对账入口（`python -m settlement_audit`，契约见 `docs/contract.md`）建立的
可证明有效的回归防线。生产代码与输出契约不变；本防线只新增测试、离线工具与文档。

## 组成

- `tests/test_full_reconciliation_regression.py`
  固定种子（`SEED=20261114`）场景生成器 + 独立预期模型，全部只用标准库。
  模型**不导入** `settlement_audit`，预期值由生成器自身的结构化事件用整数
  最小单位直接算出，不经过被测的解析、去重或对账函数。验证走真实全量 CLI
  子进程，再逐键核对 `reconciliation.json`、`exceptions.csv` 与
  `audit.sqlite`（ledger/bank/reconciliation/source_hashes 四表）。
- `tools/mutation_check.py`
  离线变异验证：把 `settlement_audit/` 复制到临时目录，逐个植入语义变异，
  用 `SETTLEMENT_AUDIT_SRC` 指到变异副本后运行上面的回归模块，判定每个变异
  是否被新测试捕获。

## 单命令运行

```sh
# 基线：完整测试套件（含新回归模块，现有测试未改动）
python3 -m unittest discover -s tests
# 只跑新回归模块
python3 tests/test_full_reconciliation_regression.py
# 基线 + 全部变异，机器可读结果写到 run/mutation/results.json
python3 tools/mutation_check.py
# 只跑基线 / 自定义报告路径
python3 tools/mutation_check.py --baseline-only --json run/mutation/results.json
```

退出码：基线通过且全部变异被捕获为 0，否则为 1。`--json` 报告含每个变异的
状态（`killed`/`survived`/`invalid`/`timeout`）、捕获它的测试名、耗时、基线
结果，以及生产代码与现有测试在运行前后的 SHA-256 指纹（证明均未改动）。

## 场景覆盖（由 test_scenario_covers_required_contract_features 显式断言）

- USD/EUR/JPY 三币种，且同一 settlement_id 跨币种（S9）不合并；
- 跨结算退款：S6 的退款引用 S5 的同币种支付；
- 等价重复：`100.0` vs `100.00`、前导零写法，重复行只计一次；
- 零净额：全额退款组（银行 0，balanced）与费用全额抵销组（missing_bank，
  expected 0 但银行字段保持 null）；
- 单侧缺失：missing_bank、missing_ledger（含负银行金额、零事件计数）；
- 正/负两个方向的 amount_mismatch；一笔支付被多笔部分退款引用。

乱序、等价金额重写、重复插入三类变换分别断言：业务不变量（分组、金额、
差额、状态、事件计数、汇总、ledger/bank 表内容）逐键不变；允许合理变化的
只有来源摘要（`input_sha256`，断言等于新输入字节的真实 SHA-256 且与基线
不同）与重复计数（`duplicate_ledger_rows`，乱序/重写不变，插入 k 行则恰增 k）。

失败保留：任一断言失败时，把当前 ledger.csv、bank.json、种子、失败阶段、
CLI 输出与重放命令写入 `run/regression_failures/<测试id>/`（可用
`REGRESSION_FAILURE_DIR` 改址），然后原样抛出失败。

## 变异集（6 个，均为可运行的独立语义变异）

| 变异 | 类别 | 位置 | 语义 |
| --- | --- | --- | --- |
| sign_refund_fee_added | 符号 | reconcile.py | 退款/手续费由减变加 |
| currency_refund_parent_check_removed | 币种隔离 | input.py | 退款可引用异币种父支付 |
| dedup_equivalence_inverted | 去重 | input.py | 等价重复被判冲突、真冲突被放过 |
| missing_one_sided_groups_dropped | 缺失 | reconcile.py | 单侧组从并集变交集而消失 |
| difference_direction_flipped | 差额 | reconcile.py | 差额方向反转 |
| refund_full_repayment_rejected | 退款边界 | input.py | 恰好全额退款被误判为超退 |

判定规则：变异副本须先通过导入预检（语法错误/导入失败记 `invalid`，不算
捕获），回归模块在超时内失败记 `killed`，超时记 `timeout`（不算捕获），
通过记 `survived`。最近一次执行记录见 `regression/evidence/`（基线输出、
变异结果 JSON 与运行日志），6/6 全部被新测试捕获。
