# Java Style Guide

## 1. Naming Conventions
- **Classes**: `PascalCase`.
- **Methods/Variables**: `camelCase`.
- **Constants**: `UPPER_SNAKE_CASE`.
- **Packages**: Lowercase, reverse domain (e.g., `com.company.project`).

## 2. Formatting
- Use 4 spaces for indentation.
- Opening braces on the same line (`K&R` style).

## 3. Best Practices
- **Optional**: Use `Optional<T>` for return types that might be empty; avoid returning `null`.
- **Immutability**: Prefer `final` fields and immutable collections.
- **Streams**: Use Stream API for collection processing where readable.
- **Logging**: Use SLF4J placeholders (`log.info("User: {}", id)`), not string concatenation.

## 4. Forbidden Patterns
- No `System.out.println` (use a logger).
- No raw types (e.g., use `List<String>`, not `List`).
- No empty catch blocks.

