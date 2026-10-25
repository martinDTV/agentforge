import pytest
from pydantic import BaseModel, ConfigDict

from app.contracts.tools import RiskClass
from app.tools import ToolRegistrationError, ToolRegistry, ToolSpec, default_registry
from app.tools.fake import EchoTool, NoInput, StubOutput, StubTool


def test_default_registry_ships_only_read_only_workspace_tools():
    registry = default_registry()
    assert registry.names() == ["workspace.find", "workspace.list_dir", "workspace.read_file"]
    assert all(tool.spec.risk is RiskClass.READ for tool in registry)
    assert "workspace.read_file" in registry and "shell.exec" not in registry


def test_duplicate_names_are_rejected():
    registry = ToolRegistry([EchoTool()])
    with pytest.raises(ToolRegistrationError, match="ya registrada"):
        registry.register(EchoTool())
    assert len(registry) == 1


@pytest.mark.parametrize("name", ["Shell", "1tool", "a..b", "a.", "with space", "a/b", "x" * 129])
def test_invalid_tool_names_are_rejected(name: str):
    with pytest.raises(ToolRegistrationError, match="inválido"):
        StubTool(name, RiskClass.READ)


def test_input_models_must_forbid_unknown_fields():
    class OpenInput(BaseModel):
        model_config = ConfigDict(extra="ignore")

    with pytest.raises(ToolRegistrationError, match="extra='forbid'"):
        ToolSpec(
            name="fake.open",
            description="abierta",
            risk=RiskClass.READ,
            input_model=OpenInput,
            output_model=StubOutput,
        )
    with pytest.raises(ToolRegistrationError, match="descripción"):
        ToolSpec(
            name="fake.blank",
            description="",
            risk=RiskClass.READ,
            input_model=NoInput,
            output_model=StubOutput,
        )


def test_describe_exposes_schema_and_filters_unknown_names():
    registry = ToolRegistry([EchoTool(), StubTool("fake.stub", RiskClass.WRITE)])
    described = registry.describe()
    assert [d.name for d in described] == ["fake.echo", "fake.stub"]
    echo = described[0]
    assert echo.risk is RiskClass.READ and echo.version == "1"
    assert echo.input_schema["additionalProperties"] is False
    assert "message" in echo.input_schema["properties"]
    assert [d.name for d in registry.describe(["fake.stub", "ghost"])] == ["fake.stub"]
    assert registry.get("ghost") is None
