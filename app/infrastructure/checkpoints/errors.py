class CheckpointSchemaError(Exception):
    """The LangGraph checkpoint tables are missing or behind the installed library.

    Raised at startup so the app never accepts runs it could not persist.
    ``scripts/setup_checkpoints.py`` is the deploy step that fixes it.
    """

    def __init__(self, expected_version: int, current_version: int | None) -> None:
        self.expected_version = expected_version
        self.current_version = current_version
        found = (
            "ninguna (tablas de checkpoints ausentes)"
            if current_version is None
            else f"la versión {current_version}"
        )
        super().__init__(
            "Esquema de checkpoints de LangGraph desactualizado: se esperaba la versión "
            f"{expected_version} y se encontró {found}. "
            "Ejecuta 'uv run python -m scripts.setup_checkpoints'."
        )
