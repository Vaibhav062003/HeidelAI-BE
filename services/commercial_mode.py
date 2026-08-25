import os


def billing_enabled() -> bool:
    return os.getenv("BILLING_PROXY_ENABLED", "false").strip().lower() == "true"
