# main.py
import sys
import io
import os
import webbrowser
import threading
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

# --- 1. STREAM FIX FOR PYINSTALLER --noconsole MODE ---
# Prevents crash: 'NoneType' object has no attribute 'encoding'
if sys.stdout is None: sys.stdout = io.StringIO()
if sys.stderr is None: sys.stderr = io.StringIO()

# --- 2. IMPORT ROUTERS ---
from auth import router as auth_router
from enquiries import router as enquiry_router
from quotations import router as quotation_router
from projects import router as project_router
from tests import router as test_request_router
from samples_workflow import router as samples_workflow_router
from invoices import router as invoice_router
from reports import router as reports_router
from search import router as search_router

app = FastAPI(title="GEL LIMS API")

# --- 3. CORS MIDDLEWARE ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 4. PATH RESOLUTION LOGIC ---
def get_base_dir():
    """
    Returns the base directory. 
    In PyInstaller, sys._MEIPASS is the temp folder where bundled files are extracted.
    """
    if hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

def get_exe_location():
    """
    Returns the actual folder where the .exe file is sitting.
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

# DIST_PATH is inside the bundle (Internal)
BASE_DIR = get_base_dir()
DIST_PATH = os.path.join(BASE_DIR, "dist")

# WRITEABLE folders are outside the bundle (External)
EXE_DIR = get_exe_location()
folders = ["uploads/reports", "generated_invoices", "generated_delivery_notes", "temp_filled_worksheets", "generated_proforma"]
for folder in folders:
    os.makedirs(os.path.join(EXE_DIR, folder), exist_ok=True)

# --- 5. ROUTER REGISTRATION (API ROUTES FIRST) ---
app.include_router(auth_router, prefix="/auth")
app.include_router(enquiry_router)  # Already has /enquiries in its file
app.include_router(quotation_router)  # Already has /quotations in its file
app.include_router(project_router)  # Already has /projects in its file
app.include_router(test_request_router)  # Already has /test-requests in its file
app.include_router(samples_workflow_router)  # Already has /samples-workflow in its file
app.include_router(invoice_router)  # Already has /invoices in its file
app.include_router(reports_router, prefix="/reports")
app.include_router(search_router, prefix="/search")

# --- 6. SERVE STATIC ASSETS ---
if os.path.exists(DIST_PATH) and os.path.exists(os.path.join(DIST_PATH, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(DIST_PATH, "assets")), name="assets")

# --- 7. API HEALTH CHECK (OPTIONAL) ---
@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "api": "running", "frontend": os.path.exists(DIST_PATH)}

# --- 8. REACT CATCH-ALL ROUTE (MUST BE LAST!) ---
# This catches any routes not matched above and serves the React app
@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    """
    Serve React app for any non-API routes.
    This only runs if no other route (API, docs, static files) matches.
    """
    # Let FastAPI handle its own docs paths automatically
    # This function only runs for paths not matched by any previous route
    
    index_path = os.path.join(DIST_PATH, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    
    return {
        "message": "GEL LIMS Backend is running",
        "frontend_status": f"Frontend not found at {index_path}",
        "note": "API endpoints available at /docs, /redoc, /openapi.json"
    }

# --- 9. RUN SERVER ---
if __name__ == "__main__":
    # Auto-open browser after 3 seconds if running as EXE
    if getattr(sys, 'frozen', False):
        threading.Thread(
            target=lambda: (
                import_time := __import__('time'), 
                import_time.sleep(3), 
                webbrowser.open("http://127.0.0.1:8000")
            ), 
            daemon=True
        ).start()
    
    uvicorn.run(app, host="0.0.0.0", port=8000)