from db import get_connection

conn = get_connection()
cur = conn.cursor()
cur.execute("SELECT * FROM users;")
print(cur.fetchall())
conn.close()
