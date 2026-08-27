<?php
/**
 * Gatekeeper for the autopilot course-keeping analysis page. Deploy as www/little_endian/
 * autopilot.php. Same access level as the logbook itself (little_endian-index.php): any account
 * nmea2log_can_view() approves (Logbook Reader, Logbook Writer, or Administrator), not admin-only
 * like little_endian-views.php.
 *
 * The static report itself lives outside the web root (private/little_endian/autopilot-
 * analysis.html), same as logbook.html -- its URL alone is never enough to read it.
 */

require_once __DIR__ . '/../wp-load.php';

if (!is_user_logged_in()) {
    $current_url = (is_ssl() ? 'https://' : 'http://') . $_SERVER['HTTP_HOST'] . $_SERVER['REQUEST_URI'];
    wp_redirect(wp_login_url($current_url));
    exit;
}

if (!function_exists('nmea2log_can_view') || !nmea2log_can_view()) {
    http_response_code(403);
    echo 'Your account does not have access to this page.';
    exit;
}

$report_path = __DIR__ . '/../../private/little_endian/autopilot-analysis.html';
if (!file_exists($report_path)) {
    http_response_code(404);
    echo 'Report not uploaded yet.';
    exit;
}

header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
header('Pragma: no-cache');
header('Content-Type: text/html; charset=utf-8');
echo file_get_contents($report_path);
