import os

# Токены берутся из переменных окружения (Render)
# При локальном запуске — заполни руками ниже
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "СЮДА_ТОКЕН_БОТА")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "СЮДА_GROQ_API_KEY")
PROXY = os.environ.get("PROXY", "")
