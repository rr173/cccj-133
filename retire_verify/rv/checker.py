"""独立核对器：不调用 rv.verify 中的任何判定函数，直接按公开规则重算结论，
并与库存的核验结果比对。用于交叉验证“证据支持退役 / 证据不足”两类结论。

规则复述（与正式实现相互独立地书写）：
  1. 在本次采集中按 实例|代际|类型|编号|创建凭据 找到有凭据观察 → 仍存在；
  2. 否则枚举必需范围 = 申请原 schema/类型 ∪ 配置声明的同类型范围，
     全部 complete → 证据支持退役；任一缺失/受限/失败 → 证据不足。
"""
from __future__ import annotations

import sqlite3


def _key(inst, gen, otype, num, cred) -> str:
    return f"{inst}|{gen}|{otype}|{num}|{cred if cred else ''}"


def check_application(conn: sqlite3.Connection, app_id: str,
                      collection_id: str) -> dict:
    app = conn.execute("SELECT * FROM applications WHERE app_id=?",
                       (app_id,)).fetchone()
    if not app:
        raise ValueError(f"申请 {app_id} 不存在")
    stored = conn.execute(
        "SELECT * FROM verifications WHERE app_id=? AND collection_id=?"
        " ORDER BY verif_id DESC LIMIT 1", (app_id, collection_id)).fetchone()
    if not stored:
        raise ValueError(f"申请 {app_id} 在采集 {collection_id} 下尚无核验结果可核对")

    key = _key(app["instance"], app["generation"], app["obj_type"],
               app["obj_number"], app["cred"])
    found = conn.execute(
        "SELECT sighting_id, schema_name, object_name FROM sightings"
        " WHERE collection_id=? AND object_key=? AND cred_available=1",
        (collection_id, key)).fetchall()

    coverage = []
    if found:
        expected = "still_exists"
    else:
        required = {(app["last_known_schema"], app["obj_type"])}
        required |= {(r["schema_name"], r["obj_type"]) for r in conn.execute(
            "SELECT schema_name, obj_type FROM config_scopes WHERE version=?"
            " AND obj_type=?", (stored["config_version"], app["obj_type"]))}
        all_complete = True
        for schema, otype in sorted(required):
            row = conn.execute(
                "SELECT status FROM collection_scopes WHERE collection_id=?"
                " AND schema_name=? AND obj_type=?",
                (collection_id, schema, otype)).fetchone()
            status = row["status"] if row else "uncollected"
            coverage.append({"schema": schema, "obj_type": otype, "status": status})
            if status != "complete":
                all_complete = False
        expected = "supports_retirement" if all_complete else "insufficient_evidence"

    return {
        "app_id": app_id,
        "collection_id": collection_id,
        "config_version": stored["config_version"],
        "expected": expected,
        "stored": stored["conclusion"],
        "match": expected == stored["conclusion"],
        "coverage": coverage,
        "found": [dict(f) for f in found],
    }


def format_check(c: dict) -> str:
    mark = "PASS" if c["match"] else "FAIL"
    lines = [f"[{mark}] 申请 {c['app_id']} @ 采集 {c['collection_id']}"
             f"（配置 {c['config_version']}）：独立重算={c['expected']}"
             f"  库存结论={c['stored']}"]
    if c["found"]:
        lines.append(f"       独立核对器找到身份观察: "
                     f"{[(f['sighting_id'], f['schema_name'], f['object_name']) for f in c['found']]}")
    for s in c["coverage"]:
        lines.append(f"       覆盖检查: {s['schema']}/{s['obj_type']} = {s['status']}")
    return "\n".join(lines)
