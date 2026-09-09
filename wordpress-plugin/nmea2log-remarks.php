<?php
/**
 * Plugin Name: nmea2log Remarks
 * Description: Stores per-trip remarks for the nmea2log HTML logbook, and (since 1.4.0) receives
 * the logbook itself, over the REST API. Previously the logbook was uploaded separately via SFTP
 * (see the nmea2000processor Python tool's upload.py) -- moved here so publishing reuses the same
 * WordPress role/Application Password this plugin already needs for remarks, instead of a
 * separate SSH key living on every machine that syncs. This plugin still supplies no page/
 * template of its own -- the logbook stays a static HTML file, just written here now rather than
 * over SFTP. Also defines the "read_logboek" capability that the page's own login gate (see
 * little_endian-index.php) checks -- this site has ordinary customer accounts too (WooCommerce),
 * so "logged in at all" is not a safe stand-in for "may view the logbook": every one of those
 * customer accounts is logged in just as much as an actual crew member would be.
 * Version: 1.4.0
 */

if (!defined('ABSPATH')) {
    exit;
}

define('NMEA2LOG_REMARKS_OPTION', 'nmea2log_remarks');
define('NMEA2LOG_VIEW_CAP', 'read_logboek');
define('NMEA2LOG_EDIT_CAP', 'edit_logboek_remarks');
// One level above ABSPATH (the www/ webroot), outside it entirely -- same path upload.py's own
// SFTP upload has always targeted (private/little_endian/logbook.html), still served only via
// little_endian-index.php's own login-gated read, never directly reachable by URL.
define('NMEA2LOG_LOGBOOK_PATH', dirname(ABSPATH) . '/private/little_endian/logbook.html');

// Neither role reuses a built-in WordPress role (Contributor, Editor, Subscriber, ...): this
// site has pre-existing accounts using those for unrelated things (ordinary WooCommerce customer
// accounts, and separately Contributor is already in use for something else on this site) --
// granting a capability to a built-in role would silently hand logbook access to whoever already
// holds that role for a completely different reason. Two purpose-built roles instead, with no
// capability beyond what this plugin needs.
define('NMEA2LOG_EDITOR_ROLE', 'logboek_editor');
define('NMEA2LOG_READER_ROLE', 'reader');

// Self-healing: runs on every admin page load (not just plugin activation), so it also fixes an
// install that was already active before a given version of this block was added, without
// needing a deactivate/reactivate. Also self-corrects the display *name*, not just whether the
// role exists: add_role() is a no-op if the slug is already registered, so an earlier version's
// name (this went through a couple of iterations: "Logboek redacteur"/"Logboek lezer", then
// "Logbook Editor"/"Reader") would otherwise linger in the role list forever even after the code
// changed (found in practice) -- remove+recreate whenever the stored name doesn't match what
// this version wants. Safe as long as no real user has been assigned the role yet, which is the
// case for a setup still in progress; a user actually holding a renamed role would need
// reassigning afterwards, since removing a role doesn't do that automatically.
add_action('admin_init', function () {
    // Leftovers from earlier iterations of this plugin that no longer apply: the reader role
    // used to be its own slug ("logboek_viewer") before being re-slugged to "reader", and an
    // even earlier iteration granted this plugin's capabilities directly to WordPress's built-in
    // Contributor role (since reverted -- Contributor is already in use for something unrelated
    // on this site, see the module docstring). remove_role()/remove_cap() on something already
    // absent is a harmless no-op, so this is safe to always run, not just once.
    remove_role('logboek_viewer');
    $contributor = get_role('contributor');
    if ($contributor) {
        $contributor->remove_cap(NMEA2LOG_VIEW_CAP);
        $contributor->remove_cap(NMEA2LOG_EDIT_CAP);
    }

    foreach (
        [
            NMEA2LOG_READER_ROLE => ['Logbook Reader', ['read' => true, NMEA2LOG_VIEW_CAP => true]],
            NMEA2LOG_EDITOR_ROLE => [
                'Logbook Writer',
                ['read' => true, NMEA2LOG_VIEW_CAP => true, NMEA2LOG_EDIT_CAP => true],
            ],
        ] as $slug => [$name, $caps]
    ) {
        $roles = wp_roles();
        $current_name = $roles->role_names[$slug] ?? null;
        if ($current_name !== $name) {
            remove_role($slug);
            add_role($slug, $name, $caps);
        }
    }
});

register_uninstall_hook(__FILE__, 'nmea2log_remarks_uninstall');

function nmea2log_remarks_uninstall() {
    remove_role(NMEA2LOG_READER_ROLE);
    remove_role(NMEA2LOG_EDITOR_ROLE);
    delete_option(NMEA2LOG_REMARKS_OPTION);
}

function nmea2log_can_view(): bool {
    // manage_options (Administrator) as an explicit fallback alongside the capability itself:
    // REST API requests (and the login-gate script) don't run WordPress's admin_init hook, only
    // actual wp-admin page loads do, so an Administrator who hasn't loaded wp-admin since this
    // plugin was installed would otherwise still be denied even though the self-heal above would
    // eventually have granted the same capability anyway.
    return current_user_can(NMEA2LOG_VIEW_CAP) || current_user_can('manage_options');
}

function nmea2log_can_edit(): bool {
    return current_user_can(NMEA2LOG_EDIT_CAP) || current_user_can('manage_options');
}

add_action('rest_api_init', function () {
    register_rest_route('nmea2log/v1', '/remarks', [
        [
            'methods' => 'GET',
            'callback' => 'nmea2log_remarks_get',
            'permission_callback' => 'nmea2log_can_view',
        ],
        [
            'methods' => 'POST',
            'callback' => 'nmea2log_remarks_set',
            'permission_callback' => 'nmea2log_can_edit',
            'args' => [
                'trip_uid' => ['required' => true, 'type' => 'string'],
                'text' => ['required' => true, 'type' => 'string'],
            ],
        ],
    ]);
    register_rest_route('nmea2log/v1', '/logbook', [
        'methods' => 'POST',
        'callback' => 'nmea2log_logbook_upload',
        'permission_callback' => 'nmea2log_can_edit',
    ]);
});

function nmea2log_remarks_all(): array {
    $remarks = get_option(NMEA2LOG_REMARKS_OPTION, []);
    return is_array($remarks) ? $remarks : [];
}

function nmea2log_remarks_get(WP_REST_Request $request) {
    return rest_ensure_response([
        // Cast to an object so an empty result still encodes as JSON "{}", not "[]" -- a PHP
        // array with no keys and a PHP array with string keys both need to look the same shape
        // to the JS side, which always expects to index it by trip_uid.
        'remarks' => (object) nmea2log_remarks_all(),
        // Lets the page show only a "Sluiten" (Close) button, no "Opslaan" (Save), for a visitor
        // who can view but not edit -- rather than showing Save and only discovering it doesn't
        // work after clicking it. Uses the exact same check as the POST permission_callback
        // above, so this flag is never wrong about what a save attempt would actually do.
        'can_edit' => nmea2log_can_edit(),
    ]);
}

function nmea2log_remarks_set(WP_REST_Request $request) {
    $trip_uid = sanitize_text_field($request->get_param('trip_uid'));
    $text = sanitize_textarea_field($request->get_param('text'));
    if ($trip_uid === '') {
        return new WP_Error('missing_trip_uid', 'trip_uid is required', ['status' => 400]);
    }

    $remarks = nmea2log_remarks_all();
    if ($text === '') {
        unset($remarks[$trip_uid]); // an emptied remark is removed, not stored as ""
    } else {
        $remarks[$trip_uid] = $text;
    }
    update_option(NMEA2LOG_REMARKS_OPTION, $remarks);

    return rest_ensure_response(['ok' => true]);
}

/** Receives the whole built HTML logbook and writes it to NMEA2LOG_LOGBOOK_PATH -- the REST
 * replacement for upload.py's own SFTP upload_file(). The raw request body is the HTML itself
 * (no JSON envelope: at several hundred KB, wrapping it in a JSON string would only cost
 * escaping overhead for no benefit), so this reads $request->get_body() directly rather than a
 * named param.
 *
 * Same atomic write-then-rename as upload_file()'s own SFTP put+rename: a plain in-place write
 * isn't atomic, and little_endian-index.php (the page's own login-gated read) reads this file
 * fresh on every single request -- a visitor loading the page mid-write could otherwise see a
 * truncated file. PHP's rename() is atomic on the same filesystem, so a concurrent read always
 * gets either the complete old file or the complete new one. */
function nmea2log_logbook_upload(WP_REST_Request $request) {
    $html = $request->get_body();
    // A real logbook.html is hundreds of KB -- anything this small is almost certainly a
    // truncated or otherwise broken upload, not a genuinely tiny (but valid) logbook. Rejected
    // outright rather than risk silently overwriting the live, working file with something
    // broken.
    if (strlen($html) < 1000) {
        return new WP_Error(
            'logbook_too_small', 'Uploaded content looks incomplete (under 1000 bytes)', ['status' => 400]
        );
    }

    $target = NMEA2LOG_LOGBOOK_PATH;
    $dir = dirname($target);
    if (!is_dir($dir) && !wp_mkdir_p($dir)) {
        return new WP_Error('logbook_dir_failed', 'Could not create the target directory', ['status' => 500]);
    }

    $tmp = $target . '.tmp-upload';
    if (file_put_contents($tmp, $html) === false) {
        return new WP_Error('logbook_write_failed', 'Could not write the uploaded file', ['status' => 500]);
    }
    if (!rename($tmp, $target)) {
        @unlink($tmp);
        return new WP_Error('logbook_rename_failed', 'Could not finalize the uploaded file', ['status' => 500]);
    }

    return rest_ensure_response(['ok' => true, 'bytes' => strlen($html)]);
}
