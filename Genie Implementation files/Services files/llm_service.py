"""
Wraps a local Mistral-7B model served by Ollama.

Requires Ollama running locally with the model pulled:
    ollama pull mistral

Ollama exposes a local HTTP API on http://localhost:11434 by default,
no separate "server start" step needed after install -- it runs as a
background service.
"""

import sys
import os

# Add project root (one level up from Services/) to sys.path so
# "from config import ..." works whether this is run directly or
# imported by whisper_service.py.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from config import (
    LLM_API_URL,
    LLM_MODEL_NAME,
    LLM_SYSTEM_PROMPT,
    LLM_TIMEOUT_SECONDS,
    LLM_MAX_TOKENS,
)


class LLMService:

    def __init__(self):
        self.api_url = LLM_API_URL
        self.model = LLM_MODEL_NAME
        self.system_prompt = LLM_SYSTEM_PROMPT

    def ask(self, prompt_text):
        """
        Sends the transcribed speech to the local model and returns its
        text response. Returns a readable error string (not an exception)
        on failure, so callers can just print/speak whatever comes back.
        """

        if not prompt_text or not prompt_text.strip():
            return ""

        try:
            response = requests.post(
                self.api_url,
                json={
                    "model": self.model,
                    "prompt": prompt_text,
                    "system": self.system_prompt,
                    "stream": False,
                    "options": {
                        "num_predict": LLM_MAX_TOKENS,
                    },
                },
                timeout=LLM_TIMEOUT_SECONDS,
            )
            response.raise_for_status()

            data = response.json()
            return data.get("response", "").strip()

        except requests.exceptions.ConnectionError:
            return (
                "[LLM unavailable: couldn't reach Ollama at "
                f"{self.api_url}. Is Ollama installed and running?]"
            )
        except requests.exceptions.Timeout:
            return "[LLM timed out. The model may be too slow on this hardware.]"
        except Exception as e:
            return f"[LLM error: {e}]"


if __name__ == "__main__":
    # Quick manual test: python Services/llm_service.py
    llm = LLMService()
    print("Asking local Mistral-7B a test question...")
    print(llm.ask("Say hello in one short sentence."))