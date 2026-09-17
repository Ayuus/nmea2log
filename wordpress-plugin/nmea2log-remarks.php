<?php
/**
 * Plugin Name: nmea2log Remarks
 * Description: Stores per-trip remarks for the nmea2log HTML logbook, and (since 1.4.0) receives
 * the logbook itself, over the REST API. Previously the logbook was uploaded separately via SFTP
 * (see the nmea2log Python tool's upload.py) -- moved here so publishing reuses the same
 * WordPress role/Application Password this plugin already needs for remarks, instead of a
 * separate SSH key living on every machine that syncs. This plugin still supplies no page/
 * template of its own -- the logbook stays a static HTML file, just written here now rather than
 * over SFTP. Also defines the "read_logboek" capability that the page's own login gate (see
 * logbook-index.php) checks -- this site has ordinary customer accounts too (WooCommerce),
 * so "logged in at all" is not a safe stand-in for "may view the logbook": every one of those
 * customer accounts is logged in just as much as an actual crew member would be.
 *
 * Since 1.6.0, multiple boats can share one WordPress site: which logbook a person sees (or, for
 * a Writer, edits/uploads) is resolved from *who they are* (nmea2log_slug_for_user(), a per-user
 * "boat" field on their profile, see nmea2log_render_boat_field()), not from the URL a
 * logbook-index.php/logbook-views.php deployment happens to be reached at -- any deployment shows
 * the right boat to the right visitor regardless. Even so, since 1.7.0 the installation
 * instructions have every boat get its own deployment anyway, at a fixed, predictable location
 * derived from its own slug alone (www/<slug>/, see nmea2log_logbook_url_for_user()) -- purely so
 * each boat gets its own memorable URL and so the invite email below needs no per-boat URL
 * configured anywhere. There's deliberately no site-wide settings page at all: the only per-boat
 * knob is that one "boat" profile field, and nmea2log_default_slug()'s hardcoded 'logboek' (or
 * NMEA2LOG_SLUG in wp-config.php) covers the rare case of a user without a personal one. Every
 * Writer also gets their own "Mijn lezers" screen (nmea2log_readers_render()) to invite/remove
 * readers for their own boat specifically, without needing the site Administrator to create every
 * account by hand: the Writer picks the new reader's username there and the account is created
 * right away, with only the password left for the reader to set themselves, via WordPress's own
 * password-reset link (get_password_reset_key(), the same mechanism as "lost your password" on
 * the login page) -- no separate invite page or token of this plugin's own needed for that.
 * Version: 1.7.0
 */

if (!defined('ABSPATH')) {
    exit;
}

define('NMEA2LOG_REMARKS_OPTION', 'nmea2log_remarks');
define('NMEA2LOG_SLUG_OPTION', 'nmea2log_slug');
define('NMEA2LOG_BOAT_META_KEY', 'nmea2log_boat_slug');
define('NMEA2LOG_BOAT_NAME_META_KEY', 'nmea2log_boat_name');
define('NMEA2LOG_VIEW_CAP', 'read_logboek');
define('NMEA2LOG_EDIT_CAP', 'edit_logboek_remarks');
define('NMEA2LOG_READERS_PAGE_SLUG', 'nmea2log-readers');

// The site-wide fallback boat -- used for anyone with no personal boat assignment (see
// nmea2log_slug_for_user() below), e.g. a single-boat site where nobody bothers assigning it per
// user, or an Administrator browsing without a ?boot= override. Deliberately not hardcoded as a
// real boat name in this (public) plugin file: a NMEA2LOG_SLUG constant in wp-config.php (never
// committed either) if set, else the generic 'logboek'.
function nmea2log_default_slug(): string {
    if (defined('NMEA2LOG_SLUG')) {
        return sanitize_title(NMEA2LOG_SLUG);
    }
    return 'logboek';
}

// null, not '', when nothing is assigned -- lets callers tell "no personal boat" apart from a
// (theoretically) empty-string slug, which sanitize_title() would never actually produce anyway.
function nmea2log_slug_for_user(int $user_id): ?string {
    $slug = get_user_meta($user_id, NMEA2LOG_BOAT_META_KEY, true);
    return $slug !== '' ? sanitize_title($slug) : null;
}

// The boat to use for the currently logged-in user specifically -- their own assignment first,
// the site default otherwise (e.g. an Administrator who hasn't been assigned one personally).
function nmea2log_slug_for_current_user(): string {
    $user_id = get_current_user_id();
    if ($user_id) {
        $own = nmea2log_slug_for_user($user_id);
        if ($own !== null) {
            return $own;
        }
    }
    return nmea2log_default_slug();
}

// Like nmea2log_slug_for_current_user(), but an Administrator may pass ?boot=<slug> to browse a
// different boat's logbook page -- e.g. to check on one whose Writer hasn't logged in yet, or
// just to look something up. Never available to a Writer/Reader: they only ever see their own
// boat, by design (this is what makes "Mijn lezers" below safe to scope by "the requester's own
// boat" without a Writer being able to lie about which one that is). Only affects which *logbook*
// is shown -- the Remarks REST calls a served page's own JS makes still resolve via
// nmea2log_slug_for_current_user() (see nmea2log_remarks_get()/_set()), since those are separate
// requests that don't carry this page's own ?boot= along; harmless in practice, since another
// boat's trip_uids essentially never collide with the one being viewed, so this just shows no
// remarks rather than the wrong ones.
function nmea2log_effective_slug(): string {
    if (current_user_can('manage_options') && isset($_GET['boot']) && $_GET['boot'] !== '') {
        return sanitize_title(wp_unslash($_GET['boot']));
    }
    return nmea2log_slug_for_current_user();
}

// Derived from the boat slug alone, per the installation instructions' convention: every boat's
// logbook-index.php lives at www/<slug>/. Keyed off the *inviting* Writer's own user id, since
// that's whose boat the invite email is actually about; falls back to the site default slug the
// same way nmea2log_slug_for_current_user() does, in the unlikely case a Writer sends an invite
// before having their own boat assigned.
function nmea2log_logbook_url_for_user(int $user_id): string {
    return home_url('/' . (nmea2log_slug_for_user($user_id) ?? nmea2log_default_slug()) . '/');
}

// A human-friendly name for the invite email's subject/body -- normally just the "Bootnaam" the
// Writer typed on their own profile (see nmea2log_render_boat_field()), which is also what the
// slug/URL is derived *from*. The title-cased-slug fallback below only covers a user who somehow
// has a boat slug (NMEA2LOG_BOAT_META_KEY) without the name it should have come from -- data from
// before this field existed, or the site-wide default boat, which has no name of its own to type.
function nmea2log_boat_display_name_for_user(int $user_id): string {
    $explicit = get_user_meta($user_id, NMEA2LOG_BOAT_NAME_META_KEY, true);
    if ($explicit !== '') {
        return $explicit;
    }
    $slug = nmea2log_slug_for_user($user_id) ?? nmea2log_default_slug();
    return ucwords(str_replace(['-', '_'], ' ', $slug));
}

// One level above ABSPATH (the www/ webroot), outside it entirely -- same path upload.py's own
// SFTP upload has always targeted, still served only via logbook-index.php's own login-gated
// read, never directly reachable by URL.
function nmea2log_logbook_path(string $slug): string {
    return dirname(ABSPATH) . '/private/' . $slug . '/logbook.html';
}


// Per-user boat assignment, on each user's own Edit Profile screen (Gebruikers > naam) --
// Administrator-only to view/edit, same trust level as assigning roles (which is also
// Administrator-only in core WordPress): a Writer's own boat is something *they* get assigned,
// not something they pick for themselves.
add_action('show_user_profile', 'nmea2log_render_boat_field');
add_action('edit_user_profile', 'nmea2log_render_boat_field');

function nmea2log_render_boat_field(WP_User $user): void {
    if (!current_user_can('manage_options')) {
        return;
    }
    $current_name = get_user_meta($user->ID, NMEA2LOG_BOAT_NAME_META_KEY, true);
    $current_slug = get_user_meta($user->ID, NMEA2LOG_BOAT_META_KEY, true);
    ?>
    <h2>nmea2log</h2>
    <table class="form-table">
        <tr>
            <th><label for="nmea2log_boat_name">Bootnaam</label></th>
            <td>
                <input
                    type="text" name="nmea2log_boat_name" id="nmea2log_boat_name"
                    value="<?= esc_attr($current_name) ?>" class="regular-text"
                    placeholder="(standaardboot)"
                >
                <p class="description">
                    Welk logboek deze gebruiker ziet (en, als Logbook Writer, bewerkt/uploadt en
                    voor lezers uitnodigt). Leeg = <code>logboek</code> (of de waarde van
                    <code>NMEA2LOG_SLUG</code> in wp-config.php, indien ingesteld). Ook gebruikt in
                    onderwerp/tekst van de uitnodigingsmail -- typ de naam zoals die er moet
                    uitzien (bv. <code>WYSIWYG</code>), de URL-slug wordt er automatisch uit
                    afgeleid.
                    <?php if ($current_slug !== ''): ?>
                        Huidige URL: <code>www/<?= esc_html($current_slug) ?>/</code> (zie de
                        installatie-instructies).
                    <?php endif; ?>
                </p>
            </td>
        </tr>
    </table>
    <?php
}

add_action('personal_options_update', 'nmea2log_save_boat_field');
add_action('edit_user_profile_update', 'nmea2log_save_boat_field');

function nmea2log_save_boat_field(int $user_id): void {
    if (!current_user_can('manage_options') || !isset($_POST['nmea2log_boat_name'])) {
        return;
    }
    $name = sanitize_text_field(wp_unslash($_POST['nmea2log_boat_name']));
    if ($name === '') {
        delete_user_meta($user_id, NMEA2LOG_BOAT_NAME_META_KEY);
        delete_user_meta($user_id, NMEA2LOG_BOAT_META_KEY);
    } else {
        update_user_meta($user_id, NMEA2LOG_BOAT_NAME_META_KEY, $name);
        update_user_meta($user_id, NMEA2LOG_BOAT_META_KEY, nmea2log_slug_from_boat_name($name));
    }
}

// sanitize_title() (the same slugifier WordPress uses for post slugs) does the real work here --
// lowercasing, stripping accents/disallowed characters -- but its output is hyphen-separated,
// while boat slugs on this site have always been underscore-separated (existing deployments,
// already-shared reader URLs). Swapping the separator after the fact keeps a boat's derived slug
// from changing (and breaking those URLs) purely because this field replaced typing it directly.
function nmea2log_slug_from_boat_name(string $name): string {
    return str_replace('-', '_', sanitize_title($name));
}

// "Mijn lezers" -- lets a Logbook Writer invite/remove readers for their *own* boat directly,
// without asking the site Administrator to create every account by hand (found in practice to be
// the real bottleneck once more than one boat/owner is involved). Gated on NMEA2LOG_EDIT_CAP, not
// a broader WordPress capability like create_users: a Writer can only ever affect accounts that
// are (a) not already an Editor/Administrator and (b) -- for removal -- already tagged with their
// own boat, both enforced in nmea2log_readers_render() itself, so this never needs to expose
// WordPress's own, unscoped "add new user" screen to a Writer at all.
add_action('admin_menu', function () {
    add_menu_page(
        'Mijn lezers', 'Mijn lezers', NMEA2LOG_EDIT_CAP, NMEA2LOG_READERS_PAGE_SLUG,
        'nmea2log_readers_render', 'dashicons-groups'
    );
});

// Scoped to exactly the one wp_mail() call below (added right before, removed right after) --
// never left on globally, so this never affects any other plugin's or WordPress core's own mail.
function nmea2log_html_mail_content_type(): string {
    return 'text/html';
}

function nmea2log_invite_email_html(string $boat_name, string $username, string $reset_link, string $logbook_url): string {
    return '<p>Je bent uitgenodigd voor het logboek van <strong>' . esc_html($boat_name) . '</strong>. '
        . 'Je gebruikersnaam is <strong>' . esc_html($username) . '</strong>.</p>'
        . '<p>Stel via onderstaande link je eigen wachtwoord in -- je komt na het instellen direct '
        . 'in het logboek zelf:</p>'
        . '<p><a href="' . esc_url($reset_link) . '">Wachtwoord instellen</a></p>'
        . '<p>Het logboek staat ook rechtstreeks op:<br>'
        . '<a href="' . esc_url($logbook_url) . '">' . esc_html($logbook_url) . '</a></p>';
}

function nmea2log_readers_render(): void {
    if (!nmea2log_can_edit()) {
        wp_die('Geen toegang.');
    }
    $my_slug = nmea2log_slug_for_current_user();
    $message = '';

    if (
        isset($_POST['nmea2log_add_reader_nonce'])
        && wp_verify_nonce($_POST['nmea2log_add_reader_nonce'], 'nmea2log_add_reader')
    ) {
        $email = sanitize_email(wp_unslash($_POST['email'] ?? ''));
        if ($email === '' || !is_email($email)) {
            $message = '<div class="notice notice-error"><p>Ongeldig e-mailadres.</p></div>';
        } else {
            $existing = get_user_by('email', $email);
            if ($existing) {
                // add_role(), not set_role(): this site also has plain WooCommerce customer
                // accounts (see the module docstring) -- set_role() would silently replace that
                // role instead of adding Reader alongside it. Blocked only for an existing
                // Editor/Administrator, so this can never quietly change what either of those can
                // already do.
                if (in_array(NMEA2LOG_EDITOR_ROLE, $existing->roles, true) || $existing->has_cap('manage_options')) {
                    $message = '<div class="notice notice-error"><p>Dit account heeft al een andere nmea2log-rol, niet aangepast.</p></div>';
                } else {
                    $existing->add_role(NMEA2LOG_READER_ROLE);
                    update_user_meta($existing->ID, NMEA2LOG_BOAT_META_KEY, $my_slug);
                    $message = '<div class="notice notice-success"><p>' . esc_html($email) . ' is nu lezer van dit logboek.</p></div>';
                }
            } else {
                // No account yet, unlike the branch above -- the Writer's own chosen username is
                // final here (no self-service "pick your own" page anymore, see the module
                // docstring for why that's fine for this use case), so the account itself is
                // created right away; only the *password* is left to the reader, via the same
                // reset-password mechanism/email WordPress's own "lost your password" uses (see
                // get_password_reset_key()) -- no separate invite page or token of this plugin's
                // own needed for that part at all.
                $username = sanitize_user(wp_unslash($_POST['username'] ?? ''), true);
                if ($username === '' || !validate_username($username)) {
                    $message = '<div class="notice notice-error"><p>Ongeldige gebruikersnaam (alleen letters, cijfers en @ . - _ zijn toegestaan).</p></div>';
                } elseif (username_exists($username)) {
                    $message = '<div class="notice notice-error"><p>Deze gebruikersnaam is al in gebruik -- kies een andere.</p></div>';
                } else {
                    $user_id = wp_insert_user([
                        'user_login' => $username,
                        'user_email' => $email,
                        'user_pass' => wp_generate_password(32), // never used as-is -- overwritten via the reset link below
                        'role' => NMEA2LOG_READER_ROLE,
                    ]);
                    if (is_wp_error($user_id)) {
                        $message = '<div class="notice notice-error"><p>' . esc_html($user_id->get_error_message()) . '</p></div>';
                    } else {
                        update_user_meta($user_id, NMEA2LOG_BOAT_META_KEY, $my_slug);
                        $reset_key = get_password_reset_key(get_userdata($user_id));
                        $reset_link = network_site_url(
                            'wp-login.php?action=rp&key=' . $reset_key . '&login=' . rawurlencode($username),
                            'login'
                        );
                        $logbook_url = nmea2log_logbook_url_for_user(get_current_user_id());
                        $boat_name = nmea2log_boat_display_name_for_user(get_current_user_id());
                        $subject = 'Uitnodiging voor het logboek van ' . $boat_name;
                        $body = nmea2log_invite_email_html($boat_name, $username, $reset_link, $logbook_url);
                        add_filter('wp_mail_content_type', 'nmea2log_html_mail_content_type');
                        $sent = wp_mail($email, $subject, $body);
                        remove_filter('wp_mail_content_type', 'nmea2log_html_mail_content_type');
                        if ($sent) {
                            $message = '<div class="notice notice-success"><p>' . esc_html($email) . ' uitgenodigd (mail met wachtwoord-link verstuurd).</p></div>';
                        } else {
                            $message = '<div class="notice notice-error"><p>Account is aangemaakt, maar versturen van de uitnodigingsmail is mislukt -- stuur handmatig een wachtwoord-reset (Gebruikers &gt; ' . esc_html($username) . ').</p></div>';
                        }
                    }
                }
            }
        }
    }

    if (
        isset($_POST['nmea2log_remove_reader_nonce'])
        && wp_verify_nonce($_POST['nmea2log_remove_reader_nonce'], 'nmea2log_remove_reader')
    ) {
        $user_id = (int) ($_POST['user_id'] ?? 0);
        $target = get_userdata($user_id);
        // Scoped to this Writer's own boat *and* the Reader role specifically: can't be used to
        // touch an Editor/Administrator, or a Reader who belongs to a different boat, even by
        // guessing/tampering with the user_id in the form post.
        if (
            $target
            && in_array(NMEA2LOG_READER_ROLE, $target->roles, true)
            && get_user_meta($user_id, NMEA2LOG_BOAT_META_KEY, true) === $my_slug
        ) {
            delete_user_meta($user_id, NMEA2LOG_BOAT_META_KEY);
            $target->remove_role(NMEA2LOG_READER_ROLE); // not set_role(''): leaves e.g. a WooCommerce customer role intact
            $message = '<div class="notice notice-success"><p>Lezer verwijderd.</p></div>';
        }
    }

    $readers = get_users([
        'role' => NMEA2LOG_READER_ROLE,
        'meta_key' => NMEA2LOG_BOAT_META_KEY,
        'meta_value' => $my_slug,
    ]);
    ?>
    <div class="wrap">
        <h1>Mijn lezers</h1>
        <?php echo $message; ?>
        <p>Lezers van jouw logboek (<code><?= esc_html($my_slug) ?></code>):</p>
        <table class="widefat striped">
            <thead><tr><th>E-mail</th><th></th></tr></thead>
            <tbody>
            <?php if (empty($readers)): ?>
                <tr><td colspan="2"><em>Nog geen lezers.</em></td></tr>
            <?php endif; ?>
            <?php foreach ($readers as $reader): ?>
                <tr>
                    <td><?= esc_html($reader->user_email) ?></td>
                    <td>
                        <form method="post" style="display:inline">
                            <?php wp_nonce_field('nmea2log_remove_reader', 'nmea2log_remove_reader_nonce'); ?>
                            <input type="hidden" name="user_id" value="<?= esc_attr($reader->ID) ?>">
                            <button
                                type="submit" class="button-link-delete"
                                onclick="return confirm('Lezer verwijderen?')"
                            >Verwijderen</button>
                        </form>
                    </td>
                </tr>
            <?php endforeach; ?>
            </tbody>
        </table>

        <h2>Lezer toevoegen</h2>
        <form method="post">
            <?php wp_nonce_field('nmea2log_add_reader', 'nmea2log_add_reader_nonce'); ?>
            <input type="email" name="email" placeholder="naam@voorbeeld.nl" required class="regular-text">
            <input type="text" name="username" placeholder="Gebruikersnaam" required class="regular-text">
            <?php submit_button('Uitnodigen', 'primary', 'submit', false); ?>
        </form>
        <p class="description">
            De gebruikersnaam staat hierna vast -- overleg 'm van tevoren met de nieuwe lezer als
            die daar een voorkeur voor heeft. De lezer stelt alleen zelf nog het wachtwoord in, via
            de link in de uitnodigingsmail.
        </p>
    </div>
    <?php
}

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

// One-time migration for a site that already had remarks saved before 1.6.0's multi-boat support:
// the option used to be a flat trip_uid => text map (one boat, implicitly), now it's boat =>
// trip_uid => text (see nmea2log_remarks_all_for_slug()) -- run on every request (not just
// admin_init, which a REST-only visitor loading the logbook page never triggers), but guarded by
// its own "already done" flag so the actual check only ever runs once in practice.
add_action('init', 'nmea2log_maybe_migrate_remarks');

function nmea2log_maybe_migrate_remarks(): void {
    if (get_option('nmea2log_remarks_migrated_v2', false)) {
        return;
    }
    $all = get_option(NMEA2LOG_REMARKS_OPTION, []);
    if (is_array($all) && !empty($all) && !is_array(reset($all))) {
        // Every remark saved so far belonged to the one boat this site had before multi-boat
        // support existed -- filed under today's own default slug rather than silently orphaned.
        update_option(NMEA2LOG_REMARKS_OPTION, [nmea2log_default_slug() => $all]);
    }
    update_option('nmea2log_remarks_migrated_v2', true);
}

register_uninstall_hook(__FILE__, 'nmea2log_remarks_uninstall');

function nmea2log_remarks_uninstall() {
    remove_role(NMEA2LOG_READER_ROLE);
    remove_role(NMEA2LOG_EDITOR_ROLE);
    delete_option(NMEA2LOG_REMARKS_OPTION);
    delete_option(NMEA2LOG_SLUG_OPTION);
    delete_option('nmea2log_remarks_migrated_v2');
    delete_metadata('user', 0, NMEA2LOG_BOAT_META_KEY, '', true); // true: every user, not just id 0
    delete_metadata('user', 0, NMEA2LOG_BOAT_NAME_META_KEY, '', true);
    delete_metadata('user', 0, 'nmea2log_boat_logbook_url', '', true); // cleanup for sites that briefly had the 1.7.0-dev per-boat override fields
    delete_metadata('user', 0, 'nmea2log_boat_invite_accept_url', '', true);
}

// Send a Reader straight to their own boat's logbook after logging in, instead of the (for them,
// empty/useless) wp-admin dashboard -- covers both the first login right after setting a password
// via the invite email, and every login after that. Deliberately scoped to the Reader role only:
// a Logbook Writer still needs wp-admin itself, for "Mijn lezers" and their Application Password.
add_filter('login_redirect', function (string $redirect_to, string $requested_redirect_to, $user) {
    if (
        $user instanceof WP_User
        && in_array(NMEA2LOG_READER_ROLE, $user->roles, true)
        && !$user->has_cap('manage_options')
    ) {
        return nmea2log_logbook_url_for_user($user->ID);
    }
    return $redirect_to;
}, 10, 3);

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

// Remarks are namespaced by boat (nmea2log_remarks_all_for_slug()'s own $slug param) -- multiple
// boats share this one option, keyed by slug, rather than one flat trip_uid => text map: without
// that, every boat's Reader/Writer would see (via the plain REST GET) every *other* boat's
// remarks mixed into the same response, not just their own.
function nmea2log_remarks_all_for_slug(string $slug): array {
    $all = get_option(NMEA2LOG_REMARKS_OPTION, []);
    if (!is_array($all) || !isset($all[$slug]) || !is_array($all[$slug])) {
        return [];
    }
    return $all[$slug];
}

function nmea2log_remarks_get(WP_REST_Request $request) {
    return rest_ensure_response([
        // Cast to an object so an empty result still encodes as JSON "{}", not "[]" -- a PHP
        // array with no keys and a PHP array with string keys both need to look the same shape
        // to the JS side, which always expects to index it by trip_uid.
        'remarks' => (object) nmea2log_remarks_all_for_slug(nmea2log_slug_for_current_user()),
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

    $slug = nmea2log_slug_for_current_user();
    $all = get_option(NMEA2LOG_REMARKS_OPTION, []);
    if (!is_array($all)) {
        $all = [];
    }
    $boat_remarks = isset($all[$slug]) && is_array($all[$slug]) ? $all[$slug] : [];
    if ($text === '') {
        unset($boat_remarks[$trip_uid]); // an emptied remark is removed, not stored as ""
    } else {
        $boat_remarks[$trip_uid] = $text;
    }
    $all[$slug] = $boat_remarks;
    update_option(NMEA2LOG_REMARKS_OPTION, $all);

    return rest_ensure_response(['ok' => true]);
}

/** Receives the whole built HTML logbook and writes it to this uploading user's own boat path
 * (nmea2log_slug_for_current_user() -- *not* nmea2log_effective_slug(): this is an unattended API
 * call, not a page a human is browsing with a ?boot= override in the address bar, so it always
 * writes to whichever boat the authenticated account itself is assigned to) -- the REST
 * replacement for upload.py's own SFTP upload_file(). The raw request body is the HTML itself
 * (no JSON envelope: at several hundred KB, wrapping it in a JSON string would only cost
 * escaping overhead for no benefit), so this reads $request->get_body() directly rather than a
 * named param.
 *
 * Same atomic write-then-rename as upload_file()'s own SFTP put+rename: a plain in-place write
 * isn't atomic, and logbook-index.php (the page's own login-gated read) reads this file
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

    $target = nmea2log_logbook_path(nmea2log_slug_for_current_user());
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
