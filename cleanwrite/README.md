# 清洗视图回写（cleanwrite）

把「客户 × 地址」平面清洗视图里的显式修改，一致地反向传播到两张源表：
`C(cid, name, internal_note)` 与 `A(aid, cid, label, address, secret_tag)`。

清洗人员只看、只改导出的平面视图；回写时提交的是**明确的动作**（改名、改
标签、改地址、迁移、删除、新增），不是把整行平面数据覆盖回库。这样：

* 同一客户在视图里重复出现的多行，不会把一次改名重复覆盖多次；
* 视图没有展示的 `internal_note` / `secret_tag` 不会被回写清空；
* 旧导出不会和新导出的编辑被悄悄合并。

本模块独立于仓库里已有的 `ctmirror/`、`metriclayer/`、`retire_verify/`，只新
增目录，不修改旧功能。

## 1. 目录与运行

```
cleanwrite/
├── cwcli.py                  # -I 隔离解释器下的命令行入口
├── drill.py                  # 主演练：get → explain → put → get，含参考模型核对
├── cleanwrite/
│   ├── codec.py              # 规范文本单元格编码（可逆、唯一、严格解码）
│   ├── store.py              # SQLite 表、严格样例导入、revision、aid 分配
│   ├── tokens.py             # snapshot_token（HMAC）、行引用、视图定义
│   ├── view.py               # 一致快照读取 + LEFT JOIN 导出
│   ├── actions.py            # 动作格式与严格解析
│   ├── derive.py             # 两种合并规则的推导与原子应用
│   ├── api.py                # 公共 API：init/import/get/explain/put
│   ├── cli.py                # init/import/config/get/explain/put/source
│   ├── errors.py
│   ├── __init__.py / __main__.py
├── samples/drill.json        # 演练样本（C1 两地址、C2 无地址）
├── tools/make_actions.py      # 只读导出文件、生成动作 JSON 的小助手
├── tests/
│   ├── test_cleanwrite.py    # 72 个标准库 unittest
│   └── reference_model.py    # 不导入产品包、不用 SQLite 的独立关系模型
└── work/                     # drill 生成的数据库
```

无第三方依赖，Python 3.10+（开发环境 3.11）。所有命令都在 `cleanwrite/`
目录下用 `python3 -I` 运行（忽略 `PYTHONPATH`、当前目录与用户 site，避免不
可信数据目录里被植入同名模块）：

```bash
cd cleanwrite
python3 -I cwcli.py --help
```

## 2. 源关系、主键与外键

| 表 | 列 | 约束 |
|---|---|---|
| C | `cid`（主键） | 非空字符串，原样比较 |
|   | `name` | 非空字符串 |
|   | `internal_note` | 文本或 NULL，**隐藏** |
| A | `aid`（主键） | 非空字符串，原样比较 |
|   | `cid` | 非空字符串，引用 `C.cid`，可重复引用 |
|   | `label` | 非空字符串，同一客户内唯一 |
|   | `address` | 文本或 NULL |
|   | `secret_tag` | 文本或 NULL，**隐藏** |

「非空字符串」是 SQL 的非 NULL 约束，**空串是合法值**（因为要求无损往返）。
身份、名字、标签都不 trim、不改大小写。初始化与样例导入会拒绝：

* 重复 `cid` / `aid`；空 `cid` / `aid`；
* 地址引用不存在的客户；
* 同一客户下重复 `label`；
* 任何类型错误（数字、布尔、对象、含孤立代理项的字符串）；
* JSON 对象里重复的键。

```bash
python3 -I cwcli.py init --db work/demo.db --sample samples/drill.json
python3 -I cwcli.py import --db work/demo.db --sample more.json   # 覆盖并 +1 revision
python3 -I cwcli.py import --db work/demo.db --sample more.json --merge
```

一个新建库的 `revision = 0`；`init --sample` 是初始状态，不算修改。`import`
和 `config` 都让 revision 加一。

## 3. 导出（get）

```bash
python3 -I cwcli.py get --db work/demo.db --out work/view.json
```

一次 `get` 在一个写事务（`BEGIN IMMEDIATE`）里读取同一版本的 C、A，然后写入
导出记录。导出文档：

```json
{
  "snapshot_token": "cw1.eyJ...<base64url>.<hmac>",
  "revision": 0,
  "view": { "...": "本导出绑定的视图定义（只读说明）" },
  "rows": [
    {"row_kind": "customer_address",
     "row_ref": "rr_<digest>_<base64url>",
     "customer_name": "s\"甲\"", "address_label": "s\"家\"",
     "address_address": "s\"中山路1号\"",
     "cid_display": "C1", "aid_display": "a1"},
    {"row_kind": "placeholder_no_address", "row_ref": "rr_...",
     "customer_name": "s\"乙\"", "address_label": "",
     "address_address": "", "cid_display": "C2", "aid_display": ""}
  ]
}
```

* 视图定义是 `C LEFT JOIN A ON A.cid = C.cid`，按 `cid`、再按 `aid` 的
  **Unicode 码点序**（Python `str` 序）排序，且是全库统一排序。
* 有地址的客户每个地址一行，**不再**额外导出占位行；没有任何地址的客户恰
  好占一行，`row_kind = "placeholder_no_address"`、`aid_display = ""`、标签
  与地址列为空串、`row_ref` 是独立的一行引用。
* `address = NULL` 的真实地址是 `row_kind = "customer_address"`，地址列是
  规范单元格 `null`，并有真实 `aid_display`，与占位行完全不可混淆。
* `internal_note` 与 `secret_tag` 不出现在导出文档里，也不在
  `snapshot_token` 里（令牌只带**单向**的状态摘要）；本地库按需保存回写所
  需的来源记录（`cw_exports`）。
* `cid_display` / `aid_display` 只是导出期身份的展示列；动作输入从不把它们
  当作可信身份，身份只来自 `row_ref`（见下）。

文本 / NULL 的规范单元格见第 6 节。

## 4. 令牌与行引用

`snapshot_token` = `cw1.<base64url(payload)>.<HMAC-SHA256>`。payload 绑定：

1. 本地库随机 `db_identity`（换库即失配）；
2. `revision` 与整个源状态（含隐藏字段）的单向 `state_hash`；
3. 当前配置的单向 `config_hash`；
4. 完整视图定义的 `view_hash`：视图名、版本、两张源表、连接、排序、列清
   单、编码规则、占位种类、新增默认值键；
5. 本次导出全部行引用的列表。

HMAC 密钥是库内随机 32 字节，只在本地保存。改动令牌任何一段都会失配。令牌
不含隐藏值，也不含隐藏值的可逆编码。

行引用 `row_ref` 形如 `rr_<keyed digest>_<base64url>`，绑定「本次导出的
export_id + 导出时的源身份（客户 cid，以及真实地址的 aid；占位行没有
aid）」。引用内容只用于定位；模块从不信任调用者回传的整行平面数据，也从不
从平面行里取 cid / aid 当身份。下列情况一律拒绝：

* 令牌被篡改（HMAC 不符）；
* 令牌属于另一个库（`db_identity` 不符）或本库不存在的导出；
* 行引用不属于该令牌的导出（跨导出、跨库、伪造、改动）；
* 导出后配置/视图定义变化（旧令牌的 `config_hash` / `view_hash` 失配）。

## 5. 动作格式（explain / put 共用）

`explain` 与 `put` 的输入完全相同：`--view <get 输出文件>` 与
`--actions <JSON 数组>`。动作里的可编辑文本值都是第 6 节的**规范单元格**：

```json
{"type": "rename_customer",      "row_ref": ROW,  "name": CELL}
{"type": "set_address_label",    "row_ref": ROW,  "label": CELL}
{"type": "set_address_address",  "row_ref": ROW,  "address": CELL_OR_NULL}
{"type": "move_address",         "row_ref": ROW,  "to_cid": CID_TEXT}
{"type": "delete_address",       "row_ref": ROW}
{"type": "add_address",          "temp_ref": TEMP, "cid": CID_TEXT,
                                 "label": CELL, "address": CELL_OR_NULL}
{"type": "delete_customer",      "row_ref": ROW}
```

* `ROW`：`get` 返回的 `row_ref`，或本批 `add_address` 已声明的 `temp_ref`。
* 地址类动作的 `ROW` 必须是真实地址行（`customer_address`）；占位行不能当
  地址修改、迁移或删除，只能改名或删客户。
* `CID_TEXT` / `to_cid` 是**原文**客户身份字符串（不编码、不取自平面行）。
* `add_address.address` 可省略：省略时用配置默认值；显式 `null` 表示 NULL。
  `set_address_address.address` 必须给出，`null` 表示 NULL。
* `add_address.label` 必填（label 非空）。
* `TEMP` 是调用者自选的临时引用：非空、不得以保留前缀 `rr_` / `cw1.` 开头、
  不得等于真实 aid、同批不得重复声明。声明后才能被后续动作引用。
* 未知字段、缺少字段、未知 `type` 都拒绝；整批错误会一起列出。

## 6. 规范地址/名字编码（必须可逆、唯一、严格）

每个数据库文本值映射成一个可编辑单元格；`null` 与文本有显式标记：

| 源值 | 单元格 |
|---|---|
| `NULL` | `null`（四个字母，不加引号） |
| 字符串 `v` | `s"` + 转义(v) + `"` |

`"` 内：

* `\\` = 反斜线，`\"` = 双引号，`\n` = 换行，`\r` = 回车，`\t` = 制表符；
* `\u{XXXX}`（大写十六进制、无前导零、至少一位）表示一个 Unicode 标量值；
  **所有控制字符**（`U+0000`–`U+001F`、`U+007F`）与**所有 BMP 以上码点**都
  必须用这种转义；
* 其余字符（中文、空格、空串、BMP 内非控制字符）原样出现。空串是 `s""`，
  与 `null` 不同；字符串 `"null"` 必须写成 `s"null"`。

严格解码，唯一规范形式，以下全部拒绝：

* `NULL`、` null`、`null `、缺失引号、`s` 后面不是双引号；
* `\/`、`\x41`、`\b`、`\f` 等未知转义，结尾孤立反斜线；
* 未转义的双引号 / 原始控制字符；
* `\u{41}`（应用字面 `A`）、`\u{0000}`（前导零）、`\u{7f}`（小写）、
  `\u{D800}`（代理项）、`\u{110000}`（越界）、两个 `\uXXXX` 拼代理对；
* 超范围 / 缺少闭合花括号 / 闭合引号后还有内容。

`get` 输出必然规范；`put`/`explain` 对每个可编辑文本字段再次严格解码，不会
trim、改大小写或猜测 `"null"` 的语义。源里的中文、空串、换行、反斜线、制表
符、NUL、BMP 外字符都能无损往返（见测试 `EncodingTests` 与
`ActionFormatTests.test_unicode_roundtrip_through_actions`）。

## 7. 两种合并规则

### 7.1 客户改名：共享字段的显式意图

* 同一批里，所有针对同一 `cid` 的改名必须**完全相等**（逐码点，不做规范化）。
* 动作按列表顺序处理，但冲突**不会被后面的动作抹掉**：先提「A」、再提
  「B」、最后又改回旧名，仍然整批拒绝。
* 没有提交改名的行不投票；单个合规的改名（哪怕来自某一条重复地址行）即可。
* 行引用的客户归属以**导出时**为准：先把 a1 迁到 C2、再用 a1 的行引用改名，
  改的是 C1。
* 占位行可以提出改名，也可以被显式删除；但绝不当地址用。
* 客户在同一批被显式删除、又被改名 => `rename_on_deleted_customer` 冲突，
  不静默吞掉改名。

### 7.2 地址动作：按列表顺序

* 同一地址可在同批多次修改，依次生效，最后一次的值落地。
* 迁移保留 `aid` 与 `secret_tag`；`address`、`label` 不因迁移改变。
* 某地址在批内被删除后，再对它修改 / 迁移 / 再删除 => `address_already_deleted`。
* 临时地址同样遵守：新增后可连续改值、迁移（目标须为导出时存在且批末保留的
  客户），新增后又删除则该临时地址**不落源、不分配正式 aid**，映射值为
  `null`。
* 已有隐藏字段一律保持原值；新增行取配置默认值（`new_address_address`、
  `new_address_secret_tag`，也可在 `add_address` 里显式给 `address`）。
* 新增地址的正式 `aid` 由系统从「从未使用过的 aid」集合分配
  （`auto-aid-XXXXXXXX`，删除过的也不复用）；成功结果返回批末仍保留的临时
  引用 → 正式 aid 映射。

### 7.3 最终状态规则

* 客户删除看**批末**：可以先声明删客户，再迁走或删掉它的地址；只要批末仍有
  地址（含保留的临时地址）引用被删客户，整批拒绝。
* 迁移目标与新增目标必须是导出时存在、且批末仍保留的客户。
* `label` 唯一性只在**最终状态**检查：两地址互换标签、或迁走后让出旧标签都
  成功，不会被逐条即时唯一检查挡住。
* 删除地址绝不连带删除客户；删掉最后一个地址后 `get` 出现占位行，新增或迁
  入地址后占位行消失。
* 没有新增客户 / 编辑主键的能力（无对应动作）。

## 8. explain 与 put 的合同

两者先做同一套事：验证令牌 → 载入导出记录 → 严格解析动作 → 在导出时源状态
的副本上完整推导。

`explain` 返回（不写库、不消耗 aid、不推进 revision）：

* `writes`：推导出的逐源身份修改，含 `op`、身份（`cid` / `aid` /
  `temp_ref`）、`before` / `after`、`changed_fields`、
  `preserved_fields`（未改动、因而保留的字段范围）、`action_indexes` 与
  `row_refs`（冲突或新增所涉及的动作位置与行引用）；
* `conflicts`：每条冲突的原因、动作下标、行引用/临时引用与明细；
* `customers_changed` / `addresses_changed` / `addresses_added` /
  `addresses_deleted` / `customers_deleted` / `pending_temp_refs`；
* `stale`、`current_revision`、`export_revision`，以及过期时的 `warnings`。

`put`：

1. `BEGIN IMMEDIATE`，原子比较导出 `revision` 与当前 `revision`（并复核整
   个源状态的 `state_hash`）；旧导出**整批拒绝**（`StaleTokenError`），要求
   重新 `get`，绝不自动合并两份旧编辑。
2. 有冲突 / 结构错误则整批拒绝，源表不变。
3. 为保留的临时地址分配从未用过的 aid。
4. 按依赖顺序执行：删地址 → 改客户名 → 改地址字段 → 删客户 → 插新地址。
5. **同一事务内重读落库结果**，再核对最终约束（label 唯一、外键存在、身份集
   合与推导完全一致、无复活行）。
6. revision 加一，记一条批次记录，提交。

任一步失败（包括最后一条动作失败）整体 `ROLLBACK`：此前的改名/改标签/新增
都不保留，aid 不被消耗，revision 不变。空动作批：验证令牌与过期，但
revision、源字段、aid 分配状态都不变。

## 9. 可复跑演练

```bash
cd cleanwrite
python3 -I drill.py                      # 全流程，自动断言 + 参考模型核对
python3 -I tests/test_cleanwrite.py      # 72 个 unittest
```

`drill.py` 每一步都打印**含隐藏列的两张源表**、导出视图、`explain` 推导与
`put` 结果，并用 `tests/reference_model.py` 独立核对（该模型不导入产品包、
不调用 `explain`、不碰 SQLite，自行实现合并规则的逐条解释、最终状态校验、
LEFT JOIN + 码点排序 + 单元格编码）。演练覆盖：

0. 初始样本与占位行、隐藏字段不泄出；
1. 只从 a1 行改名 → 两条地址行都是「新甲」，隐藏字段不变；
2. 两行提出两个不同名字 → 整批失败，revision 不变；「改回旧名」不能消除冲突；
3. 一批内交换 a1/a2 标签并把 a1 迁到 C2；
4. 先声明删 C1、再删其剩余地址 → 成功，C2 与 a1 不受影响；
5. 新增临时地址（连续改值、迁移）→ 正式 aid；新增后删除 → 不落源、不占号；
6. 真实 NULL 地址与占位行并存；删最后一个地址 → 占位出现；再新增 → 占位消失；
7. 无效转义 / `null` 当名字被拒（而 `s"null"` 是字面字符串）；
8. 删除后再修改、批末标签冲突、仍被地址引用的客户删除；
9. 最后一条动作失败 → 前面动作全部回滚；
10. 两个旧导出竞争提交，只有第一个生效；
11. 跨导出/跨库/篡改的引用与令牌、配置（视图定义）变化；
12. 空动作批与回写后的再空批都无隐形变化；
13. 失败后重新导出、修正动作继续工作。

### 完整命令演示（终端里展示源表变化）

```bash
cd cleanwrite
rm -rf walk && mkdir walk

# 1. 初始化 + 查看初始源表
python3 -I cwcli.py init --db walk/live.db --sample samples/drill.json
python3 -I cwcli.py source --db walk/live.db

# 2. 导出可编辑视图（token + 行引用 + 规范单元格）
python3 -I cwcli.py get --db walk/live.db --out walk/view1.json
python3 -m json.tool walk/view1.json | head -40

# 3. 只从 a1 那一行提出改名（生成动作 JSON；可手改）
python3 -I tools/make_actions.py walk/view1.json \
        --out walk/actions1.json --rename-from-aid a1 新甲

# 4. 先 explain：只看推导，不写库、不推进 revision
python3 -I cwcli.py explain --db walk/live.db \
        --view walk/view1.json --actions walk/actions1.json
python3 -I cwcli.py source --db walk/live.db        # revision 仍为 0

# 5. put：原子写入
python3 -I cwcli.py put --db walk/live.db \
        --view walk/view1.json --actions walk/actions1.json
python3 -I cwcli.py source --db walk/live.db        # C1.name=新甲，隐藏值不变

# 6. 再次 get：两条地址行都显示新名字
python3 -I cwcli.py get --db walk/live.db --out walk/view2.json
python3 -m json.tool walk/view2.json | head -40

# 7. 用旧导出再次 put → 退出码 3（stale），要求重新 get
python3 -I cwcli.py put --db walk/live.db \
        --view walk/view1.json --actions walk/actions1.json; echo "exit=$?"

# 8. 混用另一次导出的行引用 → 退出码 5（invalid reference），不写库
python3 -I cwcli.py get --db walk/live.db --out walk/view3.json
python3 -I tools/make_actions.py walk/view3.json \
        --out walk/foreign.json --label a2 另一个标签
python3 -I cwcli.py explain --db walk/live.db \
        --view walk/view1.json --actions walk/foreign.json; echo "exit=$?"
```

命令行退出码：`0` 成功，`2` 输入/冲突/编码被拒，`3` 导出过期，
`4` 令牌被篡改或不属于本库，`5` 行引用无效或不属于该导出。

`python3 -I drill.py` 是同样的流程加上全部边界情形与独立核对；`source`、
`explain`、`put` 的完整 JSON 都会打印出来，不是只报「测试通过」。

## 10. 测试覆盖

`tests/test_cleanwrite.py`（72 个）按主题分组：

* `EncodingTests`：规范编码往返、null/空串/字面 `"null"` 区分、非规范形式
  （未知转义、原样控制字符、前导零、代理项、越界、字面 astral、尾随内容）、
  NUL / 制表 / 换行 / 反斜线 / 中文 / emoji；数据库内往返。
* `ImportTests`：重复身份、坏引用、坏类型、重复 label、空身份、未知键、
  JSON 重复键、重复 init。
* `ViewTests`：码点序、占位行 vs 真实 NULL 地址、隐藏字段不导出、get 不改库。
* `RenameTests`：一处改名/共享字段、冲突、冲突不可被改回消除、未提交行不投
  票、删客户+改名冲突、占位行改名、迁移后引用仍属导出时客户。
* `AddressTests`：重复修改、迁移保留 aid/secret、删除后再改拒绝、先改后删、
  标签互换、互换+迁移、最终标签冲突、占位行不能当地址。
* `TempTests`：新增改值迁移、新增后删除不落源不占号、删除后改拒绝、重复临时
  引用、冒充真实身份、aid 不复用、目标须存在且保留。
* `DeleteTests`：先删客户后迁走、批末仍引用则拒绝、删最后地址只出现占位、
  再新增占位消失。
* `TokenTests`：篡改令牌/行引用、跨导出、跨库、视图定义变化。
* `StaleTests` / `AtomicityTests` / `EmptyBatchTests`：旧导出、explain 不落
  库、最后动作失败回滚、失败后修正继续、空批无变化。
* `ActionFormatTests`：未知类型/字段、名字必须是文本单元格、`null` vs
  `s"null"` vs 带空格字面量、Unicode 动作往返。
* `ReferenceModelTests`：组合动作后源表与视图逐行比对独立模型；参考模型也拒
  绝产品拒绝的批次。
* `CliTests`：在 `-I` 下走 init → get → explain → put → source，并检查过期
  put 的退出码 3。

`tests/reference_model.py` 与 `drill.py` 中的核对都不执行产品回写、不调用
`explain`、不使用 SQLite。

## 11. 已知边界

* 只支持本模块管理的两张源表；不拦截外部 SQL 偷改。外部改动若未推进
  revision，`put` 会因整状态 `state_hash` 不符而拒绝并要求重新 `get`。
* 没有网络账户 / 权限系统；令牌只证明「同一库、同一版本、同一视图定义」。
* 不提供新增客户或编辑主键的动作。
* `label` 唯一性是本模块的最终状态规则；导入时也会检查，但库里刻意不建
  `(cid, label)` 唯一索引，因为批内中间状态允许暂时重复。
