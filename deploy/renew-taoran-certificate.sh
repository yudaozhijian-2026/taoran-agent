#!/bin/sh
set -eu
# A deploy hook scoped to this certificate, not other projects' renewals.
if [ "${RENEWED_LINEAGE:-}" != /etc/letsencrypt/live/taoran.yudaozhijian.top ]; then
    exit 0
fi
if ! grep -q '<VirtualHost \*:443>' /etc/apache2/sites-available/taoran.yudaozhijian.top.conf; then
    exit 0
fi
/usr/sbin/apache2ctl configtest
/bin/systemctl reload apache2
