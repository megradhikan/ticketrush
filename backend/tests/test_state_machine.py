import pytest

from app.core.state_machine import SeatStatus, IllegalTransitionError, assert_legal_transition, is_legal_transition


@pytest.mark.parametrize(
    "frm,to",
    [
        (SeatStatus.AVAILABLE, SeatStatus.HELD),
        (SeatStatus.HELD, SeatStatus.SOLD),
        (SeatStatus.HELD, SeatStatus.AVAILABLE),
    ],
)
def test_legal_transitions(frm, to):
    assert is_legal_transition(frm, to)
    assert_legal_transition(frm, to)  # should not raise


@pytest.mark.parametrize(
    "frm,to",
    [
        (SeatStatus.AVAILABLE, SeatStatus.SOLD),  # must be held first
        (SeatStatus.AVAILABLE, SeatStatus.AVAILABLE),
        (SeatStatus.HELD, SeatStatus.HELD),
        (SeatStatus.SOLD, SeatStatus.AVAILABLE),  # sold is terminal
        (SeatStatus.SOLD, SeatStatus.HELD),
        (SeatStatus.SOLD, SeatStatus.SOLD),
    ],
)
def test_illegal_transitions(frm, to):
    assert not is_legal_transition(frm, to)
    with pytest.raises(IllegalTransitionError):
        assert_legal_transition(frm, to)
