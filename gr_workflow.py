# gr_workflow.py
# ---------------------------------------------------------------------------
# Multi-quantity test instances, range-based sample mapping and multi-GR per GQ.
#
#   LP/Walk-in -> GQ -> Samples (GS-DDMMYY-001/1..N) -> Range mapping
#   -> Sample type + Department -> one GR-DDMMYY-NNN per GS entry (created on confirm)
#   -> Upload/Review
#
# Requires migrations/001_multi_gr_tbr.sql.
# Python 3.9 compatible (no `X | None` annotations).
# ---------------------------------------------------------------------------
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from db import get_connection

router = APIRouter(prefix="/gr-workflow", tags=["GR Workflow"])

# The lab is in the UAE (UTC+4, no DST). A fixed offset avoids needing tzdata
# inside the PyInstaller bundle and gives the correct calendar day for GR dates.
LAB_TZ = timezone(timedelta(hours=4))
MAX_SAMPLES_PER_BATCH = 500
APPROVER_ROLES = {"supervisor", "manager", "super_admin"}


# ===========================================================================
# Pure helpers (no DB) - unit tested in tests_gr_workflow.py
# ===========================================================================
_RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:[-\u2013]\s*(\d+))?\s*$")


def lab_today() -> date:
    return datetime.now(LAB_TZ).date()


def parse_range(text: str) -> Tuple[int, int]:
    """'3' -> (3, 3); '1-3' -> (1, 3). Raises ValueError on anything else."""
    m = _RANGE_RE.match(str(text or ""))
    if not m:
        raise ValueError(f"'{text}' is not a valid sample range (use e.g. 4 or 1-3)")
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) is not None else start
    if start < 1:
        raise ValueError("Sample numbers start at 1")
    if end < start:
        raise ValueError(f"Range '{text}' ends before it starts")
    return start, end


def range_suffix(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


def range_label(base: str, start: int, end: int) -> str:
    """GS-101126-001 + (1, 3) -> GS-101126-001/1-3"""
    return f"{base}/{range_suffix(start, end)}"


def sample_base_for_gq(request_no: str) -> str:
    """
    GQ-101126-01  -> GS-101126-001
    GQ-101126-010 -> GS-101126-010
    The DDMMYY and sequence come straight from the GQ number so a sample can
    always be traced back to its GQ by eye.
    """
    parts = (request_no or "").split("-")
    date_part = parts[1] if len(parts) >= 2 and parts[1] else lab_today().strftime("%d%m%y")
    seq = 1
    if len(parts) >= 3 and parts[2].isdigit():
        seq = int(parts[2]) or 1
    return f"GS-{date_part}-{seq:03d}"


def format_gr_no(d: date, seq: int) -> str:
    return f"GR-{d.strftime('%d%m%y')}-{seq:03d}"


def collapse_numbers(nums: List[int]) -> List[Tuple[int, int]]:
    """[1,2,3,5,9,10] -> [(1,3),(5,5),(9,10)]"""
    out: List[Tuple[int, int]] = []
    for n in sorted(set(nums)):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def ranges_within_batches(start: int, end: int, batches: List[Tuple[int, int]]) -> bool:
    """True if every number in start..end lies inside some generated batch."""
    covered = set()
    for b_start, b_end in batches:
        covered.update(range(b_start, b_end + 1))
    return all(n in covered for n in range(start, end + 1))


# ---------------------------------------------------------------------------
# Report Identification: CAR / BF / E / R# / C / NOC
# Optional tracking codes typed when a report is uploaded. They are appended to
# the report's description everywhere (View Reports, Delivery Notes, downloaded
# D.O), e.g.  {"car": "123", "bf": "234", "r_no": "566"} -> "CAR 123 BF 234 R# 566"
# ---------------------------------------------------------------------------
IDENT_FIELDS = [("car", "CAR"), ("bf", "BF"), ("e", "E"), ("r_no", "R#"), ("c", "C"), ("noc", "NOC")]
IDENT_MAX_LEN = 40


def clean_ident(values: Dict[str, Optional[str]]) -> Dict[str, str]:
    """Trim, drop blanks, validate. Raises ValueError with a user-facing message."""
    out: Dict[str, str] = {}
    for key, label in IDENT_FIELDS:
        v = re.sub(r"\s+", " ", str(values.get(key) or "")).strip()
        if not v:
            continue
        if len(v) > IDENT_MAX_LEN:
            raise ValueError(f"{label} is too long (max {IDENT_MAX_LEN} characters)")
        if key == "r_no" and not re.fullmatch(r"[0-9]+", v):
            raise ValueError("R# must be a number")
        out[key] = v
    return out


def format_ident(fields: Optional[Dict[str, str]]) -> str:
    """Fixed order, blanks omitted: 'CAR 123 BF 234 R# 566'."""
    fields = fields or {}
    return " ".join(f"{label} {fields[key]}" for key, label in IDENT_FIELDS if fields.get(key))


def _ident_fields_dict(v) -> Dict[str, str]:
    """jsonb may come back as a dict or a string depending on the driver."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


_IDENT_COLS_READY = False


def ensure_report_ident_columns(conn) -> None:
    """Idempotent safety net for migrations/002_report_identification.sql (runs once per process)."""
    global _IDENT_COLS_READY
    if _IDENT_COLS_READY:
        return
    c = conn.cursor()
    try:
        for tbl in ("gr_reports", "reports"):   # GR = source of truth, reports rows = mirror
            c.execute(
                f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS report_ident TEXT, "
                "ADD COLUMN IF NOT EXISTS report_ident_fields JSONB"
            )
        conn.commit()
        _IDENT_COLS_READY = True
    finally:
        c.close()


# ===========================================================================
# DB helpers
# ===========================================================================
def _get_gq(cur, request_no: str) -> dict:
    cur.execute(
        """
        SELECT tr.test_request_id, tr.request_no, tr.status, tr.template_type,
               p.project_id, p.project_no, p.project_name, p.quotation_id
        FROM test_requests tr
        JOIN projects p ON p.project_id = tr.project_id
        WHERE tr.request_no = %s
        """,
        (request_no,),
    )
    r = cur.fetchone()
    if not r:
        raise HTTPException(404, f"Test request '{request_no}' not found")
    return {
        "test_request_id": r[0], "request_no": r[1], "status": r[2],
        "template_type": r[3] or "soil", "project_id": r[4], "project_no": r[5],
        "project_name": r[6], "quotation_id": r[7],
    }


def sync_instances(cur, quotation_id: int) -> None:
    """
    Make sure every quotation item has exactly `quantity` test_instances rows,
    then reconcile instances against test_request_items so GQs created before
    this feature (which only stored a quantity) still own their share.

    Idempotent and cheap; called on every read/write that cares about
    availability so quantity edits made anywhere are picked up lazily.
    """
    # Two set-based statements instead of 2 queries per quotation item.
    cur.execute(
        """
        INSERT INTO test_instances (quotation_item_id, instance_no)
        SELECT qi.item_id, s.g
        FROM quotation_items qi
        CROSS JOIN LATERAL generate_series(1, GREATEST(COALESCE(qi.quantity, 1), 1)) AS s(g)
        WHERE qi.quotation_id = %s
          AND NOT EXISTS (
                SELECT 1 FROM test_instances ti
                WHERE ti.quotation_item_id = qi.item_id AND ti.instance_no = s.g)
        ON CONFLICT (quotation_item_id, instance_no) DO NOTHING
        """,
        (quotation_id,),
    )
    # Quantity lowered: drop surplus instances that nobody has claimed.
    cur.execute(
        """
        DELETE FROM test_instances ti
        USING quotation_items qi
        WHERE ti.quotation_item_id = qi.item_id
          AND qi.quotation_id = %s
          AND ti.test_request_id IS NULL
          AND ti.instance_no > GREATEST(COALESCE(qi.quantity, 1), 1)
        """,
        (quotation_id,),
    )

    # Legacy reconcile: a tri row says "this GQ takes N of this item".
    # Only fetch the lines that are actually short of instances (normally none).
    cur.execute(
        """
        SELECT tri.tri_id, tri.test_request_id, tri.quotation_item_id,
               GREATEST(COALESCE(tri.quantity, 1), 1) AS qty,
               COUNT(ti.instance_id) AS have
        FROM test_request_items tri
        JOIN quotation_items qi ON qi.item_id = tri.quotation_item_id
        LEFT JOIN test_instances ti ON ti.tri_id = tri.tri_id
        WHERE qi.quotation_id = %s
        GROUP BY tri.tri_id, tri.test_request_id, tri.quotation_item_id, tri.quantity
        HAVING GREATEST(COALESCE(tri.quantity, 1), 1) > COUNT(ti.instance_id)
        ORDER BY tri.tri_id
        """,
        (quotation_id,),
    )
    for tri_id, gq_id, item_id, qty, have in cur.fetchall():
        need = qty - have
        if need <= 0:
            continue
        cur.execute(
            """
            SELECT instance_id FROM test_instances
            WHERE quotation_item_id = %s AND test_request_id IS NULL
            ORDER BY instance_no LIMIT %s FOR UPDATE
            """,
            (item_id, need),
        )
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute(
                """
                UPDATE test_instances
                SET test_request_id = %s, tri_id = %s, allocated_at = NOW()
                WHERE instance_id = ANY(%s)
                """,
                (gq_id, tri_id, ids),
            )


def rebalance_tri(cur, tri_id: int) -> None:
    """
    Called after a GQ line quantity is edited. Grows by claiming free
    instances, shrinks by releasing the highest-numbered instances that have
    no samples mapped yet. Raises 400 if it cannot shrink far enough.
    """
    cur.execute(
        """
        SELECT tri.test_request_id, tri.quotation_item_id, GREATEST(COALESCE(tri.quantity,1),1),
               qi.quotation_id
        FROM test_request_items tri JOIN quotation_items qi ON qi.item_id = tri.quotation_item_id
        WHERE tri.tri_id = %s
        """,
        (tri_id,),
    )
    row = cur.fetchone()
    if not row:
        return
    gq_id, item_id, qty, quotation_id = row
    sync_instances(cur, quotation_id)  # grows/claims when qty went up
    cur.execute("SELECT instance_id, instance_no FROM test_instances WHERE tri_id = %s ORDER BY instance_no DESC", (tri_id,))
    mine = cur.fetchall()
    surplus = len(mine) - qty
    if surplus <= 0:
        return
    released = 0
    for instance_id, _no in mine:
        if released >= surplus:
            break
        cur.execute("SELECT 1 FROM samples WHERE test_instance_id = %s LIMIT 1", (instance_id,))
        if cur.fetchone():
            continue
        cur.execute(
            "UPDATE test_instances SET test_request_id = NULL, tri_id = NULL, allocated_at = NULL WHERE instance_id = %s",
            (instance_id,),
        )
        released += 1
    if released < surplus:
        raise HTTPException(
            400,
            "Quantity can't go that low: some of this test's instances already have samples mapped to them.",
        )


def assert_sole_holder(cur, tri_id: int) -> None:
    """
    Editing a GQ line's quantity rewrites the quotation quantity (existing
    behaviour in tests.py). That is only safe when no OTHER GQ holds instances
    of the same test, otherwise it would silently shrink/grow their share.
    """
    cur.execute(
        """
        SELECT COUNT(*) FROM test_instances ti
        JOIN test_request_items tri ON tri.quotation_item_id = ti.quotation_item_id
        WHERE tri.tri_id = %s AND ti.test_request_id IS NOT NULL
          AND ti.test_request_id <> tri.test_request_id
        """,
        (tri_id,),
    )
    if cur.fetchone()[0] > 0:
        raise HTTPException(
            400,
            "This test is split across several requests, so its quantity can't be changed from here. "
            "Change the quantity on the quotation instead.",
        )


def _batches(cur, test_request_id: int) -> List[Tuple[int, int]]:
    cur.execute(
        "SELECT start_no, end_no FROM gq_sample_batches WHERE test_request_id = %s ORDER BY start_no",
        (test_request_id,),
    )
    return [(a, b) for a, b in cur.fetchall()]


def _gr_status_sql() -> str:
    # Derived status: no reports row => GENERATED, else the report's status.
    return """
        COALESCE(
            (SELECT r.status FROM reports r WHERE r.report_no = g.gr_no
             ORDER BY r.linked_to_report_id NULLS FIRST, r.report_id LIMIT 1),
            'GENERATED')
    """


def recalc_quotation_totals(cur, quotation_id: int) -> None:
    """Same formula tests.py already uses: subtotal = sum(rate x qty), VAT 5%, grand = subtotal x 1.05."""
    cur.execute(
        """
        UPDATE quotations
        SET total_amount = sub.total,
            vat = sub.total * 0.05,
            grand_total = sub.total * 1.05
        FROM (SELECT COALESCE(SUM(COALESCE(unit_rate, 0) * GREATEST(COALESCE(quantity, 1), 1)), 0) AS total
              FROM quotation_items WHERE quotation_id = %s) sub
        WHERE quotation_id = %s
        """,
        (quotation_id, quotation_id),
    )


def mirror_quotation_to_walkin(cur, quotation_id: int) -> int:
    """
    Walk-in LPs keep a second copy of their tests in walk_in_items (that is what the
    Walk-In Customers screen, invoices and Payment Advice read). Every change made on
    the quotation_items side (GQ scope increase, new test added in a GQ, GQ qty edit)
    is mirrored back here so the quantity is the same everywhere.

    Linked rows are matched by walk_in_items.quotation_item_id; an old unlinked row with
    the same description is linked instead of duplicated. Does nothing for non-walk-in LPs.
    Returns the number of walk_in_items rows inserted or changed.
    """
    cur.execute("SELECT project_id FROM projects WHERE quotation_id = %s AND is_walk_in = TRUE LIMIT 1", (quotation_id,))
    prow = cur.fetchone()
    if not prow:
        return 0
    project_id = prow[0]

    cur.execute(
        """
        SELECT item_id, item_code, description, test_standard, COALESCE(unit_rate, 0),
               GREATEST(COALESCE(quantity, 1), 1), net_unit
        FROM quotation_items WHERE quotation_id = %s ORDER BY item_id
        """,
        (quotation_id,),
    )
    changed = 0
    for qi_id, code, desc, std, rate, qty, net_unit in cur.fetchall():
        cur.execute("SELECT item_id FROM walk_in_items WHERE quotation_item_id = %s LIMIT 1", (qi_id,))
        w = cur.fetchone()
        if not w:
            cur.execute(
                """
                SELECT item_id FROM walk_in_items
                WHERE project_id = %s AND quotation_item_id IS NULL
                  AND LOWER(TRIM(description)) = LOWER(TRIM(%s))
                ORDER BY item_id LIMIT 1
                """,
                (project_id, desc),
            )
            w = cur.fetchone()
            if w:
                cur.execute("UPDATE walk_in_items SET quotation_item_id = %s WHERE item_id = %s", (qi_id, w[0]))
        if w:
            cur.execute(
                """
                UPDATE walk_in_items SET quantity = %s, unit_rate = %s
                WHERE item_id = %s AND (quantity IS DISTINCT FROM %s OR unit_rate IS DISTINCT FROM %s)
                """,
                (qty, rate, w[0], qty, rate),
            )
            changed += cur.rowcount
        else:
            cur.execute(
                """
                INSERT INTO walk_in_items
                    (project_id, description, test_standard, unit_rate, quantity, net_unit, item_code, quotation_item_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (project_id, desc, std, rate, qty, net_unit, code, qi_id),
            )
            changed += 1
    return changed


@router.post("/walkin-resync")
def walkin_resync(lp_no: str):
    """One-off repair: copy the LP's current quotation_items quantities/tests into walk_in_items."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT quotation_id FROM projects WHERE project_no = %s AND is_walk_in = TRUE", (lp_no,))
        row = cur.fetchone()
        if not row or row[0] is None:
            raise HTTPException(404, f"No walk-in LP '{lp_no}' with a quotation was found")
        n = mirror_quotation_to_walkin(cur, row[0])
        conn.commit()
        return {"lp_no": lp_no, "walk_in_rows_changed": n}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error resyncing walk-in items: {e}")
    finally:
        cur.close()
        conn.close()


def _grow_item(cur, quotation_id: int, item_id: int, add: int) -> None:
    """quotation_items.quantity += add (keeps `amount` proportional when it is a plain column)."""
    cur.execute("SAVEPOINT grow_item")
    try:
        cur.execute(
            """
            UPDATE quotation_items
            SET amount = CASE WHEN amount IS NOT NULL AND GREATEST(COALESCE(quantity,1),1) > 0
                              THEN amount / GREATEST(COALESCE(quantity,1),1) * (GREATEST(COALESCE(quantity,1),1) + %s)
                              ELSE amount END,
                quantity = GREATEST(COALESCE(quantity,1),1) + %s
            WHERE item_id = %s AND quotation_id = %s
            """,
            (add, add, item_id, quotation_id),
        )
    except Exception:
        # `amount` is a generated column in this schema: update the quantity only.
        cur.execute("ROLLBACK TO SAVEPOINT grow_item")
        cur.execute(
            "UPDATE quotation_items SET quantity = GREATEST(COALESCE(quantity,1),1) + %s WHERE item_id = %s AND quotation_id = %s",
            (add, item_id, quotation_id),
        )
    cur.execute("RELEASE SAVEPOINT grow_item")


def _add_catalog_test(cur, quotation_id: int, catalog_id: int, qty: int) -> int:
    """Create a brand-new quotation line from the price catalog (rate pulled from the catalog).
    The same test can't be on the LP twice."""
    cur.execute(
        "SELECT code, description, test_standard, unit, unit_rate FROM price_catalog WHERE catalog_id = %s AND active = TRUE",
        (catalog_id,),
    )
    pc = cur.fetchone()
    if not pc:
        raise HTTPException(404, f"Catalog test {catalog_id} was not found")
    code, desc, std, unit, rate = pc
    cur.execute(
        """
        SELECT item_id FROM quotation_items
        WHERE quotation_id = %s
          AND (catalog_id = %s OR LOWER(TRIM(description)) = LOWER(TRIM(%s)))
        LIMIT 1
        """,
        (quotation_id, catalog_id, desc),
    )
    if cur.fetchone():
        raise HTTPException(400, f"'{desc}' is already on this LP. Increase its Qty instead of adding it again.")
    cur.execute(
        """
        INSERT INTO quotation_items (quotation_id, catalog_id, item_code, description, test_standard, unit, unit_rate, quantity)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING item_id
        """,
        (quotation_id, catalog_id, code, desc, std, unit, rate, qty),
    )
    return cur.fetchone()[0]


# ===========================================================================
# 1. Test availability + allocation to a GQ
# ===========================================================================
class AllocItem(BaseModel):
    quotation_item_id: int
    quantity: int = Field(ge=1)          # how many slots to run on THIS GQ
    add_slots: int = Field(default=0, ge=0)   # extra slots added to the test's total on the LP (scope increase)


class NewTestIn(BaseModel):
    catalog_id: int                      # a test that is not on the LP yet
    quantity: int = Field(ge=1)          # slots to create (all go on this GQ)


class AllocIn(BaseModel):
    items: List[AllocItem] = []
    new_tests: List[NewTestIn] = []


@router.get("/test-requests/{test_request_id}/available-tests")
def available_tests(test_request_id: int):
    """
    Every test on the project's quotation with how many instances exist, how
    many are already claimed by some GQ, and how many are left. A test with
    remaining = 0 is `is_exhausted` (UI greys it out).
    """
    from tests import GROUP_FILTER_MAP  # lazy: tests.py may import this module

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT tr.request_no, tr.template_type, p.quotation_id
            FROM test_requests tr JOIN projects p ON p.project_id = tr.project_id
            WHERE tr.test_request_id = %s
            """,
            (test_request_id,),
        )
        head = cur.fetchone()
        if not head:
            raise HTTPException(404, "Test request not found")
        request_no, template_type, quotation_id = head
        if quotation_id is None:
            raise HTTPException(404, "Project has no quotation")

        sync_instances(cur, quotation_id)
        conn.commit()

        groups = GROUP_FILTER_MAP.get(template_type or "soil")
        sql = """
            SELECT qi.item_id, qi.description, qi.test_standard, qi.unit_rate,
                   GREATEST(COALESCE(qi.quantity, 1), 1) AS total_qty,
                   COUNT(ti.instance_id) FILTER (WHERE ti.test_request_id IS NOT NULL) AS allocated,
                   COUNT(ti.instance_id) FILTER (WHERE ti.test_request_id = %s)        AS in_this_gq
            FROM quotation_items qi
            LEFT JOIN price_catalog pc ON pc.catalog_id = qi.catalog_id
            LEFT JOIN test_instances ti ON ti.quotation_item_id = qi.item_id
            WHERE qi.quotation_id = %s
        """
        params = [test_request_id, quotation_id]
        if groups:
            # also keep tests already on THIS GQ (e.g. a new test added from another group)
            sql += (" AND (pc.group_name = ANY(%s) OR qi.catalog_id IS NULL"
                    " OR EXISTS (SELECT 1 FROM test_instances t2 WHERE t2.quotation_item_id = qi.item_id AND t2.test_request_id = %s))")
            params.extend([groups, test_request_id])
        sql += " GROUP BY qi.item_id, qi.description, qi.test_standard, qi.unit_rate, qi.quantity ORDER BY qi.item_id"
        cur.execute(sql, params)

        out = []
        for item_id, desc, std, rate, total, allocated, in_gq in cur.fetchall():
            remaining = max(total - allocated, 0)
            out.append({
                "item_id": item_id,
                "description": desc,
                "test_standard": std,
                "unit_rate": float(rate) if rate is not None else 0.0,
                "total_qty": total,
                "allocated_qty": allocated,
                "allocated_to_this_gq": in_gq,
                "remaining_qty": remaining,
                "is_exhausted": remaining == 0,
            })
        return {"request_no": request_no, "tests": out}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error loading available tests: {e}")
    finally:
        cur.close()
        conn.close()


@router.get("/test-requests/{test_request_id}/catalog-search")
def catalog_search(test_request_id: int, q: str = "", limit: int = 15):
    """Search the master test list for tests that are NOT on this LP yet. Descriptions only: no rates."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT p.quotation_id FROM test_requests tr JOIN projects p ON p.project_id = tr.project_id WHERE tr.test_request_id = %s",
            (test_request_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Test request not found")
        q = (q or "").strip()
        if len(q) < 2:
            return {"tests": []}
        like = f"%{q}%"
        cur.execute(
            """
            SELECT pc.catalog_id, pc.code, pc.description, pc.test_standard, pc.group_name,
                   EXISTS (SELECT 1 FROM quotation_items qi
                           WHERE qi.quotation_id = %s
                             AND (qi.catalog_id = pc.catalog_id
                                  OR LOWER(TRIM(qi.description)) = LOWER(TRIM(pc.description)))) AS on_lp
            FROM price_catalog pc
            WHERE pc.active = TRUE AND (pc.description ILIKE %s OR pc.code ILIKE %s)
            ORDER BY pc.description LIMIT %s
            """,
            (row[0], like, like, min(max(limit, 1), 50)),
        )
        return {"tests": [
            {"catalog_id": r[0], "code": r[1], "description": r[2], "test_standard": r[3],
             "group_name": r[4], "on_lp": r[5]} for r in cur.fetchall()
        ]}
    finally:
        cur.close()
        conn.close()


@router.post("/test-requests/{test_request_id}/allocate")
def allocate_tests(test_request_id: int, payload: AllocIn):
    """Claim `quantity` free instances of each selected test for this GQ."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT p.quotation_id FROM test_requests tr
            JOIN projects p ON p.project_id = tr.project_id
            WHERE tr.test_request_id = %s
            """,
            (test_request_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Test request not found")
        quotation_id = row[0]
        if not payload.items and not payload.new_tests:
            raise HTTPException(400, "Select at least one test")
        # Serialise scope edits per LP so two GQs can't grow the same test at once.
        cur.execute("SELECT quotation_id FROM quotations WHERE quotation_id = %s FOR UPDATE", (quotation_id,))
        sync_instances(cur, quotation_id)

        # --- Dynamic scope: more slots on existing tests, and brand-new tests ---
        scope_changed = False
        wanted: Dict[int, int] = {}
        grown: Dict[int, int] = {}
        for it in payload.items:
            if it.add_slots:
                grown[it.quotation_item_id] = grown.get(it.quotation_item_id, 0) + it.add_slots
        for item_id, add in grown.items():
            cur.execute(
                "SELECT 1 FROM quotation_items WHERE item_id = %s AND quotation_id = %s",
                (item_id, quotation_id),
            )
            if not cur.fetchone():
                raise HTTPException(404, f"Test {item_id} is not on this LP")
            _grow_item(cur, quotation_id, item_id, add)
            scope_changed = True
        for nt in payload.new_tests:
            new_id = _add_catalog_test(cur, quotation_id, nt.catalog_id, nt.quantity)
            wanted[new_id] = wanted.get(new_id, 0) + nt.quantity
            scope_changed = True
        if scope_changed:
            sync_instances(cur, quotation_id)   # creates the new slots so they can be claimed below

        # Merge duplicate lines for the same test.
        for it in payload.items:
            wanted[it.quotation_item_id] = wanted.get(it.quotation_item_id, 0) + it.quantity

        claimed = []
        for item_id, qty in wanted.items():
            cur.execute(
                "SELECT description FROM quotation_items WHERE item_id = %s AND quotation_id = %s",
                (item_id, quotation_id),
            )
            d = cur.fetchone()
            if not d:
                raise HTTPException(404, f"Test {item_id} is not on this LP")

            cur.execute(
                """
                SELECT instance_id FROM test_instances
                WHERE quotation_item_id = %s AND test_request_id IS NULL
                ORDER BY instance_no LIMIT %s FOR UPDATE
                """,
                (item_id, qty),
            )
            free = [r[0] for r in cur.fetchall()]
            if len(free) < qty:
                raise HTTPException(
                    400,
                    f"'{d[0]}': only {len(free)} of the {qty} requested are still available "
                    f"(the rest are already on other GQs).",
                )

            cur.execute(
                "SELECT tri_id FROM test_request_items WHERE test_request_id = %s AND quotation_item_id = %s ORDER BY tri_id LIMIT 1",
                (test_request_id, item_id),
            )
            t = cur.fetchone()
            if t:
                tri_id = t[0]
                cur.execute("UPDATE test_request_items SET quantity = COALESCE(quantity,0) + %s WHERE tri_id = %s", (qty, tri_id))
            else:
                cur.execute(
                    "INSERT INTO test_request_items (test_request_id, quotation_item_id, quantity) VALUES (%s,%s,%s) RETURNING tri_id",
                    (test_request_id, item_id, qty),
                )
                tri_id = cur.fetchone()[0]

            cur.execute(
                "UPDATE test_instances SET test_request_id = %s, tri_id = %s, allocated_at = NOW() WHERE instance_id = ANY(%s)",
                (test_request_id, tri_id, free),
            )
            claimed.append({"quotation_item_id": item_id, "tri_id": tri_id, "quantity": qty})

        # Keep the parent LP's commercial record in step with the new scope.
        if scope_changed:
            recalc_quotation_totals(cur, quotation_id)
            mirror_quotation_to_walkin(cur, quotation_id)   # walk-in LPs: Walk-In screen/invoice read walk_in_items

        conn.commit()
        return {
            "message": f"{len(claimed)} test(s) added to the request"
                       + (" and the LP was updated" if scope_changed else ""),
            "allocated": claimed,
            "lp_synced": scope_changed,    # no prices in the response: technicians never see them
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error allocating tests: {e}")
    finally:
        cur.close()
        conn.close()


# ===========================================================================
# 2. Sample batches (GS-DDMMYY-001/1..N) + range mapping
# ===========================================================================
class BatchIn(BaseModel):
    total: int = Field(ge=1, le=MAX_SAMPLES_PER_BATCH)
    created_by: Optional[int] = None


class MapItem(BaseModel):
    instance_id: int
    range: str                      # "3" or "1-3"
    department: str
    sample_type: Optional[str] = None


class MapIn(BaseModel):
    collected_by: Optional[str] = None
    mappings: List[MapItem]
    created_by: Optional[int] = None
    generate_grs: bool = True       # one GR per distinct GS label, created in the same transaction
    backdate: Optional[date] = None  # BackData: samples + GRs are dated/numbered on this past day


def _overview(cur, gq: dict) -> dict:
    gq_id = gq["test_request_id"]
    base = sample_base_for_gq(gq["request_no"])
    batches = _batches(cur, gq_id)
    total = max((b for _a, b in batches), default=0)

    cur.execute(
        """
        SELECT ti.instance_id, ti.instance_no, qi.item_id, qi.item_code, qi.description,
               qi.test_standard, s.sample_id, s.physical_sample_no, s.range_start, s.range_end,
               s.department, s.sample_type, s.report_status, g.gr_no
        FROM test_instances ti
        JOIN quotation_items qi ON qi.item_id = ti.quotation_item_id
        LEFT JOIN samples s ON s.test_instance_id = ti.instance_id
        LEFT JOIN gr_reports g ON g.gr_id = s.gr_id
        WHERE ti.test_request_id = %s
        ORDER BY qi.item_id, ti.instance_no
        """,
        (gq_id,),
    )
    instances = [{
        "instance_id": r[0], "instance_no": r[1], "quotation_item_id": r[2], "item_code": r[3],
        "test_name": r[4], "test_standard": r[5],
        "mapped": r[6] is not None, "sample_id": r[6], "label": r[7],
        "range_start": r[8], "range_end": r[9], "department": r[10], "sample_type": r[11],
        "report_status": r[12], "gr_no": r[13],
    } for r in cur.fetchall()]

    # Report Identification per GR of this GQ (shown right after GRs are created, editable any time)
    cur.execute(
        """
        SELECT g.gr_no, g.report_ident_fields,
               EXISTS (SELECT 1 FROM reports r WHERE r.report_no = g.gr_no AND COALESCE(r.is_locked, FALSE))
        FROM gr_reports g WHERE g.test_request_id = %s
        ORDER BY g.gr_date, g.gr_seq
        """,
        (gq_id,),
    )
    grs = [{"gr_no": r[0], "identification": _ident_fields_dict(r[1]), "locked": bool(r[2])}
           for r in cur.fetchall()]

    return {
        **gq,
        "grs": grs,
        "sample_base": base,
        "total_samples": total,
        "batches": [{"start": a, "end": b} for a, b in batches],
        "identifiers": [{"no": n, "label": f"{base}/{n}"} for n in range(1, total + 1)],
        "instances": instances,
    }


@router.get("/gq/{request_no}/overview")
def gq_overview(request_no: str):
    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_report_ident_columns(conn)
        gq = _get_gq(cur, request_no)
        if gq["quotation_id"] is not None:
            sync_instances(cur, gq["quotation_id"])
            conn.commit()
        return _overview(cur, gq)
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error loading request: {e}")
    finally:
        cur.close()
        conn.close()


@router.get("/gq/recent")
def recent_gqs(limit: int = 10):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT tr.request_no, tr.status, tr.created_at, p.project_no, p.project_name
            FROM test_requests tr JOIN projects p ON p.project_id = tr.project_id
            ORDER BY tr.created_at DESC LIMIT %s
            """,
            (min(max(limit, 1), 50),),
        )
        return [{"request_no": r[0], "status": r[1], "created_at": r[2],
                 "project_no": r[3], "project_name": r[4]} for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


@router.post("/gq/{request_no}/sample-batch")
def create_sample_batch(request_no: str, payload: BatchIn):
    """Enter a total (e.g. 10) -> GS-DDMMYY-001/1 .. /10. Numbering continues across batches."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        gq = _get_gq(cur, request_no)
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (gq["test_request_id"],))
        batches = _batches(cur, gq["test_request_id"])
        start = max((b for _a, b in batches), default=0) + 1
        end = start + payload.total - 1
        base = sample_base_for_gq(gq["request_no"])
        cur.execute(
            "INSERT INTO gq_sample_batches (test_request_id, sample_base, start_no, end_no, created_by) VALUES (%s,%s,%s,%s,%s)",
            (gq["test_request_id"], base, start, end, payload.created_by),
        )
        conn.commit()
        ensure_report_ident_columns(conn)
        return {
            "message": f"{payload.total} sample identifier(s) generated",
            "first": range_label(base, start, start),
            "last": range_label(base, end, end),
            "overview": _overview(cur, gq),
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error generating sample identifiers: {e}")
    finally:
        cur.close()
        conn.close()


@router.post("/gq/{request_no}/map-samples")
def map_samples(request_no: str, payload: MapIn):
    """
    Commit the whole mapping in one transaction (nothing is saved unless every
    row is valid). Each row = one test instance <-> one sample range, plus the
    Sample Type and Department chosen in the properties dialog.

    Then (generate_grs) one GR-DDMMYY-NNN is created for every distinct GS label
    in this request, e.g. GS-111126-007/1-2 -> one GR holding both PSD #1 and #2,
    GS-111126-007/8 -> one GR holding the Chemical and pH rows that share it.
    Samples, GRs and numbering all commit together or not at all.
    """
    if not payload.mappings:
        raise HTTPException(400, "Nothing to map")

    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_report_ident_columns(conn)   # cached after first call; needed by _overview below
        gq = _get_gq(cur, request_no)
        gq_id = gq["test_request_id"]
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (gq_id,))
        base = sample_base_for_gq(gq["request_no"])
        batches = _batches(cur, gq_id)
        if not batches:
            raise HTTPException(400, "Generate the sample identifiers first")

        # BackData: the working day for samples and GRs (today unless a past day is chosen)
        work_day = payload.backdate or lab_today()
        if work_day > lab_today():
            raise HTTPException(400, "BackData date cannot be in the future")
        received_ts = (datetime.combine(work_day, datetime.now(LAB_TZ).time())
                       if payload.backdate else None)   # None -> NOW()

        # ---- 1. Validate every row in Python (zero DB round-trips) ----------------------------
        parsed = []   # (MapItem, start, end, dept, label, sample_type)
        seen_instances = set()
        for m in payload.mappings:
            if m.instance_id in seen_instances:
                raise HTTPException(400, "A test instance can only be mapped once")
            seen_instances.add(m.instance_id)
            try:
                start, end = parse_range(m.range)
            except ValueError as ve:
                raise HTTPException(400, str(ve))
            if not ranges_within_batches(start, end, batches):
                raise HTTPException(
                    400, f"Range {range_suffix(start, end)} includes samples that haven't been generated for {request_no}"
                )
            dept = (m.department or "").strip()
            if not dept:
                raise HTTPException(400, f"Choose a department for {range_label(base, start, end)}")
            parsed.append((m, start, end, dept, range_label(base, start, end), (m.sample_type or "").strip() or None))

        # ---- 2. ONE query: all instances (row-locked) + whether each is already mapped --------
        cur.execute(
            """
            SELECT ti.instance_id, ti.instance_no, qi.description,
                   EXISTS (SELECT 1 FROM samples s WHERE s.test_instance_id = ti.instance_id)
            FROM test_instances ti JOIN quotation_items qi ON qi.item_id = ti.quotation_item_id
            WHERE ti.instance_id = ANY(%s) AND ti.test_request_id = %s
            FOR UPDATE OF ti
            """,
            (list(seen_instances), gq_id),
        )
        inst_by_id = {r[0]: r for r in cur.fetchall()}
        for m, *_rest in parsed:
            inst = inst_by_id.get(m.instance_id)
            if not inst:
                raise HTTPException(400, f"Test instance {m.instance_id} doesn't belong to {request_no}")
            if inst[3]:
                raise HTTPException(400, f"'{inst[2]}' #{inst[1]} is already mapped to samples")

        # ---- 3. ONE query for the per-label running counter (sample_no = label-n) -------------
        labels = sorted({p[4] for p in parsed})
        cur.execute(
            "SELECT physical_sample_no, COUNT(*) FROM samples WHERE physical_sample_no = ANY(%s) GROUP BY physical_sample_no",
            (labels,),
        )
        label_n = {r[0]: int(r[1]) for r in cur.fetchall()}

        rows = []     # one dict per sample, request order preserved
        for m, start, end, dept, label, stype in parsed:
            label_n[label] = label_n.get(label, 0) + 1
            inst = inst_by_id[m.instance_id]
            rows.append({
                "instance_id": m.instance_id, "start": start, "end": end, "dept": dept, "label": label,
                "stype": stype, "sample_no": f"{label}-{label_n[label]}",
                "test_name": inst[2], "instance_no": inst[1],
            })

        # ---- 4. ONE bulk INSERT for all samples (instance/quotation data joined in SQL) -------
        cur.execute(
            """
            INSERT INTO samples (
                sample_no, batch_no, physical_sample_no, request_id, collected_by,
                received_date, status, assigned_tri_id, assigned_quotation_item_id,
                assigned_item_code, assigned_test_name, test_standard, catalog_id,
                unit_rate, department, test_instance_id, range_start, range_end,
                sample_type, report_status
            )
            SELECT t.sample_no, %s::text, t.label, %s, %s::text, COALESCE(%s, NOW()), 'PENDING',
                   ti.tri_id, qi.item_id, qi.item_code, qi.description, qi.test_standard, qi.catalog_id,
                   qi.unit_rate, t.dept, t.iid, t.rs, t.rend, t.stype, 'PENDING'
            FROM unnest(%s::int[], %s::text[], %s::text[], %s::int[], %s::int[], %s::text[], %s::text[])
                 AS t(iid, sample_no, label, rs, rend, dept, stype)
            JOIN test_instances ti ON ti.instance_id = t.iid
            JOIN quotation_items qi ON qi.item_id = ti.quotation_item_id
            RETURNING sample_id, sample_no
            """,
            (
                base, gq_id, payload.collected_by, received_ts,
                [r["instance_id"] for r in rows], [r["sample_no"] for r in rows], [r["label"] for r in rows],
                [r["start"] for r in rows], [r["end"] for r in rows], [r["dept"] for r in rows],
                [r["stype"] for r in rows],
            ),
        )
        id_by_no = {r[1]: r[0] for r in cur.fetchall()}
        created = []
        for r in rows:
            r["sample_id"] = id_by_no[r["sample_no"]]
            created.append({
                "sample_id": r["sample_id"], "sample_no": r["sample_no"], "label": r["label"],
                "test_name": r["test_name"], "instance_no": r["instance_no"], "department": r["dept"],
                "sample_type": r["stype"],
            })

        # ---- 5. GRs: one MAX(gr_seq), one bulk INSERT, one bulk UPDATE -------------------------
        grs = []
        if payload.generate_grs and created:
            today = work_day   # GR-DDMMYY-NNN continues that day's own series
            # Serialise GR numbering per calendar day so two users can't collide.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("gr:" + today.isoformat(),))
            by_label: Dict[str, list] = {}
            for c in created:                       # `created` keeps request order
                by_label.setdefault(c["label"], []).append(c)

            cur.execute("SELECT COALESCE(MAX(gr_seq), 0) FROM gr_reports WHERE gr_date = %s", (today,))
            first_seq = int(cur.fetchone()[0]) + 1
            seqs = [first_seq + i for i in range(len(by_label))]
            gr_nos = [format_gr_no(today, s) for s in seqs]

            cur.execute(
                """
                INSERT INTO gr_reports (gr_no, gr_date, gr_seq, test_request_id, created_by)
                SELECT t.gr_no, %s::date, t.seq, %s, %s
                FROM unnest(%s::text[], %s::int[]) AS t(gr_no, seq)
                RETURNING gr_id, gr_no
                """,
                (today, gq_id, payload.created_by, gr_nos, seqs),
            )
            gr_id_by_no = {r[1]: r[0] for r in cur.fetchall()}

            sample_ids, gr_ids = [], []
            for gr_no, (label, grows) in zip(gr_nos, by_label.items()):
                gid = gr_id_by_no[gr_no]
                for r in grows:
                    sample_ids.append(r["sample_id"])
                    gr_ids.append(gid)
                grs.append({"gr_no": gr_no, "gr_id": gid, "label": label, "sample_count": len(grows),
                            "tests": sorted({r["test_name"] for r in grows})})
            cur.execute(
                """
                UPDATE samples s SET gr_id = t.gr_id, report_status = 'IN_GR'
                FROM unnest(%s::int[], %s::int[]) AS t(sample_id, gr_id)
                WHERE s.sample_id = t.sample_id
                """,
                (sample_ids, gr_ids),
            )

        conn.commit()
        msg = f"{len(created)} sample record(s) created for {request_no}"
        if grs:
            msg += f" and {len(grs)} GR(s) generated: " + ", ".join(g["gr_no"] for g in grs)
        # Return the fresh overview so the screen doesn't need a second full reload request.
        return {"message": msg, "count": len(created), "samples": created, "grs": grs,
                "overview": _overview(cur, gq)}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error mapping samples: {e}")
    finally:
        cur.close()
        conn.close()


# ===========================================================================
# 3. Unified report upload: search GR -> lineage -> upload -> submit
# ===========================================================================
@router.get("/gr/search")
def search_grs(q: str = "", limit: int = 25):
    """Empty q = most recent GRs; otherwise match GR no, GQ no or project no/name."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        sql = f"""
            SELECT g.gr_no, g.gr_date, tr.request_no, p.project_no, p.project_name,
                   {_gr_status_sql()} AS st,
                   (SELECT COUNT(*) FROM samples s WHERE s.gr_id = g.gr_id)
            FROM gr_reports g
            JOIN test_requests tr ON tr.test_request_id = g.test_request_id
            JOIN projects p ON p.project_id = tr.project_id
        """
        params: list = []
        term = q.strip()
        if term:
            like = f"%{term}%"
            sql += """
            WHERE g.gr_no ILIKE %s OR tr.request_no ILIKE %s
               OR p.project_no ILIKE %s OR p.project_name ILIKE %s
            """
            params += [like, like, like, like]
        sql += " ORDER BY g.gr_date DESC, g.gr_seq DESC LIMIT %s"
        params.append(min(max(limit, 1), 100))
        cur.execute(sql, params)
        return [{
            "gr_no": r[0], "gr_date": r[1], "request_no": r[2], "project_no": r[3],
            "project_name": r[4], "status": r[5], "sample_count": r[6],
        } for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


@router.get("/gr/{gr_no}")
def gr_lineage(gr_no: str):
    """Full lineage: LP/project -> GQ -> GR -> samples/tests, plus the report file state."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_report_ident_columns(conn)
        cur.execute(
            f"""
            SELECT g.gr_id, g.gr_no, g.gr_date, g.created_at, g.test_request_id,
                   tr.request_no, tr.created_at,
                   p.project_id, p.project_no, p.project_name, p.lpo_no, p.lpo_date,
                   p.client_name, p.quotation_id, {_gr_status_sql()} AS st,
                   g.report_ident, g.report_ident_fields
            FROM gr_reports g
            JOIN test_requests tr ON tr.test_request_id = g.test_request_id
            JOIN projects p ON p.project_id = tr.project_id
            WHERE g.gr_no = %s
            """,
            (gr_no,),
        )
        h = cur.fetchone()
        if not h:
            raise HTTPException(404, f"{gr_no} not found")

        cur.execute("SELECT quotation_no FROM quotations WHERE quotation_id = %s", (h[13],))
        q = cur.fetchone()

        cur.execute(
            """
            SELECT s.sample_id, s.sample_no, s.physical_sample_no, s.range_start, s.range_end,
                   s.status, s.department, s.sample_type, s.assigned_test_name, s.assigned_item_code,
                   s.test_standard, ti.instance_no
            FROM samples s LEFT JOIN test_instances ti ON ti.instance_id = s.test_instance_id
            WHERE s.gr_id = %s ORDER BY s.range_start NULLS LAST, s.sample_id
            """,
            (h[0],),
        )
        items = [{
            "sample_id": r[0], "sample_no": r[1], "label": r[2], "range_start": r[3], "range_end": r[4],
            "sample_status": r[5], "department": r[6], "sample_type": r[7], "test_name": r[8],
            "item_code": r[9], "test_standard": r[10], "instance_no": r[11],
        } for r in cur.fetchall()]

        cur.execute(
            """
            SELECT report_id, status, original_filename, file_path, created_at, checked_at,
                   approved_at, is_locked, notes
            FROM reports WHERE report_no = %s
            ORDER BY linked_to_report_id NULLS FIRST, report_id LIMIT 1
            """,
            (gr_no,),
        )
        rep = cur.fetchone()
        report = None
        if rep:
            report = {
                "report_id": rep[0], "status": rep[1], "original_filename": rep[2], "file_url": rep[3],
                "uploaded_at": rep[4], "submitted_at": rep[5], "approved_at": rep[6],
                "is_locked": rep[7], "notes": rep[8],
                "download_url": f"/reports/reports/{rep[0]}/download",
            }

        return {
            "gr_no": h[1], "gr_date": h[2], "created_at": h[3], "status": h[14],
            "project": {"project_id": h[7], "project_no": h[8], "project_name": h[9],
                        "lpo_no": h[10], "lpo_date": h[11], "client_name": h[12],
                        "quotation_no": q[0] if q else None},
            "gq": {"test_request_id": h[4], "request_no": h[5], "created_at": h[6]},
            "items": items,
            "identification": _ident_fields_dict(h[16]),
            "identification_text": h[15] or "",
            "report": report,
            "can_upload": report is None and len(items) > 0,
            "can_submit": bool(report and report["status"] == "DRAFT"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error loading {gr_no}: {e}")
    finally:
        cur.close()
        conn.close()


class ReportIdentIn(BaseModel):
    car: Optional[str] = None
    bf: Optional[str] = None
    e: Optional[str] = None
    r_no: Optional[str] = None
    c: Optional[str] = None
    noc: Optional[str] = None


@router.post("/gr/{gr_no}/identification")
def save_gr_identification(gr_no: str, body: ReportIdentIn):
    """
    Save the Report Identification (CAR / BF / E / R# / C / NOC) for a GR. Works before or after
    the report file is uploaded: it is stored on the GR, and mirrored onto the report rows (if any)
    so View Reports and Delivery Notes show it. Sending all-blank values clears it.
    """
    try:
        ident = clean_ident({k: getattr(body, k) for k, _ in IDENT_FIELDS})
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    text = format_ident(ident) or None
    fields_json = json.dumps(ident) if ident else None

    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_report_ident_columns(conn)
        cur.execute("SELECT gr_id FROM gr_reports WHERE gr_no = %s", (gr_no,))
        if not cur.fetchone():
            raise HTTPException(404, f"{gr_no} not found")
        cur.execute("SELECT 1 FROM reports WHERE report_no = %s AND COALESCE(is_locked, FALSE) LIMIT 1", (gr_no,))
        if cur.fetchone():
            raise HTTPException(400, f"{gr_no} is approved and locked, so its identification can no longer be changed")
        cur.execute(
            "UPDATE gr_reports SET report_ident = %s, report_ident_fields = %s::jsonb WHERE gr_no = %s",
            (text, fields_json, gr_no),
        )
        cur.execute(
            "UPDATE reports SET report_ident = %s, report_ident_fields = %s::jsonb WHERE report_no = %s",
            (text, fields_json, gr_no),
        )
        conn.commit()
        return {
            "gr_no": gr_no, "identification": text or "", "fields": ident,
            "message": f"Report identification saved for {gr_no}." if text else f"Report identification cleared for {gr_no}.",
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Could not save identification: {e}")
    finally:
        cur.close()
        conn.close()


@router.post("/gr/{gr_no}/upload")
async def upload_gr_report(
    gr_no: str,
    uploaded_by: int = Form(...),
    file: UploadFile = File(...),
    notes: Optional[str] = Form(None),
):
    """
    Attach the final report document to a GR that already exists.
    The GR's saved Report Identification (CAR / BF / E / R# / C / NOC), if any, is copied onto
    every report row so it shows up in View Reports and on Delivery Notes. Writes the
    same `reports` rows the old upload endpoint wrote (one per sample row, all
    sharing report_no = gr_no) so approval, e-mailing, invoicing and
    View Reports keep working unchanged.
    """
    # Imported here so a Supabase hiccup at import time can't stop the whole
    # router from loading.
    from reports import supabase

    conn = get_connection()
    cur = conn.cursor()
    try:
        ensure_report_ident_columns(conn)
        cur.execute("SELECT gr_id, report_ident, report_ident_fields FROM gr_reports WHERE gr_no = %s", (gr_no,))
        g = cur.fetchone()
        ident_text = (g[1] if g else None) or None
        ident_fields = _ident_fields_dict(g[2]) if g else {}
        ident_json = json.dumps(ident_fields) if ident_fields else None
        if not g:
            raise HTTPException(404, f"{gr_no} not found")
        gr_id = g[0]

        cur.execute("SELECT 1 FROM reports WHERE report_no = %s LIMIT 1", (gr_no,))
        if cur.fetchone():
            raise HTTPException(400, f"{gr_no} already has a report. Use 'Replace file' in View Reports to change it.")

        cur.execute(
            """
            SELECT sample_id, sample_no, assigned_item_code, assigned_test_name
            FROM samples WHERE gr_id = %s ORDER BY range_start NULLS LAST, sample_id
            """,
            (gr_id,),
        )
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(400, f"{gr_no} has no samples")

        content = await file.read()
        if not content:
            raise HTTPException(400, "The uploaded file is empty")
        ext = os.path.splitext(file.filename or "")[1].lower() or ".pdf"
        cloud_name = f"reports/{gr_no.replace('-', '_')}_{rows[0][2] or 'GR'}_{secrets.token_hex(4)}{ext}"
        try:
            supabase.storage.from_("reports").upload(
                path=cloud_name, file=content, file_options={"content-type": file.content_type}
            )
            public_url = supabase.storage.from_("reports").get_public_url(cloud_name)
        except Exception as e:
            raise HTTPException(500, f"Failed to upload to storage: {e}")

        names: List[str] = []
        for r in rows:
            if r[3] and r[3] not in names:
                names.append(r[3])
        covers_test_type = ", ".join(names)
        covers_samples = [r[1] for r in rows]
        ftype = ext[1:]

        cur.execute(
            """
            INSERT INTO reports (report_no, sample_id, original_filename, stored_filename, file_path,
                                 file_type, uploaded_by, status, covers_test_type, covers_samples, notes,
                                 report_ident, report_ident_fields)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'DRAFT',%s,%s,%s,%s,%s::jsonb) RETURNING report_id
            """,
            (gr_no, rows[0][0], file.filename, cloud_name, public_url, ftype, uploaded_by,
             covers_test_type, covers_samples, notes, ident_text, ident_json),
        )
        main_id = cur.fetchone()[0]
        for r in rows[1:]:
            cur.execute(
                """
                INSERT INTO reports (report_no, sample_id, original_filename, stored_filename, file_path,
                                     file_type, uploaded_by, status, covers_test_type, covers_samples,
                                     linked_to_report_id, report_ident, report_ident_fields)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'DRAFT',%s,%s,%s,%s,%s::jsonb)
                """,
                (gr_no, r[0], file.filename, cloud_name, public_url, ftype, uploaded_by,
                 covers_test_type, covers_samples, main_id, ident_text, ident_json),
            )
        conn.commit()
        return {"message": f"Report uploaded to {gr_no}", "report_id": main_id, "gr_no": gr_no,
                "status": "DRAFT", "sample_count": len(rows), "identification": ident_text or ""}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error uploading report: {e}")
    finally:
        cur.close()
        conn.close()


@router.post("/gr/{gr_no}/submit")
def submit_gr_for_review(gr_no: str, checked_by: int):
    """DRAFT -> UNDER_REVIEW for every row of the GR's report (approval stays in View Reports)."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT report_id, status, is_locked FROM reports WHERE report_no = %s", (gr_no,))
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(404, f"{gr_no} has no uploaded report yet")
        if any(r[2] for r in rows):
            raise HTTPException(400, "This report is locked")
        if any(r[1] != "DRAFT" for r in rows):
            raise HTTPException(400, "Only DRAFT reports can be submitted for review")
        cur.execute(
            "UPDATE reports SET status = 'UNDER_REVIEW', checked_by = %s, checked_at = NOW() WHERE report_no = %s AND status = 'DRAFT'",
            (checked_by, gr_no),
        )
        conn.commit()
        return {"message": f"{gr_no} submitted for supervisor review", "gr_no": gr_no, "status": "UNDER_REVIEW"}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Error submitting for review: {e}")
    finally:
        cur.close()
        conn.close()