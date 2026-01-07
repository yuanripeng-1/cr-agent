# Node.js / JavaScript Style Guide

## 1. Naming Conventions
- **Variables/Functions**: `camelCase`.
- **Classes**: `PascalCase`.
- **Files**: `kebab-case` (e.g., `user-controller.js`) or `camelCase`.

## 2. Formatting
- Use 2 spaces for indentation.
- Use single quotes `'` for strings.
- Always use semicolons `;`.

## 3. Best Practices
- **Async/Await**: Prefer `async/await` over raw Promises or callbacks.
- **Const/Let**: Use `const` by default; use `let` only when reassignment is needed. Never use `var`.
- **Destructuring**: Use object/array destructuring.
  ```javascript
  const { id, name } = user;
  ```

## 4. Forbidden Patterns
- No `console.log` in production code.
- No `==` (always use `===`).
- Avoid global variables.

