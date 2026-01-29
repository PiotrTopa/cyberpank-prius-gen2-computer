from dataclasses import dataclass
from .actions import Action, ActionType, ActionSource

@dataclass
class UpdateLastInputAction(Action):
    """Action to update the last detected input event (for debugging)."""
    type: ActionType = ActionType.UPDATE_LAST_INPUT
    source: ActionSource = ActionSource.GATEWAY
    input_type: str = "" # "BUTTON" or "TOUCH"
    data: str = "" # The raw data or decoded name
