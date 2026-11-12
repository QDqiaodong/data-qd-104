# data-qd-104 — Settlement Audit / 支付结算对账

把虚构支付平台全量流水与独立银行结算表对齐，保留每个结算币种的差异原因。

已实现：支付/退款/手续费规范化、币种小数精度、同 ID 等价去重与冲突拒绝、退款原支付约束、结算/币种汇总、精确差额和缺失分类、SQLite 可查询证据、差异 CSV 与完整 JSON。

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

## 结构与业务契约

- `settlement_audit/money.py：货币精度和有符号最小单位`
- `settlement_audit/input.py：去重与退款引用完整性`
- `settlement_audit/reconcile.py：按币种净额与差异归因`
- `settlement_audit/pipeline.py：SQLite、JSON 和差异 CSV`
- `docs/contract.md`：完整字段/格式、业务键、边界、输出含义与当前约束。
- `data/`：小型确定性合成输入和 `PROVENANCE.md`。
- `tests/`：真实业务值、重复运行、重要边界和失败行为测试。

当前范围：只分析合成离线导出，无真实账号/API/支付操作，无跨币种相加。全量重算，尚无跨期退款账、模糊匹配或账务调整。

全部示例为原创合成数据，不代表真实客户、监管标准、实际测量或线上历史。项目没有真实账号、密钥或付费依赖；不包含第三方原始数据。代码及自制数据采用 MIT；依赖保留自身许可。
