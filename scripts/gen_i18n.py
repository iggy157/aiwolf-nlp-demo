#!/usr/bin/env python3
"""言語別ゲームサーバ設定と配線を生成する（多言語デモのサーバ側ローカライズ）。

なぜ必要か:
  ゲームサーバは custom_profile（名前・プロフィール）を起動時に一度だけ読み、その1セットを
  全 room に適用する（room ごとに言語を切り替える機構はない）。よって「卓の言語ごとに
  ローカライズされた名前・プロフィールでAIを動かす」には、言語ごとに別サーバプロセスを立て、
  lobby がその言語の卓を該当サーバへ振り分ける必要がある。

このスクリプトの出力:
  configs/generated/server.<lang>.yml / server9.<lang>.yml  … ja以外13言語ぶん（base + ローカライズ custom_profile）
  repos/aiwolf-nlp-viewer/src/lib/data/profiles/<lang>.json  … 各エントリに avatar(ローカルパス)を埋め込み（全14言語）
  docker-compose.langs.yml                                   … 13言語×2サイズのサーバサービス
  caddy/langs.caddy                                          … 13言語×2サイズの WebSocket ルート（Caddyfile が import）
  configs/generated/languages.txt                           … 生成対象の言語一覧

入力:
  configs/server.yml / server9.yml      … ベース（avatar_url/voice_id/age/構造はここから）
  configs/server-i18n.json              … 言語別の性別語(male/female)とラベル(年齢/性別/性格)
  repos/.../profiles/<lang>.json        … 言語別の名前・性格（viewer用に既に翻訳済み）

ja はベース(server.yml/server9.yml)がそのまま日本語なので生成しない（lobby は ja を既定サーバへ）。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LANGS = ["ja", "en", "zh", "hi", "es", "ar", "bn", "fr", "ru", "pt", "ur", "id", "de", "nl"]
DEFAULT = "ja"
NONDEFAULT = [l for l in LANGS if l != DEFAULT]

PROFILES_DIR = ROOT / "repos" / "aiwolf-nlp-viewer" / "src" / "lib" / "data" / "profiles"
GEN_DIR = ROOT / "configs" / "generated"
IMAGE = "aiwolf-nlp-game-server:local"  # 全言語サーバで共有する単一イメージ


def local_avatar(url: str) -> str:
    """サーバの avatar_url(絶対URL) を viewer のローカル配信パスに変換。"""
    if "/images/" in url:
        return "/images/" + url.split("/images/", 1)[1]
    return url


def main() -> None:
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / "caddy").mkdir(exist_ok=True)

    i18n = json.loads((ROOT / "configs" / "server-i18n.json").read_text(encoding="utf-8"))
    base5 = yaml.safe_load((ROOT / "configs" / "server.yml").read_text(encoding="utf-8"))
    base9 = yaml.safe_load((ROOT / "configs" / "server9.yml").read_text(encoding="utf-8"))
    base_profiles = base5["custom_profile"]["profiles"]  # JP名キーの正本（順序・avatar/voice/age）

    def localized_profiles(lang: str) -> list[dict]:
        trans = json.loads((PROFILES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
        out = []
        for p in base_profiles:
            jp = p["name"]
            t = trans[jp]
            g = p.get("gender", "")
            gender = (
                i18n[lang]["male"] if g == "男性"
                else i18n[lang]["female"] if g == "女性"
                else g
            )
            np = copy.deepcopy(p)
            np["name"] = t["name"]            # GameName を現地名に（プロトコル全体で使われる）
            np["personality"] = t["personality"]
            np["gender"] = gender
            out.append(np)
        return out

    def encoding(lang: str) -> dict:
        return {
            "age": i18n[lang]["label_age"],
            "gender": i18n[lang]["label_gender"],
            "personality": i18n[lang]["label_personality"],
        }

    def write_server(base: dict, lang: str, fname: str) -> None:
        cfg = copy.deepcopy(base)
        cfg["custom_profile"]["profiles"] = localized_profiles(lang)
        cfg["custom_profile"]["profile_encoding"] = encoding(lang)
        with (GEN_DIR / fname).open("w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    # 1) 言語別サーバ設定（ja以外）
    for lang in NONDEFAULT:
        write_server(base5, lang, f"server.{lang}.yml")
        write_server(base9, lang, f"server9.{lang}.yml")

    # 2) viewer profiles に avatar(ローカルパス) を埋め込み（全14言語）。
    #    サーバが現地名を GameName で送るため、viewer は「現ロケールの 名前→avatar」で解決する。
    avatar_by_jp = {p["name"]: local_avatar(p["avatar_url"]) for p in base_profiles}
    for lang in LANGS:
        f = PROFILES_DIR / f"{lang}.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        for jp, entry in data.items():
            if jp in avatar_by_jp:
                entry["avatar"] = avatar_by_jp[jp]
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 3) docker-compose override（言語別サーバ）。単一イメージ(IMAGE)を共有して再ビルドを避ける。
    services = {}
    for lang in NONDEFAULT:
        services[f"game-server-{lang}"] = {
            "image": IMAGE,
            "command": ["-c", f"/app/configs/generated/server.{lang}.yml"],
            "volumes": ["./configs:/app/configs:ro", "./log:/app/log"],
            "expose": ["8080"],
            "depends_on": ["game-server"],  # 共有イメージのビルド元
            "restart": "unless-stopped",
        }
        services[f"game-server-9-{lang}"] = {
            "image": IMAGE,
            "command": ["-c", f"/app/configs/generated/server9.{lang}.yml"],
            "volumes": ["./configs:/app/configs:ro", "./log:/app/log"],
            "expose": ["8080"],
            "depends_on": ["game-server"],  # 共有イメージのビルド元
            "restart": "unless-stopped",
        }
    header = (
        "# 自動生成: scripts/gen_i18n.py。編集しないこと。\n"
        "# 言語別ゲームサーバ（ja以外）。base の game-server がビルドする " + IMAGE + " を共有。\n"
        "# 使い方: docker compose -f docker-compose.yml -f docker-compose.langs.yml up\n"
    )
    with (ROOT / "docker-compose.langs.yml").open("w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump({"services": services}, f, allow_unicode=True, sort_keys=False)

    # 4) Caddy ルート（言語別）。Caddyfile が import する。サーバの実WSパスは /ws なので rewrite。
    lines = ["# 自動生成: scripts/gen_i18n.py。言語別サーバへの WebSocket ルート。Caddyfile が import する。"]
    for lang in NONDEFAULT:
        lines.append(f"handle /ws-{lang} {{\n\trewrite * /ws\n\treverse_proxy game-server-{lang}:8080\n}}")
        lines.append(f"handle /ws9-{lang} {{\n\trewrite * /ws\n\treverse_proxy game-server-9-{lang}:8080\n}}")
    (ROOT / "caddy" / "langs.caddy").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 5) マニフェスト
    (GEN_DIR / "languages.txt").write_text(" ".join(LANGS) + "\n", encoding="utf-8")

    print(f"generated {len(NONDEFAULT)} langs × 2 servers, compose override, caddy routes; avatars embedded into {len(LANGS)} profiles")


if __name__ == "__main__":
    main()
