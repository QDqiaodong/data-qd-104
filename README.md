# data-qd-104 — Settlement Audit / 支付结算对账

把虚构支付平台全量流水与独立银行结算表对齐，保留每个结算币种的差异原因。

已实现：支付/退款/手续费规范化、币种小数精度、同 ID 等价去重与冲突拒绝、退款原支付约束、结算/币种汇总、精确差额和缺失分类、SQLite 可查询证据、差异 CSV 与完整 JSON。

新增：连续月度不可变导入与跨月退款核验（独立入口与独立 SQLite 结构，见 `docs/monthly_contract.md`）。每个导出月份作为不可变事实接收，退款可引用历史月份或同月支付，累计退款不得超过本金；历史月报在后续月份导入后保持字节不变。

新增：独立回单认领区间分析（`python -m settlement_audit.claims`，见 `docs/claims_contract.md`）。银行回单缺少结算编号时，按文件顺序认领会挤占别组唯一可用资金；本分析只读一个已接收月份，按币种把回单供给、组需求和允许关系建成整数最大流网络，在**所有总额最大的方案**上给出每条关系与每个组的认领最小/最大值、达到端点的完整分配见证和最小割证据。它不改月度库、不改原对账结论，并明确说明各自最大值不能拼成同时成立的归属。

新增：离线 MT940 分页对账单采集（`python -m settlement_audit.mt940`，见 `docs/mt940_contract.md`）。银行按页交来的对账单上，余额是结转核对数而非结算额；本入口严格校验标签顺序、20xx 日期、币种精度与每页/跨页平衡，按账户/币种/账单组装乱序多页，用显式 JSON 映射到结算 ID，汇总有符号交易净额生成旧入口可读的 `bank.json`，并同时原子发布逐页/逐笔审计 JSON，整批失败不留半套。

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

### 回单认领区间示例

先用真实月度入口接收 `data/claims/2026-04_*` 合成月份，再运行独立只读分析：

```sh
.venv/bin/python -m settlement_audit.monthly ingest --period 2026-04 \
  --ledger data/claims/2026-04_ledger.csv --bank data/claims/2026-04_bank.json \
  --db run/claims/monthly.sqlite
.venv/bin/python -m settlement_audit.claims --period 2026-04 \
  --db run/claims/monthly.sqlite \
  --receipts data/claims/receipts.csv --relations data/claims/relations.csv \
  --output run/claims/claims.json
```

该示例含 USD/JPY/EUR 三个币种、8 个正需求组、3 个因零/负/缺金额被排除的组、
11 张回单与 15 条允许关系（其中 `R4 -> SM` 因组无正需求被排除）。USD 最大可认领
20400 分，`SA`/`SB` 为固定满额、`SD`/`SE` 可在 1000–3000 分间摆动，`R11 -> SI`
虽合法但在所有最优方案中均为 0；最大总量由 `SA`/`SB` 需求弧与若干回单供给弧的
最小割证明。`range_semantics` 与 `witnesses` 保证每个端点都来自一份完整分配，
不同端点的最大值不可拼接为同一归属。

内置合成场景：一月 p1 收款 100 USD、手续费 3 USD，银行 97 USD；二月两笔引用 p1 的退款 30+20 USD 与 1 USD 手续费，银行 -51 USD（两月同一 `settlement_id` 各自平衡）；三月再退 50 USD 后 p1 剩余本金为 0。二月历史报告始终显示累计已退 5000、剩余 5000 分，即使三月之后重放二月也字节不变。Python 调用为 `settlement_audit.monthly.ingest(...)` 与 `settlement_audit.monthly.report(...)`。月份归属、键作用域、累计退款、重放、历史余额与故障重跑规则见 `docs/monthly_contract.md`。

## 离线 MT940 分页对账单采集（严格子集）

银行交来的对账单按页拆开，页上的余额只是结转核对数，不能当作结算额。新增独立离线入口 `python -m settlement_audit.mt940`（仅标准库，见 `docs/mt940_contract.md`）：解析严格 MT940 子集（`:25:` 账户、`:28C:` 账单号/页号、`:60F/M:` 期初、`:61:` 逐笔借贷、可跨行 `:86:` 附言、`:62M/F:` 期末），拒绝未知标签、坏顺序和超精度；金额全部按现有币种精度做整数最小单位运算，不经 float，`C/RD` 为正、`D/RC` 为负，余额按 `C/D` 带符号。乱序、多账户、多账单、多文件按账户/币种/账单组装：页号从 1 连续、首尾 F 中间 M、相邻页日期与余额衔接、每页期初加有符号交易和等于期末；重复页即使等价也拒绝，缺页或失衡整批失败。账户/币种到结算 ID 用严格 JSON 显式映射，缺映射或多余映射都拒绝；按结算/币种汇总**交易净额**（零净额保留）生成旧入口可直接读取的 `bank.json`，并同时交付逐页/逐笔 `mt940_audit.json`（原文摘要、行号、附言、页内与跨页核对及汇总依据）。两文件在新目录中一次性原子发布，失败不留半套。

```sh
python -m settlement_audit.mt940 collect \
  --mt940 data/mt940/capture_part1.mt940 data/mt940/capture_part2.mt940 \
  --mapping data/mt940/account_mapping.json --output run/mt940_demo
```

随仓合成例覆盖手工可算的借贷与冲正（C/D/RD/RC）、跨页衔接、乱序、零净额与多币种；产出的 `bank.json` 可直接喂给原有全量入口，旧行为与旧输出保持不变。

## 全量对账回归防线与离线变异验证

`tests/test_full_reconciliation_regression.py` 用固定种子生成器与独立预期模型（纯标准库，不调用被测解析/去重/对账函数生成答案）经真实全量 CLI 逐键核对 JSON、差异 CSV 与 SQLite 四表；覆盖三币种、跨结算退款、等价重复、零净额与单侧缺失，并对乱序、等价金额重写、重复插入区分业务不变量与来源摘要、重复计数的合理变化；非法退款与重复银行键失败时旧产物字节不变，失败自动保存输入、种子与重放命令。`tools/mutation_check.py` 在临时源码副本上逐个植入 6 个语义变异（符号、币种隔离、去重、缺失、差额、退款边界）并验证新测试逐个捕获；语法/导入失败与超时不算捕获。运行不改动生产代码与现有测试（哈希指纹随报告留存）。

```sh
python3 -m unittest discover -s tests                 # 基线（含新回归模块）
python3 tools/mutation_check.py                       # 基线 + 全部变异，JSON 报告写入 run/mutation/results.json
```

详见 `docs/regression.md`；最近一次实际执行记录在 `regression/evidence/`。

## 结构与业务契约

- `settlement_audit/money.py：货币精度和有符号最小单位`
- `settlement_audit/input.py：去重与退款引用完整性`
- `settlement_audit/reconcile.py：按币种净额与差异归因`
- `settlement_audit/pipeline.py：SQLite、JSON 和差异 CSV`
- `settlement_audit/monthly.py：不可变月度导入、跨月退款预算、历史月报与月度 CLI`
- `settlement_audit/flow.py：整数最大流与跨全部最大流的弧流量区间`
- `settlement_audit/claims.py：只读回单认领区间分析、见证与最小割证据`
- `settlement_audit/mt940.py：离线严格 MT940 分页采集、组装校验、显式映射、legacy bank.json 与逐页审计 JSON 原子发布`
- `docs/contract.md`：全量模式完整字段/格式、业务键、边界、输出含义与当前约束。
- `docs/monthly_contract.md`：月度模式月份序列、表结构粒度、重放、事务边界与 v2 报告结构。
- `docs/claims_contract.md`：认领分析的两份 CSV 格式、排除原因、最大流模型、区间语义、见证与最小割、原子替换。
- `docs/mt940_contract.md`：MT940 严格子集标签与顺序、20xx 日期与币种精度、分页组装不变量、映射严格性、两份输出与原子发布。
- `data/`：小型确定性合成输入（含 `data/monthly/`、`data/mt940/`）和 `PROVENANCE.md`。
- `tests/`：真实业务值、重复运行、重要边界、并发与失败行为测试。

当前范围：只分析合成离线导出，无真实账号/API/支付操作，无跨币种相加。全量模式为整批重算；月度模式提供不可变连续月份、跨月退款本金账与确定性历史报告；认领分析只读一个已接收月份并输出区间分析，不含模糊匹配或账务调整。

全部示例为原创合成数据，不代表真实客户、监管标准、实际测量或线上历史。项目没有真实账号、密钥或付费依赖；不包含第三方原始数据。代码及自制数据采用 MIT；依赖保留自身许可。
