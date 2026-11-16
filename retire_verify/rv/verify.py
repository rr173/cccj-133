"""核验引擎：对单份申请在给定采集与配置版本下给出结论并落库（只增不改）。

结论三态：
  still_exists          仍存在（找到完全相同身份；名称/schema 变化如实标注）
  supports_retirement   证据支持退役（原范围与全部配置声明的可能迁移范围均完整
                        采集且身份缺席）
  insufficient_evidence 证据不足（存在未采集/受限/失败范围，或采集被拒绝）

核验幂等：同一 (申请, 采集, 配置版本) 重复核验直接读取历史结果；
新采集或新配置版本产生新行，旧行永不改写。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

STILL_EXISTS = "still_exists"
SUPPORTS_RETIREMENT = "supports_retirement"
INSUFFICIENT = "insufficient_evidence"

CONCLUSION_LABELS = {
    STILL_EXISTS: "仍存在",
    SUPPORTS_RETIREMENT: "证据支持退役",
    INSUFFICIENT: "证据不足（暂缓）",
}

_STATUS_LABELS = {"complete": "完整", "restricted": "受限", "failed": "失败",
                  "uncollected": "未采集"}


class VerifyError(Exception):
    pass


def _latest_config_version(conn) -> str:
    row = conn.execute("SELECT version FROM configs ORDER BY rowid DESC LIMIT 1").fetchone()
    if not row:
        raise VerifyError("尚未导入任何采集配置")
    return row["version"]


def _column_section(conn, collection_id, object_key) -> dict:
    cl = conn.execute("SELECT status FROM column_lists WHERE collection_id=? AND"
                      " object_key=?", (collection_id, object_key)).fetchone()
    if cl is None:
        return {"status": "not_provided", "note": "本次采集未提供该对象的列清单"}
    items = [dict(r) for r in conn.execute(
        "SELECT col_number, col_name FROM column_items WHERE collection_id=? AND"
        " object_key=? ORDER BY col_number", (collection_id, object_key))]
    if cl["status"] != "complete":
        label = "受限" if cl["status"] == "restricted" else "读取失败"
        return {"status": cl["status"], "seen_columns": items,
                "note": f"列清单{label}：仅展示已读到的 {len(items)} 列，"
                        "不得据此断言任何列已删除；列采集问题绝不影响对象存在性判定"}
    prev = conn.execute(
        "SELECT cl.collection_id FROM column_lists cl"
        " JOIN collections c ON c.collection_id = cl.collection_id"
        " WHERE cl.object_key=? AND cl.status='complete' AND cl.collection_id<>?"
        " AND c.accepted=1"
        " ORDER BY c.collected_at DESC, cl.collection_id DESC LIMIT 1",
        (object_key, collection_id)).fetchone()
    section = {"status": "complete", "columns": items}
    if prev:
        old = {r["col_number"]: r["col_name"] for r in conn.execute(
            "SELECT col_number, col_name FROM column_items WHERE collection_id=? AND"
            " object_key=?", (prev["collection_id"], object_key))}
        new = {i["col_number"]: i["col_name"] for i in items}
        section["compared_with"] = prev["collection_id"]
        # 使用 JSON 原生结构（列表），保证新核验与历史读取的结果一致
        section["added"] = sorted([n, new[n]] for n in new.keys() - old.keys())
        section["removed"] = sorted([n, old[n]] for n in old.keys() - new.keys())
        section["renamed"] = sorted([n, old[n], new[n]]
                                    for n in old.keys() & new.keys()
                                    if old[n] != new[n])
        section["note"] = "列清单完整，已与上一次完整列清单按稳定列号比对"
    else:
        section["note"] = f"列清单完整（首次获得完整列清单，共 {len(items)} 列）"
    return section


def verify_application(conn: sqlite3.Connection, app_id: str, collection_id: str,
                       config_version: str | None = None,
                       commit: bool = True) -> dict:
    """核验单份申请。commit=False 时由调用方负责批量提交（用于批量核验）。"""
    if config_version is None:
        config_version = _latest_config_version(conn)
    existing = conn.execute(
        "SELECT * FROM verifications WHERE app_id=? AND collection_id=? AND"
        " config_version=?", (app_id, collection_id, config_version)).fetchone()
    if existing:
        return _row_to_result(existing, from_cache=True)

    app = conn.execute("SELECT * FROM applications WHERE app_id=?",
                       (app_id,)).fetchone()
    if not app:
        raise VerifyError(f"申请 {app_id} 不存在")
    coll = conn.execute("SELECT * FROM collections WHERE collection_id=?",
                        (collection_id,)).fetchone()
    if not coll:
        raise VerifyError(f"采集 {collection_id} 不存在")
    if not coll["accepted"]:
        raise VerifyError(f"采集 {collection_id} 已被拒绝（{coll['reject_reason']}），"
                          "不能用于核验")
    if not conn.execute("SELECT 1 FROM configs WHERE version=?",
                        (config_version,)).fetchone():
        raise VerifyError(f"配置版本 {config_version} 不存在")

    key = app["object_key"]
    notes = []

    # 实例代际观察（预聚合表，O(1)）
    gens = sorted(r["generation"] for r in conn.execute(
        "SELECT generation FROM collection_generations WHERE collection_id=? AND"
        " instance=?", (collection_id, app["instance"])))
    if gens and app["generation"] not in gens:
        notes.append(
            f"实例代际变化：采集 {collection_id} 中实例 {app['instance']} 仅见第 "
            f"{gens} 代，而申请身份属第 {app['generation']} 代；实例已重建，"
            "旧代际身份不可能迁移到新代际（这是实例重建，不是普通删除）")

    # 上一次核验（用于配置边界变化提示与“重新出现”提示）
    prev = conn.execute(
        "SELECT * FROM verifications WHERE app_id=? ORDER BY verif_id DESC LIMIT 1",
        (app_id,)).fetchone()
    if prev and prev["config_version"] != config_version:
        old = {(r["schema_name"], r["obj_type"]) for r in conn.execute(
            "SELECT schema_name, obj_type FROM config_scopes WHERE version=? AND"
            " obj_type=?", (prev["config_version"], app["obj_type"]))}
        new = {(r["schema_name"], r["obj_type"]) for r in conn.execute(
            "SELECT schema_name, obj_type FROM config_scopes WHERE version=? AND"
            " obj_type=?", (config_version, app["obj_type"]))}
        added = sorted(f"{s}/{t}" for s, t in new - old)
        removed = sorted(f"{s}/{t}" for s, t in old - new)
        notes.append(
            f"配置范围自上次核验（版本 {prev['config_version']}）起发生变化："
            f"新增 {added or '无'}，移除 {removed or '无'}。"
            "这是配置边界变化，不代表仓库中对象的实际变动")

    sightings = conn.execute(
        "SELECT * FROM sightings WHERE collection_id=? AND object_key=? AND"
        " cred_available=1", (collection_id, key)).fetchall()

    renamed = migrated = None
    columns = None
    if sightings:
        s = sightings[0]
        renamed = s["object_name"] != app["last_known_name"]
        migrated = s["schema_name"] != app["last_known_schema"]
        if prev and prev["conclusion"] == SUPPORTS_RETIREMENT:
            notes.append(
                f"原身份在上次核验（#{prev['verif_id']}，结论为“证据支持退役”）之后"
                "重新出现。按“同一代际内创建凭据不复用”的原则，这是同一对象持续存在，"
                "此前的缺席证据应视为有误；不得把它解释为先删除后重建")
        columns = _column_section(conn, collection_id, key)
        conclusion = STILL_EXISTS
        reason = ("在本次采集中找到与申请完全相同的身份"
                  "（实例+代际+类型+编号+创建凭据五项全等）")
        current_schema, current_name = s["schema_name"], s["object_name"]
        observations = [x["sighting_id"] for x in sightings]
        scopes, blockers = [], []
    else:
        current_schema = current_name = None
        observations = []
        # 同编号但身份不同的对象（含无凭据对象）——必须显式排除并留痕
        lookalikes = conn.execute(
            "SELECT sighting_id, object_key, cred_available, schema_name, object_name"
            " FROM sightings WHERE collection_id=? AND instance=? AND generation=?"
            " AND obj_type=? AND obj_number=? AND object_key<>?",
            (collection_id, app["instance"], app["generation"], app["obj_type"],
             app["obj_number"], key)).fetchall()
        for lk in lookalikes:
            if lk["cred_available"]:
                notes.append(
                    f"发现同编号同类型但创建凭据不同的对象（观察 "
                    f"#{lk['sighting_id']}，{lk['schema_name']}.{lk['object_name']}，"
                    f"身份 {lk['object_key']}）：按身份规则它不是申请对象，"
                    "既不能据此认定原对象仍存在，也不能据此认定原对象已删除")
            else:
                notes.append(
                    f"发现无法提供创建凭据的同编号对象（观察 #{lk['sighting_id']}，"
                    f"{lk['schema_name']}.{lk['object_name']}）：已保存该观察，"
                    "但不得仅凭名称或编号把它认作申请对象")
        # 必需范围 = 原登记范围 ∪ 配置声明的同类型范围
        required = [(app["last_known_schema"], app["obj_type"], "原登记范围")]
        seen = {(app["last_known_schema"], app["obj_type"])}
        for r in conn.execute(
                "SELECT schema_name, obj_type FROM config_scopes WHERE version=?"
                " AND obj_type=? ORDER BY schema_name",
                (config_version, app["obj_type"])):
            k = (r["schema_name"], r["obj_type"])
            if k in seen:
                required[0] = (required[0][0], required[0][1],
                               "原登记范围+配置声明范围")
            else:
                required.append((k[0], k[1], "配置声明范围"))
                seen.add(k)
        scopes, blockers = [], []
        for schema, otype, origin in required:
            row = conn.execute(
                "SELECT scope_id, status, detail FROM collection_scopes WHERE"
                " collection_id=? AND schema_name=? AND obj_type=?",
                (collection_id, schema, otype)).fetchone()
            if row is None:
                entry = {"schema": schema, "obj_type": otype, "origin": origin,
                         "status": "uncollected", "obs_id": None,
                         "detail": "本次采集未覆盖该范围"}
            else:
                entry = {"schema": schema, "obj_type": otype, "origin": origin,
                         "status": row["status"], "obs_id": row["scope_id"],
                         "detail": row["detail"]}
            scopes.append(entry)
            if entry["status"] != "complete":
                blockers.append(entry)
        if blockers:
            conclusion = INSUFFICIENT
            reason = ("申请身份未在任何范围出现，但存在未完整采集的范围；"
                      "受限/失败/未覆盖范围内的缺席不能当作不存在，必须暂缓")
        else:
            conclusion = SUPPORTS_RETIREMENT
            reason = ("申请身份未在任何范围出现，且原登记范围与配置声明的全部可能"
                      "迁移范围均已在本次采集中完整核实，身份在所有范围缺席")

    detail = {
        "object": {"object_key": key, "instance": app["instance"],
                   "generation": app["generation"], "obj_type": app["obj_type"],
                   "obj_number": app["obj_number"], "cred": app["cred"],
                   "last_known_schema": app["last_known_schema"],
                   "last_known_name": app["last_known_name"]},
        "conclusion": conclusion,
        "reason": reason,
        "current_location": ({"schema": current_schema, "name": current_name}
                             if current_schema else None),
        "renamed": renamed,
        "migrated": migrated,
        "observations": observations,
        "scopes": scopes,
        "blockers": blockers,
        "columns": columns,
        "notes": notes,
    }
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO verifications (app_id, collection_id, config_version,"
        " created_at, conclusion, current_schema, current_name, detail_json)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (app_id, collection_id, config_version, created_at, conclusion,
         current_schema, current_name, json.dumps(detail, ensure_ascii=False)))
    if commit:
        conn.commit()
    row = conn.execute("SELECT * FROM verifications WHERE verif_id=?",
                       (cur.lastrowid,)).fetchone()
    return _row_to_result(row, from_cache=False)


def _row_to_result(row, from_cache: bool) -> dict:
    detail = json.loads(row["detail_json"])
    return {"verif_id": row["verif_id"], "app_id": row["app_id"],
            "collection_id": row["collection_id"],
            "config_version": row["config_version"],
            "created_at": row["created_at"], "from_cache": from_cache, **detail}


def list_verifications(conn, app_id: str):
    return [_row_to_result(r, from_cache=True) for r in conn.execute(
        "SELECT * FROM verifications WHERE app_id=? ORDER BY verif_id", (app_id,))]


# ---------------------------------------------------------------- 展示

def format_result(r: dict) -> str:
    L = []
    cached = "（读取历史结果，未重新计算）" if r["from_cache"] else "（新核验，已存档）"
    L.append(f"核验 #{r['verif_id']}  申请 {r['app_id']}  采集 {r['collection_id']}"
             f"  配置 {r['config_version']}  {cached}")
    o = r["object"]
    L.append(f"  申请对象: {o['object_key']}   原位置 "
             f"{o['last_known_schema']}.{o['last_known_name']}")
    L.append(f"  结论: {CONCLUSION_LABELS[r['conclusion']]}")
    L.append(f"  依据: {r['reason']}")
    if r["current_location"]:
        loc = r["current_location"]
        flags = []
        if r["renamed"]:
            flags.append(f"改名 {o['last_known_name']} → {loc['name']}")
        if r["migrated"]:
            flags.append(f"迁移 {o['last_known_schema']} → {loc['schema']}")
        suffix = f"  （{'；'.join(flags)}）" if flags else "  （位置与名称均未变）"
        L.append(f"  当前位置: {loc['schema']}.{loc['name']}{suffix}")
        L.append(f"  存在性观察编号: {r['observations']}")
    if r["scopes"]:
        L.append("  相关范围采集状态:")
        for s in r["scopes"]:
            obs = f"观察#{s['obs_id']}" if s["obs_id"] is not None else "无观察记录"
            block = "  [阻断]" if s["status"] != "complete" else ""
            det = f"，{s['detail']}" if s.get("detail") else ""
            L.append(f"    - {s['schema']}/{s['obj_type']}（{s['origin']}）"
                     f" 状态={_STATUS_LABELS[s['status']]}（{obs}{det}）{block}")
    if r["blockers"]:
        L.append("  缺少的证据（阻断项）:")
        for b in r["blockers"]:
            L.append(f"    - {b['schema']}/{b['obj_type']} 范围为"
                     f"{_STATUS_LABELS[b['status']]}状态，需要一次完整采集")
    c = r.get("columns")
    if c:
        L.append(f"  列信息: {c['note']}")
        if c["status"] == "complete" and "added" in c:
            L.append(f"    新增列 {c['added'] or '无'}；删除列 {c['removed'] or '无'}；"
                     f"改名列 {c['renamed'] or '无'}（对比 {c['compared_with']}）")
        elif c["status"] in ("restricted", "failed"):
            L.append(f"    已见列: {[(x['col_number'], x['col_name']) for x in c['seen_columns']] or '无'}")
    for n in r["notes"]:
        L.append(f"  注意: {n}")
    return "\n".join(L)
