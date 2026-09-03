# SM. Blog

Автопостинг статей в блог на `blog.samimami.ru`, отдельно от Telegram-канала
@samimamiclub. Пишется на ту же тему, что и уже опубликованные посты в Telegram
(бэклог тем в `topics.json`), но каждый раз новым, самостоятельным текстом - не рерайтом
готового поста - чтобы не плодить дубли для Яндекса/Дзена.

Хедер и футер каждой страницы блога - это настоящий Zero Block хедер/футер с
samimami.ru (взят как есть из `partials/header.html` и `partials/footer.html`,
внутренние ссылки переписаны на абсолютные `https://samimami.ru/...`), чтобы блог
визуально не отличался от основного сайта.

## Как это работает

1. GitHub Actions по расписанию (`.github/workflows/daily-post.yml`, ежедневно) запускает
   `publish_blog_post.py`.
2. Скрипт берёт следующую неопубликованную тему из `topics.json` (сверяется с
   `blog_state.json`), генерирует статью через DeepSeek API, картинку - через laozhang.ai.
3. Сохраняет структуру статьи в `posts_data/<slug>.json` и **перерисовывает все посты**
   из `posts_data/` заново (`posts/<slug>.html`, `index.html`, `sitemap.xml`) - это нужно,
   чтобы у соседних по хронологии постов всегда были правильные ссылки
   "предыдущая/следующая статья".
4. Коммитит и пушит - GitHub Pages публикует новую версию сайта.

## Настройка перед первым запуском

1. В настройках репозитория на GitHub:
   - Settings → Secrets and variables → Actions: добавить `DEEPSEEK_API_KEY`, `LAOZHANG_API_KEY`
     (уже сделано для `presa4ok/sm-blog`)
   - Settings → Pages: Custom domain = `blog.samimami.ru` (GitHub создаст файл `CNAME`)
2. В DNS домена `samimami.ru` добавить CNAME-запись: `blog` → `presa4ok.github.io`
3. На сайте на Тильде (необязательно, но полезно для трафика) добавить блок/баннер
   со ссылкой на `blog.samimami.ru`

## Темы

`topics.json` изначально засеян из `PUBLISHED_ARTICLES.md` проекта `SM. Telegram`
(74 темы). Чтобы добавить новые темы для блога - просто дописать объекты
`{"id": N, "topic": "...", "title": "...", "author": "..."}` в конец файла.

## Обновление хедера/футера

Если дизайн хедера/футера на samimami.ru изменится, нужно заново вытащить их из
живой страницы (`curl` HTML главной страницы, вырезать `<header id="t-header">...
</header>` и `<footer id="t-footer">...</footer>`, переписать `href="/..."` на
`href="https://samimami.ru/..."`) и положить в `partials/header.html` /
`partials/footer.html`.

## Локальный запуск для проверки

```bash
export DEEPSEEK_API_KEY=...
export LAOZHANG_API_KEY=...
pip install -r requirements.txt
python publish_blog_post.py
```
