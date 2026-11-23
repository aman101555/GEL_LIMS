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

    -- quotation_item_id: links a walk_in_items row to the quotation_items row
    -- it was copied/synced into (see _sync_item_to_quotation() below). NULL
    -- until an LP number exists for the project (quotation_items only starts
    -- existing once create-lpo runs) — see the "live sync" note below.
    ALTER TABLE walk_in_items
        ADD COLUMN IF NOT EXISTS quotation_item_id INTEGER
            REFERENCES quotation_items(item_id) ON DELETE SET NULL;

    -- Live sync (added after launch): originally quotation_items was only
    -- ever populated ONCE, at create-lpo time, from whatever was in
    -- walk_in_items at that instant (_copy_walkin_items_to_quotation()).
    -- Anything added to walk_in_items afterward silently never made it into
    -- quotation_items — which is what Create Test Requests reads from — so
    -- new tests added post-LPO never showed up there (Invoice/Payment Advice
    -- read walk_in_items directly, so they always looked fine).
    --
    -- Fix: add_item / add_item_from_catalog / delete_item now keep
    -- quotation_items in sync live, item-by-item, via quotation_item_id as
    -- the link. update_item also pushes edits through so the two tables
    -- never drift apart.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date
import uuid
from db import get_connection
from plots import project_context
from advance_payments import ensure_client_trn_column

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
    location: Optional[str] = None      # project / plot location (blank -> 'Walk-In', the old default)
    client: Optional[str] = None        # NEW: the actual project Client/Owner — distinct from client_name (Contractor) above
    client_id: Optional[int] = None     # NEW: links to the real `clients` table row (set when picked from the
                                         # client dropdown / "+ New Client" in the UI). Powers Advance Payment
                                         # for walk-ins, which is keyed off projects.client_id like every other
                                         # project. If omitted, we best-effort resolve it by exact name match
                                         # against `clients.name` — see _resolve_client_id() below.


class WalkInDetailsUpdate(BaseModel):
    """Used to edit project-level details after the walk-in already exists
    (consultant / plot no are often only known after the customer is created,
    and project_name may need correcting later)."""
    project_name: Optional[str] = None
    consultant: Optional[str] = None
    plot_no: Optional[str] = None
    location: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    client: Optional[str] = None        # the actual project Client/Owner
    client_id: Optional[int] = None     # NEW: explicitly (re)link to a `clients` row


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
    description:   Optional[str]   = None
    quantity:      Optional[int]   = None
    unit_rate:     Optional[float] = None
    test_standard: Optional[str]   = None
    net_unit:      Optional[str]   = None


class LPNumberUpdate(BaseModel):
    lp_number: str


class LPOCreate(BaseModel):
    # Number chosen/edited on the preview before confirming. Omit to auto-generate.
    lp_number: Optional[str] = None


# ─── Helper: resolve a real clients.client_id for a walk-in ───────────────────

def _resolve_client_id(cur, client_id: Optional[int], client_name: Optional[str]) -> Optional[int]:
    """
    Best-effort resolution of the `clients` table row a walk-in's "Client / Owner"
    refers to, so Advance Payment (keyed off projects.client_id, same as every
    other project) also works for walk-ins.

    - If client_id is given, just verify it exists and return it.
    - Otherwise, if a client_name (free-typed "Client / Owner" text) is given,
      try an exact case-insensitive match against clients.name. We deliberately
      do NOT auto-create a new clients row here (that could silently create
      duplicates from typos) — only the explicit "+ New Client" flow creates
      clients, exactly as before.
    - Returns None if nothing can be resolved (walk-in behaves exactly as it
      did before this feature — no advance option shown, nothing else breaks).
    """
    if client_id:
        cur.execute("SELECT client_id FROM clients WHERE client_id = %s", (client_id,))
        row = cur.fetchone()
        return row[0] if row else None

    if client_name and client_name.strip():
        cur.execute(
            "SELECT client_id FROM clients WHERE LOWER(name) = LOWER(%s) LIMIT 1",
            (client_name.strip(),)
        )
        row = cur.fetchone()
        if row:
            return row[0]

    return None


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


# ─── Helper: a hand-typed LP number must not collide with an existing one ─────

def _assert_lp_number_free(cur, lp_number: str, exclude_project_id: int):
    cur.execute(
        """
        SELECT 1 FROM projects WHERE project_no = %s AND project_id <> %s
        UNION ALL
        SELECT 1 FROM quotations WHERE quotation_no = %s
        LIMIT 1
        """,
        (lp_number, exclude_project_id, f"WALKIN-{lp_number}"),
    )
    if cur.fetchone():
        raise HTTPException(409, f"{lp_number} is already in use. Choose a different number.")


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
    """
    Copy walk_in_items to quotation_items for the new quotation (runs once,
    at create-lpo time). Unlike the original version, this now goes row by
    row and writes the resulting quotation_items.item_id back onto
    walk_in_items.quotation_item_id, so every item — not just ones added
    later — is linked and stays sync-able going forward.
    """
    cur.execute("""
        SELECT item_id, description, test_standard, unit_rate, quantity, net_unit, item_code
        FROM walk_in_items
        WHERE project_id = %s
    """, (project_id,))
    rows = cur.fetchall()

    for (wi_item_id, description, test_standard, unit_rate, quantity, net_unit, item_code) in rows:
        # Don't include 'amount' - it's a generated column
        cur.execute("""
            INSERT INTO quotation_items (
                quotation_id, description, test_standard,
                unit_rate, quantity, net_unit, item_code
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING item_id
        """, (quotation_id, description, test_standard, unit_rate, quantity, net_unit, item_code))
        qi_item_id = cur.fetchone()[0]

        cur.execute("""
            UPDATE walk_in_items SET quotation_item_id = %s WHERE item_id = %s
        """, (qi_item_id, wi_item_id))


# ─── Helper: get a project's LP number + quotation_id (None if no LPO yet) ──────

def _get_project_lp_and_quotation(cur, project_id: int):
    cur.execute("""
        SELECT project_no, quotation_id
        FROM projects
        WHERE project_id = %s
    """, (project_id,))
    row = cur.fetchone()
    if not row:
        return None, None
    lp_number, quotation_id = row
    if not quotation_id or str(lp_number).startswith("PENDING"):
        return None, None
    return lp_number, quotation_id


# ─── Helper: live-sync a single new walk_in_items row into quotation_items ──────

def _sync_item_to_quotation(cur, project_id: int, wi_item_id: int,
                             description: str, test_standard: Optional[str],
                             unit_rate: float, quantity: int,
                             net_unit: Optional[str], item_code: Optional[str]):
    """
    If this walk-in already has an LP number (quotation_items exists for it),
    insert a matching row into quotation_items right now and link it back via
    walk_in_items.quotation_item_id — so a test added AFTER the LPO was
    created still shows up on Create Test Requests immediately.

    If there's no LP number yet, does nothing (the eventual create-lpo call
    will pick this item up in its normal one-time copy).

    Returns the linked quotation_items.item_id if synced, else None.
    """
    lp_number, quotation_id = _get_project_lp_and_quotation(cur, project_id)
    if not quotation_id:
        return None, None

    cur.execute("""
        INSERT INTO quotation_items (
            quotation_id, description, test_standard,
            unit_rate, quantity, net_unit, item_code
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING item_id
    """, (quotation_id, description, test_standard, unit_rate, quantity, net_unit, item_code))
    qi_item_id = cur.fetchone()[0]

    cur.execute("""
        UPDATE walk_in_items SET quotation_item_id = %s WHERE item_id = %s
    """, (qi_item_id, wi_item_id))

    return qi_item_id, lp_number


# ─── Helper: is a quotation_items row already pulled into a Test Request? ───────

def _quotation_item_in_use(cur, quotation_item_id: int):
    """Returns the request_no of the Test Request using this item, or None."""
    cur.execute("""
        SELECT tr.request_no
        FROM test_request_items tri
        JOIN test_requests tr ON tri.test_request_id = tr.test_request_id
        WHERE tri.quotation_item_id = %s
        LIMIT 1
    """, (quotation_item_id,))
    row = cur.fetchone()
    return row[0] if row else None


# ─── 1. CREATE WALK-IN (no LP yet — just saves customer info) ────────────────

@router.get("/lp-check", summary="Is this LP number already used? (read-only, for the LP preview)")
def check_lp_number(lp_number: str, project_id: Optional[int] = None):
    """Defined BEFORE GET /{project_id} so 'lp-check' is not read as a project id."""
    lp = (lp_number or "").strip()
    if not lp:
        raise HTTPException(400, "LP number is required")
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT project_id, COALESCE(walk_in_client, client_name, project_name)
            FROM projects
            WHERE project_no = %s AND (%s::int IS NULL OR project_id <> %s)
            LIMIT 1
            """,
            (lp, project_id, project_id),
        )
        p = cur.fetchone()
        if p:
            return {"lp_number": lp, "exists": True, "where": "project", "project_id": p[0], "used_by": p[1]}
        cur.execute("SELECT 1 FROM quotations WHERE quotation_no = %s LIMIT 1", (f"WALKIN-{lp}",))
        if cur.fetchone():
            return {"lp_number": lp, "exists": True, "where": "quotation", "project_id": None, "used_by": None}
        return {"lp_number": lp, "exists": False, "where": None, "project_id": None, "used_by": None}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


@router.post("/", summary="Create Walk-In Customer record")
def create_walk_in(payload: WalkInCreate):
    """
    Creates a shell project row tagged as walk-in.
    LP number is generated separately via POST /walkin/{project_id}/create-lpo.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        resolved_client_id = _resolve_client_id(cur, payload.client_id, payload.client)

        cur.execute("""
            INSERT INTO projects (
                project_no, quotation_id, client_id,
                project_name, location, division, status,
                is_walk_in, walk_in_client, walk_in_phone, walk_in_email,
                consultant, plot_no, client_name
            )
            VALUES (%s, NULL, %s, %s, %s, %s, 'PENDING',
                    TRUE, %s, %s, %s, %s, %s, %s)
            RETURNING project_id
        """, (
            f"PENDING-{uuid.uuid4().hex[:8]}",   # unique placeholder: project_no is UNIQUE
            resolved_client_id,
            payload.project_name or payload.client_name,
            (payload.location or "").strip() or "Walk-In",
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
            "location": (payload.location or "").strip() or "Walk-In",
            "client": payload.client,
            "client_id": resolved_client_id,  # NEW: null if it couldn't be resolved — non-breaking
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
                   consultant, plot_no, client_name, client_id, location
            FROM projects
            WHERE project_id = %s AND is_walk_in = TRUE
        """, (project_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Walk-in not found")

        # Customer TRN of the linked client (shared clients.trn — same value
        # Advance Payment and Generate Invoice show / edit)
        client_trn = None
        if row[15]:
            ensure_client_trn_column(cur)
            cur.execute("SELECT trn FROM clients WHERE client_id = %s", (row[15],))
            _t = cur.fetchone()
            client_trn = _t[0] if _t else None

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

        # totals from the items just loaded (saves a round trip to the DB)
        total = round(sum(i["amount"] for i in items), 2)
        vat = round(total * VAT_RATE, 2)
        grand_total = round(total * (1 + VAT_RATE), 2)
        ctx = project_context(cur, project_id)   # test requests (tests are NOT included)

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
            "location":     row[16],
            "client":       row[14],  # NEW: the actual Client/Owner name (distinct from Contractor above)
            "client_id":    row[15],  # NEW: links to `clients` table — powers Advance Payment for this walk-in
            "client_trn":   client_trn,  # NEW: Customer TRN (clients.trn)
            "items":        items,
            "requests":     ctx["requests"],
            "request_count": len(ctx["requests"]),
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
        # has_payment_advice is computed here (one EXISTS per row, in-DB) instead
        # of the frontend calling GET /payment-advice/{project_id}/items for every
        # walk-in with an LP number. That old approach turned one page load into
        # 1 + N sequential-ish HTTP round trips (each of which re-ran an ALTER
        # TABLE IF NOT EXISTS check and two more SELECTs) just to read a single
        # boolean — by far the biggest cost on this screen.
        cur.execute("""
            SELECT p.project_id, p.project_no,
                   COALESCE(p.walk_in_client, p.project_name) as contractor_display,
                   p.division, p.status,
                   p.walk_in_client, p.walk_in_phone, p.walk_in_email, p.created_at,
                   p.project_name, p.consultant, p.plot_no, p.client_name,
                   EXISTS (
                       SELECT 1 FROM walk_in_items wi
                       WHERE wi.project_id = p.project_id AND wi.pa_generated = TRUE
                   ) AS has_payment_advice
            FROM projects p
            WHERE p.is_walk_in = TRUE
            ORDER BY (regexp_match(p.project_no, '^(?:LP|GSL)/(\\d+)/'))[1]::int DESC NULLS LAST, p.project_id DESC
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
                "has_payment_advice": r[13],  # NEW: replaces the old N+1 frontend check
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

        # Live-sync into quotation_items if an LP number already exists for
        # this walk-in, so it shows up on Create Test Requests right away.
        quotation_item_id, lp_number = _sync_item_to_quotation(
            cur, project_id, item_id,
            payload.description, payload.test_standard,
            payload.unit_rate, payload.quantity, payload.net_unit, payload.item_code
        )

        conn.commit()

        total, vat, grand_total = _recalc_totals(cur, project_id)

        return {
            "item_id": item_id,
            "message": "Item added",
            "totals": {"total_amount": total, "vat": vat, "grand_total": grand_total},
            "synced_to_test_requests": quotation_item_id is not None,
            "lp_number": lp_number,
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

        # Live-sync into quotation_items if an LP number already exists for
        # this walk-in, so it shows up on Create Test Requests right away.
        quotation_item_id, lp_number = _sync_item_to_quotation(
            cur, project_id, item_id,
            description, test_standard, unit_rate, payload.quantity,
            None, code
        )

        conn.commit()

        total, vat, grand_total = _recalc_totals(cur, project_id)

        return {
            "item_id": item_id,
            "message": "Catalog item added",
            "totals": {"total_amount": total, "vat": vat, "grand_total": grand_total},
            "synced_to_test_requests": quotation_item_id is not None,
            "lp_number": lp_number,
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
            SELECT item_id, unit_rate, quantity, test_standard, net_unit, quotation_item_id
            FROM walk_in_items
            WHERE item_id = %s AND project_id = %s
        """, (item_id, project_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Item not found")

        quotation_item_id = row[5]

        if payload.description is not None or payload.unit_rate is not None:
            cur.execute("SELECT COALESCE(invoiced, FALSE) FROM walk_in_items WHERE item_id = %s", (item_id,))
            if cur.fetchone()[0]:
                raise HTTPException(400, "This test is already invoiced, so its description and rate can no longer be changed.")

        if payload.description is not None:
            new_desc = payload.description.strip()
            if not new_desc:
                raise HTTPException(400, "Description cannot be empty")
            # Billing wording: Payment Advice / Invoices read walk_in_items. The technical test name
            # on quotation_items (GQ sheet, samples, reports) is left as it is.
            cur.execute("UPDATE walk_in_items SET description = %s WHERE item_id = %s", (new_desc, item_id))
        elif payload.quantity is not None:
            if payload.quantity <= 0:
                raise HTTPException(400, "Quantity must be > 0")
            cur.execute("UPDATE walk_in_items SET quantity = %s WHERE item_id = %s", (payload.quantity, item_id))
            if quotation_item_id:
                cur.execute("UPDATE quotation_items SET quantity = %s WHERE item_id = %s", (payload.quantity, quotation_item_id))
        elif payload.unit_rate is not None:
            if payload.unit_rate < 0:
                raise HTTPException(400, "Unit rate cannot be negative")
            cur.execute("UPDATE walk_in_items SET unit_rate = %s WHERE item_id = %s", (payload.unit_rate, item_id))
            if quotation_item_id:
                cur.execute("UPDATE quotation_items SET unit_rate = %s WHERE item_id = %s", (payload.unit_rate, quotation_item_id))
        elif payload.test_standard is not None:
            cur.execute("UPDATE walk_in_items SET test_standard = %s WHERE item_id = %s", (payload.test_standard, item_id))
            if quotation_item_id:
                cur.execute("UPDATE quotation_items SET test_standard = %s WHERE item_id = %s", (payload.test_standard, quotation_item_id))
        elif payload.net_unit is not None:
            cur.execute("UPDATE walk_in_items SET net_unit = %s WHERE item_id = %s", (payload.net_unit, item_id))
            if quotation_item_id:
                cur.execute("UPDATE quotation_items SET net_unit = %s WHERE item_id = %s", (payload.net_unit, quotation_item_id))
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
            SELECT quotation_item_id
            FROM walk_in_items
            WHERE item_id = %s AND project_id = %s
        """, (item_id, project_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Item not found")

        quotation_item_id = row[0]

        # Guard: if this test has already been pulled into a Test Request,
        # deleting it here would silently orphan that Test Request's line
        # item. Block it and tell the user exactly where it's in use.
        if quotation_item_id:
            request_no = _quotation_item_in_use(cur, quotation_item_id)
            if request_no:
                raise HTTPException(
                    400,
                    f"This test has already been added to Test Request {request_no}. "
                    f"Remove it from that Test Request before deleting it here."
                )

        lp_number, _ = _get_project_lp_and_quotation(cur, project_id)

        # Desync: remove the mirrored row from quotation_items first (if any)
        # so it stops showing up on Create Test Requests, then delete the
        # walk_in_items row itself.
        desynced = False
        if quotation_item_id:
            cur.execute("DELETE FROM quotation_items WHERE item_id = %s", (quotation_item_id,))
            desynced = True

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
            "desynced_from_test_requests": desynced,
            "lp_number": lp_number,
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
def create_lpo(project_id: int, payload: Optional[LPOCreate] = None):
    """
    Generates the LP number and creates a quotation from walk_in_items.
    This makes walk-in projects work like regular projects.

    Optional body {"lp_number": "..."}: the number previewed/edited on screen.
    Omitted -> the next free number is generated here as before.
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
        if existing_lp and not str(existing_lp).startswith("PENDING"):
            return {
                "message": "LPO already exists for this walk-in — returning the existing one.",
                "project_id": project_id,
                "lp_number": existing_lp,
                "quotation_id": existing_quotation_id,
                "already_existed": True,
            }

        division = row[3] or "MAT"

        # No minimum item count: the LP number is issued when the walk-in is created, and tests
        # added afterwards are synced to quotation_items live (_sync_item_to_quotation).

        # Hand-edited number from the preview, else generate LP/GSL
        # (GEO division walk-ins get "GSL" instead of "LP")
        manual = ((payload.lp_number if payload else None) or "").strip()
        if manual:
            if len(manual) > 60:
                raise HTTPException(400, "LP number is too long")
            _assert_lp_number_free(cur, manual, project_id)
            lp_number = manual
        else:
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
        if payload.location is not None:
            fields.append("location = %s"); values.append(payload.location)
        if payload.phone is not None:
            fields.append("walk_in_phone = %s"); values.append(payload.phone)
        if payload.email is not None:
            fields.append("walk_in_email = %s"); values.append(payload.email)
        if payload.client is not None:
            fields.append("client_name = %s"); values.append(payload.client)

        # NEW: (re)link to a real clients row — either explicitly (client_id
        # passed) or best-effort resolved from the client name being saved.
        # This is what lets Advance Payment start working for walk-ins that
        # were created before this field existed, or that were free-typed.
        if payload.client_id is not None or payload.client is not None:
            resolved_client_id = _resolve_client_id(cur, payload.client_id, payload.client)
            if resolved_client_id is not None:
                fields.append("client_id = %s"); values.append(resolved_client_id)

        if not fields:
            raise HTTPException(400, "Nothing to update")

        values.append(project_id)
        cur.execute(f"""
            UPDATE projects
            SET {", ".join(fields)}
            WHERE project_id = %s
            RETURNING project_id, project_name, consultant, plot_no, walk_in_phone, walk_in_email, client_name, client_id, location
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
            "client_id":    row[7],  # NEW
            "location":     row[8],
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


# ─── 12. GQ-FIRST FLOW (Create Test Request screen) ─────────────────────────────
# A walk-in LP gets its tests on the Create Test Request screen: the tests are
# staged there and saved together with the GQ in ONE transaction, so an abandoned screen
# never leaves tests without a GQ. Each GQ therefore owns its own walk_in_items rows, which
# is what lets Rate / Description be edited per GQ on the Walk-In screen.

class WalkInGQNewTest(BaseModel):
    catalog_id: Optional[int] = None       # from the price catalogue (rate is taken from there)
    description: Optional[str] = None      # custom test (rate starts at 0), or a renamed catalogue test (rate stays from the catalogue)
    test_standard: Optional[str] = None
    net_unit: Optional[str] = None
    quantity: int = 1


class WalkInGQOpenTest(BaseModel):
    quotation_item_id: int                 # a test already on the LP that is not on any GQ yet
    quantity: int = 1


class WalkInGQCreate(BaseModel):
    requested_by: Optional[str] = None
    template_type: Optional[str] = "soil"
    request_no: Optional[str] = None       # typed GQ number; omit = next in line
    backdate: Optional[date] = None
    new_tests: List[WalkInGQNewTest] = []
    open_tests: List[WalkInGQOpenTest] = []


@router.post("/{project_id}/gq", summary="Create a GQ for a walk-in LP together with its tests")
def create_walk_in_gq(project_id: int, payload: WalkInGQCreate):
    from tests import generate_request_no, resolve_request_no, TEMPLATE_TYPES   # lazy: avoids import cycles
    from gr_workflow import sync_instances, recalc_quotation_totals
    import datetime as _dt

    if not payload.new_tests and not payload.open_tests:
        raise HTTPException(400, "Add at least one test before generating the GQ.")

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT project_no, quotation_id, COALESCE(lpo_verified, FALSE)
            FROM projects WHERE project_id = %s AND is_walk_in = TRUE
        """, (project_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Walk-in not found")
        project_no, quotation_id, verified = row
        if not quotation_id or not project_no or str(project_no).startswith("PENDING"):
            raise HTTPException(400, "Create the LPO first — GQs are raised against the LP number.")
        if not verified:
            raise HTTPException(400, "The LPO must be verified before Test Requests can be created.")

        backdate = payload.backdate
        if backdate and backdate > (datetime.utcnow() + _dt.timedelta(hours=4)).date():
            raise HTTPException(400, "BackData date cannot be in the future")

        # Serialise per LP so two screens can't claim the same free slots
        cur.execute("SELECT quotation_id FROM quotations WHERE quotation_id = %s FOR UPDATE", (quotation_id,))

        wanted = {}   # quotation_item_id -> slots to put on this GQ

        for nt in payload.new_tests:
            if nt.quantity <= 0:
                raise HTTPException(400, "Quantity must be > 0")
            if nt.catalog_id:
                cur.execute(
                    "SELECT code, description, test_standard, unit_rate FROM price_catalog WHERE catalog_id = %s AND active = TRUE",
                    (nt.catalog_id,),
                )
                cat = cur.fetchone()
                if not cat:
                    raise HTTPException(404, f"Catalog test {nt.catalog_id} not found")
                code, desc, std, rate = cat
                if nt.description and nt.description.strip():
                    desc = nt.description.strip()       # name edited on the Create Test Request screen; code, rate stay from the catalogue
                if nt.test_standard and nt.test_standard.strip():
                    std = nt.test_standard.strip()      # standard picked/edited on the Create Test Request screen
                net_unit = nt.net_unit
            else:
                desc = (nt.description or "").strip()
                if not desc:
                    raise HTTPException(400, "A custom test needs a description")
                code, std, rate, net_unit = None, nt.test_standard, 0, nt.net_unit
            cur.execute("""
                INSERT INTO walk_in_items (project_id, description, test_standard, unit_rate, quantity, net_unit, item_code)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING item_id
            """, (project_id, desc, std, rate, nt.quantity, net_unit, code))
            wi_id = cur.fetchone()[0]
            qi_id, _lp = _sync_item_to_quotation(cur, project_id, wi_id, desc, std, rate, nt.quantity, net_unit, code)
            wanted[qi_id] = wanted.get(qi_id, 0) + nt.quantity

        sync_instances(cur, quotation_id)

        for ot in payload.open_tests:
            if ot.quantity <= 0:
                raise HTTPException(400, "Quantity must be > 0")
            cur.execute("SELECT 1 FROM quotation_items WHERE item_id = %s AND quotation_id = %s", (ot.quotation_item_id, quotation_id))
            if not cur.fetchone():
                raise HTTPException(404, f"Test {ot.quotation_item_id} is not on this LP")
            wanted[ot.quotation_item_id] = wanted.get(ot.quotation_item_id, 0) + ot.quantity

        request_no = resolve_request_no(cur, payload.request_no, backdate)
        template_type = payload.template_type or "soil"
        if template_type not in TEMPLATE_TYPES:
            template_type = "soil"

        if backdate:
            created_at = _dt.datetime.combine(backdate, (datetime.utcnow() + _dt.timedelta(hours=4)).time())
            cur.execute("""
                INSERT INTO test_requests (project_id, request_no, requested_by, status, template_type, created_at)
                VALUES (%s, %s, %s, 'PENDING_SAMPLES', %s, %s) RETURNING test_request_id
            """, (project_id, request_no, payload.requested_by, template_type, created_at))
        else:
            cur.execute("""
                INSERT INTO test_requests (project_id, request_no, requested_by, status, template_type)
                VALUES (%s, %s, %s, 'PENDING_SAMPLES', %s) RETURNING test_request_id
            """, (project_id, request_no, payload.requested_by, template_type))
        test_request_id = cur.fetchone()[0]

        # Claim the slots for this GQ (same bookkeeping as gr_workflow.allocate_tests)
        for qi_id, qty in wanted.items():
            cur.execute("""
                SELECT instance_id FROM test_instances
                WHERE quotation_item_id = %s AND test_request_id IS NULL
                ORDER BY instance_no LIMIT %s FOR UPDATE
            """, (qi_id, qty))
            free = [r[0] for r in cur.fetchall()]
            if len(free) < qty:
                cur.execute("SELECT description FROM quotation_items WHERE item_id = %s", (qi_id,))
                d = cur.fetchone()
                raise HTTPException(400, f"'{d[0] if d else qi_id}': only {len(free)} of the {qty} requested are still available.")
            cur.execute(
                "INSERT INTO test_request_items (test_request_id, quotation_item_id, quantity) VALUES (%s, %s, %s) RETURNING tri_id",
                (test_request_id, qi_id, qty),
            )
            tri_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE test_instances SET test_request_id = %s, tri_id = %s, allocated_at = NOW() WHERE instance_id = ANY(%s)",
                (test_request_id, tri_id, free),
            )

        recalc_quotation_totals(cur, quotation_id)
        conn.commit()
        return {
            "test_request_id": test_request_id,
            "request_no": request_no,
            "project_id": project_id,
            "project_no": project_no,
            "tests": len(wanted),
            "message": f"{request_no} generated",
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


@router.get("/{project_id}/gqs", summary="GQs of a walk-in LP with their tests, rates and amounts")
def list_walk_in_gqs(project_id: int):
    """Walk-In screen only (rates included). The Create Test Request screen uses /plots/... which has no rates."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM projects WHERE project_id = %s AND is_walk_in = TRUE", (project_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Walk-in not found")

        cur.execute("""
            SELECT test_request_id, request_no, status, created_at
            FROM test_requests WHERE project_id = %s
            ORDER BY created_at DESC, test_request_id DESC
        """, (project_id,))
        gqs = [{"test_request_id": r[0], "request_no": r[1], "status": r[2],
                "created_at": str(r[3]) if r[3] else None, "items": []} for r in cur.fetchall()]
        by_id = {g["test_request_id"]: g for g in gqs}

        cur.execute("""
            SELECT tri.test_request_id, tri.tri_id, COALESCE(tri.quantity, 1),
                   wi.item_id,
                   COALESCE(wi.description, qi.description),
                   COALESCE(wi.test_standard, qi.test_standard),
                   COALESCE(wi.net_unit, qi.net_unit),
                   COALESCE(wi.unit_rate, qi.unit_rate, 0),
                   COALESCE(wi.invoiced, FALSE), COALESCE(wi.pa_generated, FALSE)
            FROM test_request_items tri
            JOIN test_requests tr ON tr.test_request_id = tri.test_request_id
            JOIN quotation_items qi ON qi.item_id = tri.quotation_item_id
            LEFT JOIN walk_in_items wi ON wi.quotation_item_id = qi.item_id AND wi.project_id = tr.project_id
            WHERE tr.project_id = %s
            ORDER BY tri.tri_id
        """, (project_id,))
        for (gq_id, tri_id, qty, wi_id, desc, std, net_unit, rate, invoiced, pa) in cur.fetchall():
            rate = float(rate or 0)
            by_id[gq_id]["items"].append({
                "tri_id": tri_id, "item_id": wi_id,           # item_id None = legacy row with no walk-in copy (read-only)
                "description": desc, "test_standard": std, "net_unit": net_unit,
                "quantity": qty, "unit_rate": rate, "amount": round(rate * qty, 2),
                "invoiced": bool(invoiced), "pa_generated": bool(pa),
            })

        # Tests on the LP that are not on any GQ yet (e.g. the ones typed when the LP was created)
        cur.execute("""
            SELECT wi.item_id, wi.description, wi.test_standard, wi.net_unit, wi.unit_rate,
                   GREATEST(COALESCE(wi.quantity, 1), 1) - COALESCE((
                       SELECT SUM(COALESCE(tri.quantity, 1)) FROM test_request_items tri
                       WHERE tri.quotation_item_id = wi.quotation_item_id), 0) AS remaining,
                   COALESCE(wi.invoiced, FALSE), COALESCE(wi.pa_generated, FALSE)
            FROM walk_in_items wi
            WHERE wi.project_id = %s
            ORDER BY wi.item_id
        """, (project_id,))
        unassigned = []
        for (wi_id, desc, std, net_unit, rate, remaining, invoiced, pa) in cur.fetchall():
            remaining = int(remaining or 0)
            if remaining <= 0:
                continue
            rate = float(rate or 0)
            unassigned.append({
                "tri_id": None, "item_id": wi_id, "description": desc, "test_standard": std,
                "net_unit": net_unit, "quantity": remaining, "unit_rate": rate,
                "amount": round(rate * remaining, 2), "invoiced": bool(invoiced), "pa_generated": bool(pa),
            })
        return {"gqs": gqs, "unassigned": unassigned}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


@router.get("/{project_id}/gq-map", summary="Which GQ each walk-in test belongs to (+ the GQ's generated reports)")
def walk_in_gq_map(project_id: int):
    """Used by Generate Payment Advice / Generate Invoice to group tests under their GQ.
    items: { walk_in_items.item_id: [GQ numbers] }"""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT tr.test_request_id, tr.request_no
            FROM test_requests tr WHERE tr.project_id = %s
            ORDER BY tr.created_at, tr.test_request_id
        """, (project_id,))
        gqs = [{"test_request_id": r[0], "request_no": r[1], "reports": []} for r in cur.fetchall()]
        by_id = {g["test_request_id"]: g for g in gqs}

        cur.execute("""
            SELECT DISTINCT wi.item_id, tr.request_no, tr.created_at, tr.test_request_id
            FROM walk_in_items wi
            JOIN test_request_items tri ON tri.quotation_item_id = wi.quotation_item_id
            JOIN test_requests tr ON tr.test_request_id = tri.test_request_id
            WHERE wi.project_id = %s AND tr.project_id = %s
            ORDER BY tr.created_at, tr.test_request_id
        """, (project_id, project_id))
        items = {}
        for wi_id, request_no, _c, _t in cur.fetchall():
            items.setdefault(str(wi_id), []).append(request_no)

        if by_id:
            cur.execute("""
                SELECT test_request_id, gr_no FROM gr_reports
                WHERE test_request_id = ANY(%s) ORDER BY gr_no
            """, (list(by_id.keys()),))
            for tr_id, gr_no in cur.fetchall():
                by_id[tr_id]["reports"].append(gr_no)
        return {"gqs": gqs, "items": items}
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