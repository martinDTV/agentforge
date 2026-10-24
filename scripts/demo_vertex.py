from anthropic import AnthropicVertex

from app.settings import get_settings


def main() -> None:
    settings = get_settings()
    client = AnthropicVertex(
        project_id=settings.anthropic_vertex_project_id,
        region=settings.cloud_ml_region,
    )

    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=150,
        messages=[
            {
                "role": "user",
                "content": (
                    "Hola Claude. Estoy construyendo "
                    "un sistema multiagente con LangGraph. "
                    "Responde brevemente si estás funcionando."
                ),
            }
        ],
    )

    for content in response.content:
        if content.type == "text":
            print(content.text)


if __name__ == "__main__":
    main()
