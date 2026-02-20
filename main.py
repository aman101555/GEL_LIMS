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

# --- 5. ROUTER REGISTRATION ---
app.include_router(auth_router, prefix="/auth")
app.include_router(enquiry_router)
app.include_router(quotation_router) # Already has /quotations in its file
app.include_router(project_router)
app.include_router(test_request_router) # Already has /test-requests in its file
app.include_router(samples_workflow_router) # Already has /samples-workflow in its file
app.include_router(invoice_router) # Already has /invoices in its file
app.include_router(reports_router, prefix="/reports")
app.include_router(search_router, prefix="/search")

# --- 6. SERVE STATIC ASSETS ---
if os.path.exists(DIST_PATH) and os.path.exists(os.path.join(DIST_PATH, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(DIST_PATH, "assets")), name="assets")

# --- 7. REACT CATCH-ALL ROUTE ---
@app.get("/{full_path:path}")
async def serve_react_or_api(full_path: str):
    # If the path starts with these, it's a broken API call, not a React route
    api_prefixes = ['auth', 'reports', 'invoices', 'search', 'enquiries', 'quotations', 'projects', 'test-requests', 'samples-workflow', 'api']
    if any(full_path.startswith(prefix) for prefix in api_prefixes):
        raise HTTPException(status_code=404, detail="API route not found")
    
    index_path = os.path.join(DIST_PATH, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    
    return {
        "message": "GEL LIMS Backend is running",
        "frontend_status": "Not Found at " + index_path
    }

# --- 8. RUN SERVER ---
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