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

import hashlib
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
PAGES_DIR = "page"
PAGE_SIZE = 10

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
  "author": "...",
  "about_speech": true/false
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
  НЕ включай в body_html подпись автора и НЕ включай никакие блоки/ссылки на
  samimami.ru/online или "артикуляционную гимнастику", даже если они были в оригинале -
  это добавляется отдельно после текста, не нужно дублировать или пересказывать своими
  словами.
- Тон: обращение на "ты" к маме, тепло, экспертно, без снобизма, без осуждения - тот же
  голос, что в оригинале, но текст и примеры - другие, не пересказ.
- ЗНАКИ ПРЕПИНАНИЯ: только короткое тире "-", никогда "—" или "–". Только прямые кавычки
  "текст", никогда «ёлочки» и не „лапки".
- faq: 2-3 вопроса-ответа по теме статьи (для расширенных сниппетов), ответ 1-2 предложения.
- author: имя автора статьи, как в оригинале (например "Баркаева", "Лучия Розенталь"
  или "Ольга Белова").
- about_speech: true, если статья про развитие речи, звукопроизношение, логопедию,
  запуск речи и т.п. (обычно это статьи за авторством Лучии Розенталь или Ольги
  Беловой) - иначе false.
- НЕ добавляй хэштеги.
"""

SPEECH_CTA_HTML = (
    '<aside class="speech-cta">'
    '<p>Если ваш ребёнок ещё не говорит или говорит неуверенно, держите рабочую '
    'артикуляционную гимнастику + календарь выполнения. Результаты улучшатся очень быстро.</p>'
    '<a class="speech-cta-link" href="https://samimami.ru/online">'
    'Артикуляционная гимнастика для красивой речи &rarr;</a>'
    '</aside>'
)


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


def style_version() -> str:
    """Хэш содержимого style.css для cache-buster'а в ссылке на стиль - без
    него браузеры кэшируют style.css на весь max-age (10 минут) или дольше по
    своей эвристике, и после правок стилей люди какое-то время видят старую
    версию, даже если HTML уже обновился."""
    with open("style.css", "rb") as f:
        return hashlib.md5(f.read()).hexdigest()[:8]


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


def is_speech_author(name: str) -> bool:
    return any(n in name for n in ("Лучия", "Розенталь", "Ольга", "Белова"))


def author_role(name: str) -> str:
    if "Ольга" in name or "Белова" in name:
        return "логопед"
    if is_speech_author(name):
        return "логопед-дефектолог"
    return "психолог и основатель"


def mark_posted(slug: str, title: str) -> None:
    state = load_json(STATE_PATH, {"posts": []})
    state.setdefault("posts", []).append({
        "slug": slug, "title": title, "date": date.today().isoformat(),
    })
    save_json(STATE_PATH, state)


SHARE_ICONS = {
    "vk": '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12.8 17.2c-5.6 0-8.8-3.8-8.9-10.2h2.8c.1 4.7 2.2 6.6 3.8 7V7h2.6v4c1.6-.2 3.3-2 3.9-4h2.6c-.4 2.5-2.2 4.3-3.4 5 1.2.6 3.3 2.2 4.1 5.2h-2.9c-.6-1.9-2.1-3.3-4.1-3.5v3.5h-.5Z"/></svg>',
    "telegram": '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M21.5 4.5 3 11.6c-1.1.4-1.1 1.6.1 1.9l4.6 1.4 1.8 5.6c.2.7 1.1.9 1.6.3l2.5-2.8 4.7 3.5c.8.6 1.9.2 2.1-.8l3.2-14.8c.3-1.2-.8-2-1.7-1.6ZM8.6 14.4l9-5.7c.3-.2.6.1.3.4l-7.3 6.7-.3 3.1-1.4-4.5Z"/></svg>',
    "max": '<i class="max-ico" aria-hidden="true"></i>',
    "ok": '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12 12a5 5 0 1 0 0-10 5 5 0 0 0 0 10Zm0-7.2a2.2 2.2 0 1 1 0 4.4 2.2 2.2 0 0 1 0-4.4ZM7.6 13.2c-.6.4-.7 1.2-.3 1.7.3.5 1.1.8 2.1 1.1l-2.2 2.2c-.5.5-.5 1.2 0 1.7.5.4 1.2.4 1.7 0L12 17.6l3.1 3.1c.5.4 1.2.4 1.7 0 .5-.5.5-1.2 0-1.7l-2.2-2.2c1-.3 1.8-.6 2.1-1.1.4-.5.3-1.3-.3-1.7-.5-.3-1.3-.2-1.9.2-1 .6-2.3.6-3.4 0-.6-.4-1.4-.5-1.9-.2Z"/></svg>',
    "whatsapp": '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12 3a9 9 0 0 0-7.7 13.6L3 21l4.5-1.2A9 9 0 1 0 12 3Zm4.6 12.3c-.2.6-1.1 1.1-1.6 1.1-.4.1-.9.1-2.9-.7-2.4-1-4-3.4-4.1-3.6-.1-.2-1-1.3-1-2.5s.6-1.8.9-2c.2-.2.4-.3.6-.3h.4c.1 0 .3 0 .4.3l.6 1.5c.1.1.1.3 0 .4l-.3.4-.3.3c-.1.1-.2.3-.1.5.4.7 1 1.3 1.6 1.7.6.5 1.2.7 1.4.8.2.1.3.1.4-.1l.6-.7c.2-.2.3-.2.5-.1l1.4.7c.2.1.3.2.3.3.1.2.1.6-.1 1.1Z"/></svg>',
    "copy": '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M5 15V6.5A2.5 2.5 0 0 1 7.5 4H15"/></svg>',
}


def render_share(url: str, title: str) -> str:
    """Блок «Поделиться»: ВК, Telegram, MAX, ОД, WhatsApp, копирование ссылки."""
    from urllib.parse import quote
    u, t = quote(url, safe=""), quote(title, safe="")
    both = quote(f"{title} {url}", safe="")
    links = [
        ("vk", "ВК", f"https://vk.com/share.php?url={u}&title={t}"),
        ("telegram", "Telegram", f"https://t.me/share/url?url={u}&text={t}"),
        ("max", "MAX", f"https://max.ru/:share?text={both}"),
        ("ok", "ОД", f"https://connect.ok.ru/offer?url={u}&title={t}"),
        ("whatsapp", "WhatsApp", f"https://wa.me/?text={both}"),
    ]
    items = "".join(
        f'<a class="share-btn" href="{html.escape(href)}" target="_blank" rel="noopener nofollow" '
        f'aria-label="Поделиться: {name}">{SHARE_ICONS[key]}<span>{name}</span></a>'
        for key, name, href in links
    )
    copy = (f'<button type="button" class="share-btn" data-copy="{html.escape(url)}" '
            f'aria-label="Скопировать ссылку">{SHARE_ICONS["copy"]}<span>Ссылка</span></button>')
    script = ("<script>document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){"
              "var u=b.getAttribute('data-copy'),s=b.querySelector('span'),o=s.textContent;"
              "function ok(){s.textContent='Готово';b.classList.add('done');"
              "setTimeout(function(){s.textContent=o;b.classList.remove('done');},2200);}"
              "if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(u).then(ok,fb);}else{fb();}"
              "function fb(){var t=document.createElement('textarea');t.value=u;t.style.position='fixed';t.style.opacity='0';"
              "document.body.appendChild(t);t.select();try{document.execCommand('copy');ok();}catch(e){}document.body.removeChild(t);}"
              "});});</script>")
    return (f'<div class="share"><div class="share-title">Поделиться статьёй</div>'
            f'<div class="share-row">{items}{copy}</div></div>{script}')


METRIKA_HTML = r"""<!-- Yandex.Metrika counter -->
<script type="text/javascript">
    (function(m,e,t,r,i,k,a){m[i]=m[i]||function(){(m[i].a=m[i].a||[]).push(arguments)};
        m[i].l=1*new Date();
        for (var j = 0; j < document.scripts.length; j++) {if (document.scripts[j].src === r) { return; }}
        k=e.createElement(t),a=e.getElementsByTagName(t)[0],k.async=1,k.src=r,a.parentNode.insertBefore(k,a)
    })(window, document,'script','https://mc.yandex.ru/metrika/tag.js?id=113442658', 'ym');

    ym(113442658, 'init', {ssr:true, webvisor:true, clickmap:true, ecommerce:"dataLayer", referrer: document.referrer, url: location.href, accurateTrackBounce:true, trackLinks:true});
    document.addEventListener('click', function (ev) {
        var a = ev.target.closest && ev.target.closest('a[href],button[data-copy]');
        if (!a) return;
        var h = a.getAttribute('href') || '';
        if (a.hasAttribute('data-copy')) ym(113442658, 'reachGoal', 'share_click', {network: 'copy'});
        else if (a.classList.contains('share-btn')) ym(113442658, 'reachGoal', 'share_click', {network: (a.getAttribute('aria-label') || '').replace('Поделиться: ', '')});
        else if (/^tel:/i.test(h)) ym(113442658, 'reachGoal', 'phone_click');
        else if (/^https?:\/\/(www\.)?samimami\.ru/i.test(h)) ym(113442658, 'reachGoal', 'site_click');
    }, true);
</script>
<noscript><div><img src="https://mc.yandex.ru/watch/113442658" style="position:absolute; left:-9999px;" alt="" /></div></noscript>
<!-- /Yandex.Metrika counter -->
<!-- cookie-notice -->
<script>
(function () {
    try { if (localStorage.getItem('ck_ok')) return; } catch (e) {}
    function show() {
        var d = document.createElement('div');
        d.setAttribute('role', 'dialog'); d.setAttribute('aria-label', 'Файлы cookie');
        d.style.cssText = 'position:fixed;left:16px;right:16px;bottom:16px;max-width:560px;margin:0 auto;z-index:99999;background:#2B1D33;color:#fff;border-radius:16px;padding:16px 18px;box-shadow:0 12px 36px rgba(0,0,0,.28);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;display:flex;gap:14px;align-items:center;flex-wrap:wrap';
        d.innerHTML = '<span style="flex:1 1 260px">Мы используем файлы cookie и Яндекс Метрику, чтобы сайт работал лучше. Продолжая пользоваться сайтом, вы соглашаетесь с этим. <a href="https://samimami.ru/conf" style="color:#E2C6EC;text-decoration:underline">Подробнее</a></span><button type="button" style="flex:none;border:0;border-radius:999px;background:#9A51AB;color:#fff;font:inherit;font-weight:700;padding:10px 22px;cursor:pointer">Понятно</button>';
        d.querySelector('button').onclick = function () { try { localStorage.setItem('ck_ok', '1'); } catch (e) {} d.remove(); };
        document.body.appendChild(d);
    }
    if (document.body) show(); else document.addEventListener('DOMContentLoaded', show);
})();
</script>
<!-- /cookie-notice -->
"""


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
<link rel="stylesheet" href="../style.css?v={style_v}">
<script type="application/ld+json">
{jsonld}
</script>
</head>
<body class="t-body" style="margin:0;">
<div id="allrecords" class="t-records" data-tilda-project-id="8566589" data-tilda-page-id="42951679" data-tilda-formskey="8c54f63a0172c9caf3e8edc6b8566589" data-tilda-cookie="no" data-tilda-lazy="yes" data-tilda-root-zone="com" data-tilda-project-country="RU">
{header_html}
<main class="post">
<h1>{h1}</h1>
<p class="post-date">{date_human}</p>
<img class="post-image" src="{image_url_rel}" alt="{h1}">
{body_html}
{speech_cta_html}
{faq_html}
<p class="post-signature"><em>{signature}</em></p>
{share_html}
{related_html}
<nav class="post-nav">
<span class="post-nav-side">{prev_link}</span>
<span class="post-nav-home">
<a href="https://samimami.ru">На главную сайта</a>
<span class="post-nav-sep">&middot;</span>
<a href="../index.html">На главную блога</a>
</span>
<span class="post-nav-side">{next_link}</span>
</nav>
</main>
{footer_html}
</div>
</body>
</html>
"""


def excerpt(body_html: str, length: int = 140) -> str:
    text = re.sub(r"<[^>]+>", " ", body_html)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "…"


# Грубая тематическая разметка по ключевым словам - нужна только для того,
# чтобы подбирать 2-3 "Похожие статьи" в конце поста (внутренняя перелинковка
# помимо навигации предыдущая/следующая). Каждый новый пост размечается этим
# же способом автоматически, руками ничего поддерживать не нужно.
TOPIC_KEYWORDS = {
    "rech": ["заикан", "речь", "речи", "слог", "звук", "логопед", "произнош",
             "дефектолог", "билингв", "английск", "пересказ", "шепел"],
    "trevoga": ["тревог", "ритуал", "страш", "боит", "паник", "экотревог"],
    "samootsenka": ["самооцен", "завид", "хвал", "похвал", "перфекцион", "рвёт рисунок",
                     "рвет рисунок", "рисунок", "внешност"],
    "granitsy": ["бабушк", "няня", "нянь", "границ"],
    "sad_shkola": ["сад", "школ", "травл", "адаптац", "простуд"],
}


# Минимум упоминаний темы в тексте, чтобы её засчитать - иначе одно случайное
# слово (например "школа" мельком в статье не про сад/школу) создавало бы
# мусорные совпадения между совсем не связанными статьями.
TOPIC_MIN_STRENGTH = 5


def topic_strengths(article: dict) -> dict[str, int]:
    text = (article.get("h1", "") + " " + re.sub(r"<[^>]+>", " ", article.get("body_html", ""))).lower()
    return {topic: sum(text.count(kw) for kw in keywords) for topic, keywords in TOPIC_KEYWORDS.items()}


def topics_for(article: dict) -> dict[str, int]:
    return {t: s for t, s in topic_strengths(article).items() if s >= TOPIC_MIN_STRENGTH}


def pick_related(slug: str, posts: list[dict], topics_by_slug: dict[str, dict[str, int]],
                  limit: int = 3) -> list[dict]:
    own_topics = topics_by_slug.get(slug, {})
    if not own_topics:
        return []
    scored = []
    for p in posts:
        if p["slug"] == slug:
            continue
        other_topics = topics_by_slug.get(p["slug"], {})
        overlap = sum(min(strength, other_topics[t]) for t, strength in own_topics.items() if t in other_topics)
        if overlap:
            scored.append((overlap, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:limit]]


def render_related(related: list[dict]) -> str:
    if not related:
        return ""
    items = "".join(
        f'<li><a href="{p["slug"]}.html">{html.escape(p["title"])}</a></li>' for p in related
    )
    return f'<section class="related-posts"><h2>Похожие статьи</h2><ul>{items}</ul></section>'


def render_faq(faq: list) -> str:
    if not faq:
        return ""
    items = "".join(f"<h3>{html.escape(f['q'])}</h3><p>{html.escape(f['a'])}</p>" for f in faq)
    return f'<section class="faq"><h2>Частые вопросы</h2>{items}</section>'


def render_post(article: dict, slug: str, prev: dict | None, next_: dict | None,
                 head_assets: str, header_html: str, footer_html: str,
                 related: list[dict] | None = None) -> str:
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
    author = article.get("author", "")
    signature = f'{author}, {author_role(author)} логопедического центра "Сами Мамы"'

    return POST_TEMPLATE.format(
        style_v=style_version(),
        meta_title=html.escape(article["meta_title"]),
        meta_description=html.escape(article["meta_description"]),
        canonical_url=canonical_url,
        image_url_rel=image_url_rel,
        image_url_abs=image_url_abs,
        h1=html.escape(article["h1"]),
        date_human=article.get("date", date.today().isoformat()),
        body_html=article["body_html"],
        speech_cta_html=SPEECH_CTA_HTML if (is_speech_author(author) or article.get("about_speech")) else "",
        faq_html=render_faq(article.get("faq", [])),
        signature=html.escape(signature),
        related_html=render_related(related or []),
        share_html=render_share(canonical_url, article["h1"]),
        jsonld=jsonld,
        head_assets=head_assets,
        header_html=header_html,
        footer_html=footer_html,
        prev_link=prev_link,
        next_link=next_link,
    )


def page_url(page_num: int, from_root: bool) -> str:
    """Ссылка на page_num, если текущая страница лежит в корне (from_root) или в page/."""
    if page_num == 1:
        return "index.html" if from_root else "../index.html"
    return f"{PAGES_DIR}/{page_num}.html" if from_root else f"{page_num}.html"


def render_pagination(page_num: int, total_pages: int) -> str:
    if total_pages <= 1:
        return ""
    from_root = page_num == 1
    if page_num > 1:
        prev_html = f'<a href="{page_url(page_num - 1, from_root)}">&larr; Назад</a>'
    else:
        prev_html = '<span class="pagination-disabled">&larr; Назад</span>'
    if page_num < total_pages:
        next_html = f'<a href="{page_url(page_num + 1, from_root)}">Далее &rarr;</a>'
    else:
        next_html = '<span class="pagination-disabled">Далее &rarr;</span>'
    return f'<nav class="pagination">{prev_html}{next_html}</nav>'


def rebuild_all() -> None:
    state = load_json(STATE_PATH, {"posts": []})
    posts = state.get("posts", [])  # хронологический порядок публикации

    head_assets = load_partial("head_assets.html") + METRIKA_HTML
    header_html = load_partial("header.html")
    footer_html = load_partial("footer.html")
    style_v = style_version()

    os.makedirs(POSTS_DIR, exist_ok=True)

    articles_by_slug = {}
    for p in posts:
        article = load_json(f"{POSTS_DATA_DIR}/{p['slug']}.json", None)
        if article is not None:
            articles_by_slug[p["slug"]] = article
    topics_by_slug = {slug: topics_for(a) for slug, a in articles_by_slug.items()}

    cards = []
    for i, p in enumerate(posts):
        article = articles_by_slug.get(p["slug"])
        if article is None:
            continue
        prev_p = posts[i - 1] if i > 0 else None
        next_p = posts[i + 1] if i + 1 < len(posts) else None
        prev = {"slug": prev_p["slug"], "title": prev_p["title"]} if prev_p else None
        next_ = {"slug": next_p["slug"], "title": next_p["title"]} if next_p else None
        related = pick_related(p["slug"], posts, topics_by_slug)
        post_html = render_post(article, p["slug"], prev, next_, head_assets, header_html, footer_html, related)
        with open(f"{POSTS_DIR}/{p['slug']}.html", "w", encoding="utf-8") as f:
            f.write(post_html)
        cards.append((p, article))

    ordered = list(reversed(cards))  # новые статьи сверху
    total_pages = max(1, (len(ordered) + PAGE_SIZE - 1) // PAGE_SIZE)
    os.makedirs(PAGES_DIR, exist_ok=True)

    for page_num in range(1, total_pages + 1):
        from_root = page_num == 1
        prefix = "" if from_root else "../"
        chunk = ordered[(page_num - 1) * PAGE_SIZE: page_num * PAGE_SIZE]

        items = []
        for p, article in chunk:
            items.append(
                f'<li><a class="post-card-link" href="{prefix}posts/{p["slug"]}.html">'
                f'<img class="post-card-img" src="{prefix}images/{p["slug"]}.jpg" alt="{html.escape(p["title"])}">'
                f'<span class="post-card-body">'
                f'<span class="post-card-title">{html.escape(p["title"])}</span>'
                f'<span class="post-card-excerpt">{html.escape(excerpt(article["body_html"]))}</span>'
                f'<span class="post-card-more">Читать далее &rarr;</span>'
                f'</span></a></li>'
            )

        page_suffix = "" if from_root else f" — страница {page_num}"
        page_title = html.escape(SITE_NAME + page_suffix)
        page_description = (
            "Статьи о развитии речи, воспитании и психологии ребёнка от логопедического "
            "центра &quot;Сами Мамы&quot;: заикание, задержка речи, билингвизм, подготовка "
            "к школе и повседневные вопросы, с которыми сталкиваются родители."
            + (f" Страница {page_num}." if not from_root else "")
        )
        intro_html = (
            '<h1 class="index-h1">Блог логопедического центра "Сами Мамы"</h1>\n'
            '<p class="index-intro">Разбираем на конкретных примерах, как помочь ребёнку '
            "с речью и не наделать ошибок в воспитании: заикание, задержка речи, билингвизм, "
            "капризы, адаптация в саду и школе. Пишут логопеды-дефектологи и психолог центра."
            "</p>"
            if from_root
            else f'<h1 class="index-h1">Блог логопедического центра "Сами Мамы" — страница {page_num}</h1>'
        )
        page_html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
<meta name="description" content="{page_description}">
<link rel="canonical" href="{SITE_URL}/{'index.html' if from_root else f'{PAGES_DIR}/{page_num}.html'}">
{head_assets}
<link rel="stylesheet" href="{prefix}style.css?v={style_v}">
</head>
<body class="t-body" style="margin:0;">
<div id="allrecords" class="t-records" data-tilda-project-id="8566589" data-tilda-page-id="42951679" data-tilda-formskey="8c54f63a0172c9caf3e8edc6b8566589" data-tilda-cookie="no" data-tilda-lazy="yes" data-tilda-root-zone="com" data-tilda-project-country="RU">
{header_html}
<main class="index-main">
{intro_html}
<ul class="post-list">
{"".join(items)}
</ul>
{render_pagination(page_num, total_pages)}
</main>
{footer_html}
</div>
</body>
</html>
"""
        target = "index.html" if from_root else f"{PAGES_DIR}/{page_num}.html"
        with open(target, "w", encoding="utf-8") as f:
            f.write(page_html)

    urls = [f"<url><loc>{SITE_URL}/index.html</loc></url>"]
    for page_num in range(2, total_pages + 1):
        urls.append(f"<url><loc>{SITE_URL}/{PAGES_DIR}/{page_num}.html</loc></url>")
    for p in posts:
        urls.append(f"<url><loc>{SITE_URL}/posts/{p['slug']}.html</loc></url>")
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls) + "\n</urlset>\n"
    )
    with open("sitemap.xml", "w", encoding="utf-8") as f:
        f.write(sitemap)


INDEXNOW_KEY = "bf333b0558fc09f68c35f37f17a34668"


def indexnow(urls: list[str]) -> None:
    """Сообщить Яндексу и Bing (протокол IndexNow) о новых/изменённых страницах, чтобы они переобошли их сразу.
    Сбой не критичен: статья уже опубликована."""
    try:
        for endpoint in ("https://yandex.com/indexnow", "https://api.indexnow.org/indexnow"):
            r = requests.post(endpoint, json={"host": "blog.samimami.ru", "key": INDEXNOW_KEY,
                              "keyLocation": f"{SITE_URL}/{INDEXNOW_KEY}.txt", "urlList": urls}, timeout=20)
            print(f"IndexNow {endpoint}: {r.status_code}")
    except Exception as e:  # noqa: BLE001
        print(f"IndexNow не отправлен: {e}")


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
    indexnow([f"{SITE_URL}/posts/{slug}.html", f"{SITE_URL}/index.html", f"{SITE_URL}/sitemap.xml"])

    subprocess.run(["git", "config", "user.name", "sm-blog-bot"], check=True)
    subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-m", f"Пост: {article['h1']}"], check=True)
    subprocess.run(["git", "push"], check=True)
    print(f"Опубликовано: posts/{slug}.html")


if __name__ == "__main__":
    main()
