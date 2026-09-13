"""Pure HTTP projection for movement outcomes whose effects remain unknown."""

from fastapi.responses import JSONResponse

try:
    from env.minecraft_bridge_diagnostics import (
        MOVEMENT_FAILURE_REASON_HEADER,
        MOVEMENT_TERMINAL_HEADER,
        OUTCOME_CERTAINTY_HEADER,
        RETRY_SAFE_HEADER,
    )
except ImportError:
    from minecraft_bridge_diagnostics import (
        MOVEMENT_FAILURE_REASON_HEADER,
        MOVEMENT_TERMINAL_HEADER,
        OUTCOME_CERTAINTY_HEADER,
        RETRY_SAFE_HEADER,
    )


async def movement_effect_unknown_response(_request, error):
    terminal = "true" if error.terminal else "false"
    return JSONResponse(
        {
            "message": str(error), "status": False, "reason": error.reason,
            "outcome_certainty": "unknown", "retry_safe": False,
            "movement_terminal": error.terminal,
        },
        status_code=error.status_code,
        headers={
            OUTCOME_CERTAINTY_HEADER: "unknown",
            RETRY_SAFE_HEADER: "false",
            MOVEMENT_TERMINAL_HEADER: terminal,
            MOVEMENT_FAILURE_REASON_HEADER: error.reason,
        },
    )
