from fridge import ModeState, ActionState, EnergyState


def test_mode_state_enum():
    assert ModeState.IDLE == 0
    assert ModeState.COOL_DOWN == 1
    assert ModeState.COOL == 2
    assert ModeState.SPECIAL == 3
    assert len(ModeState) == 4


def test_action_state_enum():
    assert ActionState.IDLE == 0
    assert ActionState.COOLING == 1
    assert ActionState.HEATING == 2
    assert len(ActionState) == 3


def test_energy_state_enum():
    assert EnergyState.LOW_PRICE == 0
    assert EnergyState.NORMAL_PRICE == 1
    assert EnergyState.HIGH_PRICE == 2
    assert len(EnergyState) == 3
