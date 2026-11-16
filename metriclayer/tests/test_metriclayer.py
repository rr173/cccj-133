"""Unit/integration tests for the metric layer (stdlib unittest only).

Run::

    python3 -I tests/test_metriclayer.py
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

from metriclayer.model import load_model, parse_model  # noqa: E402
from metriclayer.engine import build_database, execute  # noqa: E402
from metriclayer.compile import parse_query, compile_query, MISSING  # noqa: E402
from metriclayer.api import run_query, explain_query  # noqa: E402
from metriclayer.errors import (  # noqa: E402
    ModelError, DataError, CompileError,
)

MODEL = ROOT / "examples" / "model.json"
QDIR = ROOT / "examples" / "queries"
EXDATA = ROOT / "examples" / "data.json"


def q(name):
    return json.loads((QDIR / name).read_text(encoding="utf-8"))


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.model = load_model(MODEL)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "m.db"
        build_database(self.model, EXDATA, self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_int_stored_as_canonical_text_blob(self):
        # big integer must survive exactly, never coerced to REAL
        conn = sqlite3.connect(self.db)
        val = conn.execute(
            'SELECT big_amount FROM adjustments WHERE adjustment_id="adj1"'
        ).fetchone()[0]
        conn.close()
        self.assertEqual(
            val, "170141183460469231731687303715884105727"
        )
        self.assertIsInstance(val, str)

    def test_bool_stored_as_integer_0_1(self):
        conn = sqlite3.connect(self.db)
        rows = dict(conn.execute(
            "SELECT customer_id, is_gold FROM customers").fetchall())
        conn.close()
        self.assertEqual(rows, {"A": 1, "B": 0, "D": 0, "E": 0})

    def test_nullable_fk_kept_as_null(self):
        conn = sqlite3.connect(self.db)
        n = conn.execute(
            "SELECT COUNT(*) FROM shipments WHERE region_id IS NULL"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 4)  # shipments 3,4,5,6 have no region


class ScenarioTests(unittest.TestCase):
    """The headline scenario from the specification."""

    @classmethod
    def setUpClass(cls):
        cls.model = load_model(MODEL)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "m.db"
        build_database(cls.model, EXDATA, cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def query_map(self, query_doc):
        return run_query(self.model, self.db, query_doc).metric_map()

    def test_customer_totals(self):
        m = self.query_map(q("q1_customer_totals.json"))
        a, b = m[("A",)], m[("B",)]
        self.assertEqual(a["shipment_amount"], {"type": "int", "value": "20"})
        self.assertEqual(a["refund_amount"], {"type": "int", "value": "3"})
        self.assertEqual(a["refund_count"], {"type": "int", "value": "3"})
        self.assertEqual(b["shipment_amount"], {"type": "int", "value": "8"})
        self.assertIsNone(b["refund_amount"])          # sum -> null
        self.assertEqual(b["refund_count"],
                         {"type": "int", "value": "0"})  # count -> 0

    def test_existence_filter_does_not_fan_out(self):
        m = self.query_map(q("q2_existence_filter.json"))
        row = m[()]
        # 20, not 60: semi-join never joins refund rows to shipments
        self.assertEqual(row["shipment_amount"],
                         {"type": "int", "value": "20"})
        self.assertEqual(row["all_shipment_amount"],
                         {"type": "int", "value": "28"})  # ignores the filter

    def test_duplicate_equal_amounts_are_two_facts(self):
        m = self.query_map(q("q12_distinct.json"))
        a = m[("A",)]
        self.assertEqual(a["shipment_count"], {"type": "int", "value": "3"})
        self.assertEqual(a["shipment_amount_rows"],
                         {"type": "int", "value": "2"})      # null excluded
        self.assertEqual(a["distinct_shipment_amounts"],
                         {"type": "int", "value": "1"})      # {10}, not 2
        self.assertEqual(a["shipment_amount"],
                         {"type": "int", "value": "20"})     # 10+10, not 10

    def test_exact_fraction_derived_and_null_propagation(self):
        m = self.query_map(q("q1_customer_totals.json"))
        self.assertEqual(
            m[("A",)]["refund_ratio"],
            {"type": "fraction", "numerator": "3",
             "denominator": "20"},
        )
        self.assertIsNone(m[("B",)]["refund_ratio"])  # null operand

    def test_division_by_zero_is_null(self):
        # E has no shipments: in the unfiltered grid its shipment count is 0,
        # so shipment_amount_rows (the avg denominator) is 0 and the exact
        # fraction division yields null, never an exception.
        m = self.query_map(q("q1_customer_totals.json"))
        self.assertIn(("E",), m)
        self.assertIsNone(m[("E",)]["avg_shipment"])

    def test_big_integer_exact_sum(self):
        m = self.query_map(q("q6_bigint.json"))
        self.assertEqual(
            m[("A",)]["big_adjustment_total"],
            {"type": "int",
             "value": "340282366920938463463374607431768211454"},
        )
        self.assertIsNone(m[("B",)]["big_adjustment_total"])  # all null
        self.assertEqual(m[("B",)]["big_adjustment_rows"],
                         {"type": "int", "value": "1"})

    def test_region_cell_union_and_missing_marker(self):
        m = self.query_map(q("q3_region_union.json"))
        keys = {k[0] for k in m}
        self.assertIn("south", keys)           # only in refunds
        self.assertIn(MISSING, keys)
        south = m[("south",)]
        self.assertEqual(south["shipment_count"],
                         {"type": "int", "value": "0"})
        self.assertIsNone(south["shipment_amount"])
        self.assertEqual(south["refund_count"],
                         {"type": "int", "value": "1"})
        # missing-region cell: D's two null-amount shipments keep their row
        # multiplicity (2) while their sum contributes nothing; B's shipment
        # (8) and A's null shipment land here too -> 4 rows, sum 8
        missing = m[(MISSING,)]
        self.assertEqual(missing["shipment_count"],
                         {"type": "int", "value": "4"})
        self.assertEqual(missing["shipment_amount"],
                         {"type": "int", "value": "8"})
        # 'south' exists only through refunds; its distinct cell is still
        # produced because at least one metric has a contributing fact
        self.assertEqual(sorted(keys),
                         sorted(["east", "north", "south", MISSING]))

    def test_two_independent_existence_filters_different_rows(self):
        m = self.query_map(q("q4_two_existence.json"))
        # D only; r4 satisfies 'damaged', r5 satisfies 'zero' — different rows
        self.assertEqual(set(m), {("D",)})

    def test_same_row_and_gives_empty(self):
        m = self.query_map(q("q5_same_row_and.json"))
        self.assertEqual(m, {})

    def test_unreachable_dimension_rejected_before_execution(self):
        with self.assertRaises(CompileError):
            explain_query(self.model, q("q8_unreachable.json"))

    def test_named_role_path(self):
        m = self.query_map(q("q7_role_path.json"))
        # D's home_region is null -> missing marker cell exists
        missing_key = (MISSING,)
        self.assertIn(missing_key, m)
        # along the customer->home_region path D's shipments align to the
        # missing cell, but both of D's shipments have null amounts: rows are
        # still counted, the exact sum is null
        self.assertEqual(m[missing_key]["shipment_count"],
                         {"type": "int", "value": "2"})
        self.assertIsNone(m[missing_key]["shipment_amount"])
        # north = A (20) + B (8) reached through the *named* home_region role,
        # proving the dimension picks the explicit path rather than guessing
        self.assertEqual(m[("north",)]["shipment_amount"],
                         {"type": "int", "value": "28"})
        # east appears only via E's refund, with no shipment contribution
        self.assertEqual(m[("east",)]["shipment_count"],
                         {"type": "int", "value": "0"})
        self.assertIsNone(m[("east",)]["shipment_amount"])

    def test_entity_filter_with_bool_predicate(self):
        m = self.query_map(q("q9_entity_filter.json"))
        self.assertEqual(set(m), {("A",)})


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.model = load_model(MODEL)
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, data):
        p = Path(self.tmp.name) / "data.json"
        p.write_text(json.dumps(data))
        db = Path(self.tmp.name) / "m.db"
        return build_database(self.model, p, db)

    def base_data(self):
        return json.loads(EXDATA.read_text(encoding="utf-8"))

    def test_duplicate_primary_key_rejected(self):
        d = self.base_data()
        d["regions"].append(dict(d["regions"][0]))
        with self.assertRaises(DataError):
            self.build(d)

    def test_dangling_nonnull_reference_rejected(self):
        d = self.base_data()
        d["shipments"][0]["customer_id"] = "GHOST"
        with self.assertRaises(DataError):
            self.build(d)

    def test_nullable_dangling_null_allowed(self):
        d = self.base_data()
        d["shipments"][0]["region_id"] = None  # nullable -> missing member
        report = self.build(d)
        self.assertEqual(report["violations"], [])

    def test_bool_rejects_integer(self):
        d = self.base_data()
        d["customers"][0]["is_gold"] = 1
        with self.assertRaises(DataError):
            self.build(d)

    def test_int_rejects_noncanonical_text(self):
        d = self.base_data()
        d["shipments"][0]["amount"] = "10.0"
        with self.assertRaises(DataError):
            self.build(d)

    def test_int_accepts_canonical_decimal_text(self):
        d = self.base_data()
        d["shipments"][0]["amount"] = "99999999999999999999999999"
        report = self.build(d)
        self.assertEqual(report["violations"], [])

    def test_text_rejects_integer(self):
        d = self.base_data()
        d["customers"][0]["tier"] = 5
        with self.assertRaises(DataError):
            self.build(d)

    def test_text_rejects_reserved_missing_namespace(self):
        d = self.base_data()
        d["customers"][0]["tier"] = MISSING
        with self.assertRaises(DataError):
            self.build(d)

    def test_null_on_nonnullable_rejected(self):
        d = self.base_data()
        d["refunds"][0]["amount"] = None
        with self.assertRaises(DataError):
            self.build(d)

    def test_unknown_column_rejected(self):
        d = self.base_data()
        d["regions"][0]["bogus"] = 1
        with self.assertRaises(DataError):
            self.build(d)


class ModelRuleTests(unittest.TestCase):
    def test_cyclic_reference_graph_rejected(self):
        doc = json.loads(MODEL.read_text(encoding="utf-8"))
        # add a back reference from regions to customers -> cycle
        doc["tables"][0]["references"] = [{
            "column": "region_name", "target": "customers",
            "target_column": "customer_id", "role": "back"}]
        with self.assertRaises(ModelError):
            parse_model(doc)

    def test_whitelist_predicate_column_rejected(self):
        doc = json.loads(MODEL.read_text(encoding="utf-8"))
        doc["metrics"][0]["row_predicate"] = {
            "eq": {"column": "no_such_column", "value": 1}}
        with self.assertRaises(CompileError):
            parse_model(doc)

    def test_ambiguous_path_must_be_named(self):
        doc = json.loads(MODEL.read_text(encoding="utf-8"))
        # region dimension without explicit per-fact paths: shipments has two
        # ways to reach regions (direct 'region' and customer.home_region)
        for dim in doc["dimensions"]:
            if dim["name"] == "region":
                dim["paths"] = {}
        model = parse_model(doc)
        query = {"name": "x", "metrics": ["shipment_count"],
                 "dimensions": ["region"], "filters": []}
        with self.assertRaises(CompileError):
            compile_query(model, parse_query(query))

    def test_bool_eq_integer_rejected_at_model_level(self):
        doc = json.loads(MODEL.read_text(encoding="utf-8"))
        doc["metrics"][0]["row_predicate"] = {
            "eq": {"column": "priority", "value": 1}}  # int, not true
        with self.assertRaises(CompileError):
            parse_model(doc)

    def test_ordering_on_text_column_rejected(self):
        doc = json.loads(MODEL.read_text(encoding="utf-8"))
        doc["metrics"][0]["row_predicate"] = {
            "lt": {"column": "customer_id", "value": 5}}
        with self.assertRaises(CompileError):
            parse_model(doc)

    def test_fact_to_fact_reference_rejected(self):
        doc = json.loads(MODEL.read_text(encoding="utf-8"))
        doc["tables"][3]["references"].append({
            "column": "shipment_id", "target": "refunds",
            "target_column": "refund_id", "role": "refund"})
        # types differ (int vs int here) but target is a fact -> reject
        with self.assertRaises(ModelError):
            parse_model(doc)

    def test_unknown_metric_rejected(self):
        model = load_model(MODEL)
        with self.assertRaises(CompileError):
            compile_query(model, parse_query(
                {"name": "x", "metrics": ["nope"],
                 "dimensions": [], "filters": []}))

    def test_existence_filter_path_endpoint_mismatch_rejected(self):
        model = load_model(MODEL)
        query = {
            "name": "x", "metrics": ["big_adjustment_total"],
            "dimensions": [],
            "filters": [{
                "name": "wh", "type": "existence",
                "entity": "warehouses", "fact": "adjustments",
                "roles": ["customer"],
                "predicate": {"eq": {"column": "big_amount", "value": 1}}}],
        }
        with self.assertRaises(CompileError):
            compile_query(model, parse_query(query))

    def test_existence_filter_applied_via_local_semijoin_no_fact_join(self):
        # A filter defined on refunds must still constrain shipments, through
        # shipments' own customer path; the plan SQL must contain the refund
        # fact only inside an IN subquery, never as a joined table.
        model = load_model(MODEL)
        plan = compile_query(model, parse_query(q("q2_existence_filter.json")))
        ship = next(bp for bp in plan.base_plans
                    if bp.metric == "shipment_amount")
        self.assertNotIn('JOIN "refunds"', ship.sql)
        self.assertIn("IN (", ship.sql)
        self.assertIn('FROM "refunds"', ship.sql)
        # the ignored metric carries no filter at all
        ignored = next(bp for bp in plan.base_plans
                       if bp.metric == "all_shipment_amount")
        self.assertEqual(ignored.ignored_filters,
                         ["customers_with_positive_refunds"])
        self.assertNotIn("IN (", ignored.sql)

    def test_two_existence_filters_are_independent_subqueries(self):
        model = load_model(MODEL)
        plan = compile_query(model, parse_query(q("q4_two_existence.json")))
        bp = plan.base_plans[0]
        self.assertEqual(bp.sql.count("IN ("), 2)  # two independent semi-joins

    def test_multi_hop_existence_uses_named_local_paths(self):
        model = load_model(MODEL)
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "m.db"
        build_database(model, EXDATA, db)
        m = run_query(model, db,
                      q("q15_multi_hop_existence.json")).metric_map()
        # home_region north customers are A and B; D (null) and E (east) out
        self.assertEqual(sorted(m), [("A",), ("B",)])
        self.assertEqual(m[("B",)]["refund_count"],
                         {"type": "int", "value": "0"})
        plan = compile_query(model,
                             parse_query(q("q15_multi_hop_existence.json")))
        # the subquery walks both hops out of the filter's own fact
        sub_sql = plan.base_plans[0].sql
        self.assertIn('LEFT JOIN "customers" sf__customer', sub_sql)
        self.assertIn('LEFT JOIN "regions" sf__customer__home_region',
                      sub_sql)
        tmp.cleanup()

    def test_multi_hop_existence_without_local_paths_is_ambiguous(self):
        model = load_model(MODEL)
        doc = q("q15_multi_hop_existence.json")
        del doc["filters"][0]["local_paths"]
        with self.assertRaises(CompileError):
            compile_query(model, parse_query(doc))

    def test_missing_marker_is_structurally_distinct_from_text(self):
        # output JSON renders missing members as an object, never as the raw
        # reserved string a text value could collide with
        model = load_model(MODEL)
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "m.db"
        build_database(model, EXDATA, db)
        payload = run_query(model, db, q("q3_region_union.json")).to_dict()
        dim_values = [d for row in payload["rows"] for d in row["dimensions"]]
        self.assertIn({"__missing__": True}, dim_values)
        self.assertNotIn("__metriclayer__::missing_member__", dim_values)
        tmp.cleanup()


class ReadViewTests(unittest.TestCase):
    def test_single_snapshot_uses_read_only_uri(self):
        model = load_model(MODEL)
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "m.db"
        build_database(model, EXDATA, db)
        result = run_query(model, db, q("q1_customer_totals.json"))
        self.assertIn("single SQLite read transaction",
                      result.plan.read_view)
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
