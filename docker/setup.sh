#!/bin/sh
# Runs once inside the wp-cli container: installs WordPress, Yoast, categories and the API user.
set -e
cd /var/www/html

echo "waiting for WordPress files and database..."
until [ -f wp-config.php ] && wp db check >/dev/null 2>&1; do sleep 2; done

if ! wp core is-installed 2>/dev/null; then
  wp core install --url=http://localhost:8080 --title="Local WP" \
    --admin_user=admin --admin_password=admin --admin_email=admin@example.com --skip-email
  wp rewrite structure '/%postname%/' --hard
  wp plugin install wordpress-seo --activate
  wp term update category 1 --name=Blog --slug=blog
  wp term create category Guides --slug=guides
  wp term create category News --slug=news
  wp user create automation-bot bot@example.com --role=editor --user_pass="$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi

APP_PASS="$(wp user application-password create automation-bot "docker-$(date +%s)" --porcelain)"

cat <<EOF

WordPress is ready at http://localhost:8080 (admin / admin)

  export WP_URL=http://localhost:8080
  export WP_USER=automation-bot
  export WP_APP_PASSWORD="$APP_PASS"
  python wp_bulk_publish.py articles.csv --report report.json

EOF
