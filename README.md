# Telegram Italian Learning Bot

Bot Telegram Python avec:
- notifications vocabulaire (3/jour),
- agent de traduction Mistral sur chaque message texte.

Règle traduction:
- entrée en italien -> sortie en français
- entrée en français ou anglais -> sortie en italien

## Format du fichier JSON

Fichier `app/words.json` (modifiable) :

```json
[
  {"italian": "casa", "translation": "maison"},
  {"italian": "acqua", "translation": "eau"}
]
```

Clés supportées: `italian`/`it`/`word` et `translation`/`fr`/`meaning`.

## Lancement

1. Copier l'exemple:
   ```bash
   cat > .env <<'EOF'
   TELEGRAM_BOT_TOKEN=change_me
   TELEGRAM_CHAT_IDS=
   SEND_TIMES=09:00,14:00,20:00
   TZ=Europe/Paris
   EOF
   ```
2. Remplir `TELEGRAM_BOT_TOKEN` dans `.env`.
3. Ajouter `MISTRAL_API_KEY` dans `.env`.
4. Démarrer:
   ```bash
   docker compose up -d --build
   ```

## Abonnement utilisateur

- Ouvrir le bot sur Telegram et envoyer `/start`.
- Le chat est ajouté dans `data/subscribers.json`.
- Commande `/w` pour tester un mot immédiat.
- Si `TELEGRAM_CHAT_IDS` est rempli, seuls ces IDs sont autorisés.
- Tout message texte non commande est traduit par Mistral.

## Récupérer un chat_id manuellement

Si tu veux récupérer les `chat_id` via l'API Telegram:

```bash
python3 app/get_chat_id.py
```

Le script lit automatiquement `TELEGRAM_BOT_TOKEN` depuis `.env`.
Si rien ne sort, envoie d'abord `/start` au bot puis relance la commande.

## Personnalisation

- Horaires: variable `SEND_TIMES` (exactement 3 valeurs, ex `08:30,13:00,20:15`).
- Fuseau: `TZ`.
- Timeout Mistral: `MISTRAL_HTTP_TIMEOUT`.
- Timeout Telegram: `TELEGRAM_HTTP_TIMEOUT`.
- Mode IPv4 forcé (optionnel): `MISTRAL_FORCE_IPV4`, `TELEGRAM_FORCE_IPV4`.
- Modèle Mistral: `MISTRAL_MODEL`.
