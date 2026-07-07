#!/bin/bash

ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
python m3u8_downloader.py \
-H "User-Agent: $ua" \
"$@"

#--proxy http://127.0.0.1:8001 \


#  site | 
# 123av | wowstream2.cloud   | -H "Referer: https://surrit.store/" -H "Origin: https://surrit.store/  | no-proxy | https://cache-xx4.wowstream2.cloud/blah4/XEn0ezQAjwPK3U_oUqJFL9izm0xg38dbB+SHpQSajRNTmdZwwtlTGGs/qb/v.m3u8
# 7mmtv | streamsuperpro(sp) | "Referer: https://7mmtv.sx"                                            | no-proxy | "https://embedsozgfdiu0pzo1eloe9lwgacz1cq1.streamsuperpro.com/c569e32ee63459629c8f3dd16646917b/13/2/UxdwDF-Rh-m16ZbmmOos4A/UxdwDF-Rh-m16ZbmmOos4A.m3u8"

