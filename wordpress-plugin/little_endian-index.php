<?php
/**
 * Gatekeeper for the nmea2log HTML logbook. Deploy as www/little_endian/index.php.
 *
 * The generated logbook.html itself is uploaded (via the nmea2000processor --upload feature) to
 * private/little_endian/logbook.html -- outside the web-served www/ directory, so its URL alone
 * is never enough to read it. This script bootstraps WordPress just far enough to check the
 * visitor's login state and view permission (nmea2log_can_view(), defined by the nmea2log-
 * remarks plugin -- reused here rather than re-checking the capability directly, so this always
 * agrees with what the REST API itself considers "may view", including its Administrator
 * fallback), and only then reads and serves the real file from that private location.
 *
 * Checks the capability, not just is_user_logged_in(): this site also has ordinary customer
 * accounts (WooCommerce), and every one of those is just as "logged in" as an actual crew member
 * -- being logged in at all is not the same question as being allowed to see this logbook.
 *
 * Also patches a live wp_rest nonce into the served copy (replacing the "%%WP_REST_NONCE%%"
 * placeholder the Python side embeds), since logbook.html itself is static and generated well
 * before -- and completely separately from -- any particular WordPress session, so it has no way
 * to embed a real one itself. The Remarks feature's save button needs this nonce (on top of the
 * login cookie the browser already sends automatically) to satisfy WordPress's CSRF check on
 * REST POST requests.
 */

require_once __DIR__ . '/../wp-load.php';

if (!is_user_logged_in()) {
    $current_url = (is_ssl() ? 'https://' : 'http://') . $_SERVER['HTTP_HOST'] . $_SERVER['REQUEST_URI'];
    wp_redirect(wp_login_url($current_url));
    exit;
}

// Logged in, but not necessarily as someone who may see this logbook (e.g. a webshop customer
// account) -- distinct from the not-logged-in-at-all case above, so this doesn't send an already
// -authenticated visitor back through the login page again for no reason.
if (!function_exists('nmea2log_can_view') || !nmea2log_can_view()) {
    http_response_code(403);
    echo 'Je account heeft geen toegang tot dit logboek.';
    exit;
}

$logbook_path = __DIR__ . '/../../private/little_endian/logbook.html';
if (!file_exists($logbook_path)) {
    http_response_code(404);
    echo 'Logboek nog niet geüpload.';
    exit;
}

$html = file_get_contents($logbook_path);
$html = str_replace('%%WP_REST_NONCE%%', wp_create_nonce('wp_rest'), $html);

header('Content-Type: text/html; charset=utf-8');
echo $html;
