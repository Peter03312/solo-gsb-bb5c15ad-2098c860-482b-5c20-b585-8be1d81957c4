"""服务层测试：不依赖 HTTP，直接用临时 SQLite 库验证领域规则。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest

from app.db import connect, init_db
from app.errors import ApiError
from app import service
from app.timeutil import canonical
from datetime import datetime, timezone


def iso(y, mo=1, d=1, h=0, mi=0, s=0):
    return canonical(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc))


FUTURE = iso(3000, 1, 1)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        init_db(self.db_path)
        self.conn = connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def initial(self, no="SOP-001", start=(2026, 1, 1), end=(2026, 12, 31), body="v1 正文"):
        payload = {
            "procedure_no": no,
            "valid_start": iso(*start),
            "valid_end": iso(*end),
            "body": body,
        }
        return service.publish_initial(self.conn, payload)

    # ---------- 首次发布

    def test_initial_and_query(self):
        v = self.initial()
        self.assertEqual(v.version_no, 1)
        self.assertIsNotNone(v.published_at)
        hit = service.find_effective(self.conn, "SOP-001", iso(2026, 6, 1), FUTURE)
        self.assertEqual(hit.body, "v1 正文")

    def test_duplicate_no_rejected_and_untouched(self):
        self.initial()
        with self.assertRaises(ApiError) as cm:
            self.initial(body="另一个正文")
        self.assertEqual(cm.exception.code, "DUPLICATE_PROCEDURE")
        # 历史未被改写
        versions = service.list_versions(self.conn, "SOP-001")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].body, "v1 正文")

    def test_illegal_interval_initial(self):
        with self.assertRaises(ApiError) as cm:
            self.initial(start=(2026, 12, 31), end=(2026, 1, 1))
        self.assertEqual(cm.exception.code, "ILLEGAL_INTERVAL")

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(ApiError):
            service.publish_initial(self.conn, {
                "procedure_no": "X", "valid_start": "2026-01-01T00:00:00",
                "valid_end": "2026-12-31T00:00:00", "body": "b"})

    # ---------- 更正与回溯

    def test_correction_split_and_bitemporal(self):
        v1 = self.initial()
        t0 = v1.published_at  # 首版发布时刻

        correction = {
            "procedure_no": "SOP-001",
            "supersedes_version": 1,
            "valid_start": iso(2026, 3, 1),
            "valid_end": iso(2026, 4, 1),
            "body": "v2 三月修订",
        }
        v2 = service.publish_corrections(self.conn, correction)
        self.assertEqual(v2.version_no, 2)
        self.assertEqual(v2.supersedes_version, 1)
        self.assertGreater(v2.published_at, t0)

        # 知悉时刻 = 首版发布当时（更正尚未发布）→ 任何作业时刻都仍是 v1
        pre = service.find_effective(self.conn, "SOP-001", iso(2026, 3, 15), t0)
        self.assertEqual(pre.version_no, 1)
        self.assertEqual(pre.body, "v1 正文")

        # 完全知悉后：子区间内 v2，子区间外仍是 v1
        inside = service.find_effective(self.conn, "SOP-001", iso(2026, 3, 15), FUTURE)
        self.assertEqual(inside.version_no, 2)
        before = service.find_effective(self.conn, "SOP-001", iso(2026, 2, 15), FUTURE)
        self.assertEqual(before.version_no, 1)
        after = service.find_effective(self.conn, "SOP-001", iso(2026, 4, 1), FUTURE)
        # 端点相接：4/1 00:00 已落在更正区间之外
        self.assertEqual(after.version_no, 1)

    def test_correction_outside_base_rejected(self):
        self.initial()
        with self.assertRaises(ApiError) as cm:
            service.publish_corrections(self.conn, {
                "procedure_no": "SOP-001", "supersedes_version": 1,
                "valid_start": iso(2025, 12, 1), "valid_end": iso(2026, 1, 2),
                "body": "越界"})
        self.assertEqual(cm.exception.code, "ILLEGAL_INTERVAL")
        self.assertEqual(len(service.list_versions(self.conn, "SOP-001")), 1)

    def test_touching_endpoints_allowed(self):
        self.initial()
        a = service.publish_corrections(self.conn, {
            "procedure_no": "SOP-001", "supersedes_version": 1,
            "valid_start": iso(2026, 3, 1), "valid_end": iso(2026, 4, 1), "body": "v2"})
        b = service.publish_corrections(self.conn, {
            "procedure_no": "SOP-001", "supersedes_version": 1,
            "valid_start": iso(2026, 4, 1), "valid_end": iso(2026, 5, 1), "body": "v3"})
        self.assertEqual([a.version_no, b.version_no], [2, 3])
        self.assertEqual(service.find_effective(self.conn, "SOP-001", iso(2026, 4, 1), FUTURE).body, "v3")

    def test_overlapping_siblings_rejected(self):
        self.initial()
        service.publish_corrections(self.conn, {
            "procedure_no": "SOP-001", "supersedes_version": 1,
            "valid_start": iso(2026, 3, 1), "valid_end": iso(2026, 5, 1), "body": "v2"})
        with self.assertRaises(ApiError) as cm:
            service.publish_corrections(self.conn, {
                "procedure_no": "SOP-001", "supersedes_version": 1,
                "valid_start": iso(2026, 4, 1), "valid_end": iso(2026, 6, 1), "body": "v3"})
        self.assertEqual(cm.exception.code, "CONCURRENT_CORRECTION")
        self.assertEqual(len(service.list_versions(self.conn, "SOP-001")), 2)

    def test_nested_correction_and_expired_base(self):
        self.initial()
        service.publish_corrections(self.conn, {
            "procedure_no": "SOP-001", "supersedes_version": 1,
            "valid_start": iso(2026, 3, 1), "valid_end": iso(2026, 4, 1), "body": "v2"})
        # 对 v2 再更正月内子区间 → v3
        service.publish_corrections(self.conn, {
            "procedure_no": "SOP-001", "supersedes_version": 2,
            "valid_start": iso(2026, 3, 10), "valid_end": iso(2026, 3, 20), "body": "v3"})
        hit = service.find_effective(self.conn, "SOP-001", iso(2026, 3, 15), FUTURE)
        self.assertEqual(hit.version_no, 3)
        edge = service.find_effective(self.conn, "SOP-001", iso(2026, 3, 5), FUTURE)
        self.assertEqual(edge.version_no, 2)

        # 再想基于 v1 更正已被 v2 占用的子区间 → 与并发更正冲突
        with self.assertRaises(ApiError) as cm:
            service.publish_corrections(self.conn, {
                "procedure_no": "SOP-001", "supersedes_version": 1,
                "valid_start": iso(2026, 3, 1), "valid_end": iso(2026, 4, 1), "body": "v?"})
        self.assertEqual(cm.exception.code, "CONCURRENT_CORRECTION")
        # 但基版在其未被覆盖的剩余区间仍可更正
        ok = service.publish_corrections(self.conn, {
            "procedure_no": "SOP-001", "supersedes_version": 1,
            "valid_start": iso(2026, 6, 1), "valid_end": iso(2026, 6, 2), "body": "v4"})
        self.assertEqual(ok.version_no, 4)

    def test_expired_base_after_full_coverage(self):
        self.initial()
        # v2、v3 两条同基版更正恰好拼满 3 月整月
        service.publish_corrections(self.conn, {
            "procedure_no": "SOP-001", "supersedes_version": 1,
            "valid_start": iso(2026, 3, 1), "valid_end": iso(2026, 3, 16), "body": "v2"})
        service.publish_corrections(self.conn, {
            "procedure_no": "SOP-001", "supersedes_version": 1,
            "valid_start": iso(2026, 3, 16), "valid_end": iso(2026, 4, 1), "body": "v3"})
        # v2 本身也已被 v4 耗尽
        service.publish_corrections(self.conn, {
            "procedure_no": "SOP-001", "supersedes_version": 2,
            "valid_start": iso(2026, 3, 1), "valid_end": iso(2026, 3, 16), "body": "v4"})
        with self.assertRaises(ApiError) as cm:
            service.publish_corrections(self.conn, {
                "procedure_no": "SOP-001", "supersedes_version": 2,
                "valid_start": iso(2026, 3, 5), "valid_end": iso(2026, 3, 6), "body": "过期"})
        self.assertEqual(cm.exception.code, "BASE_EXPIRED")

    def test_unknown_procedure_and_base(self):
        self.initial()
        with self.assertRaises(ApiError) as cm:
            service.publish_corrections(self.conn, {
                "procedure_no": "NOPE", "supersedes_version": 1,
                "valid_start": iso(2026, 3, 1), "valid_end": iso(2026, 4, 1), "body": "x"})
        self.assertEqual(cm.exception.code, "PROCEDURE_NOT_FOUND")
        with self.assertRaises(ApiError) as cm:
            service.publish_corrections(self.conn, {
                "procedure_no": "SOP-001", "supersedes_version": 9,
                "valid_start": iso(2026, 3, 1), "valid_end": iso(2026, 4, 1), "body": "x"})
        self.assertEqual(cm.exception.code, "BASE_VERSION_NOT_FOUND")

    # ---------- 批量并发更正：整体拒绝

    def test_batch_atomic_success_and_conflict(self):
        self.initial()
        ok = service.publish_corrections(self.conn, [
            {"procedure_no": "SOP-001", "supersedes_version": 1,
             "valid_start": iso(2026, 2, 1), "valid_end": iso(2026, 3, 1), "body": "v2"},
            {"procedure_no": "SOP-001", "supersedes_version": 1,
             "valid_start": iso(2026, 3, 1), "valid_end": iso(2026, 4, 1), "body": "v3"},
        ])
        self.assertEqual(len(ok), 2)

        with self.assertRaises(ApiError) as cm:
            service.publish_corrections(self.conn, [
                {"procedure_no": "SOP-001", "supersedes_version": 1,
                 "valid_start": iso(2026, 5, 1), "valid_end": iso(2026, 6, 1), "body": "v4"},
                {"procedure_no": "SOP-001", "supersedes_version": 1,
                 "valid_start": iso(2026, 5, 15), "valid_end": iso(2026, 5, 20), "body": "v5"},
            ])
        self.assertEqual(cm.exception.code, "CONCURRENT_CORRECTION")
        # 整批未落库
        self.assertEqual(len(service.list_versions(self.conn, "SOP-001")), 3)

    def test_batch_bad_item_rolls_all_back(self):
        self.initial()
        with self.assertRaises(ApiError):
            service.publish_corrections(self.conn, [
                {"procedure_no": "SOP-001", "supersedes_version": 1,
                 "valid_start": iso(2026, 5, 1), "valid_end": iso(2026, 6, 1), "body": "v2"},
                {"procedure_no": "SOP-001", "supersedes_version": 1,
                 "valid_start": iso(2026, 6, 1), "valid_end": iso(2026, 5, 1), "body": "非法"},
            ])
        self.assertEqual(len(service.list_versions(self.conn, "SOP-001")), 1)

    # ---------- 无匹配 / 跨规程

    def test_no_match_empty(self):
        self.initial()
        # 作业时刻在适用区间之外
        self.assertIsNone(service.find_effective(self.conn, "SOP-001", iso(2030, 1, 1), FUTURE))
        # 知悉时刻早于首版发布
        self.assertIsNone(service.find_effective(
            self.conn, "SOP-001", iso(2026, 6, 1), iso(2000, 1, 1)))

    def test_effective_all_groups_and_empty(self):
        self.initial(no="SOP-A", body="A1")
        self.initial(no="SOP-B", start=(2027, 1, 1), end=(2028, 1, 1), body="B1")
        rows = service.list_effective_all(self.conn, iso(2026, 6, 1), FUTURE)
        self.assertEqual([v.procedure_no for v in rows], ["SOP-A"])
        self.assertEqual(service.list_effective_all(self.conn, iso(2029, 1, 1), FUTURE), [])

    # ---------- 真实并发：两个线程同时更正同一基版同一子区间

    def test_concurrent_threads_one_loses(self):
        self.initial()
        errors = []
        winners = []

        def worker(body):
            # 每个线程使用独立连接（模拟两个 HTTP 请求）
            conn = connect(self.db_path)
            try:
                v = service.publish_corrections(conn, {
                    "procedure_no": "SOP-001", "supersedes_version": 1,
                    "valid_start": iso(2026, 7, 1), "valid_end": iso(2026, 8, 1),
                    "body": body})
                winners.append(v.body)
            except ApiError as e:
                errors.append(e.code)
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=("线程A",))
        t2 = threading.Thread(target=worker, args=("线程B",))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(len(winners), 1)
        self.assertEqual(errors, ["CONCURRENT_CORRECTION"])
        self.assertEqual(len(service.list_versions(self.conn, "SOP-001")), 2)


if __name__ == "__main__":
    unittest.main()
