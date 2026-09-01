"""
Smoke-test that OPENAI_API_KEY in .env can reach GPT, without printing the key.
Not part of the agent loop — run manually when wiring credentials.
"""

from dotenv import load_dotenv
import os
import sys

load_dotenv()

api_key = os.environ.get("OPENAI_API_KEY")
model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

if not api_key:
    print("FAIL: OPENAI_API_KEY not set")
    sys.exit(1)

from openai import OpenAI

client = OpenAI(api_key=api_key)
resp = client.chat.completions.create(
    model=model,
    max_tokens=32,
    messages=[{"role": "user", "content": "Reply with exactly: key ok"}],
)
text = (resp.choices[0].message.content or "").strip()
print("API test result:", text)
if "key ok" in text.lower():
    print("PASS")
else:
    print("WARN: unexpected response but API responded")
    sys.exit(0)
