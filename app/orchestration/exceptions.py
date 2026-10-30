from app.exceptions import ConflictError, DomainError, NotFoundError, UnavailableError
from app.security.errors import PermissionDeniedError


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


class SweepInProgressError(ConflictError):
    code = "sweep_in_progress"

    def __init__(self) -> None:
        super().__init__("Ya hay un barrido de recuperación en curso en esta instancia.")


class RunNotResumableError(ConflictError):
    code = "run_not_resumable"

    def __init__(self, run_id: str, reason: str) -> None:
        super().__init__(f"La ejecución '{run_id}' no se puede reanudar: {reason}.")


class DecisionStaleError(ConflictError):
    code = "decision_stale"

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"La decisión para la ejecución '{run_id}' no corresponde al alcance propuesto "
            "actualmente; consulta las decisiones pendientes y repite con su digest."
        )


class DecisionStageMismatchError(ConflictError):
    code = "decision_stage_mismatch"

    def __init__(self, run_id: str, pending: str | None, requested: str) -> None:
        super().__init__(
            f"La ejecución '{run_id}' espera la etapa '{pending}' y se decidió '{requested}'."
        )


class DecisionExpiredError(ConflictError):
    code = "decision_expired"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"La ventana de decisión de la ejecución '{run_id}' expiró.")


class RunNotCancellableError(ConflictError):
    code = "run_not_cancellable"

    def __init__(self, run_id: str, status: str) -> None:
        super().__init__(
            f"La ejecución '{run_id}' no se puede cancelar en su estado actual ({status})."
        )


class WorkflowUnavailableError(UnavailableError):
    code = "workflow_unavailable"

    def __init__(self, kind: str) -> None:
        super().__init__(f"El flujo '{kind}' no está configurado en esta instancia.")


class RepositoryRequiredError(DomainError):
    code = "repository_required"

    def __init__(self) -> None:
        super().__init__("Una ejecución de desarrollo requiere 'repository_id'.")


class RepositoryNotAuthorizedError(PermissionDeniedError):
    code = "repository_not_authorized"

    def __init__(self, repository_id: str, project_id: str) -> None:
        super().__init__(
            f"El repositorio '{repository_id}' no admite ejecuciones de desarrollo "
            f"en el proyecto '{project_id}'."
        )


# -- goals (Fase 8, ADR 0012) ---------------------------------------------------------


class GoalNotFoundError(NotFoundError):
    code = "goal_not_found"

    def __init__(self, goal_id: str) -> None:
        super().__init__(f"No existe el objetivo '{goal_id}'.")


class GoalAlreadyExistsError(ConflictError):
    code = "goal_already_exists"

    def __init__(self, goal_id: str) -> None:
        super().__init__(f"Ya existe un objetivo con id '{goal_id}'.")


class GoalIdempotencyKeyReusedError(ConflictError):
    code = "goal_idempotency_key_reused"

    def __init__(self, key: str) -> None:
        super().__init__(f"La clave de idempotencia '{key}' ya se usó con otro objetivo.")


class GoalNotCancellableError(ConflictError):
    code = "goal_not_cancellable"

    def __init__(self, goal_id: str, status: str) -> None:
        super().__init__(
            f"El objetivo '{goal_id}' no se puede cancelar en su estado actual ({status})."
        )


class GoalDependencyCycleError(DomainError):
    code = "goal_dependency_cycle"

    def __init__(self, cycle: list[str]) -> None:
        super().__init__("Las dependencias del plan forman un ciclo: " + " -> ".join(cycle))


class GoalDependenciesUnsupportedError(DomainError):
    code = "goal_dependencies_unsupported"

    def __init__(self, repository_id: str) -> None:
        super().__init__(
            f"El repositorio '{repository_id}' no tiene remoto: sin Pull Requests no hay "
            "integración verificable, así que solo admite decomposition=single."
        )


class GoalLimitReachedError(ConflictError):
    code = "goal_limit_reached"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)


class GoalReconcileInProgressError(ConflictError):
    code = "goal_reconcile_in_progress"

    def __init__(self, goal_id: str) -> None:
        super().__init__(f"Otro proceso está reconciliando el objetivo '{goal_id}'.")


class SupervisorUnavailableError(UnavailableError):
    code = "supervisor_unavailable"

    def __init__(self) -> None:
        super().__init__(
            "El Supervisor de objetivos no está configurado en esta instancia "
            "(requiere repositorios y políticas de desarrollo)."
        )
