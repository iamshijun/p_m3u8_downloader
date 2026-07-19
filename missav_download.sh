#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
python $SCRIPT_DIR/m3u8_downloader.py \
--proxy http://127.0.0.1:8001 \
-H "User-Agent: $ua" \
-H "Referer: https://missav.ai/ja" \
"$@"


#Example 1: 指定临时目录,keep-tmp暂时来说重要 不然会在完成时候将 目录移动到 ${output}_segments 目录中. no-merge:不执行合并
#./missav_download.sh -r 7 -t 20 \
#  --no-merge --tmp-dir  /Volumes/TS30041/video/sprd-1181 --keep-tmp \
#  https://surrit.com/4b3926a7-e65f-4f3f-9191-c5ed0c75a596/842x480/video.m3u8


#Example 2: --no-disk 不落地文件, 需要搭配上传使用 每个片段下载后立即使用上传接口 上传到网盘,-u 指定上到的目标目录, --upload-token 指定网盘api所需的access_token
#./missav_download.sh -r 7 -t 20 \ 
#  --no-disk -u /apps/movies/meyd-009 \
#  --upload-token="121.83a598e647f5ef9df166605461680aa0.YBl_i4JqoLwGDhjQPRCIp6KSS2hdDkONBP1Sriw.wKhTUA" \
#  https://surrit.com/fd46fd6c-bd2c-4562-ab57-d7159a7dc042/1280x720/video.m3u8
