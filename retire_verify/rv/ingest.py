"""导入层：对象登记、退役申请、采集配置、目录采集结果。

采集结果导入时的完整性校验（任一不满足即拒绝整次采集，拒绝的采集不得为退役背书）：
  * 标为 complete 的范围不得带 truncated 标记；
  * 标为 complete 的范围内不得出现重复身份；
  * 同一身份不得被记到两个不同的 (schema, 名称)；
  * 同一 (实例, 代际, 类型, 编号) 不得对应两个不同的创建凭据
    （编号只在删除后才可复用，同一快照里不可能同时存在两个）；
  * 对象必须落在声明的 (schema, 类型) 范围内；
  * 列清单必须指向本次对象清单中出现过的对象。
无创建凭据的对象照常保存（cred_available=0），但不参与上述身份类校验，
也永不在核验时被认作申请对象。
"""
from __future__ import annotations

import csv
import json
import sqlite3

from .db import make_object_key

VALID_STATUS = {"complete", "restricted", "failed"}


class IngestError(Exception):
    pass


# ---------------------------------------------------------------- 对象登记

def import_registry_rows(conn: sqlite3.Connection, rows) -> int:
    """rows: 可迭代的 dict，键同 registry 列（无 object_key）。"""
    batch, inserted = [], 0
    for row in rows:
        key = make_object_key(row["instance"], row["generation"], row["obj_type"],
                              row["obj_number"], row["cred"])
        batch.append((key, row["instance"], int(row["generation"]), row["obj_type"],
                      int(row["obj_number"]), row["cred"], row["schema_name"],
                      row["object_name"], row["registered_at"]))
        if len(batch) >= 5000:
            inserted += _flush_registry(conn, batch)
            batch = []
    if batch:
        inserted += _flush_registry(conn, batch)
    conn.commit()
    return inserted


def _flush_registry(conn, batch) -> int:
    cur = conn.executemany(
        "INSERT OR IGNORE INTO registry (object_key, instance, generation, obj_type,"
        " obj_number, cred, schema_name, object_name, registered_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)", batch)
    return cur.rowcount


def import_registry(conn: sqlite3.Connection, path: str) -> int:
    with open(path, newline="", encoding="utf-8") as f:
        return import_registry_rows(conn, csv.DictReader(f))


# ---------------------------------------------------------------- 退役申请

def import_applications_data(conn: sqlite3.Connection, apps) -> int:
    n = 0
    for a in apps:
        key = make_object_key(a["instance"], a["generation"], a["obj_type"],
                              a["obj_number"], a["cred"])
        cur = conn.execute(
            "INSERT OR IGNORE INTO applications (app_id, object_key, instance, generation,"
            " obj_type, obj_number, cred, last_known_schema, last_known_name, applicant,"
            " reason, submitted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (a["app_id"], key, a["instance"], int(a["generation"]), a["obj_type"],
             int(a["obj_number"]), a["cred"], a["last_known_schema"],
             a["last_known_name"], a.get("applicant"), a.get("reason"),
             a.get("submitted_at")))
        n += cur.rowcount
    conn.commit()
    return n


def import_applications(conn: sqlite3.Connection, path: str) -> int:
    with open(path, encoding="utf-8") as f:
        return import_applications_data(conn, json.load(f))


# ---------------------------------------------------------------- 采集配置

def import_config_data(conn: sqlite3.Connection, cfg: dict) -> None:
    if conn.execute("SELECT 1 FROM configs WHERE version=?", (cfg["version"],)).fetchone():
        raise IngestError(f"配置版本 {cfg['version']} 已存在（配置按版本固化，不可覆盖）")
    conn.execute("INSERT INTO configs (version, declared_at) VALUES (?,?)",
                 (cfg["version"], cfg.get("declared_at")))
    conn.executemany(
        "INSERT INTO config_scopes (version, schema_name, obj_type) VALUES (?,?,?)",
        [(cfg["version"], s["schema_name"], s["obj_type"]) for s in cfg["scopes"]])
    conn.commit()


def import_config(conn: sqlite3.Connection, path: str) -> None:
    with open(path, encoding="utf-8") as f:
        import_config_data(conn, json.load(f))


# ---------------------------------------------------------------- 目录采集

def _validate_collection(data: dict):
    problems = []
    scopes = data.get("scopes", [])
    scope_map = {}
    for s in scopes:
        label = f"{s.get('schema_name')}/{s.get('obj_type')}"
        if s.get("status") not in VALID_STATUS:
            problems.append(f"范围 {label} 状态非法: {s.get('status')!r}")
            continue
        if (s["schema_name"], s["obj_type"]) in scope_map:
            problems.append(f"范围记录重复: {label}")
        scope_map[(s["schema_name"], s["obj_type"])] = s
        if s["status"] == "complete" and s.get("truncated"):
            problems.append(f"范围 {label} 标记为 complete 但清单被截断（truncated=true）")

    key_location = {}   # object_key -> (schema, name)，仅供凭据齐全的对象
    num_cred = {}       # (instance, generation, obj_type, obj_number) -> cred
    keys_per_scope = {}
    objects = data.get("objects", [])
    for o in objects:
        sk = (o.get("schema_name"), o.get("obj_type"))
        if sk not in scope_map:
            problems.append(f"对象 {o.get('object_name')!r} 出现在未声明的范围 "
                            f"{sk[0]}/{sk[1]}")
            continue
        cred = o.get("cred")
        key = make_object_key(o["instance"], o["generation"], o["obj_type"],
                              o["obj_number"], cred)
        if not cred:
            continue  # 无凭据对象：保存即可，不参与身份类校验
        if key in key_location and key_location[key] != (o["schema_name"], o["object_name"]):
            problems.append(
                f"记录互相矛盾: 身份 {key} 同时记为 "
                f"{key_location[key][0]}.{key_location[key][1]} 与 "
                f"{o['schema_name']}.{o['object_name']}")
        key_location[key] = (o["schema_name"], o["object_name"])
        nk = (o["instance"], o["generation"], o["obj_type"], o["obj_number"])
        if nk in num_cred and num_cred[nk] != cred:
            problems.append(
                f"记录互相矛盾: 编号 {nk} 同时对应创建凭据 "
                f"{num_cred[nk]} 与 {cred}")
        num_cred.setdefault(nk, cred)
        seen = keys_per_scope.setdefault(sk, set())
        if key in seen and scope_map[sk]["status"] == "complete":
            problems.append(f"范围 {sk[0]}/{sk[1]} 标记为 complete 但身份重复: {key}")
        seen.add(key)

    obj_keys = {make_object_key(o["instance"], o["generation"], o["obj_type"],
                                o["obj_number"], o.get("cred")) for o in objects}
    for cl in data.get("columns", []):
        ref = cl.get("object", {})
        k = make_object_key(ref.get("instance"), ref.get("generation"),
                            ref.get("obj_type"), ref.get("obj_number"), ref.get("cred"))
        if k not in obj_keys:
            problems.append(f"列清单指向未出现在对象清单中的对象: {k}")
        if cl.get("status") not in VALID_STATUS:
            problems.append(f"对象 {k} 的列清单状态非法: {cl.get('status')!r}")
    return problems


def import_collection_data(conn: sqlite3.Connection, data: dict) -> dict:
    cid = data["collection_id"]
    if conn.execute("SELECT 1 FROM collections WHERE collection_id=?",
                    (cid,)).fetchone():
        raise IngestError(f"采集 {cid} 已存在，拒绝重复导入")
    problems = _validate_collection(data)
    if problems:
        reason = "；".join(problems)
        conn.execute(
            "INSERT INTO collections (collection_id, collected_at, connector, accepted,"
            " reject_reason) VALUES (?,?,?,0,?)",
            (cid, data.get("collected_at"), data.get("connector"), reason))
        conn.commit()
        return {"collection_id": cid, "accepted": False, "problems": problems}

    with conn:
        conn.execute(
            "INSERT INTO collections (collection_id, collected_at, connector, accepted,"
            " reject_reason) VALUES (?,?,?,1,NULL)",
            (cid, data.get("collected_at"), data.get("connector")))
        conn.executemany(
            "INSERT INTO collection_scopes (collection_id, schema_name, obj_type, status,"
            " detail) VALUES (?,?,?,?,?)",
            [(cid, s["schema_name"], s["obj_type"], s["status"], s.get("detail"))
             for s in data.get("scopes", [])])
        for o in data.get("objects", []):
            cred = o.get("cred")
            key = make_object_key(o["instance"], o["generation"], o["obj_type"],
                                  o["obj_number"], cred)
            conn.execute(
                "INSERT INTO sightings (collection_id, object_key, cred_available,"
                " instance, generation, obj_type, obj_number, cred, schema_name,"
                " object_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid, key, 1 if cred else 0, o["instance"], int(o["generation"]),
                 o["obj_type"], int(o["obj_number"]), cred, o["schema_name"],
                 o["object_name"]))
            conn.execute(
                "INSERT OR IGNORE INTO collection_generations (collection_id, instance,"
                " generation) VALUES (?,?,?)",
                (cid, o["instance"], int(o["generation"])))
        for cl in data.get("columns", []):
            ref = cl["object"]
            key = make_object_key(ref["instance"], ref["generation"], ref["obj_type"],
                                  ref["obj_number"], ref.get("cred"))
            conn.execute(
                "INSERT INTO column_lists (collection_id, object_key, status)"
                " VALUES (?,?,?)", (cid, key, cl["status"]))
            conn.executemany(
                "INSERT INTO column_items (collection_id, object_key, col_number,"
                " col_name) VALUES (?,?,?,?)",
                [(cid, key, int(c["col_number"]), c["col_name"])
                 for c in cl.get("columns", [])])
    return {"collection_id": cid, "accepted": True,
            "scopes": len(data.get("scopes", [])),
            "objects": len(data.get("objects", [])),
            "columns": len(data.get("columns", []))}


def import_collection(conn: sqlite3.Connection, path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return import_collection_data(conn, json.load(f))
