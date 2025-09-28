
from __future__ import annotations

import os
from typing import Optional

import openai

# This uses the Perplexity API, so the OPENAI_API_KEY should be a Perplexity key
# and the base_url should point to Perplexity's API endpoint.
client = openai.OpenAI(api_key=os.getenv("PERPLEXITY_API_KEY"), base_url="https://api.perplexity.ai")

AUSTRALIAN_KEYWORDS = [
    "australia", "australian", "nsw", "victoria", "queensland", "qld", 
    "western australia", "wa", "south australia", "sa", "tasmania", "tas", 
    "act", "northern territory", "nt", "sydney", "melbourne", "brisbane", 
    "perth", "adelaide", "canberra", "hobart", "darwin"
]

def is_entity_australian(entity_name: str, model: str = "sonar-pro") -> bool:
    """
    Uses a keyword pre-check and then Perplexity to determine if a given entity is Australian.

    Args:
        entity_name: The name of the company or organization.
        model: The Perplexity model to use for the analysis.

    Returns:
        True if the entity is determined to be Australian, False otherwise.
    """
    if not entity_name or len(entity_name.strip()) < 2:
        return False

    # 1. Keyword Pre-Check for obvious cases
    lower_entity_name = entity_name.lower()
    if any(keyword in lower_entity_name for keyword in AUSTRALIAN_KEYWORDS):
        print(f"  - Keyword check for '{entity_name}': yes (found keyword)")
        return True

    # 2. Perplexity API call for less obvious cases
    try:
        messages = [
            {"role": "system", "content": "You are an expert business and geographical analyst. Your answer must be only the word 'Yes' or 'No'."},
            {"role": "user", "content": f"Is the company, organization, or entity named '{entity_name}' based in or primarily associated with Australia?"}
        ]

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=5,
            temperature=0.0,
        )
        answer = response.choices[0].message.content.strip().lower()
        print(f"  - Perplexity check for '{entity_name}': {answer}")
        return "yes" in answer

    except Exception as e:
        print(f"  - ⚠️ Could not determine if entity '{entity_name}' is Australian: {e}")
        return False
