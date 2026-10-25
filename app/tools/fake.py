"""Simulated tools for tests and local experiments. None touches the host."""

import time

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.tools import RiskClass

from .errors import ToolExecutionError
from .ports import ToolContext, ToolSpec


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(max_length=10_000)


class EchoOutput(BaseModel):
    message: str
    agent: str


class EchoTool:
    """Returns the message and the agent it ran for; records every call."""

    spec = ToolSpec(
        name="fake.echo",
        description="Devuelve el mensaje recibido (herramienta simulada).",
        risk=RiskClass.READ,
        input_model=EchoInput,
        output_model=EchoOutput,
    )

    def __init__(self) -> None:
        self.calls: list[tuple[ToolContext, str]] = []

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        args = EchoInput.model_validate(arguments, from_attributes=True)
        self.calls.append((context, args.message))
        return EchoOutput(message=args.message, agent=context.agent.value)


class NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StubOutput(BaseModel):
    ok: bool = True


class StubTool:
    """A tool with a chosen name and risk class that only counts invocations."""

    def __init__(self, name: str, risk: RiskClass, *, description: str = "stub") -> None:
        self.spec = ToolSpec(
            name=name,
            description=description,
            risk=risk,
            input_model=NoInput,
            output_model=StubOutput,
        )
        self.calls = 0

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        self.calls += 1
        return StubOutput()


class FailingTool:
    """Reports an expected failure with a stable code."""

    spec = ToolSpec(
        name="fake.failing",
        description="Falla siempre con un código conocido.",
        risk=RiskClass.READ,
        input_model=NoInput,
        output_model=StubOutput,
    )

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        raise ToolExecutionError("simulated_failure", "fallo simulado")


class BrokenTool:
    """Crashes with an unexpected exception (a bug in a tool)."""

    spec = ToolSpec(
        name="fake.broken",
        description="Lanza una excepción inesperada.",
        risk=RiskClass.READ,
        input_model=NoInput,
        output_model=StubOutput,
    )

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        raise RuntimeError("secret-bearing traceback that must not leak")


class SlowTool:
    spec = ToolSpec(
        name="fake.slow",
        description="Tarda más de lo permitido.",
        risk=RiskClass.READ,
        input_model=NoInput,
        output_model=StubOutput,
    )

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        time.sleep(self.seconds)
        return StubOutput()


class WrongOutputTool:
    spec = ToolSpec(
        name="fake.wrong_output",
        description="Devuelve un tipo que no es el declarado.",
        risk=RiskClass.READ,
        input_model=NoInput,
        output_model=StubOutput,
    )

    def run(self, context: ToolContext, arguments: BaseModel) -> BaseModel:
        return EchoOutput(message="x", agent="y")
