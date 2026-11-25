from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import traceback

from db import get_connection

# =====================================================================
# EXPENSES
#
# Company expense register (receipts / supplier invoices).
#   * Amount is entered by the user (excl. VAT)
#   * VAT is 5% of Amount, unless the expense is flagged "VAT Not Applicable"
#     (vat_applicable = False) -> VAT = 0 and Total = Amount
#   * Total = Amount + VAT
#   * Mode of Payment (MoP): BANK_TRANSFER (BT) / CHEQUE (Cq) / CASH
#   * Invoice/Receipt No. and Supplier Name are optional (small expenses)
#   * Supports both built-in CATEGORIES and user-created custom categories
# VAT and Total are recomputed here on the server, so whatever the
# browser sends can never put a wrong VAT/Total into the database.
#
# The DAILY EXPENSE endpoints (/expenses/daily/...) are at the bottom of this file.
# =====================================================================

router = APIRouter(prefix="/expenses", tags=["Expenses"])

CATEGORIES = [
    "General Expenses",
    "Staff Visa",
    "Staff Medical",
    "Petrol",
    "Utility",
    "Vehicle Maintenance",
    "Parking",
    "Maintenance",
]

PAYMENT_MODES = ["BANK_TRANSFER", "CHEQUE", "CASH"]

VAT_RATE = Decimal("0.05")
_CENT = Decimal("0.01")
_EXPENSES_LOCK = 7353          # pg advisory-lock key — serialises first-time table creation
_table_ready = False


def ensure_expenses_table(cur):
    """Idempotent migration (cheap no-op once applied)."""
    global _table_ready
    if _table_ready:
        return
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_EXPENSES_LOCK,))
    cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            expense_id     SERIAL PRIMARY KEY,
            invoice_no     TEXT           NOT NULL,
            expense_date   DATE           NOT NULL,
            category       TEXT           NOT NULL,
            supplier_name  TEXT           NOT NULL,
            amount         NUMERIC(12, 2) NOT NULL,
            vat            NUMERIC(12, 2) NOT NULL,
            total          NUMERIC(12, 2) NOT NULL,
            created_at     TIMESTAMP      NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses (expense_date)")
    # --- later additions (idempotent; safe on an existing table) ---------------
    cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS mode_of_payment TEXT")
    cur.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS vat_applicable BOOLEAN NOT NULL DEFAULT TRUE")
    cur.execute("ALTER TABLE expenses ALTER COLUMN invoice_no DROP NOT NULL")
    cur.execute("ALTER TABLE expenses ALTER COLUMN supplier_name DROP NOT NULL")


def _set_ready():
    """Call only after the CREATE TABLE transaction has been committed."""
    global _table_ready
    _table_ready = True


class ExpenseIn(BaseModel):
    invoice_no: Optional[str] = None        # optional - small expenses have none
    expense_date: date
    category: str
    supplier_name: Optional[str] = None     # optional - small expenses have none
    amount: Decimal
    mode_of_payment: str                    # BANK_TRANSFER | CHEQUE | CASH
    vat_applicable: bool = True             # False -> no VAT, Total = Amount


def _validate_and_compute(body: ExpenseIn):
    # blank -> None, so the database stores NULL rather than ""
    invoice_no = (body.invoice_no or "").strip() or None
    supplier = (body.supplier_name or "").strip() or None
    category = (body.category or "").strip()
    if not category:
        raise HTTPException(400, "Category is required")
    mop = (body.mode_of_payment or "").strip().upper()
    if mop not in PAYMENT_MODES:
        raise HTTPException(400, "Mode of Payment must be Bank Transfer, Cheque or Cash")
    amount = body.amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise HTTPException(400, "Amount must be greater than zero")
    if body.vat_applicable:
        vat = (amount * VAT_RATE).quantize(_CENT, rounding=ROUND_HALF_UP)
    else:
        vat = Decimal("0.00")
    total = amount + vat
    return invoice_no, category, supplier, mop, body.vat_applicable, amount, vat, total


def _row_to_dict(r):
    return {
        "expense_id":      r[0],
        "invoice_no":      r[1],
        "expense_date":    r[2].isoformat() if r[2] else None,
        "category":        r[3],
        "supplier_name":   r[4],
        "amount":          float(r[5]),
        "vat":             float(r[6]),
        "total":           float(r[7]),
        "mode_of_payment": r[8],            # None for expenses saved before MoP existed
        "vat_applicable":  True if r[9] is None else bool(r[9]),
    }


_SELECT = """
    SELECT expense_id, invoice_no, expense_date, category,
           supplier_name, amount, vat, total, mode_of_payment, vat_applicable
    FROM expenses
"""


@router.get("/categories")
def list_categories():
    """Return default categories merged with any custom categories used in expenses."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_expenses_table(cur)
        conn.commit()
        _set_ready()
        cur.execute("SELECT DISTINCT category FROM expenses WHERE category IS NOT NULL ORDER BY category")
        db_cats = [r[0] for r in cur.fetchall() if r[0]]
        merged = list(CATEGORIES)
        seen = {c.lower() for c in merged}
        for c in db_cats:
            if c.lower() not in seen:
                merged.append(c)
                seen.add(c.lower())
        return merged
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, str(e))
    finally:
        cur.close(); conn.close()


@router.get("/list")
def list_expenses():
    """All expenses, newest first. The page groups them by month."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_expenses_table(cur)
        conn.commit()
        _set_ready()
        cur.execute(_SELECT + " ORDER BY expense_date DESC, expense_id DESC")
        return [_row_to_dict(r) for r in cur.fetchall()]
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, str(e))
    finally:
        cur.close(); conn.close()


@router.post("/")
def create_expense(body: ExpenseIn):
    invoice_no, category, supplier, mop, vat_applicable, amount, vat, total = _validate_and_compute(body)
    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_expenses_table(cur)
        cur.execute("""
            INSERT INTO expenses
                (invoice_no, expense_date, category, supplier_name, amount, vat, total,
                 mode_of_payment, vat_applicable)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING expense_id, invoice_no, expense_date, category,
                      supplier_name, amount, vat, total, mode_of_payment, vat_applicable
        """, (invoice_no, body.expense_date, category, supplier, amount, vat, total,
              mop, vat_applicable))
        row = cur.fetchone()
        conn.commit()
        _set_ready()
        return _row_to_dict(row)
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, str(e))
    finally:
        cur.close(); conn.close()


@router.put("/{expense_id}")
def update_expense(expense_id: int, body: ExpenseIn):
    invoice_no, category, supplier, mop, vat_applicable, amount, vat, total = _validate_and_compute(body)
    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_expenses_table(cur)
        cur.execute("""
            UPDATE expenses
               SET invoice_no = %s, expense_date = %s, category = %s,
                   supplier_name = %s, amount = %s, vat = %s, total = %s,
                   mode_of_payment = %s, vat_applicable = %s
             WHERE expense_id = %s
            RETURNING expense_id, invoice_no, expense_date, category,
                      supplier_name, amount, vat, total, mode_of_payment, vat_applicable
        """, (invoice_no, body.expense_date, category, supplier,
              amount, vat, total, mop, vat_applicable, expense_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Expense not found")
        conn.commit()
        return _row_to_dict(row)
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, str(e))
    finally:
        cur.close(); conn.close()


@router.delete("/{expense_id}")
def delete_expense(expense_id: int):
    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_expenses_table(cur)
        cur.execute("DELETE FROM expenses WHERE expense_id = %s RETURNING expense_id", (expense_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Expense not found")
        conn.commit()
        return {"message": "Expense deleted", "expense_id": expense_id}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, str(e))
    finally:
        cur.close(); conn.close()


# =====================================================================
# DAILY EXPENSE  (daily cash cycle, shared by every user)
#
#   receipts in  ->  expenses out  ->  handover to AR / AS  ->  closing balance
#
# The server only STORES the records. Opening / closing balances and the
# week / month summaries are calculated by the page from these records.
#
# Every change is its own small request (add a receipt, remove an expense,
# save a day's settings), so two people working at the same time never
# overwrite each other's entries.
#
#   daily_expense_days      one row per date that has non-default settings
#   daily_expense_receipts  receipts; a receipt no. can only be recorded ONCE
#   daily_expense_items     expenses paid out of the day's cash
# =====================================================================

HANDOVER_TO = ["AR", "AS"]
RECEIPT_SOURCES = ["existing", "new"]
_DAILY_LOCK = 7354             # pg advisory-lock key — serialises first-time table creation
_daily_ready = False


def ensure_daily_tables(cur):
    """Idempotent migration (cheap no-op once applied)."""
    global _daily_ready
    if _daily_ready:
        return
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_DAILY_LOCK,))
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_expense_days (
            day_date           DATE PRIMARY KEY,
            handover_to        TEXT           NOT NULL DEFAULT 'AR',
            handover_override  NUMERIC(12, 2),
            clear_outstanding  BOOLEAN        NOT NULL DEFAULT FALSE,
            carry_forward      BOOLEAN        NOT NULL DEFAULT TRUE,
            opening_manual     NUMERIC(12, 2) NOT NULL DEFAULT 0,
            updated_at         TIMESTAMP      NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_expense_receipts (
            receipt_id   SERIAL PRIMARY KEY,
            day_date     DATE           NOT NULL,
            receipt_no   TEXT           NOT NULL,
            receipt_key  TEXT           NOT NULL,
            amount       NUMERIC(12, 2) NOT NULL,
            mode         TEXT           NOT NULL,
            source       TEXT           NOT NULL DEFAULT 'new',
            created_at   TIMESTAMP      NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_daily_receipts_key ON daily_expense_receipts (receipt_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_daily_receipts_date ON daily_expense_receipts (day_date)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_expense_items (
            item_id     SERIAL PRIMARY KEY,
            day_date    DATE           NOT NULL,
            amount      NUMERIC(12, 2) NOT NULL,
            reason      TEXT           NOT NULL,
            created_at  TIMESTAMP      NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_daily_items_date ON daily_expense_items (day_date)")


def _set_daily_ready():
    """Call only after the CREATE TABLE transaction has been committed."""
    global _daily_ready
    _daily_ready = True


def _with_daily(work):
    """Open a connection, make sure the tables exist, run work(cur), commit."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_daily_tables(cur)
        result = work(cur)
        conn.commit()
        _set_daily_ready()
        return result
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        if (getattr(e, "pgcode", None) or getattr(e, "sqlstate", None)) == "23505":        # unique violation: two people saved the same receipt together
            raise HTTPException(409, "That receipt number is already recorded")
        traceback.print_exc()
        raise HTTPException(500, str(e))
    finally:
        cur.close(); conn.close()


def _receipt_key(v: str) -> str:
    """Same rule as the page: 0042 and 42 are the same receipt; letters are case-insensitive."""
    s = (v or "").strip()
    return str(int(s)) if (s.isascii() and s.isdigit()) else s.upper()


def _blank_day():
    return {
        "receipts": [], "expenses": [],
        "handoverTo": "AR", "handoverOverride": None,
        "clearOutstanding": False, "carryForward": True, "openingManual": 0,
    }


class DailyReceiptIn(BaseModel):
    receipt_no: str
    amount: Decimal
    mode: str                               # BANK_TRANSFER | CHEQUE | CASH
    source: str = "new"                     # existing (found in invoices) | new


class DailyItemIn(BaseModel):
    amount: Decimal
    reason: str


class DailySettingsIn(BaseModel):
    handover_to: str = "AR"                 # AR | AS
    handover_override: Optional[Decimal] = None   # None = hand over everything available
    clear_outstanding: bool = False
    carry_forward: bool = True
    opening_manual: Decimal = Decimal("0")  # only used for the very first recorded day


class DailyImportIn(BaseModel):
    days: dict                              # { "YYYY-MM-DD": {...day as the page held it...} }


@router.get("/daily/all")
def daily_all():
    """Every recorded day with its receipts, expenses and settings."""
    def work(cur):
        days = {}

        def day_of(d):
            return days.setdefault(d.isoformat(), _blank_day())

        cur.execute("""
            SELECT day_date, handover_to, handover_override, clear_outstanding,
                   carry_forward, opening_manual
            FROM daily_expense_days
        """)
        for d, to, override, clear, carry, opening in cur.fetchall():
            x = day_of(d)
            x["handoverTo"] = to
            x["handoverOverride"] = None if override is None else float(override)
            x["clearOutstanding"] = bool(clear)
            x["carryForward"] = bool(carry)
            x["openingManual"] = float(opening or 0)

        cur.execute("""
            SELECT receipt_id, day_date, receipt_no, amount, mode, source
            FROM daily_expense_receipts ORDER BY receipt_id
        """)
        for rid, d, no, amount, mode, source in cur.fetchall():
            day_of(d)["receipts"].append(
                {"id": rid, "no": no, "amount": float(amount), "mode": mode, "source": source})

        cur.execute("SELECT item_id, day_date, amount, reason FROM daily_expense_items ORDER BY item_id")
        for iid, d, amount, reason in cur.fetchall():
            day_of(d)["expenses"].append({"id": iid, "amount": float(amount), "reason": reason})

        return {"days": days}
    return _with_daily(work)


@router.put("/daily/{day}/settings")
def daily_save_settings(day: date, body: DailySettingsIn):
    """Save one day's handover / carry-forward settings (creates the row if needed)."""
    to = (body.handover_to or "").strip().upper()
    if to not in HANDOVER_TO:
        raise HTTPException(400, "Handover must be to AR or AS")
    override = None
    if body.handover_override is not None:
        override = max(Decimal("0"), body.handover_override.quantize(_CENT, rounding=ROUND_HALF_UP))
    opening = body.opening_manual.quantize(_CENT, rounding=ROUND_HALF_UP)

    def work(cur):
        cur.execute("""
            INSERT INTO daily_expense_days
                (day_date, handover_to, handover_override, clear_outstanding,
                 carry_forward, opening_manual, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (day_date) DO UPDATE SET
                handover_to       = EXCLUDED.handover_to,
                handover_override = EXCLUDED.handover_override,
                clear_outstanding = EXCLUDED.clear_outstanding,
                carry_forward     = EXCLUDED.carry_forward,
                opening_manual    = EXCLUDED.opening_manual,
                updated_at        = NOW()
        """, (day, to, override, body.clear_outstanding, body.carry_forward, opening))
        return {"message": "Saved", "day": day.isoformat()}
    return _with_daily(work)


@router.post("/daily/{day}/receipts")
def daily_add_receipt(day: date, body: DailyReceiptIn):
    no = (body.receipt_no or "").strip()
    if not no:
        raise HTTPException(400, "Receipt number is required")
    mode = (body.mode or "").strip().upper()
    if mode not in PAYMENT_MODES:
        raise HTTPException(400, "Mode of Payment must be Bank Transfer, Cheque or Cash")
    amount = body.amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise HTTPException(400, "Amount must be greater than zero")
    source = body.source if body.source in RECEIPT_SOURCES else "new"
    key = _receipt_key(no)

    def work(cur):
        cur.execute("SELECT day_date FROM daily_expense_receipts WHERE receipt_key = %s", (key,))
        hit = cur.fetchone()
        if hit:
            raise HTTPException(409, f"Receipt {no} is already recorded on {hit[0].strftime('%d %b %Y')}.")
        cur.execute("""
            INSERT INTO daily_expense_receipts (day_date, receipt_no, receipt_key, amount, mode, source)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING receipt_id
        """, (day, no, key, amount, mode, source))
        rid = cur.fetchone()[0]
        return {"id": rid, "no": no, "amount": float(amount), "mode": mode, "source": source}
    return _with_daily(work)


@router.delete("/daily/receipts/{receipt_id}")
def daily_delete_receipt(receipt_id: int):
    def work(cur):
        cur.execute("DELETE FROM daily_expense_receipts WHERE receipt_id = %s RETURNING receipt_id", (receipt_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Receipt not found")
        return {"message": "Receipt removed", "id": receipt_id}
    return _with_daily(work)


@router.post("/daily/{day}/items")
def daily_add_item(day: date, body: DailyItemIn):
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(400, "Reason is required")
    amount = body.amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise HTTPException(400, "Amount must be greater than zero")

    def work(cur):
        cur.execute("""
            INSERT INTO daily_expense_items (day_date, amount, reason)
            VALUES (%s, %s, %s) RETURNING item_id
        """, (day, amount, reason))
        return {"id": cur.fetchone()[0], "amount": float(amount), "reason": reason}
    return _with_daily(work)


@router.delete("/daily/items/{item_id}")
def daily_delete_item(item_id: int):
    def work(cur):
        cur.execute("DELETE FROM daily_expense_items WHERE item_id = %s RETURNING item_id", (item_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Expense not found")
        return {"message": "Expense removed", "id": item_id}
    return _with_daily(work)


def _money_or_none(v):
    try:
        return Decimal(str(v)).quantize(_CENT, rounding=ROUND_HALF_UP)
    except Exception:
        return None


@router.post("/daily/import")
def daily_import(body: DailyImportIn):
    """
    One-time move of records that were saved in a browser (before this database
    existed) into the shared tables. A date that already has records here is
    skipped, so running it twice never duplicates anything.
    """
    def work(cur):
        imported = skipped = 0
        for k, d in body.days.items():
            try:
                dk = date.fromisoformat(str(k))
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            cur.execute("""
                SELECT 1 FROM daily_expense_days     WHERE day_date = %s
                UNION ALL SELECT 1 FROM daily_expense_receipts WHERE day_date = %s
                UNION ALL SELECT 1 FROM daily_expense_items    WHERE day_date = %s
                LIMIT 1
            """, (dk, dk, dk))
            if cur.fetchone():
                skipped += 1
                continue

            to = str(d.get("handoverTo") or "AR").upper()
            override = _money_or_none(d.get("handoverOverride")) if d.get("handoverOverride") is not None else None
            cur.execute("""
                INSERT INTO daily_expense_days
                    (day_date, handover_to, handover_override, clear_outstanding, carry_forward, opening_manual)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (dk, to if to in HANDOVER_TO else "AR",
                  None if override is None else max(Decimal("0"), override),
                  bool(d.get("clearOutstanding", False)),
                  bool(d.get("carryForward", True)),
                  _money_or_none(d.get("openingManual") or 0) or Decimal("0.00")))

            for r in d.get("receipts") or []:
                if not isinstance(r, dict):
                    continue
                no = str(r.get("no") or "").strip()
                mode = str(r.get("mode") or "").strip().upper()
                amount = _money_or_none(r.get("amount"))
                if not no or mode not in PAYMENT_MODES or amount is None or amount <= 0:
                    continue
                cur.execute("""
                    INSERT INTO daily_expense_receipts (day_date, receipt_no, receipt_key, amount, mode, source)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (receipt_key) DO NOTHING
                """, (dk, no, _receipt_key(no), amount, mode,
                      "existing" if r.get("source") == "existing" else "new"))

            for x in d.get("expenses") or []:
                if not isinstance(x, dict):
                    continue
                reason = str(x.get("reason") or "").strip()
                amount = _money_or_none(x.get("amount"))
                if not reason or amount is None or amount <= 0:
                    continue
                cur.execute("INSERT INTO daily_expense_items (day_date, amount, reason) VALUES (%s, %s, %s)",
                            (dk, amount, reason))
            imported += 1
        return {"imported_days": imported, "skipped_days": skipped}
    return _with_daily(work)