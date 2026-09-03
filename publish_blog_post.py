"""
Ежедневный автопостинг статей в блог на blog.samimami.ru (GitHub Pages).

Источник контента - "пул" (pool/): каждый пост, ушедший в Telegram-канал
@samimamiclub, кладётся туда же (текст + картинка) скриптами из репозитория
SM. Telegram (post_telegram.py / bot.py) и пушится в этот репозиторий.

Раз в день берём самую старую статью из pool/, просим DeepSeek переписать её
для блога (та же тема и факты, другая структура и подача - не синонимический
спин), картинку берём ТУ ЖЕ, что была в Telegram (не генерируем новую).
Если пул пуст (например, в Telegram сегодня ничего не публиковали) - просто
ничего не делаем и выходим, день пропускается.

Каждый запуск перерисовывает ВСЕ посты заново из сохранённых данных в
posts_data/ - это нужно, чтобы ссылки "предыдущая/следующая статья" у
старых постов всегда указывали на актуальных соседей по хронологии.
"""

import html
import json
import os
import re
import shutil
import subprocess
from datetime import date

import requests

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

SITE_URL = "https://blog.samimami.ru"
SITE_NAME = 'Блог логопедического центра "Сами Мамы"'

POOL_DIR = "pool"
STATE_PATH = "blog_state.json"
POSTS_DIR = "posts"
POSTS_DATA_DIR = "posts_data"
PARTIALS_DIR = "partials"

BLOG_REWRITE_PROMPT = """\
Вот статья, которая уже была опубликована в Telegram-канале @samimamiclub:

---
{original_text}
---

Перепиши эту статью для блога {site_name} на отдельном сайте (не Telegram, не Дзен).
Тема и факты - те же, но текст должен быть написан ЗАНОВО: другая структура подачи,
другие подзаголовки, другие формулировки и примеры - как будто её написал другой автор
на ту же тему, а не переставил слова местами в оригинале. Никакого синонимического
пересказа (spin) - меняй структуру и подачу по-настоящему.

ФОРМАТ ОТВЕТА - строго JSON без markdown-обёртки, просто валидный JSON:
{{
  "meta_title": "...",
  "meta_description": "...",
  "h1": "...",
  "body_html": "...",
  "faq": [{{"q": "...", "a": "..."}}, {{"q": "...", "a": "..."}}],
  "author": "..."
}}

ТРЕБОВАНИЯ:
- meta_title: до 70 символов, содержит суть темы, без кликбейта ради кликбейта
- meta_description: 150-160 символов, естественный язык, отражает пользу статьи
- h1: цепляющий заголовок статьи (может отличаться от meta_title). Если он сформулирован
  как вопрос - заканчивается "?". Если это утверждение, а не вопрос - БЕЗ знака препинания
  в конце (не ставить точку).
- body_html: ТОЛЬКО теги <h2>, <h3>, <p>, <ul>, <li>, <strong>, <em> - никакого markdown.
  Структура: вводный абзац -> 3-5 секций с <h2>-подзаголовками (короче и чаще, а не один
  длинный блок) -> списки <ul><li> сразу после того подзаголовка, к которому относятся.
- Тон: обращение на "ты" к маме, тепло, экспертно, без снобизма, без осуждения - тот же
  голос, что в оригинале, но текст и примеры - другие, не пересказ.
- ЗНАКИ ПРЕПИНАНИЯ: только короткое тире "-", никогда "—" или "–". Только прямые кавычки
  "текст", никогда «ёлочки» и не „лапки".
- Заканчивается коротким тёплым абзацем + подписью автора: <p><em>Имя, должность
  логопедического центра "Сами Мамы"</em></p> - БЕЗ призывов комментировать/сохранять/
  подписаться и без какой-либо продающей ссылки/кнопки - это просто информационная статья.
- faq: 2-3 вопроса-ответа по теме статьи (для расширенных сниппетов), ответ 1-2 предложения.
- author: имя автора статьи, как в оригинале (например "Баркаева" или "Лучия Розенталь").
- НЕ добавляй хэштеги.
"""


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_partial(name: str) -> str:
    with open(os.path.join(PARTIALS_DIR, name), encoding="utf-8") as f:
        return f.read()


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


def pick_next_pool_item() -> dict | None:
    if not os.path.isdir(POOL_DIR):
        return None
    names = sorted(f[:-5] for f in os.listdir(POOL_DIR) if f.endswith(".json"))
    if not names:
        return None
    base = names[0]
    with open(f"{POOL_DIR}/{base}.json", encoding="utf-8") as f:
        item = json.load(f)
    item["_base"] = base
    return item


def consume_pool_item(base: str) -> None:
    os.remove(f"{POOL_DIR}/{base}.json")
    img_path = f"{POOL_DIR}/{base}.jpg"
    if os.path.exists(img_path):
        os.remove(img_path)


def rewrite_article(pool_item: dict) -> dict:
    prompt = BLOG_REWRITE_PROMPT.format(original_text=pool_item["text"], site_name=SITE_NAME)
    r = requests.post(
        DEEPSEEK_URL,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": DEEPSEEK_MODEL,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)


def mark_posted(slug: str, title: str) -> None:
    state = load_json(STATE_PATH, {"posts": []})
    state.setdefault("posts", []).append({
        "slug": slug, "title": title, "date": date.today().isoformat(),
    })
    save_json(STATE_PATH, state)


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
<meta property="og:image" content="{image_url_abs}">
<meta property="og:url" content="{canonical_url}">
{head_assets}
<link rel="stylesheet" href="../style.css">
<script type="application/ld+json">
{jsonld}
</script>
</head>
<body class="t-body" style="margin:0;">
{header_html}
<main class="post">
<h1>{h1}</h1>
<p class="post-date">{date_human}</p>
<img class="post-image" src="{image_url_rel}" alt="{h1}">
{body_html}
{faq_html}
<nav class="post-nav">
<span class="post-nav-side">{prev_link}</span>
<a class="post-nav-home" href="../index.html">На главную блога</a>
<span class="post-nav-side">{next_link}</span>
</nav>
</main>
{footer_html}
</body>
</html>
"""


def excerpt(body_html: str, length: int = 140) -> str:
    text = re.sub(r"<[^>]+>", " ", body_html)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "…"


def render_faq(faq: list) -> str:
    if not faq:
        return ""
    items = "".join(f"<h3>{html.escape(f['q'])}</h3><p>{html.escape(f['a'])}</p>" for f in faq)
    return f'<section class="faq"><h2>Частые вопросы</h2>{items}</section>'


def render_post(article: dict, slug: str, prev: dict | None, next_: dict | None,
                 head_assets: str, header_html: str, footer_html: str) -> str:
    canonical_url = f"{SITE_URL}/posts/{slug}.html"
    image_url_rel = f"../images/{slug}.jpg"
    image_url_abs = f"{SITE_URL}/images/{slug}.jpg"
    jsonld = json.dumps({
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": article["h1"],
        "description": article["meta_description"],
        "image": image_url_abs,
        "datePublished": article.get("date", date.today().isoformat()),
        "author": {"@type": "Person", "name": article.get("author", "")},
        "publisher": {"@type": "Organization", "name": 'Логопедический центр "Сами Мамы"'},
        "mainEntityOfPage": canonical_url,
    }, ensure_ascii=False)

    prev_link = (
        f'<a href="{prev["slug"]}.html">&larr; {html.escape(prev["title"])}</a>' if prev else ""
    )
    next_link = (
        f'<a href="{next_["slug"]}.html">{html.escape(next_["title"])} &rarr;</a>' if next_ else ""
    )

    return POST_TEMPLATE.format(
        meta_title=html.escape(article["meta_title"]),
        meta_description=html.escape(article["meta_description"]),
        canonical_url=canonical_url,
        image_url_rel=image_url_rel,
        image_url_abs=image_url_abs,
        h1=html.escape(article["h1"]),
        date_human=article.get("date", date.today().isoformat()),
        body_html=article["body_html"],
        faq_html=render_faq(article.get("faq", [])),
        jsonld=jsonld,
        head_assets=head_assets,
        header_html=header_html,
        footer_html=footer_html,
        prev_link=prev_link,
        next_link=next_link,
    )


def rebuild_all() -> None:
    state = load_json(STATE_PATH, {"posts": []})
    posts = state.get("posts", [])  # хронологический порядок публикации

    head_assets = load_partial("head_assets.html")
    header_html = load_partial("header.html")
    footer_html = load_partial("footer.html")

    os.makedirs(POSTS_DIR, exist_ok=True)
    cards = []
    for i, p in enumerate(posts):
        article = load_json(f"{POSTS_DATA_DIR}/{p['slug']}.json", None)
        if article is None:
            continue
        prev_p = posts[i - 1] if i > 0 else None
        next_p = posts[i + 1] if i + 1 < len(posts) else None
        prev = {"slug": prev_p["slug"], "title": prev_p["title"]} if prev_p else None
        next_ = {"slug": next_p["slug"], "title": next_p["title"]} if next_p else None
        post_html = render_post(article, p["slug"], prev, next_, head_assets, header_html, footer_html)
        with open(f"{POSTS_DIR}/{p['slug']}.html", "w", encoding="utf-8") as f:
            f.write(post_html)
        cards.append((p, article))

    items = []
    for p, article in reversed(cards):  # новые статьи сверху
        items.append(
            f'<li><a class="post-card-link" href="posts/{p["slug"]}.html">'
            f'<img class="post-card-img" src="images/{p["slug"]}.jpg" alt="{html.escape(p["title"])}">'
            f'<span class="post-card-body">'
            f'<span class="post-card-title">{html.escape(p["title"])}</span>'
            f'<span class="post-card-excerpt">{html.escape(excerpt(article["body_html"]))}</span>'
            f'<span class="post-card-more">Читать далее &rarr;</span>'
            f'</span></a></li>'
        )
    index_html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(SITE_NAME)}</title>
<meta name="description" content="Статьи о развитии речи, воспитании и психологии ребёнка от логопедического центра &quot;Сами Мамы&quot;.">
{head_assets}
<link rel="stylesheet" href="style.css">
</head>
<body class="t-body" style="margin:0;">
{header_html}
<main>
<ul class="post-list">
{"".join(items)}
</ul>
</main>
{footer_html}
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
    pool_item = pick_next_pool_item()
    if pool_item is None:
        print("Пул пуст - сегодня в блоге ничего не публикуем.")
        return

    print(f"Беру из пула: {pool_item['topic']}")
    article = rewrite_article(pool_item)
    article["date"] = date.today().isoformat()
    slug = f"{date.today().isoformat()}-{slugify(article['h1'][:60])}"

    os.makedirs("images", exist_ok=True)
    shutil.copyfile(f"{POOL_DIR}/{pool_item['_base']}.jpg", f"images/{slug}.jpg")

    os.makedirs(POSTS_DATA_DIR, exist_ok=True)
    save_json(f"{POSTS_DATA_DIR}/{slug}.json", article)

    mark_posted(slug, article["h1"])
    consume_pool_item(pool_item["_base"])
    rebuild_all()

    subprocess.run(["git", "config", "user.name", "sm-blog-bot"], check=True)
    subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-m", f"Пост: {article['h1']}"], check=True)
    subprocess.run(["git", "push"], check=True)
    print(f"Опубликовано: posts/{slug}.html")


if __name__ == "__main__":
    main()
