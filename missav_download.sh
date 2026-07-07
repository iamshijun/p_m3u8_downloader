#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
python $SCRIPT_DIR/m3u8_downloader.py \
--proxy http://127.0.0.1:8001 \
-H "User-Agent: $ua" \
-H "Referer: https://missav.ai/ja" \
"$@"

