with open("app/db/local.py", "r") as f:
    lines = f.readlines()

out = []
skip = False
for line in lines:
    if line.strip() == "# ---- Provider Ratings ----":
        if skip: continue
        skip = True
        
    if "class _LocalQueryBuilder:" in line:
        skip = False
        out.append('''    # ---- Provider Ratings ----

    async def get_provider_ratings(self, user_id: str) -> dict:
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT provider, model, rating FROM provider_ratings WHERE user_id = ?",
                (user_id,)
            )
            return {(row[0], row[1]): row[2] for row in cur.fetchall()}
        finally:
            conn.close()

    async def update_provider_rating(self, user_id: str, provider: str, model: str, delta: int) -> int:
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO provider_ratings (user_id, provider, model, rating)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, provider, model) DO UPDATE SET
                    rating = rating + ?
                RETURNING rating
                """,
                (user_id, provider, model, delta, delta)
            )
            row = cur.fetchone()
            if row:
                new_rating = row[0]
            else:
                 new_rating = delta
            conn.commit()
            return new_rating
        finally:
            conn.close()

''')

    if not skip:
        out.append(line)

with open("app/db/local.py", "w") as f:
    f.writelines(out)
