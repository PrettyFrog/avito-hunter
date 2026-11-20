import os
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
db_path = (
    sys.argv[2] if len(sys.argv) > 2 else "/home/egor/scraper/test-deploy-pr0a6/data/hunter.db"
)

print(f"MOCK_BOT_SERVICE: Started with mode={mode}, pid={os.getpid()}, db={db_path}", flush=True)


def get_conn():
    conn = sqlite3.connect(db_path)
    day_str = datetime.now(UTC).strftime("%Y-%m-%d")
    conn.execute("INSERT OR IGNORE INTO metrics(day) VALUES (?)", (day_str,))
    conn.commit()
    return conn, day_str


elapsed = 0
while True:
    time.sleep(1)
    elapsed += 1

    if mode == "normal":
        if elapsed % 5 == 0:
            conn, day_str = get_conn()
            conn.execute(
                "UPDATE metrics SET parse_ok = parse_ok + 1, "
                "hits_total = hits_total + 10 WHERE day = ?",
                (day_str,),
            )
            conn.commit()
            conn.close()
            print(
                f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} | INFO | "
                "hunter.pipeline.orchestrator:_poll_watch:199 - watch "
                "narrow_watch: poll ok (hits=10)",
                flush=True,
            )

    elif mode == "traceback":
        if elapsed == 12:
            print(
                "2026-10-05 02:20:00 | ERROR | "
                "hunter.pipeline.orchestrator:_poll_watch:154 - poll interrupted:",
                file=sys.stderr,
                flush=True,
            )
            print("Traceback (most recent call last):", file=sys.stderr, flush=True)
            print(
                '  File "/home/egor/scraper/test-deploy-pr0a6/src/hunter/pipeline/'
                'orchestrator.py", line 123, in _poll_watch',
                file=sys.stderr,
                flush=True,
            )
            print("ValueError: unhandled fatal bug in orchestrator", file=sys.stderr, flush=True)

    elif mode == "crash_20s":
        if elapsed >= 20:
            print(
                "MOCK_BOT_SERVICE: Simulated unhandled fatal exception/crash at 20s! "
                "Exiting with code 1...",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(1)

    elif mode == "no_metrics":
        pass

    elif mode == "one_lot_no_price_5_polls":
        if elapsed % 5 == 0 and elapsed <= 25:
            conn, day_str = get_conn()
            no_price_inc = 1 if elapsed == 5 else 0
            conn.execute(
                "UPDATE metrics SET parse_ok = parse_ok + 1, hits_total = hits_total + 2, "
                "hits_no_price = hits_no_price + ? WHERE day = ?",
                (no_price_inc, day_str),
            )
            conn.commit()
            conn.close()
            print(
                f"MOCK_BOT_SERVICE: poll {elapsed // 5}/5 completed, no_price_inc={no_price_inc}",
                flush=True,
            )

    elif mode == "alert_delivery_failed":
        if elapsed % 5 == 0:
            conn, day_str = get_conn()
            conn.execute(
                "UPDATE metrics SET parse_ok = parse_ok + 1, "
                "hits_total = hits_total + 10 WHERE day = ?",
                (day_str,),
            )
            conn.commit()
            conn.close()
            print(
                "2026-10-05 02:20:00 | ERROR | hunter.notify.base:send:150 - "
                "Alert delivery failed for 999: Telegram network timeout",
                file=sys.stderr,
                flush=True,
            )

    elif mode == "telegram_network_error":
        if elapsed % 5 == 0:
            conn, day_str = get_conn()
            conn.execute(
                "UPDATE metrics SET parse_ok = parse_ok + 1, "
                "hits_total = hits_total + 10 WHERE day = ?",
                (day_str,),
            )
            conn.commit()
            conn.close()
        if elapsed == 12:
            print(
                "2026-10-05 02:20:00 | ERROR | "
                "hunter.notify.telegram:send:88 - Alert delivery failed for 999:",
                file=sys.stderr,
                flush=True,
            )
            print("Traceback (most recent call last):", file=sys.stderr, flush=True)
            print(
                '  File "/home/egor/scraper/test-deploy-pr0a6/src/hunter/notify/telegram.py", '
                "line 75, in send",
                file=sys.stderr,
                flush=True,
            )
            print("    await bot.send_message(...)", file=sys.stderr, flush=True)
            print(
                "aiogram.exceptions.TelegramNetworkError: HTTP Client says - "
                "ClientConnectorError: Cannot connect to host api.telegram.org:443",
                file=sys.stderr,
                flush=True,
            )

    elif mode == "http_429_growth":
        if elapsed % 5 == 0:
            conn, day_str = get_conn()
            conn.execute(
                "UPDATE metrics SET parse_ok = parse_ok + 1, "
                "hits_total = hits_total + 10 WHERE day = ?",
                (day_str,),
            )
            conn.commit()
            conn.close()
        if elapsed == 10:
            conn, day_str = get_conn()
            circuit_time = (datetime.now(UTC) + timedelta(minutes=45)).isoformat()
            conn.execute("UPDATE metrics SET http_429 = http_429 + 1 WHERE day = ?", (day_str,))
            conn.execute(
                'INSERT OR REPLACE INTO runtime (key, value) VALUES ("circuit_until", ?)',
                (circuit_time,),
            )
            conn.commit()
            conn.close()
            print(
                "2026-10-05 02:20:00 | WARNING | "
                "hunter.pipeline.orchestrator:_poll_watch:140 - HTTP 429 received from Avito, "
                f"setting circuit breaker until {circuit_time}",
                flush=True,
            )

    elif mode == "traceback_importerror":
        if elapsed == 12:
            print("Traceback (most recent call last):", file=sys.stderr, flush=True)
            print(
                '  File "/home/egor/scraper/test-deploy-pr0a6/src/hunter/pipeline/'
                'orchestrator.py", line 12, in <module>',
                file=sys.stderr,
                flush=True,
            )
            print("    import non_existent_module", file=sys.stderr, flush=True)
            print("ImportError: No module named 'non_existent_module'", file=sys.stderr, flush=True)

    elif mode == "locked_1":
        if elapsed % 5 == 0:
            conn, day_str = get_conn()
            conn.execute(
                "UPDATE metrics SET parse_ok = parse_ok + 1, "
                "hits_total = hits_total + 10 WHERE day = ?",
                (day_str,),
            )
            conn.commit()
            conn.close()
        if elapsed == 12:
            print(
                "2026-10-05 02:20:00 | ERROR | "
                "hunter.pipeline.orchestrator:_poll_watch:154 - poll query failed:",
                file=sys.stderr,
                flush=True,
            )
            print("Traceback (most recent call last):", file=sys.stderr, flush=True)
            print(
                '  File "/home/egor/scraper/test-deploy-pr0a6/src/hunter/pipeline/'
                'orchestrator.py", line 123, in _poll_watch',
                file=sys.stderr,
                flush=True,
            )
            print("sqlite3.OperationalError: database is locked", file=sys.stderr, flush=True)

    elif mode == "locked_5":
        if elapsed == 12:
            for k in range(5):
                print(
                    f"2026-10-05 02:20:0{k} | ERROR | "
                    "hunter.pipeline.orchestrator:_poll_watch:154 - "
                    "poll query failed: sqlite3.OperationalError: database is locked",
                    file=sys.stderr,
                    flush=True,
                )
