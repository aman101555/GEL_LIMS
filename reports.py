# reports.py - UPDATED VERSION WITH SUPABASE STORAGE FOR REPORTS
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, Response
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

# Add Supabase imports
from supabase import create_client, Client

# Supabase configuration
SUPABASE_URL = "https://hqwgkmbjmcxpxbwccclo.supabase.co"
SUPABASE_KEY = "sb_secret_-8uQCdQSiUgDFO_MUEsTWg_TPWtsyy3"

# Initialize Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

router = APIRouter(tags=["Reports"])

# Remove local file storage - we'll use Supabase only
# REPORTS_UPLOAD_DIR is no longer needed for permanent storage

SUPABASE_STORAGE_URL = "https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/templates"

# ---------------------------
# Helper Functions
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
# 1. Search Sample by Sample No (GS format)
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
# 2. Get Latest 10 Sample Numbers
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
        conn.close()

# ---------------------------
# 3. Get Sample Test Info & Check for Existing Reports
# ---------------------------
# ---------------------------
# 5. Upload Completed Report to Supabase - UPDATED
# ---------------------------
@router.post("/upload-report")
async def upload_report(
    sample_no: str = Form(...),
    uploaded_by: int = Form(...),
    file: UploadFile = File(...),
    notes: Optional[str] = Form(None)
):
    """Upload a completed report file to Supabase Storage (covers all samples of same test)"""
    conn = get_connection()
    cur = conn.cursor()
    
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
        existing_report_id = None
        for test_sample_id in test_sample_ids:
            cur.execute("SELECT report_id, report_no FROM reports WHERE sample_id = %s", (test_sample_id,))
            existing_report = cur.fetchone()
            if existing_report:
                existing_report_id, existing_report_no = existing_report
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
        
        # Read file content
        file_content = await file.read()
        
        # Get file extension
        file_extension = os.path.splitext(file.filename)[1].lower()
        if not file_extension:
            file_extension = ".pdf"  # default extension
        
        # Create cloud filename in reports folder
        clean_report_no = report_no.replace(' ', '_').replace('-', '_')
        cloud_filename = f"reports/{clean_report_no}_{item_code}_{secrets.token_hex(4)}{file_extension}"
        
        # Upload to Supabase Storage (using "reports" bucket)
        try:
            upload_response = supabase.storage.from_("reports").upload(
                path=cloud_filename,
                file=file_content,
                file_options={"content-type": file.content_type}
            )
            print(f"✅ Uploaded to Supabase reports bucket: {cloud_filename}")
            
            # Get the public URL
            public_url = supabase.storage.from_("reports").get_public_url(cloud_filename)
            
        except Exception as e:
            print(f"❌ Error uploading to reports bucket: {e}")
            raise HTTPException(500, f"Failed to upload to Supabase: {str(e)}")
        
        # Prepare test info with notes
        test_info_with_notes = test_name
        if notes and notes.strip():
            short_notes = notes[:100] + "..." if len(notes) > 100 else notes
            test_info_with_notes = f"{test_name}"
        
        # Insert report record for the FIRST sample with the Supabase URL
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
            cloud_filename,
            public_url,  # Store the Supabase URL here
            file_extension[1:] if file_extension.startswith('.') else file_extension,
            uploaded_by,
            test_info_with_notes,
            test_samples,
            notes
        ))
        
        main_report_id = cur.fetchone()[0]
        print(f"Created main report with ID: {main_report_id}")
        
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
                    cloud_filename,
                    public_url,  # Same URL for all linked reports
                    file_extension[1:] if file_extension.startswith('.') else file_extension,
                    uploaded_by,
                    test_info_with_notes,
                    test_samples,
                    main_report_id
                ))
            except Exception as link_error:
                print(f"Warning: Failed to link to sample {other_sample_id}: {link_error}")
                # Continue with other samples
        
        conn.commit()
        print("Transaction committed successfully")
        
        return {
            "message": f"Report uploaded successfully to cloud for {test_name}",
            "report_id": main_report_id,
            "report_no": report_no,
            "test_name": test_name,
            "item_code": item_code,
            "covers_samples": test_samples,
            "sample_count": len(test_samples),
            "status": "DRAFT",
            "file_url": public_url,
            "next_step": "Report is in DRAFT status. Submit for supervisor review."
        }
        
    except HTTPException as http_err:
        print(f"HTTP Exception: {http_err.detail}")
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        import traceback
        traceback.print_exc()
        
        if conn:
            conn.rollback()
        raise HTTPException(500, f"Error uploading report: {str(e)}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# ---------------------------
# 6. Download Report File - UPDATED for Supabase
# ---------------------------
@router.get("/reports/{report_id}/download")
def download_report_file(report_id: int):
    """Get the Supabase URL for the report file - redirects to cloud storage"""
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
        
        # Check if it's a Supabase URL
        if file_path and file_path.startswith('http'):
            # Redirect to the Supabase URL
            return {
                "has_file": True,
                "message": "Report file available",
                "report_id": report_id,
                "report_no": report_no,
                "download_url": file_path  # Send URL to frontend
            }
        else:
            # No file found or invalid path
            return {
                "has_file": False,
                "message": "No report file found. Please upload the report first.",
                "report_id": report_id,
                "report_no": report_no
            }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error fetching report: {str(e)}")
    finally:
        cur.close()
        conn.close()

# ---------------------------
# 13. Replace Report File - UPDATED for Supabase
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
            SELECT r.file_path, r.status, r.is_locked, r.report_no, r.stored_filename
            FROM reports r
            WHERE r.report_id = %s
        """, (report_id,))
        
        report = cur.fetchone()
        if not report:
            raise HTTPException(404, "Report not found")
        
        old_file_path, status, is_locked, report_no, old_stored_filename = report
        
        if is_locked:
            raise HTTPException(400, "Cannot replace locked report")
        
        if status != "DRAFT":
            raise HTTPException(400, "Can only replace DRAFT reports")
        
        # Read new file content
        file_content = await file.read()
        
        # Get file extension
        file_extension = os.path.splitext(file.filename)[1].lower()
        if not file_extension:
            file_extension = ".pdf"
        
        # Create new cloud filename
        clean_report_no = report_no.replace(' ', '_').replace('-', '_')
        new_cloud_filename = f"reports/{clean_report_no}_{secrets.token_hex(4)}{file_extension}"
        
        # Upload new file to Supabase (using "reports" bucket)
        try:
            upload_response = supabase.storage.from_("reports").upload(
                path=new_cloud_filename,
                file=file_content,
                file_options={"content-type": file.content_type}
            )
            print(f"✅ Uploaded replacement to Supabase reports bucket: {new_cloud_filename}")
            
            # Get the public URL
            new_public_url = supabase.storage.from_("reports").get_public_url(new_cloud_filename)
            
        except Exception as e:
            print(f"❌ Error uploading replacement: {e}")
            raise HTTPException(500, f"Failed to upload replacement: {str(e)}")
        
        # Update ALL reports with this report_no
        cur.execute("""
            UPDATE reports 
            SET original_filename = %s, stored_filename = %s,
                file_path = %s, file_type = %s, notes = %s
            WHERE report_no = %s
        """, (file.filename, new_cloud_filename, new_public_url, file_extension[1:], notes, report_no))
        
        updated_count = cur.rowcount
        
        conn.commit()
        
        # Try to delete old file from Supabase (optional, might want to keep for history)
        try:
            if old_stored_filename:
                supabase.storage.from_("reports").remove([old_stored_filename])
                print(f"✅ Removed old file: {old_stored_filename}")
        except Exception as e:
            print(f"⚠️ Could not remove old file: {e}")
        
        return {
            "message": f"Report file updated for {updated_count} linked reports",
            "report_id": report_id,
            "report_no": report_no,
            "replaced_by": replaced_by,
            "updated_count": updated_count,
            "new_file_url": new_public_url
        }
        
    except HTTPException as http_err:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error: {str(e)}")
    finally:
        cur.close()
        conn.close()

# Add this new endpoint to serve the actual file
@router.get("/files/{report_id}/download")
async def download_report_file_direct(report_id: int):
    """Directly download the report file from Supabase"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get report file details
        cur.execute("""
            SELECT r.file_path, r.original_filename, r.file_type, r.report_no
            FROM reports r
            WHERE r.report_id = %s
        """, (report_id,))
        
        report = cur.fetchone()
        if not report:
            raise HTTPException(404, "Report not found")
        
        file_path, original_filename, file_type, report_no = report
        
        if not file_path or not file_path.startswith('http'):
            raise HTTPException(404, "Report file not found in cloud storage")
        
        # Download the file from Supabase
        try:
            # Extract the path from the URL
            # URL format: https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/reports/reports/filename.pdf
            path_parts = file_path.split('/public/reports/')
            if len(path_parts) > 1:
                storage_path = path_parts[1]
            else:
                # Try alternative URL format
                storage_path = file_path.split('/object/public/reports/')[-1]
            
            # Download from Supabase (using "reports" bucket)
            response = supabase.storage.from_("reports").download(storage_path)
            
            # Determine content type
            content_types = {
                'pdf': 'application/pdf',
                'doc': 'application/msword',
                'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'xls': 'application/vnd.ms-excel',
                'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            }
            
            media_type = content_types.get(file_type.lower(), 'application/octet-stream')
            
            # Create filename for download
            clean_report_no = report_no.replace(' ', '_').replace('-', '_')
            filename = f"{clean_report_no}_{original_filename or f'report.{file_type}'}"
            
            return Response(
                content=response,
                media_type=media_type,
                headers={'Content-Disposition': f'attachment; filename="{filename}"'}
            )
            
        except Exception as e:
            print(f"Error downloading from Supabase: {e}")
            # Fallback: redirect to the URL
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=file_path)
        
    except Exception as e:
        raise HTTPException(500, f"Error downloading report: {str(e)}")
    finally:
        cur.close()
        conn.close()
# ---------------------------
# 7. Get Reports with Status Filter - UPDATED
# ---------------------------
@router.get("")
def get_reports(status: Optional[str] = None):
    """Get reports with optional status filter - shows which test type they cover"""
    print(f"\n" + "="*50)
    print(f"DEBUG: get_reports called with status={status}")
    print(f"="*50 + "\n")
    
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # IMPORTANT: Use DISTINCT to get unique report_no entries
        query = """
            SELECT DISTINCT ON (r.report_no)
                r.*, 
                s.sample_no,
                r.covers_test_type as test_name,
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
        print(f"="*50 + "\n")
        
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
# 8. Get Report by Sample No - UPDATED
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
# 9. Submit for Review - UPDATED
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
# 10. Approve Report - APPROVES ALL LINKED REPORTS
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
# 11. Get Report Details - UPDATED
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
            "download_url": f"/reports/reports/{report_id}/download",
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
# 12. NEW: Get Test Type Distribution for a Request
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
# 13. Replace Report File - UPDATED for Supabase
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
            SELECT r.file_path, r.status, r.is_locked, r.report_no, r.stored_filename
            FROM reports r
            WHERE r.report_id = %s
        """, (report_id,))
        
        report = cur.fetchone()
        if not report:
            raise HTTPException(404, "Report not found")
        
        old_file_path, status, is_locked, report_no, old_stored_filename = report
        
        if is_locked:
            raise HTTPException(400, "Cannot replace locked report")
        
        if status != "DRAFT":
            raise HTTPException(400, "Can only replace DRAFT reports")
        
        # Read new file content
        file_content = await file.read()
        
        # Get file extension
        file_extension = os.path.splitext(file.filename)[1].lower()
        if not file_extension:
            file_extension = ".pdf"
        
        # Create new cloud filename
        clean_report_no = report_no.replace(' ', '_').replace('-', '_')
        new_cloud_filename = f"reports/{clean_report_no}_{secrets.token_hex(4)}{file_extension}"
        
        # Upload new file to Supabase
        try:
            upload_response = supabase.storage.from_("reports").upload(
                path=new_cloud_filename,
                file=file_content,
                file_options={"content-type": file.content_type}
            )
            print(f"✅ Uploaded replacement to Supabase: {new_cloud_filename}")
            
            # Get the public URL
            new_public_url = supabase.storage.from_("reports").get_public_url(new_cloud_filename)
            
        except Exception as e:
            print(f"❌ Error uploading replacement: {e}")
            raise HTTPException(500, f"Failed to upload replacement: {str(e)}")
        
        # Update ALL reports with this report_no
        cur.execute("""
            UPDATE reports 
            SET original_filename = %s, stored_filename = %s,
                file_path = %s, file_type = %s, notes = %s
            WHERE report_no = %s
        """, (file.filename, new_cloud_filename, new_public_url, file_extension[1:], notes, report_no))
        
        updated_count = cur.rowcount
        
        conn.commit()
        
        # Try to delete old file from Supabase (optional, might want to keep for history)
        try:
            if old_stored_filename:
                supabase.storage.from_("reports").remove([old_stored_filename])
                print(f"✅ Removed old file: {old_stored_filename}")
        except Exception as e:
            print(f"⚠️ Could not remove old file: {e}")
        
        return {
            "message": f"Report file updated for {updated_count} linked reports",
            "report_id": report_id,
            "report_no": report_no,
            "replaced_by": replaced_by,
            "updated_count": updated_count,
            "new_file_url": new_public_url
        }
        
    except HTTPException as http_err:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error: {str(e)}")
    finally:
        cur.close()
        conn.close()

# ---------------------------
# 14. Download Populated Template - KEPT AS IS
# ---------------------------
def populate_report_template_from_url(template_url: str, report_data: dict) -> str:
    """Download Excel template from a URL and populate it with report data."""
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


@router.get("/samples/by-number/{sample_no}")
def get_sample_by_number(sample_no: str):
    """
    Fetch sample info by sample number.
    Returns test type, sample count, and whether a report already exists.
    Used by CreateReport to populate the form before upload/download.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        # 1. Get sample + request
        cur.execute("""
            SELECT s.sample_id, s.sample_no, s.request_id, s.status,
                   tr.request_no, tr.project_id
            FROM samples s
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            WHERE s.sample_no = %s
        """, (sample_no,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"Sample \"{sample_no}\" not found. Please check the sample number.")

        sample_id, sample_no_db, request_id, status, request_no, project_id = row

        # 2. Get test distribution to find which test this sample belongs to
        sample_to_test_map, test_distribution = get_test_distribution_for_request(request_id, cur)

        test_info = sample_to_test_map.get(sample_id)
        if not test_info:
            raise HTTPException(400, f"Cannot determine test type for sample {sample_no}")

        item_code = test_info["item_code"]
        test_name = test_info["test_name"]

        # 3. Get all samples sharing this test type in the same request
        test_samples = []
        for sid, tdata in sample_to_test_map.items():
            if tdata.get("item_code") == item_code:
                cur.execute("SELECT sample_no FROM samples WHERE sample_id = %s", (sid,))
                s_row = cur.fetchone()
                if s_row:
                    test_samples.append(s_row[0])

        # 4. Check if a report already exists for this test type
        existing_report = None
        for sid in sample_to_test_map:
            if sample_to_test_map[sid]["item_code"] == item_code:
                cur.execute("""
                    SELECT r.report_id, r.report_no, r.status, r.file_path
                    FROM reports r
                    WHERE r.sample_id = %s
                    LIMIT 1
                """, (sid,))
                r_row = cur.fetchone()
                if r_row:
                    existing_report = {
                        "report_id": r_row[0],
                        "report_no": r_row[1],
                        "status": r_row[2],
                        "file_path": r_row[3],
                    }
                    break

        return {
            "sample_id": sample_id,
            "sample_no": sample_no_db,
            "request_id": request_id,
            "request_no": request_no,
            "project_id": project_id,
            "status": status,
            "item_code": item_code,
            "test_name": test_name,
            "sample_count": len(test_samples),
            "test_samples": test_samples,
            "report_exists": existing_report is not None,
            "existing_report": existing_report,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error fetching sample info: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.get("/samples/by-number/{sample_no}/download-populated-template")
async def download_populated_template_by_sample(
    sample_no: str,
    user_id: Optional[int] = None
):
    """Download populated template based on sample number"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
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
        
        # Get all samples for THIS TEST TYPE ONLY
        test_samples = []
        for sample_id_key, test_data in sample_to_test_map.items():
            if test_data.get("item_code") == item_code:
                cur.execute("SELECT sample_no FROM samples WHERE sample_id = %s", (sample_id_key,))
                sample_row = cur.fetchone()
                if sample_row:
                    test_samples.append(sample_row[0])
        
        # Check if report already exists for this test type
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
        
        # Use existing report number if available, otherwise generate a preview
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
        
        # Prepare template data
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
        
        # Populate the template with data
        populated_path = populate_report_template_from_url(supabase_template_url, template_data)
        
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
        
    except Exception as e:
        raise HTTPException(500, f"Error generating populated template: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.get("/files/{report_id}/download")
async def download_report_file_direct(report_id: int):
    """Directly download the report file from Supabase"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Get report file details
        cur.execute("""
            SELECT r.file_path, r.original_filename, r.file_type, r.report_no
            FROM reports r
            WHERE r.report_id = %s
        """, (report_id,))
        
        report = cur.fetchone()
        if not report:
            raise HTTPException(404, "Report not found")
        
        file_path, original_filename, file_type, report_no = report
        
        if not file_path or not file_path.startswith('http'):
            raise HTTPException(404, "Report file not found in cloud storage")
        
        # Download the file from Supabase
        try:
            # Extract the path from the URL
            # Handle different URL formats:
            # Format 1: https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/reports/reports/filename.pdf
            # Format 2: https://hqwgkmbjmcxpxbwccclo.supabase.co/storage/v1/object/public/reports/filename.pdf
            
            # Try to extract path for reports bucket
            if '/public/reports/' in file_path:
                storage_path = file_path.split('/public/reports/')[1]
            elif '/object/public/reports/' in file_path:
                storage_path = file_path.split('/object/public/reports/')[1]
            else:
                # If we can't parse the URL, fall back to redirect
                print(f"Could not parse storage path from URL: {file_path}")
                from fastapi.responses import RedirectResponse
                return RedirectResponse(url=file_path)
            
            print(f"Extracted storage path: {storage_path}")
            
            # Download from Supabase using the reports bucket
            response = supabase.storage.from_("reports").download(storage_path)
            
            # Determine content type
            content_types = {
                'pdf': 'application/pdf',
                'doc': 'application/msword',
                'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'xls': 'application/vnd.ms-excel',
                'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            }
            
            media_type = content_types.get(file_type.lower(), 'application/octet-stream')
            
            # Create filename for download
            clean_report_no = report_no.replace(' ', '_').replace('-', '_')
            # Use original filename if available, otherwise create one
            if original_filename:
                # Ensure we have a valid filename with extension
                base_name = original_filename
            else:
                base_name = f"report.{file_type}"
            
            filename = f"{clean_report_no}_{base_name}"
            
            return Response(
                content=response,
                media_type=media_type,
                headers={'Content-Disposition': f'attachment; filename="{filename}"'}
            )
            
        except Exception as e:
            print(f"Error downloading from Supabase: {e}")
            import traceback
            traceback.print_exc()
            
            # Fallback: redirect to the URL
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=file_path)
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in download_report_file_direct: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Error downloading report: {str(e)}")
    finally:
        cur.close()
        conn.close()