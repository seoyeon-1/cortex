from service.user_service import UserService

_service = UserService()


def get_user_route(user_id):
    """HTTP adapter -> service layer."""
    return _service.get_user(user_id)
