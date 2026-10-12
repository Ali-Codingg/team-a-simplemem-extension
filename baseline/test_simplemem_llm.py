from dotenv import load_dotenv

load_dotenv()

from simplemem.core.utils.llm_client import LLMClient

client = LLMClient(use_streaming=False)

print("Model:", client.model)
print("Base URL:", client.base_url)

response = client.chat_completion(
    messages=[
        {
            "role": "system",
            "content": "Extract facts from fictional conversations. Return only valid JSON."
        },
        {
            "role": "user",
            "content": "Alice moved to Paris in 2024. Return a JSON object with a fact field."
        }
    ]
)

print("Response:", response)
print("Parsed JSON:", client.extract_json(response))