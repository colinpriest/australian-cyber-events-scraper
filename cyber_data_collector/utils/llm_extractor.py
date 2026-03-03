
from __future__ import annotations

from typing import List, Optional
from datetime import date
import logging

import instructor
import openai
from pydantic import BaseModel, Field

# Initialize the OpenAI client for instructor
# This will use the OPENAI_API_KEY environment variable
client = instructor.patch(openai.OpenAI())

class ExtractedEventDetails(BaseModel):
    """A structured representation of extracted cyber event details."""
    is_australian_event: bool = Field(..., description="True if the event is related to Australia or an Australian entity.")
    is_specific_event: bool = Field(..., description="True if the article describes a specific, concrete cyber event, not a general trend, warning, or report.")
    primary_entity: Optional[str] = Field(None, description="The main company, organization, or government body affected by the event.")
    affected_entities: List[str] = Field(default_factory=list, description="A list of other named entities also affected by the event.")
    summary: str = Field(..., description="A concise, one-paragraph summary of the cyber event.")
    event_date: Optional[date] = Field(None, description="The actual date when the cyber event occurred (not when it was reported). Extract from article text if available, format as YYYY-MM-DD.")
    records_affected: Optional[int] = Field(None, description="The number of individuals, customers, or records affected by the event.")

def extract_event_details_with_llm(text_content: str, model: str = "gpt-4o-mini") -> Optional[ExtractedEventDetails]:
    """
    Uses an LLM to extract structured details from the text of a news article.

    Args:
        text_content: The full text content scraped from the article page.
        model: The name of the OpenAI model to use.

    Returns:
        A Pydantic object containing the extracted details, or None if extraction fails.
    """
    if not text_content or not text_content.strip():
        print("Text content is empty, skipping LLM extraction.")
        return None

    # Truncate content to fit within model context window, leaving room for prompt and response
    max_chars = 12000
    truncated_content = text_content[:max_chars]

    system_prompt = (
        "You are an expert cybersecurity analyst. Your task is to analyze the provided news article text "
        "and extract key information with high accuracy. Adhere strictly to the response format."
    )

    user_prompt = (
        f"Please analyze the following article text and extract the required details.\n\n"
        f"Key Instructions:\n"
        f"1. `is_australian_event`: Set to `true` if the event involves Australia, an Australian company, or Australian citizens. Otherwise, `false`.\n"
        f"2. `is_specific_event`: This is the most important instruction. Set to `true` if the article describes a specific, concrete cyber incident that has already happened (e.g., a data breach at a named company, a ransomware attack on a specific date). Set to `false` if the article is about a potential future threat, a general security warning, a report on cyber trends, or an opinion piece about cybersecurity. Focus on whether a specific event is the main subject of the article.\n"
        f"3. `primary_entity`: Identify the main organization that was the target of the attack. If no single primary entity is clear, leave it as null.\n"
        f"4. `affected_entities`: List any other named organizations or groups mentioned as being affected.\n"
        f"5. `summary`: Provide a brief, neutral summary of the incident described.\n"
        f"6. `event_date`: Extract the actual date when the cyber incident occurred (NOT the publication date). Look for phrases like 'in June 2025', 'last month', 'on June 5th', etc. Format as YYYY-MM-DD. If no specific date is found, set to null.\n"
        f"7. `records_affected`: Extract the specific number of people, customers, or data records affected. If a number is mentioned (e.g., '2 million customers', '50,000 records'), extract only the integer value. If no number is specified, set to null.\n\n"
        f"--- ARTICLE TEXT ---\n{truncated_content}"
    )

    try:
        logger = logging.getLogger(__name__)
        logger.debug(f"Calling LLM ({model}) for analysis...")
        details = client.chat.completions.create(
            model=model,
            response_model=ExtractedEventDetails,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_retries=2,
        )
        logger.debug("LLM analysis successful.")
        return details
    except Exception as e:
        print(f"LLM extraction failed: {e}")
        return None
