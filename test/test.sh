#!/bin/sh

# start a webserver
#python ./test-webserver.py > /dev/null 2>&1 &
python ./test-webserver.py > /dev/null 2>stderr &
#python ./test-webserver.py &

echo test-deep
echo
python ../cocrawler/crawl.py --configfile test-deep.yml
# tests against the logfiles
grep -q "/denied/" robotslog.jsonl || (echo "FAIL: nothing about /denied/ in robotslog"; exit 1)
(grep "/denied/" crawllog.jsonl | grep -q -v '"robots"' ) && (echo "FAIL: should not have seen /denied/ in crawllog.jsonl"; exit 1)

echo
echo test-wide
echo
python ../cocrawler/crawl.py --configfile test-wide.yml

# remove logfiles
rm -f robotslog.jsonl crawllog.jsonl

# tear down the webserver
kill %1
