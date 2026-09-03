# SM. Blog

Автопостинг статей в блог на поддомене (`blog.<домен>`), отдельно от Telegram-канала
@samimamiclub. Пишется на ту же тему, что и уже опубликованные посты в Telegram
(бэклог тем в `topics.json`), но каждый раз новым, самостоятельным текстом - не рерайтом
готового поста - чтобы не плодить дубли для Яндекса/Дзена.

## Как это работает

1. GitHub Actions по расписанию (`.github/workflows/daily-post.yml`, ежедневно) запускает
   `publish_blog_post.py`.
2. Скрипт берёт следующую неопубликованную тему из `topics.json` (сверяется с
   `blog_state.json`), генерирует статью через Anthropic API, картинку - через laozhang.ai.
3. Рендерит `posts/<slug>.html`, обновляет `index.html` и `sitemap.xml`, коммитит и пушит.
4. GitHub Pages автоматически публикует новую версию сайта.

## Настройка перед первым запуском

1. В `publish_blog_post.py` заменить плейсхолдеры:
   - `SITE_URL` - реальный адрес поддомена, например `https://blog.samimamy.ru`
   - `SERM_URL` - ссылка на лендинг SERM-продукта (с UTM-метками)
   - В `robots.txt` - тот же домен в строке `Sitemap:`
2. В настройках репозитория на GitHub:
   - Settings → Secrets and variables → Actions: добавить `ANTHROPIC_API_KEY`, `LAOZHANG_API_KEY`
   - Settings → Pages: Source = `main` / `/ (root)`, Custom domain = `blog.<домен>`
     (GitHub сам создаст файл `CNAME`)
3. В DNS домена добавить CNAME-запись: `blog` → `<github-username>.github.io`
4. На сайте на Тильде добавить блок/баннер со ссылкой на `blog.<домен>`

## Темы

`topics.json` изначально засеян из `PUBLISHED_ARTICLES.md` проекта `SM. Telegram`
(74 темы). Чтобы добавить новые темы для блога - просто дописать объекты
`{"id": N, "topic": "...", "title": "...", "author": "..."}` в конец файла.

## Локальный запуск для проверки

```bash
export ANTHROPIC_API_KEY=...
export LAOZHANG_API_KEY=...
pip install -r requirements.txt
python publish_blog_post.py
```
