import sys
import time

def quicksort(arr):
    """
    Реализация алгоритма быстрой сортировки (QuickSort).
    
    Args:
        arr (list): Список чисел для сортировки.
    
    Returns:
        list: Отсортированный список.
    """
    if len(arr) <= 1:
        return arr
    
    pivot = arr[-1]  # Выбираем последний элемент как опорный
    less = []        # Элементы меньше опорного
    equal = []       # Элементы равные опорному
    greater = []     # Элементы больше опорного
    
    for num in arr:
        if num < pivot:
            less.append(num)
        elif num == pivot:
            equal.append(num)
        else:
            greater.append(num)
    
    # Рекурсивно сортируем подсписки и объединяем
    return quicksort(less) + equal + quicksort(greater)

# Тестовые сценарии
def test_scenarios():
    print("=== Тестирование различных сценариев ===")
    
    # 1. Нормальный случай
    print("\n1. Нормальный случай:")
    numbers = [38, 27, 43, 3, 9, 82, 10]
    sorted_numbers = quicksort(numbers)
    print(f"Исходный: {numbers}")
    print(f"Отсортированный: {sorted_numbers}")
    
    # 2. Пустой список
    print("\n2. Пустой список:")
    empty = []
    print(f"Результат: {quicksort(empty)}")
    
    # 3. Список из одного элемента
    print("\n3. Список из одного элемента:")
    single = [5]
    print(f"Результат: {quicksort(single)}")
    
    # 4. Уже отсортированный список
    print("\n4. Уже отсортированный список:")
    sorted_list = list(range(10))
    try:
        result = quicksort(sorted_list)
        print(f"Успешно отсортировано {len(result)} элементов")
    except Exception as e:
        print(f"Ошибка: {type(e).__name__}: {e}")
    
    # 5. Обратно отсортированный список
    print("\n5. Обратно отсортированный список:")
    reverse_sorted = list(range(1000, 0, -1))
    try:
        start = time.time()
        result = quicksort(reverse_sorted)
        end = time.time()
        print(f"Успешно отсортировано {len(result)} элементов за {end-start:.4f} секунд")
    except Exception as e:
        print(f"Ошибка: {type(e).__name__}: {e}")
    
    # 6. Список с повторяющимися элементами
    print("\n6. Список с повторяющимися элементами:")
    duplicates = [5, 2, 5, 3, 5, 1, 5]
    print(f"Результат: {quicksort(duplicates)}")
    
    # 7. Большой список
    print("\n7. Большой список (10000 элементов):")
    import random
    large_list = [random.randint(0, 100000) for _ in range(10000)]
    try:
        start = time.time()
        result = quicksort(large_list)
        end = time.time()
        print(f"Успешно отсортировано {len(result)} элементов за {end-start:.4f} секунд")
        print(f"Проверка сортировки: {result[:5]}...{result[-5:]}")
    except Exception as e:
        print(f"Ошибка: {type(e).__name__}: {e}")
    
    # 8. Проверка глубины рекурсии
    print("\n8. Проверка глубины рекурсии:")
    sys.setrecursionlimit(10000)  # Увеличим лимит для теста
    deep_list = list(range(5000))
    try:
        start = time.time()
        result = quicksort(deep_list)
        end = time.time()
        print(f"Успешно отсортировано {len(result)} элементов за {end-start:.4f} секунд")
        print(f"Использовано рекурсий: примерно {len(deep_list)}")
    except RecursionError as e:
        print(f"RecursionError: {e}")
        print(f"Текущий лимит рекурсии: {sys.getrecursionlimit()}")
    except Exception as e:
        print(f"Другая ошибка: {type(e).__name__}: {e}")
    
    # 9. Нечисловые данные
    print("\n9. Нечисловые данные (смешанные типы):")
    mixed = [3, "2", 1.5, 4]
    try:
        result = quicksort(mixed)
        print(f"Результат: {result}")
    except Exception as e:
        print(f"Ошибка: {type(e).__name__}: {e}")
    
    # 10. Очень большой список (потенциальное переполнение памяти)
    print("\n10. Очень большой список (50000 элементов):")
    try:
        very_large = list(range(50000))
        start = time.time()
        result = quicksort(very_large)
        end = time.time()
        print(f"Успешно отсортировано {len(result)} элементов за {end-start:.4f} секунд")
        
        # Проверка использования памяти
        import os
        import psutil
        process = psutil.Process(os.getpid())
        print(f"Использование памяти: {process.memory_info().rss / 1024 / 1024:.2f} MB")
    except ImportError:
        print("psutil не установлен, пропускаем проверку памяти")
    except Exception as e:
        print(f"Ошибка: {type(e).__name__}: {e}")

if __name__ == "__main__":
    test_scenarios()