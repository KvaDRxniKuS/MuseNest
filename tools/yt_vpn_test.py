#!/usr/bin/env python3
"""yt-dlp VPN/proxy tester for MuseNest — compare several VPN configs at once.

Examples
--------
# 1) Compare the system route (VPN client ON) with a SOCKS5 proxy and direct:
python tools/yt_vpn_test.py --direct --proxy "NL SOCKS5=socks5://127.0.0.1:1080"

# 2) Test the proxy that is already configured in MuseNest + direct:
python tools/yt_vpn_test.py --use-config-proxy --direct

# 3) Profiles from a file, custom target, which player clients survive, save JSON:
python tools/yt_vpn_test.py --profile-file vpn.txt \
    --url https://www.youtube.com/watch?v=dQw4w9WgXcQ \
    --clients web,android,tv --json data/vpn_tests/last.json

# 4) Just look at the toolchain:
python tools/yt_vpn_test.py --check-env

Profile file format (plain text, one per line, `#` = comment):

    Нидерланды SOCKS5 = socks5://127.0.0.1:1080
    Германия HTTP, http://user:pass@1.2.3.4:8080
    direct
    target: https://www.youtube.com/watch?v=XXXX

or JSON:  {"profiles": [{"name": "...", "proxy": "..."}], "targets": ["..."]}

Notes
-----
* `direct` means "no proxy" — yt-dlp uses the current system route, i.e. whatever
  your VPN client is tunnelling right now. Switch VPN nodes in the client and run
  the tester again with --label to record each node.
* Exit code is 0 only when every profile verdict is ok/throttled.
"""

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import vpn_probe as vp  # noqa: E402


def _load_config():
    try:
        from core import config as cfg_mod
        return cfg_mod.load_config()
    except Exception:
        return {}


def _profiles_from_args(args):
    profiles, targets = [], list(args.url or [])

    if args.profile_file:
        try:
            with open(args.profile_file, "r", encoding="utf-8") as fh:
                spec = fh.read()
        except Exception as e:
            print("Не удалось прочитать %s: %s" % (args.profile_file, e), file=sys.stderr)
            sys.exit(2)
        p, t = vp.parse_profile_spec(spec)
        profiles.extend(p)
        targets.extend(t)

    for item in args.proxy or []:
        name, sep, rest = item.partition("=")
        if sep:
            profiles.append({"name": name.strip(), "proxy": vp.normalize_proxy(rest)})
        else:
            proxy = vp.normalize_proxy(item)
            profiles.append({"name": vp.proxy_label({"proxy": proxy}), "proxy": proxy})

    if args.direct:
        profiles.append({"name": "direct (системный маршрут)", "proxy": None})

    if args.use_config_proxy:
        proxy = vp.normalize_proxy((_load_config() or {}).get("proxy"))
        profiles.append({"name": "прокси из настроек MuseNest", "proxy": proxy})

    return profiles, targets


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="yt_vpn_test",
        description="Тестер yt-dlp для разных конфигов VPN/прокси (MuseNest)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Формат профилей и примеры — в шапке файла (python tools/yt_vpn_test.py -h).")
    src = ap.add_argument_group("профили VPN")
    src.add_argument("--proxy", action="append", metavar="NAME=URL",
                     help="профиль вида «NL SOCKS5=socks5://127.0.0.1:1080» (повторяется)")
    src.add_argument("--profile-file", metavar="FILE", help="файл профилей (текст или JSON)")
    src.add_argument("--direct", action="store_true",
                     help="добавить профиль без прокси (системный маршрут = текущий VPN)")
    src.add_argument("--use-config-proxy", action="store_true",
                     help="добавить прокси из data/config.json (поле «Прокси»)")

    tgt = ap.add_argument_group("цели проверки")
    tgt.add_argument("--url", action="append", metavar="URL",
                     help="URL видео (первый используется для extract/download; повторяется)")
    tgt.add_argument("--download-url", metavar="URL",
                     help="отдельный URL для теста скачивания (по умолчанию --url)")
    tgt.add_argument("--search-query", metavar="TEXT", help="запрос для теста поиска")
    tgt.add_argument("--search-limit", type=int, default=5)

    opt = ap.add_argument_group("набор проверок")
    opt.add_argument("--clients", metavar="LIST", default="",
                     help="player clients через запятую (web,android,tv) — проверить каждый")
    opt.add_argument("--cookies", choices=["auto", "none"], default="auto",
                     help="auto = как в настройках MuseNest, none = без cookies (по умолчанию auto)")
    opt.add_argument("--no-ip", action="store_true", help="пропустить проверку исходящего IP/гео")
    opt.add_argument("--no-reach", action="store_true", help="пропустить проверку доступности youtube.com")
    opt.add_argument("--no-extract", action="store_true", help="пропустить извлечение метаданных")
    opt.add_argument("--no-search", action="store_true", help="пропустить проверку поиска")
    opt.add_argument("--no-download", action="store_true", help="пропустить реальное скачивание")
    opt.add_argument("--mp3", action="store_true",
                     help="проверить боевой путь MuseNest (конвертация в mp3 через ffmpeg)")
    opt.add_argument("--fast", action="store_true",
                     help="быстрый режим: без поиска, без скачивания, без матрицы clients")
    opt.add_argument("--timeout", type=float, default=25, help="таймаут HTTP-операций, c (25)")
    opt.add_argument("--throttle-kbps", type=float, default=60,
                     help="порог «скорость режется», кбит/с (60)")
    opt.add_argument("--delay", type=float, default=2.0, help="пауза между профилями, c (2)")

    out = ap.add_argument_group("отчёт")
    out.add_argument("--label", default="", help="метка запуска (например, имя ноды VPN)")
    out.add_argument("--json", metavar="PATH", help="сохранить результат в JSON")
    out.add_argument("--md", metavar="PATH", help="сохранить markdown-таблицу")
    out.add_argument("--save", action="store_true",
                     help="сохранить JSON в data/vpn_tests/<timestamp>.json")
    out.add_argument("--quiet", action="store_true", help="не печатать прогресс и отчёт")
    out.add_argument("--check-env", action="store_true",
                     help="только показать состояние окружения и выйти")
    args = ap.parse_args(argv)

    env = vp.env_report()
    if args.check_env:
        print(json.dumps(env, ensure_ascii=False, indent=2))
        for h in vp.env_hints(env):
            print("! " + h)
        return 0

    if not env.get("yt_dlp_ok"):
        print("ОШИБКА: yt-dlp не установлен. Выполните: pip install -U yt-dlp", file=sys.stderr)
        return 3

    profiles, targets = _profiles_from_args(args)
    if not profiles:
        cfg_proxy = vp.normalize_proxy((_load_config() or {}).get("proxy"))
        profiles = [{"name": "прокси из настроек MuseNest" if cfg_proxy else "direct (системный маршрут)",
                     "proxy": cfg_proxy}]
    profiles = vp.dedupe_profiles(profiles)
    targets = targets or [vp.DEFAULT_TARGET]

    clients = [c.strip() for c in (args.clients or "").split(",") if c.strip()]
    options = {
        "timeout": args.timeout,
        "cookies": args.cookies,
        "ip": not args.no_ip,
        "reach": not args.no_reach,
        "extract": not args.no_extract,
        "search": (not args.no_search) and (not args.fast),
        "download": (not args.no_download) and (not args.fast),
        "clients": clients if (clients and not args.fast) else None,
        "mp3": args.mp3,
        "throttle_kbps": args.throttle_kbps,
        "search_query": args.search_query or vp.DEFAULT_SEARCH,
        "search_limit": args.search_limit,
        "download_url": args.download_url,
    }

    cfg = _load_config()

    def on_log(name, msg):
        if not args.quiet:
            print("  [%s] %s" % (name, msg), flush=True)

    def on_profile(i, total, res):
        if not args.quiet:
            print("→ [%d/%d] %s: %s (%.1f c)" % (
                i, total, res.get("name"), res.get("verdict_text"), res.get("elapsed_s") or 0),
                flush=True)

    if not args.quiet:
        print("Проверка %d профилей: %s" % (
            len(profiles), ", ".join(p["name"] for p in profiles)))
        print("Цель: %s" % targets[0])
        print("-" * 72)

    started = time.time()
    results = []
    for i, p in enumerate(profiles):
        if i and args.delay > 0:
            time.sleep(args.delay)
        res = vp.run_profile(p, cfg=cfg, targets=targets, options=options, on_log=on_log)
        results.append(res)
        on_profile(i + 1, len(profiles), res)

    payload = {
        "schema": vp.PROBE_SCHEMA,
        "label": args.label,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(time.time() - started, 1),
        "env": env,
        "targets": targets,
        "options": {k: v for k, v in options.items() if k != "clients" or v},
        "profiles": results,
    }

    if args.json or args.save:
        path = args.json
        if args.save and not path:
            d = os.path.join(_ROOT, "data", "vpn_tests")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, time.strftime("vpn_test_%Y%m%d_%H%M%S.json"))
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            if not args.quiet:
                print("\nJSON сохранён: %s" % path)
        except Exception as e:
            print("Не удалось сохранить JSON (%s): %s" % (path, e), file=sys.stderr)

    if args.md:
        try:
            with open(args.md, "w", encoding="utf-8") as fh:
                fh.write(vp.format_markdown_report(results, env=env, targets=targets,
                                                   label=args.label))
            if not args.quiet:
                print("Markdown сохранён: %s" % args.md)
        except Exception as e:
            print("Не удалось сохранить markdown: %s" % e, file=sys.stderr)

    if not args.quiet:
        print()
        print(vp.format_text_report(results, env=env, targets=targets))

    bad = [r for r in results if r.get("verdict") not in ("ok", "throttled")]
    return 0 if results and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
