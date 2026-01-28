def quicksort(arr):
    """
    Реализация алгоритма быстрой сортировки (QuickSort).
    
    Args:
        arr (list): Список чисел для сортировки.
    
    Returns:
        list: Отсортированный список.
    
    Пример:
        >>> numbers = [38, 27, 43, 3, 9, 82, 10]
        >>> sorted_numbers = quicksort(numbers)
        >>> print(sorted_numbers)
        [3, 9, 10, 27, 38, 43, 82]
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

# Пример использования
if __name__ == "__main__":
    numbers = [38, 27, 43, 3, 9, 82, 10]
    sorted_numbers = quicksort(numbers)
    print(f"Исходный список: {numbers}")
    print(f"Отсортированный список: {sorted_numbers}")