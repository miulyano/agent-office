---
name: git-workflow
description: Ветки, сообщения коммитов, PR и релизы в Agent Office. Использовать перед созданием ветки, коммитом, пушем, открытием PR и выпуском версии.
license: MIT
---

# Git в Agent Office

В `main` напрямую не коммитим. Любое изменение идёт через ветку и PR.

## Старт

```bash
git fetch --prune origin
git status --porcelain
git checkout -b <type>/<kebab-case-description> origin/main
```

Ветвиться от `origin/main`, а не от локального `main`: так ветка не вырастет из устаревшего кода.

## Имена веток

| Тип | Когда | Пример |
|---|---|---|
| `feat/` | Новая фича для пользователя | `feat/office-screenshot` |
| `fix/` | Исправление бага | `fix/office-generation-proxy` |
| `refactor/` | Внутренние изменения без смены поведения | `refactor/split-ledger` |
| `docs/` | Только документация | `docs/readme-and-skills` |
| `test/` | Только тесты | `test/codex-limits` |
| `chore/` | Зависимости, конфиги, CI | `chore/bump-typescript` |
| `perf/` | Оптимизация | `perf/journal-tail` |

Имя описывает суть изменения. Автоматические имена вроде `claude/<id>` не годятся: такую ветку перед пушем переименовать.

## Коммиты: Conventional Commits

Формат: `<type>(<scope>): <описание в повелительном наклонении>`.

- `scope` необязателен: `server`, `floors`, `menubar`, `stats`.
- Описание до 70 символов, без точки в конце.
- Тело через пустую строку объясняет, почему, а не что.

```
feat(floors): add a lift ride for the lounge
fix(server): skip truncated journal lines
docs: describe office manifest fields
```

## Перед PR

1. `npm test` зелёный, `npm run check` чистый.
2. Если менялись `src/*.ts`: `npm run build`, собранные `web/*.js` в коммите.
3. README совпадает с кодом, см. скил `readme-sync`.
4. В диффе нет личных данных, см. скил `public-repo-hygiene`.
5. Для `feat` и `fix` есть строка в `## [Unreleased]` файла `CHANGELOG.md`: `### Added`, `Changed`, `Fixed` или `Removed`, 1–2 строки. `docs/`, `test/`, `chore/` чейнджлог не трогают.

## PR и мерж

Заголовок PR тоже в формате Conventional Commits: он станет текстом коммита в `main`. В описании указать ветку. Мерж только через squash:

```bash
gh pr merge --squash --delete-branch
```

## Релиз

Версия по SemVer: `fix` поднимает PATCH, `feat` MINOR, ломающее изменение MAJOR. В обычных PR номер не меняется, только в релизном:

1. В `CHANGELOG.md` заголовок `## [Unreleased]` стал `## [x.y.z] – ГГГГ-ММ-ДД`, выше новый пустой `## [Unreleased]`, внизу ссылка `[x.y.z]: .../releases/tag/vx.y.z`.
2. Новая версия в `VERSION`, `package.json`, `package-lock.json` (`npm version x.y.z --no-git-tag-version`) и бейдже README.
3. `npm test`, PR `chore: release x.y.z`, squash.
4. После мержа тег на коммит в `main`. Релиз GitHub соберёт `.github/workflows/release.yml` из раздела чейнджлога.

```bash
git fetch origin && git tag vx.y.z origin/main && git push origin vx.y.z
```
