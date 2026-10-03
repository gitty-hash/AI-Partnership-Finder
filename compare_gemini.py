"""One-off comparison script: asks Gemini the exact same competitor-discovery
question openai_client.discover_competitors() asks OpenAI, capped at 10
names, so the two can be compared side by side. Not wired into the main
pipeline -- a standalone experiment only."""
import json
import os
import re

from dotenv import load_dotenv
from google import genai

from finder.discovery import WARESPORT_DESCRIPTION

load_dotenv()

MODEL = "gemini-3.8-flash"


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def discover_competitors_gemini(product_description: str, count: int = 10) -> list[str]:
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    prompt = (
        "You are a precise market research analyst. Only name real, currently operating "
        "software companies. Never invent names, never include generic category labels, "
        "never include the company being compared against.\n\n"
        f"{product_description}\n\n"
        f"List up to {count} REAL competitor companies in this space.\n\n"
        'Respond as JSON: {"competitors": ["Name1", "Name2", ...]}'
    )
    resp = client.models.generate_content(model=MODEL, contents=prompt)
    result = _extract_json(resp.text)
    if not result:
        return []
    names = result.get("competitors", [])
    return [n.strip() for n in names if isinstance(n, str) and n.strip()][:count]


if __name__ == "__main__":
    names = discover_competitors_gemini(WARESPORT_DESCRIPTION, count=10)
    print(f"Gemini found {len(names)} competitors:")
    for n in names:
        print(" -", n)
