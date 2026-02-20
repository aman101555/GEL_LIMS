# utils.py
import os
import sys

def resource_path(relative_path):
    """
    Get absolute path to resource (works in dev AND PyInstaller EXE)
    """
    base_path = getattr(sys, '_MEIPASS', os.path.abspath("."))
    
    # For templates folder
    if relative_path.startswith("templates/"):
        # In EXE, templates are at same level
        if hasattr(sys, "_MEIPASS"):
            # PyInstaller extracts to temp folder
            return os.path.join(sys._MEIPASS, "templates", *relative_path.split("/")[1:])
        else:
            return os.path.join(os.path.abspath("."), relative_path)
    
    # For regular files
    return os.path.join(base_path, relative_path)

def get_template_path(template_name):
    """Get path for template files"""
    return resource_path(f"templates/{template_name}")