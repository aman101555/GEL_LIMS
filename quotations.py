# quotations.py exe ready
import io
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from db import get_connection
from fastapi.responses import StreamingResponse
from template_processor import QuotationTemplateProcessor
from utils import resource_path

import requests # Don't forget to 'pip install requests'
from io import BytesIO


router = APIRouter(prefix="/quotations", tags=["3. Quotations"])

VAT_RATE = 0.05

RESEND_API_KEY = "re_NqwQ7LHE_5BRb671vNkFWpgcsoNVunSHX"
SENDER_DOMAIN = "glabint.com"


# ============================================================
# Pydantic Models
# ============================================================

class QuotationCreate(BaseModel):
    enquiry_id: int
    division: str
    payment_terms: Optional[str] = None
    prepared_under: Optional[str] = None  # Can be None or empty string
    validity_days: Optional[int] = 30


class QuotationItemCreate(BaseModel):
    description: str
    test_standard: Optional[str] = None
    unit_rate: float
    quantity: int
    net_unit: Optional[str] = None


class SendEmailPayload(BaseModel):
    from_local: str   # just the part before @, e.g. "info" or "test"
    to_email: str


class QuotationItemFromCatalog(BaseModel):
    catalog_id: int
    quantity: int

class ItemQuantityUpdate(BaseModel):
    quantity: int

class UnitRateUpdate(BaseModel):
    unit_rate: float

class ItemUpdate(BaseModel):
    quantity: Optional[int] = None
    unit_rate: Optional[float] = None
    test_standard: Optional[str] = None
    net_unit: Optional[str] = None

# ============================================================
# Generate Quotation Number with New Format (Thread-safe version)
# ============================================================

TEMPLATE_URLS = {
    "GEO": "https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/quotations/GEOT_fixed.docx",
    "SRV": "https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/quotations/SRV.docx",
    "DEFAULT": "https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/quotations/QT.docx"
}

def _generate_quotation_no(cur, division, prepared_under):
    """
    Quotation numbering rules:
    - GEO  -> QG  (separate series)
    - SRV  -> QS  (separate series)
    - ALL OTHER divisions -> QL (single running series)
    - Sequence is based ONLY on prefix + year
    - prepared_under (AR / AS / NONE) does NOT affect sequence
    """

    # Prefix mapping
    if division == 'GEO':
        prefix = 'QG'
    elif division == 'SRV':
        prefix = 'QS'
    else:
        prefix = 'QL'

    year_short = datetime.now().strftime('%y')
    year_full = datetime.now().year

    # Normalize prepared_under (used only for display, not counting)
    initials = None
    if prepared_under and prepared_under.strip().upper() not in ('', 'NONE'):
        initials = prepared_under.strip().upper()[:2]

    # Prevent race conditions
    cur.execute("LOCK TABLE quotations IN EXCLUSIVE MODE")

    # Get highest sequence for prefix + year ONLY
    cur.execute("""
        SELECT MAX(
            CAST(
                regexp_replace(
                    quotation_no,
                    '^[A-Z]+-(?:[A-Z]{2}-)?([0-9]{3})-.*$',
                    '\\1'
                ) AS INTEGER
            )
        )
        FROM quotations
        WHERE quotation_no LIKE %s
        AND EXTRACT(YEAR FROM created_at) = %s
    """, (f"{prefix}-%-{year_short}", year_full))

    max_seq = cur.fetchone()[0] or 0
    next_seq = max_seq + 1

    # Build quotation number
    if initials:
        quotation_no = f"{prefix}-{initials}-{next_seq:03d}-{year_short}"
    else:
        quotation_no = f"{prefix}-{next_seq:03d}-{year_short}"

    return quotation_no


# ============================================================
# 1️⃣ CREATE QUOTATION (Updated)
# ============================================================

@router.post("/", summary="Create Quotation")
def create_quotation(payload: QuotationCreate):
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Validate enquiry exists
        cur.execute("SELECT enquiry_id FROM enquiries WHERE enquiry_id = %s", (payload.enquiry_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, "Enquiry not found")
        
        # Validate division exists (prepared_under can be empty)
        if not payload.division:
            raise HTTPException(400, "Division is required")

        # Generate new quotation number (prepared_under can be None)
        quotation_no = _generate_quotation_no(cur, payload.division, payload.prepared_under)
        revision = 1

        # Check if quotation number already exists
        cur.execute("SELECT quotation_id FROM quotations WHERE quotation_no = %s", (quotation_no,))
        if cur.fetchone():
            quotation_no = _increment_quotation_no(cur, quotation_no)

        # Insert quotation with new format
        cur.execute("""
            INSERT INTO quotations (
                quotation_no, enquiry_id, division, revision,
                payment_terms, prepared_under, validity_days, status,
                total_amount, vat, grand_total, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'DRAFT', 0, 0, 0, NOW())
            RETURNING quotation_id
        """, (
            quotation_no, payload.enquiry_id, payload.division,
            revision, payload.payment_terms, payload.prepared_under,
            payload.validity_days
        ))

        qid = cur.fetchone()[0]
        conn.commit()

        return {
            "message": "Quotation created",
            "quotation_id": qid,
            "quotation_no": quotation_no
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# Helper function to increment if duplicate
def _increment_quotation_no(cur, quotation_no):
    """Increment the sequence number if quotation_no already exists"""
    parts = quotation_no.split('-')
    
    # Handle both formats: QG-AR-001-25 and QG-001-25
    if len(parts) == 4:
        # Format with initials: QG-AR-001-25
        try:
            seq_num = int(parts[2])
            new_seq = seq_num + 1
            return f"{parts[0]}-{parts[1]}-{new_seq:03d}-{parts[3]}"
        except:
            pass
    elif len(parts) == 3:
        # Format without initials: QG-001-25
        try:
            seq_num = int(parts[1])
            new_seq = seq_num + 1
            return f"{parts[0]}-{new_seq:03d}-{parts[2]}"
        except:
            pass
    
    return quotation_no


# ============================================================
# 2️⃣ ADD ITEM FROM PRICE CATALOG (AUTO-FILL)
# ============================================================

@router.post("/{quotation_id}/items/from-catalog", summary="Add Item From Price Catalog")
def add_item_from_catalog(quotation_id: int, payload: QuotationItemFromCatalog):
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Validate quotation exists
        cur.execute("SELECT quotation_id FROM quotations WHERE quotation_id = %s", (quotation_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, "Quotation not found")

        # Fetch catalog item
        cur.execute("""
            SELECT code, description, test_standard, unit_rate
            FROM price_catalog
            WHERE catalog_id = %s
        """, (payload.catalog_id,))
        catalog = cur.fetchone()

        if catalog is None:
            raise HTTPException(404, "Catalog item not found")

        code, description, test_standard, unit_rate = catalog

        if payload.quantity <= 0:
            raise HTTPException(400, "Quantity must be greater than zero")

        # Insert item
        cur.execute("""
            INSERT INTO quotation_items
                (quotation_id, item_code, description, test_standard, unit_rate, quantity)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING item_id
        """, (
            quotation_id, code, description, test_standard, unit_rate, payload.quantity
        ))

        item_id = cur.fetchone()[0]

        # Recalculate totals
        cur.execute("""
            UPDATE quotations
            SET total_amount = COALESCE(sub.total, 0),
                vat = COALESCE(sub.total, 0) * %s,
                grand_total = COALESCE(sub.total, 0) * (1 + %s)
            FROM (
                SELECT SUM(amount) AS total
                FROM quotation_items
                WHERE quotation_id = %s
            ) sub
            WHERE quotation_id = %s
            RETURNING total_amount, vat, grand_total
        """, (VAT_RATE, VAT_RATE, quotation_id, quotation_id))

        totals = cur.fetchone()
        conn.commit()

        return {
            "message": "Item added from catalog",
            "item_id": item_id,
            "quotation_id": quotation_id,
            "totals": {
                "total_amount": float(totals[0]),
                "vat": float(totals[1]),
                "grand_total": float(totals[2]),
            },
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))

    finally:
        cur.close()
        conn.close()


# ============================================================
# 3️⃣ ADD CUSTOM ITEM TO QUOTATION (MANUAL ENTRY)
# ============================================================

@router.post("/{quotation_id}/items", summary="Add Custom Item to Quotation")
def add_item(quotation_id: int, item: QuotationItemCreate):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT quotation_id FROM quotations WHERE quotation_id = %s", (quotation_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, "Quotation not found")

        if item.unit_rate < 0 or item.quantity <= 0:
            raise HTTPException(400, "Invalid unit_rate or quantity")

        # Insert manual item
        cur.execute("""
            INSERT INTO quotation_items (quotation_id, description, test_standard, unit_rate, quantity, net_unit)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING item_id
        """, (quotation_id, item.description, item.test_standard, item.unit_rate, item.quantity, item.net_unit))

        item_id = cur.fetchone()[0]

        # Recalculate totals
        cur.execute("""
            UPDATE quotations
            SET total_amount = COALESCE(sub.total, 0),
                vat = COALESCE(sub.total, 0) * %s,
                grand_total = COALESCE(sub.total, 0) * (1 + %s)
            FROM (
                SELECT SUM(amount) AS total
                FROM quotation_items
                WHERE quotation_id = %s
            ) sub
            WHERE quotations.quotation_id = %s
            RETURNING total_amount, vat, grand_total
        """, (VAT_RATE, VAT_RATE, quotation_id, quotation_id))

        totals = cur.fetchone()
        conn.commit()

        return {
            "message": "Item added",
            "item_id": item_id,
            "quotation_id": quotation_id,
            "totals": {
                "total_amount": float(totals[0]),
                "vat": float(totals[1]),
                "grand_total": float(totals[2]),
            },
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))

    finally:
        cur.close()
        conn.close()


# ============================================================
# 4️⃣ SEND QUOTATION
# ============================================================

@router.post("/{quotation_id}/send", summary="Send Quotation to Client")
def send_quotation(quotation_id: int):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            UPDATE quotations
            SET status = 'SENT', approved_at = NULL
            WHERE quotation_id = %s
            RETURNING quotation_id
        """, (quotation_id,))

        if cur.fetchone() is None:
            raise HTTPException(404, "Quotation not found")

        conn.commit()
        return {"message": "Quotation sent", "quotation_id": quotation_id}

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))

    finally:
        cur.close()
        conn.close()


# ============================================================
# 5️⃣ APPROVE QUOTATION
# ============================================================

@router.post("/{quotation_id}/approve", summary="Approve Quotation")
def approve_quotation(quotation_id: int):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            UPDATE quotations
            SET status = 'APPROVED', approved_at = NOW()
            WHERE quotation_id = %s
            RETURNING quotation_id, enquiry_id
        """, (quotation_id,))

        row = cur.fetchone()

        if row is None:
            raise HTTPException(404, "Quotation not found")

        conn.commit()

        return {
            "message": "Quotation approved",
            "quotation_id": row[0],
            "enquiry_id": row[1]
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))

    finally:
        cur.close()
        conn.close()


# ============================================================
# 6️⃣ REJECT QUOTATION
# ============================================================

@router.post("/{quotation_id}/reject", summary="Reject Quotation")
def reject_quotation(quotation_id: int):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            UPDATE quotations
            SET status = 'REJECTED'
            WHERE quotation_id = %s
            RETURNING quotation_id
        """, (quotation_id,))

        if cur.fetchone() is None:
            raise HTTPException(404, "Quotation not found")

        conn.commit()

        return {"message": "Quotation rejected", "quotation_id": quotation_id}

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))

    finally:
        cur.close()
        conn.close()


# ============================================================
# 7️⃣ CLARIFICATION REQUEST
# ============================================================

@router.post("/{quotation_id}/clarification", summary="Request Clarification")
def clarification_request(quotation_id: int):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            UPDATE quotations
            SET status = 'CLARIFICATION'
            WHERE quotation_id = %s
            RETURNING quotation_id
        """, (quotation_id,))

        if cur.fetchone() is None:
            raise HTTPException(404, "Quotation not found")

        conn.commit()

        return {"message": "Clarification added", "quotation_id": quotation_id}

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))

    finally:
        cur.close()
        conn.close()


# ============================================================
# 8️⃣ CREATE REVISION (Updated for new format)
# ============================================================

@router.post("/{quotation_id}/revision", summary="Create Revision of Quotation")
def create_revision(quotation_id: int):
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Get original quotation
        cur.execute("""
            SELECT quotation_no, enquiry_id, division, payment_terms,
                   prepared_under, validity_days, revision
            FROM quotations
            WHERE quotation_id = %s
        """, (quotation_id,))

        orig = cur.fetchone()

        if orig is None:
            raise HTTPException(404, "Quotation not found")

        (orig_no, enquiry_id, division, payment_terms, 
         prepared_under, validity_days, orig_rev) = orig
        
        new_rev = orig_rev + 1
        
        # Generate new quotation number for revision
        # For revisions, we keep the same base but increment revision
        parts = orig_no.split('-')
        if len(parts) == 4:
            # Format: QG-AR-001-25 -> QG-AR-001-25-V2
            new_q_no = f"{orig_no}-V{new_rev}"
        else:
            # Handle existing revisions: QG-AR-001-25-V1 -> QG-AR-001-25-V2
            base = orig_no.rsplit('-V', 1)[0]
            new_q_no = f"{base}-V{new_rev}"

        # Insert new revision
        cur.execute("""
            INSERT INTO quotations (
                quotation_no, enquiry_id, division, revision,
                payment_terms, prepared_under, validity_days, status,
                total_amount, vat, grand_total, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'DRAFT', 0, 0, 0, NOW())
            RETURNING quotation_id
        """, (
            new_q_no, enquiry_id, division, new_rev, 
            payment_terms, prepared_under, validity_days
        ))

        new_qid = cur.fetchone()[0]

        # Copy items
        cur.execute("""
            INSERT INTO quotation_items (quotation_id, description, test_standard, unit_rate, quantity)
            SELECT %s, description, test_standard, unit_rate, quantity
            FROM quotation_items
            WHERE quotation_id = %s
        """, (new_qid, quotation_id))

        # Recalculate totals
        cur.execute("""
            UPDATE quotations
            SET total_amount = sub.total,
                vat = sub.total * %s,
                grand_total = sub.total * (1 + %s)
            FROM (
                SELECT SUM(amount) AS total
                FROM quotation_items
                WHERE quotation_id = %s
            ) sub
            WHERE quotation_id = %s
            RETURNING total_amount, vat, grand_total
        """, (VAT_RATE, VAT_RATE, new_qid, new_qid))

        totals = cur.fetchone()
        conn.commit()

        return {
            "message": "Revision created",
            "new_quotation_id": new_qid,
            "new_quotation_no": new_q_no,
            "totals": {
                "total_amount": float(totals[0]),
                "vat": float(totals[1]),
                "grand_total": float(totals[2]),
            }
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ============================================================
# 9️⃣ LIST QUOTATIONS
# ============================================================

@router.get("/", summary="List All Quotations")
def list_quotations(limit: int = 100, offset: int = 0):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT q.quotation_id, q.quotation_no, q.division, q.revision,
                   q.status, q.total_amount, q.grand_total,
                   e.enquiry_ref, c.client_id, c.name, c.email
            FROM quotations q
            LEFT JOIN enquiries e ON q.enquiry_id = e.enquiry_id
            LEFT JOIN clients c ON e.client_id = c.client_id
            ORDER BY q.quotation_id DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))
        rows = cur.fetchall()
        return [
            {
                "quotation_id": r[0],
                "quotation_no": r[1],
                "division": r[2],
                "revision": r[3],
                "status": r[4],
                "total_amount": float(r[5] or 0),
                "grand_total": float(r[6] or 0),
                "enquiry_ref": r[7],
                "client_id": r[8],
                "client_name": r[9],
                "client_email": r[10],
            }
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()



# ============================================================
# GET /quotations/verified — For Create Project dropdown
# Only returns APPROVED quotations that have been verified (receipt + verified_at)
# ============================================================

@router.get("/verified", summary="List Verified Quotations (ready to become projects)")
def list_verified_quotations():
    """
    Returns only APPROVED quotations that have:
    - A receipt uploaded (receipt_path IS NOT NULL)
    - Been verified (verified_at IS NOT NULL)
    These are the only quotations eligible to be converted into projects.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT q.quotation_id, q.quotation_no, q.division, q.revision,
                   q.status, q.total_amount, q.grand_total,
                   e.enquiry_ref, c.client_id, c.name,
                   q.verified_at
            FROM quotations q
            LEFT JOIN enquiries e ON q.enquiry_id = e.enquiry_id
            LEFT JOIN clients c ON e.client_id = c.client_id
            WHERE q.status = 'APPROVED'
              AND q.receipt_path IS NOT NULL
              AND q.verified_at IS NOT NULL
            ORDER BY q.verified_at DESC
        """)

        rows = cur.fetchall()
        return [
            {
                "quotation_id": r[0],
                "quotation_no": r[1],
                "division": r[2],
                "revision": r[3],
                "status": r[4],
                "total_amount": float(r[5] or 0),
                "grand_total": float(r[6] or 0),
                "enquiry_ref": r[7],
                "client_id": r[8],
                "client_name": r[9],
                "verified_at": r[10].isoformat() if r[10] else None,
            }
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()

# quotations.py - Update the download_quotation function

# ============================================================
# DOWNLOAD QUOTATION (Updated with QT.docx as default for non-GEO/SRV)
# ============================================================

@router.get("/{quotation_id}/download", summary="Download Quotation as Word Document")
def download_quotation(quotation_id: int):
    """Generate and download quotation from Supabase Cloud template"""
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Fetch quotation details
        cur.execute("""
            SELECT 
                q.quotation_id, q.quotation_no, q.division, q.revision,
                q.status, q.total_amount, q.vat, q.grand_total,
                q.payment_terms, q.validity_days, q.created_at,
                e.enquiry_ref, e.project_name, e.location, 
                e.enquiry_date,
                c.client_id, c.name, c.contact_person, c.email,
                c.phone, c.address
            FROM quotations q
            LEFT JOIN enquiries e ON q.enquiry_id = e.enquiry_id
            LEFT JOIN clients c ON e.client_id = c.client_id
            WHERE q.quotation_id = %s
        """, (quotation_id,))

        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Quotation not found")
        
        division = row[2] 
        
        # Mapping for full names
        division_names = {'GEO': 'Geotechnical', 'SRV': 'Services', 'MAT': 'Material Testing'}
        division_full_name = division_names.get(division, division)

        # Fetch items
        cur.execute("""
            SELECT description, test_standard, unit_rate, quantity,
                   COALESCE(amount, unit_rate * quantity) as amount, 
                   COALESCE(unit, 'No.') as unit,
                   net_unit
            FROM quotation_items
            WHERE quotation_id = %s
            ORDER BY item_id
        """, (quotation_id,))

        items = [
            {"description": r[0], "test_standard": r[1], "unit_rate": float(r[2]),
             "quantity": r[3], "amount": float(r[4]), "unit": r[5], "net_unit": r[6]}
            for r in cur.fetchall()
        ]

        # Prepare data for Word processor
        quotation_data = {
            "quotation_id": row[0], "quotation_no": row[1], "division": division,
            "division_full_name": division_full_name, "revision": row[3],
            "total_amount": float(row[5] or 0), "vat": float(row[6] or 0),
            "grand_total": float(row[7] or 0), "payment_terms": row[8],
            "validity_days": row[9], "created_at": row[10], "enquiry_ref": row[11],
            "project_name": row[12] or "Proposed Project", "location": row[13] or "Dubai, UAE"
        }
        client_data = {
            "name": row[16] or "", "contact_person": row[17] or "",
            "email": row[18] or "", "phone": row[19] or "", "address": row[20] or ""
        }

        # --- SUPABASE CLOUD LOGIC START ---
        # 1. Select the correct URL
        template_url = TEMPLATE_URLS.get(division, TEMPLATE_URLS["DEFAULT"])
        
        # 2. Download from Supabase
        response = requests.get(template_url)
        if response.status_code != 200:
            raise HTTPException(500, f"Cloud Template for {division} not found at {template_url}")
        
        # 3. Create a virtual file in memory
        template_stream = BytesIO(response.content)
        
        # 4. Pass the stream to your processor
        template_processor = QuotationTemplateProcessor(template_stream)
        # --- SUPABASE CLOUD LOGIC END ---

        doc_bytes = template_processor.process_quotation(quotation_data, client_data, items)
        filename = f"Quotation_{quotation_data['quotation_no']}_{division}.docx"
        
        return StreamingResponse(
            io.BytesIO(doc_bytes.getvalue()),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except Exception as e:
        raise HTTPException(500, f"Generation failed: {str(e)}")
    finally:
        cur.close()
        conn.close()
# ============================================================
# 🔟 QUOTATION DETAILS
# ============================================================

@router.get("/{quotation_id}", summary="Get Quotation Details")
def quotation_details(quotation_id: int):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT q.quotation_id, q.quotation_no, q.division, q.revision,
                   q.status, q.total_amount, q.vat, q.grand_total,
                   q.payment_terms, q.validity_days,
                   e.enquiry_ref,
                   c.client_id, c.name, c.contact_person, c.email,
                   e.project_name, e.location
            FROM quotations q
            LEFT JOIN enquiries e ON q.enquiry_id = e.enquiry_id
            LEFT JOIN clients c ON e.client_id = c.client_id
            WHERE q.quotation_id = %s
        """, (quotation_id,))

        row = cur.fetchone()

        if not row:
            raise HTTPException(404, "Quotation not found")

        # Fetch items
        cur.execute("""
            SELECT description, test_standard, unit_rate, quantity, amount, net_unit
            FROM quotation_items
            WHERE quotation_id = %s
            ORDER BY item_id
        """, (quotation_id,))

        items = [
            {
                "description": r[0],
                "test_standard": r[1],
                "unit_rate": float(r[2]),
                "quantity": r[3],
                "amount": float(r[4]),
                "net_unit": r[5]
            }
            for r in cur.fetchall()
        ]

        return {
            "quotation_id": row[0],
            "quotation_no": row[1],
            "division": row[2],
            "revision": row[3],
            "status": row[4],
            "total_amount": float(row[5] or 0),
            "vat": float(row[6] or 0),
            "grand_total": float(row[7] or 0),
            "payment_terms": row[8],
            "validity_days": row[9],
            "enquiry_ref": row[10],
            "client_id": row[11],
            "client_name": row[12],
            "client_contact": row[13],
            "client_email": row[14],
            "project_name": row[15],
            "location": row[16],
            "items": items
        }

    except Exception as e:
        raise HTTPException(500, str(e))

    finally:
        cur.close()
        conn.close()


# ============================================================
# PRICE CATALOG ENDPOINT (ADD THIS TO YOUR quotations.py)
# ============================================================

@router.get("/price-catalog/", summary="Get Active Price Catalog Items")
def get_price_catalog():
    """Get all active items from price catalog for dropdown selection"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            SELECT catalog_id, code, description, test_standard, unit_rate, unit, active, group_name
            FROM price_catalog 
            WHERE active = true
            ORDER BY code
        """)
        
        items = cur.fetchall()
        
        return [
            {
                "catalog_id": item[0],
                "code": item[1],
                "description": item[2],
                "test_standard": item[3],
                "unit_rate": float(item[4]),
                "unit": item[5],
                "active": item[6],
                "group_name": item[7]
            }
            for item in items
        ]
        
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch price catalog: {str(e)}")
    finally:
        cur.close()
        conn.close()






# Add to quotations.py after existing endpoints

# ============================================================
# UPDATE ITEM QUANTITY
# ============================================================
# Update the UPDATE endpoint in quotations.py
@router.put("/{quotation_id}/items/{item_index}", summary="Update Item Quantity, Unit Rate, or Test Standard")
def update_item(quotation_id: int, item_index: int, payload: ItemUpdate):
    """Update quantity, unit rate, or test standard of a specific item in a quotation"""
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Validate quotation exists and is in DRAFT/APPROVED status
        cur.execute("""
            SELECT status FROM quotations 
            WHERE quotation_id = %s
        """, (quotation_id,))
        
        quote = cur.fetchone()
        
        if not quote:
            raise HTTPException(404, "Quotation not found")
        
        if quote[0] not in ['DRAFT', 'APPROVED']:
            raise HTTPException(400, "Only DRAFT or APPROVED quotations can be modified")
        
        # Get the item_id for the given index
        cur.execute("""
            SELECT item_id, unit_rate, quantity, test_standard, net_unit
            FROM quotation_items
            WHERE quotation_id = %s
            ORDER BY item_id
            OFFSET %s LIMIT 1
        """, (quotation_id, item_index))
        
        item = cur.fetchone()
        
        if not item:
            raise HTTPException(404, "Item not found")
        
        item_id, current_unit_rate, current_quantity, current_test_standard, current_net_unit = item
        
        # Check what fields are being updated
        quantity = payload.quantity
        unit_rate = payload.unit_rate
        test_standard = payload.test_standard
        net_unit = payload.net_unit
        
        update_field = None
        new_value = None
        current_value = None
        
        if quantity is not None:
            if quantity <= 0:
                raise HTTPException(400, "Quantity must be greater than zero")
            update_field = 'quantity'
            new_value = quantity
            current_value = current_quantity
        elif unit_rate is not None:
            if unit_rate < 0:
                raise HTTPException(400, "Unit rate cannot be negative")
            update_field = 'unit_rate'
            new_value = unit_rate
            current_value = current_unit_rate
        elif test_standard is not None:
            update_field = 'test_standard'
            new_value = test_standard
            current_value = current_test_standard
        elif net_unit is not None:
            update_field = 'net_unit'
            new_value = net_unit
            current_value = current_net_unit
        else:
            raise HTTPException(400, "Must provide quantity, unit_rate, test_standard, or net_unit")
        
        # Update the item
        if update_field == 'quantity':
            cur.execute("""
                UPDATE quotation_items
                SET quantity = %s
                WHERE item_id = %s
                RETURNING amount
            """, (new_value, item_id))
        elif update_field == 'unit_rate':
            cur.execute("""
                UPDATE quotation_items
                SET unit_rate = %s
                WHERE item_id = %s
                RETURNING amount
            """, (new_value, item_id))
        elif update_field == 'test_standard':
            cur.execute("""
                UPDATE quotation_items
                SET test_standard = %s
                WHERE item_id = %s
                RETURNING amount
            """, (new_value, item_id))
        elif update_field == 'net_unit':
            cur.execute("""
                UPDATE quotation_items
                SET net_unit = %s
                WHERE item_id = %s
                RETURNING amount
            """, (new_value, item_id))
        
        updated_amount = cur.fetchone()[0]
        
        # Only recalculate totals if quantity or unit rate changed
        if update_field in ['quantity', 'unit_rate']:
            cur.execute("""
                UPDATE quotations
                SET total_amount = COALESCE(sub.total, 0),
                    vat = COALESCE(sub.total, 0) * %s,
                    grand_total = COALESCE(sub.total, 0) * (1 + %s)
                FROM (
                    SELECT SUM(amount) AS total
                    FROM quotation_items
                    WHERE quotation_id = %s
                ) sub
                WHERE quotation_id = %s
                RETURNING total_amount, vat, grand_total
            """, (VAT_RATE, VAT_RATE, quotation_id, quotation_id))
            
            totals = cur.fetchone()
            total_amount = float(totals[0])
            vat = float(totals[1])
            grand_total = float(totals[2])
        else:
            # For test_standard updates, fetch current totals
            cur.execute("""
                SELECT total_amount, vat, grand_total
                FROM quotations
                WHERE quotation_id = %s
            """, (quotation_id,))
            totals = cur.fetchone()
            total_amount = float(totals[0])
            vat = float(totals[1])
            grand_total = float(totals[2])
        
        conn.commit()
        
        return {
            "message": f"Item {update_field} updated",
            "item_id": item_id,
            "update_field": update_field,
            "old_value": current_value if update_field not in ('test_standard', 'net_unit') else str(current_value or ''),
            "new_value": new_value if update_field not in ('test_standard', 'net_unit') else str(new_value or ''),
            "new_amount": float(updated_amount) if update_field in ['quantity', 'unit_rate'] else None,
            "totals": {
                "total_amount": total_amount,
                "vat": vat,
                "grand_total": grand_total,
            }
        }
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()
# ============================================================
# DELETE ITEM
# ============================================================

@router.delete("/{quotation_id}/items/{item_index}", summary="Delete Item from Quotation")
def delete_item(quotation_id: int, item_index: int):
    """Delete a specific item from a quotation"""
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Validate quotation exists and is in DRAFT status
        cur.execute("""
            SELECT status FROM quotations 
            WHERE quotation_id = %s
        """, (quotation_id,))
        
        quote = cur.fetchone()
        
        if not quote:
            raise HTTPException(404, "Quotation not found")
        
        if quote[0] not in ['DRAFT', 'APPROVED']:
            raise HTTPException(400, "Only DRAFT quotations can be modified")
        
        # Get the item_id for the given index
        cur.execute("""
            SELECT item_id
            FROM quotation_items
            WHERE quotation_id = %s
            ORDER BY item_id
            OFFSET %s LIMIT 1
        """, (quotation_id, item_index))
        
        item = cur.fetchone()
        
        if not item:
            raise HTTPException(404, "Item not found")
        
        item_id = item[0]
        
        # Delete the item
        cur.execute("""
            DELETE FROM quotation_items
            WHERE item_id = %s
        """, (item_id,))
        
        # Recalculate quotation totals
        cur.execute("""
            UPDATE quotations
            SET total_amount = COALESCE(sub.total, 0),
                vat = COALESCE(sub.total, 0) * %s,
                grand_total = COALESCE(sub.total, 0) * (1 + %s)
            FROM (
                SELECT SUM(amount) AS total
                FROM quotation_items
                WHERE quotation_id = %s
            ) sub
            WHERE quotation_id = %s
            RETURNING total_amount, vat, grand_total
        """, (VAT_RATE, VAT_RATE, quotation_id, quotation_id))
        
        totals = cur.fetchone()
        conn.commit()
        
        return {
            "message": "Item deleted",
            "item_id": item_id,
            "totals": {
                "total_amount": float(totals[0]),
                "vat": float(totals[1]),
                "grand_total": float(totals[2]),
            }
        }
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()

@router.post("/{quotation_id}/send-email", summary="Email Quotation as Attachment")
def send_quotation_email(quotation_id: int, payload: SendEmailPayload):
    import base64, httpx

    if not payload.from_local.strip():
        raise HTTPException(400, "From address is required")
    if not payload.to_email.strip():
        raise HTTPException(400, "To address is required")

    from_email = f"{payload.from_local.strip().lower()}@{SENDER_DOMAIN}"

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT q.quotation_id, q.quotation_no, q.division, q.revision,
                   q.status, q.total_amount, q.vat, q.grand_total,
                   q.payment_terms, q.validity_days, q.created_at,
                   e.enquiry_ref, e.project_name, e.location, e.enquiry_date,
                   c.client_id, c.name, c.contact_person, c.email,
                   c.phone, c.address
            FROM quotations q
            LEFT JOIN enquiries e ON q.enquiry_id = e.enquiry_id
            LEFT JOIN clients c ON e.client_id = c.client_id
            WHERE q.quotation_id = %s
        """, (quotation_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Quotation not found")

        division = row[2]
        division_names = {
            'GEO': 'Geotechnical', 'SRV': 'Services', 'MAT': 'Material Testing',
            'NDT': 'NDT', 'CHM': 'Chemical', 'BIO': 'Biological',
            'ENV': 'Environmental', 'SPJ': 'Special Jobs'
        }

        cur.execute("""
            SELECT description, test_standard, unit_rate, quantity,
                   COALESCE(amount, unit_rate * quantity) as amount,
                   COALESCE(unit, 'No.') as unit, net_unit
            FROM quotation_items
            WHERE quotation_id = %s ORDER BY item_id
        """, (quotation_id,))
        items = [
            {"description": r[0], "test_standard": r[1], "unit_rate": float(r[2]),
             "quantity": r[3], "amount": float(r[4]), "unit": r[5], "net_unit": r[6]}
            for r in cur.fetchall()
        ]

        quotation_data = {
            "quotation_id": row[0], "quotation_no": row[1], "division": division,
            "division_full_name": division_names.get(division, division),
            "revision": row[3],
            "total_amount": float(row[5] or 0), "vat": float(row[6] or 0),
            "grand_total": float(row[7] or 0), "payment_terms": row[8],
            "validity_days": row[9], "created_at": row[10],
            "enquiry_ref": row[11],
            "project_name": row[12] or "Proposed Project",
            "location": row[13] or "Dubai, UAE"
        }
        client_data = {
            "name": row[16] or "", "contact_person": row[17] or "",
            "email": row[18] or "", "phone": row[19] or "", "address": row[20] or ""
        }

        # Generate DOCX using exact same logic as download endpoint
        template_url = TEMPLATE_URLS.get(division, TEMPLATE_URLS["DEFAULT"])
        tmpl_response = requests.get(template_url)
        if tmpl_response.status_code != 200:
            raise HTTPException(500, f"Template fetch failed for division {division}")

        template_stream = BytesIO(tmpl_response.content)
        template_processor = QuotationTemplateProcessor(template_stream)
        doc_bytes = template_processor.process_quotation(quotation_data, client_data, items)

        doc_b64 = base64.b64encode(doc_bytes.getvalue()).decode()
        filename = f"Quotation_{quotation_data['quotation_no']}.docx"

        contact_name = client_data['contact_person'] or client_data['name'] or "Sir/Madam"
        created_date = quotation_data['created_at']
        if hasattr(created_date, 'strftime'):
            date_str = created_date.strftime("%d %B %Y")
        else:
            date_str = str(created_date)[:10]

        email_html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #333;">
            <div style="background: #1a1a2e; padding: 24px 32px; border-radius: 8px 8px 0 0;">
                <h1 style="color: #ffffff; margin: 0; font-size: 20px; font-weight: 500;">
                    Quotation {quotation_data['quotation_no']}
                </h1>
                <p style="color: #aaa; margin: 4px 0 0; font-size: 13px;">{date_str}</p>
            </div>
            <div style="background: #f9f9f9; padding: 28px 32px; border: 1px solid #e5e5e5; border-top: none;">
                <p style="margin: 0 0 16px;">Dear {contact_name},</p>
                <p style="margin: 0 0 16px; line-height: 1.6;">
                    Please find attached our quotation <strong>{quotation_data['quotation_no']}</strong>
                    for the project <strong>{quotation_data['project_name']}</strong>.
                </p>
                <div style="background: #fff; border: 1px solid #e0e0e0; border-radius: 6px; padding: 16px 20px; margin: 20px 0;">
                    <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
                        <tr>
                            <td style="padding: 5px 0; color: #666;">Quotation No.</td>
                            <td style="padding: 5px 0; text-align: right; font-weight: 500;">{quotation_data['quotation_no']}</td>
                        </tr>
                        <tr>
                            <td style="padding: 5px 0; color: #666;">Project</td>
                            <td style="padding: 5px 0; text-align: right;">{quotation_data['project_name']}</td>
                        </tr>
                        <tr>
                            <td style="padding: 5px 0; color: #666;">Total Amount (excl. VAT)</td>
                            <td style="padding: 5px 0; text-align: right;">AED {quotation_data['total_amount']:,.2f}</td>
                        </tr>
                        <tr>
                            <td style="padding: 5px 0; color: #666;">VAT (5%)</td>
                            <td style="padding: 5px 0; text-align: right;">AED {quotation_data['vat']:,.2f}</td>
                        </tr>
                        <tr style="border-top: 1px solid #eee;">
                            <td style="padding: 10px 0 5px; font-weight: 600; font-size: 15px;">Grand Total</td>
                            <td style="padding: 10px 0 5px; text-align: right; font-weight: 600; font-size: 15px; color: #1a1a2e;">AED {quotation_data['grand_total']:,.2f}</td>
                        </tr>
                    </table>
                </div>
                <p style="margin: 16px 0; line-height: 1.6; font-size: 14px; color: #555;">
                    This quotation is valid for <strong>{quotation_data['validity_days']} days</strong>
                    from the date of issue. Please review the attached document and feel free
                    to reach out if you have any questions or require clarification.
                </p>
                <p style="margin: 24px 0 0; font-size: 14px;">
                    Best regards,<br>
                    <strong>Global Engineering Laboratory</strong><br>
                    <span style="color: #888; font-size: 13px;">{from_email}</span>
                </p>
            </div>
            <div style="background: #f0f0f0; padding: 12px 32px; border-radius: 0 0 8px 8px; border: 1px solid #e5e5e5; border-top: none;">
                <p style="margin: 0; font-size: 11px; color: #999; text-align: center;">
                    Global Engineering · Dubai, UAE · www.glabint.com
                </p>
            </div>
        </div>
        """

        resend_response = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": f"Global Engineering Laboratory <{from_email}>",
                "to": [payload.to_email.strip()],
                "subject": f"Quotation {quotation_data['quotation_no']} – {quotation_data['project_name']}",
                "html": email_html,
                "attachments": [{"filename": filename, "content": doc_b64}]
            },
            timeout=30
        )

        if resend_response.status_code not in (200, 201):
            raise HTTPException(500, f"Email send failed: {resend_response.text}")

        return {"message": f"Quotation emailed successfully to {payload.to_email.strip()}"}

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()