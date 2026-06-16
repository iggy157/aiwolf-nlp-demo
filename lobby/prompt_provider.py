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

    def config_for(self, language: str | None) -> dict[str, Any]:
        """base + 指定言語の prompt をマージした config（deep copy）を返す。"""
        cfg = copy.deepcopy(self.base_config())
        lang = self.resolve_language(language)
        cfg.update(copy.deepcopy(self.prompt_block(lang)))
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
