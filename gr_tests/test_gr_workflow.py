"""
Tests for gr_workflow.py

Pure-function tests always run.

DB-backed tests run only when GEL_TEST_DATABASE_URL points at a SCRATCH
Postgres database (they DROP SCHEMA public). To set one up:

    createdb gel_test
    psql gel_test -f gr_tests/baseline_schema.sql
    psql gel_test -f ../migrations/001_multi_gr_tbr.sql
    GEL_TEST_DATABASE_URL=postgresql://user:pass@localhost/gel_test pytest gr_tests/ -v

Run from the backend folder (the one containing gr_workflow.py).
"""
import os
import sys
import threading
import types
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = os.environ.get("GEL_TEST_DATABASE_URL")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB
else:
    os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")

# --- stubs for heavy modules the router imports lazily --------------------
_tests_stub = types.ModuleType("tests")
_tests_stub.GROUP_FILTER_MAP = {"soil": ["Soil", "Aggregate"], "water": ["Water"]}
sys.modules.setdefault("tests", _tests_stub)


class _FakeBucket:
    def __init__(self):
        self.uploads = []

    def upload(self, path, file, file_options=None):
        self.uploads.append(path)

    def get_public_url(self, path):
        return f"https://storage.example/{path}"


class _FakeStorage:
    def __init__(self):
        self.bucket = _FakeBucket()

    def from_(self, _name):
        return self.bucket


_reports_stub = types.ModuleType("reports")
_reports_stub.supabase = types.SimpleNamespace(storage=_FakeStorage())
sys.modules["reports"] = _reports_stub

import gr_workflow as gw  # noqa: E402


# ===========================================================================
# Pure helpers
# ===========================================================================
@pytest.mark.parametrize("text,expected", [
    ("3", (3, 3)), ("1-3", (1, 3)), (" 9 ", (9, 9)), ("1 - 3", (1, 3)), ("4\u20136", (4, 6)),
])
def test_parse_range_ok(text, expected):
    assert gw.parse_range(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "0", "3-1", "1-", "-2", "1,2", "1-2-3", None])
def test_parse_range_bad(text):
    with pytest.raises(ValueError):
        gw.parse_range(text)


def test_labels_match_spec_examples():
    assert gw.range_label("GS-101126-001", 1, 3) == "GS-101126-001/1-3"
    assert gw.range_label("GS-101126-001", 9, 9) == "GS-101126-001/9"
    assert gw.range_label("GS-101126-001", 10, 10) == "GS-101126-001/10"


@pytest.mark.parametrize("gq,expected", [
    ("GQ-101126-01", "GS-101126-001"),
    ("GQ-101126-010", "GS-101126-010"),
    ("GQ-060726-1", "GS-060726-001"),
    ("GQ-101126-123", "GS-101126-123"),
])
def test_sample_base_from_gq(gq, expected):
    assert gw.sample_base_for_gq(gq) == expected


def test_gr_number_format():
    assert gw.format_gr_no(date(2026, 11, 15), 1) == "GR-151126-001"
    assert gw.format_gr_no(date(2026, 11, 10), 12) == "GR-101126-012"
    assert gw.format_gr_no(date(2026, 1, 5), 1000) == "GR-050126-1000"


def test_collapse_numbers():
    assert gw.collapse_numbers([1, 2, 3, 5, 9, 10]) == [(1, 3), (5, 5), (9, 10)]
    assert gw.collapse_numbers([]) == []


def test_ranges_within_batches():
    assert gw.ranges_within_batches(1, 10, [(1, 10)])
    assert not gw.ranges_within_batches(1, 11, [(1, 10)])
    assert gw.ranges_within_batches(9, 12, [(1, 10), (11, 15)])
    assert not gw.ranges_within_batches(1, 3, [])


# ===========================================================================
# DB-backed workflow
# ===========================================================================
db = pytest.mark.skipif(not TEST_DB, reason="set GEL_TEST_DATABASE_URL to a scratch Postgres DB")


@pytest.fixture()
def conn():
    import psycopg2
    c = psycopg2.connect(TEST_DB)
    c.autocommit = True
    cur = c.cursor()
    cur.execute("""
        TRUNCATE reports, samples, test_instances, gq_sample_batches, gr_reports,
                 test_request_items, test_requests, quotation_items, projects, quotations,
                 price_catalog, users RESTART IDENTITY CASCADE
    """)
    yield c
    c.close()


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(gw.router)
    return TestClient(app)


def _seed(conn, psd_qty=5, chem_qty=2, gqs=("GQ-101126-01", "GQ-101126-02")):
    cur = conn.cursor()
    cur.execute("INSERT INTO users (username, user_role) VALUES ('u1','chemist') RETURNING user_id")
    uid = cur.fetchone()[0]
    cur.execute("INSERT INTO quotations (quotation_no, status) VALUES ('Q-1','APPROVED') RETURNING quotation_id")
    qid = cur.fetchone()[0]
    cur.execute("INSERT INTO price_catalog (code, description, group_name) VALUES ('PSD','PSD','Aggregate') RETURNING catalog_id")
    cat_psd = cur.fetchone()[0]
    cur.execute("INSERT INTO price_catalog (code, description, group_name) VALUES ('CHEM','Chem','Water') RETURNING catalog_id")
    cat_chem = cur.fetchone()[0]
    cur.execute("""INSERT INTO quotation_items (quotation_id, catalog_id, item_code, description, test_standard, unit_rate, quantity)
                   VALUES (%s,%s,'PSD','Particle Size Distribution','BS 812',100,%s) RETURNING item_id""", (qid, cat_psd, psd_qty))
    psd = cur.fetchone()[0]
    cur.execute("""INSERT INTO quotation_items (quotation_id, catalog_id, item_code, description, test_standard, unit_rate, quantity)
                   VALUES (%s,%s,'CHEM','Chloride Content','BS 812',50,%s) RETURNING item_id""", (qid, cat_chem, chem_qty))
    chem = cur.fetchone()[0]
    cur.execute("INSERT INTO projects (project_no, quotation_id, project_name, client_name, lpo_no) VALUES ('LP-1',%s,'Tower A','ACME','LPO-9') RETURNING project_id", (qid,))
    pid = cur.fetchone()[0]
    ids = []
    for no in gqs:
        cur.execute("INSERT INTO test_requests (project_id, request_no, status, template_type) VALUES (%s,%s,'PENDING_SAMPLES','soil') RETURNING test_request_id", (pid, no))
        ids.append(cur.fetchone()[0])
    return {"uid": uid, "qid": qid, "psd": psd, "chem": chem, "gq_ids": ids, "gq_nos": list(gqs)}


@db
def test_quantity_creates_instances_and_greys_out(conn, client):
    d = _seed(conn)
    r = client.get(f"/gr-workflow/test-requests/{d['gq_ids'][0]}/available-tests").json()
    psd = next(t for t in r["tests"] if t["item_id"] == d["psd"])
    assert (psd["total_qty"], psd["remaining_qty"], psd["is_exhausted"]) == (5, 5, False)
    # group filter: soil template hides the 'Water' group test
    assert all(t["item_id"] != d["chem"] for t in r["tests"])

    ok = client.post(f"/gr-workflow/test-requests/{d['gq_ids'][0]}/allocate",
                     json={"items": [{"quotation_item_id": d["psd"], "quantity": 3}]})
    assert ok.status_code == 200, ok.text

    # GQ2 sees 2 left, and can't take 3
    r2 = client.get(f"/gr-workflow/test-requests/{d['gq_ids'][1]}/available-tests").json()
    psd2 = next(t for t in r2["tests"] if t["item_id"] == d["psd"])
    assert psd2["remaining_qty"] == 2 and psd2["allocated_qty"] == 3 and psd2["allocated_to_this_gq"] == 0
    bad = client.post(f"/gr-workflow/test-requests/{d['gq_ids'][1]}/allocate",
                      json={"items": [{"quotation_item_id": d["psd"], "quantity": 3}]})
    assert bad.status_code == 400 and "only 2" in bad.json()["detail"]

    assert client.post(f"/gr-workflow/test-requests/{d['gq_ids'][1]}/allocate",
                       json={"items": [{"quotation_item_id": d["psd"], "quantity": 2}]}).status_code == 200
    r3 = client.get(f"/gr-workflow/test-requests/{d['gq_ids'][0]}/available-tests").json()
    assert next(t for t in r3["tests"] if t["item_id"] == d["psd"])["is_exhausted"] is True


@db
def test_allocate_same_test_twice_in_one_gq_merges_into_one_tri(conn, client):
    d = _seed(conn)
    gq = d["gq_ids"][0]
    for q in (2, 1):
        assert client.post(f"/gr-workflow/test-requests/{gq}/allocate",
                           json={"items": [{"quotation_item_id": d["psd"], "quantity": q}]}).status_code == 200
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), SUM(quantity) FROM test_request_items WHERE test_request_id=%s", (gq,))
    assert cur.fetchone() == (1, 3)


@db
def test_legacy_gq_items_keep_their_share(conn, client):
    """A GQ created before this feature only has a tri row with a quantity."""
    d = _seed(conn)
    cur = conn.cursor()
    cur.execute("INSERT INTO test_request_items (test_request_id, quotation_item_id, quantity) VALUES (%s,%s,5)", (d["gq_ids"][0], d["psd"]))
    r = client.get(f"/gr-workflow/test-requests/{d['gq_ids'][1]}/available-tests").json()
    psd = next(t for t in r["tests"] if t["item_id"] == d["psd"])
    assert psd["remaining_qty"] == 0 and psd["is_exhausted"]


@db
def test_quantity_increase_on_quotation_adds_instances(conn, client):
    d = _seed(conn, psd_qty=2)
    client.get(f"/gr-workflow/test-requests/{d['gq_ids'][0]}/available-tests")
    conn.cursor().execute("UPDATE quotation_items SET quantity = 4 WHERE item_id = %s", (d["psd"],))
    r = client.get(f"/gr-workflow/test-requests/{d['gq_ids'][0]}/available-tests").json()
    assert next(t for t in r["tests"] if t["item_id"] == d["psd"])["remaining_qty"] == 4


def _allocate(client, d, gq_idx, **qtys):
    items = [{"quotation_item_id": d[k], "quantity": v} for k, v in qtys.items()]
    r = client.post(f"/gr-workflow/test-requests/{d['gq_ids'][gq_idx]}/allocate", json={"items": items})
    assert r.status_code == 200, r.text


@db
def test_sample_batches_continue_numbering(client, conn):
    d = _seed(conn)
    gq = d["gq_nos"][0]
    r = client.post(f"/gr-workflow/gq/{gq}/sample-batch", json={"total": 10}).json()
    assert r["first"] == "GS-101126-001/1" and r["last"] == "GS-101126-001/10"
    assert len(r["overview"]["identifiers"]) == 10
    r2 = client.post(f"/gr-workflow/gq/{gq}/sample-batch", json={"total": 5}).json()
    assert r2["first"] == "GS-101126-001/11" and r2["last"] == "GS-101126-001/15"
    assert client.post(f"/gr-workflow/gq/{gq}/sample-batch", json={"total": 0}).status_code == 422


@db
def test_range_mapping_matches_spec_examples(client, conn):
    d = _seed(conn, psd_qty=2, chem_qty=2)
    gq, gq_id = d["gq_nos"][0], d["gq_ids"][0]
    # use group-less allocation: allocate works across groups
    _allocate(client, d, 0, psd=2, chem=2)
    client.post(f"/gr-workflow/gq/{gq}/sample-batch", json={"total": 10})
    ov = client.get(f"/gr-workflow/gq/{gq}/overview").json()
    psd = [i for i in ov["instances"] if i["test_name"].startswith("Particle")]
    chem = [i for i in ov["instances"] if i["test_name"].startswith("Chloride")]
    assert len(psd) == 2 and len(chem) == 2

    r = client.post(f"/gr-workflow/gq/{gq}/map-samples", json={
        "collected_by": "Ali",
        "mappings": [
            {"instance_id": psd[0]["instance_id"], "range": "1-3", "department": "GeoTech", "sample_type": "20mm"},
            {"instance_id": chem[0]["instance_id"], "range": "9", "department": "Chemical"},
            {"instance_id": chem[1]["instance_id"], "range": "10", "department": "Chemical"},
        ]})
    assert r.status_code == 200, r.text
    labels = sorted(s["label"] for s in r.json()["samples"])
    assert labels == ["GS-101126-001/1-3", "GS-101126-001/10", "GS-101126-001/9"]

    cur = conn.cursor()
    cur.execute("SELECT physical_sample_no, range_start, range_end, sample_type, department, status, report_status "
                "FROM samples WHERE request_id=%s ORDER BY range_start", (gq_id,))
    rows = cur.fetchall()
    assert rows[0] == ("GS-101126-001/1-3", 1, 3, "20mm", "GeoTech", "PENDING", "PENDING")

    # many-to-many: the SAME range can also feed a different test
    r2 = client.post(f"/gr-workflow/gq/{gq}/map-samples", json={"mappings": [
        {"instance_id": psd[1]["instance_id"], "range": "1-3", "department": "GeoTech", "sample_type": "10mm"}]})
    assert r2.status_code == 200
    assert r2.json()["samples"][0]["label"] == "GS-101126-001/1-3"
    assert r2.json()["samples"][0]["sample_no"] == "GS-101126-001/1-3-2"  # unique sample_no


@db
def test_mapping_validation(client, conn):
    d = _seed(conn)
    gq = d["gq_nos"][0]
    _allocate(client, d, 0, psd=2)
    client.post(f"/gr-workflow/gq/{gq}/sample-batch", json={"total": 4})
    ov = client.get(f"/gr-workflow/gq/{gq}/overview").json()
    a, b = ov["instances"][0]["instance_id"], ov["instances"][1]["instance_id"]
    url = f"/gr-workflow/gq/{gq}/map-samples"

    assert client.post(url, json={"mappings": []}).status_code == 400
    assert client.post(url, json={"mappings": [{"instance_id": a, "range": "1-9", "department": "GeoTech"}]}).status_code == 400
    assert client.post(url, json={"mappings": [{"instance_id": a, "range": "x", "department": "GeoTech"}]}).status_code == 400
    assert client.post(url, json={"mappings": [{"instance_id": a, "range": "1", "department": " "}]}).status_code == 400
    assert client.post(url, json={"mappings": [{"instance_id": 99999, "range": "1", "department": "GeoTech"}]}).status_code == 400
    # same instance twice in one payload
    assert client.post(url, json={"mappings": [
        {"instance_id": a, "range": "1", "department": "GeoTech"},
        {"instance_id": a, "range": "2", "department": "GeoTech"}]}).status_code == 400
    # all-or-nothing: second row invalid => first row must NOT be saved
    assert client.post(url, json={"mappings": [
        {"instance_id": a, "range": "1", "department": "GeoTech"},
        {"instance_id": b, "range": "7", "department": "GeoTech"}]}).status_code == 400
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM samples")
    assert cur.fetchone()[0] == 0
    # valid, then can't be mapped a second time
    assert client.post(url, json={"mappings": [{"instance_id": a, "range": "1", "department": "GeoTech"}]}).status_code == 200
    assert client.post(url, json={"mappings": [{"instance_id": a, "range": "2", "department": "GeoTech"}]}).status_code == 400

    # an instance from another GQ is rejected
    _allocate(client, d, 1, chem=1)
    other = client.get(f"/gr-workflow/gq/{d['gq_nos'][1]}/overview").json()["instances"][0]["instance_id"]
    assert client.post(url, json={"mappings": [{"instance_id": other, "range": "2", "department": "GeoTech"}]}).status_code == 400


def _ready_gq(client, d, psd=3, chem=2, total=10):
    """Allocate, generate identifiers, map PSD->1-3(+...) and chem->9,10; return sample ids."""
    gq = d["gq_nos"][0]
    _allocate(client, d, 0, psd=psd, chem=chem)
    client.post(f"/gr-workflow/gq/{gq}/sample-batch", json={"total": total})
    ov = client.get(f"/gr-workflow/gq/{gq}/overview").json()
    psds = [i for i in ov["instances"] if i["test_name"].startswith("Particle")]
    chems = [i for i in ov["instances"] if i["test_name"].startswith("Chloride")]
    maps = [{"instance_id": psds[0]["instance_id"], "range": "1-3", "department": "GeoTech", "sample_type": "20mm"},
            {"instance_id": psds[1]["instance_id"], "range": "4-6", "department": "GeoTech", "sample_type": "10mm"},
            {"instance_id": chems[0]["instance_id"], "range": "9", "department": "Chemical"},
            {"instance_id": chems[1]["instance_id"], "range": "10", "department": "Chemical"}]
    r = client.post(f"/gr-workflow/gq/{gq}/map-samples", json={"mappings": maps})
    assert r.status_code == 200, r.text
    return {s["label"].split("/")[-1]: s["sample_id"] for s in r.json()["samples"]}, gq


@db
def test_multi_gr_per_gq_and_tbr_reanchoring(client, conn, monkeypatch):
    d = _seed(conn, psd_qty=3, chem_qty=2)
    ids, gq = _ready_gq(client, d)
    today = gw.lab_today()

    # Batch A -> GR-...-001, Batch B -> GR-...-002 (same GQ, same day)
    a = client.post("/gr-workflow/generate-gr", json={"request_no": gq, "sample_ids": [ids["1-3"]], "created_by": d["uid"]})
    assert a.status_code == 200, a.text
    assert a.json()["gr_no"] == gw.format_gr_no(today, 1)
    b = client.post("/gr-workflow/generate-gr", json={"request_no": gq, "sample_ids": [ids["9"], ids["10"]]})
    assert b.json()["gr_no"] == gw.format_gr_no(today, 2) and b.json()["sample_count"] == 2

    # Mark the rest TBR; they must not be in either GR
    t = client.post("/gr-workflow/tbr/mark", json={"request_no": gq, "sample_ids": [ids["4-6"]], "reason": "awaiting oven", "marked_by": d["uid"]})
    assert t.status_code == 200 and t.json()["updated"] == 1
    ws = client.get(f"/gr-workflow/gq/{gq}/gr-workspace").json()
    assert [r["label"] for r in ws["tbr"]] == ["GS-101126-001/4-6"]
    assert ws["tbr"][0]["tbr_reason"] == "awaiting oven"
    assert len(ws["in_gr"]) == 3 and len(ws["pending"]) == 0 and len(ws["grs"]) == 2

    # A TBR item can't be put in a GR twice or marked TBR again once reported
    assert client.post("/gr-workflow/generate-gr", json={"request_no": gq, "sample_ids": [ids["1-3"]]}).status_code == 400
    assert client.post("/gr-workflow/tbr/mark", json={"request_no": gq, "sample_ids": [ids["1-3"]]}).status_code == 400

    # "Future date": TBR item is reported => new GR anchored to THAT date, seq restarts at 001
    future = today + timedelta(days=5)
    monkeypatch.setattr(gw, "lab_today", lambda: future)
    c = client.post("/gr-workflow/generate-gr", json={"request_no": gq, "sample_ids": [ids["4-6"]]})
    assert c.status_code == 200, c.text
    assert c.json()["gr_no"] == gw.format_gr_no(future, 1)
    assert c.json()["gr_no"].startswith("GR-")
    cur = conn.cursor()
    cur.execute("SELECT report_status, tbr_reason FROM samples WHERE sample_id=%s", (ids["4-6"],))
    assert cur.fetchone() == ("IN_GR", None)  # TBR flags cleared once reported


@db
def test_tbr_clear_and_list(client, conn):
    d = _seed(conn, psd_qty=3, chem_qty=2)
    ids, gq = _ready_gq(client, d)
    client.post("/gr-workflow/tbr/mark", json={"request_no": gq, "sample_ids": [ids["9"], ids["10"]], "reason": "x"})
    lst = client.get("/gr-workflow/tbr").json()
    assert {r["label"] for r in lst} == {"GS-101126-001/9", "GS-101126-001/10"}
    assert client.post("/gr-workflow/tbr/clear", json={"request_no": gq, "sample_ids": [ids["9"]]}).json()["updated"] == 1
    assert len(client.get("/gr-workflow/tbr").json()) == 1


@db
def test_cannot_report_rejected_or_foreign_samples(client, conn):
    d = _seed(conn, psd_qty=3, chem_qty=2)
    ids, gq = _ready_gq(client, d)
    conn.cursor().execute("UPDATE samples SET status='REJECTED' WHERE sample_id=%s", (ids["9"],))
    r = client.post("/gr-workflow/generate-gr", json={"request_no": gq, "sample_ids": [ids["9"]]})
    assert r.status_code == 400 and "rejected" in r.json()["detail"]
    assert client.post("/gr-workflow/generate-gr", json={"request_no": gq, "sample_ids": []}).status_code == 400
    assert client.post("/gr-workflow/generate-gr", json={"request_no": gq, "sample_ids": [987654]}).status_code == 404
    assert client.post("/gr-workflow/generate-gr", json={"request_no": "GQ-NOPE", "sample_ids": [1]}).status_code == 404


@db
def test_legacy_reported_samples_are_not_offered(client, conn):
    d = _seed(conn, psd_qty=3, chem_qty=2)
    ids, gq = _ready_gq(client, d)
    cur = conn.cursor()
    cur.execute("INSERT INTO reports (report_no, sample_id, status) VALUES ('GR - 010126 - 001', %s, 'APPROVED')", (ids["9"],))
    ws = client.get(f"/gr-workflow/gq/{gq}/gr-workspace").json()
    assert "GS-101126-001/9" not in [r["label"] for r in ws["pending"]]
    r = client.post("/gr-workflow/generate-gr", json={"request_no": gq, "sample_ids": [ids["9"]]})
    assert r.status_code == 400 and "Already reported" in r.json()["detail"]


@db
def test_parallel_gr_generation_never_duplicates(client, conn):
    d = _seed(conn, psd_qty=3, chem_qty=2)
    ids, gq = _ready_gq(client, d)
    results, lock = [], threading.Lock()

    def go(sid):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        app = FastAPI(); app.include_router(gw.router)
        r = TestClient(app).post("/gr-workflow/generate-gr", json={"request_no": gq, "sample_ids": [sid]})
        with lock:
            results.append(r)

    ts = [threading.Thread(target=go, args=(sid,)) for sid in ids.values()]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    nos = [r.json()["gr_no"] for r in results]
    assert len(set(nos)) == len(nos) == 4
    assert sorted(int(n.split("-")[-1]) for n in nos) == [1, 2, 3, 4]


@db
def test_upload_lineage_submit(client, conn):
    d = _seed(conn, psd_qty=3, chem_qty=2)
    ids, gq = _ready_gq(client, d)
    gr = client.post("/gr-workflow/generate-gr", json={"request_no": gq, "sample_ids": [ids["1-3"], ids["4-6"]]}).json()["gr_no"]

    s = client.get("/gr-workflow/gr/search", params={"q": "tower"}).json()
    assert [x["gr_no"] for x in s] == [gr] and s[0]["status"] == "GENERATED" and s[0]["sample_count"] == 2
    assert client.get("/gr-workflow/gr/search", params={"q": gr}).json()[0]["gr_no"] == gr
    assert len(client.get("/gr-workflow/gr/search").json()) == 1

    lin = client.get(f"/gr-workflow/gr/{gr}").json()
    assert lin["project"]["project_no"] == "LP-1" and lin["project"]["quotation_no"] == "Q-1"
    assert lin["gq"]["request_no"] == gq
    assert [i["label"] for i in lin["items"]] == ["GS-101126-001/1-3", "GS-101126-001/4-6"]
    assert lin["can_upload"] is True and lin["report"] is None and lin["status"] == "GENERATED"

    up = client.post(f"/gr-workflow/gr/{gr}/upload", data={"uploaded_by": d["uid"], "notes": "n"},
                     files={"file": ("final.pdf", b"%PDF-1.4 test", "application/pdf")})
    assert up.status_code == 200, up.text
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE linked_to_report_id IS NULL), MIN(status) FROM reports WHERE report_no=%s", (gr,))
    assert cur.fetchone() == (2, 1, "DRAFT")

    # one file per GR
    again = client.post(f"/gr-workflow/gr/{gr}/upload", data={"uploaded_by": d["uid"]},
                        files={"file": ("x.pdf", b"x", "application/pdf")})
    assert again.status_code == 400
    # empty file rejected on a different GR
    gr2 = client.post("/gr-workflow/generate-gr", json={"request_no": gq, "sample_ids": [ids["9"]]}).json()["gr_no"]
    assert client.post(f"/gr-workflow/gr/{gr2}/upload", data={"uploaded_by": d["uid"]},
                       files={"file": ("e.pdf", b"", "application/pdf")}).status_code == 400

    lin2 = client.get(f"/gr-workflow/gr/{gr}").json()
    assert lin2["status"] == "DRAFT" and lin2["can_submit"] is True and lin2["can_upload"] is False

    sub = client.post(f"/gr-workflow/gr/{gr}/submit", params={"checked_by": d["uid"]})
    assert sub.status_code == 200
    cur.execute("SELECT DISTINCT status FROM reports WHERE report_no=%s", (gr,))
    assert cur.fetchall() == [("UNDER_REVIEW",)]
    assert client.post(f"/gr-workflow/gr/{gr}/submit", params={"checked_by": d["uid"]}).status_code == 400
    assert client.get(f"/gr-workflow/gr/{gr}").json()["status"] == "UNDER_REVIEW"

    # the existing approve endpoint (UPDATE ... WHERE report_no=%s AND status='UNDER_REVIEW')
    # now approves every row; status shown here follows automatically
    cur.execute("UPDATE reports SET status='APPROVED', is_locked=TRUE WHERE report_no=%s", (gr,))
    assert client.get(f"/gr-workflow/gr/{gr}").json()["status"] == "APPROVED"
    assert client.post(f"/gr-workflow/gr/{gr}/submit", params={"checked_by": d["uid"]}).status_code == 400
    assert client.get("/gr-workflow/gr/GR-000000-999").status_code == 404


@db
def test_rebalance_tri_shrink_grow_and_guard(client, conn):
    d = _seed(conn, psd_qty=4, chem_qty=1)
    _allocate(client, d, 0, psd=4)
    cur = conn.cursor()
    cur.execute("SELECT tri_id FROM test_request_items WHERE test_request_id=%s", (d["gq_ids"][0],))
    tri = cur.fetchone()[0]

    # shrink 4 -> 2 (quotation qty is rewritten by tests.py first, then rebalance runs)
    cur.execute("UPDATE test_request_items SET quantity=2 WHERE tri_id=%s", (tri,))
    cur.execute("UPDATE quotation_items SET quantity=2 WHERE item_id=%s", (d["psd"],))
    c = gw.get_connection(); cc = c.cursor()
    gw.rebalance_tri(cc, tri); c.commit()
    cur.execute("SELECT COUNT(*) FROM test_instances WHERE tri_id=%s", (tri,))
    assert cur.fetchone()[0] == 2

    # grow 2 -> 3
    cur.execute("UPDATE test_request_items SET quantity=3 WHERE tri_id=%s", (tri,))
    cur.execute("UPDATE quotation_items SET quantity=3 WHERE item_id=%s", (d["psd"],))
    gw.rebalance_tri(cc, tri); c.commit()
    cur.execute("SELECT COUNT(*) FROM test_instances WHERE tri_id=%s", (tri,))
    assert cur.fetchone()[0] == 3

    # can't shrink below instances that already have samples
    gq = d["gq_nos"][0]
    client.post(f"/gr-workflow/gq/{gq}/sample-batch", json={"total": 3})
    inst = client.get(f"/gr-workflow/gq/{gq}/overview").json()["instances"]
    client.post(f"/gr-workflow/gq/{gq}/map-samples", json={"mappings": [
        {"instance_id": i["instance_id"], "range": str(n + 1), "department": "GeoTech"} for n, i in enumerate(inst)]})
    cur.execute("UPDATE test_request_items SET quantity=1 WHERE tri_id=%s", (tri,))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        gw.rebalance_tri(cc, tri)
    assert e.value.status_code == 400
    c.rollback(); c.close()


@db
def test_assert_sole_holder(conn, client):
    d = _seed(conn, psd_qty=4)
    _allocate(client, d, 0, psd=2)
    cur = conn.cursor()
    cur.execute("SELECT tri_id FROM test_request_items WHERE test_request_id=%s", (d["gq_ids"][0],))
    tri = cur.fetchone()[0]
    c = gw.get_connection(); cc = c.cursor()
    gw.assert_sole_holder(cc, tri)  # only GQ1 holds it -> fine
    _allocate(client, d, 1, psd=1)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        gw.assert_sole_holder(cc, tri)
    assert "split across" in e.value.detail
    c.close()
