#!/usr/bin/env python3
"""手工样例：一份申请（APP-001，表 A.orders）从提交到复核的完整生命周期。

  1. col-1：A 完整、B 受限且未见该表            → 证据不足（暂缓）
  2. col-2：在 B 找到同一身份，已改名 orders_v2 → 仍存在（标注迁移+改名）
  3. col-3：A、B 都完整且身份缺席               → 证据支持退役
  4. 重复核验 col-3                             → 读取历史结果，不重新计算
  5. col-4：出现同编号同旧名但凭据不同的对象     → 仍支持退役（替身不算数）
  6. 配置扩大到 cfg-2（新增 schema C）          → 证据不足（C 未采集）
  7. col-5：实例重建为第 2 代，A/B/C 完整       → 支持退役（标注代际变化）
每一步都用独立核对器复核结论。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rv import checker, db as dbmod, ingest, verify as V

BASE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(BASE, "samples")
DB = os.path.join(BASE, "run", "demo.db")


def step(title):
    print("=" * 78)
    print(title)
    print("=" * 78)


def main():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)
    conn = dbmod.init_db(DB)

    step("准备：导入对象登记、退役申请、采集配置 cfg-1")
    print(f"登记 {ingest.import_registry(conn, os.path.join(SAMPLES, 'registry.csv'))} 条；"
          f"申请 {ingest.import_applications(conn, os.path.join(SAMPLES, 'applications.json'))} 份")
    ingest.import_config(conn, os.path.join(SAMPLES, "config_v1.json"))
    print("配置 cfg-1 已导入（TABLE 可能范围: A, B；VIEW 可能范围: B）")

    def run(title, colfile, app, col, cfg=None):
        step(title)
        res = ingest.import_collection(conn, os.path.join(SAMPLES, colfile))
        assert res["accepted"], res
        r = V.verify_application(conn, app, col, cfg)
        print(V.format_result(r))
        c = checker.check_application(conn, app, col)
        print(checker.format_check(c))
        return r

    run("第 1 次核验：col-1  A 完整、B 受限且未见该表 → 只能暂缓",
        "col1_a_complete_b_restricted.json", "APP-001", "col-1")

    run("第 2 次核验：col-2  在 B 找到同一身份（改名 orders_v2）→ 仍存在+迁移",
        "col2_found_in_b_renamed.json", "APP-001", "col-2")

    run("第 3 次核验：col-3  A、B 都完整且身份缺席 → 证据支持退役",
        "col3_ab_complete_absent.json", "APP-001", "col-3")

    step("重复核验 APP-001 @ col-3：应读取历史结果（同一核验编号）")
    r = V.verify_application(conn, "APP-001", "col-3")
    print(V.format_result(r))

    run("第 4 次核验：col-4  出现同编号、同旧名但创建凭据不同的对象 → 替身不算数",
        "col4_decoy_same_number_name.json", "APP-001", "col-4")

    step("配置升级：导入 cfg-2（TABLE 可能范围增加 schema C）")
    ingest.import_config(conn, os.path.join(SAMPLES, "config_v2.json"))
    print("配置 cfg-2 已导入（TABLE 可能范围: A, B, C）")

    step("第 5 次核验：APP-001 @ col-4 用 cfg-2 → C 未采集，证据不足")
    r = V.verify_application(conn, "APP-001", "col-4", "cfg-2")
    print(V.format_result(r))
    print(checker.format_check(checker.check_application(conn, "APP-001", "col-4")))

    run("第 6 次核验：col-5  实例重建为第 2 代，A/B/C 完整 → 支持退役+代际提示",
        "col5_gen2_all_complete.json", "APP-001", "col-5", "cfg-2")

    step("APP-001 全部核验历史（只增不改）")
    for r in V.list_verifications(conn, "APP-001"):
        print(V.format_result(r))
        print("-" * 78)


if __name__ == "__main__":
    main()
