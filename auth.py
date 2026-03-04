from fastapi import APIRouter, HTTPException, Form
from db import get_connection
import hashlib

router = APIRouter(tags=["1. Auth"])

@router.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    conn = get_connection()
    cur = conn.cursor()
    
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
        "role": row[3],  # user_role ("MANAGER", "SUPERVISOR", "CHEMIST", "super_admin")
        "full_name": row[4]
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


@router.get("/roles")
def get_all_roles():
    """Fetch all roles from the roles table for the dropdown."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT role_id, role_name FROM roles ORDER BY role_name")
        roles = []
        for row in cur.fetchall():
            roles.append({
                "role_id": row[0],
                "role_name": row[1]
            })
        return roles
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching roles: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.post("/users/add")
def add_user(
    full_name: str = Form(...),
    username: str = Form(...),
    user_role: str = Form(...),
    password: str = Form(...)
):
    """Add a new user. Only callable by super_admin from the frontend."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        # Check if username already exists
        cur.execute("SELECT user_id FROM users WHERE username = %s", (username,))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Username already exists")

        cur.execute("""
            INSERT INTO users (full_name, username, password_hash, user_role, is_active)
            VALUES (%s, %s, %s, %s, true)
            RETURNING user_id, username, full_name, user_role
        """, (full_name, username, password, user_role.lower()))

        new_user = cur.fetchone()
        conn.commit()

        return {
            "message": "User created successfully",
            "user_id": new_user[0],
            "username": new_user[1],
            "full_name": new_user[2],
            "user_role": new_user[3]
        }

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error creating user: {str(e)}")
    finally:
        cur.close()
        conn.close()