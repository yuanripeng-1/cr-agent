# Python Style Guide

## 1. Naming Conventions
- **Variables/Functions**: Use `snake_case` (e.g., `user_name`, `calculate_total`).
- **Classes**: Use `PascalCase` (e.g., `UserAccount`).
- **Constants**: Use `UPPER_CASE` (e.g., `MAX_RETRIES`).
- **Private Members**: Prefix with `_` (e.g., `_internal_helper`).

## 2. Formatting
- Use 4 spaces for indentation (no tabs).
- Limit line length to 88 characters (Black style).
- Sort imports: Standard library > Third party > Local application (use `isort`).

## 3. Best Practices
- **Type Hints**: Explicitly type hints for function arguments and return values.
  ```python
  def connect(host: str, port: int) -> bool: ...
  ```
- **Docstrings**: Use Google-style docstrings for all public modules, classes, and functions.
- **Exceptions**: Use custom exceptions rather than generic `Exception`.
- **f-strings**: Prefer f-strings over `%` formatting or `.format()`.

## 4. Forbidden Patterns
- No wildcard imports (`from module import *`).
- No mutable default arguments (`def func(a=[])`).
- Avoid `pass` in exception handling (catch and log at minimum).

