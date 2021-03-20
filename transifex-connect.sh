#!/usr/bin/expect -f
spawn sh transifex-run.sh
expect "Enter your api token: "
send -- $TRANSIFEX_API
