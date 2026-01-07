# Go Style Guide

## 1. Naming Conventions
- **Variables/Functions**: Use `camelCase` (e.g., `userName`, `calculateTotal`).
- **Exported Members**: Use `PascalCase` (e.g., `NewUser`).
- **Interfaces**: Suffix with `er` if single method (e.g., `Reader`, `Writer`).
- **Package Names**: Short, lowercase, singular (e.g., `auth` not `authentication`).

## 2. Formatting
- Use `gofmt` or `goimports` for all formatting.
- Group imports: Standard library first, then third-party.

## 3. Best Practices
- **Error Handling**: Always check errors. Do not use `_` to ignore errors unless justified.
  ```go
  if err != nil {
      return fmt.Errorf("context: %w", err)
  }
  ```
- **Context**: Pass `context.Context` as the first argument to functions doing I/O.
- **Defer**: Use `defer` for resource cleanup close to allocation.

## 4. Forbidden Patterns
- Avoid `init()` functions where possible.
- Avoid package-level variables.
- Do not panic in libraries; return errors instead.

