"""
plots.py
Request / test helpers used by the project screens (View Projects, Walk-In Customers).

One LP (Project No.) = one plot. The plot number is an ordinary field on the LP
(projects.plot_no) — there are no sub-plots, plot tabs or "[LP-Plot]" entries.

  GET  /plots/project/{id}/context      test requests of one project (no tests)
  GET  /plots/request/{id}/tests        tests of ONE request (loaded when it is clicked)
  GET  /plots/project/{id}/open-tests   tests on the LP that are not on a GQ yet (no rates)
  POST /plots/project/{id}/items        add a test to a NON walk-in project (walk-ins use /walkin/{id}/items)
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import get_connection

router = APIRouter(prefix="/plots", tags=["LP Requests"])


def ensure_plot_schema() -> None:
    """No-op, kept only so an existing `ensure_plot_schema()` call in main.py keeps working.
    Plot sub-entries no longer exist (1 LP = 1 plot). Remove that call from main.py,
    then delete this function."""
    return None


def _clean(s: Optional[str]) -> str:
    return (s or "").strip()


def project_context(cur, project_id: int) -> dict:
    """Test requests of this project (no tests: those load when a request is clicked)."""
    cur.execute(
        """
        SELECT tr.test_request_id, tr.request_no, tr.status, tr.requested_by, tr.created_at, tr.template_type,
               COALESCE(c.n, 0), COALESCE(c.q, 0)
        FROM test_requests tr
        LEFT JOIN (SELECT test_request_id, COUNT(*) AS n, SUM(quantity) AS q
                   FROM test_request_items
                   WHERE test_request_id IN (SELECT test_request_id FROM test_requests WHERE project_id = %s)
                   GROUP BY test_request_id) c ON c.test_request_id = tr.test_request_id
        WHERE tr.project_id = %s
        ORDER BY tr.created_at DESC, tr.test_request_id DESC
        """,
        (project_id, project_id),
    )
    requests = [
        {"test_request_id": r[0], "request_no": r[1], "status": r[2], "requested_by": r[3],
         "created_at": str(r[4]) if r[4] else None, "template_type": r[5] or "soil",
         "test_count": int(r[6]), "total_qty": int(r[7] or 0)}
        for r in cur.fetchall()
    ]
    return {"requests": requests}


@router.get("/project/{project_id}/context")
def get_context(project_id: int):
    conn = get_connection()
    cur = conn.cursor()
    try:
        return project_context(cur, project_id)
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


@router.get("/project/{project_id}/open-tests")
def open_tests(project_id: int):
    """Tests already on this LP that are not (fully) on a GQ yet. No rates: same rule as request_tests."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT qi.item_id, qi.description, qi.test_standard,
                   GREATEST(COALESCE(qi.quantity, 1), 1) - COALESCE((
                       SELECT SUM(COALESCE(tri.quantity, 1)) FROM test_request_items tri
                       WHERE tri.quotation_item_id = qi.item_id), 0) AS remaining
            FROM quotation_items qi JOIN projects p ON p.quotation_id = qi.quotation_id
            WHERE p.project_id = %s
            ORDER BY qi.item_id
            """,
            (project_id,),
        )
        return {"items": [
            {"quotation_item_id": r[0], "description": r[1], "test_standard": r[2], "remaining_qty": int(r[3])}
            for r in cur.fetchall() if int(r[3] or 0) > 0
        ]}
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


@router.get("/request/{test_request_id}/tests")
def request_tests(test_request_id: int):
    """No rates on purpose (same rule as gr_workflow.allocate: technicians never see prices)."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT tri.tri_id, qi.description, qi.test_standard, tri.quantity
            FROM test_request_items tri
            JOIN quotation_items qi ON qi.item_id = tri.quotation_item_id
            WHERE tri.test_request_id = %s
            ORDER BY tri.tri_id
            """,
            (test_request_id,),
        )
        return {"items": [
            {"tri_id": r[0], "description": r[1], "test_standard": r[2], "quantity": r[3] if r[3] is not None else 1}
            for r in cur.fetchall()
        ]}
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()


class ProjectItemCreate(BaseModel):
    catalog_id: Optional[int] = None
    description: Optional[str] = None
    test_standard: Optional[str] = None
    unit_rate: Optional[float] = None
    net_unit: Optional[str] = None
    quantity: int = 1


@router.post("/project/{project_id}/items", summary="Add a test to a NON walk-in project")
def add_project_item(project_id: int, payload: ProjectItemCreate):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT quotation_id, COALESCE(is_walk_in, FALSE) FROM projects WHERE project_id = %s", (project_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Project not found")
        quotation_id, is_walk_in = row
        if is_walk_in:
            raise HTTPException(400, "This is a walk-in LP — add tests from the Walk-In Customers screen.")
        if not quotation_id:
            raise HTTPException(400, "This project has no quotation to add tests to.")
        if payload.quantity <= 0:
            raise HTTPException(400, "Quantity must be > 0")

        if payload.catalog_id:
            cur.execute(
                "SELECT code, description, test_standard, unit, unit_rate FROM price_catalog WHERE catalog_id = %s AND active = TRUE",
                (payload.catalog_id,),
            )
            pc = cur.fetchone()
            if not pc:
                raise HTTPException(404, "Catalog test not found")
            code, desc, std, unit, rate = pc
        else:
            if not _clean(payload.description):
                raise HTTPException(400, "Description is required")
            if payload.unit_rate is None or payload.unit_rate < 0:
                raise HTTPException(400, "A valid unit rate is required for a custom test")
            code, desc, std, unit, rate = None, _clean(payload.description), payload.test_standard, None, payload.unit_rate

        cur.execute(
            "SELECT 1 FROM quotation_items WHERE quotation_id = %s AND LOWER(TRIM(description)) = LOWER(TRIM(%s)) LIMIT 1",
            (quotation_id, desc),
        )
        if cur.fetchone():
            raise HTTPException(400, f"'{desc}' is already on this project. Increase its Qty instead.")

        cur.execute(
            """
            INSERT INTO quotation_items
                (quotation_id, catalog_id, item_code, description, test_standard, unit, unit_rate, quantity, net_unit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING item_id
            """,
            (quotation_id, payload.catalog_id, code, desc, payload.test_standard or std, unit, rate, payload.quantity, payload.net_unit),
        )
        item_id = cur.fetchone()[0]
        cur.execute(
            """
            UPDATE quotations SET total_amount = sub.t, vat = sub.t * 0.05, grand_total = sub.t * 1.05
            FROM (SELECT COALESCE(SUM(COALESCE(unit_rate,0) * GREATEST(COALESCE(quantity,1),1)),0) AS t
                  FROM quotation_items WHERE quotation_id = %s) sub
            WHERE quotation_id = %s
            """,
            (quotation_id, quotation_id),
        )
        conn.commit()
        return {"item_id": item_id, "message": "Test added"}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        conn.close()