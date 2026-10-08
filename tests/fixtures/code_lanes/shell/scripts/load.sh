#!/bin/bash
set -e
. ../lib/common.sh
psql -h db -c "SELECT count() FROM users"
sqlite3 app.db "SELECT name FROM sqlite_master"
curl -s https://example.org | jq .version
./notify.sh done
log "loaded"
