from enum import IntEnum


class ModeState(IntEnum):
    """
    Enum representing the operating mode of the fridge.
    """

    IDLE = 0
    COOL_DOWN = 1
    COOL = 2


class ActionState(IntEnum):
    """
    Enum representing the current action being taken by the regulator.
    """

    IDLE = 0
    COOLING = 1
    HEATING = 2


class EnergyState(IntEnum):
    """
    Enum representing the energy price state.
    """

    LOW_PRICE = 0
    NORMAL_PRICE = 1
    HIGH_PRICE = 2
