from dotenv import load_dotenv
from openai import OpenAI
import os

load_dotenv()

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL"),
)

response = client.chat.completions.create(
    model=os.getenv("LLM_MODEL"),
    messages=[
    {
        "role": "system",
        "content": (
            "You are a memory extraction assistant. "
            "Extract facts from fictional conversations. "
            "Return only valid JSON."
        )
    },
    {
        "role": "user",
        "content": (
            "Fictional conversation:\n"
            "Alice: I moved to Paris in 2024.\n\n"
            'Return: {"fact": "Alice moved to Paris in 2024"}'
        )
    }
],
    temperature=0.1,
)

print(response.choices[0].message.content)