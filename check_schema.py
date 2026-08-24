import sqlite3
conn = sqlite3.connect('zai_usage.db')
rows = conn.execute("SELECT DISTINCT task_type, COUNT(*) FROM api_calls GROUP BY task_type ORDER BY COUNT(*) DESC LIMIT 10").fetchall()
for r in rows:
    print(r)
conn.close()