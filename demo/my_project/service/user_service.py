from repo.user_repo import UserRepo


class UserService:
    """Read-side user queries."""

    def __init__(self, repo=None):
        self.repo = repo or UserRepo()

    def get_user(self, user_id):
        """Fetch user from the DB repository."""
        return self.repo.fetch_user(user_id)
