#!/bin/sh -e
# Build history-rag-lambda.zip for the python3.12 x86_64 runtime.
# Cross-installs manylinux wheels so this works from macOS; --only-binary
# guarantees nothing tries to compile for the wrong platform.
cd "$(dirname "$0")"
rm -rf build history-rag-lambda.zip
python3 -m pip install --quiet --target build --no-compile \
  --platform manylinux2014_x86_64 --implementation cp \
  --python-version 3.12 --only-binary=:all: \
  -r requirements.txt
cp app.py ../../server.py ../../config.py ../../ask.py build/
# server.py's expanders import sources/* lazily — the import must resolve
# even though live context falls back to index reconstruction here.
# appusage/ stays out: its expander guards its own import.
cp -R ../../sources build/sources
rm -rf build/sources/__pycache__
(cd build && zip -qr ../history-rag-lambda.zip .)
echo "built history-rag-lambda.zip ($(du -h history-rag-lambda.zip | cut -f1))"
