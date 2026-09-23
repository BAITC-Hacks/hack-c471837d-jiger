"""Must have 3: условия покупки и контакты берутся только из data/terms.md.

Модель не вызывается: проверяется сам блок и то, что он доходит до системного промпта.
"""

import pytest
from openai.types.chat import ChatCompletion

from app import agent, terms


class CapturingOpenAI:
    """Возвращает готовый ответ и запоминает системный промпт, с которым его позвали."""

    prompts: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.chat = self
        self.completions = self

    def __enter__(self) -> "CapturingOpenAI":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def create(self, **kwargs) -> ChatCompletion:
        CapturingOpenAI.prompts.append(kwargs["messages"][0]["content"])
        return ChatCompletion.model_validate({
            "id": "chatcmpl-fake", "object": "chat.completion", "created": 0, "model": "fake",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Готово"},
            }],
        })


@pytest.fixture(autouse=True)
def fresh_terms_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terms, "_cache", {"key": None, "text": ""})


def test_repository_terms_answer_the_required_questions() -> None:
    """В ТЗ названы оплата, доставка и минимальная партия."""
    note = terms.terms_note().casefold()

    assert note != terms.MISSING_TERMS_NOTE.casefold(), "data/terms.md должен быть в репозитории."
    for topic in ("оплат", "доставк", "самовывоз", "возврат", "контакт"):
        assert topic in note, f"В условиях нет раздела «{topic}»."
    assert "не выдумывай" in note, "Блок обязан запрещать модели домысливать условия."


def test_missing_file_makes_the_assistant_refuse_instead_of_inventing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terms, "TERMS_PATH", tmp_path / "terms.md")

    assert terms.load_terms() == ""
    note = terms.terms_note()
    assert note == terms.MISSING_TERMS_NOTE
    assert "ничего не выдумывай" in note.casefold()


def test_unreadable_file_is_treated_as_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "terms.md"
    directory.mkdir()
    monkeypatch.setattr(terms, "TERMS_PATH", directory)

    assert terms.load_terms() == ""
    assert terms.terms_note() == terms.MISSING_TERMS_NOTE


def test_edited_file_is_picked_up_without_a_restart(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "terms.md"
    path.write_text("## Доставка\nБесплатно от 30 000 ₸.", encoding="utf-8")
    monkeypatch.setattr(terms, "TERMS_PATH", path)
    assert "30 000" in terms.load_terms()

    path.write_text("## Доставка\nБесплатно от 50 000 ₸ (условия обновлены).", encoding="utf-8")

    assert "50 000" in terms.load_terms(), "Правка файла должна подхватываться на лету."
    assert "30 000" not in terms.load_terms()


def test_oversized_terms_are_truncated(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "terms.md"
    path.write_text("я" * (terms.MAX_TERMS_CHARS + 500), encoding="utf-8")
    monkeypatch.setattr(terms, "TERMS_PATH", path)

    assert len(terms.load_terms()) == terms.MAX_TERMS_CHARS


def test_terms_reach_the_system_prompt(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = "ТЕСТОВОЕ-УСЛОВИЕ-4242"
    path = tmp_path / "terms.md"
    path.write_text(f"## Оплата\n{marker}", encoding="utf-8")
    monkeypatch.setattr(terms, "TERMS_PATH", path)
    monkeypatch.setattr(agent, "OpenAI", CapturingOpenAI)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    CapturingOpenAI.prompts = []

    result = agent.run_agent("Какие условия доставки?", [])

    assert result["reply"] == "Готово"
    assert CapturingOpenAI.prompts, "Агент должен был обратиться к модели."
    assert marker in CapturingOpenAI.prompts[0], "Условия покупки не попали в системный промпт."


def test_prompt_says_terms_are_missing_when_the_file_is_absent(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terms, "TERMS_PATH", tmp_path / "terms.md")
    monkeypatch.setattr(agent, "OpenAI", CapturingOpenAI)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    CapturingOpenAI.prompts = []

    agent.run_agent("Какие условия доставки?", [])

    assert terms.MISSING_TERMS_NOTE in CapturingOpenAI.prompts[0]
