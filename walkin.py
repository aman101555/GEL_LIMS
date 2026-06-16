"""
walkin.py
FastAPI router for Walk-In Customers module.

Reuses:
- LP number generation logic from projects.py (identical query pattern)
- Price catalog from quotations.py
- walk_in_items mirrors quotation_items structure

DB additions required (run once):
    ALTER TABLE projects
        ADD COLUMN IF NOT EXISTS is_walk_in       BOOLEAN DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS walk_in_client    TEXT,
        ADD COLUMN IF NOT EXISTS walk_in_phone     TEXT,
        ADD COLUMN IF NOT EXISTS walk_in_email     TEXT;

    CREATE TABLE IF NOT EXISTS walk_in_items (
        item_id        SERIAL PRIMARY KEY,
        project_id     INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
        description    TEXT NOT NULL,
        test_standard  TEXT,
        unit_rate      NUMERIC(12,2) NOT NULL DEFAULT 0,
        quantity       INTEGER NOT NULL DEFAULT 1,
        net_unit       TEXT,
        amount         NUMERIC(12,2) GENERATED ALWAYS AS (unit_rate * quantity) STORED,
        item_code      TEXT,
        created_at     TIMESTAMPTZ DEFAULT NOW()
    );
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from db import get_connection

router = APIRouter(prefix="/walkin", tags=["Walk-In Customers"])

VAT_RATE = 0.05

# ─── Pydantic models ──────────────────────────────────────────────────────────

class WalkInCreate(BaseModel):
    client_name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    division: Optional[str] = "MAT"   # default division for walk-ins


class WalkInItemCreate(BaseModel):
    description: str
    test_standard: Optional[str] = None
    unit_rate: float
    quantity: int = 1
    net_unit: Optional[str] = None
    item_code: Optional[str] = None


class WalkInItemFromCatalog(BaseModel):
    catalog_id: int
    quantity: int = 1


class WalkInItemUpdate(BaseModel):
    quantity:      Optional[int]   = None
    unit_rate:     Optional[float] = None
    test_standard: Optional[str]   = None
    net_unit:      Optional[str]   = None


class LPNumberUpdate(BaseModel):
    lp_number: str


# ─── Helper: generate LP number (identical logic to projects.py) ──────────────

def _generate_lp_number(cur) -> str:
    year_last_two = datetime.utcnow().strftime("%y")

    cur.execute("""
        SELECT project_no
        FROM projects
        WHERE project_no LIKE 'LP/%'
        ORDER BY project_id DESC
        LIMIT 1
    """)
    last = cur.fetchone()
    if last:
        try:
            last_number = int(last[0].split('/')[1])
            next_number = last_number + 1
        except (IndexError, ValueError):
            next_number = 16732
    else:
        next_number = 16732

    return f"LP/{next_number}/{year_last_two}/DXB"


# ─── Helper: recalculate totals on the project row ────────────────────────────

def _recalc_totals(cur, project_id: int):
    cur.execute("""
        SELECT COALESCE(SUM(amount), 0)
        FROM walk_in_items
        WHERE project_id = %s
    """, (project_id,))
    total = float(cur.fetchone()[0])
    vat = round(total * VAT_RATE, 2)
    grand_total = round(total * (1 + VAT_RATE), 2)
    return total, vat, grand_total


# ─── Helper: Copy walk_in_items to quotation_items ──────────────────────────────

def _copy_walkin_items_to_quotation(cur, project_id: int, quotation_id: int):
    """Copy walk_in_items to quotation_items for the new quotation"""
    # Don't include 'amount' - it's a generated column
    cur.execute("""
        INSERT INTO quotation_items (
            quotation_id, description, test_standard, 
            unit_rate, quantity, net_unit, item_code
        )
        SELECT 
            %s, description, test_standard,
            unit_rate, quantity, net_unit, item_code
        FROM walk_in_items
        WHERE project_id = %s
    """, (quotation_id, project_id))


# ─── 1. CREATE WALK-IN (no LP yet — just saves customer info) ────────────────

@router.post("/", summary="Create Walk-In Customer record")
def create_walk_in(payload: WalkInCreate):
    """
    Creates a shell project row tagged as walk-in.
    LP number is generated separately via POST /walkin/{project_id}/create-lpo.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO projects (
                project_no, quotation_id, client_id,
                project_name, location, division, status,
                is_walk_in, walk_in_client, walk_in_phone, walk_in_email
            )
            VALUES ('PENDING', NULL, NULL, %s, 'Walk-In', %s, 'PENDING',
                    TRUE, %s, %s, %s)
            RETURNING project_id
        """, (
            payload.client_name,
            payload.division,
            payload.client_name,
            payload.phone,
            payload.email,
        ))
        project_id = cur.fetchone()[0]
        conn.commit()
        return {
            "project_id": project_id,
            "client_name": payload.client_name,
            "division": payload.division,
            "message": "Walk-in customer created. Add tests and then generate LPO.",
        }
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ─── 2. GET WALK-IN DETAIL ────────────────────────────────────────────────────

@router.get("/{project_id}", summary="Get Walk-In detail")
def get_walk_in(project_id: int):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT project_id, project_no, project_name, division, status,
                   walk_in_client, walk_in_phone, walk_in_email,
                   is_walk_in, created_at, lpo_no, lpo_date
            FROM projects
            WHERE project_id = %s AND is_walk_in = TRUE
        """, (project_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Walk-in not found")

        cur.execute("""
            SELECT item_id, description, test_standard, unit_rate,
                   quantity, amount, net_unit, item_code
            FROM walk_in_items
            WHERE project_id = %s
            ORDER BY item_id
        """, (project_id,))
        items = [
            {
                "item_id": r[0], "description": r[1], "test_standard": r[2],
                "unit_rate": float(r[3]), "quantity": r[4],
                "amount": float(r[5]) if r[5] else 0.0,
                "net_unit": r[6], "item_code": r[7],
            }
            for r in cur.fetchall()
        ]

        total, vat, grand_total = _recalc_totals(cur, project_id)

        return {
            "project_id":   row[0],
            "project_no":   row[1],   # will be 'PENDING' until LPO created
            "client_name":  row[2],
            "division":     row[3],
            "status":       row[4],
            "walk_in_client": row[5],
            "walk_in_phone":  row[6],
            "walk_in_email":  row[7],
            "is_walk_in":   row[8],
            "created_at":   str(row[9]) if row[9] else None,
            "lpo_no":       row[10],
            "lpo_date":     str(row[11]) if row[11] else None,
            "items":        items,
            "total_amount": total,
            "vat":          vat,
            "grand_total":  grand_total,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ─── 3. LIST ALL WALK-INS ─────────────────────────────────────────────────────

@router.get("/", summary="List all Walk-In records")
def list_walk_ins(limit: int = 100, offset: int = 0):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT project_id, project_no, 
                   COALESCE(walk_in_client, project_name) as client_name,
                   division, status,
                   walk_in_client, walk_in_phone, walk_in_email, created_at
            FROM projects
            WHERE is_walk_in = TRUE
            ORDER BY project_id DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))
        rows = cur.fetchall()
        return [
            {
                "project_id":    r[0],
                "project_no":    r[1],
                "client_name":   r[2],
                "division":      r[3],
                "status":        r[4],
                "walk_in_client": r[5],
                "walk_in_phone":  r[6],
                "walk_in_email":  r[7],
                "created_at":    str(r[8]) if r[8] else None,
            }
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ─── 4. ADD ITEM (custom) ─────────────────────────────────────────────────────

@router.post("/{project_id}/items", summary="Add custom test item to walk-in")
def add_item(project_id: int, payload: WalkInItemCreate):
    conn = get_connection()
    cur = conn.cursor()
    try:
        # verify walk-in exists
        cur.execute("SELECT project_id FROM projects WHERE project_id = %s AND is_walk_in = TRUE", (project_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Walk-in not found")

        if payload.quantity <= 0:
            raise HTTPException(400, "Quantity must be > 0")
        if payload.unit_rate < 0:
            raise HTTPException(400, "Unit rate cannot be negative")

        cur.execute("""
            INSERT INTO walk_in_items
                (project_id, description, test_standard, unit_rate, quantity, net_unit, item_code)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING item_id
        """, (
            project_id, payload.description, payload.test_standard,
            payload.unit_rate, payload.quantity, payload.net_unit, payload.item_code
        ))
        item_id = cur.fetchone()[0]
        conn.commit()

        total, vat, grand_total = _recalc_totals(cur, project_id)

        return {
            "item_id": item_id,
            "message": "Item added",
            "totals": {"total_amount": total, "vat": vat, "grand_total": grand_total},
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ─── 5. ADD ITEM FROM PRICE CATALOG ──────────────────────────────────────────

@router.post("/{project_id}/items/from-catalog", summary="Add item from price catalog")
def add_item_from_catalog(project_id: int, payload: WalkInItemFromCatalog):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT project_id FROM projects WHERE project_id = %s AND is_walk_in = TRUE", (project_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Walk-in not found")

        cur.execute("""
            SELECT code, description, test_standard, unit_rate
            FROM price_catalog
            WHERE catalog_id = %s AND active = TRUE
        """, (payload.catalog_id,))
        cat = cur.fetchone()
        if not cat:
            raise HTTPException(404, "Catalog item not found")

        code, description, test_standard, unit_rate = cat

        if payload.quantity <= 0:
            raise HTTPException(400, "Quantity must be > 0")

        cur.execute("""
            INSERT INTO walk_in_items
                (project_id, description, test_standard, unit_rate, quantity, item_code)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING item_id
        """, (project_id, description, test_standard, unit_rate, payload.quantity, code))
        item_id = cur.fetchone()[0]
        conn.commit()

        total, vat, grand_total = _recalc_totals(cur, project_id)

        return {
            "item_id": item_id,
            "message": "Catalog item added",
            "totals": {"total_amount": total, "vat": vat, "grand_total": grand_total},
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ─── 6. UPDATE ITEM ───────────────────────────────────────────────────────────

@router.put("/{project_id}/items/{item_id}", summary="Update walk-in test item")
def update_item(project_id: int, item_id: int, payload: WalkInItemUpdate):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT item_id, unit_rate, quantity, test_standard, net_unit
            FROM walk_in_items
            WHERE item_id = %s AND project_id = %s
        """, (item_id, project_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Item not found")

        if payload.quantity is not None:
            if payload.quantity <= 0:
                raise HTTPException(400, "Quantity must be > 0")
            cur.execute("UPDATE walk_in_items SET quantity = %s WHERE item_id = %s", (payload.quantity, item_id))
        elif payload.unit_rate is not None:
            if payload.unit_rate < 0:
                raise HTTPException(400, "Unit rate cannot be negative")
            cur.execute("UPDATE walk_in_items SET unit_rate = %s WHERE item_id = %s", (payload.unit_rate, item_id))
        elif payload.test_standard is not None:
            cur.execute("UPDATE walk_in_items SET test_standard = %s WHERE item_id = %s", (payload.test_standard, item_id))
        elif payload.net_unit is not None:
            cur.execute("UPDATE walk_in_items SET net_unit = %s WHERE item_id = %s", (payload.net_unit, item_id))
        else:
            raise HTTPException(400, "Nothing to update")

        conn.commit()
        total, vat, grand_total = _recalc_totals(cur, project_id)

        return {
            "message": "Item updated",
            "totals": {"total_amount": total, "vat": vat, "grand_total": grand_total},
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ─── 7. DELETE ITEM ───────────────────────────────────────────────────────────

@router.delete("/{project_id}/items/{item_id}", summary="Remove item from walk-in")
def delete_item(project_id: int, item_id: int):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            DELETE FROM walk_in_items
            WHERE item_id = %s AND project_id = %s
            RETURNING item_id
        """, (item_id, project_id))
        if not cur.fetchone():
            raise HTTPException(404, "Item not found")
        conn.commit()

        total, vat, grand_total = _recalc_totals(cur, project_id)
        return {
            "message": "Item deleted",
            "totals": {"total_amount": total, "vat": vat, "grand_total": grand_total},
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ─── 8. CREATE LPO (generate LP number) ──────────────────────────────────────

@router.post("/{project_id}/create-lpo", summary="Generate LP number for walk-in")
def create_lpo(project_id: int):
    """
    Generates the LP number and creates a quotation from walk_in_items.
    This makes walk-in projects work like regular projects.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT project_id, project_no, is_walk_in, division
            FROM projects
            WHERE project_id = %s AND is_walk_in = TRUE
        """, (project_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Walk-in not found")

        division = row[3] or "MAT"

        # Check if there are items
        cur.execute("SELECT COUNT(*) FROM walk_in_items WHERE project_id = %s", (project_id,))
        item_count = cur.fetchone()[0]
        if item_count == 0:
            raise HTTPException(400, "No tests added. Add at least one test before generating LPO.")

        # Generate LP number
        lp_number = _generate_lp_number(cur)
        
        # Create quotation
        cur.execute("""
            INSERT INTO quotations (quotation_no, status, division)
            VALUES (%s, 'APPROVED', %s)
            RETURNING quotation_id
        """, (f"WALKIN-{lp_number}", division))
        quotation_id = cur.fetchone()[0]
        
        # Copy items from walk_in_items to quotation_items (amount is generated)
        _copy_walkin_items_to_quotation(cur, project_id, quotation_id)

        # Update project with LP number and quotation_id
        cur.execute("""
            UPDATE projects
            SET project_no = %s,
                status = 'ACTIVE',
                lpo_date = CURRENT_DATE,
                quotation_id = %s
            WHERE project_id = %s
        """, (lp_number, quotation_id, project_id))
        
        # Update quotation totals
        cur.execute("""
            UPDATE quotations
            SET total_amount = sub.total,
                vat = sub.total * 0.05,
                grand_total = sub.total * 1.05
            FROM (
                SELECT COALESCE(SUM(unit_rate * quantity), 0) as total
                FROM quotation_items
                WHERE quotation_id = %s
            ) sub
            WHERE quotation_id = %s
        """, (quotation_id, quotation_id))
        
        conn.commit()

        return {
            "message": "LPO created successfully",
            "project_id": project_id,
            "lp_number": lp_number,
            "quotation_id": quotation_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ─── 9. UPDATE LP NUMBER (manual edit by authorised user) ────────────────────

@router.patch("/{project_id}/lp-number", summary="Manually override LP number")
def update_lp_number(project_id: int, payload: LPNumberUpdate):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE projects
            SET project_no = %s
            WHERE project_id = %s AND is_walk_in = TRUE
            RETURNING project_id, project_no
        """, (payload.lp_number, project_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Walk-in not found")
        conn.commit()
        return {"message": "LP number updated", "project_id": row[0], "lp_number": row[1]}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ─── 10. PRICE CATALOG (proxy — same source as quotations) ──────────────────
# NOTE: This route MUST be defined before /{project_id} to avoid FastAPI
# treating "catalog" as a project_id integer path parameter.

@router.get("/catalog/items", summary="Get active price catalog items")
def get_catalog():
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT catalog_id, code, description, test_standard, unit_rate, unit, group_name
            FROM price_catalog
            WHERE active = TRUE
            ORDER BY code
        """)
        return [
            {
                "catalog_id":    r[0],
                "code":          r[1],
                "description":   r[2],
                "test_standard": r[3],
                "unit_rate":     float(r[4]),
                "unit":          r[5],
                "group_name":    r[6],
            }
            for r in cur.fetchall()
        ]
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


# ─── 11. DELETE WALK-IN (hard delete — cascades walk_in_items) ───────────────

@router.delete("/{project_id}", summary="Delete a Walk-In record permanently")
def delete_walk_in(project_id: int):
    conn = get_connection()
    cur = conn.cursor()
    try:
        # walk_in_items will cascade due to ON DELETE CASCADE on project_id FK
        cur.execute("""
            DELETE FROM projects
            WHERE project_id = %s AND is_walk_in = TRUE
            RETURNING project_id, project_no
        """, (project_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Walk-in not found")
        conn.commit()
        return {
            "message":    f"Walk-in #{row[0]} ({row[1]}) deleted",
            "project_id": row[0],
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()