"""Тесты. Эталоны — CONTROL_SUMS.md. Запуск: python3 -m unittest discover -s tests -v"""
import csv
import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import report  # noqa: E402

DATA = ROOT / "data"
R = 100  # рубль в копейках


def run(d: date, data_dir: Path = DATA):
    return {c.contract.contract_id: c for c in report.build_report(report.load(data_dir), d)}


def works(cl):
    return {w.work.work_id: (w.status, w.paid, w.remaining) for w in cl.works}


def totals(cl):
    return dict(payable=cl.payable, earned=cl.earned, net=cl.net, works_paid=cl.works_paid,
                overpaid=cl.overpaid, advance=cl.advance, remaining=cl.remaining,
                queued=cl.queued, after_queue=cl.remaining_after_queue,
                cancelled=cl.cancelled)


class InvariantsMixin:
    def assertInvariants(self, cl):
        self.assertEqual(cl.works_paid + cl.overpaid, cl.net)
        self.assertEqual(cl.net + cl.remaining, cl.payable + cl.overpaid)


class ControlDates(unittest.TestCase, InvariantsMixin):
    def test_2026_11_10(self):
        r = run(date(2026, 11, 10))
        self.assertEqual(works(r["D-1"]), {
            "W-11": ("done", 100_000 * R, 0),
            "W-12": ("done", 100_000 * R, 0),
            "W-13": ("cancelled", 0, 0),
            "W-14": ("done", 100_000 * R, 0),
            "W-15": ("planned", 15_000 * R, 85_000 * R),
            "W-16": ("planned", 0, 100_000 * R),
        })
        self.assertEqual(works(r["D-2"]), {
            "W-21": ("done", 20_000 * R, 0),
            "W-22": ("done", 30_000 * R, 0),
            "W-23": ("planned", 25_000 * R, 15_000 * R),
        })
        self.assertEqual(totals(r["D-1"]), dict(
            payable=500_000 * R, earned=300_000 * R, net=315_000 * R, works_paid=315_000 * R,
            overpaid=0, advance=15_000 * R, remaining=185_000 * R, queued=150_000 * R,
            after_queue=35_000 * R, cancelled=100_000 * R))
        self.assertEqual(totals(r["D-2"]), dict(
            payable=90_000 * R, earned=50_000 * R, net=75_000 * R, works_paid=75_000 * R,
            overpaid=0, advance=25_000 * R, remaining=15_000 * R, queued=0,
            after_queue=15_000 * R, cancelled=0))
        for cl in r.values():
            self.assertInvariants(cl)

    def test_2026_11_12_overpayment(self):
        r = run(date(2026, 11, 12))
        self.assertEqual(totals(r["D-1"]), totals(run(date(2026, 11, 10))["D-1"]))
        self.assertEqual(works(r["D-2"]), {
            "W-21": ("done", 20_000 * R, 0),
            "W-22": ("done", 30_000 * R, 0),
            "W-23": ("done", 40_000 * R, 0),
        })
        d2 = r["D-2"]
        # по работам 90 000 + переплата 5 000 = выплачено 95 000; Σ paid != net — ожидаемо
        self.assertEqual(d2.net, 95_000 * R)
        self.assertEqual(d2.works_paid, 90_000 * R)
        self.assertEqual(d2.overpaid, 5_000 * R)
        self.assertEqual(d2.earned, 90_000 * R)
        self.assertEqual(d2.advance, 0)
        self.assertEqual(d2.remaining, 0)
        for cl in r.values():
            self.assertInvariants(cl)

    def test_2026_09_05_between_payment_and_refund(self):
        r = run(date(2026, 9, 5))
        self.assertEqual(set(r), {"D-1"})  # D-2 подписан 20.09
        self.assertEqual(works(r["D-1"]), {
            "W-11": ("planned", 100_000 * R, 0),
            "W-12": ("planned", 100_000 * R, 0),
            "W-13": ("planned", 100_000 * R, 0),
            "W-14": ("planned", 50_000 * R, 50_000 * R),
            "W-15": ("planned", 0, 100_000 * R),
            "W-16": ("planned", 0, 100_000 * R),
        })
        d1 = r["D-1"]
        self.assertEqual((d1.payable, d1.earned, d1.net, d1.overpaid, d1.advance, d1.remaining, d1.queued),
                         (600_000 * R, 0, 350_000 * R, 0, 350_000 * R, 250_000 * R, 0))
        self.assertInvariants(d1)


class DayBoundary(unittest.TestCase):
    """Событие в дату отчёта учитывается (решение 2)."""

    def test_refund_on_report_date(self):
        self.assertEqual(run(date(2026, 9, 9))["D-1"].net, 350_000 * R)
        self.assertEqual(run(date(2026, 9, 10))["D-1"].net, 300_000 * R)

    def test_cancellation_on_report_date(self):
        st = lambda d: works(run(d)["D-1"])["W-13"][0]
        self.assertEqual(st(date(2026, 10, 11)), "planned")
        self.assertEqual(st(date(2026, 10, 12)), "cancelled")

    def test_completion_on_report_date(self):
        st = lambda d: works(run(d)["D-2"])["W-23"][0]
        self.assertEqual(st(date(2026, 11, 10)), "planned")
        self.assertEqual(st(date(2026, 11, 11)), "done")

    def test_contract_signed_on_report_date(self):
        self.assertNotIn("D-2", run(date(2026, 9, 19)))
        self.assertIn("D-2", run(date(2026, 9, 20)))

    def test_queue_starts_on_created_at(self):
        self.assertEqual(run(date(2026, 10, 31))["D-1"].queued, 0)
        self.assertEqual(run(date(2026, 11, 1))["D-1"].queued, 150_000 * R)


class AllDatesInvariants(unittest.TestCase, InvariantsMixin):
    def test_every_day_aug_to_dec(self):
        data = report.load(DATA)
        d = date(2026, 8, 1)
        while d <= date(2026, 12, 31):
            for cl in report.build_report(data, d):
                with self.subTest(date=d, contract=cl.contract.contract_id):
                    self.assertInvariants(cl)
                    for w in cl.works:
                        self.assertGreaterEqual(w.remaining, 0)
            d = d.fromordinal(d.toordinal() + 1)


# ------------------------------------------------------------- синтетика

CONTRACTS_H = ["contract_id", "contractor", "amount", "signed_at"]
WORKS_H = ["work_id", "contract_id", "title", "price", "status", "completed_at", "cancelled_at"]
PAYMENTS_H = ["payment_id", "contract_id", "work_id", "purpose", "amount", "status",
              "created_at", "paid_at", "refunded_at"]


class Synthetic(unittest.TestCase, InvariantsMixin):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, contracts, works_, payments):
        for name, head, rows in (("contracts.csv", CONTRACTS_H, contracts),
                                 ("works.csv", WORKS_H, works_),
                                 ("payments.csv", PAYMENTS_H, payments)):
            with (self.dir / name).open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(head)
                w.writerows(rows)

    def base(self, payments, works_=None):
        self.write([["C", "X", "300.00", "2026-01-01"]],
                   works_ or [["A", "C", "a", "100.00", "done", "2026-02-01", ""],
                              ["B", "C", "b", "100.00", "done", "2026-01-15", ""],
                              ["Z", "C", "z", "100.00", "planned", "", ""]],
                   payments)

    def errors(self):
        with self.assertRaises(report.DataError) as cm:
            report.load(self.dir)
        return "\n".join(cm.exception.errors)

    # --- распределение

    def test_pool_goes_to_done_by_completion_date_first(self):
        self.base([["P", "C", "", "", "150.00", "paid", "2026-03-01", "2026-03-01", ""]])
        cl = run(date(2026, 3, 1), self.dir)["C"]
        self.assertEqual(works(cl), {"A": ("done", 5000, 5000),
                                     "B": ("done", 10000, 0),
                                     "Z": ("planned", 0, 10000)})

    def test_bound_payment_excess_goes_to_pool(self):
        self.base([["P", "C", "Z", "", "150.00", "paid", "2026-03-01", "2026-03-01", ""]])
        cl = run(date(2026, 3, 1), self.dir)["C"]
        self.assertEqual(works(cl)["Z"], ("planned", 10000, 0))
        self.assertEqual(works(cl)["B"], ("done", 5000, 5000))
        self.assertInvariants(cl)

    def test_bound_to_cancelled_work_goes_to_pool(self):
        self.base([["P", "C", "Z", "", "50.00", "paid", "2026-01-10", "2026-01-10", ""]],
                  [["B", "C", "b", "100.00", "done", "2026-01-15", ""],
                   ["Z", "C", "z", "200.00", "cancelled", "", "2026-01-20"]])
        before = run(date(2026, 1, 19), self.dir)["C"]
        after = run(date(2026, 1, 20), self.dir)["C"]
        self.assertEqual(works(before)["Z"], ("planned", 5000, 15000))
        self.assertEqual(works(after)["Z"], ("cancelled", 0, 0))
        self.assertEqual(works(after)["B"], ("done", 5000, 5000))

    def test_overpayment_beyond_all_works(self):
        self.base([["P", "C", "", "", "350.01", "paid", "2026-03-01", "2026-03-01", ""]])
        cl = run(date(2026, 3, 1), self.dir)["C"]
        self.assertEqual(cl.overpaid, 5001)
        self.assertEqual(cl.remaining, 0)
        self.assertInvariants(cl)

    def test_kopecks_exact(self):
        self.base([["P1", "C", "", "", "0.10", "paid", "2026-03-01", "2026-03-01", ""],
                   ["P2", "C", "", "", "0.20", "paid", "2026-03-01", "2026-03-01", ""]])
        self.assertEqual(run(date(2026, 3, 1), self.dir)["C"].net, 30)

    # --- проверки данных

    def test_status_date_errors(self):
        self.write(
            [["C", "X", "300.00", "2026-01-01"]],
            [["A", "C", "a", "100.00", "done", "", ""],
             ["B", "C", "b", "100.00", "cancelled", "", ""],
             ["D", "C", "d", "100.00", "planned", "2026-02-01", ""]],
            [["P1", "C", "", "", "10.00", "paid", "2026-01-02", "", ""],
             ["P2", "C", "", "", "10.00", "refunded", "2026-01-02", "", ""],
             ["P3", "C", "", "", "10.00", "refunded", "2026-01-02", "2026-01-05", "2026-01-03"],
             ["P4", "C", "", "", "10.00", "queued", "2026-01-02", "2026-01-03", ""],
             ["P5", "C", "", "", "10.00", "paid", "2026-01-05", "2026-01-03", ""]])
        e = self.errors()
        for needle in ["(A): статус done, но completed_at не заполнена",
                       "(B): статус cancelled, но cancelled_at не заполнена",
                       "(D): статус planned",
                       "(P1): статус paid, но paid_at не заполнена",
                       "(P2): статус refunded, но paid_at не заполнена",
                       "(P2): статус refunded, но refunded_at не заполнена",
                       "(P3): refunded_at 2026-01-03 раньше paid_at",
                       "(P4): статус queued",
                       "(P5): paid_at 2026-01-03 раньше created_at"]:
            self.assertIn(needle, e)

    def test_non_positive_amounts(self):
        self.write([["C", "X", "0.00", "2026-01-01"]],
                   [["A", "C", "a", "-5.00", "planned", "", ""]],
                   [["P", "C", "", "", "0", "queued", "2026-01-02", "", ""]])
        e = self.errors()
        self.assertIn("(C): amount: сумма должна быть больше нуля", e)
        self.assertIn("(A): price: сумма должна быть больше нуля", e)
        self.assertIn("(P): amount: сумма должна быть больше нуля", e)

    def test_reference_and_format_errors(self):
        self.write([["C", "X", "300.00", "2026-01-01"], ["E", "Y", "100.00", "2026-01-01"]],
                   [["A", "C", "a", "100.005", "planned", "", ""],
                    ["A", "C", "dup", "100.00", "planned", "", ""],
                    ["W", "NOPE", "w", "100.00", "planned", "", ""],
                    ["U", "E", "u", "100.00", "weird", "", ""],
                    ["V", "E", "v", "100.00", "planned", "", ""]],
                   [["P", "C", "V", "", "10.00", "paid", "2026-13-01", "2026-01-02", ""]])
        e = self.errors()
        for needle in ["точнее копейки", "повторяющийся work_id", "договор 'NOPE' не найден",
                       "неизвестный статус 'weird'", "неразбираемая дата",
                       "работа V относится к договору E"]:
            self.assertIn(needle, e)

    def test_warnings(self):
        self.write([["C", "X", "999.00", "2026-01-10"]],
                   [["A", "C", "a", "100.00", "done", "2026-01-05", ""]],
                   [["P", "C", "", "", "10.00", "paid", "2026-01-01", "2026-01-01", ""]])
        w = "\n".join(report.load(self.dir).warnings)
        self.assertIn("A: дата приёмки/отмены 2026-01-05 раньше подписания", w)
        self.assertIn("P: заведён 2026-01-01, раньше подписания", w)
        self.assertIn("не равна сумме работ", w)

    def test_real_data_is_clean(self):
        self.assertEqual(report.load(DATA).warnings, [])


    # --- добавлено по замечаниям верификатора (notes/verifier.md)

    def test_extra_and_missing_fields_are_errors_not_crash(self):
        self.base([["P", "C", "", "", "10.00", "paid", "2026-03-01", "2026-03-01", "", "extra"],
                   ["Q", "C", "", ""]])
        e = self.errors()
        self.assertIn("payments.csv:2: лишние поля ['extra']", e)
        self.assertIn("payments.csv:3: не хватает полей", e)

    def test_line_numbers_survive_skipped_rows(self):
        self.base([["P", "C", "", "", "10.00", "paid", "2026-03-01", "2026-03-01", "", "extra"],
                   ["Q", "C", "", "", "0", "paid", "2026-03-01", "2026-03-01", ""]])
        self.assertIn("payments.csv:3 (Q): amount", self.errors())

    def test_strict_formats(self):
        self.write([["C", "X", "1e2", "20260101"]], [], [])
        e = self.errors()
        self.assertIn("неразбираемая сумма '1e2'", e)
        self.assertIn("неразбираемая дата '20260101'", e)

    def test_no_cascading_errors(self):
        self.write([["C", "X", "bad", "2026-01-01"]],
                   [["A", "C", "a", "bad", "planned", "", ""]],
                   [["P", "C", "A", "", "10.00", "queued", "2026-01-02", "", ""]])
        e = self.errors()
        self.assertNotIn("не найден", e)
        self.assertEqual(e.count("неразбираемая сумма"), 2)

    def test_refund_same_day_never_counted(self):
        self.base([["P", "C", "", "", "50.00", "refunded", "2026-03-01", "2026-03-01", "2026-03-01"]])
        cl = run(date(2026, 3, 1), self.dir)["C"]
        self.assertEqual((cl.net, cl.queued), (0, 0))

    def test_two_bound_payments_on_one_work(self):
        self.base([["P1", "C", "Z", "", "60.00", "paid", "2026-03-01", "2026-03-01", ""],
                   ["P2", "C", "Z", "", "60.00", "paid", "2026-03-02", "2026-03-02", ""]])
        cl = run(date(2026, 3, 2), self.dir)["C"]
        self.assertEqual(works(cl)["Z"], ("planned", 10000, 0))
        self.assertEqual(works(cl)["B"], ("done", 2000, 8000))  # излишек 20 -> пул -> B (принята раньше)
        self.assertInvariants(cl)

    def test_natural_sort_of_planned_works(self):
        self.write([["C", "X", "200.00", "2026-01-01"]],
                   [["W-10", "C", "x", "100.00", "planned", "", ""],
                    ["W-9", "C", "y", "100.00", "planned", "", ""]],
                   [["P", "C", "", "", "100.00", "paid", "2026-01-02", "2026-01-02", ""]])
        cl = run(date(2026, 1, 2), self.dir)["C"]
        self.assertEqual([w.work.work_id for w in cl.works], ["W-9", "W-10"])
        self.assertEqual(works(cl)["W-9"][1], 10000)
        self.assertEqual(works(cl)["W-10"][1], 0)

    def test_queued_bound_to_cancelled_work_still_in_queue(self):
        self.base([["P", "C", "Z", "", "40.00", "queued", "2026-03-01", "", ""]],
                  [["B", "C", "b", "100.00", "done", "2026-01-15", ""],
                   ["Z", "C", "z", "100.00", "cancelled", "", "2026-02-01"]])
        cl = run(date(2026, 3, 1), self.dir)["C"]
        self.assertEqual((cl.queued, cl.remaining, cl.remaining_after_queue), (4000, 10000, 6000))

    def test_queue_larger_than_remaining_goes_negative(self):
        self.base([["P", "C", "", "", "400.00", "queued", "2026-03-01", "", ""]])
        self.assertEqual(run(date(2026, 3, 1), self.dir)["C"].remaining_after_queue, -10000)


class Cli(unittest.TestCase):
    def test_cli_text_and_csv(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = report.main(["--date", "2026-11-10", "--data", str(DATA), "--csv", str(tmp / "r.csv")])
        self.assertEqual(rc, 0)
        self.assertIn("Отчёт на 10.11.2026", out.getvalue())
        with (tmp / "r.csv").open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        d2 = next(r for r in rows if r["level"] == "contract" and r["contract_id"] == "D-2")
        self.assertEqual((d2["net"], d2["remaining"], d2["advance"]), ("75000.00", "15000.00", "25000.00"))
        w23 = next(r for r in rows if r["work_id"] == "W-23")
        self.assertEqual((w23["status_on_date"], w23["paid"], w23["remaining"]),
                         ("planned", "25000.00", "15000.00"))

    def test_cli_data_error_exit_code(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = report.main(["--date", "2026-11-10", "--data", str(tmp)])
        self.assertEqual(rc, 1)
        self.assertIn("файл не найден", err.getvalue())

    def test_cli_invariant_failure_is_readable(self):
        orig = report.check_invariants
        report.check_invariants = lambda cl: (_ for _ in ()).throw(AssertionError("тест"))
        self.addCleanup(setattr, report, "check_invariants", orig)
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = report.main(["--date", "2026-11-10", "--data", str(DATA)])
        self.assertEqual(rc, 3)
        self.assertIn("Внутренняя ошибка расчёта", err.getvalue())


if __name__ == "__main__":
    unittest.main()
