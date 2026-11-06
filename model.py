import logging
import os
import re

log = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 512
REQUEST_TIMEOUT_MS = 20000
# The name is fixed in code so the evaluated model and the served model cannot drift apart.
MODEL_NAME = "gemini-3.1-flash-lite"
# The page shows this name, so a value that is empty or carries odd characters stops the app at import.
if not re.fullmatch(r"[A-Za-z0-9._/-]{1,64}", MODEL_NAME):
    raise ValueError("MODEL_NAME must be 1 to 64 characters from letters, digits, dot, underscore, slash and hyphen")


# HTTP statuses the provider uses for a used-up quota and for a service that is
# busy or slow; both are temporary, but they call for different advice.
QUOTA_CODES = (429,)
UNAVAILABLE_CODES = (500, 502, 503, 504)


class ModelError(Exception):
    """A failed model call; kind is quota, unavailable or model."""

    def __init__(self, message, kind="model"):
        super().__init__(message)
        # Anything unexpected falls back to the generic kind so a message always exists.
        self.kind = kind if kind in ("quota", "unavailable", "model") else "model"


def classify_error(exc):
    """Name the kind of a provider failure from its class and status code.

    The class lineage is checked by name so this works without importing the
    SDK, and the message text is never read.
    """
    names = {cls.__name__ for cls in type(exc).__mro__}
    code = getattr(exc, "code", None)
    if "APIError" in names and isinstance(code, int) and not isinstance(code, bool):
        if code in QUOTA_CODES:
            return "quota"
        if code in UNAVAILABLE_CODES:
            return "unavailable"
        return "model"
    # Client-side timeouts and connection failures.
    if isinstance(exc, (TimeoutError, ConnectionError)) or names & {"TimeoutException", "TransportError"}:
        return "unavailable"
    return "model"


def generate_sql(prompt):
    key = os.environ.get("GEMINI_API_KEY")
    # Only the variable name is reported, never a value.
    if not key:
        raise ModelError("environment variable GEMINI_API_KEY is not set")

    try:
        # Imported here so the project can be imported and tested without the SDK.
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS)
        )
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0, max_output_tokens=MAX_OUTPUT_TOKENS
            ),
        )
        text = response.text
        if not isinstance(text, str):
            raise ValueError("model returned no text")
        return text
    except Exception as exc:
        log.error(
            "model call failed: %s: %s", type(exc).__name__, str(exc).replace(key, "")
        )
        raise ModelError("model call failed", classify_error(exc)) from exc
