// The parts of the card layout that need the page itself (logbook-layout.css has the look; logbook-prefs.js decides when it is
// on): the "More" button of the totals, and the labels of the cells of a card. Inlined at the end of <body> by html_writer.py,
// after the script that defines I18N and the language switcher.
(function () {
  var root = document.documentElement;
  var MAIN_TOTALS = ['totals_trips', 'totals_distance', 'totals_hours', 'totals_top_speed'];
  var moreButtons = [];

  function text(key) {
    var table = I18N[root.lang] || I18N.en;
    return table[key] !== undefined ? table[key] : key;
  }

  // With a single year the totals at the top repeat the ones of that year.
  var topTotals = document.querySelector('body > section.totals');
  if (document.querySelectorAll('section.year').length === 1 && topTotals) topTotals.classList.add('totals-redundant');

  // Four main tiles, the rest behind "More" (only in the cards layout, see the css).
  document.querySelectorAll('section.totals').forEach(function (totals) {
    if (totals.classList.contains('totals-redundant')) return;
    totals.querySelectorAll('.stat').forEach(function (stat) {
      var label = stat.querySelector('[data-i18n]');
      var place = label ? MAIN_TOTALS.indexOf(label.dataset.i18n) : -1;
      if (place >= 0) {
        stat.classList.add('main');
        stat.style.order = place;
      } else {
        stat.style.order = MAIN_TOTALS.length;
      }
    });
    var more = document.createElement('button');
    more.type = 'button';
    more.className = 'totals-more';
    more.addEventListener('click', function () { totals.classList.toggle('expanded'); refreshTexts(); });
    totals.parentNode.insertBefore(more, totals.nextSibling);
    moreButtons.push([more, totals]);
  });

  function refreshTexts() {
    moreButtons.forEach(function (pair) {
      pair[0].textContent = text(pair[1].classList.contains('expanded') ? 'totals_less' : 'totals_more');
    });
  }

  // The label above a value in a card is the header of its column in the (then hidden) table header.
  function labelCells() {
    document.querySelectorAll('table.trips').forEach(function (table) {
      var headers = table.tHead ? table.tHead.querySelectorAll('th') : [];
      table.tBodies[0].querySelectorAll(':scope > tr.trip-row').forEach(function (row) {
        Array.prototype.forEach.call(row.children, function (cell, i) {
          if (!headers[i]) return;
          var full = headers[i].querySelector('.hdr-full');
          cell.setAttribute('data-label', (full || headers[i]).textContent.trim());
        });
      });
    });
  }

  function refresh() {
    labelCells();
    refreshTexts();
  }
  refresh();
  // The language switcher rewrites the headers and sets <html lang>.
  var observer = new MutationObserver(refresh);
  document.querySelectorAll('table.trips > thead').forEach(function (head) {
    observer.observe(head, {subtree: true, childList: true, characterData: true});
  });
  observer.observe(root, {attributes: true, attributeFilter: ['lang']});
})();
