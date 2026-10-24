from app.exceptions import ConflictError, NotFoundError, UnavailableError


class RunNotFoundError(NotFoundError):
    code = "run_not_found"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"No existe la ejecución '{run_id}'.")


class RunAlreadyDecidedError(ConflictError):
    code = "run_already_decided"

    def __init__(self, run_id: str, status: str) -> None:
        super().__init__(f"La ejecución '{run_id}' ya no espera aprobación (estado: {status}).")


class RunAlreadyExistsError(ConflictError):
    code = "run_already_exists"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Ya existe una ejecución con id '{run_id}'.")


class IdempotencyKeyReusedError(ConflictError):
    code = "idempotency_key_reused"

    def __init__(self, key: str) -> None:
        super().__init__(f"La clave de idempotencia '{key}' ya se usó con una solicitud distinta.")


class LeaseLostError(ConflictError):
    code = "run_lease_lost"

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"Este proceso perdió la propiedad de la ejecución '{run_id}'; "
            "otro proceso la está conduciendo."
        )


class ShuttingDownError(UnavailableError):
    code = "shutting_down"

    def __init__(self) -> None:
        super().__init__("El servicio se está apagando; reintenta en otra instancia.")
