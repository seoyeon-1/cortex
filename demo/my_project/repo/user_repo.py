USERS = {"1": {"id": "1", "name": "alice"}, "2": {"id": "2", "name": "bob"}}


class UserRepo:
    """In-memory stand-in for the users table."""

    def __init__(self):
        self.calls = 0

    def fetch_user(self, user_id):
        """Single DB read per call."""
        self.calls += 1
        return USERS.get(user_id)
