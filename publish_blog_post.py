"""
Ежедневный автопостинг статей в блог на blog.<домен> (GitHub Pages).

Берёт следующую нерасписанную тему из topics.json (тот же бэклог тем,
что и у Telegram-канала @samimamiclub), генерирует ПОЛНОСТЬЮ НОВУЮ статью
под неё (не рерайт готового текста - с нуля, чтобы структура и подача
были самостоятельными, а не спином), генерирует картинку, рендерит
статичную HTML-страницу, обновляет index.html и sitemap.xml, коммитит.
"""

import html
import json
import os
import re
import subprocess
from datetime import date

import requests

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
LAOZHANG_KEY = os.environ["LAOZHANG_API_KEY"]

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-5"
LAOZHANG_IMG_URL = "https://api.laozhang.ai/v1/images/generations"
IMAGE_MODEL = "gemini-2.5-flash-image"
IMAGE_SUFFIX = 'пастельные цвета, стиль иллюстрация, написано "Логоцентр Сами Мамы"'

# TODO: заменить на настоящий домен и ссылку на лендинг SERM перед первым запуском
SITE_URL = "https://blog.REPLACE_ME.ru"
SITE_NAME = 'Блог логопедического центра "Сами Мамы"'
SERM_URL = "https://REPLACE_ME.tilda.ws/serm?utm_source=blog&utm_medium=article&utm_campaign=serm"

TOPICS_PATH = "topics.json"
STATE_PATH = "blog_state.json"
POSTS_DIR = "posts"

BLOG_SYSTEM_PROMPT = """\
Ты пишешь SEO-статью для блога {site_name} на отдельном сайте (не Telegram, не Дзен).
Эта статья ДОЛЖНА рассказывать о той же теме, что уже раскрыта в постах в Telegram-канале
@samimamiclub, но должна быть написана заново, своими словами, с другой структурой подачи -
как будто это отдельный автор пишет отдельную статью на ту же тему. Не пытайся угадать,
как это было сформулировано в Telegram - пиши свою версию с нуля.

ТЕМА: {topic}
ОРИЕНТИР ПО СМЫСЛУ (не копировать формулировку): {title}
АВТОР: {author}, {author_role}

ФОРМАТ ОТВЕТА - строго JSON без markdown-обёртки, одна строка не нужна, просто валидный JSON:
{{
  "meta_title": "...",
  "meta_description": "...",
  "h1": "...",
  "body_html": "...",
  "faq": [{{"q": "...", "a": "..."}}, {{"q": "...", "a": "..."}}],
  "image_description": "..."
}}

ТРЕБОВАНИЯ:
- meta_title: до 70 символов, содержит суть темы, без кликбейта ради кликбейта
- meta_description: 150-160 символов, естественный язык, отражает пользу статьи
- h1: цепляющий заголовок статьи (может отличаться от meta_title), заканчивается точкой/?/!
- body_html: 3500-4500 символов, ТОЛЬКО теги <h2>, <h3>, <p>, <ul>, <li>, <strong>, <em> -
  никакого markdown. Структура: вводный абзац -> 3-5 секций с <h2>-подзаголовками (короче
  и чаще, 150-250 слов на секцию, а не один длинный блок) -> списки <ul><li> сразу после
  того подзаголовка, к которому относятся. Между подзаголовком и текстом секции - обычный
  html-поток (браузер сам даёт отступ, лишние пустые <p></p> не нужны).
- Тон: обращение на "ты" к маме, тепло, экспертно, без снобизма, без осуждения - тот же
  голос, что в Telegram-версии, но текст и примеры - другие, не пересказ.
- ЗНАКИ ПРЕПИНАНИЯ: только короткое тире "-", никогда "—" или "–". Только прямые кавычки
  "текст", никогда «ёлочки» и не „лапки".
- Заканчивается коротким тёплым абзацем + подписью автора: <p><em>{author}, {author_role}
  логопедического центра "Сами Мамы"</em></p> - БЕЗ призывов комментировать/сохранять/подписаться.
- faq: 2-3 вопроса-ответа по теме статьи (для расширенных сниппетов), ответ 1-2 предложения.
- image_description: 1-2 предложения, что нарисовать (без художественного стиля, только сцена).
- НЕ добавляй хэштеги, НЕ добавляй ссылку на SERM/лендинг сама - она будет добавлена отдельно.
"""

AUTHOR_ROLES = {
    "Баркаева": "психолог и основатель",
    "Лучия Розенталь": "логопед-дефектолог",
}


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def slugify(text: str) -> str:
    translit = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    text = text.lower()
    text = "".join(translit.get(ch, ch) for ch in text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


def pick_next_topic() -> dict:
    topics = load_json(TOPICS_PATH, [])
    state = load_json(STATE_PATH, {"posted_ids": []})
    posted = set(state["posted_ids"])
    for t in topics:
        if t["id"] not in posted:
            return t
    raise RuntimeError("Все темы из topics.json уже опубликованы в блоге - добавьте новые")


def mark_posted(topic_id: int, slug: str) -> None:
    state = load_json(STATE_PATH, {"posted_ids": [], "posts": []})
    state["posted_ids"].append(topic_id)
    state.setdefault("posts", []).append({
        "id": topic_id, "slug": slug, "date": date.today().isoformat(),
    })
    save_json(STATE_PATH, state)


def generate_article(topic: dict) -> dict:
    author = topic.get("author", "Баркаева")
    role = AUTHOR_ROLES.get(author, "специалист")
    prompt = BLOG_SYSTEM_PROMPT.format(
        site_name=SITE_NAME, topic=topic["topic"], title=topic["title"],
        author=author, author_role=role,
    )
    r = requests.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    r.raise_for_status()
    text = r.json()["content"][0]["text"].strip()
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)


def generate_image(description: str) -> bytes:
    prompt = f"{description}, {IMAGE_SUFFIX}"
    r = requests.post(
        LAOZHANG_IMG_URL,
        headers={"Authorization": f"Bearer {LAOZHANG_KEY}", "Content-Type": "application/json"},
        json={"model": IMAGE_MODEL, "prompt": prompt, "n": 1, "size": "1024x1024"},
        timeout=120,
    )
    r.raise_for_status()
    item = r.json()["data"][0]
    if "b64_json" in item:
        import base64
        return base64.b64decode(item["b64_json"])
    return requests.get(item["url"], timeout=30).content


POST_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{meta_title}</title>
<meta name="description" content="{meta_description}">
<link rel="canonical" href="{canonical_url}">
<meta property="og:type" content="article">
<meta property="og:title" content="{meta_title}">
<meta property="og:description" content="{meta_description}">
<meta property="og:image" content="{image_url}">
<meta property="og:url" content="{canonical_url}">
<link rel="stylesheet" href="../style.css">
<script type="application/ld+json">
{jsonld}
</script>
</head>
<body>
<header class="site-header"><a href="../index.html">{site_name}</a></header>
<main class="post">
<h1>{h1}</h1>
<p class="post-date">{date_human}</p>
<img class="post-image" src="{image_url}" alt="{h1}">
{body_html}
{faq_html}
<div class="cta-box">
<p>Хотите системную помощь, а не только советы из статьи?</p>
<a class="cta-button" href="{serm_url}">Записаться на диагностику в "Сами Мамы"</a>
</div>
</main>
<footer class="site-footer"><a href="../index.html">&larr; Все статьи {site_name}</a></footer>
</body>
</html>
"""


def render_faq(faq: list) -> str:
    if not faq:
        return ""
    items = "".join(f"<h3>{html.escape(f['q'])}</h3><p>{html.escape(f['a'])}</p>" for f in faq)
    return f'<section class="faq"><h2>Частые вопросы</h2>{items}</section>'


def render_post(article: dict, slug: str, image_url: str) -> str:
    canonical_url = f"{SITE_URL}/posts/{slug}.html"
    jsonld = json.dumps({
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": article["h1"],
        "description": article["meta_description"],
        "image": image_url,
        "datePublished": date.today().isoformat(),
        "author": {"@type": "Person", "name": article.get("author", "")},
        "publisher": {"@type": "Organization", "name": 'Логопедический центр "Сами Мамы"'},
        "mainEntityOfPage": canonical_url,
    }, ensure_ascii=False)
    return POST_TEMPLATE.format(
        meta_title=html.escape(article["meta_title"]),
        meta_description=html.escape(article["meta_description"]),
        canonical_url=canonical_url,
        image_url=image_url,
        site_name=html.escape(SITE_NAME),
        h1=html.escape(article["h1"]),
        date_human=date.today().strftime("%d.%m.%Y"),
        body_html=article["body_html"],
        faq_html=render_faq(article.get("faq", [])),
        serm_url=SERM_URL,
        jsonld=jsonld,
    )


def rebuild_index_and_sitemap() -> None:
    state = load_json(STATE_PATH, {"posts": []})
    posts = sorted(state.get("posts", []), key=lambda p: p["date"], reverse=True)

    topics_by_id = {t["id"]: t for t in load_json(TOPICS_PATH, [])}
    items = []
    for p in posts:
        title = topics_by_id.get(p["id"], {}).get("title", p["slug"])
        items.append(
            f'<li><a href="posts/{p["slug"]}.html">{html.escape(title)}</a> '
            f'<span class="post-date">{p["date"]}</span></li>'
        )
    index_html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(SITE_NAME)}</title>
<meta name="description" content="Статьи о развитии речи, воспитании и психологии ребёнка от логопедического центра &quot;Сами Мамы&quot;.">
<link rel="stylesheet" href="style.css">
</head>
<body>
<header class="site-header">{html.escape(SITE_NAME)}</header>
<main>
<ul class="post-list">
{"".join(items)}
</ul>
</main>
</body>
</html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(index_html)

    urls = [f"<url><loc>{SITE_URL}/index.html</loc></url>"]
    for p in posts:
        urls.append(f"<url><loc>{SITE_URL}/posts/{p['slug']}.html</loc></url>")
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls) + "\n</urlset>\n"
    )
    with open("sitemap.xml", "w", encoding="utf-8") as f:
        f.write(sitemap)


def main():
    topic = pick_next_topic()
    print(f"Тема #{topic['id']}: {topic['topic']} - {topic['title']}")

    article = generate_article(topic)
    article["author"] = topic.get("author", "Баркаева")
    slug = f"{topic['id']:03d}-{slugify(article['h1'][:60])}"

    print("Генерирую картинку...")
    img_bytes = generate_image(article["image_description"])
    image_path = f"images/{slug}.jpg"
    os.makedirs("images", exist_ok=True)
    with open(image_path, "wb") as f:
        f.write(img_bytes)

    os.makedirs(POSTS_DIR, exist_ok=True)
    post_html = render_post(article, slug, f"../{image_path}")
    with open(f"{POSTS_DIR}/{slug}.html", "w", encoding="utf-8") as f:
        f.write(post_html)

    mark_posted(topic["id"], slug)
    rebuild_index_and_sitemap()

    subprocess.run(["git", "config", "user.name", "sm-blog-bot"], check=True)
    subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-m", f"Пост #{topic['id']}: {article['h1']}"], check=True)
    subprocess.run(["git", "push"], check=True)
    print(f"Опубликовано: posts/{slug}.html")


if __name__ == "__main__":
    main()
