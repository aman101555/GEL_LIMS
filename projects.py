from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from db import get_connection
from datetime import datetime
from typing import Optional, List
import os
import shutil
import supabase

from supabase import create_client, Client
from template_processor import CoverSheetTemplateProcessor, WorkInstructionTemplateProcessor, BoreholeLogTemplateProcessor, SampleDescriptionTemplateProcessor

SUPABASE_URL = "https://hqwgkmbjmcxpxbwccclo.supabase.co"
SUPABASE_KEY = "sb_secret_-8uQCdQSiUgDFO_MUEsTWg_TPWtsyy3"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

router = APIRouter(prefix="/projects", tags=["Projects"])

# DB migration (run once):
# ALTER TABLE projects
#     ADD COLUMN IF NOT EXISTS contractor TEXT,
#     ADD COLUMN IF NOT EXISTS phone_no   TEXT;
#
# DB migration (run once) — LPO/quotation verification gate:
# ALTER TABLE projects
#     ADD COLUMN IF NOT EXISTS lpo_verified    BOOLEAN NOT NULL DEFAULT FALSE,
#     ADD COLUMN IF NOT EXISTS lpo_verified_at TIMESTAMP;

BUCKET_NAME = "projects"  # Supabase bucket name

# ------------------------------
# Pydantic Models
# ------------------------------
class ProjectCreate(BaseModel):
    quotation_id: int
    project_name: str
    location: str
    lpo_no: Optional[str] = None
    lpo_date: Optional[str] = None
    consultant: Optional[str] = None
    plot_no: Optional[str] = None
    client_name: Optional[str] = None
    contractor: Optional[str] = None   # Non Walk-In: contractor name
    phone_no: Optional[str] = None     # Non Walk-In: phone number

class ProjectOut(BaseModel):
    project_id: int
    project_no: str
    quotation_id: int
    client_id: Optional[int]
    project_name: str
    location: str
    status: str
    lpo_no: Optional[str] = None
    lpo_date: Optional[str] = None
    quotation_no: Optional[str] = None
    client_name: Optional[str] = None
    lpo_file: Optional[str] = None
    consultant: Optional[str] = None
    plot_no: Optional[str] = None
    contractor: Optional[str] = None
    phone_no: Optional[str] = None
    lpo_verified: bool = False
    lpo_verified_at: Optional[str] = None


class ProjectStatusUpdate(BaseModel):
    status: str
    halted_date: Optional[str] = None


class LpoVerifyPayload(BaseModel):
    verified: bool = True


# ------------------------------
# HELPER: Delete old LPO from Supabase
# ------------------------------
def delete_old_lpo_from_supabase(project_id: int):
    """
    Deletes ALL existing LPO files for this project_id from Supabase storage.
    Files are stored as lpos/LPO_{project_id}.{ext} - we list and delete any match.
    """
    try:
        files = supabase.storage.from_(BUCKET_NAME).list("lpos")

        if not files:
            return

        prefix = f"LPO_{project_id}."
        files_to_delete = [
            f"lpos/{f['name']}"
            for f in files
            if f.get("name", "").startswith(prefix)
        ]

        if files_to_delete:
            supabase.storage.from_(BUCKET_NAME).remove(files_to_delete)
            print(f"[LPO Replace] Deleted old files: {files_to_delete}")
        else:
            print(f"[LPO Replace] No existing LPO file found for project_id={project_id}")

    except Exception as e:
        print(f"[LPO Replace] Warning: Could not delete old LPO files: {e}")


# ------------------------------
# LIST PROJECTS
# ------------------------------
@router.get("/projects", response_model=List[ProjectOut])
def list_projects(limit: int = 100, offset: int = 0):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT p.project_id, p.project_no, p.quotation_id, p.client_id,
                   p.project_name, p.location, p.lpo_no, p.lpo_date,
                   p.division, p.status, p.created_at,
                   q.quotation_no, COALESCE(c.name, p.client_name) as client_name, p.lpo_file,
                   p.consultant, p.plot_no, p.contractor, p.phone_no,
                   p.lpo_verified, p.lpo_verified_at
            FROM projects p
            LEFT JOIN quotations q ON p.quotation_id = q.quotation_id
            LEFT JOIN clients c ON p.client_id = c.client_id
            ORDER BY p.project_id DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cur.fetchall()

        return [
            {
                "project_id": r[0],
                "project_no": r[1],
                "quotation_id": r[2],
                "client_id": r[3],
                "project_name": r[4],
                "location": r[5],
                "lpo_no": r[6],
                "lpo_date": str(r[7]) if r[7] else None,
                "division": r[8],
                "status": r[9],
                "created_at": str(r[10]) if r[10] else None,
                "quotation_no": r[11],
                "client_name": r[12],
                "lpo_file": r[13],
                "consultant": r[14],
                "plot_no": r[15],
                "contractor": r[16],
                "phone_no": r[17],
                "lpo_verified": bool(r[18]),
                "lpo_verified_at": str(r[19]) if r[19] else None,
            }
            for r in rows
        ]

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ------------------------------
# SUMMARY (dashboard only — no JOINs, minimal columns, fast)
# ------------------------------
@router.get("/summary")
def list_projects_summary():
    """Minimal project list for the dashboard. No JOINs — returns only what the
    dashboard home needs: counts, latest project, activity rate."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT project_id, project_no, project_name, status,
                   COALESCE(client_name, walk_in_client) AS client_name,
                   created_at, client_id
            FROM projects
            ORDER BY project_id DESC
            LIMIT 500
        """)
        rows = cur.fetchall()
        return [
            {
                "project_id":   r[0],
                "project_no":   r[1],
                "project_name": r[2],
                "status":       r[3],
                "client_name":  r[4],
                "created_at":   str(r[5]) if r[5] else None,
                "client_id":    r[6],
            }
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ------------------------------
# CREATE PROJECT
# ------------------------------
@router.post("/", response_model=ProjectOut)
def create_project(payload: ProjectCreate):
    """Create a new project from approved quotation"""
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT q.quotation_id, e.client_id, q.division,
                   e.project_name as enquiry_project_name,
                   e.location as enquiry_location
            FROM quotations q
            JOIN enquiries e ON q.enquiry_id = e.enquiry_id
            WHERE q.quotation_id = %s
        """, (payload.quotation_id,))
        row = cur.fetchone()

        if not row:
            raise HTTPException(404, "Quotation not found")

        quotation_id, client_id, division, enquiry_project_name, enquiry_location = row

        project_name = payload.project_name if payload.project_name != "string" else enquiry_project_name
        location = payload.location if payload.location != "string" else enquiry_location

        # Picks (highest LP/GSL number ever issued) + 1, checking both
        # `projects.project_no` and walk-in `quotations.quotation_no`.
        # LP and GSL share ONE running counter — GEO division projects get
        # the "GSL" prefix instead of "LP", but the number keeps counting
        # up from the same shared sequence (e.g. ...LP/16732, GSL/16733...).
        year_last_two = datetime.utcnow().strftime("%y")

        cur.execute(r"""
            SELECT MAX(num) FROM (
                SELECT (regexp_match(project_no, '^(?:LP|GSL)/(\d+)/'))[1]::int AS num
                FROM projects
                WHERE project_no ~ '^(?:LP|GSL)/\d+/'
                UNION ALL
                SELECT (regexp_match(quotation_no, '^WALKIN-(?:LP|GSL)/(\d+)/'))[1]::int AS num
                FROM quotations
                WHERE quotation_no ~ '^WALKIN-(?:LP|GSL)/\d+/'
            ) all_numbers
        """)
        max_existing = cur.fetchone()[0]
        next_number = (max_existing + 1) if max_existing else 16732

        prefix = "GSL" if division == "GEO" else "LP"
        project_no = f"{prefix}/{next_number}/{year_last_two}/DXB"

        lpo_date = None
        if payload.lpo_date and payload.lpo_date != "string":
            lpo_date = datetime.strptime(payload.lpo_date, "%Y-%m-%d").date()

        cur.execute("""
            INSERT INTO projects (
                project_no, quotation_id, client_id, project_name,
                location, lpo_no, lpo_date, division, status,
                consultant, plot_no, contractor, phone_no
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s, %s, %s)
            RETURNING project_id
        """, (
            project_no,
            quotation_id,
            client_id,
            project_name,
            location,
            payload.lpo_no if payload.lpo_no != "string" else None,
            lpo_date,
            division,
            payload.consultant if payload.consultant != "string" else None,
            payload.plot_no if payload.plot_no != "string" else None,
            payload.contractor if payload.contractor != "string" else None,
            payload.phone_no if payload.phone_no != "string" else None,
        ))

        project_id = cur.fetchone()[0]
        conn.commit()

        return {
            "project_id": project_id,
            "project_no": project_no,
            "quotation_id": quotation_id,
            "client_id": client_id,
            "project_name": project_name,
            "location": location,
            "status": "ACTIVE",
            "lpo_no": payload.lpo_no,
            "lpo_date": payload.lpo_date,
            "consultant": payload.consultant,
            "plot_no": payload.plot_no,
            "contractor": payload.contractor,
            "phone_no": payload.phone_no,
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ------------------------------
# GET PROJECT DETAILS
# ------------------------------
# ------------------------------
# GET PROJECT DETAILS
# ------------------------------
@router.get("/{project_id}", summary="Get Project Details")
def get_project_details(project_id: int):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT p.project_id, p.project_no, p.quotation_id, p.client_id,
                   p.project_name, p.location, p.lpo_no, p.lpo_date,
                   p.lpo_file, p.division, p.status, p.created_at,
                   q.quotation_no, COALESCE(c.name, p.client_name) as client_name,
                   c.address as client_address,
                   c.contact_person as client_contact_person,
                   p.consultant, p.plot_no, p.contractor, p.phone_no,
                   p.is_walk_in, p.walk_in_client, p.walk_in_phone, p.walk_in_email,
                   p.lpo_verified, p.lpo_verified_at
            FROM projects p
            LEFT JOIN quotations q ON p.quotation_id = q.quotation_id
            LEFT JOIN clients c ON p.client_id = c.client_id
            WHERE p.project_id = %s
        """, (project_id,))

        row = cur.fetchone()

        if not row:
            raise HTTPException(404, "Project not found")

        # Determine if this is a walk-in project
        is_walk_in = row[20] if row[20] is not None else False
        
        # For walk-in projects: use walk_in_client as contractor, walk_in_phone as phone_no
        # For non-walk-in: use the contractor and phone_no fields directly
        if is_walk_in:
            contractor_value = row[21] or row[18]  # walk_in_client first, fallback to contractor
            phone_value = row[22] or row[19]       # walk_in_phone first, fallback to phone_no
        else:
            contractor_value = row[18]  # contractor field
            phone_value = row[19]       # phone_no field

        return {
            "project_id":            row[0],
            "project_no":            row[1],
            "quotation_id":          row[2],
            "client_id":             row[3],
            "project_name":          row[4],
            "location":              row[5],
            "lpo_no":                row[6],
            "lpo_date":              str(row[7]) if row[7] else None,
            "lpo_file":              row[8],
            "division":              row[9],
            "status":                row[10],
            "created_at":            str(row[11]) if row[11] else None,
            "quotation_no":          row[12],
            "client_name":           row[13],
            "client_address":        row[14],
            "client_contact_person": row[15],
            "consultant":            row[16],
            "plot_no":               row[17],
            "contractor":            contractor_value,  # Mapped from walk_in_client for walk-ins
            "phone_no":              phone_value,       # Mapped from walk_in_phone for walk-ins
            "is_walk_in":            is_walk_in,
            "walk_in_client":        row[21],
            "walk_in_phone":         row[22],
            "walk_in_email":         row[23],
            "lpo_verified":          bool(row[24]),
            "lpo_verified_at":       str(row[25]) if row[25] else None,
        }

    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()

# ------------------------------
# UPDATE PROJECT
# ------------------------------
@router.put("/{project_id}", summary="Update Project")
def update_project(project_id: int, payload: ProjectCreate):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT project_id FROM projects WHERE project_id = %s", (project_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, "Project not found")

        lpo_date = None
        if payload.lpo_date and payload.lpo_date != "string":
            lpo_date = datetime.strptime(payload.lpo_date, "%Y-%m-%d").date()

        cur.execute("""
            UPDATE projects
            SET project_name = %s, location = %s, lpo_no = %s, lpo_date = %s,
                consultant = COALESCE(%s, consultant), plot_no = COALESCE(%s, plot_no),
                client_name = COALESCE(%s, client_name),
                contractor = COALESCE(%s, contractor),
                phone_no = COALESCE(%s, phone_no)
            WHERE project_id = %s
            RETURNING project_id, project_no
        """, (
            payload.project_name,
            payload.location,
            payload.lpo_no if payload.lpo_no != "string" else None,
            lpo_date,
            payload.consultant if payload.consultant != "string" else None,
            payload.plot_no if payload.plot_no != "string" else None,
            payload.client_name if payload.client_name != "string" else None,
            payload.contractor if payload.contractor != "string" else None,
            payload.phone_no if payload.phone_no != "string" else None,
            project_id
        ))

        result = cur.fetchone()
        conn.commit()

        return {
            "message": "Project updated successfully",
            "project_id": result[0],
            "project_no": result[1]
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ------------------------------
# UPLOAD / REPLACE LPO FILE
# ------------------------------
@router.post("/{project_id}/upload-lpo")
async def upload_lpo_file(project_id: int, file: UploadFile = File(...)):
    """
    Upload or REPLACE the LPO file for a project.
    - Deletes any existing LPO file(s) for this project from Supabase first.
    - Uploads the new file.
    - Updates the database with the new public URL.
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT project_id, lpo_file FROM projects WHERE project_id = %s", (project_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "Project not found")

        existing_lpo_url = row[1]

        file_content = await file.read()
        extension = file.filename.rsplit(".", 1)[-1].lower()
        new_cloud_path = f"lpos/LPO_{project_id}.{extension}"

        if existing_lpo_url:
            delete_old_lpo_from_supabase(project_id)

        upload_response = supabase.storage.from_(BUCKET_NAME).upload(
            path=new_cloud_path,
            file=file_content,
            file_options={
                "content-type": file.content_type,
                "x-upsert": "true"
            }
        )

        if isinstance(upload_response, dict):
            error = upload_response.get("error") or upload_response.get("message")
            if error:
                raise HTTPException(500, f"Supabase upload error: {error}")

        public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{new_cloud_path}"

        # Any new LPO file invalidates a prior verification — force re-review
        cur.execute("""
            UPDATE projects
            SET lpo_file = %s,
                lpo_verified = FALSE,
                lpo_verified_at = NULL
            WHERE project_id = %s
        """, (public_url, project_id))

        conn.commit()

        action = "replaced" if existing_lpo_url else "uploaded"

        return {
            "message": f"LPO file {action} successfully in cloud storage",
            "url": public_url,
            "file_name": file.filename,
            "action": action
        }

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Cloud Upload Error: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ------------------------------
# DOWNLOAD LPO FILE
# ------------------------------
@router.get("/{project_id}/download-lpo")
def download_lpo(project_id: int):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT lpo_file FROM projects WHERE project_id = %s", (project_id,))
        row = cur.fetchone()

        if not row or not row[0]:
            raise HTTPException(404, "LPO file link not found in database")

        return {"download_url": row[0]}

    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ------------------------------
# CONFIRM LPO + QUOTATION REVIEWED (gate before Test Requests)
# ------------------------------
@router.patch("/{project_id}/verify-lpo", summary="Confirm LPO & Quotation Reviewed For Accuracy")
def verify_lpo(project_id: int, payload: LpoVerifyPayload):
    """
    Sets/clears the confirmation that the LPO and quotation have been reviewed
    and verified for accuracy. A project cannot proceed to Test Requests until
    this is TRUE. Uploading a new/replacement LPO automatically clears it again
    (see upload_lpo_file), so it always reflects review of the *current* file.
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT lpo_file FROM projects WHERE project_id = %s", (project_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "Project not found")

        if payload.verified and not row[0]:
            raise HTTPException(400, "Cannot verify: no LPO file has been uploaded for this project yet")

        cur.execute("""
            UPDATE projects
            SET lpo_verified = %s,
                lpo_verified_at = CASE WHEN %s THEN NOW() ELSE NULL END
            WHERE project_id = %s
            RETURNING project_id, lpo_verified, lpo_verified_at
        """, (payload.verified, payload.verified, project_id))

        result = cur.fetchone()
        conn.commit()

        return {
            "message": "LPO verified" if result[1] else "LPO verification cleared",
            "project_id": result[0],
            "lpo_verified": result[1],
            "lpo_verified_at": str(result[2]) if result[2] else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ------------------------------
# UPDATE PROJECT STATUS
# ------------------------------
@router.patch("/{project_id}/status", summary="Update Project Status")
def update_project_status(project_id: int, payload: ProjectStatusUpdate):
    """Update project status (ACTIVE/INACTIVE)"""
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT project_id FROM projects WHERE project_id = %s", (project_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, "Project not found")

        halted_date = None
        if payload.halted_date and payload.halted_date != "string":
            halted_date = datetime.strptime(payload.halted_date, "%Y-%m-%d").date()

        cur.execute("""
            UPDATE projects
            SET status = %s,
                halted_date = %s
            WHERE project_id = %s
            RETURNING project_id, project_no, status, halted_date
        """, (
            payload.status,
            halted_date,
            project_id
        ))

        result = cur.fetchone()
        conn.commit()

        return {
            "message": f"Project status updated to {payload.status}",
            "project_id": result[0],
            "project_no": result[1],
            "status": result[2],
            "halted_date": str(result[3]) if result[3] else None
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ------------------------------
# DELETE PROJECT
# ------------------------------
@router.delete("/{project_id}", summary="Delete Project")
def delete_project(project_id: int):
    """Permanently delete a project and its associated LPO file from storage."""
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT project_id, project_no, lpo_file FROM projects WHERE project_id = %s", (project_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "Project not found")

        project_no = row[1]
        lpo_file = row[2]

        if lpo_file:
            delete_old_lpo_from_supabase(project_id)

        cur.execute("DELETE FROM projects WHERE project_id = %s", (project_id,))
        conn.commit()

        return {"message": f"Project {project_no} deleted successfully", "project_id": project_id}

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ------------------------------
# GENERATE COVER SHEET (GEO only)
# ------------------------------
@router.get("/{project_id}/cover-sheet", summary="Generate GEO Cover Sheet")
def generate_cover_sheet(project_id: int):
    """
    Generate and download a filled Cover Sheet for a GEO division project.
    Template fetched from Supabase: templates/cover_sheet/COVER_SHEET.docx
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT p.project_id, p.project_no, p.project_name, p.location,
                   p.division, p.status, p.created_at,
                   c.name            AS client_name,
                   c.address         AS client_address,
                   c.contact_person  AS client_contact_person
            FROM projects p
            LEFT JOIN clients c ON p.client_id = c.client_id
            WHERE p.project_id = %s
        """, (project_id,))

        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Project not found")

        division = row[4]
        if division != "GEO":
            raise HTTPException(400, "Cover Sheet is only available for GEO division projects")

        project_data = {
            "project_id":            row[0],
            "project_no":            row[1],
            "project_name":          row[2],
            "location":              row[3],
            "division":              row[4],
            "status":                row[5],
            "created_at":            row[6],
            "client_name":           row[7],
            "client_address":        row[8],
            "client_contact_person": row[9],
        }

        processor = CoverSheetTemplateProcessor()
        output = processor.process(project_data)

        safe_project_no = (project_data["project_no"] or "project").replace("/", "_")
        filename = f"CoverSheet_{safe_project_no}.docx"

        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ------------------------------
# GENERATE WORK INSTRUCTION SHEET (GEO only)
# ------------------------------
@router.get("/{project_id}/work-instruction", summary="Generate GEO Work Instruction Sheet")
def generate_work_instruction(project_id: int):
    """
    Generate and download a filled Work Instruction Sheet for a GEO division project.
    Template fetched from Supabase: templates/work_instruction/WORK_INSTRUCTION.docx

    Pulls:
      - Project + client details  (project_no, project_name, location, lpo_date,
                                   client name/address/phone/contact_person)
      - All quotation items tied to this project's quotation
        (description, test_standard, unit, quantity, notes)
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        # ── 1. Fetch project + client info ────────────────────────
        cur.execute("""
            SELECT p.project_id, p.project_no, p.project_name, p.location,
                   p.division,   p.lpo_date,   p.quotation_id,
                   c.name            AS client_name,
                   c.address         AS client_address,
                   c.phone           AS client_phone,
                   c.contact_person  AS client_contact_person
            FROM projects p
            LEFT JOIN clients c ON p.client_id = c.client_id
            WHERE p.project_id = %s
        """, (project_id,))

        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Project not found")

        division     = row[4]
        quotation_id = row[6]

        if division != "GEO":
            raise HTTPException(400, "Work Instruction Sheet is only available for GEO division projects")

        project_data = {
            "project_id":            row[0],
            "project_no":            row[1],
            "project_name":          row[2],
            "location":              row[3],
            "division":              row[4],
            "lpo_date":              row[5],   # raw date object — processor will format it
            "client_name":           row[7],
            "client_address":        row[8],
            "client_phone":          row[9],
            "client_contact_person": row[10],
        }

        # ── 2. Fetch quotation items ──────────────────────────────
        items = []
        if quotation_id:
            cur.execute("""
                SELECT item_id, description, test_standard, unit,
                       unit_rate, quantity, amount, notes, net_unit
                FROM quotation_items
                WHERE quotation_id = %s
                ORDER BY item_id
            """, (quotation_id,))

            for r in cur.fetchall():
                items.append({
                    "item_id":       r[0],
                    "description":   r[1] or "",
                    "test_standard": r[2] or "",
                    "unit":          r[3] or "",
                    "unit_rate":     float(r[4]) if r[4] is not None else 0.0,
                    "quantity":      r[5],
                    "amount":        float(r[6]) if r[6] is not None else 0.0,
                    "notes":         r[7] or "",
                    "net_unit":      r[8] or "",
                })

        # ── 3. Render template ────────────────────────────────────
        processor = WorkInstructionTemplateProcessor()
        output    = processor.process(project_data, items)

        safe_project_no = (project_data["project_no"] or "project").replace("/", "_")
        filename = f"WorkInstruction_{safe_project_no}.docx"

        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()

@router.get("/{project_id}/sample-description")
def download_sample_description(project_id: int):
    """Generate and download a Sample Description sheet."""
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT p.project_id, p.project_no, p.project_name, p.location,
                   p.division, p.status, p.created_at,
                   c.name AS client_name
            FROM projects p
            LEFT JOIN clients c ON p.client_id = c.client_id
            WHERE p.project_id = %s
        """, (project_id,))

        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Project not found")

        project_data = {
            "project_id":   row[0],
            "project_no":   row[1],
            "project_name": row[2],
            "location":     row[3],
            "division":     row[4],
            "status":       row[5],
            "created_at":   row[6],
            "client_name":  row[7],
        }

        processor = SampleDescriptionTemplateProcessor()
        output = processor.process(project_data)

        safe_project_no = (project_data["project_no"] or "project").replace("/", "_")
        filename = f"SampleDescription_{safe_project_no}.docx"

        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()
# ------------------------------
# GENERATE BOREHOLE LOG (GEO only)
# ------------------------------
@router.get("/{project_id}/borehole-log", summary="Generate GEO Borehole Log")
def generate_borehole_log(project_id: int):
    """
    Generate and download a filled Borehole Log for a GEO division project.
    Template fetched from Supabase: templates/borehole_log/BOREHOLE_LOG.docx

    Fills:
      - Client name
      - Project name
      - Site/Location
      - Project No.
    """
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT p.project_id, p.project_no, p.project_name, p.location,
                   p.division, p.status, p.created_at,
                   c.name AS client_name
            FROM projects p
            LEFT JOIN clients c ON p.client_id = c.client_id
            WHERE p.project_id = %s
        """, (project_id,))

        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Project not found")

        division = row[4]
        if division != "GEO":
            raise HTTPException(400, "Borehole Log is only available for GEO division projects")

        project_data = {
            "project_id":   row[0],
            "project_no":   row[1],
            "project_name": row[2],
            "location":     row[3],
            "division":     row[4],
            "status":       row[5],
            "created_at":   row[6],
            "client_name":  row[7],
        }

        processor = BoreholeLogTemplateProcessor()
        output = processor.process(project_data)

        safe_project_no = (project_data["project_no"] or "project").replace("/", "_")
        filename = f"BoreholeLog_{safe_project_no}.docx"

        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()