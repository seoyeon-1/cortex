from repo.user_repo import UserRepo
from service.user_service import UserService


def test_get_user_returns_repo_record():
    repo = UserRepo()
    svc = UserService(repo=repo)
    assert svc.get_user("1")["name"] == "alice"
    assert repo.calls == 1


def test_get_user_missing_returns_none():
    svc = UserService(repo=UserRepo())
    assert svc.get_user("404") is None
