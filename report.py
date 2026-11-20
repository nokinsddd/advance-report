#!/usr/bin/env python3
"""Отчёт по оплате работ и договоров на дату.

Запуск:
    python3 report.py --date 2026-11-10 [--data data] [--csv out.csv]

Правила расчёта описаны в DECISIONS.md. Деньги хранятся в целых копейках (int).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

# ---------------------------------------------------------------- модель


@dataclass(frozen=True)
class Contract:
    contract_id: str
    contractor: str
    amount: int  # копейки
    signed_at: date


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    price: int
    status: str
    completed_at: date | None
    cancelled_at: date | None


@dataclass(frozen=True)
class Payment:
    payment_id: str
    contract_id: str
    work_id: str | None
    purpose: str
    amount: int
    status: str
    created_at: date
    paid_at: date | None
    refunded_at: date | None


@dataclass
class Data:
    contracts: dict[str, Contract]
    works: dict[str, Work]
    payments: dict[str, Payment]
    warnings: list[str] = field(default_factory=list)


class DataError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


# ---------------------------------------------------------------- разбор


def natural_key(s: str):
    """W-9 раньше W-10."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s)]


MONEY_RE = re.compile(r"-?\d+(\.\d+)?")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_money(raw: str) -> int:
    """'100000.00' -> 10000000 копеек. Без float. Только цифры и точка."""
    if not MONEY_RE.fullmatch(raw.strip()):
        raise ValueError(f"неразбираемая сумма {raw!r}")
    d = Decimal(raw.strip())
    kop = d * 100
    if kop != kop.to_integral_value():
        raise ValueError(f"сумма {raw!r} точнее копейки")
    return int(kop)


def parse_date(raw: str) -> date:
    """Только ГГГГ-ММ-ДД."""
    if not DATE_RE.fullmatch(raw.strip()):
        raise ValueError(f"неразбираемая дата {raw!r}")
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise ValueError(f"неразбираемая дата {raw!r}")


def read_csv(path: Path, required: list[str], errors: list[str]) -> list[dict] | None:
    """None — файл непригоден целиком. В каждой строке ключ _line — номер строки в файле."""
    if not path.exists():
        errors.append(f"{path.name}: файл не найден")
        return None
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            errors.append(f"{path.name}: нет колонок {', '.join(missing)}")
            return None
        rows = []
        for row in reader:
            n = reader.line_num
            if None in row:
                errors.append(f"{path.name}:{n}: лишние поля {row[None]!r}")
                continue
            if any(v is None for v in row.values()):
                errors.append(f"{path.name}:{n}: не хватает полей")
                continue
            rows.append({k: v.strip() for k, v in row.items()} | {"_line": n})
        return rows


class _RowCtx:
    """Собирает ошибки разбора одной строки, чтобы показать все сразу."""

    def __init__(self, where: str, errors: list[str]):
        self.where, self.errors, self.ok = where, errors, True

    def err(self, msg: str):
        self.errors.append(f"{self.where}: {msg}")
        self.ok = False

    def money(self, row, col) -> int:
        try:
            v = parse_money(row[col])
        except ValueError as e:
            self.err(f"{col}: {e}")
            return 0
        if v <= 0:
            self.err(f"{col}: сумма должна быть больше нуля, получено {row[col]!r}")
        return v

    def date(self, row, col, required: bool) -> date | None:
        raw = row[col]
        if not raw:
            if required:
                self.err(f"{col}: не заполнена")
            return None
        try:
            return parse_date(raw)
        except ValueError as e:
            self.err(f"{col}: {e}")
            return None


WORK_STATUSES = {"planned", "done", "cancelled"}
PAYMENT_STATUSES = {"paid", "queued", "refunded"}


def load(data_dir: Path) -> Data:
    errors: list[str] = []
    warnings: list[str] = []

    rows_c = read_csv(data_dir / "contracts.csv",
                      ["contract_id", "contractor", "amount", "signed_at"], errors)
    rows_w = read_csv(data_dir / "works.csv",
                      ["work_id", "contract_id", "title", "price", "status",
                       "completed_at", "cancelled_at"], errors)
    rows_p = read_csv(data_dir / "payments.csv",
                      ["payment_id", "contract_id", "work_id", "purpose", "amount",
                       "status", "created_at", "paid_at", "refunded_at"], errors)
    if rows_c is None or rows_w is None or rows_p is None:
        raise DataError(errors)  # без целого файла проверять ссылки бессмысленно

    seen_contracts: set[str] = set()
    seen_works: set[str] = set()
    seen_payments: set[str] = set()
    contracts: dict[str, Contract] = {}
    for r in rows_c:
        ctx = _RowCtx(f"contracts.csv:{r['_line']} ({r['contract_id'] or '?'})", errors)
        if not r["contract_id"]:
            ctx.err("пустой contract_id")
        elif r["contract_id"] in seen_contracts:
            ctx.err("повторяющийся contract_id")
        seen_contracts.add(r["contract_id"])
        amount = ctx.money(r, "amount")
        signed = ctx.date(r, "signed_at", required=True)
        if ctx.ok:
            contracts[r["contract_id"]] = Contract(r["contract_id"], r["contractor"], amount, signed)

    works: dict[str, Work] = {}
    for r in rows_w:
        ctx = _RowCtx(f"works.csv:{r['_line']} ({r['work_id'] or '?'})", errors)
        if not r["work_id"]:
            ctx.err("пустой work_id")
        elif r["work_id"] in seen_works:
            ctx.err("повторяющийся work_id")
        seen_works.add(r["work_id"])
        if r["contract_id"] not in seen_contracts:
            ctx.err(f"договор {r['contract_id']!r} не найден")
        elif r["contract_id"] not in contracts:
            ctx.ok = False  # строка договора с ошибкой — о ней уже сообщено
        price = ctx.money(r, "price")
        st = r["status"]
        completed = ctx.date(r, "completed_at", required=False)
        cancelled = ctx.date(r, "cancelled_at", required=False)
        if st not in WORK_STATUSES:
            ctx.err(f"неизвестный статус {st!r}")
        elif st == "done":
            if not r["completed_at"]:
                ctx.err("статус done, но completed_at не заполнена")
            if r["cancelled_at"]:
                ctx.err("статус done, но заполнена cancelled_at")
        elif st == "cancelled":
            if not r["cancelled_at"]:
                ctx.err("статус cancelled, но cancelled_at не заполнена")
            if r["completed_at"]:
                ctx.err("статус cancelled, но заполнена completed_at")
        elif st == "planned" and (r["completed_at"] or r["cancelled_at"]):
            ctx.err("статус planned, но заполнена completed_at или cancelled_at")
        if ctx.ok:
            w = Work(r["work_id"], r["contract_id"], r["title"], price, st, completed, cancelled)
            works[w.work_id] = w
            c = contracts[w.contract_id]
            ev = w.completed_at or w.cancelled_at
            if ev and ev < c.signed_at:
                warnings.append(f"{w.work_id}: дата приёмки/отмены {ev} раньше подписания договора {c.signed_at}")

    payments: dict[str, Payment] = {}
    for r in rows_p:
        ctx = _RowCtx(f"payments.csv:{r['_line']} ({r['payment_id'] or '?'})", errors)
        if not r["payment_id"]:
            ctx.err("пустой payment_id")
        elif r["payment_id"] in seen_payments:
            ctx.err("повторяющийся payment_id")
        seen_payments.add(r["payment_id"])
        if r["contract_id"] not in seen_contracts:
            ctx.err(f"договор {r['contract_id']!r} не найден")
        elif r["contract_id"] not in contracts:
            ctx.ok = False  # строка договора с ошибкой — о ней уже сообщено
        work_id = r["work_id"] or None
        if work_id is not None:
            w = works.get(work_id)
            if work_id not in seen_works:
                ctx.err(f"работа {work_id!r} не найдена")
            elif w is None:
                ctx.ok = False  # строка работы с ошибкой — о ней уже сообщено
            elif w.contract_id != r["contract_id"]:
                ctx.err(f"работа {work_id} относится к договору {w.contract_id}, а платёж — к {r['contract_id']}")
        amount = ctx.money(r, "amount")
        created = ctx.date(r, "created_at", required=True)
        paid = ctx.date(r, "paid_at", required=False)
        refunded = ctx.date(r, "refunded_at", required=False)
        st = r["status"]
        if st not in PAYMENT_STATUSES:
            ctx.err(f"неизвестный статус {st!r}")
        elif st == "paid":
            if not r["paid_at"]:
                ctx.err("статус paid, но paid_at не заполнена")
            if r["refunded_at"]:
                ctx.err("статус paid, но заполнена refunded_at")
        elif st == "refunded":
            if not r["paid_at"]:
                ctx.err("статус refunded, но paid_at не заполнена")
            if not r["refunded_at"]:
                ctx.err("статус refunded, но refunded_at не заполнена")
        elif st == "queued" and (r["paid_at"] or r["refunded_at"]):
            ctx.err("статус queued, но заполнена paid_at или refunded_at")
        if paid and refunded and refunded < paid:
            ctx.err(f"refunded_at {refunded} раньше paid_at {paid}")
        if paid and created and paid < created:
            ctx.err(f"paid_at {paid} раньше created_at {created}")
        if ctx.ok:
            p = Payment(r["payment_id"], r["contract_id"], work_id, r["purpose"], amount,
                        st, created, paid, refunded)
            payments[p.payment_id] = p
            c = contracts[p.contract_id]
            if p.created_at < c.signed_at:
                warnings.append(f"{p.payment_id}: заведён {p.created_at}, раньше подписания договора {c.signed_at}")

    if errors:
        raise DataError(errors)

    for c in contracts.values():
        total = sum(w.price for w in works.values() if w.contract_id == c.contract_id)
        if total != c.amount:
            warnings.append(f"{c.contract_id}: сумма договора {fmt(c.amount)} не равна сумме работ {fmt(total)}")

    return Data(contracts, works, payments, warnings)


# ---------------------------------------------------------------- расчёт

PLANNED, DONE, CANCELLED = "planned", "done", "cancelled"
STATUS_RU = {PLANNED: "запланирована", DONE: "выполнена", CANCELLED: "отменена"}


def work_status_on(w: Work, d: date) -> str:
    if w.completed_at and w.completed_at <= d:
        return DONE
    if w.cancelled_at and w.cancelled_at <= d:
        return CANCELLED
    return PLANNED


def is_paid_on(p: Payment, d: date) -> bool:
    return (p.paid_at is not None and p.paid_at <= d
            and not (p.refunded_at is not None and p.refunded_at <= d))


def is_queued_on(p: Payment, d: date) -> bool:
    return p.created_at <= d and not (p.paid_at is not None and p.paid_at <= d)


@dataclass
class WorkLine:
    work: Work
    status: str  # на дату
    paid: int = 0

    @property
    def due(self) -> int:  # сколько нужно заплатить за работу
        return 0 if self.status == CANCELLED else self.work.price

    @property
    def remaining(self) -> int:
        return self.due - self.paid


@dataclass
class ContractLine:
    contract: Contract
    works: list[WorkLine]
    net: int          # выплачено по договору (оплачено и не возвращено)
    overpaid: int     # не легло ни на одну работу
    queued: int       # в очереди на дату

    @property
    def cancelled(self) -> int:
        return sum(w.work.price for w in self.works if w.status == CANCELLED)

    @property
    def payable(self) -> int:
        return sum(w.due for w in self.works)

    @property
    def earned(self) -> int:
        return sum(w.work.price for w in self.works if w.status == DONE)

    @property
    def works_paid(self) -> int:
        return sum(w.paid for w in self.works)

    @property
    def advance(self) -> int:
        return self.works_paid - self.earned

    @property
    def remaining(self) -> int:
        return sum(w.remaining for w in self.works)

    @property
    def remaining_after_queue(self) -> int:
        return self.remaining - self.queued  # минус: очередь приведёт к переплате


def build_report(data: Data, d: date) -> list[ContractLine]:
    result = []
    for c in sorted(data.contracts.values(), key=lambda c: natural_key(c.contract_id)):
        if c.signed_at > d:
            continue  # решение 13
        lines = {w.work_id: WorkLine(w, work_status_on(w, d))
                 for w in data.works.values() if w.contract_id == c.contract_id}
        pays = sorted((p for p in data.payments.values() if p.contract_id == c.contract_id),
                      key=lambda p: (p.paid_at or p.created_at, natural_key(p.payment_id)))
        paid_now = [p for p in pays if is_paid_on(p, d)]
        net = sum(p.amount for p in paid_now)
        queued = sum(p.amount for p in pays if is_queued_on(p, d))

        pool = 0
        # 1) привязанные платежи — в свою работу, излишек в пул (решение 7)
        for p in paid_now:
            if p.work_id is None:
                pool += p.amount
                continue
            wl = lines[p.work_id]
            take = min(p.amount, max(wl.remaining, 0))
            wl.paid += take
            pool += p.amount - take

        # 2) пул: принятые по дате приёмки, затем запланированные по номеру (решения 8, 9)
        done = sorted((l for l in lines.values() if l.status == DONE),
                      key=lambda l: (l.work.completed_at, natural_key(l.work.work_id)))
        planned = sorted((l for l in lines.values() if l.status == PLANNED),
                         key=lambda l: natural_key(l.work.work_id))
        for wl in done + planned:
            take = min(pool, wl.remaining)
            wl.paid += take
            pool -= take

        cl = ContractLine(c, sorted(lines.values(), key=lambda l: natural_key(l.work.work_id)),
                          net=net, overpaid=pool, queued=queued)
        check_invariants(cl)
        result.append(cl)
    return result


def check_invariants(cl: ContractLine) -> None:
    cid = cl.contract.contract_id
    problems = []
    if cl.works_paid + cl.overpaid != cl.net:
        problems.append("Σ paid + overpaid != net")
    if cl.net + cl.remaining != cl.payable + cl.overpaid:
        problems.append("net + remaining != payable + overpaid")
    for w in cl.works:
        if not 0 <= w.paid <= w.due:
            problems.append(f"{w.work.work_id}: выплачено вне [0, цена]")
    if cl.overpaid < 0:
        problems.append("overpaid < 0")
    if problems:
        raise AssertionError(f"{cid}: нарушены инварианты: {'; '.join(problems)}")


# ---------------------------------------------------------------- вывод


def fmt(kop: int) -> str:
    sign = "-" if kop < 0 else ""
    rub, k = divmod(abs(kop), 100)
    return f"{sign}{rub:,}.{k:02d}".replace(",", " ")


def fmt_plain(kop: int) -> str:
    sign = "-" if kop < 0 else ""
    rub, k = divmod(abs(kop), 100)
    return f"{sign}{rub}.{k:02d}"


def status_text(wl: WorkLine) -> str:
    s = STATUS_RU[wl.status]
    if wl.status == DONE:
        s += f" {wl.work.completed_at:%d.%m.%Y}"
    elif wl.status == CANCELLED:
        s += f" {wl.work.cancelled_at:%d.%m.%Y}"
    return s


CONTRACT_ROWS = [
    ("Сумма договора", lambda c: c.contract.amount),
    ("Отменено работ", lambda c: c.cancelled),
    ("К оплате по работам", lambda c: c.payable),
    ("Принято работ", lambda c: c.earned),
    ("Выплачено по договору", lambda c: c.net),
    ("  в т.ч. в счёт работ", lambda c: c.works_paid),
    ("  в т.ч. переплата", lambda c: c.overpaid),
    ("Аванс (+) / долг за принятые (−)", lambda c: c.advance),
    ("Осталось оплатить", lambda c: c.remaining),
    ("В очереди на оплату", lambda c: c.queued),
    ("Осталось после очереди", lambda c: c.remaining_after_queue),
]


def table(headers: list[str], rows: list[list[str]], right: set[int]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]

    def line(cells):
        return "  ".join(str(c).rjust(w) if i in right else str(c).ljust(w)
                         for i, (c, w) in enumerate(zip(cells, widths)))

    sep = "  ".join("-" * w for w in widths)
    return "\n".join([line(headers), sep] + [line(r) for r in rows])


def render_text(report: list[ContractLine], d: date) -> str:
    out = [f"Отчёт на {d:%d.%m.%Y} (включительно), суммы в рублях", ""]
    if not report:
        return "\n".join(out + ["На эту дату нет подписанных договоров."])

    out.append("ПО РАБОТАМ")
    rows = []
    for cl in report:
        for wl in cl.works:
            rows.append([cl.contract.contract_id, wl.work.work_id, wl.work.title,
                         status_text(wl), fmt(wl.work.price), fmt(wl.paid), fmt(wl.remaining)])
    out.append(table(["Договор", "Работа", "Название", "Статус на дату", "Цена",
                      "Выплачено", "Осталось"], rows, right={4, 5, 6}))
    out.append("")

    out.append("ПО ДОГОВОРАМ")
    heads = ["Показатель"] + [f"{c.contract.contract_id} {c.contract.contractor}" for c in report]
    if len(report) > 1:
        heads.append("Итого")
    rows = []
    for label, get in CONTRACT_ROWS:
        vals = [get(c) for c in report]
        row = [label] + [fmt(v) for v in vals]
        if len(report) > 1:
            row.append(fmt(sum(vals)))
        rows.append(row)
    out.append(table(heads, rows, right=set(range(1, len(heads)))))

    notes = [f"{c.contract.contract_id}: переплата {fmt(c.overpaid)} — выплачено больше, чем стоят работы."
             for c in report if c.overpaid]
    if notes:
        out += [""] + ["Внимание: " + n for n in notes]
    return "\n".join(out)


def write_csv(report: list[ContractLine], d: date, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["report_date", "level", "contract_id", "work_id", "title", "status_on_date",
                     "price", "paid", "remaining",
                     "contract_amount", "cancelled", "payable", "earned", "net",
                     "works_paid", "overpaid", "advance", "queued", "remaining_after_queue"])
        for cl in report:
            for wl in cl.works:
                wr.writerow([d.isoformat(), "work", cl.contract.contract_id, wl.work.work_id,
                             wl.work.title, wl.status, fmt_plain(wl.work.price),
                             fmt_plain(wl.paid), fmt_plain(wl.remaining)] + [""] * 10)
            wr.writerow([d.isoformat(), "contract", cl.contract.contract_id, "",
                         cl.contract.contractor, "", "", fmt_plain(cl.works_paid),
                         fmt_plain(cl.remaining)]
                        + [fmt_plain(v) for v in (cl.contract.amount, cl.cancelled, cl.payable,
                                                  cl.earned, cl.net, cl.works_paid, cl.overpaid,
                                                  cl.advance, cl.queued, cl.remaining_after_queue)])


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Отчёт по оплате работ и договоров на дату.")
    ap.add_argument("--date", required=True, help="дата отчёта, ГГГГ-ММ-ДД (включительно)")
    ap.add_argument("--data", default="data", help="папка с CSV (по умолчанию data)")
    ap.add_argument("--csv", help="дополнительно сохранить отчёт в CSV")
    args = ap.parse_args(argv)

    try:
        d = parse_date(args.date)
    except ValueError as e:
        ap.error(str(e))

    try:
        data = load(Path(args.data))
    except DataError as e:
        print("Ошибки в данных, отчёт не построен:", file=sys.stderr)
        for msg in e.errors:
            print(f"  - {msg}", file=sys.stderr)
        return 1

    for w in data.warnings:
        print(f"Предупреждение: {w}", file=sys.stderr)

    try:
        report = build_report(data, d)
    except AssertionError as e:
        print(f"Внутренняя ошибка расчёта, отчёт не построен: {e}", file=sys.stderr)
        return 3
    print(render_text(report, d))
    if args.csv:
        write_csv(report, d, Path(args.csv))
        print(f"\nCSV сохранён: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
