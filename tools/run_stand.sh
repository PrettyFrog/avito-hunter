#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export PYTHONPATH="${REPO_ROOT}/src:${REPO_ROOT}"
export HUNTER_TIMEZONE="Europe/Moscow"

echo "=== Starting Avito Hunter Local Stand (Fake Telegram + Fake Avito) ==="

TMP_STAND_DIR=$(mktemp -d /tmp/avito_hunter_stand_XXXXXX)
cleanup() {
    echo "=== Shutting down stand and cleaning up ${TMP_STAND_DIR} ==="
    rm -rf "${TMP_STAND_DIR}"
}
trap cleanup EXIT INT TERM

# Run python stand runner
"${REPO_ROOT}/.venv/bin/python3" -c "
import asyncio
from tools.stand import create_stand

async def main():
    print('Starting simulated stand...')
    stand = await create_stand()
    print(f'Stand running! Owner: {stand.owner.chat_id}, Temp DB: {stand.temp_dir}')
    print('Testing /start command from owner...')
    await stand.owner.send_command('start')
    msg = await stand.owner.wait_for_message(timeout=5.0)
    print(f'Bot responded to /start: {msg.text[:60]}...')
    print('Stand verified. Press Ctrl+C or wait 2 seconds.')
    await asyncio.sleep(2.0)
    await stand.stop()
    print('Stand stopped cleanly.')

asyncio.run(main())
"

echo "=== Stand run completed successfully ==="
