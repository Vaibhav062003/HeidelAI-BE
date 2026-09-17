from auth.auth_utils import reverification_required
from auth.dependencies import _factor_verification_age
from auth.models import CurrentUserModel


def _user(fva: tuple[int, int]) -> CurrentUserModel:
    return CurrentUserModel(
        user_id="user-1",
        org_id="org-1",
        role="org:member",
        factor_verification_age=fva,
    )


def test_factor_verification_age_is_fail_closed() -> None:
    assert _factor_verification_age({}) == (-1, -1)
    assert _factor_verification_age({"fva": [3, -1]}) == (3, -1)
    assert _factor_verification_age({"fva": ["bad", 2]}) == (-1, -1)


def test_reverification_uses_strongest_available_factor() -> None:
    assert reverification_required(_user((5, -1))) is None
    assert reverification_required(_user((1, 9))) is None
    assert reverification_required(_user((1, 11))) is not None
    assert reverification_required(_user((-1, -1))) is not None


def test_reverification_response_matches_clerk_contract() -> None:
    response = reverification_required(_user((11, -1)))
    assert response is not None
    assert response.status_code == 403
    assert b'"reason":"reverification-error"' in response.body
    assert b'"reverification":"strict"' in response.body


if __name__ == "__main__":
    test_factor_verification_age_is_fail_closed()
    test_reverification_uses_strongest_available_factor()
    test_reverification_response_matches_clerk_contract()
    print("Reverification checks passed.")
