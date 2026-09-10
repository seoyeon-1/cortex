from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import os
from typing import List, Dict, Any
from rich.console import Console

console = Console()

class LLMClient:
    def __init__(self, config: Dict):
        api_key = config.get("api_key") or os.getenv("OPENAI_API_KEY")
        base_url = config.get("base_url")
        self.model = config.get("model", "gpt-4o-mini")
        self.temperature = config.get("temperature", 0.0)
        self.max_tokens = config.get("max_tokens", 4096)

        if not api_key:
            raise ValueError("API Key not found. Set in config.yaml or OPENAI_API_KEY env var.")

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        console.print(f"[dim]LLM Client initialized: {self.model} @ {base_url or 'OpenAI'}[/dim]")

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception)
    )
    def chat(self, messages: List[Dict], tools: List[Dict] = None, tool_choice: str = "auto") -> Any:
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        total_chars = sum(len(m.get("content", "")) for m in messages)
        console.print(f"[dim]LLM Request: ~{total_chars/4:.0f} tokens, {len(messages)} msgs, Tools: {len(tools) if tools else 0}[/dim]")

        response = self.client.chat.completions.create(**kwargs)
        return response
