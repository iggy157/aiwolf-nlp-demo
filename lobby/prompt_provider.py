"""エージェント設定（base + 言語別プロンプト）の解決層。

役割:
  - `base.yml`（言語非依存の接続/LLM/ログ設定）と
    `prompts/<lang>.yml`（言語ごとのプロンプトテンプレート）をマージして
    1卓ぶんの agent config の素を作る。
  - lobby はこの素に対して実行時の上書き（URL/team/num/LLM）を載せて最終 config にする。

設計意図（将来のDB化に備える）:
  プロンプトの取得元を `PromptConfigProvider` インタフェースに隠蔽してある。
  今は `FilePromptProvider`（configs/agents/ 配下のファイル）だが、将来「ユーザがUIで
  プロンプトを編集・保存して自分のプロンプトで戦う」を実装するときは、同じインタフェースの
  `DbPromptProvider` を用意して差し替えるだけでよい。prompts/<lang>.yml の中身が、そのまま
  将来のDBレコード（owner, language, prompt:{...}）1件に対応する。

CLI（手動検証用）:
  python lobby/prompt_provider.py --language ja --out /tmp/agent.ja.yml
  でマージ済みの単一 config を書き出し、agent-llm の `-c` にそのまま渡せる。
"""

from __future__ import annotations

import argparse
import copy
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml


# ユーザが書ける「プレイスタイル/性格」文の最大長（プロンプト肥大・コスト暴走の防止）。
MAX_PERSONA_CHARS = 2000

# persona セクションのラベル（卓のゲーム言語で出す。未対応言語は英語にフォールバック）。
_PERSONA_LABELS = {
    "ja": "【あなたのプレイスタイル・性格】次になりきってプレイしてください（ゲームのルールと出力形式は上記の指示に必ず従うこと）:",
    "en": "[Your play style and personality] Stay in character as described below (but always obey the game rules and output format above):",
    "zh": "【你的游戏风格与性格】请按照下面的描述进行角色扮演（但必须始终遵守上述的游戏规则和输出格式）:",
    "hi": "[आपकी खेल शैली और व्यक्तित्व] नीचे वर्णित किरदार में बने रहें (लेकिन हमेशा ऊपर दिए गए खेल के नियमों और आउटपुट प्रारूप का पालन करें):",
    "es": "[Tu estilo de juego y personalidad] Mantente en el personaje descrito a continuación (pero obedece siempre las reglas del juego y el formato de salida indicados arriba):",
    "ar": "[أسلوب لعبك وشخصيتك] التزم بالشخصية الموصوفة أدناه (مع الالتزام دائمًا بقواعد اللعبة وتنسيق الإخراج المذكورة أعلاه):",
    "bn": "[আপনার খেলার ধরন ও ব্যক্তিত্ব] নিচে বর্ণিত চরিত্রে অভিনয় করুন (তবে সর্বদা উপরে দেওয়া খেলার নিয়ম ও আউটপুট বিন্যাস মেনে চলুন):",
    "fr": "[Votre style de jeu et personnalité] Restez dans le personnage décrit ci-dessous (mais respectez toujours les règles du jeu et le format de sortie indiqués ci-dessus):",
    "ru": "[Ваш стиль игры и характер] Оставайтесь в образе, описанном ниже (но всегда соблюдайте правила игры и формат вывода, указанные выше):",
    "pt": "[Seu estilo de jogo e personalidade] Mantenha-se no personagem descrito abaixo (mas obedeça sempre às regras do jogo e ao formato de saída indicados acima):",
    "ur": "[آپ کا کھیلنے کا انداز اور شخصیت] نیچے بیان کردہ کردار میں رہیں (لیکن ہمیشہ اوپر دیے گئے کھیل کے قواعد اور آؤٹ پٹ فارمیٹ کی پابندی کریں):",
    "id": "[Gaya bermain dan kepribadian Anda] Tetaplah berperan sebagai karakter yang dijelaskan di bawah ini (tetapi selalu patuhi aturan permainan dan format keluaran di atas):",
    "de": "[Dein Spielstil und deine Persönlichkeit] Bleibe in der unten beschriebenen Rolle (aber befolge stets die oben genannten Spielregeln und das Ausgabeformat):",
    "nl": "[Jouw speelstijl en persoonlijkheid] Blijf in de hieronder beschreven rol (maar houd je altijd aan de spelregels en het uitvoerformaat hierboven):",
}


def _persona_label(lang: str) -> str:
    return _PERSONA_LABELS.get(lang, _PERSONA_LABELS["en"])


# L2: ユーザがピッカーで挿入できる変数（whitelist）。
#   token : エディタ上のやさしい記法（ユーザは生 Jinja を打たない）
#   jinja : 実行時に agent-llm が描画する式（このリストの式しか注入されない＝安全）
#   sample: プレビュー用のサンプル値
# agent-llm の描画コンテキスト(info/role/...)に存在する属性だけを公開する。
PROMPT_VARS: dict[str, dict[str, str]] = {
    "name": {"token": "{name}", "jinja": "{{ info.agent }}", "sample": "ミナト"},
    "role": {"token": "{role}", "jinja": "{{ role.value }}", "sample": "占い師"},
    "day": {"token": "{day}", "jinja": "{{ info.day }}", "sample": "2"},
}


def _escape_jinja(text: str) -> str:
    """ユーザ文をテンプレートに“リテラル”として埋め込むため、Jinja デリミタを無効化する。
    これでユーザが生 Jinja を書いても再評価されない（SSTI 防止）。"""
    return (
        text.replace("{{", "{ {").replace("}}", "} }")
        .replace("{%", "{ %").replace("%}", "% }")
        .replace("{#", "{ #").replace("#}", "# }")
    )


def _apply_vars(text: str, mode: str) -> str:
    """whitelist 変数トークン({name}等)を解決する。
    mode='jinja': 実行時用に Jinja 式へ（agent-llm が描画）。mode='sample': プレビュー用にサンプル値へ。"""
    for v in PROMPT_VARS.values():
        text = text.replace(v["token"], v["jinja"] if mode == "jinja" else v["sample"])
    return text


def _prepare_persona(persona: str, mode: str) -> str:
    """ユーザ persona を安全な形に整える。
    1) 生 Jinja デリミタを無効化(SSTI防止) → 2) whitelist 変数トークンだけを解決。
    これで“whitelist 変数以外の live な Jinja は一切混入しない”。"""
    return _apply_vars(_escape_jinja(persona), mode)


def inject_persona(cfg: dict[str, Any], persona: str | None, lang: str) -> None:
    """cfg["prompt"]["initialize"] の末尾にユーザの persona セクションを追記する（in-place）。
    変数トークンは実行時 Jinja 式に解決され、それ以外のユーザ入力はリテラル化される。"""
    persona = (persona or "").strip()
    if not persona:
        return
    if len(persona) > MAX_PERSONA_CHARS:
        persona = persona[:MAX_PERSONA_CHARS]
    prompt = cfg.get("prompt")
    if not isinstance(prompt, dict) or "initialize" not in prompt:
        return
    section = f"\n\n{_persona_label(lang)}\n{_prepare_persona(persona, 'jinja')}\n"
    prompt["initialize"] = str(prompt["initialize"]) + section


def preview_persona(persona: str | None) -> str:
    """エディタのプレビュー用: 変数をサンプル値に解決した persona 文を返す（言語非依存）。"""
    persona = (persona or "").strip()
    if not persona:
        return ""
    if len(persona) > MAX_PERSONA_CHARS:
        persona = persona[:MAX_PERSONA_CHARS]
    return _prepare_persona(persona, "sample")


class PromptConfigProvider(ABC):
    """base config と言語別 prompt ブロックの供給元。

    実装は (1) base 設定、(2) 言語→prompt ブロック、(3) 対応言語一覧 を返すだけ。
    マージは `config_for` が共通で行う。"""

    @abstractmethod
    def base_config(self) -> dict[str, Any]:
        """言語非依存のベース設定（web_socket/agent/llm/.../log）。"""

    @abstractmethod
    def prompt_block(self, language: str) -> dict[str, Any]:
        """指定言語の prompt ブロック。形は {"prompt": {...}}。
        未対応言語は既定言語にフォールバックする。"""

    @abstractmethod
    def supported_languages(self) -> list[str]:
        """利用可能な言語コード一覧（例: ["ja", "en", ...]）。"""

    @abstractmethod
    def resolve_language(self, language: str | None) -> str:
        """要求言語を実在する言語コードに解決する（未対応なら既定言語）。"""

    def config_for(self, language: str | None, persona: str | None = None) -> dict[str, Any]:
        """base + 指定言語の prompt をマージした config（deep copy）を返す。
        persona があれば、ユーザの「プレイスタイル/性格」文を initialize に安全注入する。"""
        cfg = copy.deepcopy(self.base_config())
        lang = self.resolve_language(language)
        cfg.update(copy.deepcopy(self.prompt_block(lang)))
        inject_persona(cfg, persona, lang)
        return cfg


class FilePromptProvider(PromptConfigProvider):
    """configs/agents/ 配下のファイルから設定を読む実装。

    レイアウト:
      <agents_dir>/base.yml
      <agents_dir>/prompts/<lang>.yml   # 各ファイルは {"prompt": {...}}
    """

    def __init__(self, agents_dir: Path, default_language: str = "ja") -> None:
        self.agents_dir = Path(agents_dir)
        self.prompts_dir = self.agents_dir / "prompts"
        self.default_language = default_language

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data or {}

    def base_config(self) -> dict[str, Any]:
        return self._load_yaml(self.agents_dir / "base.yml")

    def supported_languages(self) -> list[str]:
        if not self.prompts_dir.is_dir():
            return []
        return sorted(p.stem for p in self.prompts_dir.glob("*.yml"))

    def resolve_language(self, language: str | None) -> str:
        langs = self.supported_languages()
        if language and language in langs:
            return language
        if self.default_language in langs:
            return self.default_language
        return langs[0] if langs else self.default_language

    def prompt_block(self, language: str) -> dict[str, Any]:
        lang = self.resolve_language(language)
        block = self._load_yaml(self.prompts_dir / f"{lang}.yml")
        # prompts/<lang>.yml は {"prompt": {...}} を持つ。万一欠けても落とさない。
        if "prompt" not in block:
            return {"prompt": block}
        return {"prompt": block["prompt"]}


def _default_agents_dir() -> Path:
    # lobby/ の1つ上が aiwolf-nlp-demo/。その下の configs/agents/。
    return Path(__file__).resolve().parent.parent / "configs" / "agents"


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="base.yml と prompts/<lang>.yml をマージして単一 agent config を出力する（手動検証用）",
    )
    parser.add_argument("--language", "-l", default="ja", help="言語コード（既定: ja）")
    parser.add_argument(
        "--agents-dir",
        default=str(_default_agents_dir()),
        help="configs/agents ディレクトリ",
    )
    parser.add_argument(
        "--out",
        "-o",
        default="",
        help="出力先 YAML パス（未指定なら標準出力）",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="対応言語一覧を表示して終了",
    )
    args = parser.parse_args(argv)

    provider = FilePromptProvider(Path(args.agents_dir))
    if args.list:
        print(" ".join(provider.supported_languages()))
        return 0

    cfg = provider.config_for(args.language)
    text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"wrote {args.out} (language={provider.resolve_language(args.language)})")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
