#!/usr/bin/env python3
"""命令行入口：导入登记/申请/配置/采集，运行核验，查询单份申请依据。

用法示例：
  python3 cli.py init run/demo.db
  python3 cli.py import-registry run/demo.db samples/registry.csv
  python3 cli.py import-applications run/demo.db samples/applications.json
  python3 cli.py import-config run/demo.db samples/config_v1.json
  python3 cli.py import-collection run/demo.db samples/col1_a_complete_b_restricted.json
  python3 cli.py verify run/demo.db --app APP-001 --collection col-1
  python3 cli.py show run/demo.db --app APP-001
  python3 cli.py check run/demo.db --app APP-001 --collection col-1
"""
from __future__ import annotations

import argparse
import sys

from rv import checker
from rv import db as dbmod
from rv import ingest
from rv import verify as V


def cmd_init(a):
    dbmod.init_db(a.db).close()
    print(f"已初始化数据库 {a.db}")


def cmd_import_registry(a):
    conn = dbmod.connect(a.db)
    n = ingest.import_registry(conn, a.file)
    print(f"登记导入完成：新增 {n} 条对象登记")


def cmd_import_applications(a):
    conn = dbmod.connect(a.db)
    n = ingest.import_applications(conn, a.file)
    print(f"申请导入完成：新增 {n} 份退役申请")


def cmd_import_config(a):
    conn = dbmod.connect(a.db)
    ingest.import_config(conn, a.file)
    print(f"配置导入完成：{a.file}")


def cmd_import_collection(a):
    conn = dbmod.connect(a.db)
    res = ingest.import_collection(conn, a.file)
    if res["accepted"]:
        print(f"采集 {res['collection_id']} 已接受：范围 {res['scopes']} 个，"
              f"对象 {res['objects']} 个，列清单 {res['columns']} 份")
    else:
        print(f"采集 {res['collection_id']} 被拒绝，不得用于退役核验：")
        for p in res["problems"]:
            print(f"  - {p}")
        sys.exit(3)


def cmd_verify(a):
    conn = dbmod.connect(a.db)
    try:
        r = V.verify_application(conn, a.app, a.collection, a.config)
    except V.VerifyError as e:
        print(f"核验失败：{e}")
        sys.exit(2)
    print(V.format_result(r))


def cmd_verify_all(a):
    conn = dbmod.connect(a.db)
    apps = [r["app_id"] for r in
            conn.execute("SELECT app_id FROM applications ORDER BY app_id")]
    counts = {}
    for app_id in apps:
        try:
            r = V.verify_application(conn, app_id, a.collection, a.config)
        except V.VerifyError as e:
            print(f"申请 {app_id}：核验失败：{e}")
            continue
        counts[r["conclusion"]] = counts.get(r["conclusion"], 0) + 1
        print(V.format_result(r))
        print("-" * 72)
    print("汇总：" + "，".join(f"{V.CONCLUSION_LABELS[k]} {v} 份"
                              for k, v in sorted(counts.items())))


def cmd_show(a):
    conn = dbmod.connect(a.db)
    rows = V.list_verifications(conn, a.app)
    if not rows:
        print(f"申请 {a.app} 尚无核验记录")
        return
    print(f"申请 {a.app} 的核验历史（{len(rows)} 条，只增不改）：")
    for r in rows:
        print(V.format_result(r))
        print("-" * 72)


def cmd_check(a):
    conn = dbmod.connect(a.db)
    c = checker.check_application(conn, a.app, a.collection)
    print(checker.format_check(c))
    if not c["match"]:
        sys.exit(4)


def cmd_list_collections(a):
    conn = dbmod.connect(a.db)
    for r in conn.execute("SELECT * FROM collections ORDER BY collected_at,"
                          " collection_id"):
        state = "接受" if r["accepted"] else f"拒绝（{r['reject_reason']}）"
        print(f"  {r['collection_id']}  {r['collected_at']}  {state}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="retire-verify", description="离线退役核验工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args):
        sp = sub.add_parser(name)
        for args_, kwargs in args:
            sp.add_argument(*args_, **kwargs)
        sp.set_defaults(fn=fn)

    add("init", cmd_init, (("db",), {}))
    add("import-registry", cmd_import_registry, (("db",), {}), (("file",), {}))
    add("import-applications", cmd_import_applications, (("db",), {}), (("file",), {}))
    add("import-config", cmd_import_config, (("db",), {}), (("file",), {}))
    add("import-collection", cmd_import_collection, (("db",), {}), (("file",), {}))
    add("verify", cmd_verify, (("db",), {}), (("--app",), {"required": True}),
        (("--collection",), {"required": True}), (("--config",), {}))
    add("verify-all", cmd_verify_all, (("db",), {}),
        (("--collection",), {"required": True}), (("--config",), {}))
    add("show", cmd_show, (("db",), {}), (("--app",), {"required": True}))
    add("check", cmd_check, (("db",), {}), (("--app",), {"required": True}),
        (("--collection",), {"required": True}))
    add("list-collections", cmd_list_collections, (("db",), {}))

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except ingest.IngestError as e:
        print(f"导入失败：{e}")
        sys.exit(2)
    except ValueError as e:
        print(str(e))
        sys.exit(2)


if __name__ == "__main__":
    main()
