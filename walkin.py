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
        ADD COLUMN IF NOT EXISTS walk_in_email     TEXT,
        ADD COLUMN IF NOT EXISTS consultant        TEXT,
        ADD COLUMN IF NOT EXISTS plot_no           TEXT,
        ADD COLUMN IF NOT EXISTS client_name       TEXT;

    -- consultant / plot_no / client_name live on `projects` (not on a
    -- PA-only table) on purpose: they're general project attributes
    -- Payment Advice, Invoice, Cover Sheet, etc. can all read from the
    -- same place. `project_name` already existed on `projects` and is
    -- reused here as the LP's actual project name (previously it was
    -- always set equal to the client name for walk-ins — see
    -- create_walk_in() below, now decoupled).
    --
    -- `client_name` is the actual project Client/Owner — distinct from
    -- `walk_in_client` (labelled "Contractor" in the UI, the counter
    -- party who actually walks in with the samples). For regular
    -- (non-walk-in) projects, `client_name` falls back via COALESCE to
    -- the linked `clients.name` in projects.py, so ViewProjects keeps
    -- working unchanged for those.

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
    client_name: str   # Contractor — the walk-in counter party (kept as `client_name` for backward compatibility)
    phone: Optional[str] = None
    email: Optional[str] = None
    division: Optional[str] = "MAT"   # default division for walk-ins
    project_name: Optional[str] = None  # the LP's own project name; falls back to client_name if blank
    consultant: Optional[str] = None
    plot_no: Optional[str] = None
    client: Optional[str] = None        # NEW: the actual project Client/Owner — distinct from client_name (Contractor) above


class WalkInDetailsUpdate(BaseModel):
    """Used to edit project-level details after the walk-in already exists
    (consultant / plot no are often only known after the customer is created,
    and project_name may need correcting later)."""
    project_name: Optional[str] = None
    consultant: Optional[str] = None
    plot_no: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    client: Optional[str] = None        # the actual project Client/Owner


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

def _generate_lp_number(cur, division: str = None) -> str:
    """
    Picks the next LP/GSL number as (highest LP/GSL number ever issued) + 1.

    Previously this looked at the LATEST-CREATED row that happened to have
    an LP-style project_no (ORDER BY project_id DESC LIMIT 1). That breaks
    the moment a walk-in is deleted: its `projects` row disappears, but the
    `quotations` row it created (quotation_no = 'WALKIN-LP/xxxx/yy/DXB')
    does NOT — nothing ever deleted it. The next call would then see a
    lower "last" number and reissue one that's still sitting in
    `quotations`, causing:
        duplicate key value violates unique constraint "quotations_quotation_no_key"

    Fix: take the MAX numeric LP/GSL value across BOTH `projects.project_no`
    AND `quotations.quotation_no` (the latter never gets deleted), so a
    number can never be reissued once it's been used anywhere.

    GEO division walk-ins get the "GSL" prefix instead of "LP", but they
    share the SAME running counter as LP (e.g. ...LP/16732, GSL/16733...).
    """
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
    return f"{prefix}/{next_number}/{year_last_two}/DXB"


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
                is_walk_in, walk_in_client, walk_in_phone, walk_in_email,
                consultant, plot_no, client_name
            )
            VALUES ('PENDING', NULL, NULL, %s, 'Walk-In', %s, 'PENDING',
                    TRUE, %s, %s, %s, %s, %s, %s)
            RETURNING project_id
        """, (
            payload.project_name or payload.client_name,
            payload.division,
            payload.client_name,
            payload.phone,
            payload.email,
            payload.consultant,
            payload.plot_no,
            payload.client,
        ))
        project_id = cur.fetchone()[0]
        conn.commit()
        return {
            "project_id": project_id,
            "client_name": payload.client_name,
            "project_name": payload.project_name or payload.client_name,
            "consultant": payload.consultant,
            "plot_no": payload.plot_no,
            "client": payload.client,
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
                   is_walk_in, created_at, lpo_no, lpo_date,
                   consultant, plot_no, client_name
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
            "project_name": row[2],
            "client_name":  row[5],   # walk_in_client (Contractor) — kept under this key for backward compatibility
            "division":     row[3],
            "status":       row[4],
            "walk_in_client": row[5],
            "walk_in_phone":  row[6],
            "walk_in_email":  row[7],
            "is_walk_in":   row[8],
            "created_at":   str(row[9]) if row[9] else None,
            "lpo_no":       row[10],
            "lpo_date":     str(row[11]) if row[11] else None,
            "consultant":   row[12],
            "plot_no":      row[13],
            "client":       row[14],  # NEW: the actual Client/Owner name (distinct from Contractor above)
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
                   COALESCE(walk_in_client, project_name) as contractor_display,
                   division, status,
                   walk_in_client, walk_in_phone, walk_in_email, created_at,
                   project_name, consultant, plot_no, client_name
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
                "client_name":   r[2],   # Contractor (kept under this key for backward compatibility)
                "division":      r[3],
                "status":        r[4],
                "walk_in_client": r[5],
                "walk_in_phone":  r[6],
                "walk_in_email":  r[7],
                "created_at":    str(r[8]) if r[8] else None,
                "project_name":  r[9],
                "consultant":    r[10],
                "plot_no":       r[11],
                "client":        r[12],  # NEW: the actual Client/Owner name
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
            SELECT project_id, project_no, is_walk_in, division, quotation_id
            FROM projects
            WHERE project_id = %s AND is_walk_in = TRUE
        """, (project_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Walk-in not found")

        # Idempotency guard: if this walk-in already has an LP number, don't
        # generate a second one. Without this, clicking "Create LPO" again
        # (e.g. because the UI didn't visibly update the first time) burns
        # a brand new LP number and quotation row for the same walk-in.
        existing_lp, existing_quotation_id = row[1], row[4]
        if existing_lp and existing_lp != "PENDING":
            return {
                "message": "LPO already exists for this walk-in — returning the existing one.",
                "project_id": project_id,
                "lp_number": existing_lp,
                "quotation_id": existing_quotation_id,
                "already_existed": True,
            }

        division = row[3] or "MAT"

        # Check if there are items
        cur.execute("SELECT COUNT(*) FROM walk_in_items WHERE project_id = %s", (project_id,))
        item_count = cur.fetchone()[0]
        if item_count == 0:
            raise HTTPException(400, "No tests added. Add at least one test before generating LPO.")

        # Generate LP/GSL number (GEO division walk-ins get "GSL" instead of "LP")
        lp_number = _generate_lp_number(cur, division)
        
        # Create quotation
        cur.execute("""
            INSERT INTO quotations (quotation_no, status, division)
            VALUES (%s, 'APPROVED', %s)
            RETURNING quotation_id
        """, (f"WALKIN-{lp_number}", division))
        quotation_id = cur.fetchone()[0]
        
        # Copy items from walk_in_items to quotation_items (amount is generated)
        _copy_walkin_items_to_quotation(cur, project_id, quotation_id)

        # Update project with LP number and quotation_id.
        # Walk-ins are auto-verified: there's no separate LPO document to
        # review (the walk-in itself IS the confirmed order), so this skips
        # the manual "Verify LPO" gate and lets the LP go straight to Test
        # Requests, same as if someone had clicked Verify.
        cur.execute("""
            UPDATE projects
            SET project_no = %s,
                status = 'ACTIVE',
                lpo_date = CURRENT_DATE,
                quotation_id = %s,
                lpo_verified = TRUE,
                lpo_verified_at = NOW()
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


# ─── 9b. UPDATE PROJECT DETAILS (consultant, plot no, project name, contact) ─

@router.patch("/{project_id}/details", summary="Update walk-in project details")
def update_walk_in_details(project_id: int, payload: WalkInDetailsUpdate):
    """
    Edits the general project-level fields that live on `projects` (not on
    any PA-only table), so they stay available to Payment Advice, Invoice,
    Cover Sheet, etc. — anything that already reads from `projects`.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT project_id FROM projects WHERE project_id = %s AND is_walk_in = TRUE", (project_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Walk-in not found")

        fields, values = [], []
        if payload.project_name is not None:
            fields.append("project_name = %s"); values.append(payload.project_name)
        if payload.consultant is not None:
            fields.append("consultant = %s"); values.append(payload.consultant)
        if payload.plot_no is not None:
            fields.append("plot_no = %s"); values.append(payload.plot_no)
        if payload.phone is not None:
            fields.append("walk_in_phone = %s"); values.append(payload.phone)
        if payload.email is not None:
            fields.append("walk_in_email = %s"); values.append(payload.email)
        if payload.client is not None:
            fields.append("client_name = %s"); values.append(payload.client)

        if not fields:
            raise HTTPException(400, "Nothing to update")

        values.append(project_id)
        cur.execute(f"""
            UPDATE projects
            SET {", ".join(fields)}
            WHERE project_id = %s
            RETURNING project_id, project_name, consultant, plot_no, walk_in_phone, walk_in_email, client_name
        """, values)
        row = cur.fetchone()
        conn.commit()

        return {
            "message":      "Details updated",
            "project_id":   row[0],
            "project_name": row[1],
            "consultant":   row[2],
            "plot_no":      row[3],
            "walk_in_phone": row[4],
            "walk_in_email": row[5],
            "client":       row[6],
        }
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
        cur.execute("""
            SELECT quotation_id FROM projects
            WHERE project_id = %s AND is_walk_in = TRUE
        """, (project_id,))
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(404, "Walk-in not found")
        quotation_id = existing[0]

        # walk_in_items will cascade due to ON DELETE CASCADE on project_id FK
        cur.execute("""
            DELETE FROM projects
            WHERE project_id = %s AND is_walk_in = TRUE
            RETURNING project_id, project_no
        """, (project_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Walk-in not found")

        # Clean up the dedicated quotation create-lpo made for this walk-in.
        # Without this, the quotation_no ('WALKIN-LP/xxxx/yy/DXB') stays in
        # `quotations` forever as an orphan, and a future walk-in can be
        # issued that exact same LP number and crash on the unique
        # constraint when it tries to insert its own quotation.
        if quotation_id:
            cur.execute("DELETE FROM quotation_items WHERE quotation_id = %s", (quotation_id,))
            cur.execute(
                "DELETE FROM quotations WHERE quotation_id = %s AND quotation_no LIKE 'WALKIN-%%'",
                (quotation_id,)
            )

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