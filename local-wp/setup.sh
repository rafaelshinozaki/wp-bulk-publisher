#!/usr/bin/env bash
# Spins up a real WordPress locally without Docker or MySQL:
# PHP built-in server + SQLite (official sqlite-database-integration plugin) + WP-CLI.
#
# Requirements: git, curl, PHP 8.1+ with pdo_sqlite.
# Usage: ./local-wp/setup.sh [install_dir]   (default: ./.local-wp)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIR="${1:-$REPO_DIR/.local-wp}"
PORT="${PORT:-8080}"
WP_VERSION="${WP_VERSION:-6.8.3}"
SQLITE_VERSION="${SQLITE_VERSION:-v2.2.2}"
WPCLI_VERSION="${WPCLI_VERSION:-2.12.0}"

mkdir -p "$DIR"
cd "$DIR"

[ -d wordpress ] || git clone -q --depth 1 --branch "$WP_VERSION" https://github.com/WordPress/WordPress.git wordpress
[ -f wp-cli.phar ] || curl -sSL -o wp-cli.phar \
  "https://github.com/wp-cli/wp-cli/releases/download/v$WPCLI_VERSION/wp-cli-$WPCLI_VERSION.phar"

WP="php $DIR/wp-cli.phar --allow-root --path=$DIR/wordpress"
PLUGIN_DIR="$DIR/wordpress/wp-content/plugins/sqlite-database-integration"

if [ ! -d "$PLUGIN_DIR" ]; then
  git -c advice.detachedHead=false clone -q --depth 1 --branch "$SQLITE_VERSION" \
    https://github.com/WordPress/sqlite-database-integration.git "$PLUGIN_DIR"
fi
sed "s#{SQLITE_IMPLEMENTATION_FOLDER_PATH}#$PLUGIN_DIR#; s#{SQLITE_PLUGIN}#sqlite-database-integration/load.php#" \
  "$PLUGIN_DIR/db.copy" > "$DIR/wordpress/wp-content/db.php"

if ! $WP core is-installed 2>/dev/null; then
  # WP_ENVIRONMENT_TYPE=local lets Application Passwords work over plain HTTP.
  $WP config create --dbname=wp --dbuser=x --dbpass=x --dbhost=localhost --skip-check --force \
    --extra-php <<<"define('WP_ENVIRONMENT_TYPE', 'local');"
  $WP core install --url="http://127.0.0.1:$PORT" --title="Local WP" \
    --admin_user=admin --admin_password=admin --admin_email=admin@example.com --skip-email
  $WP option update home "http://127.0.0.1:$PORT"
  $WP option update siteurl "http://127.0.0.1:$PORT"
  $WP rewrite structure '/%postname%/'

  $WP term update category 1 --name=Blog --slug=blog
  $WP term create category Guides --slug=guides
  $WP term create category News --slug=news
  $WP user create automation-bot bot@example.com --role=editor --user_pass="$(openssl rand -hex 12)"

  # Yoast comes from wordpress.org; skipped if it can't be downloaded.
  # The mu-plugin below is what makes the meta keys writable either way.
  $WP plugin install wordpress-seo --activate || echo "warning: could not install Yoast, continuing without it"
fi

mkdir -p "$DIR/wordpress/wp-content/mu-plugins"
cp "$REPO_DIR/yoast-rest-meta.php" "$DIR/wordpress/wp-content/mu-plugins/"

cat > "$DIR/router.php" <<'PHP'
<?php
// Router for PHP's built-in server: serve static files, send everything else to WordPress.
$root = __DIR__ . '/wordpress';
$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
if ($path !== '/' && is_file($root . $path)) {
    return false;
}
$_SERVER['SCRIPT_NAME'] = '/index.php';
require $root . '/index.php';
PHP

APP_PASS="$($WP user application-password create automation-bot "local-$(date +%s)" --porcelain)"

cat <<EOF

WordPress is ready. Start the server with:

  php -S 127.0.0.1:$PORT -t "$DIR/wordpress" "$DIR/router.php"

Admin: http://127.0.0.1:$PORT/wp-admin  (admin / admin)

Then, in another terminal:

  export WP_URL=http://127.0.0.1:$PORT
  export WP_USER=automation-bot
  export WP_APP_PASSWORD="$APP_PASS"
  python wp_bulk_publish.py articles.csv --report report.json

EOF
