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