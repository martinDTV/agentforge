from app.exceptions import ConflictError, NotFoundError


class RunNotFoundError(NotFoundError):
    code = "run_not_found"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"No existe la ejecución '{run_id}'.")


class RunAlreadyDecidedError(ConflictError):
    code = "run_already_decided"

    def __init__(self, run_id: str, status: str) -> None:
        super().__init__(f"La ejecución '{run_id}' ya no espera aprobación (estado: {status}).")
