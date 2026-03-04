from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from db import get_connection
from datetime import datetime
from typing import Optional, List
import os
import shutil
import supabase

from supabase import create_client, Client

SUPABASE_URL = "https://hqwgkmbjmcxpxbwccclo.supabase.co"
SUPABASE_KEY = "sb_secret_-8uQCdQSiUgDFO_MUEsTWg_TPWtsyy3"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

router = APIRouter(prefix="/projects", tags=["Projects"])

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


class ProjectStatusUpdate(BaseModel):
    status: str
    halted_date: Optional[str] = None


# ------------------------------
# HELPER: Delete old LPO from Supabase
# ------------------------------
def delete_old_lpo_from_supabase(project_id: int):
    """
    Deletes ALL existing LPO files for this project_id from Supabase storage.
    Files are stored as lpos/LPO_{project_id}.{ext} - we list and delete any match.
    """
    try:
        # List all files in the lpos/ folder
        files = supabase.storage.from_(BUCKET_NAME).list("lpos")
        
        if not files:
            return  # Nothing to delete
        
        # Find files that belong to this project_id
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
        # Log but don't crash — upload will still proceed with upsert
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
                   q.quotation_no, c.name as client_name, p.lpo_file
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
                "lpo_file": r[13]
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

        year_last_two = datetime.utcnow().strftime("%y")
        
        cur.execute("""
            SELECT project_no 
            FROM projects 
            WHERE project_no LIKE 'LP/%'
            ORDER BY project_id DESC 
            LIMIT 1
        """)
        
        last_project = cur.fetchone()
        if last_project:
            last_number = int(last_project[0].split('/')[1])
            next_number = last_number + 1
        else:
            next_number = 16732
        
        project_no = f"LP/{next_number}/{year_last_two}/DXB"

        lpo_date = None
        if payload.lpo_date and payload.lpo_date != "string":
            lpo_date = datetime.strptime(payload.lpo_date, "%Y-%m-%d").date()

        cur.execute("""
            INSERT INTO projects (
                project_no, quotation_id, client_id, project_name,
                location, lpo_no, lpo_date, division, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE')
            RETURNING project_id
        """, (
            project_no,
            quotation_id,
            client_id,
            project_name,
            location,
            payload.lpo_no if payload.lpo_no != "string" else None,
            lpo_date,
            division
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
            "lpo_date": payload.lpo_date
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
@router.get("/{project_id}", summary="Get Project Details")
def get_project_details(project_id: int):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT p.project_id, p.project_no, p.quotation_id, p.client_id,
                   p.project_name, p.location, p.lpo_no, p.lpo_date,
                   p.lpo_file, p.division, p.status, p.created_at,
                   q.quotation_no, c.name as client_name
            FROM projects p
            LEFT JOIN quotations q ON p.quotation_id = q.quotation_id
            LEFT JOIN clients c ON p.client_id = c.client_id
            WHERE p.project_id = %s
        """, (project_id,))

        row = cur.fetchone()

        if not row:
            raise HTTPException(404, "Project not found")

        return {
            "project_id": row[0],
            "project_no": row[1],
            "quotation_id": row[2],
            "client_id": row[3],
            "project_name": row[4],
            "location": row[5],
            "lpo_no": row[6],
            "lpo_date": str(row[7]) if row[7] else None,
            "lpo_file": row[8],
            "division": row[9],
            "status": row[10],
            "created_at": str(row[11]) if row[11] else None,
            "quotation_no": row[12],
            "client_name": row[13]
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
            SET project_name = %s, location = %s, lpo_no = %s, lpo_date = %s
            WHERE project_id = %s
            RETURNING project_id, project_no
        """, (
            payload.project_name,
            payload.location,
            payload.lpo_no if payload.lpo_no != "string" else None,
            lpo_date,
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
        # 1. Verify project exists
        cur.execute("SELECT project_id, lpo_file FROM projects WHERE project_id = %s", (project_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "Project not found")

        existing_lpo_url = row[1]  # May be None if no LPO uploaded yet

        # 2. Read new file content
        file_content = await file.read()
        extension = file.filename.rsplit(".", 1)[-1].lower()
        new_cloud_path = f"lpos/LPO_{project_id}.{extension}"

        # 3. Delete ALL old LPO files for this project (handles different extensions)
        #    This ensures a clean replace even if the extension changed (pdf -> docx etc.)
        if existing_lpo_url:
            delete_old_lpo_from_supabase(project_id)

        # 4. Upload new file to Supabase Storage
        #    x-upsert: true overwrites if same path exists
        upload_response = supabase.storage.from_(BUCKET_NAME).upload(
            path=new_cloud_path,
            file=file_content,
            file_options={
                "content-type": file.content_type,
                "x-upsert": "true"
            }
        )

        # Check upload response for errors (supabase-py can return error dicts instead of raising)
        if isinstance(upload_response, dict):
            error = upload_response.get("error") or upload_response.get("message")
            if error:
                raise HTTPException(500, f"Supabase upload error: {error}")

        # 5. Build the public URL directly — most reliable across all supabase-py versions
        #    Avoids get_public_url() version inconsistencies entirely
        public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{new_cloud_path}"

        # 6. Save new URL to database
        cur.execute("""
            UPDATE projects 
            SET lpo_file = %s 
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