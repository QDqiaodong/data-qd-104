# data-qd-104 — Settlement Audit / 支付结算对账

把虚构支付平台全量流水与独立银行结算表对齐，保留每个结算币种的差异原因。

已实现：支付/退款/手续费规范化、币种小数精度、同 ID 等价去重与冲突拒绝、退款原支付约束、结算/币种汇总、精确差额和缺失分类、SQLite 可查询证据、差异 CSV 与完整 JSON。

新增：连续月度不可变导入与跨月退款核验（独立入口与独立 SQLite 结构，见 `docs/monthly_contract.md`）。每个导出月份作为不可变事实接收，退款可引用历史月份或同月支付，累计退款不得超过本金；历史月报在后续月份导入后保持字节不变。

## 独立安装

需要 Python >=3.11（先确认 python3 --version）。运行依赖全部为标准库。 构建工具也已固定版本。
```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
```

首次安装可能需要联网；安装后核心演示与测试可离线运行。每仓独立 `.venv`，不使用其他项目源代码或环境。macOS/Linux 使用相同命令；具体已验证环境不由此声明。

## 演示、输出与测试

在仓库根目录执行。首次 `run/` 不存在即为干净数据状态；需要干净重演可指定新的输出路径。

```sh
.venv/bin/python -m settlement_audit --output run/audit
.venv/bin/python -m settlement_audit --output run/replay
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -c 'import sqlite3; db=sqlite3.connect("run/audit/audit.sqlite"); print(db.execute("SELECT * FROM reconciliation ORDER BY settlement_id,currency").fetchall())'
```

8 行流水含 1 等价重复，7 唯一事件。4 个结算/币种组：S1 USD 12700 分平衡；S2 JPY 银行少 1 日元；S3 EUR 缺银行记录；S4 USD 缺内部流水。

相同输入重放得到一致 JSON；允许同路径全量重算。输入契约失败在创建新报告前报错，已有报告不会因此被伪装为新结果。运行 SQLite 与报告均在忽略的 `run/` 内。

## 连续月度导入与跨月退款核验

独立入口、独立 SQLite 结构（版本 1）与独立报告（`schema_version=2`），不改动上面的全量入口和 v1 输出。月份必须严格连续（首次可为任意有效月份，之后只接受下一个日历月，支持跨年），已接收月份不可变；同规范事实的重复导入识别为 `replayed` 且不二次扣减退款本金。

```sh
.venv/bin/python -m settlement_audit.monthly ingest --period 2026-01 \
  --ledger data/monthly/2026-01_ledger.csv --bank data/monthly/2026-01_bank.json \
  --db run/monthly/monthly.sqlite
.venv/bin/python -m settlement_audit.monthly ingest --period 2026-02 \
  --ledger data/monthly/2026-02_ledger.csv --bank data/monthly/2026-02_bank.json \
  --db run/monthly/monthly.sqlite
.venv/bin/python -m settlement_audit.monthly ingest --period 2026-03 \
  --ledger data/monthly/2026-03_ledger.csv --bank data/monthly/2026-03_bank.json \
  --db run/monthly/monthly.sqlite
.venv/bin/python -m settlement_audit.monthly report --period 2026-02 \
  --db run/monthly/monthly.sqlite --output run/monthly/report_2026-02.json
```

内置合成场景：一月 p1 收款 100 USD、手续费 3 USD，银行 97 USD；二月两笔引用 p1 的退款 30+20 USD 与 1 USD 手续费，银行 -51 USD（两月同一 `settlement_id` 各自平衡）；三月再退 50 USD 后 p1 剩余本金为 0。二月历史报告始终显示累计已退 5000、剩余 5000 分，即使三月之后重放二月也字节不变。Python 调用为 `settlement_audit.monthly.ingest(...)` 与 `settlement_audit.monthly.report(...)`。月份归属、键作用域、累计退款、重放、历史余额与故障重跑规则见 `docs/monthly_contract.md`。

## 结构与业务契约

- `settlement_audit/money.py：货币精度和有符号最小单位`
- `settlement_audit/input.py：去重与退款引用完整性`
- `settlement_audit/reconcile.py：按币种净额与差异归因`
- `settlement_audit/pipeline.py：SQLite、JSON 和差异 CSV`
- `settlement_audit/monthly.py：不可变月度导入、跨月退款预算、历史月报与月度 CLI`
- `docs/contract.md`：全量模式完整字段/格式、业务键、边界、输出含义与当前约束。
- `docs/monthly_contract.md`：月度模式月份序列、表结构粒度、重放、事务边界与 v2 报告结构。
- `data/`：小型确定性合成输入（含 `data/monthly/`）和 `PROVENANCE.md`。
- `tests/`：真实业务值、重复运行、重要边界、并发与失败行为测试。

当前范围：只分析合成离线导出，无真实账号/API/支付操作，无跨币种相加。全量模式为整批重算；月度模式提供不可变连续月份、跨月退款本金账与确定性历史报告，不含模糊匹配或账务调整。

全部示例为原创合成数据，不代表真实客户、监管标准、实际测量或线上历史。项目没有真实账号、密钥或付费依赖；不包含第三方原始数据。代码及自制数据采用 MIT；依赖保留自身许可。
