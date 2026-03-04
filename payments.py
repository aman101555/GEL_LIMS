# payments.py
# FastAPI router for Payment Confirmation Section
# Handles: listing quotations by payment filter, uploading receipts to Supabase, verifying quotations

import os
import uuid
from fastapi import APIRouter, HTTPException, UploadFile, File, Query
from typing import Optional
from datetime import datetime
from db import get_connection

import requests  # pip install requests

router = APIRouter(prefix="/payments", tags=["Payments"])

# ============================================================
# Supabase Config
# Bucket 'receipts' must be set to PUBLIC in Supabase dashboard.
# Anon key is safe to use here — it only allows uploads to public buckets.
# ============================================================
SUPABASE_URL = "https://hqwgkmbjmcxpxbwccclo.supabase.co"
SUPABASE_BUCKET = "receipts"

# Set in your environment: SUPABASE_ANON_KEY=your_anon_key
# Found in: Supabase Dashboard → Settings → API → anon/public key
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imhxd2drbWJqbWN4cHhid2NjY2xvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjkzNjA3MjcsImV4cCI6MjA4NDkzNjcyN30.lyvXvkNe5dYeXj6_zVGReHZDph-QZm35BFp6RcgB0Gk"


def supabase_headers():
    return {
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "apikey": SUPABASE_ANON_KEY,
    }


# ============================================================
# DB Migration — run once in your Supabase SQL editor:
#   ALTER TABLE quotations ADD COLUMN IF NOT EXISTS receipt_path TEXT;
#   ALTER TABLE quotations ADD COLUMN IF NOT EXISTS verified_at TIMESTAMP;
# ============================================================


# ============================================================
# 1. GET /payments/ — List quotations with payment info
# ============================================================

@router.get("/", summary="List Quotations for Payment Confirmation")
def list_payment_quotations(filter: Optional[str] = Query(None)):
    """
    Returns APPROVED quotations with payment/receipt info.
    filter options: CASH, CREDIT, VERIFIED
    If no filter, returns all APPROVED quotations.
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        base_query = """
            SELECT
                q.quotation_id,
                q.quotation_no,
                q.division,
                q.payment_terms,
                q.grand_total,
                q.receipt_path,
                q.verified_at,
                c.name
            FROM quotations q
            LEFT JOIN enquiries e ON q.enquiry_id = e.enquiry_id
            LEFT JOIN clients c ON e.client_id = c.client_id
            WHERE q.status = 'APPROVED'
        """

        params = []

        if filter == "VERIFIED":
            base_query += " AND q.verified_at IS NOT NULL"
        elif filter:
            # Dynamic filter: matches any payment_terms containing the filter value (case-insensitive)
            base_query += " AND UPPER(q.payment_terms) LIKE %s"
            params.append(f"%{filter.upper()}%")

        base_query += " ORDER BY q.created_at DESC"

        cur.execute(base_query, params)
        rows = cur.fetchall()

        return [
            {
                "quotation_id": r[0],
                "quotation_no": r[1],
                "division": r[2],
                "payment_terms": r[3],
                "grand_total": float(r[4]) if r[4] else 0.0,
                "receipt_path": r[5],
                "verified_at": r[6].isoformat() if r[6] else None,
                "client_name": r[7],
            }
            for r in rows
        ]

    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ============================================================
# 2. POST /payments/{quotation_id}/upload-receipt
# ============================================================

@router.post("/{quotation_id}/upload-receipt", summary="Upload Receipt to Supabase Storage")
async def upload_receipt(quotation_id: int, file: UploadFile = File(...)):
    """
    Uploads a receipt file (PDF/image) to Supabase Storage bucket 'receipts'.
    Updates quotation.receipt_path and sets status to PAID (via receipt presence).
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Validate quotation
        cur.execute(
            "SELECT quotation_id, quotation_no FROM quotations WHERE quotation_id = %s AND status = 'APPROVED'",
            (quotation_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Quotation not found or not in APPROVED status")

        quotation_no = row[1]

        # Read file
        contents = await file.read()
        if len(contents) > 20 * 1024 * 1024:  # 20MB limit
            raise HTTPException(400, "File too large. Maximum size is 20MB.")

        # Determine extension
        original_ext = os.path.splitext(file.filename)[1].lower() if file.filename else ".bin"
        allowed_exts = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif"}
        if original_ext not in allowed_exts:
            raise HTTPException(400, f"File type not allowed. Allowed: {', '.join(allowed_exts)}")

        # Unique path in bucket
        unique_name = f"{quotation_no}_{uuid.uuid4().hex[:8]}{original_ext}"
        storage_path = f"quotations/{unique_name}"

        # Upload to Supabase Storage
        upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{storage_path}"
        headers = {
            **supabase_headers(),
            "Content-Type": file.content_type or "application/octet-stream",
            "x-upsert": "true",
        }

        resp = requests.post(upload_url, data=contents, headers=headers)

        if resp.status_code not in (200, 201):
            raise HTTPException(500, f"Supabase upload failed: {resp.text}")

        # Save path to DB
        cur.execute(
            "UPDATE quotations SET receipt_path = %s WHERE quotation_id = %s",
            (storage_path, quotation_id)
        )
        conn.commit()

        return {
            "message": "Receipt uploaded successfully",
            "quotation_id": quotation_id,
            "receipt_path": storage_path,
        }

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ============================================================
# 3. GET /payments/{quotation_id}/receipt-url
# ============================================================

@router.get("/{quotation_id}/receipt-url", summary="Get Public URL for Receipt")
def get_receipt_url(quotation_id: int):
    """
    Returns the public URL to view the receipt from Supabase Storage.
    Works because the 'receipts' bucket is set to public.
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            "SELECT receipt_path FROM quotations WHERE quotation_id = %s",
            (quotation_id,)
        )
        row = cur.fetchone()
        if not row or not row[0]:
            raise HTTPException(404, "No receipt found for this quotation")

        receipt_path = row[0]
        public_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{receipt_path}"

        return {"url": public_url, "receipt_path": receipt_path}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ============================================================
# 4. POST /payments/{quotation_id}/verify
# ============================================================

from pydantic import BaseModel

class SuperAdminCredentials(BaseModel):
    username: str
    password: str

@router.post("/{quotation_id}/verify-with-credentials", summary="Verify a Quotation using superadmin credentials")
def verify_with_credentials(quotation_id: int, credentials: SuperAdminCredentials):
    """
    Verifies a quotation after checking that the provided credentials
    belong to an active super_admin user. Does NOT log the user in —
    only performs this one verification action.
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        # 1. Validate that the provided credentials belong to a super_admin
        cur.execute("""
            SELECT user_id FROM users
            WHERE username = %s
              AND password_hash = %s
              AND LOWER(user_role) = 'super_admin'
              AND is_active = true
        """, (credentials.username, credentials.password))

        admin_row = cur.fetchone()
        if not admin_row:
            raise HTTPException(403, "Invalid credentials or user is not a Super Admin")

        # 2. Validate the quotation
        cur.execute(
            "SELECT receipt_path, verified_at FROM quotations WHERE quotation_id = %s AND status = 'APPROVED'",
            (quotation_id,)
        )
        row = cur.fetchone()

        if not row:
            raise HTTPException(404, "Quotation not found or not APPROVED")

        receipt_path, verified_at = row

        if not receipt_path:
            raise HTTPException(400, "Cannot verify quotation without an uploaded receipt")

        if verified_at:
            raise HTTPException(400, "Quotation is already verified")

        # 3. Mark as verified
        cur.execute(
            "UPDATE quotations SET verified_at = %s WHERE quotation_id = %s",
            (datetime.utcnow(), quotation_id)
        )
        conn.commit()

        return {"message": "Quotation verified successfully", "quotation_id": quotation_id}

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


@router.post("/{quotation_id}/verify", summary="Verify a Quotation (mark as verified)")
def verify_quotation(quotation_id: int):
    """
    Marks a quotation as verified (sets verified_at timestamp).
    Only quotations with a receipt uploaded can be verified.
    Only verified quotations can be converted into projects.
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            "SELECT receipt_path, verified_at FROM quotations WHERE quotation_id = %s AND status = 'APPROVED'",
            (quotation_id,)
        )
        row = cur.fetchone()

        if not row:
            raise HTTPException(404, "Quotation not found or not APPROVED")

        receipt_path, verified_at = row

        if not receipt_path:
            raise HTTPException(400, "Cannot verify quotation without an uploaded receipt")

        if verified_at:
            raise HTTPException(400, "Quotation is already verified")

        cur.execute(
            "UPDATE quotations SET verified_at = %s WHERE quotation_id = %s",
            (datetime.utcnow(), quotation_id)
        )
        conn.commit()

        return {"message": "Quotation verified successfully", "quotation_id": quotation_id}

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()