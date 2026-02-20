# enquiries.py - UPDATED VERSION
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import date, datetime
from typing import Optional, List
from db import get_connection

router = APIRouter(prefix="/enquiries", tags=["2. Enquiries"])

# ----------------------------
# Pydantic Models
# ----------------------------
class EnquiryCreate(BaseModel):
    client_id: int
    enquiry_ref: Optional[str] = None
    enquiry_date: Optional[date] = None
    project_name: Optional[str] = None
    location: Optional[str] = None
    notes: Optional[str] = None

class EnquiryOut(BaseModel):
    enquiry_id: int
    enquiry_ref: str
    client_id: int
    enquiry_date: Optional[date]
    project_name: Optional[str]
    location: Optional[str]
    status: str
    notes: Optional[str]

############################################
# Client Models for BOTH GET and POST
class ClientCreate(BaseModel):
    name: str
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None

class ClientOut(BaseModel):
    client_id: int
    name: str
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    created_at: Optional[datetime] = None

# ----------------------------
# CLIENT ENDPOINTS - UPDATED with POST
# ----------------------------
@router.get("/clients/", response_model=List[ClientOut])
def get_clients():
    """Get all clients for dropdown selection"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        # Query to get all clients (updated to include all fields)
        cur.execute("""
            SELECT client_id, name, contact_person, email, phone, address, created_at 
            FROM clients 
            ORDER BY name ASC
        """)
        
        clients = cur.fetchall()
        
        # Format response
        client_list = []
        for client in clients:
            client_list.append({
                "client_id": client[0],
                "name": client[1],
                "contact_person": client[2],
                "email": client[3],
                "phone": client[4],
                "address": client[5],
                "created_at": client[6]
            })
        
        return client_list
        
    except Exception as e:
        print(f"Error fetching clients: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


@router.post("/clients/", response_model=ClientOut)
def create_client(client: ClientCreate):
    """Create a new client"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            INSERT INTO clients (name, contact_person, email, phone, address, created_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            RETURNING client_id, name, contact_person, email, phone, address, created_at
        """, (
            client.name,
            client.contact_person,
            client.email,
            client.phone,
            client.address
        ))
        
        row = cur.fetchone()
        conn.commit()
        
        return {
            "client_id": row[0],
            "name": row[1],
            "contact_person": row[2],
            "email": row[3],
            "phone": row[4],
            "address": row[5],
            "created_at": row[6]
        }
        
    except Exception as e:
        conn.rollback()
        print(f"Error creating client: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


@router.get("/clients/{client_id}", response_model=ClientOut)
def get_client_by_id(client_id: int):
    """Get specific client by ID"""
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            SELECT client_id, name, contact_person, email, phone, address, created_at 
            FROM clients 
            WHERE client_id = %s
        """, (client_id,))
        
        client = cur.fetchone()
        
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        
        return {
            "client_id": client[0],
            "name": client[1],
            "contact_person": client[2],
            "email": client[3],
            "phone": client[4],
            "address": client[5],
            "created_at": client[6]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching client: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ... [REST OF YOUR CODE REMAINS THE SAME] ...

#####################################

# ----------------------------
# FIXED Generate Auto Enquiry Ref
# ----------------------------
def _generate_enquiry_ref(cur):
    year = datetime.utcnow().year
    # FIX: Check for existing enquiries in the current year
    cur.execute(
        "SELECT COUNT(*) FROM enquiries WHERE EXTRACT(YEAR FROM enquiry_date) = %s",
        (year,),
    )
    count_result = cur.fetchone()
    count = count_result[0] if count_result else 0
    count += 1
    return f"ENQ-{year}-{count:03d}"

# ----------------------------
# CREATE ENQUIRY - FIXED
# ----------------------------
@router.post("/", response_model=EnquiryOut)
def create_enquiry(payload: EnquiryCreate):
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Ensure client exists
        cur.execute("SELECT client_id FROM clients WHERE client_id = %s", (payload.client_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Client not found")

        # Default enquiry_date to today
        enquiry_date = payload.enquiry_date or date.today()

        # Generate ref if user didn't supply - FIXED LOGIC
        if payload.enquiry_ref:
            enquiry_ref = payload.enquiry_ref
        else:
            enquiry_ref = _generate_enquiry_ref(cur)

        print(f"Generated enquiry_ref: {enquiry_ref}")  # Debug log

        # Insert enquiry
        cur.execute(
            """
            INSERT INTO enquiries (
                client_id, enquiry_ref, enquiry_date, project_name, location, status, notes
            )
            VALUES (%s, %s, %s, %s, %s, 'OPEN', %s)
            RETURNING enquiry_id, enquiry_ref, client_id, enquiry_date, project_name, location, status, notes
            """,
            (
                payload.client_id,
                enquiry_ref,  # This should never be NULL now
                enquiry_date,
                payload.project_name,
                payload.location,
                payload.notes,
            ),
        )

        row = cur.fetchone()
        conn.commit()

        # Debug: Print what we're returning
        print(f"Returning row: {row}")

        return {
            "enquiry_id": row[0],
            "enquiry_ref": row[1] or "ERROR-MISSING-REF",  # Fallback if still NULL
            "client_id": row[2],
            "enquiry_date": row[3],
            "project_name": row[4],
            "location": row[5],
            "status": row[6],
            "notes": row[7],
        }

    except Exception as e:
        conn.rollback()
        print(f"Error in create_enquiry: {str(e)}")  # Debug log
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        cur.close()
        conn.close()

# ----------------------------
# LIST ENQUIRIES - FIXED with NULL handling
# ----------------------------
@router.get("/", response_model=List[EnquiryOut])
def list_enquiries(limit: int = 100, offset: int = 0):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT enquiry_id, enquiry_ref, client_id,
                   enquiry_date, project_name, location,
                   status, notes
            FROM enquiries
            ORDER BY enquiry_id DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )

        rows = cur.fetchall()

        enquiries = []
        for r in rows:
            # FIX: Handle NULL enquiry_ref
            enquiry_ref = r[1] or f"ENQ-{r[3].year if r[3] else datetime.now().year}-MISSING-{r[0]}"
            
            enquiries.append({
                "enquiry_id": r[0],
                "enquiry_ref": enquiry_ref,
                "client_id": r[2],
                "enquiry_date": r[3],
                "project_name": r[4],
                "location": r[5],
                "status": r[6],
                "notes": r[7],
            })

        return enquiries

    except Exception as e:
        print(f"Error in list_enquiries: {str(e)}")  # Debug log
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        cur.close()
        conn.close()


@router.get("/recent", response_model=List[EnquiryOut])
def recent_enquiries(limit: int = 10):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT enquiry_id, enquiry_ref, client_id,
                   enquiry_date, project_name,
                   location, status, notes
            FROM enquiries
            ORDER BY enquiry_id DESC
            LIMIT %s
        """, (limit,))

        return [
            {
                "enquiry_id": r[0],
                "enquiry_ref": r[1] or f"ENQ-{r[0]}",
                "client_id": r[2],
                "enquiry_date": r[3],
                "project_name": r[4],
                "location": r[5],
                "status": r[6],
                "notes": r[7],
            }
            for r in cur.fetchall()
        ]

    finally:
        cur.close()
        conn.close()


# ============================================================
# SEARCH ENQUIRIES
# ============================================================

@router.get("/search", response_model=List[EnquiryOut])
def search_enquiries(q: str):
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT e.enquiry_id, e.enquiry_ref, e.client_id,
                   e.enquiry_date, e.project_name,
                   e.location, e.status, e.notes
            FROM enquiries e
            JOIN clients c ON c.client_id = e.client_id
            WHERE
                e.enquiry_ref ILIKE %s OR
                e.project_name ILIKE %s OR
                e.location ILIKE %s OR
                c.name ILIKE %s
            ORDER BY e.enquiry_id DESC
        """, tuple([f"%{q}%"] * 4))

        return [
            {
                "enquiry_id": r[0],
                "enquiry_ref": r[1],
                "client_id": r[2],
                "enquiry_date": r[3],
                "project_name": r[4],
                "location": r[5],
                "status": r[6],
                "notes": r[7],
            }
            for r in cur.fetchall()
        ]

    finally:
        cur.close()
        conn.close()

# ----------------------------
# UPDATE ENQUIRY STATUS (unchanged)
# ----------------------------
@router.post("/{enquiry_id}/status")
def update_enquiry_status(enquiry_id: int, status: str):
    conn = get_connection()
    cur = conn.cursor()

    try:
        # Ensure enquiry exists
        cur.execute("SELECT enquiry_id FROM enquiries WHERE enquiry_id = %s", (enquiry_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Enquiry not found")

        # Update status
        cur.execute("UPDATE enquiries SET status = %s WHERE enquiry_id = %s", (status, enquiry_id))
        conn.commit()

        return {"message": "Status updated", "enquiry_id": enquiry_id, "status": status}

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        cur.close()
        conn.close()