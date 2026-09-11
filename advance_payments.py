# advance_payments.py
# FastAPI router for the "Advance Payment" section (lives inside the Invoice section).
#
# DESIGN NOTES
# ------------
# - Re-uses the existing `clients` table (client_id, name, contact_person, email,
#   phone, address, created_at) instead of creating a duplicate client list.
# - Adds ONE new table, `client_advance_payments`, which is an append-only ledger:
#       entry_type = 'TOPUP'      -> client adds/increases their advance (positive amount)
#       entry_type = 'USAGE'      -> an invoice consumed some advance (negative amount)
#       entry_type = 'ADJUSTMENT' -> manual correction (positive or negative)
#   The client's current advance balance is always SUM(amount) for that client_id.
#   This keeps a single source of truth and gives us the full history for free
#   (no separate "balance" column to keep in sync).
# - `get_client_advance_balance()` and `record_advance_usage()` are imported by
#   invoices.py so invoice generation can deduct from / respect this same ledger.

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from decimal import Decimal
from datetime import date, datetime
from db import get_connection

router = APIRouter(prefix="/advance-payments", tags=["Advance Payments"])


# ============================================================
# Migration helpers (idempotent — safe to call on every request)
# ============================================================

def ensure_advance_table(cur):
    """Create the advance-payment ledger table if it doesn't exist yet."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS client_advance_payments (
            id            SERIAL PRIMARY KEY,
            client_id     INTEGER NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
            entry_type    VARCHAR(20) NOT NULL CHECK (entry_type IN ('TOPUP', 'USAGE', 'ADJUSTMENT')),
            amount        NUMERIC NOT NULL,          -- positive for TOPUP, negative for USAGE
            invoice_id    INTEGER,
            invoice_no    TEXT,
            invoice_date  DATE,
            note          TEXT,
            created_at    TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_client_advance_payments_client_id
        ON client_advance_payments (client_id)
    """)


# ============================================================
# Shared helpers — also used by invoices.py during invoice creation
# ============================================================

def get_client_advance_balance(cur, client_id: int) -> Decimal:
    """Current advance balance for a client (can be negative — see spec)."""
    ensure_advance_table(cur)
    cur.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM client_advance_payments WHERE client_id = %s",
        (client_id,)
    )
    row = cur.fetchone()
    return Decimal(row[0]) if row and row[0] is not None else Decimal("0")


def record_advance_usage(cur, client_id: int, amount: float, invoice_id: int,
                          invoice_no: str, invoice_date, note: Optional[str] = None):
    """
    Deduct `amount` (the invoice's final total incl. VAT) from a client's advance
    balance by recording a USAGE ledger entry. Always records the FULL amount,
    even if it takes the balance negative (invoice generation is never blocked).
    """
    ensure_advance_table(cur)
    cur.execute("""
        INSERT INTO client_advance_payments
            (client_id, entry_type, amount, invoice_id, invoice_no, invoice_date, note)
        VALUES (%s, 'USAGE', %s, %s, %s, %s, %s)
    """, (client_id, -abs(float(amount)), invoice_id, invoice_no, invoice_date,
          note or f"Applied to invoice {invoice_no}"))


# ============================================================
# Pydantic models
# ============================================================

class ClientCreate(BaseModel):
    name: str
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None


class AdvanceTopUp(BaseModel):
    amount: float
    note: Optional[str] = None


# ============================================================
# 1. GET /advance-payments/clients — list all clients + advance balance
# ============================================================

@router.get("/clients", summary="List clients with their advance payment balance")
def list_clients_with_advance():
    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_advance_table(cur)
        cur.execute("""
            SELECT
                c.client_id, c.name, c.contact_person, c.email, c.phone, c.address,
                COALESCE(SUM(cap.amount), 0) AS advance_balance
            FROM clients c
            LEFT JOIN client_advance_payments cap ON cap.client_id = c.client_id
            GROUP BY c.client_id, c.name, c.contact_person, c.email, c.phone, c.address
            ORDER BY c.name ASC
        """)
        rows = cur.fetchall()
        return [
            {
                "client_id": r[0],
                "name": r[1],
                "contact_person": r[2],
                "email": r[3],
                "phone": r[4],
                "address": r[5],
                "advance_balance": float(r[6]) if r[6] is not None else 0.0,
            }
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ============================================================
# 2. POST /advance-payments/clients — "+ Add Client"
#    Reuses the existing clients table (no duplicate client system).
# ============================================================

@router.post("/clients", summary="Add a new client (reuses existing clients table)")
def add_client(payload: ClientCreate):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO clients (name, contact_person, email, phone, address, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING client_id
        """, (payload.name, payload.contact_person, payload.email,
              payload.phone, payload.address, datetime.utcnow()))
        client_id = cur.fetchone()[0]
        conn.commit()
        return {"message": "Client added successfully", "client_id": client_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ============================================================
# 3. POST /advance-payments/clients/{client_id}/topup — record an advance payment
# ============================================================

@router.post("/clients/{client_id}/topup", summary="Add an advance payment for a client")
def top_up_advance(client_id: int, payload: AdvanceTopUp):
    if payload.amount <= 0:
        raise HTTPException(400, "Advance amount must be greater than 0")

    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_advance_table(cur)

        cur.execute("SELECT client_id, name FROM clients WHERE client_id = %s", (client_id,))
        client_row = cur.fetchone()
        if not client_row:
            raise HTTPException(404, "Client not found")

        cur.execute("""
            INSERT INTO client_advance_payments (client_id, entry_type, amount, note)
            VALUES (%s, 'TOPUP', %s, %s)
        """, (client_id, payload.amount, payload.note or "Advance payment received"))

        new_balance = get_client_advance_balance(cur, client_id)
        conn.commit()

        return {
            "message": "Advance payment recorded successfully",
            "client_id": client_id,
            "advance_balance": float(new_balance),
        }
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ============================================================
# 4. GET /advance-payments/clients/{client_id}/balance — quick lookup
#    (used by Generate Invoice to decide whether to show "Advance Paid" option)
# ============================================================

@router.get("/clients/{client_id}/balance", summary="Get a client's current advance balance")
def get_advance_balance(client_id: int):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT client_id, name FROM clients WHERE client_id = %s", (client_id,))
        client_row = cur.fetchone()
        if not client_row:
            raise HTTPException(404, "Client not found")

        balance = get_client_advance_balance(cur, client_id)
        return {"client_id": client_id, "name": client_row[1], "advance_balance": float(balance)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ============================================================
# 5. GET /advance-payments/clients/{client_id}/history — full ledger + running balance
# ============================================================

@router.get("/clients/{client_id}/history", summary="Get a client's advance payment history")
def get_advance_history(client_id: int):
    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_advance_table(cur)

        cur.execute("SELECT client_id, name FROM clients WHERE client_id = %s", (client_id,))
        client_row = cur.fetchone()
        if not client_row:
            raise HTTPException(404, "Client not found")

        cur.execute("""
            SELECT id, entry_type, amount, invoice_id, invoice_no, invoice_date, note, created_at
            FROM client_advance_payments
            WHERE client_id = %s
            ORDER BY created_at ASC, id ASC
        """, (client_id,))
        rows = cur.fetchall()

        history = []
        running_balance = Decimal("0")
        for r in rows:
            (entry_id, entry_type, amount, invoice_id, invoice_no, invoice_date, note, created_at) = r
            amount_f = float(amount)
            running_balance += Decimal(amount)
            history.append({
                "id": entry_id,
                "entry_type": entry_type,
                "amount": amount_f,
                "invoice_id": invoice_id,
                "invoice_no": invoice_no,
                "invoice_date": invoice_date.isoformat() if invoice_date else None,
                "note": note,
                "created_at": created_at.isoformat() if created_at else None,
                "balance_after": float(running_balance),
            })

        # Most recent first for display
        history.reverse()

        return {
            "client_id": client_id,
            "name": client_row[1],
            "advance_balance": float(running_balance),
            "history": history,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()