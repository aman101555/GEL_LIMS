import psycopg2
import os
import sys
from dotenv import load_dotenv

# 1. Determine where the EXE or Script is sitting
if getattr(sys, 'frozen', False):
    # If running as EXE
    base_path = os.path.dirname(sys.executable)
else:
    # If running as .py script
    base_path = os.path.dirname(os.path.abspath(__file__))

# 2. Force load the .env from that specific folder
env_path = os.path.join(base_path, '.env')
load_dotenv(env_path, override=True) 

def get_connection():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise Exception(f"DATABASE_URL not found in {env_path}")
    return psycopg2.connect(url)