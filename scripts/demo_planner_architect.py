from app.contracts.planning import ArchitectureReview, DevelopmentPlan
from app.infrastructure.llm import build_llm
from app.orchestration.graph import build_graph


def main() -> None:
    graph = build_graph(build_llm())

    result = graph.invoke(
        {"task": "Diseña un módulo de autenticación con FastAPI, JWT y PostgreSQL."},
        {"configurable": {"thread_id": "demo"}},
    )

    print("\n=== PLAN VALIDADO ===")
    print(DevelopmentPlan.model_validate(result["plan"]).model_dump_json(indent=2))

    print("\n=== REVISIÓN VALIDADA ===")
    print(ArchitectureReview.model_validate(result["architecture"]).model_dump_json(indent=2))

    print("\n(Grafo en espera de aprobación humana; no se invoca automáticamente.)")


if __name__ == "__main__":
    main()
