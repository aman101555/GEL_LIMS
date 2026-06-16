from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Literal
from datetime import date, datetime
from db import get_connection
from decimal import Decimal
from fastapi.responses import HTMLResponse
import traceback
from utils import resource_path
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
    payment_method: Optional[Literal['CASH', 'CREDIT']] = 'CASH'
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
    payment_method: str
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
# NEW: Models for Test/Report Selection
# =====================================================
class InvoiceableTestItem(BaseModel):
    type: Literal['report', 'test']
    id: int
    name: str
    report_no: Optional[str] = None
    test_type: str
    unit_rate: float
    quantity: int
    amount: float
    already_invoiced: bool = False


class InvoiceReportSelection(BaseModel):
    project_id: int
    selected_report_ids: Optional[List[int]] = None
    selected_test_ids: Optional[List[int]] = None
    include_all_reports: bool = True
    include_all_tests: bool = False
    invoice_type: Literal['PROFORMA', 'TAX']


# ----------------------------
# Utility Functions
# ----------------------------
def number_to_words(num: float) -> str:
    """Convert number to words for amount in words field"""
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
        millions = whole_part // 1000000
        remainder = whole_part % 1000000
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
        words = words.strip()
    
    if decimal_part > 0:
        result = f"{words} Dirhams and {convert_less_than_thousand(decimal_part)} Fils Only"
    else:
        result = f"{words} Dirhams Only"
    
    return result


def generate_invoice_no(cur, invoice_type: str) -> str:
    """Generate invoice number with different systems for PROFORMA vs other invoices"""
    year_short = str(datetime.now().year)[-2:]
    
    print(f"DEBUG generate_invoice_no: invoice_type='{invoice_type}', year_short='{year_short}'")
    
    if invoice_type.upper() == 'PROFORMA':
        print("DEBUG: Generating PROFORMA invoice number")
        
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
                last_number_str = last_proforma[0].split('/')[0]
                last_number = int(last_number_str)
                
                if last_number <= 999:
                    next_number = last_number + 1
                    invoice_no = f"{next_number:03d}/{year_short}"
                    print(f"DEBUG: Incremented new format PROFORMA: {last_number} -> {next_number}")
                else:
                    next_number = 1
                    invoice_no = f"{next_number:03d}/{year_short}"
                    print(f"DEBUG: Old format found, starting new format from 001")
            except (ValueError, IndexError):
                next_number = 1
                invoice_no = f"{next_number:03d}/{year_short}"
                print(f"DEBUG: Parse failed, starting from 001")
        else:
            next_number = 1
            invoice_no = f"{next_number:03d}/{year_short}"
            print(f"DEBUG: No existing PROFORMA, starting from 001")
        
        print(f"DEBUG: Generated PROFORMA invoice_no: {invoice_no}")
        return invoice_no
    
    print(f"DEBUG: Generating non-PROFORMA invoice number for type: {invoice_type}")
    
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
        try:
            last_number_str = last_invoice[0].split('/')[0]
            last_number = int(last_number_str)
            next_number = last_number + 1
            print(f"DEBUG: Parsed last non-PROFORMA number: {last_number}, next: {next_number}")
        except (ValueError, IndexError) as e:
            print(f"DEBUG: Failed to parse non-PROFORMA number '{last_invoice[0]}': {e}")
            next_number = 36001
    else:
        print("DEBUG: First non-PROFORMA invoice ever")
        next_number = 36001
    
    invoice_no = f"{next_number}/{year_short}"
    print(f"DEBUG: Generated non-PROFORMA invoice_no: {invoice_no}")
    return invoice_no


def download_template_from_supabase(template_type: str = "invoice"):
    """
    Download template from Supabase storage.
    template_type: "invoice" or "delivery_note"
    """
    template_urls = {
        "invoice": "https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/invoices/invoice.xlsx",
        "delivery_note": "https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/invoices/delivery_note.xlsx"
    }
    
    if template_type not in template_urls:
        raise ValueError(f"Template type {template_type} not supported")
    
    url = template_urls[template_type]
    
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as temp_file:
            temp_file.write(response.content)
            temp_path = temp_file.name
        
        print(f"DEBUG: Downloaded {template_type} template from {url}")
        return temp_path
        
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Failed to download template from {url}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to download template: {e}")


def get_invoice_complete(invoice_id: int, cur):
    """Get complete invoice details with items"""
    cur.execute("""
        SELECT i.invoice_id, i.invoice_no, i.project_id, i.invoice_type, i.payment_method, i.invoice_date,
               i.client_reference, i.lpo_reference, i.lpo_date, i.payment_terms,
               i.subtotal, i.vat, i.total, i.amount_in_words, i.services_description, 
               i.remarks, i.payment_status, i.paid_date,
               p.project_no, p.project_name, p.location,
               c.client_id, c.name, c.contact_person, c.email, c.address, c.phone,
               p.is_walk_in, p.walk_in_client
        FROM invoices i
        JOIN projects p ON i.project_id = p.project_id
        LEFT JOIN clients c ON p.client_id = c.client_id
        WHERE i.invoice_id = %s
    """, (invoice_id,))
    
    header = cur.fetchone()
    if not header:
        raise HTTPException(status_code=404, detail="Invoice not found")
    
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
            tr.test_request_id,
            r.report_no  -- Add report_no from reports table
        FROM invoice_items ii
        LEFT JOIN samples s ON ii.sample_id = s.sample_id
        LEFT JOIN test_requests tr ON ii.test_request_id = tr.test_request_id
        LEFT JOIN reports r ON s.sample_id = r.sample_id AND r.status = 'APPROVED'
        WHERE ii.invoice_id = %s
        ORDER BY ii.item_id
    """, (invoice_id,))
    
    items_data = cur.fetchall()
    
    items_list = []
    for item in items_data:
        item_amount = float(item[5]) if isinstance(item[5], Decimal) else item[5]
        
        # Get report_no if available
        report_no = item[11] if len(item) > 11 and item[11] else "-"
        
        items_list.append({
            "item_id": item[0],
            "description": item[1],
            "test_standard": item[2] or "N/A",
            "unit_rate": float(item[3]) if isinstance(item[3], Decimal) else item[3],
            "quantity": item[4],
            "amount": item_amount,
            "sample_id": item[6],
            "sample_no": item[7],
            "sample_status": item[8],
            "request_no": item[9],
            "test_request_id": item[10],
            "report_no": report_no  # Add report_no to items
        })
    
    # Determine client name (handle walk-in)
    client_name = header[21]  # c.name
    is_walk_in = header[26] if len(header) > 26 else False
    walk_in_client = header[27] if len(header) > 27 else None
    
    if is_walk_in and walk_in_client:
        client_name = walk_in_client
    
    return {
        "invoice_id": header[0],
        "invoice_no": header[1],
        "project_id": header[2],
        "invoice_type": header[3],
        "payment_method": header[4],
        "invoice_date": header[5],
        "client_reference": header[6],
        "lpo_reference": header[7],
        "lpo_date": header[8],
        "payment_terms": header[9],
        "subtotal": float(header[10]) if isinstance(header[10], Decimal) else header[10],
        "vat": float(header[11]) if isinstance(header[11], Decimal) else header[11],
        "total": float(header[12]) if isinstance(header[12], Decimal) else header[12],
        "amount_in_words": header[13],
        "services_description": header[14],
        "remarks": header[15],
        "payment_status": header[16],
        "paid_date": header[17],
        "items": items_list,
        "project_details": {
            "project_no": header[18],
            "project_name": header[19],
            "location": header[20],
            "client_name": client_name,
            "client_contact": header[22],
            "client_email": header[23],
            "client_address": header[24],
            "client_phone": header[25],
            "is_walk_in": is_walk_in,
            "walk_in_client": walk_in_client
        }
    }


def get_project_quotation_items(project_id: int, cur):
    """Get invoiceable items for a project"""
    cur.execute("""
        SELECT qi.item_id, qi.description, qi.test_standard, qi.unit_rate, qi.quantity,
               q.quotation_id
        FROM quotation_items qi
        JOIN quotations q ON qi.quotation_id = q.quotation_id
        JOIN projects p ON q.quotation_id = p.quotation_id
        WHERE p.project_id = %s
    """, (project_id,))
    
    return cur.fetchall()


# =====================================================
# UPDATED: Get Invoiceable Items (Reports + Tests + Walk-in)
# =====================================================
@router.get("/projects/{project_id}/invoiceable-items-v2")
def get_invoiceable_items_v2(project_id: int):
    """
    Get all invoiceable items including reports, individual tests, and walk-in items
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get project details
        cur.execute("""
            SELECT p.project_id, p.project_no, p.project_name, 
                   c.name as client_name,
                   p.is_walk_in,
                   p.walk_in_client,
                   p.quotation_id
            FROM projects p
            LEFT JOIN clients c ON p.client_id = c.client_id
            WHERE p.project_id = %s
        """, (project_id,))
        
        project_data = cur.fetchone()
        if not project_data:
            raise HTTPException(404, "Project not found")
        
        is_walk_in = project_data[4] or False
        walk_in_client = project_data[5]
        quotation_id = project_data[6]
        client_name = project_data[3] or walk_in_client or "Unknown Client"
        
        print(f"DEBUG: Project {project_id} - is_walk_in: {is_walk_in}, quotation_id: {quotation_id}")
        
        # =====================================================
        # 1. Get quotation items (for ALL projects)
        # =====================================================
        invoiceable_items = []
        
        if quotation_id:
            cur.execute("""
                SELECT DISTINCT
                    qi.item_id,
                    qi.description,
                    qi.test_standard,
                    qi.unit_rate,
                    qi.quantity as original_quantity,
                    CASE 
                        WHEN EXISTS (
                            SELECT 1 FROM invoice_items ii
                            WHERE ii.description = qi.description
                            AND ii.invoice_id IN (
                                SELECT invoice_id FROM invoices 
                                WHERE project_id = %s
                                AND invoice_type IN ('PROFORMA', 'TAX')
                            )
                        ) THEN TRUE
                        ELSE FALSE
                    END as already_invoiced
                FROM quotation_items qi
                WHERE qi.quotation_id = %s
                AND qi.description IS NOT NULL
                ORDER BY qi.description
            """, (project_id, quotation_id))
            
            tests = cur.fetchall()
            
            print(f"DEBUG: Found {len(tests)} quotation items")
            
            # Add individual tests
            for test in tests:
                item_id, description, test_standard, unit_rate, original_quantity, already_invoiced = test
                
                unit_rate_float = float(unit_rate) if unit_rate else 0.0
                quantity = original_quantity or 1
                amount = unit_rate_float * quantity
                
                invoiceable_items.append({
                    "id": item_id,
                    "type": "test",
                    "name": description,
                    "report_no": "-",
                    "test_type": description,
                    "unit_rate": unit_rate_float,
                    "quantity": quantity,
                    "amount": amount,
                    "already_invoiced": already_invoiced,
                    "test_standard": test_standard or "N/A",
                    "created_at": None,
                    "status": None
                })
        
        # =====================================================
        # 2. Get reports (only for non-walk-in projects)
        # =====================================================
        reports = []
        if not is_walk_in:
            cur.execute("""
                SELECT DISTINCT ON (r.report_no)
                    r.report_id, 
                    r.report_no, 
                    r.covers_test_type as test_name,
                    r.covers_samples,
                    array_length(r.covers_samples, 1) as sample_count,
                    r.created_at,
                    r.status,
                    EXISTS (
                        SELECT 1 FROM invoice_report_links irl
                        WHERE irl.report_no = r.report_no 
                        AND irl.invoice_type IN ('PROFORMA', 'TAX')
                    ) as already_invoiced,
                    COALESCE(qi.unit_rate, 0) as unit_rate
                FROM reports r
                LEFT JOIN quotation_items qi ON r.covers_test_type = qi.description
                JOIN samples s ON r.sample_id = s.sample_id
                JOIN test_requests tr ON s.request_id = tr.test_request_id
                WHERE tr.project_id = %s 
                AND r.status = 'APPROVED'
                ORDER BY r.report_no, r.created_at DESC
            """, (project_id,))
            reports = cur.fetchall()
            
            print(f"DEBUG: Found {len(reports)} reports")
            
            # Add reports
            for report in reports:
                report_id, report_no, test_name, covers_samples, sample_count, created_at, status, already_invoiced, unit_rate = report
                
                quantity = sample_count or 1
                unit_rate_float = float(unit_rate) if unit_rate else 0.0
                amount = unit_rate_float * quantity
                
                invoiceable_items.append({
                    "id": report_id,
                    "type": "report",
                    "name": report_no,
                    "report_no": report_no,
                    "test_type": test_name or "Report",
                    "unit_rate": unit_rate_float,
                    "quantity": quantity,
                    "amount": amount,
                    "already_invoiced": already_invoiced,
                    "created_at": str(created_at) if created_at else None,
                    "status": status
                })
        
        # =====================================================
        # 3. Build response
        # =====================================================
        return {
            "project_id": project_id,
            "project_no": project_data[1],
            "project_name": project_data[2],
            "client_name": client_name,
            "items": invoiceable_items,
            "total_items": len(invoiceable_items),
            "reports_count": len(reports),
            "tests_count": len(invoiceable_items) - len(reports),
            "is_walk_in": is_walk_in,
            "walk_in_client": walk_in_client
        }
        
    except Exception as e:
        print(f"ERROR in get_invoiceable_items_v2: {str(e)}")
        traceback.print_exc()
        raise HTTPException(500, f"Error fetching invoiceable items: {str(e)}")
    finally:
        cur.close()
        conn.close()


# =====================================================
# Generate Invoice with Reports and Tests
# =====================================================
@router.post("/generate-with-reports-and-tests")
def generate_invoice_with_reports_and_tests(payload: dict):
    """
    Generate invoice with selected reports AND/OR individual tests
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        print(f"DEBUG: Received payload for generate-with-reports-and-tests: {payload}")
        
        project_id = payload.get("project_id")
        invoice_type = payload.get("invoice_type", "PROFORMA")
        payment_method = payload.get("payment_method", "CASH")
        
        selected_report_ids = payload.get("selected_report_ids", [])
        selected_test_ids = payload.get("selected_test_ids", [])
        include_all_reports = payload.get("include_all_reports", False)
        include_all_tests = payload.get("include_all_tests", False)
        
        print(f"DEBUG: Selected reports: {selected_report_ids}")
        print(f"DEBUG: Selected tests: {selected_test_ids}")
        print(f"DEBUG: include_all_reports: {include_all_reports}")
        print(f"DEBUG: include_all_tests: {include_all_tests}")
        
        # Get all invoiceable items
        all_items_response = get_invoiceable_items_v2(project_id)
        all_items_list = all_items_response["items"]
        is_walk_in = all_items_response.get("is_walk_in", False)
        
        print(f"DEBUG: Total items available: {len(all_items_list)}")
        print(f"DEBUG: Is walk-in: {is_walk_in}")
        
        # Filter selected items
        selected_items = []
        
        # Handle reports
        if include_all_reports:
            report_items = [item for item in all_items_list if item["type"] == "report" and not item["already_invoiced"]]
            selected_items.extend(report_items)
        elif selected_report_ids:
            report_items = [
                item for item in all_items_list 
                if item["type"] == "report" and item["id"] in selected_report_ids
            ]
            selected_items.extend(report_items)
        
        # Handle tests
        if include_all_tests:
            test_items = [item for item in all_items_list if item["type"] == "test" and not item["already_invoiced"]]
            selected_items.extend(test_items)
        elif selected_test_ids:
            test_items = [
                item for item in all_items_list 
                if item["type"] == "test" and item["id"] in selected_test_ids
            ]
            selected_items.extend(test_items)
        
        # If no specific selection, include all uninvoiced items (for walk-in support)
        if not selected_items and not selected_report_ids and not selected_test_ids and not include_all_reports and not include_all_tests:
            # For walk-in projects, include all tests by default
            if is_walk_in:
                selected_items = [item for item in all_items_list if item["type"] == "test" and not item["already_invoiced"]]
                print(f"DEBUG: Walk-in project - auto-selected {len(selected_items)} items")
            else:
                raise HTTPException(400, "No items selected for invoicing")
        
        if not selected_items:
            raise HTTPException(400, "No items selected for invoicing")
        
        print(f"DEBUG: Selected {len(selected_items)} items for invoicing")
        
        # Calculate totals
        subtotal = sum(item["amount"] for item in selected_items)
        vat = subtotal * 0.05
        total = subtotal + vat
        amount_words = number_to_words(total)
        
        # Generate invoice number
        invoice_no = generate_invoice_no(cur, invoice_type)
        
        # Get project details
        cur.execute("""
            SELECT p.project_no, p.project_name, p.location, p.lpo_no, p.lpo_date,
                   c.client_id, c.name, c.contact_person, c.email, c.address, c.phone,
                   q.quotation_no,
                   p.is_walk_in, p.walk_in_client
            FROM projects p
            LEFT JOIN clients c ON p.client_id = c.client_id
            LEFT JOIN quotations q ON p.quotation_id = q.quotation_id
            WHERE p.project_id = %s
        """, (project_id,))
        
        project_data = cur.fetchone()
        if not project_data:
            raise HTTPException(404, "Project not found")
        
        # Determine client name (handle walk-in)
        is_walk_in_project = project_data[12] or False
        walk_in_client = project_data[13]
        client_name = project_data[5] or walk_in_client or "Unknown Client"
        
        # Create invoice
        lpo_reference = project_data[3]
        lpo_date = project_data[4]
        payment_terms = "30 days" if payment_method == "CREDIT" else "Immediate"
        invoice_date = date.today()
        
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
            project_id,
            invoice_type,
            payment_method,
            invoice_date,
            payload.get("client_reference"),
            lpo_reference,
            lpo_date,
            payment_terms,
            subtotal,
            vat,
            total,
            amount_words,
            payload.get("services_description") or f"Invoice for {len(selected_items)} items",
            payload.get("remarks"),
            "UNPAID"
        ))
        
        invoice_id = cur.fetchone()[0]
        
        # Insert invoice items
        for item in selected_items:
            # For walk-in items, we might not have a sample_id
            sample_id = None
            
            cur.execute("""
                INSERT INTO invoice_items (
                    invoice_id, description, test_standard,
                    unit_rate, quantity, amount, sample_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                invoice_id,
                item["name"],
                item.get("test_standard", "N/A"),
                item["unit_rate"],
                item["quantity"],
                item["amount"],
                sample_id
            ))
        
        # Record report links for PROFORMA/TAX invoices (skip for walk-in)
        if invoice_type in ["PROFORMA", "TAX"] and not is_walk_in_project:
            for item in selected_items:
                if item["type"] == "report" and item["report_no"] and item["report_no"] != "-":
                    cur.execute("""
                        INSERT INTO invoice_report_links (invoice_id, report_no, invoice_type)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (invoice_id, report_no, invoice_type) DO NOTHING
                    """, (invoice_id, item["report_no"], invoice_type))
        
        conn.commit()
        
        # Return the invoice using existing get_invoice_complete
        return get_invoice_complete(invoice_id, cur)
        
    except Exception as e:
        print(f"ERROR in generate_invoice_with_reports_and_tests: {str(e)}")
        traceback.print_exc()
        if conn:
            conn.rollback()
        raise HTTPException(500, f"Error generating invoice: {str(e)}")
    finally:
        cur.close()
        conn.close()


# =====================================================
# Excel Generation
# =====================================================
@router.get("/{invoice_id}/excel")
def generate_excel_invoice(invoice_id: int):
    """
    Generate Excel invoice using the template, insert rows dynamically,
    fill test items, report numbers, amounts, totals and save on server.
    Supports both reports and individual tests.
    """
    template_path = download_template_from_supabase("invoice")

    if not os.path.exists(template_path):
        raise HTTPException(status_code=404, detail="Invoice template not found")

    conn = get_connection()
    cur = conn.cursor()

    try:
        invoice = get_invoice_complete(invoice_id, cur)
        project_details = invoice.get("project_details", {})
        invoice_type = invoice.get("invoice_type", "CASH")
        items = invoice.get("items", [])
        
        print("=== DEBUG INVOICE DATA ===")
        print(f"Invoice: {invoice.get('invoice_no')} (Type: {invoice_type})")
        print(f"Subtotal: {invoice.get('subtotal')}")
        print(f"Number of items: {len(items)}")
        for i, item in enumerate(items):
            print(f"  Item {i}: {item.get('description')} - {item.get('quantity')} x {item.get('unit_rate')}")
        print("==========================")

        wb = openpyxl.load_workbook(template_path, data_only=False)
        ws = wb.active

        # Set title based on invoice type
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
            title_text = "INVOICE"
        
        ws["A3"] = title_text
        ws["A3"].font = Font(name="Arial", size=12, bold=True, color="00008B")
        ws["A3"].alignment = Alignment(horizontal="center")

        FIRST_ITEM_ROW = 18
        LAST_TEMPLATE_ITEM_ROW = 34
        TEMPLATE_ITEM_ROWS = LAST_TEMPLATE_ITEM_ROW - FIRST_ITEM_ROW + 1

        # Fill Header Fields
        ws["I4"] = invoice.get("invoice_no", " - ")
        
        invoice_date = invoice.get("invoice_date")
        if invoice_date:
            if isinstance(invoice_date, str):
                ws["I5"] = invoice_date
            else:
                ws["I5"] = invoice_date.strftime("%d-%b-%Y")
        else:
            ws["I5"] = " - "

        ws["A5"] = project_details.get("client_name", " - ")
        ws["C10"] = project_details.get("client_contact", " - ")
        ws["C13"] = project_details.get("project_no", " - ")
        ws["C15"] = project_details.get("project_name", " - ")
        ws["C14"] = project_details.get("location", " - ")

        lpo_reference = invoice.get("lpo_reference", " - ")
        ws["C18"] = lpo_reference
        
        lpo_date = invoice.get("lpo_date")
        if lpo_date:
            if isinstance(lpo_date, str):
                ws["I6"] = lpo_date
            else:
                ws["I6"] = lpo_date.strftime("%d-%b-%Y")
        else:
            ws["I6"] = " - "

        payment_display = "CASH / Immediate" if invoice_type == "CASH" else "CREDIT / 30 days"
        ws["I8"] = payment_display

        # Prepare Items for Excel
        excel_items = []
        
        for item in items:
            report_no = item.get("report_no", "-")
            
            if not report_no or report_no == "N/A" or "INV-" in report_no:
                report_no = "-"
            
            report_date = "-"
            if item.get("created_at"):
                if hasattr(item["created_at"], 'strftime'):
                    report_date = item["created_at"].strftime("%d-%b-%Y")
                else:
                    report_date = str(item["created_at"])
            
            excel_items.append({
                "report_no": report_no,
                "report_date": report_date,
                "description": item.get("description", "Test"),
                "test_standard": item.get("test_standard", "N/A"),
                "unit_rate": float(item.get("unit_rate", 0)),
                "quantity": int(item.get("quantity", 0)),
                "amount": float(item.get("amount", 0))
            })
        
        print(f"Prepared {len(excel_items)} items for Excel")
        for item in excel_items:
            print(f"  {item['report_no']} - {item['description']} - {item['quantity']} x {item['unit_rate']}")

        # Fill Rows
        num_items = len(excel_items)
        
        for row in range(FIRST_ITEM_ROW, LAST_TEMPLATE_ITEM_ROW + 1):
            for col in ['A', 'B', 'D', 'E', 'I', 'J', 'K']:
                ws[f"{col}{row}"].value = None
        
        if num_items > TEMPLATE_ITEM_ROWS:
            rows_needed = num_items - TEMPLATE_ITEM_ROWS
            ws.insert_rows(LAST_TEMPLATE_ITEM_ROW + 1, amount=rows_needed)
            last_item_row = LAST_TEMPLATE_ITEM_ROW + rows_needed
        else:
            last_item_row = FIRST_ITEM_ROW + num_items - 1 if num_items > 0 else FIRST_ITEM_ROW
        
        for index, item in enumerate(excel_items):
            if index < TEMPLATE_ITEM_ROWS:
                row = FIRST_ITEM_ROW + index
            else:
                extra_index = index - TEMPLATE_ITEM_ROWS
                row = LAST_TEMPLATE_ITEM_ROW + 1 + extra_index
            
            ws[f"A{row}"] = item["report_no"]
            ws[f"B{row}"] = item["report_date"]
            ws[f"D{row}"] = item["description"]
            ws[f"E{row}"] = item["test_standard"]
            ws[f"I{row}"] = item["quantity"]
            ws[f"J{row}"] = item["unit_rate"]
            ws[f"K{row}"] = item["amount"]

        # Update Totals
        ws["K35"] = round(float(invoice.get("subtotal", 0)), 2)
        ws["K36"] = round(float(invoice.get("vat", 0)), 2)
        ws["K37"] = round(float(invoice.get("total", 0)), 2)
        ws["B38"] = invoice.get("amount_in_words", " - ")

        # Save and Return
        output_dir = "generated_invoices"
        os.makedirs(output_dir, exist_ok=True)

        invoice_no = invoice.get('invoice_no', 'invoice')
        invoice_no_hyphen = invoice_no.replace('/', '-')
        output_path = os.path.join(output_dir, f"{invoice_no_hyphen}.xlsx")

        wb.save(output_path)

        project_name = project_details.get('project_name', '')
        import re
        def clean_filename(text):
            if not text:
                return ""
            text = re.sub(r'[\\/*?:"<>|]', '-', text)
            text = re.sub(r'\s+', '-', text)
            text = text.strip('- ')
            return text
        
        clean_project_name = clean_filename(project_name)
        download_filename = f"{invoice_no_hyphen}-{clean_project_name}.xlsx"
        
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


# =====================================================
# Project Latest Endpoint
# =====================================================
@router.get("/projects/latest/")
def get_latest_projects():
    """
    Get the latest 10 projects with complete info for invoice creation
    Includes walk-in projects
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            SELECT 
                p.project_id,
                p.project_name,
                p.project_no,
                COALESCE(c.name, p.walk_in_client) as client_name,
                p.location,
                q.quotation_no,
                p.is_walk_in,
                p.walk_in_client,
                p.status
            FROM projects p
            LEFT JOIN clients c ON p.client_id = c.client_id
            LEFT JOIN quotations q ON p.quotation_id = q.quotation_id
            WHERE p.status = 'ACTIVE'
            ORDER BY p.project_id DESC
            LIMIT 10
        """)
        
        projects = []
        for row in cur.fetchall():
            project_id, project_name, project_no, client_name, location, quotation_no, is_walk_in, walk_in_client, status = row
            
            # Use walk_in_client if available
            display_client = client_name or walk_in_client or "Walk-in Customer"
            display_label = f"{project_no} - {project_name} ({display_client})"
            
            projects.append({
                "project_id": project_id,
                "project_name": display_label,
                "project_no": project_no,
                "project_name_raw": project_name,
                "client_name": display_client,
                "location": location,
                "quotation_no": quotation_no,
                "is_walk_in": is_walk_in or False,
                "walk_in_client": walk_in_client,
                "status": status,
                "value": project_id,
                "label": display_label
            })
        
        return projects
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# =====================================================
# Create Invoice with Payment Method
# =====================================================
@router.post("/with-payment-method", response_model=InvoiceOut)
def create_invoice_with_payment_method(payload: InvoiceCreate):
    """
    Create a new invoice for a project with payment_method support.
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT p.project_id, p.project_no, p.project_name, p.location, p.lpo_no, p.lpo_date,
                   c.client_id, c.name, c.contact_person, c.email, c.address, c.phone,
                   q.quotation_no,
                   p.is_walk_in, p.walk_in_client
            FROM projects p
            LEFT JOIN clients c ON p.client_id = c.client_id
            LEFT JOIN quotations q ON p.quotation_id = q.quotation_id
            WHERE p.project_id = %s
        """, (payload.project_id,))
        project_data = cur.fetchone()
        if not project_data:
            raise HTTPException(status_code=404, detail="Project not found")

        invoiceable_items = get_project_quotation_items(payload.project_id, cur)
        if not invoiceable_items:
            raise HTTPException(status_code=400, detail="No test items available for invoicing")

        from collections import defaultdict
        grouped_items = defaultdict(lambda: {
            "description": "",
            "test_standard": "",
            "unit_rate": 0,
            "quantity": 0,
            "sample_ids": []
        })

        for item in invoiceable_items:
            item_id, description, test_standard, unit_rate, quantity, quotation_id = item
            key = (description, test_standard, unit_rate)
            grouped_items[key]["description"] = description
            grouped_items[key]["test_standard"] = test_standard
            grouped_items[key]["unit_rate"] = unit_rate
            grouped_items[key]["quantity"] += quantity

        final_items = []
        for (desc, std, rate), data in grouped_items.items():
            final_items.append((desc, std, rate, data["quantity"], []))

        invoice_no = generate_invoice_no(cur, payload.invoice_type)

        subtotal = 0.0
        for item in final_items:
            desc, std, unit_rate, quantity, sample_ids = item
            unit_rate_float = float(unit_rate) if isinstance(unit_rate, Decimal) else unit_rate
            subtotal += unit_rate_float * quantity

        vat = subtotal * 0.05
        total = subtotal + vat
        amount_words = number_to_words(total)

        lpo_reference = payload.lpo_reference or project_data[4]
        lpo_date = payload.lpo_date or project_data[5]
        
        if payload.payment_method == "CREDIT":
            payment_terms = payload.payment_terms or "30 days"
        else:
            payment_terms = "Immediate"

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
            "UNPAID"
        ))
        invoice_id = cur.fetchone()[0]

        for item in final_items:
            description, test_standard, unit_rate, quantity, sample_ids = item
            unit_rate_float = float(unit_rate) if isinstance(unit_rate, Decimal) else unit_rate
            amount = unit_rate_float * quantity

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
                None
            ))

        conn.commit()
        return get_invoice_complete(invoice_id, cur)

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# =====================================================
# List Invoices
# =====================================================
@router.get("/", response_model=List[InvoiceOut])
def list_invoices(limit: int = 100, offset: int = 0):
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            SELECT invoice_id FROM invoices 
            ORDER BY invoice_id DESC 
            LIMIT %s OFFSET %s
        """, (limit, offset))
        
        invoice_ids = [row[0] for row in cur.fetchall()]
        invoices = []
        
        for inv_id in invoice_ids:
            invoices.append(get_invoice_complete(inv_id, cur))
        
        return invoices
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# =====================================================
# Get Single Invoice
# =====================================================
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


# =====================================================
# Get Reports for Invoice (PROFORMA/TAX)
# =====================================================
@router.get("/projects/{project_id}/reports-for-invoice/{invoice_type}")
def get_reports_for_invoice(project_id: int, invoice_type: str):
    """
    Get all reports for a project that can be included in an invoice
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Check if this is a walk-in project
        cur.execute("SELECT is_walk_in FROM projects WHERE project_id = %s", (project_id,))
        is_walk_in_result = cur.fetchone()
        is_walk_in = is_walk_in_result[0] if is_walk_in_result else False
        
        # For walk-in projects, return empty reports (they'll use tests instead)
        if is_walk_in:
            return {
                "project_id": project_id,
                "invoice_type": invoice_type,
                "reports": [],
                "total_reports": 0,
                "is_walk_in": True,
                "message": "Walk-in projects use tests directly, not reports"
            }
        
        # Get all approved reports for this project
        cur.execute("""
            SELECT DISTINCT ON (r.report_no)
                r.report_id,
                r.report_no,
                r.covers_test_type as test_name,
                r.covers_samples,
                array_length(r.covers_samples, 1) as sample_count,
                r.created_at,
                r.status,
                COALESCE(qi.unit_rate, 0) as unit_rate,
                EXISTS (
                    SELECT 1 FROM invoice_report_links irl
                    WHERE irl.report_no = r.report_no 
                    AND irl.invoice_type = %s
                ) as already_invoiced
            FROM reports r
            LEFT JOIN quotation_items qi ON r.covers_test_type = qi.description
            JOIN samples s ON r.sample_id = s.sample_id
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            WHERE tr.project_id = %s 
            AND r.status = 'APPROVED'
            ORDER BY r.report_no, r.created_at DESC
        """, (invoice_type, project_id))
        
        reports = cur.fetchall()
        
        # Format response
        formatted_reports = []
        for report in reports:
            report_id, report_no, test_name, covers_samples, sample_count, created_at, status, unit_rate, already_invoiced = report
            
            formatted_reports.append({
                "report_id": report_id,
                "report_no": report_no,
                "test_name": test_name or "Report",
                "sample_count": sample_count or 1,
                "created_date": str(created_at) if created_at else None,
                "status": status,
                "unit_rate": float(unit_rate) if unit_rate else 0.0,
                "already_invoiced": already_invoiced
            })
        
        return {
            "project_id": project_id,
            "invoice_type": invoice_type,
            "reports": formatted_reports,
            "total_reports": len(formatted_reports),
            "is_walk_in": False
        }
        
    except Exception as e:
        print(f"ERROR in get_reports_for_invoice: {str(e)}")
        traceback.print_exc()
        raise HTTPException(500, f"Error fetching reports: {str(e)}")
    finally:
        cur.close()
        conn.close()


# =====================================================
# Get Reports for Delivery Note
# =====================================================
@router.get("/projects/{project_id}/reports-for-delivery")
def get_reports_for_delivery(project_id: int):
    """
    Get all reports for a project that can be included in a delivery note
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Check if this is a walk-in project
        cur.execute("SELECT is_walk_in FROM projects WHERE project_id = %s", (project_id,))
        is_walk_in_result = cur.fetchone()
        is_walk_in = is_walk_in_result[0] if is_walk_in_result else False
        
        # For walk-in projects, return empty reports
        if is_walk_in:
            return {
                "project_id": project_id,
                "reports": [],
                "total_reports": 0,
                "is_walk_in": True,
                "message": "Walk-in projects don't have reports"
            }
        
        # Get all approved reports for this project
        cur.execute("""
            SELECT DISTINCT ON (r.report_no)
                r.report_id,
                r.report_no,
                r.covers_test_type as test_name,
                r.covers_samples,
                array_length(r.covers_samples, 1) as sample_count,
                r.created_at,
                r.status,
                COALESCE(qi.unit_rate, 0) as unit_rate,
                EXISTS (
                    SELECT 1 FROM invoice_report_links irl
                    WHERE irl.report_no = r.report_no 
                    AND irl.invoice_type = 'DELIVERY_NOTE'
                ) as already_invoiced
            FROM reports r
            LEFT JOIN quotation_items qi ON r.covers_test_type = qi.description
            JOIN samples s ON r.sample_id = s.sample_id
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            WHERE tr.project_id = %s 
            AND r.status = 'APPROVED'
            ORDER BY r.report_no, r.created_at DESC
        """, (project_id,))
        
        reports = cur.fetchall()
        
        # Format response
        formatted_reports = []
        for report in reports:
            report_id, report_no, test_name, covers_samples, sample_count, created_at, status, unit_rate, already_invoiced = report
            
            formatted_reports.append({
                "report_id": report_id,
                "report_no": report_no,
                "test_name": test_name or "Report",
                "sample_count": sample_count or 1,
                "created_date": str(created_at) if created_at else None,
                "status": status,
                "unit_rate": float(unit_rate) if unit_rate else 0.0,
                "already_invoiced": already_invoiced
            })
        
        return {
            "project_id": project_id,
            "reports": formatted_reports,
            "total_reports": len(formatted_reports)
        }
        
    except Exception as e:
        print(f"ERROR in get_reports_for_delivery: {str(e)}")
        traceback.print_exc()
        raise HTTPException(500, f"Error fetching reports: {str(e)}")
    finally:
        cur.close()
        conn.close()


# =====================================================
# Generate Delivery Note Excel
# =====================================================
@router.post("/delivery-notes/generate-excel-template")
def generate_delivery_note_excel(payload: dict):
    """
    Generate a delivery note Excel file for selected reports
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        project_id = payload.get("project_id")
        selected_report_ids = payload.get("selected_report_ids", [])
        include_all_reports = payload.get("include_all_reports", True)
        
        # Check if this is a walk-in project
        cur.execute("SELECT is_walk_in FROM projects WHERE project_id = %s", (project_id,))
        is_walk_in_result = cur.fetchone()
        is_walk_in = is_walk_in_result[0] if is_walk_in_result else False
        
        if is_walk_in:
            raise HTTPException(400, "Delivery notes are not available for walk-in projects")
        
        # Get the selected reports
        if include_all_reports:
            cur.execute("""
                SELECT r.report_id, r.report_no, r.covers_test_type as test_name,
                       r.covers_samples, array_length(r.covers_samples, 1) as sample_count,
                       r.created_at
                FROM reports r
                JOIN samples s ON r.sample_id = s.sample_id
                JOIN test_requests tr ON s.request_id = tr.test_request_id
                WHERE tr.project_id = %s AND r.status = 'APPROVED'
                ORDER BY r.report_no
            """, (project_id,))
        else:
            # Use selected_report_ids
            if not selected_report_ids:
                raise HTTPException(400, "No reports selected")
            
            placeholders = ','.join(['%s'] * len(selected_report_ids))
            cur.execute(f"""
                SELECT r.report_id, r.report_no, r.covers_test_type as test_name,
                       r.covers_samples, array_length(r.covers_samples, 1) as sample_count,
                       r.created_at
                FROM reports r
                WHERE r.report_id IN ({placeholders})
                ORDER BY r.report_no
            """, selected_report_ids)
        
        reports = cur.fetchall()
        
        if not reports:
            raise HTTPException(404, "No reports found for delivery note")
        
        # Get project details
        cur.execute("""
            SELECT p.project_no, p.project_name, p.location,
                   COALESCE(c.name, p.walk_in_client) as client_name, 
                   c.address, c.contact_person, c.phone
            FROM projects p
            LEFT JOIN clients c ON p.client_id = c.client_id
            WHERE p.project_id = %s
        """, (project_id,))
        
        project = cur.fetchone()
        if not project:
            raise HTTPException(404, "Project not found")
        
        # Download template
        template_path = download_template_from_supabase("delivery_note")
        
        if not os.path.exists(template_path):
            raise HTTPException(404, "Delivery note template not found")
        
        wb = openpyxl.load_workbook(template_path)
        ws = wb.active
        
        # Fill project details (adjust cell references based on your template)
        ws["C5"] = project[0]  # project_no
        ws["C6"] = project[1]  # project_name
        ws["C7"] = project[2]  # location
        ws["C10"] = project[3]  # client_name
        ws["C11"] = project[4] or ""  # address
        ws["C12"] = project[5] or ""  # contact_person
        ws["C13"] = project[6] or ""  # phone
        
        # Fill report rows (adjust row numbers based on your template)
        start_row = 18
        for idx, report in enumerate(reports):
            row = start_row + idx
            report_id, report_no, test_name, covers_samples, sample_count, created_at = report
            
            ws[f"A{row}"] = idx + 1  # S.No
            ws[f"B{row}"] = report_no
            ws[f"C{row}"] = test_name or "Report"
            ws[f"D{row}"] = sample_count or 1
            ws[f"E{row}"] = str(created_at) if created_at else ""
        
        # Save file
        output_dir = "generated_delivery_notes"
        os.makedirs(output_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"Delivery_Note_{project[0]}_{timestamp}.xlsx"
        output_path = os.path.join(output_dir, filename)
        
        wb.save(output_path)
        
        return FileResponse(
            output_path,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        
    except Exception as e:
        print(f"ERROR generating delivery note: {str(e)}")
        traceback.print_exc()
        raise HTTPException(500, f"Error generating delivery note: {str(e)}")
    finally:
        cur.close()
        conn.close()