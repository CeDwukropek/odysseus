"""Non-secret provider metadata for the restricted schedule agent."""
from urllib.parse import urlsplit

from src.schedule_bridge import local_model_url


def model_destination(url):
    """Only the user's private server and the two approved official APIs."""
    try:
        parsed = urlsplit(url or "")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        port = parsed.port  # Validate malformed ports before classifying.
        if local_model_url(url):
            return {"provider": "local", "execution": "local", "destination": "Twój serwer modeli"}
        if parsed.scheme != "https" or port not in {None, 443}:
            return None
        path = parsed.path.rstrip("/")
        if parsed.hostname == "api.openai.com" and path in {"", "/v1"}:
            return {"provider": "openai", "execution": "cloud", "destination": "OpenAI"}
        if parsed.hostname == "generativelanguage.googleapis.com" and path == "/v1beta/openai":
            return {"provider": "google", "execution": "cloud", "destination": "Google Gemini"}
    except (ValueError, TypeError):
        pass
    return None


def chat_models(models):
    # Respect Odysseus's cached/pinned/hidden model configuration upstream.
    non_chat = ("embedding", "tts", "whisper", "rerank", "dall-e", "moderation",
                "image", "audio", "realtime", "transcri", "speech", "imagen", "veo")
    return [model for model in models if isinstance(model, str)
            and not any(word in model.lower() for word in non_chat)]
