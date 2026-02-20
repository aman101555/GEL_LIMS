# samples_workflow.py - FIXED VERSION WITH CONSISTENT TEST ASSIGNMENT with excel template
# Each sample gets ONE test at creation and keeps it forever

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from db import get_connection
from utils import resource_path

from fastapi import UploadFile, File
import shutil
import os
import sys
import tempfile 
import secrets
from decimal import Decimal
from fastapi.responses import FileResponse

import openpyxl
from openpyxl import load_workbook

# Add these imports for Supabase template downloading
import requests
import tempfile

router = APIRouter(prefix="/samples-workflow", tags=["Samples Workflow"])

# Use system temp directory for worksheets in EXE mode
if hasattr(sys, "_MEIPASS"):
    WORKSHEET_TEMPLATES_DIR = os.path.join(tempfile.gettempdir(), "lab_app_worksheets")
else:
    WORKSHEET_TEMPLATES_DIR = resource_path("templates/worksheets")
os.makedirs(WORKSHEET_TEMPLATES_DIR, exist_ok=True)

# ---------------------------
# NEW: Function to download worksheet templates from Supabase
# ---------------------------
def download_worksheet_template_from_supabase(item_code: str):
    """
    Download worksheet template from Supabase storage.
    item_code: The test item code (e.g., "RH", "SPT")
    """
    try:
        # Check for worksheet templates in Supabase
        template_urls = [
            f"https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/worksheets/{item_code}.xlsx",
            f"https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/worksheets/{item_code}_Worksheet.xlsx",
            f"https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/worksheets/{item_code}.xls"
        ]
        
        template_found = False
        template_path = None
        
        for url in template_urls:
            try:
                print(f"DEBUG: Trying to download worksheet template from {url}")
                response = requests.get(url, timeout=30)
                if response.status_code == 200:
                    template_found = True
                    # Create a temporary file
                    with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as temp_file:
                        temp_file.write(response.content)
                        template_path = temp_file.name
                    print(f"DEBUG: Successfully downloaded {item_code} worksheet template from {url}")
                    break
            except requests.exceptions.RequestException as e:
                print(f"DEBUG: Failed to download from {url}: {e}")
                continue
        
        if not template_found:
            print(f"DEBUG: No worksheet template found for {item_code} in Supabase")
            # Check for generic/default worksheet template
            generic_urls = [
                "https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/worksheets/DEFAULT_Worksheet.xlsx",
                "https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates/worksheets/GENERIC_Worksheet.xlsx"
            ]
            
            for url in generic_urls:
                try:
                    response = requests.get(url, timeout=30)
                    if response.status_code == 200:
                        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as temp_file:
                            temp_file.write(response.content)
                            template_path = temp_file.name
                        print(f"DEBUG: Using generic worksheet template from {url}")
                        break
                except requests.exceptions.RequestException as e:
                    continue
        
        if not template_path:
            raise HTTPException(status_code=404, detail=f"No worksheet template found for {item_code} in Supabase storage")
        
        return template_path
        
    except Exception as e:
        print(f"ERROR in download_worksheet_template_from_supabase: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to download worksheet template: {str(e)}")

# ---------------------------
# Helpers - UPDATED
# ---------------------------

def generate_sample_no(cur, request_id: int, sequence_num: int):
    """
    Generate sample number in format: GS-{date}-{request_seq}-{sequence}
    
    Example request_no: GQ-121225-01
    Example sample_no: GS-121225-01-1, GS-121225-01-2, etc.
    """
    # Get the request number and created date
    cur.execute("""
        SELECT request_no, created_at 
        FROM test_requests 
        WHERE test_request_id = %s
    """, (request_id,))
    row = cur.fetchone()
    
    if not row:
        return f"GS-{datetime.now().strftime('%d%m%y')}-REQ{request_id:04d}-{sequence_num:02d}"
    
    request_no, created_at = row
    
    # Extract date part from request_no or use created_at
    date_part = ""
    
    # Check if request_no follows GQ-DDMMYY-XX pattern
    if len(request_no) >= 9 and '-' in request_no:
        parts = request_no.split('-')
        if len(parts) >= 2:
            # Extract the date part (e.g., "121225" from "GQ-121225-01")
            date_part = parts[1]
        else:
            # Use created date if pattern not found
            date_part = created_at.strftime("%d%m%y") if created_at else datetime.now().strftime("%d%m%y")
    else:
        # Use created date
        date_part = created_at.strftime("%d%m%y") if created_at else datetime.now().strftime("%d%m%y")
    
    # Extract the sequence number from request_no (e.g., "01" from "GQ-121225-01")
    request_seq = "01"  # default
    
    if len(request_no) >= 12 and request_no.count('-') >= 2:
        try:
            request_seq = request_no.split('-')[2]
        except (IndexError, AttributeError):
            request_seq = "01"
    
    # Format: GS-{date}-{request_seq}-{sample_sequence}
    return f"GS-{date_part}-{request_seq}-{sequence_num}"


def generate_worksheet_no(cur, sample_id: int):
    year = datetime.utcnow().year
    cur.execute("""
        SELECT COUNT(*) 
        FROM worksheets 
        WHERE EXTRACT(YEAR FROM created_at) = %s
    """, (year,))
    seq = cur.fetchone()[0] + 1
    return f"WKS-{year}-{sample_id:04d}-{seq:03d}"


def generate_barcode():
    return secrets.token_hex(8).upper()


# ---------------------------
# Pydantic models
# ---------------------------

class GenerateSamplesIn(BaseModel):
    collected_by: Optional[str] = None


class AcceptSampleIn(BaseModel):
    storage_location: Optional[str] = None
    note: Optional[str] = None


class RejectSampleIn(BaseModel):
    reason: Optional[str] = None
    inform_client: Optional[bool] = False


class GenerateWorksheetIn(BaseModel):
    technician: Optional[str] = None


# ---------------------------
# CRITICAL FIX: Helper to assign tests to samples consistently
# ---------------------------
def assign_tests_to_samples(cur, test_request_id: int):
    """
    Assign tests to samples in a consistent way:
    1. Get all tests with their quantities
    2. Distribute tests to samples based on quantity
    3. Return mapping: sample_sequence -> test_details
    
    Example: Test A (qty=2), Test B (qty=1), Test C (qty=1)
    Sample 1 -> Test A
    Sample 2 -> Test A
    Sample 3 -> Test B
    Sample 4 -> Test C
    """
    # Get all tests for this request with their quantities
    cur.execute("""
        SELECT tri.tri_id, tri.quotation_item_id, tri.quantity,
               qi.item_code, qi.description, qi.test_standard, qi.unit_rate
        FROM test_request_items tri
        JOIN quotation_items qi ON tri.quotation_item_id = qi.item_id
        WHERE tri.test_request_id = %s
        ORDER BY tri.tri_id
    """, (test_request_id,))
    
    tests = cur.fetchall()
    
    # Build test distribution
    test_distribution = []
    sample_counter = 1
    
    for tri_id, quotation_item_id, quantity, item_code, description, test_standard, unit_rate in tests:
        for _ in range(quantity):
            test_distribution.append({
                "sample_sequence": sample_counter,
                "tri_id": tri_id,
                "quotation_item_id": quotation_item_id,
                "item_code": item_code,
                "description": description,
                "test_standard": test_standard,
                "unit_rate": unit_rate
            })
            sample_counter += 1
    
    return test_distribution


# ---------------------------
# 1) Generate Samples from a Test Request - FIXED
# ---------------------------
@router.post("/generate-samples-by-request-no/{request_no}")
def generate_samples_by_request_no(request_no: str, payload: GenerateSamplesIn):
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Find test request by request_no
        cur.execute("""
            SELECT test_request_id, project_id 
            FROM test_requests 
            WHERE request_no = %s
        """, (request_no,))
        req = cur.fetchone()
        if not req:
            raise HTTPException(404, f"Test request with number '{request_no}' not found")

        test_request_id, project_id = req

        # Get consistent test distribution
        test_distribution = assign_tests_to_samples(cur, test_request_id)
        
        if not test_distribution:
            raise HTTPException(400, "This request has no items")

        created_samples = []
        test_assignments = []

        # Create samples with pre-assigned tests
        for test_info in test_distribution:
            sample_sequence = test_info["sample_sequence"]
            
            # Generate sample number
            sample_no = generate_sample_no(cur, test_request_id, sample_sequence)

            # Create sample WITH ASSIGNED TEST
            cur.execute("""
                INSERT INTO samples (
                    sample_no, 
                    request_id, 
                    collected_by, 
                    received_date, 
                    status,
                    assigned_tri_id,          -- Store which test request item this sample is for
                    assigned_quotation_item_id -- Store which quotation item this sample is for
                )
                VALUES (%s, %s, %s, NULL, 'PENDING', %s, %s)
                RETURNING sample_id
            """, (
                sample_no, 
                test_request_id, 
                payload.collected_by,
                test_info["tri_id"],
                test_info["quotation_item_id"]
            ))

            new_sample_id = cur.fetchone()[0]
            created_samples.append(new_sample_id)
            
            # Record assignment
            test_assignments.append({
                "sample_id": new_sample_id,
                "sample_no": sample_no,
                "assigned_test": test_info["item_code"],
                "test_name": test_info["description"],
                "tri_id": test_info["tri_id"],
                "quotation_item_id": test_info["quotation_item_id"],
                "sequence": sample_sequence
            })

        conn.commit()

        return {
            "message": f"Samples generated for request {request_no}",
            "count": len(created_samples),
            "test_request_id": test_request_id,
            "request_no": request_no,
            "sample_ids": created_samples,
            "test_distribution": test_assignments,
            "note": "Each sample has a permanently assigned test. This assignment will not change."
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))

    finally:
        cur.close()
        conn.close()


# ---------------------------
# 2) Accept sample - UPDATED to preserve assigned test
# ---------------------------
@router.post("/samples/{sample_id}/accept")
def accept_sample(sample_id: int, payload: AcceptSampleIn):
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Get sample with its assigned test
        cur.execute("""
            SELECT s.sample_id, s.sample_no, s.request_id, 
                   s.assigned_tri_id, s.assigned_quotation_item_id,
                   qi.item_code, qi.description
            FROM samples s
            LEFT JOIN quotation_items qi ON s.assigned_quotation_item_id = qi.item_id
            WHERE s.sample_id = %s
        """, (sample_id,))
        row = cur.fetchone()

        if not row:
            raise HTTPException(404, "Sample not found")

        sample_id, existing_sample_no, request_id, assigned_tri_id, assigned_quotation_item_id, item_code, test_name = row

        sample_no = existing_sample_no or generate_sample_no(cur, request_id, 1)  # Default sequence
        barcode = generate_barcode()

        cur.execute("""
            UPDATE samples
            SET sample_no = %s,
                barcode = %s,
                received_date = NOW(),
                status = 'ACCEPTED',
                storage_location = COALESCE(%s, storage_location)
            WHERE sample_id = %s
            RETURNING sample_no, barcode
        """, (sample_no, barcode, payload.storage_location, sample_id))

        updated = cur.fetchone()

        conn.commit()

        return {
            "message": "Sample accepted",
            "sample_id": sample_id,
            "sample_no": updated[0],
            "barcode": updated[1],
            "assigned_test": item_code,
            "test_name": test_name,
            "note": "Test assignment preserved from creation"
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))

    finally:
        cur.close()
        conn.close()


# ---------------------------
# 3) Reject sample
# ---------------------------
@router.post("/samples/{sample_id}/reject")
def reject_sample(sample_id: int, payload: RejectSampleIn):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT sample_id FROM samples WHERE sample_id = %s", (sample_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, "Sample not found")

        cur.execute("""
            UPDATE samples
            SET status = 'REJECTED',
                reason_rejected = %s,
                received_date = NOW()
            WHERE sample_id = %s
        """, (payload.reason, sample_id))

        conn.commit()
        return {"message": "Sample rejected"}

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))

    finally:
        cur.close()
        conn.close()


# ---------------------------
# 4) Generate Worksheet - FIXED to use stored test assignment
# ---------------------------
@router.post("/samples/{sample_id}/generate-worksheet")
def generate_worksheet(sample_id: int, payload: GenerateWorksheetIn):
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get sample with its PRE-ASSIGNED test
        cur.execute("""
            SELECT s.sample_id, s.sample_no, s.request_id,
                   s.assigned_tri_id, s.assigned_quotation_item_id,
                   qi.item_code, qi.description, qi.test_standard, qi.unit_rate
            FROM samples s
            LEFT JOIN quotation_items qi ON s.assigned_quotation_item_id = qi.item_id
            WHERE s.sample_id = %s
        """, (sample_id,))
        
        sample_row = cur.fetchone()
        if not sample_row:
            raise HTTPException(404, f"Sample {sample_id} not found")
        
        sample_id_db, sample_no, request_id, assigned_tri_id, assigned_quotation_item_id, item_code, description, test_standard, unit_rate = sample_row
        
        # Check if test is assigned
        if not assigned_quotation_item_id:
            raise HTTPException(400, f"Sample {sample_id} has no assigned test. Please regenerate samples.")
        
        # Check if worksheet already exists for this sample AND this specific test
        cur.execute("""
            SELECT worksheet_id, worksheet_no, status, created_at
            FROM worksheets 
            WHERE sample_id = %s AND quotation_item_id = %s
        """, (sample_id, assigned_quotation_item_id))
        
        existing = cur.fetchone()
        if existing:
            existing_id, existing_no, existing_status, existing_created = existing
            # Return download link instead of throwing error
            return {
                "message": f"Worksheet {existing_no} already exists for this sample/test combination.",
                "existing_worksheet": {
                    "worksheet_id": existing_id,
                    "worksheet_no": existing_no,
                    "status": existing_status,
                    "created_at": existing_created,
                    "download_url": f"/samples-workflow/worksheets/{existing_id}/download"
                },
                "download_available": True,
                "next_step": f"Download the existing worksheet using the link above."
            }
        
        # Generate worksheet number
        year = datetime.utcnow().year
        cur.execute("""
            SELECT COUNT(*) 
            FROM worksheets 
            WHERE EXTRACT(YEAR FROM created_at) = %s
        """, (year,))
        seq = cur.fetchone()[0] + 1
        worksheet_no = f"WKS-{year}-{sample_id:04d}-{seq:03d}"
        
        # Check if template exists in Supabase
        template_available = False
        template_path = None
        
        if item_code:
            try:
                # Try to download from Supabase
                template_path = download_worksheet_template_from_supabase(item_code)
                template_available = True
                print(f"✅ Found template in Supabase for {item_code}")
            except Exception as e:
                # Don't fail if template not found - just mark as unavailable
                template_available = False
                print(f"⚠️ Template not found in Supabase for {item_code}: {e}")
        
        # Create worksheet using the PRE-ASSIGNED test
        cur.execute("""
            INSERT INTO worksheets (
                worksheet_no, sample_id, quotation_item_id, test_name,
                standard, unit_rate, quantity, technician, status, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'GENERATED', NOW())
            RETURNING worksheet_id
        """, (
            worksheet_no, 
            sample_id, 
            assigned_quotation_item_id,  # Use the stored assignment
            description,
            test_standard, 
            float(unit_rate) if isinstance(unit_rate, Decimal) else unit_rate,
            1,  # Each worksheet is for 1 test on 1 sample
            payload.technician or "Lab Technician"
        ))
        
        worksheet_id_new = cur.fetchone()[0]
        
        conn.commit()
        
        return {
            "message": f"Worksheet generated for {description}",
            "worksheet_id": worksheet_id_new,
            "worksheet_no": worksheet_no,
            "sample_id": sample_id,
            "sample_no": sample_no,
            "test_name": description,
            "item_code": item_code,
            "test_standard": test_standard,
            "status": "GENERATED",
            "assigned_test": item_code,
            "template_available": template_available,
            "manual_upload_needed": not template_available,
            "next_step": "Download template, edit, and upload completed worksheet" if template_available else "Create worksheet manually and upload",
            "note": "Using test assigned during sample creation"
        }
        
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error generating worksheet: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ---------------------------
# 5) Get pending samples - FIXED to use stored assignment
# ---------------------------
@router.get("/pending-samples")
def get_pending_samples():
    """Get all samples with PENDING status - USING STORED ASSIGNMENT"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get samples with their stored test assignments
        cur.execute("""
            SELECT 
                s.sample_id,
                s.sample_no,
                s.request_id,
                s.collected_by,
                s.received_date,
                s.status,
                s.reason_rejected,
                s.barcode,
                s.storage_location,
                tr.request_no,
                s.assigned_quotation_item_id,
                qi.item_code,
                qi.description
            FROM samples s
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            LEFT JOIN quotation_items qi ON s.assigned_quotation_item_id = qi.item_id
            WHERE s.status = 'PENDING'
            ORDER BY s.sample_id DESC
        """)
        
        samples = cur.fetchall()
        
        result = []
        for sample in samples:
            (sample_id, sample_no, request_id, collected_by, received_date, status, 
             reason_rejected, barcode, storage_location, request_no, 
             assigned_quotation_item_id, item_code, description) = sample
            
            # Use the stored assignment, not recalculated
            assigned_test = item_code or "Not Assigned"
            test_name = description or "No test name"
            
            result.append({
                "sample_id": sample_id,
                "sample_no": sample_no,
                "request_id": request_id,
                "collected_by": collected_by,
                "received_date": received_date,
                "status": status,
                "reason_rejected": reason_rejected,
                "barcode": barcode,
                "storage_location": storage_location,
                "request_no": request_no,
                "assigned_test": assigned_test,
                "test_name": test_name,
                "assigned_from_storage": assigned_quotation_item_id is not None
            })
        
        return result
        
    except Exception as e:
        raise HTTPException(500, f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ---------------------------
# 6) Debug endpoint - FIXED to show stored assignment
# ---------------------------
@router.get("/debug/worksheet/{sample_id}")
def debug_worksheet_data(sample_id: int):
    """Debug endpoint to check sample and its stored test assignment"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get sample with its stored assignment
        cur.execute("""
            SELECT s.sample_id, s.sample_no, s.request_id,
                   s.assigned_tri_id, s.assigned_quotation_item_id,
                   tr.test_request_id, tr.request_no,
                   qi.item_code, qi.description, qi.test_standard, qi.unit_rate
            FROM samples s
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            LEFT JOIN quotation_items qi ON s.assigned_quotation_item_id = qi.item_id
            WHERE s.sample_id = %s
        """, (sample_id,))
        
        sample_info = cur.fetchone()
        if not sample_info:
            raise HTTPException(404, "Sample not found")
        
        (sample_id_db, sample_no, request_id, assigned_tri_id, assigned_quotation_item_id,
         test_request_id, request_no, item_code, description, test_standard, unit_rate) = sample_info
        
        # Get all tests for this request (for comparison)
        cur.execute("""
            SELECT qi.item_id, qi.item_code, qi.description, qi.test_standard, qi.unit_rate,
                   tri.quantity as requested_quantity, tri.tri_id
            FROM quotation_items qi
            JOIN test_request_items tri ON qi.item_id = tri.quotation_item_id
            WHERE tri.test_request_id = %s
            ORDER BY tri.tri_id
        """, (test_request_id,))
        
        all_tests = cur.fetchall()
        
        # Get existing worksheets for this sample
        cur.execute("""
            SELECT w.worksheet_id, w.worksheet_no, w.test_name, w.standard, w.created_at, qi.item_code
            FROM worksheets w
            LEFT JOIN quotation_items qi ON w.quotation_item_id = qi.item_id
            WHERE w.sample_id = %s
        """, (sample_id,))
        
        existing_worksheets = cur.fetchall()
        
        return {
            "sample_info": {
                "sample_id": sample_id_db,
                "sample_no": sample_no,
                "request_id": request_id,
                "test_request_id": test_request_id,
                "request_no": request_no,
                "stored_assignment": {
                    "assigned_tri_id": assigned_tri_id,
                    "assigned_quotation_item_id": assigned_quotation_item_id,
                    "item_code": item_code,
                    "description": description,
                    "test_standard": test_standard,
                    "unit_rate": float(unit_rate) if isinstance(unit_rate, Decimal) else unit_rate
                }
            },
            "all_tests_in_request": [
                {
                    "item_id": item[0],
                    "item_code": item[1],
                    "description": item[2],
                    "test_standard": item[3],
                    "unit_rate": float(item[4]) if isinstance(item[4], Decimal) else item[4],
                    "requested_quantity": item[5],
                    "tri_id": item[6]
                }
                for item in all_tests
            ],
            "existing_worksheets": [
                {
                    "worksheet_id": ws[0],
                    "worksheet_no": ws[1],
                    "test_name": ws[2],
                    "standard": ws[3],
                    "created_at": ws[4],
                    "item_code": ws[5]
                }
                for ws in existing_worksheets
            ]
        }
        
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ---------------------------
# 7) Database migration SQL (Run this once to add the new columns)
# ---------------------------
"""
-- Run this SQL in your database to add the columns for storing test assignments:
ALTER TABLE samples 
ADD COLUMN IF NOT EXISTS assigned_tri_id INTEGER REFERENCES test_request_items(tri_id),
ADD COLUMN IF NOT EXISTS assigned_quotation_item_id INTEGER REFERENCES quotation_items(item_id);

-- Create an index for faster lookups:
CREATE INDEX IF NOT EXISTS idx_samples_assigned_test ON samples(assigned_quotation_item_id);
CREATE INDEX IF NOT EXISTS idx_samples_assigned_tri ON samples(assigned_tri_id);
"""


# ---------------------------
# Remaining endpoints (unchanged but will use stored assignment)
# ---------------------------

@router.post("/worksheets/{worksheet_id}/upload")
async def upload_worksheet_file(
    worksheet_id: int,
    worksheet_file: UploadFile = File(...)
):
    """Upload a custom worksheet file"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Check if worksheet exists
        cur.execute("""
            SELECT w.worksheet_id, w.test_name, qi.item_code
            FROM worksheets w
            LEFT JOIN quotation_items qi ON w.quotation_item_id = qi.item_id
            WHERE w.worksheet_id = %s
        """, (worksheet_id,))
        
        worksheet_info = cur.fetchone()
        if not worksheet_info:
            raise HTTPException(404, f"Worksheet {worksheet_id} not found")
        
        worksheet_id_db, test_name, item_code = worksheet_info
        
        # Determine file extension
        file_ext = os.path.splitext(worksheet_file.filename)[1]
        if not file_ext:
            file_ext = ".pdf"  # default to pdf
        
        # Create filename
        filename = f"{item_code or f'worksheet_{worksheet_id}'}{file_ext}"
        file_path = os.path.join(WORKSHEET_TEMPLATES_DIR, filename)
        
        # Save the file
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(worksheet_file.file, buffer)
        
        # Update worksheet record
        cur.execute("""
            UPDATE worksheets 
            SET template_path = %s, updated_at = NOW()
            WHERE worksheet_id = %s
        """, (file_path, worksheet_id))
        
        conn.commit()
        
        return {
            "message": "Worksheet file uploaded successfully",
            "worksheet_id": worksheet_id,
            "filename": filename,
            "file_path": file_path,
            "file_size": os.path.getsize(file_path),
            "test_name": test_name,
            "item_code": item_code
        }
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error uploading worksheet: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.get("/worksheets/{worksheet_id}/download")
def download_worksheet(worksheet_id: int):
    """Download the worksheet file"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get worksheet info with sample and test details
        cur.execute("""
            SELECT w.worksheet_no, w.template_path, w.test_name, s.sample_no, w.status, qi.item_code
            FROM worksheets w
            JOIN samples s ON w.sample_id = s.sample_id
            LEFT JOIN quotation_items qi ON w.quotation_item_id = qi.item_id
            WHERE w.worksheet_id = %s
        """, (worksheet_id,))
        
        worksheet = cur.fetchone()
        if not worksheet:
            raise HTTPException(404, "Worksheet not found")
        
        worksheet_no, template_path, test_name, sample_no, status, item_code = worksheet
        
        # Check if file exists via template_path
        if template_path and os.path.exists(template_path):
            # Get original filename from template_path
            original_filename = os.path.basename(template_path)
            # Create a nice download filename
            download_filename = f"{sample_no}_{worksheet_no}_{test_name.replace(' ', '_')}{os.path.splitext(original_filename)[1]}"
            
            return FileResponse(
                path=template_path,
                filename=download_filename,
                media_type='application/octet-stream'
            )
        
        # No file found
        return {
            "has_file": False,
            "message": "No worksheet file uploaded yet. Please upload the completed worksheet first.",
            "worksheet_id": worksheet_id,
            "worksheet_no": worksheet_no,
            "sample_no": sample_no,
            "test_name": test_name,
            "item_code": item_code,
            "status": status
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Download error: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.get("/recent-samples")
def get_recent_samples(limit: int = 5):
    """Get most recent samples using stored test assignment"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            SELECT 
                s.sample_id,
                s.sample_no,
                s.status,
                s.barcode,
                tr.request_no,
                s.request_id,
                qi.item_code,
                qi.description
            FROM samples s
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            LEFT JOIN quotation_items qi ON s.assigned_quotation_item_id = qi.item_id
            WHERE s.status IN ('PENDING', 'ACCEPTED')
            ORDER BY s.sample_id DESC
            LIMIT %s
        """, (limit,))
        
        rows = cur.fetchall()
        
        result = []
        for row in rows:
            sample_id, sample_no, status, barcode, request_no, request_id, item_code, description = row
            
            result.append({
                "sample_id": sample_id,
                "sample_no": sample_no,
                "status": status,
                "barcode": barcode,
                "request_no": request_no,
                "assigned_test": description or "Test not assigned",
                "item_code": item_code or "N/A",
                "note": "Using stored assignment" if item_code else "No test assigned"
            })
        
        return result
        
    except Exception as e:
        raise HTTPException(500, f"Error fetching recent samples: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.get("/samples/{sample_id}/download-template")
def download_worksheet_template(sample_id: int):
    """Download the worksheet template for this sample - USING STORED ASSIGNMENT"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get sample with its STORED test assignment
        cur.execute("""
            SELECT s.sample_id, s.sample_no, s.assigned_quotation_item_id,
                   qi.item_code, qi.description
            FROM samples s
            LEFT JOIN quotation_items qi ON s.assigned_quotation_item_id = qi.item_id
            WHERE s.sample_id = %s
        """, (sample_id,))
        
        sample_row = cur.fetchone()
        if not sample_row:
            raise HTTPException(404, f"Sample {sample_id} not found")
        
        sample_id_db, sample_no, assigned_quotation_item_id, item_code, test_name = sample_row
        
        if not assigned_quotation_item_id or not item_code:
            raise HTTPException(400, f"Sample {sample_id} has no assigned test. Please regenerate samples.")
        
        print(f"🔍 Sample {sample_id} has stored test: {item_code}")

        # Try to download template from Supabase
        template_path = None
        
        try:
            template_path = download_worksheet_template_from_supabase(item_code)
            print(f"✅ Downloaded template from Supabase: {item_code}")
        except Exception as e:
            print(f"⚠️ Could not download template from Supabase: {e}")
            # Try uppercase/lowercase variations
            for variation in [item_code, item_code.upper(), item_code.lower()]:
                try:
                    template_path = download_worksheet_template_from_supabase(variation)
                    print(f"✅ Found template (variation): {variation}")
                    break
                except Exception:
                    continue

        # If still no template, return a JSON response instead of raising an error
        if not template_path:
            print(f"❌ No template found for {item_code}")
            return {
                "has_template": False,
                "message": f"No standard template found for {test_name} ({item_code}). Please create the worksheet manually and upload it.",
                "item_code": item_code,
                "test_name": test_name,
                "sample_no": sample_no,
                "next_step": "Create worksheet manually and upload using the upload section below."
            }
        
        # Return the template file
        filename = os.path.basename(template_path)
        print(f"📤 Returning file: {filename}")
        
        return FileResponse(
            path=template_path,
            filename=filename,
            media_type='application/octet-stream'
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error downloading template: {str(e)}")
        raise HTTPException(500, f"Error downloading template: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.get("/all-samples")
def get_all_samples():
    """Get ALL samples regardless of status"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get all samples with their stored test assignments
        cur.execute("""
            SELECT 
                s.sample_id,
                s.sample_no,
                s.request_id,
                s.collected_by,
                s.received_date,
                s.status,
                s.reason_rejected,
                s.barcode,
                s.storage_location,
                tr.request_no,
                s.assigned_quotation_item_id,
                qi.item_code,
                qi.description
            FROM samples s
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            LEFT JOIN quotation_items qi ON s.assigned_quotation_item_id = qi.item_id
            ORDER BY s.sample_id DESC
        """)
        
        samples = cur.fetchall()
        
        result = []
        for sample in samples:
            (sample_id, sample_no, request_id, collected_by, received_date, status, 
             reason_rejected, barcode, storage_location, request_no, 
             assigned_quotation_item_id, item_code, description) = sample
            
            result.append({
                "sample_id": sample_id,
                "sample_no": sample_no,
                "request_id": request_id,
                "collected_by": collected_by,
                "received_date": received_date,
                "status": status,
                "reason_rejected": reason_rejected,
                "barcode": barcode,
                "storage_location": storage_location,
                "request_no": request_no,
                "assigned_test": item_code or "Not Assigned",
                "test_name": description or "No test name",
                "assigned_from_storage": assigned_quotation_item_id is not None
            })
        
        return result
        
    except Exception as e:
        raise HTTPException(500, f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()


def get_worksheet_data_for_sample(sample_id: int):
    """
    Get all data needed to fill a worksheet for a specific sample
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            -- Get sample details with request and project info
            SELECT 
                -- Sample details
                s.sample_id,
                s.sample_no,
                s.collected_by,
                TO_CHAR(s.received_date, 'DD-MM-YYYY') as received_date_formatted,
                s.received_date,
                s.barcode,
                s.storage_location,
                
                -- Test request details
                tr.test_request_id,
                tr.request_no,
                tr.requested_by,
                
                -- Project details
                p.project_id,
                p.project_no,
                p.project_name,
                p.location,
                p.lpo_no,
                p.lpo_date,
                
                -- Test assignment details (from sample)
                s.assigned_item_code,
                s.assigned_test_name,
                
                -- Worksheet details (if exists)
                w.worksheet_id,
                w.worksheet_no,
                w.technician,
                w.status as worksheet_status,
                w.created_at as worksheet_created
                
            FROM samples s
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            JOIN projects p ON tr.project_id = p.project_id
            LEFT JOIN worksheets w ON w.sample_id = s.sample_id 
                AND w.quotation_item_id = s.assigned_quotation_item_id
            WHERE s.sample_id = %s
        """, (sample_id,))
        
        sample_data = cur.fetchone()
        
        if not sample_data:
            raise HTTPException(404, f"Sample {sample_id} not found")
        
        # Now get ALL samples for this request to fill the worksheet table
        request_id = sample_data[7]  # test_request_id
        
        cur.execute("""
            SELECT 
                s.sample_id,
                s.sample_no,
                s.assigned_item_code,
                s.assigned_test_name,
                ROW_NUMBER() OVER (ORDER BY s.sample_id) as sequence_num
            FROM samples s
            WHERE s.request_id = %s
            ORDER BY s.sample_id
        """, (request_id,))
        
        all_samples = cur.fetchall()
        
        return {
            "sample": {
                "sample_id": sample_data[0],
                "sample_no": sample_data[1],
                "collected_by": sample_data[2],
                "received_date_formatted": sample_data[3],
                "received_date": sample_data[4],
                "barcode": sample_data[5],
                "storage_location": sample_data[6]
            },
            "test_request": {
                "test_request_id": sample_data[7],
                "request_no": sample_data[8],
                "requested_by": sample_data[9]
            },
            "project": {
                "project_id": sample_data[10],
                "project_no": sample_data[11],
                "project_name": sample_data[12],
                "location": sample_data[13],
                "lpo_no": sample_data[14],
                "lpo_date": sample_data[15]
            },
            "assigned_test": {
                "item_code": sample_data[16],
                "test_name": sample_data[17]
            },
            "worksheet": {
                "worksheet_id": sample_data[18],
                "worksheet_no": sample_data[19],
                "technician": sample_data[20],
                "status": sample_data[21],
                "created_at": sample_data[22]
            },
            "all_samples": [
                {
                    "sample_id": s[0],
                    "sample_no": s[1],
                    "item_code": s[2],
                    "test_name": s[3],
                    "sequence": s[4]  # This is the SI.No
                }
                for s in all_samples
            ]
        }
        
    except Exception as e:
        raise HTTPException(500, f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()


def populate_worksheet_template(template_path: str, worksheet_id: int, output_path: str):
    """
    Populate an Excel worksheet template with data from database
    worksheet_id: The ID of the worksheet (not sample_id)
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get worksheet info to find the sample
        cur.execute("""
            SELECT w.sample_id, s.sample_no, s.request_id
            FROM worksheets w
            JOIN samples s ON w.sample_id = s.sample_id
            WHERE w.worksheet_id = %s
        """, (worksheet_id,))
        
        worksheet_data = cur.fetchone()
        if not worksheet_data:
            raise HTTPException(404, f"Worksheet {worksheet_id} not found")
        
        sample_id, sample_no, request_id = worksheet_data
        
        # Get the sample's assigned test first
        cur.execute("""
            SELECT s.assigned_quotation_item_id, qi.item_code, qi.description
            FROM samples s
            LEFT JOIN quotation_items qi ON s.assigned_quotation_item_id = qi.item_id
            WHERE s.sample_id = %s
        """, (sample_id,))
        
        sample_info = cur.fetchone()
        if not sample_info:
            raise HTTPException(404, f"Sample {sample_id} not found")
        
        assigned_quotation_item_id, assigned_item_code, assigned_test_name = sample_info
        
        # Now get ONLY samples with the SAME assigned test
        cur.execute("""
            SELECT 
                s.sample_id,
                s.sample_no,
                qi.item_code,
                qi.description,
                ROW_NUMBER() OVER (ORDER BY s.sample_id) as sequence_num
            FROM samples s
            LEFT JOIN quotation_items qi ON s.assigned_quotation_item_id = qi.item_id
            WHERE s.request_id = %s 
              AND s.assigned_quotation_item_id = %s  -- Filter by the same test!
            ORDER BY s.sample_id
        """, (request_id, assigned_quotation_item_id))
        
        test_samples = cur.fetchall()
        
        # Get sample details with request and project info
        cur.execute("""
            SELECT 
                -- Sample details
                s.sample_id,
                s.sample_no,
                s.collected_by,
                TO_CHAR(s.received_date, 'DD-MM-YYYY') as received_date_formatted,
                s.received_date,
                s.barcode,
                s.storage_location,
                
                -- Test request details
                tr.test_request_id,
                tr.request_no,
                tr.requested_by,
                
                -- Project details
                p.project_id,
                p.project_no,
                p.project_name,
                p.location,
                p.lpo_no,
                p.lpo_date,
                
                -- Test assignment details (from sample)
                qi.item_code,
                qi.description,
                
                -- Worksheet details (if exists)
                w.worksheet_id,
                w.worksheet_no,
                w.technician,
                w.status as worksheet_status,
                w.created_at as worksheet_created
                
            FROM samples s
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            JOIN projects p ON tr.project_id = p.project_id
            LEFT JOIN quotation_items qi ON s.assigned_quotation_item_id = qi.item_id
            LEFT JOIN worksheets w ON w.sample_id = s.sample_id 
                AND w.quotation_item_id = s.assigned_quotation_item_id
            WHERE s.sample_id = %s
        """, (sample_id,))
        
        sample_data = cur.fetchone()
        
        if not sample_data:
            raise HTTPException(404, f"Sample {sample_id} not found")
        
        # Prepare the data structure
        data = {
            "sample": {
                "sample_id": sample_data[0],
                "sample_no": sample_data[1],
                "collected_by": sample_data[2],
                "received_date_formatted": sample_data[3],
                "received_date": sample_data[4],
                "barcode": sample_data[5],
                "storage_location": sample_data[6]
            },
            "test_request": {
                "test_request_id": sample_data[7],
                "request_no": sample_data[8],
                "requested_by": sample_data[9]
            },
            "project": {
                "project_id": sample_data[10],
                "project_no": sample_data[11],
                "project_name": sample_data[12],
                "location": sample_data[13],
                "lpo_no": sample_data[14],
                "lpo_date": sample_data[15]
            },
            "assigned_test": {
                "item_code": sample_data[16],
                "test_name": sample_data[17]
            },
            "worksheet": {
                "worksheet_id": sample_data[18],
                "worksheet_no": sample_data[19],
                "technician": sample_data[20],
                "status": sample_data[21],
                "created_at": sample_data[22]
            },
            "test_samples": [  # Only samples with the same test
                {
                    "sample_id": s[0],
                    "sample_no": s[1],
                    "item_code": s[2],
                    "test_name": s[3],
                    "sequence": s[4]  # This is the SI.No
                }
                for s in test_samples
            ]
        }
        
        # Load the template
        workbook = load_workbook(template_path)
        sheet = workbook.active
        
        # Fill the fixed cells
        # D7 = request_no
        sheet['D7'] = data['test_request']['request_no']
        
        # D8 = project_no
        sheet['D8'] = data['project']['project_no']
        
        # E39 = collected_by
        sheet['E39'] = data['sample']['collected_by']
        
        # J9 = received_date (formatted)
        sheet['J9'] = data['sample']['received_date_formatted']
        
        # Fill the sample table (starting from F14)
        start_col = 6  # Column F = 6
        start_row = 14  # Row 14
        
        for idx, sample in enumerate(data['test_samples']):
            # Calculate column (F=6, G=7, H=8, etc.)
            col = start_col + idx
            
            # Get column letter
            col_letter = openpyxl.utils.get_column_letter(col)
            
            # Fill sample_no in row 14 (F14, G14, H14...)
            sheet[f'{col_letter}14'] = sample['sample_no']
            
            # Fill SI.No in row 15 (F15, G15, H15...)
            sheet[f'{col_letter}15'] = sample['sequence']
        
        # Save the populated worksheet
        workbook.save(output_path)
        
        return {
            "output_path": output_path,
            "filled_cells": {
                "D7": data['test_request']['request_no'],
                "D8": data['project']['project_no'],
                "E39": data['sample']['collected_by'],
                "J9": data['sample']['received_date_formatted'],
                "sample_count": len(data['test_samples']),  # Updated to test_samples
            }
        }
    
    except Exception as e:
        raise HTTPException(500, f"Error populating worksheet: {str(e)}")
    finally:
        cur.close()
        conn.close()

@router.get("/worksheets/{worksheet_id}/download-filled-worksheet")
def download_filled_worksheet(worksheet_id: int):
    """
    Download a worksheet template populated with data
    """
    try:
        # 1. Get the worksheet info
        conn = get_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT w.worksheet_id, w.worksheet_no, w.quotation_item_id,
                   qi.item_code, s.sample_no, s.sample_id
            FROM worksheets w
            JOIN samples s ON w.sample_id = s.sample_id
            LEFT JOIN quotation_items qi ON w.quotation_item_id = qi.item_id
            WHERE w.worksheet_id = %s
            LIMIT 1
        """, (worksheet_id,))
        
        worksheet_info = cur.fetchone()
        
        if not worksheet_info:
            raise HTTPException(404, f"No worksheet found with ID {worksheet_id}")
        
        worksheet_id_db, worksheet_no, quotation_item_id, item_code, sample_no, sample_id = worksheet_info
        
        # 2. Download the template from Supabase
        try:
            template_path = download_worksheet_template_from_supabase(item_code)
        except HTTPException as e:
            if e.status_code == 404:
                # Try uppercase/lowercase variations
                for variation in [item_code, item_code.upper(), item_code.lower()]:
                    try:
                        template_path = download_worksheet_template_from_supabase(variation)
                        break
                    except HTTPException:
                        continue
                else:
                    # If all variations fail
                    raise HTTPException(404, f"No template found for {item_code}")
            else:
                raise
        
        # 3. Create output filename and path
        output_filename = f"{sample_no}_{worksheet_no}_FILLED.xlsx"
        
        # Create a temporary directory if it doesn't exist
        temp_dir = "temp_filled_worksheets"
        os.makedirs(temp_dir, exist_ok=True)
        
        # Use os.path.join for cross-platform path creation
        output_path = os.path.join(temp_dir, output_filename)
        
        # 4. Populate the template
        result = populate_worksheet_template(template_path, worksheet_id_db, output_path)
        
        # 5. Return the file
        return FileResponse(
            path=output_path,
            filename=output_filename,
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error generating filled worksheet: {str(e)}")
    finally:
        cur.close()
        conn.close()