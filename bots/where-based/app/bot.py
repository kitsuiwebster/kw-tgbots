import asyncio
import logging
import os
import random
import socket
from pathlib import Path
import json

from telegram import Update
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import httpx

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a factual assistant. "
    "The user gives you the EXACT name of a company, association, foundation, or any entity. "
    "You MUST look up the entity that carries EXACTLY this name. "
    "Do NOT make any free association, do NOT drift to another company, "
    "do NOT confuse it with a similar word or homonym. "
    "If a company carries exactly this name, that is the one you must describe. "
    "If multiple entities share this exact name, ALWAYS prioritize the one with the highest global visibility "
    "(largest valuation, most worldwide media coverage, most employees). Think GLOBALLY, not locally. "
    "Do NOT default to French or European companies — search worldwide. "
    "Deprioritize entities that have been dissolved or become fully inactive, but still include acquired entities if they remain the most globally known under that name. "
    "If you mention other homonymous entities, do so briefly at the end.\n\n"
    "You must respond with the following information, concisely and structured:\n\n"
    "1. **HQ**: where the entity is based (city, country + country flag emoji). "
    "If the legal HQ and operational HQ differ, specify both with their locations and flags.\n"
    "2. **Founder(s)**: the founder(s), with each one's origin/nationality and the corresponding flag emoji.\n"
    "3. **Activity**: the main activity of the entity summarized in one sentence.\n"
    "4. **Current CEO**: the current leader (CEO/president/director), their origin/nationality and flag emoji.\n\n"
    "FORMATTING: Do NOT use Markdown headers (###), horizontal rules (---), or numbered lists. "
    "Use ONLY the emoji-prefixed lines shown below. One line per field, no extra blank lines.\n\n"
    "Expected response format:\n"
    "🏢 **HQ**: City, Country FLAG\n"
    "(if different) 📋 **Legal HQ**: City, Country FLAG | 🏗️ **Operational HQ**: City, Country FLAG\n"
    "👤 **Founder(s)**: Name (Nationality FLAG), Name (Nationality FLAG)\n"
    "💼 **Activity**: One sentence describing the activity.\n"
    "🎯 **CEO**: Name (Nationality FLAG)\n\n"
    "If there is no specific founder (merger of groups, state creation, cooperative, etc.), "
    "briefly explain the real origin of the entity instead (e.g., resulting from the merger of X and Y, created by decree, etc.).\n\n"
    "IMPORTANT: if you are not certain about a piece of information, state it explicitly (e.g., '(unconfirmed)', '(uncertain)'). "
    "If you do not know an information at all, indicate it with ❓.\n"
    "CRITICAL: If you are not 100% sure which entity the user is referring to, DO NOT guess. "
    "Instead, list the possible entities you know with that name and ask the user to clarify. "
    "Never fabricate or mix up information from different entities.\n"
    "Do not add any extra commentary. Respond only with the requested format."
)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def maybe_force_ipv4() -> None:
    if not env_flag("FORCE_IPV4"):
        return

    original_getaddrinfo = socket.getaddrinfo

    def ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        infos = original_getaddrinfo(host, port, family, type, proto, flags)
        ipv4_infos = [info for info in infos if info[0] == socket.AF_INET]
        return ipv4_infos or infos

    socket.getaddrinfo = ipv4_only_getaddrinfo  # type: ignore[assignment]
    logger.info("Mode IPv4 forcé activé")


class MistralClient:
    def __init__(self, api_key: str, timeout_seconds: float, model: str):
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.url = "https://api.mistral.ai/v1/chat/completions"

    async def query(self, entity: str) -> str:
        content = (entity or "").strip()
        if not content:
            return ""

        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = httpx.Timeout(self.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(self.url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices", [])
        if not choices:
            raise ValueError("Réponse Mistral invalide: 'choices' vide")

        message = choices[0].get("message", {})
        result = (message.get("content") or "").strip()
        if not result:
            raise ValueError("Réponse Mistral vide")
        return result


class SubscriberStore:
    def __init__(self, path: Path, allowed_ids: set[int]):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._allowed_ids = allowed_ids
        self._subscribers: set[int] = set(allowed_ids)
        self._subscribers.update(self._load_file())
        self._subscribers.intersection_update(self._allowed_ids)
        self._save()

    def _load_file(self) -> set[int]:
        if not self.path.exists():
            return set()
        try:
            with self.path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, list):
                return set()
            return {int(x) for x in payload}
        except Exception:
            logger.warning("Impossible de lire %s, reset", self.path)
            return set()

    def _save(self) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(sorted(self._subscribers), f, ensure_ascii=False, indent=2)

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self._allowed_ids

    def add(self, user_id: int) -> bool:
        if not self.is_allowed(user_id):
            return False
        before = len(self._subscribers)
        self._subscribers.add(user_id)
        if len(self._subscribers) != before:
            self._save()
            return True
        return False


def parse_user_ids(raw: str) -> list[int]:
    if not raw.strip():
        return []
    return [int(chunk.strip()) for chunk in raw.split(",") if chunk.strip()]


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: SubscriberStore = context.application.bot_data["subscribers"]
    if not update.effective_chat or not update.message:
        return

    user_id = update.effective_chat.id
    if not store.is_allowed(user_id):
        await update.message.reply_text("Non autorisé.")
        return

    created = store.add(user_id)
    if created:
        text = (
            "Bot Where-Based activé !\n"
            "Envoie-moi le nom d'une entreprise, association ou fondation "
            "et je te dirai où elle est basée, qui l'a fondée et qui la dirige."
        )
    else:
        text = "Déjà activé. Envoie-moi un nom d'entité."

    await update.message.reply_text(text)


async def lookup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message or not update.message.text:
        return

    store: SubscriberStore = context.application.bot_data["subscribers"]
    if not store.is_allowed(update.effective_chat.id):
        await update.message.reply_text("Non autorisé.")
        return

    mistral: MistralClient = context.application.bot_data["mistral"]

    emoji = random.choice(["⏳", "🔍", "🌍", "🏢", "🔎", "💡", "📡", "🧭", "🗺️", "⚡"])
    loading_msg = await update.message.reply_text(emoji)

    try:
        result = await mistral.query(update.message.text)
    except Exception:
        logger.exception("Erreur Mistral")
        await loading_msg.edit_text("Erreur, réessaie dans un instant.")
        return

    await loading_msg.edit_text(result, parse_mode="Markdown")


def main() -> None:
    maybe_force_ipv4()

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    subscribers_path = Path(os.getenv("SUBSCRIBERS_PATH", "/data/subscribers.json"))
    mistral_api_key = os.environ["MISTRAL_API_KEY"]
    mistral_timeout = float(os.getenv("MISTRAL_HTTP_TIMEOUT", "60"))
    telegram_timeout = float(os.getenv("TELEGRAM_HTTP_TIMEOUT", "5"))
    mistral_model = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
    user_ids = parse_user_ids(os.getenv("ALLOWED_USER_IDS", ""))

    if not user_ids:
        raise ValueError("ALLOWED_USER_IDS ne doit pas être vide")

    allowed_ids = set(user_ids)
    subscribers = SubscriberStore(subscribers_path, allowed_ids=allowed_ids)
    mistral = MistralClient(
        api_key=mistral_api_key,
        timeout_seconds=mistral_timeout,
        model=mistral_model,
    )

    telegram_request = HTTPXRequest(
        connect_timeout=telegram_timeout,
        read_timeout=telegram_timeout,
        write_timeout=telegram_timeout,
        pool_timeout=telegram_timeout,
    )

    app = (
        Application.builder()
        .request(telegram_request)
        .token(token)
        .build()
    )

    app.bot_data["subscribers"] = subscribers
    app.bot_data["mistral"] = mistral

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lookup_cmd))

    logger.info("Bot Where-Based prêt")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
