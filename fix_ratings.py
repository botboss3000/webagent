import json

def update_scores():
    try:
        from app.db import get_db
        db = get_db()
        print(db)
    except Exception as e:
        print(e)

if __name__ == "__main__":
    update_scores()
