<?php
/**
 * Admin-only viewer for views.log (see logbook-index.php, which appends one line per
 * successful logbook view). Deploy once, next to index.php (see its own doc comment on why this
 * no longer needs a per-boat URL since 1.6.0). Add ?boot=<slug> to view a specific boat's log --
 * defaults to nmea2log_effective_slug(), same as index.php.
 *
 * Gated on manage_options (Administrator) specifically, not nmea2log_can_view() -- this is who
 * viewed the logbook and when, not the logbook itself, so a Logbook Reader/Writer account (which
 * only has read_logboek/edit_logboek_remarks, see nmea2log-remarks.php) is deliberately not
 * enough here.
 */

require_once __DIR__ . '/../wp-load.php';

if (!is_user_logged_in()) {
    $current_url = (is_ssl() ? 'https://' : 'http://') . $_SERVER['HTTP_HOST'] . $_SERVER['REQUEST_URI'];
    wp_redirect(wp_login_url($current_url));
    exit;
}

if (!current_user_can('manage_options')) {
    http_response_code(403);
    echo 'Alleen voor beheerders.';
    exit;
}

$slug = nmea2log_effective_slug();
$views_log_path = dirname(nmea2log_logbook_path($slug)) . '/views.log';
$lines = [];
if (file_exists($views_log_path)) {
    $raw = file_get_contents($views_log_path);
    $lines = $raw === false ? [] : array_filter(explode("\n", trim($raw)), fn($l) => $l !== '');
}
// Newest first -- what an admin checks this page for is almost always "who's looked at it
// recently", not the full history from the start.
$lines = array_reverse($lines);

header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
header('Pragma: no-cache');
header('Content-Type: text/html; charset=utf-8');
?>
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<title>Logboek - Weergaven</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 700px; margin: 2rem auto; padding: 0 1rem; color: #1a2a3a; }
  h1 { font-size: 1.3rem; }
  .count { color: #5a6b7a; margin-bottom: 1rem; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #e0e6ec; }
  th { color: #5a6b7a; font-weight: 600; font-size: 0.85rem; }
  tr:hover td { background: #f4f8fc; }
  .empty { color: #5a6b7a; font-style: italic; }
  a { color: #1a4a7a; }
</style>
</head>
<body>
<h1>Weergaven van het logboek (<?= esc_html($slug) ?>)</h1>
<p class="count"><?= count($lines) ?> weergave(n) geregistreerd.</p>
<?php if (empty($lines)): ?>
<p class="empty">Nog geen weergaven geregistreerd.</p>
<?php else: ?>
<table>
<thead><tr><th>Tijdstip</th><th>Gebruiker</th></tr></thead>
<tbody>
<?php foreach ($lines as $line): ?>
<?php
    // Each line is "YYYY-MM-DD HH:MM:SS username" (see logbook-index.php) -- split on the
    // first run of whitespace after the fixed-width timestamp, so a username containing a space
    // still comes through whole instead of getting truncated at its own first space.
    $timestamp = substr($line, 0, 19);
    $username = trim(substr($line, 19));
?>
<tr><td><?= htmlspecialchars($timestamp) ?></td><td><?= htmlspecialchars($username) ?></td></tr>
<?php endforeach; ?>
</tbody>
</table>
<?php endif; ?>
<p><a href="index.php">&larr; Terug naar het logboek</a></p>
</body>
</html>
