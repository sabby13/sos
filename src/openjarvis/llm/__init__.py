"""LLM provider abstraction (Groq + OpenAI today, anything OpenAI-shaped tomorrow)."""

from .base import Completion, LLMProvider, Message, ToolCall
from .openai_compatible import (
    OpenAICompatibleProvider,
    build_audio_provider,
    build_provider,
)

__all__ = [
    "Completion",
    "LLMProvider",
    "Message",
    "OpenAICompatibleProvider",
    "ToolCall",
    "build_audio_provider",
    "build_provider",
]
