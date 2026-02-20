from fastapi import APIRouter, HTTPException, Form
from db import get_connection

router = APIRouter(tags=["1. Auth"])

@router.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    conn = get_connection()
    cur = conn.cursor()
    
    # FIXED: Get user_role instead of role_id
    cur.execute("""
        SELECT user_id, username, password_hash, user_role, full_name
        FROM users 
        WHERE username = %s AND is_active = true
    """, (username,))
    
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row or row[2] != password:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    return {
        "message": "Login successful",
        "user_id": row[0],
        "username": row[1],
        "role": row[3],  # user_role ("MANAGER", "SUPERVISOR", "CHEMIST")
        "full_name": row[4]  # optional: include full name
    }


@router.get("/users/all")
def get_all_users():
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT user_id, username, full_name, user_role
            FROM users 
            WHERE is_active = true
            ORDER BY username
        """)
        
        users = []
        for row in cur.fetchall():
            users.append({
                "user_id": row[0],
                "username": row[1],
                "full_name": row[2],
                "user_role": row[3]
            })
        
        return users
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching users: {str(e)}")
    finally:
        cur.close()
        conn.close()