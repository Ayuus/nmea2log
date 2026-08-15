<?php
/**
 * Plugin Name: nmea2log Remarks
 * Description: Stores per-trip remarks for the nmea2log HTML logbook and exposes them over the
 * REST API. The logbook itself is a static HTML file uploaded separately (via SFTP, see the
 * nmea2000processor Python tool) -- not rendered by WordPress -- so this plugin only supplies
 * the storage + API, not any page/template. Also defines the "read_logboek" capability that the
 * page's own login gate (see little_endian-index.php) checks -- this site has ordinary customer
 * accounts too (WooCommerce), so "logged in at all" is not a safe stand-in for "may view the
 * logbook": every one of those customer accounts is logged in just as much as an actual crew
 * member would be.
 * Version: 1.3.0
 */

if (!defined('ABSPATH')) {
    exit;
}

define('NMEA2LOG_REMARKS_OPTION', 'nmea2log_remarks');
define('NMEA2LOG_VIEW_CAP', 'read_logboek');
define('NMEA2LOG_EDIT_CAP', 'edit_logboek_remarks');

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
