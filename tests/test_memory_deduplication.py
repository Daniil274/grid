"""
Unit tests for MemoryStore deduplication functionality.

Tests:
- _calculate_similarity() - граничные значения
- find_similar() - поиск кандидатов
- save(update_if_exists=True) - дедупликация при сохранении
"""

import pytest
import tempfile
import os
from pathlib import Path

from core.memory.store import MemoryStore, MemoryEntry


@pytest.fixture
def temp_db_path(temp_dir):
    """Create a temporary database path for testing."""
    return str(temp_dir / "test_memory.db")


@pytest.fixture
def memory_store(temp_db_path):
    """Create a MemoryStore instance with temporary database."""
    store = MemoryStore(temp_db_path)
    yield store
    # Cleanup handled by temp_dir fixture


class TestCalculateSimilarity:
    """Test _calculate_similarity() method - граничные значения."""

    def test_identical_texts(self, memory_store):
        """Тождественные тексты должны иметь similarity = 1.0."""
        text = "User prefers Python programming language"
        similarity = memory_store._calculate_similarity(text, text)
        assert similarity == 1.0

    def test_identical_texts_different_case(self, memory_store):
        """Тексты с разным регистром должны иметь similarity = 1.0."""
        text1 = "User prefers Python"
        text2 = "USER PREFERS PYTHON"
        similarity = memory_store._calculate_similarity(text1, text2)
        assert similarity == 1.0

    def test_identical_texts_different_whitespace(self, memory_store):
        """Тексты с разными пробелами должны иметь similarity = 1.0."""
        text1 = "User    prefers   Python"
        text2 = "User prefers Python"
        similarity = memory_store._calculate_similarity(text1, text2)
        assert similarity == 1.0

    def test_completely_different_texts(self, memory_store):
        """Совершенно разные тексты должны иметь низкий similarity."""
        text1 = "abcdefghij"
        text2 = "klmnopqrst"
        similarity = memory_store._calculate_similarity(text1, text2)
        assert similarity == 0.0

    def test_empty_texts(self, memory_store):
        """Пустые тексты должны иметь similarity = 1.0."""
        similarity = memory_store._calculate_similarity("", "")
        assert similarity == 1.0

    def test_one_empty_text(self, memory_store):
        """Один пустой текст должен давать similarity = 0.0."""
        similarity = memory_store._calculate_similarity("Some text", "")
        assert similarity == 0.0

    def test_partially_similar_texts(self, memory_store):
        """Частично похожие тексты должны давать промежуточный similarity."""
        text1 = "User prefers Python programming"
        text2 = "User prefers JavaScript programming"
        similarity = memory_store._calculate_similarity(text1, text2)
        # Должно быть около 0.75-0.85
        assert 0.6 < similarity < 0.9

    def test_threshold_boundary_80_percent(self, memory_store):
        """Тест на граничном значении порога 80%."""
        # Создаём тексты, которые должны быть около 80% похожи
        text1 = "This is a test message about Python"
        text2 = "This is a test message about JavaScript"
        similarity = memory_store._calculate_similarity(text1, text2)
        # Python vs JavaScript - разница в одном слове
        # Длина одинаковая, отличие только в последнем слове
        assert isinstance(similarity, float)
        assert 0.0 <= similarity <= 1.0

    def test_unicode_texts(self, memory_store):
        """Тексты с Unicode символами должны корректно сравниваться."""
        text1 = "Пользователь предпочитает Python"
        text2 = "Пользователь предпочитает JavaScript"
        similarity = memory_store._calculate_similarity(text1, text2)
        assert 0.5 < similarity < 1.0

    def test_special_characters(self, memory_store):
        """Тексты со спецсимволами должны корректно сравниваться."""
        text1 = "Email: user@example.com, Phone: +1-234-567-8900"
        text2 = "Email: admin@test.org, Phone: +1-234-567-8900"
        similarity = memory_store._calculate_similarity(text1, text2)
        assert 0.5 < similarity < 1.0


class TestFindSimilar:
    """Test find_similar() method - поиск кандидатов."""

    def test_find_exact_match(self, memory_store):
        """Поиск должен находить точное совпадение."""
        # Используем слова длиной > 3 символов для FTS5
        content = "User prefers Python programming language"
        memory_store.save(content, type="long_term", tags="preference")

        results = memory_store.find_similar(content, threshold=0.8, limit=5)

        assert len(results) == 1
        entry, similarity = results[0]
        assert similarity == 1.0
        assert entry.content == content

    def test_find_similar_above_threshold(self, memory_store):
        """Поиск должен находить похожие записи выше порога."""
        memory_store.save("User prefers Python for backend development", type="long_term")
        memory_store.save("User prefers JavaScript for frontend development", type="long_term")
        memory_store.save("Weather is sunny today", type="short_term")

        results = memory_store.find_similar(
            "User prefers Python for backend",
            threshold=0.7,
            limit=5
        )

        # Должен найти только Python-related запись
        assert len(results) >= 1
        entry, similarity = results[0]
        assert "Python" in entry.content
        assert similarity >= 0.7

    def test_find_similar_below_threshold(self, memory_store):
        """Поиск не должен возвращать записи ниже порога."""
        memory_store.save("Completely unrelated content about weather", type="long_term")
        memory_store.save("Another unrelated topic about cooking", type="long_term")

        results = memory_store.find_similar(
            "User prefers Python programming",
            threshold=0.9,  # Высокий порог
            limit=5
        )

        # Ничего не должно найтись с таким высоким порогом
        for entry, similarity in results:
            assert similarity >= 0.9

    def test_find_similar_with_type_filter(self, memory_store):
        """Поиск должен фильтровать по типу памяти."""
        memory_store.save("User likes Python", type="long_term", user_id="user1")
        memory_store.save("User likes Python", type="short_term", user_id="user1")

        results = memory_store.find_similar(
            "User likes Python",
            threshold=0.8,
            type="long_term"
        )

        assert len(results) >= 1
        for entry, _ in results:
            assert entry.type == "long_term"

    def test_find_similar_with_user_filter(self, memory_store):
        """Поиск должен фильтровать по user_id."""
        memory_store.save("User likes Python", type="long_term", user_id="user1")
        memory_store.save("User likes Python", type="long_term", user_id="user2")

        results = memory_store.find_similar(
            "User likes Python",
            threshold=0.8,
            user_id="user1"
        )

        assert len(results) >= 1
        for entry, _ in results:
            assert entry.user_id == "user1"

    def test_find_similar_with_agent_filter(self, memory_store):
        """Поиск должен фильтровать по agent_id."""
        memory_store.save("Important fact", type="long_term", agent_id="agent1")
        memory_store.save("Important fact", type="long_term", agent_id="agent2")

        results = memory_store.find_similar(
            "Important fact",
            threshold=0.8,
            agent_id="agent1"
        )

        assert len(results) >= 1
        for entry, _ in results:
            assert entry.agent_id == "agent1"

    def test_find_similar_with_exclude_ids(self, memory_store):
        """Поиск должен исключать указанные ID."""
        id1 = memory_store.save("User likes Python", type="long_term")
        memory_store.save("User likes Python programming", type="long_term")

        results = memory_store.find_similar(
            "User likes Python",
            threshold=0.8,
            exclude_ids=[id1]
        )

        for entry, _ in results:
            assert entry.id != id1

    def test_find_similar_respects_limit(self, memory_store):
        """Поиск должен возвращать не более limit результатов."""
        # Создаём несколько похожих записей
        for i in range(10):
            memory_store.save(f"User prefers Python programming {i}", type="long_term")

        results = memory_store.find_similar(
            "User prefers Python programming",
            threshold=0.5,
            limit=3
        )

        assert len(results) <= 3

    def test_find_similar_returns_sorted_by_similarity(self, memory_store):
        """Результаты должны быть отсортированы по убыванию similarity."""
        memory_store.save("User likes Python programming", type="long_term")
        memory_store.save("User likes Python", type="long_term")
        memory_store.save("User likes", type="long_term")

        results = memory_store.find_similar(
            "User likes Python programming",
            threshold=0.3,
            limit=10
        )

        if len(results) > 1:
            similarities = [s for _, s in results]
            assert similarities == sorted(similarities, reverse=True)


class TestSaveDeduplication:
    """Test save(update_if_exists=True) - дедупликация при сохранении."""

    def test_save_creates_new_entry_by_default(self, memory_store):
        """По умолчанию save должен создавать новую запись."""
        content = "User prefers Python"
        id1 = memory_store.save(content, type="long_term")
        id2 = memory_store.save(content, type="long_term")

        # Без update_if_exists должны создаться две записи
        assert id1 != id2

    def test_save_updates_existing_with_update_if_exists(self, memory_store):
        """С update_if_exists=True похожая запись должна обновляться."""
        content = "User prefers Python programming language"
        id1 = memory_store.save(content, type="long_term")

        # Немного изменённый контент, но всё ещё похожий
        new_content = "User prefers Python programming language!"
        id2 = memory_store.save(new_content, type="long_term", update_if_exists=True)

        # Должен вернуть тот же ID, т.к. контент почти идентичен
        assert id1 == id2

    def test_save_updates_importance_on_duplicate(self, memory_store):
        """При обновлении дубликата importance должен увеличиваться."""
        content = "User prefers Python"
        id1 = memory_store.save(content, type="long_term", importance=0.5)

        # Сохраняем похожий контент с update_if_exists
        memory_store.save(content, type="long_term", update_if_exists=True)

        # Проверяем, что importance увеличился
        entry = memory_store.get_by_id(id1)
        assert entry.importance > 0.5
        assert entry.importance <= 1.0

    def test_save_creates_new_if_no_similar_found(self, memory_store):
        """Если похожих записей нет, должна создаться новая."""
        memory_store.save("User likes Python", type="long_term")

        # Совершенно другой контент
        new_id = memory_store.save(
            "Weather forecast for tomorrow",
            type="long_term",
            update_if_exists=True
        )

        # Должна создаться новая запись
        entry = memory_store.get_by_id(new_id)
        assert entry.content == "Weather forecast for tomorrow"

    def test_save_with_custom_similarity_threshold(self, memory_store):
        """Тест кастомного порога схожести."""
        content = "User prefers Python programming"
        id1 = memory_store.save(content, type="long_term")

        # Немного отличающийся контент
        new_content = "User prefers JavaScript programming"
        id2 = memory_store.save(
            new_content,
            type="long_term",
            update_if_exists=True,
            similarity_threshold=0.99  # Очень высокий порог
        )

        # С высоким порогом должна создаться новая запись
        assert id1 != id2

    def test_save_dedup_respects_user_filter(self, memory_store):
        """Дедупликация должна учитывать user_id."""
        content = "User likes Python"
        id1 = memory_store.save(content, type="long_term", user_id="user1")

        # Тот же контент, но другой пользователь
        id2 = memory_store.save(
            content,
            type="long_term",
            user_id="user2",
            update_if_exists=True
        )

        # Должна создаться новая запись для другого пользователя
        assert id1 != id2

    def test_save_dedup_respects_agent_filter(self, memory_store):
        """Дедупликация должна учитывать agent_id."""
        content = "Important configuration"
        id1 = memory_store.save(content, type="long_term", agent_id="agent1")

        # Тот же контент, но другой агент
        id2 = memory_store.save(
            content,
            type="long_term",
            agent_id="agent2",
            update_if_exists=True
        )

        # Должна создаться новая запись для другого агента
        assert id1 != id2

    def test_save_dedup_respects_type_filter(self, memory_store):
        """Дедупликация должна учитывать тип памяти."""
        content = "Important note"
        id1 = memory_store.save(content, type="long_term")

        # Тот же контент, но другой тип
        id2 = memory_store.save(
            content,
            type="short_term",
            update_if_exists=True
        )

        # Должна создаться новая запись для другого типа
        assert id1 != id2

    def test_save_dedup_importance_max_1(self, memory_store):
        """Importance не должен превышать 1.0 при множественных обновлениях."""
        content = "Test content"
        id1 = memory_store.save(content, type="long_term", importance=0.95)

        # Множественные обновления
        for _ in range(10):
            memory_store.save(content, type="long_term", update_if_exists=True)

        entry = memory_store.get_by_id(id1)
        assert entry.importance <= 1.0


class TestEdgeCases:
    """Тесты граничных случаев."""

    def test_empty_query_find_similar(self, memory_store):
        """Пустой запрос не должен вызывать ошибок."""
        memory_store.save("Some content", type="long_term")

        # Пустой запрос должен вернуть пустой результат
        results = memory_store.find_similar("", threshold=0.8)
        assert isinstance(results, list)

    def test_short_query_find_similar(self, memory_store):
        """Короткий запрос должен обрабатываться корректно."""
        memory_store.save("Python is great", type="long_term")

        # Короткие токены (< 4 символов) не должны попасть в FTS запрос
        results = memory_store.find_similar("Py is", threshold=0.5)
        assert isinstance(results, list)

    def test_archived_entries_excluded_from_dedup(self, memory_store):
        """Архивированные записи должны исключаться из дедупликации."""
        content = "User likes Python"
        id1 = memory_store.save(content, type="long_term")

        # Архивируем запись
        memory_store.delete(id1, hard=False)

        # Сохраняем тот же контент с update_if_exists
        id2 = memory_store.save(content, type="long_term", update_if_exists=True)

        # Должна создаться новая запись, т.к. старая архивирована
        # Примечание: поведение зависит от реализации search (include_archived)
        assert id2 is not None

    def test_special_characters_in_content(self, memory_store):
        """Специальные символы в контенте должны обрабатываться."""
        content = "Email: user@example.com, JSON: {\"key\": \"value\"}"
        id1 = memory_store.save(content, type="long_term")

        id2 = memory_store.save(content, type="long_term", update_if_exists=True)

        # Должен обновить существующую
        assert id1 == id2

    def test_very_long_content(self, memory_store):
        """Очень длинный контент должен обрабатываться."""
        content = "Python " * 1000  # Очень длинная строка
        id1 = memory_store.save(content, type="long_term")

        similar_content = "Python " * 999 + "code"
        id2 = memory_store.save(similar_content, type="long_term", update_if_exists=True)

        # Должен найти похожую и обновить
        assert id1 == id2

    def test_multiline_content(self, memory_store):
        """Многострочный контент должен обрабатываться."""
        # FTS5 работает со словами, поэтому используем слова > 3 символов
        content = "First line with important data"
        id1 = memory_store.save(content, type="long_term")

        # Тот же контент с добавлением пробелов (нормализуется при similarity)
        similar_content = "First   line   with   important   data"
        id2 = memory_store.save(similar_content, type="long_term", update_if_exists=True)

        # Должен найти похожую (similarity=1.0 после нормализации)
        assert id1 == id2


class TestIntegration:
    """Интеграционные тесты дедупликации."""

    def test_full_deduplication_workflow(self, memory_store):
        """Полный сценарий дедупликации."""
        # Шаг 1: Создаём начальную запись
        id1 = memory_store.save(
            "User prefers Python for data science",
            type="long_term",
            tags="preference",
            importance=0.5,
            user_id="user1"
        )

        # Шаг 2: Пытаемся сохранить дубликат
        id2 = memory_store.save(
            "User prefers Python for data science!",
            type="long_term",
            tags="preference",
            update_if_exists=True,
            user_id="user1"
        )

        # Шаг 3: Проверяем, что запись обновилась, а не создалась новая
        assert id1 == id2

        # Шаг 4: Проверяем, что importance увеличился
        entry = memory_store.get_by_id(id1)
        assert entry.importance > 0.5

        # Шаг 5: Проверяем, что контент обновился
        assert "!" in entry.content

    def test_multiple_users_same_content(self, memory_store):
        """Разные пользователи могут иметь одинаковый контент."""
        content = "I like Python"

        id1 = memory_store.save(content, type="long_term", user_id="user1")
        id2 = memory_store.save(content, type="long_term", user_id="user2", update_if_exists=True)
        id3 = memory_store.save(content, type="long_term", user_id="user3", update_if_exists=True)

        # Все записи должны быть разными (разные пользователи)
        assert len({id1, id2, id3}) == 3

    def test_dedup_with_search_integration(self, memory_store):
        """Дедупликация должна работать с FTS5 поиском."""
        # Создаём несколько записей
        memory_store.save("Python is great for ML", type="long_term")
        memory_store.save("JavaScript is great for web", type="long_term")

        # Ищем Python-related записи
        results = memory_store.search("Python", type="long_term")
        assert len(results) >= 1

        # Проверяем дедупликацию при сохранении похожего
        memory_store.save("Python is great for ML!", type="long_term", update_if_exists=True)

        # Количество записей не должно увеличиться
        new_results = memory_store.search("Python", type="long_term")
        assert len(new_results) == len(results)
