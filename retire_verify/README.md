# 离线退役核验工具

数据仓库退役审批的离线核验：针对退役申请清单中的已知对象，结合历史登记、
只读目录采集结果和采集配置声明的 schema 范围，判断**当前证据是否足以支持
退役**，或必须**暂缓并说明缺少哪项证据**。本工具只整理数据，不执行任何
删除或权限变更，也不读取表内业务数据。

## 核心规则

**对象身份** = 实例标识 + 实例代际 + 对象类型 + 实例内编号 + 创建凭据，
五项全等才是同一对象。schema 与名称只描述当前位置：改名、迁移不等于消失；
同名同编号但凭据不同，不是原对象。实例重建必须使用新代际；编号可在删除后
复用；创建凭据在一个代际内不复用。来源无法提供创建凭据的对象照常保存观察，
但绝不仅凭名称或编号认作申请对象。

**采集状态**按 (schema, 对象类型) 记录：

| 状态 | 含义 |
|---|---|
| `complete` | 连接器已核实该范围列举完整，缺席=不存在 |
| `restricted` | 只读到部分（如账号被收紧），缺席不能当不存在；但读到的对象仍是有效存在证据 |
| `failed` | 读取失败，缺席不能当不存在 |

标为 `complete` 的清单若被截断、身份重复或记录互相矛盾（同身份两个位置、
同编号两个凭据），**整次采集被拒绝**，不得为退役背书。

**结论三态**（对每份申请 × 每次采集 × 每版配置）：

1. **仍存在**：本次采集任何范围中找到完全相同身份。给出当前名称和 schema；
   名称或 schema 变化时如实标注改名/迁移。
2. **证据支持退役**：身份未出现，且**原登记范围 ∪ 配置声明的全部可能迁移
   范围**均已完整采集、身份均缺席。结论逐项展开所有排除迁移可能的范围及
   其观察编号。
3. **证据不足（暂缓）**：存在未采集/受限/失败范围。结论逐项列出阻断项及
   缺少的证据。

附加规则：

- 对象仍在而列清单受限/失败时，只展示已看到的列，不得声称列已删除；列采集
  失败绝不当成对象消失。列清单完整时按稳定列号与上一次完整清单比对差异。
- schema 范围增减（配置版本变化）、实例代际切换都会在结论备注中显式标注，
  避免把配置边界变化当成仓库变化。
- 原身份在“支持退役”之后重新出现时，历史核验原样保留；新核验按同一对象
  持续存在处理，**不虚构一次先删后建**。
- 核验结果只增不改：同一（申请, 采集, 配置版本）重复核验读取历史结果；
  新采集或新配置版本生成新核验，旧结果不被改写。

## 安装与运行

无第三方依赖，Python 3.10+ 即可。

```bash
cd retire_verify
python3 demo.py          # 手工样例：一份申请从提交到复核的完整生命周期
python3 test_suite.py    # 17 项边界情形测试
python3 perf.py          # 十万级规模验证（查询计划 + 峰值内存）
```

## 命令行

```bash
DB=run/my.db
python3 cli.py init $DB
python3 cli.py import-registry     $DB samples/registry.csv
python3 cli.py import-applications $DB samples/applications.json
python3 cli.py import-config       $DB samples/config_v1.json
python3 cli.py import-collection   $DB samples/col1_a_complete_b_restricted.json
python3 cli.py verify  $DB --app APP-001 --collection col-1 [--config cfg-1]
python3 cli.py verify-all $DB --collection col-1
python3 cli.py show    $DB --app APP-001          # 单份申请的全部核验依据
python3 cli.py check   $DB --app APP-001 --collection col-1   # 独立核对器复核
python3 cli.py list-collections $DB
```

Python 接口：

```python
from rv import db, ingest, verify, checker
conn = db.init_db("run/my.db")
ingest.import_registry(conn, "samples/registry.csv")
ingest.import_applications(conn, "samples/applications.json")
ingest.import_config(conn, "samples/config_v1.json")
ingest.import_collection(conn, "samples/col1_a_complete_b_restricted.json")
result = verify.verify_application(conn, "APP-001", "col-1")
print(verify.format_result(result))
print(checker.format_check(checker.check_application(conn, "APP-001", "col-1")))
```

核验结果字段：`object`（申请对象身份与原位置）、`conclusion`、
`current_location`（若有）、`renamed`/`migrated`、`scopes`（每个相关范围的
采集状态与观察编号）、`blockers`（暂缓时缺少的证据）、`observations`
（存在性观察编号）、`columns`（列信息）、`notes`（代际/配置边界/替身/
重新出现等提示）、`reason`（得出结论的原因）。

## 手工样例（samples/，demo.py 自动演练）

表 `orders` 登记在 schema A（身份 `inst1|1|TABLE|101|C-1`），申请 APP-001
退役，配置声明 TABLE 可能在 A、B：

1. **col-1**：A 完整、B 受限且未见该表 → **暂缓**，阻断项 B/TABLE 受限。
2. **col-2**：在 B 找到同一身份，已改名 `orders_v2` → **仍存在**，标注
   迁移 A→B 与改名；同名不同身份不算原对象。
3. **col-3**：A、B 都完整且身份缺席 → **证据支持退役**，逐项列出 A、B
   两个已排除迁移可能的范围及观察编号。
4. 重复核验 col-3 → 读取历史结果（同一核验编号），不重新计算。
5. **col-4**：出现同编号 101、同旧名 `orders` 但凭据 `C-9` 的对象 →
   替身被显式排除，**仍支持退役**。
6. 配置升级为 cfg-2（新增 schema C）后复核 col-4 → **暂缓**（C 未采集），
   并提示这是配置边界变化而非仓库变化。
7. **col-5**：实例重建为第 2 代，A/B/C 完整 → **支持退役**，标注代际
   切换 g1→g2。

每一步都由独立核对器（`rv/checker.py`，不调用正式判定函数）重算并比对。

## 测试（test_suite.py，17 项）

受限范围正向发现、范围读取失败、清单截断拒绝、身份重复拒绝、记录矛盾
拒绝、无创建凭据对象、列采集失败/受限/完整差异比对、同编号不同实例、
支持退役后重新出现、同编号同旧名不同凭据替身、受限转完整、配置范围扩大、
实例代际切换、重复核验幂等、独立核对器三态一致、改名/迁移区分、
范围未采集阻断。

## 规模验证（perf.py）

十万条登记、6 次采集共 12 万条观察（范围状态 complete/restricted/failed
混合轮转）、500 份申请在**非相邻时点**（第 2、5 次采集）各核验一次。
实测（本机）：平均约 1.4 ms/次，tracemalloc 峰值约 0.1 MB；全部核验查询
的 `EXPLAIN QUERY PLAN` 均为索引 SEARCH（`sightings(collection_id,
object_key)`、`sightings(collection_id, instance, generation, obj_type,
obj_number)`、`collection_scopes` 主键、`verifications` 唯一索引、
`collection_generations` 主键），不存在任何全表 SCAN——不会为单项申请
扫描全部历史对象。

## 文件说明

```
rv/db.py       表结构、连接、身份键
rv/ingest.py   登记/申请/配置/采集导入，采集完整性校验（拒绝规则）
rv/verify.py   核验引擎（三态结论、列信息、备注、只增不改落库）
rv/checker.py  独立核对器（不调用正式判定函数）
cli.py         命令行入口
demo.py        手工样例演练
test_suite.py  边界情形测试
perf.py        规模验证
samples/       可重复运行的样例数据
```
