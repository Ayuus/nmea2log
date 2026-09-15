<?php
/**
 * Gatekeeper for the nmea2log HTML logbook. Deploy once, anywhere under www/ (e.g.
 * www/logboek/index.php) -- since 1.6.0 this no longer needs to live at a URL matching any
 * particular boat's own slug: nmea2log_effective_slug() (see nmea2log-remarks.php) resolves
 * *which* boat's logbook to show from who's actually logged in, so one shared page serves every
 * boat on the site.
 *
 * The generated logbook.html itself is uploaded (via the nmea2000processor --upload feature) to
 * nmea2log_logbook_path() (see nmea2log-remarks.php) -- outside the web-served www/ directory, so
 * its URL alone is never enough to read it. This script bootstraps WordPress just far enough to
 * check the visitor's login state and view permission (nmea2log_can_view(), defined by the nmea2log-
 * remarks plugin -- reused here rather than re-checking the capability directly, so this always
 * agrees with what the REST API itself considers "may view", including its Administrator
 * fallback), and only then reads and serves the real file from that private location.
 *
 * Checks the capability, not just is_user_logged_in(): this site also has ordinary customer
 * accounts (WooCommerce), and every one of those is just as "logged in" as an actual crew member
 * -- being logged in at all is not the same question as being allowed to see this logbook.
 *
 * Every successful view (i.e. past the permission check below) is appended to views.log next to
 * logbook.html -- outside the web root, same as the logbook itself, so it's not publicly
 * browsable. A failed file write is never fatal to actually serving the logbook; this is a
 * bonus, not something the page depends on.
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

$logbook_path = nmea2log_logbook_path(nmea2log_effective_slug());
if (!file_exists($logbook_path)) {
    http_response_code(404);
    echo 'Logboek nog niet geüpload.';
    exit;
}

$views_log_path = dirname($logbook_path) . '/views.log';
$line = date('Y-m-d H:i:s') . ' ' . wp_get_current_user()->user_login . "\n";
@file_put_contents($views_log_path, $line, FILE_APPEND | LOCK_EX);

$html = file_get_contents($logbook_path);
$html = str_replace('%%WP_REST_NONCE%%', wp_create_nonce('wp_rest'), $html);

// The whole point of this page is to always show whatever was most recently uploaded -- a cached
// copy (e.g. Safari on iPhone, found in practice) can silently keep showing an old logbook until
// manually refreshed, which defeats that. No-store also covers the WP_REST_NONCE baked into this
// response, which would otherwise itself go stale if the page were cached.
header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
header('Pragma: no-cache');
header('Content-Type: text/html; charset=utf-8');
echo $html;
