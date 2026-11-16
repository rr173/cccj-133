#!/usr/bin/env python3
"""边界情形测试套件：每个用例独立建库，断言结论与依据文本。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rv import checker, db as dbmod, ingest, verify as V

TESTS = []


def t(name):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


def fresh():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return dbmod.init_db(path)


def seed(conn, objects=None, apps=None, cfg_scopes=None, cfg="c1"):
    if objects:
        ingest.import_registry_rows(conn, objects)
    if cfg_scopes:
        ingest.import_config_data(conn, {
            "version": cfg, "declared_at": "2026-10-01T00:00:00Z",
            "scopes": [{"schema_name": s, "obj_type": t} for s, t in cfg_scopes]})
    if apps:
        ingest.import_applications_data(conn, apps)


def obj(num=101, cred="C-1", inst="inst1", gen=1, otype="TABLE",
        schema="A", name="orders"):
    return {"instance": inst, "generation": gen, "obj_type": otype,
            "obj_number": num, "cred": cred, "schema_name": schema,
            "object_name": name, "registered_at": "2026-09-01T00:00:00Z"}


def app(app_id="APP-T", num=101, cred="C-1", inst="inst1", gen=1,
        otype="TABLE", schema="A", name="orders"):
    return {"app_id": app_id, "instance": inst, "generation": gen,
            "obj_type": otype, "obj_number": num, "cred": cred,
            "last_known_schema": schema, "last_known_name": name,
            "applicant": "tester", "reason": "t",
            "submitted_at": "2026-10-01T00:00:00Z"}


def coll(cid, scopes, objects=(), columns=(), at="2026-11-01T00:00:00Z"):
    return {"collection_id": cid, "collected_at": at,
            "connector": "mock-catalog", "scopes": scopes,
            "objects": list(objects), "columns": list(columns)}


def scope(schema, otype, status, **kw):
    d = {"schema_name": schema, "obj_type": otype, "status": status}
    d.update(kw)
    return d


def sight(o):
    """registry 行转采集对象行（去掉 registered_at）。"""
    return {k: o[k] for k in ("instance", "generation", "obj_type", "obj_number",
                              "cred", "schema_name", "object_name")}


STD_CFG = [("A", "TABLE"), ("B", "TABLE")]


# 1. 受限范围里的正向发现仍是有效存在证据
@t("受限范围中的正向发现 → 仍存在")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"),
        scope("B", "TABLE", "restricted", detail="账号收紧")],
        objects=[sight(obj(schema="B", name="orders"))]))
    r = V.verify_application(conn, "APP-T", "k1")
    assert r["conclusion"] == V.STILL_EXISTS, r["conclusion"]
    assert r["current_location"] == {"schema": "B", "name": "orders"}
    assert r["migrated"] is True and r["renamed"] is False
    return "受限范围内实际读到的对象被采信为存在证据"


# 2. 范围读取失败 → 证据不足并点名阻断项
@t("范围读取失败 → 证据不足，阻断项点名失败范围")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"),
        scope("B", "TABLE", "failed", detail="连接超时")]))
    r = V.verify_application(conn, "APP-T", "k1")
    assert r["conclusion"] == V.INSUFFICIENT
    assert r["blockers"][0]["status"] == "failed"
    assert "B" == r["blockers"][0]["schema"]
    return "失败范围被列为阻断项"


# 3. 标完整但被截断的清单 → 整次采集被拒绝，核验拒绝使用
@t("完整清单被截断 → 采集被拒绝，核验拒绝背书")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    res = ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete", truncated=True),
        scope("B", "TABLE", "complete")]))
    assert not res["accepted"] and any("截断" in p for p in res["problems"])
    try:
        V.verify_application(conn, "APP-T", "k1")
    except V.VerifyError as e:
        assert "拒绝" in str(e)
        return "被拒绝的采集不能用于核验"
    raise AssertionError("应对被拒绝的采集抛出 VerifyError")


# 4. 完整清单内身份重复 → 拒绝
@t("完整清单内身份重复 → 采集被拒绝")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    res = ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"),
        scope("B", "TABLE", "complete")],
        objects=[sight(obj()), sight(obj())]))
    assert not res["accepted"] and any("身份重复" in p for p in res["problems"])
    return res["problems"][0]


# 5. 记录互相矛盾（同编号不同凭据 / 同身份不同位置）→ 拒绝
@t("记录互相矛盾 → 采集被拒绝")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    r1 = ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[sight(obj()), sight(obj(cred="C-9", schema="B", name="x"))]))
    assert not r1["accepted"] and any("创建凭据" in p for p in r1["problems"])
    r2 = ingest.import_collection_data(conn, coll("k2", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[sight(obj()), sight(obj(schema="B", name="orders"))]))
    assert not r2["accepted"] and any("互相矛盾" in p for p in r2["problems"])
    return "同编号不同凭据、同身份不同位置均被拒绝"


# 6. 无创建凭据的对象：保存观察但不认作申请对象
@t("无创建凭据的对象 → 保存但不参与身份判定")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    nocred = sight(obj(schema="B"))
    nocred["cred"] = None
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[nocred]))
    row = conn.execute("SELECT cred_available FROM sightings").fetchone()
    assert row["cred_available"] == 0
    r = V.verify_application(conn, "APP-T", "k1")
    assert r["conclusion"] == V.SUPPORTS_RETIREMENT, r["conclusion"]
    assert any("无法提供创建凭据" in n for n in r["notes"])
    return "同编号同名的无凭据对象不影响缺席判定，且在备注中留痕"


# 7. 对象可见但列采集失败/受限 → 不误判对象消失，不妄言列删除
@t("对象可见但列采集失败/受限 → 只展示已见列")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    ref = {"instance": "inst1", "generation": 1, "obj_type": "TABLE",
           "obj_number": 101, "cred": "C-1"}
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[sight(obj())],
        columns=[{"object": ref, "status": "failed"}]))
    r = V.verify_application(conn, "APP-T", "k1")
    assert r["conclusion"] == V.STILL_EXISTS
    assert r["columns"]["status"] == "failed"
    assert "不得据此断言任何列已删除" in r["columns"]["note"]
    # 受限列清单：只展示已见列
    ingest.import_collection_data(conn, coll("k2", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[sight(obj())],
        columns=[{"object": ref, "status": "restricted",
                  "columns": [{"col_number": 1, "col_name": "id"}]}],
        at="2026-11-02T00:00:00Z"))
    r2 = V.verify_application(conn, "APP-T", "k2")
    assert r2["columns"]["status"] == "restricted"
    assert r2["columns"]["seen_columns"] == [{"col_number": 1, "col_name": "id"}]
    # 完整列清单按稳定列号比对差异
    ingest.import_collection_data(conn, coll("k3", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[sight(obj())],
        columns=[{"object": ref, "status": "complete",
                  "columns": [{"col_number": 1, "col_name": "id"},
                              {"col_number": 2, "col_name": "amount"}]}],
        at="2026-11-03T00:00:00Z"))
    ingest.import_collection_data(conn, coll("k4", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[sight(obj())],
        columns=[{"object": ref, "status": "complete",
                  "columns": [{"col_number": 2, "col_name": "amount"},
                              {"col_number": 3, "col_name": "note"}]}],
        at="2026-11-04T00:00:00Z"))
    r4 = V.verify_application(conn, "APP-T", "k4")
    assert r4["columns"]["added"] == [[3, "note"]]
    assert r4["columns"]["removed"] == [[1, "id"]]
    # 缓存读取与新计算结构一致（JSON 往返后仍相同）
    r4b = V.verify_application(conn, "APP-T", "k4")
    assert r4b["from_cache"] and r4b["columns"] == r4["columns"]
    return "列失败不误判对象消失；列差异按稳定列号比对"


# 8. 相同编号出现在不同实例 → 互不相干
@t("相同编号出现在不同实例 → 不构成同一对象")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[sight(obj(inst="inst2", schema="A"))]))
    r = V.verify_application(conn, "APP-T", "k1")
    assert r["conclusion"] == V.SUPPORTS_RETIREMENT
    assert not any("同编号" in n for n in r["notes"])
    return "inst2 的同编号对象既不算存在证据也不触发替身提示"


# 9. 旧申请对象在后续重新出现 → 历史核验保留，不虚构先删后建
@t("支持退役后原身份重新出现 → 历史保留+不虚构删建")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")]))
    r1 = V.verify_application(conn, "APP-T", "k1")
    assert r1["conclusion"] == V.SUPPORTS_RETIREMENT
    ingest.import_collection_data(conn, coll("k2", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[sight(obj())], at="2026-11-02T00:00:00Z"))
    r2 = V.verify_application(conn, "APP-T", "k2")
    assert r2["conclusion"] == V.STILL_EXISTS
    assert any("不得把它解释为先删除后重建" in n for n in r2["notes"])
    hist = V.list_verifications(conn, "APP-T")
    assert hist[0]["conclusion"] == V.SUPPORTS_RETIREMENT
    assert hist[0]["verif_id"] == r1["verif_id"]
    return "历史结论原样保留，新核验按同一对象持续存在处理"


# 10. 同编号同旧名但凭据不同 → 不能替申请对象下任何结论
@t("同编号同旧名不同凭据 → 替身被显式排除")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[sight(obj(cred="C-9", schema="B"))]))
    r = V.verify_application(conn, "APP-T", "k1")
    assert r["conclusion"] == V.SUPPORTS_RETIREMENT
    assert any("创建凭据不同" in n for n in r["notes"])
    return "替身对象被点名排除，结论仍按身份缺席判定"


# 11. 受限 → 完整 的转变改变结论
@t("受限范围补采为完整 → 暂缓转为支持退役")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"),
        scope("B", "TABLE", "restricted")]))
    r1 = V.verify_application(conn, "APP-T", "k1")
    assert r1["conclusion"] == V.INSUFFICIENT
    ingest.import_collection_data(conn, coll("k2", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        at="2026-11-02T00:00:00Z"))
    r2 = V.verify_application(conn, "APP-T", "k2")
    assert r2["conclusion"] == V.SUPPORTS_RETIREMENT
    return "同一申请在补齐完整采集后转为支持退役"


# 12. 扩大可能 schema 范围 → 原支持结论对新配置不再成立
@t("扩大配置范围 → 未覆盖的新范围成为阻断项")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG, cfg="c1")
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")]))
    r1 = V.verify_application(conn, "APP-T", "k1", "c1")
    assert r1["conclusion"] == V.SUPPORTS_RETIREMENT
    ingest.import_config_data(conn, {
        "version": "c2", "declared_at": "2026-11-05T00:00:00Z",
        "scopes": [{"schema_name": s, "obj_type": "TABLE"} for s in "ABC"]})
    r2 = V.verify_application(conn, "APP-T", "k1", "c2")
    assert r2["conclusion"] == V.INSUFFICIENT
    assert r2["blockers"][0]["schema"] == "C"
    assert r2["blockers"][0]["status"] == "uncollected"
    assert any("配置边界变化" in n for n in r2["notes"])
    return "配置扩大后同一采集证据不再充分，且提示这是配置边界变化"


# 13. 实例代际切换 → 结论中显示代际变化
@t("实例代际切换 → 结论显示代际提示")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[sight(obj(num=1, cred="G2-1", gen=2, name="x"))]))
    r = V.verify_application(conn, "APP-T", "k1")
    assert r["conclusion"] == V.SUPPORTS_RETIREMENT
    assert any("实例代际变化" in n for n in r["notes"])
    return "代际切换在结论中显式标注"


# 14. 重复核验读取历史结果
@t("重复核验 → 读取历史结果，不产生新行")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")]))
    r1 = V.verify_application(conn, "APP-T", "k1")
    r2 = V.verify_application(conn, "APP-T", "k1")
    assert r1["verif_id"] == r2["verif_id"] and r2["from_cache"]
    n = conn.execute("SELECT COUNT(*) c FROM verifications").fetchone()["c"]
    assert n == 1
    return "同一(申请,采集,配置)只落一行"


# 15. 独立核对器与正式结论一致（支持/暂缓/仍存在三态）
@t("独立核对器三态核对一致")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"),
        scope("B", "TABLE", "restricted")]))
    V.verify_application(conn, "APP-T", "k1")
    c1 = checker.check_application(conn, "APP-T", "k1")
    assert c1["match"] and c1["expected"] == "insufficient_evidence"
    ingest.import_collection_data(conn, coll("k2", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        at="2026-11-02T00:00:00Z"))
    V.verify_application(conn, "APP-T", "k2")
    c2 = checker.check_application(conn, "APP-T", "k2")
    assert c2["match"] and c2["expected"] == "supports_retirement"
    ingest.import_collection_data(conn, coll("k3", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[sight(obj())], at="2026-11-03T00:00:00Z"))
    V.verify_application(conn, "APP-T", "k3")
    c3 = checker.check_application(conn, "APP-T", "k3")
    assert c3["match"] and c3["expected"] == "still_exists"
    return "独立核对器在三种结论上均与正式核验一致"


# 16. 改名/迁移分别如实标注
@t("改名与迁移分别如实标注")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[sight(obj(name="orders_new"))]))
    r = V.verify_application(conn, "APP-T", "k1")
    assert r["renamed"] is True and r["migrated"] is False
    ingest.import_collection_data(conn, coll("k2", [
        scope("A", "TABLE", "complete"), scope("B", "TABLE", "complete")],
        objects=[sight(obj(schema="B"))], at="2026-11-02T00:00:00Z"))
    r2 = V.verify_application(conn, "APP-T", "k2")
    assert r2["renamed"] is False and r2["migrated"] is True
    return "仅改名、仅迁移两种情形都正确区分"


# 17. 配置声明的范围在采集中完全缺失 → 未采集阻断
@t("配置范围未被采集覆盖 → 未采集阻断")
def _():
    conn = fresh()
    seed(conn, [obj()], [app()], STD_CFG)
    ingest.import_collection_data(conn, coll("k1", [
        scope("A", "TABLE", "complete")]))
    r = V.verify_application(conn, "APP-T", "k1")
    assert r["conclusion"] == V.INSUFFICIENT
    assert r["blockers"][0]["status"] == "uncollected"
    return "缺失范围以 uncollected 列入阻断项"


def main():
    failures = 0
    for name, fn in TESTS:
        try:
            msg = fn()
            print(f"✓ {name}  —— {msg}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"✗ {name}  —— 失败: {type(e).__name__}: {e}")
    print("-" * 72)
    print(f"共 {len(TESTS)} 项，失败 {failures} 项")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
