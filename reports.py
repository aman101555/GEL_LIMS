# reports.py - UPDATED VERSION FOR COMBINED REPORTS PER TEST TYPE EXCEL TEMPLATE SUPA
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from typing import Optional, List, Dict, Any
from datetime import datetime
from db import get_connection
import os
import shutil
import secrets
import sys

import requests
from utils import resource_path


import openpyxl
from openpyxl.styles import Font, Alignment
import tempfile

router = APIRouter(tags=["Reports"])

# Use system temp directory for uploads in EXE mode
if hasattr(sys, "_MEIPASS"):
    REPORTS_UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "lab_app_uploads", "reports")
else:
    REPORTS_UPLOAD_DIR = "uploads/reports"
os.makedirs(REPORTS_UPLOAD_DIR, exist_ok=True)


SUPABASE_STORAGE_URL = "https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates"
# ---------------------------
# NEW: Helper to get test type distribution for a request
# ---------------------------
# ---------------------------
# FIXED: Helper to get test type distribution for a request

# reports.py

# Add this endpoint to allow the SearchBar to fetch all reports
@router.get("/")
def get_all_reports():
    """Get all reports for the search interface"""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT r.report_id, r.report_no, r.sample_id, r.status, 
                   r.created_at, r.uploaded_by, s.sample_no
            FROM reports r
            LEFT JOIN samples s ON r.sample_id = s.sample_id
            ORDER BY r.created_at DESC
        """)
        reports = cur.fetchall()
        return [
            {
                "report_id": r[0],
                "report_no": r[1],
                "sample_id": r[2],
                "status": r[3],
                "created_at": r[4],
                "uploaded_by": r[5],
                "sample_no": r[6]
            } for r in reports
        ]
    finally:
        cur.close()
        conn.close()


# ---------------------------
# UPDATED: Helper function to get template from Supabase
# ---------------------------
def get_template_from_supabase(item_code: str, test_name: str):
    """Get template from Supabase storage"""
    possible_filenames = [
        f"{item_code}_Report.xlsx",
        f"{item_code}_Report.docx", 
        f"{item_code}_Report.pdf",
        f"{item_code}.xlsx",
        f"{item_code}.docx",
        f"{test_name.replace(' ', '_')}_Report.xlsx",
        f"{test_name.replace(' ', '_')}_Report.docx"
    ]
    
    for filename in possible_filenames:
        template_url = f"{SUPABASE_STORAGE_URL}/reports/{filename}"
        
        try:
            # Check if the file exists by making a HEAD request
            response = requests.head(template_url)
            if response.status_code == 200:
                return template_url, filename.split('.')[-1]
        except Exception:
            continue
    
    return None, None



# ---------------------------
def get_test_distribution_for_request(request_id: int, cur):
    """Get how samples are distributed across test types"""
    cur.execute("""
        -- Get all test items for this request with their quantities
        SELECT tri.tri_id, tri.quotation_item_id, tri.quantity,
               qi.item_code, qi.description
        FROM test_request_items tri
        JOIN quotation_items qi ON tri.quotation_item_id = qi.item_id
        WHERE tri.test_request_id = %s
        ORDER BY tri.tri_id
    """, (request_id,))
    
    test_items = cur.fetchall()
    
    # Get all samples for this request
    cur.execute("""
        SELECT sample_id, sample_no
        FROM samples 
        WHERE request_id = %s
        ORDER BY sample_id
    """, (request_id,))
    
    samples = cur.fetchall()
    
    # Map each sample to its test type
    sample_to_test_map = {}
    test_distribution = {}
    
    sample_index = 0
    for test_item in test_items:
        # Safely unpack with default values
        tri_id = test_item[0] if len(test_item) > 0 else None
        item_id = test_item[1] if len(test_item) > 1 else None
        quantity = test_item[2] if len(test_item) > 2 else 1
        item_code = test_item[3] if len(test_item) > 3 else "UNKNOWN"
        description = test_item[4] if len(test_item) > 4 else "Unknown Test"
        
        for i in range(quantity):
            if sample_index < len(samples):
                sample = samples[sample_index]
                sample_id = sample[0] if len(sample) > 0 else None
                sample_no = sample[1] if len(sample) > 1 else f"GS-UNKNOWN-{sample_index}"
                
                if sample_id:  # Only map if we have a valid sample_id
                    sample_to_test_map[sample_id] = {
                        "tri_id": tri_id,
                        "item_id": item_id,
                        "item_code": item_code,
                        "test_name": description,
                        "quantity": quantity
                    }
                    
                    # Track test distribution
                    if item_code not in test_distribution:
                        test_distribution[item_code] = {
                            "test_name": description,
                            "item_code": item_code,
                            "samples": [],
                            "total_quantity": quantity,
                            "sample_count": 0
                        }
                    
                    test_distribution[item_code]["samples"].append({
                        "sample_id": sample_id,
                        "sample_no": sample_no
                    })
                    test_distribution[item_code]["sample_count"] += 1
                
                sample_index += 1
    
    return sample_to_test_map, test_distribution

# ---------------------------
# UPDATED: Report number generator with better uniqueness
# ---------------------------
# ---------------------------
# FIXED: Report number generator - simplified version
# ---------------------------
def generate_report_no(cur):
    """Generate unique report number: GR - DDMMYY - XXX"""
    today = datetime.now()
    date_str = today.strftime("%d%m%y")  # DDMMYY format
    
    try:
        # Simple approach: count reports created today
        cur.execute("""
            SELECT COUNT(*) 
            FROM reports 
            WHERE DATE(created_at) = CURRENT_DATE
        """)
        count = cur.fetchone()[0]
        
        # Generate sequence number
        seq_num = count + 1
        report_seq = f"{seq_num:03d}"
        
        report_no = f"GR - {date_str} - {report_seq}"
        
        # Double-check for uniqueness (in case of race condition)
        cur.execute("SELECT COUNT(*) FROM reports WHERE report_no = %s", (report_no,))
        if cur.fetchone()[0] > 0:
            # Add timestamp if duplicate
            timestamp = int(datetime.now().timestamp() % 1000)
            report_no = f"GR - {date_str} - {report_seq}-{timestamp}"
        
        return report_no
        
    except Exception as e:
        # Fallback: use timestamp-based number
        timestamp = int(datetime.now().timestamp() % 1000000)
        return f"GR - {date_str} - {timestamp:06d}"

# ---------------------------
# 1. Search Sample by Sample No (GS format) - UPDATED
# ---------------------------
@router.get("/samples/search")
def search_sample_by_no(sample_no: str):
    """Search for sample by sample number (GS format)"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            SELECT s.sample_id, s.sample_no, s.request_id, s.status,
                   tr.request_no
            FROM samples s
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            WHERE LOWER(s.sample_no) LIKE LOWER(%s)
            ORDER BY s.created_at DESC
            LIMIT 10
        """, (f"%{sample_no}%",))
        
        samples = cur.fetchall()
        if not samples:
            raise HTTPException(404, "No samples found with that sample number")
        
        result = []
        for sample in samples:
            sample_id, sample_no, request_id, status, request_no = sample
            
            # Get test distribution for this request
            sample_to_test_map, test_distribution = get_test_distribution_for_request(request_id, cur)
            
            # Get which test this sample belongs to
            test_info = sample_to_test_map.get(sample_id, {})
            
            result.append({
                "sample_id": sample_id,
                "sample_no": sample_no,
                "request_id": request_id,
                "status": status,
                "request_no": request_no,
                "test_name": test_info.get("test_name", "Unknown"),
                "item_code": test_info.get("item_code", "Unknown")
            })
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error searching samples: {str(e)}")
    finally:
        cur.close()
        conn.close()

# ---------------------------
# 2. Get Latest 10 Sample Numbers - UPDATED
# ---------------------------
@router.get("/samples/latest")
def get_latest_samples():
    """Get latest 10 sample numbers for dropdown with test info"""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT s.sample_id, s.sample_no, s.request_id, s.status,
                   tr.request_no
            FROM samples s
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            WHERE s.sample_no LIKE 'GS%'
            ORDER BY s.sample_id DESC
            LIMIT 10
        """)
        
        samples = cur.fetchall()
        result = []
        
        for sample in samples:
            sample_id, sample_no, request_id, status, request_no = sample
            
            # Get test distribution
            sample_to_test_map, _ = get_test_distribution_for_request(request_id, cur)
            test_info = sample_to_test_map.get(sample_id, {})
            
            result.append({
                "sample_id": sample_id,
                "sample_no": sample_no,
                "request_no": request_no,
                "test_name": test_info.get("test_name", "Unknown"),
                "item_code": test_info.get("item_code", "Unknown"),
                "status": status
            })
        
        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
    finally:
        cur.close()
        if conn is not None:
            conn.close()

# ---------------------------
# 3. Get Sample Test Info & Check for Existing Reports - COMPLETELY REWRITTEN
# ---------------------------

@router.get("/samples/by-number/{sample_no}")
def get_sample_template_info_by_no(sample_no: str):
    """
    Check which test this sample is for,
    if template exists, and if report already created
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        # First, get the sample by sample_no
        cur.execute("""
            SELECT sample_id, request_id
            FROM samples
            WHERE sample_no = %s
        """, (sample_no,))

        sample_info = cur.fetchone()
        if not sample_info:
            raise HTTPException(404, f"Sample not found: {sample_no}")

        sample_id, request_id = sample_info

        # Get test distribution for this request
        sample_to_test_map, test_distribution = get_test_distribution_for_request(
            request_id, cur
        )

        # Get which test this sample belongs to
        test_info = sample_to_test_map.get(sample_id)
        if not test_info:
            raise HTTPException(
                400, f"Cannot determine test type for sample {sample_no}"
            )

        item_code = test_info["item_code"]
        test_name = test_info["test_name"]
        tri_id = test_info["tri_id"]

        # Get ALL samples for this test type
        test_samples = []
        for sample_id_key, test_data in sample_to_test_map.items():
            if test_data["item_code"] == item_code:
                cur.execute("""
                    SELECT sample_no
                    FROM samples
                    WHERE sample_id = %s
                """, (sample_id_key,))
                sample_row = cur.fetchone()
                if sample_row:
                    test_samples.append(sample_row[0])

        # Check if a report already exists for this test type
        report_exists = False
        existing_report = None

        # Check if any sample of this test type already has a report
        for sample_id_key in sample_to_test_map:
            if sample_to_test_map[sample_id_key]["item_code"] == item_code:
                cur.execute("""
                    SELECT r.report_id, r.report_no, r.status, r.file_path
                    FROM reports r
                    WHERE r.sample_id = %s
                """, (sample_id_key,))

                report_row = cur.fetchone()
                if report_row:
                    report_exists = True
                    existing_report = {
                        "report_id": report_row[0],
                        "report_no": report_row[1],
                        "status": report_row[2],
                        "file_path": report_row[3],
                        "covers_samples": test_samples
                    }
                    break

        # Check if template exists in Supabase
        template_exists = False
        template_path = None
        template_type = None

        supabase_template_url, template_ext = get_template_from_supabase(
            item_code, test_name
        )
        if supabase_template_url:
            template_exists = True
            template_path = supabase_template_url
            template_type = template_ext

        return {
            "sample_id": sample_id,
            "sample_no": sample_no,
            "test_name": test_name,
            "item_code": item_code,
            "tri_id": tri_id,
            "template_available": template_exists,
            "template_path": template_path,
            "template_type": template_type,
            "test_samples": test_samples,  # All samples for this test type
            "sample_count": len(test_samples),
            "report_exists": report_exists,
            "existing_report": existing_report,
            "message": (
                f"This sample is for {test_name}. "
                f"{len(test_samples)} samples share this test type."
            )
        }

    except Exception as e:
        raise HTTPException(500, f"Error: {str(e)}")

    finally:
        cur.close()
        conn.close()


# ---------------------------
# 4. Download Report Template by Sample No - UPDATED
# ---------------------------
@router.get("/samples/by-number/{sample_no}/download-template")
def download_report_template_by_no(sample_no: str):
    """Download the Excel/Word report template for this test type"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get sample info
        cur.execute("""
            SELECT sample_id, request_id 
            FROM samples 
            WHERE sample_no = %s
        """, (sample_no,))
        
        sample_info = cur.fetchone()
        if not sample_info:
            raise HTTPException(404, f"Sample not found: {sample_no}")
        
        sample_id, request_id = sample_info
        
        # Get test distribution
        sample_to_test_map, _ = get_test_distribution_for_request(request_id, cur)
        
        # Get which test this sample belongs to
        test_info = sample_to_test_map.get(sample_id)
        if not test_info:
            raise HTTPException(400, f"Cannot determine test type for sample {sample_no}")
        
        item_code = test_info["item_code"]
        test_name = test_info["test_name"]
        
        # Look for template in Supabase
        supabase_template_url, _ = get_template_from_supabase(item_code, test_name)
        if not supabase_template_url:
            raise HTTPException(404, f"No report template found for {test_name} ({item_code})")

        template_path = supabase_template_url
        
        if not template_path:
            raise HTTPException(404, f"No report template found for {test_name} ({item_code})")
        
        # Download the template from Supabase
        try:
            response = requests.get(template_path)
            if response.status_code != 200:
                raise HTTPException(404, f"Template not found in storage: {template_path}")
            
            # Return the file content
            filename = os.path.basename(template_path)
            return Response(
                content=response.content,
                media_type='application/octet-stream',
                headers={'Content-Disposition': f'attachment; filename="{filename}"'}
            )
        except Exception as e:
            raise HTTPException(500, f"Error downloading template from storage: {str(e)}")
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error downloading template: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ---------------------------
# 5. Upload Completed Report - SIMPLIFIED WORKING VERSION
# ---------------------------
@router.post("/upload-report")
async def upload_report(
    sample_no: str = Form(...),
    uploaded_by: int = Form(...),
    file: UploadFile = File(...),
    notes: Optional[str] = Form(None)
):
    """Upload a completed report file for a test type (covers all samples of same test)"""
    conn = get_connection()
    cur = conn.cursor()
    file_path = None
    
    try:
        print(f"Starting report upload for sample: {sample_no}")
        
        # Verify sample exists
        cur.execute("SELECT sample_id, request_id FROM samples WHERE sample_no = %s", (sample_no,))
        sample_data = cur.fetchone()
        if not sample_data:
            raise HTTPException(404, f"Sample not found: {sample_no}")
        
        sample_id, request_id = sample_data
        print(f"Found sample: {sample_id}, request: {request_id}")
        
        # Get test distribution
        try:
            sample_to_test_map, test_distribution = get_test_distribution_for_request(request_id, cur)
            print(f"Test distribution loaded: {len(sample_to_test_map)} samples mapped")
        except Exception as e:
            print(f"Error in get_test_distribution_for_request: {str(e)}")
            raise HTTPException(500, f"Error processing test distribution: {str(e)}")
        
        # Get which test this sample belongs to
        test_info = sample_to_test_map.get(sample_id)
        if not test_info:
            raise HTTPException(400, f"Cannot determine test type for sample {sample_no}")
        
        item_code = test_info.get("item_code", "UNKNOWN")
        test_name = test_info.get("test_name", "Unknown Test")
        print(f"Test info: {item_code} - {test_name}")
        
        # Get all samples for this test type
        test_samples = []
        test_sample_ids = []
        for sample_id_key, test_data in sample_to_test_map.items():
            if test_data.get("item_code") == item_code:
                cur.execute("SELECT sample_no FROM samples WHERE sample_id = %s", (sample_id_key,))
                sample_row = cur.fetchone()
                if sample_row:
                    test_samples.append(sample_row[0])
                    test_sample_ids.append(sample_id_key)
        
        print(f"Found {len(test_samples)} samples for test type {item_code}: {test_samples}")
        
        # Check if report already exists for ANY of these samples
        existing_report_no = None
        for test_sample_id in test_sample_ids:
            cur.execute("SELECT report_no FROM reports WHERE sample_id = %s", (test_sample_id,))
            existing_report = cur.fetchone()
            if existing_report:
                existing_report_no = existing_report[0]
                break
        
        if existing_report_no:
            raise HTTPException(400, 
                f"A report already exists for {test_name}. "
                f"Report No: {existing_report_no}. "
                f"Please use the existing report instead of creating a new one."
            )
        
        # Generate unique report number
        report_no = generate_report_no(cur)
        print(f"Generated report number: {report_no}")
        
        # Generate unique filename
        file_extension = os.path.splitext(file.filename)[1].lower()
        if not file_extension:
            file_extension = ".docx"
        
        unique_filename = f"{report_no.replace(' ', '_')}_{item_code}_{secrets.token_hex(4)}{file_extension}"
        file_path = os.path.join(REPORTS_UPLOAD_DIR, unique_filename)
        
        # Ensure upload directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        # Save uploaded file
        print(f"Saving file to: {file_path}")
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # Prepare test info with notes
        test_info_with_notes = test_name
        if notes and notes.strip():
            short_notes = notes[:100] + "..." if len(notes) > 100 else notes
            test_info_with_notes = f"{test_name}"
        
        # Insert report record for the FIRST sample
        print(f"Inserting report for sample {test_sample_ids[0]}")
        cur.execute("""
            INSERT INTO reports (
                report_no, sample_id, original_filename, 
                stored_filename, file_path, file_type, uploaded_by, status,
                covers_test_type, covers_samples, notes
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'DRAFT', %s, %s, %s)
            RETURNING report_id
        """, (
            report_no,
            test_sample_ids[0],
            file.filename,
            unique_filename,
            file_path,
            file_extension[1:] if file_extension.startswith('.') else file_extension,
            uploaded_by,
            test_info_with_notes,
            test_samples,
            notes
        ))
        
        report_id = cur.fetchone()[0]
        print(f"Created main report with ID: {report_id}")
        
        # Link this report to other samples of the same test type
        for i, other_sample_id in enumerate(test_sample_ids[1:], 1):
            try:
                print(f"Linking report to sample {other_sample_id} ({i+1}/{len(test_sample_ids)})")
                cur.execute("""
                    INSERT INTO reports (
                        report_no, sample_id, original_filename, 
                        stored_filename, file_path, file_type, uploaded_by, status,
                        covers_test_type, covers_samples, linked_to_report_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'DRAFT', %s, %s, %s)
                """, (
                    report_no,
                    other_sample_id,
                    file.filename,
                    unique_filename,
                    file_path,
                    file_extension[1:] if file_extension.startswith('.') else file_extension,
                    uploaded_by,
                    test_info_with_notes,
                    test_samples,
                    report_id
                ))
            except Exception as link_error:
                print(f"Warning: Failed to link to sample {other_sample_id}: {link_error}")
                # Continue with other samples
        
        conn.commit()
        print("Transaction committed successfully")
        
        return {
            "message": f"Report uploaded successfully for {test_name}",
            "report_id": report_id,
            "report_no": report_no,
            "test_name": test_name,
            "item_code": item_code,
            "covers_samples": test_samples,
            "sample_count": len(test_samples),
            "status": "DRAFT",
            "next_step": "Report is in DRAFT status. Submit for supervisor review."
        }
        
    except HTTPException as http_err:
        print(f"HTTP Exception: {http_err.detail}")
        if conn:
            conn.rollback()
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        raise
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        import traceback
        traceback.print_exc()
        
        if conn:
            conn.rollback()
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(500, f"Error uploading report: {str(e)}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
# ---------------------------
# 6. Get Reports with New Format - FIXED
# ---------------------------
# ---------------------------
# 6. Get Reports with New Format - FIXED
# ---------------------------
@router.get("")
def get_reports(status: Optional[str] = None):
    """Get reports with optional status filter - shows which test type they cover - FIXED VERSION"""
    print(f"\n" + "="*50)
    print(f"DEBUG: get_reports called with status={status}")
    print(f"="*50 + "\n")
    
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # IMPORTANT: Use DISTINCT to get unique report_no entries
        # Use covers_test_type from reports table instead of joining with quotation_items
        query = """
            SELECT DISTINCT ON (r.report_no)
                r.*, 
                s.sample_no,
                -- Use covers_test_type from reports table, not from quotation_items
                r.covers_test_type as test_name,
                -- Try to get item_code from the test distribution if possible
                COALESCE(
                    (SELECT qi.item_code 
                     FROM test_request_items tri 
                     JOIN quotation_items qi ON tri.quotation_item_id = qi.item_id
                     JOIN samples s2 ON tri.test_request_id = s2.request_id
                     WHERE s2.sample_id = r.sample_id 
                     LIMIT 1),
                    'N/A'
                ) as item_code,
                u.username as uploaded_by_username,
                uc.username as checked_by_username,
                ua.username as approved_by_username
            FROM reports r
            LEFT JOIN samples s ON r.sample_id = s.sample_id
            LEFT JOIN users u ON r.uploaded_by = u.user_id
            LEFT JOIN users uc ON r.checked_by = uc.user_id
            LEFT JOIN users ua ON r.approved_by = ua.user_id
            WHERE 1=1
        """
        params = []
        
        if status and status != "ALL":
            query += " AND r.status = %s"
            params.append(status)
        
        query += " ORDER BY r.report_no, r.created_at DESC"
        
        print(f"DEBUG: Executing query:\n{query}")
        print(f"DEBUG: Query params: {params}")
        
        cur.execute(query, tuple(params))
        
        columns = [desc[0] for desc in cur.description]
        print(f"DEBUG: Query columns: {columns}")
        
        all_rows = cur.fetchall()
        print(f"DEBUG: Fetched {len(all_rows)} rows from database")
        
        reports = []
        
        for i, row in enumerate(all_rows):
            print(f"\nDEBUG: Row {i}: {row}")
            report_dict = dict(zip(columns, row))
            print(f"DEBUG: Report dict: {report_dict.get('report_no')}")
            
            # Get all samples covered by this report (same report_no)
            cur.execute("""
                SELECT s.sample_no
                FROM reports r2
                JOIN samples s ON r2.sample_id = s.sample_id
                WHERE r2.report_no = %s
                ORDER BY s.sample_no
            """, (report_dict["report_no"],))
            
            covered_samples = [row[0] for row in cur.fetchall()]
            print(f"DEBUG: Covered samples for {report_dict.get('report_no')}: {covered_samples}")
            
            report_dict["covered_samples"] = covered_samples
            report_dict["sample_count"] = len(covered_samples)
            
            reports.append(report_dict)
        
        print(f"\n" + "="*50)
        print(f"DEBUG: Returning {len(reports)} reports")
        print(f"DEBUG: Reports: {reports}")
        print(f"="*50 + "\n")
        
        # If empty, return a test structure
        if len(reports) == 0:
            print("WARNING: No reports found! Returning test data structure")
        
        return reports
        
    except Exception as e:
        print(f"\n" + "!"*50)
        print(f"ERROR in get_reports: {str(e)}")
        import traceback
        traceback.print_exc()
        print(f"!"*50 + "\n")
        raise HTTPException(500, f"Error fetching reports: {str(e)}")
    finally:
        cur.close()
        conn.close()
# ---------------------------
# 7. Get Report by Sample No - UPDATED
# ---------------------------
@router.get("/by-sample/{sample_no}")
def get_report_by_sample_no(sample_no: str):
    """Get report details by sample number - returns the combined report for the test type"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get sample and its test type
        cur.execute("""
            SELECT s.sample_id, s.request_id
            FROM samples s
            WHERE s.sample_no = %s
        """, (sample_no,))
        
        sample_row = cur.fetchone()
        if not sample_row:
            raise HTTPException(404, f"Sample not found: {sample_no}")
        
        sample_id, request_id = sample_row
        
        # Get test distribution
        sample_to_test_map, _ = get_test_distribution_for_request(request_id, cur)
        
        # Get which test this sample belongs to
        test_info = sample_to_test_map.get(sample_id)
        if not test_info:
            raise HTTPException(400, f"Cannot determine test type for sample {sample_no}")
        
        item_code = test_info["item_code"]
        
        # Find report for any sample of this test type
        cur.execute("""
            SELECT r.report_id, r.report_no, r.status, r.file_path,
                   r.created_at, r.checked_at, r.approved_at,
                   u.username as uploaded_by_username,
                   uc.username as checked_by_username,
                   ua.username as approved_by_username,
                   s2.sample_no as linked_sample_no
            FROM reports r
            JOIN samples s2 ON r.sample_id = s2.sample_id
            JOIN test_request_items tri ON s2.request_id = tri.test_request_id
            JOIN quotation_items qi ON tri.quotation_item_id = qi.item_id
            LEFT JOIN users u ON r.uploaded_by = u.user_id
            LEFT JOIN users uc ON r.checked_by = uc.user_id
            LEFT JOIN users ua ON r.approved_by = ua.user_id
            WHERE qi.item_code = %s AND s2.request_id = %s
            ORDER BY r.created_at DESC
            LIMIT 1
        """, (item_code, request_id))
        
        report_row = cur.fetchone()
        if not report_row:
            raise HTTPException(404, f"No report found for test type: {item_code}")
        
        # Get all samples covered by this report (same report_no)
        report_no = report_row[1]
        cur.execute("""
            SELECT DISTINCT s.sample_no
            FROM reports r
            JOIN samples s ON r.sample_id = s.sample_id
            WHERE r.report_no = %s
            ORDER BY s.sample_no
        """, (report_no,))
        
        covered_samples = [row[0] for row in cur.fetchall()]
        
        return {
            "report_id": report_row[0],
            "report_no": report_row[1],
            "status": report_row[2],
            "file_path": report_row[3],
            "created_at": report_row[4],
            "checked_at": report_row[5],
            "approved_at": report_row[6],
            "uploaded_by_username": report_row[7],
            "checked_by_username": report_row[8],
            "approved_by_username": report_row[9],
            "linked_sample_no": report_row[10],
            "test_type": item_code,
            "covered_samples": covered_samples,
            "sample_count": len(covered_samples),
            "download_url": f"/reports/reports/{report_row[0]}/download"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error fetching report: {str(e)}")
    finally:
        cur.close()
        conn.close()

# ---------------------------
# 8. Submit for Review - UPDATED (Single report)
# ---------------------------
@router.post("/reports/{report_id}/submit-for-review")
def submit_for_review(
    report_id: int,
    checked_by: int
):
    """Submit report for supervisor review"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get report details
        cur.execute("""
            SELECT report_id, report_no, status, is_locked
            FROM reports 
            WHERE report_id = %s
        """, (report_id,))
        
        report_row = cur.fetchone()
        if not report_row:
            raise HTTPException(404, "Report not found")
        
        report_id_db, report_no, status, is_locked = report_row
        
        if status != "DRAFT":
            raise HTTPException(400, f"Cannot submit - report status is {status}, not DRAFT")
        
        if is_locked:
            raise HTTPException(400, "Cannot submit - report is locked")
        
        # Update the single report
        cur.execute("""
            UPDATE reports 
            SET status = 'UNDER_REVIEW', checked_by = %s, checked_at = NOW()
            WHERE report_id = %s
            RETURNING report_id, report_no
        """, (checked_by, report_id))
        
        updated = cur.fetchone()
        if not updated:
            raise HTTPException(400, "Failed to update report")
        
        conn.commit()
        return {
            "message": "Report submitted for supervisor review", 
            "report_id": report_id_db,
            "report_no": report_no,
            "status": "UNDER_REVIEW",
            "checked_by": checked_by
        }
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error: {str(e)}")
    finally:
        cur.close()
        conn.close()

# ---------------------------
# 9. Approve Report - APPROVES ALL LINKED REPORTS
# ---------------------------
@router.post("/reports/{report_id}/approve")
def approve_report(
    report_id: int,
    approved_by: int
):
    """Approve and lock report - approves all linked reports"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get the report_no for this report
        cur.execute("SELECT report_no FROM reports WHERE report_id = %s", (report_id,))
        report_row = cur.fetchone()
        if not report_row:
            raise HTTPException(404, "Report not found")
        
        report_no = report_row[0]
        
        # Update ALL reports with this report_no
        cur.execute("""
            UPDATE reports 
            SET status = 'APPROVED', approved_by = %s, approved_at = NOW(), 
                is_locked = TRUE
            WHERE report_no = %s AND status = 'UNDER_REVIEW'
            RETURNING report_id, report_no
        """, (approved_by, report_no))
        
        updated_count = cur.rowcount
        if updated_count == 0:
            raise HTTPException(400, "Cannot approve - reports not under review")
        
        conn.commit()
        return {
            "message": f"{updated_count} report(s) approved and locked permanently", 
            "report_id": report_id,
            "report_no": report_no,
            "status": "APPROVED",
            "is_locked": True,
            "approved_by": approved_by,
            "updated_count": updated_count
        }
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error: {str(e)}")
    finally:
        cur.close()
        conn.close()

# ---------------------------
# 10. Get Report Details - UPDATED
# ---------------------------
@router.get("/{report_id}")
def get_report(report_id: int):
    """Get report details - shows which samples it covers"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            SELECT r.report_id, r.report_no, r.sample_id, r.status, r.is_locked,
                   r.original_filename, r.file_path, r.file_type, r.created_at,
                   r.checked_at, r.approved_at, r.notes,
                   r.uploaded_by, r.checked_by, r.approved_by
            FROM reports r
            WHERE r.report_id = %s
        """, (report_id,))
        
        report = cur.fetchone()
        if not report:
            raise HTTPException(404, "Report not found")
        
        # Get all samples covered by this report (same report_no)
        report_no = report[1]
        cur.execute("""
            SELECT s.sample_no
            FROM reports r
            JOIN samples s ON r.sample_id = s.sample_id
            WHERE r.report_no = %s
            ORDER BY s.sample_no
        """, (report_no,))
        
        covered_samples = [row[0] for row in cur.fetchall()]
        
        return {
            "report_id": report[0],
            "report_no": report[1],
            "sample_id": report[2],
            "status": report[3],
            "is_locked": report[4],
            "original_filename": report[5],
            "file_path": report[6],
            "file_type": report[7],
            "created_at": report[8],
            "checked_at": report[9],
            "approved_at": report[10],
            "notes": report[11],
            "uploaded_by": report[12],
            "checked_by": report[13],
            "approved_by": report[14],
            "download_url": f"/reports/{report_id}/download",
            "covered_samples": covered_samples,
            "sample_count": len(covered_samples),
            "can_edit": report[3] == "DRAFT" and not report[4],
            "can_submit": report[3] == "DRAFT",
            "can_approve": report[3] == "UNDER_REVIEW"
        }
        
    except Exception as e:
        raise HTTPException(500, f"Error: {str(e)}")
    finally:
        cur.close()
        conn.close()

# ---------------------------
# 11. NEW: Get Test Type Distribution for a Request
# ---------------------------
@router.get("/request/{request_id}/test-distribution")
def get_request_test_distribution(request_id: int):
    """Get how samples are distributed across test types for a request"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        sample_to_test_map, test_distribution = get_test_distribution_for_request(request_id, cur)
        
        # Get request info
        cur.execute("""
            SELECT request_no, project_id
            FROM test_requests 
            WHERE test_request_id = %s
        """, (request_id,))
        
        request_info = cur.fetchone()
        if not request_info:
            raise HTTPException(404, "Request not found")
        
        return {
            "request_id": request_id,
            "request_no": request_info[0],
            "project_id": request_info[1],
            "test_distribution": test_distribution,
            "total_samples": len(sample_to_test_map),
            "unique_test_types": len(test_distribution)
        }
        
    except Exception as e:
        raise HTTPException(500, f"Error: {str(e)}")
    finally:
        cur.close()
        conn.close()

# ---------------------------
# 12. Replace Report File - UPDATES ALL LINKED REPORTS
# ---------------------------
@router.post("/reports/{report_id}/replace-file")
async def replace_report_file(
    report_id: int,
    replaced_by: int = Form(...),
    file: UploadFile = File(...),
    notes: Optional[str] = Form(None)
):
    """Replace report file with corrected version - updates all linked reports"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Check if main report can be modified
        cur.execute("""
            SELECT r.file_path, r.status, r.is_locked, r.report_no
            FROM reports r
            WHERE r.report_id = %s
        """, (report_id,))
        
        report = cur.fetchone()
        if not report:
            raise HTTPException(404, "Report not found")
        
        old_file_path, status, is_locked, report_no = report
        
        if is_locked:
            raise HTTPException(400, "Cannot replace locked report")
        
        if status != "DRAFT":
            raise HTTPException(400, "Can only replace DRAFT reports")
        
        # Save new file
        file_ext = os.path.splitext(file.filename)[1].lower()
        unique_name = f"rev_{report_no.replace(' ', '_')}_{secrets.token_hex(8)}{file_ext}"
        new_file_path = os.path.join(REPORTS_UPLOAD_DIR, unique_name)
        
        with open(new_file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # Update ALL reports with this report_no
        cur.execute("""
            UPDATE reports 
            SET original_filename = %s, stored_filename = %s,
                file_path = %s, file_type = %s, notes = %s
            WHERE report_no = %s
        """, (file.filename, unique_name, new_file_path, file_ext[1:], notes, report_no))
        
        updated_count = cur.rowcount
        
        conn.commit()
        
        # Remove old file (only if it's not used by other reports)
        if os.path.exists(old_file_path):
            # Check if any other report still uses this file
            cur.execute("SELECT COUNT(*) FROM reports WHERE file_path = %s", (old_file_path,))
            if cur.fetchone()[0] == 0:
                os.remove(old_file_path)
        
        return {
            "message": f"Report file updated for {updated_count} linked reports",
            "report_id": report_id,
            "report_no": report_no,
            "replaced_by": replaced_by,
            "updated_count": updated_count
        }
        
    except HTTPException:
        conn.rollback()
        if 'new_file_path' in locals() and os.path.exists(new_file_path):
            os.remove(new_file_path)
        raise
    except Exception as e:
        conn.rollback()
        if 'new_file_path' in locals() and os.path.exists(new_file_path):
            os.remove(new_file_path)
        raise HTTPException(500, f"Error: {str(e)}")
    finally:
        cur.close()
        conn.close()





# ---------------------------
# 13. Download Report File - NEW ENDPOINT FOR VIEWREPORTS.JSX
# ---------------------------
@router.get("/reports/{report_id}/download")
def download_report_file(report_id: int):
    """Download the actual report file - for ViewReports.jsx"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get report file details
        cur.execute("""
            SELECT r.original_filename, r.file_path, r.file_type, r.report_no
            FROM reports r
            WHERE r.report_id = %s
        """, (report_id,))
        
        report = cur.fetchone()
        if not report:
            raise HTTPException(404, "Report not found")
        
        original_filename, file_path, file_type, report_no = report
        
        # Check if file exists
        if not os.path.exists(file_path):
            raise HTTPException(404, f"Report file not found at: {file_path}")
        
        # Determine content type
        content_types = {
            'pdf': 'application/pdf',
            'doc': 'application/msword',
            'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'xls': 'application/vnd.ms-excel',
            'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        }
        
        media_type = content_types.get(file_type.lower(), 'application/octet-stream')
        
        # Generate a clean filename
        clean_report_no = report_no.replace(' ', '').replace('-', '_')  # FIXED LINE
        if original_filename:
            clean_original = original_filename.rstrip('_ ')  # REMOVE TRAILING UNDERSCORE
            filename = f"{clean_report_no}_{clean_original}"
        else:
            ext = f".{file_type}" if file_type else ""
            filename = f"{clean_report_no}_report{ext}"
        
        # Optional: final cleanup
        filename = filename.rstrip('_ ')
        
        return FileResponse(
            path=file_path,
            filename=filename,
            media_type=media_type
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error downloading report: {str(e)}")
    finally:
        cur.close()
        conn.close()
















# Add this function to your reports.py file, or create a new module for it

def populate_report_template_from_url(template_url: str, report_data: dict) -> str:
    """
    Download Excel template from a URL and populate it with report data.
    
    Args:
        template_url: URL to the Excel template (Supabase)
        report_data: Dictionary containing report data to populate
        
    Returns:
        Path to the populated Excel file
    """
    temp_template_path = None
    try:
        # Download the template from URL
        response = requests.get(template_url)
        if response.status_code != 200:
            raise Exception(f"Failed to download template from {template_url}")
        
        temp_dir = tempfile.gettempdir()
        temp_template_filename = f"template_{secrets.token_hex(8)}.xlsx"
        temp_template_path = os.path.join(temp_dir, temp_template_filename)
        
        with open(temp_template_path, 'wb') as f:
            f.write(response.content)
        
        # Load the workbook
        wb = openpyxl.load_workbook(temp_template_path)
        ws = wb.active  # Assume first sheet is where we populate
        
        # Format sample numbers
        sample_nos = report_data.get('sample_nos', '')
        if isinstance(sample_nos, list):
            sample_nos_str = ", ".join(sample_nos)
        else:
            sample_nos_str = str(sample_nos)
        
        # Map of cells to values
        cell_mapping = {
            'N7': report_data.get('report_no', ''),
            'N8': report_data.get('report_date', ''),
            'N9': report_data.get('request_no', ''),
            'N10': sample_nos_str,
            'N11': report_data.get('lp_number', ''),
            'N12': report_data.get('date_of_test', ''),
            'N13': report_data.get('tested_by', ''),
            'E12': report_data.get('location', ''),
            'E9': report_data.get('client_name', ''),
            'E43': report_data.get('test_standard', '')
        }
        
        # Populate the cells
        for cell_ref, value in cell_mapping.items():
            if value:
                ws[cell_ref] = value
        
        # Save populated workbook to temp file
        temp_filename = f"populated_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        temp_path = os.path.join(temp_dir, temp_filename)
        
        wb.save(temp_path)
        wb.close()
        
        return temp_path
    
    except Exception as e:
        raise Exception(f"Error populating template from URL: {str(e)}")
    
    finally:
        # Clean up downloaded template
        if temp_template_path and os.path.exists(temp_template_path):
            os.remove(temp_template_path)

# Add this endpoint to your reports.py router


# You'll also need to add this import at the top of reports.py:
# import openpyxl
# import tempfile









# Add this new endpoint to reports.py

@router.get("/samples/by-number/{sample_no}/download-populated-template")
async def download_populated_template_by_sample(
    sample_no: str,
    user_id: Optional[int] = None
):
    """
    Download populated template based on sample number
    (Creates temporary data without saving to DB)
    """
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # First, check if a report already exists for this sample/test type
        # Get sample details
        cur.execute("""
            SELECT s.sample_id, s.sample_no, s.request_id,
                   tr.request_no, tr.project_id
            FROM samples s
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            WHERE s.sample_no = %s
        """, (sample_no,))
        
        sample_data = cur.fetchone()
        if not sample_data:
            raise HTTPException(404, f"Sample not found: {sample_no}")
        
        sample_id, sample_no_db, request_id, request_no, project_id = sample_data
        
        # Get test distribution to find which test type this sample belongs to
        sample_to_test_map, test_distribution = get_test_distribution_for_request(request_id, cur)
        
        # Get which test this sample belongs to
        test_info = sample_to_test_map.get(sample_id)
        if not test_info:
            raise HTTPException(400, f"Cannot determine test type for sample {sample_no}")
        
        item_code = test_info["item_code"]
        test_name = test_info["test_name"]
        
        # Get all samples for THIS TEST TYPE ONLY (not all samples in the request)
        test_samples = []
        for sample_id_key, test_data in sample_to_test_map.items():
            if test_data.get("item_code") == item_code:
                cur.execute("SELECT sample_no FROM samples WHERE sample_id = %s", (sample_id_key,))
                sample_row = cur.fetchone()
                if sample_row:
                    test_samples.append(sample_row[0])
        
        # ✅ CHECK IF REPORT ALREADY EXISTS FOR THIS TEST TYPE
        existing_report_no = None
        for sample_id_key in sample_to_test_map:
            if sample_to_test_map[sample_id_key]["item_code"] == item_code:
                cur.execute("""
                    SELECT r.report_no
                    FROM reports r
                    WHERE r.sample_id = %s
                """, (sample_id_key,))
                report_row = cur.fetchone()
                if report_row:
                    existing_report_no = report_row[0]
                    break
        
        # Get project details
        cur.execute("""
            SELECT p.project_no, p.project_name, p.location,
                   c.name as client_name
            FROM projects p
            JOIN clients c ON p.client_id = c.client_id
            WHERE p.project_id = %s
        """, (project_id,))
        
        project_data = cur.fetchone()
        if not project_data:
            raise HTTPException(404, "Project not found")
        
        project_no, project_name, location, client_name = project_data
        
        # Get test item details
        cur.execute("""
            SELECT qi.item_code, qi.description, qi.test_standard
            FROM quotation_items qi
            WHERE qi.item_code = %s
            LIMIT 1
        """, (item_code,))
        
        item_data = cur.fetchone()
        if not item_data:
            # Try by description if not found by item_code
            cur.execute("""
                SELECT qi.item_code, qi.description, qi.test_standard
                FROM quotation_items qi
                WHERE qi.description ILIKE %s
                LIMIT 1
            """, (f"%{test_name}%",))
            item_data = cur.fetchone()
        
        if not item_data:
            raise HTTPException(404, f"Test item details not found for {item_code}")
        
        item_code_db, test_name_db, test_standard = item_data
        
        # Get user details if user_id is provided
        tested_by = "Lab Chemist"  # Default
        if user_id:
            try:
                cur.execute("""
                    SELECT username, full_name 
                    FROM users 
                    WHERE user_id = %s
                """, (user_id,))
                user_data = cur.fetchone()
                if user_data:
                    # Use full_name if available, otherwise username
                    tested_by = user_data[1] if user_data[1] else user_data[0]
            except Exception as user_error:
                print(f"Error fetching user details: {user_error}")
                # Keep default value
        
        # ✅ USE EXISTING REPORT NUMBER IF AVAILABLE, OTHERWISE GENERATE A PREVIEW
        if existing_report_no:
            report_no_for_template = existing_report_no
            print(f"Using existing report number: {report_no_for_template}")
        else:
            today = datetime.now()
            date_str = today.strftime("%d%m%y")
            
            # Count reports created today to get the next sequence number
            cur.execute("""
                SELECT COUNT(*) 
                FROM reports 
                WHERE DATE(created_at) = CURRENT_DATE
            """)
            count = cur.fetchone()[0]
            
            # Generate the next sequence number
            seq_num = count + 1
            report_seq = f"{seq_num:03d}"
            
            # Create the preview report number
            report_no_for_template = f"GR - {date_str} - {report_seq}"
            print(f"Generated preview report number: {report_no_for_template}")
        
        # ✅ NOW USE THE ACTUAL/EXISTING REPORT NUMBER
        template_data = {
            'report_no': report_no_for_template,
            'report_date': datetime.now().strftime("%d/%m/%Y"),
            'request_no': request_no,
            'sample_nos': test_samples,
            'lp_number': project_no,
            'date_of_test': datetime.now().strftime("%d/%m/%Y"),
            'tested_by': tested_by,
            'location': location,
            'client_name': client_name,
            'test_standard': test_standard or "Not specified"
        }
        
        # Look for the template in Supabase
        supabase_template_url, _ = get_template_from_supabase(item_code, test_name)
        if not supabase_template_url:
            # Try with item_code_db as fallback
            supabase_template_url, _ = get_template_from_supabase(item_code_db, test_name_db)
        
        if not supabase_template_url:
            raise HTTPException(404, f"No template found for item code: {item_code}")

        template_path = supabase_template_url
        
        if not template_path:
            raise HTTPException(404, f"No template found for item code: {item_code}")
        
        # Populate the template with data
        populated_path = populate_report_template_from_url(template_path, template_data)
        
        # Create a nice filename for download
        if existing_report_no:
            download_filename = f"{existing_report_no.replace(' ', '_')}_{item_code}.xlsx"
        else:
            download_filename = f"{item_code}_Report_Template_{len(test_samples)}_samples.xlsx"
        
        # Return the populated file
        return FileResponse(
            path=populated_path,
            filename=download_filename,
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error generating populated template: {str(e)}")
    finally:
        cur.close()
        conn.close()
