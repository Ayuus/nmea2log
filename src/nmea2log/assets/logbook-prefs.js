// How the logbook page looks: it sets two classes on <html> that logbook-layout.css reacts to, "dark" and "cards".
//  * "dark": when the device is in dark mode -- or what the app says: the Android and iOS apps have an Appearance setting
//    (light / dark / follow the device) and tell the page after loading it, with logbookPrefs.setFromHost({theme}).
//  * "cards": on a narrow screen (a phone held upright); a wide screen shows the table.
//
// Inlined in <head> by html_writer.py, so the right look is there from the first paint instead of flashing light and table.
(function () {
  var NARROW_PX = 700;
  var theme = 'auto';  // 'auto' (follow the device), 'light' or 'dark'
  var root = document.documentElement;
  var mqDark = window.matchMedia('(prefers-color-scheme: dark)');
  var mqNarrow = window.matchMedia('(max-width: ' + NARROW_PX + 'px)');

  function apply() {
    root.classList.toggle('dark', theme === 'dark' || (theme === 'auto' && mqDark.matches));
    root.classList.toggle('cards', mqNarrow.matches);
  }

  window.logbookPrefs = {
    setFromHost: function (prefs) {
      if (prefs && (prefs.theme === 'auto' || prefs.theme === 'light' || prefs.theme === 'dark')) theme = prefs.theme;
      apply();
    },
  };

  mqDark.addEventListener('change', apply);
  mqNarrow.addEventListener('change', apply);
  apply();
})();
