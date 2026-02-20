# search.py
from fastapi import APIRouter, Query, HTTPException
from db import get_connection  # Use your existing PostgreSQL connection helper
import os

router = APIRouter()

# ----------------------------
# GLOBAL SEARCH ENDPOINT
# ----------------------------
@router.get("/search")
async def search_global(
    query: str = Query(..., min_length=3),
    limit: int = Query(7, ge=1, le=50)
):
    """
    Search across enquiries, quotations, projects, and invoices
    using the PostgreSQL 'gel_lims' database.
    """
    search_term = f"%{query.lower()}%"
    
    # Connect to PostgreSQL instead of SQLite
    conn = get_connection()
    cur = conn.cursor()

    results = {
        "enquiries": [],
        "quotations": [],
        "projects": [],
        "invoices": []
    }

    try:
        # 1. Enquiries - PostgreSQL uses %s for placeholders
        cur.execute("""
            SELECT 
                enquiry_id, enquiry_ref, project_name, 
                client_id, status, enquiry_date, location
            FROM enquiries
            WHERE LOWER(enquiry_ref) LIKE %s 
               OR LOWER(project_name) LIKE %s
            ORDER BY enquiry_date DESC
            LIMIT %s
        """, (search_term, search_term, limit))
        
        colnames = [desc[0] for desc in cur.description]
        results["enquiries"] = [dict(zip(colnames, row)) for row in cur.fetchall()]

        # 2. Quotations
        cur.execute("""
            SELECT 
                quotation_id, quotation_no, prepared_under, 
                status, grand_total, created_at
            FROM quotations
            WHERE LOWER(quotation_no) LIKE %s 
               OR LOWER(prepared_under) LIKE %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (search_term, search_term, limit))
        
        colnames = [desc[0] for desc in cur.description]
        results["quotations"] = [dict(zip(colnames, row)) for row in cur.fetchall()]

        # 3. Projects
        cur.execute("""
            SELECT 
                project_id, project_no, project_name, 
                location, lpo_no, status, created_at
            FROM projects
            WHERE LOWER(project_no) LIKE %s 
               OR LOWER(project_name) LIKE %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (search_term, search_term, limit))
        
        colnames = [desc[0] for desc in cur.description]
        results["projects"] = [dict(zip(colnames, row)) for row in cur.fetchall()]

        # 4. Invoices (Added logic)
        cur.execute("""
            SELECT 
                invoice_id, invoice_no, invoice_type, 
                grand_total, payment_status, created_at
            FROM invoices
            WHERE LOWER(invoice_no) LIKE %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (search_term, limit))
        
        colnames = [desc[0] for desc in cur.description]
        results["invoices"] = [dict(zip(colnames, row)) for row in cur.fetchall()]

    except Exception as e:
        print(f"Error during Global Search: {e}")
        # Optionally: raise HTTPException(500, detail=str(e))
    finally:
        cur.close()
        conn.close()

    return results