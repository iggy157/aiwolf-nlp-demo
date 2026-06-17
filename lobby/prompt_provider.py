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
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml


# ユーザが編集できるリクエスト別プロンプトの最大長（プロンプト肥大・コスト暴走の防止）。
MAX_PROMPT_CHARS = 4000

# 編集可能なリクエスト（agent-llm の prompt キー）。
REQUESTS = [
    "initialize", "daily_initialize", "talk", "whisper", "daily_finish",
    "divine", "guard", "vote", "attack",
]

# 既定プロンプトに現れるループ（複合変数）の“厳密な”Jinja 文字列。
# これらを丸ごと1つのトークンに対応させ、ユーザにはループ(Jinja)を書かせない。
# 文字列は configs/agents/prompts/*.yml の該当ループと完全一致させること（往復変換のため）。
_TALK_LOOP = "{% for w in talk_history[sent_talk_count:] -%}\n{{ w.agent }}: {{ w.text }}\n{% endfor %}"
_WHISPER_LOOP = "{% for w in whisper_history[sent_whisper_count:] -%}\n{{ w.agent }}: {{ w.text }}\n{% endfor %}"
# template = 既定プロンプト中の素のループ（往復変換のマッチ用、info あり前提）。
_ALIVE_LOOP = "{% for k, v in info.status_map.items() -%}\n{%- if v == 'ALIVE' -%}\n{{ k }}\n{% endif -%}\n{%- endfor %}"
# runtime = ユーザがどのリクエストに入れても安全な版（info が None でも空に落ちる）。
_ALIVE_LOOP_SAFE = "{% for k, v in (info.status_map if info else {}).items() -%}\n{%- if v == 'ALIVE' -%}\n{{ k }}\n{% endif -%}\n{%- endfor %}"

# 変数カタログ。各 (key, token, template_jinja, runtime_jinja, sample, composite)。
#   token        : エディタ上のやさしい記法（ユーザは生 Jinja を打たない）
#   template_jinja: 既定プロンプト中の素の式（トークン化＝逆変換のマッチ用）
#   runtime_jinja : 実行時に注入する式（None セーフ）。agent-llm がこれを描画する
#   sample        : プレビュー用のサンプル値
#   composite     : ループ等のブロック変数か
def _nz(attr: str) -> str:  # None セーフな実行時式
    return f"{{{{ {attr} if {attr} is not none else '' }}}}"


_VARS: list[dict[str, Any]] = [
    {"key": "name", "token": "{name}", "template": "{{ info.agent }}", "runtime": "{{ info.agent }}", "sample": "ミナト", "composite": False},
    {"key": "role", "token": "{role}", "template": "{{ role.value }}", "runtime": "{{ role.value }}", "sample": "占い師", "composite": False},
    {"key": "day", "token": "{day}", "template": "{{ info.day }}", "runtime": "{{ info.day }}", "sample": "2", "composite": False},
    {"key": "profile", "token": "{profile}", "template": "{{ info.profile }}", "runtime": _nz("info.profile"), "sample": "おっとりした性格", "composite": False},
    {"key": "divine_result", "token": "{divine_result}", "template": "{{ info.divine_result }}", "runtime": _nz("info.divine_result"), "sample": "ミナトは人間でした", "composite": False},
    {"key": "medium_result", "token": "{medium_result}", "template": "{{ info.medium_result }}", "runtime": _nz("info.medium_result"), "sample": "タクミは人狼でした", "composite": False},
    {"key": "executed_agent", "token": "{executed_agent}", "template": "{{ info.executed_agent }}", "runtime": _nz("info.executed_agent"), "sample": "ケンジ", "composite": False},
    {"key": "attacked_agent", "token": "{attacked_agent}", "template": "{{ info.attacked_agent }}", "runtime": _nz("info.attacked_agent"), "sample": "リン", "composite": False},
    {"key": "vote_list", "token": "{vote_list}", "template": "{{ info.vote_list }}", "runtime": _nz("info.vote_list"), "sample": "ミナト→タクミ", "composite": False},
    {"key": "attack_vote_list", "token": "{attack_vote_list}", "template": "{{ info.attack_vote_list }}", "runtime": _nz("info.attack_vote_list"), "sample": "（襲撃投票）", "composite": False},
    {"key": "remain_talk", "token": "{remain_talk}", "template": "{{ info.remain_count }}", "runtime": _nz("info.remain_count"), "sample": "3", "composite": False},
    {"key": "remain_skip", "token": "{remain_skip}", "template": "{{ info.remain_skip }}", "runtime": _nz("info.remain_skip"), "sample": "2", "composite": False},
    {"key": "talk_history", "token": "{talk_history}", "template": _TALK_LOOP, "runtime": _TALK_LOOP, "sample": "ミナト: みなさんこんにちは\nタクミ: 怪しい人がいますね", "composite": True},
    {"key": "whisper_history", "token": "{whisper_history}", "template": _WHISPER_LOOP, "runtime": _WHISPER_LOOP, "sample": "ミナト: 今夜はケンジを襲おう", "composite": True},
    {"key": "alive_agents", "token": "{alive_agents}", "template": _ALIVE_LOOP, "runtime": _ALIVE_LOOP_SAFE, "sample": "ミナト\nタクミ\nケンジ\nリン", "composite": True},
]
_VARS_BY_KEY = {v["key"]: v for v in _VARS}

# リクエストごとに「使える変数」（ピッカー表示用）。
REQUEST_VARS: dict[str, list[str]] = {
    "initialize": ["name", "role", "profile"],
    "daily_initialize": ["day", "medium_result", "divine_result", "executed_agent", "attacked_agent", "vote_list", "attack_vote_list"],
    "talk": ["talk_history", "name", "role", "day", "remain_talk", "remain_skip"],
    "whisper": ["whisper_history", "name", "role"],
    "daily_finish": ["talk_history", "day", "medium_result", "divine_result", "executed_agent", "attacked_agent", "vote_list", "attack_vote_list"],
    "divine": ["alive_agents", "name", "role", "day"],
    "guard": ["alive_agents", "name", "role", "day"],
    "vote": ["alive_agents", "name", "role", "day"],
    "attack": ["alive_agents", "name", "role", "day", "attack_vote_list"],
}

# 長い順（部分一致での誤置換を避ける）に並べたトークン/式。
_VARS_LONGEST = sorted(_VARS, key=lambda v: -len(v["token"]))
_VARS_TPL_LONGEST = sorted(_VARS, key=lambda v: -len(v["template"]))
_COND_RE = re.compile(r"\{%-?.*?-?%\}", re.DOTALL)  # 残った条件タグ除去用

# ── 条件分岐ブロック（UIのブロックエディタ専用）──────────────────────────
# ユーザには生 Jinja を書かせず、ブロックエディタが下のブロックトークンを挿入する:
#   {if:role=WEREWOLF} ... {endif}      （役職＝人狼）
#   {if:day>=2} ... {endif}             （日数が2以上）
# これらだけを安全な Jinja に変換する。変数キーは whitelist の式に、演算子は許可集合に、
# 値は型ごとに厳格制限（enum=許可値のみ / number=数字のみ）するため、式インジェクションは起きない。
BRANCH_VARS: dict[str, dict[str, Any]] = {
    # key: {type, expr=安全な実行時式, ops=許可演算子, values=enumの許可値}
    "role": {"type": "enum", "expr": "role.value", "ops": ["=", "!="],
             "values": ["WEREWOLF", "POSSESSED", "SEER", "BODYGUARD", "VILLAGER", "MEDIUM"]},
    "day": {"type": "number", "expr": "(info.day if info and info.day is not none else 0)", "ops": ["=", "!=", ">=", "<=", ">", "<"]},
    "turn": {"type": "number", "expr": "(talk_history | length)", "ops": ["=", "!=", ">=", "<=", ">", "<"]},
    "remain_talk": {"type": "number", "expr": "(info.remain_count if info and info.remain_count is not none else 0)", "ops": ["=", "!=", ">=", "<=", ">", "<"]},
}
_ENUM_VALUE_RE = re.compile(r"^[A-Za-z0-9_]+$")  # enum値の許可文字（インジェクション防止）
_NUM_VALUE_RE = re.compile(r"^[0-9]+$")           # number値の許可文字
# 演算子は長いものを先に（>= を > より先に）マッチさせる。
_IF_RE = re.compile(r"\{(if|elif):([a-z_]+)(!=|>=|<=|=|>|<)([^}]*)\}")


def _branch_expr(var: str, op: str, value: str) -> str | None:
    """{if:var op value} の安全な比較式を返す。未知の変数/演算子/不正な値なら None。"""
    spec = BRANCH_VARS.get(var)
    if not spec or op not in spec.get("ops", []):
        return None
    jop = "==" if op == "=" else op  # トークンの = は Jinja の ==
    if spec["type"] == "enum":
        if value not in spec.get("values", []):
            return None
        return f"{spec['expr']} {jop} '{value}'"
    if spec["type"] == "number":
        if not _NUM_VALUE_RE.match(value or ""):
            return None
        return f"{spec['expr']} {jop} {value}"
    return None


def _blocks_to_jinja(text: str) -> str:
    """ブロックトークン → 実 Jinja 制御タグ。無効な分岐はそのまま（リテラル）残す。"""
    def repl(m: "re.Match[str]") -> str:
        kw, var, op, value = m.group(1), m.group(2), m.group(3), m.group(4)
        expr = _branch_expr(var, op, value)
        return m.group(0) if expr is None else f"{{% {kw} {expr} %}}"

    out = _IF_RE.sub(repl, text)
    return out.replace("{else}", "{% else %}").replace("{endif}", "{% endif %}")


def _blocks_to_preview(text: str) -> str:
    """ブロックトークン → 読みやすい目印（プレビュー用、Jinja 描画はしない）。"""
    def repl(m: "re.Match[str]") -> str:
        kw, var, op, value = m.group(1), m.group(2), m.group(3), m.group(4)
        if _branch_expr(var, op, value) is None:
            return m.group(0)
        return f"⟦{kw} {var} {op} {value}⟧"

    out = _IF_RE.sub(repl, text)
    return out.replace("{else}", "⟦else⟧").replace("{endif}", "⟦/if⟧")


def jinja_compiles(jinja_text: str) -> bool:
    """生成した Jinja が構文的に正しいか（if/endif の対応漏れ等）を検証する。
    parse のみ＝描画しないので安全。壊れたプロンプトでエージェントを落とさないための関門。"""
    try:
        import jinja2
        jinja2.Environment().parse(jinja_text)  # コンパイルのみ（実行なし）
        return True
    except ImportError:
        # jinja2 が無い環境では構造の釣り合いだけ確認（best-effort）。
        return (
            jinja_text.count("{% if ") == jinja_text.count("{% endif %}")
            and jinja_text.count("{% for ") == jinja_text.count("{% endfor %}")
        )
    except Exception:
        return False


def _escape_jinja(text: str) -> str:
    """ユーザ文中の生 Jinja デリミタを無効化（SSTI 防止）。"""
    return (
        text.replace("{{", "{ {").replace("}}", "} }")
        .replace("{%", "{ %").replace("%}", "% }")
        .replace("{#", "{ #").replace("#}", "# }")
    )


def tokens_to_jinja(text: str) -> str:
    """ユーザのトークン入りプロンプト → 実行時 Jinja。
    1) 生 Jinja を無効化（whitelist 以外の live な式を排除）→ 2) 分岐ブロックを安全な制御タグへ
    → 3) 変数トークンを runtime 式へ。"""
    out = _escape_jinja(text)
    out = _blocks_to_jinja(out)
    for v in _VARS_LONGEST:
        out = out.replace(v["token"], v["runtime"])
    return out


def jinja_to_tokens(text: str) -> str:
    """既定プロンプト(Jinja) → エディタ表示用のトークン文（ユーザに Jinja を見せない）。
    ループ/スカラーをトークン化し、残った条件タグ({% if %}等)は除去する。"""
    out = text
    for v in _VARS_TPL_LONGEST:  # 長い(=ループ)を先に置換
        out = out.replace(v["template"], v["token"])
    out = _COND_RE.sub("", out)            # 残る条件タグを除去
    out = re.sub(r"\n{3,}", "\n\n", out)   # 連続改行を整える
    return out.strip("\n")


def preview_prompt(text: str) -> str:
    """エディタのプレビュー用: 分岐は目印に、変数はサンプル値に解決した文を返す。"""
    out = _escape_jinja(text or "")
    out = _blocks_to_preview(out)
    for v in _VARS_LONGEST:
        out = out.replace(v["token"], v["sample"])
    return out


def apply_custom_prompts(cfg: dict[str, Any], custom_prompts: dict[str, str] | None) -> None:
    """ユーザが編集したリクエスト別プロンプトで cfg["prompt"] を上書きする（in-place）。
    各テキストはトークン→実行時 Jinja に変換して注入。空/対象外キーは無視。"""
    if not custom_prompts:
        return
    prompt = cfg.get("prompt")
    if not isinstance(prompt, dict):
        return
    for req, text in custom_prompts.items():
        if req not in REQUESTS:
            continue
        text = (text or "").strip()
        if not text:
            continue
        rendered = tokens_to_jinja(text[:MAX_PROMPT_CHARS])
        # 分岐の対応漏れ等で壊れた Jinja は注入せず既定のままにする（エージェントを落とさない）。
        if jinja_compiles(rendered):
            prompt[req] = rendered


def variable_catalog() -> dict[str, Any]:
    """フロントのピッカー用: 変数一覧（key/token/composite）とリクエスト別の使える変数。"""
    return {
        "vars": [{"key": v["key"], "token": v["token"], "composite": v["composite"]} for v in _VARS],
        "by_request": REQUEST_VARS,
        "requests": REQUESTS,
        # 分岐ビルダー用: 分岐できる変数（型/演算子/enum許可値）。フロントは値・演算子を UI で出す。
        "branch_vars": [
            {"key": k, "type": spec["type"], "ops": spec.get("ops", []), "values": spec.get("values", [])}
            for k, spec in BRANCH_VARS.items()
        ],
        "max_chars": MAX_PROMPT_CHARS,
    }


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

    def config_for(
        self, language: str | None, custom_prompts: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """base + 指定言語の prompt をマージした config（deep copy）を返す。
        custom_prompts があれば、ユーザが編集したリクエスト別プロンプトで上書きする。"""
        cfg = copy.deepcopy(self.base_config())
        lang = self.resolve_language(language)
        cfg.update(copy.deepcopy(self.prompt_block(lang)))
        apply_custom_prompts(cfg, custom_prompts)
        return cfg

    def defaults_as_tokens(self, language: str | None) -> dict[str, str]:
        """指定言語の既定プロンプトを、エディタ表示用のトークン文に変換して返す。"""
        lang = self.resolve_language(language)
        block = self.prompt_block(lang).get("prompt", {})
        return {req: jinja_to_tokens(str(block.get(req, ""))) for req in REQUESTS if req in block}


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
