"""
Smoke-test that ANTHROPIC_API_KEY in .env can reach Claude, without printing the key.
Not part of the agent loop — run manually when wiring credentials.
"""

from dotenv import load_dotenv
import os
import sys

load_dotenv()

api_key = os.environ.get("ANTHROPIC_API_KEY")
model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

if not api_key:
    print("FAIL: ANTHROPIC_API_KEY not set")
    sys.exit(1)

from anthropic import Anthropic

client = Anthropic(api_key=api_key)
resp = client.messages.create(
    model=model,
    max_tokens=32,
    messages=[{"role": "user", "content": "Reply with exactly: key ok"}],
)
text = resp.content[0].text.strip()
print("API test result:", text)
if "key ok" in text.lower():
    print("PASS")
else:
    print("WARN: unexpected response but API responded")
    sys.exit(0)
