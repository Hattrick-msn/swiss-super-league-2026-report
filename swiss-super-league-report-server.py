"""Serve and refresh the 2026/27 Swiss Super League second-yellow report."""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "report-assets" / "swiss-super-league-report-cache.json"
ACCOUNT_KEY = "Os-hXumIK"
TRPC_BASE = "https://origins-webex-orchestrator.origins-digital.com/trpc"
WIDGET_BASE = "https://origins-widgets-orchestrator.origins-digital.com"
COMPETITION_PROVIDER_ID = "e0lck99w8meo9qoalfrxgo33o"
SEASON_PROVIDER_ID = "cx0b7yl7kqmgay9wn2c3zyjh0"
SEASON_START = "2026-07-01T00:00:00.000Z"
SEASON_END = "2027-06-30T23:59:59.999Z"
REPORT_FILE = "2026-2027-swiss-super-league-second-yellow-report.html"
FINISHED_RECHECK_SECONDS = 6 * 60 * 60
LIVE_RECHECK_SECONDS = 45


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def number(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ReportStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.refresh_lock = threading.Lock()
        self.entries: dict[str, dict[str, Any]] = {}
        self.fixture_count = 0
        self.eligible_count = 0
        self.last_sync: str | None = None
        self.failures: list[str] = []
        self.refreshing = False
        self.card_leaderboards: dict[str, list[dict[str, Any]]] = {
            "yellow": [],
            "doubleYellow": [],
            "red": [],
        }
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        with self.lock:
            self.entries = cached.get("entries", {})
            self.fixture_count = number(cached.get("fixtureCount"))
            self.eligible_count = number(cached.get("eligibleCount"))
            self.last_sync = cached.get("lastSync")
            leaderboards = cached.get("cardLeaderboards", {})
            for key in self.card_leaderboards:
                rows = leaderboards.get(key, [])
                self.card_leaderboards[key] = rows if isinstance(rows, list) else []

    def _save_cache(self) -> None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": self.entries,
            "fixtureCount": self.fixture_count,
            "eligibleCount": self.eligible_count,
            "lastSync": self.last_sync,
            "cardLeaderboards": self.card_leaderboards,
        }
        temporary = CACHE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, CACHE_PATH)

    @staticmethod
    def _request_json(url: str, timeout: int = 35) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "User-Agent": "Mozilla/5.0 Swiss-Super-League-Card-Report/1.0",
                "x-account-key": ACCOUNT_KEY,
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    @classmethod
    def _trpc_query(cls, procedure: str, payload: dict[str, Any]) -> Any:
        encoded = urllib.parse.quote(
            json.dumps({"0": payload}, separators=(",", ":")), safe=""
        )
        data = cls._request_json(
            f"{TRPC_BASE}/{procedure}?batch=1&input={encoded}", timeout=40
        )
        if not isinstance(data, list) or not data:
            raise ValueError(f"Unexpected {procedure} response")
        item = data[0]
        if "error" in item:
            raise ValueError(item["error"].get("message", f"{procedure} failed"))
        return item["result"]["data"]

    @classmethod
    def _fetch_schedule(cls) -> list[dict[str, Any]]:
        data = cls._trpc_query(
            "event.getAll",
            {
                "competitionsProviderIds": COMPETITION_PROVIDER_ID,
                "startDate": SEASON_START,
                "endDate": SEASON_END,
                "language": "fr",
            },
        )
        events = data.get("events", []) if isinstance(data, dict) else []
        return [event for event in events if isinstance(event, dict)]

    @staticmethod
    def _player_id(player: dict[str, Any]) -> str:
        codename = str(player.get("codename") or "")
        if "_" in codename:
            return codename.rsplit("_", 1)[-1]
        return str(player.get("slug") or player.get("name") or "unknown")

    @staticmethod
    def _stat_value(player: dict[str, Any], stat_type: str) -> int:
        for stat in player.get("stats", []):
            if stat.get("type") == stat_type:
                return number(stat.get("value"))
        return 0

    @classmethod
    def _fetch_stat_rows(cls, stat_type: str) -> list[dict[str, Any]]:
        data = cls._trpc_query(
            "stats.getStats",
            {
                "competitionProviderId": COMPETITION_PROVIDER_ID,
                "order": "desc",
                "sortKey": stat_type,
                "statType": "player",
                "seasonProviderId": SEASON_PROVIDER_ID,
                "limit": 100,
                "cursor": 0,
                "language": "fr",
            },
        )
        rows: list[dict[str, Any]] = []
        for player in data.get("players", []):
            team = player.get("team") or {}
            rows.append(
                {
                    "playerId": cls._player_id(player),
                    "name": str(player.get("name") or "-"),
                    "team": str(team.get("name") or "-"),
                    "teamSlug": str(team.get("slug") or ""),
                    "playerSlug": str(player.get("slug") or ""),
                    "value": cls._stat_value(player, stat_type),
                }
            )
        return rows

    @staticmethod
    def _rank_rows(
        players: dict[str, dict[str, Any]], primary: str
    ) -> list[dict[str, Any]]:
        rows = [dict(row) for row in players.values() if number(row.get(primary)) > 0]
        rows.sort(
            key=lambda row: (
                -number(row.get(primary)),
                -number(row.get("doubleYellow")),
                str(row.get("name", "")).casefold(),
            )
        )
        previous: int | None = None
        rank = 0
        for index, row in enumerate(rows, start=1):
            value = number(row.get(primary))
            if value != previous:
                rank = index
                previous = value
            row["rank"] = rank
        return rows

    @classmethod
    def _fetch_leaderboards(cls) -> dict[str, list[dict[str, Any]]]:
        stat_keys = {
            "yellow": "total yellow card",
            "doubleYellow": "total second yellow",
            "red": "total red card",
        }
        fetched: dict[str, list[dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(cls._fetch_stat_rows, stat): key
                for key, stat in stat_keys.items()
            }
            for future in as_completed(futures):
                fetched[futures[future]] = future.result()

        combined: dict[str, dict[str, Any]] = {}
        for key, rows in fetched.items():
            for row in rows:
                player_id = row["playerId"]
                current = combined.setdefault(
                    player_id,
                    {
                        "playerId": player_id,
                        "name": row["name"],
                        "team": row["team"],
                        "teamSlug": row["teamSlug"],
                        "playerSlug": row["playerSlug"],
                        "yellow": 0,
                        "doubleYellow": 0,
                        "red": 0,
                    },
                )
                current[key] = row["value"]
                if current["name"] == "-" and row["name"] != "-":
                    current["name"] = row["name"]
                if current["team"] == "-" and row["team"] != "-":
                    current["team"] = row["team"]

        return {
            "yellow": cls._rank_rows(combined, "yellow"),
            "doubleYellow": cls._rank_rows(combined, "doubleYellow"),
            "red": cls._rank_rows(combined, "red"),
        }

    @staticmethod
    def _message_groups(commentary: dict[str, Any]) -> list[dict[str, Any]]:
        groups = commentary.get("messages") or []
        for group in groups:
            if str(group.get("language", "")).lower() == "en-gb":
                return group.get("message") or []
        return groups[0].get("message", []) if groups else []

    @staticmethod
    def _display_minute(message: dict[str, Any]) -> str:
        value = str(message.get("time") or "").strip()
        if value:
            return value.replace("'+'", "+").replace("'", "'")
        minute = number(message.get("minute"), -1)
        return f"{minute + 1}'" if minute >= 0 else "待定"

    @staticmethod
    def _player_name(comment: str) -> str:
        patterns = (
            r"Second yellow card to (.+?) \(",
            r"(.+?) \(.+?\) is shown the second yellow card",
        )
        for pattern in patterns:
            match = re.search(pattern, comment, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return comment.split("(", 1)[0].replace("Second yellow card to", "").strip()

    @staticmethod
    def _reason(comment: str) -> str:
        lowered = comment.casefold()
        translations = (
            ("bad foul", "危险犯规"),
            ("dissent", "对判罚表示不满"),
            ("simulation", "假摔"),
            ("time wasting", "拖延时间"),
            ("unsporting", "非体育行为"),
        )
        for needle, label in translations:
            if needle in lowered:
                return label
        return "第二张黄牌"

    @classmethod
    def _extract_match(
        cls, fixture: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any] | None:
        commentary = data.get("commentary") or {}
        messages = cls._message_groups(commentary)
        second_yellows = [
            message
            for message in messages
            if str(message.get("dataProviderType", "")).casefold()
            == "secondyellow card"
        ]
        if not second_yellows:
            return None

        match_info = commentary.get("matchInfo") or {}
        contestants = {
            str(team.get("id")): team for team in match_info.get("contestant", [])
        }
        events: list[dict[str, Any]] = []
        for second in second_yellows:
            player_id = str(second.get("playerRef1") or "")
            first = next(
                (
                    message
                    for message in reversed(messages)
                    if str(message.get("playerRef1") or "") == player_id
                    and str(message.get("dataProviderType", "")).casefold()
                    == "yellow card"
                ),
                None,
            )
            team = contestants.get(str(second.get("teamRef1") or ""), {})
            comment = str(second.get("comment") or "")
            events.append(
                {
                    "playerId": player_id,
                    "person": cls._player_name(comment),
                    "team": str(
                        team.get("officialName")
                        or team.get("name")
                        or fixture.get("awayTeamName")
                        or "-"
                    ),
                    "role": "球员",
                    "first": cls._display_minute(first or {}),
                    "second": cls._display_minute(second),
                    "reason": cls._reason(comment),
                    "reasonOriginal": comment,
                    "eventId": str(second.get("id") or ""),
                }
            )

        details = commentary.get("liveData", {}).get("matchDetails", {})
        scores = details.get("scores", {}).get("total", {})
        status = "已结束" if details.get("matchStatus") in {"Played", "Awarded"} else "进行中"
        local_time = str(match_info.get("localTime") or fixture.get("time") or "--:--")[:5]
        week = str(match_info.get("week") or fixture.get("week") or "-")
        return {
            "id": str(fixture.get("providerId") or match_info.get("id") or ""),
            "date": str(match_info.get("localDate") or fixture.get("date") or "")[:10],
            "time": local_time,
            "stage": f"第{week}轮",
            "type": "regular",
            "status": status,
            "home": str(fixture.get("homeTeamName") or "主队"),
            "homeScore": number(scores.get("home"), number(fixture.get("homeScore"))),
            "awayScore": number(scores.get("away"), number(fixture.get("awayScore"))),
            "away": str(fixture.get("awayTeamName") or "客队"),
            "events": events,
        }

    @classmethod
    def _fetch_commentary(
        cls, fixture: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        match_id = str(fixture.get("providerId") or "")
        data = cls._request_json(
            f"{WIDGET_BASE}/api/commentary?fixtureId={urllib.parse.quote(match_id)}",
            timeout=40,
        )
        match_info = (data.get("commentary") or {}).get("matchInfo") or {}
        return match_id, {
            "checkedAt": time.time(),
            "lastUpdated": match_info.get("lastUpdated"),
            "status": str(fixture.get("gameStatus") or ""),
            "match": cls._extract_match(fixture, data),
        }

    def refresh(self, force: bool = False) -> None:
        if not self.refresh_lock.acquire(blocking=False):
            return
        try:
            with self.lock:
                self.refreshing = True
            failures: list[str] = []
            schedule = self._fetch_schedule()
            eligible = [
                fixture
                for fixture in schedule
                if fixture.get("gameStatus") in {"post-game", "live", "half-time"}
            ]
            now = time.time()
            with self.lock:
                cached_entries = dict(self.entries)
            due: list[dict[str, Any]] = []
            for fixture in eligible:
                match_id = str(fixture.get("providerId") or "")
                cached = cached_entries.get(match_id)
                age = now - float(cached.get("checkedAt", 0)) if cached else float("inf")
                interval = (
                    FINISHED_RECHECK_SECONDS
                    if fixture.get("gameStatus") == "post-game"
                    else LIVE_RECHECK_SECONDS
                )
                if force or not cached or age >= interval:
                    due.append(fixture)

            updated_entries = dict(cached_entries)
            if due:
                with ThreadPoolExecutor(max_workers=6) as executor:
                    futures = {
                        executor.submit(self._fetch_commentary, fixture): str(
                            fixture.get("providerId") or ""
                        )
                        for fixture in due
                    }
                    for future in as_completed(futures):
                        match_id = futures[future]
                        try:
                            result_id, entry = future.result()
                            updated_entries[result_id] = entry
                        except (
                            urllib.error.URLError,
                            TimeoutError,
                            json.JSONDecodeError,
                            OSError,
                            ValueError,
                        ) as error:
                            failures.append(f"比赛 {match_id}: {error}")

            try:
                leaderboards = self._fetch_leaderboards()
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                OSError,
                ValueError,
            ) as error:
                failures.append(f"球员牌榜: {error}")
                leaderboards = self.card_leaderboards

            eligible_ids = {str(item.get("providerId") or "") for item in eligible}
            with self.lock:
                self.entries = {
                    match_id: entry
                    for match_id, entry in updated_entries.items()
                    if match_id in eligible_ids
                }
                self.fixture_count = len(schedule)
                self.eligible_count = len(eligible)
                self.card_leaderboards = leaderboards
                self.last_sync = utc_now()
                self.failures = failures
                self._save_cache()
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
            ValueError,
        ) as error:
            with self.lock:
                self.failures = [f"赛程读取: {error}"]
        finally:
            with self.lock:
                self.refreshing = False
            self.refresh_lock.release()

    def report(self) -> dict[str, Any]:
        with self.lock:
            matches = [
                entry["match"]
                for entry in self.entries.values()
                if isinstance(entry.get("match"), dict)
            ]
            matches.sort(
                key=lambda item: (item.get("date", ""), item.get("time", "")),
                reverse=True,
            )
            player_ids = {
                event.get("playerId")
                for match in matches
                for event in match.get("events", [])
            }
            return {
                "season": "2026/2027",
                "source": "SFL / Opta",
                "lastSync": self.last_sync,
                "refreshing": self.refreshing,
                "failures": list(self.failures),
                "fixtureCount": self.fixture_count,
                "checkedMatches": self.eligible_count,
                "matchCount": len(matches),
                "eventCount": sum(len(match.get("events", [])) for match in matches),
                "playerCount": len(player_ids),
                "cardLeaderboards": self.card_leaderboards,
                "matches": matches,
            }

    def run_scheduler(self) -> None:
        while True:
            self.refresh()
            time.sleep(60)


STORE = ReportStore()


class ReportHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/report":
            params = urllib.parse.parse_qs(parsed.query)
            if params.get("refresh") == ["1"]:
                threading.Thread(
                    target=STORE.refresh, kwargs={"force": True}, daemon=True
                ).start()
            self._send_json(STORE.report())
            return
        if parsed.path == "/api/health":
            self._send_json({"ok": True, "refreshing": STORE.report()["refreshing"]})
            return
        if parsed.path == "/":
            self.send_response(302)
            self.send_header("Location", f"/{REPORT_FILE}")
            self.end_headers()
            return
        super().do_GET()

    def _send_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: Any) -> None:
        if args and str(args[1]) in {"200", "304"}:
            return
        super().log_message(format_string, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Swiss Super League card report")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8775)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--refresh-once", action="store_true")
    parser.add_argument("--export-report", type=Path)
    args = parser.parse_args()

    if args.refresh_once:
        STORE.refresh(force=True)
        if args.export_report:
            args.export_report.parent.mkdir(parents=True, exist_ok=True)
            args.export_report.write_text(
                json.dumps(STORE.report(), ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        if STORE.failures:
            print("Refresh completed with cached fallbacks:", *STORE.failures, sep="\n- ")
        else:
            print(f"Swiss Super League data refreshed at {STORE.last_sync}")
        return

    scheduler = threading.Thread(target=STORE.run_scheduler, daemon=True)
    scheduler.start()
    server = ThreadingHTTPServer((args.host, args.port), ReportHandler)
    report_url = f"http://{args.host}:{args.port}/{REPORT_FILE}"
    print(f"Swiss Super League report: {report_url}")
    if args.open:
        threading.Timer(0.5, webbrowser.open, args=(report_url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
