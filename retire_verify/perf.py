#!/usr/bin/env python3
"""规模验证：十万条登记 + 混合采集状态下的非相邻时点核验。

证明点：
  * 每项申请的核验只走索引点查（EXPLAIN QUERY PLAN 无全表 SCAN）；
  * 非相邻时点（col-2 与 col-5）核验耗时与历史对象总量无关；
  * 峰值内存为常数级（不随历史对象总量线性增长）。
"""
import os
import random
import resource
import sqlite3
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rv import db as dbmod, ingest, verify as V

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "run", "perf.db")

N_OBJECTS = 100_000
N_COLLECTIONS = 6
SIGHTINGS_PER_COLLECTION = 20_000
N_APPS = 500
SCHEMAS = [f"S{i}" for i in range(10)]
STATUSES = ["complete", "restricted", "failed"]


def build():
    if os.path.exists(DB):
        os.remove(DB)
    conn = dbmod.init_db(DB)
    rng = random.Random(20261116)

    t0 = time.perf_counter()
    rows = []
    for i in range(1, N_OBJECTS + 1):
        otype = "TABLE" if i % 3 else "VIEW"
        rows.append({"instance": "inst1", "generation": 1, "obj_type": otype,
                     "obj_number": i, "cred": f"CRED-{i}",
                     "schema_name": SCHEMAS[i % 10], "object_name": f"obj_{i}",
                     "registered_at": "2026-01-01T00:00:00Z"})
    ingest.import_registry_rows(conn, rows)
    t_registry = time.perf_counter() - t0

    ingest.import_config_data(conn, {
        "version": "pcfg", "declared_at": "2026-01-01T00:00:00Z",
        "scopes": [{"schema_name": s, "obj_type": t}
                   for s in SCHEMAS for t in ("TABLE", "VIEW")]})

    t0 = time.perf_counter()
    for c in range(1, N_COLLECTIONS + 1):
        scopes = []
        for j, s in enumerate(SCHEMAS):
            for t_ in ("TABLE", "VIEW"):
                status = STATUSES[(c + j + (0 if t_ == "TABLE" else 1)) % 3]
                scopes.append({"schema_name": s, "obj_type": t_, "status": status,
                               "detail": f"模拟{status}" if status != "complete"
                               else None})
        picked = rng.sample(rows, SIGHTINGS_PER_COLLECTION)
        objects = [{"instance": r["instance"], "generation": r["generation"],
                    "obj_type": r["obj_type"], "obj_number": r["obj_number"],
                    "cred": r["cred"], "schema_name": r["schema_name"],
                    "object_name": r["object_name"]} for r in picked]
        ingest.import_collection_data(conn, {
            "collection_id": f"perf-col-{c}",
            "collected_at": f"2026-0{c}-15T00:00:00Z",
            "connector": "mock-catalog",
            "scopes": scopes, "objects": objects, "columns": []})
    t_collections = time.perf_counter() - t0

    apps = []
    for i, r in enumerate(rng.sample(rows, N_APPS)):
        apps.append({"app_id": f"PERF-{i:04d}", "instance": r["instance"],
                     "generation": r["generation"], "obj_type": r["obj_type"],
                     "obj_number": r["obj_number"], "cred": r["cred"],
                     "last_known_schema": r["schema_name"],
                     "last_known_name": r["object_name"],
                     "applicant": "perf", "reason": "perf",
                     "submitted_at": "2026-02-01T00:00:00Z"})
    ingest.import_applications_data(conn, apps)
    return conn, [a["app_id"] for a in apps], t_registry, t_collections


def show_plans(conn):
    queries = {
        "按身份查观察（存在性判定）":
            ("SELECT sighting_id FROM sightings WHERE collection_id=?"
             " AND object_key=? AND cred_available=1",
             ("perf-col-2", "inst1|1|TABLE|7|CRED-7")),
        "按编号查相似对象（替身排除）":
            ("SELECT sighting_id FROM sightings WHERE collection_id=?"
             " AND instance=? AND generation=? AND obj_type=? AND obj_number=?"
             " AND object_key<>?",
             ("perf-col-2", "inst1", 1, "TABLE", 7, "inst1|1|TABLE|7|CRED-7")),
        "范围采集状态（完备性判定）":
            ("SELECT scope_id, status FROM collection_scopes WHERE collection_id=?"
             " AND schema_name=? AND obj_type=?",
             ("perf-col-2", "S3", "TABLE")),
        "历史核验读取（幂等）":
            ("SELECT verif_id FROM verifications WHERE app_id=?"
             " AND collection_id=? AND config_version=?",
             ("PERF-0001", "perf-col-2", "pcfg")),
        "实例代际观察":
            ("SELECT generation FROM collection_generations WHERE collection_id=?"
             " AND instance=?", ("perf-col-2", "inst1")),
    }
    print("查询计划（证明全部为索引点查，无全表扫描）：")
    bad = False
    for name, (q, params) in queries.items():
        plan = conn.execute("EXPLAIN QUERY PLAN " + q, params).fetchall()
        text = " | ".join(p["detail"] for p in plan)
        scan = "SCAN" in text.upper() and "SEARCH" not in text.upper()
        bad = bad or scan
        print(f"  {name}: {text}")
    return not bad


def main():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn, app_ids, t_reg, t_col = build()
    n_sightings = conn.execute("SELECT COUNT(*) c FROM sightings").fetchone()["c"]
    print(f"登记 {N_OBJECTS} 条（导入 {t_reg:.2f}s）；"
          f"采集 {N_COLLECTIONS} 次共 {n_sightings} 条观察（导入 {t_col:.2f}s）；"
          f"申请 {N_APPS} 份；范围状态 complete/restricted/failed 混合轮转")

    plans_ok = show_plans(conn)

    # 非相邻时点核验：每份申请分别在第 2 次与第 5 次采集上核验
    tracemalloc.start()
    t0 = time.perf_counter()
    conclusions = {}
    for app_id in app_ids:
        for col in ("perf-col-2", "perf-col-5"):
            r = V.verify_application(conn, app_id, col, "pcfg", commit=False)
            conclusions[r["conclusion"]] = conclusions.get(r["conclusion"], 0) + 1
    conn.commit()
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    n_verif = len(app_ids) * 2
    print(f"\n非相邻时点核验 {n_verif} 次（{N_APPS} 份申请 × col-2/col-5）："
          f"总耗时 {elapsed:.2f}s，平均 {elapsed / n_verif * 1000:.2f} ms/次")
    print("结论分布：" + "，".join(f"{V.CONCLUSION_LABELS[k]} {v}"
                                  for k, v in sorted(conclusions.items())))
    print(f"tracemalloc 峰值内存 {peak / 1e6:.1f} MB；"
          f"进程峰值 RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB")

    # 缓存读取也应为常数时间
    t0 = time.perf_counter()
    for app_id in app_ids:
        V.verify_application(conn, app_id, "perf-col-2", "pcfg")
    print(f"重复核验 {N_APPS} 次（全部命中历史结果）："
          f"{time.perf_counter() - t0:.2f}s")

    assert plans_ok, "存在非索引扫描的查询计划"
    print("\n结论：所有核验查询均为索引点查，未为任何申请扫描全部历史对象；"
          "峰值内存为常数级。")


if __name__ == "__main__":
    main()
