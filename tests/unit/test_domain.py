import pytest

from taskforge.domain import ALLOWED_TRANSITIONS, InvalidTransition, RetryPolicy, State, transition


@pytest.mark.parametrize("source", list(State))
@pytest.mark.parametrize("target", list(State))
def test_all_state_pairs(source, target):
    if target in ALLOWED_TRANSITIONS[source]:
        assert transition(source, target) == target
    else:
        with pytest.raises(InvalidTransition):
            transition(source, target)


def test_retry_budget_and_capped_exponential_jitter():
    policy = RetryPolicy(max_attempts=4, base_delay=2, max_delay=5, jitter=0.5)
    assert policy.can_retry(3)
    assert not policy.can_retry(4)
    assert policy.delay(1, random_value=0) == 1
    assert policy.delay(2, random_value=1) == 4
    assert policy.delay(3, random_value=1) == 5
    assert policy.delay(100, random_value=1) == 5


@pytest.mark.parametrize(
    "values", [{"max_attempts": 0}, {"jitter": 2}, {"base_delay": -1}, {"max_delay": float("inf")}]
)
def test_invalid_retry_policies(values):
    with pytest.raises(ValueError):
        RetryPolicy(**values)
