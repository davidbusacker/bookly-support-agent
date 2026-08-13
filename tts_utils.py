"""
Prepare agent reply text for ElevenLabs TTS.

Claude often returns markdown (bold, bullets, order IDs like BK-10001).
Fed raw into TTS, that causes misreads, pauses, and audio hallucinations.
"""

import re


def prepare_text_for_speech(text: str, *, max_chars: int = 600) -> str:
    """
    Strip formatting and normalize text so TTS reads it cleanly.

    Returns plain spoken English — no markdown, no bullet syntax, no emoji.
    """
    spoken = text

    # Markdown links: [label](url) -> label
    spoken = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", spoken)

    # Bold / italic
    spoken = re.sub(r"\*\*(.+?)\*\*", r"\1", spoken)
    spoken = re.sub(r"\*(.+?)\*", r"\1", spoken)
    spoken = re.sub(r"__(.+?)__", r"\1", spoken)
    spoken = re.sub(r"_(.+?)_", r"\1", spoken)

    # Headers and list markers
    spoken = re.sub(r"^#+\s*", "", spoken, flags=re.MULTILINE)
    spoken = re.sub(r"^\s*[-*•]\s+", "", spoken, flags=re.MULTILINE)

    # Order numbers read naturally (BK-10001 -> "order 10001")
    spoken = re.sub(r"\bBK-(\d+)\b", r"order \1", spoken, flags=re.IGNORECASE)

    # Money: $97.16 stays fine; remove stray markdown chars
    spoken = spoken.replace("`", "").replace("#", "")

    # Newlines -> single flowing sentence
    spoken = re.sub(r"\n+", ". ", spoken)

    # Emoji and odd unicode
    spoken = re.sub(r"[\U00010000-\U0010ffff]", "", spoken)

    # Collapse whitespace
    spoken = re.sub(r"\s+", " ", spoken).strip()

    # Avoid empty or punctuation-only input (causes TTS glitches)
    if not spoken or not re.search(r"[a-zA-Z0-9]", spoken):
        return "Sorry, I didn't catch that."

    if len(spoken) > max_chars:
        # Cut at last sentence boundary before limit
        cut = spoken[:max_chars]
        last_stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        spoken = cut[: last_stop + 1] if last_stop > max_chars // 2 else cut.rstrip() + "."

    return spoken
