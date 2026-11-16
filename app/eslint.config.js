import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'node_modules', 'src-tauri', 'test-results', 'playwright-report'] },
  js.configs.recommended,
  ...tseslint.configs.strict,
  {
    files: ['**/*.{ts,tsx,js,mjs}'],
    languageOptions: { ecmaVersion: 2023, sourceType: 'module', globals: { ...globals.browser, ...globals.node } },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',
      eqeqeq: ['error', 'always'],
      'no-console': 'error',
      'no-eval': 'error',
      'no-implied-eval': 'error',
      'no-new-func': 'error',
      '@typescript-eslint/no-non-null-assertion': 'error',
      '@typescript-eslint/consistent-type-imports': 'error',
    },
  },
  {
    // The app's own code: text reaches the page as text only, and nothing leaves except through the home client.
    files: ['src/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': [
        'error',
        { selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']", message: 'Render text as text. No dangerouslySetInnerHTML.' },
        { selector: "MemberExpression[property.name=/^(innerHTML|outerHTML)$/]", message: 'Render text as text. No innerHTML.' },
        { selector: "CallExpression[callee.property.name='insertAdjacentHTML']", message: 'Render text as text.' },
        { selector: "NewExpression[callee.name=/^(WebSocket|EventSource|XMLHttpRequest)$/]", message: 'The app talks only to the home client (polling).' },
      ],
      'no-restricted-globals': ['error', { name: 'fetch', message: 'Only src/home/http.ts may call fetch.' }],
    },
  },
  { files: ['src/home/http.ts'], rules: { 'no-restricted-globals': 'off' } },
  { files: ['scripts/**', 'e2e/**'], rules: { 'no-console': 'off', 'react-hooks/rules-of-hooks': 'off' } }, // Playwright's fixtures have a function named use
);
