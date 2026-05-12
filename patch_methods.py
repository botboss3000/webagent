def run():
    with open('app/db/interface.py', 'a') as f:
        f.write('''
    # ---- Provider Ratings ----

    @abstractmethod
    async def get_provider_ratings(self, user_id: str) -> dict:
        """Get all provider ratings for a user. Returns dict: {(provider, model): rating}"""
        pass

    @abstractmethod
    async def update_provider_rating(self, user_id: str, provider: str, model: str, delta: int) -> int:
        """Increment/decrement a provider rating. Returns the new rating."""
        pass
''')

    with open('app/db/local.py', 'a') as f:
        f.write('''
    # ---- Provider Ratings ----

    async def get_provider_ratings(self, user_id: str) -> dict:
        def _get(conn):
            cur = conn.cursor()
            cur.execute(
                "SELECT provider, model, rating FROM provider_ratings WHERE user_id = ?",
                (user_id,)
            )
            return {(row[0], row[1]): row[2] for row in cur.fetchall()}
        return await asyncio.to_thread(_get, self._get_connection())

    async def update_provider_rating(self, user_id: str, provider: str, model: str, delta: int) -> int:
        def _update(conn):
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO provider_ratings (user_id, provider, model, rating)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, provider, model) DO UPDATE SET
                    rating = rating + excluded.rating,
                    updated_at = datetime('now')
                RETURNING rating
                """,
                (user_id, provider, model, delta)
            )
            new_rating = cur.fetchone()[0]
            conn.commit()
            return new_rating
        return await asyncio.to_thread(_update, self._get_connection())
''')
run()
