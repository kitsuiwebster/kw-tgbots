import asyncio
import json
import logging
import os
import random
import socket
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.request import HTTPXRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
import httpx

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def maybe_force_ipv4() -> None:
    if not (env_flag("MISTRAL_FORCE_IPV4") or env_flag("TELEGRAM_FORCE_IPV4")):
        return

    original_getaddrinfo = socket.getaddrinfo

    def ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        infos = original_getaddrinfo(host, port, family, type, proto, flags)
        ipv4_infos = [info for info in infos if info[0] == socket.AF_INET]
        return ipv4_infos or infos

    socket.getaddrinfo = ipv4_only_getaddrinfo  # type: ignore[assignment]
    logger.info("Mode IPv4 forcé activé pour les requêtes réseau")


class MistralTranslator:
    def __init__(self, api_key: str, timeout_seconds: float, model: str):
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.url = "https://api.mistral.ai/v1/chat/completions"

    async def _call(self, system: str, user_content: str) -> str:
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
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
        result = (choices[0].get("message", {}).get("content") or "").strip()
        if not result:
            raise ValueError("Réponse Mistral vide")
        return result

    async def translate(self, text: str) -> str:
        content = (text or "").strip()
        if not content:
            return ""
        return await self._call(
            "Tu es un traducteur strict. Tu traduis TOUJOURS, tu ne renvoies jamais le texte tel quel. "
            "3 cas possibles :\n"
            "1) Entrée en italien → réponds 🇫🇷 + traduction française.\n"
            "2) Entrée en français → réponds 🇮🇹 + traduction italienne.\n"
            "3) Entrée en anglais → réponds 🇮🇹 + traduction italienne.\n"
            "En cas de doute sur la langue, considère que c'est de l'italien (cas 1). "
            "Format exact : [drapeau] [traduction]. Rien d'autre. "
            "Pas de flèche, pas de variante, pas de synonyme, pas d'explication, pas de ponctuation ajoutée. "
            "Exemples : 'Legato' → '🇫🇷 Lié', 'invece' → '🇫🇷 en revanche', 'maison' → '🇮🇹 casa', 'remove' → '🇮🇹 rimuovere'.",
            content,
        )

    async def explain(self, original: str, translation: str) -> str:
        return await self._call(
            "Tu es un professeur d'italien pour un francophone. "
            "On te donne un mot ou une phrase avec sa traduction. "
            "Réponds en français avec :\n"
            "1. Une brève explication de l'usage dans l'italien parlé (registre, contexte, fréquence)\n"
            "2. Un exemple de phrase en italien utilisant ce mot/cette expression\n"
            "3. La traduction française de cet exemple\n"
            "Sois concis. Pas de titre, pas de numérotation.",
            f"{original} = {translation}",
        )


@dataclass(frozen=True)
class Word:
    italian: str
    translation: str


class WordRepository:
    def __init__(self, words_path: Path):
        self.words_path = words_path
        self.words: list[Word] = self._load_words()

    def _load_words(self) -> list[Word]:
        if not self.words_path.exists():
            raise FileNotFoundError(f"Fichier introuvable: {self.words_path}")

        with self.words_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)

        if not isinstance(payload, list) or not payload:
            raise ValueError("Le JSON de mots doit être une liste non vide")

        parsed: list[Word] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            italian = item.get("italian") or item.get("it") or item.get("word")
            translation = item.get("translation") or item.get("fr") or item.get("meaning")
            if italian and translation:
                parsed.append(Word(str(italian).strip(), str(translation).strip()))

        if not parsed:
            raise ValueError(
                "Aucun mot valide. Format attendu: [{\"italian\":\"casa\",\"translation\":\"maison\"}]"
            )

        return parsed

    def random_word(self) -> Word:
        return random.choice(self.words)


class SubscriberStore:
    def __init__(self, path: Path, initial_ids: Iterable[int], allowed_ids: set[int] | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._allowed_ids = allowed_ids
        self._subscribers: set[int] = set(initial_ids)
        self._subscribers.update(self._load_file())
        if self._allowed_ids is not None:
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
            logger.warning("Impossible de lire %s, reset de la liste des abonnés", self.path)
            return set()

    def _save(self) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(sorted(self._subscribers), f, ensure_ascii=False, indent=2)

    def is_allowed(self, chat_id: int) -> bool:
        if self._allowed_ids is None:
            return True
        return chat_id in self._allowed_ids

    def add(self, chat_id: int) -> bool:
        if not self.is_allowed(chat_id):
            return False
        before = len(self._subscribers)
        self._subscribers.add(chat_id)
        changed = len(self._subscribers) != before
        if changed:
            self._save()
        return changed

    def all(self) -> list[int]:
        return sorted(self._subscribers)


def parse_times(raw: str) -> list[str]:
    values = [t.strip() for t in raw.split(",") if t.strip()]
    if not values:
        raise ValueError("SEND_TIMES ne doit pas être vide")

    parsed: list[str] = []
    for value in values:
        datetime.strptime(value, "%H:%M")
        parsed.append(value)

    if len(parsed) != 3:
        raise ValueError("Utilise exactement 3 horaires, ex: 09:00,14:00,20:00")
    return parsed


def parse_initial_chat_ids(raw: str) -> list[int]:
    if not raw.strip():
        return []
    ids = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk:
            ids.append(int(chunk))
    return ids


def format_word(word: Word) -> str:
    italian = word.italian[:1].upper() + word.italian[1:]
    translation = word.translation[:1].upper() + word.translation[1:]
    return f"{italian} = {translation}"


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: SubscriberStore = context.application.bot_data["subscribers"]
    if not update.effective_chat or not update.message:
        return

    chat_id = update.effective_chat.id
    if not store.is_allowed(chat_id):
        await update.message.reply_text("Ce chat_id n'est pas autorisé.")
        return

    created = store.add(chat_id)
    if created:
        text = (
            "Tu es abonné aux notifications de vocabulaire.\\n"
            "Tu recevras 3 mots italiens par jour.\\n"
            "Envoie aussi un texte: je le traduirai (it->fr, fr/en->it)."
        )
    else:
        text = "Tu es déjà abonné."

    await update.message.reply_text(text)


async def w_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return

    store: SubscriberStore = context.application.bot_data["subscribers"]
    if not store.is_allowed(update.effective_chat.id):
        await update.message.reply_text("Ce chat_id n'est pas autorisé.")
        return

    repo: WordRepository = context.application.bot_data["repo"]
    await update.message.reply_text(format_word(repo.random_word()))


async def translate_text_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message or not update.message.text:
        return

    store: SubscriberStore = context.application.bot_data["subscribers"]
    if not store.is_allowed(update.effective_chat.id):
        await update.message.reply_text("Ce chat_id n'est pas autorisé.")
        return

    translator: MistralTranslator = context.application.bot_data["translator"]
    original = update.message.text

    loading_emoji = random.choice(["⏳", "💭", "🪄"])
    loading_msg = await update.message.reply_text(loading_emoji)

    try:
        translated = await translator.translate(original)
    except Exception:
        logger.exception("Erreur traduction Mistral")
        await loading_msg.edit_text("Erreur traduction, réessaie dans un instant.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👀", callback_data="explain")]
    ])
    await loading_msg.edit_text(translated, reply_markup=keyboard)
    context.chat_data[f"eyes_{loading_msg.message_id}"] = {"original": original, "translation": translated}


async def eyes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer()

    data = context.chat_data.pop(f"eyes_{query.message.message_id}", None)
    if not data:
        return

    await query.message.edit_reply_markup(reply_markup=None)

    loading_emoji = random.choice(["⏳", "💭", "🪄"])
    loading_msg = await query.message.reply_text(loading_emoji)

    translator: MistralTranslator = context.application.bot_data["translator"]
    try:
        explanation = await translator.explain(data["original"], data["translation"])
    except Exception:
        logger.exception("Erreur explication Mistral")
        await loading_msg.edit_text("Erreur, réessaie.")
        return

    await loading_msg.edit_text(explanation)


async def scheduler_loop(application: Application) -> None:
    repo: WordRepository = application.bot_data["repo"]
    store: SubscriberStore = application.bot_data["subscribers"]
    send_times: list[str] = application.bot_data["send_times"]
    timezone_name: str = application.bot_data["timezone"]
    tz = ZoneInfo(timezone_name)

    logger.info("Scheduler démarré (timezone=%s, horaires=%s)", timezone_name, ", ".join(send_times))

    last_sent_marker: str | None = None

    while True:
        now = datetime.now(tz)
        hhmm = now.strftime("%H:%M")
        marker = now.strftime("%Y-%m-%d %H:%M")

        if hhmm in send_times and marker != last_sent_marker:
            last_sent_marker = marker
            subscribers = store.all()
            if not subscribers:
                logger.info("Aucun abonné, notification ignorée (%s)", marker)
            else:
                message = format_word(repo.random_word())
                logger.info("Envoi à %d abonné(s): %s", len(subscribers), message)
                for chat_id in subscribers:
                    try:
                        await application.bot.send_message(chat_id=chat_id, text=message)
                    except Exception:
                        logger.exception("Erreur envoi vers chat_id=%s", chat_id)

        await asyncio.sleep(20)


async def post_init(application: Application) -> None:
    application.bot_data["scheduler_task"] = asyncio.create_task(scheduler_loop(application))


async def post_shutdown(application: Application) -> None:
    task: asyncio.Task | None = application.bot_data.get("scheduler_task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main() -> None:
    maybe_force_ipv4()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    words_path = Path(os.getenv("WORDS_PATH", "/app/words.json"))
    subscribers_path = Path(os.getenv("SUBSCRIBERS_PATH", "/data/subscribers.json"))
    send_times = parse_times(os.getenv("SEND_TIMES", "09:00,14:00,20:00"))
    timezone_name = os.getenv("TZ", "Europe/Paris")
    mistral_api_key = os.environ["MISTRAL_API_KEY"]
    mistral_timeout = float(os.getenv("MISTRAL_HTTP_TIMEOUT", "60"))
    telegram_timeout = float(os.getenv("TELEGRAM_HTTP_TIMEOUT", "5"))
    mistral_model = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
    ZoneInfo(timezone_name)
    initial_chat_ids = parse_initial_chat_ids(os.getenv("TELEGRAM_CHAT_IDS", ""))
    allowed_ids = set(initial_chat_ids) if initial_chat_ids else None

    repo = WordRepository(words_path)
    subscribers = SubscriberStore(
        subscribers_path, initial_ids=initial_chat_ids, allowed_ids=allowed_ids
    )
    translator = MistralTranslator(
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
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.bot_data["repo"] = repo
    app.bot_data["subscribers"] = subscribers
    app.bot_data["send_times"] = send_times
    app.bot_data["timezone"] = timezone_name
    app.bot_data["translator"] = translator

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("w", w_cmd))
    app.add_handler(CallbackQueryHandler(eyes_callback, pattern="^explain$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_text_cmd))

    logger.info("Bot prêt")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
