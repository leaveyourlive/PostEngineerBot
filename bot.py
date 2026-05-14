import asyncio
import json
import re
import urllib.parse
from datetime import datetime
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import GROQ_API_KEY, PROXY, TELEGRAM_BOT_TOKEN

HISTORY_FILE = Path("post_history.json")
BLOG_SETTINGS_FILE = Path("blog_settings.json")


# ─────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────
class BotStates(StatesGroup):
    waiting_for_blog_topic = State()
    waiting_for_blog_description = State()
    waiting_for_tg_channel = State()
    idle = State()


# ─────────────────────────────────────────────
# История постов
# ─────────────────────────────────────────────
def load_history() -> dict:
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"social": [], "telegram": [], "prompt": []}


def save_history(history: dict):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def add_to_history(post_type: str, topic: str, caption: str, image_prompt: str = ""):
    history = load_history()
    if post_type not in history:
        history[post_type] = []
    history[post_type].append({
        "date": datetime.now().isoformat(),
        "topic": topic,
        "caption": caption[:300],
        "image_prompt": image_prompt,
    })
    history[post_type] = history[post_type][-200:]
    save_history(history)


def get_recent_topics(post_type: str, n: int = 30) -> list[str]:
    history = load_history()
    posts = history.get(post_type, [])
    return [p["topic"] for p in posts[-n:]]


# ─────────────────────────────────────────────
# Настройки
# ─────────────────────────────────────────────
def load_settings() -> dict:
    if BLOG_SETTINGS_FILE.exists():
        with open(BLOG_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_settings(settings: dict):
    with open(BLOG_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
# Groq API
# ─────────────────────────────────────────────
async def call_groq(system_prompt: str, user_prompt: str, max_tokens: int = 2000) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.92,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"Groq error {resp.status}: {text}")
            data = await resp.json()
            return data["choices"][0]["message"]["content"].strip()


# Groq с веб-поиском (через встроенный инструмент)
async def call_groq_with_search(system_prompt: str, user_prompt: str, max_tokens: int = 2000) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.85,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web for current information about AI tools, prompts, and trends",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"}
                        },
                        "required": ["query"]
                    }
                }
            }
        ],
        "tool_choice": "none",  # Не вызываем реальный поиск, LLM использует свои знания
    }
    # Убираем tools из payload — Groq не поддерживает реальный веб-поиск, используем знания модели
    del payload["tools"]
    del payload["tool_choice"]

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"Groq error {resp.status}: {text}")
            data = await resp.json()
            return data["choices"][0]["message"]["content"].strip()


def parse_json_response(raw: str) -> dict:
    raw = re.sub(r"```json|```", "", raw).strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        raw = match.group(0)

    def fix_control_chars(m):
        s = m.group(0)
        s = s.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
        return s

    raw = re.sub(r'"(?:[^"\\]|\\.)*"', fix_control_chars, raw, flags=re.DOTALL)
    return json.loads(raw)


# ─────────────────────────────────────────────
# ГЕНЕРАЦИЯ: Пост для соц. сетей
# ─────────────────────────────────────────────
async def generate_social_post(settings: dict) -> dict:
    topic = settings.get("topic", "нейросети и AI")
    description = settings.get("description", "")
    tg_channel = settings.get("tg_channel", "")
    recent = get_recent_topics("social", 30)

    avoid_block = ""
    if recent:
        avoid_block = "\n\nУЖЕ ИСПОЛЬЗОВАННЫЕ ТЕМЫ (не повторяй):\n" + "\n".join(f"- {t}" for t in recent)

    system = """Ты — эксперт по вирусному контенту про AI и нейросети для соц. сетей (Threads, TikTok, ВКонтакте, Instagram).

ГЛАВНОЕ ПРАВИЛО: каждый пост должен быть про КОНКРЕТНЫЙ инструмент, КОНКРЕТНЫЙ приём или КОНКРЕТНЫЙ факт.
Никаких абстракций типа "нейросети меняют мир". Только конкретика.

Примеры ХОРОШИХ тем:
- "Попросил Claude написать промпт для себя — вот что получилось"
- "Perplexity vs ChatGPT для исследований: тестировал 2 недели"
- "Этот промпт в Midjourney даёт стиль аниме без подписки"
- "Suno AI: генерирую музыку для Reels за 30 секунд"
- "Как я использую Notion AI чтобы писать посты в 3 раза быстрее"
- "ElevenLabs озвучивает текст лучше живого диктора — проверил"

Структура поста (400-700 символов):
1. КРЮЧОК — конкретный факт или результат, который удивляет
2. СУТЬ — что именно делает инструмент/приём, 1-2 конкретных примера
3. МОСТИК — мягкая отсылка в TG-канал за продолжением

Язык: живой, как будто другу рассказываешь. Без канцелярита."""

    channel_line = f"TG-канал: {tg_channel}" if tg_channel else "упомяни 'мой Telegram-канал'"

    user = f"""Тематика: {topic}
Аудитория: {description}
{channel_line}
{avoid_block}

Выбери КОНКРЕТНЫЙ AI-инструмент или приём (реально существующий в 2024-2025) и напиши пост про него.

Верни СТРОГО JSON (без markdown, без ```):
{{
  "topic": "название инструмента/приёма (5-7 слов)",
  "tool_name": "точное название инструмента или техники",
  "caption": "текст поста 400-700 символов: крючок с конкретным результатом + суть + мостик в канал",
  "hashtags": "#хэштег1 #хэштег2 #хэштег3 #хэштег4 #хэштег5",
  "image_prompt": "English image prompt: visual representation of the specific AI tool or result, bold modern style, no text",
  "tip": "лучшая платформа для этого поста и почему"
}}"""

    raw = await call_groq(system, user)
    return parse_json_response(raw)


# ─────────────────────────────────────────────
# ГЕНЕРАЦИЯ: Пост для TG-канала
# ─────────────────────────────────────────────
async def generate_telegram_post(settings: dict) -> dict:
    topic = settings.get("topic", "нейросети и AI")
    description = settings.get("description", "")
    recent = get_recent_topics("telegram", 30)

    avoid_block = ""
    if recent:
        avoid_block = "\n\nУЖЕ ИСПОЛЬЗОВАННЫЕ ТЕМЫ (не повторяй):\n" + "\n".join(f"- {t}" for t in recent)

    system = """Ты — автор топового Telegram-канала про AI и нейросети. Твои посты сохраняют и пересылают.

СТАНДАРТ КАЧЕСТВА: каждый пост = реальная польза которую можно применить сегодня.

Форматы которые работают:
1. РАЗБОР ИНСТРУМЕНТА: название → что делает → как запустить (3 шага) → реальный результат → лайфхак
   Пример: "Claude Projects: как я организую 10 параллельных проектов без потери контекста"

2. КОНКРЕТНЫЙ ПРОМПТ: задача → промпт дословно → что получается → вариации
   Пример: "Промпт который превращает скучное резюме в продающий текст [проверено на 50 вакансиях]"

3. СРАВНЕНИЕ: инструмент А vs Б для конкретной задачи → таблица/список → вывод
   Пример: "Tестировал Gemini 2.0 vs GPT-4o для анализа данных: неожиданный результат"

4. ИНСАЙД/ФАКТ: малоизвестная функция или возможность + как её использовать
   Пример: "В ChatGPT есть режим которым пользуются 2% людей — он в 3 раза мощнее обычного"

5. КЕЙС: конкретная задача → решение с AI → цифры результата
   Пример: "Автоматизировал отчёты на работе через GPT-4: сэкономил 6 часов в неделю"

Длина: 900-1800 символов. Структура с эмодзи-маркерами. В конце — вопрос или призыв поделиться опытом."""

    user = f"""Тематика канала: {topic}
Аудитория: {description}
{avoid_block}

Выбери один из форматов выше и напиши конкретный, ценный пост. Используй реальные инструменты 2024-2025.
Если пишешь промпт — дай его дословно. Если разбор — дай конкретные шаги.

Верни СТРОГО JSON (без markdown, без ```):
{{
  "topic": "тема поста (5-8 слов)",
  "tool_name": "главный инструмент или техника в посте",
  "content_type": "тип: разбор инструмента / конкретный промпт / сравнение / инсайд / кейс",
  "caption": "полный текст поста 900-1800 символов с эмодзи, структурой, конкретными шагами/промптами/цифрами",
  "hashtags": "#хэштег1 #хэштег2 #хэштег3",
  "image_prompt": "English prompt for clean diagram or illustration about the specific tool/technique, minimalist, informative"
}}"""

    raw = await call_groq(system, user, max_tokens=2200)
    return parse_json_response(raw)


# ─────────────────────────────────────────────
# ГЕНЕРАЦИЯ: Крутой промпт (новая рубрика)
# ─────────────────────────────────────────────
async def generate_viral_prompt(settings: dict) -> dict:
    recent = get_recent_topics("prompt", 20)

    avoid_block = ""
    if recent:
        avoid_block = "\n\nУЖЕ ИСПОЛЬЗОВАННЫЕ ТЕМЫ (не повторяй):\n" + "\n".join(f"- {t}" for t in recent)

    system = """Ты — эксперт по промпт-инжинирингу. Ты знаешь самые мощные и вирусные промпты для нейросетей 2024-2025.

Твоя задача — найти или придумать промпт который:
1. Делает что-то ВПЕЧАТЛЯЮЩЕЕ за один запрос
2. Легко повторить любому человеку
3. Результат — виден сразу и вызывает "вау"

Категории крутых промптов:
- Генерация изображений (Midjourney, DALL-E, Stable Diffusion) — стиль, эффект, трюк
- ChatGPT/Claude — промпт который меняет поведение модели, ролевые, цепочки
- Suno/Udio — генерация музыки с конкретным настроением
- Видео (Runway, Kling) — эффекты, движения
- Мультимодальные — комбинации

Формат поста для Telegram:
- Название рубрики: 🎯 ПРОМПТ ДНЯ
- Категория инструмента
- Сам промпт ДОСЛОВНО (это главное!)
- Что получится (описание результата)
- Лайфхаки как улучшить промпт
- Призыв попробовать и поделиться результатом

Посты этой рубрики должны быть САМЫМИ репостными в канале."""

    user = f"""Придумай пост рубрики "Промпт дня" для Telegram-канала про нейросети.
{avoid_block}

Выбери конкретный инструмент (Midjourney, ChatGPT, Claude, Suno, DALL-E, Stable Diffusion, Runway и др.)
и дай реально крутой промпт который делает что-то впечатляющее.

Верни СТРОГО JSON (без markdown, без ```):
{{
  "topic": "название темы промпта (5-7 слов)",
  "tool": "название инструмента (например: Midjourney, ChatGPT, Claude, Suno)",
  "prompt_text": "сам промпт ДОСЛОВНО на том языке на котором его надо вводить",
  "what_you_get": "описание результата — что именно получится, как выглядит",
  "tips": "2-3 лайфхака как изменить промпт для другого результата",
  "caption": "полный текст поста для Telegram 600-1200 символов: рубрика + инструмент + промпт + результат + лайфхаки + призыв",
  "hashtags": "#промпт #нейросети #AI #хэштег4 #хэштег5",
  "image_prompt": "English prompt to generate an example image that this AI prompt would create, make it impressive"
}}"""

    raw = await call_groq(system, user, max_tokens=2000)
    return parse_json_response(raw)


# ─────────────────────────────────────────────
# Картинка — Pollinations
# ─────────────────────────────────────────────
def make_image_url(prompt: str, width: int = 1080, height: int = 1080) -> str:
    encoded = urllib.parse.quote(prompt)
    seed = int(datetime.now().timestamp()) % 99999
    return f"https://image.pollinations.ai/prompt/{encoded}?width={width}&height={height}&seed={seed}&nologo=true"


# ─────────────────────────────────────────────
# Клавиатуры
# ─────────────────────────────────────────────
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Пост для соц. сетей", callback_data="gen_social")],
        [InlineKeyboardButton(text="✈️ Пост для TG-канала", callback_data="gen_telegram")],
        [InlineKeyboardButton(text="🎯 Промпт дня", callback_data="gen_prompt")],
        [
            InlineKeyboardButton(text="📋 История", callback_data="history"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings_menu"),
        ],
    ])


def after_post_keyboard(post_type: str) -> InlineKeyboardMarkup:
    buttons = {
        "social": ("📱 Ещё соц. сети", "gen_social"),
        "telegram": ("✈️ Ещё TG-канал", "gen_telegram"),
        "prompt": ("🎯 Ещё промпт", "gen_prompt"),
    }
    same_btn, same_cb = buttons[post_type]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=same_btn, callback_data=same_cb)],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
    ])


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Изменить тематику", callback_data="change_topic")],
        [InlineKeyboardButton(text="✈️ Изменить TG-канал", callback_data="change_tg_channel")],
        [InlineKeyboardButton(text="🏠 Назад", callback_data="menu")],
    ])


def history_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Соц. сети", callback_data="history_social")],
        [InlineKeyboardButton(text="✈️ TG-канал", callback_data="history_telegram")],
        [InlineKeyboardButton(text="🎯 Промпты", callback_data="history_prompt")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
    ])


# ─────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    settings = load_settings()
    if settings.get("topic"):
        await state.set_state(BotStates.idle)
        tg_info = f"\n✈️ Канал: {settings.get('tg_channel', 'не указан')}"
        await message.answer(
            f"👋 Привет!\n\n📌 Блог: <b>{settings['topic']}</b>{tg_info}\n\nВыбери тип контента:",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    else:
        await state.set_state(BotStates.waiting_for_blog_topic)
        await message.answer(
            "👋 Привет! Я генерирую три типа контента:\n\n"
            "📱 <b>Соц. сети</b> — короткие вирусные посты про конкретные AI-инструменты\n"
            "✈️ <b>TG-канал</b> — глубокие полезные посты с промптами и инструкциями\n"
            "🎯 <b>Промпт дня</b> — крутые промпты для генерации чего угодно\n\n"
            "<b>Какая тематика у твоего канала?</b>\n"
            "<i>Например: AI и нейросети / Технологии для бизнеса</i>",
            parse_mode="HTML",
        )


@dp.message(BotStates.waiting_for_blog_topic)
async def process_topic(message: Message, state: FSMContext):
    await state.update_data(topic=message.text.strip())
    await state.set_state(BotStates.waiting_for_blog_description)
    await message.answer(
        f"✅ Тематика: <b>{message.text.strip()}</b>\n\n"
        "Опиши аудиторию и стиль в 1-2 предложениях:\n"
        "<i>Например: «Люди 20-35, интересуются AI. Стиль: дерзкий, без воды, с конкретикой»</i>",
        parse_mode="HTML",
    )


@dp.message(BotStates.waiting_for_blog_description)
async def process_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await state.set_state(BotStates.waiting_for_tg_channel)
    await message.answer(
        "Последнее — название TG-канала:\n"
        "<i>Например: @NeyronkyVZhizny или просто напиши название</i>\n\n"
        "Если канала нет — напиши <b>нет</b>",
        parse_mode="HTML",
    )


@dp.message(BotStates.waiting_for_tg_channel)
async def process_tg_channel(message: Message, state: FSMContext):
    data = await state.get_data()
    tg_channel = message.text.strip()
    if tg_channel.lower() in ["нет", "no", "-", "."]:
        tg_channel = ""
    settings = {
        "topic": data.get("topic", ""),
        "description": data.get("description", ""),
        "tg_channel": tg_channel,
    }
    save_settings(settings)
    await state.set_state(BotStates.idle)
    await message.answer(
        "✅ <b>Готово!</b>\n\n"
        f"📌 Тематика: <b>{settings['topic']}</b>\n"
        f"✈️ Канал: <b>{tg_channel or 'не указан'}</b>\n\n"
        "Выбери тип контента:",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# ─── Меню ───
@dp.message(Command("menu"))
@dp.callback_query(F.data == "menu")
async def cb_menu(update, state: FSMContext):
    settings = load_settings()
    await state.set_state(BotStates.idle)
    text = (
        f"🏠 <b>Меню</b>\n\n"
        f"📌 Блог: <b>{settings.get('topic', 'не задан')}</b>\n"
        f"✈️ Канал: <b>{settings.get('tg_channel', 'не указан')}</b>"
    )
    if isinstance(update, CallbackQuery):
        await update.message.answer(text, parse_mode="HTML", reply_markup=main_keyboard())
        await update.answer()
    else:
        await update.answer(text, parse_mode="HTML", reply_markup=main_keyboard())


@dp.callback_query(F.data == "settings_menu")
async def cb_settings_menu(call: CallbackQuery):
    settings = load_settings()
    await call.message.answer(
        f"⚙️ <b>Настройки</b>\n\n"
        f"📌 Тематика: {settings.get('topic', 'не задана')}\n"
        f"👥 Аудитория: {settings.get('description', 'не описана')}\n"
        f"✈️ Канал: {settings.get('tg_channel', 'не указан')}",
        parse_mode="HTML",
        reply_markup=settings_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data == "change_topic")
async def cb_change_topic(call: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_for_blog_topic)
    await call.message.answer("📌 Введи новую тематику:")
    await call.answer()


@dp.callback_query(F.data == "change_tg_channel")
async def cb_change_tg_channel(call: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_for_tg_channel)
    await call.message.answer("✈️ Введи название TG-канала:")
    await call.answer()


# ─── История ───
@dp.callback_query(F.data == "history")
async def cb_history(call: CallbackQuery):
    await call.message.answer("📋 Выбери раздел истории:", reply_markup=history_keyboard())
    await call.answer()


@dp.callback_query(F.data.in_(["history_social", "history_telegram", "history_prompt"]))
async def cb_history_type(call: CallbackQuery):
    type_map = {"history_social": "social", "history_telegram": "telegram", "history_prompt": "prompt"}
    label_map = {"social": "📱 Соц. сети", "telegram": "✈️ TG-канал", "prompt": "🎯 Промпты"}
    post_type = type_map[call.data]
    label = label_map[post_type]
    history = load_history()
    posts = history.get(post_type, [])
    if not posts:
        await call.message.answer(f"📋 История «{label}» пуста.")
        await call.answer()
        return
    lines = [f"📋 <b>{label} — последние посты:</b>\n"]
    for p in reversed(posts[-15:]):
        lines.append(f"• {p['date'][:10]} — {p['topic']}")
    await call.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_keyboard())
    await call.answer()


# ─── Отправка поста ───
async def send_post(call: CallbackQuery, caption: str, image_prompt: str, extra_msg: str = ""):
    image_url = make_image_url(image_prompt, width=1080, height=1080)
    full_caption = caption
    try:
        await call.message.answer_photo(photo=image_url, caption=full_caption[:1024])
        if len(full_caption) > 1024:
            await call.message.answer(full_caption[1024:])
    except Exception:
        await call.message.answer(full_caption)
        await call.message.answer(f"🖼 Картинка: {image_url}")
    if extra_msg:
        await call.message.answer(extra_msg, parse_mode="HTML")


# ─── Генерация соц. сети ───
@dp.callback_query(F.data == "gen_social")
async def cb_gen_social(call: CallbackQuery):
    settings = load_settings()
    if not settings.get("topic"):
        await call.message.answer("Сначала настрой бота через /start")
        await call.answer()
        return
    await call.answer()
    msg = await call.message.answer("⏳ Выбираю инструмент и генерирую пост для соц. сетей...")
    try:
        post = await generate_social_post(settings)
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}\n\nПопробуй ещё раз.")
        return
    add_to_history("social", post["topic"], post["caption"], post["image_prompt"])
    caption = f"{post['caption']}\n\n{post['hashtags']}"
    tip = post.get("tip", "")
    await send_post(call, caption, post["image_prompt"])
    if tip:
        await call.message.answer(f"💡 <b>Совет:</b> {tip}", parse_mode="HTML")
    await msg.delete()
    await call.message.answer(
        f"✅ <b>Пост для соц. сетей готов!</b>\n"
        f"🔧 Инструмент: <b>{post.get('tool_name', post['topic'])}</b>",
        parse_mode="HTML",
        reply_markup=after_post_keyboard("social"),
    )


# ─── Генерация TG-канал ───
@dp.callback_query(F.data == "gen_telegram")
async def cb_gen_telegram(call: CallbackQuery):
    settings = load_settings()
    if not settings.get("topic"):
        await call.message.answer("Сначала настрой бота через /start")
        await call.answer()
        return
    await call.answer()
    msg = await call.message.answer("⏳ Готовлю глубокий пост для TG-канала...")
    try:
        post = await generate_telegram_post(settings)
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}\n\nПопробуй ещё раз.")
        return
    add_to_history("telegram", post["topic"], post["caption"], post["image_prompt"])
    caption = f"{post['caption']}\n\n{post['hashtags']}"
    await send_post(call, caption, post["image_prompt"], width=1280, height=720)
    await msg.delete()
    await call.message.answer(
        f"✅ <b>Пост для TG-канала готов!</b>\n"
        f"🔧 Инструмент: <b>{post.get('tool_name', post['topic'])}</b>\n"
        f"📂 Тип: {post.get('content_type', '')}",
        parse_mode="HTML",
        reply_markup=after_post_keyboard("telegram"),
    )


# ─── Генерация промпта ───
@dp.callback_query(F.data == "gen_prompt")
async def cb_gen_prompt(call: CallbackQuery):
    await call.answer()
    msg = await call.message.answer("⏳ Ищу крутой промпт для рубрики «Промпт дня»...")
    settings = load_settings()
    try:
        post = await generate_viral_prompt(settings)
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}\n\nПопробуй ещё раз.")
        return
    add_to_history("prompt", post["topic"], post["caption"])
    caption = f"{post['caption']}\n\n{post['hashtags']}"
    # Для промптов картинка — пример того что промпт генерирует
    image_prompt = post.get("image_prompt", "AI generated art, impressive, detailed, vibrant")
    await send_post(call, caption, image_prompt)
    await msg.delete()
    await call.message.answer(
        f"✅ <b>Промпт дня готов!</b>\n"
        f"🔧 Инструмент: <b>{post.get('tool', '')}</b>\n\n"
        f"<i>Картинка выше — пример того, что этот промпт может создать</i>",
        parse_mode="HTML",
        reply_markup=after_post_keyboard("prompt"),
    )


# ─────────────────────────────────────────────
# Хелпер для send_post с разными размерами
# ─────────────────────────────────────────────
async def send_post(call, caption, image_prompt, width=1080, height=1080):
    image_url = make_image_url(image_prompt, width=width, height=height)
    try:
        await call.message.answer_photo(photo=image_url, caption=caption[:1024])
        if len(caption) > 1024:
            await call.message.answer(caption[1024:])
    except Exception:
        await call.message.answer(caption)
        await call.message.answer(f"🖼 Картинка: {image_url}")


# ─────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────
async def main():
    if PROXY:
        bot = Bot(token=TELEGRAM_BOT_TOKEN, proxy=PROXY)
        print(f"🌐 Прокси: {PROXY}")
    else:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
    # Сбрасываем все предыдущие сессии при старте
    await bot.delete_webhook(drop_pending_updates=True)
    print("🤖 Бот запущен! Ctrl+C для остановки.")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
