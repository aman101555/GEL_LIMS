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

# This 'supabase' object is what you'll use to upload files
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# FIXED: Remove duplicate prefix - just use prefix="/projects"
router = APIRouter(prefix="/projects", tags=["Projects"])

# 4. Save 'public_url' in your PostgreSQL table instead of the local path
# This way, ViewInvoices.jsx can just click the link!

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



class ProjectStatusUpdate(BaseModel):
    status: str
    halted_date: Optional[str] = None


# ------------------------------
# LIST PROJECTS - FIXED
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
                   q.quotation_no, c.name as client_name
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
                "lpo_date": str(r[7]) if r[7] else None,  # Convert date to string
                "division": r[8],
                "status": r[9],
                "created_at": str(r[10]) if r[10] else None,
                "quotation_no": r[11],
                "client_name": r[12]
            }
            for r in rows
        ]

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()



################Project Creation#############
@router.post("/", response_model=ProjectOut)
def create_project(payload: ProjectCreate):
    """Create a new project from approved quotation"""
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Get quotation details
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

        # Use provided project name/location or fall back to enquiry values
        project_name = payload.project_name if payload.project_name != "string" else enquiry_project_name
        location = payload.location if payload.location != "string" else enquiry_location

        # Get current year's last two digits
        year_last_two = datetime.utcnow().strftime("%y")
        
        # Find the latest project number to increment from 16732
        cur.execute("""
            SELECT project_no 
            FROM projects 
            WHERE project_no LIKE 'LP/%'
            ORDER BY project_id DESC 
            LIMIT 1
        """)
        
        last_project = cur.fetchone()
        if last_project:
            # Extract the middle number from format LP/16732/25/DXB
            last_number = int(last_project[0].split('/')[1])
            next_number = last_number + 1
        else:
            # Start from 16732 if no projects exist yet
            next_number = 16732
        
        # Format: LP/16732/25/DXB
        project_no = f"LP/{next_number}/{year_last_two}/DXB"

        # Parse LPO date if provided
        lpo_date = None
        if payload.lpo_date and payload.lpo_date != "string":
            lpo_date = datetime.strptime(payload.lpo_date, "%Y-%m-%d").date()

        # Insert the project
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
# UPLOAD LPO FILE
# ------------------------------
@router.post("/{project_id}/upload-lpo")
async def upload_lpo_file(project_id: int, file: UploadFile = File(...)):
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Check if project exists
        cur.execute("SELECT project_id FROM projects WHERE project_id = %s", (project_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, "Project not found")

        # 1. Prepare file info
        file_content = await file.read()
        extension = file.filename.split(".")[-1]
        # Store in a subfolder named 'lpos' inside the bucket
        cloud_filename = f"lpos/LPO_{project_id}.{extension}"

        # 2. Upload to Supabase Storage (Bucket name: "projects")
        # Ensure you created a bucket named 'projects' in Supabase dashboard first!
        upload_response = supabase.storage.from_("projects").upload(
            path=cloud_filename,
            file=file_content,
            file_options={"content-type": file.content_type, "x-upsert": "true"}
        )

        # 3. Get the Public URL
        public_url = supabase.storage.from_("projects").get_public_url(cloud_filename)

        # 4. Update Database with the URL instead of just the filename
        cur.execute("""
            UPDATE projects 
            SET lpo_file = %s 
            WHERE project_id = %s
        """, (public_url, project_id))

        conn.commit()

        return {
            "message": "LPO uploaded to cloud successfully",
            "url": public_url
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Cloud Upload Error: {str(e)}")
    finally:
        cur.close()
        conn.close()

# ------------------------------
# DOWNLOAD LPO FILE (FROM ANY DEVICE)
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

        # row[0] is now the full Supabase URL (e.g., https://.../file.pdf)
        return {"download_url": row[0]}

    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()



# Add this endpoint after other endpoints
@router.patch("/{project_id}/status", summary="Update Project Status")
def update_project_status(project_id: int, payload: ProjectStatusUpdate):
    """Update project status (ACTIVE/INACTIVE)"""
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Check if project exists
        cur.execute("SELECT project_id FROM projects WHERE project_id = %s", (project_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, "Project not found")

        # Parse halted date if provided
        halted_date = None
        if payload.halted_date and payload.halted_date != "string":
            halted_date = datetime.strptime(payload.halted_date, "%Y-%m-%d").date()

        # Update project status and halted date
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

