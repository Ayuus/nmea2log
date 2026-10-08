// How the logbook page looks: a theme (auto / light / dark) and a layout (auto / cards / table). It sets two classes on <html>
// that logbook-layout.css reacts to: "dark" and "cards". "auto" follows the device: the dark mode of the system, and cards on a
// narrow screen (a phone held upright), a table on anything wider.
//
// Where the choice comes from:
//  * in a browser, the two buttons next to the language buttons (logbook-layout.js); the choice is kept in localStorage,
//  * in the Android and iOS app, their Settings: the app calls logbookPrefs.setFromHost({theme, view}) after loading the
//    page, and the buttons disappear. (The apps' web views do not keep localStorage between runs.)
//
// Inlined in <head> by html_writer.py, so the right look is there from the first paint instead of flashing light and table.
(function () {
  var KEY = 'logbook-view';
  var THEMES = ['auto', 'light', 'dark'];
  var VIEWS = ['auto', 'cards', 'table'];
  var NARROW_PX = 700;
  var state = {theme: 'auto', view: 'auto'};
  var root = document.documentElement;
  var mqDark = window.matchMedia('(prefers-color-scheme: dark)');
  var mqNarrow = window.matchMedia('(max-width: ' + NARROW_PX + 'px)');

  try {
    var saved = JSON.parse(localStorage.getItem(KEY));
    if (saved && THEMES.indexOf(saved.theme) >= 0) state.theme = saved.theme;
    if (saved && VIEWS.indexOf(saved.view) >= 0) state.view = saved.view;
  } catch (e) {}

  function apply() {
    root.classList.toggle('dark', state.theme === 'dark' || (state.theme === 'auto' && mqDark.matches));
    root.classList.toggle('cards', state.view === 'cards' || (state.view === 'auto' && mqNarrow.matches));
    document.dispatchEvent(new CustomEvent('logbookprefs'));
  }

  function next(list, value) {
    return list[(list.indexOf(value) + 1) % list.length];
  }

  window.logbookPrefs = {
    get: function () { return {theme: state.theme, view: state.view}; },
    // The buttons: the next theme / layout in the row auto, light, dark / auto, cards, table.
    cycle: function (name) {
      if (name === 'theme') state.theme = next(THEMES, state.theme);
      else state.view = next(VIEWS, state.view);
      try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
      apply();
    },
    // The app's Settings. Not remembered here: the app tells the page again on every load.
    setFromHost: function (prefs) {
      if (prefs && THEMES.indexOf(prefs.theme) >= 0) state.theme = prefs.theme;
      if (prefs && VIEWS.indexOf(prefs.view) >= 0) state.view = prefs.view;
      root.classList.add('host-prefs');
      apply();
    },
  };

  mqDark.addEventListener('change', apply);
  mqNarrow.addEventListener('change', apply);
  apply();
})();
