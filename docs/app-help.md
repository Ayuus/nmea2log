# The help of the apps

The Android and iOS apps have a help: one page for both, in English, Dutch, French and German (any other language shows English). It is
opened with a short welcome the first time the app starts (not for someone who has already filled in the W2K-2 login), and
at any time from **Settings > Help and manual** (the button at the top; the toolbar has no room for another icon).

## Where it lives

| What | Where |
|---|---|
| The text, all four languages, both platforms | [`src/nmea2log/assets/help/help.html`](../src/nmea2log/assets/help/help.html) |
| What the apps do with it | [`src/nmea2log/help_page.py`](../src/nmea2log/help_page.py) (tests: `tests/test_help_page.py`) |
| The screenshots in it | each app brings its own, as `img/<name>.png` (`settings`, `logbook`, `map-trip`, `boat-mode`): Android `app/src/main/assets/help/img/`, iOS `src/mysailinglogbook/resources/help/img/` |
| The welcome texts, the Help button | `app_texts.py` (`button_help`, `dialog_help_*`, `button_close`), shared like all app texts |

The page is plain HTML and CSS, no JavaScript. Every block of text is there twice (`lang="en"`, `lang="nl"`), and what only
holds for one platform has `data-platform="android"` or `"ios"`. `help_page.render()` puts the language and the platform on the
`<html>` element and the page's own CSS hides the rest.

## How it stays up to date, and works at sea

1. The app comes with the page (it is part of the `nmea2log` package), so the help works from the first start, without internet.
2. When the app starts it asks GitHub (`raw.githubusercontent.com/Ayuus/nmea2log/main/...`) whether the page changed, at most
   once a day (a conditional request, so it costs nothing when nothing changed), and keeps a newer page in its own storage
   (`help/help.html` in the app's files on Android, in the cache folder on iOS).
3. The app shows the downloaded page when it has one, the bundled page otherwise. A download that is not a help page of the
   format this code knows (`<meta name="help-format" content="1">`), or is too big, is thrown away; no internet is just
   quiet.

So: **to change the text, edit `help.html` and push to `main`** -- every phone that has internet picks it up within a day, no
new app version needed. A change that needs new app code (a new screenshot name, a new format) goes with an app update; raise
`help-format` for a page that older apps must not use (they keep the page they have).

The screenshots are the one thing that does not refresh: they are part of each app. Retake them when the screens change
(`docs/screenshots` of each app has the full-size ones; the help uses them at 360 px wide).
