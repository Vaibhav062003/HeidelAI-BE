import re


def message_contains_keyword(message_text: str, keyword: str) -> bool:
    message = str(message_text or "").casefold()
    target = str(keyword or "").strip().casefold()
    return bool(target and re.search(rf"(?<!\w){re.escape(target)}(?!\w)", message))
