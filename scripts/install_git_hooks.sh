#!/usr/bin/env sh
set -eu

git config core.hooksPath .githooks
chmod 700 .githooks/pre-commit
echo "Northstar Git hooks enabled."

