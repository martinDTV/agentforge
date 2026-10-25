"""AgentForge Runner: isolated execution service (protocol v1, ADR 0007).

Independent of the AgentForge API package: it speaks the same JSON contracts
(``protocol.py``) but imports nothing from ``app``. It runs on a dedicated
Linux VM and executes allowlisted programs inside rootless Podman containers.
"""

__version__ = "0.1.0"
PROTOCOL_VERSION = "v1"
