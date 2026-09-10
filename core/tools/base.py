from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from typing import Any, Dict

@dataclass
class ToolResult:
    success: bool
    data: Any = None
    error: str = ""
    summary: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)

class BaseTool(ABC):
    name: str
    description: str
    parameters: Dict[str, Any]

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        pass

    def to_openai_function(self) -> Dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }
