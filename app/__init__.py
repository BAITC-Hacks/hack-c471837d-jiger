import json
import os


def redact_secrets(text: str) -> str:
    variants = set()
    for key in ("OPENAI_API_KEY", "EKT_API_USER", "EKT_API_PASSWORD"):
        secret = os.environ.get(key)
        if secret:
            variants.update((
                secret,
                repr(secret)[1:-1],
                json.dumps(secret, ensure_ascii=False)[1:-1],
                json.dumps(secret, ensure_ascii=True)[1:-1],
            ))
    for value in sorted(variants, key=len, reverse=True):
        text = text.replace(value, "[скрыто]")
    return text
