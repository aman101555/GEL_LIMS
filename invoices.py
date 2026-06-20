from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Literal
from datetime import date, datetime
from db import get_connection
from decimal import Decimal
from fastapi.responses import HTMLResponse
import traceback
from utils import resource_path  # ADD THIS LINE
import requests
import tempfile


import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from fastapi.responses import FileResponse
import openpyxl
from datetime import datetime
import os

router = APIRouter(prefix="/invoices", tags=["6. Invoices"])

# ----------------------------
# Pydantic Models
# ----------------------------
class InvoiceItemCreate(BaseModel):
    quotation_item_id: int
    quantity: int = 1
    sample_id: Optional[int] = None

class InvoiceCreate(BaseModel):
    project_id: int
    invoice_type: Literal['CASH', 'CREDIT', 'PROFORMA', 'TAX']
    payment_method: Optional[Literal['CASH', 'CREDIT']] = 'CASH'  # NEW
    invoice_date: Optional[date] = None
    client_reference: Optional[str] = None
    lpo_reference: Optional[str] = None
    lpo_date: Optional[date] = None
    payment_terms: Optional[str] = "30 days"
    services_description: Optional[str] = None
    remarks: Optional[str] = None
    payment_status: Optional[str] = "UNPAID"


class InvoiceOut(BaseModel):
    invoice_id: int
    invoice_no: str
    project_id: int
    invoice_type: str
    payment_method: str  # NEW
    invoice_date: date
    client_reference: Optional[str]
    lpo_reference: Optional[str]
    lpo_date: Optional[date]
    payment_terms: Optional[str]
    subtotal: float
    vat: float
    total: float
    amount_in_words: str
    services_description: Optional[str]
    remarks: Optional[str]
    payment_status: str
    paid_date: Optional[date]
    items: List[dict]
    project_details: dict


# =====================================================
# NEW: Models for Invoice Reports
# =====================================================
class InvoiceReportSelection(BaseModel):
    project_id: int
    selected_report_ids: Optional[List[int]] = None
    include_all_reports: bool = True
    invoice_type: Literal['PROFORMA', 'TAX']  # New field

# ----------------------------
# Utility Functions - FIXED
# ----------------------------
# ----------------------------
# Utility Functions - FIXED
# ----------------------------
def number_to_words(num: float) -> str:
    """Convert number to words for amount in words field"""
    # First, separate whole dirhams and fils
    whole_part = int(num)
    decimal_part = round((num - whole_part) * 100)
    
    def convert_less_than_thousand(n):
        ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]
        teens = ["Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", 
                 "Seventeen", "Eighteen", "Nineteen"]
        tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]
        
        if n == 0:
            return ""
        elif n < 10:
            return ones[n]
        elif n < 20:
            return teens[n - 10]
        elif n < 100:
            return tens[n // 10] + (" " + ones[n % 10] if n % 10 != 0 else "")
        else:
            return ones[n // 100] + " Hundred" + (" " + convert_less_than_thousand(n % 100) if n % 100 != 0 else "")
    
    if whole_part == 0:
        words = "Zero"
    else:
        # Handle millions
        millions = whole_part // 1000000
        remainder = whole_part % 1000000
        
        # Handle thousands
        thousands = remainder // 1000
        remainder = remainder % 1000
        
        words_parts = []
        
        if millions > 0:
            words_parts.append(convert_less_than_thousand(millions) + " Million")
        
        if thousands > 0:
            words_parts.append(convert_less_than_thousand(thousands) + " Thousand")
        
        if remainder > 0:
            words_parts.append(convert_less_than_thousand(remainder))
        
        words = " and ".join(words_parts)
        
        # Capitalize first letter
        words = words.strip()
    
    # Add decimal part if exists
    if decimal_part > 0:
        result = f"{words} Dirhams and {convert_less_than_thousand(decimal_part)} Fils Only"
    else:
        result = f"{words} Dirhams Only"
    
    return result

def generate_invoice_no(cur, invoice_type: str) -> str:
    """Generate invoice number with different systems for PROFORMA vs other invoices"""
    # Get the last 2 digits of current year
    year_short = str(datetime.now().year)[-2:]
    
    print(f"DEBUG generate_invoice_no: invoice_type='{invoice_type}', year_short='{year_short}'")
    
    # SPECIAL CASE FOR PROFORMA INVOICES - Reset each year starting from 001
    if invoice_type.upper() == 'PROFORMA':
        print("DEBUG: Generating PROFORMA invoice number")
        
        # Get the last PROFORMA invoice number for this year
        cur.execute("""
            SELECT invoice_no 
            FROM invoices 
            WHERE invoice_type = 'PROFORMA'
            AND invoice_no LIKE %s
            ORDER BY invoice_id DESC 
            LIMIT 1
        """, (f'%/{year_short}',))
        
        last_proforma = cur.fetchone()
        print(f"DEBUG: Last PROFORMA invoice found: {last_proforma}")
        
        if last_proforma and last_proforma[0]:
            try:
                # Get the number part
                last_number_str = last_proforma[0].split('/')[0]
                last_number = int(last_number_str)
                
                # If it's a new format invoice (001, 002, etc.), increment normally
                if last_number <= 999:
                    next_number = last_number + 1
                    invoice_no = f"{next_number:03d}/{year_short}"
                    print(f"DEBUG: Incremented new format PROFORMA: {last_number} -> {next_number}")
                else:
                    # Old format invoice, start new format from 001
                    next_number = 1
                    invoice_no = f"{next_number:03d}/{year_short}"
                    print(f"DEBUG: Old format found, starting new format from 001")
            except (ValueError, IndexError):
                # If parsing fails, start from 001
                next_number = 1
                invoice_no = f"{next_number:03d}/{year_short}"
                print(f"DEBUG: Parse failed, starting from 001")
        else:
            # No PROFORMA invoice for this year yet, start from 001
            next_number = 1
            invoice_no = f"{next_number:03d}/{year_short}"
            print(f"DEBUG: No existing PROFORMA, starting from 001")
        
        print(f"DEBUG: Generated PROFORMA invoice_no: {invoice_no}")
        return invoice_no
    
    # ORIGINAL LOGIC FOR OTHER INVOICE TYPES (CASH, CREDIT, TAX)
    print(f"DEBUG: Generating non-PROFORMA invoice number for type: {invoice_type}")
    
    # Get the max invoice number overall for non-PROFORMA invoices
    cur.execute("""
        SELECT invoice_no 
        FROM invoices 
        WHERE invoice_type != 'PROFORMA'
        ORDER BY invoice_id DESC 
        LIMIT 1
    """)
    
    last_invoice = cur.fetchone()
    print(f"DEBUG: Last non-PROFORMA invoice found: {last_invoice}")
    
    if last_invoice and last_invoice[0]:
        # Parse existing invoice number
        try:
            last_number_str = last_invoice[0].split('/')[0]
            last_number = int(last_number_str)
            next_number = last_number + 1
            print(f"DEBUG: Parsed last non-PROFORMA number: {last_number}, next: {next_number}")
        except (ValueError, IndexError) as e:
            print(f"DEBUG: Failed to parse non-PROFORMA number '{last_invoice[0]}': {e}")
            # If parsing fails, start from 36001
            next_number = 36001
    else:
        # First invoice ever (non-PROFORMA)
        print("DEBUG: First non-PROFORMA invoice ever")
        next_number = 36001
    
    invoice_no = f"{next_number}/{year_short}"
    print(f"DEBUG: Generated non-PROFORMA invoice_no: {invoice_no}")
    return invoice_no
def ensure_delivery_note_reports_table(cur):
    """Ensure the delivery_note_reports junction table exists"""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS delivery_note_reports (
            id SERIAL PRIMARY KEY,
            delivery_note_no VARCHAR(50) NOT NULL,
            report_no VARCHAR(50) NOT NULL,  -- Changed from VARCHAR(100) to VARCHAR(50)
            UNIQUE(delivery_note_no, report_no)
        )
    """)
    # Note: removed included_at column since your SQL table doesn't have it


# ----------------------------
# Assignment Logic - SAME AS WORKSHEET GENERATION
# ----------------------------

def get_assigned_test_for_sample(sample_id: int, cur):
    """Get the assigned test for a sample - SAME LOGIC AS WORKSHEET GENERATION"""
    # Get sample's test request
    cur.execute("SELECT request_id FROM samples WHERE sample_id = %s", (sample_id,))
    sample_data = cur.fetchone()
    if not sample_data:
        return None
    
    request_id = sample_data[0]
    
    # Get all tests for this request
    cur.execute("""
        SELECT qi.item_id, qi.item_code, qi.description, qi.test_standard, qi.unit_rate,
               tri.quantity, tri.tri_id,
               ROW_NUMBER() OVER (ORDER BY tri.tri_id) as test_index
        FROM test_request_items tri
        JOIN quotation_items qi ON tri.quotation_item_id = qi.item_id
        WHERE tri.test_request_id = %s
        ORDER BY tri.tri_id
    """, (request_id,))
    
    test_items = cur.fetchall()
    
    if not test_items:
        return None
    
    # Get all samples for this request to find position
    cur.execute("SELECT sample_id FROM samples WHERE request_id = %s ORDER BY sample_id", (request_id,))
    all_samples = [row[0] for row in cur.fetchall()]
    
    # ✅ SAME LOGIC AS WORKSHEET: Calculate assigned test
    sample_position = all_samples.index(sample_id)
    assigned_test_index = sample_position % len(test_items)
    
    return test_items[assigned_test_index]


def get_sample_ids_for_reports(report_ids: List[int], cur) -> List[int]:
    """
    Resolve a list of report_id values to the underlying sample_id values
    they cover. A report always has a primary sample_id, and may additionally
    cover other samples via the covers_samples array (stored as sample_no text).
    """
    if not report_ids:
        return []

    cur.execute("""
        SELECT report_id, sample_id, covers_samples
        FROM reports
        WHERE report_id = ANY(%s)
    """, (report_ids,))
    rows = cur.fetchall()

    sample_id_set = set()
    covers_samples_nos = set()

    for report_id, sample_id, covers_samples in rows:
        if sample_id:
            sample_id_set.add(sample_id)
        if covers_samples:
            covers_samples_nos.update(covers_samples)

    if covers_samples_nos:
        cur.execute("""
            SELECT sample_id FROM samples WHERE sample_no = ANY(%s)
        """, (list(covers_samples_nos),))
        sample_id_set.update(row[0] for row in cur.fetchall())

    return list(sample_id_set)


def get_report_info_for_samples(sample_ids: List[int], cur) -> dict:
    """
    Reverse lookup of get_sample_ids_for_reports: given a list of sample_id
    values, return a dict mapping sample_id -> {"report_no", "created_at"}
    for the report that covers each sample (either as its primary sample_id,
    or via the covers_samples array).

    If a sample happens to be covered by more than one report, the most
    recently created one wins (ORDER BY r.created_at DESC, so the first
    match found per sample is kept).
    """
    if not sample_ids:
        return {}

    result = {}

    # Primary match: reports.sample_id = the invoice item's sample_id
    cur.execute("""
        SELECT sample_id, report_no, created_at
        FROM reports
        WHERE sample_id = ANY(%s)
        ORDER BY created_at DESC
    """, (sample_ids,))
    for sample_id, report_no, created_at in cur.fetchall():
        result.setdefault(sample_id, {"report_no": report_no, "created_at": created_at})

    # Remaining samples (not matched as a primary sample_id) may still be
    # covered via a report's covers_samples array (stored as sample_no text).
    remaining = [sid for sid in sample_ids if sid not in result]
    if remaining:
        cur.execute("""
            SELECT sample_id, sample_no
            FROM samples
            WHERE sample_id = ANY(%s)
        """, (remaining,))
        sample_no_by_id = {row[0]: row[1] for row in cur.fetchall()}
        sample_nos = [sample_no_by_id[sid] for sid in remaining if sid in sample_no_by_id]

        if sample_nos:
            cur.execute("""
                SELECT covers_samples, report_no, created_at
                FROM reports
                WHERE covers_samples IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM unnest(covers_samples) cs WHERE cs = ANY(%s)
                )
                ORDER BY created_at DESC
            """, (sample_nos,))
            for covers_samples, report_no, created_at in cur.fetchall():
                if not covers_samples:
                    continue
                for sid in remaining:
                    sno = sample_no_by_id.get(sid)
                    if sno and sno in covers_samples and sid not in result:
                        result[sid] = {"report_no": report_no, "created_at": created_at}

    return result


def get_project_quotation_items(project_id: int, cur, sample_ids: Optional[List[int]] = None):
    """
    Get invoiceable items - USING EXACT WORKSHEET LOGIC.

    If `sample_ids` is provided, only those samples are considered (used when
    the caller has already selected a specific set of reports to invoice).
    Otherwise every sample in the project is used, as before.
    """
    if sample_ids is not None:
        # Restrict to the explicitly given samples, but still verify they
        # belong to this project.
        if not sample_ids:
            return []
        cur.execute("""
            SELECT s.sample_id
            FROM projects p
            JOIN test_requests tr ON p.project_id = tr.project_id
            JOIN samples s ON tr.test_request_id = s.request_id
            WHERE p.project_id = %s AND s.sample_id = ANY(%s)
            ORDER BY s.sample_id
        """, (project_id, sample_ids))
    else:
        # Get all samples for this project
        cur.execute("""
            SELECT s.sample_id 
            FROM projects p
            JOIN test_requests tr ON p.project_id = tr.project_id
            JOIN samples s ON tr.test_request_id = s.request_id
            WHERE p.project_id = %s
            ORDER BY s.sample_id
        """, (project_id,))
    
    sample_ids_resolved = [row[0] for row in cur.fetchall()]
    
    filtered_items = []
    for sample_id in sample_ids_resolved:
        assigned_test = get_assigned_test_for_sample(sample_id, cur)
        if assigned_test:
            item_id, item_code, description, test_standard, unit_rate, quantity, tri_id, test_index = assigned_test
            
            # Get sample details
            cur.execute("SELECT sample_no, status FROM samples WHERE sample_id = %s", (sample_id,))
            sample_data = cur.fetchone()
            if sample_data:
                sample_no, sample_status = sample_data
            else:
                sample_no, sample_status = f"SMP-{sample_id}", "PENDING"
            
            # Get test request details
            cur.execute("""
                SELECT tr.test_request_id, tr.request_no 
                FROM test_requests tr 
                WHERE tr.test_request_id = (SELECT request_id FROM samples WHERE sample_id = %s)
            """, (sample_id,))
            request_data = cur.fetchone()
            if request_data:
                test_request_id, request_no = request_data
            else:
                test_request_id, request_no = None, "UNKNOWN"
            
            filtered_items.append((
                item_id, description, test_standard, unit_rate, 1,  # Quantity always 1 per sample
                test_request_id, request_no, sample_id, sample_no, sample_status
            ))
    
    return filtered_items

def get_invoice_complete(invoice_id: int, cur):
    """Get complete invoice details with items - FIXED for PROFORMA/TAX filtered totals with payment_method support"""
    # Get invoice header with payment_method
    cur.execute("""
        SELECT i.invoice_id, i.invoice_no, i.project_id, i.invoice_type, i.payment_method, i.invoice_date,
               i.client_reference, i.lpo_reference, i.lpo_date, i.payment_terms,
               i.subtotal, i.vat, i.total, i.amount_in_words, i.services_description, 
               i.remarks, i.payment_status, i.paid_date,
               p.project_no, p.project_name, p.location,
               c.client_id, c.name, c.contact_person, c.email, c.address, c.phone
        FROM invoices i
        JOIN projects p ON i.project_id = p.project_id
        JOIN clients c ON p.client_id = c.client_id
        WHERE i.invoice_id = %s
    """, (invoice_id,))
    
    header = cur.fetchone()
    if not header:
        raise HTTPException(status_code=404, detail="Invoice not found")
    
    # Get invoice items
    cur.execute("""
        SELECT 
            ii.item_id, 
            ii.description, 
            ii.test_standard, 
            ii.unit_rate, 
            ii.quantity, 
            ii.amount, 
            ii.sample_id, 
            s.sample_no,
            s.status as sample_status,
            tr.request_no,
            tr.test_request_id
        FROM invoice_items ii
        LEFT JOIN samples s ON ii.sample_id = s.sample_id
        LEFT JOIN test_requests tr ON ii.test_request_id = tr.test_request_id
        WHERE ii.invoice_id = %s
        ORDER BY ii.item_id
    """, (invoice_id,))
    
    items_data = cur.fetchall()
    
    # Get invoice type
    invoice_type = header[3]
    payment_method = header[4]  # NEW: Get payment_method
    
    # =====================================================
    # NOTE: invoice_items is now correctly scoped at invoice-creation
    # time (only samples belonging to the reports the user actually
    # selected are inserted), so no further fuzzy re-filtering by
    # description/test-type text matching happens here. That used to
    # exist as a second, independent filter and could wrongly include
    # or exclude items when two reports had similar test-type names.
    # =====================================================
    
    # Build items list
    items_list = []
    filtered_subtotal = 0.0
    
    for item in items_data:
        item_amount = float(item[5]) if isinstance(item[5], Decimal) else item[5]
        filtered_subtotal += item_amount
        
        items_list.append({
            "item_id": item[0],
            "description": item[1],
            "test_standard": item[2],
            "unit_rate": float(item[3]) if isinstance(item[3], Decimal) else item[3],
            "quantity": item[4],
            "amount": item_amount,
            "sample_id": item[6],
            "sample_no": item[7],
            "sample_status": item[8],
            "request_no": item[9],
            "test_request_id": item[10]
        })
    
    # =====================================================
    # NEW: Calculate CORRECT totals for filtered items
    # =====================================================
    if invoice_type in ["PROFORMA", "TAX"] and items_list:
        # Recalculate based on filtered items
        filtered_vat = filtered_subtotal * 0.05
        filtered_total = filtered_subtotal + filtered_vat
        filtered_amount_words = number_to_words(filtered_total)
        
        # Check if different from original
        original_subtotal = float(header[10]) if isinstance(header[10], Decimal) else header[10]
        
        if abs(original_subtotal - filtered_subtotal) > 0.01:
            print(f"DEBUG: Using FILTERED totals for {invoice_type} invoice {invoice_id}")
            print(f"  Original: Subtotal={original_subtotal}")
            print(f"  Filtered: Subtotal={filtered_subtotal}, Total={filtered_total}")
            
            subtotal = filtered_subtotal
            vat = filtered_vat
            total = filtered_total
            amount_words = filtered_amount_words
        else:
            # Use original totals
            subtotal = original_subtotal
            vat = float(header[11]) if isinstance(header[11], Decimal) else header[11]
            total = float(header[12]) if isinstance(header[12], Decimal) else header[12]
            amount_words = header[13]
    else:
        # For other invoice types, use original totals
        subtotal = float(header[10]) if isinstance(header[10], Decimal) else header[10]
        vat = float(header[11]) if isinstance(header[11], Decimal) else header[11]
        total = float(header[12]) if isinstance(header[12], Decimal) else header[12]
        amount_words = header[13]
    
    # Return the complete invoice data
    return {
        "invoice_id": header[0],
        "invoice_no": header[1],
        "project_id": header[2],
        "invoice_type": invoice_type,
        "payment_method": payment_method,  # NEW
        "invoice_date": header[5],
        "client_reference": header[6],
        "lpo_reference": header[7],
        "lpo_date": header[8],
        "payment_terms": header[9],
        "subtotal": subtotal,
        "vat": vat,
        "total": total,
        "amount_in_words": amount_words,
        "services_description": header[14],
        "remarks": header[15],
        "payment_status": header[16],
        "paid_date": header[17],
        "items": items_list,
        "project_details": {
            "project_no": header[18],
            "project_name": header[19],
            "location": header[20],
            "client_id": header[21],
            "client_name": header[22],
            "client_contact": header[23],
            "client_email": header[24],
            "client_address": header[25],
            "client_phone": header[26]
        },
        # Add these new fields to help frontend understand what's happening
        "is_filtered": invoice_type in ["PROFORMA", "TAX"] and len(items_list) > 0,
        "filtered_item_count": len(items_list),
        "original_total": float(header[12]) if isinstance(header[12], Decimal) else header[12]
    }
# ----------------------------
# CREATE INVOICE - FIXED DATA TYPES AND QUERIES
# ----------------------------
# ----------------------------
# CREATE INVOICE - FIXED DATA TYPES AND QUERIES
# ----------------------------
@router.post("/with-payment-method", response_model=InvoiceOut)
def create_invoice_with_payment_method(payload: InvoiceCreate):
    """
    Create a new invoice for a project with payment_method support.
    (Public API route - always invoices every item in the project.)
    """
    return _create_invoice_with_payment_method_impl(payload, sample_ids=None)


def _create_invoice_with_payment_method_impl(payload: InvoiceCreate, sample_ids: Optional[List[int]] = None):
    """
    Shared implementation for creating an invoice.

    `sample_ids`, when provided, restricts which project samples/tests get
    pulled onto the invoice (used when the caller has already resolved a
    specific set of selected reports down to their underlying samples).
    When None, every sample in the project is invoiced (original behavior).
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        # ---------------------------------------------------
        # 1. Get project details with client info
        # ---------------------------------------------------
        cur.execute("""
            SELECT p.project_id, p.project_no, p.project_name, p.location, p.lpo_no, p.lpo_date,
                   c.client_id, c.name, c.contact_person, c.email, c.address, c.phone,
                   q.quotation_no
            FROM projects p
            JOIN clients c ON p.client_id = c.client_id
            JOIN quotations q ON p.quotation_id = q.quotation_id
            WHERE p.project_id = %s
        """, (payload.project_id,))
        project_data = cur.fetchone()
        if not project_data:
            raise HTTPException(status_code=404, detail="Project not found")

        # ---------------------------------------------------
        # 2. Get invoiceable items from project (optionally restricted
        #    to a specific set of samples resolved from selected reports)
        # ---------------------------------------------------
        invoiceable_items = get_project_quotation_items(payload.project_id, cur, sample_ids=sample_ids)
        if not invoiceable_items:
            raise HTTPException(status_code=400, detail="No test items available for invoicing")

        # ---------------------------------------------------
        # 2a. Group invoiceable items by description, test standard, unit rate
        # ---------------------------------------------------
        from collections import defaultdict

        grouped_items = defaultdict(lambda: {
            "description": "",
            "test_standard": "",
            "unit_rate": 0,
            "quantity": 0,
            "sample_ids": []
        })

        for item in invoiceable_items:
            item_id, description, test_standard, unit_rate, quantity, test_request_id, request_no, sample_id, sample_no, sample_status = item
            key = (description, test_standard, unit_rate)
            grouped_items[key]["description"] = description
            grouped_items[key]["test_standard"] = test_standard
            grouped_items[key]["unit_rate"] = unit_rate
            grouped_items[key]["quantity"] += quantity
            grouped_items[key]["sample_ids"].append(sample_id)

        # Convert grouped items to a list
        final_items = []
        for (desc, std, rate), data in grouped_items.items():
            final_items.append((desc, std, rate, data["quantity"], data["sample_ids"]))

        # ---------------------------------------------------
        # 3. Generate invoice number
        # ---------------------------------------------------
        invoice_no = generate_invoice_no(cur, payload.invoice_type)

        # ---------------------------------------------------
        # 4. Calculate totals
        # ---------------------------------------------------
        subtotal = 0.0
        for item in final_items:
            desc, std, unit_rate, quantity, item_sample_ids = item
            unit_rate_float = float(unit_rate) if isinstance(unit_rate, Decimal) else unit_rate
            subtotal += unit_rate_float * quantity

        vat = subtotal * 0.05  # 5% VAT
        total = subtotal + vat
        amount_words = number_to_words(total)

        # ---------------------------------------------------
        # 5. LPO and payment terms
        # ---------------------------------------------------
        lpo_reference = payload.lpo_reference or project_data[4]
        lpo_date = payload.lpo_date or project_data[5]
        
        # Set payment terms based on payment_method
        if payload.payment_method == "CREDIT":
            payment_terms = payload.payment_terms or "30 days"
        else:
            payment_terms = "Immediate"

        # ---------------------------------------------------
        # 6. Insert invoice header with payment_method
        # ---------------------------------------------------
        cur.execute("""
            INSERT INTO invoices (
                invoice_no, project_id, invoice_type, payment_method, invoice_date,
                client_reference, lpo_reference, lpo_date, payment_terms,
                subtotal, vat, total, amount_in_words, services_description, remarks,
                payment_status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING invoice_id
        """, (
            invoice_no,
            payload.project_id,
            payload.invoice_type,
            payload.payment_method,
            payload.invoice_date or date.today(),
            payload.client_reference,
            lpo_reference,
            lpo_date,
            payment_terms,
            subtotal,
            vat,
            total,
            amount_words,
            payload.services_description or f"Testing services for {project_data[2]}",
            payload.remarks,
            "UNPAID"  # Default status
        ))
        invoice_id = cur.fetchone()[0]

        # ---------------------------------------------------
        # 7. Insert grouped invoice items
        # ---------------------------------------------------
        for item in final_items:
            description, test_standard, unit_rate, quantity, item_sample_ids = item
            unit_rate_float = float(unit_rate) if isinstance(unit_rate, Decimal) else unit_rate
            amount = unit_rate_float * quantity
            sample_id = item_sample_ids[0] if item_sample_ids else None  # representative sample

            cur.execute("""
                INSERT INTO invoice_items (
                    invoice_id, description, test_standard,
                    unit_rate, quantity, amount, sample_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                invoice_id,
                description,
                test_standard,
                unit_rate_float,
                quantity,
                amount,
                sample_id
            ))

        # ---------------------------------------------------
        # 8. Commit transaction
        # ---------------------------------------------------
        conn.commit()

        # ---------------------------------------------------
        # 9. Return complete invoice
        # ---------------------------------------------------
        return get_invoice_complete(invoice_id, cur)

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# ----------------------------
# LIST INVOICES - FIXED
# ----------------------------
@router.get("/", response_model=List[InvoiceOut])
def list_invoices(limit: int = 100, offset: int = 0):
    conn = get_connection()
    cur = conn.cursor()
# Add debug print in Excel generation function
    try:
        cur.execute("""
            SELECT invoice_id FROM invoices 
            ORDER BY invoice_id DESC 
            LIMIT %s OFFSET %s
        """, (limit, offset))
        
        invoice_ids = [row[0] for row in cur.fetchall()]
        invoices = []
        
        for inv_id in invoice_ids:
            try:
                invoices.append(get_invoice_complete(inv_id, cur))
            except HTTPException as he:
                print(f"WARNING: Skipping invoice {inv_id} in list view: {he.detail}")
                continue
            except Exception as e:
                print(f"WARNING: Skipping invoice {inv_id} in list view due to error: {e}")
                traceback.print_exc()
                continue
        
        return invoices
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# ----------------------------
# GET SINGLE INVOICE
# ----------------------------
@router.get("/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: int):
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        return get_invoice_complete(invoice_id, cur)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# ----------------------------
# UPDATE PAYMENT STATUS
# ----------------------------
@router.post("/{invoice_id}/payment-status")
def update_payment_status(invoice_id: int, status: str, paid_date: Optional[date] = None):
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            UPDATE invoices 
            SET payment_status = %s, paid_date = %s
            WHERE invoice_id = %s
            RETURNING invoice_id
        """, (status, paid_date, invoice_id))
        
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Invoice not found")
        
        conn.commit()
        return {"message": "Payment status updated", "invoice_id": invoice_id, "status": status}
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# ----------------------------
# GET PROJECT INVOICEABLE ITEMS - FIXED
# ----------------------------
@router.get("/projects/{project_id}/invoiceable-items")
def get_invoiceable_items(project_id: int):
    """Get all items that can be invoiced for a project - FIXED"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        items = get_project_quotation_items(project_id, cur)
        
        return {
            "project_id": project_id,
            "invoiceable_items": [
                {
                    "quotation_item_id": item[0],
                    "description": item[1],
                    "test_standard": item[2],
                    "unit_rate": float(item[3]) if isinstance(item[3], Decimal) else item[3],
                    "quantity": item[4],
                    "test_request_id": item[5],
                    "request_no": item[6],
                    "sample_id": item[7],
                    "sample_no": item[8],
                    "sample_status": item[9]
                }
                for item in items
            ]
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()



# Add this import at the top with other imports
from fastapi import Query

# Add this DELETE endpoint after the other endpoints
@router.delete("/{invoice_id}")
def delete_invoice(invoice_id: int):
    """
    Delete an invoice and its items from the database.
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # First, get invoice details for response
        cur.execute("""
            SELECT invoice_no FROM invoices WHERE invoice_id = %s
        """, (invoice_id,))
        
        invoice_data = cur.fetchone()
        if not invoice_data:
            raise HTTPException(status_code=404, detail="Invoice not found")
        
        # Delete invoice items first (foreign key constraint)
        cur.execute("DELETE FROM invoice_items WHERE invoice_id = %s", (invoice_id,))
        
        # Delete the invoice
        cur.execute("DELETE FROM invoices WHERE invoice_id = %s RETURNING invoice_id", (invoice_id,))
        
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Invoice not found")
        
        conn.commit()
        
        return {
            "message": f"Invoice {invoice_data[0]} deleted successfully",
            "deleted_invoice_id": invoice_id,
            "invoice_no": invoice_data[0]
        }
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()











# INVOICES 
@router.get("/{invoice_id}/excel")
def generate_excel_invoice(invoice_id: int):
    """
    Generate Excel invoice using the template, insert rows dynamically,
    fill test items, report numbers, amounts, totals and save on server.
    """

    # Path to converted template (.xlsx)
    template_path = download_template_from_supabase("invoice")

    if not os.path.exists(template_path):
        raise HTTPException(status_code=404, detail="Invoice template not found. Convert invoice.xls → .xlsx")

    conn = get_connection()
    cur = conn.cursor()

    try:
        # =====================================================
        # 1. Load full invoice from DB
        # =====================================================
        invoice = get_invoice_complete(invoice_id, cur)
        project_details = invoice.get("project_details", {})
        
        # Get invoice type to determine if we should filter by reports
        invoice_type = invoice.get("invoice_type", "CASH")
        
        # =====================================================
        # NEW: Filter items for PROFORMA/TAX invoices
        # =====================================================
        if invoice_type in ["PROFORMA", "TAX"]:
            print(f"DEBUG: Filtering items for {invoice_type} invoice {invoice_id}")
            
            # Get reports linked to this invoice
            cur.execute("""
                SELECT report_no 
                FROM invoice_report_links 
                WHERE invoice_id = %s 
                AND invoice_type = %s
            """, (invoice_id, invoice_type))
            
            linked_reports = [row[0] for row in cur.fetchall()]
            print(f"DEBUG: Found {len(linked_reports)} linked reports: {linked_reports}")
            
            # Filter invoice items to only include those with linked reports
            original_items = invoice.get("items", [])
            filtered_items = []
            
            if linked_reports:
                # For each report, find matching invoice items
                for report_no in linked_reports:
                    # Get test type from report
                    cur.execute("""
                        SELECT covers_test_type 
                        FROM reports 
                        WHERE report_no = %s
                    """, (report_no,))
                    
                    report_data = cur.fetchone()
                    if report_data:
                        test_type = report_data[0]
                        print(f"DEBUG: Report {report_no} covers test type: '{test_type}'")
                        
                        # Find invoice items that match this test type
                        for item in original_items:
                            if item.get("description") == test_type:
                                filtered_items.append(item)
                                print(f"DEBUG: Added item '{item.get('description')}' for report {report_no}")
                                break  # Only add one item per report
            else:
                # No linked reports, use all items
                filtered_items = original_items
                print("DEBUG: No linked reports found, using all items")
            
            # Update invoice items with filtered list
            invoice["items"] = filtered_items
            print(f"DEBUG: Filtered from {len(original_items)} to {len(filtered_items)} items")
            
            # =====================================================
            # FIX: RECALCULATE TOTALS based on filtered items only
            # =====================================================
            # Calculate new totals from filtered items
            filtered_subtotal = sum(item.get("amount", 0) for item in filtered_items)
            filtered_vat = filtered_subtotal * 0.05  # 5% VAT
            filtered_total = filtered_subtotal + filtered_vat
            filtered_amount_words = number_to_words(filtered_total)
            
            # Update invoice totals with filtered amounts
            invoice["subtotal"] = filtered_subtotal
            invoice["vat"] = filtered_vat
            invoice["total"] = filtered_total
            invoice["amount_in_words"] = filtered_amount_words
            
            print(f"DEBUG: Recalculated totals for filtered items:")
            print(f"  Subtotal: {filtered_subtotal} (was {invoice.get('subtotal', 'original')})")
            print(f"  VAT: {filtered_vat} (was {invoice.get('vat', 'original')})")
            print(f"  Total: {filtered_total} (was {invoice.get('total', 'original')})")
            
        else:
            # For CASH/CREDIT invoices, use all items and original totals
            invoice["items"] = invoice.get("items", [])
            print("DEBUG: Using original totals for CASH/CREDIT invoice")
        
        items = invoice.get("items", [])
        
        # DEBUG: Print what data we're getting
        print("=== DEBUG INVOICE DATA ===")
        print(f"Invoice: {invoice.get('invoice_no')} (Type: {invoice_type})")
        print(f"Subtotal: {invoice.get('subtotal')}")
        print(f"VAT (5%): {invoice.get('vat')}")
        print(f"Total: {invoice.get('total')}")
        print(f"Number of items after filtering: {len(items)}")
        for i, item in enumerate(items):
            print(f"  Item {i}: Sample {item.get('sample_id')} - {item.get('description')} - Qty: {item.get('quantity')}")
        print("==========================")

        # =====================================================
        # 2. Load the Excel template
        # =====================================================
        wb = openpyxl.load_workbook(template_path, data_only=False)
        ws = wb.active

                # =====================================================
        # NEW: Set title in cell A3 based on invoice type
        # =====================================================
        title_text = ""
        if invoice_type == "PROFORMA":
            title_text = "PROFORMA INVOICE"
        elif invoice_type == "TAX":
            title_text = "TAX INVOICE"
        elif invoice_type == "CASH":
            title_text = "CASH INVOICE"
        elif invoice_type == "CREDIT":
            title_text = "CREDIT INVOICE"
        else:
            title_text = "INVOICE"  # Default
        
        # Set the title text in cell A3
        ws["A3"] = title_text
        
        # Apply formatting: Size 12, Arial, Dark Blue color
        from openpyxl.styles import Font
        
        # Create dark blue color (RGB: 0, 0, 139)
        dark_blue_color = "00008B"  # Hex code for dark blue
        
        # Apply font formatting
        ws["A3"].font = Font(
            name="Arial",
            size=12,
            bold=True,  # Usually titles are bold
            color=dark_blue_color  # Dark blue text
        )
        
        # Optional: Center align the title horizontally
        from openpyxl.styles import Alignment
        ws["A3"].alignment = Alignment(horizontal="center")

        # =====================================================
        # 3. Define Template Structure
        # =====================================================
        FIRST_ITEM_ROW = 18  # First item row in template (based on your image)
        LAST_TEMPLATE_ITEM_ROW = 34  # Last available item row in template
        TOTAL_ROW = 35  # Row with "GRAND TOTAL"
        VAT_ROW = 36    # Row with "VAT @ 5%"
        NET_TOTAL_ROW = 37  # Row with "NET TOTAL"
        AMOUNT_WORDS_ROW = 38  # Row with "United Arab Emirates Dirhams Only"
        
        # Calculate available template rows for items
        TEMPLATE_ITEM_ROWS = LAST_TEMPLATE_ITEM_ROW - FIRST_ITEM_ROW + 1  # 17 rows

        # =====================================================
        # 4. Fill Header Fields - WITH PAYMENT TERMS UPDATE
        # =====================================================
        ws["I4"] = invoice.get("invoice_no", " - ")
        
        # Invoice date
        invoice_date = invoice.get("invoice_date")
        if invoice_date:
            if isinstance(invoice_date, str):
                ws["I5"] = invoice_date
            else:
                ws["I5"] = invoice_date.strftime("%d-%b-%Y")
        else:
            ws["I5"] = " - "

        # Client Section
        ws["A5"] = project_details.get("client_name", " - ")
        ws["C10"] = project_details.get("client_contact", " - ")

        # Project details
        ws["C13"] = project_details.get("project_no", " - ")
        ws["C15"] = project_details.get("project_name", " - ")
        ws["C14"] = project_details.get("location", " - ")

        # LPO
        lpo_reference = invoice.get("lpo_reference", " - ")
        ws["C18"] = lpo_reference
        
        # LPO date
        lpo_date = invoice.get("lpo_date")
        if lpo_date:
            if isinstance(lpo_date, str):
                ws["I6"] = lpo_date
            else:
                ws["I6"] = lpo_date.strftime("%d-%b-%Y")
        else:
            ws["I6"] = " - "

        # Payment Terms - Show CASH or CREDIT based on invoice_type
        invoice_type = invoice.get("invoice_type", "CASH")
        if invoice_type == "CASH":
            payment_display = "CASH / Immediate"
        else:
            payment_display = "CREDIT / 30 days"
            
        ws["I8"] = payment_display

        # =====================================================
        # 5. Handle Dynamic Item Rows - FIXED LOGIC
        # =====================================================
        # We'll determine this after we process items
        # Clear all existing item rows first
        for row in range(FIRST_ITEM_ROW, LAST_TEMPLATE_ITEM_ROW + 1):
            for col in ['A', 'B', 'D', 'E', 'I', 'J', 'K']:
                ws[f"{col}{row}"].value = None

        # =====================================================
        # 6. CORRECTED: Get reports by TEST TYPE not just by sample
        # =====================================================
        print("=== GETTING REPORTS GROUPED BY TEST TYPE ===")

        # FIRST: Get all reports that are linked to this specific invoice
        cur.execute("""
            SELECT report_no 
            FROM invoice_report_links 
            WHERE invoice_id = %s
        """, (invoice_id,))

        linked_report_nos = [row[0] for row in cur.fetchall()]
        print(f"DEBUG: Invoice {invoice_id} has {len(linked_report_nos)} linked reports: {linked_report_nos}")

        if linked_report_nos:
            # This invoice has linked reports (PROFORMA/TAX invoice with selected reports)
            # ONLY fetch the linked reports
            cur.execute("""
                SELECT 
                    r.report_no,
                    r.created_at,
                    r.covers_test_type,
                    r.sample_id,
                    COUNT(DISTINCT s.sample_id) as sample_count
                FROM reports r
                LEFT JOIN samples s ON (
                    r.sample_id = s.sample_id 
                    OR 
                    (r.covers_samples IS NOT NULL AND s.sample_id::text = ANY(r.covers_samples))
                )
                LEFT JOIN test_requests tr ON s.request_id = tr.test_request_id
                WHERE r.report_no = ANY(%s)
                AND r.status = 'APPROVED'
                GROUP BY r.report_no, r.created_at, r.covers_test_type, r.sample_id
                ORDER BY r.report_no
            """, (linked_report_nos,))
        else:
            # This invoice has no linked reports (regular CASH/CREDIT invoice)
            # Get all approved reports for the project
            cur.execute("""
                SELECT 
                    r.report_no,
                    r.created_at,
                    r.covers_test_type,
                    r.sample_id,
                    COUNT(DISTINCT s.sample_id) as sample_count
                FROM reports r
                LEFT JOIN samples s ON (
                    r.sample_id = s.sample_id 
                    OR 
                    (r.covers_samples IS NOT NULL AND s.sample_id::text = ANY(r.covers_samples))
                )
                LEFT JOIN test_requests tr ON s.request_id = tr.test_request_id
                WHERE tr.project_id = %s
                AND r.status = 'APPROVED'
                GROUP BY r.report_no, r.created_at, r.covers_test_type, r.sample_id
                ORDER BY r.report_no
            """, (invoice.get("project_id"),))

        all_reports = cur.fetchall()
        
        print(f"Found {len(all_reports)} approved reports for project")
        
        # Create mapping: test_type -> list of reports
        test_type_to_reports = {}
        for report in all_reports:
            report_no, created_at, test_type, sample_id, sample_count = report
            if test_type:
                if test_type not in test_type_to_reports:
                    test_type_to_reports[test_type] = []
                test_type_to_reports[test_type].append({
                    "report_no": report_no,
                    "created_at": created_at,
                    "sample_id": sample_id,
                    "sample_count": sample_count
                })
                print(f"Report {report_no}: Test Type = '{test_type}', Sample ID = {sample_id}, Covers {sample_count} samples")

        print("\n=== TEST TYPE TO REPORTS MAPPING ===")
        for test_type, reports in test_type_to_reports.items():
            print(f"Test Type: '{test_type}' has {len(reports)} reports:")
            for report in reports:
                print(f"  - {report['report_no']}")
        print("====================================\n")

        # =====================================================
        # 7. Match invoice items to reports by TEST TYPE
        # =====================================================
        print("=== MATCHING INVOICE ITEMS TO REPORTS ===")
        
        matched_items = []
        unmatched_items = []
        
        for index, item in enumerate(items):
            sample_id = item.get("sample_id")
            description = item.get("description", " - ")
            test_standard = item.get("test_standard", " - ")
            unit_rate = float(item.get("unit_rate", 0))
            quantity = int(item.get("quantity", 0))
            amount = float(item.get("amount", 0))
            
            print(f"\nProcessing invoice item {index}:")
            print(f"  Sample: {sample_id}")
            print(f"  Description: '{description}'")
            print(f"  Test Standard: '{test_standard}'")
            
            # Try to find a report for this test type
            matched_report = None
            
            # First: Check if we have any reports for this exact test type
            if description in test_type_to_reports and test_type_to_reports[description]:
                # Use the first report for this test type
                report_info = test_type_to_reports[description][0]
                matched_report = {
                    "report_no": report_info["report_no"],
                    "created_at": report_info["created_at"],
                    "test_type": description
                }
                print(f"  ✓ Found report by test type: {report_info['report_no']}")
                
                # Remove this report from available list so we don't reuse it
                test_type_to_reports[description].pop(0)
                
            # Second: Try fuzzy match if exact match not found
            if not matched_report:
                for test_type, reports in test_type_to_reports.items():
                    if reports and (test_type in description or description in test_type):
                        report_info = reports[0]
                        matched_report = {
                            "report_no": report_info["report_no"],
                            "created_at": report_info["created_at"],
                            "test_type": test_type
                        }
                        print(f"  ≈ Found report by fuzzy match: {report_info['report_no']} (Test: '{test_type}' matches '{description}')")
                        reports.pop(0)
                        break
            
            # Third: If still no match, create a placeholder
            if not matched_report:
                print(f"  ✗ No report found for test type '{description}'")
                matched_report = {
                    "report_no": f"INV-{invoice.get('invoice_no')}-{index+1}",
                    "created_at": None,
                    "test_type": description
                }
                unmatched_items.append(item)
            
            # Format date
            if matched_report["created_at"]:
                report_date = matched_report["created_at"].strftime("%d-%b-%Y") if hasattr(matched_report["created_at"], 'strftime') else str(matched_report["created_at"])
            else:
                report_date = " - "
            
            matched_items.append({
                "report_no": matched_report["report_no"],
                "report_date": report_date,
                "description": matched_report["test_type"],  # Use report's test type
                "test_standard": test_standard,
                "unit_rate": unit_rate,
                "quantity": quantity,
                "amount": amount,
                "sample_id": sample_id,
                "original_description": description  # Keep original for reference
            })
        
        # =====================================================
        # 8. GROUP by report number (combine same reports)
        # =====================================================
        print("\n=== GROUPING BY REPORT NUMBER ===")
        
        report_grouping = {}
        for item in matched_items:
            report_no = item["report_no"]
            
            if report_no not in report_grouping:
                report_grouping[report_no] = {
                    "report_no": report_no,
                    "report_date": item["report_date"],
                    "description": item["description"],
                    "test_standard": item["test_standard"],
                    "unit_rate": item["unit_rate"],
                    "total_quantity": 0,
                    "total_amount": 0.0,
                    "samples": [],
                    "sample_count": 0
                }
            
            # Accumulate quantities and amounts
            report_grouping[report_no]["total_quantity"] += item["quantity"]
            report_grouping[report_no]["total_amount"] += item["amount"]
            report_grouping[report_no]["samples"].append(item["sample_id"])
            report_grouping[report_no]["sample_count"] += 1
        
        # Convert to list
        grouped_items = list(report_grouping.values())
        
        print(f"Created {len(grouped_items)} grouped items from {len(matched_items)} invoice items")
        for i, item in enumerate(grouped_items):
            print(f"Group {i+1}: {item['report_no']} - {item['description']} - Qty: {item['total_quantity']} - Amount: AED {item['total_amount']}")

        # =====================================================
        # 9. Determine if we need extra rows
        # =====================================================
        num_items_to_display = len(grouped_items)

        # Calculate where items will actually go
        if num_items_to_display <= TEMPLATE_ITEM_ROWS:
            # Case 1: Items fit within template rows
            last_item_row = FIRST_ITEM_ROW + num_items_to_display - 1
        else:
            # Case 2: Need more rows than template provides
            rows_needed = num_items_to_display - TEMPLATE_ITEM_ROWS
            
            # Insert rows AFTER the template item area (after row 34)
            ws.insert_rows(LAST_TEMPLATE_ITEM_ROW + 1, amount=rows_needed)
            
            # Copy formatting from last template row (row 34) to new rows
            from copy import copy
            for i in range(rows_needed):
                new_row = LAST_TEMPLATE_ITEM_ROW + 1 + i
                # Copy formatting from row 34
                for col in range(1, 12):  # Columns A-K
                    source_cell = ws.cell(row=34, column=col)
                    target_cell = ws.cell(row=new_row, column=col)
                    target_cell.font = copy(source_cell.font)
                    target_cell.border = copy(source_cell.border)
                    target_cell.fill = copy(source_cell.fill)
                    target_cell.number_format = source_cell.number_format
                    target_cell.alignment = copy(source_cell.alignment)
            
            last_item_row = LAST_TEMPLATE_ITEM_ROW + rows_needed

        # =====================================================
        # 10. Fill rows with matched data
        # =====================================================
        print("\n=== FILLING EXCEL ROWS ===")
        for index, item in enumerate(grouped_items):
            # Determine which row to use
            if index < TEMPLATE_ITEM_ROWS:
                # Use template rows (18-34)
                row = FIRST_ITEM_ROW + index
            else:
                # Use newly inserted rows
                extra_index = index - TEMPLATE_ITEM_ROWS
                row = LAST_TEMPLATE_ITEM_ROW + 1 + extra_index

            # Fill columns A–K
            ws[f"A{row}"] = item["report_no"]
            ws[f"B{row}"] = item["report_date"]
            ws[f"D{row}"] = item["description"]
            ws[f"E{row}"] = item["test_standard"]
            ws[f"I{row}"] = item["total_quantity"]
            ws[f"J{row}"] = item["unit_rate"]
            ws[f"K{row}"] = item["total_amount"]
            
            print(f"Row {row}: {item['report_no']} - {item['description']} x{item['total_quantity']} = AED {item['total_amount']}")

        # =====================================================
        # 11. UPDATE FORMULAS - FIXED
        # =====================================================
        print(f"\n=== UPDATING EXCEL FORMULAS ===")
        print(f"First item row: {FIRST_ITEM_ROW}")
        print(f"Last item row: {last_item_row}")
        print(f"Items displayed: {num_items_to_display}")
        
        # IMPORTANT: Update the SUM formula to cover ALL item rows
        invoice_subtotal = float(invoice.get("subtotal", 0))
        invoice_vat = float(invoice.get("vat", 0))
        invoice_total = float(invoice.get("total", 0)) 

        ws["K35"] = round(invoice_subtotal, 2)
        ws["K36"] = round(invoice_vat, 2)
        ws["K37"] = round(invoice_total, 2) 
        
        # Amount in words - USE THE RECALCULATED VALUE for PROFORMA/TAX invoices
        ws["B38"] = invoice.get("amount_in_words", " - ")
        
        print(f"K35 formula: =SUM(K{FIRST_ITEM_ROW}:K{last_item_row})")
        print(f"K36 formula: {ws['K36'].value}")
        print(f"K37 formula: {ws['K37'].value}")
        print(f"B38 amount in words: {invoice.get('amount_in_words', ' - ')}")
        
        # =====================================================
        # 12. Verify calculations match database
        # =====================================================
        # Calculate what Excel should show
        excel_subtotal = sum(item.get("total_amount", 0) for item in grouped_items)
        excel_vat = excel_subtotal * 0.05
        excel_total = excel_subtotal + excel_vat
        
        # Use the invoice totals (which are now recalculated for PROFORMA/TAX invoices)
        db_subtotal = float(invoice.get("subtotal", 0))
        db_vat = float(invoice.get("vat", 0))
        db_total = float(invoice.get("total", 0))
        
        print(f"\n=== VERIFICATION ===")
        print(f"Database values -> Subtotal: {db_subtotal}, VAT: {db_vat}, Total: {db_total}")
        print(f"Excel will show -> Subtotal: {excel_subtotal}, VAT: {excel_vat}, Total: {excel_total}")
        
        # Check if they match
        if abs(db_subtotal - excel_subtotal) > 0.01:
            print(f"WARNING: Subtotal mismatch! Database: {db_subtotal}, Excel: {excel_subtotal}")
        
        if abs(db_total - excel_total) > 0.01:
            print(f"WARNING: Total mismatch! Database: {db_total}, Excel: {excel_total}")

        # =====================================================
        # 13. Save Final File on Server
        # =====================================================
        output_dir = "generated_invoices"
        os.makedirs(output_dir, exist_ok=True)

        # Create download filename
        invoice_no = invoice.get('invoice_no', 'invoice')
        project_name = project_details.get('project_name', '')
        lpo_reference = invoice.get("lpo_reference", "")
        
        # Replace slash with hyphen in invoice number for filename
        invoice_no_hyphen = invoice_no.replace('/', '-')
        
        # Clean up strings for filename
        import re
        
        def clean_filename(text):
            if not text:
                return ""
            text = re.sub(r'[\\/*?:"<>|]', '-', text)
            text = re.sub(r'\s+', '-', text)
            text = text.strip('- ')
            return text
        
        clean_project_name = clean_filename(project_name)
        
        has_lpo = lpo_reference and lpo_reference != " - " and lpo_reference != ""
        
        if has_lpo:
            clean_lpo_number = clean_filename(str(lpo_reference))
            download_filename = f"{invoice_no_hyphen}-{clean_project_name}-{clean_lpo_number}.xlsx"
        else:
            download_filename = f"{invoice_no_hyphen}-{clean_project_name}.xlsx"
        
        # Save file
        output_path = os.path.join(output_dir, f"{invoice_no_hyphen}.xlsx")

        wb.save(output_path)

        # =====================================================
        # 14. Return File for Download
        # =====================================================
        import urllib.parse
        encoded_filename = urllib.parse.quote(download_filename)
        
        return FileResponse(
            output_path,
            filename=download_filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}; filename=\"{download_filename}\""
            }
        )

    except Exception as e:
        print("Error generating invoice:", e)
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        cur.close()
        conn.close()
    """
    Generate Excel invoice using the template, insert rows dynamically,
    fill test items, report numbers, amounts, totals and save on server.
    """

    # Path to converted template (.xlsx)
    template_path = download_template_from_supabase("invoice")

    if not os.path.exists(template_path):
        raise HTTPException(status_code=404, detail="Invoice template not found. Convert invoice.xls → .xlsx")

    conn = get_connection()
    cur = conn.cursor()

    try:
        # =====================================================
        # 1. Load full invoice from DB
        # =====================================================
        invoice = get_invoice_complete(invoice_id, cur)
        project_details = invoice.get("project_details", {})
        
        # Get invoice type to determine if we should filter by reports
        invoice_type = invoice.get("invoice_type", "CASH")
        
        # =====================================================
        # NEW: Filter items for PROFORMA/TAX invoices
        # =====================================================
        if invoice_type in ["PROFORMA", "TAX"]:
            print(f"DEBUG: Filtering items for {invoice_type} invoice {invoice_id}")
            
            # Get reports linked to this invoice
            cur.execute("""
                SELECT report_no 
                FROM invoice_report_links 
                WHERE invoice_id = %s 
                AND invoice_type = %s
            """, (invoice_id, invoice_type))
            
            linked_reports = [row[0] for row in cur.fetchall()]
            print(f"DEBUG: Found {len(linked_reports)} linked reports: {linked_reports}")
            
            # Filter invoice items to only include those with linked reports
            original_items = invoice.get("items", [])
            filtered_items = []
            
            if linked_reports:
                # For each report, find matching invoice items
                for report_no in linked_reports:
                    # Get test type from report
                    cur.execute("""
                        SELECT covers_test_type 
                        FROM reports 
                        WHERE report_no = %s
                    """, (report_no,))
                    
                    report_data = cur.fetchone()
                    if report_data:
                        test_type = report_data[0]
                        print(f"DEBUG: Report {report_no} covers test type: '{test_type}'")
                        
                        # Find invoice items that match this test type
                        for item in original_items:
                            if item.get("description") == test_type:
                                filtered_items.append(item)
                                print(f"DEBUG: Added item '{item.get('description')}' for report {report_no}")
                                break  # Only add one item per report
            else:
                # No linked reports, use all items
                filtered_items = original_items
                print("DEBUG: No linked reports found, using all items")
            
            # Update invoice items with filtered list
            invoice["items"] = filtered_items
            print(f"DEBUG: Filtered from {len(original_items)} to {len(filtered_items)} items")
        else:
            # For CASH/CREDIT invoices, use all items
            invoice["items"] = invoice.get("items", [])
        
        items = invoice.get("items", [])
        
        # DEBUG: Print what data we're getting
        print("=== DEBUG INVOICE DATA ===")
        print(f"Invoice: {invoice.get('invoice_no')} (Type: {invoice_type})")
        print(f"Subtotal: {invoice.get('subtotal')}")
        print(f"VAT (5%): {invoice.get('vat')}")
        print(f"Total: {invoice.get('total')}")
        print(f"Number of items after filtering: {len(items)}")
        for i, item in enumerate(items):
            print(f"  Item {i}: Sample {item.get('sample_id')} - {item.get('description')} - Qty: {item.get('quantity')}")
        print("==========================")

        # =====================================================
        # 2. Load the Excel template
        # =====================================================
        wb = openpyxl.load_workbook(template_path, data_only=False)
        ws = wb.active

                # =====================================================
        # NEW: Set title in cell A3 based on invoice type
        # =====================================================
        title_text = ""
        if invoice_type == "PROFORMA":
            title_text = "PROFORMA INVOICE"
        elif invoice_type == "TAX":
            title_text = "TAX INVOICE"
        elif invoice_type == "CASH":
            title_text = "CASH INVOICE"
        elif invoice_type == "CREDIT":
            title_text = "CREDIT INVOICE"
        else:
            title_text = "INVOICE"  # Default
        
        # Set the title text in cell A3
        ws["A3"] = title_text
        
        # Apply formatting: Size 12, Arial, Dark Blue color
        from openpyxl.styles import Font
        
        # Create dark blue color (RGB: 0, 0, 139)
        dark_blue_color = "00008B"  # Hex code for dark blue
        
        # Apply font formatting
        ws["A3"].font = Font(
            name="Arial",
            size=12,
            bold=True,  # Usually titles are bold
            color=dark_blue_color  # Dark blue text
        )
        
        # Optional: Center align the title horizontally
        from openpyxl.styles import Alignment
        ws["A3"].alignment = Alignment(horizontal="center")

        # =====================================================
        # 3. Define Template Structure
        # =====================================================
        FIRST_ITEM_ROW = 18  # First item row in template (based on your image)
        LAST_TEMPLATE_ITEM_ROW = 34  # Last available item row in template
        TOTAL_ROW = 35  # Row with "GRAND TOTAL"
        VAT_ROW = 36    # Row with "VAT @ 5%"
        NET_TOTAL_ROW = 37  # Row with "NET TOTAL"
        AMOUNT_WORDS_ROW = 38  # Row with "United Arab Emirates Dirhams Only"
        
        # Calculate available template rows for items
        TEMPLATE_ITEM_ROWS = LAST_TEMPLATE_ITEM_ROW - FIRST_ITEM_ROW + 1  # 17 rows

        # =====================================================
        # 4. Fill Header Fields - WITH PAYMENT TERMS UPDATE
        # =====================================================
        ws["I4"] = invoice.get("invoice_no", " - ")
        
        # Invoice date
        invoice_date = invoice.get("invoice_date")
        if invoice_date:
            if isinstance(invoice_date, str):
                ws["I5"] = invoice_date
            else:
                ws["I5"] = invoice_date.strftime("%d-%b-%Y")
        else:
            ws["I5"] = " - "

        # Client Section
        ws["A5"] = project_details.get("client_name", " - ")
        ws["C10"] = project_details.get("client_contact", " - ")

        # Project details
        ws["C13"] = project_details.get("project_no", " - ")
        ws["C15"] = project_details.get("project_name", " - ")
        ws["C14"] = project_details.get("location", " - ")

        # LPO
        lpo_reference = invoice.get("lpo_reference", " - ")
        ws["C18"] = lpo_reference
        
        # LPO date
        lpo_date = invoice.get("lpo_date")
        if lpo_date:
            if isinstance(lpo_date, str):
                ws["I6"] = lpo_date
            else:
                ws["I6"] = lpo_date.strftime("%d-%b-%Y")
        else:
            ws["I6"] = " - "

        # Payment Terms - Show CASH or CREDIT based on invoice_type
        invoice_type = invoice.get("invoice_type", "CASH")
        if invoice_type == "CASH":
            payment_display = "CASH / Immediate"
        else:
            payment_display = "CREDIT / 30 days"
            
        ws["I8"] = payment_display

        # =====================================================
        # 5. Handle Dynamic Item Rows - FIXED LOGIC
        # =====================================================
        # We'll determine this after we process items
        # Clear all existing item rows first
        for row in range(FIRST_ITEM_ROW, LAST_TEMPLATE_ITEM_ROW + 1):
            for col in ['A', 'B', 'D', 'E', 'I', 'J', 'K']:
                ws[f"{col}{row}"].value = None

        # =====================================================
        # 6. CORRECTED: Get reports by TEST TYPE not just by sample
        # =====================================================
        print("=== GETTING REPORTS GROUPED BY TEST TYPE ===")

        # FIRST: Get all reports that are linked to this specific invoice
        cur.execute("""
            SELECT report_no 
            FROM invoice_report_links 
            WHERE invoice_id = %s
        """, (invoice_id,))

        linked_report_nos = [row[0] for row in cur.fetchall()]
        print(f"DEBUG: Invoice {invoice_id} has {len(linked_report_nos)} linked reports: {linked_report_nos}")

        if linked_report_nos:
            # This invoice has linked reports (PROFORMA/TAX invoice with selected reports)
            # ONLY fetch the linked reports
            cur.execute("""
                SELECT 
                    r.report_no,
                    r.created_at,
                    r.covers_test_type,
                    r.sample_id,
                    COUNT(DISTINCT s.sample_id) as sample_count
                FROM reports r
                LEFT JOIN samples s ON (
                    r.sample_id = s.sample_id 
                    OR 
                    (r.covers_samples IS NOT NULL AND s.sample_id::text = ANY(r.covers_samples))
                )
                LEFT JOIN test_requests tr ON s.request_id = tr.test_request_id
                WHERE r.report_no = ANY(%s)
                AND r.status = 'APPROVED'
                GROUP BY r.report_no, r.created_at, r.covers_test_type, r.sample_id
                ORDER BY r.report_no
            """, (linked_report_nos,))
        else:
            # This invoice has no linked reports (regular CASH/CREDIT invoice)
            # Get all approved reports for the project
            cur.execute("""
                SELECT 
                    r.report_no,
                    r.created_at,
                    r.covers_test_type,
                    r.sample_id,
                    COUNT(DISTINCT s.sample_id) as sample_count
                FROM reports r
                LEFT JOIN samples s ON (
                    r.sample_id = s.sample_id 
                    OR 
                    (r.covers_samples IS NOT NULL AND s.sample_id::text = ANY(r.covers_samples))
                )
                LEFT JOIN test_requests tr ON s.request_id = tr.test_request_id
                WHERE tr.project_id = %s
                AND r.status = 'APPROVED'
                GROUP BY r.report_no, r.created_at, r.covers_test_type, r.sample_id
                ORDER BY r.report_no
            """, (invoice.get("project_id"),))

        all_reports = cur.fetchall()
        
        print(f"Found {len(all_reports)} approved reports for project")
        
        # Create mapping: test_type -> list of reports
        test_type_to_reports = {}
        for report in all_reports:
            report_no, created_at, test_type, sample_id, sample_count = report
            if test_type:
                if test_type not in test_type_to_reports:
                    test_type_to_reports[test_type] = []
                test_type_to_reports[test_type].append({
                    "report_no": report_no,
                    "created_at": created_at,
                    "sample_id": sample_id,
                    "sample_count": sample_count
                })
                print(f"Report {report_no}: Test Type = '{test_type}', Sample ID = {sample_id}, Covers {sample_count} samples")

        print("\n=== TEST TYPE TO REPORTS MAPPING ===")
        for test_type, reports in test_type_to_reports.items():
            print(f"Test Type: '{test_type}' has {len(reports)} reports:")
            for report in reports:
                print(f"  - {report['report_no']}")
        print("====================================\n")

        # =====================================================
        # 7. Match invoice items to reports by TEST TYPE
        # =====================================================
        print("=== MATCHING INVOICE ITEMS TO REPORTS ===")
        
        matched_items = []
        unmatched_items = []
        
        for index, item in enumerate(items):
            sample_id = item.get("sample_id")
            description = item.get("description", " - ")
            test_standard = item.get("test_standard", " - ")
            unit_rate = float(item.get("unit_rate", 0))
            quantity = int(item.get("quantity", 0))
            amount = float(item.get("amount", 0))
            
            print(f"\nProcessing invoice item {index}:")
            print(f"  Sample: {sample_id}")
            print(f"  Description: '{description}'")
            print(f"  Test Standard: '{test_standard}'")
            
            # Try to find a report for this test type
            matched_report = None
            
            # First: Check if we have any reports for this exact test type
            if description in test_type_to_reports and test_type_to_reports[description]:
                # Use the first report for this test type
                report_info = test_type_to_reports[description][0]
                matched_report = {
                    "report_no": report_info["report_no"],
                    "created_at": report_info["created_at"],
                    "test_type": description
                }
                print(f"  ✓ Found report by test type: {report_info['report_no']}")
                
                # Remove this report from available list so we don't reuse it
                test_type_to_reports[description].pop(0)
                
            # Second: Try fuzzy match if exact match not found
            if not matched_report:
                for test_type, reports in test_type_to_reports.items():
                    if reports and (test_type in description or description in test_type):
                        report_info = reports[0]
                        matched_report = {
                            "report_no": report_info["report_no"],
                            "created_at": report_info["created_at"],
                            "test_type": test_type
                        }
                        print(f"  ≈ Found report by fuzzy match: {report_info['report_no']} (Test: '{test_type}' matches '{description}')")
                        reports.pop(0)
                        break
            
            # Third: If still no match, create a placeholder
            if not matched_report:
                print(f"  ✗ No report found for test type '{description}'")
                matched_report = {
                    "report_no": f"INV-{invoice.get('invoice_no')}-{index+1}",
                    "created_at": None,
                    "test_type": description
                }
                unmatched_items.append(item)
            
            # Format date
            if matched_report["created_at"]:
                report_date = matched_report["created_at"].strftime("%d-%b-%Y") if hasattr(matched_report["created_at"], 'strftime') else str(matched_report["created_at"])
            else:
                report_date = " - "
            
            matched_items.append({
                "report_no": matched_report["report_no"],
                "report_date": report_date,
                "description": matched_report["test_type"],  # Use report's test type
                "test_standard": test_standard,
                "unit_rate": unit_rate,
                "quantity": quantity,
                "amount": amount,
                "sample_id": sample_id,
                "original_description": description  # Keep original for reference
            })
        
        # =====================================================
        # 8. GROUP by report number (combine same reports)
        # =====================================================
        print("\n=== GROUPING BY REPORT NUMBER ===")
        
        report_grouping = {}
        for item in matched_items:
            report_no = item["report_no"]
            
            if report_no not in report_grouping:
                report_grouping[report_no] = {
                    "report_no": report_no,
                    "report_date": item["report_date"],
                    "description": item["description"],
                    "test_standard": item["test_standard"],
                    "unit_rate": item["unit_rate"],
                    "total_quantity": 0,
                    "total_amount": 0.0,
                    "samples": [],
                    "sample_count": 0
                }
            
            # Accumulate quantities and amounts
            report_grouping[report_no]["total_quantity"] += item["quantity"]
            report_grouping[report_no]["total_amount"] += item["amount"]
            report_grouping[report_no]["samples"].append(item["sample_id"])
            report_grouping[report_no]["sample_count"] += 1
        
        # Convert to list
        grouped_items = list(report_grouping.values())
        
        print(f"Created {len(grouped_items)} grouped items from {len(matched_items)} invoice items")
        for i, item in enumerate(grouped_items):
            print(f"Group {i+1}: {item['report_no']} - {item['description']} - Qty: {item['total_quantity']} - Amount: AED {item['total_amount']}")

        # =====================================================
        # 9. Determine if we need extra rows
        # =====================================================
        num_items_to_display = len(grouped_items)

        # Calculate where items will actually go
        if num_items_to_display <= TEMPLATE_ITEM_ROWS:
            # Case 1: Items fit within template rows
            last_item_row = FIRST_ITEM_ROW + num_items_to_display - 1
        else:
            # Case 2: Need more rows than template provides
            rows_needed = num_items_to_display - TEMPLATE_ITEM_ROWS
            
            # Insert rows AFTER the template item area (after row 34)
            ws.insert_rows(LAST_TEMPLATE_ITEM_ROW + 1, amount=rows_needed)
            
            # Copy formatting from last template row (row 34) to new rows
            from copy import copy
            for i in range(rows_needed):
                new_row = LAST_TEMPLATE_ITEM_ROW + 1 + i
                # Copy formatting from row 34
                for col in range(1, 12):  # Columns A-K
                    source_cell = ws.cell(row=34, column=col)
                    target_cell = ws.cell(row=new_row, column=col)
                    target_cell.font = copy(source_cell.font)
                    target_cell.border = copy(source_cell.border)
                    target_cell.fill = copy(source_cell.fill)
                    target_cell.number_format = source_cell.number_format
                    target_cell.alignment = copy(source_cell.alignment)
            
            last_item_row = LAST_TEMPLATE_ITEM_ROW + rows_needed

        # =====================================================
        # 10. Fill rows with matched data
        # =====================================================
        print("\n=== FILLING EXCEL ROWS ===")
        for index, item in enumerate(grouped_items):
            # Determine which row to use
            if index < TEMPLATE_ITEM_ROWS:
                # Use template rows (18-34)
                row = FIRST_ITEM_ROW + index
            else:
                # Use newly inserted rows
                extra_index = index - TEMPLATE_ITEM_ROWS
                row = LAST_TEMPLATE_ITEM_ROW + 1 + extra_index

            # Fill columns A–K
            ws[f"A{row}"] = item["report_no"]
            ws[f"B{row}"] = item["report_date"]
            ws[f"D{row}"] = item["description"]
            ws[f"E{row}"] = item["test_standard"]
            ws[f"I{row}"] = item["total_quantity"]
            ws[f"J{row}"] = item["unit_rate"]
            ws[f"K{row}"] = item["total_amount"]
            
            print(f"Row {row}: {item['report_no']} - {item['description']} x{item['total_quantity']} = AED {item['total_amount']}")

        # =====================================================
        # 11. UPDATE FORMULAS - FIXED
        # =====================================================
        print(f"\n=== UPDATING EXCEL FORMULAS ===")
        print(f"First item row: {FIRST_ITEM_ROW}")
        print(f"Last item row: {last_item_row}")
        print(f"Items displayed: {num_items_to_display}")
        
        # IMPORTANT: Update the SUM formula to cover ALL item rows
        ws["K35"].value = f"=SUM(K{FIRST_ITEM_ROW}:K{last_item_row})"
        
        # K36 FORMULA: 5% of subtotal
        ws["K36"].value = "=K35*0.05"
        
        # K37 FORMULA: Subtotal + VAT
        ws["K37"].value = "=K35+K36"
        
        # Amount in words (static value)
        ws["B38"] = invoice.get("amount_in_words", " - ")
        
        print(f"K35 formula: =SUM(K{FIRST_ITEM_ROW}:K{last_item_row})")
        print(f"K36 formula: {ws['K36'].value}")
        print(f"K37 formula: {ws['K37'].value}")
        
        # =====================================================
        # 12. Verify calculations match database
        # =====================================================
        # Calculate what Excel should show
        excel_subtotal = sum(item.get("total_amount", 0) for item in grouped_items)
        excel_vat = excel_subtotal * 0.05
        excel_total = excel_subtotal + excel_vat
        
        db_subtotal = float(invoice.get("subtotal", 0))
        db_vat = float(invoice.get("vat", 0))
        db_total = float(invoice.get("total", 0))
        
        print(f"\n=== VERIFICATION ===")
        print(f"Database values -> Subtotal: {db_subtotal}, VAT: {db_vat}, Total: {db_total}")
        print(f"Excel will show -> Subtotal: {excel_subtotal}, VAT: {excel_vat}, Total: {excel_total}")

        # =====================================================
        # 13. Save Final File on Server
        # =====================================================
        output_dir = "generated_invoices"
        os.makedirs(output_dir, exist_ok=True)

        # Create download filename
        invoice_no = invoice.get('invoice_no', 'invoice')
        project_name = project_details.get('project_name', '')
        lpo_reference = invoice.get("lpo_reference", "")
        
        # Replace slash with hyphen in invoice number for filename
        invoice_no_hyphen = invoice_no.replace('/', '-')
        
        # Clean up strings for filename
        import re
        
        def clean_filename(text):
            if not text:
                return ""
            text = re.sub(r'[\\/*?:"<>|]', '-', text)
            text = re.sub(r'\s+', '-', text)
            text = text.strip('- ')
            return text
        
        clean_project_name = clean_filename(project_name)
        
        has_lpo = lpo_reference and lpo_reference != " - " and lpo_reference != ""
        
        if has_lpo:
            clean_lpo_number = clean_filename(str(lpo_reference))
            download_filename = f"{invoice_no_hyphen}-{clean_project_name}-{clean_lpo_number}.xlsx"
        else:
            download_filename = f"{invoice_no_hyphen}-{clean_project_name}.xlsx"
        
        # Save file

        output_path = os.path.join(output_dir, f"{invoice_no_hyphen}.xlsx")
        wb.save(output_path)

        # =====================================================
        # 14. Return File for Download
        # =====================================================
        import urllib.parse
        encoded_filename = urllib.parse.quote(download_filename)
        
        return FileResponse(
            output_path,
            filename=download_filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}; filename=\"{download_filename}\""
            }
        )

    except Exception as e:
        print("Error generating invoice:", e)
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        cur.close()
        conn.close()


# ----------------------------
# GET LATEST PROJECTS FOR INVOICE DROPDOWN - ENHANCED VERSION
# ----------------------------
@router.get("/projects/latest/")
def get_latest_projects():
    """
    Get the latest 10 projects with complete info for invoice creation
    Returns: Array of project objects with details
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Enhanced query with more details.
        #
        # NOTE: walk-in projects (is_walk_in = TRUE) have client_id = NULL and
        # only get a quotation_id once an LPO has been generated (see
        # walkin.py / create_lpo). The previous version of this query used
        # INNER JOINs on both `clients` and `quotations`, which silently
        # dropped every walk-in row from the result — that's why walk-in LPs
        # never showed up in Generate Invoices. Switched to LEFT JOINs and
        # fall back to the walk-in columns (client_name / walk_in_client)
        # for display info. Walk-ins still need an LPO (quotation_id set)
        # before they're invoiceable, so those are filtered separately
        # instead of relying on the join to do it implicitly.
        cur.execute("""
            SELECT 
                p.project_id,
                p.project_name,
                p.project_no,
                COALESCE(c.name, p.client_name, p.walk_in_client) as client_name,
                p.location,
                q.quotation_no,
                p.is_walk_in
            FROM projects p
            LEFT JOIN clients c ON p.client_id = c.client_id
            LEFT JOIN quotations q ON p.quotation_id = q.quotation_id
            WHERE p.is_walk_in IS NOT TRUE
               OR (p.is_walk_in = TRUE AND p.quotation_id IS NOT NULL AND p.project_no != 'PENDING')
            ORDER BY p.project_id DESC
            LIMIT 10
        """)
        
        # Format response
        projects = []
        for row in cur.fetchall():
            project_id, project_name, project_no, client_name, location, quotation_no, is_walk_in = row
            
            # Create display label for dropdown
            display_label = f"{project_no} - {project_name} ({client_name})"
            
            projects.append({
                "project_id": project_id,
                "project_name": display_label,  # Combined display text
                "project_no": project_no,
                "project_name_raw": project_name,  # Original project name
                "client_name": client_name,
                "location": location,
                "quotation_no": quotation_no,
                "is_walk_in": bool(is_walk_in),
                "value": project_id,  # For dropdown value
                "label": display_label  # For dropdown label
            })
        
        return projects
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()



# Add this to your invoices.py file, after the existing endpoints

# ----------------------------
# DELIVERY NOTE GENERATION
# ----------------------------

# Replace the existing DeliveryNoteRequest class with this:
class DeliveryNoteRequest(BaseModel):
    project_id: int
    selected_report_ids: Optional[List[int]] = None
    include_all_reports: bool = True





def generate_delivery_note_number(cur):
    """Generate delivery note number: 13212/25, 13213/25, etc."""
    # Get the last 2 digits of current year
    year_short = str(datetime.now().year)[-2:]
    
    try:
        # Check if delivery_notes table exists
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'delivery_notes'
            )
        """)
        table_exists = cur.fetchone()[0]
        
        if table_exists:
            # Get max delivery note number
            cur.execute("""
                SELECT delivery_note_no FROM delivery_notes 
                ORDER BY delivery_note_id DESC LIMIT 1
            """)
            last_dn = cur.fetchone()
            
            if last_dn and last_dn[0]:
                try:
                    # Extract number from format like "13212/25"
                    last_number = int(last_dn[0].split('/')[0])
                    next_number = last_number + 1
                except:
                    next_number = 13212
            else:
                next_number = 13212
        else:
            # Table doesn't exist, start from 13212
            next_number = 13212
            
    except Exception as e:
        print(f"Error in generate_delivery_note_number: {e}")
        next_number = 13212
    
    return f"{next_number}/{year_short}"




@router.get("/projects/{project_id}/reports-for-delivery")
def get_reports_for_delivery_note(project_id: int):
    """
    Get all approved reports for a project to select for delivery note
    Now includes delivery note status check
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Ensure the junction table exists before querying
        ensure_delivery_note_reports_table(cur)
        
        # Get project details
        cur.execute(""" 
            SELECT p.project_no, p.project_name, c.name as client_name
            FROM projects p
            JOIN clients c ON p.client_id = c.client_id
            WHERE p.project_id = %s
        """, (project_id,))
        
        project_data = cur.fetchone()
        if not project_data:
            raise HTTPException(status_code=404, detail="Project not found")
        
        project_no, project_name, client_name = project_data
        
        print(f"DEBUG: Fetching reports for project {project_id} - {project_name}")
        
        # Get all approved reports for this project
        # SIMPLIFIED QUERY - Let's first get all reports, then check delivery note status
        cur.execute("""
            SELECT DISTINCT ON (r.report_no)
                r.report_id, 
                r.report_no, 
                r.created_at,
                r.covers_test_type as test_name,
                r.covers_samples,
                -- Count samples in the covers_samples array
                array_length(r.covers_samples, 1) as sample_count
            FROM reports r
            JOIN samples s ON r.sample_id = s.sample_id
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            WHERE tr.project_id = %s 
            AND r.status = 'APPROVED'
            ORDER BY r.report_no, r.created_at DESC
        """, (project_id,))
        
        all_reports = cur.fetchall()
        
        print(f"DEBUG: Found {len(all_reports)} approved reports")
        
        reports = []
        delivered_count = 0
        undelivered_count = 0
        
        for row in all_reports:
            report_id = row[0]
            report_no = row[1]
            created_date = row[2]
            test_name = row[3]
            covers_samples = row[4]
            sample_count = row[5] or 0
            
            # Check if this report is already in delivery_note_reports
            cur.execute("""
                SELECT 1 FROM delivery_note_reports 
                WHERE report_no = %s
                LIMIT 1
            """, (report_no,))
            
            already_in_delivery_note = cur.fetchone() is not None
            
            # Get the actual sample numbers from the array if available
            sample_nos = []
            if covers_samples and len(covers_samples) > 0:
                # Join with samples table to get sample numbers
                placeholders = ','.join(['%s'] * len(covers_samples))
                cur.execute(f"""
                    SELECT sample_no FROM samples 
                    WHERE sample_no IN ({placeholders})
                """, tuple(covers_samples))
                sample_rows = cur.fetchall()
                sample_nos = [row[0] for row in sample_rows] if sample_rows else []
            else:
                # Fallback: get the sample_no for this specific report
                cur.execute("""
                    SELECT s.sample_no FROM samples s
                    WHERE s.sample_id = (
                        SELECT sample_id FROM reports 
                        WHERE report_no = %s LIMIT 1
                    )
                """, (report_no,))
                sample_row = cur.fetchone()
                if sample_row:
                    sample_nos = [sample_row[0]]
                    sample_count = 1
            
            if already_in_delivery_note:
                delivered_count += 1
                print(f"DEBUG: Report {report_no} is already in delivery note")
            else:
                undelivered_count += 1
                print(f"DEBUG: Report {report_no} is NOT in delivery note yet")
            
            reports.append({
                "report_id": report_id,
                "report_no": report_no,
                "created_date": created_date.strftime("%Y-%m-%d") if created_date else None,
                "test_name": test_name or "Test Report",
                "sample_count": sample_count,
                "covers_samples": sample_nos,
                "already_invoiced": already_in_delivery_note,  # Using same field name for compatibility
                "status": "Delivered" if already_in_delivery_note else "Not Delivered"
            })
        
        print(f"DEBUG: Total: {len(reports)}, Delivered: {delivered_count}, Undelivered: {undelivered_count}")
        
        return {
            "project_id": project_id,
            "project_no": project_no,
            "project_name": project_name,
            "client_name": client_name,
            "total_reports": len(reports),
            "delivered_count": delivered_count,
            "undelivered_count": undelivered_count,
            "reports": reports
        }
        
    except Exception as e:
        print(f"ERROR in get_reports_for_delivery_note: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()








@router.post("/delivery-notes/generate-excel-template")
def generate_delivery_note_excel_template(payload: DeliveryNoteRequest):
    """
    Generate delivery note Excel file using the template for selected reports
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get project details
        cur.execute("""
            SELECT p.project_no, p.project_name, c.name as client_name,
                   c.address, c.phone, c.email
            FROM projects p
            JOIN clients c ON p.client_id = c.client_id
            WHERE p.project_id = %s
        """, (payload.project_id,))
        
        project_data = cur.fetchone()
        if not project_data:
            raise HTTPException(status_code=404, detail="Project not found")
        
        project_no, project_name, client_name, client_address, client_phone, client_email = project_data
        
        # Get selected reports or all if include_all_reports is True
        if payload.include_all_reports:
            # Get all approved reports BUT EXCLUDE ALREADY DELIVERED ONES
            cur.execute("""
                SELECT DISTINCT ON (r.report_no)
                    r.report_id, r.report_no, r.created_at,
                    r.covers_test_type as test_name,
                    r.covers_samples,
                    array_length(r.covers_samples, 1) as sample_count
                FROM reports r
                JOIN samples s ON r.sample_id = s.sample_id
                JOIN test_requests tr ON s.request_id = tr.test_request_id
                WHERE tr.project_id = %s 
                AND r.status = 'APPROVED'
                AND r.report_no NOT IN (
                    SELECT report_no FROM delivery_note_reports
                )
                ORDER BY r.report_no, r.created_at DESC
            """, (payload.project_id,))
        elif payload.selected_report_ids:
            # Get specific selected reports
            cur.execute("""
                SELECT DISTINCT ON (r.report_no)
                    r.report_id, r.report_no, r.created_at,
                    r.covers_test_type as test_name,
                    r.covers_samples,
                    array_length(r.covers_samples, 1) as sample_count
                FROM reports r
                WHERE r.report_id = ANY(%s)
                AND r.status = 'APPROVED'
                ORDER BY r.report_no, r.created_at DESC
            """, (payload.selected_report_ids,))
        else:
            raise HTTPException(status_code=400, detail="No reports selected")
        
        reports_data = cur.fetchall()
        
        if not reports_data:
            if payload.include_all_reports:
                raise HTTPException(status_code=400, detail="No undelivered reports found for this project")
            else:
                raise HTTPException(status_code=400, detail="No approved reports found for this project")
        
        # Generate delivery note number
        delivery_note_no = generate_delivery_note_number(cur)
        
        # =====================================================
        # 1. Load the Excel template
        # =====================================================
        template_path = download_template_from_supabase("delivery_note")
        
        if not os.path.exists(template_path):
            raise HTTPException(status_code=404, detail="Delivery note template not found")
        
        wb = openpyxl.load_workbook(template_path, data_only=False)
        ws = wb.active
        
        # =====================================================
        # 2. Fill Template Fields
        # =====================================================
        # Fill Ref. No.: delivery note number
        ws["B6"] = delivery_note_no  # Ref. No.: (row 10, column B)
        
        # Fill Lab Project No.: project number
        ws["B7"] = project_no  # Lab Project No.: (row 12, column B)
        
        # Fill Customer Name:
        ws["B8"] = client_name  # Customer Name: (row 13, column B)
        
        # P.O.Box: (already has Dubai - U.A.E.)
        # This is at ws["B14"] which already has "Dubai - U.A.E."
        
        # =====================================================
        # 3. Fill Report Table
        # =====================================================
        # Starting row for reports in the template (row 19 based on your template)
        START_ROW = 12
        
        for i, report in enumerate(reports_data, 1):
            row = START_ROW + i - 1
            report_id, report_no, created_date, test_name, covers_samples, sample_count = report
            
            # Use the actual sample count from the covers_samples array
            actual_sample_count = sample_count or 1
            if covers_samples and isinstance(covers_samples, list):
                actual_sample_count = len(covers_samples)
            
            # Fill columns according to template:
            # A = Report No.
            # B = Description
            # G = No. Tests (based on your template with columns A-K)
            
            ws[f"A{row}"] = report_no
            ws[f"B{row}"] = test_name or "Test Report"
            ws[f"G{row}"] = actual_sample_count
        
        # Clear any remaining rows in the template
        MAX_TEMPLATE_ROWS = 34  # Adjust based on your template
        for row in range(START_ROW + len(reports_data), MAX_TEMPLATE_ROWS + 1):
            ws[f"A{row}"].value = None
            ws[f"B{row}"].value = None
            ws[f"G{row}"].value = None
        
        # =====================================================
        # 4. Save Final File on Server
        # =====================================================
        output_dir = "generated_delivery_notes"
        os.makedirs(output_dir, exist_ok=True)
        
        # Clean filename — same approach as tax/proforma invoices
        import re, urllib.parse
        def clean_dn_filename(text):
            if not text:
                return ""
            text = str(text).strip()
            text = re.sub(r'[\\/*?:"<>|]', '-', text)
            text = re.sub(r'\s+', '-', text)
            text = text.strip('-')
            return text

        dn_clean = delivery_note_no.strip().replace('/', '-')
        clean_project_no = clean_dn_filename(project_no)
        filename = f"DN-{dn_clean}-{clean_project_no}.xlsx"
        filepath = os.path.join(output_dir, filename)
        
        wb.save(filepath)
        
        # =====================================================
        # 5. NEW: Track which reports were included in this delivery note
        # =====================================================
        try:
            # First ensure the delivery_note_reports junction table exists
            ensure_delivery_note_reports_table(cur)
            
            # Insert records for each report included
            for report in reports_data:
                report_no = report[1]  # report_no is at index 1
                cur.execute("""
                    INSERT INTO delivery_note_reports (delivery_note_no, report_no)
                    VALUES (%s, %s)
                    ON CONFLICT (delivery_note_no, report_no) DO NOTHING
                """, (delivery_note_no, report_no))
            
            # Update delivery_notes table - FIXED to match existing schema
            cur.execute("""
                CREATE TABLE IF NOT EXISTS delivery_notes (
                    delivery_note_id SERIAL PRIMARY KEY,
                    delivery_note_no VARCHAR(50) NOT NULL UNIQUE,
                    project_id INTEGER NOT NULL REFERENCES projects(project_id),
                    generated_by INTEGER REFERENCES users(user_id),
                    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_reports INTEGER DEFAULT 0,
                    file_path TEXT
                )
            """)
            
            # Use existing user ID or default to NULL
            # In a real app, you would get this from the current user session
            user_id = None  # You might want to pass this from the frontend or use a default
            
            cur.execute("""
                INSERT INTO delivery_notes 
                (delivery_note_no, project_id, generated_by, total_reports, file_path)
                VALUES (%s, %s, %s, %s, %s)
            """, (delivery_note_no, payload.project_id, user_id, len(reports_data), filepath))
            
            conn.commit()
        except Exception as db_error:
            print(f"Note: Could not save delivery note record: {db_error}")
            # Don't fail the request if we can't save the record
            if conn:
                conn.rollback()
        
        # =====================================================
        # 6. Return File for Download
        # =====================================================
        encoded_filename = urllib.parse.quote(filename)
        return FileResponse(
            filepath,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}; filename=\"{filename}\""
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in generate_delivery_note_excel_template: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error generating delivery note: {str(e)}")
    finally:
        cur.close()
        conn.close()









@router.get("/projects/{project_id}/reports-for-invoice/{invoice_type}")
def get_reports_for_invoice(project_id: int, invoice_type: str):
    """
    Get all approved reports for a project to select for PROFORMA or TAX invoice
    Similar to delivery notes but checks invoice_report_links table
    """
    if invoice_type not in ['PROFORMA', 'TAX']:
        raise HTTPException(status_code=400, detail="Invoice type must be PROFORMA or TAX")
    
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get project details
        cur.execute(""" 
            SELECT p.project_no, p.project_name, c.name as client_name
            FROM projects p
            JOIN clients c ON p.client_id = c.client_id
            WHERE p.project_id = %s
        """, (project_id,))
        
        project_data = cur.fetchone()
        if not project_data:
            raise HTTPException(status_code=404, detail="Project not found")
        
        project_no, project_name, client_name = project_data
        
        print(f"DEBUG: Fetching reports for {invoice_type} invoice - project {project_id}")
        
        # Get all approved reports for this project
        # Check if they're already in invoice_report_links for this invoice type
        cur.execute("""
            SELECT DISTINCT ON (r.report_no)
                r.report_id, 
                r.report_no, 
                r.created_at,
                r.covers_test_type as test_name,
                r.covers_samples,
                -- Count samples in the covers_samples array
                array_length(r.covers_samples, 1) as sample_count,
                -- Check if already in PROFORMA/TAX invoices
                EXISTS (
                    SELECT 1 FROM invoice_report_links irl
                    WHERE irl.report_no = r.report_no 
                    AND irl.invoice_type = %s
                ) as already_invoiced
            FROM reports r
            JOIN samples s ON r.sample_id = s.sample_id
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            WHERE tr.project_id = %s 
            AND r.status = 'APPROVED'
            ORDER BY r.report_no, r.created_at DESC
        """, (invoice_type, project_id))
        
        all_reports = cur.fetchall()
        
        print(f"DEBUG: Found {len(all_reports)} approved reports for {invoice_type}")
        
        reports = []
        invoiced_count = 0
        uninvoiced_count = 0
        
        for row in all_reports:
            report_id = row[0]
            report_no = row[1]
            created_date = row[2]
            test_name = row[3]
            covers_samples = row[4]
            sample_count = row[5] or 0
            already_invoiced = row[6]
            
            # Get the actual sample numbers from the array if available
            sample_nos = []
            if covers_samples and len(covers_samples) > 0:
                # Join with samples table to get sample numbers
                placeholders = ','.join(['%s'] * len(covers_samples))
                cur.execute(f"""
                    SELECT sample_no FROM samples 
                    WHERE sample_no IN ({placeholders})
                """, tuple(covers_samples))
                sample_rows = cur.fetchall()
                sample_nos = [row[0] for row in sample_rows] if sample_rows else []
            else:
                # Fallback: get the sample_no for this specific report
                cur.execute("""
                    SELECT s.sample_no FROM samples s
                    WHERE s.sample_id = (
                        SELECT sample_id FROM reports 
                        WHERE report_no = %s LIMIT 1
                    )
                """, (report_no,))
                sample_row = cur.fetchone()
                if sample_row:
                    sample_nos = [sample_row[0]]
                    sample_count = 1
            
            if already_invoiced:
                invoiced_count += 1
                print(f"DEBUG: Report {report_no} is already in {invoice_type} invoice")
            else:
                uninvoiced_count += 1
                print(f"DEBUG: Report {report_no} is NOT in {invoice_type} invoice yet")
            
            reports.append({
                "report_id": report_id,
                "report_no": report_no,
                "created_date": created_date.strftime("%Y-%m-%d") if created_date else None,
                "test_name": test_name or "Test Report",
                "sample_count": sample_count,
                "covers_samples": sample_nos,
                "already_invoiced": already_invoiced,
                "invoice_type": invoice_type,
                "status": "Invoiced" if already_invoiced else "Not Invoiced"
            })
        
        print(f"DEBUG: Total: {len(reports)}, {invoice_type} Invoiced: {invoiced_count}, Not Invoiced: {uninvoiced_count}")
        
        return {
            "project_id": project_id,
            "project_no": project_no,
            "project_name": project_name,
            "client_name": client_name,
            "invoice_type": invoice_type,
            "total_reports": len(reports),
            "invoiced_count": invoiced_count,
            "uninvoiced_count": uninvoiced_count,
            "reports": reports
        }
        
    except Exception as e:
        print(f"ERROR in get_reports_for_invoice: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()


# =====================================================
# MODIFIED: Create Invoice - Record report links
# =====================================================
@router.post("/with-reports", response_model=InvoiceOut)
def create_invoice_with_reports(payload: InvoiceCreate, selection: Optional[InvoiceReportSelection] = None):
    """
    Create a new invoice for a project with optional report selection.
    If invoice_type is PROFORMA or TAX and selection is provided, record report links.
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        # ---------------------------------------------------
        # 1. Get project details with client info
        # ---------------------------------------------------
        cur.execute("""
            SELECT p.project_id, p.project_no, p.project_name, p.location, p.lpo_no, p.lpo_date,
                   c.client_id, c.name, c.contact_person, c.email, c.address, c.phone,
                   q.quotation_no
            FROM projects p
            JOIN clients c ON p.client_id = c.client_id
            JOIN quotations q ON p.quotation_id = q.quotation_id
            WHERE p.project_id = %s
        """, (payload.project_id,))
        project_data = cur.fetchone()
        if not project_data:
            raise HTTPException(status_code=404, detail="Project not found")

        # ---------------------------------------------------
        # 2. Get invoiceable items from project
        # ---------------------------------------------------
        invoiceable_items = get_project_quotation_items(payload.project_id, cur)
        if not invoiceable_items:
            raise HTTPException(status_code=400, detail="No test items available for invoicing")

        # ---------------------------------------------------
        # 2a. Group invoiceable items by description, test standard, unit rate
        # ---------------------------------------------------
        from collections import defaultdict

        grouped_items = defaultdict(lambda: {
            "description": "",
            "test_standard": "",
            "unit_rate": 0,
            "quantity": 0,
            "sample_ids": []
        })

        for item in invoiceable_items:
            item_id, description, test_standard, unit_rate, quantity, test_request_id, request_no, sample_id, sample_no, sample_status = item
            key = (description, test_standard, unit_rate)
            grouped_items[key]["description"] = description
            grouped_items[key]["test_standard"] = test_standard
            grouped_items[key]["unit_rate"] = unit_rate
            grouped_items[key]["quantity"] += quantity
            grouped_items[key]["sample_ids"].append(sample_id)

        # Convert grouped items to a list
        final_items = []
        for (desc, std, rate), data in grouped_items.items():
            final_items.append((desc, std, rate, data["quantity"], data["sample_ids"]))

        # ---------------------------------------------------
        # 3. Generate invoice number
        # ---------------------------------------------------
        invoice_no = generate_invoice_no(cur, payload.invoice_type)

        # ---------------------------------------------------
        # 4. Calculate totals
        # ---------------------------------------------------
        subtotal = 0.0
        for item in final_items:
            desc, std, unit_rate, quantity, sample_ids = item
            unit_rate_float = float(unit_rate) if isinstance(unit_rate, Decimal) else unit_rate
            subtotal += unit_rate_float * quantity

        vat = subtotal * 0.05  # 5% VAT
        total = subtotal + vat
        amount_words = number_to_words(total)

        # ---------------------------------------------------
        # 5. LPO and payment terms
        # ---------------------------------------------------
        lpo_reference = payload.lpo_reference or project_data[4]
        lpo_date = payload.lpo_date or project_data[5]
        payment_terms = payload.payment_terms or "30 days"

        # ---------------------------------------------------
        # 6. Insert invoice header
        # ---------------------------------------------------
        cur.execute("""
            INSERT INTO invoices (
                invoice_no, project_id, invoice_type, invoice_date,
                client_reference, lpo_reference, lpo_date, payment_terms,
                subtotal, vat, total, amount_in_words, services_description, remarks
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING invoice_id
        """, (
            invoice_no,
            payload.project_id,
            payload.invoice_type,
            payload.invoice_date or date.today(),
            payload.client_reference,
            lpo_reference,
            lpo_date,
            payment_terms,
            subtotal,
            vat,
            total,
            amount_words,
            payload.services_description or f"Testing services for {project_data[2]}",
            payload.remarks
        ))
        invoice_id = cur.fetchone()[0]

        # ---------------------------------------------------
        # 7. Insert grouped invoice items
        # ---------------------------------------------------
        for item in final_items:
            description, test_standard, unit_rate, quantity, sample_ids = item
            unit_rate_float = float(unit_rate) if isinstance(unit_rate, Decimal) else unit_rate
            amount = unit_rate_float * quantity
            sample_id = sample_ids[0] if sample_ids else None  # representative sample

            cur.execute("""
                INSERT INTO invoice_items (
                    invoice_id, description, test_standard,
                    unit_rate, quantity, amount, sample_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                invoice_id,
                description,
                test_standard,
                unit_rate_float,
                quantity,
                amount,
                sample_id
            ))

        # ---------------------------------------------------
        # 8. Record report links for PROFORMA/TAX invoices
        # ---------------------------------------------------
        if payload.invoice_type in ['PROFORMA', 'TAX'] and selection:
            # Determine which reports to include
            if selection.include_all_reports:
                # Get all approved reports NOT already in this invoice type
                cur.execute("""
                    SELECT DISTINCT r.report_no
                    FROM reports r
                    JOIN samples s ON r.sample_id = s.sample_id
                    JOIN test_requests tr ON s.request_id = tr.test_request_id
                    WHERE tr.project_id = %s 
                    AND r.status = 'APPROVED'
                    AND r.report_no NOT IN (
                        SELECT report_no FROM invoice_report_links 
                        WHERE invoice_type = %s
                    )
                """, (payload.project_id, payload.invoice_type))
                report_nos = [row[0] for row in cur.fetchall()]
            elif selection.selected_report_ids:
                # Get specific selected reports
                cur.execute("""
                    SELECT DISTINCT r.report_no
                    FROM reports r
                    WHERE r.report_id = ANY(%s)
                    AND r.status = 'APPROVED'
                """, (selection.selected_report_ids,))
                report_nos = [row[0] for row in cur.fetchall()]
            else:
                report_nos = []
            
            # Insert into invoice_report_links
            for report_no in report_nos:
                cur.execute("""
                    INSERT INTO invoice_report_links (invoice_id, report_no, invoice_type)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (invoice_id, report_no, invoice_type) DO NOTHING
                """, (invoice_id, report_no, payload.invoice_type))

        # ---------------------------------------------------
        # 9. Commit transaction
        # ---------------------------------------------------
        conn.commit()

        # ---------------------------------------------------
        # 10. Return complete invoice
        # ---------------------------------------------------
        return get_invoice_complete(invoice_id, cur)

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# =====================================================
# NEW: Generate Invoice with Report Selection
# =====================================================
# =====================================================
# NEW: Generate Invoice with Report Selection
# =====================================================
@router.post("/generate-with-reports")
def generate_invoice_with_reports(payload: dict):
    """
    Combined endpoint to create invoice, record report links, and generate Excel
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        print(f"DEBUG: Received payload for generate-with-reports: {payload}")
        
        # Extract data from payload
        project_id = payload.get("project_id")
        invoice_type = payload.get("invoice_type")  # This should be "PROFORMA" or "TAX"
        document_type = payload.get("document_type")  # This is the actual document type
        payment_method = payload.get("payment_method", "CASH")  # Get payment method, default to CASH
        
        # For backward compatibility, use document_type if invoice_type is not provided
        if not invoice_type and document_type in ["PROFORMA", "TAX"]:
            invoice_type = document_type
        
        print(f"DEBUG: Creating {invoice_type} invoice for project {project_id} with payment method {payment_method}")
        
        # Create invoice payload
        invoice_payload = {
            "project_id": project_id,
            "invoice_type": invoice_type,
            "payment_method": payment_method,  # Include payment_method
            "invoice_date": payload.get("invoice_date") or date.today().isoformat(),
            "client_reference": payload.get("client_reference"),
            "lpo_reference": payload.get("lpo_reference"),
            "lpo_date": payload.get("lpo_date"),
            "payment_terms": payload.get("payment_terms") or ("30 days" if invoice_type in ["PROFORMA", "TAX"] else "Immediate"),
            "services_description": payload.get("services_description") or "Professional services rendered",
            "remarks": payload.get("remarks")
        }
        
        print(f"DEBUG: Invoice payload: {invoice_payload}")
        
        # Create the invoice WITH PAYMENT METHOD
        invoice_create = InvoiceCreate(**invoice_payload)
        invoice_result = create_invoice_with_payment_method(invoice_create)  # FIXED: Use the correct function
        invoice_id = invoice_result["invoice_id"]

        print(f"DEBUG: Created invoice {invoice_result['invoice_no']} with ID {invoice_id}")

        # For PROFORMA/TAX invoices, record report links
        if invoice_type in ["PROFORMA", "TAX"]:
            include_all_reports = payload.get("include_all_reports", True)
            selected_report_ids = payload.get("selected_report_ids")
            
            print(f"DEBUG: Recording report links for {invoice_type} invoice")
            print(f"DEBUG: include_all_reports: {include_all_reports}")
            print(f"DEBUG: selected_report_ids: {selected_report_ids}")
            
            # Determine which reports to include
            if include_all_reports:
                # Get all approved reports NOT already in this invoice type
                cur.execute("""
                    SELECT DISTINCT r.report_no
                    FROM reports r
                    JOIN samples s ON r.sample_id = s.sample_id
                    JOIN test_requests tr ON s.request_id = tr.test_request_id
                    WHERE tr.project_id = %s 
                    AND r.status = 'APPROVED'
                    AND r.report_no NOT IN (
                        SELECT report_no FROM invoice_report_links 
                        WHERE invoice_type = %s
                    )
                """, (project_id, invoice_type))
                report_nos = [row[0] for row in cur.fetchall()]
            elif selected_report_ids:
                # Get specific selected reports
                cur.execute("""
                    SELECT DISTINCT r.report_no
                    FROM reports r
                    WHERE r.report_id = ANY(%s)
                    AND r.status = 'APPROVED'
                """, (selected_report_ids,))
                report_nos = [row[0] for row in cur.fetchall()]
            else:
                report_nos = []
            
            print(f"DEBUG: Will link {len(report_nos)} reports to invoice")
            
            # Insert into invoice_report_links
            for report_no in report_nos:
                try:
                    cur.execute("""
                        INSERT INTO invoice_report_links (invoice_id, report_no, invoice_type)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (invoice_id, report_no, invoice_type) DO NOTHING
                    """, (invoice_id, report_no, invoice_type))
                    print(f"DEBUG: Linked report {report_no} to invoice")
                except Exception as e:
                    print(f"WARNING: Could not link report {report_no}: {e}")
            
            conn.commit()
            print(f"DEBUG: Report links committed to database")
        
        # Generate Excel file
        print(f"DEBUG: Generating Excel for invoice {invoice_id}")
        if invoice_type == "PROFORMA_TAX":
            return generate_excel_invoice_combined(invoice_id)
        return generate_excel_invoice(invoice_id)
        
    except Exception as e:
        print(f"ERROR in generate_invoice_with_reports: {str(e)}")
        traceback.print_exc()
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error generating invoice: {str(e)}")
    finally:
        cur.close()
        conn.close()


# Add this endpoint to invoices.py, after the existing endpoints

@router.get("/projects/{project_id}/reports-invoiced-status")
def get_reports_invoiced_status(project_id: int):
    """
    Get all approved reports for a project with combined invoice/delivery status
    Shows "Invoiced" if in PROFORMA/TAX invoice OR delivery note
    Shows "Uninvoiced" if in neither
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get project details
        cur.execute(""" 
            SELECT p.project_no, p.project_name, c.name as client_name
            FROM projects p
            JOIN clients c ON p.client_id = c.client_id
            WHERE p.project_id = %s
        """, (project_id,))
        
        project_data = cur.fetchone()
        if not project_data:
            raise HTTPException(status_code=404, detail="Project not found")
        
        project_no, project_name, client_name = project_data
        
        # Get all approved reports with combined invoice/delivery status
        cur.execute("""
            SELECT DISTINCT ON (r.report_no)
                r.report_id, 
                r.report_no, 
                r.created_at,
                r.covers_test_type as test_name,
                r.covers_samples,
                array_length(r.covers_samples, 1) as sample_count,
                -- Combined status
                CASE 
                    WHEN EXISTS (
                        SELECT 1 FROM invoice_report_links irl
                        WHERE irl.report_no = r.report_no 
                        AND irl.invoice_type IN ('PROFORMA', 'TAX')
                    ) OR EXISTS (
                        SELECT 1 FROM delivery_note_reports dnr
                        WHERE dnr.report_no = r.report_no
                    ) THEN true
                    ELSE false
                END as is_invoiced
            FROM reports r
            JOIN samples s ON r.sample_id = s.sample_id
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            WHERE tr.project_id = %s 
            AND r.status = 'APPROVED'
            ORDER BY r.report_no, r.created_at DESC
        """, (project_id,))
        
        all_reports = cur.fetchall()
        
        reports = []
        for row in all_reports:
            report_id = row[0]
            report_no = row[1]
            created_date = row[2]
            test_name = row[3]
            covers_samples = row[4]
            sample_count = row[5] or 0
            is_invoiced = row[6]
            
            # Get sample numbers from covers_samples array
            sample_nos = []
            if covers_samples and len(covers_samples) > 0:
                # Try to get sample numbers directly from the array
                sample_nos = [str(s) for s in covers_samples if s]
            
            reports.append({
                "report_id": report_id,
                "report_no": report_no,
                "created_date": created_date,
                "test_name": test_name or "Test Report",
                "sample_count": sample_count,
                "covers_samples": sample_nos,
                "is_invoiced": is_invoiced,
                "invoice_status": "Invoiced" if is_invoiced else "Uninvoiced"
            })
        
        return {
            "project_id": project_id,
            "project_no": project_no,
            "project_name": project_name,
            "client_name": client_name,
            "total_reports": len(reports),
            "reports": reports
        }
        
    except Exception as e:
        print(f"ERROR in get_reports_invoiced_status: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()




@router.post("/generate-proforma-for-multiple-reports")
def generate_proforma_for_multiple_reports(payload: dict):
    """
    Generate Proforma Invoice for multiple selected reports
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        print(f"DEBUG: Generating proforma invoice for multiple reports: {payload}")
        
        project_id = payload.get("project_id")
        report_ids = payload.get("report_ids", [])  # List of report IDs
        report_nos = payload.get("report_nos", [])   # List of report numbers
        
        if not project_id or (not report_ids and not report_nos):
            raise HTTPException(status_code=400, detail="Project ID and at least one report are required")
        
        # Get project details
        cur.execute("""
            SELECT p.project_id, p.project_no, p.project_name, p.location, p.lpo_no, p.lpo_date,
                   c.client_id, c.name, c.contact_person, c.email, c.address, c.phone,
                   q.quotation_no
            FROM projects p
            JOIN clients c ON p.client_id = c.client_id
            JOIN quotations q ON p.quotation_id = q.quotation_id
            WHERE p.project_id = %s
        """, (project_id,))
        
        project_data = cur.fetchone()
        if not project_data:
            raise HTTPException(status_code=404, detail="Project not found")
        
        # Get all selected reports details
        reports_data = []
        total_sample_count = 0
        
        if report_nos:
            # Get by report numbers
            for report_no in report_nos:
                cur.execute("""
                    SELECT r.report_id, r.report_no, r.covers_test_type, 
                           r.covers_samples, r.sample_id, r.created_at
                    FROM reports r
                    WHERE r.report_no = %s AND r.status = 'APPROVED'
                """, (report_no,))
                report_data = cur.fetchone()
                if report_data:
                    reports_data.append(report_data)
        else:
            # Get by report IDs
            for report_id in report_ids:
                cur.execute("""
                    SELECT r.report_id, r.report_no, r.covers_test_type, 
                           r.covers_samples, r.sample_id, r.created_at
                    FROM reports r
                    WHERE r.report_id = %s AND r.status = 'APPROVED'
                """, (report_id,))
                report_data = cur.fetchone()
                if report_data:
                    reports_data.append(report_data)
        
        if not reports_data:
            raise HTTPException(status_code=404, detail="No valid approved reports found")
        
        # Get test items for this project
        invoiceable_items = get_project_quotation_items(project_id, cur)
        
        if not invoiceable_items:
            raise HTTPException(status_code=400, detail="No invoiceable items found for this project")
        
        # Generate invoice number
        invoice_no = generate_invoice_no(cur, "PROFORMA")
        
        # Prepare items list for invoice
        invoice_items = []
        subtotal = 0.0
        
        for report in reports_data:
            report_id, report_no, test_type, covers_samples, sample_id, created_at = report
            
            # Calculate sample count for this report
            sample_count = len(covers_samples) if covers_samples else 1
            total_sample_count += sample_count
            
            print(f"DEBUG: Report {report_no} covers {sample_count} samples")
            
            # Find matching item for this report's test type
            matching_item = None
            for item in invoiceable_items:
                item_id, description, test_standard, unit_rate, quantity, test_request_id, request_no, sample_id_item, sample_no, sample_status = item
                
                # Check if this test type matches the report
                if test_type and description and test_type.lower() in description.lower():
                    matching_item = {
                        "quotation_item_id": item_id,
                        "description": description,
                        "test_standard": test_standard,
                        "unit_rate": unit_rate,
                        "quantity": sample_count,
                        "sample_id": sample_id_item if sample_id_item else sample_id,
                        "report_no": report_no,
                        "created_at": created_at
                    }
                    break
            
            # If no exact match, use the first item
            if not matching_item:
                item = invoiceable_items[0]
                matching_item = {
                    "quotation_item_id": item[0],
                    "description": test_type or item[1],
                    "test_standard": item[2],
                    "unit_rate": item[3],
                    "quantity": sample_count,
                    "sample_id": item[7] if item[7] else sample_id,
                    "report_no": report_no,
                    "created_at": created_at
                }
            
            # Calculate item amount
            unit_rate_float = float(matching_item["unit_rate"]) if isinstance(matching_item["unit_rate"], Decimal) else matching_item["unit_rate"]
            item_amount = unit_rate_float * matching_item["quantity"]
            matching_item["amount"] = item_amount
            subtotal += item_amount
            
            invoice_items.append(matching_item)
        
        # Calculate totals
        vat = subtotal * 0.05  # 5% VAT
        total = subtotal + vat
        amount_words = number_to_words(total)
        
        print(f"DEBUG: {len(invoice_items)} items, Total samples: {total_sample_count}, Subtotal: {subtotal}, Total: {total}")
        
        # Create invoice data structure
        invoice_date = date.today()
        lpo_reference = project_data[4] or f"PROFORMA-MULTI-{invoice_date.strftime('%Y%m%d')}"
        lpo_date = project_data[5] or invoice_date
        
        # Prepare reports list for description
        report_numbers = ", ".join([item["report_no"] for item in invoice_items])
        
        invoice_data = {
            "invoice_no": invoice_no,
            "project_id": project_id,
            "invoice_type": "PROFORMA",
            "invoice_date": invoice_date,
            "client_reference": f"Proforma for Reports: {report_numbers}",
            "lpo_reference": lpo_reference,
            "lpo_date": lpo_date,
            "payment_terms": "Proforma Invoice - Payment on delivery",
            "subtotal": subtotal,
            "vat": vat,
            "total": total,
            "amount_in_words": amount_words,
            "services_description": f"Testing services for {len(invoice_items)} reports",
            "remarks": f"Proforma invoice for {len(invoice_items)} test reports",
            "project_details": {
                "project_no": project_data[1],
                "project_name": project_data[2],
                "location": project_data[3],
                "client_name": project_data[7],
                "client_contact": project_data[8],
                "client_email": project_data[9],
                "client_address": project_data[10],
                "client_phone": project_data[11]
            },
            "items": invoice_items
        }
        
        # Generate Excel using existing template
        template_path = download_template_from_supabase("invoice")
        
        if not os.path.exists(template_path):
            raise HTTPException(status_code=404, detail="Invoice template not found")
        
        wb = openpyxl.load_workbook(template_path, data_only=False)
        ws = wb.active
        
        # Fill template fields
        ws["A3"] = "PROFORMA INVOICE"
        ws["A3"].font = Font(name="Arial", size=12, bold=True, color="000060")
        ws["A3"].alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions['A'].width = 25
        ws["I4"] = invoice_no
        ws["I5"] = invoice_date.strftime("%d-%b-%Y")
        ws["A5"] = invoice_data["project_details"]["client_name"]
        ws["C10"] = invoice_data["project_details"]["client_contact"]
        ws["C13"] = invoice_data["project_details"]["project_no"]
        ws["C15"] = invoice_data["project_details"]["project_name"]
        ws["C14"] = invoice_data["project_details"]["location"]
        ws["C18"] = lpo_reference
        ws["I6"] = lpo_date.strftime("%d-%b-%Y") if hasattr(lpo_date, 'strftime') else str(lpo_date)
        ws["I8"] = "PROFORMA / Immediate"
        
        # Fill items section
        FIRST_ITEM_ROW = 18
        
        # Clear existing rows
        for row in range(FIRST_ITEM_ROW, 35):
            for col in ['A', 'B', 'D', 'E', 'I', 'J', 'K']:
                ws[f"{col}{row}"].value = None
        
        # Fill each report as a separate row
        row = FIRST_ITEM_ROW
        for item in invoice_items:
            if row > 34:  # Limit to template rows
                break
                
            ws[f"A{row}"] = item["report_no"]
            ws[f"B{row}"] = item["created_at"].strftime("%d-%b-%Y") if hasattr(item["created_at"], 'strftime') else " - "
            ws[f"D{row}"] = item["description"]
            ws[f"E{row}"] = item["test_standard"] or " - "
            ws[f"I{row}"] = item["quantity"]
            ws[f"J{row}"] = item["unit_rate"]
            ws[f"K{row}"] = item["amount"]
            row += 1
        
        # Update totals
        if len(invoice_items) == 1:
            ws["K35"].value = f"=K{FIRST_ITEM_ROW}"
        else:
            # Sum all item rows
            sum_range = f"K{FIRST_ITEM_ROW}:K{row-1}"
           
        ws["K35"] = round(subtotal, 2)
        ws["K36"] = round(vat, 2)
        ws["K37"] = round(total, 2)   
        ws["B38"] = amount_words
        
        # Save the file
        output_dir = "generated_proforma"
        os.makedirs(output_dir, exist_ok=True)
        
        # Create filename
        filename = f"Proforma-{invoice_date.strftime('%Y%m%d')}-{len(invoice_items)}reports.xlsx"
        filepath = os.path.join(output_dir, filename)
        
        wb.save(filepath)
        
        print(f"DEBUG: Proforma invoice saved to {filepath}")
        
        # Return the file for download
        return FileResponse(
            filepath,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        
    except Exception as e:
        print(f"ERROR in generate_proforma_for_multiple_reports: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error generating proforma invoice: {str(e)}")
    finally:
        cur.close()
        conn.close()



def generate_invoice_with_payment_method(payload: dict):
    """
    Combined endpoint to create invoice with payment_method, record report links, and generate Excel
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        print(f"DEBUG: Received payload for generate-with-payment-method: {payload}")
        
        # Extract data from payload
        project_id = payload.get("project_id")
        invoice_type = payload.get("invoice_type")  # PROFORMA or TAX
        payment_method = payload.get("payment_method", "CASH")  # NEW
        document_type = payload.get("document_type")
        
        print(f"DEBUG: Creating {invoice_type} invoice with {payment_method} payment method for project {project_id}")
        
        # Create invoice payload
        invoice_payload = {
            "project_id": project_id,
            "invoice_type": invoice_type,
            "payment_method": payment_method,  # NEW
            "invoice_date": payload.get("invoice_date") or date.today().isoformat(),
            "client_reference": payload.get("client_reference"),
            "lpo_reference": payload.get("lpo_reference"),
            "lpo_date": payload.get("lpo_date"),
            "payment_terms": payload.get("payment_terms") or ("30 days" if payment_method == "CREDIT" else "Immediate"),
            "services_description": payload.get("services_description") or "Professional services rendered",
            "remarks": payload.get("remarks")
        }
        
        print(f"DEBUG: Invoice payload with payment method: {invoice_payload}")
        
        # Create the invoice with payment_method
        invoice_create = InvoiceCreate(**invoice_payload)
        invoice_result = create_invoice_with_payment_method(invoice_create)
        invoice_id = invoice_result["invoice_id"]
        
        print(f"DEBUG: Created invoice {invoice_result['invoice_no']} with ID {invoice_id}, payment method: {payment_method}")
        
        # For PROFORMA/TAX invoices, record report links
        if invoice_type in ["PROFORMA", "TAX"]:
            include_all_reports = payload.get("include_all_reports", True)
            selected_report_ids = payload.get("selected_report_ids")
            
            print(f"DEBUG: Recording report links for {invoice_type} invoice")
            
            # Determine which reports to include
            if include_all_reports:
                # Get all approved reports NOT already in this invoice type
                cur.execute("""
                    SELECT DISTINCT r.report_no
                    FROM reports r
                    JOIN samples s ON r.sample_id = s.sample_id
                    JOIN test_requests tr ON s.request_id = tr.test_request_id
                    WHERE tr.project_id = %s 
                    AND r.status = 'APPROVED'
                    AND r.report_no NOT IN (
                        SELECT report_no FROM invoice_report_links 
                        WHERE invoice_type = %s
                    )
                """, (project_id, invoice_type))
                report_nos = [row[0] for row in cur.fetchall()]
            elif selected_report_ids:
                # Get specific selected reports
                cur.execute("""
                    SELECT DISTINCT r.report_no
                    FROM reports r
                    WHERE r.report_id = ANY(%s)
                    AND r.status = 'APPROVED'
                """, (selected_report_ids,))
                report_nos = [row[0] for row in cur.fetchall()]
            else:
                report_nos = []
            
            print(f"DEBUG: Will link {len(report_nos)} reports to invoice")
            
            # Insert into invoice_report_links
            for report_no in report_nos:
                try:
                    cur.execute("""
                        INSERT INTO invoice_report_links (invoice_id, report_no, invoice_type)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (invoice_id, report_no, invoice_type) DO NOTHING
                    """, (invoice_id, report_no, invoice_type))
                    print(f"DEBUG: Linked report {report_no} to invoice")
                except Exception as e:
                    print(f"WARNING: Could not link report {report_no}: {e}")
            
            conn.commit()
            print(f"DEBUG: Report links committed to database")
        
        # Generate Excel file
        print(f"DEBUG: Generating Excel for invoice {invoice_id}")
        if invoice_type == "PROFORMA_TAX":
            return generate_excel_invoice_combined(invoice_id)
        return generate_excel_invoice(invoice_id)
        
    except Exception as e:
        print(f"ERROR in generate_invoice_with_payment_method: {str(e)}")
        traceback.print_exc()
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error generating invoice: {str(e)}")
    finally:
        cur.close()
        conn.close()


# =====================================================
# NEW: Create invoice (reports and/or tests) and return JSON, not a file.
#
# GenerateInvoice.jsx calls this for BOTH regular and walk-in projects, then
# separately fetches /{invoice_id}/excel-combined once it has the invoice_id
# back as JSON. That's different from the older /generate-with-reports and
# /generate-with-payment-method endpoints above, which create the invoice
# AND stream the Excel file back in one shot.
#
# Scope note: only the non-walk-in (report-based) path is implemented here.
# Walk-in (test-based) invoicing needs its own item-linking/already-invoiced
# tracking and is intentionally NOT handled yet — a walk-in project_id will
# get a clear 400 instead of hitting create_invoice_with_payment_method's
# `JOIN clients`, which assumes a non-null client_id and would otherwise
# fail with a confusing 404/500.
# =====================================================
@router.post("/generate-with-reports-and-tests")
def generate_invoice_with_reports_and_tests(payload: dict):
    """
    Create a PROFORMA invoice (reports linked for non-walk-in projects) and
    return its invoice_id as JSON. The frontend then calls
    GET /{invoice_id}/excel-combined separately to download the workbook.
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        print(f"DEBUG: Received payload for generate-with-reports-and-tests: {payload}")

        project_id     = payload.get("project_id")
        invoice_type   = payload.get("invoice_type", "PROFORMA")
        payment_method = payload.get("payment_method", "CASH")

        if not project_id:
            raise HTTPException(400, "project_id is required")

        # Walk-ins aren't supported by this endpoint yet — fail clearly
        # instead of letting it fall through to client_id-dependent queries.
        cur.execute("SELECT is_walk_in FROM projects WHERE project_id = %s", (project_id,))
        proj_row = cur.fetchone()
        if not proj_row:
            raise HTTPException(404, "Project not found")
        if proj_row[0]:
            raise HTTPException(
                400,
                "Invoicing walk-in (LP) projects from this screen isn't supported yet. "
                "This is being worked on separately."
            )

        # ---------------------------------------------------
        # 1. Resolve which reports/samples this invoice should cover.
        #    This MUST happen before the invoice is created — the actual
        #    invoice_items rows have to be restricted to the selected
        #    reports, not just have report links recorded cosmetically
        #    afterward (which was the cause of unselected reports still
        #    showing up on the generated workbook).
        # ---------------------------------------------------
        report_nos = []
        sample_ids_filter = None  # None = no restriction (all project items)

        if invoice_type in ["PROFORMA", "TAX"]:
            include_all_reports = payload.get("include_all_reports", True)
            selected_report_ids = payload.get("selected_report_ids")

            if include_all_reports:
                cur.execute("""
                    SELECT DISTINCT r.report_id, r.report_no
                    FROM reports r
                    JOIN samples s ON r.sample_id = s.sample_id
                    JOIN test_requests tr ON s.request_id = tr.test_request_id
                    WHERE tr.project_id = %s
                    AND r.status = 'APPROVED'
                    AND r.report_no NOT IN (
                        SELECT report_no FROM invoice_report_links
                        WHERE invoice_type = %s
                    )
                """, (project_id, invoice_type))
                rows = cur.fetchall()
                report_ids = [row[0] for row in rows]
                report_nos = [row[1] for row in rows]
                # "All" still means all *uninvoiced* reports for this type,
                # so we restrict to their samples rather than leaving the
                # filter as None (which would mean literally every sample
                # in the project, including already-invoiced ones).
                sample_ids_filter = get_sample_ids_for_reports(report_ids, cur)
            elif selected_report_ids:
                cur.execute("""
                    SELECT DISTINCT r.report_id, r.report_no
                    FROM reports r
                    WHERE r.report_id = ANY(%s)
                    AND r.status = 'APPROVED'
                """, (selected_report_ids,))
                rows = cur.fetchall()
                report_ids = [row[0] for row in rows]
                report_nos = [row[1] for row in rows]
                sample_ids_filter = get_sample_ids_for_reports(report_ids, cur)
            else:
                report_nos = []
                sample_ids_filter = []  # explicitly nothing selected

            if not sample_ids_filter:
                raise HTTPException(
                    400,
                    "No uninvoiced reports were selected for this invoice."
                )

        # ---------------------------------------------------
        # 2. Create the invoice, restricted to the resolved samples
        #    (reuses the same logic as create_invoice_with_payment_method,
        #    just scoped to the selected reports' samples).
        # ---------------------------------------------------
        invoice_payload = {
            "project_id": project_id,
            "invoice_type": invoice_type,
            "payment_method": payment_method,
            "invoice_date": payload.get("invoice_date") or date.today().isoformat(),
            "client_reference": payload.get("client_reference"),
            "lpo_reference": payload.get("lpo_reference"),
            "lpo_date": payload.get("lpo_date"),
            "payment_terms": payload.get("payment_terms") or ("30 days" if invoice_type in ["PROFORMA", "TAX"] else "Immediate"),
            "services_description": payload.get("services_description") or "Professional services rendered",
            "remarks": payload.get("remarks"),
        }

        invoice_create = InvoiceCreate(**invoice_payload)
        invoice_result = _create_invoice_with_payment_method_impl(invoice_create, sample_ids=sample_ids_filter)
        invoice_id = invoice_result["invoice_id"]

        print(f"DEBUG: Created invoice {invoice_result['invoice_no']} with ID {invoice_id}")

        # ---------------------------------------------------
        # 3. Record report links for PROFORMA/TAX invoices, using the
        #    same report_nos we already resolved in step 1 above —
        #    no need to re-query.
        # ---------------------------------------------------
        if invoice_type in ["PROFORMA", "TAX"] and report_nos:
            print(f"DEBUG: Will link {len(report_nos)} reports to invoice")

            for report_no in report_nos:
                try:
                    cur.execute("""
                        INSERT INTO invoice_report_links (invoice_id, report_no, invoice_type)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (invoice_id, report_no, invoice_type) DO NOTHING
                    """, (invoice_id, report_no, invoice_type))
                except Exception as e:
                    print(f"WARNING: Could not link report {report_no}: {e}")

            conn.commit()

        return {
            "invoice_id": invoice_id,
            "invoice_no": invoice_result["invoice_no"],
            "message": "Invoice created successfully",
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"ERROR in generate_invoice_with_reports_and_tests: {str(e)}")
        traceback.print_exc()
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error generating invoice: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.patch("/{invoice_id}/payment-status")
def update_invoice_payment_status(invoice_id: int, status_update: dict):
    """
    Update invoice payment status (PAID/UNPAID)
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Validate status
        new_status = status_update.get("payment_status")
        if new_status not in ["PAID", "UNPAID"]:
            raise HTTPException(status_code=400, detail="Status must be PAID or UNPAID")
        
        # Update payment status and paid_date
        paid_date = None
        if new_status == "PAID":
            paid_date = status_update.get("paid_date") or date.today()
        
        cur.execute("""
            UPDATE invoices 
            SET payment_status = %s, paid_date = %s
            WHERE invoice_id = %s
            RETURNING invoice_id, invoice_no, payment_status, paid_date
        """, (new_status, paid_date, invoice_id))
        
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Invoice not found")
        
        conn.commit()
        
        return {
            "message": f"Invoice {result[1]} payment status updated",
            "invoice_id": result[0],
            "invoice_no": result[1],
            "payment_status": result[2],
            "paid_date": result[3]
        }
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


def download_template_from_supabase(template_type: str = "invoice"):
    """
    Download template from Supabase storage.
    template_type: "invoice" or "delivery_note"
    """
    template_urls = {
        "invoice": "https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/invoices/invoice.xlsx",
        "delivery_note": "https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/invoices/delivery_note.xlsx",
        "invoices_combined": "https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/invoices/invoices.xlsx"
    }
    
    if template_type not in template_urls:
        raise ValueError(f"Template type {template_type} not supported")
    
    url = template_urls[template_type]
    
    try:
        # Download the file
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        # Create a temporary file
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as temp_file:
            temp_file.write(response.content)
            temp_path = temp_file.name
        
        print(f"DEBUG: Downloaded {template_type} template from {url}")
        return temp_path
        
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Failed to download template from {url}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to download template: {e}")

# =====================================================
# COMBINED PROFORMA + TAX INVOICE GENERATION
# =====================================================

# -------------------------------------------------------
# HELPER: Write to a cell that may be part of a merged range
# -------------------------------------------------------
def set_cell(ws, cell_addr: str, value):
    """
    Write value to a cell even if it is part of a merged region.
    openpyxl raises ReadOnlyCell errors when you try to write to
    a non-top-left cell of a merged range, so we always find the
    top-left anchor and write there.
    """
    from openpyxl.utils import column_index_from_string
    import re

    # Parse the address
    match = re.match(r"([A-Za-z]+)(\d+)", cell_addr)
    if not match:
        ws[cell_addr] = value
        return
    col_letter, row_str = match.group(1).upper(), int(match.group(2))
    col_idx = column_index_from_string(col_letter)

    # Check if this cell is inside a merged range
    for merged_range in ws.merged_cells.ranges:
        if (merged_range.min_row <= row_str <= merged_range.max_row and
                merged_range.min_col <= col_idx <= merged_range.max_col):
            # Write to the top-left anchor only
            anchor = ws.cell(row=merged_range.min_row, column=merged_range.min_col)
            anchor.value = value
            return

    # Not merged - write normally
    ws[cell_addr] = value


# -------------------------------------------------------
# HELPER: Clear a row's data cells (merge-safe)
# -------------------------------------------------------
def clear_row_cols(ws, row: int, col_letters: list):
    for col in col_letters:
        set_cell(ws, f"{col}{row}", None)


# -------------------------------------------------------
# CORE: Fill one sheet (Proforma or Tax) from invoice data
# -------------------------------------------------------
def _fill_proforma_sheet(ws, invoice: dict, project: dict, items: list):
    """
    Fill Sheet 1 (Proforma Invoice) of the combined template.

    Cell map (from user spec):
      A5  - Contractor (client_name)
      C11 - Consultant
      C12 - Client Name
      C13 - Plot No.
      C15 - Project Name
      I4  - Invoice Number (Proforma)
      I5  - Date
      I6  - LP No.
      Rows 18-34, cols A/B/D/I/J/K - line items
        A  - Report No. (of the report covering this test/sample)
        B  - Report creation date (dd-mm-yy)
        D  - Description
        I  - Quantity
        J  - Rate
        K  - Amount
      K35 - Subtotal
      K36 - VAT (5%)
      K37 - Grand Total
      C38 - Amount in words
    """
    from openpyxl.styles import Font, Alignment
    from datetime import date as date_type

    def fmt_date(d):
        if not d:
            return " - "
        if isinstance(d, str):
            return d
        if hasattr(d, "strftime"):
            return d.strftime("%d-%b-%Y")
        return str(d)

    def fmt_date_short(d):
        """dd-mm-yy, as requested for the report creation date in column B."""
        if not d:
            return " - "
        if isinstance(d, str):
            return d
        if hasattr(d, "strftime"):
            return d.strftime("%d-%m-%y")
        return str(d)

    # Header
    set_cell(ws, "A5",  project.get("client_name") or " - ")      # Contractor
    set_cell(ws, "C11", project.get("consultant") or " - ")        # Consultant
    set_cell(ws, "C12", project.get("client_name") or " - ")       # Client Name
    set_cell(ws, "C13", project.get("plot_no") or " - ")           # Plot No.
    set_cell(ws, "C15", project.get("project_name") or " - ")      # Project Name
    set_cell(ws, "I4",  invoice.get("invoice_no") or " - ")        # Invoice No.
    set_cell(ws, "I5",  fmt_date(invoice.get("invoice_date")))      # Date
    set_cell(ws, "I6",  invoice.get("lpo_reference") or " - ")     # LP No.

    FIRST_ROW = 18
    LAST_TEMPLATE_ROW = 34
    ITEM_COLS = ["A", "B", "D", "I", "J", "K"]

    # Clear all item rows first
    for r in range(FIRST_ROW, LAST_TEMPLATE_ROW + 1):
        clear_row_cols(ws, r, ITEM_COLS)

    # If more items than template rows, insert extra rows (copy formatting from row 34)
    num_items = len(items)
    if num_items > (LAST_TEMPLATE_ROW - FIRST_ROW + 1):
        extra = num_items - (LAST_TEMPLATE_ROW - FIRST_ROW + 1)
        ws.insert_rows(LAST_TEMPLATE_ROW + 1, amount=extra)
        from copy import copy
        for i in range(extra):
            new_row = LAST_TEMPLATE_ROW + 1 + i
            for col in range(1, 14):
                src = ws.cell(row=LAST_TEMPLATE_ROW, column=col)
                tgt = ws.cell(row=new_row, column=col)
                tgt.font = copy(src.font)
                tgt.border = copy(src.border)
                tgt.fill = copy(src.fill)
                tgt.number_format = src.number_format
                tgt.alignment = copy(src.alignment)

    # Fill item rows
    for idx, item in enumerate(items):
        row = FIRST_ROW + idx
        report_no  = item.get("report_no") or " - "
        report_dt  = fmt_date_short(item.get("report_created_at"))
        desc       = item.get("description") or " - "
        qty        = item.get("quantity") or 0
        rate       = float(item.get("unit_rate") or 0)
        amount     = float(item.get("amount") or qty * rate)

        set_cell(ws, f"A{row}", report_no)
        set_cell(ws, f"B{row}", report_dt)
        set_cell(ws, f"D{row}", desc)
        set_cell(ws, f"I{row}", qty)
        set_cell(ws, f"J{row}", rate)
        set_cell(ws, f"K{row}", round(amount, 2))

    # Totals
    subtotal = float(invoice.get("subtotal", 0))
    vat      = float(invoice.get("vat", 0))
    total    = float(invoice.get("total", 0))

    set_cell(ws, "K35", round(subtotal, 2))
    set_cell(ws, "K36", round(vat, 2))
    set_cell(ws, "K37", round(total, 2))
    set_cell(ws, "C38", invoice.get("amount_in_words") or " - ")


def _fill_tax_sheet(ws, invoice: dict, project: dict, items: list):
    """
    Fill Sheet 2 (Tax Invoice - renamed 'Tax-1') of the combined template.

    Cell map (from user spec):
      A5  - Contractor (client_name, from clients.name)
      C11 - Consultant
      C12 - Client Name
      C13 - Plot No.
      C15 - Project Name
      K4  - Invoice Number (Tax)
      K5  - Date
      K6  - LP No. (this is the project_no from the projects table, NOT the LPO number)
      Rows 18-34, cols A/B/D/I/J/K/L/M - line items
        A  - Report No. (of the report covering this test/sample)
        B  - Report creation date (dd-mm-yy)
        D  - Description
        I  - Quantity
        J  - Rate
        K  - Amount Excl. VAT
        L  - VAT Amount
        M  - Amount Incl. VAT
      M35 - Total Excl. VAT  (=SUM(K18:Kn))
      M36 - Total VAT        (=SUM(L18:Ln))
      M37 - Grand Total Incl VAT (=SUM(M18:Mn))
      D38 - Amount in words
    """
    from openpyxl.styles import Font, Alignment
    from datetime import date as date_type

    def fmt_date(d):
        if not d:
            return " - "
        if isinstance(d, str):
            return d
        if hasattr(d, "strftime"):
            return d.strftime("%d-%b-%Y")
        return str(d)

    def fmt_date_short(d):
        """dd-mm-yy, as requested for the report creation date in column B."""
        if not d:
            return " - "
        if isinstance(d, str):
            return d
        if hasattr(d, "strftime"):
            return d.strftime("%d-%m-%y")
        return str(d)

    # Header
    set_cell(ws, "A5",  project.get("client_name") or " - ")
    set_cell(ws, "C11", project.get("consultant") or " - ")
    set_cell(ws, "C12", project.get("client_name") or " - ")
    set_cell(ws, "C13", project.get("plot_no") or " - ")
    set_cell(ws, "C15", project.get("project_name") or " - ")
    set_cell(ws, "K4",  invoice.get("tax_invoice_no") or invoice.get("invoice_no") or " - ")
    set_cell(ws, "K5",  fmt_date(invoice.get("invoice_date")))
    set_cell(ws, "K6",  project.get("project_no") or " - ")

    FIRST_ROW = 18
    LAST_TEMPLATE_ROW = 34
    ITEM_COLS = ["A", "B", "D", "I", "J", "K", "L", "M"]

    # Clear all item rows first
    for r in range(FIRST_ROW, LAST_TEMPLATE_ROW + 1):
        clear_row_cols(ws, r, ITEM_COLS)

    # Insert extra rows if needed
    num_items = len(items)
    if num_items > (LAST_TEMPLATE_ROW - FIRST_ROW + 1):
        extra = num_items - (LAST_TEMPLATE_ROW - FIRST_ROW + 1)
        ws.insert_rows(LAST_TEMPLATE_ROW + 1, amount=extra)
        from copy import copy
        for i in range(extra):
            new_row = LAST_TEMPLATE_ROW + 1 + i
            for col in range(1, 14):
                src = ws.cell(row=LAST_TEMPLATE_ROW, column=col)
                tgt = ws.cell(row=new_row, column=col)
                tgt.font = copy(src.font)
                tgt.border = copy(src.border)
                tgt.fill = copy(src.fill)
                tgt.number_format = src.number_format
                tgt.alignment = copy(src.alignment)

    # Fill item rows
    for idx, item in enumerate(items):
        row = FIRST_ROW + idx
        report_no  = item.get("report_no") or " - "
        report_dt  = fmt_date_short(item.get("report_created_at"))
        desc       = item.get("description") or " - "
        qty        = item.get("quantity") or 0
        rate       = float(item.get("unit_rate") or 0)
        excl_vat   = float(item.get("amount") or qty * rate)
        vat_amt    = round(excl_vat * 0.05, 2)
        incl_vat   = round(excl_vat + vat_amt, 2)

        set_cell(ws, f"A{row}", report_no)
        set_cell(ws, f"B{row}", report_dt)
        set_cell(ws, f"D{row}", desc)
        set_cell(ws, f"I{row}", qty)
        set_cell(ws, f"J{row}", rate)
        set_cell(ws, f"K{row}", round(excl_vat, 2))
        set_cell(ws, f"L{row}", vat_amt)
        set_cell(ws, f"M{row}", incl_vat)

    # Totals
    subtotal = float(invoice.get("subtotal", 0))
    vat      = float(invoice.get("vat", 0))
    total    = float(invoice.get("total", 0))

    set_cell(ws, "M35", round(subtotal, 2))   # Total Excl. VAT
    set_cell(ws, "M36", round(vat, 2))         # Total VAT
    set_cell(ws, "M37", round(total, 2))        # Grand Total Incl. VAT
    set_cell(ws, "D38", invoice.get("amount_in_words") or " - ")


# -------------------------------------------------------
# MAIN: Generate combined Proforma + Tax workbook
# -------------------------------------------------------
@router.get("/{invoice_id}/excel-combined")
def generate_excel_invoice_combined(invoice_id: int):
    """
    Generate a combined Proforma + Tax Invoice workbook (2 sheets).
    Sheet 1 = Proforma Invoice, Sheet 2 = Tax Invoice (Tax-1).
    Uses the invoices.xlsx 2-sheet template from Supabase.
    """
    import re, urllib.parse, os, traceback
    from fastapi.responses import FileResponse

    template_path = download_template_from_supabase("invoices_combined")
    if not os.path.exists(template_path):
        raise HTTPException(status_code=404, detail="Combined invoice template not found.")

    conn = get_connection()
    cur = conn.cursor()

    try:
        # 1. Load full invoice
        invoice = get_invoice_complete(invoice_id, cur)
        invoice_type = invoice.get("invoice_type", "")
        project_details = invoice.get("project_details", {})

        # 2. Enrich project_details with consultant / plot_no from projects table.
        #    NOTE: client_name is intentionally NOT overwritten here — it already
        #    comes from clients.name via get_invoice_complete, which is the
        #    correct company/client name. projects.client_name is a separate,
        #    unrelated free-text column and was previously clobbering the
        #    correct value (showing client_id-like text on the invoice).
        cur.execute("""
            SELECT consultant, plot_no
            FROM projects
            WHERE project_id = %s
        """, (invoice.get("project_id"),))
        proj_extra = cur.fetchone()
        if proj_extra:
            project_details["consultant"]  = proj_extra[0] or " - "
            project_details["plot_no"]     = proj_extra[1] or " - "

        # 3. Items for the workbook.
        #    invoice["items"] (from get_invoice_complete) already reflects
        #    exactly the samples/tests belonging to the reports the user
        #    selected when the invoice was created — no further filtering
        #    or report-matching needed here.
        items = invoice.get("items", [])

        # 3b. Attach the governing report's report_no / created_at to each
        #     item, for display in columns A/B of the Tax-1 sheet.
        item_sample_ids = [it.get("sample_id") for it in items if it.get("sample_id")]
        report_info_by_sample = get_report_info_for_samples(item_sample_ids, cur)
        for it in items:
            info = report_info_by_sample.get(it.get("sample_id"))
            it["report_no"] = info["report_no"] if info else None
            it["report_created_at"] = info["created_at"] if info else None

        # 4. Generate tax invoice number (TAX numbering sequence)
        tax_invoice_no = generate_invoice_no(cur, "TAX")
        # Don't commit yet - we're generating only, not persisting a new invoice row.
        # Just use it for the Excel label.
        invoice["tax_invoice_no"] = tax_invoice_no

        # 5. Load the 2-sheet workbook
        wb = openpyxl.load_workbook(template_path, data_only=False)

        if len(wb.worksheets) < 2:
            raise HTTPException(
                status_code=500,
                detail="Combined template must have 2 sheets (Proforma + Tax). "
                       "Please upload the correct invoices.xlsx to Supabase."
            )

        ws_proforma = wb.worksheets[0]   # Sheet 1
        ws_tax      = wb.worksheets[1]   # Sheet 2

        # 6. Fill both sheets
        _fill_proforma_sheet(ws_proforma, invoice, project_details, items)
        _fill_tax_sheet(ws_tax, invoice, project_details, items)

        # 7. Save & return
        output_dir = "generated_invoices"
        os.makedirs(output_dir, exist_ok=True)

        invoice_no        = invoice.get("invoice_no", "invoice").replace("/", "-")
        project_name      = project_details.get("project_name", "")
        lpo_ref           = invoice.get("lpo_reference", "")

        def _clean(text):
            if not text:
                return ""
            text = re.sub(r'[\\/*?:"<>|]', '-', text)
            return re.sub(r'\s+', '-', text).strip('- ')

        if lpo_ref and lpo_ref not in (" - ", ""):
            filename = f"{invoice_no}-{_clean(project_name)}-{_clean(lpo_ref)}-Combined.xlsx"
        else:
            filename = f"{invoice_no}-{_clean(project_name)}-Combined.xlsx"

        output_path = os.path.join(output_dir, f"{invoice_no}-combined.xlsx")
        wb.save(output_path)

        encoded = urllib.parse.quote(filename)
        return FileResponse(
            output_path,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}; filename=\"{filename}\""
            }
        )

    except Exception as e:
        print(f"ERROR in generate_excel_invoice_combined: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# =====================================================
# WALK-IN LP: Combined Proforma + Tax Invoice
#
# Endpoint: POST /invoices/walkin/generate-invoice
#
# Flow:
#   1. Validate project is is_walk_in = TRUE and has an LP number
#   2. Resolve which walk_in_items to include
#   3. Calculate totals (subtotal / VAT 5% / grand total)
#   4. Generate PROFORMA invoice number + TAX invoice number
#   5. Insert invoice header + items into invoices / invoice_items tables
#   6. Fill the 2-sheet combined workbook (invoices.xlsx template)
#      PAGE 1 (Proforma):
#        A5  = walk_in_client (Contractor)
#        B7  = walk_in_phone
#        C11 = consultant
#        C12 = client_name   (Client / Owner)
#        C13 = plot_no
#        C15 = project_name
#        I4  = proforma invoice number
#        I5  = date
#        I6  = project_no (LP number)
#        D18:D34 = description, I = qty, J = rate, K = amount
#        K35 = subtotal, K36 = VAT, K37 = grand total, C38 = words
#      PAGE 2 (Tax-1):
#        same header layout but K4/K5/K6 instead of I4/I5/I6
#        D/I/J/K/L/M line items with VAT split
#        M35/M36/M37 = totals, D38 = words
#   7. Return FileResponse (xlsx download)
# =====================================================


class WalkInInvoiceRequest(BaseModel):
    project_id: int
    payment_method: Optional[Literal['CASH', 'CREDIT']] = 'CASH'
    selected_item_ids: Optional[List[int]] = None
    include_all_items: bool = True


@router.post("/walkin/generate-invoice")
def generate_walkin_invoice(payload: WalkInInvoiceRequest):
    """
    Generate a combined Proforma + Tax Invoice workbook for a Walk-In LP.
    Uses walk_in_items directly — no test_requests / samples / reports involved.
    """
    import re as _re
    import urllib.parse as _urlparse

    conn = get_connection()
    cur = conn.cursor()

    try:
        # ──────────────────────────────────────────────────────────
        # 1. Load walk-in project details
        # ──────────────────────────────────────────────────────────
        cur.execute("""
            SELECT project_id, project_no, walk_in_client, walk_in_phone,
                   consultant, plot_no, client_name, project_name
            FROM projects
            WHERE project_id = %s AND is_walk_in = TRUE
        """, (payload.project_id,))
        proj = cur.fetchone()
        if not proj:
            raise HTTPException(404, "Walk-in LP not found")

        (project_id, project_no, walk_in_client, walk_in_phone,
         consultant, plot_no, client_name, project_name) = proj

        if not project_no or project_no == "PENDING":
            raise HTTPException(400, "LP number has not been generated for this walk-in yet")

        contractor    = walk_in_client or project_name or "—"
        phone         = walk_in_phone or ""
        consultant    = consultant or ""
        plot_no       = plot_no or ""
        client_field  = client_name or ""   # Client/Owner → C12
        proj_name     = project_name or contractor

        # ──────────────────────────────────────────────────────────
        # 2. Resolve items
        #
        # BUSINESS RULE: Only tests that have a Payment Advice
        # (pa_generated = TRUE) can be invoiced. Tests without a PA
        # are not yet invoiceable and must be excluded regardless of
        # what the frontend sends.
        # ──────────────────────────────────────────────────────────
        if payload.include_all_items:
            # "Select All" from the UI = all ADVISED items for this LP
            cur.execute("""
                SELECT item_id, description, test_standard, unit_rate, quantity, amount
                FROM walk_in_items
                WHERE project_id = %s AND pa_generated = TRUE
                ORDER BY item_id
            """, (project_id,))
        else:
            if not payload.selected_item_ids:
                raise HTTPException(400, "No tests selected")
            # Explicit selection — enforce pa_generated = TRUE as a safety guard
            cur.execute("""
                SELECT item_id, description, test_standard, unit_rate, quantity, amount
                FROM walk_in_items
                WHERE project_id = %s
                  AND item_id = ANY(%s)
                  AND pa_generated = TRUE
                ORDER BY item_id
            """, (project_id, payload.selected_item_ids))

        rows = cur.fetchall()
        if not rows:
            raise HTTPException(
                400,
                "No invoiceable tests found. Only tests with a Payment Advice (Already Advised) "
                "can be included on an invoice. Please generate a Payment Advice first."
            )

        items = [
            {
                "item_id":      r[0],
                "description":  r[1] or "—",
                "test_standard": r[2] or "",
                "unit_rate":    float(r[3]) if r[3] is not None else 0.0,
                "quantity":     r[4] or 1,
                "amount":       float(r[5]) if r[5] is not None else 0.0,
            }
            for r in rows
        ]

        # ──────────────────────────────────────────────────────────
        # 3. Totals
        # ──────────────────────────────────────────────────────────
        subtotal    = sum(i["amount"] for i in items)
        vat         = round(subtotal * 0.05, 2)
        grand_total = round(subtotal + vat, 2)
        words       = number_to_words(grand_total)

        # ──────────────────────────────────────────────────────────
        # 4. Invoice numbers
        # ──────────────────────────────────────────────────────────
        proforma_no = generate_invoice_no(cur, "PROFORMA")
        tax_no      = generate_invoice_no(cur, "TAX")

        payment_terms = "30 days" if payload.payment_method == "CREDIT" else "Immediate"

        # ──────────────────────────────────────────────────────────
        # 5. Persist invoice header (walk-in projects have no client_id /
        #    quotation_id so we use the PROFORMA invoice row only; the Tax
        #    number is recorded in the remarks field for traceability).
        # ──────────────────────────────────────────────────────────
        cur.execute("""
            INSERT INTO invoices (
                invoice_no, project_id, invoice_type, payment_method, invoice_date,
                lpo_reference, payment_terms,
                subtotal, vat, total, amount_in_words,
                services_description, remarks, payment_status
            )
            VALUES (%s, %s, 'PROFORMA', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'UNPAID')
            RETURNING invoice_id
        """, (
            proforma_no,
            project_id,
            payload.payment_method,
            date.today(),
            project_no,          # LP number stored as lpo_reference for walk-ins
            payment_terms,
            subtotal,
            vat,
            grand_total,
            words,
            f"Testing services — {proj_name}",
            f"Tax Invoice No: {tax_no}",
        ))
        invoice_id = cur.fetchone()[0]

        # Insert invoice items (one row per walk_in_item)
        for item in items:
            cur.execute("""
                INSERT INTO invoice_items (
                    invoice_id, description, test_standard,
                    unit_rate, quantity, amount
                )
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                invoice_id,
                item["description"],
                item["test_standard"],
                item["unit_rate"],
                item["quantity"],
                item["amount"],
            ))

        conn.commit()

        # ──────────────────────────────────────────────────────────
        # 6. Fill the 2-sheet Excel workbook
        # ──────────────────────────────────────────────────────────
        template_path = download_template_from_supabase("invoices_combined")
        if not os.path.exists(template_path):
            raise HTTPException(404, "Combined invoice template not found. "
                                     "Upload invoices.xlsx to Supabase templates/invoices/.")

        wb = openpyxl.load_workbook(template_path, data_only=False)

        if len(wb.worksheets) < 2:
            raise HTTPException(
                500,
                "Combined template must have 2 sheets (Proforma + Tax). "
                "Please upload the correct invoices.xlsx to Supabase."
            )

        ws_proforma = wb.worksheets[0]
        ws_tax      = wb.worksheets[1]

        today_str = date.today().strftime("%d-%b-%Y")

        # ── Helper already defined at module level as set_cell() ──────────

        # ── PAGE 1: Proforma Invoice ──────────────────────────────────────
        _wi_fill_proforma(ws_proforma, {
            "contractor":   contractor,
            "phone":        phone,
            "consultant":   consultant,
            "client_name":  client_field,
            "plot_no":      plot_no,
            "project_name": proj_name,
            "invoice_no":   proforma_no,
            "date":         today_str,
            "lp_number":    project_no,
            "subtotal":     subtotal,
            "vat":          vat,
            "grand_total":  grand_total,
            "words":        words,
        }, items)

        # ── PAGE 2: Tax Invoice (Tax-1) ───────────────────────────────────
        _wi_fill_tax(ws_tax, {
            "contractor":   contractor,
            "phone":        phone,
            "consultant":   consultant,
            "client_name":  client_field,
            "plot_no":      plot_no,
            "project_name": proj_name,
            "invoice_no":   tax_no,
            "date":         today_str,
            "lp_number":    project_no,
            "subtotal":     subtotal,
            "vat":          vat,
            "grand_total":  grand_total,
            "words":        words,
        }, items)

        # ──────────────────────────────────────────────────────────
        # 7. Save & return
        # ──────────────────────────────────────────────────────────
        output_dir = "generated_invoices"
        os.makedirs(output_dir, exist_ok=True)

        def _clean(text):
            if not text:
                return ""
            text = _re.sub(r'[\\/*?":<>|]', '-', text)
            return _re.sub(r'\s+', '-', text).strip('- ')

        proforma_no_hyphen = proforma_no.replace('/', '-')
        filename    = f"{proforma_no_hyphen}-{_clean(proj_name)}-WalkIn-Combined.xlsx"
        output_path = os.path.join(output_dir, f"{proforma_no_hyphen}-walkin.xlsx")
        wb.save(output_path)

        encoded = _urlparse.quote(filename)
        from fastapi.responses import FileResponse as _FR
        return _FR(
            output_path,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}; filename=\"{filename}\""
            }
        )

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        print(f"ERROR in generate_walkin_invoice: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error generating walk-in invoice: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS: fill Proforma and Tax sheets for walk-in invoices
# (separate from _fill_proforma_sheet / _fill_tax_sheet which are used for
#  standard report-based invoices — different header cell layout)
# ──────────────────────────────────────────────────────────────────────────────

def _wi_fill_proforma(ws, meta: dict, items: list):
    """
    Fill Sheet 1 (Proforma Invoice) for a Walk-In LP.

    Header cell map:
      A5  = Contractor (walk_in_client)
      B7  = Phone
      C11 = Consultant
      C12 = Client/Owner name  (client_name column)
      C13 = Plot No.
      C15 = Project Name
      I4  = Proforma Invoice Number
      I5  = Date
      I6  = LP No. (project_no)

    Line items (rows 18–34, extendable):
      D = Description
      I = Quantity
      J = Rate
      K = Amount (= Qty × Rate)

    Summary:
      K35 = Subtotal
      K36 = VAT (5%)
      K37 = Grand Total
      C38 = Amount in Words
    """
    FIRST_ROW         = 18
    LAST_TEMPLATE_ROW = 34
    ITEM_COLS         = ["D", "I", "J", "K"]

    # Header
    set_cell(ws, "A5",  meta.get("contractor") or "—")
    set_cell(ws, "B7",  meta.get("phone") or "")
    set_cell(ws, "C11", meta.get("consultant") or "")
    set_cell(ws, "C12", meta.get("client_name") or "")
    set_cell(ws, "C13", meta.get("plot_no") or "")
    set_cell(ws, "C15", meta.get("project_name") or "")
    set_cell(ws, "I4",  meta.get("invoice_no") or "")
    set_cell(ws, "I5",  meta.get("date") or "")
    set_cell(ws, "I6",  meta.get("lp_number") or "")

    # Clear existing item rows
    for r in range(FIRST_ROW, LAST_TEMPLATE_ROW + 1):
        clear_row_cols(ws, r, ITEM_COLS)

    # Insert extra rows if more items than template allows
    num_items = len(items)
    template_slots = LAST_TEMPLATE_ROW - FIRST_ROW + 1
    if num_items > template_slots:
        extra = num_items - template_slots
        ws.insert_rows(LAST_TEMPLATE_ROW + 1, amount=extra)
        from copy import copy as _copy
        for i in range(extra):
            new_row = LAST_TEMPLATE_ROW + 1 + i
            for col in range(1, 14):
                src = ws.cell(row=LAST_TEMPLATE_ROW, column=col)
                tgt = ws.cell(row=new_row, column=col)
                tgt.font        = _copy(src.font)
                tgt.border      = _copy(src.border)
                tgt.fill        = _copy(src.fill)
                tgt.number_format = src.number_format
                tgt.alignment   = _copy(src.alignment)

    # Fill item rows
    for idx, item in enumerate(items):
        row = FIRST_ROW + idx
        set_cell(ws, f"D{row}", item.get("description") or "—")
        set_cell(ws, f"I{row}", item.get("quantity") or 0)
        set_cell(ws, f"J{row}", item.get("unit_rate") or 0)
        set_cell(ws, f"K{row}", f'=IF(I{row}="","",(I{row}*J{row}))')

    # Blank remaining IF formulas inside K35 SUM range
    for row in range(FIRST_ROW + num_items, LAST_TEMPLATE_ROW + 1):
        set_cell(ws, f"K{row}", f'=IF(I{row}="","",(I{row}*J{row}))')

    # Totals
    set_cell(ws, "K35", f"=SUM(K{FIRST_ROW}:K{LAST_TEMPLATE_ROW})")
    set_cell(ws, "K36", "=K35*5%")
    set_cell(ws, "K37", "=K35+K36")
    set_cell(ws, "C38", meta.get("words") or "")


def _wi_fill_tax(ws, meta: dict, items: list):
    """
    Fill Sheet 2 (Tax Invoice – 'Tax-1') for a Walk-In LP.

    Header cell map:
      A5  = Contractor (walk_in_client)
      B7  = Phone
      C11 = Consultant
      C12 = Client/Owner name
      C13 = Plot No.
      C15 = Project Name
      K4  = Tax Invoice Number
      K5  = Date
      K6  = LP No. (project_no)

    Line items (rows 18–34, extendable):
      D = Description
      I = Quantity
      J = Rate
      K = Amount Excl. VAT  (= Qty × Rate)
      L = VAT Amount        (= K × 5%)
      M = Amount Incl. VAT  (= K + L)

    Summary:
      M35 = Total Excl. VAT  (=SUM(K18:K34))
      M36 = Total VAT        (=SUM(L18:L34))
      M37 = Grand Total      (=SUM(M18:M34))
      D38 = Amount in Words
    """
    FIRST_ROW         = 18
    LAST_TEMPLATE_ROW = 34
    ITEM_COLS         = ["D", "I", "J", "K", "L", "M"]

    # Header
    set_cell(ws, "A5",  meta.get("contractor") or "—")
    set_cell(ws, "B7",  meta.get("phone") or "")
    set_cell(ws, "C11", meta.get("consultant") or "")
    set_cell(ws, "C12", meta.get("client_name") or "")
    set_cell(ws, "C13", meta.get("plot_no") or "")
    set_cell(ws, "C15", meta.get("project_name") or "")
    set_cell(ws, "K4",  meta.get("invoice_no") or "")
    set_cell(ws, "K5",  meta.get("date") or "")
    set_cell(ws, "K6",  meta.get("lp_number") or "")

    # Clear existing item rows
    for r in range(FIRST_ROW, LAST_TEMPLATE_ROW + 1):
        clear_row_cols(ws, r, ITEM_COLS)

    # Insert extra rows if needed
    num_items = len(items)
    template_slots = LAST_TEMPLATE_ROW - FIRST_ROW + 1
    if num_items > template_slots:
        extra = num_items - template_slots
        ws.insert_rows(LAST_TEMPLATE_ROW + 1, amount=extra)
        from copy import copy as _copy
        for i in range(extra):
            new_row = LAST_TEMPLATE_ROW + 1 + i
            for col in range(1, 14):
                src = ws.cell(row=LAST_TEMPLATE_ROW, column=col)
                tgt = ws.cell(row=new_row, column=col)
                tgt.font        = _copy(src.font)
                tgt.border      = _copy(src.border)
                tgt.fill        = _copy(src.fill)
                tgt.number_format = src.number_format
                tgt.alignment   = _copy(src.alignment)

    # Fill item rows with formulas for VAT split
    for idx, item in enumerate(items):
        row = FIRST_ROW + idx
        set_cell(ws, f"D{row}", item.get("description") or "—")
        set_cell(ws, f"I{row}", item.get("quantity") or 0)
        set_cell(ws, f"J{row}", item.get("unit_rate") or 0)
        set_cell(ws, f"K{row}", f'=IF(I{row}="","",(I{row}*J{row}))')
        set_cell(ws, f"L{row}", f'=IF(K{row}="","",K{row}*5%)')
        set_cell(ws, f"M{row}", f'=IF(K{row}="","",K{row}+L{row})')

    # Blank remaining formula rows
    for row in range(FIRST_ROW + num_items, LAST_TEMPLATE_ROW + 1):
        set_cell(ws, f"K{row}", f'=IF(I{row}="","",(I{row}*J{row}))')
        set_cell(ws, f"L{row}", f'=IF(K{row}="","",K{row}*5%)')
        set_cell(ws, f"M{row}", f'=IF(K{row}="","",K{row}+L{row})')

    # Totals
    set_cell(ws, "M35", f"=SUM(K{FIRST_ROW}:K{LAST_TEMPLATE_ROW})")
    set_cell(ws, "M36", f"=SUM(L{FIRST_ROW}:L{LAST_TEMPLATE_ROW})")
    set_cell(ws, "M37", f"=SUM(M{FIRST_ROW}:M{LAST_TEMPLATE_ROW})")
    set_cell(ws, "D38", meta.get("words") or "")