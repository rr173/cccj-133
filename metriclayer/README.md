# metriclayer — 带类型、路径显式、精确算术的指标查询层（Python + SQLite）

分析员只声明**实体关系、事实表、指标和查询维度**，本程序把声明编译成符合各自
口径的查询并在本地 SQLite 上执行，同时输出一份说明每个指标**按什么粒度、沿什么
角色路径、受哪些筛选、忽略哪些筛选、用什么中间粒度**计算的查询计划。

它解决的核心问题是：**两张事实明细表直接连接后求和会同时放大两边金额**。本层
保证每个基础指标只读自己那一张事实表、保留事实本身的行重数，事实之间永不连接；
存在筛选（existence filter）编译成独立的**半连接子查询**，只选出实体群，再让该
实体群沿各指标自己的关系路径施加约束。

---

## 1. 目录结构

```
metriclayer/
├── pyproject.toml                  # 包声明（无第三方依赖，标准库即可运行）
├── mlcli.py                        # -I 隔离模式下的命令行入口
├── metriclayer/
│   ├── __init__.py                 # 公共 API 导出
│   ├── errors.py                   # ModelError / DataError / CompileError
│   ├── model.py                    # 模型解析与校验（类型、主键、引用图、白名单）
│   ├── compile.py                  # 查询编译器：路径解析、筛选作用域、SQL 生成
│   ├── engine.py                   # 数据校验入库 + 只读快照执行 + 精确聚合
│   ├── api.py                      # 稳定公共 API
│   ├── cli.py                      # build / validate / plan / query 子命令
│   └── __main__.py
├── examples/
│   ├── model.json                  # 建模文件（实体、事实、引用、指标、维度）
│   ├── data.json                   # 样例数据
│   ├── queries/                    # 15 个查询示例
│   └── plans/                      # 生成的查询计划示例（JSON）
└── tests/
    ├── test_metriclayer.py         # 38 个标准库 unittest 用例
    └── independent_enumerator.py   # 独立枚举器：不执行产品 SQL，逐事实逐格核对
```

---

## 2. 安装步骤

无第三方运行依赖，只需要 Python 3.10+（开发验证环境为 3.11）。

### 方式 A：直接用源码 + 隔离解释器（推荐，无需安装）

所有命令都用 `python3 -I` 运行。`-I` 忽略 `PYTHONPATH`、当前目录与用户站点
定制，避免不可信数据目录里被植入同名模块（如 `json.py`）。`mlcli.py` 会在
解释器启动后显式加入包目录。

```bash
cd metriclayer
python3 -I mlcli.py --help
```

### 方式 B：安装为包（环境允许时）

```bash
python3 -m pip install .            # 或 pip install -e . 做可编辑安装
metriclayer --help                  # 控制台脚本
python3 -m metriclayer --help       # 模块入口
```

### 从零构建样例数据库

```bash
python3 -I mlcli.py validate \
    --model examples/model.json --data examples/data.json
python3 -I mlcli.py build \
    --model examples/model.json --data examples/data.json --db /tmp/metrics.db
```

`build` 会先做全部数据校验，再（重新）创建 SQLite 文件，并打印每张表的行数。

---

## 3. 查询命令

```bash
# 只编译、不碰数据库，输出完整查询计划
python3 -I mlcli.py plan \
    --model examples/model.json \
    --query examples/queries/q2_existence_filter.json

# 执行查询，文本表格输出
python3 -I mlcli.py query \
    --model examples/model.json --db /tmp/metrics.db \
    --query examples/queries/q1_customer_totals.json

# 精确 JSON 输出（大整数为十进制字符串，比率为约分分子/分母）
python3 -I mlcli.py query --json \
    --model examples/model.json --db /tmp/metrics.db \
    --query examples/queries/q1_customer_totals.json
```

### 公共 Python API

```python
from metriclayer import load_model, build_database, run_query, explain_query

model = load_model("examples/model.json")
build_database(model, "examples/data.json", "/tmp/metrics.db")

# 只看计划（编译期，不需要数据库）
plan = explain_query(model, "examples/queries/q2_existence_filter.json")
print(plan.to_json())

# 执行
result = run_query(model, "/tmp/metrics.db",
                   "examples/queries/q1_customer_totals.json")
for row in result.rows:
    print(row)
# 程序化断言：维度元组 -> 指标值（缺失成员用 compile.MISSING 哨兵）
cell = result.metric_map()[("A",)]
assert cell["shipment_amount"] == {"type": "int", "value": "20"}
assert cell["refund_amount"]    == {"type": "int", "value": "3"}
```

---

## 4. 样例数据与题面场景

`examples/data.json` 建立了题面要求的客户 A / B 主场景，并额外加入 D / E 覆盖
角色、可空引用、双存在筛选等情形：

| 客户 | 发货 | 退款 |
| --- | --- | --- |
| A | 两笔各 10（另加一笔金额为 null 的发货） | 三笔各 1 |
| B | 一笔 8，无退款 | — |
| D | 两笔金额全为 null 的发货（覆盖全 null 金额） | 一笔 damaged(1)、一笔 0 元 goodwill、一笔 3 元无原因（reason_code 为 null） |
| E | 无发货 | 一笔 2 元，区域 east |

另有 `adjustments` 事实表保存 **128 位大整数**（`170141183460469231731687303715884105727`，
即 2^127−1），以及 B 的一笔金额全 null 调整。

### 按客户查询（q1）

```
customer  shipment_count  shipment_amount  refund_count  refund_amount  ...
A         3               20               3             3              ...
B         1               8                0             null           ...
```

- A 发货 **20**、退款 **3**；B 发货 **8**、退款和为 **null**、退款行数 **0**。
- 金额相同的两笔发货仍是两笔事实：`shipment_count=3`、`shipment_amount=20`，
  而不是被 `SUM(DISTINCT amount)` 压成 10（见 q12）。

### 存在筛选不放大（q2）

加入“存在正退款的客户”这一存在筛选后：

```
shipment_count  shipment_amount  refund_amount  all_shipment_amount
3               20               5              28
```

- 受筛选的发货总额是 **20，不是 60**：筛选事实 `refunds` 只出现在半连接子查询
  里，**从不与 `shipments` 连接**，因此不会因为 A 有 3 笔退款而把发货乘 3。
- 指标 `all_shipment_amount` 在模型里声明 `ignore_filters` 忽略该存在筛选，
  返回**全体**发货额 **28**（= 20 + 8；D 的发货金额全为 null，不影响求和）。

### 格子并集与缺失成员（q3）

区域 `south` 只在退款中出现，仍生成独立格子；可空区域为 null 的事实进入一个
结构化缺失标记格，输出为 `{"__missing__": true}`，与普通字符串不会混淆：

```
region     shipment_count  shipment_amount  refund_count  refund_amount
east       0               null             1             2
north      2               20               2             2
south      0               null             1             1
∅ MISSING  4               8                3             4
```

只生成至少有一项指标实际贡献事实的格子；某指标在某格无事实时，count 补 0、
sum 补 null。

---

## 5. 关键设计与口径保证

### 5.1 严格类型：int / text / bool，各自可空

- 模型列类型只能是 `int`、`text`、`bool`，每列显式 `nullable`。
- **int**：数据接受 JSON 整数或**规范十进制 TEXT**（无多余前导零、可选符号），
  拒绝 `10.0`、`"abc"` 等混入文本；参与比较/连接前按整数值解释。
- **bool**：只接受 JSON `true/false`，**拒绝 0/1 整数**；bool 与 int 是不同
  类型，bool 列禁止与整数字面量比较（建模期即报错）。
- 入库后 int 列使用 **BLOB 亲和性**保存规范十进制文本，SQLite 不会把大整数
  强转成丢失精度的 REAL；bool 列用 INTEGER 亲和性只存 0/1；text 列用 TEXT。
- int 的**大小比较**通过注册的 `int_cmp` UDF 按整数值比较（见 q13），不依赖
  文本字典序；等值 `IN`/`=` 基于同一规范表示。

### 5.2 主键与引用：多对一、无环、角色命名

- 每张表声明**非空唯一主键**（单列）；引用必须指向目标表主键，且本地外键与
  目标主键类型一致。
- 实体间及事实到实体的引用是**多对一**；禁止事实指向事实。引用图做 DAG 着色
  校验，存在环直接拒绝建模。
- 同一目标存在多条路径时必须为引用命名 `role`，维度和指标显式选择角色；
  未显式给出路径时，仅当唯一路径可确定才自动解析，**歧义或不可达一律在执行
  前拒绝，绝不猜路径**（见 q8 与 `test_ambiguous_path_must_be_named`）。
- 数据校验：主键唯一性、引用目标唯一性（目标即主键）、非 null 悬空引用全部
  报错；**可空外键的 null 作为缺失成员保留**，LEFT JOIN 后落到缺失标记格。

### 5.3 列白名单，禁止任意 SQL 片段

谓词、维度、指标只能引用模型白名单内的列，且在建模/编译期逐表校验。SQL 中
所有标识符（表名、列名、角色别名）都来自模型声明并加双引号；字面量全部以
绑定参数传入，不存在拼接用户 SQL 文本的通道。

### 5.4 指标口径

- **基础指标**各自只来自一张事实表：`count_rows`（行数）、`count`（非 null
  计数）、`sum`（仅 int 列精确求和）、`count_distinct`（精确不同值数，按当前
  完整维度重新计算，绝不把更细格的去重数相加）。
- **派生指标**由基础量做 `add/sub/mul/div`，用 `fractions.Fraction` 精确计算，
  非整数结果输出为**约分分子/分母**，全程不使用 float。
- 空值规则：`sum` 没有非 null 值时为 **null**；`count` 空集为 **0**；除法
  除数为 0 或 null 时结果为 **null**；任意运算数为 null 时结果为 null。
- 大整数（超过 64 位）以十进制字符串输出，例如
  `340282366920938463463374607431768211454`（见 q6）。
- 每个指标可有自己的**行谓词**，并可用 `ignore_filters` 按名称声明忽略指定
  查询筛选（第一版不引入权限系统）。

### 5.5 两类查询筛选与三值逻辑

- **实体属性筛选（entity）**：沿明确角色约束相关事实，编译成该角色链上的
  LEFT JOIN 谓词。
- **存在筛选（existence）**：明确一个实体和一条“事实 → 实体”的角色路径，
  含义是“只选在该事实中至少有一行符合条件的实体”。编译器生成
  `SELECT DISTINCT <实体主键> FROM <筛选事实> ...` 的半连接子查询（路径可跨
  多跳，逐跳 LEFT JOIN 到目标实体），再让各指标沿**自己的**角色路径用
  `<本地实体>.<主键> IN (子查询)` 约束。筛选事实与指标事实之间没有任何连接，
  因此**不可能制造重数**。当某个指标事实到筛选实体存在多条路径时，查询须在
  筛选的 `local_paths` 里按事实表显式命名角色链（见 q15）；缺省时只接受唯一
  路径，歧义或不可达在执行前拒绝。
- 多个存在筛选分别独立判定是否存在，**不要求同一事实行同时满足**；若要求
  同一行满足，必须在一个筛选的 `and` 内表达（q4 对比 q5）。
- 谓词支持 `eq / lt / lte / gt / gte / is_null / in / and / or`。普通比较
  遇 null 为 unknown，只有 true 通过（`and/or` 严格实现 SQL 三值逻辑）。

### 5.6 行重数、格对齐与单一读取视图

- SQL 只做关系工作（角色连接、白名单谓词、存在半连接），**不产生任何
  `SUM/COUNT/GROUP BY`**；引擎取回的是带维度标记的事实原始行。
- 先在各事实内部保留行重数做精确聚合，再按查询维度对齐取**格并集**；派生
  比率在对齐后的基础量上计算。
- 一次查询固定**同一个只读连接、同一读事务快照**（`file:...?mode=ro` +
  `BEGIN`），所有按事实生成的 SQL 都跑在同一数据库读取视图上。

---

## 6. 查询计划说明（plan）

`plan` 子命令对每个基础指标输出：

- `fact` / `kind` / `measure`：来源事实表、聚合种类、度量列与粒度；
- `dimension_paths`：每个维度沿哪些角色到达实体、链上哪些引用可空；
- `applied_filters`：实际生效的筛选及其作用域（实体属性连接 / 存在半连接）；
- `ignored_filters`：该指标按名称忽略的筛选；
- `joins`：去重后的多对一 LEFT JOIN 边（不存在事实到事实的连接）；
- `intermediate_grain`：中间粒度——事实原始行对齐到查询维度；
- `sql` / `params`：最终执行的参数化 SQL 与绑定参数；
- 顶层还有 `read_view`（单一读取视图说明）与 `derived_plans`（派生表达式、
  涉及的基础指标、有效忽略项）。

生成好的示例见 `examples/plans/`。

---

## 7. 测试说明

### 标准库单元 / 集成测试（38 个）

```bash
python3 -I tests/test_metriclayer.py
```

覆盖：题面主场景、连接不放大、同值多事实、精确分数与 null 传播、除零为 null、
大整数精确求和、全 null 金额、格并集与缺失标记、双存在筛选不同行满足、同行
AND 为空、不可达维度执行前拒绝、命名角色路径、bool 谓词、主键/悬空引用/类型
违规的数据校验、引用图成环、白名单列、歧义路径、事实连事实拒绝、读取视图等。

### 独立枚举器（不执行产品生成的 SQL）

`tests/independent_enumerator.py` **不导入产品包的任何执行逻辑、不碰 SQLite**，
仅用标准库直接读取 JSON 模型与数据，独立重新实现类型解析、角色链行走、存在
筛选的逐实体 `any(...)` 判定、逐事实逐格枚举（行数 / 非 null 计数 / 精确求和 /
精确不同值集合）、格并集、count=0 与 sum=null 补齐以及 Fraction 派生计算，然后
把每个查询的期望结果与产品（经公共 API，使用 SQLite）的结果**逐格、逐值**比对：

```bash
# 先构建产品数据库，再让枚举器逐格核对（含“应拒绝”查询的双向核对）
python3 -I mlcli.py build \
    --model examples/model.json --data examples/data.json --db /tmp/metrics.db
python3 -I tests/independent_enumerator.py \
    --model examples/model.json --data examples/data.json \
    --queries examples/queries --db /tmp/metrics.db
```

预期结尾：`15 queries cross-checked cell-by-cell; 0 failure(s)`，其中 q8 在
枚举器与产品两侧都被拒绝。枚举器也支持不传 `--db` 单独运行（只枚举、不比对）。

---

## 8. 查询文件一览

| 查询 | 说明 |
| --- | --- |
| q1_customer_totals | 按客户的发货/退款主场景 + 派生指标 |
| q2_existence_filter | 正退款客户存在筛选：20（非 60）与忽略筛选的 28 |
| q3_region_union | 仅退款出现的区域、缺失标记、格并集 |
| q4_two_existence | 两个存在筛选由不同事实行分别满足（D） |
| q5_same_row_and | 同一行 AND 同时满足两个条件 → 空结果 |
| q6_bigint | 128 位大整数精确求和、全 null 金额 |
| q7_role_path | 显式命名角色路径（customer.home_region） |
| q8_unreachable | 不可达维度，执行前拒绝 |
| q9_entity_filter | 实体属性 + bool 谓词 |
| q10_predicate_gallery | lt / is_null / in 多存在筛选组合 |
| q11_or_nested | OR 与嵌套 AND（同行）语义 |
| q12_distinct | 不同值数按完整维度重算，同值多事实不被掩盖 |
| q13_bigint_ordering | 大整数经 int_cmp UDF 精确比较 |
| q14_text_distinct | 可空 text 列不同值计数（null 不计入） |
| q15_multi_hop_existence | 多跳存在筛选，local_paths 显式命名本地角色路径 |
