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
import time

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

_template_cache = {"files": None, "loaded_at": 0.0}
_TEMPLATE_CACHE_TTL_SECONDS = 300  # 5 minutes


def _get_available_template_filenames():
    """
    List the templates/reports folder in Supabase Storage ONCE (cached for
    a few minutes) instead of firing an HTTP HEAD request per possible
    filename per test. A single sample-group lookup with N tests used to
    make up to N * 7 blocking network calls; this brings it down to at
    most one list() call per cache window, with everything else answered
    from an in-memory set.
    """
    now = time.time()
    if _template_cache["files"] is None or (now - _template_cache["loaded_at"]) > _TEMPLATE_CACHE_TTL_SECONDS:
        try:
            entries = supabase.storage.from_("templates").list("reports")
            _template_cache["files"] = {e["name"] for e in entries if e.get("name")}
        except Exception:
            # Don't cache a failure — retry on the next call instead of
            # silently reporting "no templates" for 5 minutes.
            return set()
        _template_cache["loaded_at"] = now
    return _template_cache["files"]


def get_template_from_supabase(item_code: str, test_name: str):
    """Get template from Supabase storage (checked against a cached folder listing)."""
    possible_filenames = [
        f"{item_code}_Report.xlsx",
        f"{item_code}_Report.docx", 
        f"{item_code}_Report.pdf",
        f"{item_code}.xlsx",
        f"{item_code}.docx",
        f"{test_name.replace(' ', '_')}_Report.xlsx",
        f"{test_name.replace(' ', '_')}_Report.docx"
    ]

    available = _get_available_template_filenames()
    for filename in possible_filenames:
        if filename in available:
            template_url = f"{SUPABASE_STORAGE_URL}/reports/{filename}"
            return template_url, filename.split('.')[-1]

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


def resolve_physical_key(cur, sample_input: str):
    """
    Given anything the user typed or clicked — a full test sample_no
    (e.g. 'GS-060726-02/3-1') or a bare physical sample_no
    (e.g. 'GS-060726-02/3') — resolve it to the physical_sample_no that
    groups every test recorded under that physical sample. Falls back to
    the row's own sample_no for legacy rows with no physical_sample_no.
    """
    cur.execute("""
        SELECT COALESCE(physical_sample_no, sample_no)
        FROM samples
        WHERE sample_no = %s OR physical_sample_no = %s
        LIMIT 1
    """, (sample_input, sample_input))
    row = cur.fetchone()
    return row[0] if row else None


def get_physical_sample_group(cur, physical_key: str):
    """
    Fetch every test (samples table row) recorded under one physical
    sample, along with each test's report status. A physical sample may
    carry one or more tests (e.g. '.../3-1', '.../3-2' share physical
    sample '.../3'); legacy rows without physical_sample_no are treated
    as their own single-test group.
    """
    cur.execute("""
        SELECT s.sample_id, s.sample_no, s.request_id, s.status,
               s.assigned_item_code, s.assigned_test_name, s.test_standard,
               s.physical_sample_no, s.received_date,
               tr.request_no, tr.project_id
        FROM samples s
        JOIN test_requests tr ON s.request_id = tr.test_request_id
        WHERE COALESCE(s.physical_sample_no, s.sample_no) = %s
        ORDER BY s.sample_id
    """, (physical_key,))
    rows = cur.fetchall()
    if not rows:
        return None

    request_id = rows[0][2]

    # Legacy rows created before assigned_item_code/assigned_test_name
    # existed need the old order-based inference as a fallback.
    fallback_map = {}
    if any(r[4] is None or r[5] is None for r in rows):
        fallback_map, _ = get_test_distribution_for_request(request_id, cur)

    tests = []
    for r in rows:
        (sample_id, sample_no, req_id, status, item_code, test_name,
         test_standard, phys_no, received_date, request_no, project_id) = r

        if not item_code or not test_name:
            fb = fallback_map.get(sample_id, {})
            item_code = item_code or fb.get("item_code", "UNKNOWN")
            test_name = test_name or fb.get("test_name", "Unknown Test")

        cur.execute("""
            SELECT report_id, report_no, status
            FROM reports
            WHERE sample_id = %s
            LIMIT 1
        """, (sample_id,))
        report_row = cur.fetchone()

        template_url, template_ext = get_template_from_supabase(item_code, test_name)

        tests.append({
            "sample_id": sample_id,
            "sample_no": sample_no,
            "item_code": item_code,
            "test_name": test_name,
            "test_standard": test_standard,
            "sample_status": status,
            "is_reported": report_row is not None,
            "report": {
                "report_id": report_row[0],
                "report_no": report_row[1],
                "status": report_row[2],
            } if report_row else None,
            "template_available": template_url is not None,
        })

    first = rows[0]
    return {
        "physical_sample_no": first[7] or first[1],
        "request_id": request_id,
        "request_no": first[9],
        "project_id": first[10],
        "received_date": first[8],
        "tests": tests,
        "test_count": len(tests),
        "reported_count": sum(1 for t in tests if t["is_reported"]),
        "pending_count": sum(1 for t in tests if not t["is_reported"]),
    }


def get_request_sample_groups(cur, request_id: int):
    """
    Fetch every physical sample under one test request, each with its own
    nested list of tests (samples table rows) and per-test report status —
    i.e. the same shape get_physical_sample_group() returns, but for every
    physical sample in the request at once instead of just one. Powers the
    request-first Step 2 screen: pick a Test Request, see every sample
    under it, pick specific unreported tests from any of them.
    """
    cur.execute("""
        SELECT s.sample_id, s.sample_no, s.request_id, s.status,
               s.assigned_item_code, s.assigned_test_name, s.test_standard,
               s.physical_sample_no, s.received_date,
               tr.request_no, tr.project_id
        FROM samples s
        JOIN test_requests tr ON s.request_id = tr.test_request_id
        WHERE s.request_id = %s
        ORDER BY s.sample_id
    """, (request_id,))
    rows = cur.fetchall()
    if not rows:
        return []

    # Legacy rows created before assigned_item_code/assigned_test_name
    # existed need the old order-based inference as a fallback.
    fallback_map = {}
    if any(r[4] is None or r[5] is None for r in rows):
        fallback_map, _ = get_test_distribution_for_request(request_id, cur)

    groups_by_key = {}
    order = []
    for r in rows:
        (sample_id, sample_no, req_id, status, item_code, test_name,
         test_standard, phys_no, received_date, request_no, project_id) = r

        if not item_code or not test_name:
            fb = fallback_map.get(sample_id, {})
            item_code = item_code or fb.get("item_code", "UNKNOWN")
            test_name = test_name or fb.get("test_name", "Unknown Test")

        key = phys_no or sample_no
        if key not in groups_by_key:
            groups_by_key[key] = {
                "physical_sample_no": key,
                "request_id": req_id,
                "request_no": request_no,
                "project_id": project_id,
                "received_date": received_date,
                "tests": [],
            }
            order.append(key)

        cur.execute("""
            SELECT report_id, report_no, status
            FROM reports
            WHERE sample_id = %s
            LIMIT 1
        """, (sample_id,))
        report_row = cur.fetchone()

        template_url, template_ext = get_template_from_supabase(item_code, test_name)

        groups_by_key[key]["tests"].append({
            "sample_id": sample_id,
            "sample_no": sample_no,
            "item_code": item_code,
            "test_name": test_name,
            "test_standard": test_standard,
            "sample_status": status,
            "is_reported": report_row is not None,
            "report": {
                "report_id": report_row[0],
                "report_no": report_row[1],
                "status": report_row[2],
            } if report_row else None,
            "template_available": template_url is not None,
        })

    groups = []
    for key in order:
        g = groups_by_key[key]
        tests = g["tests"]
        g["test_count"] = len(tests)
        g["reported_count"] = sum(1 for t in tests if t["is_reported"])
        g["pending_count"] = sum(1 for t in tests if not t["is_reported"])
        groups.append(g)
    return groups


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
            SELECT
                s.sample_id,
                s.sample_no,
                s.request_id,
                s.status,
                tr.request_no,
                COALESCE(qi.description, 'Unknown') AS test_name,
                COALESCE(qi.item_code,   'Unknown') AS item_code
            FROM samples s
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            LEFT JOIN test_request_items tri ON tri.test_request_id = s.request_id
            LEFT JOIN quotation_items qi     ON qi.item_id = tri.quotation_item_id
            WHERE LOWER(s.sample_no) LIKE LOWER(%s)
            ORDER BY s.created_at DESC
            LIMIT 10
        """, (f"%{sample_no}%",))

        rows = cur.fetchall()
        if not rows:
            raise HTTPException(404, "No samples found with that sample number")

        result = [
            {
                "sample_id":  r[0],
                "sample_no":  r[1],
                "request_id": r[2],
                "status":     r[3],
                "request_no": r[4],
                "test_name":  r[5],
                "item_code":  r[6],
            }
            for r in rows
        ]
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
        # Single query — join test info directly, no per-sample loop
        cur.execute("""
            SELECT
                s.sample_id,
                s.sample_no,
                s.request_id,
                s.status,
                tr.request_no,
                COALESCE(qi.description, 'Unknown')  AS test_name,
                COALESCE(qi.item_code,   'Unknown')  AS item_code
            FROM samples s
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            LEFT JOIN test_request_items tri ON tri.test_request_id = s.request_id
            LEFT JOIN quotation_items qi     ON qi.item_id = tri.quotation_item_id
            WHERE s.sample_no LIKE 'GS%%'
            ORDER BY s.sample_id DESC
            LIMIT 10
        """)

        rows = cur.fetchall()
        result = [
            {
                "sample_id":   r[0],
                "sample_no":   r[1],
                "request_id":  r[2],
                "status":      r[3],
                "request_no":  r[4],
                "test_name":   r[5],
                "item_code":   r[6],
            }
            for r in rows
        ]
        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ---------------------------
# 2b. Get Latest 10 UNIQUE Physical Samples (with all tests nested)
# ---------------------------
@router.get("/samples/recent")
def get_recent_samples():
    """
    Latest 10 unique physical samples for the picker.

    This only needs to return summary info (physical_sample_no, request_no,
    test_count, pending_count) — the picker in CreateReport.jsx doesn't
    render per-test details or template availability for this list; that's
    fetched separately, per-sample, via /samples/group/{sample_input} once
    the user actually clicks a sample. So this endpoint does ONE query and
    never touches Supabase Storage (which was previously the bottleneck —
    the old version called get_physical_sample_group() per group, which in
    turn made an HTTP HEAD request to Supabase Storage per test, up to 7
    attempts each, purely to populate a field this list never displays).
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            WITH latest_groups AS (
                SELECT COALESCE(physical_sample_no, sample_no) AS phys_key,
                       MAX(sample_id) AS latest_sample_id
                FROM samples
                WHERE sample_no LIKE 'GS%%'
                GROUP BY COALESCE(physical_sample_no, sample_no)
                ORDER BY latest_sample_id DESC
                LIMIT 10
            )
            SELECT
                lg.phys_key,
                lg.latest_sample_id,
                tr.request_no,
                s.request_id,
                COUNT(s.sample_id) AS test_count,
                COUNT(s.sample_id) FILTER (WHERE r.report_id IS NULL) AS pending_count
            FROM latest_groups lg
            JOIN samples s ON COALESCE(s.physical_sample_no, s.sample_no) = lg.phys_key
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            LEFT JOIN reports r ON r.sample_id = s.sample_id
            GROUP BY lg.phys_key, lg.latest_sample_id, tr.request_no, s.request_id
            ORDER BY lg.latest_sample_id DESC
        """)
        rows = cur.fetchall()

        results = [
            {
                "physical_sample_no": r[0],
                "request_id": r[3],
                "request_no": r[2],
                "test_count": r[4],
                "pending_count": r[5],
                "reported_count": r[4] - r[5],
            }
            for r in rows
        ]
        return results

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ---------------------------
# 2b-2. Samples Progress — generated but not yet reported (Dashboard widget)
# ---------------------------
@router.get("/samples/in-progress")
def get_samples_in_progress(limit: int = 10):
    """
    Samples that have been generated (exist in the `samples` table) but
    have no row in `reports` yet — i.e. still being processed. Used by
    the Dashboard "Samples Progress" widget. One query, LEFT JOIN reports
    to exclude anything already reported, LEFT JOIN worksheets to know
    whether a worksheet has been created yet (drives the 3-step tracker:
    Sample Creation -> Worksheet Creation -> Report Creation).
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                s.sample_id,
                s.sample_no,
                COALESCE(s.physical_sample_no, s.sample_no) AS physical_sample_no,
                COALESCE(qi.item_code, s.assigned_item_code) AS item_code,
                COALESCE(qi.description, s.assigned_test_name) AS test_name,
                s.test_standard,
                s.status,
                s.received_date,
                tr.request_no,
                COALESCE(ws.worksheet_count, 0) AS worksheet_count,
                ws.latest_worksheet_status,
                s.department
            FROM samples s
            LEFT JOIN reports r ON r.sample_id = s.sample_id
            LEFT JOIN test_requests tr ON s.request_id = tr.test_request_id
            LEFT JOIN quotation_items qi ON s.assigned_quotation_item_id = qi.item_id
            LEFT JOIN (
                SELECT sample_id, COUNT(*) AS worksheet_count, MAX(status) AS latest_worksheet_status
                FROM worksheets
                GROUP BY sample_id
            ) ws ON ws.sample_id = s.sample_id
            WHERE r.report_id IS NULL
              AND s.status IS DISTINCT FROM 'REJECTED'
            ORDER BY s.received_date DESC NULLS LAST, s.sample_id DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()

        now = datetime.now()
        results = []
        for (sample_id, sample_no, physical_sample_no, item_code, test_name,
             test_standard, status, received_date, request_no,
             worksheet_count, latest_worksheet_status, department) in rows:
            sample_age_days = (now - received_date).days if received_date else None
            results.append({
                "sample_id": sample_id,
                "sample_no": sample_no,
                "physical_sample_no": physical_sample_no,
                "item_code": item_code,
                "test_name": test_name or "Unassigned Test",
                "test_standard": test_standard,
                "db_status": status,
                "status": "In Progress",
                "received_date": received_date.isoformat() if received_date else None,
                "sample_age_days": sample_age_days,
                "request_no": request_no,
                "has_worksheet": worksheet_count > 0,
                "worksheet_status": latest_worksheet_status,
                "sample_type": department,
            })
        return results

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ---------------------------
# 2c. Get Full Test List for One Physical Sample
# ---------------------------
@router.get("/samples/group/{sample_input:path}")
def get_sample_group(sample_input: str):
    """
    Resolve whatever the user typed or clicked (a specific test's
    sample_no, or a bare physical sample_no) to its physical sample, and
    return every test recorded under it with report status per test.
    Used by CreateReport Step 2 to drive individual/select-all reporting.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        phys_key = resolve_physical_key(cur, sample_input)
        if not phys_key:
            raise HTTPException(404, f"Sample \"{sample_input}\" not found. Please check the sample number.")

        group = get_physical_sample_group(cur, phys_key)
        if not group:
            raise HTTPException(404, f"Sample \"{sample_input}\" not found. Please check the sample number.")

        return group

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error fetching sample group: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ---------------------------
# 2d. Get Latest 10 Test Requests (Step 1 of the request-first workflow)
# ---------------------------
@router.get("/requests/recent")
def get_recent_requests():
    """
    Last 10 test requests for Step 1 of Create/Upload Report. Each entry
    summarizes how many physical samples/tests it has and how many tests
    are still pending a report, so the picker can be scanned at a glance
    before the user drills into Step 2.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT tr.test_request_id, tr.request_no, tr.status, tr.created_at,
                   COUNT(DISTINCT COALESCE(s.physical_sample_no, s.sample_no)) AS sample_count,
                   COUNT(s.sample_id) AS test_count,
                   COUNT(s.sample_id) FILTER (WHERE r.report_id IS NULL) AS pending_count
            FROM test_requests tr
            JOIN samples s ON s.request_id = tr.test_request_id
            LEFT JOIN reports r ON r.sample_id = s.sample_id
            WHERE s.sample_no LIKE 'GS%%'
            GROUP BY tr.test_request_id, tr.request_no, tr.status, tr.created_at
            ORDER BY tr.created_at DESC NULLS LAST, tr.test_request_id DESC
            LIMIT 10
        """)
        rows = cur.fetchall()
        results = [
            {
                "test_request_id": r[0],
                "request_no": r[1],
                "status": r[2],
                "created_at": r[3],
                "sample_count": r[4],
                "test_count": r[5],
                "pending_count": r[6],
                "reported_count": r[5] - r[6],
            }
            for r in rows
        ]
        return results

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ---------------------------
# 2e. Search Test Requests by Request No (GQ format)
# ---------------------------
@router.get("/requests/search")
def search_requests(request_no: str):
    """Search for a test request by request number, for the Step 1 search box."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT tr.test_request_id, tr.request_no, tr.status, tr.created_at
            FROM test_requests tr
            WHERE LOWER(tr.request_no) LIKE LOWER(%s)
            ORDER BY tr.created_at DESC
            LIMIT 10
        """, (f"%{request_no}%",))

        rows = cur.fetchall()
        if not rows:
            raise HTTPException(404, "No test requests found with that request number")

        return [
            {
                "test_request_id": r[0],
                "request_no": r[1],
                "status": r[2],
                "created_at": r[3],
            }
            for r in rows
        ]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error searching test requests: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ---------------------------
# 2f. Get Every Sample (+ its tests) Under One Test Request
# ---------------------------
@router.get("/requests/{request_input:path}/samples")
def get_request_samples(request_input: str):
    """
    Step 2 of the request-first workflow: given a test request (either its
    numeric test_request_id, as clicked from the recent list, or its
    request_no text, e.g. 'GQ-060726-04'), return every physical sample
    under it with its nested tests and per-test report status. A sample
    with exactly one test is meant to be auto-selected by the frontend;
    a sample with several tests lets the user pick any subset of the
    ones that aren't already reported.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        if request_input.isdigit():
            cur.execute("""
                SELECT test_request_id, request_no, project_id
                FROM test_requests WHERE test_request_id = %s
            """, (int(request_input),))
        else:
            cur.execute("""
                SELECT test_request_id, request_no, project_id
                FROM test_requests WHERE request_no = %s
            """, (request_input,))
        req_row = cur.fetchone()
        if not req_row:
            raise HTTPException(404, f"Test request \"{request_input}\" not found. Please check the request number.")
        request_id, request_no, project_id = req_row

        groups = get_request_sample_groups(cur, request_id)
        if not groups:
            raise HTTPException(404, f"Test request \"{request_no}\" has no samples yet.")

        total_tests = sum(g["test_count"] for g in groups)
        total_pending = sum(g["pending_count"] for g in groups)

        return {
            "test_request_id": request_id,
            "request_no": request_no,
            "project_id": project_id,
            "samples": groups,
            "sample_count": len(groups),
            "test_count": total_tests,
            "pending_count": total_pending,
            "reported_count": total_tests - total_pending,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error fetching request samples: {str(e)}")
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
    sample_ids: List[int] = Form(...),
    uploaded_by: int = Form(...),
    file: UploadFile = File(...),
    notes: Optional[str] = Form(None),
    user_role: Optional[str] = Form(None)
):
    """
    Upload a completed report file covering one or more explicitly
    selected tests (samples table rows) — either a single test or every
    test under a physical sample, per the user's Step 2 selection.
    Any test that already has a report is rejected up front so the same
    test can never be reported twice.
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        if not sample_ids:
            raise HTTPException(400, "Select at least one test to report")

        print(f"Starting report upload for sample_ids: {sample_ids}")

        # Load the selected test rows
        cur.execute("""
            SELECT sample_id, sample_no, request_id, assigned_item_code, assigned_test_name
            FROM samples
            WHERE sample_id = ANY(%s)
        """, (sample_ids,))
        rows = cur.fetchall()

        found_ids = {r[0] for r in rows}
        missing = set(sample_ids) - found_ids
        if missing:
            raise HTTPException(404, f"Sample(s) not found: {sorted(missing)}")

        # Fill in item_code/test_name for legacy rows via the order-based
        # fallback (grouped by request_id, computed once per request).
        fallback_map = {}
        request_ids_needing_fallback = {r[2] for r in rows if not r[3] or not r[4]}
        for rid in request_ids_needing_fallback:
            m, _ = get_test_distribution_for_request(rid, cur)
            fallback_map.update(m)

        tests_selected = []
        for sample_id, sample_no, request_id, item_code, test_name in rows:
            if not item_code or not test_name:
                fb = fallback_map.get(sample_id, {})
                item_code = item_code or fb.get("item_code", "UNKNOWN")
                test_name = test_name or fb.get("test_name", "Unknown Test")
            tests_selected.append({
                "sample_id": sample_id,
                "sample_no": sample_no,
                "item_code": item_code,
                "test_name": test_name,
            })
        # Keep a stable, predictable order for covers_samples / filenames
        tests_selected.sort(key=lambda t: t["sample_no"])

        print(f"Resolved {len(tests_selected)} test(s): {[t['sample_no'] for t in tests_selected]}")

        # Duplicate-reporting guard: none of the selected tests may
        # already have a report, regardless of role — a super_admin
        # replaces an EXISTING report through its own flow, not by
        # re-submitting here.
        cur.execute("""
            SELECT s.sample_no, r.report_no
            FROM reports r
            JOIN samples s ON r.sample_id = s.sample_id
            WHERE r.sample_id = ANY(%s)
        """, (sample_ids,))
        already_reported = cur.fetchall()
        if already_reported:
            details = ", ".join(f"{sno} (Report No: {rno})" for sno, rno in already_reported)
            raise HTTPException(
                400,
                f"The following test(s) have already been reported and can't be reported again: {details}. "
                f"Refresh the sample to see its current status."
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
        primary_item_code = tests_selected[0]["item_code"]
        cloud_filename = f"reports/{clean_report_no}_{primary_item_code}_{secrets.token_hex(4)}{file_extension}"

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

        # Dedup test names for a readable covers_test_type, preserving order
        distinct_test_names = []
        for t in tests_selected:
            if t["test_name"] not in distinct_test_names:
                distinct_test_names.append(t["test_name"])
        covers_test_type = ", ".join(distinct_test_names)
        covers_samples = [t["sample_no"] for t in tests_selected]

        # Insert one report row per selected test, all sharing report_no;
        # the first is the "main" row, the rest link back to it — same
        # pattern used elsewhere for multi-sample reports.
        print(f"Inserting report for sample {tests_selected[0]['sample_id']}")
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
            tests_selected[0]["sample_id"],
            file.filename,
            cloud_filename,
            public_url,
            file_extension[1:] if file_extension.startswith('.') else file_extension,
            uploaded_by,
            covers_test_type,
            covers_samples,
            notes
        ))

        main_report_id = cur.fetchone()[0]
        print(f"Created main report with ID: {main_report_id}")

        for i, t in enumerate(tests_selected[1:], 1):
            try:
                print(f"Linking report to sample {t['sample_id']} ({i+1}/{len(tests_selected)})")
                cur.execute("""
                    INSERT INTO reports (
                        report_no, sample_id, original_filename, 
                        stored_filename, file_path, file_type, uploaded_by, status,
                        covers_test_type, covers_samples, linked_to_report_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'DRAFT', %s, %s, %s)
                """, (
                    report_no,
                    t["sample_id"],
                    file.filename,
                    cloud_filename,
                    public_url,
                    file_extension[1:] if file_extension.startswith('.') else file_extension,
                    uploaded_by,
                    covers_test_type,
                    covers_samples,
                    main_report_id
                ))
            except Exception as link_error:
                print(f"Warning: Failed to link to sample {t['sample_id']}: {link_error}")
                # Continue with other samples

        conn.commit()
        print("Transaction committed successfully")

        return {
            "message": f"Report uploaded successfully to cloud for {covers_test_type}",
            "report_id": main_report_id,
            "report_no": report_no,
            "covers_test_type": covers_test_type,
            "covers_samples": covers_samples,
            "sample_count": len(tests_selected),
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
# 7. Get Reports with Status Filter - OPTIMISED
# ---------------------------
@router.get("")
def get_reports(status: Optional[str] = None):
    """Get reports with optional status filter - shows which test type they cover"""
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Single query: DISTINCT ON deduplicates by report_no, and a lateral
        # aggregate pulls all covered sample numbers in one pass — no per-row loop.
        query = """
            SELECT DISTINCT ON (r.report_no)
                r.*,
                s.sample_no,
                r.covers_test_type                      AS test_name,
                COALESCE(
                    (SELECT qi.item_code
                     FROM test_request_items tri
                     JOIN quotation_items qi ON tri.quotation_item_id = qi.item_id
                     JOIN samples s2 ON tri.test_request_id = s2.request_id
                     WHERE s2.sample_id = r.sample_id
                     LIMIT 1),
                    'N/A'
                )                                       AS item_code,
                u.username                              AS uploaded_by_username,
                uc.username                             AS checked_by_username,
                ua.username                             AS approved_by_username,
                -- All sample_nos that share this report_no, as a comma-separated string
                (
                    SELECT STRING_AGG(s3.sample_no, ',' ORDER BY s3.sample_no)
                    FROM reports r3
                    JOIN samples s3 ON r3.sample_id = s3.sample_id
                    WHERE r3.report_no = r.report_no
                )                                       AS covered_samples_csv
            FROM reports r
            LEFT JOIN samples s  ON r.sample_id  = s.sample_id
            LEFT JOIN users u    ON r.uploaded_by = u.user_id
            LEFT JOIN users uc   ON r.checked_by  = uc.user_id
            LEFT JOIN users ua   ON r.approved_by  = ua.user_id
            WHERE 1=1
        """
        params = []

        if status and status != "ALL":
            query += " AND r.status = %s"
            params.append(status)

        query += " ORDER BY r.report_no, r.created_at DESC"

        cur.execute(query, tuple(params))

        columns = [desc[0] for desc in cur.description]
        all_rows = cur.fetchall()

        reports = []
        for row in all_rows:
            report_dict = dict(zip(columns, row))
            # Convert the CSV string back to a list (or empty list if NULL)
            csv = report_dict.pop("covered_samples_csv", None)
            covered = csv.split(",") if csv else []
            report_dict["covered_samples"] = covered
            report_dict["sample_count"] = len(covered)
            reports.append(report_dict)

        return reports

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Error fetching reports: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ---------------------------
# 8. Get Report by Sample No - UPDATED
# ---------------------------
@router.get("/by-sample/{sample_no:path}")
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


@router.get("/samples/by-number/{sample_no:path}")
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


@router.get("/samples/by-number/{sample_no:path}/download-populated-template")
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


# ---------------------------
# 14b. Download Populated Template for a Selected GROUP of Tests
# ---------------------------
@router.get("/samples/group-download-template")
async def download_populated_template_for_group(
    sample_ids: str,
    user_id: Optional[int] = None
):
    """
    Same as download-populated-template above, but driven by an explicit
    set of selected tests (Step 2's checkboxes) instead of "every sample
    sharing this test type in the request". A template is only used when
    every selected test shares the same item_code — a template file maps
    to one test type, so a mixed selection falls back to the existing
    "no template" default behavior (manual upload in Step 3) rather than
    guessing which template to use.
    """
    ids = [int(x) for x in sample_ids.split(",") if x.strip()]
    if not ids:
        raise HTTPException(400, "No tests selected")

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT s.sample_id, s.sample_no, s.request_id,
                   s.assigned_item_code, s.assigned_test_name,
                   tr.request_no, tr.project_id
            FROM samples s
            JOIN test_requests tr ON s.request_id = tr.test_request_id
            WHERE s.sample_id = ANY(%s)
            ORDER BY s.sample_no
        """, (ids,))
        rows = cur.fetchall()

        found_ids = {r[0] for r in rows}
        missing = set(ids) - found_ids
        if missing:
            raise HTTPException(404, f"Sample(s) not found: {sorted(missing)}")

        request_id = rows[0][2]
        project_id = rows[0][6]
        request_no = rows[0][5]

        fallback_map = {}
        if any(r[3] is None or r[4] is None for r in rows):
            fallback_map, _ = get_test_distribution_for_request(request_id, cur)

        test_samples = []
        item_codes = set()
        test_names = []
        sample_id_by_no = {}
        for sample_id, sample_no, req_id, item_code, test_name, req_no, proj_id in rows:
            if not item_code or not test_name:
                fb = fallback_map.get(sample_id, {})
                item_code = item_code or fb.get("item_code", "UNKNOWN")
                test_name = test_name or fb.get("test_name", "Unknown Test")
            test_samples.append(sample_no)
            item_codes.add(item_code)
            if test_name not in test_names:
                test_names.append(test_name)
            sample_id_by_no[sample_no] = sample_id

        if len(item_codes) != 1:
            raise HTTPException(
                400,
                "Template download needs every selected test to be the same test type. "
                "Select tests of one type, or upload a manually prepared report instead."
            )
        item_code = item_codes.pop()
        test_name = test_names[0]

        # Check if a report already exists for any of the selected tests
        cur.execute("SELECT report_no FROM reports WHERE sample_id = ANY(%s) LIMIT 1", (ids,))
        existing_row = cur.fetchone()
        existing_report_no = existing_row[0] if existing_row else None

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
        tested_by = "Lab Chemist"
        if user_id:
            try:
                cur.execute("SELECT username, full_name FROM users WHERE user_id = %s", (user_id,))
                user_data = cur.fetchone()
                if user_data:
                    tested_by = user_data[1] if user_data[1] else user_data[0]
            except Exception as user_error:
                print(f"Error fetching user details: {user_error}")

        if existing_report_no:
            report_no_for_template = existing_report_no
        else:
            today = datetime.now()
            date_str = today.strftime("%d%m%y")
            cur.execute("SELECT COUNT(*) FROM reports WHERE DATE(created_at) = CURRENT_DATE")
            count = cur.fetchone()[0]
            report_seq = f"{count + 1:03d}"
            report_no_for_template = f"GR - {date_str} - {report_seq}"

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

        supabase_template_url, _ = get_template_from_supabase(item_code, test_name)
        if not supabase_template_url:
            supabase_template_url, _ = get_template_from_supabase(item_code_db, test_name_db)
        if not supabase_template_url:
            raise HTTPException(404, f"No template found for item code: {item_code}")

        populated_path = populate_report_template_from_url(supabase_template_url, template_data)

        if existing_report_no:
            download_filename = f"{existing_report_no.replace(' ', '_')}_{item_code}.xlsx"
        else:
            download_filename = f"{item_code}_Report_Template_{len(test_samples)}_samples.xlsx"

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