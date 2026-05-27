from . import gemini, perplexity, copilot

# Registry: command_key -> (display_name, ask_callable)
# ask_callable signature: async def ask(prompt: str, history: list[dict]) -> str
REGISTRY = {
    "g": ("Gemini", gemini.ask),
    "pr": ("Perplexity", perplexity.ask),
    "co": ("Copilot", copilot.ask),
}


def register(cmd: str, name: str, func):
    """Owner-extensible: add a new provider at runtime."""
    REGISTRY[cmd] = (name, func)
