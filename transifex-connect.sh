#!/usr/bin/expect -f
spawn ./transifex-run.sh
expect "Enter your api token: "
send -- $TRANSIFEX_API
