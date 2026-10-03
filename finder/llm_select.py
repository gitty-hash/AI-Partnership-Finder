"""Picks which LLM provider module backs "openai_client" calls throughout
discovery.py/enrich.py/pipeline.py, based on the LLM_PROVIDER env var.

Set LLM_PROVIDER=gemini in .env (or `set LLM_PROVIDER=gemini` before running)
to run the whole pipeline on Gemini instead of OpenAI for a side-by-side
comparison -- gemini_client.py mirrors openai_client.py's exact function
names/signatures, so no other code needs to change. Defaults to OpenAI.
"""
import os

from . import gemini_client, openai_client

llm_client = gemini_client if os.environ.get("LLM_PROVIDER", "").strip().lower() == "gemini" else openai_client
